"""Patient Summary Projection.

A derived view that tracks active conditions and active treatments
for a patient, rebuilt entirely from events.

Consumes:
- clinical.judgment.DiagnosisConfirmed
- clinical.judgment.TreatmentStarted
- clinical.judgment.TreatmentStopped

Produces (state keys):
- active_conditions: dict[diagnosis_id → {condition, icd_code, patient_id}]
- active_treatments: dict[treatment_id → {treatment, diagnosis_id, patient_id}]
- stopped_treatments: dict[treatment_id → {treatment, diagnosis_id, reason, patient_id}]
"""

from __future__ import annotations

from typing import Any

from clinical_core.application.projection_handler import ProjectionHandler
from clinical_core.domain.events import DomainEvent

_DIAGNOSIS_CONFIRMED = "clinical.judgment.DiagnosisConfirmed"
_TREATMENT_STARTED = "clinical.judgment.TreatmentStarted"
_TREATMENT_STOPPED = "clinical.judgment.TreatmentStopped"


class PatientSummaryProjection(ProjectionHandler):

    @property
    def subscribed_event_types(self) -> list[str]:
        return [_DIAGNOSIS_CONFIRMED, _TREATMENT_STARTED, _TREATMENT_STOPPED]

    def _apply(self, state: dict[str, Any], event: DomainEvent) -> dict[str, Any]:
        active_conditions: dict[str, Any] = dict(state.get("active_conditions", {}))
        active_treatments: dict[str, Any] = dict(state.get("active_treatments", {}))
        stopped_treatments: dict[str, Any] = dict(state.get("stopped_treatments", {}))

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

        return {
            "active_conditions": active_conditions,
            "active_treatments": active_treatments,
            "stopped_treatments": stopped_treatments,
        }
