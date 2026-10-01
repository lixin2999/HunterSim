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

    def test_allocate_ex_downgrades_on_overload(self) -> None:
        """过载降级：EPIC 容量耗尽后自动降为 MEDIUM（文档 §10.3.1）。"""
        pool = GPUResourcePool(gpu_count=1, total_memory_gb=64.0)
        assert pool.allocate_ex(QualityLevel.EPIC) == (0, QualityLevel.EPIC)
        assert pool.allocate_ex(QualityLevel.EPIC) == (0, QualityLevel.EPIC)
        # EPIC 容量（2）已满，降级到 MEDIUM
        gid, actual = pool.allocate_ex(QualityLevel.EPIC, allow_downgrade=True)
        assert gid == 0
        assert actual == QualityLevel.MEDIUM

    def test_allocate_ex_no_downgrade_returns_minus_one(self) -> None:
        pool = GPUResourcePool(gpu_count=1, total_memory_gb=64.0)
        pool.allocate_ex(QualityLevel.EPIC)
        pool.allocate_ex(QualityLevel.EPIC)
        gid, actual = pool.allocate_ex(QualityLevel.EPIC, allow_downgrade=False)
        assert gid == -1
        assert actual == QualityLevel.EPIC


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

    def test_carla_ports_incremental_allocation(self) -> None:
        """审查项 N：多实例按空闲端口对递增分配，销毁后可复用。"""
        mgr = self._make_manager()
        inst1 = mgr.create_instance(mode=SimMode.SIL, map_id="Town01", quality=QualityLevel.LOW)
        inst2 = mgr.create_instance(mode=SimMode.SIL, map_id="Town01", quality=QualityLevel.LOW)
        assert (inst1.carla_rpc_port, inst1.carla_stream_port) == (2000, 2001)
        assert (inst2.carla_rpc_port, inst2.carla_stream_port) == (2002, 2003)
        mgr.destroy_instance(inst2.sim_instance_id)
        inst3 = mgr.create_instance(mode=SimMode.SIL, map_id="Town01", quality=QualityLevel.LOW)
        assert (inst3.carla_rpc_port, inst3.carla_stream_port) == (2002, 2003)  # 空闲对回收复用

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
        mgr.create_instance(SimMode.SIL, "Town01", QualityLevel.EPIC)  # 占满第二次分配
        # 第三次应该失败
        with pytest.raises(ResourceError):
            mgr.create_instance(SimMode.SIL, "Town01", QualityLevel.EPIC)
        # 销毁第一个后应可再次分配
        mgr.destroy_instance(i1.sim_instance_id)
        i3 = mgr.create_instance(SimMode.SIL, "Town01", QualityLevel.EPIC)
        assert i3.gpu_id >= 0

    def test_overload_downgrade_creates_lower_quality(self) -> None:
        """开启过载后，EPIC 满容时以 MEDIUM 画质创建实例（文档 §10.3.1）。"""
        from hunter_sim.common.models import ResourceSettings
        pool = GPUResourcePool(gpu_count=1, total_memory_gb=64.0)
        mgr = SimInstanceManager(
            settings=ResourceSettings(gpu_allow_overload_downgrade=True), gpu_pool=pool
        )
        mgr.create_instance(SimMode.SIL, "Town01", QualityLevel.EPIC)
        mgr.create_instance(SimMode.SIL, "Town01", QualityLevel.EPIC)
        inst = mgr.create_instance(SimMode.SIL, "Town01", QualityLevel.EPIC)
        assert inst.quality == QualityLevel.MEDIUM
        assert inst.gpu_id == 0

    def test_overload_downgrade_disabled_by_default(self) -> None:
        """默认不开启过载时保持严格分配语义。"""
        from hunter_sim.common.models import ResourceSettings
        pool = GPUResourcePool(gpu_count=1, total_memory_gb=64.0)
        mgr = SimInstanceManager(settings=ResourceSettings(), gpu_pool=pool)
        mgr.create_instance(SimMode.SIL, "Town01", QualityLevel.EPIC)
        mgr.create_instance(SimMode.SIL, "Town01", QualityLevel.EPIC)
        with pytest.raises(ResourceError):
            mgr.create_instance(SimMode.SIL, "Town01", QualityLevel.EPIC)


class TestSimInstanceDataModel:
    """实例数据模型序列化测试（设计文档 §10.2.2）。"""

    def test_to_dict_matches_doc_structure(self) -> None:
        from hunter_sim.resource_manager.instance_manager import SimInstance
        inst = SimInstance(
            sim_instance_id="sim_001",
            status=InstanceStatus.RUNNING,
            mode=SimMode.VIL,
            map_id="Town03",
            quality=QualityLevel.MEDIUM,
            gpu_id=0,
            carla_host="10.0.0.10",
            scene_id="scene_001",
            vehicle_id="HUNTER-001",
            create_time=1755597600.0,
            start_time=1755597605.0,
            docker_container_id="abc123",
        )
        d = inst.to_dict()
        assert d["sim_instance_id"] == "sim_001"
        assert d["status"] == "running"
        assert d["carla_server"] == {
            "host": "10.0.0.10", "rpc_port": 2000, "stream_port": 2001, "container_id": "abc123",
        }
        assert d["map"] == "Town03"
        assert d["scene_id"] == "scene_001"
        assert d["mode"] == "vil"
        assert d["vehicle_id"] == "HUNTER-001"
        assert d["create_time"].startswith("2025-08-19T10:00:00")
        assert d["start_time"].startswith("2025-08-19T10:00:05")
        assert d["gpu_id"] == 0
        assert d["resources"] == {"cpu_limit": "4", "memory_limit": "8Gi", "gpu_limit": "1"}

    def test_start_time_none_when_not_started(self) -> None:
        from hunter_sim.resource_manager.instance_manager import SimInstance
        inst = SimInstance(
            sim_instance_id="sim_x", status=InstanceStatus.CREATED, mode=SimMode.SIL,
            map_id="Town01", quality=QualityLevel.LOW, gpu_id=0,
        )
        assert inst.to_dict()["start_time"] is None
