"""交通流管理器（PROMPT-ENG-005-A）。

管理 CARLA Traffic Manager 实例，控制交通流参数，
支持批量/按需生成车辆和行人，实现参与者复用（对象池）。
"""

from __future__ import annotations

import random
import threading
from typing import Any, Optional

from pydantic import BaseModel, Field

from hunter_sim.common.exceptions import CarlaSimulationError
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

# 行人最大步速（文档 §7.5：set_max_speed(1.4)）
_WALKER_MAX_SPEED_MS = 1.4

# 支持的车型蓝图列表（设计文档 §7.2：乘用车/商用车/摩托车/自行车）
_VEHICLE_BLUEPRINTS: list[str] = [
    "vehicle.tesla.model3",
    "vehicle.carlamotors.carlacola",
    "vehicle.chevrolet.impala",
    "vehicle.audi.a2",
    "vehicle.bmw.grandturismo",
    "vehicle.nissan.patrol_2021",
    "vehicle.mini.cooper_s_2021",
    "vehicle.ford.ambulance",
    "vehicle.vespa.vespa",           # 摩托车（两轮机动车）
    "vehicle.diamondback.century",   # 自行车（非机动车）
]

_WALKER_BLUEPRINTS: list[str] = [
    "walker.pedestrian.0001",
    "walker.pedestrian.0002",
    "walker.pedestrian.0003",
    "walker.pedestrian.0013",
    "walker.pedestrian.0014",
]

# 静态障碍物蓝图（文档 §7.2：锥桶、路障等 static.prop.*）
_STATIC_PROP_BLUEPRINTS: list[str] = [
    "static.prop.constructioncone",
    "static.prop.trafficcone",
    "static.prop.barrier",
    "static.prop.warningtriangle",
]


class TrafficFlowConfig(BaseModel):
    """交通流配置参数（默认值对齐设计文档 §7.3.2 参数化交通流表）。

    Attributes:
        num_vehicles: 自动生成车辆数量（文档默认 20）。
        num_walkers: 自动生成行人数量（文档默认 10）。
        follow_distance_m: 全局跟车距离（文档默认 2.0m）。
        speed_limit_percentage: 全局速度限制百分比（文档默认 80%）。
        allow_lane_change: 是否允许自动变道（文档默认 True）。
        use_mixed_blueprints: 车型多样性（文档默认 True）。
        ignore_traffic_light_rate: 忽略红绿灯概率 (0~1)。
        aggressive_driving_rate: 激进驾驶比例 (0~1)。
        tm_port: Traffic Manager 端口。
        synchronous_mode: 是否启用同步模式。
    """

    num_vehicles: int = Field(20, ge=0, le=200)
    num_walkers: int = Field(10, ge=0, le=100)
    follow_distance_m: float = Field(2.0, ge=0.5, le=10.0)
    speed_limit_percentage: float = Field(80.0, ge=10.0, le=200.0)
    allow_lane_change: bool = True
    use_mixed_blueprints: bool = True
    ignore_traffic_light_rate: float = Field(0.0, ge=0.0, le=1.0)
    aggressive_driving_rate: float = Field(0.1, ge=0.0, le=1.0)
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
        self._spawned_props: list[Any] = []
        self._lock: threading.Lock = threading.Lock()
        self._rng = random.Random(42)

    def initialize(self) -> None:
        """初始化 TrafficManager 并配置同步模式和参数（文档 §7.3.1）。"""
        try:
            self._tm = self._client.get_trafficmanager(self._config.tm_port)
            if self._config.synchronous_mode:
                self._tm.set_synchronous_mode(True)
            # 文档 §7.3.1：全局跟车距离（米）
            self._tm.set_global_distance_to_leading_vehicle(self._config.follow_distance_m)
            # 文档 §7.3.2：全局速度限制百分比（默认 80%）
            self._tm.set_global_percentage_speed_limits(int(self._config.speed_limit_percentage))
            self._tm.set_global_percentage_ignore_lights(
                int(self._config.ignore_traffic_light_rate * 100)
            )
            # 文档 §7.3.2：是否允许自动变道（默认 True → 100%）
            self._tm.set_global_percentage_change_lane(100 if self._config.allow_lane_change else 0)
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
            for actor in self._spawned_vehicles + self._spawned_walkers + self._spawned_props:
                try:
                    actor.destroy()
                except Exception:
                    pass
            self._spawned_vehicles.clear()
            self._spawned_walkers.clear()
            self._spawned_props.clear()
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
                    # 文档 §7.5：start → go_to_location → set_max_speed（行人速度 1.4 m/s）
                    controller.start()
                    controller.set_max_speed(_WALKER_MAX_SPEED_MS)
                    target = self._world.get_random_location_from_navigation()
                    controller.go_to_location(target)
                    self._spawned_walkers.extend([walker, controller])
                    spawned += 1
                except Exception:
                    pass
            return spawned
        except Exception as exc:
            raise CarlaSimulationError("spawn_walkers", str(exc)) from exc

    def spawn_static_obstacles(self, count: int = 5) -> int:
        """生成静态障碍物（锥桶、路障等，文档 §7.2）。

        Args:
            count: 障碍物数量。

        Returns:
            实际生成的障碍物数量。
        """
        if count <= 0:
            return 0
        try:
            bp_lib = self._world.get_blueprint_library()
            spawn_points = self._world.get_map().get_spawn_points()
            if not spawn_points:
                logger.warning("No spawn points available for static props")
                return 0
            self._rng.shuffle(spawn_points)
            spawned = 0
            for i in range(min(count, len(spawn_points))):
                bp_name = self._rng.choice(_STATIC_PROP_BLUEPRINTS)
                bp = bp_lib.find(bp_name)
                if bp is None:
                    continue
                try:
                    prop = self._world.spawn_actor(bp, spawn_points[i])
                    self._spawned_props.append(prop)
                    spawned += 1
                except Exception:
                    pass
            logger.info(f"Static obstacles spawned: {spawned}")
            return spawned
        except Exception as exc:
            raise CarlaSimulationError("spawn_static_obstacles", str(exc)) from exc
