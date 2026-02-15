"""Event Dispatcher — Pipeline Stages 3 + 4 (Publish + Route).

Connects the event store to projection handlers. The dispatcher:
- Maintains a registry of subscriptions (event_type → list of callables).
- Dispatches events to all matching subscribers.
- Has no knowledge of what handlers do internally.
- Isolates handler failures — one failing handler does not block others.
- Provides batch dispatch with deterministic per-aggregate version ordering.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable

from clinical_core.domain.events import DomainEvent

logger = logging.getLogger(__name__)

EventHandler = Callable[[DomainEvent], None]


class EventDispatcher:
    """In-process event dispatcher.

    Subscribers register interest in specific event types.
    When an event is dispatched, all matching handlers are invoked.
    """

    def __init__(self) -> None:
        self._subscriptions: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """Register a handler for a specific event type."""
        self._subscriptions[event_type].append(handler)

    def dispatch(self, event: DomainEvent) -> None:
        """Dispatch a single event to all matching subscribers.

        Handler failures are logged and isolated — they do not propagate
        or prevent other handlers from receiving the event.
        """
        handlers = self._subscriptions.get(event.event_type, [])
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                logger.exception(
                    "Handler %r failed processing event %s (type=%s)",
                    handler,
                    event.event_id,
                    event.event_type,
                )

    def dispatch_batch(self, events: list[DomainEvent]) -> None:
        """Dispatch a batch of events with deterministic ordering.

        Events are sorted by (aggregate_id, aggregate_version) before
        dispatch, ensuring that within each aggregate stream, events
        arrive in version order. This is critical for projection
        correctness after offline sync.
        """
        sorted_events = sorted(
            events,
            key=lambda e: (e.aggregate_id, e.aggregate_version),
        )
        for event in sorted_events:
            self.dispatch(event)
