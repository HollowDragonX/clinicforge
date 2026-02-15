"""Domain event value objects and metadata.

Events are immutable records of clinical facts. They carry mandatory metadata
as defined in docs/clinical-event-invariants-and-metadata.md.

This module defines:
- EventMetadata: the 17 mandatory metadata fields every event must carry.
- DomainEvent: the immutable event envelope (metadata + payload).
- ConcurrencyError: raised when aggregate version conflicts are detected.
- EventValidationError: raised when event metadata is malformed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID


class ConnectionStatus(Enum):
    ONLINE = "online"
    OFFLINE = "offline"


class EventValidationError(Exception):
    """Raised when an event fails metadata validation (pipeline Stage 1)."""


class ConcurrencyError(Exception):
    """Raised when aggregate version does not match expected next version (INV-XX-3)."""

    def __init__(self, aggregate_id: UUID, expected_version: int, actual_version: int) -> None:
        self.aggregate_id = aggregate_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"Concurrency conflict on aggregate {aggregate_id}: "
            f"expected version {expected_version}, got {actual_version}"
        )


@dataclass(frozen=True)
class EventMetadata:
    """Mandatory metadata for every clinical event.

    All 17 fields as specified in the metadata design document.
    """

    # Identity fields
    event_id: UUID
    event_type: str
    schema_version: int

    # Aggregate fields
    aggregate_id: UUID
    aggregate_type: str
    aggregate_version: int

    # Temporal fields
    occurred_at: datetime

    # Actor fields
    performed_by: UUID
    performer_role: str

    # Organizational context fields
    organization_id: UUID
    facility_id: UUID

    # Device & sync fields
    device_id: str
    connection_status: ConnectionStatus

    # Traceability fields
    correlation_id: UUID

    # --- Fields with defaults must follow fields without defaults ---

    recorded_at: datetime | None = None  # Set by event store at persist time
    causation_id: UUID | None = None  # Nullable for root events
    visibility: tuple[str, ...] = ("clinical_staff",)


@dataclass(frozen=True)
class DomainEvent:
    """An immutable clinical event: metadata envelope + domain-specific payload.

    The payload is an unstructured dict because event schemas are defined per
    event type and versioned via schema_version. The event store does not
    interpret the payload — it stores it opaquely.
    """

    metadata: EventMetadata
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def event_id(self) -> UUID:
        return self.metadata.event_id

    @property
    def event_type(self) -> str:
        return self.metadata.event_type

    @property
    def aggregate_id(self) -> UUID:
        return self.metadata.aggregate_id

    @property
    def aggregate_type(self) -> str:
        return self.metadata.aggregate_type

    @property
    def aggregate_version(self) -> int:
        return self.metadata.aggregate_version

    @property
    def occurred_at(self) -> datetime:
        return self.metadata.occurred_at

    @property
    def recorded_at(self) -> datetime | None:
        return self.metadata.recorded_at

    def with_recorded_at(self, timestamp: datetime) -> DomainEvent:
        """Return a new event with recorded_at set. Used by event store at persist time."""
        new_metadata = EventMetadata(
            event_id=self.metadata.event_id,
            event_type=self.metadata.event_type,
            schema_version=self.metadata.schema_version,
            aggregate_id=self.metadata.aggregate_id,
            aggregate_type=self.metadata.aggregate_type,
            aggregate_version=self.metadata.aggregate_version,
            occurred_at=self.metadata.occurred_at,
            performed_by=self.metadata.performed_by,
            performer_role=self.metadata.performer_role,
            organization_id=self.metadata.organization_id,
            facility_id=self.metadata.facility_id,
            device_id=self.metadata.device_id,
            connection_status=self.metadata.connection_status,
            correlation_id=self.metadata.correlation_id,
            recorded_at=timestamp,
            causation_id=self.metadata.causation_id,
            visibility=self.metadata.visibility,
        )
        return DomainEvent(metadata=new_metadata, payload=self.payload)
