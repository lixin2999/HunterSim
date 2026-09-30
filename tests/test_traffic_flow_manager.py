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
        assert cfg.num_vehicles == 20
        assert cfg.num_walkers == 10
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
            ignore_traffic_light_rate=0.2,
            ignore_lane_change_rate=0.4,
            aggressive_driving_rate=0.5,
        )
        mgr = TrafficFlowManager(world, client, cfg)
        mgr.initialize()
        tm = client._traffic_manager.settings
        assert tm["synchronous_mode"] is True
        assert tm["follow_distance_pct"] == 30
        assert tm["ignore_lights_pct"] == 20
        assert tm["change_lane_pct"] == 60  # (1 - 0.4) * 100
        assert tm["aggressive_pct"] == 50

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
