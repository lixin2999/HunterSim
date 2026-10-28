"""适配器编排层测试（create/submit 幂等、create+submit 合并、控制委托）。"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Optional

import pytest

from hunter_sim.common.models import HunterSimSettings
from hunter_sim.platform_adapter.config import AdapterSettings
from hunter_sim.platform_adapter.models import (
    CreateInstanceRequest,
    InstanceRecord,
    PLATFORM_RUNNING,
    SubmitScenarioRequest,
)
from hunter_sim.platform_adapter.service import AdapterService
from hunter_sim.platform_adapter.store import InstanceStore


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


class _FakeExecutor:
    """记录调度调用的执行器替身（不启动线程/不接触 CARLA）。"""

    def __init__(self, store: InstanceStore) -> None:
        self._store = store
        self.submits: list[tuple[str, int, bool]] = []

    def submit(self, record: InstanceRecord, *, restart: bool = False) -> None:
        record.revision += 1
        self._store.update(record)
        self.submits.append((record.instance_id, record.revision, restart))

    def pause(self, instance_id: str) -> Optional[InstanceRecord]:
        self._store.patch(
            instance_id, status=PLATFORM_RUNNING, carla_status="PAUSED", message="paused"
        )
        return self._store.get(instance_id)

    def resume(self, instance_id: str) -> Optional[InstanceRecord]:
        self._store.patch(
            instance_id, status=PLATFORM_RUNNING, carla_status="RUNNING", message=None
        )
        return self._store.get(instance_id)

    def cancel(self, instance_id: str, reason: str = "canceled") -> Optional[InstanceRecord]:
        self._store.patch(
            instance_id, status="canceled", carla_status="CANCELED", message=reason
        )
        return self._store.get(instance_id)


@pytest.fixture
def store(tmp_path: Path) -> InstanceStore:
    store = InstanceStore(str(tmp_path / "adapter.db")).connect()
    yield store
    store.close()


@pytest.fixture
def executor(store: InstanceStore) -> _FakeExecutor:
    return _FakeExecutor(store)


@pytest.fixture
def service(
    test_settings: HunterSimSettings, store: InstanceStore, executor: _FakeExecutor
) -> AdapterService:
    adapter = AdapterSettings(
        db_path=str(store._db_path),  # noqa: SLF001  测试直接复用同一路径
        artifacts_dir="unused",
        watchdog_factor=3.0,
        watchdog_grace_seconds=120.0,
    )
    return AdapterService(test_settings, adapter, store, executor)


def _create_request(sim_id: str = "sim-1", **overrides: Any) -> CreateInstanceRequest:
    payload: dict[str, Any] = {
        "scene_id": "scene-1",
        "scene_name": "测试场景",
        "scene_config": scene_config(),
        "sim_id": sim_id,
        "mode": "sil",
        "quality": "high",
    }
    payload.update(overrides)
    return CreateInstanceRequest(**payload)


class TestCreate:
    def test_create_enqueues_immediately(
        self, service: AdapterService, executor: _FakeExecutor
    ) -> None:
        record = service.create(_create_request())
        assert record.sim_id == "sim-1"
        assert record.status == "pending"
        assert record.total_time_s == pytest.approx(30.0)
        assert record.actors_count == 2  # 1 个参与者 + 自车
        assert record.scene_config["quality"] == "high"
        assert executor.submits == [(record.instance_id, 1, False)]

    def test_idempotent_by_sim_id(
        self, service: AdapterService, executor: _FakeExecutor
    ) -> None:
        first = service.create(_create_request())
        second = service.create(_create_request())
        assert first.instance_id == second.instance_id
        assert len(executor.submits) == 1

    def test_idempotent_by_scene_id(
        self, service: AdapterService, executor: _FakeExecutor
    ) -> None:
        first = service.create(_create_request(sim_id="sim-1"))
        second = service.create(_create_request(sim_id="sim-2"))
        assert first.instance_id == second.instance_id
        assert len(executor.submits) == 1

    def test_missing_sim_id_is_generated(self, service: AdapterService) -> None:
        record = service.create(_create_request(sim_id=None))
        assert record.sim_id.startswith("sim-")


class TestSubmit:
    def test_same_config_is_coalesced(
        self, service: AdapterService, executor: _FakeExecutor
    ) -> None:
        record = service.create(_create_request())
        updated = service.submit(record.instance_id, SubmitScenarioRequest(scene_config=scene_config()))
        assert updated is not None
        assert len(executor.submits) == 1  # create 已入队，submit 不重复调度

    def test_changed_config_requeues(
        self, service: AdapterService, executor: _FakeExecutor
    ) -> None:
        record = service.create(_create_request())
        service.submit(
            record.instance_id,
            SubmitScenarioRequest(scene_config=scene_config(duration=45.0)),
        )
        assert len(executor.submits) == 2
        assert executor.submits[-1][2] is False  # 尚未运行，不需要中断
        latest = service.get(record.instance_id)
        assert latest is not None
        assert latest.total_time_s == pytest.approx(45.0)

    def test_changed_config_while_running_restarts(
        self, service: AdapterService, executor: _FakeExecutor, store: InstanceStore
    ) -> None:
        record = service.create(_create_request())
        store.patch(record.instance_id, status=PLATFORM_RUNNING)
        service.submit(
            record.instance_id,
            SubmitScenarioRequest(scene_config=scene_config(duration=45.0)),
        )
        assert executor.submits[-1][2] is True

    def test_submit_unknown_instance(self, service: AdapterService) -> None:
        assert service.submit("missing", SubmitScenarioRequest()) is None


class TestQueriesAndControls:
    def test_list_and_result(
        self, service: AdapterService, store: InstanceStore
    ) -> None:
        record = service.create(_create_request())
        assert [r.instance_id for r in service.list("scene-1")] == [record.instance_id]
        assert [r.instance_id for r in service.list()] == [record.instance_id]
        assert service.list("missing") == []

        store.patch(record.instance_id, result={"success": True, "evaluation": {"grade": "A"}})
        view = service.result(record.instance_id)
        assert view is not None
        assert view.to_result_view()["success"] is True
        assert view.to_result_view()["evaluation"]["grade"] == "A"

    def test_controls_delegate(
        self, service: AdapterService, store: InstanceStore
    ) -> None:
        record = service.create(_create_request())
        store.patch(record.instance_id, status=PLATFORM_RUNNING)

        paused = service.pause(record.instance_id)
        assert paused is not None and paused.message == "paused"
        resumed = service.resume(record.instance_id)
        assert resumed is not None and resumed.carla_status == "RUNNING"
        stopped = service.stop(record.instance_id)
        assert stopped is not None and stopped.status == "canceled"

    def test_unknown_instance(self, service: AdapterService) -> None:
        assert service.get("missing") is None
        assert service.pause("missing") is None
        assert service.stop("missing") is None
