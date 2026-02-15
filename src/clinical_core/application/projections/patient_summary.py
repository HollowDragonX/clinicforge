"""Patient Summary Projection.

A derived view that tracks active conditions, active treatments,
and recorded vitals for a patient, rebuilt entirely from events.

Consumes:
- clinical.judgment.DiagnosisConfirmed
- clinical.judgment.TreatmentStarted
- clinical.judgment.TreatmentStopped
- clinical.observation.VitalSignsRecorded

Produces (state keys):
- active_conditions: dict[diagnosis_id → {condition, icd_code, patient_id}]
- active_treatments: dict[treatment_id → {treatment, diagnosis_id, patient_id}]
- stopped_treatments: dict[treatment_id → {treatment, diagnosis_id, reason, patient_id}]
- vitals: list[{recorded_at, readings, patient_id, encounter_id}]
"""

from __future__ import annotations

from typing import Any

from clinical_core.application.projection_handler import ProjectionHandler
from clinical_core.domain.events import DomainEvent

_DIAGNOSIS_CONFIRMED = "clinical.judgment.DiagnosisConfirmed"
_TREATMENT_STARTED = "clinical.judgment.TreatmentStarted"
_TREATMENT_STOPPED = "clinical.judgment.TreatmentStopped"
_VITAL_SIGNS_RECORDED = "clinical.observation.VitalSignsRecorded"


class PatientSummaryProjection(ProjectionHandler):

    @property
    def subscribed_event_types(self) -> list[str]:
        return [
            _DIAGNOSIS_CONFIRMED, _TREATMENT_STARTED, _TREATMENT_STOPPED,
            _VITAL_SIGNS_RECORDED,
        ]

    def _apply(self, state: dict[str, Any], event: DomainEvent) -> dict[str, Any]:
        active_conditions: dict[str, Any] = dict(state.get("active_conditions", {}))
        active_treatments: dict[str, Any] = dict(state.get("active_treatments", {}))
        stopped_treatments: dict[str, Any] = dict(state.get("stopped_treatments", {}))
        vitals: list[dict[str, Any]] = list(state.get("vitals", []))

        payload = event.payload

        if event.event_type == _DIAGNOSIS_CONFIRMED:
            diagnosis_id = payload["diagnosis_id"]
            active_conditions[diagnosis_id] = {
                "condition": payload["condition"],
                "icd_code": payload["icd_code"],
                "patient_id": payload.get("patient_id"),
            }

        elif event.event_type == _TREATMENT_STARTED:
            treatment_id = payload["treatment_id"]
            active_treatments[treatment_id] = {
                "treatment": payload["treatment"],
                "diagnosis_id": payload.get("diagnosis_id"),
                "patient_id": payload.get("patient_id"),
            }

        elif event.event_type == _TREATMENT_STOPPED:
            treatment_id = payload["treatment_id"]
            stopped_entry = {
                "reason": payload.get("reason"),
                "patient_id": payload.get("patient_id"),
            }
            if treatment_id in active_treatments:
                stopped_entry.update(active_treatments.pop(treatment_id))
            stopped_treatments[treatment_id] = stopped_entry

        elif event.event_type == _VITAL_SIGNS_RECORDED:
            vitals.append({
                "recorded_at": str(event.metadata.occurred_at),
                "readings": payload.get("readings", {}),
                "patient_id": payload.get("patient_id"),
                "encounter_id": payload.get("encounter_id"),
            })

        return {
            "active_conditions": active_conditions,
            "active_treatments": active_treatments,
            "stopped_treatments": stopped_treatments,
            "vitals": vitals,
        }
