"""TDD tests for the Event Dispatcher.

The dispatcher is Pipeline Stage 3+4: Publish + Route.
It connects the event store to projection handlers.

Requirements verified:
- Subscribe handlers to specific event types.
- Dispatch events only to matching subscribers.
- No knowledge of handler internals (handler is a callable).
- Deterministic ordering: events for the same aggregate arrive in version order.
- Multiple handlers can subscribe to the same event type.
- A handler can subscribe to multiple event types.
- Dispatching an event with no subscribers is a silent no-op.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from clinical_core.domain.events import (
    ConnectionStatus,
    DomainEvent,
    EventMetadata,
)


# ---------------------------------------------------------------------------
# Test helper
# ---------------------------------------------------------------------------

def _make_event(
    event_type: str = "clinical.encounter.EncounterBegan",
    aggregate_id=None,
    aggregate_version: int = 1,
    occurred_at: datetime | None = None,
) -> DomainEvent:
    agg_id = aggregate_id or uuid4()
    return DomainEvent(
        metadata=EventMetadata(
            event_id=uuid4(),
            event_type=event_type,
            schema_version=1,
            aggregate_id=agg_id,
            aggregate_type="Encounter",
            aggregate_version=aggregate_version,
            occurred_at=occurred_at or datetime.now(timezone.utc),
            performed_by=uuid4(),
            performer_role="physician",
            organization_id=uuid4(),
            facility_id=uuid4(),
            device_id="device-001",
            connection_status=ConnectionStatus.ONLINE,
            correlation_id=uuid4(),
        ),
    )


class SpyHandler:
    """A test double that records received events."""

    def __init__(self) -> None:
        self.received: list[DomainEvent] = []

    def __call__(self, event: DomainEvent) -> None:
        self.received.append(event)


# ---------------------------------------------------------------------------
# Tests: Subscription
# ---------------------------------------------------------------------------

class TestSubscription:

    def test_subscribe_handler_to_event_type(self) -> None:
        from clinical_core.application.event_dispatcher import EventDispatcher

        dispatcher = EventDispatcher()
        handler = SpyHandler()

        dispatcher.subscribe("clinical.encounter.EncounterBegan", handler)
        event = _make_event(event_type="clinical.encounter.EncounterBegan")
        dispatcher.dispatch(event)

        assert len(handler.received) == 1
        assert handler.received[0].event_id == event.event_id

    def test_handler_not_called_for_unsubscribed_type(self) -> None:
        from clinical_core.application.event_dispatcher import EventDispatcher

        dispatcher = EventDispatcher()
        handler = SpyHandler()

        dispatcher.subscribe("clinical.encounter.EncounterBegan", handler)
        event = _make_event(event_type="clinical.encounter.EncounterCompleted")
        dispatcher.dispatch(event)

        assert len(handler.received) == 0

    def test_multiple_handlers_for_same_type(self) -> None:
        from clinical_core.application.event_dispatcher import EventDispatcher

        dispatcher = EventDispatcher()
        handler_a = SpyHandler()
        handler_b = SpyHandler()

        dispatcher.subscribe("clinical.encounter.EncounterBegan", handler_a)
        dispatcher.subscribe("clinical.encounter.EncounterBegan", handler_b)
        event = _make_event(event_type="clinical.encounter.EncounterBegan")
        dispatcher.dispatch(event)

        assert len(handler_a.received) == 1
        assert len(handler_b.received) == 1

    def test_handler_subscribed_to_multiple_types(self) -> None:
        from clinical_core.application.event_dispatcher import EventDispatcher

        dispatcher = EventDispatcher()
        handler = SpyHandler()

        dispatcher.subscribe("clinical.encounter.EncounterBegan", handler)
        dispatcher.subscribe("clinical.encounter.EncounterCompleted", handler)

        dispatcher.dispatch(_make_event(event_type="clinical.encounter.EncounterBegan"))
        dispatcher.dispatch(_make_event(event_type="clinical.encounter.EncounterCompleted"))

        assert len(handler.received) == 2

    def test_dispatch_with_no_subscribers_is_silent(self) -> None:
        from clinical_core.application.event_dispatcher import EventDispatcher

        dispatcher = EventDispatcher()
        event = _make_event(event_type="clinical.orphan.NoOneListens")

        # Should not raise
        dispatcher.dispatch(event)


# ---------------------------------------------------------------------------
# Tests: Dispatch behavior
# ---------------------------------------------------------------------------

class TestDispatch:

    def test_dispatch_delivers_correct_event(self) -> None:
        from clinical_core.application.event_dispatcher import EventDispatcher

        dispatcher = EventDispatcher()
        handler = SpyHandler()
        dispatcher.subscribe("clinical.test.EventA", handler)

        event = _make_event(event_type="clinical.test.EventA")
        dispatcher.dispatch(event)

        assert handler.received[0] is event

    def test_dispatch_multiple_events_preserves_order(self) -> None:
        from clinical_core.application.event_dispatcher import EventDispatcher

        dispatcher = EventDispatcher()
        handler = SpyHandler()
        dispatcher.subscribe("clinical.test.EventA", handler)

        events = [_make_event(event_type="clinical.test.EventA") for _ in range(5)]
        for e in events:
            dispatcher.dispatch(e)

        assert [h.event_id for h in handler.received] == [e.event_id for e in events]

    def test_dispatch_isolates_handler_failures(self) -> None:
        """A failing handler must not prevent other handlers from receiving the event."""
        from clinical_core.application.event_dispatcher import EventDispatcher

        dispatcher = EventDispatcher()

        def failing_handler(event: DomainEvent) -> None:
            raise RuntimeError("handler bug")

        good_handler = SpyHandler()

        dispatcher.subscribe("clinical.test.EventA", failing_handler)
        dispatcher.subscribe("clinical.test.EventA", good_handler)

        event = _make_event(event_type="clinical.test.EventA")
        dispatcher.dispatch(event)

        assert len(good_handler.received) == 1


# ---------------------------------------------------------------------------
# Tests: Deterministic ordering per stream
# ---------------------------------------------------------------------------

class TestDeterministicOrdering:

    def test_dispatch_batch_preserves_version_order(self) -> None:
        """dispatch_batch must deliver events sorted by aggregate_version per aggregate."""
        from clinical_core.application.event_dispatcher import EventDispatcher

        dispatcher = EventDispatcher()
        handler = SpyHandler()
        dispatcher.subscribe("clinical.test.EventA", handler)

        agg_id = uuid4()
        e1 = _make_event(event_type="clinical.test.EventA", aggregate_id=agg_id, aggregate_version=1)
        e3 = _make_event(event_type="clinical.test.EventA", aggregate_id=agg_id, aggregate_version=3)
        e2 = _make_event(event_type="clinical.test.EventA", aggregate_id=agg_id, aggregate_version=2)

        dispatcher.dispatch_batch([e1, e3, e2])

        versions = [e.aggregate_version for e in handler.received]
        assert versions == [1, 2, 3]

    def test_dispatch_batch_interleaves_different_aggregates_by_version(self) -> None:
        """Events from different aggregates are grouped and sorted per aggregate."""
        from clinical_core.application.event_dispatcher import EventDispatcher

        dispatcher = EventDispatcher()
        handler = SpyHandler()
        dispatcher.subscribe("clinical.test.EventA", handler)

        agg_a = uuid4()
        agg_b = uuid4()

        events = [
            _make_event(event_type="clinical.test.EventA", aggregate_id=agg_a, aggregate_version=2),
            _make_event(event_type="clinical.test.EventA", aggregate_id=agg_b, aggregate_version=1),
            _make_event(event_type="clinical.test.EventA", aggregate_id=agg_a, aggregate_version=1),
        ]

        dispatcher.dispatch_batch(events)

        # Within each aggregate, versions must be ascending
        agg_a_versions = [e.aggregate_version for e in handler.received if e.aggregate_id == agg_a]
        agg_b_versions = [e.aggregate_version for e in handler.received if e.aggregate_id == agg_b]

        assert agg_a_versions == [1, 2]
        assert agg_b_versions == [1]

    def test_dispatch_batch_empty_is_silent(self) -> None:
        from clinical_core.application.event_dispatcher import EventDispatcher

        dispatcher = EventDispatcher()
        handler = SpyHandler()
        dispatcher.subscribe("clinical.test.EventA", handler)

        dispatcher.dispatch_batch([])
        assert len(handler.received) == 0
