"""交通流管理器（PROMPT-ENG-005-A）。

管理 CARLA Traffic Manager 实例，控制交通流参数，
支持批量/按需生成车辆和行人，实现参与者复用（对象池）。
"""

from __future__ import annotations

import random
import threading
from typing import Any, Optional, Protocol

from pydantic import BaseModel, Field

from hunter_sim.common.exceptions import CarlaSimulationError
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

# 支持的车型蓝图列表
_VEHICLE_BLUEPRINTS: list[str] = [
    "vehicle.tesla.model3",
    "vehicle.carlamotors.carlacola",
    "vehicle.chevrolet.impala",
    "vehicle.audi.a2",
    "vehicle.bmw.grandturismo",
    "vehicle.nissan.patrol_2021",
    "vehicle.mini.cooper_s_2021",
    "vehicle.ford.ambulance",
]

_WALKER_BLUEPRINTS: list[str] = [
    "walker.pedestrian.0001",
    "walker.pedestrian.0002",
    "walker.pedestrian.0003",
    "walker.pedestrian.0013",
    "walker.pedestrian.0014",
]


class TrafficFlowConfig(BaseModel):
    """交通流配置参数。

    Attributes:
        num_vehicles: 自动生成车辆数量。
        num_walkers: 自动生成行人数量。
        follow_distance_m: 跟车距离（米）。
        ignore_traffic_light_rate: 忽略红绿灯概率 (0~1)。
        ignore_lane_change_rate: 禁止车道变更比例 (0~1)。
        aggressive_driving_rate: 激进驾驶比例 (0~1)。
        use_mixed_blueprints: 是否使用混合车型。
        tm_port: Traffic Manager 端口。
        synchronous_mode: 是否启用同步模式。
    """

    num_vehicles: int = Field(20, ge=0, le=200)
    num_walkers: int = Field(10, ge=0, le=100)
    follow_distance_m: float = Field(2.0, ge=0.5, le=10.0)
    ignore_traffic_light_rate: float = Field(0.0, ge=0.0, le=1.0)
    ignore_lane_change_rate: float = Field(0.3, ge=0.0, le=1.0)
    aggressive_driving_rate: float = Field(0.1, ge=0.0, le=1.0)
    use_mixed_blueprints: bool = True
    tm_port: int = Field(8000, ge=1024, le=65535)
    synchronous_mode: bool = True


class TrafficFlowManager:
    """CARLA 交通流生成与管理器。

    通过 TrafficManager 控制车辆自动驾驶行为，
    管理参与者的生成、复用和销毁。

    Args:
        world: CARLA World 对象。
        client: CARLA Client 对象（用于获取 TrafficManager）。
        config: 交通流配置。
    """

    def __init__(
        self,
        world: Any,
        client: Any,
        config: Optional[TrafficFlowConfig] = None,
    ) -> None:
        self._world = world
        self._client = client
        self._config = config or TrafficFlowConfig()
        self._tm: Any = None
        self._spawned_vehicles: list[Any] = []
        self._spawned_walkers: list[Any] = []
        self._lock: threading.Lock = threading.Lock()
        self._rng = random.Random(42)

    def initialize(self) -> None:
        """初始化 TrafficManager 并配置同步模式和参数。"""
        try:
            self._tm = self._client.get_trafficmanager(self._config.tm_port)
            if self._config.synchronous_mode:
                self._tm.set_synchronous_mode(True)
            self._tm.set_global_percentage_distance_to_leading_vehicle(
                int(self._config.follow_distance_m * 10)
            )
            self._tm.set_global_percentage_ignore_lights(
                int(self._config.ignore_traffic_light_rate * 100)
            )
            self._tm.set_global_percentage_change_lane(
                int((1.0 - self._config.ignore_lane_change_rate) * 100)
            )
            self._tm.set_global_percentage_aggressive_driving(
                int(self._config.aggressive_driving_rate * 100)
            )
            logger.info(
                f"TrafficManager initialized: port={self._config.tm_port}, "
                f"sync={self._config.synchronous_mode}"
            )
        except Exception as exc:
            raise CarlaSimulationError("TrafficManager.init", str(exc)) from exc

    def spawn_traffic_flow(
        self,
        num_vehicles: Optional[int] = None,
        num_walkers: Optional[int] = None,
    ) -> tuple[int, int]:
        """批量生成交通参与者。

        Args:
            num_vehicles: 覆盖配置中的车辆数量（None 使用配置值）。
            num_walkers: 覆盖配置中的行人数量。

        Returns:
            (spawned_vehicles, spawned_walkers) 数量元组。
        """
        nv = num_vehicles if num_vehicles is not None else self._config.num_vehicles
        nw = num_walkers if num_walkers is not None else self._config.num_walkers
        with self._lock:
            v_count = self._spawn_vehicles(nv)
            w_count = self._spawn_walkers(nw)
        logger.info(f"Traffic flow spawned: vehicles={v_count}, walkers={w_count}")
        return v_count, w_count

    def cleanup(self) -> None:
        """销毁所有已生成的交通参与者。"""
        with self._lock:
            for actor in self._spawned_vehicles + self._spawned_walkers:
                try:
                    actor.destroy()
                except Exception:
                    pass
            self._spawned_vehicles.clear()
            self._spawned_walkers.clear()
        logger.info("Traffic flow cleaned up")

    @property
    def vehicle_count(self) -> int:
        """当前存活车辆数。"""
        with self._lock:
            return len(self._spawned_vehicles)

    @property
    def walker_count(self) -> int:
        """当前存活行人数。"""
        with self._lock:
            return len(self._spawned_walkers)

    def _spawn_vehicles(self, count: int) -> int:
        """内部生成车辆实现。"""
        if count == 0:
            return 0
        try:
            bp_lib = self._world.get_blueprint_library()
            spawn_points = self._world.get_map().get_spawn_points()
            if not spawn_points:
                logger.warning("No spawn points available for vehicles")
                return 0

            self._rng.shuffle(spawn_points)
            spawned = 0
            for i in range(min(count, len(spawn_points))):
                bp_name = (
                    self._rng.choice(_VEHICLE_BLUEPRINTS)
                    if self._config.use_mixed_blueprints
                    else _VEHICLE_BLUEPRINTS[0]
                )
                bp = bp_lib.find(bp_name)
                if bp is None:
                    bp = bp_lib.filter("vehicle.*")[0]
                try:
                    vehicle = self._world.spawn_actor(bp, spawn_points[i])
                    vehicle.set_autopilot(True, self._config.tm_port)
                    self._spawned_vehicles.append(vehicle)
                    spawned += 1
                except Exception:
                    pass
            return spawned
        except Exception as exc:
            raise CarlaSimulationError("spawn_vehicles", str(exc)) from exc

    def _spawn_walkers(self, count: int) -> int:
        """内部生成行人实现。"""
        if count == 0:
            return 0
        try:
            bp_lib = self._world.get_blueprint_library()
            walker_bp = bp_lib.filter("walker.pedestrian.*")
            if not walker_bp:
                return 0
            controller_bp = bp_lib.find("controller.ai.walker")
            if controller_bp is None:
                return 0

            spawned = 0
            for _ in range(count):
                try:
                    bp = self._rng.choice(walker_bp)
                    transform = self._world.get_random_location_from_navigation()
                    if transform is None:
                        continue
                    walker = self._world.spawn_actor(bp, transform)
                    controller = self._world.spawn_actor(controller_bp, transform, attach_to=walker)
                    controller.start()
                    controller.go_to(
                        self._world.get_random_location_from_navigation(),
                        speed=self._rng.uniform(0.5, 1.4),
                    )
                    self._spawned_walkers.extend([walker, controller])
                    spawned += 1
                except Exception:
                    pass
            return spawned
        except Exception as exc:
            raise CarlaSimulationError("spawn_walkers", str(exc)) from exc
