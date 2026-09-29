"""simulation.vehicle 单元测试（mock world/actor）。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hunter_sim.core.contracts import VehicleState
from hunter_sim.core.exceptions import CarlaSimulationError
from hunter_sim.simulation.models import (
    ControlMode,
    Location,
    Transform,
    Vector3D,
    VehicleKinematicState,
)
from hunter_sim.simulation.protocols import VehicleController
from hunter_sim.simulation.vehicle import VehicleControllerImpl


def _world_with_actor() -> tuple[MagicMock, MagicMock]:
    world = MagicMock(name="world")
    bp = MagicMock(name="blueprint")
    bp.has_attribute.return_value = True
    world.get_blueprint_library.return_value.filter.return_value = [bp]
    actor = MagicMock(name="actor")
    world.try_spawn_actor.return_value = actor
    return world, actor


def _connection(world: MagicMock) -> MagicMock:
    conn = MagicMock(name="connection")
    conn.get_world.return_value = world
    return conn


async def test_satisfies_protocol() -> None:
    controller = VehicleControllerImpl(_connection(MagicMock()))
    assert isinstance(controller, VehicleController)


async def test_spawn_sets_alive() -> None:
    world, actor = _world_with_actor()
    controller = VehicleControllerImpl(_connection(world))
    await controller.spawn("vehicle.tesla.model3", Transform(location=Location(1.0, 2.0, 0.5)))
    assert controller.is_alive is True
    world.try_spawn_actor.assert_called_once()
    actor.set_autopilot.assert_called_with(False)


async def test_spawn_blueprint_not_found_raises() -> None:
    world = MagicMock()
    world.get_blueprint_library.return_value.filter.return_value = []
    controller = VehicleControllerImpl(_connection(world))
    with pytest.raises(CarlaSimulationError):
        await controller.spawn("vehicle.unknown", Transform())


async def test_spawn_position_occupied_raises() -> None:
    world, _ = _world_with_actor()
    world.try_spawn_actor.return_value = None
    controller = VehicleControllerImpl(_connection(world))
    with pytest.raises(CarlaSimulationError):
        await controller.spawn("vehicle.tesla.model3", Transform())


async def test_apply_control_updates_state() -> None:
    world, actor = _world_with_actor()
    controller = VehicleControllerImpl(_connection(world))
    await controller.spawn("vehicle.tesla.model3", Transform())

    world.get_snapshot.return_value = SimpleNamespace(timestamp=2.0, frame=20)
    actor.get_location.return_value = SimpleNamespace(x=1.0, y=2.0, z=0.5)
    actor.get_transform.return_value = SimpleNamespace(
        rotation=SimpleNamespace(pitch=0.0, yaw=90.0, roll=0.0)
    )
    actor.get_velocity.return_value = SimpleNamespace(x=5.0, y=0.0, z=0.0)
    actor.get_acceleration.return_value = SimpleNamespace(x=0.0, y=0.0, z=9.8)

    from hunter_sim.simulation.models import VehicleCommand

    await controller.apply_control(VehicleCommand(throttle=0.7, brake=0.0, steer=0.2, gear=2))
    actor.apply_control.assert_called_once()

    state = controller.get_state()
    assert isinstance(state, VehicleState)
    assert (state.timestamp, state.frame_id) == (2.0, 20)
    assert state.yaw == 90.0
    assert state.velocity_x == 5.0
    assert state.throttle == 0.7
    assert state.steer == 0.2
    assert state.gear == 2


async def test_set_autopilot_toggle() -> None:
    world, actor = _world_with_actor()
    controller = VehicleControllerImpl(_connection(world))
    await controller.spawn("vehicle.tesla.model3", Transform())
    await controller.set_autopilot(True)
    assert controller.autopilot_enabled is True
    actor.set_autopilot.assert_called_with(True)


async def test_destroy_is_idempotent() -> None:
    world, actor = _world_with_actor()
    controller = VehicleControllerImpl(_connection(world))
    await controller.spawn("vehicle.tesla.model3", Transform())
    await controller.destroy()
    assert controller.is_alive is False
    actor.destroy.assert_called_once()
    # 二次销毁不报错，且不再调用
    await controller.destroy()
    actor.destroy.assert_called_once()


async def test_operations_before_spawn_raise() -> None:
    controller = VehicleControllerImpl(_connection(MagicMock()))
    with pytest.raises(CarlaSimulationError):
        controller.get_state()
    from hunter_sim.simulation.models import VehicleCommand

    with pytest.raises(CarlaSimulationError):
        await controller.apply_control(VehicleCommand())


async def test_default_control_mode_is_sil() -> None:
    controller = VehicleControllerImpl(_connection(MagicMock()))
    assert controller.control_mode is ControlMode.SIL


async def test_spawn_vil_mode_disables_autopilot() -> None:
    world, actor = _world_with_actor()
    controller = VehicleControllerImpl(_connection(world))
    await controller.spawn(
        "vehicle.tesla.model3", Transform(), autopilot=True, mode=ControlMode.VIL
    )
    assert controller.control_mode is ControlMode.VIL
    # VIL 下强制关闭 Traffic Manager，以便直接位姿注入。
    actor.set_autopilot.assert_called_with(False)


async def test_set_transform_calls_actor() -> None:
    world, actor = _world_with_actor()
    controller = VehicleControllerImpl(_connection(world))
    await controller.spawn("vehicle.tesla.model3", Transform(), mode=ControlMode.VIL)
    await controller.set_transform(Transform(location=Location(3.0, 4.0, 0.5)))
    actor.set_transform.assert_called_once()


async def test_set_velocity_calls_actor() -> None:
    world, actor = _world_with_actor()
    controller = VehicleControllerImpl(_connection(world))
    await controller.spawn("vehicle.tesla.model3", Transform(), mode=ControlMode.VIL)
    await controller.set_velocity(Vector3D(x=10.0, y=1.0, z=0.0))
    actor.set_velocity.assert_called_once()


async def test_set_angular_velocity_calls_actor() -> None:
    world, actor = _world_with_actor()
    controller = VehicleControllerImpl(_connection(world))
    await controller.spawn("vehicle.tesla.model3", Transform(), mode=ControlMode.VIL)
    await controller.set_angular_velocity(Vector3D(x=0.0, y=0.0, z=0.5))
    actor.set_angular_velocity.assert_called_once()


async def test_set_kinematic_state_sets_all_three() -> None:
    world, actor = _world_with_actor()
    controller = VehicleControllerImpl(_connection(world))
    await controller.spawn("vehicle.tesla.model3", Transform(), mode=ControlMode.VIL)
    state = VehicleKinematicState(
        transform=Transform(location=Location(1.0, 2.0, 0.3)),
        velocity=Vector3D(x=8.0, y=0.0, z=0.0),
        angular_velocity=Vector3D(x=0.0, y=0.0, z=0.2),
    )
    await controller.set_kinematic_state(state)
    actor.set_transform.assert_called_once()
    actor.set_velocity.assert_called_once()
    actor.set_angular_velocity.assert_called_once()


async def test_vil_operations_before_spawn_raise() -> None:
    controller = VehicleControllerImpl(_connection(MagicMock()))
    with pytest.raises(CarlaSimulationError):
        await controller.set_transform(Transform())
    with pytest.raises(CarlaSimulationError):
        await controller.set_velocity(Vector3D())
    with pytest.raises(CarlaSimulationError):
        await controller.set_angular_velocity(Vector3D())
    with pytest.raises(CarlaSimulationError):
        await controller.set_kinematic_state(VehicleKinematicState())


async def test_apply_control_passes_gear() -> None:
    import carla

    world, actor = _world_with_actor()
    controller = VehicleControllerImpl(_connection(world))
    await controller.spawn("vehicle.tesla.model3", Transform())

    from hunter_sim.simulation.models import VehicleCommand

    carla.VehicleControl.reset_mock()
    await controller.apply_control(VehicleCommand(throttle=0.5, gear=3))
    # apply_control 将高层 gear 透传给底层 carla.VehicleControl 构造。
    kwargs = carla.VehicleControl.call_args.kwargs
    assert kwargs["gear"] == 3
    assert kwargs["manual_gear_shift"] is True
    actor.apply_control.assert_called_once()
