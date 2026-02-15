"""Event Store port (interface).

This is a domain-layer port — it defines WHAT the event store must do,
not HOW it does it. Infrastructure adapters implement this protocol.

The event store is append-only. It stores immutable domain events in
per-aggregate streams with sequential versioning.

No projection logic is permitted in the event store or its implementations.
The event store persists and retrieves events — nothing more.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from clinical_core.domain.events import DomainEvent


class EventStore(Protocol):
    """Port for event persistence.

    Implementations must satisfy:
    - Append-only: events are never modified or deleted.
    - Immutable: a persisted event's content never changes (except recorded_at set once).
    - Sequential per stream: aggregate_version is contiguous (1, 2, 3, ...) per aggregate_id.
    - No projection logic: the event store does not interpret events or update read models.
    """

    def append(self, event: DomainEvent) -> DomainEvent:
        """Append an event to its aggregate's stream.

        Pipeline Stage 2: Persist.

        Behavior:
        - Validates aggregate_version == current stream length + 1 (INV-XX-3).
        - If event_id already exists, returns the existing event (idempotent, no error).
        - Sets recorded_at to the current system time.
        - Returns the persisted event (with recorded_at populated).

        Raises:
            ConcurrencyError: if aggregate_version does not match expected next version.
        """
        ...

    def read_stream(self, aggregate_id: UUID) -> list[DomainEvent]:
        """Read all events for an aggregate, ordered by aggregate_version.

        Used by:
        - Aggregate rehydration (loading current state from events).
        - Projection targeted rebuild (replaying one aggregate's history).

        Returns an empty list if the aggregate has no events.
        """
        ...

    def read_stream_from(self, aggregate_id: UUID, from_version: int) -> list[DomainEvent]:
        """Read events for an aggregate starting from a given version (inclusive).

        Used by:
        - Incremental aggregate rehydration (loading events after a snapshot).
        - Catch-up processing.

        Returns an empty list if no events exist at or after from_version.
        """
        ...

    def read_all_events(self) -> list[DomainEvent]:
        """Read all events across all streams, ordered by recorded_at then event_id.

        Used by:
        - Full projection rebuild.
        - Catch-up polling (with filtering applied by the caller).

        This is a potentially expensive operation. Callers should prefer
        filtered reads when possible.
        """
        ...

    def stream_version(self, aggregate_id: UUID) -> int:
        """Return the current version (highest aggregate_version) of a stream.

        Returns 0 if the stream does not exist (no events).
        """
        ...

    def event_exists(self, event_id: UUID) -> bool:
        """Check if an event with the given ID has been persisted.

        Used for deduplication checks.
        """
        ...
