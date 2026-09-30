"""数据回放引擎单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from hunter_sim.common.exceptions import ConfigurationError
from hunter_sim.replay_service import replay_engine as re_mod
from hunter_sim.replay_service.replay_engine import (
    ReplayConfig,
    ReplayEngine,
    ReplayStatus,
)


def _traj(n: int, t0: float = 100.0) -> list[dict[str, Any]]:
    return [
        {"timestamp": t0 + i, "position": {"x": i, "y": 0, "z": 0}, "rotation": {"yaw": 0}}
        for i in range(n)
    ]


def _engine(**overrides: Any) -> ReplayEngine:
    cfg = ReplayConfig(replay_id="r1", **overrides)
    return ReplayEngine(cfg, world=object(), vehicle_actor=object())


class TestLifecycle:
    def test_initial_idle(self) -> None:
        assert _engine().status == ReplayStatus.IDLE

    def test_load_sorts_and_sets_loading(self) -> None:
        eng = _engine()
        eng.load_trajectory([{"timestamp": 3}, {"timestamp": 1}, {"timestamp": 2}])
        assert eng.status == ReplayStatus.LOADING
        assert eng._trajectory[0]["timestamp"] == 1
        assert eng._current_index == 0

    def test_play_without_trajectory_raises(self) -> None:
        with pytest.raises(ConfigurationError):
            _engine().play()

    def test_get_state_progress(self) -> None:
        eng = _engine()
        eng.load_trajectory(_traj(4, t0=100.0))
        eng._current_index = 2
        s = eng.get_state()
        assert s.total_frames == 4
        assert s.progress == pytest.approx(0.5)
        assert s.current_timestamp == 102.0
        assert s.current_frame == 2

    def test_get_state_empty(self) -> None:
        s = _engine(start_timestamp=55.0).get_state()
        assert s.total_frames == 0
        assert s.progress == 0.0
        assert s.current_timestamp == 55.0


class TestTransport:
    def test_pause_resume(self) -> None:
        eng = _engine()
        eng.load_trajectory(_traj(2))
        eng._status = ReplayStatus.PLAYING
        eng.pause()
        assert eng.status == ReplayStatus.PAUSED
        eng.resume()
        assert eng.status == ReplayStatus.PLAYING

    def test_pause_only_when_playing(self) -> None:
        eng = _engine()
        eng.load_trajectory(_traj(2))
        eng.pause()  # 当前 LOADING，应无变化
        assert eng.status == ReplayStatus.LOADING

    def test_set_time_factor_valid(self) -> None:
        eng = _engine()
        eng.set_time_factor(4.0)
        assert eng._config.time_factor == 4.0

    def test_set_time_factor_invalid(self) -> None:
        with pytest.raises(ConfigurationError):
            _engine().set_time_factor(3.0)

    def test_step_forward(self) -> None:
        eng = _engine()
        eng.load_trajectory(_traj(3))
        eng.step_forward()
        assert eng._current_index == 1

    def test_seek_to_timestamp(self) -> None:
        eng = _engine()
        eng.load_trajectory(_traj(5, t0=100.0))
        eng.seek(102.5)
        assert eng._current_index == 3  # 第一个 >=102.5 是 103(idx3)

    def test_seek_beyond_end(self) -> None:
        eng = _engine()
        eng.load_trajectory(_traj(5, t0=100.0))
        eng.seek(9999.0)
        assert eng._current_index == 4

    def test_stop_sets_idle(self) -> None:
        eng = _engine()
        eng._status = ReplayStatus.PLAYING
        eng.stop()
        assert eng.status == ReplayStatus.IDLE


class TestPlayThread:
    def test_play_launches_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        started: list[bool] = []

        class _FakeThread:
            def __init__(self, target: Any = None, name: str = "", daemon: bool = False) -> None:
                self._target = target

            def start(self) -> None:
                started.append(True)

            def is_alive(self) -> bool:
                return False

            def join(self, timeout: float | None = None) -> None:
                pass

        class _Shim:
            Thread = _FakeThread
            Lock = threading.Lock
            Event = threading.Event

        monkeypatch.setattr(re_mod, "threading", _Shim)  # type: ignore[attr-defined]
        eng = _engine()
        eng.load_trajectory(_traj(3))
        eng.play()
        assert eng.status == ReplayStatus.PLAYING
        assert started == [True]

    def test_play_loop_completes(self) -> None:
        eng = _engine(time_factor=8.0)  # 极小 sleep 间隔
        eng.load_trajectory(_traj(3))
        eng._status = ReplayStatus.PLAYING
        eng._play_loop()  # 直接驱动循环（不启线程），跑完返回 COMPLETED
        assert eng.status == ReplayStatus.COMPLETED
        assert eng._current_index == 3
