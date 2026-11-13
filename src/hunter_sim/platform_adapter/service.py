"""适配器编排层（平台控制面语义）。

职责：
- ``create``：按 ``sim_id``/``scene_id`` 幂等创建实例，立即入队并返回（不等待
  地图加载，``load_world`` 可达 10~30s）；
- ``submit``：平台在 create 后紧接着下发场景；若配置与 create 一致则**不重启**
  正在装配的场景（避免重复加载世界），仅当配置变化时才中断并重跑；
- ``pause/resume/stop``：幂等控制；
- ``get/list/result``：平台轮询视图（状态统一为平台词汇）。

本层不直接操作 CARLA，全部交给 ``SimulationExecutor``。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import threading
import time
import uuid
from typing import Any, AsyncGenerator, Callable, Optional, cast

from hunter_sim.common.metrics import INSTANCE_LIFECYCLE_TOTAL, STREAM_CLIENTS
from hunter_sim.common.models import ROS2_TOPIC_MAP, HunterSimSettings
from hunter_sim.common.utils import get_logger
from hunter_sim.platform_adapter.config import AdapterSettings
from hunter_sim.platform_adapter.executor import SimulationExecutor
from hunter_sim.platform_adapter.models import (
    ACTIVE_STATUSES,
    CreateInstanceRequest,
    InstanceRecord,
    PLATFORM_RUNNING,
    SubmitScenarioRequest,
)
from hunter_sim.platform_adapter.store import InstanceStore
from hunter_sim.sensor_sim.frame_hub import KNOWN_STREAMS, STREAM_RGB, Frame, FrameHub

logger = get_logger(__name__)

_DEFAULT_DURATION_S = 60.0
_QUALITY_KEY = "quality"
# MJPEG 无帧容忍时长（秒）：超过即结束响应，避免客户端无限等空流
_STREAM_IDLE_TIMEOUT_S = 5.0
#: 推流名额租约的闲置判定（秒）：活客户端每 1/stream_fps touch 一次，
#: 超过该值没动静即为客户端未正常断开（浏览器刷新/代理掉线），名额必须回收
_STREAM_SLOT_STALE_S = 30.0
# 事件列表类型别名：类内已有同名方法 ``list``，直接写 ``list[...]`` 会被遮蔽
EventItems = list[dict[str, Any]]
# 实例 ID 列表类型别名（同上：规避类内 ``list`` 方法遮蔽内置类型）
InstanceIds = list[str]
# 参与者状态列表类型别名（同上）
ActorItems = list[dict[str, Any]]


class StreamLimitExceeded(RuntimeError):
    """MJPEG 并发客户端数超过 ``ADAPTER_STREAM_MAX_CLIENTS``。"""

    def __init__(self, limit: int) -> None:
        super().__init__(f"stream clients limit reached: {limit}")
        self.limit = limit


def _latest_frame(hub: FrameHub, stream: Optional[str]) -> Optional[Frame]:
    """取指定流最新帧；指定流无帧时回退 ``rgb``（缺省即 ``rgb``）。"""
    name = (stream or STREAM_RGB).strip().lower()
    frame = hub.latest(name)
    if frame is not None:
        return frame
    if name != STREAM_RGB:
        return hub.latest(STREAM_RGB)
    return None


class AdapterService:
    """控制面编排（create/submit/control/query）。

    Args:
        settings: 全局配置（CARLA/Kafka/Cloud）。
        adapter_settings: 适配器配置（时长上限/产物目录等）。
        store: 实例注册表。
        executor: 串行执行器。
        frame_hub_provider: 帧汇聚中心查询器（默认委托执行器；单测可注入）。
    """

    def __init__(
        self,
        settings: HunterSimSettings,
        adapter_settings: AdapterSettings,
        store: InstanceStore,
        executor: SimulationExecutor,
        frame_hub_provider: Optional[Callable[[str], Optional[FrameHub]]] = None,
    ) -> None:
        self._settings = settings
        self._adapter = adapter_settings
        self._store = store
        self._executor = executor
        self._frame_hub_provider = frame_hub_provider or _executor_frame_hub(executor)
        # MJPEG 并发客户端计数（与 Prometheus Gauge 同步增减）
        self._stream_lock = threading.Lock()
        self._stream_clients = 0
        # 名额租约（token → 最近活跃时刻）：客户端异常断开时异步生成器可能不被
        # 及时关闭，仅靠 finally 归还会让名额长期滞留，“推流已达上限”就是这么来的
        self._stream_slots: dict[int, float] = {}
        self._stream_slot_seq = 0

    # ── 创建 / 下发 ─────────────────────────────────────────────────────────

    def create(self, request: CreateInstanceRequest) -> InstanceRecord:
        """创建实例并立即入队（幂等）。

        幂等键：
        - 平台 ``sim_id`` 相同 → 返回已有实例（避免同一 Topic 出现两个世界）；
        - 同一 ``scene_id`` 已有进行中实例 → 返回已有实例（单世界场景语义）。
        """
        sim_id = request.resolved_sim_id
        if sim_id:
            existing = self._store.find_by_sim_id(sim_id)
            if existing is not None:
                logger.info(
                    f"Adapter create idempotent by sim_id: sim_id={sim_id}, "
                    f"instance={existing.instance_id}"
                )
                return existing
        else:
            sim_id = f"sim-{uuid.uuid4().hex}"

        existing_scene = self._store.find_active_by_scene(request.scene_id)
        if existing_scene is not None:
            logger.warning(
                "Adapter create idempotent by scene_id: scene_id=%s, instance=%s, "
                "platform_sim_id=%s, adapter_sim_id=%s",
                request.scene_id,
                existing_scene.instance_id,
                sim_id,
                existing_scene.sim_id,
            )
            return existing_scene

        scene_config = dict(request.scene_config or {})
        quality = (request.quality or "").strip()
        if quality:
            scene_config[_QUALITY_KEY] = quality

        record = InstanceRecord(
            instance_id=f"inst-{uuid.uuid4().hex}",
            sim_id=sim_id,
            scene_id=request.scene_id,
            scene_name=request.scene_name,
            mode=(request.mode or "sil").strip().lower(),
            vehicle_id=(request.vehicle_id or "").strip(),
            scene_config=scene_config,
            vil_config=request.vil,
            total_time_s=_duration_of(scene_config, self._adapter.default_duration_seconds),
            actors_count=_actors_of(scene_config),
            created_at=time.time(),
            updated_at=time.time(),
        )
        self._store.insert(record)
        INSTANCE_LIFECYCLE_TOTAL.labels(event="created").inc()
        self._executor.submit(record)
        logger.info(
            f"Adapter instance created: instance={record.instance_id}, "
            f"scene={record.scene_id}, sim_id={record.sim_id}, mode={record.mode}"
        )
        return self._store.get(record.instance_id) or record

    def submit(self, instance_id: str, request: SubmitScenarioRequest) -> Optional[InstanceRecord]:
        """下发（或重新下发）场景配置，立即返回。

        平台 create 时已携带完整 ``scene_config``，随后的 submit 通常是同一份配置：
        此时不重启装配中的世界，只确认实例存在。配置真正变化时才提高 ``revision``
        并中断当前运行，由串行队列按最新版本重跑。
        """
        record = self._store.get(instance_id)
        if record is None:
            return None

        incoming = dict(request.scene_config or record.scene_config or {})
        quality = record.scene_config.get(_QUALITY_KEY) if record.scene_config else None
        if quality is not None:
            incoming.setdefault(_QUALITY_KEY, quality)

        same_config = _same_scene_config(record.scene_config, incoming)
        same_mode = _same_optional(request.mode, record.mode)
        same_vehicle = _same_optional(request.vehicle_id, record.vehicle_id)

        if same_config and same_mode and same_vehicle and record.status in ACTIVE_STATUSES:
            logger.info(
                f"Adapter scenario submit coalesced: instance={instance_id}, "
                f"status={record.status} (create/submit race)"
            )
            return record

        record.scene_config = incoming
        if request.mode:
            record.mode = request.mode.strip().lower()
        if request.vehicle_id:
            record.vehicle_id = request.vehicle_id.strip()
        record.total_time_s = _duration_of(incoming, self._adapter.default_duration_seconds)
        record.actors_count = _actors_of(incoming)
        restart = record.status == PLATFORM_RUNNING
        self._executor.submit(record, restart=restart)
        logger.info(
            f"Adapter scenario submitted: instance={instance_id}, "
            f"restart={restart}, revision={record.revision}"
        )
        return self._store.get(instance_id)

    # ── 控制 ────────────────────────────────────────────────────────────────

    def pause(self, instance_id: str) -> Optional[InstanceRecord]:
        """暂停实例（幂等）。"""
        return self._executor.pause(instance_id)

    def resume(self, instance_id: str) -> Optional[InstanceRecord]:
        """恢复实例（幂等）。"""
        return self._executor.resume(instance_id)

    def stop(self, instance_id: str) -> Optional[InstanceRecord]:
        """停止实例并标记 canceled（幂等）。"""
        record = self._executor.cancel(instance_id, reason="canceled")
        if record is not None:
            INSTANCE_LIFECYCLE_TOTAL.labels(event="canceled").inc()
        return record

    # ── 查询 ────────────────────────────────────────────────────────────────

    def get(self, instance_id: str) -> Optional[InstanceRecord]:
        """按实例 ID 查询。"""
        return self._store.get(instance_id)

    def list(self, scene_id: Optional[str] = None) -> list[InstanceRecord]:
        """按场景查询（无 ``scene_id`` 时返回全部，按创建时间倒序）。"""
        scene = (scene_id or "").strip()
        return self._store.list_by_scene(scene) if scene else self._store.list_all()

    def result(self, instance_id: str) -> Optional[InstanceRecord]:
        """查询结果（非终态也返回当前状态，由平台映射为 3003）。"""
        return self._store.get(instance_id)

    def running_instance_ids(self) -> InstanceIds:
        """当前进行中的实例 ID（节点心跳上报，供平台核对 ``running_count``）。

        只有 ``pending``/``running`` 计入；终态实例不占用节点容量。
        """
        return [
            record.instance_id
            for record in self._store.list_all()
            if record.status in ACTIVE_STATUSES
        ]

    def mark_restarted_instances_failed(self) -> int:
        """启动恢复：把重启前未终结的实例标记为 failed。"""
        return self._store.mark_restarted_instances_failed("adapter restarted")

    # ── 实时画面 ─────────────────────────────────────────────────────────────

    def health_snapshot(self) -> dict[str, Any]:
        """管理面健康快照（供只读探针 ``huntersim probes`` 代理转发）。

        只汇总本进程**真实掌握**的信息：无法采样的项显式置 ``None``，
        避免探针与业务面各自探测导致结论不一致。

        Returns:
            含 ``carla_connected`` / ``active_instance`` / ``sensor`` / ``cloud``
            等键的扁平字典（字段语义与探针 ``/health`` 一致）。
        """
        active = [
            record for record in self._store.list_all() if record.status in ACTIVE_STATUSES
        ]
        current = active[0] if active else None
        streams: dict[str, int] = {}
        if current is not None:
            hub = self._frame_hub_provider(current.instance_id)
            if hub is not None:
                streams = dict(hub.stats())
        return {
            "carla_connected": None if current is None else True,
            "carla_probe_error": None,
            "active_instance": None
            if current is None
            else {
                "instance_id": current.instance_id,
                "scene_id": current.scene_id,
                "status": current.status,
                "carla_status": current.carla_status,
                "progress_percent": round(current.progress_percent, 3),
            },
            "vil_state": None,
            "sensor": {
                "recording": bool(current.recording_path) if current is not None else False,
                "recording_bytes": int(current.recording_bytes) if current is not None else 0,
                "streams": streams,
            },
            "stream": self._stream_view(),
            "cloud": {"enabled": bool(self._settings.cloud.enabled)},
        }

    def runtime_gauges(self) -> dict[str, Any]:
        """采样运行时 Gauge 值（抓取 ``/metrics`` 时刷新）。"""
        active = [
            record for record in self._store.list_all() if record.status in ACTIVE_STATUSES
        ]
        return {
            "active_instances": float(len(active)),
            "carla_connected": bool(active),
            "disk_usage_ratio": _disk_usage_ratio(self._adapter.recordings_dir),
        }

    def screenshot(self, instance_id: str, stream: Optional[str] = None) -> Optional[Frame]:
        """取实例最新帧（默认 ``rgb``；``overview`` 无帧时回退 ``rgb``）。

        无活动场景/未挂相机时返回 None。
        """
        hub = self._frame_hub_provider(instance_id)
        if hub is None:
            return None
        return _latest_frame(hub, stream)

    # ── 可视化只读视图（/viz 面板数据源） ──────────────────────────────────────

    def events(self, instance_id: str) -> EventItems:
        """已检测到的场景事件（无事件/无运行器时为空列表）。"""
        getter = getattr(self._executor, "detected_events", None)
        if not callable(getter):
            return []
        try:
            events = getter(instance_id)
        except Exception as exc:  # noqa: BLE001  只读视图不得因运行器已释放而 500
            logger.debug(f"Read detected events failed for {instance_id}: {exc}")
            return []
        return [dict(item) for item in events or [] if isinstance(item, dict)]

    def sensor_frame(self, instance_id: str, stream: str) -> Optional[Frame]:
        """取指定流的最新帧（未知流/无帧返回 None）。"""
        hub = self._frame_hub_provider(instance_id)
        if hub is None:
            return None
        return hub.latest(stream)

    def actors(self, instance_id: str) -> ActorItems:
        """交通参与者状态快照（可视化「参与者」面板；无运行时为空列表）。

        只读视图：数据由运行器 tick 线程缓存，未运行/已释放时返回空列表，
        不得因运行器状态变化抛异常（面板拉取失败会连带整页报错）。
        """
        getter = getattr(self._executor, "actors", None)
        if not callable(getter):
            return []
        try:
            states = getter(instance_id)
        except Exception as exc:  # noqa: BLE001  只读视图不得因运行器已释放而 500
            logger.debug(f"Read actor states failed for {instance_id}: {exc}")
            return []
        return [dict(item) for item in states or [] if isinstance(item, dict)]

    def sensors(self, instance_id: str) -> dict[str, Any]:
        """传感器各流的累计帧数、实时帧率与最新帧摘要。

        ``attached=False`` 表示当前实例没有活动运行器（未开始采集或已结束）。
        """
        hub = self._frame_hub_provider(instance_id)
        if hub is None:
            return {"attached": False, "counts": {}, "streams": {}}
        now = time.time()
        # V3.8.18：丢帧计数与标称帧率（运行器诊断，无运行器时为空）
        diagnostics: dict[str, Any] = {"dropped": {}, "nominal_fps": {}}
        diag_getter = getattr(self._executor, "sensor_diagnostics", None)
        if callable(diag_getter):
            try:
                raw = diag_getter(instance_id)
                if isinstance(raw, dict):
                    diagnostics = raw
            except Exception as exc:  # noqa: BLE001  只读视图不得因瞬时异常 500
                logger.debug(f"Read sensor diagnostics failed for {instance_id}: {exc}")
        dropped = diagnostics.get("dropped") or {}
        nominal_fps = diagnostics.get("nominal_fps") or {}
        streams: dict[str, Any] = {}
        for name in KNOWN_STREAMS:
            frame = hub.latest(name)
            streams[name] = {
                "count": hub.count(name),
                "fps": round(hub.fps(name), 2),
                "nominal_fps": nominal_fps.get(name),
                "dropped_frames": int(dropped.get(name, 0)),
                "frame": None
                if frame is None
                else {
                    "seq": frame.seq,
                    # V3.8.18：ts 为仿真经过秒，wall_ts 为墙钟 Unix 秒；
                    # 帧龄必须用墙钟相减（旧版用 now - 仿真秒 → ~1.8e9 s）。
                    "ts": round(frame.ts, 3),
                    "sim_ts": round(frame.ts, 3),
                    "wall_ts": round(frame.wall_ts, 3) if frame.wall_ts else None,
                    "size": frame.size,
                    "age_s": round(
                        max(0.0, now - (frame.wall_ts or now)), 3
                    ),
                    "meta": dict(frame.meta),
                },
            }
        counts = dict(hub.stats())
        return {"attached": True, "counts": counts, "streams": streams}

    def ros(self, instance_id: str) -> dict[str, Any]:
        """ROS2 桥接状态（未启用时 ``enabled=False``，``topics`` 仍给出映射表）。

        V3.8.18：透传 ``reason``（disabled/unavailable/enabled），面板区分
        「未启用」与「rclpy 不可用」。
        """
        getter = getattr(self._executor, "ros_status", None)
        status: dict[str, Any] = {"enabled": False, "reason": "disabled", "topics": {}}
        if callable(getter):
            try:
                raw = getter(instance_id)
                if isinstance(raw, dict):
                    status = {
                        "enabled": bool(raw.get("enabled", False)),
                        "reason": str(raw.get("reason") or "disabled"),
                        "topics": dict(raw.get("topics") or {}),
                    }
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"Read ROS status failed for {instance_id}: {exc}")
        status["mappings"] = [_topic_view(item, status["topics"]) for item in ROS2_TOPIC_MAP]
        return status

    def runtime_status(self) -> dict[str, Any]:
        """仿真端运行态汇总（可视化客户端顶栏）。

        只汇总本进程真实掌握的信息：CARLA 连通性用一次短超时 TCP 探测，
        探测失败不影响返回（面板显示离线即可）。
        """
        records = self._store.list_all()
        active = [record for record in records if record.status in ACTIVE_STATUSES]
        current = active[0] if active else None
        carla = self._settings.carla
        sensor: dict[str, Any] = {"attached": False, "counts": {}, "streams": {}}
        ros: dict[str, Any] = {"enabled": False, "topics": {}}
        if current is not None:
            sensor = self.sensors(current.instance_id)
            ros = self.ros(current.instance_id)
        return {
            "instances": {"total": len(records), "active": len(active)},
            "active_instance": None if current is None else current.to_view(),
            "carla": {
                "endpoint": f"{carla.host}:{carla.rpc_port}",
                "connected": _tcp_open(carla.host, carla.rpc_port),
            },
            "platform": {
                "enabled": bool(self._settings.cloud.enabled),
                "gateway_url": self._settings.cloud.gateway_url
                if self._settings.cloud.enabled
                else "",
                "kafka_bootstrap": self._settings.kafka.bootstrap_servers,
            },
            "sensor": sensor,
            "ros": ros,
            "stream": self._stream_view(),
        }

    @property
    def active_streams(self) -> int:
        """当前在线的 MJPEG 客户端数（与 ``huntersim_stream_clients`` 同源）。"""
        with self._stream_lock:
            return self._stream_clients

    def _stream_view(self) -> dict[str, Any]:
        """推流现状（V3.5.5）：名额占用/上限/帧率。

        存在的意义是把「MJPEG 不可用」从推测变成可读：名额吃紧时
        ``clients`` 会贴近 ``limit``，无需再看服务端日志就能定性是并发上限
        还是相机未挂载（后者表现为 ``sensor.streams.overview`` 为 0）。
        """
        return {
            "clients": self.active_streams,
            "limit": int(self._adapter.stream_max_clients),
            "fps": float(self._adapter.stream_fps),
        }

    async def stream_frames(
        self, instance_id: str, fps: Optional[float] = None, stream: Optional[str] = None
    ) -> AsyncGenerator[Frame, None]:
        """持续产出最新帧（默认 ``rgb``；``overview`` 无帧时回退 ``rgb``）。

        异步生成器：帧读取本身非阻塞，等待用 ``asyncio.sleep``，因此不占用
        anyio 工作线程（同步生成器 + ``time.sleep`` 会每个客户端独占 1 个线程，
        推流客户端一多就把控制面其他同步端点拖死）。

        无相机时立即结束；无新帧时重发上一帧以维持 MJPEG 连接；
        连续 ``_STREAM_IDLE_TIMEOUT_S`` 秒无任何帧则结束（由路由层拒绝空流）。

        Args:
            instance_id: 实例 ID。
            fps: 推流帧率（None 时取 ``ADAPTER_STREAM_FPS``）。
            stream: 流名（None 时取 ``rgb``）。

        Raises:
            StreamLimitExceeded: 在线客户端数已达 ``stream_max_clients`` 上限。
        """
        rate = float(fps if fps is not None else self._adapter.stream_fps)
        interval = 1.0 / max(0.5, rate)
        token = self._acquire_stream_slot()
        last_frame: Optional[Frame] = None
        last_seen_at = time.time()
        try:
            while True:
                hub = self._frame_hub_provider(instance_id)
                if hub is None:
                    return
                frame = _latest_frame(hub, stream)
                if frame is not None:
                    last_frame = frame
                    last_seen_at = time.time()
                elif time.time() - last_seen_at > _STREAM_IDLE_TIMEOUT_S:
                    return
                self._touch_stream_slot(token)
                if last_frame is not None:
                    yield last_frame
                await asyncio.sleep(interval)
        finally:
            self._release_stream_slot(token)

    def _acquire_stream_slot(self) -> int:
        """占用一个推流名额并返回租约 token。

        超限抛 ``StreamLimitExceeded``（由路由转 503）；占用前先回收闲置租约，
        避免长期运行后名额被僵尸连接吃满。
        """
        limit = int(self._adapter.stream_max_clients)
        self._sweep_stale_stream_slots()
        with self._stream_lock:
            if limit > 0 and len(self._stream_slots) >= limit:
                raise StreamLimitExceeded(limit)
            self._stream_slot_seq += 1
            token = self._stream_slot_seq
            self._stream_slots[token] = time.time()
            self._stream_clients = len(self._stream_slots)
        STREAM_CLIENTS.inc()
        return token

    def _touch_stream_slot(self, token: int) -> None:
        """刷新租约活跃时刻（每出帧一次，供闲置回收判定）。"""
        with self._stream_lock:
            if token in self._stream_slots:
                self._stream_slots[token] = time.time()

    def _release_stream_slot(self, token: int) -> None:
        """归还推流名额（与 ``_acquire_stream_slot`` 成对调用）。

        计数不得为负也不得重复减：Prometheus Gauge 与进程内计数同源，-count
        一旦跑偏就会永久偏小（后续名额看起来永远够用，但实际会莫名 503）。
        """
        with self._stream_lock:
            if self._stream_slots.pop(token, None) is None:
                return
            self._stream_clients = len(self._stream_slots)
        STREAM_CLIENTS.dec()

    def _sweep_stale_stream_slots(self) -> int:
        """回收闲置超时的名额（客户端未正常断开时的兜底）。"""
        deadline = time.time() - _STREAM_SLOT_STALE_S
        with self._stream_lock:
            stale = [token for token, seen in self._stream_slots.items() if seen < deadline]
            for token in stale:
                self._stream_slots.pop(token, None)
            self._stream_clients = len(self._stream_slots)
        for _ in stale:
            STREAM_CLIENTS.dec()
        if stale:
            logger.warning(
                f"Stream slots reclaimed as stale: count={len(stale)}, "
                f"idle>{_STREAM_SLOT_STALE_S:.0f}s "
                "(client disconnected without closing the MJPEG response)"
            )
        return len(stale)


def _disk_usage_ratio(path: str) -> Optional[float]:
    """录制目录所在盘的占用率（目录不可用时返回 None，不产出样本）。"""
    try:
        usage = shutil.disk_usage(path or ".")
    except OSError:
        return None
    return usage.used / usage.total if usage.total else None


def _tcp_open(host: str, port: int, timeout: float = 0.3) -> bool:
    """TCP 端口可连通（可视化顶栏的 CARLA 在线指示，短超时避免拖慢轮询）。"""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def _topic_view(mapping: Any, published: dict[str, Any]) -> dict[str, Any]:
    """ROS 话题映射行（sim/real 话题名 + 消息类型 + 已发条数）。"""
    topic = str(getattr(mapping, "sim_topic", ""))
    count = published.get(topic, 0)
    return {
        "sim_topic": topic,
        "real_topic": str(getattr(mapping, "real_topic", "")),
        "msg_type": str(getattr(mapping, "msg_type", "")),
        "frequency_hz": float(getattr(mapping, "frequency_hz", 0.0) or 0.0),
        "published": int(count) if isinstance(count, (int, float)) else 0,
    }


def _executor_frame_hub(
    executor: Any,
) -> Callable[[str], Optional[FrameHub]]:
    """取执行器的帧汇聚中心查询器（测试替身可能未提供，降级为“无画面”）。"""
    provider = getattr(executor, "frame_hub", None)
    if callable(provider):
        return cast(Callable[[str], Optional[FrameHub]], provider)
    return lambda _instance_id: None


def _same_scene_config(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """比较场景配置是否等价（忽略 create 时附加的 ``quality`` 元数据）。"""
    return _canonical(left) == _canonical(right)


def _canonical(config: dict[str, Any]) -> str:
    """规范化 JSON（键排序，忽略 quality 键）。"""
    if not isinstance(config, dict):
        return ""
    payload = {key: value for key, value in config.items() if key != _QUALITY_KEY}
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return ""


def _same_optional(value: Optional[str], current: str) -> bool:
    """可选字段比较：None/空串表示“不修改”，视为一致。"""
    text = (value or "").strip().lower()
    return not text or text == (current or "").strip().lower()


def _duration_of(scene_config: dict[str, Any], default: float) -> float:
    """读取场景时长（秒）。"""
    try:
        value = float(scene_config.get("duration", default))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def _actors_of(scene_config: dict[str, Any]) -> int:
    """参与者数量（含自车）。"""
    actors = scene_config.get("actors")
    return (len(actors) if isinstance(actors, list) else 0) + 1


__all__ = ["AdapterService"]
