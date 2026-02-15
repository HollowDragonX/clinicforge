"""TDD tests for the Command Model: Command, Aggregate, and CommandHandler.

Tests the full command handling flow:
1. Command arrives
2. Handler loads aggregate event stream from event store
3. Handler rehydrates aggregate state by replaying events
4. Handler passes command to aggregate
5. Aggregate executes domain logic, enforces invariants
6. Aggregate produces events or rejects the command
7. Handler persists events to event store
8. Handler dispatches events via event dispatcher

Uses a test aggregate (SimpleEncounter) with a minimal state machine:
  created → started → completed
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from clinical_core.domain.events import (
    ConnectionStatus,
    ConcurrencyError,
    DomainEvent,
    EventMetadata,
)
from clinical_core.infrastructure.in_memory_event_store import InMemoryEventStore


# ---------------------------------------------------------------------------
# Test domain: SimpleEncounter aggregate + commands
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StartEncounter:
    """Command: intent to start an encounter."""
    encounter_id: UUID
    patient_id: UUID
    practitioner_id: UUID
    occurred_at: datetime
    performed_by: UUID
    performer_role: str
    organization_id: UUID
    facility_id: UUID
    device_id: str
    connection_status: ConnectionStatus
    correlation_id: UUID


@dataclass(frozen=True)
class CompleteEncounter:
    """Command: intent to complete an active encounter."""
    encounter_id: UUID
    occurred_at: datetime
    performed_by: UUID
    performer_role: str
    organization_id: UUID
    facility_id: UUID
    device_id: str
    connection_status: ConnectionStatus
    correlation_id: UUID


from clinical_core.domain.aggregate import Aggregate, DomainError


class SimpleEncounter(Aggregate):
    """Test aggregate: a minimal encounter with states created → started → completed."""

    @property
    def aggregate_type(self) -> str:
        return "Encounter"

    def initial_state(self) -> dict[str, Any]:
        return {"status": "none", "patient_id": None, "practitioner_id": None}

    def apply_event(self, state: dict[str, Any], event: DomainEvent) -> dict[str, Any]:
        if event.event_type == "test.encounter.Started":
            return {
                **state,
                "status": "started",
                "patient_id": event.payload.get("patient_id"),
                "practitioner_id": event.payload.get("practitioner_id"),
            }
        elif event.event_type == "test.encounter.Completed":
            return {**state, "status": "completed"}
        return state

    def execute(self, state: dict[str, Any], command: Any) -> list[DomainEvent]:
        if isinstance(command, StartEncounter):
            if state["status"] != "none":
                raise DomainError("Encounter already started")
            return [self._build_event(
                command,
                event_type="test.encounter.Started",
                aggregate_id=command.encounter_id,
                payload={
                    "patient_id": str(command.patient_id),
                    "practitioner_id": str(command.practitioner_id),
                },
            )]
        elif isinstance(command, CompleteEncounter):
            if state["status"] != "started":
                raise DomainError("Encounter must be started before completing")
            return [self._build_event(
                command,
                event_type="test.encounter.Completed",
                aggregate_id=command.encounter_id,
                payload={},
            )]
        raise DomainError(f"Unknown command: {type(command).__name__}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_start_command(encounter_id: UUID | None = None) -> StartEncounter:
    return StartEncounter(
        encounter_id=encounter_id or uuid4(),
        patient_id=uuid4(),
        practitioner_id=uuid4(),
        occurred_at=datetime.now(timezone.utc),
        performed_by=uuid4(),
        performer_role="physician",
        organization_id=uuid4(),
        facility_id=uuid4(),
        device_id="device-001",
        connection_status=ConnectionStatus.ONLINE,
        correlation_id=uuid4(),
    )


def _make_complete_command(encounter_id: UUID) -> CompleteEncounter:
    return CompleteEncounter(
        encounter_id=encounter_id,
        occurred_at=datetime.now(timezone.utc),
        performed_by=uuid4(),
        performer_role="physician",
        organization_id=uuid4(),
        facility_id=uuid4(),
        device_id="device-001",
        connection_status=ConnectionStatus.ONLINE,
        correlation_id=uuid4(),
    )


class SpyHandler:
    """Spy that records dispatched events."""
    def __init__(self) -> None:
        self.received: list[DomainEvent] = []

    def __call__(self, event: DomainEvent) -> None:
        self.received.append(event)


# ---------------------------------------------------------------------------
# Tests: Aggregate rehydration
# ---------------------------------------------------------------------------

class TestAggregateRehydration:

    def test_initial_state(self) -> None:
        agg = SimpleEncounter()
        state = agg.initial_state()
        assert state["status"] == "none"

    def test_rehydrate_from_empty_stream(self) -> None:
        agg = SimpleEncounter()
        state = agg.rehydrate([])
        assert state["status"] == "none"

    def test_rehydrate_replays_events(self) -> None:
        agg = SimpleEncounter()
        enc_id = uuid4()

        start_cmd = _make_start_command(encounter_id=enc_id)
        events = agg.execute(agg.initial_state(), start_cmd)
        state = agg.rehydrate(events)

        assert state["status"] == "started"
        assert state["patient_id"] == str(start_cmd.patient_id)

    def test_rehydrate_multiple_events(self) -> None:
        agg = SimpleEncounter()
        enc_id = uuid4()

        start_cmd = _make_start_command(encounter_id=enc_id)
        start_events = agg.execute(agg.initial_state(), start_cmd)

        state_after_start = agg.rehydrate(start_events)
        complete_cmd = _make_complete_command(encounter_id=enc_id)
        complete_events = agg.execute(state_after_start, complete_cmd)

        all_events = start_events + complete_events
        final_state = agg.rehydrate(all_events)
        assert final_state["status"] == "completed"


# ---------------------------------------------------------------------------
# Tests: Aggregate domain logic
# ---------------------------------------------------------------------------

class TestAggregateDomainLogic:

    def test_start_encounter_produces_event(self) -> None:
        agg = SimpleEncounter()
        cmd = _make_start_command()
        events = agg.execute(agg.initial_state(), cmd)

        assert len(events) == 1
        assert events[0].event_type == "test.encounter.Started"

    def test_start_already_started_encounter_raises(self) -> None:
        agg = SimpleEncounter()
        cmd = _make_start_command()
        events = agg.execute(agg.initial_state(), cmd)
        started_state = agg.rehydrate(events)

        with pytest.raises(DomainError, match="already started"):
            agg.execute(started_state, _make_start_command(encounter_id=cmd.encounter_id))

    def test_complete_encounter_produces_event(self) -> None:
        agg = SimpleEncounter()
        enc_id = uuid4()
        start_events = agg.execute(agg.initial_state(), _make_start_command(encounter_id=enc_id))
        started_state = agg.rehydrate(start_events)

        events = agg.execute(started_state, _make_complete_command(encounter_id=enc_id))
        assert len(events) == 1
        assert events[0].event_type == "test.encounter.Completed"

    def test_complete_before_start_raises(self) -> None:
        agg = SimpleEncounter()
        with pytest.raises(DomainError, match="must be started"):
            agg.execute(agg.initial_state(), _make_complete_command(encounter_id=uuid4()))

    def test_complete_already_completed_raises(self) -> None:
        agg = SimpleEncounter()
        enc_id = uuid4()
        start_events = agg.execute(agg.initial_state(), _make_start_command(encounter_id=enc_id))
        complete_events = agg.execute(agg.rehydrate(start_events), _make_complete_command(encounter_id=enc_id))
        final_state = agg.rehydrate(start_events + complete_events)

        with pytest.raises(DomainError, match="must be started"):
            agg.execute(final_state, _make_complete_command(encounter_id=enc_id))


# ---------------------------------------------------------------------------
# Tests: CommandHandler full flow
# ---------------------------------------------------------------------------

class TestCommandHandlerFlow:

    def test_handle_persists_event_to_store(self) -> None:
        from clinical_core.application.command_handler import CommandHandler
        from clinical_core.application.event_dispatcher import EventDispatcher

        store = InMemoryEventStore()
        dispatcher = EventDispatcher()
        agg = SimpleEncounter()
        handler = CommandHandler(event_store=store, dispatcher=dispatcher, aggregate=agg)

        enc_id = uuid4()
        cmd = _make_start_command(encounter_id=enc_id)

        result = handler.handle(cmd, aggregate_id=enc_id)

        assert len(result) == 1
        assert result[0].event_type == "test.encounter.Started"
        assert store.stream_version(enc_id) == 1

    def test_handle_dispatches_event(self) -> None:
        from clinical_core.application.command_handler import CommandHandler
        from clinical_core.application.event_dispatcher import EventDispatcher

        store = InMemoryEventStore()
        dispatcher = EventDispatcher()
        spy = SpyHandler()
        dispatcher.subscribe("test.encounter.Started", spy)

        agg = SimpleEncounter()
        handler = CommandHandler(event_store=store, dispatcher=dispatcher, aggregate=agg)

        enc_id = uuid4()
        handler.handle(_make_start_command(encounter_id=enc_id), aggregate_id=enc_id)

        assert len(spy.received) == 1
        assert spy.received[0].event_type == "test.encounter.Started"

    def test_handle_rehydrates_before_execute(self) -> None:
        """Second command should see state from the first command's event."""
        from clinical_core.application.command_handler import CommandHandler
        from clinical_core.application.event_dispatcher import EventDispatcher

        store = InMemoryEventStore()
        dispatcher = EventDispatcher()
        agg = SimpleEncounter()
        handler = CommandHandler(event_store=store, dispatcher=dispatcher, aggregate=agg)

        enc_id = uuid4()
        handler.handle(_make_start_command(encounter_id=enc_id), aggregate_id=enc_id)
        result = handler.handle(_make_complete_command(encounter_id=enc_id), aggregate_id=enc_id)

        assert result[0].event_type == "test.encounter.Completed"
        assert store.stream_version(enc_id) == 2

    def test_handle_rejects_invalid_command(self) -> None:
        from clinical_core.application.command_handler import CommandHandler
        from clinical_core.application.event_dispatcher import EventDispatcher

        store = InMemoryEventStore()
        dispatcher = EventDispatcher()
        agg = SimpleEncounter()
        handler = CommandHandler(event_store=store, dispatcher=dispatcher, aggregate=agg)

        enc_id = uuid4()
        with pytest.raises(DomainError, match="must be started"):
            handler.handle(_make_complete_command(encounter_id=enc_id), aggregate_id=enc_id)

        # Nothing persisted
        assert store.stream_version(enc_id) == 0

    def test_handle_sets_correct_aggregate_version(self) -> None:
        from clinical_core.application.command_handler import CommandHandler
        from clinical_core.application.event_dispatcher import EventDispatcher

        store = InMemoryEventStore()
        dispatcher = EventDispatcher()
        agg = SimpleEncounter()
        handler = CommandHandler(event_store=store, dispatcher=dispatcher, aggregate=agg)

        enc_id = uuid4()
        result1 = handler.handle(_make_start_command(encounter_id=enc_id), aggregate_id=enc_id)
        result2 = handler.handle(_make_complete_command(encounter_id=enc_id), aggregate_id=enc_id)

        assert result1[0].aggregate_version == 1
        assert result2[0].aggregate_version == 2

    def test_handle_sets_aggregate_type(self) -> None:
        from clinical_core.application.command_handler import CommandHandler
        from clinical_core.application.event_dispatcher import EventDispatcher

        store = InMemoryEventStore()
        dispatcher = EventDispatcher()
        agg = SimpleEncounter()
        handler = CommandHandler(event_store=store, dispatcher=dispatcher, aggregate=agg)

        enc_id = uuid4()
        result = handler.handle(_make_start_command(encounter_id=enc_id), aggregate_id=enc_id)

        assert result[0].aggregate_type == "Encounter"

    def test_rejected_command_does_not_dispatch(self) -> None:
        from clinical_core.application.command_handler import CommandHandler
        from clinical_core.application.event_dispatcher import EventDispatcher

        store = InMemoryEventStore()
        dispatcher = EventDispatcher()
        spy = SpyHandler()
        dispatcher.subscribe("test.encounter.Completed", spy)

        agg = SimpleEncounter()
        handler = CommandHandler(event_store=store, dispatcher=dispatcher, aggregate=agg)

        enc_id = uuid4()
        with pytest.raises(DomainError):
            handler.handle(_make_complete_command(encounter_id=enc_id), aggregate_id=enc_id)

        assert len(spy.received) == 0

    def test_new_aggregate_starts_from_initial_state(self) -> None:
        """A command on a fresh aggregate_id should work (empty stream → initial state)."""
        from clinical_core.application.command_handler import CommandHandler
        from clinical_core.application.event_dispatcher import EventDispatcher

        store = InMemoryEventStore()
        dispatcher = EventDispatcher()
        agg = SimpleEncounter()
        handler = CommandHandler(event_store=store, dispatcher=dispatcher, aggregate=agg)

        enc_id = uuid4()
        result = handler.handle(_make_start_command(encounter_id=enc_id), aggregate_id=enc_id)

        assert len(result) == 1
        stream = store.read_stream(enc_id)
        assert len(stream) == 1

    def test_events_have_unique_event_ids(self) -> None:
        from clinical_core.application.command_handler import CommandHandler
        from clinical_core.application.event_dispatcher import EventDispatcher

        store = InMemoryEventStore()
        dispatcher = EventDispatcher()
        agg = SimpleEncounter()
        handler = CommandHandler(event_store=store, dispatcher=dispatcher, aggregate=agg)

        enc_id = uuid4()
        r1 = handler.handle(_make_start_command(encounter_id=enc_id), aggregate_id=enc_id)
        r2 = handler.handle(_make_complete_command(encounter_id=enc_id), aggregate_id=enc_id)

        assert r1[0].event_id != r2[0].event_id
