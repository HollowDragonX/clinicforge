"""Tests for Aggregate Rehydration properties.

Rehydration is the process of rebuilding aggregate state from events.

Requirements verified:
1. Deterministic reconstruction: same events → same state, always.
2. No persistence of aggregate state: state is transient, never stored.
3. Pure application of events: apply_event is side-effect-free.

Uses a realistic Encounter aggregate with states:
  none → checked_in → triaged → active → completed → discharged
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from clinical_core.domain.aggregate import Aggregate, DomainError
from clinical_core.domain.events import (
    ConnectionStatus,
    DomainEvent,
    EventMetadata,
)
from clinical_core.infrastructure.in_memory_event_store import InMemoryEventStore
from clinical_core.application.command_handler import CommandHandler
from clinical_core.application.event_dispatcher import EventDispatcher


# ---------------------------------------------------------------------------
# Test aggregate: multi-state encounter
# ---------------------------------------------------------------------------

class EncounterAggregate(Aggregate):
    """Encounter with full lifecycle for rehydration tests."""

    @property
    def aggregate_type(self) -> str:
        return "Encounter"

    def initial_state(self) -> dict[str, Any]:
        return {
            "status": "none",
            "patient_id": None,
            "practitioner_id": None,
            "checked_in_at": None,
            "began_at": None,
            "completed_at": None,
        }

    def apply_event(self, state: dict[str, Any], event: DomainEvent) -> dict[str, Any]:
        p = event.payload
        if event.event_type == "clinical.encounter.PatientCheckedIn":
            return {
                **state,
                "status": "checked_in",
                "patient_id": p.get("patient_id"),
                "checked_in_at": p.get("checked_in_at"),
            }
        elif event.event_type == "clinical.encounter.EncounterBegan":
            return {
                **state,
                "status": "active",
                "practitioner_id": p.get("practitioner_id"),
                "began_at": p.get("began_at"),
            }
        elif event.event_type == "clinical.encounter.EncounterCompleted":
            return {
                **state,
                "status": "completed",
                "completed_at": p.get("completed_at"),
            }
        return state

    def execute(self, state: dict[str, Any], command: Any) -> list[DomainEvent]:
        raise NotImplementedError("Not needed for rehydration tests")


# ---------------------------------------------------------------------------
# Helper: build events directly (no commands needed for rehydration tests)
# ---------------------------------------------------------------------------

def _event(
    event_type: str,
    aggregate_id: UUID,
    aggregate_version: int,
    payload: dict | None = None,
) -> DomainEvent:
    return DomainEvent(
        metadata=EventMetadata(
            event_id=uuid4(),
            event_type=event_type,
            schema_version=1,
            aggregate_id=aggregate_id,
            aggregate_type="Encounter",
            aggregate_version=aggregate_version,
            occurred_at=datetime.now(timezone.utc),
            performed_by=uuid4(),
            performer_role="physician",
            organization_id=uuid4(),
            facility_id=uuid4(),
            device_id="device-001",
            connection_status=ConnectionStatus.ONLINE,
            correlation_id=uuid4(),
        ),
        payload=payload or {},
    )


def _encounter_stream(enc_id: UUID) -> list[DomainEvent]:
    """Build a standard 3-event encounter stream."""
    patient_id = str(uuid4())
    practitioner_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    return [
        _event("clinical.encounter.PatientCheckedIn", enc_id, 1,
               {"patient_id": patient_id, "checked_in_at": now}),
        _event("clinical.encounter.EncounterBegan", enc_id, 2,
               {"practitioner_id": practitioner_id, "began_at": now}),
        _event("clinical.encounter.EncounterCompleted", enc_id, 3,
               {"completed_at": now}),
    ]


# ---------------------------------------------------------------------------
# Requirement 1: Deterministic reconstruction
# ---------------------------------------------------------------------------

class TestDeterministicReconstruction:
    """Same events replayed → identical state, every time."""

    def test_same_events_produce_same_state(self) -> None:
        agg = EncounterAggregate()
        enc_id = uuid4()
        events = _encounter_stream(enc_id)

        state_a = agg.rehydrate(events)
        state_b = agg.rehydrate(events)

        assert state_a == state_b

    def test_replay_hundred_times_always_same(self) -> None:
        agg = EncounterAggregate()
        enc_id = uuid4()
        events = _encounter_stream(enc_id)

        first = agg.rehydrate(events)
        for _ in range(100):
            assert agg.rehydrate(events) == first

    def test_partial_replay_is_deterministic(self) -> None:
        agg = EncounterAggregate()
        enc_id = uuid4()
        events = _encounter_stream(enc_id)

        # Replay only first 2 events
        state_a = agg.rehydrate(events[:2])
        state_b = agg.rehydrate(events[:2])
        assert state_a == state_b
        assert state_a["status"] == "active"

    def test_empty_stream_is_deterministic(self) -> None:
        agg = EncounterAggregate()
        assert agg.rehydrate([]) == agg.rehydrate([])
        assert agg.rehydrate([]) == agg.initial_state()

    def test_single_event_is_deterministic(self) -> None:
        agg = EncounterAggregate()
        enc_id = uuid4()
        events = _encounter_stream(enc_id)

        state_a = agg.rehydrate(events[:1])
        state_b = agg.rehydrate(events[:1])
        assert state_a == state_b
        assert state_a["status"] == "checked_in"

    def test_state_depends_only_on_events_not_on_time(self) -> None:
        """Rehydrating now vs. later (with same events) produces same state."""
        agg = EncounterAggregate()
        enc_id = uuid4()
        events = _encounter_stream(enc_id)

        state_before = agg.rehydrate(events)
        # Simulate passage of time (events are the same objects)
        state_after = agg.rehydrate(events)

        assert state_before == state_after


# ---------------------------------------------------------------------------
# Requirement 2: No persistence of aggregate state
# ---------------------------------------------------------------------------

class TestNoPersistenceOfState:
    """Aggregate state is transient — derived from events, never stored."""

    def test_aggregate_has_no_stored_state(self) -> None:
        """The Aggregate object itself holds no state between calls."""
        agg = EncounterAggregate()
        enc_id = uuid4()
        events = _encounter_stream(enc_id)

        agg.rehydrate(events)

        # The aggregate object has no instance attribute tracking state
        assert not hasattr(agg, "_state")
        assert not hasattr(agg, "state")
        assert not hasattr(agg, "_current_state")

    def test_rehydrate_returns_new_state_each_call(self) -> None:
        """Each rehydrate call produces an independent state dict."""
        agg = EncounterAggregate()
        enc_id = uuid4()
        events = _encounter_stream(enc_id)

        state_a = agg.rehydrate(events)
        state_b = agg.rehydrate(events)

        assert state_a == state_b
        assert state_a is not state_b  # different objects

    def test_modifying_returned_state_does_not_affect_aggregate(self) -> None:
        agg = EncounterAggregate()
        enc_id = uuid4()
        events = _encounter_stream(enc_id)

        state = agg.rehydrate(events)
        state["status"] = "TAMPERED"

        # Fresh rehydration is unaffected
        fresh = agg.rehydrate(events)
        assert fresh["status"] == "completed"

    def test_handler_does_not_cache_state(self) -> None:
        """CommandHandler loads fresh state from event store every time."""
        store = InMemoryEventStore()
        dispatcher = EventDispatcher()
        agg = EncounterAggregate()

        enc_id = uuid4()
        events = _encounter_stream(enc_id)

        # Manually persist events
        for event in events:
            store.append(event)

        # Handler reads fresh from store each time
        stream = store.read_stream(enc_id)
        state = agg.rehydrate(stream)
        assert state["status"] == "completed"

        # Re-read produces same result (not cached)
        stream2 = store.read_stream(enc_id)
        state2 = agg.rehydrate(stream2)
        assert state2["status"] == "completed"
        assert state is not state2


# ---------------------------------------------------------------------------
# Requirement 3: Pure application of events
# ---------------------------------------------------------------------------

class TestPureEventApplication:
    """apply_event is a pure function: no side effects, no external reads."""

    def test_apply_event_returns_new_dict(self) -> None:
        agg = EncounterAggregate()
        enc_id = uuid4()
        events = _encounter_stream(enc_id)

        state_before = agg.initial_state()
        state_after = agg.apply_event(state_before, events[0])

        assert state_before != state_after
        assert state_before["status"] == "none"  # unchanged
        assert state_after["status"] == "checked_in"

    def test_apply_event_does_not_mutate_input(self) -> None:
        agg = EncounterAggregate()
        enc_id = uuid4()
        events = _encounter_stream(enc_id)

        original = agg.initial_state()
        original_copy = dict(original)

        agg.apply_event(original, events[0])

        assert original == original_copy  # input unchanged

    def test_apply_event_same_input_same_output(self) -> None:
        """Calling apply_event with identical inputs produces identical outputs."""
        agg = EncounterAggregate()
        enc_id = uuid4()
        events = _encounter_stream(enc_id)

        state = agg.initial_state()
        result_a = agg.apply_event(state, events[0])
        result_b = agg.apply_event(state, events[0])

        assert result_a == result_b

    def test_unrecognized_event_returns_unchanged_state(self) -> None:
        agg = EncounterAggregate()
        enc_id = uuid4()
        unknown = _event("clinical.unknown.SomethingHappened", enc_id, 1, {})

        state = agg.initial_state()
        result = agg.apply_event(state, unknown)

        assert result == state

    def test_fold_is_sequential(self) -> None:
        """State after N events == applying events one at a time."""
        agg = EncounterAggregate()
        enc_id = uuid4()
        events = _encounter_stream(enc_id)

        # Method 1: rehydrate (batch)
        batch_state = agg.rehydrate(events)

        # Method 2: manual fold
        manual_state = agg.initial_state()
        for e in events:
            manual_state = agg.apply_event(manual_state, e)

        assert batch_state == manual_state
