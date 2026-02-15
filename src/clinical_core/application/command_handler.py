"""Command Handler — application-layer orchestrator.

The command handler connects the outside world to the domain:
1. Loads the aggregate's event stream from the event store.
2. Rehydrates aggregate state by replaying events.
3. Passes the command to the aggregate for domain logic execution.
4. Persists resulting events to the event store.
5. Dispatches persisted events to projections via the event dispatcher.

The handler contains NO domain logic. It loads, delegates, and persists.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from clinical_core.domain.aggregate import Aggregate
from clinical_core.domain.events import DomainEvent, EventMetadata


class CommandHandler:
    """Generic command handler that works with any Aggregate implementation.

    Orchestrates the full command flow:
      load stream → rehydrate → execute → persist → dispatch
    """

    def __init__(
        self,
        event_store: Any,
        dispatcher: Any,
        aggregate: Aggregate,
    ) -> None:
        self._event_store = event_store
        self._dispatcher = dispatcher
        self._aggregate = aggregate

    def handle(self, command: Any, aggregate_id: UUID) -> list[DomainEvent]:
        """Handle a command against a specific aggregate instance.

        Returns the list of persisted events on success.
        Raises DomainError if the aggregate rejects the command.
        Raises ConcurrencyError if the event store detects a version conflict.
        """
        # 1. Load the aggregate's event stream
        stream = self._event_store.read_stream(aggregate_id)

        # 2. Rehydrate aggregate state
        state = self._aggregate.rehydrate(stream)
        current_version = self._event_store.stream_version(aggregate_id)

        # 3. Execute domain logic (may raise DomainError)
        new_events = self._aggregate.execute(state, command)

        # 4. Assign final event metadata (version, event_id) and persist
        persisted: list[DomainEvent] = []
        for i, event in enumerate(new_events):
            versioned = _set_version(event, current_version + i + 1)
            result = self._event_store.append(versioned)
            persisted.append(result)

        # 5. Dispatch persisted events to projections
        for event in persisted:
            self._dispatcher.dispatch(event)

        return persisted


def _set_version(event: DomainEvent, version: int) -> DomainEvent:
    """Return a new event with the correct aggregate_version."""
    m = event.metadata
    new_metadata = EventMetadata(
        event_id=m.event_id,
        event_type=m.event_type,
        schema_version=m.schema_version,
        aggregate_id=m.aggregate_id,
        aggregate_type=m.aggregate_type,
        aggregate_version=version,
        occurred_at=m.occurred_at,
        performed_by=m.performed_by,
        performer_role=m.performer_role,
        organization_id=m.organization_id,
        facility_id=m.facility_id,
        device_id=m.device_id,
        connection_status=m.connection_status,
        correlation_id=m.correlation_id,
        recorded_at=m.recorded_at,
        causation_id=m.causation_id,
        visibility=m.visibility,
    )
    return DomainEvent(metadata=new_metadata, payload=event.payload)
