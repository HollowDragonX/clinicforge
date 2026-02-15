"""End-to-end clinical flow test.

Scenario:
  1) Start encounter  — patient checks in, encounter begins
  2) Record observation — vital signs recorded
  3) Confirm diagnosis — via Command Gateway
  4) Query patient summary — via Query Gateway

Expected: Projection reflects clinical reality.

This test wires the full system: event store, dispatcher, projection,
command gateway, and query gateway. Encounter and observation events
are created directly (no aggregates for those yet). The diagnosis
flows through the Command Gateway. The query flows through the
Query Gateway.
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
# Clinical identities for this scenario
# ---------------------------------------------------------------------------

_ORG_ID = uuid4()
_FACILITY_ID = uuid4()
_PATIENT_ID = uuid4()
_DOCTOR_ID = uuid4()
_NURSE_ID = uuid4()
_ENCOUNTER_ID = uuid4()
_DIAGNOSIS_ID = uuid4()


# ---------------------------------------------------------------------------
# Event factory
# ---------------------------------------------------------------------------

def _clinical_event(
    aggregate_id: UUID,
    aggregate_type: str,
    event_type: str,
    version: int,
    payload: dict[str, Any],
    performed_by: UUID | None = None,
    performer_role: str = "physician",
    device_id: str = "exam-room-tablet",
) -> DomainEvent:
    return DomainEvent(
        metadata=EventMetadata(
            event_id=uuid4(),
            event_type=event_type,
            schema_version=1,
            aggregate_id=aggregate_id,
            aggregate_type=aggregate_type,
            aggregate_version=version,
            occurred_at=datetime.now(timezone.utc),
            performed_by=performed_by or _DOCTOR_ID,
            performer_role=performer_role,
            organization_id=_ORG_ID,
            facility_id=_FACILITY_ID,
            device_id=device_id,
            connection_status=ConnectionStatus.ONLINE,
            correlation_id=uuid4(),
        ),
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Response mapper
# ---------------------------------------------------------------------------

def _patient_summary_mapper(
    state: dict[str, Any], params: dict[str, Any],
) -> dict[str, Any]:
    """Map PatientSummaryProjection state → external response DTO."""
    conditions = state.get("active_conditions", {})
    treatments = state.get("active_treatments", {})
    stopped = state.get("stopped_treatments", {})
    vitals = state.get("vitals", [])

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
        "vitals": [
            {
                "recorded_at": v["recorded_at"],
                "readings": v["readings"],
                "patient_id": v.get("patient_id"),
                "encounter_id": v.get("encounter_id"),
            }
            for v in vitals
        ],
    }


# ---------------------------------------------------------------------------
# System wiring
# ---------------------------------------------------------------------------

def _wire_full_system() -> tuple[
    CommandGateway,
    QueryGateway,
    InMemoryEventStore,
    EventDispatcher,
    PatientSummaryProjection,
]:
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
    cmd_gw = CommandGateway()
    cmd_gw.register(
        "ConfirmDiagnosis", handler=handler, aggregate_id_field="diagnosis_id",
    )

    # Query gateway
    qry_gw = QueryGateway()
    qry_gw.register(
        query_type="PatientSummary",
        projection=projection,
        mapper=_patient_summary_mapper,
    )

    return cmd_gw, qry_gw, store, dispatcher, projection


# ---------------------------------------------------------------------------
# Clinical scenario execution
# ---------------------------------------------------------------------------

def _execute_clinical_scenario() -> tuple[
    CommandGateway,
    QueryGateway,
    InMemoryEventStore,
    EventDispatcher,
    PatientSummaryProjection,
    GatewayResult,
]:
    """Execute the full clinical scenario and return all components."""
    cmd_gw, qry_gw, store, dispatcher, projection = _wire_full_system()

    # ── Step 1: Start encounter ─────────────────────────────────────
    # Patient checks in
    store.append(_clinical_event(
        aggregate_id=_ENCOUNTER_ID,
        aggregate_type="Encounter",
        event_type="clinical.encounter.PatientCheckedIn",
        version=1,
        payload={"patient_id": str(_PATIENT_ID)},
        performed_by=_NURSE_ID,
        performer_role="nurse",
        device_id="front-desk-workstation",
    ))

    # Encounter begins (doctor enters room)
    store.append(_clinical_event(
        aggregate_id=_ENCOUNTER_ID,
        aggregate_type="Encounter",
        event_type="clinical.encounter.EncounterBegan",
        version=2,
        payload={
            "patient_id": str(_PATIENT_ID),
            "practitioner_id": str(_DOCTOR_ID),
        },
    ))

    # ── Step 2: Record observation ──────────────────────────────────
    # Nurse records vital signs
    vitals_event = _clinical_event(
        aggregate_id=uuid4(),
        aggregate_type="Observation",
        event_type="clinical.observation.VitalSignsRecorded",
        version=1,
        payload={
            "patient_id": str(_PATIENT_ID),
            "encounter_id": str(_ENCOUNTER_ID),
            "readings": {
                "systolic_bp": 158,
                "diastolic_bp": 95,
                "heart_rate": 82,
                "temperature_f": 98.6,
                "respiratory_rate": 16,
                "o2_saturation": 97,
            },
        },
        performed_by=_NURSE_ID,
        performer_role="nurse",
        device_id="vitals-monitor",
    )
    store.append(vitals_event)
    dispatcher.dispatch(vitals_event)

    # ── Step 3: Confirm diagnosis via Command Gateway ───────────────
    diag_result = cmd_gw.handle({
        "command_type": "ConfirmDiagnosis",
        "payload": {
            "diagnosis_id": str(_DIAGNOSIS_ID),
            "encounter_id": str(_ENCOUNTER_ID),
            "patient_id": str(_PATIENT_ID),
            "condition": "Essential Hypertension",
            "icd_code": "I10",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "performed_by": str(_DOCTOR_ID),
            "performer_role": "physician",
            "organization_id": str(_ORG_ID),
            "facility_id": str(_FACILITY_ID),
            "device_id": "exam-room-tablet",
            "connection_status": "online",
            "correlation_id": str(uuid4()),
        },
    })

    return cmd_gw, qry_gw, store, dispatcher, projection, diag_result


# ---------------------------------------------------------------------------
# Tests: Step verification
# ---------------------------------------------------------------------------

class TestClinicalFlowSteps:
    """Verify each step of the clinical flow executed correctly."""

    def test_encounter_started(self) -> None:
        _, _, store, _, _, _ = _execute_clinical_scenario()

        stream = store.read_stream(_ENCOUNTER_ID)
        event_types = [e.event_type for e in stream]

        assert "clinical.encounter.PatientCheckedIn" in event_types
        assert "clinical.encounter.EncounterBegan" in event_types

    def test_observation_recorded(self) -> None:
        _, _, store, _, _, _ = _execute_clinical_scenario()

        all_events = store.read_all_events()
        vitals_events = [
            e for e in all_events
            if e.event_type == "clinical.observation.VitalSignsRecorded"
        ]

        assert len(vitals_events) == 1
        assert vitals_events[0].payload["readings"]["systolic_bp"] == 158

    def test_diagnosis_confirmed(self) -> None:
        _, _, _, _, _, diag_result = _execute_clinical_scenario()

        assert diag_result.success is True
        assert len(diag_result.events) == 1
        assert diag_result.events[0].event_type == "clinical.judgment.DiagnosisConfirmed"

    def test_diagnosis_persisted(self) -> None:
        _, _, store, _, _, _ = _execute_clinical_scenario()

        stream = store.read_stream(_DIAGNOSIS_ID)
        assert len(stream) == 1
        assert stream[0].event_type == "clinical.judgment.DiagnosisConfirmed"


# ---------------------------------------------------------------------------
# Tests: Projection reflects clinical reality
# ---------------------------------------------------------------------------

class TestProjectionReflectsClinicalReality:
    """After the full clinical flow, the patient summary must reflect
    what actually happened to the patient."""

    def test_projection_has_diagnosis(self) -> None:
        _, qry_gw, _, _, _, _ = _execute_clinical_scenario()

        result = qry_gw.handle({"query_type": "PatientSummary"})

        assert result.success is True
        conditions = result.data["active_conditions"]
        assert len(conditions) == 1
        assert conditions[0]["condition"] == "Essential Hypertension"
        assert conditions[0]["icd_code"] == "I10"

    def test_projection_has_vitals(self) -> None:
        _, qry_gw, _, _, _, _ = _execute_clinical_scenario()

        result = qry_gw.handle({"query_type": "PatientSummary"})

        vitals = result.data["vitals"]
        assert len(vitals) == 1

        readings = vitals[0]["readings"]
        assert readings["systolic_bp"] == 158
        assert readings["diastolic_bp"] == 95
        assert readings["heart_rate"] == 82
        assert readings["temperature_f"] == 98.6

    def test_vitals_linked_to_encounter(self) -> None:
        _, qry_gw, _, _, _, _ = _execute_clinical_scenario()

        result = qry_gw.handle({"query_type": "PatientSummary"})

        vitals = result.data["vitals"]
        assert vitals[0]["encounter_id"] == str(_ENCOUNTER_ID)

    def test_vitals_linked_to_patient(self) -> None:
        _, qry_gw, _, _, _, _ = _execute_clinical_scenario()

        result = qry_gw.handle({"query_type": "PatientSummary"})

        vitals = result.data["vitals"]
        assert vitals[0]["patient_id"] == str(_PATIENT_ID)

    def test_diagnosis_linked_to_patient(self) -> None:
        _, qry_gw, _, _, _, _ = _execute_clinical_scenario()

        result = qry_gw.handle({"query_type": "PatientSummary"})

        condition = result.data["active_conditions"][0]
        assert condition["patient_id"] == str(_PATIENT_ID)

    def test_clinical_picture_is_coherent(self) -> None:
        """The projection tells a coherent clinical story:
        elevated BP → hypertension diagnosis."""
        _, qry_gw, _, _, _, _ = _execute_clinical_scenario()

        result = qry_gw.handle({"query_type": "PatientSummary"})

        # Patient has elevated blood pressure
        vitals = result.data["vitals"]
        assert vitals[0]["readings"]["systolic_bp"] >= 140  # stage 2 hypertension

        # Doctor diagnosed hypertension
        conditions = result.data["active_conditions"]
        assert any(c["condition"] == "Essential Hypertension" for c in conditions)

    def test_query_response_is_complete(self) -> None:
        _, qry_gw, _, _, _, _ = _execute_clinical_scenario()

        result = qry_gw.handle({"query_type": "PatientSummary"})

        assert isinstance(result, QueryResult)
        assert result.success is True
        assert "active_conditions" in result.data
        assert "active_treatments" in result.data
        assert "stopped_treatments" in result.data
        assert "vitals" in result.data

    def test_projection_matches_raw_state(self) -> None:
        """QueryGateway response is consistent with raw projection state."""
        _, qry_gw, _, _, projection, _ = _execute_clinical_scenario()

        result = qry_gw.handle({"query_type": "PatientSummary"})

        raw_conditions = projection.state.get("active_conditions", {})
        raw_vitals = projection.state.get("vitals", [])

        assert len(result.data["active_conditions"]) == len(raw_conditions)
        assert len(result.data["vitals"]) == len(raw_vitals)

    def test_projection_rebuildable(self) -> None:
        """Projection can be rebuilt from event store and produce same state."""
        _, qry_gw, store, _, projection, _ = _execute_clinical_scenario()

        original_state = dict(projection.state)

        fresh = PatientSummaryProjection()
        fresh.rebuild_from(store.read_all_events())

        assert fresh.state == original_state
