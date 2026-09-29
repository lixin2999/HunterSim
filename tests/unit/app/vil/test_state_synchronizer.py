"""VIL 状态同步器单元测试（对应 §4.4.1）。

VehicleController / CoordinateMapper / SyncController 均以最小 fake 实现，
不依赖 CARLA。
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from hunter_sim.app.vil.models import (
    ChassisData,
    ImuData,
    LocalizationData,
    PerceptionData,
    VehicleTelemetry,
    VILCalibration,
    VILConfig,
)
from hunter_sim.app.vil.state_synchronizer import StateSynchronizerImpl
from hunter_sim.simulation.models import Transform, Vector3D


class _FakeVehicle:
    def __init__(self) -> None:
        self.transforms: list[Transform] = []
        self.velocities: list[Vector3D] = []
        self.angulars: list[Vector3D] = []

    async def set_transform(self, transform: Transform) -> None:
        self.transforms.append(transform)

    async def set_velocity(self, velocity: Vector3D) -> None:
        self.velocities.append(velocity)

    async def set_angular_velocity(self, angular_velocity: Vector3D) -> None:
        self.angulars.append(angular_velocity)


class _IdentityMapper:
    """将 odom 直接透传，yaw 保持（yaw0=0 情况下的等价）。"""

    def __init__(self, calibration: VILCalibration) -> None:
        self._cal = calibration

    @property
    def calibration(self) -> VILCalibration:
        return self._cal

    def odom_to_map(self, x: float, y: float, yaw: float) -> tuple[float, float, float]:
        return x, y, -yaw  # yaw 取反（与 CoordinateMapperImpl 语义一致）

    def object_to_map(self, x: float, y: float) -> tuple[float, float]:
        return x, y


class _NoOpSyncCtrl:
    def should_pause(self, delay_ms: float) -> bool:
        return False

    def should_extrapolate(self, delay_ms: float) -> bool:
        return False

    def compensate(self, telemetry: VehicleTelemetry, elapsed_s: float) -> VehicleTelemetry:
        return telemetry


def _make_config(**overrides: Any) -> VILConfig:
    base: dict[str, Any] = {
        "target_vehicle_id": "hunter-001",
        "calibration": VILCalibration(x0=0.0, y0=0.0, yaw0=0.0),
        "ego_z_offset": 0.5,
    }
    base.update(overrides)
    return VILConfig(**base)


def _make_telemetry(
    *,
    x: float = 1.0,
    y: float = 2.0,
    heading: float = 0.1,
    velocity: float = 8.0,
    angular_velocity: float = 0.05,
    ts: float = 1000.0,
    imu: ImuData | None = None,
) -> VehicleTelemetry:
    return VehicleTelemetry(
        vehicle_id="hunter-001",
        timestamp=ts,
        localization=LocalizationData(x=x, y=y, heading=heading),
        chassis=ChassisData(
            velocity=velocity,
            steering=0.0,
            throttle=0.0,
            brake=0.0,
            angular_velocity=angular_velocity,
        ),
        perception=PerceptionData(),
        imu=imu,
    )


async def test_sync_calls_all_three_setters() -> None:
    vehicle = _FakeVehicle()
    config = _make_config()
    syncer = StateSynchronizerImpl(
        vehicle=vehicle,  # type: ignore[arg-type]
        mapper=_IdentityMapper(config.calibration),  # type: ignore[arg-type]
        sync_ctrl=_NoOpSyncCtrl(),  # type: ignore[arg-type]
        config=config,
        clock=lambda: 1000.0,
    )
    await syncer.sync(_make_telemetry())
    assert len(vehicle.transforms) == 1
    assert len(vehicle.velocities) == 1
    assert len(vehicle.angulars) == 1


async def test_sync_transform_pose_matches_mapper_output() -> None:
    vehicle = _FakeVehicle()
    config = _make_config()
    syncer = StateSynchronizerImpl(
        vehicle=vehicle,  # type: ignore[arg-type]
        mapper=_IdentityMapper(config.calibration),  # type: ignore[arg-type]
        sync_ctrl=_NoOpSyncCtrl(),  # type: ignore[arg-type]
        config=config,
        clock=lambda: 1000.0,
    )
    tel = _make_telemetry(x=3.0, y=4.0, heading=math.radians(30.0))
    pose = await syncer.sync(tel)
    assert pose.location.x == pytest.approx(3.0)
    assert pose.location.y == pytest.approx(4.0)
    assert pose.location.z == pytest.approx(config.ego_z_offset)
    # yaw 取反后转度
    assert pose.rotation.yaw == pytest.approx(math.degrees(-math.radians(30.0)))


async def test_sync_velocity_decomposition() -> None:
    vehicle = _FakeVehicle()
    config = _make_config()
    syncer = StateSynchronizerImpl(
        vehicle=vehicle,  # type: ignore[arg-type]
        mapper=_IdentityMapper(config.calibration),  # type: ignore[arg-type]
        sync_ctrl=_NoOpSyncCtrl(),  # type: ignore[arg-type]
        config=config,
        clock=lambda: 1000.0,
    )
    # yaw_odom=0 → yaw_map=0 → 线速度 (v, 0, 0)
    tel = _make_telemetry(heading=0.0, velocity=10.0)
    await syncer.sync(tel)
    vx, vy, vz = vehicle.velocities[0].x, vehicle.velocities[0].y, vehicle.velocities[0].z
    assert vx == pytest.approx(10.0)
    assert vy == pytest.approx(0.0)
    assert vz == 0.0


async def test_sync_angular_velocity_sign_flip() -> None:
    """odom 右手系的 +yaw 角速度 → CARLA 左手系的 z 分量取反。"""
    vehicle = _FakeVehicle()
    config = _make_config()
    syncer = StateSynchronizerImpl(
        vehicle=vehicle,  # type: ignore[arg-type]
        mapper=_IdentityMapper(config.calibration),  # type: ignore[arg-type]
        sync_ctrl=_NoOpSyncCtrl(),  # type: ignore[arg-type]
        config=config,
        clock=lambda: 1000.0,
    )
    tel = _make_telemetry(angular_velocity=0.4)
    await syncer.sync(tel)
    assert vehicle.angulars[0].z == pytest.approx(-0.4)


async def test_sync_imu_pitch_roll_applied() -> None:
    vehicle = _FakeVehicle()
    config = _make_config()
    syncer = StateSynchronizerImpl(
        vehicle=vehicle,  # type: ignore[arg-type]
        mapper=_IdentityMapper(config.calibration),  # type: ignore[arg-type]
        sync_ctrl=_NoOpSyncCtrl(),  # type: ignore[arg-type]
        config=config,
        clock=lambda: 1000.0,
    )
    tel = _make_telemetry(imu=ImuData(pitch=math.radians(5.0), roll=math.radians(-3.0)))
    pose = await syncer.sync(tel)
    assert pose.rotation.pitch == pytest.approx(5.0)
    assert pose.rotation.roll == pytest.approx(-3.0)
