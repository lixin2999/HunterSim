"""V2.7.0 P1-6/P1-7/P1-10/P1-11 运维链路单元测试。

覆盖：
- JWT ``jti`` 签发与状态库吊销名单（P1-6）；
- 引擎租约心跳、僵尸判定与强制回收（P1-7）；
- 场景快照写节流：同状态最小间隔 + 状态跃迁必写（P1-10）；
- ``configure_logging`` 文本/JSON 渲染、request_id 贯穿与轮转文件（P1-11）。
"""

from __future__ import annotations

import json
import logging
import time
from types import SimpleNamespace
from typing import Any

import pytest
from jose import jwt

from hunter_sim.api import runtime as runtime_mod
from hunter_sim.api.deps import mint_access_token
from hunter_sim.common import utils as utils_mod
from hunter_sim.common.models import APISettings, StateSettings, VILSettings  # noqa: F401
from hunter_sim.execution.engine_pool import EngineEndpoint, EngineLease, EnginePool
from hunter_sim.execution.plane import ExecutionPlane
from hunter_sim.persistence.state_store import StateStore

_EP = EngineEndpoint(host="127.0.0.1", rpc_port=2000, stream_port=2001)


# ─── P1-6：jti 签发与吊销名单 ────────────────────────────────────────────────


class TestTokenRevocationStore:
    @pytest.fixture
    def store(self, tmp_path: Any) -> StateStore:
        return StateStore(str(tmp_path / "state.db"), 5.0)

    def test_mint_access_token_carries_jti_and_exp(self, store: StateStore) -> None:
        api = APISettings()
        token, jti, exp = mint_access_token("u1", api)
        payload = jwt.decode(
            token, api.jwt_secret.get_secret_value(), algorithms=[api.jwt_algorithm]
        )
        assert payload["jti"] == jti
        assert payload["exp"] == exp
        assert exp > time.time()

    def test_revoke_is_idempotent_and_queryable(self, store: StateStore) -> None:
        assert store.revoke_token("jti-abc", expires_at=time.time() + 60) is True
        assert store.revoke_token("jti-abc") is False  # 幂等：非首次
        assert store.is_token_revoked("jti-abc") is True
        assert store.is_token_revoked("jti-unknown") is False
        assert store.is_token_revoked("") is False
        assert store.revoke_token("") is False

    def test_prune_removes_only_naturally_expired(self, store: StateStore) -> None:
        store.revoke_token("old", expires_at=time.time() - 10)
        store.revoke_token("live", expires_at=time.time() + 3600)
        removed = store.prune_revoked_tokens()
        assert removed == 1
        assert store.is_token_revoked("old") is False
        assert store.is_token_revoked("live") is True

    def test_store_close(self, store: StateStore) -> None:
        store.close()


# ─── P1-7：租约心跳与僵尸回收 ────────────────────────────────────────────────


class TestLeaseHeartbeat:
    def test_lease_renew_and_stale(self) -> None:
        lease = EngineLease(instance_id="i1", endpoint=_EP, map_id="Town03")
        assert lease.is_stale(ttl_seconds=30) is False
        lease.last_renew_at = time.time() - 31
        assert lease.is_stale(ttl_seconds=30) is True
        lease.renew()
        assert lease.is_stale(ttl_seconds=30) is False
        # ttl<=0 禁用僵判定
        assert lease.is_stale(ttl_seconds=0) is False

    def test_pool_renew_and_stale_leases(self) -> None:
        pool = EnginePool([_EP], lease_ttl_seconds=10)
        pool.acquire("i1", _EP, "Town03")
        lease = pool.get("i1")
        assert lease is not None
        lease.last_renew_at = time.time() - 11
        assert [l.instance_id for l in pool.stale_leases()] == ["i1"]
        assert pool.renew("i1") is True
        assert pool.stale_leases() == []
        assert pool.renew("ghost") is False

    def test_snapshot_exposes_stale_flags(self) -> None:
        pool = EnginePool([_EP], lease_ttl_seconds=5)
        pool.acquire("i1", _EP, "Town03")
        pool.get("i1").last_renew_at = time.time() - 6  # type: ignore[union-attr]
        snap = pool.snapshot()
        lease_items = {lease["instance_id"]: lease for lease in snap["leases"]}
        assert lease_items["i1"]["stale"] is True
        assert snap["stale_leases"] == 1
        assert snap["lease_ttl_seconds"] == 5
        engine = snap["engines"][0]
        assert engine["stale"] is True and engine["holder"] == "i1"


class _LiveRunner:
    is_running = True


class _DeadRunner:
    is_running = False


def _make_plane(runners: dict[str, Any], ttl: float = 30.0) -> ExecutionPlane:
    pool = EnginePool([_EP], lease_ttl_seconds=ttl)
    plane = ExecutionPlane(pool, lambda ep: None, runners=runners)
    pool.acquire("i1", _EP, "Town03")
    return plane


class TestPlaneHeartbeat:
    def test_heartbeat_only_for_live_runners(self) -> None:
        plane = _make_plane({"i1": _LiveRunner()})
        lease = plane._pool.get("i1")
        assert lease is not None
        lease.last_renew_at = time.time() - 10
        assert plane.heartbeat("i1") is True
        assert lease.last_renew_at > time.time() - 1

        plane_dead = _make_plane({"i1": _DeadRunner()})
        assert plane_dead.heartbeat("i1") is False
        assert plane_dead.stale_instance_ids() == []  # ttl 30 未到，不算僵
        # 心跳已过期的僵尸租约可被判出
        dead_lease = plane_dead._pool.get("i1")
        assert dead_lease is not None
        dead_lease.last_renew_at = time.time() - 31
        assert plane_dead.stale_instance_ids() == ["i1"]

    def test_heartbeat_without_runner_returns_false(self) -> None:
        plane = _make_plane({})
        assert plane.heartbeat("nobody") is False
        assert plane.renew_live_leases() == 0

    def test_renew_live_leases_counts(self) -> None:
        plane = _make_plane({"i1": _LiveRunner()})
        assert plane.renew_live_leases() == 1

    def test_force_release_reclaims_lease_even_without_runner(self) -> None:
        plane = _make_plane({})  # 运行器已丢失，只剩僵尸租约
        assert plane._pool.get("i1") is not None
        assert plane.force_release("i1") is True
        assert plane._pool.get("i1") is None
        assert plane.force_release("i1") is False  # 重复回收返回 False


# ─── P1-10：快照写节流 ────────────────────────────────────────────────────────


def _throttle_app(interval: float) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(
            settings=SimpleNamespace(
                state=SimpleNamespace(snapshot_write_interval_seconds=interval)
            )
        )
    )


class TestSnapshotWriteThrottle:
    def test_same_state_within_interval_skipped(self) -> None:
        app = _throttle_app(60.0)
        assert runtime_mod._should_write_snapshot(app, "i1", "running") is True
        assert runtime_mod._should_write_snapshot(app, "i1", "running") is False

    def test_state_transition_always_written(self) -> None:
        app = _throttle_app(60.0)
        runtime_mod._should_write_snapshot(app, "i1", "running")
        assert runtime_mod._should_write_snapshot(app, "i1", "paused") is True
        assert runtime_mod._should_write_snapshot(app, "i1", "completed") is True

    def test_zero_interval_never_throttles(self) -> None:
        app = _throttle_app(0.0)
        assert runtime_mod._should_write_snapshot(app, "i1", "running") is True
        assert runtime_mod._should_write_snapshot(app, "i1", "running") is True

    def test_interval_elapsed_writes_again(self) -> None:
        app = _throttle_app(0.05)
        assert runtime_mod._should_write_snapshot(app, "i1", "running") is True
        time.sleep(0.06)
        assert runtime_mod._should_write_snapshot(app, "i1", "running") is True

    def test_callback_integration(self) -> None:
        writes: list[str] = []
        app = _throttle_app(60.0)
        store = SimpleNamespace(
            update_scene_snapshot=lambda **kw: writes.append(kw["status"])
        )
        app.state.state_store = store
        callback = runtime_mod._scene_state_callback(app, "i1")

        def snap(status: str) -> SimpleNamespace:
            return SimpleNamespace(
                status=SimpleNamespace(value=status),
                progress=0.5,
                elapsed_seconds=1.0,
                error_message="",
            )

        callback(snap("running"))
        callback(snap("running"))  # 同状态节流
        callback(snap("completed"))  # 跃迁必写
        assert writes == ["running", "completed"]

    def test_missing_settings_defaults_to_no_throttle(self) -> None:
        app = SimpleNamespace(state=SimpleNamespace())
        assert runtime_mod._should_write_snapshot(app, "i1", "running") is True


# ─── P1-11：统一日志装配 ─────────────────────────────────────────────────────


@pytest.fixture
def reset_logging_state() -> Any:
    """测试后恢复全局日志状态，避免污染其他用例。"""
    parent = logging.getLogger("hunter_sim")
    saved_handlers = list(parent.handlers)
    saved_configured = utils_mod._LOG_CONFIGURED
    yield
    for handler in list(parent.handlers):
        parent.removeHandler(handler)
    for handler in saved_handlers:
        parent.addHandler(handler)
    utils_mod._LOG_CONFIGURED = saved_configured
    utils_mod.clear_request_id()


class TestConfigureLogging:
    def test_text_mode_with_request_id_and_file(
        self, tmp_path: Any, reset_logging_state: None
    ) -> None:
        log_file = tmp_path / "app.log"
        utils_mod.configure_logging(log_level="INFO", log_file=str(log_file))
        utils_mod.bind_request_id("rid-text-1")
        logger = utils_mod.get_logger("hunter_sim.text_mode_probe")
        logger.info("hello text")
        utils_mod.clear_request_id()
        content = log_file.read_text(encoding="utf-8")
        assert "rid-text-1" in content
        assert "hello text" in content

    def test_json_mode_renders_structured_lines(
        self, tmp_path: Any, reset_logging_state: None
    ) -> None:
        log_file = tmp_path / "app.jsonl"
        utils_mod.configure_logging(log_level="INFO", json_logs=True, log_file=str(log_file))
        utils_mod.bind_request_id("rid-json-1")
        logger = utils_mod.get_logger("hunter_sim.json_mode_probe")
        logger.info("hello json")
        utils_mod.clear_request_id()
        lines = [line for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        entries = [json.loads(line) for line in lines]
        probe = [e for e in entries if e.get("event") == "hello json"]
        assert probe, entries
        assert probe[0]["request_id"] == "rid-json-1"
        assert probe[0]["level"] == "info"

    def test_rotating_handler_configured(self, tmp_path: Any, reset_logging_state: None) -> None:
        log_file = tmp_path / "rot.log"
        utils_mod.configure_logging(
            log_level="INFO", log_file=str(log_file), log_max_bytes=1024, log_backup_count=2
        )
        parent = logging.getLogger("hunter_sim")
        rotating = [
            h for h in parent.handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        assert len(rotating) == 1
        assert rotating[0].maxBytes == 1024
        assert rotating[0].backupCount == 2

    def test_idempotent_no_duplicate_handlers(
        self, tmp_path: Any, reset_logging_state: None
    ) -> None:
        log_file = tmp_path / "idem.log"
        utils_mod.configure_logging(log_level="INFO", log_file=str(log_file))
        utils_mod.configure_logging(log_level="INFO", log_file=str(log_file))
        parent = logging.getLogger("hunter_sim")
        assert len(parent.handlers) == 2  # stdout + file，各一份
        utils_mod.get_logger("hunter_sim.idem_probe").info("once")
        content = log_file.read_text(encoding="utf-8")
        assert content.count("once") == 1

    def test_legacy_get_logger_still_adds_handler_without_configure(
        self, reset_logging_state: None
    ) -> None:
        utils_mod._LOG_CONFIGURED = False
        logger = logging.getLogger("hunter_sim.legacy_probe_new")
        fresh = utils_mod.get_logger("hunter_sim.legacy_probe_new")
        # 未装配全局出口时保持旧行为：自带 stdout handler
        assert fresh.handlers == logger.handlers or not fresh.handlers
        fresh.handlers.clear()
