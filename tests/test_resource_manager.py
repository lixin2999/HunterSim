"""资源管理模块单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

import pytest

from hunter_sim.common.exceptions import InstanceStateError, ResourceError
from hunter_sim.common.models import InstanceStatus, QualityLevel, SimMode
from hunter_sim.resource_manager.gpu_resource_pool import GPUResourcePool
from hunter_sim.resource_manager.health_monitor import ResourceQuotaManager
from hunter_sim.resource_manager.instance_manager import SimInstanceManager


class TestGPUResourcePool:
    """GPU 资源池测试。"""

    def test_allocate_single(self) -> None:
        pool = GPUResourcePool(gpu_count=1)
        gid = pool.allocate(QualityLevel.MEDIUM)
        assert gid == 0

    def test_epic_capacity_limit(self) -> None:
        """EPIC 画质单 GPU 最多 2 实例（隔离显存约束，仅验证并发容量）。"""
        pool = GPUResourcePool(gpu_count=1, total_memory_gb=64.0)
        id1 = pool.allocate(QualityLevel.EPIC)
        id2 = pool.allocate(QualityLevel.EPIC)
        id3 = pool.allocate(QualityLevel.EPIC)  # 应失败
        assert id1 == 0
        assert id2 == 0
        assert id3 == -1

    def test_epic_memory_constraint(self) -> None:
        """默认 12GB 显存下，第 2 个 EPIC（每个 10GB）因显存不足失败。"""
        pool = GPUResourcePool(gpu_count=1)  # total_memory_gb=12.0
        assert pool.allocate(QualityLevel.EPIC) == 0
        assert pool.allocate(QualityLevel.EPIC) == -1

    def test_release_restores_capacity(self) -> None:
        pool = GPUResourcePool(gpu_count=1, total_memory_gb=64.0)
        pool.allocate(QualityLevel.EPIC)
        pool.allocate(QualityLevel.EPIC)
        assert pool.allocate(QualityLevel.EPIC) == -1
        pool.release(0, QualityLevel.EPIC)
        assert pool.allocate(QualityLevel.EPIC) == 0

    def test_multi_gpu_load_balance(self) -> None:
        """多 GPU 时优先分配负载最低的设备。"""
        pool = GPUResourcePool(gpu_count=2)
        id1 = pool.allocate(QualityLevel.LOW)
        id2 = pool.allocate(QualityLevel.LOW)
        # 第一块分配后，第二块应该负载更低，被选中
        assert id1 == 0
        assert id2 == 1

    def test_status(self) -> None:
        pool = GPUResourcePool(gpu_count=1)
        pool.allocate(QualityLevel.MEDIUM)
        status = pool.get_status()
        assert len(status) == 1
        assert status[0]["active_medium"] == 1


class TestResourceQuotaManager:
    """配额管理测试。"""

    def test_within_quota(self) -> None:
        mgr = ResourceQuotaManager(max_per_user=2, max_total=5)
        mgr.check_and_reserve("user_a")
        mgr.check_and_reserve("user_a")
        usage = mgr.get_usage(user_id="user_a")
        assert usage["user_count"] == 2

    def test_user_quota_exceeded(self) -> None:
        mgr = ResourceQuotaManager(max_per_user=2, max_total=10)
        mgr.check_and_reserve("user_b")
        mgr.check_and_reserve("user_b")
        with pytest.raises(ResourceError):
            mgr.check_and_reserve("user_b")

    def test_global_quota_exceeded(self) -> None:
        mgr = ResourceQuotaManager(max_per_user=5, max_total=2)
        mgr.check_and_reserve("user_c")
        mgr.check_and_reserve("user_d")
        with pytest.raises(ResourceError):
            mgr.check_and_reserve("user_e")

    def test_release(self) -> None:
        mgr = ResourceQuotaManager(max_per_user=1, max_total=5)
        mgr.check_and_reserve("user_f")
        mgr.release("user_f")
        # 释放后应可再次预约
        mgr.check_and_reserve("user_f")  # 不应抛出异常


class TestSimInstanceManager:
    """实例生命周期管理测试。"""

    def _make_manager(self) -> SimInstanceManager:
        pool = GPUResourcePool(gpu_count=1)
        from hunter_sim.common.models import ResourceSettings
        return SimInstanceManager(settings=ResourceSettings(), gpu_pool=pool)

    def test_create_instance(self) -> None:
        mgr = self._make_manager()
        inst = mgr.create_instance(
            mode=SimMode.VIL,
            map_id="Town03",
            quality=QualityLevel.MEDIUM,
            user_id="user_x",
        )
        assert inst.status == InstanceStatus.CREATED
        assert inst.gpu_id >= 0

    def test_lifecycle_transitions(self) -> None:
        """CREATED -> LOADING -> READY -> RUNNING -> COMPLETED。"""
        mgr = self._make_manager()
        inst = mgr.create_instance(
            mode=SimMode.SIL,
            map_id="Town01",
            quality=QualityLevel.LOW,
        )
        iid = inst.sim_instance_id
        mgr.transition(iid, InstanceStatus.LOADING)
        mgr.transition(iid, InstanceStatus.READY)
        mgr.transition(iid, InstanceStatus.RUNNING)
        mgr.transition(iid, InstanceStatus.COMPLETED)
        result = mgr.get_instance(iid)
        assert result is not None
        assert result.status == InstanceStatus.COMPLETED

    def test_invalid_transition_raises(self) -> None:
        mgr = self._make_manager()
        inst = mgr.create_instance(
            mode=SimMode.SIL,
            map_id="Town01",
            quality=QualityLevel.LOW,
        )
        with pytest.raises(InstanceStateError):
            # CREATED 不能直接到 RUNNING
            mgr.transition(inst.sim_instance_id, InstanceStatus.RUNNING)

    def test_destroy_releases_gpu(self) -> None:
        pool = GPUResourcePool(gpu_count=1, total_memory_gb=64.0)
        from hunter_sim.common.models import ResourceSettings
        mgr = SimInstanceManager(settings=ResourceSettings(), gpu_pool=pool)
        # EPIC 只能分配 2 次
        i1 = mgr.create_instance(SimMode.SIL, "Town01", QualityLevel.EPIC)
        i2 = mgr.create_instance(SimMode.SIL, "Town01", QualityLevel.EPIC)
        # 第三次应该失败
        with pytest.raises(ResourceError):
            mgr.create_instance(SimMode.SIL, "Town01", QualityLevel.EPIC)
        # 销毁第一个后应可再次分配
        mgr.destroy_instance(i1.sim_instance_id)
        i3 = mgr.create_instance(SimMode.SIL, "Town01", QualityLevel.EPIC)
        assert i3.gpu_id >= 0
