"""VILSyncService 转向/灯光同步测试（V3.0.0）。

覆盖计划 §6.1：
- 舵角按弧度→度换算后写入左右前轮；
- 灯光位组合正确传递（刹车/左转/右转/近光）；
- 无 carla SDK 时降级不抛异常、不污染同步统计；
- ``lights`` 为空或开关关闭时保持灯光不变。
"""

from __future__ import annotations

import math
import sys
import types
from typing import Any, Optional

import pytest

from hunter_sim.common.models import Transform, VehicleState, VILSettings
from hunter_sim.vil_mapper.data_health_monitor import DataHealthStatus
from hunter_sim.vil_mapper.telemetry_buffer import TelemetryBuffer
from hunter_sim.vil_mapper.vil_sync_service import VILSyncService


# ── 桩件 ──────────────────────────────────────────────────────────────────────


class _FakeHealth:
    def __init__(self, status: DataHealthStatus = DataHealthStatus.INTERRUPTED) -> None:
        self.status = status

    @property
    def current_status(self) -> DataHealthStatus:
        return self.status

    def check(self) -> Any:
        return types.SimpleNamespace(status=self.status)


class _FakeCoord:
    def odom_to_carla(self, **_kwargs: Any) -> tuple[float, float, float, float, float, float]:
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


class _FakeExtrapolator:
    def extrapolate_transform(self, transform: Any, _speed: float, _yaw_rate: float) -> Any:
        return transform


class _RecordingVehicle:
    def __init__(self) -> None:
        self.steer_calls: list[tuple[Any, float]] = []

    def set_transform(self, transform: Any) -> None:  # noqa: ARG002
        return None

    def set_velocity(self, vector: Any) -> None:  # noqa: ARG002
        return None

    def set_angular_velocity(self, vector: Any) -> None:  # noqa: ARG002
        return None

    def set_wheel_steer_angle(self, wheel_location: Any, steer_angle: float) -> None:
        self.steer_calls.append((wheel_location, steer_angle))

    def set_light_state(self, light_state: Any) -> None:  # noqa: ARG002
        return None

    def get_velocity(self) -> Any:
        return types.SimpleNamespace(x=0.0, y=0.0, z=0.0)

    def get_transform(self) -> Any:
        return types.SimpleNamespace(rotation=types.SimpleNamespace(yaw=0.0))


class _FakeWorld:
    def tick(self) -> int:
        return 1


class _RecordingVisualizer:
    """记录灯光下发参数。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def apply_vehicle_lights(
        self,
        vehicle: Any,  # noqa: ARG002
        brake: float = 0.0,
        left_turn: bool = False,
        right_turn: bool = False,
        low_beam: bool = False,
    ) -> None:
        self.calls.append(
            {
                "brake": brake,
                "left_turn": left_turn,
                "right_turn": right_turn,
                "low_beam": low_beam,
            }
        )


def _state(
    *,
    steering: float = 0.0,
    brake: float = 0.0,
    lights: Optional[dict[str, bool]] = None,
) -> VehicleState:
    return VehicleState(
        time_stamp=0.0,
        transform=Transform(x=0.0, y=0.0, z=0.0),
        velocity=(0.0, 0.0, 0.0),
        steering=steering,
        brake=brake,
        lights=lights if lights is not None else {},
    )


def _service(
    *,
    vehicle: Optional[_RecordingVehicle] = None,
    visualizer: Optional[_RecordingVisualizer] = None,
    settings: Optional[VILSettings] = None,
) -> tuple[VILSyncService, _RecordingVehicle]:
    veh = vehicle if vehicle is not None else _RecordingVehicle()
    svc = VILSyncService(
        settings=settings or VILSettings(),
        buffer=TelemetryBuffer(),
        coord_transformer=_FakeCoord(),  # type: ignore[arg-type]
        extrapolator=_FakeExtrapolator(),  # type: ignore[arg-type]
        health_monitor=_FakeHealth(),  # type: ignore[arg-type]
        vehicle_actor=veh,  # type: ignore[arg-type]
        world=_FakeWorld(),  # type: ignore[arg-type]
        visualizer=visualizer,  # type: ignore[arg-type]
    )
    return svc, veh


@pytest.fixture
def fake_carla(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """注入最小 carla 模块（含 WheelLocation 枚举）。"""
    mod = types.ModuleType("carla")
    mod.WheelLocation = types.SimpleNamespace(FRONT_LEFT=0, FRONT_RIGHT=1)  # type: ignore[attr-defined]
    mod.Vector3D = lambda **kwargs: types.SimpleNamespace(**kwargs)  # type: ignore[attr-defined]
    mod.Location = lambda **kwargs: types.SimpleNamespace(**kwargs)  # type: ignore[attr-defined]
    mod.Rotation = lambda **kwargs: types.SimpleNamespace(**kwargs)  # type: ignore[attr-defined]
    mod.Transform = lambda location, rotation: types.SimpleNamespace(  # type: ignore[attr-defined]
        location=location, rotation=rotation
    )
    monkeypatch.setitem(sys.modules, "carla", mod)
    return mod


# ── 舵角 ──────────────────────────────────────────────────────────────────────


class TestSteering:
    def test_writes_both_front_wheels_in_degrees(self, fake_carla: types.ModuleType) -> None:
        svc, veh = _service()
        svc._apply_steering(_state(steering=math.radians(20.0)))

        assert len(veh.steer_calls) == 2
        wheels = [call[0] for call in veh.steer_calls]
        assert wheels == [fake_carla.WheelLocation.FRONT_LEFT, fake_carla.WheelLocation.FRONT_RIGHT]
        for _wheel, angle in veh.steer_calls:
            assert angle == pytest.approx(20.0)

    def test_negative_steering_keeps_sign(self, fake_carla: types.ModuleType) -> None:
        svc, veh = _service()
        svc._apply_steering(_state(steering=math.radians(-35.0)))

        assert [angle for _w, angle in veh.steer_calls] == pytest.approx([-35.0, -35.0])

    def test_zero_steering_still_written(self, fake_carla: types.ModuleType) -> None:
        """回正也需下发，否则虚拟车保持上一次转角。"""
        svc, veh = _service()
        svc._apply_steering(_state(steering=0.0))

        assert [angle for _w, angle in veh.steer_calls] == [0.0, 0.0]

    def test_missing_carla_module_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "carla", None)
        svc, veh = _service()

        svc._apply_steering(_state(steering=0.5))  # 不得抛异常

        assert veh.steer_calls == []

    def test_backend_error_does_not_raise(self, fake_carla: types.ModuleType) -> None:
        class _Broken(_RecordingVehicle):
            def set_wheel_steer_angle(self, wheel_location: Any, steer_angle: float) -> None:
                raise RuntimeError("backend not support wheel steer")

        svc, _veh = _service(vehicle=_Broken())
        svc._apply_steering(_state(steering=0.1))  # 不得抛异常


# ── 灯光 ──────────────────────────────────────────────────────────────────────


class TestLights:
    def test_all_light_bits_forwarded(self) -> None:
        visualizer = _RecordingVisualizer()
        svc, veh = _service(visualizer=visualizer)

        svc._apply_lights(
            _state(
                brake=0.8,
                lights={
                    "left_turn": True,
                    "right_turn": False,
                    "low_beam": True,
                    "hazard": False,
                },
            )
        )

        assert visualizer.calls == [
            {"brake": 0.8, "left_turn": True, "right_turn": False, "low_beam": True}
        ]
        # 灯光由 visualizer 下发，不应直接写 vehicle.set_light_state
        assert visualizer.calls[0]["brake"] == pytest.approx(0.8)

    def test_turn_signal_combination(self) -> None:
        visualizer = _RecordingVisualizer()
        svc, _veh = _service(visualizer=visualizer)

        svc._apply_lights(_state(lights={"right_turn": True}))
        svc._apply_lights(_state(lights={"left_turn": True}))

        assert visualizer.calls[0]["right_turn"] is True
        assert visualizer.calls[0]["left_turn"] is False
        assert visualizer.calls[1]["left_turn"] is True
        assert visualizer.calls[1]["right_turn"] is False

    def test_empty_lights_skips_call(self) -> None:
        """实车未上报灯光时不得改动已开灯光。"""
        visualizer = _RecordingVisualizer()
        svc, _veh = _service(visualizer=visualizer)

        svc._apply_lights(_state(lights={}))

        assert visualizer.calls == []

    def test_missing_visualizer_skips(self) -> None:
        svc, _veh = _service(visualizer=None)
        svc._apply_lights(_state(lights={"left_turn": True}))  # 不得抛异常

    def test_lights_disabled_by_setting(self) -> None:
        visualizer = _RecordingVisualizer()
        svc, _veh = _service(visualizer=visualizer, settings=VILSettings(lights_enabled=False))

        svc._apply_lights(_state(lights={"left_turn": True}))

        assert visualizer.calls == []

    def test_unknown_light_keys_ignored(self) -> None:
        visualizer = _RecordingVisualizer()
        svc, _veh = _service(visualizer=visualizer)

        svc._apply_lights(_state(lights={"fog": True}))

        assert visualizer.calls == [
            {"brake": 0.0, "left_turn": False, "right_turn": False, "low_beam": False}
        ]
