"""资源配额与健康监控单元测试（PROMPT-TEST-001）。"""

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
