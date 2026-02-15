"""TDD tests for the Projection Handler abstraction.

The ProjectionHandler is the base class for all projections (Pipeline Stage 5).

Requirements verified:
- Receives events via a handle() method.
- Maintains projection state (in-memory dict).
- Tracks processed event IDs for idempotent processing.
- Rebuilds entirely from a list of events (rebuild_from).
- Stateless processing logic: the fold function depends only on current state + event.
- Declares which event types it subscribes to.
- Skips duplicate events (by event_id).
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

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
    event_type: str = "clinical.test.EventA",
    aggregate_id: UUID | None = None,
    aggregate_version: int = 1,
    event_id: UUID | None = None,
    payload: dict | None = None,
) -> DomainEvent:
    return DomainEvent(
        metadata=EventMetadata(
            event_id=event_id or uuid4(),
            event_type=event_type,
            schema_version=1,
            aggregate_id=aggregate_id or uuid4(),
            aggregate_type="TestAggregate",
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


# ---------------------------------------------------------------------------
# A concrete test projection (counter)
# ---------------------------------------------------------------------------

from clinical_core.application.projection_handler import ProjectionHandler


class CounterProjection(ProjectionHandler):
    """A trivial projection that counts events per type. Used for testing."""

    @property
    def subscribed_event_types(self) -> list[str]:
        return ["clinical.test.EventA", "clinical.test.EventB"]

    def _apply(self, state: dict, event: DomainEvent) -> dict:
        event_type = event.event_type
        count = state.get(event_type, 0)
        return {**state, event_type: count + 1}


# ---------------------------------------------------------------------------
# Tests: Event handling
# ---------------------------------------------------------------------------

class TestEventHandling:

    def test_handle_updates_state(self) -> None:
        proj = CounterProjection()
        proj.handle(_make_event(event_type="clinical.test.EventA"))

        assert proj.state == {"clinical.test.EventA": 1}

    def test_handle_accumulates_state(self) -> None:
        proj = CounterProjection()
        proj.handle(_make_event(event_type="clinical.test.EventA"))
        proj.handle(_make_event(event_type="clinical.test.EventA"))
        proj.handle(_make_event(event_type="clinical.test.EventB"))

        assert proj.state == {"clinical.test.EventA": 2, "clinical.test.EventB": 1}

    def test_handle_ignores_unsubscribed_event_types(self) -> None:
        proj = CounterProjection()
        proj.handle(_make_event(event_type="clinical.test.Unknown"))

        assert proj.state == {}

    def test_handle_is_idempotent_by_event_id(self) -> None:
        proj = CounterProjection()
        event_id = uuid4()
        event = _make_event(event_type="clinical.test.EventA", event_id=event_id)

        proj.handle(event)
        proj.handle(event)
        proj.handle(event)

        assert proj.state == {"clinical.test.EventA": 1}


# ---------------------------------------------------------------------------
# Tests: Rebuild from events
# ---------------------------------------------------------------------------

class TestRebuild:

    def test_rebuild_from_empty_produces_empty_state(self) -> None:
        proj = CounterProjection()
        proj.handle(_make_event(event_type="clinical.test.EventA"))

        proj.rebuild_from([])
        assert proj.state == {}

    def test_rebuild_from_replays_all_events(self) -> None:
        proj = CounterProjection()

        events = [
            _make_event(event_type="clinical.test.EventA"),
            _make_event(event_type="clinical.test.EventA"),
            _make_event(event_type="clinical.test.EventB"),
        ]

        proj.rebuild_from(events)
        assert proj.state == {"clinical.test.EventA": 2, "clinical.test.EventB": 1}

    def test_rebuild_clears_previous_state(self) -> None:
        proj = CounterProjection()
        proj.handle(_make_event(event_type="clinical.test.EventA"))
        assert proj.state == {"clinical.test.EventA": 1}

        proj.rebuild_from([_make_event(event_type="clinical.test.EventB")])
        assert proj.state == {"clinical.test.EventB": 1}

    def test_rebuild_clears_processed_event_ids(self) -> None:
        """After rebuild, the same event_id can be processed again if it's in the new list."""
        proj = CounterProjection()
        event = _make_event(event_type="clinical.test.EventA")

        proj.handle(event)
        assert proj.state == {"clinical.test.EventA": 1}

        # Rebuild includes the same event — should be counted
        proj.rebuild_from([event])
        assert proj.state == {"clinical.test.EventA": 1}

    def test_rebuild_filters_unsubscribed_types(self) -> None:
        proj = CounterProjection()

        events = [
            _make_event(event_type="clinical.test.EventA"),
            _make_event(event_type="clinical.test.Unknown"),
            _make_event(event_type="clinical.test.EventB"),
        ]

        proj.rebuild_from(events)
        assert proj.state == {"clinical.test.EventA": 1, "clinical.test.EventB": 1}

    def test_rebuild_deduplicates_events(self) -> None:
        proj = CounterProjection()
        event = _make_event(event_type="clinical.test.EventA")

        proj.rebuild_from([event, event, event])
        assert proj.state == {"clinical.test.EventA": 1}


# ---------------------------------------------------------------------------
# Tests: Stateless processing
# ---------------------------------------------------------------------------

class TestStatelessProcessing:

    def test_apply_is_pure_function(self) -> None:
        """_apply must depend only on state + event, not on instance fields."""
        proj = CounterProjection()
        event = _make_event(event_type="clinical.test.EventA")

        result_a = proj._apply({}, event)
        result_b = proj._apply({}, event)

        assert result_a == result_b

    def test_state_is_dict(self) -> None:
        proj = CounterProjection()
        assert isinstance(proj.state, dict)

    def test_initial_state_is_empty(self) -> None:
        proj = CounterProjection()
        assert proj.state == {}
