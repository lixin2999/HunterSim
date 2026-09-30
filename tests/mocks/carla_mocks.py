"""CARLA Mock 类，用于隔离测试。

MockCarlaClient / MockWorld / MockVehicle 等类完全模拟 CARLA Python API 0.9.16 的行为，
不需要真实 CARLA 服务器即可运行所有单元测试和集成测试。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional


# ─── 基础几何 Mock ────────────────────────────────────────────────────────────


@dataclass
class MockLocation:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    def distance(self, other: "MockLocation") -> float:
        return math.sqrt((self.x - other.x) ** 2 + (self.y - other.y) ** 2 + (self.z - other.z) ** 2)

    def __sub__(self, other: "MockLocation") -> "MockVector3D":
        return MockVector3D(self.x - other.x, self.y - other.y, self.z - other.z)


@dataclass
class MockRotation:
    pitch: float = 0.0
    yaw: float = 0.0
    roll: float = 0.0


@dataclass
class MockVector3D:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    def length(self) -> float:
        return math.sqrt(self.x ** 2 + self.y ** 2 + self.z ** 2)


@dataclass
class MockTransform:
    location: MockLocation = field(default_factory=MockLocation)
    rotation: MockRotation = field(default_factory=MockRotation)

    def get_forward_vector(self) -> MockVector3D:
        yaw_rad = math.radians(self.rotation.yaw)
        return MockVector3D(math.cos(yaw_rad), math.sin(yaw_rad), 0.0)


@dataclass
class MockBoundingBox:
    location: MockLocation = field(default_factory=MockLocation)
    extent: MockVector3D = field(default_factory=lambda: MockVector3D(2.0, 1.0, 0.75))


# ─── Actor Mock ───────────────────────────────────────────────────────────────


class MockActor:
    """CARLA Actor 基类 Mock。"""

    _next_id: int = 1

    def __init__(self, type_id: str = "vehicle.unknown", attributes: Optional[dict[str, str]] = None) -> None:
        self.id: int = MockActor._next_id
        MockActor._next_id += 1
        self.type_id: str = type_id
        self.attributes: dict[str, str] = attributes or {}
        self._transform: MockTransform = MockTransform()
        self._velocity: MockVector3D = MockVector3D()
        self._angular_velocity: MockVector3D = MockVector3D()
        self._acceleration: MockVector3D = MockVector3D()
        self._alive: bool = True
        self._bounding_box: MockBoundingBox = MockBoundingBox()

    def get_transform(self) -> MockTransform:
        return self._transform

    def set_transform(self, transform: MockTransform) -> None:
        if not self._alive:
            raise RuntimeError("Cannot set transform on destroyed actor")
        self._transform = transform

    def get_velocity(self) -> MockVector3D:
        return self._velocity

    def get_angular_velocity(self) -> MockVector3D:
        return self._angular_velocity

    def get_acceleration(self) -> MockVector3D:
        return self._acceleration

    def get_bounding_box(self) -> MockBoundingBox:
        return self._bounding_box

    def get_location(self) -> MockLocation:
        return self._transform.location

    def is_alive(self) -> bool:
        return self._alive

    def destroy(self) -> None:
        self._alive = False


class MockVehicle(MockActor):
    """CARLA Vehicle Mock。"""

    def __init__(self, type_id: str = "vehicle.tesla.model3") -> None:
        super().__init__(type_id)
        self._control: Any = None
        self._autopilot: bool = False
        self._speed: float = 0.0

    def apply_control(self, control: Any) -> None:
        self._control = control

    def get_control(self) -> Any:
        return self._control

    def set_autopilot(self, enabled: bool = True, tm_port: int = 8000) -> None:
        self._autopilot = enabled

    def get_traffic_light(self) -> None:
        return None


class MockWalker(MockActor):
    """CARLA Walker Mock。"""

    pass


class MockActorControl:
    """controller.ai.walker Mock（文档 §7.5：start/go_to_location/set_max_speed）。"""

    def __init__(self) -> None:
        self._started = False
        self.max_speed: float = 0.0
        self.destination: MockLocation | None = None

    def start(self) -> None:
        self._started = True

    def go_to_location(self, location: MockLocation) -> None:
        self.destination = location

    def set_max_speed(self, speed: float) -> None:
        self.max_speed = speed

    def stop(self) -> None:
        self._started = False


# ─── World Snapshot Mock ──────────────────────────────────────────────────────


class MockSnapshot:
    def __init__(self, actors: list[MockActor]) -> None:
        self._actors = actors

    def get_actors(self) -> list[MockActor]:
        return self._actors


# ─── Blueprint Mock ───────────────────────────────────────────────────────────


class MockBlueprint:
    def __init__(self, type_id: str) -> None:
        self.id: str = type_id
        self._attributes: dict[str, str] = {}

    def set_attribute(self, key: str, value: str) -> None:
        self._attributes[key] = value

    def get_attribute(self, key: str) -> str:
        return self._attributes.get(key, "")

    def has_attribute(self, key: str) -> bool:
        return key in self._attributes


class MockBlueprintLibrary:
    def __init__(self) -> None:
        self._blueprints: list[MockBlueprint] = [
            MockBlueprint("vehicle.tesla.model3"),
            MockBlueprint("vehicle.chevrolet.impala"),
            MockBlueprint("vehicle.hunter_se"),
            MockBlueprint("walker.pedestrian.0001"),
            MockBlueprint("sensor.lidar.ray_cast"),
            MockBlueprint("sensor.camera.rgb"),
            MockBlueprint("sensor.camera.depth"),
            MockBlueprint("sensor.other.imu"),
            MockBlueprint("sensor.other.gnss"),
            MockBlueprint("sensor.other.collision"),
            MockBlueprint("sensor.other.lane_invasion"),
            MockBlueprint("sensor.other.obstacle"),
            MockBlueprint("controller.ai.walker"),
            MockBlueprint("static.prop.constructioncone"),
            MockBlueprint("static.prop.trafficcone"),
            MockBlueprint("static.prop.barrier"),
            MockBlueprint("static.prop.warningtriangle"),
        ]

    def find(self, type_id: str) -> Optional[MockBlueprint]:
        return next((bp for bp in self._blueprints if bp.id == type_id), None)

    def filter(self, pattern: str) -> list[MockBlueprint]:
        if pattern.endswith("*"):
            prefix = pattern[:-1]
            return [bp for bp in self._blueprints if bp.id.startswith(prefix)]
        return [bp for bp in self._blueprints if bp.id == pattern]


# ─── Map Mock ─────────────────────────────────────────────────────────────────


class MockMap:
    def __init__(self, name: str = "Town03") -> None:
        self.name = name
        self._spawn_points: list[MockTransform] = [
            MockTransform(MockLocation(float(i * 20), 0.0, 0.0)) for i in range(50)
        ]

    def get_spawn_points(self) -> list[MockTransform]:
        return list(self._spawn_points)

    def transform_to_geolocation(self, transform: MockTransform) -> Any:
        return None


# ─── Debug Draw Mock ──────────────────────────────────────────────────────────


class MockDebug:
    def __init__(self) -> None:
        self.draw_calls: list[dict[str, Any]] = []

    def draw_box(self, **kwargs: Any) -> None:
        self.draw_calls.append({"type": "box", **kwargs})

    def draw_line(self, start: Any, end: Any, **kwargs: Any) -> None:
        self.draw_calls.append({"type": "line", "start": start, "end": end, **kwargs})

    def draw_point(self, location: Any, **kwargs: Any) -> None:
        self.draw_calls.append({"type": "point", "location": location, **kwargs})


# ─── World Mock ───────────────────────────────────────────────────────────────


class MockWorld:
    """CARLA World Mock，模拟同步/异步模式下的 tick() 和 actor 管理。"""

    def __init__(self, map_name: str = "Town03") -> None:
        self._map = MockMap(map_name)
        self._actors: list[MockActor] = []
        self._bp_lib = MockBlueprintLibrary()
        self.debug: MockDebug = MockDebug()
        self._tick_count: int = 0
        self._settings: Any = None

    def get_map(self) -> MockMap:
        return self._map

    def get_blueprint_library(self) -> MockBlueprintLibrary:
        return self._bp_lib

    def get_actors(self) -> list[MockActor]:
        return list(self._actors)

    def get_snapshot(self) -> MockSnapshot:
        return MockSnapshot(list(self._actors))

    def tick(self) -> int:
        self._tick_count += 1
        return self._tick_count

    def wait_for_tick(self) -> None:
        pass

    def get_settings(self) -> Any:
        return self._settings

    def apply_settings(self, settings: Any) -> None:
        self._settings = settings

    def spawn_actor(
        self,
        blueprint: MockBlueprint,
        transform: MockTransform,
        attach_to: Optional[MockActor] = None,
    ) -> MockActor:
        if blueprint.id.startswith("walker"):
            actor: MockActor = MockWalker(blueprint.id)
        elif blueprint.id.startswith("vehicle"):
            actor = MockVehicle(blueprint.id)
        elif blueprint.id.startswith("controller"):
            return MockActorControl()
        else:
            actor = MockActor(blueprint.id)
        actor._transform = transform
        self._actors.append(actor)
        return actor

    def get_random_location_from_navigation(self) -> Optional[MockTransform]:
        return MockTransform(MockLocation(10.0, 5.0, 0.0))

    def get_trafficmanager(self, port: int = 8000) -> "MockTrafficManager":
        return MockTrafficManager()

    def set_weather(self, weather: Any) -> None:
        pass


# ─── Traffic Manager Mock ─────────────────────────────────────────────────────


class MockTrafficManager:
    def __init__(self) -> None:
        self.settings: dict[str, Any] = {}

    def set_synchronous_mode(self, enabled: bool) -> None:
        self.settings["synchronous_mode"] = enabled

    def set_global_distance_to_leading_vehicle(self, distance_m: float) -> None:
        self.settings["follow_distance_m"] = distance_m

    def set_global_percentage_speed_limits(self, pct: int) -> None:
        self.settings["speed_limit_pct"] = pct

    def set_global_percentage_ignore_lights(self, pct: int) -> None:
        self.settings["ignore_lights_pct"] = pct

    def set_global_percentage_change_lane(self, pct: int) -> None:
        self.settings["change_lane_pct"] = pct

    def set_global_percentage_aggressive_driving(self, pct: int) -> None:
        self.settings["aggressive_pct"] = pct


# ─── Client Mock ──────────────────────────────────────────────────────────────


class MockCarlaClient:
    """CARLA Client Mock，模拟 RPC 连接和地图加载。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 2000) -> None:
        self.host = host
        self.port = port
        self._world: MockWorld = MockWorld()
        self._traffic_manager: MockTrafficManager = MockTrafficManager()

    def get_world(self) -> MockWorld:
        return self._world

    def load_world(self, map_name: str) -> MockWorld:
        self._world = MockWorld(map_name)
        return self._world

    def generate_opendrive_world(self, xodr_content: str, **kwargs: Any) -> MockWorld:
        self._world = MockWorld("custom")
        return self._world

    def get_trafficmanager(self, port: int = 8000) -> MockTrafficManager:
        return self._traffic_manager
