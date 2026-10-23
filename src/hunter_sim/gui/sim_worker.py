"""仿真工作线程（V2.2.0，PySide6）。

`SimWorker(QThread)` 独占 `carla.Client` 与 world，所有阻塞 CARLA 调用只在本线程
执行（遵循"阻塞调用异步化""线程模型"规范）；GUI 线程通过 `request_*` 方法投递
命令到内部队列，本线程在 tick 循环中消费，并以 Qt 信号回推结果/画面/状态。

CARLA 与 PySide6 均为可选依赖：本模块仅在桌面客户端运行时被导入。
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, Signal

from hunter_sim.common.models import VehicleControlCommand
from hunter_sim.gui.camera_bridge import image_to_rgb_array
from hunter_sim.gui.sim_presets import MOUNT_PRESETS, build_rgb_camera_config, pick_spawn_point


class SimWorker(QThread):
    """持有 CARLA 连接与仿真主循环的工作线程。"""

    connected = Signal(str)            # 服务器版本
    connectionFailed = Signal(str)
    disconnected = Signal()
    frameReady = Signal(int, object)   # (sensor_id, QImage)
    stateUpdated = Signal(object)      # dict（自车位姿/速度/挡位等）
    logLine = Signal(str)
    errorOccurred = Signal(str)
    actorsChanged = Signal(object)     # list[int]（受管 actor id）
    sensorsChanged = Signal(object)    # list[int]（相机传感器 id，供 SOURCE 下拉）
    blueprintsReady = Signal(object)   # list[str]（蓝图过滤结果）
    mapChanged = Signal(str)

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._commands: "queue.Queue[Any]" = queue.Queue()
        self._stop_event = threading.Event()
        self._carla: Any = None
        self._client: Any = None
        self._world: Any = None
        self._blueprint_library: Any = None
        self._ego: Any = None
        self._actors: dict[int, Any] = {}
        self._camera_sensors: dict[int, Any] = {}
        self._running = False
        self._sync_mode = True
        self._last_state_emit = 0.0

    # ── 命令投递（GUI 线程调用，线程安全）────────────────────────────────
    def request_connect(self, host: str, port: int, timeout: float) -> None:
        self._commands.put(("connect", (host, port, timeout)))

    def request_load_map(self, map_name: str) -> None:
        self._commands.put(("load_map", (map_name,)))

    def request_import_xodr(self, path: str) -> None:
        self._commands.put(("import_xodr", (path,)))

    def request_blueprints(self, prefix: str) -> None:
        self._commands.put(("blueprints", (prefix,)))

    def request_spawn(self, blueprint: str, x: float, y: float, z: float, use_spawn_point: bool = False) -> None:
        self._commands.put(("spawn", (blueprint, x, y, z, use_spawn_point)))

    def request_attach_sensor(
        self,
        bp_name: str,
        preset: str,
        width: int,
        height: int,
        fov: float,
        parent_id: int,
        rel_x: float,
        rel_y: float,
        rel_z: float,
        pitch: float,
    ) -> None:
        self._commands.put(
            ("attach_sensor", (bp_name, preset, width, height, fov, parent_id, rel_x, rel_y, rel_z, pitch))
        )

    def request_set_autopilot(self, enabled: bool) -> None:
        self._commands.put(("autopilot", (enabled,)))

    def request_control(self, command: VehicleControlCommand) -> None:
        self._commands.put(("control", (command,)))

    def request_set_sync(self, enabled: bool) -> None:
        self._commands.put(("set_sync", (enabled,)))

    def request_start(self) -> None:
        self._commands.put(("start", ()))

    def request_pause(self) -> None:
        self._commands.put(("pause", ()))

    def request_stop(self) -> None:
        self._commands.put(("stop", ()))

    def request_clear(self) -> None:
        self._commands.put(("clear", ()))

    def shutdown(self) -> None:
        """请求线程退出（GUI 关闭时调用）。"""
        self._stop_event.set()

    # ── 线程主体 ──────────────────────────────────────────────────────────
    def run(self) -> None:  # noqa: C901  事件循环，分支多属正常
        """消费命令队列并按帧推进仿真。"""
        while not self._stop_event.is_set():
            self._drain_commands()
            if self._running and self._world is not None:
                try:
                    if self._sync_mode:
                        self._world.tick()
                    self._emit_state()
                except Exception as exc:  # noqa: BLE001
                    self.errorOccurred.emit(f"tick 失败：{exc}")
                    self._running = False
            time.sleep(0.016)  # ~60Hz 上限
        self._teardown()

    def _drain_commands(self) -> None:
        while True:
            try:
                name, args = self._commands.get_nowait()
            except queue.Empty:
                return
            handler = getattr(self, f"_do_{name}", None)
            if handler is None:
                continue
            try:
                handler(*args)
            except Exception as exc:  # noqa: BLE001
                self.errorOccurred.emit(f"{name} 失败：{type(exc).__name__}: {exc}")

    # ── 命令处理（均在本线程）─────────────────────────────────────────────
    def _do_connect(self, host: str, port: int, timeout: float) -> None:
        try:
            import carla  # noqa: PLC0415

            self._carla = carla
            self._client = carla.Client(host, port)
            self._client.set_timeout(timeout)
            version = self._client.get_server_version()
            self._world = self._client.get_world()
            self._blueprint_library = self._world.get_blueprint_library()
            self.connected.emit(version)
            self.logLine.emit(f"已连接 CARLA {version} @ {host}:{port}")
            self._apply_sync_settings()
        except Exception as exc:  # noqa: BLE001
            self._client = None
            self.connectionFailed.emit(str(exc))

    def _do_load_map(self, map_name: str) -> None:
        if self._client is None:
            self.errorOccurred.emit("未连接 CARLA")
            return
        self._world = self._client.load_world(map_name)
        self._blueprint_library = self._world.get_blueprint_library()
        self._reset_scene_state()
        self._apply_sync_settings()  # load_world 返回新 world 会重置为异步，需重下发
        self.mapChanged.emit(map_name)
        self.logLine.emit(f"已加载地图 {map_name}")

    def _do_import_xodr(self, path: str) -> None:
        if self._client is None:
            self.errorOccurred.emit("未连接 CARLA")
            return
        content = Path(path).read_text(encoding="utf-8")
        self._world = self._client.generate_opendrive_world(content)
        self._blueprint_library = self._world.get_blueprint_library()
        self._reset_scene_state()
        self._apply_sync_settings()  # generate_opendrive_world 返回新 world，需重下发
        self.mapChanged.emit(Path(path).name)
        self.logLine.emit(f"已导入 OpenDRIVE 地图 {Path(path).name}")

    def _do_blueprints(self, prefix: str) -> None:
        if self._blueprint_library is None:
            self.blueprintsReady.emit([])
            return
        names = sorted({bp.id for bp in self._blueprint_library.filter(prefix)})
        self.blueprintsReady.emit(names)

    def _do_spawn(self, blueprint: str, x: float, y: float, z: float, use_spawn_point: bool = False) -> None:
        if self._world is None or self._carla is None:
            self.errorOccurred.emit("未连接 CARLA")
            return
        bp = self._blueprint_library.find(blueprint)
        # 世界原点 (0,0) 在多数地图上非法（空地/水面/路网外），勾选“使用出生点”
        # 或坐标仍为默认 (0,0) 时，回退到地图自带的合法出生点，避免 try_spawn_actor 静默失败。
        if use_spawn_point or (x == 0.0 and y == 0.0):
            spawn_points = self._world.get_map().get_spawn_points()
            chosen = pick_spawn_point(spawn_points)
            if chosen is None:
                self.errorOccurred.emit("地图无可用出生点，请手动填写合法坐标后重试")
                return
            transform = chosen
        else:
            transform = self._carla.Transform(
                self._carla.Location(x=x, y=y, z=z),
                self._carla.Rotation(pitch=0.0, yaw=0.0, roll=0.0),
            )
        actor = self._world.try_spawn_actor(bp, transform)
        if actor is None:
            loc = transform.location
            self.logLine.emit(
                f"生成失败（位置非法/被占）：{blueprint} @ ({loc.x:.1f},{loc.y:.1f},{loc.z:.1f})；"
                "可勾选“使用地图出生点”或改用其他坐标"
            )
            return
        self._actors[actor.id] = actor
        if self._ego is None and blueprint.startswith("vehicle."):
            self._ego = actor
            self.logLine.emit(f"自车已生成 id={actor.id}（{blueprint}）→ 在 Add Sensor 的 Parent 选 {actor.id}")
        else:
            self.logLine.emit(f"已生成 {blueprint} id={actor.id}")
        self.actorsChanged.emit(list(self._actors))

    def _do_attach_sensor(self, bp_name, preset, width, height, fov, parent_id, rel_x, rel_y, rel_z, pitch) -> None:
        if self._world is None or self._carla is None:
            self.errorOccurred.emit("未连接 CARLA")
            return
        parent = self._actors.get(parent_id)
        if parent is None:
            self.errorOccurred.emit(f"父 Actor 不存在：{parent_id}")
            return
        bp = self._blueprint_library.find(bp_name)
        if bp_name.startswith("sensor.camera."):
            cfg = build_rgb_camera_config(self._label_for(width, height), fov_deg=fov)
            bp.set_attribute("image_size_x", str(cfg.width))
            bp.set_attribute("image_size_y", str(cfg.height))
            bp.set_attribute("fov", str(cfg.fov_deg))
        mount = MOUNT_PRESETS.get(preset)
        if mount is not None:
            rel_x, rel_y, rel_z, pitch = mount.x, mount.y, mount.z, mount.pitch
        transform = self._carla.Transform(
            self._carla.Location(x=rel_x, y=rel_y, z=rel_z),
            self._carla.Rotation(pitch=pitch, yaw=0.0, roll=0.0),
        )
        sensor = self._world.spawn_actor(bp, transform, attach_to=parent)
        self._actors[sensor.id] = sensor
        if bp_name.startswith("sensor.camera."):
            self._camera_sensors[sensor.id] = sensor
            # CARLA Image 不携带 actor id，用闭包绑定 sensor.id 传递
            sensor.listen(lambda image, sid=sensor.id: self._on_frame(sid, image))
            self.sensorsChanged.emit(list(self._camera_sensors))
        self.logLine.emit(f"已挂载传感器 {bp_name} id={sensor.id} → parent {parent_id}")
        self.actorsChanged.emit(list(self._actors))

    def _do_autopilot(self, enabled: bool) -> None:
        if self._ego is None:
            self.errorOccurred.emit("尚未生成自车")
            return
        self._ego.set_autopilot(enabled, self._tm_port_or_default())
        self.logLine.emit(f"Autopilot {'ENABLED' if enabled else 'DISABLED'} for {self._ego.id}")

    def _do_control(self, command: VehicleControlCommand) -> None:
        if self._ego is None or self._carla is None:
            return
        ctrl = self._carla.VehicleControl(
            throttle=command.throttle,
            steer=command.steer,
            brake=command.brake,
            hand_brake=command.hand_brake,
            reverse=command.reverse,
            gear=command.gear,
        )
        self._ego.apply_control(ctrl)

    def _do_set_sync(self, enabled: bool) -> None:
        self._sync_mode = enabled
        self._apply_sync_settings()
        self.logLine.emit(f"同步模式：{'开' if enabled else '关'}")

    def _apply_sync_settings(self) -> None:
        """把 `_sync_mode` 标志单一事实源下发到 world（connect/load_map/xodr/set_sync 共用）。

        world 句柄切换后（load_world/generate_opendrive_world）会重置为异步，
        必须重新下发，否则标志与实际 world 设置长期脱节。
        """
        if self._world is None:
            return
        try:
            settings = self._world.get_settings()
            settings.synchronous_mode = self._sync_mode
            settings.fixed_delta_seconds = 0.02 if self._sync_mode else 0.0
            self._world.apply_settings(settings)
        except Exception as exc:  # noqa: BLE001
            self.errorOccurred.emit(f"同步模式下发失败：{type(exc).__name__}: {exc}")

    def _do_start(self) -> None:
        self._running = True
        self.logLine.emit("仿真已启动")

    def _do_pause(self) -> None:
        self._running = False
        self.logLine.emit("仿真已暂停")

    def _do_stop(self) -> None:
        self._running = False
        self.logLine.emit("仿真已停止")

    def _do_clear(self) -> None:
        self._reset_scene_state()
        self.logLine.emit("场景已清空")

    # ── 相机回调（CARLA 线程 → Qt 信号跨线程投递）─────────────────────────
    def _on_frame(self, sensor_id: int, image: Any) -> None:
        try:
            from PySide6.QtGui import QImage  # noqa: PLC0415

            arr = image_to_rgb_array(image)
            h, w = arr.shape[:2]
            qimg = QImage(arr.data, w, h, arr.strides[0], QImage.Format_RGB888).copy()
            self.frameReady.emit(sensor_id, qimg)
        except Exception as exc:  # noqa: BLE001
            self.errorOccurred.emit(f"帧转换失败：{exc}")

    # ── 辅助 ──────────────────────────────────────────────────────────────
    def _emit_state(self) -> None:
        now = time.time()
        if now - self._last_state_emit < 0.1 or self._ego is None:
            return
        self._last_state_emit = now
        tf = self._ego.get_transform()
        vel = self._ego.get_velocity()
        self.stateUpdated.emit(
            {
                "x": round(tf.location.x, 2),
                "y": round(tf.location.y, 2),
                "z": round(tf.location.z, 2),
                "yaw": round(tf.rotation.yaw, 1),
                "speed": round(3.6 * (vel.x**2 + vel.y**2 + vel.z**2) ** 0.5, 2),
                "actor_id": self._ego.id,
            }
        )

    def _reset_scene_state(self) -> None:
        for sensor in self._camera_sensors.values():
            try:
                sensor.stop()
            except Exception:  # noqa: BLE001
                pass
        for actor in self._actors.values():
            try:
                actor.destroy()
            except Exception:  # noqa: BLE001
                pass
        self._actors.clear()
        self._camera_sensors.clear()
        self._ego = None
        self.actorsChanged.emit([])
        self.sensorsChanged.emit([])

    def _teardown(self) -> None:
        self._reset_scene_state()
        self.disconnected.emit()

    def _tm_port_or_default(self) -> int:
        return getattr(self, "_tm_port", 8000)

    @staticmethod
    def _label_for(width: int, height: int) -> str:
        return f"{width}x{height}"

    # ── 供 GUI 注入 ───────────────────────────────────────────────────────
    def set_tm_port(self, port: int) -> None:
        """设置 Traffic Manager 端口（autopilot 用）。"""
        self._tm_port = port
