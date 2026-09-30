"""行人控制器封装单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

from typing import Any

import pytest

from hunter_sim.common.exceptions import CarlaSimulationError
from hunter_sim.traffic_sim.walker_controller import WalkerControllerWrapper


class _RecordingController:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.go_calls: list[tuple[Any, float]] = []

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def go_to(self, destination: Any, speed: float) -> None:
        self.go_calls.append((destination, speed))


class _Walker:
    def __init__(self, location: Any = "LOC", raise_on_location: bool = False) -> None:
        self._location = location
        self._raise = raise_on_location
        self.destroyed = False

    def get_location(self) -> Any:
        if self._raise:
            raise RuntimeError("no location")
        return self._location

    def destroy(self) -> None:
        self.destroyed = True


class TestNavigation:
    def test_start_navigation_clamps_speed(self) -> None:
        ctrl = _RecordingController()
        wc = WalkerControllerWrapper(_Walker(), ctrl)
        wc.start_navigation("DEST", speed_ms=5.0)  # 超过上限
        assert ctrl.started is True
        assert ctrl.go_calls[0][1] == pytest.approx(1.4)  # 最大步速

    def test_start_navigation_min_speed(self) -> None:
        ctrl = _RecordingController()
        wc = WalkerControllerWrapper(_Walker(), ctrl)
        wc.start_navigation("DEST", speed_ms=0.01)
        assert ctrl.go_calls[0][1] == pytest.approx(0.1)

    def test_start_error_wrapped(self) -> None:
        class _Boom:
            def start(self) -> None:
                raise RuntimeError("attach failed")

            def go_to(self, d: Any, s: float) -> None:
                pass

        wc = WalkerControllerWrapper(_Walker(), _Boom())
        with pytest.raises(CarlaSimulationError):
            wc.start_navigation("DEST")

    def test_stop_navigation(self) -> None:
        ctrl = _RecordingController()
        wc = WalkerControllerWrapper(_Walker(), ctrl)
        wc.stop_navigation()
        assert ctrl.stopped is True

    def test_set_destination(self) -> None:
        ctrl = _RecordingController()
        wc = WalkerControllerWrapper(_Walker(), ctrl)
        wc.set_destination("D2", speed_ms=1.0)
        assert ctrl.go_calls == [("D2", 1.0)]


class TestLocationAndDestroy:
    def test_get_current_location(self) -> None:
        wc = WalkerControllerWrapper(_Walker(location="HERE"), _RecordingController())
        assert wc.get_current_location() == "HERE"

    def test_get_current_location_error_returns_none(self) -> None:
        wc = WalkerControllerWrapper(
            _Walker(raise_on_location=True), _RecordingController()
        )
        assert wc.get_current_location() is None

    def test_destroy_both_actors(self) -> None:
        walker = _Walker()
        ctrl = _RecordingController()
        # controller 无 destroy 方法，destroy 内部 try/except 应吞掉异常
        wc = WalkerControllerWrapper(walker, ctrl)
        wc.destroy()
        assert walker.destroyed is True
