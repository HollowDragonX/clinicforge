"""TDD tests for the ConfirmDiagnosis command.

Flow:
  ConfirmDiagnosis command
  → Encounter state checked (cross-aggregate, eventually consistent)
  → Diagnosis aggregate validates own invariants
  → DiagnosisConfirmed event emitted

Invariants tested:
- INV-CJ-1: Encounter must be active before a diagnosis can be confirmed.
- Diagnosis cannot be confirmed twice on the same aggregate.
- DiagnosisConfirmed event carries correct clinical payload.
- Full round-trip: command → persist → dispatch → projection update.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from clinical_core.domain.aggregate import Aggregate, DomainError
from clinical_core.domain.events import (
    ConnectionStatus,
    DomainEvent,
    EventMetadata,
)
from clinical_core.infrastructure.in_memory_event_store import InMemoryEventStore
from clinical_core.application.command_handler import CommandHandler
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
    payload: dict | None = None,
) -> DomainEvent:
    return DomainEvent(
        metadata=EventMetadata(
            event_id=uuid4(),
            event_type=event_type,
            schema_version=1,
            aggregate_id=aggregate_id,
            aggregate_type="Encounter",
            aggregate_version=aggregate_version,
            occurred_at=datetime.now(timezone.utc),
            performed_by=uuid4(),
            performer_role="physician",
            organization_id=_ORG_ID,
            facility_id=_FACILITY_ID,
            device_id="device-001",
            connection_status=ConnectionStatus.ONLINE,
            correlation_id=uuid4(),
        ),
        payload=payload or {},
    )


def _confirm_diagnosis_cmd(
    diagnosis_id: UUID | None = None,
    encounter_id: UUID | None = None,
    patient_id: UUID | None = None,
    condition: str = "Hypertension",
    icd_code: str = "I10",
) -> "ConfirmDiagnosis":
    from clinical_core.domain.diagnosis import ConfirmDiagnosis

    return ConfirmDiagnosis(
        diagnosis_id=diagnosis_id or uuid4(),
        encounter_id=encounter_id or uuid4(),
        patient_id=patient_id or uuid4(),
        condition=condition,
        icd_code=icd_code,
        occurred_at=datetime.now(timezone.utc),
        performed_by=uuid4(),
        performer_role="physician",
        organization_id=_ORG_ID,
        facility_id=_FACILITY_ID,
        device_id="device-001",
        connection_status=ConnectionStatus.ONLINE,
        correlation_id=uuid4(),
    )


def _setup_active_encounter(store: InMemoryEventStore, enc_id: UUID) -> None:
    """Persist encounter events so the encounter is in 'active' state."""
    patient_id = str(uuid4())
    store.append(_event(
        "clinical.encounter.PatientCheckedIn", enc_id, 1,
        {"patient_id": patient_id},
    ))
    store.append(_event(
        "clinical.encounter.EncounterBegan", enc_id, 2,
        {"practitioner_id": str(uuid4())},
    ))


class SpyHandler:
    def __init__(self) -> None:
        self.received: list[DomainEvent] = []

    def __call__(self, event: DomainEvent) -> None:
        self.received.append(event)


# ---------------------------------------------------------------------------
# Tests: Diagnosis aggregate — rehydration
# ---------------------------------------------------------------------------

class TestDiagnosisAggregateRehydration:

    def test_initial_state_is_unconfirmed(self) -> None:
        from clinical_core.domain.diagnosis import DiagnosisAggregate

        agg = DiagnosisAggregate()
        state = agg.initial_state()
        assert state["status"] == "unconfirmed"

    def test_rehydrate_from_confirmed_event(self) -> None:
        from clinical_core.domain.diagnosis import DiagnosisAggregate

        agg = DiagnosisAggregate()
        diag_id = uuid4()
        event = DomainEvent(
            metadata=EventMetadata(
                event_id=uuid4(),
                event_type="clinical.judgment.DiagnosisConfirmed",
                schema_version=1,
                aggregate_id=diag_id,
                aggregate_type="Diagnosis",
                aggregate_version=1,
                occurred_at=datetime.now(timezone.utc),
                performed_by=uuid4(),
                performer_role="physician",
                organization_id=_ORG_ID,
                facility_id=_FACILITY_ID,
                device_id="device-001",
                connection_status=ConnectionStatus.ONLINE,
                correlation_id=uuid4(),
            ),
            payload={
                "diagnosis_id": str(diag_id),
                "encounter_id": str(uuid4()),
                "patient_id": str(uuid4()),
                "condition": "Hypertension",
                "icd_code": "I10",
            },
        )
        state = agg.rehydrate([event])
        assert state["status"] == "confirmed"
        assert state["condition"] == "Hypertension"
        assert state["icd_code"] == "I10"


# ---------------------------------------------------------------------------
# Tests: Diagnosis aggregate — domain invariants
# ---------------------------------------------------------------------------

class TestDiagnosisInvariants:

    def test_confirm_produces_event(self) -> None:
        from clinical_core.domain.diagnosis import DiagnosisAggregate

        agg = DiagnosisAggregate()
        cmd = _confirm_diagnosis_cmd()
        events = agg.execute(agg.initial_state(), cmd)

        assert len(events) == 1
        assert events[0].event_type == "clinical.judgment.DiagnosisConfirmed"

    def test_confirm_event_carries_clinical_payload(self) -> None:
        from clinical_core.domain.diagnosis import DiagnosisAggregate

        agg = DiagnosisAggregate()
        patient_id = uuid4()
        encounter_id = uuid4()
        cmd = _confirm_diagnosis_cmd(
            patient_id=patient_id,
            encounter_id=encounter_id,
            condition="Type 2 Diabetes",
            icd_code="E11",
        )
        events = agg.execute(agg.initial_state(), cmd)
        payload = events[0].payload

        assert payload["condition"] == "Type 2 Diabetes"
        assert payload["icd_code"] == "E11"
        assert payload["patient_id"] == str(patient_id)
        assert payload["encounter_id"] == str(encounter_id)

    def test_cannot_confirm_already_confirmed_diagnosis(self) -> None:
        """INV: A diagnosis aggregate can only be confirmed once."""
        from clinical_core.domain.diagnosis import DiagnosisAggregate

        agg = DiagnosisAggregate()
        diag_id = uuid4()
        cmd = _confirm_diagnosis_cmd(diagnosis_id=diag_id)
        events = agg.execute(agg.initial_state(), cmd)
        confirmed_state = agg.rehydrate(events)

        with pytest.raises(DomainError, match="already confirmed"):
            agg.execute(confirmed_state, _confirm_diagnosis_cmd(diagnosis_id=diag_id))

    def test_aggregate_type_is_diagnosis(self) -> None:
        from clinical_core.domain.diagnosis import DiagnosisAggregate

        agg = DiagnosisAggregate()
        assert agg.aggregate_type == "Diagnosis"


# ---------------------------------------------------------------------------
# Tests: INV-CJ-1 — Encounter must be active
# ---------------------------------------------------------------------------

class TestEncounterInvariant:
    """Cross-aggregate check: encounter must be active for diagnosis."""

    def test_confirm_succeeds_when_encounter_active(self) -> None:
        from clinical_core.domain.diagnosis import DiagnosisAggregate, DiagnosisCommandHandler

        store = InMemoryEventStore()
        dispatcher = EventDispatcher()
        enc_id = uuid4()
        _setup_active_encounter(store, enc_id)

        handler = DiagnosisCommandHandler(
            event_store=store,
            dispatcher=dispatcher,
            aggregate=DiagnosisAggregate(),
            encounter_store=store,
        )

        diag_id = uuid4()
        cmd = _confirm_diagnosis_cmd(diagnosis_id=diag_id, encounter_id=enc_id)
        result = handler.handle(cmd, aggregate_id=diag_id)

        assert len(result) == 1
        assert result[0].event_type == "clinical.judgment.DiagnosisConfirmed"

    def test_confirm_rejected_when_encounter_not_started(self) -> None:
        """Encounter exists but is only checked-in, not active."""
        from clinical_core.domain.diagnosis import DiagnosisAggregate, DiagnosisCommandHandler

        store = InMemoryEventStore()
        dispatcher = EventDispatcher()
        enc_id = uuid4()

        # Only check in, don't begin the encounter
        store.append(_event(
            "clinical.encounter.PatientCheckedIn", enc_id, 1,
            {"patient_id": str(uuid4())},
        ))

        handler = DiagnosisCommandHandler(
            event_store=store,
            dispatcher=dispatcher,
            aggregate=DiagnosisAggregate(),
            encounter_store=store,
        )

        diag_id = uuid4()
        cmd = _confirm_diagnosis_cmd(diagnosis_id=diag_id, encounter_id=enc_id)

        with pytest.raises(DomainError, match="[Ee]ncounter.*not active"):
            handler.handle(cmd, aggregate_id=diag_id)

    def test_confirm_rejected_when_encounter_completed(self) -> None:
        from clinical_core.domain.diagnosis import DiagnosisAggregate, DiagnosisCommandHandler

        store = InMemoryEventStore()
        dispatcher = EventDispatcher()
        enc_id = uuid4()

        _setup_active_encounter(store, enc_id)
        store.append(_event(
            "clinical.encounter.EncounterCompleted", enc_id, 3,
            {"completed_at": datetime.now(timezone.utc).isoformat()},
        ))

        handler = DiagnosisCommandHandler(
            event_store=store,
            dispatcher=dispatcher,
            aggregate=DiagnosisAggregate(),
            encounter_store=store,
        )

        diag_id = uuid4()
        cmd = _confirm_diagnosis_cmd(diagnosis_id=diag_id, encounter_id=enc_id)

        with pytest.raises(DomainError, match="[Ee]ncounter.*not active"):
            handler.handle(cmd, aggregate_id=diag_id)

    def test_confirm_rejected_when_encounter_does_not_exist(self) -> None:
        from clinical_core.domain.diagnosis import DiagnosisAggregate, DiagnosisCommandHandler

        store = InMemoryEventStore()
        dispatcher = EventDispatcher()

        handler = DiagnosisCommandHandler(
            event_store=store,
            dispatcher=dispatcher,
            aggregate=DiagnosisAggregate(),
            encounter_store=store,
        )

        diag_id = uuid4()
        cmd = _confirm_diagnosis_cmd(diagnosis_id=diag_id, encounter_id=uuid4())

        with pytest.raises(DomainError, match="[Ee]ncounter.*not active"):
            handler.handle(cmd, aggregate_id=diag_id)


# ---------------------------------------------------------------------------
# Tests: Full round-trip
# ---------------------------------------------------------------------------

class TestFullRoundTrip:

    def test_command_persists_and_dispatches(self) -> None:
        from clinical_core.domain.diagnosis import DiagnosisAggregate, DiagnosisCommandHandler

        store = InMemoryEventStore()
        dispatcher = EventDispatcher()
        spy = SpyHandler()
        dispatcher.subscribe("clinical.judgment.DiagnosisConfirmed", spy)

        enc_id = uuid4()
        _setup_active_encounter(store, enc_id)

        handler = DiagnosisCommandHandler(
            event_store=store,
            dispatcher=dispatcher,
            aggregate=DiagnosisAggregate(),
            encounter_store=store,
        )

        diag_id = uuid4()
        cmd = _confirm_diagnosis_cmd(diagnosis_id=diag_id, encounter_id=enc_id)
        result = handler.handle(cmd, aggregate_id=diag_id)

        # Persisted
        stream = store.read_stream(diag_id)
        assert len(stream) == 1
        assert stream[0].event_type == "clinical.judgment.DiagnosisConfirmed"
        assert stream[0].aggregate_version == 1

        # Dispatched
        assert len(spy.received) == 1
        assert spy.received[0].event_id == result[0].event_id

    def test_rejected_command_leaves_no_trace(self) -> None:
        from clinical_core.domain.diagnosis import DiagnosisAggregate, DiagnosisCommandHandler

        store = InMemoryEventStore()
        dispatcher = EventDispatcher()
        spy = SpyHandler()
        dispatcher.subscribe("clinical.judgment.DiagnosisConfirmed", spy)

        handler = DiagnosisCommandHandler(
            event_store=store,
            dispatcher=dispatcher,
            aggregate=DiagnosisAggregate(),
            encounter_store=store,
        )

        diag_id = uuid4()
        cmd = _confirm_diagnosis_cmd(diagnosis_id=diag_id, encounter_id=uuid4())

        with pytest.raises(DomainError):
            handler.handle(cmd, aggregate_id=diag_id)

        assert store.stream_version(diag_id) == 0
        assert len(spy.received) == 0

    def test_diagnosis_rehydrates_after_persist(self) -> None:
        """After confirming, rehydrating from the store yields confirmed state."""
        from clinical_core.domain.diagnosis import DiagnosisAggregate, DiagnosisCommandHandler

        store = InMemoryEventStore()
        dispatcher = EventDispatcher()
        enc_id = uuid4()
        _setup_active_encounter(store, enc_id)

        agg = DiagnosisAggregate()
        handler = DiagnosisCommandHandler(
            event_store=store,
            dispatcher=dispatcher,
            aggregate=agg,
            encounter_store=store,
        )

        diag_id = uuid4()
        cmd = _confirm_diagnosis_cmd(
            diagnosis_id=diag_id,
            encounter_id=enc_id,
            condition="Asthma",
            icd_code="J45",
        )
        handler.handle(cmd, aggregate_id=diag_id)

        # Rehydrate from store
        stream = store.read_stream(diag_id)
        state = agg.rehydrate(stream)

        assert state["status"] == "confirmed"
        assert state["condition"] == "Asthma"
        assert state["icd_code"] == "J45"
