"""状态持久化测试（V2.4.0）。

覆盖 ``StateStore`` 的实例/场景绑定读写、快照更新、重启归一与级联删除，
全部使用 tmp_path 下的真实 SQLite 文件（不依赖 CARLA 与 FastAPI）。
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from hunter_sim.persistence.state_store import StateStore


def _record(instance_id: str = "inst-1", **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "sim_instance_id": instance_id,
        "status": "running",
        "mode": "sil",
        "map_id": "Town03",
        "quality": "medium",
        "gpu_id": 0,
        "carla_host": "127.0.0.1",
        "carla_rpc_port": 2000,
        "carla_stream_port": 2001,
        "user_id": "user-1",
    }
    record.update(overrides)
    return record


def _config(scene_id: str = "scene-1") -> dict[str, Any]:
    return {
        "scene_id": scene_id,
        "map_id": "Town03",
        "mode": "builtin",
        "weather": {"preset_name": "clear"},
        "ego": {"spawn_point": {"x": 1.0, "y": 2.0, "z": 0.3, "yaw": 0.0}},
        "actors": [],
        "duration_seconds": 60.0,
        "trigger_conditions": [],
    }


@pytest.fixture()
def store(tmp_path: Any):
    store = StateStore(str(tmp_path / "state.db"))
    yield store
    store.close()


class TestInstanceState:
    def test_save_and_load_roundtrip(self, store: StateStore) -> None:
        store.save_instance(_record())
        records = store.load_instances()
        assert len(records) == 1
        assert records[0]["sim_instance_id"] == "inst-1"
        assert records[0]["status"] == "running"

    def test_upsert_overwrites_payload(self, store: StateStore) -> None:
        store.save_instance(_record())
        store.save_instance(_record(status="ready"))
        records = store.load_instances()
        assert len(records) == 1
        assert records[0]["status"] == "ready"

    def test_delete_instance_cascades_scene_binding(self, store: StateStore) -> None:
        store.save_instance(_record())
        store.save_scene("inst-1", "scene-1", "Town03", _config())

        store.delete_instance("inst-1")

        assert store.load_instances() == []
        assert store.load_scene("inst-1") is None

    def test_corrupted_instance_record_is_skipped(self, store: StateStore) -> None:
        store.save_instance(_record())
        with sqlite3.connect(store.path) as conn:
            conn.execute(
                "INSERT INTO instances (sim_instance_id, payload, updated_at) VALUES (?, ?, ?)",
                ("bad", "{not-json", 0.0),
            )
        assert [r["sim_instance_id"] for r in store.load_instances()] == ["inst-1"]


class TestSceneBinding:
    def test_save_and_load_scene(self, store: StateStore) -> None:
        store.save_scene("inst-1", "scene-1", "Town03", _config())

        binding = store.load_scene("inst-1")
        assert binding is not None
        assert binding["scene_id"] == "scene-1"
        assert binding["status"] == "ready"
        assert binding["config"]["scene_id"] == "scene-1"
        assert binding["config"]["duration_seconds"] == 60.0

    def test_load_missing_returns_none(self, store: StateStore) -> None:
        assert store.load_scene("ghost") is None

    def test_save_scene_resets_snapshot(self, store: StateStore) -> None:
        store.save_scene("inst-1", "scene-1", "Town03", _config())
        store.update_scene_snapshot("inst-1", "failed", progress=0.5, error_message="boom")

        store.save_scene("inst-1", "scene-2", "Town03", _config("scene-2"))

        binding = store.load_scene("inst-1")
        assert binding is not None
        assert binding["status"] == "ready"
        assert binding["progress"] == 0.0
        assert binding["error_message"] == ""

    def test_update_snapshot(self, store: StateStore) -> None:
        store.save_scene("inst-1", "scene-1", "Town03", _config())
        store.update_scene_snapshot(
            "inst-1", "running", progress=0.25, elapsed_seconds=15.0, error_message=""
        )

        binding = store.load_scene("inst-1")
        assert binding is not None
        assert binding["status"] == "running"
        assert binding["progress"] == 0.25
        assert binding["elapsed_seconds"] == 15.0

    def test_update_snapshot_without_binding_is_noop(self, store: StateStore) -> None:
        store.update_scene_snapshot("ghost", "running", progress=1.0)
        assert store.load_scene("ghost") is None

    def test_list_scenes(self, store: StateStore) -> None:
        store.save_scene("inst-1", "scene-1", "Town03", _config())
        store.save_scene("inst-2", "scene-2", "Town04", _config("scene-2"))

        scenes = store.list_scenes()
        assert {s["instance_id"] for s in scenes} == {"inst-1", "inst-2"}
        assert "config" not in scenes[0]

    def test_delete_scene(self, store: StateStore) -> None:
        store.save_scene("inst-1", "scene-1", "Town03", _config())
        store.delete_scene("inst-1")
        assert store.load_scene("inst-1") is None

    def test_corrupted_scene_config_returns_none(self, store: StateStore) -> None:
        with sqlite3.connect(store.path) as conn:
            conn.execute(
                "INSERT INTO scene_bindings (instance_id, scene_id, map_id, config, status, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("inst-bad", "scene-x", "Town03", "{broken", "ready", 0.0),
            )
        assert store.load_scene("inst-bad") is None


class TestNormalizeOnRestart:
    def test_running_paused_loading_become_ready(self, store: StateStore) -> None:
        for iid, status in (
            ("inst-running", "running"),
            ("inst-paused", "paused"),
            ("inst-loading", "loading"),
            ("inst-done", "completed"),
            ("inst-failed", "failed"),
        ):
            store.save_scene(iid, "scene-1", "Town03", _config(), status=status)

        assert store.normalize_scene_statuses() == 3

        assert store.load_scene("inst-running")["status"] == "ready"
        assert store.load_scene("inst-paused")["status"] == "ready"
        assert store.load_scene("inst-loading")["status"] == "ready"
        assert store.load_scene("inst-done")["status"] == "completed"
        assert store.load_scene("inst-failed")["status"] == "failed"

    def test_normalize_is_idempotent(self, store: StateStore) -> None:
        store.save_scene("inst-1", "scene-1", "Town03", _config(), status="running")
        store.normalize_scene_statuses()
        assert store.normalize_scene_statuses() == 0


class TestStoreLifecycle:
    def test_memory_store_path(self) -> None:
        store = StateStore(":memory:")
        try:
            store.save_instance(_record())
            assert len(store.load_instances()) == 1
            assert store.path == ":memory:"
        finally:
            store.close()

    def test_creates_parent_directory(self, tmp_path: Any) -> None:
        db_path = tmp_path / "nested" / "dir" / "state.db"
        store = StateStore(str(db_path))
        store.close()
        assert db_path.exists()

    def test_close_is_idempotent(self, tmp_path: Any) -> None:
        store = StateStore(str(tmp_path / "state.db"))
        store.close()
        store.close()  # 二次关闭只告警，不抛异常
