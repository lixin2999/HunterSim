"""资源配额与健康监控单元测试（PROMPT-TEST-001）。

覆盖设计文档 §14.1/§14.2：超时自动销毁、崩溃重试≤3、单用户配额默认 5。
"""

from __future__ import annotations

import pytest

from hunter_sim.common.exceptions import ResourceError
from hunter_sim.common.models import ResourceSettings
from hunter_sim.resource_manager.health_monitor import (
    InstanceHealthMonitor,
    ResourceQuotaManager,
)


class TestResourceQuotaManager:
    def test_reserve_within_limits(self) -> None:
        q = ResourceQuotaManager(max_per_user=2, max_total=4)
        q.check_and_reserve("alice")
        assert q.get_usage("alice") == {"user_count": 1, "max": 2}

    def test_per_user_exceeded(self) -> None:
        q = ResourceQuotaManager(max_per_user=1, max_total=10)
        q.check_and_reserve("bob")
        with pytest.raises(ResourceError):
            q.check_and_reserve("bob")

    def test_total_exceeded(self) -> None:
        q = ResourceQuotaManager(max_per_user=5, max_total=2)
        q.check_and_reserve("u1")
        q.check_and_reserve("u2")
        with pytest.raises(ResourceError):
            q.check_and_reserve("u3")

    def test_release_frees_slot(self) -> None:
        q = ResourceQuotaManager(max_per_user=1, max_total=5)
        q.check_and_reserve("carol")
        q.release("carol")
        assert q.get_usage("carol") == {"user_count": 0, "max": 1}
        q.check_and_reserve("carol")  # 释放后可再次预留

    def test_release_unknown_user(self) -> None:
        q = ResourceQuotaManager()
        q.check_and_reserve("x")
        q.release("ghost")  # 未知用户不应报错，仅递减 total
        assert q.get_usage()["total"] == 0

    def test_global_usage_snapshot(self) -> None:
        q = ResourceQuotaManager(max_per_user=3, max_total=8)
        q.check_and_reserve("dave")
        usage = q.get_usage()
        assert usage["total"] == 1
        assert usage["max_total"] == 8
        assert usage["users"] == {"dave": 1}

    def test_quota_default_per_user(self) -> None:
        """§14.2：单用户最大并发实例数默认 5 个。"""
        q = ResourceQuotaManager()
        for _ in range(5):
            q.check_and_reserve("heavy_user")
        with pytest.raises(ResourceError):
            q.check_and_reserve("heavy_user")
        assert q.get_usage("heavy_user")["max"] == 5


class TestInstanceHealthMonitor:
    def test_default_retry_count(self) -> None:
        assert InstanceHealthMonitor().get_retry_count("inst") == 0

    def test_settings_defaults(self) -> None:
        mon = InstanceHealthMonitor(settings=ResourceSettings(max_retry_count=5))
        assert mon._settings.max_retry_count == 5

    def test_start_stop(self) -> None:
        mon = InstanceHealthMonitor(settings=ResourceSettings(health_check_interval_seconds=1))
        mon.start()
        assert mon._running is True
        assert mon._thread is not None
        mon.stop()
        assert mon._running is False

    def test_crash_restart_up_to_max_retries(self) -> None:
        """§14.1：CARLA 崩溃自动重启，最多重试 3 次，超出后不再重启。"""
        restarted: list[str] = []
        mon = InstanceHealthMonitor(
            settings=ResourceSettings(max_retry_count=3),
            check_callback=lambda iid: False,
            restart_callback=lambda iid: restarted.append(iid),
        )
        mon.register_instance("i1")
        for _ in range(3):
            cycle = mon.run_check_cycle()
            assert cycle["failed"] == ["i1"]
        assert restarted == ["i1", "i1", "i1"]
        assert mon.get_retry_count("i1") == 3
        # 第 4 轮：超出最大重试次数，标记不可恢复，不再重启
        cycle = mon.run_check_cycle()
        assert cycle["exhausted"] == ["i1"]
        assert mon.is_unrecoverable("i1") is True
        assert len(restarted) == 3

    def test_healthy_resets_retry_count(self) -> None:
        """探针恢复健康后重试计数归零。"""
        flags = {"ok": False}
        mon = InstanceHealthMonitor(
            settings=ResourceSettings(max_retry_count=3),
            check_callback=lambda iid: flags["ok"],
        )
        mon.register_instance("i2")
        mon.run_check_cycle()
        assert mon.get_retry_count("i2") == 1
        flags["ok"] = True
        cycle = mon.run_check_cycle()
        assert cycle["failed"] == []
        assert mon.get_retry_count("i2") == 0

    def test_expired_cleanup_callback(self) -> None:
        """§14.1/§15.5：超时（默认 2 小时）实例由监控周期自动销毁。"""
        mon = InstanceHealthMonitor(
            expired_cleanup_callback=lambda: ["zombie-1", "zombie-2"],
        )
        cycle = mon.run_check_cycle()
        assert cycle["expired_destroyed"] == ["zombie-1", "zombie-2"]

    def test_expired_instances_auto_unregistered(self) -> None:
        """审查项 K：过期销毁后自动注销监控，防 _watched 集合单调增长。"""
        mon = InstanceHealthMonitor(
            check_callback=lambda iid: True,
            expired_cleanup_callback=lambda: ["gone-1"],
        )
        mon.register_instance("gone-1")
        mon.register_instance("alive-1")
        mon.run_check_cycle()
        assert mon.watched_ids() == ["alive-1"]  # 过期项已注销，健康项保留

    def test_watched_ids_is_sorted_snapshot(self) -> None:
        """审查项 K：watched_ids 返回锁内拷贝快照（外部修改不影响内部集合）。"""
        mon = InstanceHealthMonitor()
        mon.register_instance("b")
        mon.register_instance("a")
        snap = mon.watched_ids()
        assert snap == ["a", "b"]
        snap.append("c")
        assert mon.watched_ids() == ["a", "b"]

    def test_unregister_clears_state(self) -> None:
        mon = InstanceHealthMonitor(
            settings=ResourceSettings(max_retry_count=0),
            check_callback=lambda iid: False,
        )
        mon.register_instance("i3")
        mon.run_check_cycle()
        assert mon.is_unrecoverable("i3") is True
        mon.unregister_instance("i3")
        assert mon.is_unrecoverable("i3") is False
        assert mon.get_retry_count("i3") == 0
