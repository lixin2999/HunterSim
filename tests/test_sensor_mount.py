"""传感器安装位置管理单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from hunter_sim.common.exceptions import ConfigurationError, SensorSimulationError
from hunter_sim.common.models import SensorType
from hunter_sim.sensor_sim.sensor_mount import (
    SensorEnvelope,
    SensorMountManager,
    SensorMountSpec,
    load_mount_config,
    local_envelope,
    resolve_envelope,
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
    """注入 carla 桩模块，让 to_carla_transform 可测试。"""
    mod = types.ModuleType("carla")
    mod.Location = _Loc  # type: ignore[attr-defined]
    mod.Rotation = _Rot  # type: ignore[attr-defined]
    mod.Transform = _Tf  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "carla", mod)


class _BBox:
    def __init__(self) -> None:
        self.location = _Loc(0.0, 0.0, 0.75)
        self.extent = _Loc(2.0, 0.9, 0.75)


class _FakeVehicle:
    """带真实包围盒的车辆桩（与 CARLA Python API 一致：``bounding_box`` 属性）。"""

    type_id = "vehicle.fallback"

    @property
    def bounding_box(self) -> _BBox:
        return _BBox()


class _MethodBBoxVehicle:
    """只提供 ``get_bounding_box()`` 的车辆桩（兼容旧封装/测试替身）。"""

    type_id = "vehicle.method_bbox"

    def get_bounding_box(self) -> _BBox:
        return _BBox()


class _NoBBoxVehicle:
    """不支持包围盒的车辆桩：挂载必须显式报错，绝不写死尺寸。"""

    type_id = "vehicle.no_bbox"


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


class TestEnvelope:
    def test_local_envelope_from_bbox(self) -> None:
        envelope = local_envelope(_FakeVehicle())
        assert envelope["front_edge_x_m"] == pytest.approx(2.0)
        assert envelope["rear_edge_x_m"] == pytest.approx(-2.0)
        assert envelope["side_edge_y_m"] == pytest.approx(0.9)
        assert envelope["roof_z_m"] == pytest.approx(1.5)
        assert envelope["bottom_z_m"] == pytest.approx(0.0)

    def test_local_envelope_from_get_bounding_box(self) -> None:
        # 兼容提供 get_bounding_box() 方法的封装
        envelope = local_envelope(_MethodBBoxVehicle())
        assert envelope["roof_z_m"] == pytest.approx(1.5)
        assert envelope["front_edge_x_m"] == pytest.approx(2.0)

    def test_from_mapping_requires_all_fields(self) -> None:
        with pytest.raises(ValueError):
            SensorEnvelope.from_mapping({"front_edge_x_m": 1.0})

    def test_platform_resolved_envelope_wins(self) -> None:
        reference = {
            "status": "resolved",
            "blueprint": "vehicle.fallback",
            "envelope_m": {
                "front_edge_x_m": 3.0,
                "rear_edge_x_m": -2.5,
                "side_edge_y_m": 1.0,
                "roof_z_m": 2.0,
                "bottom_z_m": 0.2,
            },
        }
        envelope = resolve_envelope(_FakeVehicle(), reference)
        assert envelope.front_edge_x_m == pytest.approx(3.0)
        assert envelope.roof_z_m == pytest.approx(2.0)

    def test_blueprint_mismatch_falls_back_to_local(self) -> None:
        reference = {
            "status": "resolved",
            "blueprint": "vehicle.other",
            "envelope_m": {
                "front_edge_x_m": 3.0,
                "rear_edge_x_m": -2.5,
                "side_edge_y_m": 1.0,
                "roof_z_m": 2.0,
                "bottom_z_m": 0.2,
            },
        }
        envelope = resolve_envelope(_FakeVehicle(), reference)
        assert envelope.front_edge_x_m == pytest.approx(2.0)

    def test_unresolved_reference_falls_back_to_local(self) -> None:
        envelope = resolve_envelope(_FakeVehicle(), {"status": "unavailable"})
        assert envelope.roof_z_m == pytest.approx(1.5)


class TestMountQueries:
    def test_default_mounts_lookup(self) -> None:
        mgr = SensorMountManager(_FakeVehicle())
        lidar = mgr.get_mount(SensorType.LIDAR)
        assert lidar is not None
        assert lidar.mount_id == "lidar_top"
        # 锚点 roof_z=1.5 + 传感器余量 0.15
        assert lidar.x == pytest.approx(0.0)
        assert lidar.z == pytest.approx(1.65)
        # 文档 §6.2.5：障碍物传感器默认前向安装
        obstacle = mgr.get_mount(SensorType.OBSTACLE)
        assert obstacle is not None
        assert obstacle.mount_id == "obstacle_front"

    def test_rgb_camera_uses_front_edge_and_pitch(self) -> None:
        mgr = SensorMountManager(_FakeVehicle())
        camera = mgr.get_mount(SensorType.RGB_CAMERA)
        assert camera is not None
        # front_edge=2.0 + (-0.45)，roof=1.5 + (-0.10)
        assert camera.x == pytest.approx(1.55)
        assert camera.z == pytest.approx(1.40)
        assert camera.pitch_deg == pytest.approx(-8.0)

    def test_missing_bbox_is_loud(self) -> None:
        with pytest.raises(SensorSimulationError):
            SensorMountManager(_NoBBoxVehicle())

    def test_custom_mounts_override(self) -> None:
        custom = [SensorMountSpec("cx", SensorType.LIDAR, 1.0, 2.0, 3.0)]
        mgr = SensorMountManager(object(), custom_mounts=custom)
        m = mgr.get_mount(SensorType.LIDAR)
        assert m is not None
        assert m.mount_id == "cx"
        assert m.x == 1.0

    def test_mounts_config_drives_position(self) -> None:
        mounts = {
            "lidar": {
                "mount_id": "lidar_cfg",
                "anchor": "roof",
                "offset_m": {"x": 0.3, "y": 0.1, "z": 0.2},
            }
        }
        mgr = SensorMountManager(_FakeVehicle(), mounts_config={"mounts": mounts})
        lidar = mgr.get_mount(SensorType.LIDAR)
        assert lidar is not None
        assert lidar.mount_id == "lidar_cfg"
        assert lidar.x == pytest.approx(0.3)
        assert lidar.y == pytest.approx(0.1)
        assert lidar.z == pytest.approx(1.7)


class TestLoadMountConfig:
    def test_reads_mounts_section(self, tmp_path: Path) -> None:
        cfg = tmp_path / "sensor_configs.json"
        cfg.write_text(
            json.dumps({"mounts": {"lidar": {"anchor": "roof", "offset_m": {"z": 0.1}}}}),
            encoding="utf-8",
        )
        mounts = load_mount_config(cfg)
        assert mounts["lidar"]["anchor"] == "roof"
        assert "imu" in mounts  # 内建默认补齐未覆盖的传感器

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        cfg = tmp_path / "sensor_configs.json"
        cfg.write_text("{not json", encoding="utf-8")
        with pytest.raises(ConfigurationError):
            load_mount_config(cfg)


class TestAttachDetach:
    def test_attach_records_actor(self) -> None:
        mgr = SensorMountManager(_FakeVehicle())
        world = _FakeWorld()
        actor = mgr.attach_sensor(SensorType.LIDAR, blueprint="bp", world=world)
        assert mgr.attached_count == 1
        assert actor in world.spawn_calls[0] or isinstance(actor, _StubSensor)
        assert mgr._attached["lidar_top"] is actor

    def test_attach_unknown_type_raises(self) -> None:
        # 自定义安装列表中没有 OBSTACLE 位置时报错
        custom = [SensorMountSpec("only_lidar", SensorType.LIDAR, 0.0, 0.0, 0.0)]
        mgr = SensorMountManager(object(), custom_mounts=custom)
        with pytest.raises(SensorSimulationError):
            mgr.attach_sensor(SensorType.OBSTACLE, blueprint="bp", world=_FakeWorld())

    def test_attach_spawn_error_wrapped(self) -> None:
        class _BoomWorld:
            def spawn_actor(self, *a: Any, **k: Any) -> Any:
                raise RuntimeError("spawn failed")

        mgr = SensorMountManager(_FakeVehicle())
        with pytest.raises(SensorSimulationError):
            mgr.attach_sensor(SensorType.IMU, blueprint="bp", world=_BoomWorld())

    def test_detach_single(self) -> None:
        mgr = SensorMountManager(_FakeVehicle())
        actor = mgr.attach_sensor(SensorType.LIDAR, "bp", _FakeWorld())
        mgr.detach_sensor(SensorType.LIDAR)
        assert actor.stopped is True
        assert actor.destroyed is True
        assert mgr.attached_count == 0

    def test_detach_all(self) -> None:
        mgr = SensorMountManager(_FakeVehicle())
        world = _FakeWorld()
        a1 = mgr.attach_sensor(SensorType.LIDAR, "bp", world)
        a2 = mgr.attach_sensor(SensorType.IMU, "bp", world)
        assert mgr.attached_count == 2
        mgr.detach_all()
        assert mgr.attached_count == 0
        assert a1.destroyed and a2.destroyed
