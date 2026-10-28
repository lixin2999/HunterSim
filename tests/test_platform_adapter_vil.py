"""VIL 运行时测试（延迟绑定、会话切换清缓存、数据中断状态覆盖）。

不连接 Kafka/CARLA：消费线程与同步服务均用替身注入。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from hunter_sim.common.models import HunterSimSettings
from hunter_sim.platform_adapter import vil as vil_module
from hunter_sim.platform_adapter.vil import VILRuntime


class _FakeRunner:
    """最小运行器视图（只暴露 VIL 绑定所需属性）。"""

    def __init__(self) -> None:
        self.ego_vehicle = SimpleNamespace(name="ego-1")
        self.world = SimpleNamespace(name="world-1")


class _FakeSyncService:
    """假同步服务（记录构造参数，返回可控结果）。"""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.calls = 0
        self.outcome: Any = "applied"

    def apply_once(self) -> str:
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return str(self.outcome)


class _FakeConsumer:
    """假 Kafka 消费者（只关心 start/stop 幂等）。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self.started = 0
        self.stopped: list[float] = []
        self.resets = 0
        self.fail_stop = False

    def start(self) -> None:
        self.started += 1

    def reset_ordering_guard(self) -> None:
        self.resets += 1

    def stop(self, timeout_s: float = 3.0) -> None:
        self.stopped.append(timeout_s)
        if self.fail_stop:
            raise RuntimeError("consumer stop failed")


@pytest.fixture
def services(monkeypatch: pytest.MonkeyPatch) -> list[_FakeSyncService]:
    created: list[_FakeSyncService] = []

    def factory(**kwargs: Any) -> _FakeSyncService:
        service = _FakeSyncService(**kwargs)
        created.append(service)
        return service

    monkeypatch.setattr(vil_module, "VILSyncService", factory)
    return created


@pytest.fixture
def consumers(monkeypatch: pytest.MonkeyPatch) -> list[_FakeConsumer]:
    created: list[_FakeConsumer] = []

    def factory(*args: Any, **kwargs: Any) -> _FakeConsumer:
        consumer = _FakeConsumer(*args, **kwargs)
        created.append(consumer)
        return consumer

    monkeypatch.setattr(vil_module, "VILDataConsumer", factory)
    return created


def _runtime(
    settings: HunterSimSettings,
    runner: _FakeRunner | None,
    *,
    vil_config: dict[str, Any] | None = None,
) -> VILRuntime:
    return VILRuntime(
        settings,
        sim_id="sim-1",
        vehicle_id="vehicle-1",
        vil_config=vil_config,
        runner_provider=lambda: runner,
    )


class TestConfiguration:
    def test_vil_overrides_reach_runtime(
        self, test_settings: HunterSimSettings
    ) -> None:
        runtime = _runtime(
            test_settings,
            None,
            vil_config={"extrapolation_ms": 250, "data_timeout_ms": 900},
        )
        stats = runtime.stats
        assert stats["extrapolation_ms"] == 250
        assert stats["max_data_latency_ms"] == 900
        assert stats["steps"] == 0
        assert stats["applied"] == 0
        assert stats["interrupted"] is False

    def test_overrides_are_clamped(self, test_settings: HunterSimSettings) -> None:
        runtime = _runtime(
            test_settings,
            None,
            vil_config={"extrapolation_ms": 99999, "data_timeout_ms": 1},
        )
        stats = runtime.stats
        assert stats["extrapolation_ms"] == 1000
        assert stats["max_data_latency_ms"] == 100


class TestStep:
    def test_step_binds_lazily_and_applies(
        self,
        test_settings: HunterSimSettings,
        services: list[_FakeSyncService],
    ) -> None:
        runner = _FakeRunner()
        runtime = _runtime(test_settings, runner)

        runtime.step(0.05)

        assert len(services) == 1
        assert services[0].kwargs["vehicle_actor"] is runner.ego_vehicle
        assert services[0].kwargs["world"] is runner.world
        assert runtime.stats["steps"] == 1
        assert runtime.stats["applied"] == 1
        assert runtime.interrupted is False
        assert runtime.status_override() is None

    def test_step_reuses_service_for_same_vehicle(
        self,
        test_settings: HunterSimSettings,
        services: list[_FakeSyncService],
    ) -> None:
        runtime = _runtime(test_settings, _FakeRunner())
        runtime.step(0.05)
        runtime.step(0.05)
        assert len(services) == 1
        assert services[0].calls == 2

    def test_vehicle_switch_rebinds_and_clears_buffer(
        self,
        test_settings: HunterSimSettings,
        services: list[_FakeSyncService],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner = _FakeRunner()
        runtime = _runtime(test_settings, runner)
        clears: list[int] = []
        monkeypatch.setattr(runtime._buffer, "clear", lambda: clears.append(1))  # noqa: SLF001

        runtime.step(0.05)
        assert len(clears) == 1
        runner.ego_vehicle = SimpleNamespace(name="ego-2")
        runtime.step(0.05)

        assert len(services) == 2
        assert len(clears) == 2
        assert services[1].kwargs["vehicle_actor"] is runner.ego_vehicle

    def test_vehicle_switch_resets_consumer_ordering_guard(
        self,
        test_settings: HunterSimSettings,
        services: list[_FakeSyncService],
        consumers: list[_FakeConsumer],
    ) -> None:
        runner = _FakeRunner()
        runtime = _runtime(test_settings, runner)
        runtime.start()

        runtime.step(0.05)
        runner.ego_vehicle = SimpleNamespace(name="ego-2")
        runtime.step(0.05)

        assert len(services) == 2
        assert consumers[0].resets == 2  # 首次绑定 + 会话切换各清空一次

    def test_interrupted_outcome_overrides_status(
        self,
        test_settings: HunterSimSettings,
        services: list[_FakeSyncService],
    ) -> None:
        runtime = _runtime(test_settings, _FakeRunner())
        runtime.step(0.05)
        services[0].outcome = "interrupted"

        runtime.step(0.05)

        assert runtime.interrupted is True
        assert runtime.status_override() == ("PAUSED", "vil_data_interrupted")
        assert runtime.stats["applied"] == 1

    def test_no_data_outcome_is_not_interrupted(
        self,
        test_settings: HunterSimSettings,
        services: list[_FakeSyncService],
    ) -> None:
        runtime = _runtime(test_settings, _FakeRunner())
        runtime.step(0.05)
        services[0].outcome = "no_data"

        runtime.step(0.05)

        assert runtime.interrupted is False
        assert runtime.status_override() is None

    def test_step_swallows_service_errors(
        self,
        test_settings: HunterSimSettings,
        services: list[_FakeSyncService],
    ) -> None:
        runtime = _runtime(test_settings, _FakeRunner())
        runtime.step(0.05)
        services[0].outcome = RuntimeError("boom")

        runtime.step(0.05)

        assert runtime.stats["steps"] == 2
        assert runtime.stats["applied"] == 1
        assert runtime.interrupted is False

    def test_step_without_runner_is_noop(
        self, test_settings: HunterSimSettings, services: list[_FakeSyncService]
    ) -> None:
        runtime = _runtime(test_settings, None)
        runtime.step(0.05)
        assert services == []
        assert runtime.stats["steps"] == 1
        assert runtime.status_override() is None


class TestLifecycle:
    def test_start_and_stop_are_idempotent(
        self,
        test_settings: HunterSimSettings,
        consumers: list[_FakeConsumer],
    ) -> None:
        runtime = _runtime(test_settings, _FakeRunner())
        runtime.start()
        runtime.start()
        assert len(consumers) == 1
        assert consumers[0].started == 1
        assert consumers[0].kwargs["sim_id"] == "sim-1"

        runtime.stop()
        runtime.stop()
        assert consumers[0].stopped == [3.0]

    def test_stop_failure_is_swallowed(
        self,
        test_settings: HunterSimSettings,
        consumers: list[_FakeConsumer],
    ) -> None:
        runtime = _runtime(test_settings, _FakeRunner())
        runtime.start()
        consumers[0].fail_stop = True

        runtime.stop(timeout_s=1.0)  # 不抛异常

        assert consumers[0].stopped == [1.0]
