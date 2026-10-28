"""感知叠加接入测试（V3.0.0）。

覆盖计划 §6.1：
- 叠加绘制被调用（栅格可被 3 整除时）；
- 节流：其余栅格不绘制；
- ``overlay_enabled=False`` 时不绘制；
- 绘制异常不影响 tick 返回 ``"applied"``；
- 感知为空时跳过绘制。
"""

from __future__ import annotations

import time
import types
from typing import Any, Optional

import pytest

from hunter_sim.common.models import PerceptionResult, Transform, VehicleState, VILSettings
from hunter_sim.vil_mapper.data_health_monitor import DataHealthStatus
from hunter_sim.vil_mapper.frame_aligner import AlignedFrame
from hunter_sim.vil_mapper.telemetry_buffer import TelemetryBuffer, TelemetryFrame
from hunter_sim.vil_mapper.vil_sync_service import VILSyncService


class _FakeHealth:
    status = DataHealthStatus.OK

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


class _FakeVehicle:
    def set_transform(self, transform: Any) -> None:  # noqa: ARG002
        return None

    def set_velocity(self, vector: Any) -> None:  # noqa: ARG002
        return None

    def set_angular_velocity(self, vector: Any) -> None:  # noqa: ARG002
        return None

    def set_wheel_steer_angle(self, wheel_location: Any, steer_angle: float) -> None:  # noqa: ARG002
        return None

    def set_light_state(self, light_state: Any) -> None:  # noqa: ARG002
        return None

    def get_velocity(self) -> Any:
        return types.SimpleNamespace(x=0.0, y=0.0, z=0.0)

    def get_transform(self) -> Any:
        return types.SimpleNamespace(rotation=types.SimpleNamespace(yaw=0.0))


class _FakeWorld:
    def tick(self) -> int:
        return 1


class _RecordingOverlay:
    def __init__(self, error: Optional[Exception] = None) -> None:
        self.calls: list[PerceptionResult] = []
        self._error = error

    def draw_perception_result(self, perception: PerceptionResult, **_kwargs: Any) -> int:
        self.calls.append(perception)
        if self._error is not None:
            raise self._error
        return len(perception.objects)


class _StubAligner:
    def __init__(self, results: list[Optional[AlignedFrame]]) -> None:
        self._results = list(results)

    def next(self, _now: float) -> Optional[AlignedFrame]:
        return self._results.pop(0) if self._results else None

    def reset(self) -> None:
        return None


def _perception() -> PerceptionResult:
    return PerceptionResult(time_stamp=time.time(), objects=[])


def _aligned(grid_index: int, *, perception: Optional[PerceptionResult] = None) -> AlignedFrame:
    ts = time.time()
    frame = TelemetryFrame(
        vehicle_id="veh-1",
        timestamp=ts,
        receive_time=ts,
        vehicle_state=VehicleState(
            time_stamp=ts,
            transform=Transform(x=0.0, y=0.0, z=0.0),
            velocity=(0.0, 0.0, 0.0),
        ),
        perception=perception,
    )
    return AlignedFrame(frame=frame, mode="interpolated", skew_ms=1.0, grid_index=grid_index)


def _service(
    results: list[Optional[AlignedFrame]],
    *,
    overlay: Optional[_RecordingOverlay] = None,
    settings: Optional[VILSettings] = None,
) -> tuple[VILSyncService, _RecordingOverlay]:
    recorder = overlay if overlay is not None else _RecordingOverlay()
    svc = VILSyncService(
        settings=settings or VILSettings(),
        buffer=TelemetryBuffer(),
        coord_transformer=_FakeCoord(),  # type: ignore[arg-type]
        extrapolator=_FakeExtrapolator(),  # type: ignore[arg-type]
        health_monitor=_FakeHealth(),  # type: ignore[arg-type]
        vehicle_actor=_FakeVehicle(),  # type: ignore[arg-type]
        world=_FakeWorld(),  # type: ignore[arg-type]
        aligner=_StubAligner(results),  # type: ignore[arg-type]
        overlay=recorder,  # type: ignore[arg-type]
    )
    return svc, recorder


# ── 绘制与节流 ────────────────────────────────────────────────────────────────


class TestOverlayWiring:
    def test_draws_on_every_third_grid(self) -> None:
        results = [_aligned(i, perception=_perception()) for i in (3, 4, 5, 6)]
        svc, overlay = _service(results)

        for _ in range(4):
            assert svc.apply_once() == "applied"

        # 栅格 3 与 6 可被 3 整除 → 绘制 2 次
        assert len(overlay.calls) == 2

    def test_throttled_grids_do_not_draw(self) -> None:
        results = [_aligned(i, perception=_perception()) for i in (7, 8)]
        svc, overlay = _service(results)

        assert svc.apply_once() == "applied"
        assert svc.apply_once() == "applied"

        assert overlay.calls == []

    def test_disabled_by_setting(self) -> None:
        svc, overlay = _service(
            [_aligned(3, perception=_perception())],
            settings=VILSettings(overlay_enabled=False),
        )

        assert svc.apply_once() == "applied"
        assert overlay.calls == []

    def test_no_perception_skips_draw(self) -> None:
        svc, overlay = _service([_aligned(3, perception=None)])

        assert svc.apply_once() == "applied"
        assert overlay.calls == []

    def test_overlay_exception_does_not_break_tick(self) -> None:
        overlay = _RecordingOverlay(error=RuntimeError("drawer unavailable"))
        svc, recorder = _service([_aligned(3, perception=_perception())], overlay=overlay)

        assert svc.apply_once() == "applied"
        assert len(recorder.calls) == 1

    def test_missing_overlay_is_noop(self) -> None:
        svc = VILSyncService(
            settings=VILSettings(),
            buffer=TelemetryBuffer(),
            coord_transformer=_FakeCoord(),  # type: ignore[arg-type]
            extrapolator=_FakeExtrapolator(),  # type: ignore[arg-type]
            health_monitor=_FakeHealth(),  # type: ignore[arg-type]
            vehicle_actor=_FakeVehicle(),  # type: ignore[arg-type]
            world=_FakeWorld(),  # type: ignore[arg-type]
            aligner=_StubAligner([_aligned(3, perception=_perception())]),  # type: ignore[arg-type]
        )

        assert svc.apply_once() == "applied"


class TestGridIndexUse:
    def test_grid_index_not_incrementing_does_not_draw(self) -> None:
        """同一栅格序号重复出现（缓冲抖动）时仍按取模规则，不额外绘制。"""
        results = [_aligned(9, perception=_perception()), _aligned(9, perception=_perception())]
        svc, overlay = _service(results)

        assert svc.apply_once() == "applied"
        assert svc.apply_once() == "applied"
        assert len(overlay.calls) == 2


@pytest.fixture(autouse=True)
def fake_carla(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = types.ModuleType("carla")
    mod.Transform = lambda location, rotation: types.SimpleNamespace(  # type: ignore[attr-defined]
        location=location, rotation=rotation
    )
    mod.Location = lambda **kwargs: types.SimpleNamespace(**kwargs)  # type: ignore[attr-defined]
    mod.Rotation = lambda **kwargs: types.SimpleNamespace(**kwargs)  # type: ignore[attr-defined]
    mod.Vector3D = lambda **kwargs: types.SimpleNamespace(**kwargs)  # type: ignore[attr-defined]
    mod.WheelLocation = types.SimpleNamespace(FRONT_LEFT=0, FRONT_RIGHT=1)  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "carla", mod)
