"""In-memory Event Store adapter.

Implements the EventStore protocol defined in domain/event_store.py.

This is an infrastructure adapter — it provides a concrete, in-memory
implementation for development and testing. A persistent implementation
(e.g., backed by a file or database) would implement the same protocol.

Rules enforced:
- Append-only: events are stored in insertion order, never removed.
- Immutable: stored events are never modified (except recorded_at set once).
- Sequential versioning: aggregate_version must be contiguous per stream.
- Idempotent: duplicate event_id returns existing event without error.
- No business logic, no domain interpretation of event payloads.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from clinical_core.domain.events import ConcurrencyError, DomainEvent


class InMemoryEventStore:
    """In-memory implementation of the EventStore protocol.

    Stores events in two structures:
    - _streams: dict mapping aggregate_id → list of events (ordered by version).
    - _events_by_id: dict mapping event_id → event (for deduplication and lookup).
    - _all_events: list of all events in insertion order (for read_all_events).
    """

    def __init__(self) -> None:
        self._streams: dict[UUID, list[DomainEvent]] = {}
        self._events_by_id: dict[UUID, DomainEvent] = {}
        self._all_events: list[DomainEvent] = []

    def append(self, event: DomainEvent) -> DomainEvent:
        if event.event_id in self._events_by_id:
            return self._events_by_id[event.event_id]

        stream = self._streams.get(event.aggregate_id, [])
        expected_version = len(stream) + 1

        if event.aggregate_version != expected_version:
            raise ConcurrencyError(
                aggregate_id=event.aggregate_id,
                expected_version=expected_version,
                actual_version=event.aggregate_version,
            )

        persisted = event.with_recorded_at(datetime.now(timezone.utc))

        if event.aggregate_id not in self._streams:
            self._streams[event.aggregate_id] = []
        self._streams[event.aggregate_id].append(persisted)
        self._events_by_id[persisted.event_id] = persisted
        self._all_events.append(persisted)

        return persisted

    def read_stream(self, aggregate_id: UUID) -> list[DomainEvent]:
        return list(self._streams.get(aggregate_id, []))

    def read_stream_from(self, aggregate_id: UUID, from_version: int) -> list[DomainEvent]:
        stream = self._streams.get(aggregate_id, [])
        return [e for e in stream if e.aggregate_version >= from_version]

    def read_all_events(self) -> list[DomainEvent]:
        return list(self._all_events)

    def stream_version(self, aggregate_id: UUID) -> int:
        stream = self._streams.get(aggregate_id, [])
        if not stream:
            return 0
        return stream[-1].aggregate_version

    def event_exists(self, event_id: UUID) -> bool:
        return event_id in self._events_by_id
