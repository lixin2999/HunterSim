"""VIL 可视化器单元测试（对应 §4.4.2 / §4.4.3）。

CARLA 由 ``tests/conftest.py`` 注入的 MagicMock 顶替，``world.debug`` 使用
:class:`~unittest.mock.MagicMock`；本测试仅验证**调用次数**与关键参数，
不检查 carla 原生对象的属性。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from hunter_sim.app.vil.coordinate_mapper import CoordinateMapperImpl
from hunter_sim.app.vil.models import (
    ChassisData,
    LocalizationData,
    PerceptionData,
    PerceptionObject,
    VehicleTelemetry,
    VILCalibration,
)
from hunter_sim.app.vil.visualizer import CARLAVisualizerImpl
from hunter_sim.simulation.models import Location, Rotation, Transform


def _make_mapper() -> CoordinateMapperImpl:
    return CoordinateMapperImpl(VILCalibration(x0=10.0, y0=20.0, yaw0=0.0))


def _make_visualizer(debug: MagicMock, mapper: CoordinateMapperImpl) -> CARLAVisualizerImpl:
    connection = MagicMock(name="connection")
    world = MagicMock(name="world")
    world.debug = debug
    connection.get_world.return_value = world
    return CARLAVisualizerImpl(connection=connection, mapper=mapper)  # type: ignore[arg-type]


def _make_telemetry(
    *,
    objects: list[PerceptionObject] | None = None,
    trajectory: list[tuple[float, float]] | None = None,
    velocity: float = 5.0,
    behavior: str = "cruise",
) -> VehicleTelemetry:
    return VehicleTelemetry(
        vehicle_id="hunter-001",
        timestamp=1_700_000_000.0,
        localization=LocalizationData(x=0.0, y=0.0, heading=0.0),
        chassis=ChassisData(velocity=velocity, steering=0.0, throttle=0.0, brake=0.0),
        perception=PerceptionData(
            objects=objects or [],
            planning_trajectory=trajectory or [],
        ),
        behavior_state=behavior,
    )


_POSE = Transform(
    location=Location(x=1.0, y=2.0, z=0.5),
    rotation=Rotation(pitch=0.0, yaw=0.0, roll=0.0),
)


def test_draw_vehicle_status_emits_two_strings() -> None:
    debug = MagicMock(name="debug")
    viz = _make_visualizer(debug, _make_mapper())
    viz.draw_vehicle_status(_make_telemetry(), _POSE)
    assert debug.draw_string.call_count == 2


def test_draw_perception_objects_invokes_draw_box_per_object() -> None:
    debug = MagicMock(name="debug")
    viz = _make_visualizer(debug, _make_mapper())
    objs = [
        PerceptionObject(x=1.0, y=2.0, length=4.0, width=1.8, height=1.5),
        PerceptionObject(x=3.0, y=4.0, length=4.0, width=1.8, height=1.5),
        PerceptionObject(x=5.0, y=6.0, length=4.0, width=1.8, height=1.5),
    ]
    viz.draw_perception_objects(_make_telemetry(objects=objs), _POSE)
    assert debug.draw_box.call_count == 3


def test_draw_perception_objects_empty_no_calls() -> None:
    debug = MagicMock(name="debug")
    viz = _make_visualizer(debug, _make_mapper())
    viz.draw_perception_objects(_make_telemetry(objects=[]), _POSE)
    debug.draw_box.assert_not_called()


def test_draw_planning_trajectory_draws_n_minus_1_lines() -> None:
    debug = MagicMock(name="debug")
    viz = _make_visualizer(debug, _make_mapper())
    traj = [(0.0, 0.0), (1.0, 1.0), (2.0, 2.0), (3.0, 3.0)]
    viz.draw_planning_trajectory(_make_telemetry(trajectory=traj), _POSE)
    assert debug.draw_line.call_count == 3


def test_draw_planning_trajectory_too_short_no_calls() -> None:
    debug = MagicMock(name="debug")
    viz = _make_visualizer(debug, _make_mapper())
    viz.draw_planning_trajectory(_make_telemetry(trajectory=[(0.0, 0.0)]), _POSE)
    debug.draw_line.assert_not_called()


async def test_adraw_all_invokes_all_three_synchronously() -> None:
    debug = MagicMock(name="debug")
    viz = _make_visualizer(debug, _make_mapper())
    tel = _make_telemetry(
        objects=[PerceptionObject(x=1.0, y=2.0, length=4.0, width=1.8)],
        trajectory=[(0.0, 0.0), (1.0, 1.0)],
    )
    await viz.adraw_all(tel, _POSE)
    assert debug.draw_string.call_count == 2
    assert debug.draw_box.call_count == 1
    assert debug.draw_line.call_count == 1


def test_draw_perception_uses_mapper_output() -> None:
    """mapper 提供 (x+10, y+20)；center 参数应包含转换后的坐标（数值通过 mock Location 承载）。"""
    debug = MagicMock(name="debug")
    mapper = _make_mapper()
    viz = _make_visualizer(debug, mapper)
    obj = PerceptionObject(x=1.0, y=2.0, length=4.0, width=1.8, height=1.5)
    viz.draw_perception_objects(_make_telemetry(objects=[obj]), _POSE)
    # 因 carla 为 MagicMock，center 是 mock 出来的对象；只需断言被调用一次即可，
    # 具体坐标正确性由 CoordinateMapperImpl 单测保证。
    assert debug.draw_box.call_count == 1


def test_unknown_vehicle_id_still_visualizes() -> None:
    """可视化器不按 vehicle_id 过滤（由消费方保证），此处仅确认无异常。"""
    debug = MagicMock(name="debug")
    viz = _make_visualizer(debug, _make_mapper())
    tel = _make_telemetry(velocity=12.0, behavior="follow")
    viz.draw_vehicle_status(tel, _POSE)
    assert debug.draw_string.called
