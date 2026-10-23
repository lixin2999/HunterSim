"""状态持久化测试（V2.4.0）。

覆盖 ``StateStore`` 的实例/场景绑定读写、快照更新、重启归一与级联删除，
以及 V2.8.0 P2-13 的用户表（PBKDF2 口令摘要、登录回写、部分更新与硬删除），
全部使用 tmp_path 下的真实 SQLite 文件（不依赖 CARLA 与 FastAPI）。
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from hunter_sim.persistence import state_store as state_store_module
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


class TestPasswordDigest:
    """口令摘要与恒时比对（P2-13）。"""

    def test_roundtrip_and_wrong_password(self) -> None:
        fingerprint = StateStore.hash_password("S3cure-Pass-2026", iterations=1_000)
        assert fingerprint.startswith("pbkdf2-sha256$1000$")
        assert len(fingerprint.split("$")) == 4
        assert StateStore.verify_password("S3cure-Pass-2026", fingerprint) is True
        assert StateStore.verify_password("S3cure-Pass-2027", fingerprint) is False

    def test_plaintext_never_appears_in_digest(self) -> None:
        fingerprint = StateStore.hash_password("Uniqued-Pass-1", iterations=1_000)
        assert "Uniqued-Pass-1" not in fingerprint

    def test_same_password_yields_different_digest(self) -> None:
        """随机 salt 使相同口令的摘要不同（防跨用户比对）。"""
        first = StateStore.hash_password("Shared-Pass-1", iterations=1_000)
        second = StateStore.hash_password("Shared-Pass-1", iterations=1_000)
        assert first != second
        assert StateStore.verify_password("Shared-Pass-1", first)
        assert StateStore.verify_password("Shared-Pass-1", second)

    @pytest.mark.parametrize(
        "fingerprint",
        [
            "",
            "not-a-digest",
            "bcrypt$12$abc$def",  # 算法不符
            "pbkdf2-sha256$abc$def$ghi",  # 迭代次数非法
            "pbkdf2-sha256$1000$zz$ghi",  # salt 非 hex
            "pbkdf2-sha256$1000$abcdef",  # 字段不足
        ],
    )
    def test_malformed_digest_returns_false(self, fingerprint: str) -> None:
        assert StateStore.verify_password("whatever", fingerprint) is False


class TestUserTable:
    """用户表 CRUD 与登录校验（P2-13）。"""

    def test_create_and_get_user(self, store: StateStore) -> None:
        record = store.create_user(
            "alice", "Alice-Pass-2026", role="operator", tenant="team-a", created_by="root", iterations=1_000
        )
        assert record is not None
        assert record["username"] == "alice"
        assert record["role"] == "operator"
        assert record["tenant"] == "team-a"
        assert record["created_by"] == "root"
        assert record["iterations"] == 1_000
        assert record["disabled_at"] == 0
        assert record["disabled"] is False
        assert store.count_users() == 1

    def test_username_is_trimmed_and_blank_rejected(self, store: StateStore) -> None:
        assert store.create_user("  bob ", "Bob-Pass-2026", iterations=1_000) is not None
        assert store.get_user("bob") is not None
        assert store.create_user("   ", "x", iterations=1_000) is None

    def test_duplicate_username_returns_none(self, store: StateStore) -> None:
        assert store.create_user("carol", "Carol-Pass-01", iterations=1_000) is not None
        assert store.create_user("carol", "Other-Pass-02", iterations=1_000) is None
        assert store.count_users() == 1

    def test_list_users_strips_digest_fields(self, store: StateStore) -> None:
        store.create_user("dave", "Dave-Pass-2026", tenant="team-a", iterations=1_000)
        store.create_user("erin", "Erin-Pass-2026", tenant="team-b", iterations=1_000)

        items = store.list_users()
        assert len(items) == 2
        for item in items:
            assert "pass_hash" not in item
            assert "salt" not in item
            assert "disabled" in item
        assert [item["username"] for item in store.list_users("team-b")] == ["erin"]

    def test_verify_user_success_writes_last_login(self, store: StateStore) -> None:
        store.create_user("frank", "Frank-Pass-01", role="admin", tenant="team-a", iterations=1_000)
        assert float(store.get_user("frank")["last_login_at"]) == 0.0

        identity = store.verify_user("frank", "Frank-Pass-01")
        assert identity == {"username": "frank", "role": "admin", "tenant": "team-a"}
        assert float(store.get_user("frank")["last_login_at"]) > 0.0

    def test_verify_user_rejects_wrong_password_and_unknown_user(self, store: StateStore) -> None:
        store.create_user("grace", "Grace-Pass-01", iterations=1_000)
        assert store.verify_user("grace", "Grace-Pass-99") is None
        assert store.verify_user("ghost", "Grace-Pass-01") is None

    def test_unknown_user_still_runs_one_pbkdf2(
        self, store: StateStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """用户不存在时也计算一次同参数 PBKDF2，避免用响应时间枚举登录名。"""
        calls: list[tuple[str, int]] = []
        real = state_store_module._password_fingerprint

        def spy(password: str, salt: str, rounds: int) -> str:
            calls.append((salt, rounds))
            return real(password, salt, rounds)

        monkeypatch.setattr(state_store_module, "_password_fingerprint", spy)
        store.create_user("mia", "Mia-Pass-01", iterations=1_000)
        calls.clear()

        assert store.verify_user("ghost", "Whatever-Pass-01") is None
        # 不存在分支用固定 dummy salt 跑一次真实迭代，耗时与正常校验对齐
        assert calls == [("00" * 16, state_store_module._DEFAULT_PASSWORD_ITERATIONS)]

    def test_disabled_user_cannot_verify(self, store: StateStore) -> None:
        store.create_user("heidi", "Heidi-Pass-01", iterations=1_000)
        store.update_user("heidi", disabled=True)
        assert store.verify_user("heidi", "Heidi-Pass-01") is None
        store.update_user("heidi", disabled=False)
        assert store.verify_user("heidi", "Heidi-Pass-01") is not None
        assert store.list_users()[0]["disabled"] is False

    def test_update_role_tenant_and_password(self, store: StateStore) -> None:
        store.create_user("ivan", "Ivan-Pass-01", iterations=1_000)
        record = store.update_user("ivan", role="admin", tenant="team-c")
        assert record is not None
        assert record["role"] == "admin"
        assert record["tenant"] == "team-c"

        assert store.verify_user("ivan", "Ivan-Pass-01") is not None
        store.update_user("ivan", password="Ivan-Pass-02", iterations=1_000)
        assert store.verify_user("ivan", "Ivan-Pass-01") is None
        assert store.verify_user("ivan", "Ivan-Pass-02") is not None

    def test_update_without_fields_is_noop(self, store: StateStore) -> None:
        store.create_user("judy", "Judy-Pass-01", iterations=1_000)
        before = store.get_user("judy")
        assert store.update_user("judy") == before

    def test_update_unknown_user_returns_none(self, store: StateStore) -> None:
        assert store.update_user("ghost", role="admin") is None
        assert store.delete_user("ghost") is False

    def test_get_user_derives_disabled_flag(self, store: StateStore) -> None:
        """V2.10.0：get_user 与 list_users 结构对齐，同样派生布尔 disabled。"""
        store.create_user("mallory", "Mallory-Pass-01", iterations=1_000)
        record = store.get_user("mallory")
        assert record is not None and record["disabled"] is False

        store.update_user("mallory", disabled=True)
        disabled = store.get_user("mallory")
        assert disabled is not None
        assert disabled["disabled"] is True
        assert float(disabled["disabled_at"]) > 0.0

    def test_delete_user(self, store: StateStore) -> None:
        store.create_user("karl", "Karl-Pass-01", iterations=1_000)
        assert store.delete_user("karl") is True
        assert store.get_user("karl") is None
        assert store.count_users() == 0

    def test_users_survive_reopen(self, tmp_path: Any) -> None:
        db_path = str(tmp_path / "users.db")
        store = StateStore(db_path)
        store.create_user("lena", "Lena-Pass-01", role="operator", iterations=1_000)
        store.close()

        reopened = StateStore(db_path)
        try:
            assert reopened.count_users() == 1
            assert reopened.verify_user("lena", "Lena-Pass-01") is not None
        finally:
            reopened.close()
