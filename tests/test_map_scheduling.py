"""地图感知调度测试（V2.8.0 P2-14）。

覆盖控制面 ``SimInstanceManager._assign_endpoint`` 与执行面 ``EnginePool``
协同的调度决策：同地图亲和复用、空闲端点优先、全部占用拒绝，以及
“创建即预留”的双重占用防护（租约在执行面装配时才产生）。
不依赖真实 CARLA server。
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from hunter_sim.common.exceptions import ResourceError
from hunter_sim.common.metrics import SCHEDULING_DECISIONS_TOTAL, counter_value
from hunter_sim.common.models import (
    CarlaSettings,
    InstanceStatus,
    PlatformSettings,
    QualityLevel,
    SimMode,
)
from hunter_sim.execution.engine_pool import EngineEndpoint, EnginePool
from hunter_sim.resource_manager.instance_manager import SimInstanceManager


def _endpoints() -> list[EngineEndpoint]:
    return [
        EngineEndpoint("10.0.0.1", 2000, 2001),
        EngineEndpoint("10.0.0.2", 2000, 2001),
    ]


def _manager(
    endpoints: Optional[list[EngineEndpoint]] = None,
    *,
    with_pool: bool = True,
    **manager_kwargs: Any,
) -> tuple[SimInstanceManager, Optional[EnginePool]]:
    inventory = endpoints if endpoints is not None else _endpoints()
    pool = EnginePool(inventory) if with_pool else None
    manager = SimInstanceManager(
        carla=CarlaSettings(host="10.0.0.1", rpc_port=2000, stream_port=2001),
        engine_endpoints=inventory,
        engine_pool=pool,
        platform=PlatformSettings(max_instances_per_tenant=50),
        **manager_kwargs,
    )
    return manager, pool


def _create(
    manager: SimInstanceManager, map_id: str, quality: QualityLevel = QualityLevel.MEDIUM
) -> Any:
    return manager.create_instance(mode=SimMode.SIL, map_id=map_id, quality=quality)


class TestAffinityScheduling:
    def test_same_map_reuses_endpoint_that_already_loaded_it(self) -> None:
        manager, pool = _manager()
        assert pool is not None
        first = _create(manager, "Town03")
        # 模拟执行面装配：领取租约并以实际加载结果登记地图
        pool.acquire(first.sim_instance_id, _endpoints()[0], "Town03")
        pool.note_loaded_map("10.0.0.1:2000", "Town03")
        manager.destroy_instance(first.sim_instance_id)
        pool.release(first.sim_instance_id)

        second = _create(manager, "Town03")

        assert (second.carla_host, second.carla_rpc_port) == ("10.0.0.1", 2000)
        assert counter_value(SCHEDULING_DECISIONS_TOTAL, kind="affinity_reuse") >= 1

    def test_different_map_prefers_never_loaded_endpoint(self) -> None:
        manager, pool = _manager()
        assert pool is not None
        pool.note_loaded_map("10.0.0.1:2000", "Town03")

        instance = _create(manager, "Town04")

        # 不去切换已加载他图（且此刻空闲）的引擎，优先动“干净”的第二个端点
        assert (instance.carla_host, instance.carla_rpc_port) == ("10.0.0.2", 2000)

    def test_created_instances_do_not_double_book_one_endpoint(self) -> None:
        """控制面创建即预留端点：租约尚未产生时也不能把两个实例排到同一引擎。"""
        manager, pool = _manager()
        assert pool is not None

        first = _create(manager, "Town03")
        second = _create(manager, "Town03")

        assert (first.carla_host, first.carla_rpc_port) != (second.carla_host, second.carla_rpc_port)
        assert pool.active_count == 0  # 租约由执行面装配时领取，控制面只占位

    def test_destroyed_endpoint_becomes_assignable_again(self) -> None:
        manager, pool = _manager()
        assert pool is not None
        first = _create(manager, "Town03")
        second = _create(manager, "Town03")
        manager.destroy_instance(first.sim_instance_id)

        third = _create(manager, "Town03")

        assert (third.carla_host, third.carla_rpc_port) == (first.carla_host, first.carla_rpc_port)
        assert (third.carla_host, third.carla_rpc_port) != (second.carla_host, second.carla_rpc_port)

    def test_all_endpoints_busy_rejects_creation(self) -> None:
        manager, _ = _manager()
        _create(manager, "Town03")
        _create(manager, "Town03")

        with pytest.raises(ResourceError, match="engine"):
            _create(manager, "Town04")

    def test_fallback_decision_when_only_other_map_endpoints_free(self) -> None:
        manager, pool = _manager(endpoints=[EngineEndpoint("10.0.0.1", 2000, 2001)])
        assert pool is not None
        pool.note_loaded_map("10.0.0.1:2000", "Town03")

        before = counter_value(SCHEDULING_DECISIONS_TOTAL, kind="fallback")
        instance = _create(manager, "Town05")

        assert instance.carla_rpc_port == 2000
        assert counter_value(SCHEDULING_DECISIONS_TOTAL, kind="fallback") == before + 1

    def test_fresh_endpoint_decision_recorded(self) -> None:
        manager, _ = _manager()
        before = counter_value(SCHEDULING_DECISIONS_TOTAL, kind="fresh_endpoint")

        _create(manager, "Town03")

        assert counter_value(SCHEDULING_DECISIONS_TOTAL, kind="fresh_endpoint") == before + 1


class TestLegacyAndSingleEnginePaths:
    def test_without_pool_falls_back_to_active_allocation_table(self) -> None:
        """未注入租约池时保持 V2.4.0 语义：按活跃实例占用表选首个空闲端点。"""
        manager, pool = _manager(with_pool=False)
        assert pool is None

        first = _create(manager, "Town03")
        second = _create(manager, "Town03")

        assert (first.carla_host, first.carla_rpc_port) == ("10.0.0.1", 2000)
        assert (second.carla_host, second.carla_rpc_port) == ("10.0.0.2", 2000)
        assert counter_value(SCHEDULING_DECISIONS_TOTAL, kind="fallback") >= 2

    def test_without_pool_terminal_instances_release_endpoint(self) -> None:
        manager, _ = _manager(with_pool=False)
        _create(manager, "Town03")
        second = _create(manager, "Town03")
        manager.transition(second.sim_instance_id, InstanceStatus.LOADING)
        manager.transition(second.sim_instance_id, InstanceStatus.READY)
        manager.transition(second.sim_instance_id, InstanceStatus.RUNNING)
        manager.transition(second.sim_instance_id, InstanceStatus.COMPLETED)

        third = _create(manager, "Town03")

        # COMPLETED 不再占用端点，可回收复用
        assert (third.carla_host, third.carla_rpc_port) == ("10.0.0.2", 2000)

    def test_single_engine_mode_when_no_inventory(self) -> None:
        manager, _ = _manager(endpoints=[], with_pool=False)

        first = _create(manager, "Town03")
        second = _create(manager, "Town04")

        # 未声明库存：共享单引擎端点，地图互斥由执行面按端点判定
        assert (first.carla_host, first.carla_rpc_port) == ("10.0.0.1", 2000)
        assert (second.carla_host, second.carla_rpc_port) == ("10.0.0.1", 2000)
