"""Integration test: full end-to-end flow from external request to projection.

Simulates the complete lifecycle:

  External request (dict)
  → Command Gateway (validate, map, route)
  → DiagnosisCommandHandler (cross-aggregate check, domain logic)
  → DiagnosisConfirmed event emitted
  → Event persisted to event store
  → Event dispatched to PatientSummaryProjection
  → Projection state updated

This test wires real implementations together — no mocks, no stubs.
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
from clinical_core.application.gateway import CommandGateway, GatewayResult
from clinical_core.application.projections.patient_summary import PatientSummaryProjection
from clinical_core.domain.diagnosis import DiagnosisAggregate, DiagnosisCommandHandler


# ---------------------------------------------------------------------------
# Shared identities
# ---------------------------------------------------------------------------

_ORG_ID = uuid4()
_FACILITY_ID = uuid4()
_PATIENT_ID = uuid4()
_ENCOUNTER_ID = uuid4()
_DOCTOR_ID = uuid4()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _encounter_event(
    enc_id: UUID, event_type: str, version: int, payload: dict | None = None,
) -> DomainEvent:
    return DomainEvent(
        metadata=EventMetadata(
            event_id=uuid4(),
            event_type=event_type,
            schema_version=1,
            aggregate_id=enc_id,
            aggregate_type="Encounter",
            aggregate_version=version,
            occurred_at=datetime.now(timezone.utc),
            performed_by=_DOCTOR_ID,
            performer_role="physician",
            organization_id=_ORG_ID,
            facility_id=_FACILITY_ID,
            device_id="exam-room-tablet",
            connection_status=ConnectionStatus.ONLINE,
            correlation_id=uuid4(),
        ),
        payload=payload or {},
    )


def _wire_system() -> tuple[
    CommandGateway, InMemoryEventStore, EventDispatcher, PatientSummaryProjection
]:
    """Wire the full system: gateway + handler + store + dispatcher + projection."""
    store = InMemoryEventStore()
    dispatcher = EventDispatcher()

    # Wire projection to dispatcher
    projection = PatientSummaryProjection()
    for event_type in projection.subscribed_event_types:
        dispatcher.subscribe(event_type, projection.handle)

    # Wire command handler
    handler = DiagnosisCommandHandler(
        event_store=store,
        dispatcher=dispatcher,
        aggregate=DiagnosisAggregate(),
        encounter_store=store,
    )

    # Wire gateway
    gateway = CommandGateway()
    gateway.register("ConfirmDiagnosis", handler=handler, aggregate_id_field="diagnosis_id")

    return gateway, store, dispatcher, projection


def _setup_active_encounter(store: InMemoryEventStore) -> UUID:
    """Create an active encounter in the store. Returns encounter_id."""
    enc_id = _ENCOUNTER_ID
    store.append(_encounter_event(
        enc_id, "clinical.encounter.PatientCheckedIn", 1,
        {"patient_id": str(_PATIENT_ID)},
    ))
    store.append(_encounter_event(
        enc_id, "clinical.encounter.EncounterBegan", 2,
        {"practitioner_id": str(_DOCTOR_ID)},
    ))
    return enc_id


def _make_request(
    encounter_id: UUID,
    diagnosis_id: UUID | None = None,
    condition: str = "Hypertension",
    icd_code: str = "I10",
) -> dict[str, Any]:
    return {
        "command_type": "ConfirmDiagnosis",
        "payload": {
            "diagnosis_id": str(diagnosis_id or uuid4()),
            "encounter_id": str(encounter_id),
            "patient_id": str(_PATIENT_ID),
            "condition": condition,
            "icd_code": icd_code,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "performed_by": str(_DOCTOR_ID),
            "performer_role": "physician",
            "organization_id": str(_ORG_ID),
            "facility_id": str(_FACILITY_ID),
            "device_id": "exam-room-tablet",
            "connection_status": "online",
            "correlation_id": str(uuid4()),
        },
    }


# ---------------------------------------------------------------------------
# Integration test: full flow
# ---------------------------------------------------------------------------

class TestConfirmDiagnosisIntegration:
    """End-to-end: request → gateway → handler → event → projection."""

    def test_full_flow_succeeds(self) -> None:
        gateway, store, dispatcher, projection = _wire_system()
        enc_id = _setup_active_encounter(store)

        request = _make_request(enc_id)
        result = gateway.handle(request)

        assert result.success is True
        assert len(result.events) == 1
        assert result.events[0].event_type == "clinical.judgment.DiagnosisConfirmed"

    def test_event_is_persisted(self) -> None:
        gateway, store, dispatcher, projection = _wire_system()
        enc_id = _setup_active_encounter(store)
        diag_id = uuid4()

        request = _make_request(enc_id, diagnosis_id=diag_id)
        result = gateway.handle(request)

        stream = store.read_stream(diag_id)
        assert len(stream) == 1
        assert stream[0].event_id == result.events[0].event_id
        assert stream[0].aggregate_version == 1

    def test_projection_updated_with_diagnosis(self) -> None:
        gateway, store, dispatcher, projection = _wire_system()
        enc_id = _setup_active_encounter(store)

        request = _make_request(enc_id, condition="Hypertension", icd_code="I10")
        result = gateway.handle(request)

        # Projection should now have the diagnosis
        conditions = projection.state.get("active_conditions", {})
        assert len(conditions) == 1

        diag_entry = list(conditions.values())[0]
        assert diag_entry["condition"] == "Hypertension"
        assert diag_entry["icd_code"] == "I10"
        assert diag_entry["patient_id"] == str(_PATIENT_ID)

    def test_multiple_diagnoses_accumulate_in_projection(self) -> None:
        gateway, store, dispatcher, projection = _wire_system()
        enc_id = _setup_active_encounter(store)

        gateway.handle(_make_request(enc_id, condition="Hypertension", icd_code="I10"))
        gateway.handle(_make_request(enc_id, condition="Type 2 Diabetes", icd_code="E11"))

        conditions = projection.state.get("active_conditions", {})
        assert len(conditions) == 2

        condition_names = {c["condition"] for c in conditions.values()}
        assert condition_names == {"Hypertension", "Type 2 Diabetes"}

    def test_failed_request_does_not_update_projection(self) -> None:
        gateway, store, dispatcher, projection = _wire_system()
        # No active encounter — command will fail

        request = _make_request(uuid4())
        result = gateway.handle(request)

        assert result.success is False
        conditions = projection.state.get("active_conditions", {})
        assert len(conditions) == 0

    def test_event_metadata_is_complete(self) -> None:
        gateway, store, dispatcher, projection = _wire_system()
        enc_id = _setup_active_encounter(store)

        request = _make_request(enc_id)
        result = gateway.handle(request)

        event = result.events[0]
        assert event.event_type == "clinical.judgment.DiagnosisConfirmed"
        assert event.aggregate_type == "Diagnosis"
        assert event.aggregate_version == 1
        assert event.recorded_at is not None
        assert event.metadata.performed_by == _DOCTOR_ID
        assert event.metadata.performer_role == "physician"
        assert event.metadata.organization_id == _ORG_ID
        assert event.metadata.facility_id == _FACILITY_ID
        assert event.metadata.device_id == "exam-room-tablet"

    def test_event_payload_matches_request(self) -> None:
        gateway, store, dispatcher, projection = _wire_system()
        enc_id = _setup_active_encounter(store)
        diag_id = uuid4()

        request = _make_request(
            enc_id, diagnosis_id=diag_id, condition="Asthma", icd_code="J45",
        )
        result = gateway.handle(request)

        payload = result.events[0].payload
        assert payload["condition"] == "Asthma"
        assert payload["icd_code"] == "J45"
        assert payload["diagnosis_id"] == str(diag_id)
        assert payload["encounter_id"] == str(enc_id)
        assert payload["patient_id"] == str(_PATIENT_ID)

    def test_projection_survives_rebuild_from_store(self) -> None:
        """After the flow, rebuilding projection from store events matches."""
        gateway, store, dispatcher, projection = _wire_system()
        enc_id = _setup_active_encounter(store)

        gateway.handle(_make_request(enc_id, condition="Hypertension", icd_code="I10"))
        gateway.handle(_make_request(enc_id, condition="Asthma", icd_code="J45"))

        # Live projection state
        live_conditions = projection.state.get("active_conditions", {})
        assert len(live_conditions) == 2

        # Rebuild fresh projection from all events in store
        fresh = PatientSummaryProjection()
        fresh.rebuild_from(store.read_all_events())

        assert fresh.state == projection.state

    def test_gateway_result_type(self) -> None:
        gateway, store, dispatcher, projection = _wire_system()
        enc_id = _setup_active_encounter(store)

        result = gateway.handle(_make_request(enc_id))

        assert isinstance(result, GatewayResult)
        assert result.success is True
        assert isinstance(result.events, list)
        assert result.error == ""
