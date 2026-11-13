"""V3.8.18 面板异常修复专项单测（A1–A6）。

覆盖：
- A1 时钟统一：``Frame.wall_ts`` 墙钟、``sensors()`` 帧龄/双时间戳字段；
- A2 评估结论上屏：``to_view().evaluation``（succeeded ≠ 评估通过）；
- A3 运行期录制态：``recording_active`` 持久化、旧库迁移、进度回写；
- A4 终态快照：释放前落库、终态回退读 actors/events；
- A5 脚本横穿楔死：行人物理禁用 + 距离接触补报碰撞（去重/复位）；
- A6 诊断：丢帧计数、标称帧率、ROS2 reason 三态。

用桩件隔离 CARLA/Kafka，仅验证适配器与场景语义。
"""

from __future__ import annotations

import queue
import sqlite3
import time
import types
from pathlib import Path
from typing import Any, Optional

import pytest

from hunter_sim.common.models import HunterSimSettings, SceneStatus
from hunter_sim.platform_adapter.config import AdapterSettings
from hunter_sim.platform_adapter.executor import SimulationExecutor, _RunContext
from hunter_sim.platform_adapter.models import InstanceRecord
from hunter_sim.platform_adapter.service import AdapterService
from hunter_sim.platform_adapter.store import InstanceStore
from hunter_sim.scene_runner.builtin_scenario import (
    _SCRIPTED_WALKER_CONTACT_M,
    BuiltinScenario,
    _ScriptedWalker,
)
from hunter_sim.scene_runner.event_detector import EventDetector
from hunter_sim.scene_runner.scene_config import (
    SceneConfig,
    SpawnPoint,
    TrafficParticipantConfig,
)
from hunter_sim.scene_runner.scene_state_manager import (
    SceneStateManager,
    SceneStateSnapshot,
)
from hunter_sim.sensor_sim.frame_hub import STREAM_RGB, FrameHub
from tests.mocks.carla_mocks import MockActor, MockLocation, MockTransform

TERMINAL = {"succeeded", "failed", "canceled"}

_SCENE_PAYLOAD: dict[str, Any] = {
    "scene_id": "scene-v3811",
    "map_id": "Town03",
    "duration_seconds": 30.0,
    "ego_vehicle": {
        "vehicle_blueprint": "hunter_se",
        "spawn_point": {"x": 1.0, "y": 2.0},
        "autopilot": True,
    },
    "traffic_participants": [],
}


def _scene_config() -> SceneConfig:
    return SceneConfig(**_SCENE_PAYLOAD)


def _platform_scene_config() -> dict[str, Any]:
    """平台下发格式（executor 校验 map/duration/actors，与运行器格式不同）。"""
    return {
        "map": {
            "map_id": "Town03",
            "spawn_point": {"x": 1.0, "y": 2.0, "z": 0.0, "yaw": 0.0},
        },
        "duration": 30.0,
        "ego_vehicle": {"model": "hunter_se"},
        "actors": [],
    }


def _record_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "instance_id": "inst-1",
        "sim_id": "sim-42",
        "scene_id": "scene-v3811",
        "scene_name": "面板修复回归",
        "mode": "sil",
        "scene_config": _platform_scene_config(),
        "total_time_s": 30.0,
        "actors_count": 1,
    }
    payload.update(overrides)
    return payload


def _new_record(store: InstanceStore, **overrides: Any) -> InstanceRecord:
    record = InstanceRecord(**_record_payload(**overrides))
    store.insert(record)
    return record


def _adapter_settings(tmp_path: Path) -> AdapterSettings:
    return AdapterSettings(
        db_path=str(tmp_path / "adapter.db"),
        artifacts_dir=str(tmp_path / "artifacts"),
        watchdog_factor=3.0,
        watchdog_grace_seconds=120.0,
    )


class _StubPlane:
    """最小执行平面桩（只实现执行器构造/收尾实际调用的方法）。"""

    def __init__(self, runner: Any = None) -> None:
        self.runner = runner
        self.released: list[str] = []
        self.release_all_calls = 0

    def ensure_runner(
        self,
        instance: Any,
        *,
        on_state_change: Any = None,
        on_event: Any = None,
        runner_hooks: Optional[dict[str, Any]] = None,
    ) -> Any:
        if self.runner is not None:
            self.runner.on_state_change = on_state_change
            self.runner.hooks = runner_hooks or {}
        return self.runner

    def get_runner(self, instance_id: str) -> Any:
        return self.runner

    def release(self, instance_id: str) -> None:
        self.released.append(instance_id)

    def release_all(self) -> None:
        self.release_all_calls += 1

    def producer_for_sim_id(self, sim_id: str) -> None:
        return None


@pytest.fixture
def store(tmp_path: Path) -> InstanceStore:
    instance_store = InstanceStore(str(tmp_path / "adapter.db")).connect()
    yield instance_store
    instance_store.close()


@pytest.fixture
def executor(
    test_settings: HunterSimSettings, tmp_path: Path, store: InstanceStore
) -> SimulationExecutor:
    instance_executor = SimulationExecutor(
        test_settings, _adapter_settings(tmp_path), store, _StubPlane()
    )
    yield instance_executor
    instance_executor.stop()


def _make_scenario() -> BuiltinScenario:
    config = _scene_config()
    return BuiltinScenario(
        config,
        SceneStateManager(config.scene_id),
        EventDetector(config.scene_events),
        transform_factory=lambda _world, sp: MockTransform(
            MockLocation(sp.x, sp.y, sp.z or 0.5)
        ),
    )


def _context(
    record: InstanceRecord, runner: Any = None, **overrides: Any
) -> _RunContext:
    context = _RunContext(record=record, scene=_scene_config(), runner=runner)
    for key, value in overrides.items():
        setattr(context, key, value)
    return context


def _collision_event() -> Any:
    return types.SimpleNamespace(
        event_id="collision_detected",
        event_type="collision",
        time_stamp=1.0,
        sim_time=1.0,
        description="Collision with actor 7",
        event_level="warning",
    )


# ─── A1 时钟统一 ─────────────────────────────────────────────────────────────


class TestClockUnification:
    def test_push_records_wall_ts_and_keeps_sim_ts(self) -> None:
        hub = FrameHub()
        before = time.time()
        frame = hub.push(STREAM_RGB, 100.0, b"payload")
        after = time.time()
        assert frame is not None
        # ts 保留仿真经过秒（fps 滑窗语义不变），wall_ts 是入汇聚中心的墙钟
        assert frame.ts == pytest.approx(100.0)
        assert before <= frame.wall_ts <= after

    def test_sensors_age_uses_wall_ts_not_sim_ts(
        self,
        test_settings: HunterSimSettings,
        tmp_path: Path,
        store: InstanceStore,
    ) -> None:
        hub = FrameHub()
        hub.push(STREAM_RGB, 100.0, b"payload")  # 仿真秒 100，绝非 epoch
        service = AdapterService(
            test_settings,
            _adapter_settings(tmp_path),
            store,
            _StubPlane(),
            frame_hub_provider=lambda _id: hub,
        )
        view = service.sensors("inst-1")
        frame = view["streams"][STREAM_RGB]["frame"]
        assert frame is not None
        # 旧版 now - 仿真秒 ≈ 1.8e9 s；修复后帧龄必须接近 0
        assert frame["age_s"] < 2.0
        assert frame["sim_ts"] == pytest.approx(100.0)
        assert frame["ts"] == pytest.approx(100.0)
        assert frame["wall_ts"] == pytest.approx(time.time(), abs=5.0)

    def test_sensors_wall_ts_zero_falls_back(
        self,
        test_settings: HunterSimSettings,
        tmp_path: Path,
        store: InstanceStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("hunter_sim.sensor_sim.frame_hub.now_ts", lambda: 0.0)
        hub = FrameHub()
        hub.push(STREAM_RGB, 100.0, b"payload")
        service = AdapterService(
            test_settings,
            _adapter_settings(tmp_path),
            store,
            _StubPlane(),
            frame_hub_provider=lambda _id: hub,
        )
        frame = service.sensors("inst-1")["streams"][STREAM_RGB]["frame"]
        assert frame["wall_ts"] is None
        assert frame["age_s"] == 0.0


# ─── A2 评估结论上屏 ─────────────────────────────────────────────────────────


class TestEvaluationView:
    def test_no_result_returns_none(self) -> None:
        record = InstanceRecord(**_record_payload())
        assert record.to_view()["evaluation"] is None

    def test_failed_evaluation_is_not_passed(self) -> None:
        record = InstanceRecord(
            **_record_payload(
                status="succeeded",
                result={
                    "success": False,
                    "evaluation": {
                        "grade": "D",
                        "success_criteria": {
                            "min_distance": {"passed": False, "actual": 0.4},
                        },
                    },
                },
            )
        )
        view = record.to_view()
        # 平台状态 succeeded 不得掩盖评估 fail
        assert view["status"] == "succeeded"
        evaluation = view["evaluation"]
        assert evaluation is not None
        assert evaluation["passed"] is False
        assert evaluation["grade"] == "D"
        assert evaluation["success_criteria"]["min_distance"]["passed"] is False

    def test_success_flag_drives_passed(self) -> None:
        record = InstanceRecord(
            **_record_payload(
                status="succeeded",
                result={
                    "success": True,
                    "evaluation": {"grade": "A", "passed": False},
                },
            )
        )
        evaluation = record.to_view()["evaluation"]
        assert evaluation is not None
        assert evaluation["passed"] is True

    def test_empty_evaluation_returns_none(self) -> None:
        record = InstanceRecord(
            **_record_payload(result={"success": True, "evaluation": {}})
        )
        assert record.to_view()["evaluation"] is None


# ─── A3 运行期录制态 ─────────────────────────────────────────────────────────


class TestRecordingState:
    def test_roundtrip_and_patch(self, store: InstanceStore) -> None:
        record = _new_record(store, recording_active=True, recording_bytes=2048)
        loaded = store.get(record.instance_id)
        assert loaded is not None
        assert loaded.recording_active is True
        assert loaded.recording_bytes == 2048
        store.patch(record.instance_id, recording_active=False)
        reloaded = store.get(record.instance_id)
        assert reloaded is not None
        assert reloaded.recording_active is False

    def test_default_false_and_view(self) -> None:
        record = InstanceRecord(**_record_payload())
        assert record.recording_active is False
        assert record.to_view()["recording_active"] is False

    def test_legacy_db_migration(self, tmp_path: Path) -> None:
        """V3.8.10 旧库（无 recording_active 列）就地升级且默认 False。"""
        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """
            CREATE TABLE instances (
                instance_id TEXT PRIMARY KEY,
                sim_id TEXT NOT NULL,
                scene_id TEXT NOT NULL,
                scene_name TEXT NOT NULL DEFAULT '',
                mode TEXT NOT NULL DEFAULT 'sil',
                vehicle_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                carla_status TEXT NOT NULL DEFAULT 'CREATED',
                message TEXT,
                progress_percent REAL NOT NULL DEFAULT 0,
                current_time_s REAL NOT NULL DEFAULT 0,
                total_time_s REAL NOT NULL DEFAULT 0,
                actors_count INTEGER NOT NULL DEFAULT 0,
                collisions INTEGER NOT NULL DEFAULT 0,
                lane_invasions INTEGER NOT NULL DEFAULT 0,
                ego_state TEXT NOT NULL DEFAULT '{}',
                scene_config TEXT NOT NULL DEFAULT '{}',
                vil_config TEXT,
                result TEXT,
                recording_path TEXT NOT NULL DEFAULT '',
                recording_bytes INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                watchdog_deadline REAL NOT NULL DEFAULT 0
            )
            """
        )
        now = time.time()
        conn.execute(
            "INSERT INTO instances (instance_id, sim_id, scene_id, created_at,"
            " updated_at) VALUES (?, ?, ?, ?, ?)",
            ("inst-legacy", "sim-1", "scene-1", now, now),
        )
        conn.commit()
        conn.close()

        migrated = InstanceStore(str(db_path)).connect()
        try:
            record = migrated.get("inst-legacy")
            assert record is not None
            assert record.recording_active is False
            migrated.patch("inst-legacy", recording_active=True)
            reloaded = migrated.get("inst-legacy")
            assert reloaded is not None
            assert reloaded.recording_active is True
        finally:
            migrated.close()

    def test_progress_writes_recording_state(
        self, executor: SimulationExecutor, store: InstanceStore
    ) -> None:
        record = _new_record(store)
        runner = types.SimpleNamespace(
            get_state_snapshot=lambda: SceneStateSnapshot(
                scene_id="scene-v3811",
                status=SceneStatus.RUNNING,
                elapsed_seconds=5.0,
                progress=0.2,
            ),
            detected_events=[],
            get_ego_state=lambda: {"velocity": 3.0},
        )
        context = _context(
            record,
            runner,
            recorder=types.SimpleNamespace(is_recording=True, bytes_written=4096),
            last_progress_at=0.0,
        )
        executor._persist_progress(context)  # noqa: SLF001
        loaded = store.get(record.instance_id)
        assert loaded is not None
        assert loaded.recording_active is True
        assert loaded.recording_bytes == 4096

        # 录制停止后进度回写必须翻转为 False（不再常驻「录制中」）
        context.recorder = None
        context.recording_stopped = "disk_gate"
        context.last_progress_at = 0.0
        executor._persist_progress(context)  # noqa: SLF001
        stopped = store.get(record.instance_id)
        assert stopped is not None
        assert stopped.recording_active is False


# ─── A4 终态快照 ─────────────────────────────────────────────────────────────


class _SnapshotRunner:
    """带终态缓存的运行器桩（actor_states/detected_events 供快照读取）。"""

    def __init__(self) -> None:
        self.scene: Any = None
        self.is_running = False
        self.started = False
        self.on_state_change: Any = None
        self.hooks: dict[str, Any] = {}
        self.detected_events = [_collision_event()]

    def load_scene(self, scene: Any) -> None:
        self.scene = scene

    def start(self) -> None:
        self.started = True
        self.is_running = False

    def pause(self) -> None:
        pass

    def resume(self) -> None:
        pass

    def stop(self, timeout_s: float = 0.0) -> None:
        self.is_running = False

    def get_state_snapshot(self) -> SceneStateSnapshot:
        return SceneStateSnapshot(
            scene_id="scene-v3811",
            status=SceneStatus.COMPLETED,
            elapsed_seconds=30.0,
            progress=1.0,
        )

    def get_ego_state(self) -> dict[str, Any]:
        return {"x": 1.0, "y": 2.0, "yaw": 0.0, "velocity": 0.0}

    def actor_states(self) -> list[dict[str, Any]]:
        return [{"actor_id": "a1", "actor_type": "walker", "x": 5.0, "y": 5.0}]


class TestTerminalSnapshot:
    def test_persist_merges_into_result(
        self, executor: SimulationExecutor, store: InstanceStore
    ) -> None:
        record = _new_record(store, status="succeeded", result={"success": True})
        context = _context(record, _SnapshotRunner())
        executor._persist_terminal_snapshot(context)  # noqa: SLF001
        saved = store.get(record.instance_id)
        assert saved is not None and saved.result is not None
        # 追加字段不得覆盖既有 result
        assert saved.result["success"] is True
        snapshot = saved.result["terminal_snapshot"]
        assert snapshot["actors"][0]["actor_id"] == "a1"
        assert snapshot["events"][0]["event_type"] == "collision"
        assert snapshot["captured_at"] > 0

    def test_terminal_fallback_read(
        self, executor: SimulationExecutor, store: InstanceStore
    ) -> None:
        record = _new_record(store, status="succeeded", result={"success": True})
        executor._persist_terminal_snapshot(  # noqa: SLF001
            _context(record, _SnapshotRunner())
        )
        # 无活动上下文（运行器已释放）→ 回退读快照，面板终态不再空
        actors = executor.actors(record.instance_id)
        events = executor.detected_events(record.instance_id)
        assert [item["actor_id"] for item in actors] == ["a1"]
        assert [item["event_type"] for item in events] == ["collision"]

    def test_non_terminal_returns_empty(
        self, executor: SimulationExecutor, store: InstanceStore
    ) -> None:
        record = _new_record(
            store,
            status="running",
            result={"terminal_snapshot": {"actors": [{"actor_id": "a1"}]}},
        )
        assert executor.actors(record.instance_id) == []
        assert executor.detected_events(record.instance_id) == []

    def test_missing_record_returns_empty(
        self, executor: SimulationExecutor
    ) -> None:
        assert executor.actors("no-such-instance") == []
        assert executor.detected_events("no-such-instance") == []

    def test_snapshot_survives_release(
        self,
        test_settings: HunterSimSettings,
        tmp_path: Path,
        store: InstanceStore,
    ) -> None:
        """端到端：submit → 终态 → plane.release 后 actors/events 仍可读。"""
        runner = _SnapshotRunner()
        plane = _StubPlane(runner)
        instance_executor = SimulationExecutor(
            test_settings, _adapter_settings(tmp_path), store, plane
        )
        try:
            record = _new_record(store)
            instance_executor.start()
            instance_executor.submit(record)
            deadline = time.time() + 5.0
            final: Optional[InstanceRecord] = None
            while time.time() < deadline:
                final = store.get(record.instance_id)
                if final is not None and final.status in TERMINAL:
                    break
                time.sleep(0.02)
            assert final is not None and final.status == "succeeded"
            # 终态落库与 finally 收尾（快照落库 → plane.release）存在时间窗，
            # 必须轮询等待 release 完成（快照先于 release，release 后快照必已可读）
            release_deadline = time.time() + 5.0
            while (
                time.time() < release_deadline
                and plane.released != [record.instance_id]
            ):
                time.sleep(0.02)
            assert plane.released == [record.instance_id]
            assert instance_executor.actors(record.instance_id) != []
            assert instance_executor.detected_events(record.instance_id) != []
        finally:
            instance_executor.stop()


# ─── A5 脚本横穿楔死修复 ─────────────────────────────────────────────────────


class _PhysicsWalker(MockActor):
    """记录 set_simulate_physics 调用的行人 Actor 桩。"""

    def __init__(self) -> None:
        super().__init__("walker.pedestrian.0001")
        self.physics_calls: list[bool] = []

    def set_simulate_physics(self, enabled: bool) -> None:
        self.physics_calls.append(enabled)


def _walker_at(x: float, y: float) -> _ScriptedWalker:
    actor = MockActor("walker.pedestrian.0001")
    actor._transform = MockTransform(MockLocation(x, y, 0.5))  # noqa: SLF001
    return _ScriptedWalker(
        actor=actor,
        destination=(x, y + 10.0, 0.5),
        speed_ms=1.5,
        carla_module=types.ModuleType("carla"),
        spawn=SpawnPoint(x=x, y=y),
    )


def _ego_at(x: float, y: float) -> MockActor:
    ego = MockActor("vehicle.hunter_se")
    ego._transform = MockTransform(MockLocation(x, y, 0.5))  # noqa: SLF001
    return ego


class TestScriptedWalkerPhysics:
    def test_register_disables_physics(self) -> None:
        scenario = _make_scenario()
        actor = _PhysicsWalker()
        participant = TrafficParticipantConfig(
            participant_id="w1",
            actor_type="walker",
            spawn_point=SpawnPoint(x=0.0, y=0.0),
        )
        scenario._register_scripted_walker(  # noqa: SLF001
            actor,
            SpawnPoint(x=0.0, y=0.0),
            types.SimpleNamespace(x=0.0, y=10.0, z=0.5),
            1.5,
            participant,
            types.ModuleType("carla"),
        )
        assert actor.physics_calls == [False]
        assert scenario._scripted_walkers[-1].contact_reported is False  # noqa: SLF001

    def test_register_without_physics_setter_degrades(self) -> None:
        """旧版服务端/桩无 set_simulate_physics 时不得抛异常。"""
        scenario = _make_scenario()
        actor = MockActor("walker.pedestrian.0001")
        participant = TrafficParticipantConfig(
            participant_id="w1",
            actor_type="walker",
            spawn_point=SpawnPoint(x=0.0, y=0.0),
        )
        scenario._register_scripted_walker(  # noqa: SLF001
            actor,
            SpawnPoint(x=0.0, y=0.0),
            types.SimpleNamespace(x=0.0, y=10.0, z=0.5),
            1.5,
            participant,
            types.ModuleType("carla"),
        )
        assert len(scenario._scripted_walkers) == 1  # noqa: SLF001

    def test_contact_report_once_then_reset(self) -> None:
        scenario = _make_scenario()
        scenario._ego = _ego_at(0.0, 0.0)  # noqa: SLF001
        detector = scenario._event_detector  # noqa: SLF001
        walker = _walker_at(1.0, 0.0)  # gap=1.0 ≤ 1.5 → 接触

        scenario._report_scripted_walker_contact(walker)  # noqa: SLF001
        assert walker.contact_reported is True
        assert len(detector.detected_events) == 1

        # 持续接触不得逐 tick 重复计数
        scenario._report_scripted_walker_contact(walker)  # noqa: SLF001
        assert len(detector.detected_events) == 1

        # 离开 2 倍阈值 → 复位去重标记（不新增事件）
        walker.actor._transform = MockTransform(  # noqa: SLF001
            MockLocation(5.0, 0.0, 0.5)
        )
        scenario._report_scripted_walker_contact(walker)  # noqa: SLF001
        assert walker.contact_reported is False
        assert len(detector.detected_events) == 1

        # 再次接触可再报
        walker.actor._transform = MockTransform(  # noqa: SLF001
            MockLocation(1.0, 0.0, 0.5)
        )
        scenario._report_scripted_walker_contact(walker)  # noqa: SLF001
        assert len(detector.detected_events) == 2

    def test_mid_gap_neither_reports_nor_resets(self) -> None:
        scenario = _make_scenario()
        scenario._ego = _ego_at(0.0, 0.0)  # noqa: SLF001
        walker = _walker_at(1.0, 0.0)
        scenario._report_scripted_walker_contact(walker)  # noqa: SLF001
        assert walker.contact_reported is True
        # 1.5 < gap=2.0 ≤ 3.0：既不补报也不复位
        walker.actor._transform = MockTransform(  # noqa: SLF001
            MockLocation(2.0, 0.0, 0.5)
        )
        scenario._report_scripted_walker_contact(walker)  # noqa: SLF001
        assert walker.contact_reported is True
        assert len(scenario._event_detector.detected_events) == 1  # noqa: SLF001

    def test_no_ego_no_report(self) -> None:
        scenario = _make_scenario()
        walker = _walker_at(0.0, 0.0)
        scenario._report_scripted_walker_contact(walker)  # noqa: SLF001
        assert walker.contact_reported is False
        assert scenario._event_detector.detected_events == []  # noqa: SLF001

    def test_contact_threshold_constant(self) -> None:
        assert _SCRIPTED_WALKER_CONTACT_M == pytest.approx(1.5)


# ─── A6 丢帧/标称 fps 与 ROS reason ──────────────────────────────────────────


class _DiagExecutor:
    """带诊断/ROS 视图的执行器替身（供 AdapterService 只读视图测试）。"""

    def __init__(
        self,
        diagnostics: Optional[dict[str, Any]] = None,
        ros: Optional[dict[str, Any]] = None,
    ) -> None:
        self._diagnostics = diagnostics
        self._ros = ros

    def sensor_diagnostics(self, instance_id: str) -> dict[str, Any]:
        if self._diagnostics is None:
            raise RuntimeError("runner released")
        return self._diagnostics

    def ros_status(self, instance_id: str) -> dict[str, Any]:
        return self._ros or {"enabled": False, "reason": "disabled", "topics": {}}


class TestSensorDiagnostics:
    def test_scenario_diagnostics_returns_copy(self) -> None:
        scenario = _make_scenario()
        scenario._frame_drops["rgb"] = 3  # noqa: SLF001
        scenario._nominal_fps["rgb"] = 20.0  # noqa: SLF001
        diag = scenario.sensor_diagnostics
        assert diag == {"dropped": {"rgb": 3}, "nominal_fps": {"rgb": 20.0}}
        diag["dropped"]["rgb"] = 99
        assert scenario._frame_drops["rgb"] == 3  # noqa: SLF001

    def test_queue_full_increments_drop_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scenario = _make_scenario()
        # 不启动编码线程：只验证队满丢帧计数路径
        monkeypatch.setattr(scenario, "_ensure_frame_worker", lambda: None)
        scenario._frame_accepting = True  # noqa: SLF001
        scenario._frame_queue = queue.Queue(maxsize=1)  # noqa: SLF001
        scenario._frame_queue.put_nowait(object())  # noqa: SLF001
        data = types.SimpleNamespace(
            data=bytes(4 * 2 * 4), width=4, height=2, timestamp=100.0
        )
        scenario._on_sensor_data(STREAM_RGB, data)  # noqa: SLF001
        assert scenario._frame_drops[STREAM_RGB] == 1  # noqa: SLF001
        assert scenario._frame_pending == 0  # noqa: SLF001
        scenario._on_sensor_data(STREAM_RGB, data)  # noqa: SLF001
        assert scenario._frame_drops[STREAM_RGB] == 2  # noqa: SLF001

    def test_executor_diagnostics_passthrough_and_empty(
        self, executor: SimulationExecutor, store: InstanceStore
    ) -> None:
        # 无活动上下文 → 空诊断（不抛异常）
        assert executor.sensor_diagnostics("inst-1") == {
            "dropped": {},
            "nominal_fps": {},
        }
        record = _new_record(store)
        runner = types.SimpleNamespace(
            sensor_diagnostics=lambda: {
                "dropped": {"rgb": 2},
                "nominal_fps": {"rgb": 20.0},
            }
        )
        executor._contexts[record.instance_id] = _context(record, runner)  # noqa: SLF001
        assert executor.sensor_diagnostics(record.instance_id) == {
            "dropped": {"rgb": 2},
            "nominal_fps": {"rgb": 20.0},
        }

    def test_executor_diagnostics_swallows_runner_error(
        self, executor: SimulationExecutor, store: InstanceStore
    ) -> None:
        record = _new_record(store)

        def _boom() -> dict[str, Any]:
            raise RuntimeError("runner released")

        runner = types.SimpleNamespace(sensor_diagnostics=_boom)
        executor._contexts[record.instance_id] = _context(record, runner)  # noqa: SLF001
        assert executor.sensor_diagnostics(record.instance_id) == {
            "dropped": {},
            "nominal_fps": {},
        }

    def test_service_sensors_merges_diagnostics(
        self,
        test_settings: HunterSimSettings,
        tmp_path: Path,
        store: InstanceStore,
    ) -> None:
        hub = FrameHub()
        hub.push(STREAM_RGB, 100.0, b"payload")
        diag_executor = _DiagExecutor(
            diagnostics={"dropped": {"rgb": 2}, "nominal_fps": {"rgb": 20.0}}
        )
        service = AdapterService(
            test_settings,
            _adapter_settings(tmp_path),
            store,
            diag_executor,
            frame_hub_provider=lambda _id: hub,
        )
        streams = service.sensors("inst-1")["streams"]
        assert streams[STREAM_RGB]["dropped_frames"] == 2
        assert streams[STREAM_RGB]["nominal_fps"] == pytest.approx(20.0)
        # 无诊断数据的流：丢帧 0、标称 None（面板显示「—」）
        assert streams["depth"]["dropped_frames"] == 0
        assert streams["depth"]["nominal_fps"] is None

    def test_service_sensors_tolerates_diagnostics_error(
        self,
        test_settings: HunterSimSettings,
        tmp_path: Path,
        store: InstanceStore,
    ) -> None:
        hub = FrameHub()
        hub.push(STREAM_RGB, 100.0, b"payload")
        service = AdapterService(
            test_settings,
            _adapter_settings(tmp_path),
            store,
            _DiagExecutor(diagnostics=None),  # getter 抛异常
            frame_hub_provider=lambda _id: hub,
        )
        streams = service.sensors("inst-1")["streams"]
        assert streams[STREAM_RGB]["dropped_frames"] == 0


class TestRosReason:
    def test_no_context_reports_disabled(
        self, executor: SimulationExecutor
    ) -> None:
        status = executor.ros_status("inst-1")
        assert status == {"enabled": False, "reason": "disabled", "topics": {}}

    def test_unavailable_reason_passthrough(
        self, executor: SimulationExecutor, store: InstanceStore
    ) -> None:
        record = _new_record(store)
        executor._contexts[record.instance_id] = _context(  # noqa: SLF001
            record, ros_reason="unavailable"
        )
        status = executor.ros_status(record.instance_id)
        assert status["enabled"] is False
        assert status["reason"] == "unavailable"

    def test_enabled_bridge_reports_counts(
        self, executor: SimulationExecutor, store: InstanceStore
    ) -> None:
        record = _new_record(store)
        bridge = types.SimpleNamespace(
            is_initialized=True, message_counts={"/hunter/rgb": 5}
        )
        executor._contexts[record.instance_id] = _context(  # noqa: SLF001
            record, ros_bridge=bridge, ros_reason="enabled"
        )
        status = executor.ros_status(record.instance_id)
        assert status["enabled"] is True
        assert status["reason"] == "enabled"
        assert status["topics"] == {"/hunter/rgb": 5}

    def test_service_ros_passthrough_reason(
        self,
        test_settings: HunterSimSettings,
        tmp_path: Path,
        store: InstanceStore,
    ) -> None:
        service = AdapterService(
            test_settings,
            _adapter_settings(tmp_path),
            store,
            _DiagExecutor(
                ros={"enabled": False, "reason": "unavailable", "topics": {}}
            ),
        )
        status = service.ros("inst-1")
        assert status["enabled"] is False
        assert status["reason"] == "unavailable"
        assert "mappings" in status
