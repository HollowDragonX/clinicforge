"""Aggregate base class.

An aggregate is a transactional clinical boundary that:
- Maintains state derived exclusively from its event stream.
- Accepts commands and produces events (or rejects with DomainError).
- Enforces intra-aggregate invariants (strong consistency).
- Has no knowledge of infrastructure, projections, or other aggregates.

The aggregate's execute() method is a pure function:
  execute(state, command) → list[DomainEvent] | raises DomainError
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from clinical_core.domain.events import (
    ConnectionStatus,
    DomainEvent,
    EventMetadata,
)


class DomainError(Exception):
    """Raised when an aggregate rejects a command due to an invariant violation."""


class Aggregate(ABC):
    """Base class for all aggregates.

    Subclasses implement:
    - aggregate_type: string name of the aggregate.
    - initial_state(): the empty state before any events.
    - apply_event(state, event) -> new_state: the fold function.
    - execute(state, command) -> list[DomainEvent]: domain logic.
    """

    @property
    @abstractmethod
    def aggregate_type(self) -> str:
        """The aggregate type name (e.g., 'Encounter', 'Diagnosis')."""
        ...

    @abstractmethod
    def initial_state(self) -> dict[str, Any]:
        """Return the initial state for a new aggregate (no events yet)."""
        ...

    @abstractmethod
    def apply_event(self, state: dict[str, Any], event: DomainEvent) -> dict[str, Any]:
        """Pure fold: apply one event to produce new state.

        Used during rehydration. Must be deterministic and side-effect-free.
        """
        ...

    @abstractmethod
    def execute(self, state: dict[str, Any], command: Any) -> list[DomainEvent]:
        """Execute domain logic: decide whether to accept the command.

        Returns a list of new events if the command is accepted.
        Raises DomainError if an invariant is violated.

        This is a pure function — it reads only state and command.
        """
        ...

    def rehydrate(self, events: list[DomainEvent]) -> dict[str, Any]:
        """Rebuild aggregate state by replaying events through apply_event."""
        state = self.initial_state()
        for event in events:
            state = self.apply_event(state, event)
        return state

    def _build_event(
        self,
        command: Any,
        event_type: str,
        aggregate_id: UUID,
        payload: dict[str, Any],
    ) -> DomainEvent:
        """Helper: construct a DomainEvent from a command's context.

        Sets placeholder values for fields the CommandHandler will override
        (event_id, aggregate_version). The handler is responsible for
        assigning the final event_id and version before persistence.
        """
        return DomainEvent(
            metadata=EventMetadata(
                event_id=uuid4(),
                event_type=event_type,
                schema_version=1,
                aggregate_id=aggregate_id,
                aggregate_type=self.aggregate_type,
                aggregate_version=0,  # placeholder — handler sets real version
                occurred_at=command.occurred_at,
                performed_by=command.performed_by,
                performer_role=command.performer_role,
                organization_id=command.organization_id,
                facility_id=command.facility_id,
                device_id=command.device_id,
                connection_status=command.connection_status,
                correlation_id=command.correlation_id,
            ),
            payload=payload,
        )
