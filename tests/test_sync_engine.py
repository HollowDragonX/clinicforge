"""TDD tests for the Sync Engine skeleton.

The sync engine exchanges missing events between two nodes.
No networking layer — sync is simulated locally via direct method calls.

Requirements tested:
1. Exchange known event positions
2. Request missing events
3. Append received events (idempotent, dedup by event_id)
4. Trigger projection updates after receiving events

Uses InMemoryEventStore as the local store on each node.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from clinical_core.domain.events import (
    ConnectionStatus,
    DomainEvent,
    EventMetadata,
)
from clinical_core.infrastructure.in_memory_event_store import InMemoryEventStore
from clinical_core.application.event_dispatcher import EventDispatcher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ORG_ID = uuid4()
_FACILITY_ID = uuid4()


def _event(
    event_type: str,
    aggregate_id: UUID,
    aggregate_version: int,
    device_id: str = "device-a",
    payload: dict | None = None,
) -> DomainEvent:
    return DomainEvent(
        metadata=EventMetadata(
            event_id=uuid4(),
            event_type=event_type,
            schema_version=1,
            aggregate_id=aggregate_id,
            aggregate_type="TestAggregate",
            aggregate_version=aggregate_version,
            occurred_at=datetime.now(timezone.utc),
            performed_by=uuid4(),
            performer_role="physician",
            organization_id=_ORG_ID,
            facility_id=_FACILITY_ID,
            device_id=device_id,
            connection_status=ConnectionStatus.OFFLINE,
            correlation_id=uuid4(),
        ),
        payload=payload or {},
    )


class SpyHandler:
    """Records dispatched events."""
    def __init__(self) -> None:
        self.received: list[DomainEvent] = []

    def __call__(self, event: DomainEvent) -> None:
        self.received.append(event)


# ---------------------------------------------------------------------------
# Tests: SyncNode — position tracking
# ---------------------------------------------------------------------------

class TestSyncNodePosition:
    """A node knows its current event position."""

    def test_empty_node_has_zero_position(self) -> None:
        from clinical_core.sync.engine import SyncNode

        store = InMemoryEventStore()
        dispatcher = EventDispatcher()
        node = SyncNode(node_id="node-a", event_store=store, dispatcher=dispatcher)

        assert node.event_count() == 0

    def test_position_reflects_stored_events(self) -> None:
        from clinical_core.sync.engine import SyncNode

        store = InMemoryEventStore()
        dispatcher = EventDispatcher()

        agg_id = uuid4()
        store.append(_event("test.Created", agg_id, 1))

        node = SyncNode(node_id="node-a", event_store=store, dispatcher=dispatcher)

        assert node.event_count() == 1

    def test_position_includes_multiple_streams(self) -> None:
        from clinical_core.sync.engine import SyncNode

        store = InMemoryEventStore()
        dispatcher = EventDispatcher()

        store.append(_event("test.Created", uuid4(), 1))
        store.append(_event("test.Created", uuid4(), 1))
        store.append(_event("test.Created", uuid4(), 1))

        node = SyncNode(node_id="node-a", event_store=store, dispatcher=dispatcher)

        assert node.event_count() == 3

    def test_known_event_ids_returns_all_ids(self) -> None:
        from clinical_core.sync.engine import SyncNode

        store = InMemoryEventStore()
        dispatcher = EventDispatcher()

        agg1 = uuid4()
        agg2 = uuid4()
        e1 = store.append(_event("test.A", agg1, 1))
        e2 = store.append(_event("test.B", agg2, 1))

        node = SyncNode(node_id="node-a", event_store=store, dispatcher=dispatcher)

        ids = node.known_event_ids()
        assert e1.event_id in ids
        assert e2.event_id in ids
        assert len(ids) == 2


# ---------------------------------------------------------------------------
# Tests: SyncEngine — missing event detection
# ---------------------------------------------------------------------------

class TestMissingEventDetection:
    """The engine detects which events one node has that the other lacks."""

    def test_detect_events_node_b_is_missing(self) -> None:
        from clinical_core.sync.engine import SyncNode, SyncEngine

        store_a = InMemoryEventStore()
        store_b = InMemoryEventStore()
        disp_a = EventDispatcher()
        disp_b = EventDispatcher()

        agg_id = uuid4()
        store_a.append(_event("test.Created", agg_id, 1, device_id="device-a"))

        node_a = SyncNode("node-a", store_a, disp_a)
        node_b = SyncNode("node-b", store_b, disp_b)
        engine = SyncEngine()

        missing = engine.detect_missing(source=node_a, target=node_b)
        assert len(missing) == 1
        assert missing[0].event_type == "test.Created"

    def test_detect_no_missing_when_in_sync(self) -> None:
        from clinical_core.sync.engine import SyncNode, SyncEngine

        store_a = InMemoryEventStore()
        store_b = InMemoryEventStore()
        disp_a = EventDispatcher()
        disp_b = EventDispatcher()

        agg_id = uuid4()
        event = _event("test.Created", agg_id, 1)
        store_a.append(event)
        store_b.append(event)

        node_a = SyncNode("node-a", store_a, disp_a)
        node_b = SyncNode("node-b", store_b, disp_b)
        engine = SyncEngine()

        missing = engine.detect_missing(source=node_a, target=node_b)
        assert len(missing) == 0

    def test_detect_events_across_multiple_streams(self) -> None:
        from clinical_core.sync.engine import SyncNode, SyncEngine

        store_a = InMemoryEventStore()
        store_b = InMemoryEventStore()
        disp_a = EventDispatcher()
        disp_b = EventDispatcher()

        store_a.append(_event("test.A", uuid4(), 1, device_id="device-a"))
        store_a.append(_event("test.B", uuid4(), 1, device_id="device-a"))
        store_a.append(_event("test.C", uuid4(), 1, device_id="device-a"))

        node_a = SyncNode("node-a", store_a, disp_a)
        node_b = SyncNode("node-b", store_b, disp_b)
        engine = SyncEngine()

        missing = engine.detect_missing(source=node_a, target=node_b)
        assert len(missing) == 3


# ---------------------------------------------------------------------------
# Tests: SyncEngine — event transfer
# ---------------------------------------------------------------------------

class TestEventTransfer:
    """Events are transferred from source to target."""

    def test_transfer_appends_to_target_store(self) -> None:
        from clinical_core.sync.engine import SyncNode, SyncEngine

        store_a = InMemoryEventStore()
        store_b = InMemoryEventStore()
        disp_a = EventDispatcher()
        disp_b = EventDispatcher()

        agg_id = uuid4()
        store_a.append(_event("test.Created", agg_id, 1, device_id="device-a"))

        node_a = SyncNode("node-a", store_a, disp_a)
        node_b = SyncNode("node-b", store_b, disp_b)
        engine = SyncEngine()

        result = engine.sync(source=node_a, target=node_b)

        assert result.transferred_count == 1
        assert store_b.stream_version(agg_id) == 1

    def test_transfer_preserves_event_identity(self) -> None:
        from clinical_core.sync.engine import SyncNode, SyncEngine

        store_a = InMemoryEventStore()
        store_b = InMemoryEventStore()
        disp_a = EventDispatcher()
        disp_b = EventDispatcher()

        agg_id = uuid4()
        original = store_a.append(_event("test.Created", agg_id, 1))

        node_a = SyncNode("node-a", store_a, disp_a)
        node_b = SyncNode("node-b", store_b, disp_b)
        engine = SyncEngine()

        engine.sync(source=node_a, target=node_b)

        received = store_b.read_stream(agg_id)
        assert len(received) == 1
        assert received[0].event_id == original.event_id
        assert received[0].event_type == original.event_type
        assert received[0].payload == original.payload

    def test_transfer_multiple_streams(self) -> None:
        from clinical_core.sync.engine import SyncNode, SyncEngine

        store_a = InMemoryEventStore()
        store_b = InMemoryEventStore()
        disp_a = EventDispatcher()
        disp_b = EventDispatcher()

        agg1, agg2 = uuid4(), uuid4()
        store_a.append(_event("test.A", agg1, 1))
        store_a.append(_event("test.B", agg2, 1))

        node_a = SyncNode("node-a", store_a, disp_a)
        node_b = SyncNode("node-b", store_b, disp_b)
        engine = SyncEngine()

        result = engine.sync(source=node_a, target=node_b)

        assert result.transferred_count == 2
        assert store_b.stream_version(agg1) == 1
        assert store_b.stream_version(agg2) == 1


# ---------------------------------------------------------------------------
# Tests: SyncEngine — duplicate prevention (idempotent)
# ---------------------------------------------------------------------------

class TestDuplicatePrevention:
    """Syncing the same events twice produces no duplicates."""

    def test_sync_twice_is_idempotent(self) -> None:
        from clinical_core.sync.engine import SyncNode, SyncEngine

        store_a = InMemoryEventStore()
        store_b = InMemoryEventStore()
        disp_a = EventDispatcher()
        disp_b = EventDispatcher()

        agg_id = uuid4()
        store_a.append(_event("test.Created", agg_id, 1))

        node_a = SyncNode("node-a", store_a, disp_a)
        node_b = SyncNode("node-b", store_b, disp_b)
        engine = SyncEngine()

        result1 = engine.sync(source=node_a, target=node_b)
        result2 = engine.sync(source=node_a, target=node_b)

        assert result1.transferred_count == 1
        assert result2.transferred_count == 0
        assert result2.duplicate_count == 1

        assert len(store_b.read_all_events()) == 1

    def test_sync_skips_events_target_already_has(self) -> None:
        from clinical_core.sync.engine import SyncNode, SyncEngine

        store_a = InMemoryEventStore()
        store_b = InMemoryEventStore()
        disp_a = EventDispatcher()
        disp_b = EventDispatcher()

        agg_id = uuid4()
        event = _event("test.Created", agg_id, 1)
        store_a.append(event)
        store_b.append(event)  # already on target

        node_a = SyncNode("node-a", store_a, disp_a)
        node_b = SyncNode("node-b", store_b, disp_b)
        engine = SyncEngine()

        result = engine.sync(source=node_a, target=node_b)

        assert result.transferred_count == 0
        assert result.duplicate_count == 1


# ---------------------------------------------------------------------------
# Tests: SyncEngine — projection updates
# ---------------------------------------------------------------------------

class TestProjectionUpdates:
    """Received events trigger projection dispatch on the target node."""

    def test_received_events_dispatched_on_target(self) -> None:
        from clinical_core.sync.engine import SyncNode, SyncEngine

        store_a = InMemoryEventStore()
        store_b = InMemoryEventStore()
        disp_a = EventDispatcher()
        disp_b = EventDispatcher()

        spy = SpyHandler()
        disp_b.subscribe("test.Created", spy)

        agg_id = uuid4()
        store_a.append(_event("test.Created", agg_id, 1))

        node_a = SyncNode("node-a", store_a, disp_a)
        node_b = SyncNode("node-b", store_b, disp_b)
        engine = SyncEngine()

        engine.sync(source=node_a, target=node_b)

        assert len(spy.received) == 1
        assert spy.received[0].event_type == "test.Created"

    def test_duplicate_events_not_dispatched_again(self) -> None:
        from clinical_core.sync.engine import SyncNode, SyncEngine

        store_a = InMemoryEventStore()
        store_b = InMemoryEventStore()
        disp_a = EventDispatcher()
        disp_b = EventDispatcher()

        spy = SpyHandler()
        disp_b.subscribe("test.Created", spy)

        agg_id = uuid4()
        store_a.append(_event("test.Created", agg_id, 1))

        node_a = SyncNode("node-a", store_a, disp_a)
        node_b = SyncNode("node-b", store_b, disp_b)
        engine = SyncEngine()

        engine.sync(source=node_a, target=node_b)
        engine.sync(source=node_a, target=node_b)  # second sync

        assert len(spy.received) == 1  # dispatched only once

    def test_multiple_event_types_dispatched(self) -> None:
        from clinical_core.sync.engine import SyncNode, SyncEngine

        store_a = InMemoryEventStore()
        store_b = InMemoryEventStore()
        disp_a = EventDispatcher()
        disp_b = EventDispatcher()

        spy_a = SpyHandler()
        spy_b = SpyHandler()
        disp_b.subscribe("test.A", spy_a)
        disp_b.subscribe("test.B", spy_b)

        store_a.append(_event("test.A", uuid4(), 1))
        store_a.append(_event("test.B", uuid4(), 1))

        node_a = SyncNode("node-a", store_a, disp_a)
        node_b = SyncNode("node-b", store_b, disp_b)
        engine = SyncEngine()

        engine.sync(source=node_a, target=node_b)

        assert len(spy_a.received) == 1
        assert len(spy_b.received) == 1


# ---------------------------------------------------------------------------
# Tests: SyncEngine — bidirectional sync
# ---------------------------------------------------------------------------

class TestBidirectionalSync:
    """Full sync exchanges events in both directions."""

    def test_bidirectional_sync_exchanges_all_events(self) -> None:
        from clinical_core.sync.engine import SyncNode, SyncEngine

        store_a = InMemoryEventStore()
        store_b = InMemoryEventStore()
        disp_a = EventDispatcher()
        disp_b = EventDispatcher()

        agg_a = uuid4()
        agg_b = uuid4()
        store_a.append(_event("test.FromA", agg_a, 1, device_id="device-a"))
        store_b.append(_event("test.FromB", agg_b, 1, device_id="device-b"))

        node_a = SyncNode("node-a", store_a, disp_a)
        node_b = SyncNode("node-b", store_b, disp_b)
        engine = SyncEngine()

        result = engine.full_sync(node_a, node_b)

        # Both nodes now have both events
        assert len(store_a.read_all_events()) == 2
        assert len(store_b.read_all_events()) == 2

        assert result.a_to_b_transferred == 1
        assert result.b_to_a_transferred == 1

    def test_bidirectional_sync_is_idempotent(self) -> None:
        from clinical_core.sync.engine import SyncNode, SyncEngine

        store_a = InMemoryEventStore()
        store_b = InMemoryEventStore()
        disp_a = EventDispatcher()
        disp_b = EventDispatcher()

        store_a.append(_event("test.FromA", uuid4(), 1, device_id="device-a"))
        store_b.append(_event("test.FromB", uuid4(), 1, device_id="device-b"))

        node_a = SyncNode("node-a", store_a, disp_a)
        node_b = SyncNode("node-b", store_b, disp_b)
        engine = SyncEngine()

        r1 = engine.full_sync(node_a, node_b)
        r2 = engine.full_sync(node_a, node_b)

        assert r1.a_to_b_transferred == 1
        assert r1.b_to_a_transferred == 1
        assert r2.a_to_b_transferred == 0
        assert r2.b_to_a_transferred == 0

        assert len(store_a.read_all_events()) == 2
        assert len(store_b.read_all_events()) == 2

    def test_sync_result_has_summary(self) -> None:
        from clinical_core.sync.engine import SyncNode, SyncEngine

        store_a = InMemoryEventStore()
        store_b = InMemoryEventStore()
        disp_a = EventDispatcher()
        disp_b = EventDispatcher()

        store_a.append(_event("test.A", uuid4(), 1))

        node_a = SyncNode("node-a", store_a, disp_a)
        node_b = SyncNode("node-b", store_b, disp_b)
        engine = SyncEngine()

        result = engine.sync(source=node_a, target=node_b)

        assert result.transferred_count == 1
        assert result.duplicate_count == 0
