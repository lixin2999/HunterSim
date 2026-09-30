"""传感器安装位置管理单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from hunter_sim.common.exceptions import SensorSimulationError
from hunter_sim.common.models import SensorType
from hunter_sim.sensor_sim.sensor_mount import (
    SensorMountManager,
    SensorMountSpec,
)


class _Loc:
    def __init__(self, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> None:
        self.x, self.y, self.z = x, y, z


class _Rot:
    def __init__(self, pitch: float = 0.0, yaw: float = 0.0, roll: float = 0.0) -> None:
        self.pitch, self.yaw, self.roll = pitch, yaw, roll


class _Tf:
    def __init__(self, location: Any, rotation: Any) -> None:
        self.location, self.rotation = location, rotation


@pytest.fixture(autouse=True)
def fake_carla(monkeypatch: pytest.MonkeyPatch) -> None:
    """注入 carla 桩模块，使 to_carla_transform 可测。"""
    mod = types.ModuleType("carla")
    mod.Location = _Loc  # type: ignore[attr-defined]
    mod.Rotation = _Rot  # type: ignore[attr-defined]
    mod.Transform = _Tf  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "carla", mod)


class _StubSensor:
    def __init__(self) -> None:
        self.stopped = False
        self.destroyed = False

    def stop(self) -> None:
        self.stopped = True

    def destroy(self) -> None:
        self.destroyed = True


class _FakeWorld:
    def __init__(self) -> None:
        self.spawn_calls: list[tuple[Any, Any]] = []

    def spawn_actor(self, blueprint: Any, tf: Any, attach_to: Any = None) -> _StubSensor:
        self.spawn_calls.append((blueprint, tf))
        return _StubSensor()


class TestSensorMountSpec:
    def test_to_carla_transform(self) -> None:
        spec = SensorMountSpec("m", SensorType.LIDAR, 0.23, 0.0, 0.35, yaw_deg=90.0)
        tf = spec.to_carla_transform()
        assert isinstance(tf, _Tf)
        assert tf.location.x == 0.23
        assert tf.location.z == 0.35
        assert tf.rotation.yaw == 90.0


class TestMountQueries:
    def test_default_mounts_lookup(self) -> None:
        mgr = SensorMountManager(object())
        lidar = mgr.get_mount(SensorType.LIDAR)
        assert lidar is not None
        assert lidar.mount_id == "lidar_top"
        assert lidar.x == pytest.approx(0.23)
        # 文档 §6.2.5：障碍物传感器默认前向安装
        obstacle = mgr.get_mount(SensorType.OBSTACLE)
        assert obstacle is not None
        assert obstacle.mount_id == "obstacle_front"

    def test_custom_mounts_override(self) -> None:
        custom = [SensorMountSpec("cx", SensorType.LIDAR, 1.0, 2.0, 3.0)]
        mgr = SensorMountManager(object(), custom_mounts=custom)
        m = mgr.get_mount(SensorType.LIDAR)
        assert m is not None
        assert m.mount_id == "cx"
        assert m.x == 1.0


class TestAttachDetach:
    def test_attach_records_actor(self) -> None:
        mgr = SensorMountManager(object())
        world = _FakeWorld()
        actor = mgr.attach_sensor(SensorType.LIDAR, blueprint="bp", world=world)
        assert mgr.attached_count == 1
        assert actor in world.spawn_calls[0] or isinstance(actor, _StubSensor)
        assert mgr._attached["lidar_top"] is actor

    def test_attach_unknown_type_raises(self) -> None:
        # 自定义安装列表中无 OBSTACLE 位置时报错
        custom = [SensorMountSpec("only_lidar", SensorType.LIDAR, 0.0, 0.0, 0.0)]
        mgr = SensorMountManager(object(), custom_mounts=custom)
        with pytest.raises(SensorSimulationError):
            mgr.attach_sensor(SensorType.OBSTACLE, blueprint="bp", world=_FakeWorld())

    def test_attach_spawn_error_wrapped(self) -> None:
        class _BoomWorld:
            def spawn_actor(self, *a: Any, **k: Any) -> Any:
                raise RuntimeError("spawn failed")

        mgr = SensorMountManager(object())
        with pytest.raises(SensorSimulationError):
            mgr.attach_sensor(SensorType.IMU, blueprint="bp", world=_BoomWorld())

    def test_detach_single(self) -> None:
        mgr = SensorMountManager(object())
        actor = mgr.attach_sensor(SensorType.LIDAR, "bp", _FakeWorld())
        mgr.detach_sensor(SensorType.LIDAR)
        assert actor.stopped is True
        assert actor.destroyed is True
        assert mgr.attached_count == 0

    def test_detach_all(self) -> None:
        mgr = SensorMountManager(object())
        world = _FakeWorld()
        a1 = mgr.attach_sensor(SensorType.LIDAR, "bp", world)
        a2 = mgr.attach_sensor(SensorType.IMU, "bp", world)
        assert mgr.attached_count == 2
        mgr.detach_all()
        assert mgr.attached_count == 0
        assert a1.destroyed and a2.destroyed
