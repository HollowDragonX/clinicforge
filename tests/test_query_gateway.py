"""TDD tests for the Query Gateway skeleton.

The Query Gateway reads projection state and returns it to external clients.
No framework coupling — queries are plain dicts, responses are QueryResult.

Requirements tested:
1. Receive query request (dict)
2. Fetch projection state
3. Map projection → response DTO
4. Return result (never throws)

Rules enforced:
- Gateway reads ONLY projections.
- Gateway never accesses aggregates.
- Gateway never reads event store directly.
- Gateway contains no business logic.
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
from clinical_core.application.projections.patient_summary import PatientSummaryProjection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ORG_ID = uuid4()
_FACILITY_ID = uuid4()
_PATIENT_ID = uuid4()


def _diagnosis_event(
    diagnosis_id: UUID,
    condition: str,
    icd_code: str,
) -> DomainEvent:
    return DomainEvent(
        metadata=EventMetadata(
            event_id=uuid4(),
            event_type="clinical.judgment.DiagnosisConfirmed",
            schema_version=1,
            aggregate_id=diagnosis_id,
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
            "diagnosis_id": str(diagnosis_id),
            "patient_id": str(_PATIENT_ID),
            "condition": condition,
            "icd_code": icd_code,
        },
    )


def _treatment_event(
    treatment_id: UUID,
    treatment: str,
    diagnosis_id: UUID,
) -> DomainEvent:
    return DomainEvent(
        metadata=EventMetadata(
            event_id=uuid4(),
            event_type="clinical.judgment.TreatmentStarted",
            schema_version=1,
            aggregate_id=treatment_id,
            aggregate_type="TreatmentPlan",
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
            "treatment_id": str(treatment_id),
            "patient_id": str(_PATIENT_ID),
            "diagnosis_id": str(diagnosis_id),
            "treatment": treatment,
        },
    )


def _populated_projection() -> PatientSummaryProjection:
    """Build a projection with 2 diagnoses and 1 treatment."""
    proj = PatientSummaryProjection()
    diag1 = uuid4()
    diag2 = uuid4()
    treat1 = uuid4()

    proj.handle(_diagnosis_event(diag1, "Hypertension", "I10"))
    proj.handle(_diagnosis_event(diag2, "Type 2 Diabetes", "E11"))
    proj.handle(_treatment_event(treat1, "Metformin 500mg BID", diag2))

    return proj


def _build_query_gateway(projection: PatientSummaryProjection | None = None):
    from clinical_core.application.query_gateway import QueryGateway

    projection = projection or _populated_projection()

    gateway = QueryGateway()
    gateway.register(
        query_type="PatientSummary",
        projection=projection,
        mapper=_patient_summary_mapper,
    )

    return gateway, projection


def _patient_summary_mapper(state: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """Map PatientSummaryProjection state → external response DTO."""
    conditions = state.get("active_conditions", {})
    treatments = state.get("active_treatments", {})
    stopped = state.get("stopped_treatments", {})

    return {
        "active_conditions": [
            {"id": k, "condition": v["condition"], "icd_code": v["icd_code"]}
            for k, v in conditions.items()
        ],
        "active_treatments": [
            {"id": k, "treatment": v["treatment"], "diagnosis_id": v.get("diagnosis_id")}
            for k, v in treatments.items()
        ],
        "stopped_treatments": [
            {"id": k, "reason": v.get("reason")}
            for k, v in stopped.items()
        ],
    }


# ---------------------------------------------------------------------------
# Tests: Receive query request
# ---------------------------------------------------------------------------

class TestReceiveQuery:

    def test_accepts_valid_query(self) -> None:
        gateway, _ = _build_query_gateway()

        result = gateway.handle({"query_type": "PatientSummary"})

        assert result.success is True

    def test_rejects_missing_query_type(self) -> None:
        gateway, _ = _build_query_gateway()

        result = gateway.handle({})

        assert result.success is False
        assert "query_type" in result.error.lower()

    def test_rejects_unknown_query_type(self) -> None:
        gateway, _ = _build_query_gateway()

        result = gateway.handle({"query_type": "UnknownQuery"})

        assert result.success is False
        assert "unknown" in result.error.lower()

    def test_accepts_query_with_params(self) -> None:
        gateway, _ = _build_query_gateway()

        result = gateway.handle({
            "query_type": "PatientSummary",
            "params": {"patient_id": str(_PATIENT_ID)},
        })

        assert result.success is True


# ---------------------------------------------------------------------------
# Tests: Fetch projection state
# ---------------------------------------------------------------------------

class TestFetchProjection:

    def test_returns_projection_data(self) -> None:
        gateway, _ = _build_query_gateway()

        result = gateway.handle({"query_type": "PatientSummary"})

        assert result.success is True
        assert "active_conditions" in result.data

    def test_empty_projection_returns_empty_data(self) -> None:
        empty_proj = PatientSummaryProjection()
        gateway, _ = _build_query_gateway(projection=empty_proj)

        result = gateway.handle({"query_type": "PatientSummary"})

        assert result.success is True
        assert result.data["active_conditions"] == []
        assert result.data["active_treatments"] == []


# ---------------------------------------------------------------------------
# Tests: Map projection → response DTO
# ---------------------------------------------------------------------------

class TestMapToResponse:

    def test_conditions_mapped_to_list(self) -> None:
        gateway, _ = _build_query_gateway()

        result = gateway.handle({"query_type": "PatientSummary"})

        conditions = result.data["active_conditions"]
        assert isinstance(conditions, list)
        assert len(conditions) == 2

    def test_condition_has_expected_fields(self) -> None:
        gateway, _ = _build_query_gateway()

        result = gateway.handle({"query_type": "PatientSummary"})

        condition = result.data["active_conditions"][0]
        assert "id" in condition
        assert "condition" in condition
        assert "icd_code" in condition

    def test_treatments_mapped_to_list(self) -> None:
        gateway, _ = _build_query_gateway()

        result = gateway.handle({"query_type": "PatientSummary"})

        treatments = result.data["active_treatments"]
        assert isinstance(treatments, list)
        assert len(treatments) == 1

    def test_treatment_has_expected_fields(self) -> None:
        gateway, _ = _build_query_gateway()

        result = gateway.handle({"query_type": "PatientSummary"})

        treatment = result.data["active_treatments"][0]
        assert "id" in treatment
        assert "treatment" in treatment
        assert "diagnosis_id" in treatment

    def test_condition_values_correct(self) -> None:
        gateway, _ = _build_query_gateway()

        result = gateway.handle({"query_type": "PatientSummary"})

        names = {c["condition"] for c in result.data["active_conditions"]}
        assert names == {"Hypertension", "Type 2 Diabetes"}


# ---------------------------------------------------------------------------
# Tests: Return result
# ---------------------------------------------------------------------------

class TestReturnResult:

    def test_success_result_structure(self) -> None:
        from clinical_core.application.query_gateway import QueryResult

        gateway, _ = _build_query_gateway()
        result = gateway.handle({"query_type": "PatientSummary"})

        assert isinstance(result, QueryResult)
        assert result.success is True
        assert isinstance(result.data, dict)
        assert result.error == ""

    def test_error_result_structure(self) -> None:
        from clinical_core.application.query_gateway import QueryResult

        gateway, _ = _build_query_gateway()
        result = gateway.handle({})

        assert isinstance(result, QueryResult)
        assert result.success is False
        assert result.data == {}
        assert isinstance(result.error, str)
        assert len(result.error) > 0

    def test_never_raises_exception(self) -> None:
        gateway, _ = _build_query_gateway()

        # Various bad inputs — none should raise
        result1 = gateway.handle({})
        result2 = gateway.handle({"query_type": 12345})
        result3 = gateway.handle(None)  # type: ignore

        assert result1.success is False
        assert result2.success is False
        assert result3.success is False


# ---------------------------------------------------------------------------
# Tests: Architecture rule enforcement
# ---------------------------------------------------------------------------

class TestQueryGatewayArchitecture:
    """The query gateway must not import aggregates, event store, or domain logic."""

    def test_no_aggregate_imports(self) -> None:
        import ast
        from pathlib import Path

        gateway_file = (
            Path(__file__).parent.parent
            / "src" / "clinical_core" / "application" / "query_gateway.py"
        )
        source = gateway_file.read_text()
        tree = ast.parse(source)

        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module)

        aggregate_imports = [i for i in imports if "aggregate" in i.lower()]
        assert aggregate_imports == [], (
            f"Query gateway imports aggregate modules: {aggregate_imports}"
        )

    def test_no_event_store_imports(self) -> None:
        import ast
        from pathlib import Path

        gateway_file = (
            Path(__file__).parent.parent
            / "src" / "clinical_core" / "application" / "query_gateway.py"
        )
        source = gateway_file.read_text()

        assert "event_store" not in source.lower(), (
            "Query gateway references event store"
        )

    def test_no_command_handler_imports(self) -> None:
        import ast
        from pathlib import Path

        gateway_file = (
            Path(__file__).parent.parent
            / "src" / "clinical_core" / "application" / "query_gateway.py"
        )
        source = gateway_file.read_text()

        assert "command_handler" not in source.lower(), (
            "Query gateway references command handler"
        )
