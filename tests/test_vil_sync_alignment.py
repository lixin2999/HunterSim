"""VILSyncService 栅格对齐接入测试（V3.0.0）。

覆盖计划 §6.1 的 ``apply_once`` 行为：
- 走 ``FrameAligner.next()`` 取帧，同栅格返回 ``"skipped"``（降采样）；
- 缓冲区为空返回 ``"no_data"`` 并触发减速；
- 有新栅格时返回 ``"applied"`` 并下发位姿；
- 数据中断返回 ``"interrupted"`` 且不消费对齐器；
- 对齐结果 timestamp/grid_index 透传到感知叠加。

使用桩件替代 CARLA / 坐标转换 / 外推，无需真实引擎。
"""

from __future__ import annotations

import time
import types
from typing import Any, Optional

import pytest

from hunter_sim.common.models import Transform, VehicleState, VILSettings
from hunter_sim.vil_mapper.data_health_monitor import DataHealthStatus
from hunter_sim.vil_mapper.frame_aligner import AlignedFrame
from hunter_sim.vil_mapper.telemetry_buffer import TelemetryBuffer, TelemetryFrame
from hunter_sim.vil_mapper.vil_sync_service import VILSyncService


# ── 桩件 ──────────────────────────────────────────────────────────────────────


class _FakeReport:
    def __init__(self, status: DataHealthStatus) -> None:
        self.status = status


class _FakeHealth:
    def __init__(self, status: DataHealthStatus = DataHealthStatus.OK) -> None:
        self.status = status

    @property
    def current_status(self) -> DataHealthStatus:
        return self.status

    def check(self) -> _FakeReport:
        return _FakeReport(self.status)


class _FakeCoord:
    def odom_to_carla(self, **_kwargs: Any) -> tuple[float, float, float, float, float, float]:
        return (1.0, 2.0, 0.0, 0.0, 0.0, 0.0)


class _FakeExtrapolator:
    def extrapolate_transform(self, transform: Any, _speed: float, _yaw_rate: float) -> Any:
        return transform


class _FakeVehicle:
    """记录下发调用的车辆桩件。"""

    def __init__(self, speed: float = 5.0) -> None:
        self.transforms: list[Any] = []
        self.velocities: list[Any] = []
        self._speed = speed

    def set_transform(self, transform: Any) -> None:
        self.transforms.append(transform)

    def set_velocity(self, vector: Any) -> None:
        self.velocities.append(vector)

    def set_angular_velocity(self, vector: Any) -> None:  # noqa: ARG002
        return None

    def set_wheel_steer_angle(self, wheel_location: Any, steer_angle: float) -> None:  # noqa: ARG002
        return None

    def set_light_state(self, light_state: Any) -> None:  # noqa: ARG002
        return None

    def get_velocity(self) -> Any:
        return types.SimpleNamespace(x=self._speed, y=0.0, z=0.0)

    def get_transform(self) -> Any:
        return types.SimpleNamespace(rotation=types.SimpleNamespace(yaw=0.0))


class _FakeWorld:
    def tick(self) -> int:
        return 1


class _StubAligner:
    """按预设序列返回对齐结果的桩件（可记录被调用次数）。"""

    def __init__(self, results: list[Optional[AlignedFrame]]) -> None:
        self._results = list(results)
        self.calls = 0

    def next(self, _now: float) -> Optional[AlignedFrame]:
        self.calls += 1
        if not self._results:
            return None
        return self._results.pop(0)

    def reset(self) -> None:
        return None


def _frame(ts: float, *, x: float = 0.0) -> TelemetryFrame:
    return TelemetryFrame(
        vehicle_id="veh-1",
        timestamp=ts,
        receive_time=ts,
        vehicle_state=VehicleState(
            time_stamp=ts,
            transform=Transform(x=x, y=0.0, z=0.0),
            velocity=(0.0, 0.0, 0.0),
            vehicle_speed=0.0,
        ),
    )


def _aligned(grid_index: int = 10, ts: Optional[float] = None) -> AlignedFrame:
    stamp = time.time() if ts is None else ts
    return AlignedFrame(
        frame=_frame(stamp),
        mode="interpolated",
        skew_ms=1.0,
        grid_index=grid_index,
    )


def _service(
    *,
    aligner: Optional[_StubAligner] = None,
    buffer: Optional[TelemetryBuffer] = None,
    status: DataHealthStatus = DataHealthStatus.OK,
    vehicle: Optional[_FakeVehicle] = None,
    settings: Optional[VILSettings] = None,
) -> tuple[VILSyncService, _StubAligner, _FakeVehicle]:
    stub = aligner if aligner is not None else _StubAligner([])
    veh = vehicle if vehicle is not None else _FakeVehicle()
    svc = VILSyncService(
        settings=settings or VILSettings(),
        buffer=buffer if buffer is not None else TelemetryBuffer(),
        coord_transformer=_FakeCoord(),  # type: ignore[arg-type]
        extrapolator=_FakeExtrapolator(),  # type: ignore[arg-type]
        health_monitor=_FakeHealth(status),  # type: ignore[arg-type]
        vehicle_actor=veh,  # type: ignore[arg-type]
        world=_FakeWorld(),  # type: ignore[arg-type]
        aligner=stub,  # type: ignore[arg-type]
    )
    return svc, stub, veh


@pytest.fixture(autouse=True)
def fake_carla(monkeypatch: pytest.MonkeyPatch) -> None:
    """注入最小 carla 模块，使 set_transform / set_velocity 可达。"""
    mod = types.ModuleType("carla")
    mod.Transform = lambda location, rotation: types.SimpleNamespace(  # type: ignore[attr-defined]
        location=location, rotation=rotation
    )
    mod.Location = lambda **kwargs: types.SimpleNamespace(**kwargs)  # type: ignore[attr-defined]
    mod.Rotation = lambda **kwargs: types.SimpleNamespace(**kwargs)  # type: ignore[attr-defined]
    mod.Vector3D = lambda **kwargs: types.SimpleNamespace(**kwargs)  # type: ignore[attr-defined]
    mod.WheelLocation = types.SimpleNamespace(FRONT_LEFT=0, FRONT_RIGHT=1)  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "carla", mod)


# ── apply_once 分支 ───────────────────────────────────────────────────────────


class TestApplyOnceBranches:
    def test_empty_buffer_returns_no_data_and_decelerates(self) -> None:
        svc, stub, veh = _service(aligner=_StubAligner([None]))

        assert svc.apply_once() == "no_data"
        assert stub.calls == 1
        # 断流减速：尝试读取速度并下发（carla 桩可用）
        assert len(veh.velocities) == 1

    def test_same_grid_returns_skipped_without_pose_write(self) -> None:
        buffer = TelemetryBuffer()
        buffer.push(_frame(time.time()))
        svc, stub, veh = _service(aligner=_StubAligner([None]), buffer=buffer)

        assert svc.apply_once() == "skipped"
        assert stub.calls == 1
        # 降采样 tick 不得重复写位姿
        assert veh.transforms == []

    def test_new_grid_returns_applied_and_writes_pose(self) -> None:
        svc, _stub, veh = _service(aligner=_StubAligner([_aligned()]))

        assert svc.apply_once() == "applied"
        assert len(veh.transforms) == 1
        assert veh.transforms[0].location.x == pytest.approx(1.0)
        assert veh.transforms[0].location.y == pytest.approx(2.0)

    def test_interrupted_skips_aligner(self) -> None:
        svc, stub, _veh = _service(
            aligner=_StubAligner([_aligned()]),
            status=DataHealthStatus.INTERRUPTED,
        )

        assert svc.apply_once() == "interrupted"
        # 断流优先于对齐：对齐器不被消费，避免恢复时误跳栅格
        assert stub.calls == 0

    def test_do_sync_cycle_raises_on_interrupt(self) -> None:
        from hunter_sim.common.exceptions import SimTimeoutError

        svc, _stub, _veh = _service(
            aligner=_StubAligner([_aligned()]),
            status=DataHealthStatus.INTERRUPTED,
        )
        with pytest.raises(SimTimeoutError):
            svc._do_sync_cycle()

    def test_alternating_outcomes(self) -> None:
        """连续 tick 中 skipped 不累加位姿写入次数。"""
        buffer = TelemetryBuffer()
        buffer.push(_frame(time.time()))
        svc, _stub, veh = _service(
            aligner=_StubAligner([_aligned(grid_index=10), None, _aligned(grid_index=11)]),
            buffer=buffer,
        )

        assert [svc.apply_once() for _ in range(3)] == ["applied", "skipped", "applied"]
        assert len(veh.transforms) == 2


# ── 对齐结果透传 ──────────────────────────────────────────────────────────────


class TestAlignedFramePropagation:
    def test_grid_index_drives_overlay_throttle(self) -> None:
        """grid_index 决定叠加绘制节流（每 3 个栅格一次）。"""
        drawn: list[Any] = []

        class _Overlay:
            def draw_perception_result(self, perception: Any, **_kwargs: Any) -> int:
                drawn.append(perception)
                return 1

        # 11/13 不可被 3 整除 → 节流跳过；12 可被 3 整除 → 绘制一次
        results = [_aligned(grid_index=i) for i in (11, 12, 13)]
        for item in results:
            item.frame.perception = _perception()

        svc, _stub, _veh = _service(aligner=_StubAligner(results))
        svc._overlay = _Overlay()  # type: ignore[assignment]

        for _ in range(3):
            assert svc.apply_once() == "applied"
        assert len(drawn) == 1

    def test_extrapolator_used_for_stale_frame(self) -> None:
        calls: list[tuple[float, float]] = []

        class _RecordingExtrapolator:
            def extrapolate_transform(self, transform: Any, speed: float, yaw_rate: float) -> Any:
                calls.append((speed, yaw_rate))
                return transform

        aligned = _aligned()
        aligned.frame.is_fresh = False
        svc, _stub, _veh = _service(aligner=_StubAligner([aligned]))
        svc._extrap = _RecordingExtrapolator()  # type: ignore[assignment]

        assert svc.apply_once() == "applied"
        assert len(calls) == 1


def _perception() -> Any:
    from hunter_sim.common.models import PerceptionResult

    return PerceptionResult(time_stamp=time.time(), objects=[])


__all__ = ["TestApplyOnceBranches", "TestAlignedFramePropagation"]
