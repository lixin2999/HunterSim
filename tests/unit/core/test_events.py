"""core.events / core.event_bus / core.protocols 单元测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hunter_sim.core.event_bus import InMemoryEventBus
from hunter_sim.core.events import (
    CollisionEvent,
    Event,
    SimulationTickEvent,
)
from hunter_sim.core.protocols import EventBus


def test_event_is_immutable() -> None:
    ev = SimulationTickEvent(sequence=1, timestamp=0.05, tick=1, delta_seconds=0.05)
    with pytest.raises(ValidationError):
        ev.timestamp = 9.0  # type: ignore[misc]


def test_bus_satisfies_protocol() -> None:
    bus = InMemoryEventBus()
    assert isinstance(bus, EventBus)


def test_publish_dispatches_to_exact_type() -> None:
    bus = InMemoryEventBus()
    received: list[SimulationTickEvent] = []
    bus.subscribe(SimulationTickEvent, received.append)
    bus.publish(SimulationTickEvent(sequence=0, timestamp=0.0, tick=0, delta_seconds=0.05))
    assert len(received) == 1


def test_base_type_subscription_receives_subclasses() -> None:
    bus = InMemoryEventBus()
    seen: list[Event] = []
    bus.subscribe(Event, seen.append)
    bus.publish(SimulationTickEvent(sequence=0, timestamp=0.0, tick=0, delta_seconds=0.05))
    bus.publish(
        CollisionEvent(
            sequence=1,
            timestamp=1.0,
            other_actor_id=42,
            impulse=100.0,
            location_x=0.0,
            location_y=0.0,
            location_z=0.0,
        )
    )
    assert len(seen) == 2


def test_unsubscribe_stops_delivery() -> None:
    bus = InMemoryEventBus()
    received: list[Event] = []
    unsubscribe = bus.subscribe(SimulationTickEvent, received.append)
    bus.publish(SimulationTickEvent(sequence=0, timestamp=0.0, tick=0, delta_seconds=0.05))
    unsubscribe()
    bus.publish(SimulationTickEvent(sequence=1, timestamp=0.1, tick=1, delta_seconds=0.05))
    assert len(received) == 1


def test_callback_exception_is_isolated() -> None:
    bus = InMemoryEventBus()
    reached_second = {"ok": False}

    def _boom(_: Event) -> None:
        raise RuntimeError("callback failed")

    def _second(_: Event) -> None:
        reached_second["ok"] = True

    bus.subscribe(SimulationTickEvent, _boom)
    bus.subscribe(SimulationTickEvent, _second)
    # publish 不应抛出，即使某个回调失败
    bus.publish(SimulationTickEvent(sequence=0, timestamp=0.0, tick=0, delta_seconds=0.05))
    assert reached_second["ok"] is True


def test_clear_removes_subscribers() -> None:
    bus = InMemoryEventBus()
    received: list[Event] = []
    bus.subscribe(SimulationTickEvent, received.append)
    bus.clear()
    bus.publish(SimulationTickEvent(sequence=0, timestamp=0.0, tick=0, delta_seconds=0.05))
    assert received == []
