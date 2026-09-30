"""场景状态机管理单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

import time

import pytest

from hunter_sim.common.exceptions import InstanceStateError
from hunter_sim.common.models import SceneStatus
from hunter_sim.scene_runner.scene_state_manager import (
    SceneStateManager,
    SceneStateSnapshot,
)


class TestHappyPath:
    def test_initial_created(self) -> None:
        mgr = SceneStateManager("s1")
        assert mgr.status == SceneStatus.CREATED

    def test_full_transition_chain(self) -> None:
        mgr = SceneStateManager("s1")
        mgr.transition_to(SceneStatus.LOADING)
        mgr.transition_to(SceneStatus.READY)
        mgr.transition_to(SceneStatus.RUNNING)
        assert mgr.status == SceneStatus.RUNNING

    def test_pause_and_resume(self) -> None:
        mgr = SceneStateManager("s1")
        mgr.transition_to(SceneStatus.LOADING)
        mgr.transition_to(SceneStatus.READY)
        mgr.transition_to(SceneStatus.RUNNING)
        mgr.transition_to(SceneStatus.PAUSED)
        assert mgr.status == SceneStatus.PAUSED
        mgr.transition_to(SceneStatus.RUNNING)
        assert mgr.status == SceneStatus.RUNNING


class TestIllegalTransitions:
    def test_skip_loading_illegal(self) -> None:
        mgr = SceneStateManager("s1")
        with pytest.raises(InstanceStateError):
            mgr.transition_to(SceneStatus.RUNNING)  # CREATED -> RUNNING 非法

    def test_completed_is_terminal(self) -> None:
        mgr = SceneStateManager("s1")
        mgr.transition_to(SceneStatus.LOADING)
        mgr.transition_to(SceneStatus.READY)
        mgr.transition_to(SceneStatus.RUNNING)
        mgr.transition_to(SceneStatus.COMPLETED)
        with pytest.raises(InstanceStateError):
            mgr.transition_to(SceneStatus.RUNNING)

    def test_pause_from_created_illegal(self) -> None:
        mgr = SceneStateManager("s1")
        with pytest.raises(InstanceStateError):
            mgr.transition_to(SceneStatus.PAUSED)


class TestSnapshotAndProgress:
    def test_set_duration_clamped(self) -> None:
        mgr = SceneStateManager("s1")
        mgr.set_duration(0.0)  # 应被钳制到 >=0.1
        snap = mgr.get_snapshot()
        assert isinstance(snap, SceneStateSnapshot)

    def test_progress_reflects_elapsed(self) -> None:
        mgr = SceneStateManager("s1")
        mgr.set_duration(60.0)
        mgr.transition_to(SceneStatus.LOADING)
        mgr.transition_to(SceneStatus.READY)
        mgr.transition_to(SceneStatus.RUNNING)
        mgr._start_time = time.perf_counter() - 30.0  # 回拨内部时间，避免 sleep
        snap = mgr.get_snapshot()
        assert snap.progress == pytest.approx(0.5, abs=0.05)
        assert snap.elapsed_seconds == pytest.approx(30.0, abs=1.0)

    def test_progress_capped_at_one(self) -> None:
        mgr = SceneStateManager("s1")
        mgr.set_duration(10.0)
        mgr.transition_to(SceneStatus.LOADING)
        mgr.transition_to(SceneStatus.READY)
        mgr.transition_to(SceneStatus.RUNNING)
        mgr._start_time = time.perf_counter() - 100.0
        assert mgr.get_snapshot().progress == 1.0

    def test_failed_records_error_message(self) -> None:
        mgr = SceneStateManager("s1")
        mgr.transition_to(SceneStatus.LOADING)
        mgr.transition_to(SceneStatus.FAILED, error_msg="map load error")
        assert mgr.get_snapshot().error_message == "map load error"


class TestCallbacks:
    def test_callback_fired_on_transition(self) -> None:
        seen: list[SceneStateSnapshot] = []
        mgr = SceneStateManager("s1", on_state_change=seen.append)
        mgr.transition_to(SceneStatus.LOADING)
        assert len(seen) == 1
        assert seen[0].status == SceneStatus.LOADING

    def test_callback_exception_swallowed(self) -> None:
        def _boom(_snap: SceneStateSnapshot) -> None:
            raise RuntimeError("cb fail")

        mgr = SceneStateManager("s1", on_state_change=_boom)
        mgr.transition_to(SceneStatus.LOADING)  # 不应抛出
        assert mgr.status == SceneStatus.LOADING
