"""适配器实例注册表测试（SQLite 持久化 / 幂等 / 重启恢复）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from hunter_sim.platform_adapter.models import (
    InstanceRecord,
    PLATFORM_FAILED,
    PLATFORM_PENDING,
    PLATFORM_RUNNING,
)
from hunter_sim.platform_adapter.store import InstanceStore


def _record(instance_id: str = "inst-1", scene_id: str = "scene-1") -> InstanceRecord:
    return InstanceRecord(
        instance_id=instance_id,
        sim_id=f"sim-{instance_id}",
        scene_id=scene_id,
        scene_name="测试场景",
        mode="sil",
        scene_config={"map": {"map_id": "Town03"}},
    )


def _store(tmp_path: Path) -> InstanceStore:
    return InstanceStore(str(tmp_path / "adapter.db")).connect()


def test_insert_and_get(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        record = _record()
        store.insert(record)

        loaded = store.get("inst-1")
        assert loaded is not None
        assert loaded.sim_id == "sim-inst-1"
        assert loaded.scene_config["map"]["map_id"] == "Town03"
    finally:
        store.close()


def test_patch_updates_fields(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        store.insert(_record())
        store.patch(
            "inst-1",
            status=PLATFORM_RUNNING,
            carla_status="RUNNING",
            progress_percent=42.5,
            ego_state={"x": 1.0},
        )
        loaded = store.get("inst-1")
        assert loaded is not None
        assert loaded.status == PLATFORM_RUNNING
        assert loaded.progress_percent == pytest.approx(42.5)
        assert loaded.ego_state["x"] == pytest.approx(1.0)
    finally:
        store.close()


def test_list_by_scene_and_all(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        store.insert(_record("inst-1", "scene-1"))
        store.insert(_record("inst-2", "scene-2"))
        assert len(store.list_all()) == 2
        assert [r.instance_id for r in store.list_by_scene("scene-1")] == ["inst-1"]
        assert store.list_by_scene("missing") == []
    finally:
        store.close()


def test_find_by_sim_id_ignores_terminal(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        store.insert(_record())
        assert store.find_by_sim_id("sim-inst-1") is not None
        store.patch("inst-1", status=PLATFORM_FAILED)
        assert store.find_by_sim_id("sim-inst-1") is None
        assert store.find_by_sim_id("") is None
    finally:
        store.close()


def test_find_active_by_scene(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        store.insert(_record())
        assert store.find_active_by_scene("scene-1") is not None
        store.patch("inst-1", status=PLATFORM_FAILED)
        assert store.find_active_by_scene("scene-1") is None
    finally:
        store.close()


def test_restart_marks_non_terminal_failed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        store.insert(_record("inst-1"))
        store.insert(_record("inst-2"))
        store.insert(_record("inst-3"))
        store.patch("inst-2", status=PLATFORM_RUNNING)
        store.patch("inst-3", status=PLATFORM_FAILED)

        count = store.mark_restarted_instances_failed("adapter restarted")
        assert count == 2
        first = store.get("inst-1")
        second = store.get("inst-2")
        assert first is not None and second is not None
        assert first.status == PLATFORM_FAILED
        assert first.message == "adapter restarted"
        assert second.status == PLATFORM_FAILED
        assert second.finished_at is not None
    finally:
        store.close()


def test_persists_across_reconnect(tmp_path: Path) -> None:
    db_path = tmp_path / "adapter.db"
    store = InstanceStore(str(db_path)).connect()
    store.insert(_record())
    store.patch("inst-1", status=PLATFORM_PENDING)
    store.close()

    reopened = InstanceStore(str(db_path)).connect()
    try:
        loaded = reopened.get("inst-1")
        assert loaded is not None
        assert loaded.status == PLATFORM_PENDING
    finally:
        reopened.close()
