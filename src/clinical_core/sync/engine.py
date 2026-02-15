"""Sync Engine — local simulation of node-to-node event synchronization.

No networking layer. Sync is performed via direct method calls between
SyncNode instances, each wrapping an InMemoryEventStore and EventDispatcher.

The engine implements the four sync operations:
1. Exchange known event positions (event_count, known_event_ids).
2. Detect missing events (set difference on event IDs).
3. Transfer missing events (append to target store).
4. Trigger projection updates (dispatch received events on target).

All operations are idempotent — syncing twice produces no duplicates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from clinical_core.domain.events import DomainEvent


@dataclass(frozen=True)
class SyncResult:
    """Result of a one-directional sync (source → target)."""
    transferred_count: int
    duplicate_count: int


@dataclass(frozen=True)
class FullSyncResult:
    """Result of a bidirectional sync between two nodes."""
    a_to_b_transferred: int
    b_to_a_transferred: int
    a_to_b_duplicates: int
    b_to_a_duplicates: int


class SyncNode:
    """A sync-capable node wrapping an event store and dispatcher.

    Each node represents an independent device with its own local
    event store and projection dispatcher.
    """

    def __init__(
        self,
        node_id: str,
        event_store: Any,
        dispatcher: Any,
    ) -> None:
        self.node_id = node_id
        self.event_store = event_store
        self.dispatcher = dispatcher

    def event_count(self) -> int:
        """Total number of events in this node's store."""
        return len(self.event_store.read_all_events())

    def known_event_ids(self) -> set[UUID]:
        """Set of all event IDs this node has."""
        return {e.event_id for e in self.event_store.read_all_events()}

    def all_events(self) -> list[DomainEvent]:
        """All events in insertion order."""
        return self.event_store.read_all_events()

    def receive_event(self, event: DomainEvent) -> bool:
        """Receive an event from sync. Returns True if new, False if duplicate.

        Appends to the local store and dispatches to projections if new.
        Duplicates are silently skipped (idempotent).
        """
        if self.event_store.event_exists(event.event_id):
            return False

        self.event_store.append(event)
        self.dispatcher.dispatch(event)
        return True


class SyncEngine:
    """Orchestrates sync between two SyncNodes.

    No networking — direct method calls. The engine:
    1. Compares known event IDs between source and target.
    2. Identifies events the target is missing.
    3. Transfers missing events to the target.
    4. Triggers projection dispatch on the target for new events.
    """

    def detect_missing(self, source: SyncNode, target: SyncNode) -> list[DomainEvent]:
        """Detect events that source has but target lacks."""
        target_ids = target.known_event_ids()
        return [e for e in source.all_events() if e.event_id not in target_ids]

    def sync(self, source: SyncNode, target: SyncNode) -> SyncResult:
        """One-directional sync: source → target.

        Returns a SyncResult with counts of transferred and duplicate events.
        """
        missing = self.detect_missing(source, target)
        transferred = 0
        duplicates = 0

        for event in missing:
            if target.receive_event(event):
                transferred += 1
            else:
                duplicates += 1

        # Events already in target that were not in the missing list
        # are also duplicates (detected during detect_missing).
        source_ids = source.known_event_ids()
        target_ids = target.known_event_ids()
        already_had = source_ids & (target_ids - {e.event_id for e in missing})
        duplicates += len(already_had)

        return SyncResult(
            transferred_count=transferred,
            duplicate_count=duplicates,
        )

    def full_sync(self, node_a: SyncNode, node_b: SyncNode) -> FullSyncResult:
        """Bidirectional sync: A ↔ B.

        Syncs A → B, then B → A.
        """
        a_to_b = self.sync(source=node_a, target=node_b)
        b_to_a = self.sync(source=node_b, target=node_a)

        return FullSyncResult(
            a_to_b_transferred=a_to_b.transferred_count,
            b_to_a_transferred=b_to_a.transferred_count,
            a_to_b_duplicates=a_to_b.duplicate_count,
            b_to_a_duplicates=b_to_a.duplicate_count,
        )
