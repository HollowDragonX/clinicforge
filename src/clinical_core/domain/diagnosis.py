"""Diagnosis aggregate and ConfirmDiagnosis command.

The Diagnosis aggregate is a lifecycle aggregate that tracks a single
diagnosis through its states: unconfirmed → confirmed (→ revised → resolved
in future iterations).

ConfirmDiagnosis flow:
  1. DiagnosisCommandHandler checks encounter is active (INV-CJ-1, cross-aggregate).
  2. DiagnosisAggregate checks own invariants (not already confirmed).
  3. DiagnosisConfirmed event emitted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from clinical_core.domain.aggregate import Aggregate, DomainError
from clinical_core.domain.events import (
    ConnectionStatus,
    DomainEvent,
    EventMetadata,
)


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConfirmDiagnosis:
    """Command: intent to confirm a diagnosis for a patient during an encounter."""
    diagnosis_id: UUID
    encounter_id: UUID
    patient_id: UUID
    condition: str
    icd_code: str
    occurred_at: datetime
    performed_by: UUID
    performer_role: str
    organization_id: UUID
    facility_id: UUID
    device_id: str
    connection_status: ConnectionStatus
    correlation_id: UUID


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

class DiagnosisAggregate(Aggregate):
    """Lifecycle aggregate for a single diagnosis.

    States: unconfirmed → confirmed
    """

    @property
    def aggregate_type(self) -> str:
        return "Diagnosis"

    def initial_state(self) -> dict[str, Any]:
        return {
            "status": "unconfirmed",
            "condition": None,
            "icd_code": None,
            "patient_id": None,
            "encounter_id": None,
        }

    def apply_event(self, state: dict[str, Any], event: DomainEvent) -> dict[str, Any]:
        if event.event_type == "clinical.judgment.DiagnosisConfirmed":
            p = event.payload
            return {
                **state,
                "status": "confirmed",
                "condition": p.get("condition"),
                "icd_code": p.get("icd_code"),
                "patient_id": p.get("patient_id"),
                "encounter_id": p.get("encounter_id"),
            }
        return state

    def execute(self, state: dict[str, Any], command: Any) -> list[DomainEvent]:
        if isinstance(command, ConfirmDiagnosis):
            if state["status"] != "unconfirmed":
                raise DomainError("Diagnosis already confirmed")
            return [self._build_event(
                command,
                event_type="clinical.judgment.DiagnosisConfirmed",
                aggregate_id=command.diagnosis_id,
                payload={
                    "diagnosis_id": str(command.diagnosis_id),
                    "encounter_id": str(command.encounter_id),
                    "patient_id": str(command.patient_id),
                    "condition": command.condition,
                    "icd_code": command.icd_code,
                },
            )]
        raise DomainError(f"Unknown command: {type(command).__name__}")


# ---------------------------------------------------------------------------
# Specialized command handler with cross-aggregate encounter check
# ---------------------------------------------------------------------------

class DiagnosisCommandHandler:
    """Command handler for diagnosis commands.

    Extends the generic flow with a cross-aggregate precondition check:
    INV-CJ-1 — the referenced encounter must be in 'active' state.

    The encounter state is derived by replaying the encounter's event stream.
    This is an eventually consistent check (the encounter stream may be stale
    under offline operation).
    """

    _ENCOUNTER_ACTIVE_EVENTS = {
        "clinical.encounter.EncounterBegan",
    }
    _ENCOUNTER_DEACTIVE_EVENTS = {
        "clinical.encounter.EncounterCompleted",
        "clinical.encounter.PatientDischarged",
    }

    def __init__(
        self,
        event_store: Any,
        dispatcher: Any,
        aggregate: DiagnosisAggregate,
        encounter_store: Any,
    ) -> None:
        self._event_store = event_store
        self._dispatcher = dispatcher
        self._aggregate = aggregate
        self._encounter_store = encounter_store

    def handle(self, command: ConfirmDiagnosis, aggregate_id: UUID) -> list[DomainEvent]:
        # Cross-aggregate check: encounter must be active (INV-CJ-1)
        self._check_encounter_active(command.encounter_id)

        # Load diagnosis stream and rehydrate
        stream = self._event_store.read_stream(aggregate_id)
        state = self._aggregate.rehydrate(stream)
        current_version = self._event_store.stream_version(aggregate_id)

        # Execute domain logic
        new_events = self._aggregate.execute(state, command)

        # Persist with correct version
        persisted: list[DomainEvent] = []
        for i, event in enumerate(new_events):
            versioned = _set_version(event, current_version + i + 1)
            result = self._event_store.append(versioned)
            persisted.append(result)

        # Dispatch
        for event in persisted:
            self._dispatcher.dispatch(event)

        return persisted

    def _check_encounter_active(self, encounter_id: UUID) -> None:
        """Derive encounter state from its event stream and verify it's active."""
        enc_stream = self._encounter_store.read_stream(encounter_id)
        enc_status = "none"
        for event in enc_stream:
            if event.event_type in self._ENCOUNTER_ACTIVE_EVENTS:
                enc_status = "active"
            elif event.event_type in self._ENCOUNTER_DEACTIVE_EVENTS:
                enc_status = "completed"
            elif event.event_type == "clinical.encounter.PatientCheckedIn":
                enc_status = "checked_in"
            elif event.event_type == "clinical.encounter.EncounterReopened":
                enc_status = "active"
        if enc_status != "active":
            raise DomainError(
                f"Encounter {encounter_id} is not active (status: {enc_status}). "
                f"INV-CJ-1: encounter must be active to confirm a diagnosis."
            )


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
