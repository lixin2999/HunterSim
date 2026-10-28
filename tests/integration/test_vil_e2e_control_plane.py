"""控制面 VIL 全链集成测试（V3.0.0 §6.2）。

链路：``create`` → 执行器装配（VILRuntime 钩子）→ ``start`` → VIL 断流上报
``PAUSED(vil_data_interrupted)`` → 恢复 → 场景完成 → 发布 ``sim_metrics`` 与
``sensor_file``（mcap 真值）。

CARLA / Kafka / MinIO 全部用替身：执行平面、运行器、SimDataProducer、
BagUploader、VILRuntime 均不触达外部依赖，只验证控制面编排语义。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from hunter_sim.common.models import CloudSettings, HunterSimSettings, SceneStatus
from hunter_sim.platform_adapter.config import AdapterSettings
from hunter_sim.platform_adapter.executor import SimulationExecutor
from hunter_sim.platform_adapter.models import CreateInstanceRequest
from hunter_sim.platform_adapter.service import AdapterService
from hunter_sim.platform_adapter.store import InstanceStore
from hunter_sim.scene_runner.scene_state_manager import SceneStateSnapshot
from hunter_sim.sensor_sim.mcap_recorder import build_image_message

pytest.importorskip("mcap")
pytest.importorskip("mcap_ros2")

_NS = 1_000_000_000
_TIMEOUT_S = 10.0


# ─── 替身 ────────────────────────────────────────────────────────────────────


class _FakeProducer:
    """SimDataProducer 替身：记录 sensor_file / metrics 发布。"""

    def __init__(self) -> None:
        self.sensor_files: list[dict[str, Any]] = []
        self.metrics: list[Any] = []
        self._seq = 0
        self._lock = threading.Lock()

    def next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def publish_sensor_file(self, **kwargs: Any) -> bool:
        self.sensor_files.append(kwargs)
        return True

    def publish_metrics(self, message: Any) -> bool:
        self.metrics.append(message)
        return True

    def publish_artifact(self, **_kwargs: Any) -> bool:
        return True

    def publish_event(self, **_kwargs: Any) -> bool:
        return True

    def close(self) -> None:  # pragma: no cover - 关闭由执行器负责
        pass


class _FakeUploadResult:
    bucket = "hunter-rosbag"
    object_key = "sim-e2e/sensors_r0.mcap"
    size_bytes = 2048
    sha256 = "a" * 64
    md5 = "b" * 32


class _FakeUploader:
    """BagUploader 替身（真实上传需要 MinIO）。"""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.calls: list[Path] = []

    def connect(self) -> bool:
        return True

    def upload(self, file_path: Path, data_type: str = "rosbag") -> _FakeUploadResult:
        self.calls.append(Path(file_path))
        self.data_types = getattr(self, "data_types", []) + [data_type]
        return _FakeUploadResult()


class _FakeVILRuntime:
    """VILRuntime 替身：先断流再恢复，模拟实车数据中断。"""

    instances: list["_FakeVILRuntime"] = []

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.steps = 0
        self.started = False
        self.stopped = False
        self.interrupted = True
        _FakeVILRuntime.instances.append(self)

    def step(self, _now: float) -> None:
        self.steps += 1

    def status_override(self) -> Optional[tuple[str, Optional[str]]]:
        if self.interrupted:
            return ("paused", "vil_data_interrupted")
        return None

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class _FakeRunner:
    """场景运行器替身：受控多拍状态（RUNNING → COMPLETED）。"""

    def __init__(self) -> None:
        self.hooks: dict[str, Any] = {}
        self.scene: Any = None
        self.started = 0
        self.paused = 0
        self.resumed = 0
        self.stops = 0
        self.snapshots: list[Any] = [_snapshot(SceneStatus.RUNNING, 0.0)]

    # 控制面接口
    def load_scene(self, scene: Any) -> None:
        self.scene = scene

    def start(self) -> None:
        self.started += 1

    def pause(self) -> None:
        self.paused += 1

    def resume(self) -> None:
        self.resumed += 1

    def stop(self, _timeout_s: float = 0.0) -> None:
        self.stops += 1

    def destroy(self) -> None:
        self.stops += 1

    def get_state_snapshot(self) -> Optional[SceneStateSnapshot]:
        if not self.snapshots:
            return None
        return self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]

    @property
    def is_running(self) -> bool:
        snapshot = self.snapshots[0] if self.snapshots else None
        return bool(snapshot is not None and snapshot.status == SceneStatus.RUNNING)

    @property
    def detected_events(self) -> list[Any]:
        return []


class _FakePlane:
    """执行平面替身：记录钩子并暴露 producer 查询。"""

    def __init__(self, runner: _FakeRunner, producer: _FakeProducer) -> None:
        self.runner = runner
        self.producer = producer
        self.hooks: dict[str, Any] = {}
        self.released: list[str] = []

    def ensure_runner(self, instance: Any, **_kwargs: Any) -> _FakeRunner:
        self.hooks = dict(_kwargs.get("runner_hooks") or {})
        self.runner.hooks = self.hooks
        return self.runner

    def release(self, instance_id: str) -> bool:
        self.released.append(instance_id)
        return True

    def release_all(self) -> int:
        return len(self.released)

    def producer_for_sim_id(self, _sim_id: str) -> Any:
        return self.producer

    def get_runner(self, _instance_id: str) -> _FakeRunner:
        return self.runner


# ─── 脚手架 ──────────────────────────────────────────────────────────────────


def _snapshot(status: SceneStatus, progress: float = 0.0) -> SceneStateSnapshot:
    return SceneStateSnapshot(
        scene_id="scene-e2e",
        status=status,
        elapsed_seconds=progress,
        progress=min(1.0, progress / 30.0) if progress else 0.0,
    )


def _adapter(tmp_path: Path) -> AdapterSettings:
    return AdapterSettings(
        db_path=str(tmp_path / "adapter.db"),
        artifacts_dir=str(tmp_path / "artifacts"),
        recordings_dir=str(tmp_path / "recordings"),
        default_duration_seconds=30.0,
    )


def _scene_config() -> dict[str, Any]:
    return {
        "map": {"map_id": "Town03", "spawn_point": {"x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0}},
        "duration": 30.0,
        "sensors": [{"profile": "hunter_se_full", "record": True}],
        "actors": [],
    }


def _wait_until(predicate: Any, timeout: float = _TIMEOUT_S) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def _stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """装配 service / executor / store 与全部替身。"""
    _FakeVILRuntime.instances.clear()
    monkeypatch.setattr("hunter_sim.platform_adapter.executor.VILRuntime", _FakeVILRuntime)
    monkeypatch.setattr("hunter_sim.cloud.connector.BagUploader", _FakeUploader)

    settings = HunterSimSettings(cloud=CloudSettings(enabled=True, sim_id="sim-e2e"))
    adapter = _adapter(tmp_path)
    store = InstanceStore(adapter.db_path).connect()
    runner = _FakeRunner()
    producer = _FakeProducer()
    plane = _FakePlane(runner, producer)
    executor = SimulationExecutor(settings, adapter, store, plane)
    executor.start()
    service = AdapterService(settings, adapter, store, executor)

    yield SimpleNamespace(
        settings=settings,
        adapter=adapter,
        store=store,
        runner=runner,
        producer=producer,
        plane=plane,
        executor=executor,
        service=service,
    )

    executor.stop()
    store.close()


# ─── 用例 ────────────────────────────────────────────────────────────────────


class TestControlPlaneE2E:
    def test_vil_chain_reaches_sensor_file(self, _stack: Any) -> None:
        stack = _stack
        # 1) create：实例入库并立即入队
        record = stack.service.create(
            CreateInstanceRequest(
                scene_id="scene-e2e",
                scene_name="端到端",
                sim_id="sim-e2e",
                mode="vil",
                vehicle_id="veh-1",
                scene_config=_scene_config(),
            )
        )
        assert record.sim_id == "sim-e2e"
        assert _wait_until(lambda: stack.runner.started == 1), "场景未启动"

        # 2) VIL 钩子已装配（位姿驱动 + 断流状态覆盖）
        hooks = stack.plane.hooks
        assert callable(hooks["pose_driver"])
        assert callable(hooks["status_provider"])
        assert hooks["status_provider"]() == ("paused", "vil_data_interrupted")
        assert hooks["frame_hub"] is not None
        assert hooks["recorder"] is not None

        # 3) 传感器帧经 FrameHub / mcap 落盘（录制器由执行器打开）
        recorder = hooks["recorder"]
        stack.runner.hooks["frame_hub"].push(
            "rgb", time.time(), b"\x01\x02\x03\x04", {"width": 2, "height": 1}
        )
        _wait_until(lambda: stack.runner.hooks["frame_hub"].latest("rgb") is not None)
        ts_ns = int(time.time() * _NS)
        assert recorder.push_stream(
            "rgb", ts_ns, build_image_message(b"\x01\x02\x03\x04", 2, 1, "bgra8", "cam", ts_ns)
        )

        # 4) 恢复后场景完成 → 终态 succeeded
        assert _wait_until(
            lambda: stack.store.get(record.instance_id).status == "running"  # type: ignore[union-attr]
        )
        stack.runner.snapshots = [_snapshot(SceneStatus.COMPLETED, 30.0)]
        assert _wait_until(
            lambda: stack.store.get(record.instance_id).status == "succeeded",  # type: ignore[union-attr]
        )

        # 5) sim_metrics 已发布
        assert _wait_until(lambda: bool(stack.producer.metrics))
        # 6) 录制文件关闭后上传 → sensor_file（字段取 RecordStats 真值）
        assert _wait_until(lambda: bool(stack.producer.sensor_files))
        payload = stack.producer.sensor_files[0]
        assert payload["file_format"] == "mcap"
        assert payload["scene_id"] == "scene-e2e"
        assert payload["vehicle_id"] == "veh-1"
        assert recorder.is_recording is False

    def test_cancel_path_closes_recording(self, _stack: Any) -> None:
        stack = _stack
        record = stack.service.create(
            CreateInstanceRequest(
                scene_id="scene-cancel",
                sim_id="sim-e2e",
                mode="sil",
                vehicle_id="veh-1",
                scene_config=_scene_config(),
            )
        )
        assert _wait_until(lambda: stack.runner.started == 1)
        recorder = stack.plane.hooks["recorder"]

        stack.service.stop(record.instance_id)

        assert _wait_until(
            lambda: stack.store.get(record.instance_id).status == "canceled"  # type: ignore[union-attr]
        )
        assert _wait_until(lambda: recorder.is_recording is False)

    def test_probes_health_reflects_active_instance(self, _stack: Any) -> None:
        stack = _stack
        record = stack.service.create(
            CreateInstanceRequest(
                scene_id="scene-health",
                sim_id="sim-e2e",
                mode="sil",
                vehicle_id="veh-1",
                scene_config=_scene_config(),
            )
        )
        assert _wait_until(lambda: stack.runner.started == 1)

        snapshot = stack.service.health_snapshot()

        assert snapshot["carla_connected"] is True
        active = snapshot["active_instance"]
        assert active is not None
        assert active["instance_id"] == record.instance_id
        assert snapshot["cloud"]["enabled"] is True

        stack.service.stop(record.instance_id)
