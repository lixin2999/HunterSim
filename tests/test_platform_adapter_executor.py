"""串行执行器测试（完成/失败/取消/看门狗/revision 合并）。

用假执行平面与假运行器隔离 CARLA/Kafka，仅验证适配器语义。
"""

from __future__ import annotations

import math
import threading
import time
from pathlib import Path
from typing import Any, Optional

import pytest

from hunter_sim.common.models import HunterSimSettings, SceneStatus
from hunter_sim.platform_adapter.config import AdapterSettings
from hunter_sim.platform_adapter.executor import SimulationExecutor
from hunter_sim.platform_adapter.models import InstanceRecord
from hunter_sim.platform_adapter.store import InstanceStore
from hunter_sim.scene_runner.scene_state_manager import SceneStateSnapshot

TERMINAL = {"succeeded", "failed", "canceled"}


def scene_config(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "map": {
            "map_id": "Town03",
            "spawn_point": {"x": 1.0, "y": 2.0, "z": 0.0, "yaw": math.pi / 2},
        },
        "duration": 30.0,
        "ego_vehicle": {"model": "hunter_se"},
        "actors": [
            {
                "actor_id": "a1",
                "type": "vehicle",
                "spawn_point": {"x": 5.0, "y": 5.0},
                "behavior": {"behavior_type": "static"},
            }
        ],
    }
    payload.update(overrides)
    return payload


class _FakeRunner:
    """假运行器：``start`` 后立即“结束”，由快照状态决定成功/失败。"""

    def __init__(self, snapshot: SceneStateSnapshot) -> None:
        self.scene: Any = None
        self.snapshot = snapshot
        self.is_running = False
        self.started = False
        self.paused = 0
        self.resumed = 0
        self.stops: list[float] = []
        self.detected_events: list[Any] = []
        self.on_state_change: Any = None
        self.hooks: dict[str, Any] = {}

    def load_scene(self, scene: Any) -> None:
        self.scene = scene

    def start(self) -> None:
        self.started = True
        self.is_running = False

    def pause(self) -> None:
        self.paused += 1

    def resume(self) -> None:
        self.resumed += 1

    def stop(self, timeout_s: float = 0.0) -> None:
        self.stops.append(timeout_s)
        self.is_running = False

    def get_state_snapshot(self) -> SceneStateSnapshot:
        return self.snapshot

    def get_ego_state(self) -> dict[str, Any]:
        return {"x": 1.0, "y": 2.0, "yaw": 0.0, "velocity": 3.0}


class _FakeProducer:
    """假 SimDataProducer（只记录指标/产物发布）。"""

    def __init__(self) -> None:
        self.metrics: list[dict[str, Any]] = []
        self.artifacts: list[dict[str, Any]] = []
        self._seq = 0

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def publish_metrics(self, payload: dict[str, Any]) -> None:
        self.metrics.append(payload)

    def publish_artifact(self, **payload: Any) -> None:
        self.artifacts.append(payload)


class _FakePlane:
    """假执行平面（只实现执行器实际调用的方法）。"""

    def __init__(self, runner: _FakeRunner) -> None:
        self.runner = runner
        self.ensure_calls: list[tuple[Any, dict[str, Any], Any]] = []
        self.released: list[str] = []
        self.release_all_calls = 0
        self.producer: Optional[_FakeProducer] = None
        self.fail_ensure: Optional[Exception] = None

    def ensure_runner(
        self,
        instance: Any,
        *,
        on_state_change: Any = None,
        on_event: Any = None,
        runner_hooks: Optional[dict[str, Any]] = None,
    ) -> _FakeRunner:
        if self.fail_ensure is not None:
            raise self.fail_ensure
        self.ensure_calls.append((instance, runner_hooks or {}, on_state_change))
        self.runner.on_state_change = on_state_change
        self.runner.hooks = runner_hooks or {}
        return self.runner

    def get_runner(self, instance_id: str) -> _FakeRunner:
        return self.runner

    def release(self, instance_id: str) -> None:
        self.released.append(instance_id)

    def release_all(self) -> None:
        self.release_all_calls += 1

    def producer_for_sim_id(self, sim_id: str) -> Optional[_FakeProducer]:
        return self.producer


@pytest.fixture
def store(tmp_path: Path) -> InstanceStore:
    instance_store = InstanceStore(str(tmp_path / "adapter.db")).connect()
    yield instance_store
    instance_store.close()


def _adapter_settings(tmp_path: Path) -> AdapterSettings:
    return AdapterSettings(
        db_path=str(tmp_path / "adapter.db"),
        artifacts_dir=str(tmp_path / "artifacts"),
        watchdog_factor=3.0,
        watchdog_grace_seconds=120.0,
    )


def _snapshot(status: SceneStatus, **overrides: Any) -> SceneStateSnapshot:
    payload: dict[str, Any] = {
        "scene_id": "scene-1",
        "status": status,
        "elapsed_seconds": 30.0,
        "progress": 1.0,
    }
    payload.update(overrides)
    return SceneStateSnapshot(**payload)


@pytest.fixture
def runner() -> _FakeRunner:
    return _FakeRunner(_snapshot(SceneStatus.COMPLETED))


@pytest.fixture
def plane(runner: _FakeRunner) -> _FakePlane:
    return _FakePlane(runner)


@pytest.fixture
def executor(
    test_settings: HunterSimSettings,
    tmp_path: Path,
    store: InstanceStore,
    plane: _FakePlane,
) -> SimulationExecutor:
    instance_executor = SimulationExecutor(
        test_settings, _adapter_settings(tmp_path), store, plane
    )
    yield instance_executor
    instance_executor.stop()


def _new_record(store: InstanceStore, **overrides: Any) -> InstanceRecord:
    payload: dict[str, Any] = {
        "instance_id": "inst-1",
        "sim_id": "sim-42",
        "scene_id": "scene-1",
        "scene_name": "测试场景",
        "mode": "sil",
        "scene_config": scene_config(),
        "total_time_s": 30.0,
        "actors_count": 2,
    }
    payload.update(overrides)
    record = InstanceRecord(**payload)
    store.insert(record)
    return record


def _wait_terminal(store: InstanceStore, instance_id: str, timeout: float = 5.0) -> InstanceRecord:
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = store.get(instance_id)
        if record is not None and record.status in TERMINAL:
            return record
        time.sleep(0.02)
    final = store.get(instance_id)
    raise AssertionError(f"instance did not finish: {final}")


def _wait_until(predicate: Any, timeout: float = 2.0) -> bool:
    """轮询等待条件成立（用于断言工作线程的收尾副作用）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


class TestExecution:
    def test_completed_scene_marks_succeeded(
        self,
        executor: SimulationExecutor,
        store: InstanceStore,
        plane: _FakePlane,
        runner: _FakeRunner,
    ) -> None:
        record = _new_record(store)
        executor.start()
        executor.submit(record)

        final = _wait_terminal(store, record.instance_id)
        assert final.status == "succeeded"
        assert final.carla_status == "COMPLETED"
        assert final.progress_percent == pytest.approx(100.0)
        assert final.current_time_s == pytest.approx(30.0)
        assert final.finished_at is not None
        assert final.watchdog_deadline == 0
        assert final.result is not None
        assert final.result["success"] is True
        assert [item["name"] for item in final.result["artifacts"]] == [
            "eval.json",
            "events.jsonl",
            "run.log",
        ]

        assert runner.started is True
        assert runner.scene is not None and runner.scene.scene_id == "scene-1"
        assert _wait_until(lambda: plane.released == [record.instance_id])
        plane_instance, hooks, _ = plane.ensure_calls[0]
        assert plane_instance.sim_instance_id == record.instance_id
        assert "sample_sink" in hooks  # SIL 也要采样
        assert "pose_driver" not in hooks

    def test_failed_snapshot_marks_failed(
        self,
        executor: SimulationExecutor,
        store: InstanceStore,
        runner: _FakeRunner,
    ) -> None:
        runner.snapshot = _snapshot(
            SceneStatus.FAILED, error_message="collision detected", progress=0.4
        )
        record = _new_record(store)
        executor.start()
        executor.submit(record)

        final = _wait_terminal(store, record.instance_id)
        assert final.status == "failed"
        assert final.carla_status == "FAILED"
        assert final.message == "collision detected"

    def test_unsupported_behavior_fails_loudly(
        self,
        executor: SimulationExecutor,
        store: InstanceStore,
        plane: _FakePlane,
    ) -> None:
        config = scene_config()
        config["actors"][0]["behavior"] = {"behavior_type": "teleport"}
        record = _new_record(store, scene_config=config)
        executor.start()
        executor.submit(record)

        final = _wait_terminal(store, record.instance_id)
        assert final.status == "failed"
        assert final.message == "unsupported_behavior:teleport"
        assert plane.ensure_calls == []  # 不会静默直线行驶

    def test_unsupported_mode_fails_loudly(
        self,
        executor: SimulationExecutor,
        store: InstanceStore,
        plane: _FakePlane,
    ) -> None:
        record = _new_record(store, mode="replay")
        executor.start()
        executor.submit(record)

        final = _wait_terminal(store, record.instance_id)
        assert final.status == "failed"
        assert final.message == "unsupported_mode:replay"
        assert plane.ensure_calls == []

    def test_stale_revision_skips_execution(
        self,
        executor: SimulationExecutor,
        store: InstanceStore,
        plane: _FakePlane,
    ) -> None:
        record = _new_record(store)
        executor.submit(record)
        stale = store.get(record.instance_id)
        assert stale is not None
        store.patch(record.instance_id, revision=stale.revision + 1)

        executor._run(stale)  # noqa: SLF001  直接验证过期版本被丢弃

        final = store.get(record.instance_id)
        assert plane.ensure_calls == []
        assert final is not None and final.carla_status == "CREATED"

    def test_publishes_metrics_and_artifacts(
        self,
        executor: SimulationExecutor,
        store: InstanceStore,
        plane: _FakePlane,
    ) -> None:
        producer = _FakeProducer()
        plane.producer = producer
        record = _new_record(store, vehicle_id="vehicle-1")
        executor.start()
        executor.submit(record)

        final = _wait_terminal(store, record.instance_id)
        assert final.status == "succeeded"
        assert _wait_until(lambda: bool(producer.metrics))
        assert len(producer.metrics) == 1
        metrics = producer.metrics[0]
        assert metrics["sim_id"] == "sim-42"
        assert metrics["type"] == "sim_metrics"
        assert metrics["payload"]["passed"] is True

        assert len(producer.artifacts) == 3
        assert {item["name"] for item in producer.artifacts} == {
            "eval.json",
            "events.jsonl",
            "run.log",
        }
        for item in producer.artifacts:
            assert item["bucket"] == "hunter-scene-assets"
            assert item["object_key"].startswith("scenarios/simulations/sim-42/scene-1/")
            assert "upload_url" not in item

    def test_vil_mode_installs_pose_hooks(
        self,
        executor: SimulationExecutor,
        store: InstanceStore,
        plane: _FakePlane,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from hunter_sim.platform_adapter import executor as executor_module

        created: list[_FakeVIL] = []

        class _Runtime(_FakeVIL):
            def __init__(self, settings: Any, **kwargs: Any) -> None:
                super().__init__(settings, **kwargs)
                created.append(self)

        monkeypatch.setattr(executor_module, "VILRuntime", _Runtime)
        record = _new_record(
            store,
            mode="vil",
            vehicle_id="vehicle-1",
            vil_config={"extrapolation_ms": 200},
        )
        executor.start()
        executor.submit(record)

        final = _wait_terminal(store, record.instance_id)
        assert final.status == "succeeded"
        _, hooks, _ = plane.ensure_calls[0]
        assert "pose_driver" in hooks
        assert "status_provider" in hooks
        assert hooks["pose_driver"] == created[0].step
        assert created[0].sim_id == "sim-42"
        assert created[0].vehicle_id == "vehicle-1"
        assert created[0].started == 1
        assert _wait_until(lambda: created[0].stopped == 1)
        assert created[0].stopped == 1

    def test_assembly_failure_marks_failed(
        self,
        executor: SimulationExecutor,
        store: InstanceStore,
        plane: _FakePlane,
    ) -> None:
        plane.fail_ensure = RuntimeError("engine unavailable")
        record = _new_record(store)
        executor.start()
        executor.submit(record)

        final = _wait_terminal(store, record.instance_id)
        assert final.status == "failed"
        assert final.message == "execution_unavailable:engine unavailable"


class _FakeVIL:
    """假 VIL 运行时（记录生命周期与钩子）。"""

    def __init__(self, settings: Any, **kwargs: Any) -> None:
        self.settings = settings
        self.sim_id = kwargs.get("sim_id")
        self.vehicle_id = kwargs.get("vehicle_id")
        self.vil_config = kwargs.get("vil_config")
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self, timeout_s: float = 3.0) -> None:
        self.stopped += 1

    def step(self, delta_seconds: float) -> None:
        return None

    def status_override(self) -> Optional[tuple[str, Optional[str]]]:
        return None


class TestControls:
    def test_submit_resets_terminal_state(self, store: InstanceStore, executor: SimulationExecutor) -> None:
        record = _new_record(store)
        store.patch(
            record.instance_id,
            status="succeeded",
            message="old",
            result={"success": True},
            finished_at=time.time(),
            progress_percent=100.0,
        )
        current = store.get(record.instance_id)
        assert current is not None
        expected_revision = current.revision + 1

        executor.submit(current)

        updated = store.get(record.instance_id)
        assert updated is not None
        assert updated.status == "pending"
        assert updated.carla_status == "CREATED"
        assert updated.message is None
        assert updated.result is None
        assert updated.finished_at is None
        assert updated.revision == expected_revision

    def test_pause_and_resume(
        self, store: InstanceStore, executor: SimulationExecutor, runner: _FakeRunner
    ) -> None:
        record = _new_record(store, status="running", carla_status="RUNNING")
        record = store.get(record.instance_id) or record
        executor._contexts[record.instance_id] = _context(record, runner)  # noqa: SLF001

        paused = executor.pause(record.instance_id)
        assert paused is not None
        assert paused.status == "running"
        assert paused.carla_status == "PAUSED"
        assert paused.message == "paused"
        assert runner.paused == 1

        resumed = executor.resume(record.instance_id)
        assert resumed is not None
        assert resumed.carla_status == "RUNNING"
        assert resumed.message is None
        assert runner.resumed == 1

    def test_cancel_marks_canceled(
        self, store: InstanceStore, executor: SimulationExecutor
    ) -> None:
        record = _new_record(store, status="running", carla_status="RUNNING")
        canceled = executor.cancel(record.instance_id, reason="canceled")
        assert canceled is not None
        assert canceled.status == "canceled"
        assert canceled.carla_status == "CANCELED"
        assert canceled.finished_at is not None

    def test_controls_are_noop_for_terminal_instances(
        self, store: InstanceStore, executor: SimulationExecutor
    ) -> None:
        record = _new_record(store, status="succeeded", carla_status="COMPLETED")
        assert executor.pause(record.instance_id) is not None
        assert executor.resume(record.instance_id) is not None
        assert executor.cancel(record.instance_id) is not None
        final = store.get(record.instance_id)
        assert final is not None and final.status == "succeeded"

    def test_unknown_instance_returns_none(self, executor: SimulationExecutor) -> None:
        assert executor.pause("missing") is None
        assert executor.resume("missing") is None
        assert executor.cancel("missing") is None

    def test_pause_requested_during_assembly(
        self,
        store: InstanceStore,
        executor: SimulationExecutor,
        runner: _FakeRunner,
    ) -> None:
        record = _new_record(store)
        executor._pause_requested.add(record.instance_id)  # noqa: SLF001
        assert executor._pause_requested_of(record.instance_id) is True  # noqa: SLF001


class TestStateCallbacks:
    def test_on_state_change_persists_paused(
        self, store: InstanceStore, executor: SimulationExecutor
    ) -> None:
        record = _new_record(store, status="running", carla_status="RUNNING")
        executor._scene_instances["scene-1"] = record.instance_id  # noqa: SLF001
        executor._on_state_change(  # noqa: SLF001
            _snapshot(SceneStatus.PAUSED, progress=0.5, elapsed_seconds=15.0)
        )

        current = store.get(record.instance_id)
        assert current is not None
        assert current.status == "running"
        assert current.carla_status == "PAUSED"
        assert current.message == "paused"
        assert current.progress_percent == pytest.approx(50.0)
        assert current.current_time_s == pytest.approx(15.0)

    def test_on_state_change_persists_failure(
        self, store: InstanceStore, executor: SimulationExecutor
    ) -> None:
        record = _new_record(store, status="running", carla_status="RUNNING")
        executor._scene_instances["scene-1"] = record.instance_id  # noqa: SLF001
        executor._on_state_change(  # noqa: SLF001
            _snapshot(SceneStatus.FAILED, progress=0.2, error_message="collision")
        )

        current = store.get(record.instance_id)
        assert current is not None
        assert current.status == "failed"
        assert current.carla_status == "FAILED"
        assert current.message == "collision"

    def test_on_state_change_ignores_unmapped_scene(
        self, store: InstanceStore, executor: SimulationExecutor
    ) -> None:
        record = _new_record(store, status="running")
        executor._on_state_change(_snapshot(SceneStatus.PAUSED))  # noqa: SLF001
        current = store.get(record.instance_id)
        assert current is not None and current.status == "running"

    def test_persist_progress_updates_ego_state(
        self, store: InstanceStore, executor: SimulationExecutor, runner: _FakeRunner
    ) -> None:
        record = _new_record(store, status="running", carla_status="RUNNING")
        runner.snapshot = _snapshot(SceneStatus.RUNNING, progress=0.5, elapsed_seconds=15.0)
        context = _context(record, runner)

        executor._persist_progress(context)  # noqa: SLF001

        current = store.get(record.instance_id)
        assert current is not None
        assert current.progress_percent == pytest.approx(50.0)
        assert current.current_time_s == pytest.approx(15.0)
        assert current.status == "running"
        assert current.carla_status == "RUNNING"
        assert current.ego_state["x"] == pytest.approx(1.0)
        assert context.last_progress_at > 0

    def test_await_completion_restart_leaves_instance_active(
        self, store: InstanceStore, executor: SimulationExecutor, runner: _FakeRunner
    ) -> None:
        record = _new_record(store, status="running")
        context = _context(record, runner)
        runner.is_running = True
        executor._stop_reasons[record.instance_id] = "restart"  # noqa: SLF001

        executor._await_completion(context)  # noqa: SLF001

        current = store.get(record.instance_id)
        assert current is not None and current.status == "running"

    def test_await_completion_canceled(
        self, store: InstanceStore, executor: SimulationExecutor, runner: _FakeRunner
    ) -> None:
        record = _new_record(store, status="running")
        context = _context(record, runner)
        runner.is_running = True
        executor._stop_reasons[record.instance_id] = "canceled"  # noqa: SLF001

        executor._await_completion(context)  # noqa: SLF001

        current = store.get(record.instance_id)
        assert current is not None
        assert current.status == "canceled"
        assert current.message == "canceled"
        assert current.finished_at is not None

    def test_await_completion_watchdog_timeout(
        self, store: InstanceStore, executor: SimulationExecutor, runner: _FakeRunner
    ) -> None:
        record = _new_record(store, status="running")
        context = _context(record, runner)
        runner.is_running = True
        executor._stop_reasons[record.instance_id] = "watchdog_timeout"  # noqa: SLF001

        executor._await_completion(context)  # noqa: SLF001

        current = store.get(record.instance_id)
        assert current is not None
        assert current.status == "failed"
        assert current.message == "watchdog_timeout"

    def test_fail_instance_ignores_terminal(
        self, store: InstanceStore, executor: SimulationExecutor
    ) -> None:
        record = _new_record(store, status="succeeded", carla_status="COMPLETED")
        executor._fail_instance(record.instance_id, "late failure")  # noqa: SLF001
        current = store.get(record.instance_id)
        assert current is not None
        assert current.status == "succeeded"
        assert current.message is None


class TestWatchdog:
    def test_watchdog_marks_expired_instance_failed(
        self, store: InstanceStore, executor: SimulationExecutor
    ) -> None:
        record = _new_record(store, status="running", carla_status="RUNNING")
        executor._deadlines[record.instance_id] = time.time() - 1.0  # noqa: SLF001
        thread = threading.Thread(target=executor._watchdog_loop, daemon=True)  # noqa: SLF001
        thread.start()
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline:
                current = store.get(record.instance_id)
                if current is not None and current.status == "failed":
                    break
                time.sleep(0.02)
        finally:
            executor._shutdown.set()  # noqa: SLF001
            thread.join(timeout=3.0)

        final = store.get(record.instance_id)
        assert final is not None
        assert final.status == "failed"
        assert final.message == "watchdog_timeout"
        assert final.watchdog_deadline == 0


def _context(record: InstanceRecord, runner: _FakeRunner) -> Any:
    """构造最小运行上下文（控制端点只读 ``runner``）。"""
    from hunter_sim.platform_adapter.executor import _RunContext  # noqa: PLC0415

    return _RunContext(record=record, scene=record.scene_config, runner=runner)
