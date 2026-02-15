"""Convergence test: two offline nodes sync to identical state.

Scenario:
  Node A (nurse tablet, offline) creates clinical events.
  Node B (doctor laptop, offline) creates different clinical events.

After bidirectional sync:
  1. Both nodes contain identical event history (same event IDs, same count).
  2. Projections built on each node match exactly.

This test proves that the sync engine + event store + projections
converge to the same state regardless of which node created which events.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
from clinical_core.application.projections.patient_summary import PatientSummaryProjection
from clinical_core.sync.engine import SyncNode, SyncEngine


# ---------------------------------------------------------------------------
# Shared identities (same patient, same encounter across both devices)
# ---------------------------------------------------------------------------

_ORG_ID = uuid4()
_FACILITY_ID = uuid4()
_PATIENT_ID = uuid4()
_ENCOUNTER_ID = uuid4()
_NURSE_ID = uuid4()
_DOCTOR_ID = uuid4()


# ---------------------------------------------------------------------------
# Helper: build clinical events
# ---------------------------------------------------------------------------

def _clinical_event(
    event_type: str,
    aggregate_id: UUID,
    aggregate_version: int,
    device_id: str,
    performed_by: UUID,
    performer_role: str,
    payload: dict,
    occurred_at: datetime | None = None,
    connection_status: ConnectionStatus = ConnectionStatus.OFFLINE,
) -> DomainEvent:
    return DomainEvent(
        metadata=EventMetadata(
            event_id=uuid4(),
            event_type=event_type,
            schema_version=1,
            aggregate_id=aggregate_id,
            aggregate_type=event_type.split(".")[1].capitalize(),
            aggregate_version=aggregate_version,
            occurred_at=occurred_at or datetime.now(timezone.utc),
            performed_by=performed_by,
            performer_role=performer_role,
            organization_id=_ORG_ID,
            facility_id=_FACILITY_ID,
            device_id=device_id,
            connection_status=connection_status,
            correlation_id=uuid4(),
        ),
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Fixture: build the two offline nodes with their events
# ---------------------------------------------------------------------------

def _build_offline_nodes() -> tuple[SyncNode, SyncNode, dict[str, UUID]]:
    """Create two nodes with independent offline events.

    Node A (nurse tablet):
      - VitalSigns recorded
      - Symptom reported
      - DiagnosisConfirmed (hypertension) — nurse-practitioner scope

    Node B (doctor laptop):
      - DiagnosisConfirmed (diabetes)
      - TreatmentStarted (metformin for diabetes)
      - TreatmentStarted (lisinopril for hypertension — different aggregate)

    All events target different aggregate streams (fact aggregates or
    independent lifecycle aggregates), so no conflicts on sync.
    """
    store_a = InMemoryEventStore()
    store_b = InMemoryEventStore()
    disp_a = EventDispatcher()
    disp_b = EventDispatcher()

    # Projection on each node
    proj_a = PatientSummaryProjection()
    proj_b = PatientSummaryProjection()
    for event_type in proj_a.subscribed_event_types:
        disp_a.subscribe(event_type, proj_a.handle)
        disp_b.subscribe(event_type, proj_b.handle)

    # --- Node A events (nurse, offline) ---

    vitals_id = uuid4()
    symptom_id = uuid4()
    diag_hypertension_id = uuid4()

    now = datetime.now(timezone.utc)

    store_a.append(_clinical_event(
        event_type="clinical.observation.VitalSignsRecorded",
        aggregate_id=vitals_id,
        aggregate_version=1,
        device_id="nurse-tablet",
        performed_by=_NURSE_ID,
        performer_role="nurse",
        occurred_at=now,
        payload={
            "patient_id": str(_PATIENT_ID),
            "encounter_id": str(_ENCOUNTER_ID),
            "systolic": 145,
            "diastolic": 92,
        },
    ))

    store_a.append(_clinical_event(
        event_type="clinical.observation.SymptomReported",
        aggregate_id=symptom_id,
        aggregate_version=1,
        device_id="nurse-tablet",
        performed_by=_NURSE_ID,
        performer_role="nurse",
        occurred_at=now + timedelta(minutes=2),
        payload={
            "patient_id": str(_PATIENT_ID),
            "encounter_id": str(_ENCOUNTER_ID),
            "symptom": "headache",
            "severity": "moderate",
        },
    ))

    store_a.append(_clinical_event(
        event_type="clinical.judgment.DiagnosisConfirmed",
        aggregate_id=diag_hypertension_id,
        aggregate_version=1,
        device_id="nurse-tablet",
        performed_by=_NURSE_ID,
        performer_role="nurse_practitioner",
        occurred_at=now + timedelta(minutes=5),
        payload={
            "diagnosis_id": str(diag_hypertension_id),
            "patient_id": str(_PATIENT_ID),
            "encounter_id": str(_ENCOUNTER_ID),
            "condition": "Hypertension",
            "icd_code": "I10",
        },
    ))
    # Dispatch to local projection
    for e in store_a.read_all_events():
        disp_a.dispatch(e)

    # --- Node B events (doctor, offline) ---

    diag_diabetes_id = uuid4()
    treatment_metformin_id = uuid4()
    treatment_lisinopril_id = uuid4()

    store_b.append(_clinical_event(
        event_type="clinical.judgment.DiagnosisConfirmed",
        aggregate_id=diag_diabetes_id,
        aggregate_version=1,
        device_id="doctor-laptop",
        performed_by=_DOCTOR_ID,
        performer_role="physician",
        occurred_at=now + timedelta(minutes=3),
        payload={
            "diagnosis_id": str(diag_diabetes_id),
            "patient_id": str(_PATIENT_ID),
            "encounter_id": str(_ENCOUNTER_ID),
            "condition": "Type 2 Diabetes",
            "icd_code": "E11",
        },
    ))

    store_b.append(_clinical_event(
        event_type="clinical.judgment.TreatmentStarted",
        aggregate_id=treatment_metformin_id,
        aggregate_version=1,
        device_id="doctor-laptop",
        performed_by=_DOCTOR_ID,
        performer_role="physician",
        occurred_at=now + timedelta(minutes=6),
        payload={
            "treatment_id": str(treatment_metformin_id),
            "patient_id": str(_PATIENT_ID),
            "diagnosis_id": str(diag_diabetes_id),
            "treatment": "Metformin 500mg BID",
        },
    ))

    store_b.append(_clinical_event(
        event_type="clinical.judgment.TreatmentStarted",
        aggregate_id=treatment_lisinopril_id,
        aggregate_version=1,
        device_id="doctor-laptop",
        performed_by=_DOCTOR_ID,
        performer_role="physician",
        occurred_at=now + timedelta(minutes=7),
        payload={
            "treatment_id": str(treatment_lisinopril_id),
            "patient_id": str(_PATIENT_ID),
            "diagnosis_id": str(diag_hypertension_id),
            "treatment": "Lisinopril 10mg QD",
        },
    ))
    # Dispatch to local projection
    for e in store_b.read_all_events():
        disp_b.dispatch(e)

    node_a = SyncNode("nurse-tablet", store_a, disp_a)
    node_b = SyncNode("doctor-laptop", store_b, disp_b)

    ids = {
        "vitals": vitals_id,
        "symptom": symptom_id,
        "diag_hypertension": diag_hypertension_id,
        "diag_diabetes": diag_diabetes_id,
        "treatment_metformin": treatment_metformin_id,
        "treatment_lisinopril": treatment_lisinopril_id,
    }

    return node_a, node_b, ids


# ---------------------------------------------------------------------------
# Tests: Pre-sync state (verify isolation)
# ---------------------------------------------------------------------------

class TestPreSyncIsolation:
    """Before sync, each node only has its own events."""

    def test_node_a_has_3_events(self) -> None:
        node_a, node_b, _ = _build_offline_nodes()
        assert node_a.event_count() == 3

    def test_node_b_has_3_events(self) -> None:
        node_a, node_b, _ = _build_offline_nodes()
        assert node_b.event_count() == 3

    def test_nodes_have_no_shared_events(self) -> None:
        node_a, node_b, _ = _build_offline_nodes()
        shared = node_a.known_event_ids() & node_b.known_event_ids()
        assert len(shared) == 0


# ---------------------------------------------------------------------------
# Tests: Event history convergence
# ---------------------------------------------------------------------------

class TestEventHistoryConvergence:
    """After sync, both nodes have identical event history."""

    def test_both_nodes_have_all_six_events(self) -> None:
        node_a, node_b, _ = _build_offline_nodes()
        engine = SyncEngine()
        engine.full_sync(node_a, node_b)

        assert node_a.event_count() == 6
        assert node_b.event_count() == 6

    def test_both_nodes_have_identical_event_ids(self) -> None:
        node_a, node_b, _ = _build_offline_nodes()
        engine = SyncEngine()
        engine.full_sync(node_a, node_b)

        assert node_a.known_event_ids() == node_b.known_event_ids()

    def test_node_a_has_node_b_events(self) -> None:
        node_a, node_b, ids = _build_offline_nodes()
        engine = SyncEngine()
        engine.full_sync(node_a, node_b)

        a_types = {e.event_type for e in node_a.all_events()}
        assert "clinical.judgment.TreatmentStarted" in a_types

    def test_node_b_has_node_a_events(self) -> None:
        node_a, node_b, ids = _build_offline_nodes()
        engine = SyncEngine()
        engine.full_sync(node_a, node_b)

        b_types = {e.event_type for e in node_b.all_events()}
        assert "clinical.observation.VitalSignsRecorded" in b_types
        assert "clinical.observation.SymptomReported" in b_types

    def test_event_payloads_preserved_across_sync(self) -> None:
        node_a, node_b, ids = _build_offline_nodes()
        engine = SyncEngine()
        engine.full_sync(node_a, node_b)

        # Find the vitals event on node B (came from A)
        vitals_on_b = [
            e for e in node_b.all_events()
            if e.aggregate_id == ids["vitals"]
        ]
        assert len(vitals_on_b) == 1
        assert vitals_on_b[0].payload["systolic"] == 145
        assert vitals_on_b[0].payload["diastolic"] == 92

    def test_sync_is_idempotent(self) -> None:
        node_a, node_b, _ = _build_offline_nodes()
        engine = SyncEngine()

        engine.full_sync(node_a, node_b)
        r2 = engine.full_sync(node_a, node_b)

        assert r2.a_to_b_transferred == 0
        assert r2.b_to_a_transferred == 0
        assert node_a.event_count() == 6
        assert node_b.event_count() == 6


# ---------------------------------------------------------------------------
# Tests: Projection convergence
# ---------------------------------------------------------------------------

class TestProjectionConvergence:
    """After sync, projections built on each node produce identical state."""

    def _sync_and_rebuild_projections(
        self,
    ) -> tuple[PatientSummaryProjection, PatientSummaryProjection]:
        """Sync nodes, then rebuild projections from each node's full event set."""
        node_a, node_b, _ = _build_offline_nodes()
        engine = SyncEngine()
        engine.full_sync(node_a, node_b)

        # Rebuild fresh projections from each node's complete event store
        proj_a = PatientSummaryProjection()
        proj_b = PatientSummaryProjection()

        proj_a.rebuild_from(node_a.all_events())
        proj_b.rebuild_from(node_b.all_events())

        return proj_a, proj_b

    def test_active_conditions_match(self) -> None:
        proj_a, proj_b = self._sync_and_rebuild_projections()

        conditions_a = proj_a.state.get("active_conditions", {})
        conditions_b = proj_b.state.get("active_conditions", {})

        assert len(conditions_a) == 2
        assert conditions_a == conditions_b

    def test_active_treatments_match(self) -> None:
        proj_a, proj_b = self._sync_and_rebuild_projections()

        treatments_a = proj_a.state.get("active_treatments", {})
        treatments_b = proj_b.state.get("active_treatments", {})

        assert len(treatments_a) == 2
        assert treatments_a == treatments_b

    def test_projection_contains_hypertension(self) -> None:
        proj_a, proj_b = self._sync_and_rebuild_projections()

        conditions_a = proj_a.state["active_conditions"]
        condition_names = [c["condition"] for c in conditions_a.values()]
        assert "Hypertension" in condition_names

    def test_projection_contains_diabetes(self) -> None:
        proj_a, proj_b = self._sync_and_rebuild_projections()

        conditions_a = proj_a.state["active_conditions"]
        condition_names = [c["condition"] for c in conditions_a.values()]
        assert "Type 2 Diabetes" in condition_names

    def test_projection_contains_both_treatments(self) -> None:
        proj_a, proj_b = self._sync_and_rebuild_projections()

        treatments_a = proj_a.state["active_treatments"]
        treatment_names = [t["treatment"] for t in treatments_a.values()]
        assert "Metformin 500mg BID" in treatment_names
        assert "Lisinopril 10mg QD" in treatment_names

    def test_full_projection_state_identical(self) -> None:
        """The complete projection state dict matches between nodes."""
        proj_a, proj_b = self._sync_and_rebuild_projections()
        assert proj_a.state == proj_b.state
