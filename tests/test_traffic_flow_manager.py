"""交通流管理器单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from hunter_sim.common.exceptions import CarlaSimulationError
from hunter_sim.traffic_sim.traffic_flow_manager import (
    TrafficFlowConfig,
    TrafficFlowManager,
)
from tests.mocks.carla_mocks import MockCarlaClient, MockWorld


class TestTrafficFlowConfig:
    def test_defaults(self) -> None:
        cfg = TrafficFlowConfig()
        # 文档 §7.3.2 默认值表：车辆20/行人10/跟车2.0m/限速80%/允许变道/混合车型
        assert cfg.num_vehicles == 20
        assert cfg.num_walkers == 10
        assert cfg.follow_distance_m == 2.0
        assert cfg.speed_limit_percentage == 80.0
        assert cfg.allow_lane_change is True
        assert cfg.use_mixed_blueprints is True
        assert cfg.tm_port == 8000
        assert cfg.synchronous_mode is True

    def test_rejects_out_of_range(self) -> None:
        with pytest.raises(ValidationError):
            TrafficFlowConfig(num_vehicles=-1)
        with pytest.raises(ValidationError):
            TrafficFlowConfig(follow_distance_m=0.1)
        with pytest.raises(ValidationError):
            TrafficFlowConfig(ignore_traffic_light_rate=1.5)

    def test_rejects_bad_port(self) -> None:
        with pytest.raises(ValidationError):
            TrafficFlowConfig(tm_port=80)


class TestTrafficFlowManagerInit:
    def test_initialize_configures_tm(self) -> None:
        world = MockWorld()
        client = MockCarlaClient()
        cfg = TrafficFlowConfig(
            synchronous_mode=True,
            follow_distance_m=3.0,
            speed_limit_percentage=70.0,
            ignore_traffic_light_rate=0.2,
            allow_lane_change=False,
            aggressive_driving_rate=0.5,
        )
        mgr = TrafficFlowManager(world, client, cfg)
        mgr.initialize()
        tm = client._traffic_manager.settings
        assert tm["synchronous_mode"] is True
        # 文档 §7.3.1：跟车距离为绝对米值
        assert tm["follow_distance_m"] == 3.0
        assert tm["speed_limit_pct"] == 70
        assert tm["ignore_lights_pct"] == 20
        assert tm["change_lane_pct"] == 0  # allow_lane_change=False
        assert tm["aggressive_pct"] == 50

    def test_initialize_lane_change_allowed_by_default(self) -> None:
        world = MockWorld()
        client = MockCarlaClient()
        mgr = TrafficFlowManager(world, client, TrafficFlowConfig())
        mgr.initialize()
        # 文档 §7.3.2：默认允许自动变道 → 100%
        assert client._traffic_manager.settings["change_lane_pct"] == 100

    def test_initialize_error_wrapped(self) -> None:
        class _BoomClient:
            def get_trafficmanager(self, port: int) -> Any:
                raise RuntimeError("rpc down")

        mgr = TrafficFlowManager(MockWorld(), _BoomClient(), TrafficFlowConfig())
        with pytest.raises(CarlaSimulationError):
            mgr.initialize()


class TestSpawnAndCleanup:
    def test_spawn_vehicles_and_walkers(self) -> None:
        world = MockWorld()
        client = MockCarlaClient()
        mgr = TrafficFlowManager(world, client, TrafficFlowConfig())
        mgr.initialize()
        v, w = mgr.spawn_traffic_flow(num_vehicles=5, num_walkers=3)
        assert v == 5
        assert w == 3
        assert mgr.vehicle_count == 5
        assert mgr.walker_count == 6  # 每个行人占用 walker + controller

    def test_spawn_zero_counts(self) -> None:
        mgr = TrafficFlowManager(MockWorld(), MockCarlaClient(), TrafficFlowConfig())
        mgr.initialize()
        assert mgr.spawn_traffic_flow(num_vehicles=0, num_walkers=0) == (0, 0)

    def test_cleanup_destroys_all(self) -> None:
        world = MockWorld()
        mgr = TrafficFlowManager(world, MockCarlaClient(), TrafficFlowConfig())
        mgr.initialize()
        mgr.spawn_traffic_flow(num_vehicles=4, num_walkers=2)
        mgr.cleanup()
        assert mgr.vehicle_count == 0
        assert mgr.walker_count == 0

    def test_no_spawn_points_yields_zero(self) -> None:
        world = MockWorld()
        world._map._spawn_points = []  # 模拟无生成点
        mgr = TrafficFlowManager(world, MockCarlaClient(), TrafficFlowConfig())
        mgr.initialize()
        assert mgr.spawn_traffic_flow(num_vehicles=5, num_walkers=0) == (0, 0)

    def test_config_counts_used_when_args_none(self) -> None:
        mgr = TrafficFlowManager(
            MockWorld(), MockCarlaClient(), TrafficFlowConfig(num_vehicles=2, num_walkers=1)
        )
        mgr.initialize()
        v, w = mgr.spawn_traffic_flow()
        assert v == 2
        assert w == 1

    def test_spawn_static_obstacles(self) -> None:
        # 文档 §7.2：支持静态障碍物（锥桶、路障等）
        world = MockWorld()
        mgr = TrafficFlowManager(world, MockCarlaClient(), TrafficFlowConfig())
        mgr.initialize()
        count = mgr.spawn_static_obstacles(3)
        assert count == 3
        mgr.cleanup()
        assert mgr.vehicle_count == 0
