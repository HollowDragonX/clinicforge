"""Integration test: Patient Summary Query.

Wires the full write→read flow:

  Command Gateway (write side)
  → DiagnosisCommandHandler → DiagnosisConfirmed event
  → EventDispatcher → PatientSummaryProjection (updated)
  → Query Gateway (read side)
  → PatientSummaryProjection.state → response mapper → formatted response

No mocks. Real implementations wired together.
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
from clinical_core.application.query_gateway import QueryGateway, QueryResult
from clinical_core.application.projections.patient_summary import PatientSummaryProjection
from clinical_core.domain.diagnosis import DiagnosisAggregate, DiagnosisCommandHandler


# ---------------------------------------------------------------------------
# Shared identities
# ---------------------------------------------------------------------------

_ORG_ID = uuid4()
_FACILITY_ID = uuid4()
_PATIENT_ID = uuid4()
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


def _patient_summary_mapper(
    state: dict[str, Any], params: dict[str, Any],
) -> dict[str, Any]:
    """Map PatientSummaryProjection state → external response DTO."""
    conditions = state.get("active_conditions", {})
    treatments = state.get("active_treatments", {})
    stopped = state.get("stopped_treatments", {})

    return {
        "active_conditions": [
            {
                "id": k,
                "condition": v["condition"],
                "icd_code": v["icd_code"],
                "patient_id": v.get("patient_id"),
            }
            for k, v in conditions.items()
        ],
        "active_treatments": [
            {
                "id": k,
                "treatment": v["treatment"],
                "diagnosis_id": v.get("diagnosis_id"),
                "patient_id": v.get("patient_id"),
            }
            for k, v in treatments.items()
        ],
        "stopped_treatments": [
            {"id": k, "reason": v.get("reason")}
            for k, v in stopped.items()
        ],
    }


def _wire_system() -> tuple[
    CommandGateway, QueryGateway, InMemoryEventStore, PatientSummaryProjection,
]:
    """Wire both gateways, store, dispatcher, and projection."""
    store = InMemoryEventStore()
    dispatcher = EventDispatcher()

    # Projection
    projection = PatientSummaryProjection()
    for event_type in projection.subscribed_event_types:
        dispatcher.subscribe(event_type, projection.handle)

    # Command gateway
    handler = DiagnosisCommandHandler(
        event_store=store,
        dispatcher=dispatcher,
        aggregate=DiagnosisAggregate(),
        encounter_store=store,
    )
    cmd_gateway = CommandGateway()
    cmd_gateway.register(
        "ConfirmDiagnosis", handler=handler, aggregate_id_field="diagnosis_id",
    )

    # Query gateway
    qry_gateway = QueryGateway()
    qry_gateway.register(
        query_type="PatientSummary",
        projection=projection,
        mapper=_patient_summary_mapper,
    )

    return cmd_gateway, qry_gateway, store, projection


def _setup_active_encounter(store: InMemoryEventStore) -> UUID:
    enc_id = uuid4()
    store.append(_encounter_event(
        enc_id, "clinical.encounter.PatientCheckedIn", 1,
        {"patient_id": str(_PATIENT_ID)},
    ))
    store.append(_encounter_event(
        enc_id, "clinical.encounter.EncounterBegan", 2,
        {"practitioner_id": str(_DOCTOR_ID)},
    ))
    return enc_id


def _make_diagnosis_request(
    encounter_id: UUID,
    condition: str = "Hypertension",
    icd_code: str = "I10",
) -> dict[str, Any]:
    return {
        "command_type": "ConfirmDiagnosis",
        "payload": {
            "diagnosis_id": str(uuid4()),
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
# Tests: Patient Summary Query via QueryGateway
# ---------------------------------------------------------------------------

class TestPatientSummaryQuery:
    """Write data via CommandGateway, read via QueryGateway."""

    def test_query_empty_projection(self) -> None:
        _, qry_gw, _, _ = _wire_system()

        result = qry_gw.handle({"query_type": "PatientSummary"})

        assert result.success is True
        assert result.data["active_conditions"] == []
        assert result.data["active_treatments"] == []

    def test_query_after_single_diagnosis(self) -> None:
        cmd_gw, qry_gw, store, _ = _wire_system()
        enc_id = _setup_active_encounter(store)

        cmd_gw.handle(_make_diagnosis_request(enc_id, "Hypertension", "I10"))

        result = qry_gw.handle({"query_type": "PatientSummary"})

        assert result.success is True
        conditions = result.data["active_conditions"]
        assert len(conditions) == 1
        assert conditions[0]["condition"] == "Hypertension"
        assert conditions[0]["icd_code"] == "I10"
        assert conditions[0]["patient_id"] == str(_PATIENT_ID)

    def test_query_after_multiple_diagnoses(self) -> None:
        cmd_gw, qry_gw, store, _ = _wire_system()
        enc_id = _setup_active_encounter(store)

        cmd_gw.handle(_make_diagnosis_request(enc_id, "Hypertension", "I10"))
        cmd_gw.handle(_make_diagnosis_request(enc_id, "Type 2 Diabetes", "E11"))
        cmd_gw.handle(_make_diagnosis_request(enc_id, "Asthma", "J45"))

        result = qry_gw.handle({"query_type": "PatientSummary"})

        conditions = result.data["active_conditions"]
        assert len(conditions) == 3

        names = {c["condition"] for c in conditions}
        assert names == {"Hypertension", "Type 2 Diabetes", "Asthma"}

    def test_query_returns_formatted_response(self) -> None:
        cmd_gw, qry_gw, store, _ = _wire_system()
        enc_id = _setup_active_encounter(store)

        cmd_gw.handle(_make_diagnosis_request(enc_id, "Hypertension", "I10"))

        result = qry_gw.handle({"query_type": "PatientSummary"})

        assert isinstance(result, QueryResult)
        assert isinstance(result.data, dict)
        assert "active_conditions" in result.data
        assert "active_treatments" in result.data
        assert "stopped_treatments" in result.data

    def test_condition_dto_has_id_field(self) -> None:
        cmd_gw, qry_gw, store, _ = _wire_system()
        enc_id = _setup_active_encounter(store)

        cmd_gw.handle(_make_diagnosis_request(enc_id, "Hypertension", "I10"))

        result = qry_gw.handle({"query_type": "PatientSummary"})

        condition = result.data["active_conditions"][0]
        assert "id" in condition
        assert len(condition["id"]) > 0

    def test_write_then_read_consistency(self) -> None:
        """Data written via command gateway is immediately visible via query gateway."""
        cmd_gw, qry_gw, store, _ = _wire_system()
        enc_id = _setup_active_encounter(store)

        # Before write
        before = qry_gw.handle({"query_type": "PatientSummary"})
        assert before.data["active_conditions"] == []

        # Write
        cmd_gw.handle(_make_diagnosis_request(enc_id, "Hypertension", "I10"))

        # After write
        after = qry_gw.handle({"query_type": "PatientSummary"})
        assert len(after.data["active_conditions"]) == 1

    def test_failed_command_leaves_query_unchanged(self) -> None:
        cmd_gw, qry_gw, store, _ = _wire_system()

        # No active encounter — command will fail
        cmd_gw.handle(_make_diagnosis_request(uuid4()))

        result = qry_gw.handle({"query_type": "PatientSummary"})

        assert result.success is True
        assert result.data["active_conditions"] == []
