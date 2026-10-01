"""HUNTER SE 车辆模型与蓝图生成器测试（PROMPT-ENG-001-C）。

注入最小 carla 模块使 _make_carla_transform / _make_carla_vehicle_control 可执行，
覆盖 VIL/SIL 双模式控制器、蓝图生成与传感器安装位置查询。
"""

from __future__ import annotations

import math
import sys
import types
from typing import Any, Optional

import pytest

from hunter_sim.common.exceptions import CarlaSimulationError, ValidationError
from hunter_sim.common.models import Transform, VehicleControlCommand
from hunter_sim.engine import hunter_se_vehicle as hsv
from hunter_sim.engine.hunter_se_vehicle import (
    HunterSEParameters,
    HunterSESILController,
    HunterSEVehicleController,
    VehicleBlueprintGenerator,
)
from hunter_sim.engine import vehicle_blueprint_generator as vbg
from tests.mocks.carla_mocks import MockBlueprintLibrary, MockLocation, MockTransform


class _Vec3:
    def __init__(self, x: float = 0, y: float = 0, z: float = 0) -> None:
        self.x, self.y, self.z = x, y, z


class _Rot:
    def __init__(self, pitch: float = 0, yaw: float = 0, roll: float = 0) -> None:
        self.pitch, self.yaw, self.roll = pitch, yaw, roll


class _StubActor:
    """具备 get_location/get_rotation/get_velocity 的车辆替身。"""

    def __init__(self, speed: float = 0.0) -> None:
        self.destroyed = False
        self.last_transform: Any = None
        self.last_control: Any = None
        self.last_velocity: Any = None
        self.last_angular_velocity: Any = None
        self._loc = MockLocation(1.0, 2.0, 0.5)
        self._rot = _Rot(0.0, 90.0, 0.0)
        self._vel = _Vec3(speed, 0.0, 0.0)

    def set_transform(self, tf: Any) -> None:
        if self.destroyed:
            raise RuntimeError("destroyed")
        self.last_transform = tf

    def set_velocity(self, vec: Any) -> None:
        if self.destroyed:
            raise RuntimeError("destroyed")
        self.last_velocity = vec

    def set_angular_velocity(self, vec: Any) -> None:
        if self.destroyed:
            raise RuntimeError("destroyed")
        self.last_angular_velocity = vec

    def apply_control(self, ctrl: Any) -> None:
        if self.destroyed:
            raise RuntimeError("destroyed")
        self.last_control = ctrl

    def get_location(self) -> Any:
        return self._loc

    def get_rotation(self) -> Any:
        return self._rot

    def get_velocity(self) -> Any:
        return self._vel

    def destroy(self) -> None:
        self.destroyed = True


@pytest.fixture(autouse=True)
def fake_carla(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = types.ModuleType("carla")

    class _Loc:
        def __init__(self, x: float = 0, y: float = 0, z: float = 0) -> None:
            self.x, self.y, self.z = x, y, z

    class _Rotation:
        def __init__(self, pitch: float = 0, yaw: float = 0, roll: float = 0) -> None:
            self.pitch, self.yaw, self.roll = pitch, yaw, roll

    class _Transform:
        def __init__(self, location: Any = None, rotation: Any = None) -> None:
            self.location, self.rotation = location, rotation

    class _VehicleControl:
        def __init__(self, **kw: Any) -> None:
            self.__dict__.update(kw)

    class _Vector3D:
        def __init__(self, x: float = 0, y: float = 0, z: float = 0) -> None:
            self.x, self.y, self.z = x, y, z

    mod.Location = _Loc  # type: ignore[attr-defined]
    mod.Rotation = _Rotation  # type: ignore[attr-defined]
    mod.Transform = _Transform  # type: ignore[attr-defined]
    mod.VehicleControl = _VehicleControl  # type: ignore[attr-defined]
    mod.Vector3D = _Vector3D  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "carla", mod)


class TestParameters:
    def test_defaults(self) -> None:
        p = HunterSEParameters()
        assert p.mass_kg == 60.0
        assert p.wheelbase_m == pytest.approx(0.46)

    def test_max_steer_validator(self) -> None:
        with pytest.raises(ValueError):
            HunterSEParameters(max_steer_rad=1.0)  # > pi/4


class TestVILController:
    def test_apply_transform(self) -> None:
        actor = _StubActor()
        ctrl = HunterSEVehicleController(actor)
        ctrl.apply_transform(Transform(x=1, y=2, z=0, yaw=math.pi / 2))
        assert actor.last_transform is not None

    def test_get_state(self) -> None:
        actor = _StubActor(speed=2.0)
        ctrl = HunterSEVehicleController(actor)
        state = ctrl.get_state()
        assert state.vehicle_speed == pytest.approx(2.0)
        assert state.transform.yaw == pytest.approx(math.pi / 2)

    def test_apply_velocity_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        actor = _StubActor()
        ctrl = HunterSEVehicleController(actor)
        ctrl.apply_velocity(100.0, 0.0, 0.0)  # 超限速触发 warning（不抛异常）
        assert actor.last_velocity is not None

    def test_apply_velocity_sets_actor(self) -> None:
        # 设计文档 §4.4.1：VIL 模式同步实车速度到虚拟车辆
        actor = _StubActor()
        ctrl = HunterSEVehicleController(actor)
        ctrl.apply_velocity(2.0, 0.5, 0.0)
        assert actor.last_velocity.x == pytest.approx(2.0)
        assert actor.last_velocity.y == pytest.approx(0.5)

    def test_apply_angular_velocity(self) -> None:
        # 审查项 I：内部 rad/s，CARLA set_angular_velocity 边界换算 deg/s
        actor = _StubActor()
        ctrl = HunterSEVehicleController(actor)
        ctrl.apply_angular_velocity(0.0, 0.0, 0.3)
        assert actor.last_angular_velocity.z == pytest.approx(math.degrees(0.3))

    def test_destroy_then_error(self) -> None:
        actor = _StubActor()
        ctrl = HunterSEVehicleController(actor)
        ctrl.destroy()
        assert actor.destroyed is True
        ctrl.destroy()  # 幂等
        with pytest.raises(CarlaSimulationError):
            ctrl.apply_transform(Transform(x=0, y=0, z=0))

    def test_get_state_error_wrapped(self) -> None:
        actor = _StubActor()
        actor.get_location = lambda: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[assignment]
        ctrl = HunterSEVehicleController(actor)
        with pytest.raises(CarlaSimulationError):
            ctrl.get_state()


class TestSILController:
    def test_apply_command(self) -> None:
        actor = _StubActor()
        ctrl = HunterSESILController(actor)
        ctrl.apply_command(VehicleControlCommand(throttle=0.5, steer=0.2, brake=0.0))
        assert actor.last_control.throttle == 0.5

    def test_invalid_throttle(self) -> None:
        ctrl = HunterSESILController(_StubActor())
        cmd = VehicleControlCommand()
        cmd.throttle = 2.0  # 绕过构造期校验
        with pytest.raises(ValidationError):
            ctrl.apply_command(cmd)

    def test_invalid_steer(self) -> None:
        ctrl = HunterSESILController(_StubActor())
        cmd = VehicleControlCommand()
        cmd.steer = -3.0
        with pytest.raises(ValidationError):
            ctrl.apply_command(cmd)

    def test_get_state_and_destroy(self) -> None:
        actor = _StubActor(speed=1.0)
        ctrl = HunterSESILController(actor)
        assert ctrl.get_state().vehicle_speed == pytest.approx(1.0)
        ctrl.destroy()
        with pytest.raises(CarlaSimulationError):
            ctrl.apply_command(VehicleControlCommand())


class TestBlueprintGenerator:
    def test_fallback_blueprint(self) -> None:
        lib = MockBlueprintLibrary()
        gen = VehicleBlueprintGenerator(lib)
        bp = gen.create_blueprint()
        assert bp.id.startswith("vehicle")

    def test_no_blueprint_found(self) -> None:
        class _Empty:
            def find(self, _n: str) -> Optional[Any]:
                return None

            def filter(self, _p: str) -> list:
                return []

        gen = VehicleBlueprintGenerator(_Empty())
        with pytest.raises(CarlaSimulationError):
            gen.create_blueprint()

    def test_get_carla_time_with_carla(self) -> None:
        assert hsv._get_carla_time() == 0.0


class TestSensorMountsAndSpawn:
    def test_supported_vehicles(self) -> None:
        assert any(v["blueprint_id"] == "vehicle.hunter_se" for v in vbg.SUPPORTED_VEHICLES)

    def test_get_sensor_mount(self) -> None:
        mount = vbg.get_sensor_mount("lidar_top")
        assert mount is not None
        assert mount.x == pytest.approx(0.23)
        assert vbg.get_sensor_mount("ghost") is None

    def test_mount_to_carla_transform(self) -> None:
        mount = vbg.get_sensor_mount("camera_front")
        tf = mount.to_carla_transform()  # type: ignore[union-attr]
        assert tf.location.z == pytest.approx(0.30)

    def test_spawn_hunter_se(self) -> None:
        from tests.mocks.carla_mocks import MockWorld

        world = MockWorld("Town03")
        vehicle = vbg.spawn_hunter_se(world, MockBlueprintLibrary(), MockTransform())
        assert vehicle is not None

    def test_spawn_hunter_se_failure(self) -> None:
        class _NullWorld:
            def spawn_actor(self, bp: Any, tf: Any) -> Optional[Any]:
                return None

        with pytest.raises(RuntimeError):
            vbg.spawn_hunter_se(_NullWorld(), MockBlueprintLibrary(), MockTransform())
