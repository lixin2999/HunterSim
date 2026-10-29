"""BuiltinScenario 仿真传感器挂载与数据分发单测（V3.0.0）。

覆盖：按 ``config.sensors`` 剖面挂载/跳过、蓝图缺失不阻断、cleanup 销毁、
回调 → FrameHub/McapRecorder/ROS2 三路分发、格式与降采样参数。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from hunter_sim.scene_runner.builtin_scenario import BuiltinScenario
from hunter_sim.scene_runner.event_detector import EventDetector
from hunter_sim.scene_runner.scene_config import SceneConfig, SensorSpec
from hunter_sim.scene_runner.scene_state_manager import SceneStateManager
from hunter_sim.sensor_sim.frame_hub import STREAM_DEPTH, STREAM_IMU, STREAM_LIDAR, STREAM_RGB
from hunter_sim.sensor_sim.mcap_recorder import McapRecorder
from tests.mocks.carla_mocks import (
    MockActor,
    MockBlueprintLibrary,
    MockLocation,
    MockRotation,
    MockTransform,
    MockWorld,
)

_BASE_CONFIG: dict[str, Any] = {
    "scene_id": "scene-sensors-1",
    "map_id": "Town03",
    "duration_seconds": 30.0,
    "ego_vehicle": {
        "vehicle_blueprint": "hunter_se",
        "spawn_point": {"x": 1.0, "y": 2.0},
        "autopilot": True,
    },
    "traffic_participants": [],
}

# 默认剖面 hunter_se_full 的三件套（LiDAR + RGB + Depth + IMU）
_ALL_IMAGE = "rgb"
_IMAGE_BYTES_PER_PIXEL = 4


# ─── 桩件 ────────────────────────────────────────────────────────────────────


class _SensorActor(MockActor):
    """带 ``listen`` / ``stop`` 的传感器 Actor 桩。"""

    def __init__(self, type_id: str) -> None:
        super().__init__(type_id)
        self.callbacks: list[Any] = []
        self.stop_calls = 0

    def listen(self, callback: Any) -> None:
        self.callbacks.append(callback)

    def stop(self) -> None:
        self.stop_calls += 1

    def emit(self, data: Any) -> int:
        for callback in list(self.callbacks):
            callback(data)
        return len(self.callbacks)


class _SensorWorld(MockWorld):
    """``sensor.*`` 蓝图返回可监听 Actor 的 World 桩。"""

    def spawn_actor(
        self,
        blueprint: Any,
        transform: Any,
        attach_to: Any = None,
    ) -> Any:
        if str(getattr(blueprint, "id", "")).startswith("sensor."):
            actor = _SensorActor(blueprint.id)
            actor._transform = transform
            self._actors.append(actor)
            return actor
        return super().spawn_actor(blueprint, transform, attach_to)


class _NoSensorLibrary:
    """只提供车辆蓝图、缺全部 ``sensor.*`` 的蓝图库桩。"""

    def __init__(self) -> None:
        self._inner = MockBlueprintLibrary()

    def find(self, type_id: str) -> Any:
        if type_id.startswith("sensor."):
            return None
        return self._inner.find(type_id)

    def filter(self, pattern: str) -> list[Any]:
        return self._inner.filter(pattern)


class _NoSensorWorld(_SensorWorld):
    """蓝图库缺传感器资产的 World 桩。"""

    def get_blueprint_library(self) -> Any:
        return _NoSensorLibrary()


class _FakeRecorder:
    """记录 push 调用的录制器桩。"""

    def __init__(self) -> None:
        self.messages: list[tuple[str, int, Any]] = []

    def push_stream(self, stream: str, log_time_ns: int, message: Any) -> bool:
        self.messages.append((stream, log_time_ns, message))
        return True


class _FakeRosBridge:
    """记录 ROS2 发布调用的桥接器桩。"""

    def __init__(self, is_initialized: bool = True) -> None:
        self.is_initialized = is_initialized
        self.calls: list[tuple[str, Any]] = []

    def publish_rgb_image(self, data: Any) -> None:
        self.calls.append(("rgb", data))

    def publish_depth_image(self, data: Any) -> None:
        self.calls.append(("depth", data))

    def publish_lidar(self, data: Any) -> None:
        self.calls.append(("lidar", data))

    def publish_imu(self, data: Any) -> None:
        self.calls.append(("imu", data))


@pytest.fixture(autouse=True)
def fake_carla(monkeypatch: pytest.MonkeyPatch) -> None:
    """注入最小 carla 桩（``SensorMountSpec.to_carla_transform`` 需要）。"""
    module = types.ModuleType("carla")

    class Location(MockLocation):
        def __init__(self, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> None:
            super().__init__(x, y, z)

    class Rotation(MockRotation):
        def __init__(
            self, pitch: float = 0.0, yaw: float = 0.0, roll: float = 0.0
        ) -> None:
            super().__init__(pitch, yaw, roll)

    class Transform:
        def __init__(self, location: Any = None, rotation: Any = None) -> None:
            self.location = location if location is not None else Location()
            self.rotation = rotation if rotation is not None else Rotation()

    module.Location = Location  # type: ignore[attr-defined]
    module.Rotation = Rotation  # type: ignore[attr-defined]
    module.Transform = Transform  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "carla", module)


def _make_scenario(
    overrides: dict[str, Any] | None = None,
    *,
    frame_hub: Any = None,
    recorder: Any = None,
    ros_bridge: Any = None,
    screenshot_quality: int = 75,
    stream_max_width: int = 0,
) -> BuiltinScenario:
    config = SceneConfig(**{**_BASE_CONFIG, **(overrides or {})})
    return BuiltinScenario(
        config,
        SceneStateManager(config.scene_id),
        EventDetector(config.scene_events),
        transform_factory=lambda _world, sp: MockTransform(
            MockLocation(sp.x, sp.y, sp.z or 0.5)
        ),
        frame_hub=frame_hub,
        recorder=recorder,
        ros_bridge=ros_bridge,
        screenshot_quality=screenshot_quality,
        stream_max_width=stream_max_width,
    )


def _image_data(width: int = 4, height: int = 2, ts: float = 100.0) -> Any:
    return types.SimpleNamespace(
        data=bytes(width * height * _IMAGE_BYTES_PER_PIXEL),
        width=width,
        height=height,
        timestamp=ts,
    )


def _lidar_data(points: int = 3, ts: float = 100.0) -> Any:
    return types.SimpleNamespace(data=bytes(points * 16), timestamp=ts)


def _imu_data(ts: float = 100.0) -> Any:
    return types.SimpleNamespace(
        accelerometer=(1.0, 2.0, 3.0),
        gyroscope=(0.1, 0.2, 0.3),
        timestamp=ts,
    )


def _sensor_actors(scenario: BuiltinScenario) -> list[_SensorActor]:
    return [actor for actor in scenario._sensors if isinstance(actor, _SensorActor)]  # noqa: SLF001


# ─── 挂载 ────────────────────────────────────────────────────────────────────


class TestAttach:
    def test_default_profile_attaches_full_bundle(self) -> None:
        scenario = _make_scenario()
        world = _SensorWorld()

        scenario.setup(world)

        type_ids = sorted(actor.type_id for actor in _sensor_actors(scenario))
        assert type_ids == [
            "sensor.camera.depth",
            "sensor.camera.rgb",
            "sensor.lidar.ray_cast",
            "sensor.other.imu",
        ]
        # 每个传感器都已注册回调
        assert all(actor.callbacks for actor in _sensor_actors(scenario))

    def test_single_profile_attaches_only_that_sensor(self) -> None:
        scenario = _make_scenario({"sensors": [{"profile": "helios_16p"}]})
        world = _SensorWorld()

        scenario.setup(world)

        type_ids = [actor.type_id for actor in _sensor_actors(scenario)]
        assert type_ids == ["sensor.lidar.ray_cast"]

    def test_disabled_sensor_specs_attach_nothing(self) -> None:
        scenario = _make_scenario({"sensors": [{"profile": "hunter_se_full", "enabled": False}]})
        world = _SensorWorld()

        scenario.setup(world)

        assert _sensor_actors(scenario) == []

    def test_multiple_specs_are_merged(self) -> None:
        scenario = _make_scenario(
            {"sensors": [{"profile": "helios_16p"}, {"profile": "ch10x"}]}
        )
        world = _SensorWorld()

        scenario.setup(world)

        type_ids = sorted(actor.type_id for actor in _sensor_actors(scenario))
        assert type_ids == ["sensor.lidar.ray_cast", "sensor.other.imu"]

    def test_unknown_profile_falls_back_to_default(self) -> None:
        scenario = _make_scenario({"sensors": [{"profile": "not_a_profile"}]})
        world = _SensorWorld()

        scenario.setup(world)

        assert len(_sensor_actors(scenario)) == 4

    def test_missing_sensor_blueprints_do_not_block_scene(self) -> None:
        scenario = _make_scenario()
        world = _NoSensorWorld()

        scenario.setup(world)

        # 自车照常生成，传感器全部跳过
        assert scenario.ego_actor is not None
        assert _sensor_actors(scenario) == []
        assert len(world.get_actors()) == 1

    def test_ros_flag_requires_enabled_spec(self) -> None:
        scenario = _make_scenario({"sensors": [{"profile": "ch10x", "ros_enabled": True}]})
        scenario.setup(_SensorWorld())

        assert scenario._ros_enabled is True  # noqa: SLF001

    def test_ros_flag_defaults_to_false(self) -> None:
        scenario = _make_scenario()
        scenario.setup(_SensorWorld())

        assert scenario._ros_enabled is False  # noqa: SLF001

    def test_setup_twice_does_not_leak_sensors(self) -> None:
        scenario = _make_scenario()
        world = _SensorWorld()

        scenario.setup(world)
        first = _sensor_actors(scenario)
        scenario.setup(world)

        assert all(actor.stop_calls >= 1 for actor in first)
        assert len(_sensor_actors(scenario)) == 4


class TestCleanup:
    def test_cleanup_stops_and_destroys_sensors(self) -> None:
        scenario = _make_scenario()
        world = _SensorWorld()
        scenario.setup(world)
        actors = _sensor_actors(scenario)

        scenario.cleanup(world)

        assert actors
        assert all(actor.stop_calls == 1 for actor in actors)
        assert all(actor.is_alive() is False for actor in actors)
        assert scenario._sensors == []  # noqa: SLF001
        assert scenario.ego_actor is None

    def test_cleanup_is_idempotent(self) -> None:
        scenario = _make_scenario()
        world = _SensorWorld()
        scenario.setup(world)
        actors = _sensor_actors(scenario)

        scenario.cleanup(world)
        scenario.cleanup(world)

        assert all(actor.stop_calls == 1 for actor in actors)


# ─── 数据分发 ────────────────────────────────────────────────────────────────


class TestDispatch:
    def test_rgb_pushes_encoded_frame_and_records(self) -> None:
        from hunter_sim.sensor_sim.frame_hub import FrameHub

        hub = FrameHub()
        recorder = _FakeRecorder()
        scenario = _make_scenario(frame_hub=hub, recorder=recorder)

        scenario._dispatch_sensor_data(STREAM_RGB, _image_data(width=4, height=2))  # noqa: SLF001

        frame = hub.latest(STREAM_RGB)
        assert frame is not None
        assert frame.payload.startswith((b"\x89PNG", b"\xff\xd8"))  # PNG 兜底 / JPEG
        assert frame.meta["width"] == 4
        assert frame.meta["height"] == 2
        assert frame.meta["content_type"] in ("image/png", "image/jpeg")
        assert frame.ts == pytest.approx(100.0)

        assert len(recorder.messages) == 1
        stream, log_time_ns, message = recorder.messages[0]
        assert stream == STREAM_RGB
        assert log_time_ns == 100 * 1_000_000_000
        assert message.encoding == "bgra8"  # 录制保留原始像素
        assert message.header.frame_id == "camera_front"

    def test_rgb_respects_stream_max_width(self) -> None:
        from hunter_sim.sensor_sim.frame_hub import FrameHub

        hub = FrameHub()
        scenario = _make_scenario(frame_hub=hub, stream_max_width=2)

        scenario._dispatch_sensor_data(STREAM_RGB, _image_data(width=8, height=4))  # noqa: SLF001

        frame = hub.latest(STREAM_RGB)
        assert frame is not None
        assert frame.meta["width"] == 2
        assert frame.meta["height"] == 1

    def test_depth_is_recorded_as_metric_image(self) -> None:
        from hunter_sim.sensor_sim.frame_hub import FrameHub

        hub = FrameHub()
        recorder = _FakeRecorder()
        scenario = _make_scenario(frame_hub=hub, recorder=recorder)

        scenario._dispatch_sensor_data(STREAM_DEPTH, _image_data(width=4, height=2))  # noqa: SLF001

        assert len(recorder.messages) == 1
        stream, _, message = recorder.messages[0]
        assert stream == STREAM_DEPTH
        assert message.encoding == "32FC1"
        assert len(message.data) == 4 * 2 * 4  # float32 × 像素数

    def test_lidar_pushes_pointcloud(self) -> None:
        from hunter_sim.sensor_sim.frame_hub import FrameHub

        hub = FrameHub()
        recorder = _FakeRecorder()
        scenario = _make_scenario(frame_hub=hub, recorder=recorder)

        scenario._dispatch_sensor_data(STREAM_LIDAR, _lidar_data(points=3))  # noqa: SLF001

        frame = hub.latest(STREAM_LIDAR)
        assert frame is not None
        assert frame.meta["points"] == 3
        assert len(frame.payload) == 48

        stream, _, message = recorder.messages[0]
        assert stream == STREAM_LIDAR
        assert message.width == 3
        assert message.point_step == 16

    def test_malformed_lidar_is_skipped(self) -> None:
        from hunter_sim.sensor_sim.frame_hub import FrameHub

        hub = FrameHub()
        recorder = _FakeRecorder()
        scenario = _make_scenario(frame_hub=hub, recorder=recorder)

        scenario._dispatch_sensor_data(  # noqa: SLF001
            STREAM_LIDAR, types.SimpleNamespace(data=bytes(17), timestamp=100.0)
        )

        assert hub.latest(STREAM_LIDAR) is None
        assert recorder.messages == []

    def test_imu_pushes_and_records(self) -> None:
        from hunter_sim.sensor_sim.frame_hub import FrameHub

        hub = FrameHub()
        recorder = _FakeRecorder()
        scenario = _make_scenario(frame_hub=hub, recorder=recorder)

        scenario._dispatch_sensor_data(STREAM_IMU, _imu_data())  # noqa: SLF001

        frame = hub.latest(STREAM_IMU)
        assert frame is not None
        assert b"1.0" in frame.payload

        stream, _, message = recorder.messages[0]
        assert stream == STREAM_IMU
        assert message.linear_acceleration.x == pytest.approx(1.0)

    def test_dispatch_without_hub_or_recorder_is_noop(self) -> None:
        scenario = _make_scenario()

        scenario._dispatch_sensor_data(STREAM_RGB, _image_data())  # noqa: SLF001
        scenario._dispatch_sensor_data(STREAM_LIDAR, _lidar_data())  # noqa: SLF001

    def test_frame_error_is_swallowed(self) -> None:
        from hunter_sim.sensor_sim.frame_hub import FrameHub

        hub = FrameHub()
        scenario = _make_scenario(frame_hub=hub, recorder=_FakeRecorder())

        # 像素长度与宽高不匹配：编码失败但不得抛出
        bad = types.SimpleNamespace(data=b"\x00\x01", width=640, height=480, timestamp=1.0)
        scenario._on_sensor_data(STREAM_RGB, bad)  # noqa: SLF001

        assert scenario.drain_sensor_frames() is True
        assert hub.latest(STREAM_RGB) is None

    def test_callback_from_sensor_actor_reaches_hub(self) -> None:
        from hunter_sim.sensor_sim.frame_hub import FrameHub

        hub = FrameHub()
        scenario = _make_scenario(frame_hub=hub)
        scenario.setup(_SensorWorld())

        rgb_actor = next(
            actor for actor in _sensor_actors(scenario) if actor.type_id == "sensor.camera.rgb"
        )
        assert rgb_actor.emit(_image_data()) == 1

        # 回调只入队，编码在 hunter-sensor-encoder 线程完成
        assert scenario.drain_sensor_frames() is True
        assert hub.latest(STREAM_RGB) is not None

    def test_callback_lands_in_recorder_via_worker_thread(self) -> None:
        """回调只入队，编码/落盘在 worker 线程完成（tick 线程不被拖慢）。"""
        from hunter_sim.sensor_sim.frame_hub import FrameHub

        hub = FrameHub()
        recorder = _FakeRecorder()
        scenario = _make_scenario(frame_hub=hub, recorder=recorder)
        scenario.setup(_SensorWorld())

        rgb_actor = next(
            actor for actor in _sensor_actors(scenario) if actor.type_id == "sensor.camera.rgb"
        )
        rgb_actor.emit(_image_data())

        # 已入队但尚未（必然）完成编码，随后 drain 确保最终落盘
        assert scenario.drain_sensor_frames() is True
        assert [stream for stream, _, _ in recorder.messages] == [STREAM_RGB]

    def test_cleanup_flushes_pending_frames(self) -> None:
        """cleanup 先排空编码队列再销毁传感器，未处理的帧不丢。"""
        recorder = _FakeRecorder()
        scenario = _make_scenario(recorder=recorder)
        world = _SensorWorld()
        scenario.setup(world)

        imu_actor = next(
            actor for actor in _sensor_actors(scenario) if actor.type_id == "sensor.other.imu"
        )
        imu_actor.emit(_imu_data())
        scenario.cleanup(world)

        assert [stream for stream, _, _ in recorder.messages] == [STREAM_IMU]
        # 清理后回调不再入队（老传感器可能还会吐一帧）
        imu_actor.emit(_imu_data())
        assert len(recorder.messages) == 1

    def test_full_queue_drops_frame_and_counts(self) -> None:
        """队列满时丢帧并计数（不阻塞 tick 线程，不抛异常）。"""
        from hunter_sim.common import metrics as metrics_module

        scenario = _make_scenario()
        # 不启动 worker，直接填满有界队列
        scenario._frame_accepting = True  # noqa: SLF001
        original = scenario._ensure_frame_worker  # noqa: SLF001
        scenario._ensure_frame_worker = lambda: None  # type: ignore[method-assign]  # noqa: SLF001
        try:
            baseline = metrics_module.counter_value(
                metrics_module.SENSOR_FRAMES_DROPPED_TOTAL, stream=STREAM_IMU
            )
            for _ in range(40):
                scenario._on_sensor_data(STREAM_IMU, _imu_data())  # noqa: SLF001
            dropped = metrics_module.counter_value(
                metrics_module.SENSOR_FRAMES_DROPPED_TOTAL, stream=STREAM_IMU
            )
        finally:
            scenario._ensure_frame_worker = original  # type: ignore[method-assign]  # noqa: SLF001
        assert dropped == baseline + 8  # 队列上限 32，多出 8 帧被丢
        assert scenario._frame_queue.qsize() == 32  # noqa: SLF001


class TestRos:
    def test_ros_disabled_by_default(self) -> None:
        bridge = _FakeRosBridge()
        scenario = _make_scenario(ros_bridge=bridge)
        scenario.setup(_SensorWorld())

        scenario._dispatch_sensor_data(STREAM_IMU, _imu_data())  # noqa: SLF001

        assert bridge.calls == []

    def test_ros_publish_when_enabled(self) -> None:
        bridge = _FakeRosBridge()
        scenario = _make_scenario(
            {"sensors": [{"profile": "ch10x", "ros_enabled": True}]}, ros_bridge=bridge
        )
        scenario.setup(_SensorWorld())

        scenario._dispatch_sensor_data(STREAM_IMU, _imu_data())  # noqa: SLF001

        assert [name for name, _ in bridge.calls] == ["imu"]

    def test_ros_skipped_when_bridge_not_initialized(self) -> None:
        bridge = _FakeRosBridge(is_initialized=False)
        scenario = _make_scenario(
            {"sensors": [{"profile": "ch10x", "ros_enabled": True}]}, ros_bridge=bridge
        )
        scenario.setup(_SensorWorld())

        scenario._dispatch_sensor_data(STREAM_IMU, _imu_data())  # noqa: SLF001

        assert bridge.calls == []

    def test_ros_publish_error_does_not_propagate(self) -> None:
        class _BrokenBridge(_FakeRosBridge):
            def publish_imu(self, data: Any) -> None:
                raise RuntimeError("rclpy down")

        scenario = _make_scenario(
            {"sensors": [{"profile": "ch10x", "ros_enabled": True}]},
            ros_bridge=_BrokenBridge(),
        )
        scenario.setup(_SensorWorld())

        scenario._dispatch_sensor_data(STREAM_IMU, _imu_data())  # noqa: SLF001


# ─── 与真实录制器联动 ────────────────────────────────────────────────────────


class TestIntegration:
    def test_scene_frames_land_in_mcap(self, tmp_path: Path) -> None:
        pytest.importorskip("mcap")
        pytest.importorskip("mcap_ros2")
        from hunter_sim.sensor_sim.frame_hub import FrameHub

        recorder = McapRecorder()
        assert recorder.open(tmp_path / "scene.mcap") is True
        hub = FrameHub()
        scenario = _make_scenario(frame_hub=hub, recorder=recorder)
        scenario.setup(_SensorWorld())

        for actor in _sensor_actors(scenario):
            payload = {
                "sensor.camera.rgb": _image_data(),
                "sensor.camera.depth": _image_data(),
                "sensor.lidar.ray_cast": _lidar_data(),
                "sensor.other.imu": _imu_data(),
            }[actor.type_id]
            actor.emit(payload)

        assert scenario.drain_sensor_frames() is True
        stats = recorder.close()

        assert stats.frame_count == 4
        assert set(stats.topics) == {
            "/carla/camera/color/image_raw",
            "/carla/camera/depth/image_rect_raw",
            "/carla/lidar_points",
            "/carla/imu/data",
        }
        assert stats.size_bytes > 0
        assert hub.stats()["total"] == 3  # depth 只录制，不进 FrameHub


class TestSensorSpec:
    def test_scene_config_defaults(self) -> None:
        config = SceneConfig(**_BASE_CONFIG)

        assert len(config.sensors) == 1
        assert config.sensors[0] == SensorSpec()
        assert config.sensors[0].enabled is True
        assert config.sensors[0].ros_enabled is False
        assert config.sensors[0].record is True

    def test_sensor_spec_fields_are_coerced(self) -> None:
        spec = SensorSpec(
            profile="d435", enabled="false", ros_enabled="true", record=0
        )

        assert spec.profile == "d435"
        assert spec.enabled is False
        assert spec.ros_enabled is True
        assert spec.record is False
