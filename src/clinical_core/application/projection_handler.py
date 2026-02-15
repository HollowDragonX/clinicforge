"""Projection Handler abstraction — Pipeline Stage 5.

Base class for all projections. A projection is a derived, rebuildable
view of events. It folds events into an in-memory state dict.

Requirements:
- Receives events via handle().
- Updates projection state via a pure _apply() fold function.
- Tracks processed event IDs for idempotent processing.
- Rebuilds entirely from event history via rebuild_from().
- Stateless processing logic: _apply depends only on current state + event.
- Declares subscribed event types so the dispatcher can route correctly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any
from uuid import UUID

from clinical_core.domain.events import DomainEvent


class ProjectionHandler(ABC):
    """Abstract base for projection handlers.

    Subclasses implement:
    - subscribed_event_types: which event types this projection cares about.
    - _apply(state, event) -> new_state: the pure fold function.
    """

    def __init__(self) -> None:
        self._state: dict[str, Any] = {}
        self._processed_event_ids: set[UUID] = set()

    @property
    def state(self) -> dict[str, Any]:
        """Current projection state. Read-only access for queries."""
        return self._state

    @property
    @abstractmethod
    def subscribed_event_types(self) -> list[str]:
        """Event types this projection consumes."""
        ...

    @abstractmethod
    def _apply(self, state: dict[str, Any], event: DomainEvent) -> dict[str, Any]:
        """Pure fold function: (current_state, event) -> new_state.

        Must be deterministic and side-effect-free. Must not read from
        external sources or modify instance fields beyond returning
        the new state dict.
        """
        ...

    def handle(self, event: DomainEvent) -> None:
        """Process a single event. Skips unsubscribed types and duplicates."""
        if event.event_type not in self.subscribed_event_types:
            return

        if event.event_id in self._processed_event_ids:
            return

        self._state = self._apply(self._state, event)
        self._processed_event_ids.add(event.event_id)

    def rebuild_from(self, events: list[DomainEvent]) -> None:
        """Rebuild projection state entirely from a list of events.

        Clears all existing state and processed event IDs, then replays
        each event through handle(). This guarantees the projection
        converges to the correct state regardless of prior history.
        """
        self._state = {}
        self._processed_event_ids = set()
        for event in events:
            self.handle(event)
