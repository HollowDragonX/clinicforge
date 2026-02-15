"""TDD tests for the Patient Summary Projection.

Consumes:
- clinical.judgment.DiagnosisConfirmed
- clinical.judgment.TreatmentStarted
- clinical.judgment.TreatmentStopped

Produces:
- active_conditions: dict of diagnosis_id → condition info
- active_treatments: dict of treatment_id → treatment info

Must rebuild entirely from events.
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
# Test helpers
# ---------------------------------------------------------------------------

_ORG_ID = uuid4()
_FACILITY_ID = uuid4()
_PATIENT_ID = uuid4()


def _make_event(
    event_type: str,
    aggregate_id: UUID | None = None,
    aggregate_version: int = 1,
    payload: dict | None = None,
) -> DomainEvent:
    return DomainEvent(
        metadata=EventMetadata(
            event_id=uuid4(),
            event_type=event_type,
            schema_version=1,
            aggregate_id=aggregate_id or uuid4(),
            aggregate_type="Diagnosis",
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


def _diagnosis_confirmed(
    diagnosis_id: UUID | None = None,
    condition: str = "Hypertension",
    icd_code: str = "I10",
) -> DomainEvent:
    diag_id = diagnosis_id or uuid4()
    return _make_event(
        event_type="clinical.judgment.DiagnosisConfirmed",
        aggregate_id=diag_id,
        payload={
            "patient_id": str(_PATIENT_ID),
            "diagnosis_id": str(diag_id),
            "condition": condition,
            "icd_code": icd_code,
        },
    )


def _treatment_started(
    treatment_id: UUID | None = None,
    diagnosis_id: UUID | None = None,
    treatment: str = "Lisinopril 10mg daily",
) -> DomainEvent:
    t_id = treatment_id or uuid4()
    return _make_event(
        event_type="clinical.judgment.TreatmentStarted",
        aggregate_id=t_id,
        payload={
            "patient_id": str(_PATIENT_ID),
            "treatment_id": str(t_id),
            "diagnosis_id": str(diagnosis_id or uuid4()),
            "treatment": treatment,
        },
    )


def _treatment_stopped(
    treatment_id: UUID,
    reason: str = "Adverse reaction",
) -> DomainEvent:
    return _make_event(
        event_type="clinical.judgment.TreatmentStopped",
        aggregate_id=treatment_id,
        aggregate_version=2,
        payload={
            "patient_id": str(_PATIENT_ID),
            "treatment_id": str(treatment_id),
            "reason": reason,
        },
    )


# ---------------------------------------------------------------------------
# Tests: Subscription
# ---------------------------------------------------------------------------

class TestSubscription:

    def test_subscribes_to_correct_event_types(self) -> None:
        from clinical_core.application.projections.patient_summary import PatientSummaryProjection

        proj = PatientSummaryProjection()
        assert "clinical.judgment.DiagnosisConfirmed" in proj.subscribed_event_types
        assert "clinical.judgment.TreatmentStarted" in proj.subscribed_event_types
        assert "clinical.judgment.TreatmentStopped" in proj.subscribed_event_types

    def test_ignores_unrelated_events(self) -> None:
        from clinical_core.application.projections.patient_summary import PatientSummaryProjection

        proj = PatientSummaryProjection()
        proj.handle(_make_event(event_type="clinical.encounter.EncounterBegan"))

        assert proj.state.get("active_conditions", {}) == {}
        assert proj.state.get("active_treatments", {}) == {}


# ---------------------------------------------------------------------------
# Tests: Diagnosis tracking
# ---------------------------------------------------------------------------

class TestDiagnosisTracking:

    def test_diagnosis_confirmed_adds_to_active_conditions(self) -> None:
        from clinical_core.application.projections.patient_summary import PatientSummaryProjection

        proj = PatientSummaryProjection()
        diag_id = uuid4()
        proj.handle(_diagnosis_confirmed(diagnosis_id=diag_id, condition="Hypertension", icd_code="I10"))

        conditions = proj.state["active_conditions"]
        assert str(diag_id) in conditions
        assert conditions[str(diag_id)]["condition"] == "Hypertension"
        assert conditions[str(diag_id)]["icd_code"] == "I10"

    def test_multiple_diagnoses_tracked(self) -> None:
        from clinical_core.application.projections.patient_summary import PatientSummaryProjection

        proj = PatientSummaryProjection()
        proj.handle(_diagnosis_confirmed(condition="Hypertension", icd_code="I10"))
        proj.handle(_diagnosis_confirmed(condition="Type 2 Diabetes", icd_code="E11"))

        assert len(proj.state["active_conditions"]) == 2

    def test_duplicate_diagnosis_event_is_idempotent(self) -> None:
        from clinical_core.application.projections.patient_summary import PatientSummaryProjection

        proj = PatientSummaryProjection()
        event = _diagnosis_confirmed(condition="Hypertension")
        proj.handle(event)
        proj.handle(event)

        assert len(proj.state["active_conditions"]) == 1


# ---------------------------------------------------------------------------
# Tests: Treatment tracking
# ---------------------------------------------------------------------------

class TestTreatmentTracking:

    def test_treatment_started_adds_to_active_treatments(self) -> None:
        from clinical_core.application.projections.patient_summary import PatientSummaryProjection

        proj = PatientSummaryProjection()
        t_id = uuid4()
        diag_id = uuid4()
        proj.handle(_treatment_started(treatment_id=t_id, diagnosis_id=diag_id, treatment="Lisinopril 10mg daily"))

        treatments = proj.state["active_treatments"]
        assert str(t_id) in treatments
        assert treatments[str(t_id)]["treatment"] == "Lisinopril 10mg daily"
        assert treatments[str(t_id)]["diagnosis_id"] == str(diag_id)

    def test_treatment_stopped_removes_from_active(self) -> None:
        from clinical_core.application.projections.patient_summary import PatientSummaryProjection

        proj = PatientSummaryProjection()
        t_id = uuid4()
        proj.handle(_treatment_started(treatment_id=t_id, treatment="Lisinopril 10mg daily"))
        proj.handle(_treatment_stopped(treatment_id=t_id, reason="Adverse reaction"))

        assert str(t_id) not in proj.state["active_treatments"]

    def test_treatment_stopped_records_in_stopped_treatments(self) -> None:
        from clinical_core.application.projections.patient_summary import PatientSummaryProjection

        proj = PatientSummaryProjection()
        t_id = uuid4()
        proj.handle(_treatment_started(treatment_id=t_id, treatment="Lisinopril 10mg daily"))
        proj.handle(_treatment_stopped(treatment_id=t_id, reason="Adverse reaction"))

        stopped = proj.state["stopped_treatments"]
        assert str(t_id) in stopped
        assert stopped[str(t_id)]["reason"] == "Adverse reaction"

    def test_multiple_treatments_tracked(self) -> None:
        from clinical_core.application.projections.patient_summary import PatientSummaryProjection

        proj = PatientSummaryProjection()
        proj.handle(_treatment_started(treatment="Lisinopril 10mg daily"))
        proj.handle(_treatment_started(treatment="Metformin 500mg BID"))

        assert len(proj.state["active_treatments"]) == 2

    def test_stopping_unknown_treatment_is_safe(self) -> None:
        from clinical_core.application.projections.patient_summary import PatientSummaryProjection

        proj = PatientSummaryProjection()
        # Stop a treatment that was never started — should not crash
        proj.handle(_treatment_stopped(treatment_id=uuid4()))

        assert proj.state.get("active_treatments", {}) == {}


# ---------------------------------------------------------------------------
# Tests: Full rebuild from events
# ---------------------------------------------------------------------------

class TestRebuild:

    def test_rebuild_produces_correct_state(self) -> None:
        from clinical_core.application.projections.patient_summary import PatientSummaryProjection

        proj = PatientSummaryProjection()

        diag_id = uuid4()
        t1_id = uuid4()
        t2_id = uuid4()

        events = [
            _diagnosis_confirmed(diagnosis_id=diag_id, condition="Hypertension", icd_code="I10"),
            _treatment_started(treatment_id=t1_id, diagnosis_id=diag_id, treatment="Lisinopril 10mg"),
            _treatment_started(treatment_id=t2_id, diagnosis_id=diag_id, treatment="HCTZ 25mg"),
            _treatment_stopped(treatment_id=t1_id, reason="Cough side effect"),
        ]

        proj.rebuild_from(events)

        assert len(proj.state["active_conditions"]) == 1
        assert proj.state["active_conditions"][str(diag_id)]["condition"] == "Hypertension"

        assert len(proj.state["active_treatments"]) == 1
        assert str(t2_id) in proj.state["active_treatments"]

        assert len(proj.state["stopped_treatments"]) == 1
        assert str(t1_id) in proj.state["stopped_treatments"]

    def test_rebuild_clears_previous_state(self) -> None:
        from clinical_core.application.projections.patient_summary import PatientSummaryProjection

        proj = PatientSummaryProjection()
        proj.handle(_diagnosis_confirmed(condition="Old Diagnosis"))

        proj.rebuild_from([_diagnosis_confirmed(condition="New Diagnosis")])

        conditions = proj.state["active_conditions"]
        condition_names = [c["condition"] for c in conditions.values()]
        assert "Old Diagnosis" not in condition_names
        assert "New Diagnosis" in condition_names

    def test_rebuild_from_empty_clears_everything(self) -> None:
        from clinical_core.application.projections.patient_summary import PatientSummaryProjection

        proj = PatientSummaryProjection()
        proj.handle(_diagnosis_confirmed(condition="Hypertension"))
        proj.handle(_treatment_started(treatment="Lisinopril"))

        proj.rebuild_from([])

        assert proj.state.get("active_conditions", {}) == {}
        assert proj.state.get("active_treatments", {}) == {}


# ---------------------------------------------------------------------------
# Tests: Integration with dispatcher
# ---------------------------------------------------------------------------

class TestDispatcherIntegration:

    def test_dispatcher_routes_to_patient_summary(self) -> None:
        from clinical_core.application.event_dispatcher import EventDispatcher
        from clinical_core.application.projections.patient_summary import PatientSummaryProjection

        dispatcher = EventDispatcher()
        proj = PatientSummaryProjection()

        for event_type in proj.subscribed_event_types:
            dispatcher.subscribe(event_type, proj.handle)

        diag_id = uuid4()
        t_id = uuid4()

        dispatcher.dispatch(_diagnosis_confirmed(diagnosis_id=diag_id, condition="Asthma", icd_code="J45"))
        dispatcher.dispatch(_treatment_started(treatment_id=t_id, diagnosis_id=diag_id, treatment="Albuterol inhaler"))

        assert len(proj.state["active_conditions"]) == 1
        assert len(proj.state["active_treatments"]) == 1
