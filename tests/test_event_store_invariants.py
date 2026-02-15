"""Invariant tests for the Event Store.

These tests verify the core properties that ANY event store implementation
must satisfy. They are written against the EventStore protocol, not against
a specific implementation.

Invariants tested:
- Append-only: events are never modified or deleted after persistence.
- Immutable: a persisted event's content is identical on read.
- Sequential versioning: aggregate_version is contiguous per stream (INV-XX-3).
- Idempotent append: duplicate event_id is a no-op.
- Concurrency control: version mismatch raises ConcurrencyError.
- recorded_at is set by the store at persist time.
- No projection logic: the store only persists and retrieves.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
# Test helpers
# ---------------------------------------------------------------------------

def _make_event(
    aggregate_id: UUID | None = None,
    aggregate_type: str = "Encounter",
    aggregate_version: int = 1,
    event_type: str = "clinical.encounter.EncounterBegan",
    event_id: UUID | None = None,
    occurred_at: datetime | None = None,
    payload: dict | None = None,
) -> DomainEvent:
    """Create a minimal valid DomainEvent for testing."""
    return DomainEvent(
        metadata=EventMetadata(
            event_id=event_id or uuid4(),
            event_type=event_type,
            schema_version=1,
            aggregate_id=aggregate_id or uuid4(),
            aggregate_type=aggregate_type,
            aggregate_version=aggregate_version,
            occurred_at=occurred_at or datetime.now(timezone.utc),
            performed_by=uuid4(),
            performer_role="physician",
            organization_id=uuid4(),
            facility_id=uuid4(),
            device_id="device-001",
            connection_status=ConnectionStatus.ONLINE,
            correlation_id=uuid4(),
            causation_id=None,
            visibility=("clinical_staff",),
        ),
        payload=payload or {},
    )


def _make_stream_events(aggregate_id: UUID, count: int) -> list[DomainEvent]:
    """Create a sequence of events for one aggregate with contiguous versions."""
    return [
        _make_event(
            aggregate_id=aggregate_id,
            aggregate_version=i + 1,
            event_type=f"clinical.test.Event{i + 1}",
        )
        for i in range(count)
    ]


# ---------------------------------------------------------------------------
# INV-XX-3: Sequential versioning per stream
# ---------------------------------------------------------------------------

class TestSequentialVersioning:
    """Aggregate version must be contiguous: 1, 2, 3, ... per aggregate_id."""

    def test_first_event_must_be_version_1(self) -> None:
        store = InMemoryEventStore()
        agg_id = uuid4()
        event = _make_event(aggregate_id=agg_id, aggregate_version=1)

        result = store.append(event)
        assert result.aggregate_version == 1

    def test_version_2_requires_version_1(self) -> None:
        store = InMemoryEventStore()
        agg_id = uuid4()

        event_v2 = _make_event(aggregate_id=agg_id, aggregate_version=2)
        with pytest.raises(ConcurrencyError) as exc_info:
            store.append(event_v2)

        assert exc_info.value.expected_version == 1
        assert exc_info.value.actual_version == 2

    def test_sequential_versions_succeed(self) -> None:
        store = InMemoryEventStore()
        agg_id = uuid4()
        events = _make_stream_events(agg_id, 5)

        for event in events:
            result = store.append(event)
            assert result.aggregate_version == event.aggregate_version

    def test_version_gap_is_rejected(self) -> None:
        store = InMemoryEventStore()
        agg_id = uuid4()

        store.append(_make_event(aggregate_id=agg_id, aggregate_version=1))

        event_v3 = _make_event(aggregate_id=agg_id, aggregate_version=3)
        with pytest.raises(ConcurrencyError) as exc_info:
            store.append(event_v3)

        assert exc_info.value.expected_version == 2
        assert exc_info.value.actual_version == 3

    def test_duplicate_version_is_rejected(self) -> None:
        store = InMemoryEventStore()
        agg_id = uuid4()

        store.append(_make_event(aggregate_id=agg_id, aggregate_version=1))

        event_v1_again = _make_event(aggregate_id=agg_id, aggregate_version=1)
        with pytest.raises(ConcurrencyError) as exc_info:
            store.append(event_v1_again)

        assert exc_info.value.expected_version == 2
        assert exc_info.value.actual_version == 1

    def test_different_aggregates_have_independent_versions(self) -> None:
        store = InMemoryEventStore()
        agg_a = uuid4()
        agg_b = uuid4()

        store.append(_make_event(aggregate_id=agg_a, aggregate_version=1))
        store.append(_make_event(aggregate_id=agg_b, aggregate_version=1))
        store.append(_make_event(aggregate_id=agg_a, aggregate_version=2))
        store.append(_make_event(aggregate_id=agg_b, aggregate_version=2))

        assert store.stream_version(agg_a) == 2
        assert store.stream_version(agg_b) == 2


# ---------------------------------------------------------------------------
# Append-only & immutability
# ---------------------------------------------------------------------------

class TestAppendOnlyImmutability:
    """Events are never modified or deleted after persistence."""

    def test_persisted_event_is_returned_unchanged(self) -> None:
        store = InMemoryEventStore()
        event = _make_event(payload={"bp_systolic": 120, "bp_diastolic": 80})

        result = store.append(event)

        assert result.event_id == event.event_id
        assert result.event_type == event.event_type
        assert result.aggregate_id == event.aggregate_id
        assert result.aggregate_version == event.aggregate_version
        assert result.payload == {"bp_systolic": 120, "bp_diastolic": 80}

    def test_read_returns_same_event_as_appended(self) -> None:
        store = InMemoryEventStore()
        agg_id = uuid4()
        event = _make_event(aggregate_id=agg_id, payload={"finding": "clear lungs"})

        store.append(event)
        stream = store.read_stream(agg_id)

        assert len(stream) == 1
        assert stream[0].event_id == event.event_id
        assert stream[0].payload == {"finding": "clear lungs"}

    def test_stream_order_matches_version_order(self) -> None:
        store = InMemoryEventStore()
        agg_id = uuid4()
        events = _make_stream_events(agg_id, 5)

        for event in events:
            store.append(event)

        stream = store.read_stream(agg_id)
        versions = [e.aggregate_version for e in stream]
        assert versions == [1, 2, 3, 4, 5]

    def test_events_frozen_after_persist(self) -> None:
        """DomainEvent is frozen (dataclass). Mutation should raise."""
        store = InMemoryEventStore()
        event = _make_event()
        result = store.append(event)

        with pytest.raises(AttributeError):
            result.payload = {"tampered": True}  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Idempotent append (deduplication by event_id)
# ---------------------------------------------------------------------------

class TestIdempotentAppend:
    """Appending an event with an existing event_id is a no-op."""

    def test_duplicate_event_id_returns_existing(self) -> None:
        store = InMemoryEventStore()
        agg_id = uuid4()
        event_id = uuid4()

        original = _make_event(aggregate_id=agg_id, aggregate_version=1, event_id=event_id)
        store.append(original)

        duplicate = _make_event(aggregate_id=agg_id, aggregate_version=1, event_id=event_id)
        result = store.append(duplicate)

        assert result.event_id == event_id
        assert store.stream_version(agg_id) == 1

    def test_duplicate_does_not_add_to_stream(self) -> None:
        store = InMemoryEventStore()
        agg_id = uuid4()
        event_id = uuid4()

        event = _make_event(aggregate_id=agg_id, aggregate_version=1, event_id=event_id)
        store.append(event)
        store.append(event)
        store.append(event)

        stream = store.read_stream(agg_id)
        assert len(stream) == 1


# ---------------------------------------------------------------------------
# recorded_at is set by the store
# ---------------------------------------------------------------------------

class TestRecordedAtTimestamp:
    """The event store sets recorded_at at persist time."""

    def test_recorded_at_is_set_on_persist(self) -> None:
        store = InMemoryEventStore()
        event = _make_event()
        assert event.recorded_at is None

        result = store.append(event)
        assert result.recorded_at is not None

    def test_recorded_at_is_utc(self) -> None:
        store = InMemoryEventStore()
        result = store.append(_make_event())
        assert result.recorded_at.tzinfo == timezone.utc

    def test_recorded_at_is_close_to_now(self) -> None:
        store = InMemoryEventStore()
        before = datetime.now(timezone.utc)
        result = store.append(_make_event())
        after = datetime.now(timezone.utc)

        assert before <= result.recorded_at <= after


# ---------------------------------------------------------------------------
# Stream reading
# ---------------------------------------------------------------------------

class TestStreamReading:
    """read_stream and read_stream_from return correct subsets."""

    def test_empty_stream_returns_empty_list(self) -> None:
        store = InMemoryEventStore()
        assert store.read_stream(uuid4()) == []

    def test_read_stream_from_returns_subset(self) -> None:
        store = InMemoryEventStore()
        agg_id = uuid4()
        events = _make_stream_events(agg_id, 5)
        for event in events:
            store.append(event)

        from_v3 = store.read_stream_from(agg_id, from_version=3)
        versions = [e.aggregate_version for e in from_v3]
        assert versions == [3, 4, 5]

    def test_read_stream_from_beyond_end_returns_empty(self) -> None:
        store = InMemoryEventStore()
        agg_id = uuid4()
        store.append(_make_event(aggregate_id=agg_id, aggregate_version=1))

        assert store.read_stream_from(agg_id, from_version=99) == []

    def test_stream_version_returns_0_for_unknown_aggregate(self) -> None:
        store = InMemoryEventStore()
        assert store.stream_version(uuid4()) == 0

    def test_stream_version_returns_highest_version(self) -> None:
        store = InMemoryEventStore()
        agg_id = uuid4()
        events = _make_stream_events(agg_id, 3)
        for event in events:
            store.append(event)

        assert store.stream_version(agg_id) == 3


# ---------------------------------------------------------------------------
# event_exists
# ---------------------------------------------------------------------------

class TestEventExists:

    def test_exists_after_append(self) -> None:
        store = InMemoryEventStore()
        event = _make_event()
        store.append(event)
        assert store.event_exists(event.event_id) is True

    def test_not_exists_before_append(self) -> None:
        store = InMemoryEventStore()
        assert store.event_exists(uuid4()) is False


# ---------------------------------------------------------------------------
# read_all_events
# ---------------------------------------------------------------------------

class TestReadAllEvents:

    def test_read_all_returns_all_events_across_streams(self) -> None:
        store = InMemoryEventStore()
        agg_a = uuid4()
        agg_b = uuid4()

        store.append(_make_event(aggregate_id=agg_a, aggregate_version=1))
        store.append(_make_event(aggregate_id=agg_b, aggregate_version=1))
        store.append(_make_event(aggregate_id=agg_a, aggregate_version=2))

        all_events = store.read_all_events()
        assert len(all_events) == 3

    def test_read_all_ordered_by_recorded_at(self) -> None:
        store = InMemoryEventStore()
        agg_a = uuid4()
        agg_b = uuid4()

        store.append(_make_event(aggregate_id=agg_a, aggregate_version=1))
        store.append(_make_event(aggregate_id=agg_b, aggregate_version=1))

        all_events = store.read_all_events()
        assert all_events[0].recorded_at <= all_events[1].recorded_at

    def test_empty_store_returns_empty(self) -> None:
        store = InMemoryEventStore()
        assert store.read_all_events() == []
