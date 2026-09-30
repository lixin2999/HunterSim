"""VIL 状态可视化单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

import math
import sys
import types
from typing import Any

import pytest

from hunter_sim.common.models import (
    DetectedObject,
    PerceptionResult,
    Transform,
    VehicleState,
)
from hunter_sim.vil_mapper.state_visualizer import PerceptionOverlay, StateVisualizer


class _Loc:
    def __init__(self, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> None:
        self.x, self.y, self.z = x, y, z


class _Color:
    def __init__(self, *args: int) -> None:
        self.values = args


class _Vec3:
    def __init__(self, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> None:
        self.x, self.y, self.z = x, y, z


class _Rot:
    def __init__(self, pitch: float = 0.0, yaw: float = 0.0, roll: float = 0.0) -> None:
        self.pitch, self.yaw, self.roll = pitch, yaw, roll


class _LightState(int):
    """模拟 carla.VehicleLightState 位标志枚举。"""

    NONE = 0
    LowBeam = 1
    LeftTurnLight = 128
    RightTurnLight = 256
    Brake = 512


@pytest.fixture(autouse=True)
def fake_carla(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = types.ModuleType("carla")
    mod.Location = _Loc  # type: ignore[attr-defined]
    mod.Color = _Color  # type: ignore[attr-defined]
    mod.Vector3D = _Vec3  # type: ignore[attr-defined]
    mod.Rotation = _Rot  # type: ignore[attr-defined]
    mod.VehicleLightState = _LightState  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "carla", mod)


class _Debug:
    def __init__(self) -> None:
        self.lines: list[dict[str, Any]] = []
        self.boxes: list[dict[str, Any]] = []

    def draw_line(self, start: Any, end: Any, **kwargs: Any) -> None:
        self.lines.append({"start": start, "end": end, **kwargs})

    def draw_box(self, **kwargs: Any) -> None:
        self.boxes.append(kwargs)


class _World:
    def __init__(self) -> None:
        self.debug = _Debug()


def _state(speed: float, velocity: tuple[float, float, float] = (1.0, 0.0, 0.0)) -> VehicleState:
    return VehicleState(
        time_stamp=0.0,
        transform=Transform(x=1.0, y=2.0, z=0.3),
        velocity=velocity,
        vehicle_speed=speed,
    )


class TestStateVisualizer:
    def test_draws_speed_line_when_moving(self) -> None:
        world = _World()
        StateVisualizer(world).draw_vehicle_state(_state(speed=3.0))
        assert len(world.debug.lines) == 1

    def test_skips_when_stationary(self) -> None:
        world = _World()
        StateVisualizer(world).draw_vehicle_state(_state(speed=0.05))
        assert world.debug.lines == []

    def test_skips_when_disabled(self) -> None:
        world = _World()
        StateVisualizer(world, enabled=False).draw_vehicle_state(_state(speed=5.0))
        assert world.debug.lines == []

    def test_error_swallowed(self) -> None:
        class _Bad:
            debug = None  # 触发 AttributeError

        StateVisualizer(_Bad()).draw_vehicle_state(_state(speed=5.0))  # 不应抛出

    def test_apply_vehicle_lights(self) -> None:
        # 设计文档 §4.4.2：刹车灯/转向灯同步
        class _Vehicle:
            def __init__(self) -> None:
                self.light_state: Any = None

            def set_light_state(self, state: Any) -> None:
                self.light_state = state

        v = _Vehicle()
        StateVisualizer(_World()).apply_vehicle_lights(v, brake=0.8, left_turn=True)
        assert v.light_state & _LightState.Brake
        assert v.light_state & _LightState.LeftTurnLight

    def test_apply_vehicle_lights_disabled_or_none(self) -> None:
        StateVisualizer(_World(), enabled=False).apply_vehicle_lights(object(), brake=1.0)
        StateVisualizer(_World()).apply_vehicle_lights(None)  # 不应抛出

    def test_draw_planned_trajectory(self) -> None:
        # 设计文档 §4.4.2：规划轨迹彩色折线
        world = _World()
        pts = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (2.0, 1.0, 0.0)]
        drawn = StateVisualizer(world).draw_planned_trajectory(pts)
        assert drawn == 2
        assert len(world.debug.lines) == 2

    def test_draw_planned_trajectory_too_few_points(self) -> None:
        world = _World()
        assert StateVisualizer(world).draw_planned_trajectory([(0.0, 0.0, 0.0)]) == 0
        assert world.debug.lines == []


class TestPerceptionOverlay:
    def _perception(self, n: int) -> PerceptionResult:
        return PerceptionResult(
            time_stamp=0.0,
            objects=[
                DetectedObject(
                    object_id=i,
                    object_type="vehicle",
                    transform=Transform(x=float(i), y=1.0, z=0.0, yaw=math.pi / 2),
                    size=(4.0, 2.0, 1.5),
                )
                for i in range(n)
            ],
        )

    def test_draws_all_objects(self) -> None:
        world = _World()
        overlay = PerceptionOverlay(world)
        count = overlay.draw_perception_result(self._perception(3))
        assert count == 3
        assert len(world.debug.boxes) == 3
        assert overlay.total_draw_count == 3

    def test_disabled_returns_zero(self) -> None:
        world = _World()
        overlay = PerceptionOverlay(world, enabled=False)
        assert overlay.draw_perception_result(self._perception(2)) == 0
        assert world.debug.boxes == []

    def test_accumulates_and_resets(self) -> None:
        overlay = PerceptionOverlay(_World())
        overlay.draw_perception_result(self._perception(2))
        overlay.draw_perception_result(self._perception(1))
        assert overlay.total_draw_count == 3
        overlay.reset_counter()
        assert overlay.total_draw_count == 0

    def test_draw_error_on_object_decremented(self) -> None:
        class _FailingDebug:
            def draw_box(self, **kwargs: Any) -> None:
                raise RuntimeError("draw fail")

        class _W:
            debug = _FailingDebug()

        overlay = PerceptionOverlay(_W())  # type: ignore[arg-type]
        count = overlay.draw_perception_result(self._perception(2))
        assert count == 0  # 每个目标绘制失败被吞掉
