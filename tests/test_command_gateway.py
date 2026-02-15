"""TDD tests for the Command Gateway skeleton.

The gateway is the single entry point for external clients to interact
with the domain. It receives raw request dicts, validates input shape,
maps to command dataclasses, routes to handlers, and returns results.

Requirements tested:
1. Receive request (dict)
2. Map request → command
3. Send command to handler
4. Return command result (success or failure, never throws)

No framework coupling — requests are plain dicts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from clinical_core.domain.events import (
    ConnectionStatus,
    ConcurrencyError,
    DomainEvent,
    EventMetadata,
)
from clinical_core.domain.aggregate import DomainError
from clinical_core.infrastructure.in_memory_event_store import InMemoryEventStore
from clinical_core.application.event_dispatcher import EventDispatcher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ORG_ID = uuid4()
_FACILITY_ID = uuid4()


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


def _setup_active_encounter(store: InMemoryEventStore, enc_id: UUID) -> None:
    store.append(_encounter_event(
        enc_id, "clinical.encounter.PatientCheckedIn", 1,
        {"patient_id": str(uuid4())},
    ))
    store.append(_encounter_event(
        enc_id, "clinical.encounter.EncounterBegan", 2,
        {"practitioner_id": str(uuid4())},
    ))


def _valid_confirm_diagnosis_request(
    encounter_id: UUID,
    diagnosis_id: UUID | None = None,
) -> dict[str, Any]:
    return {
        "command_type": "ConfirmDiagnosis",
        "payload": {
            "diagnosis_id": str(diagnosis_id or uuid4()),
            "encounter_id": str(encounter_id),
            "patient_id": str(uuid4()),
            "condition": "Hypertension",
            "icd_code": "I10",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "performed_by": str(uuid4()),
            "performer_role": "physician",
            "organization_id": str(_ORG_ID),
            "facility_id": str(_FACILITY_ID),
            "device_id": "device-001",
            "connection_status": "online",
            "correlation_id": str(uuid4()),
        },
    }


def _build_gateway(
    store: InMemoryEventStore | None = None,
    dispatcher: EventDispatcher | None = None,
):
    """Build a gateway wired to a DiagnosisCommandHandler."""
    from clinical_core.application.gateway import CommandGateway
    from clinical_core.domain.diagnosis import DiagnosisAggregate, DiagnosisCommandHandler

    store = store or InMemoryEventStore()
    dispatcher = dispatcher or EventDispatcher()

    handler = DiagnosisCommandHandler(
        event_store=store,
        dispatcher=dispatcher,
        aggregate=DiagnosisAggregate(),
        encounter_store=store,
    )

    gateway = CommandGateway()
    gateway.register("ConfirmDiagnosis", handler=handler, aggregate_id_field="diagnosis_id")

    return gateway, store, dispatcher


# ---------------------------------------------------------------------------
# Tests: Receive request
# ---------------------------------------------------------------------------

class TestReceiveRequest:

    def test_accepts_valid_request_dict(self) -> None:
        from clinical_core.application.gateway import CommandGateway

        gateway, store, _ = _build_gateway()
        enc_id = uuid4()
        _setup_active_encounter(store, enc_id)

        request = _valid_confirm_diagnosis_request(enc_id)
        result = gateway.handle(request)

        assert result.success is True

    def test_rejects_missing_command_type(self) -> None:
        from clinical_core.application.gateway import CommandGateway

        gateway, _, _ = _build_gateway()
        result = gateway.handle({"payload": {}})

        assert result.success is False
        assert "command_type" in result.error.lower()

    def test_rejects_missing_payload(self) -> None:
        from clinical_core.application.gateway import CommandGateway

        gateway, _, _ = _build_gateway()
        result = gateway.handle({"command_type": "ConfirmDiagnosis"})

        assert result.success is False
        assert "payload" in result.error.lower()

    def test_rejects_unknown_command_type(self) -> None:
        from clinical_core.application.gateway import CommandGateway

        gateway, _, _ = _build_gateway()
        result = gateway.handle({
            "command_type": "DoSomethingWeird",
            "payload": {},
        })

        assert result.success is False
        assert "unknown" in result.error.lower()


# ---------------------------------------------------------------------------
# Tests: Validate input shape
# ---------------------------------------------------------------------------

class TestValidateInputShape:

    def test_rejects_missing_required_field(self) -> None:
        gateway, store, _ = _build_gateway()
        enc_id = uuid4()
        _setup_active_encounter(store, enc_id)

        request = _valid_confirm_diagnosis_request(enc_id)
        del request["payload"]["condition"]  # remove required field

        result = gateway.handle(request)

        assert result.success is False
        assert "condition" in result.error.lower()

    def test_rejects_invalid_uuid(self) -> None:
        gateway, store, _ = _build_gateway()
        enc_id = uuid4()
        _setup_active_encounter(store, enc_id)

        request = _valid_confirm_diagnosis_request(enc_id)
        request["payload"]["diagnosis_id"] = "not-a-uuid"

        result = gateway.handle(request)

        assert result.success is False


# ---------------------------------------------------------------------------
# Tests: Map request → command
# ---------------------------------------------------------------------------

class TestMapRequestToCommand:

    def test_maps_to_correct_command_type(self) -> None:
        gateway, store, _ = _build_gateway()
        enc_id = uuid4()
        _setup_active_encounter(store, enc_id)

        request = _valid_confirm_diagnosis_request(enc_id)
        result = gateway.handle(request)

        assert result.success is True
        assert len(result.events) == 1
        assert result.events[0].event_type == "clinical.judgment.DiagnosisConfirmed"

    def test_maps_payload_fields_correctly(self) -> None:
        gateway, store, _ = _build_gateway()
        enc_id = uuid4()
        diag_id = uuid4()
        _setup_active_encounter(store, enc_id)

        request = _valid_confirm_diagnosis_request(enc_id, diagnosis_id=diag_id)
        request["payload"]["condition"] = "Type 2 Diabetes"
        request["payload"]["icd_code"] = "E11"

        result = gateway.handle(request)

        assert result.success is True
        payload = result.events[0].payload
        assert payload["condition"] == "Type 2 Diabetes"
        assert payload["icd_code"] == "E11"
        assert payload["diagnosis_id"] == str(diag_id)


# ---------------------------------------------------------------------------
# Tests: Send command to handler and return result
# ---------------------------------------------------------------------------

class TestCommandRouting:

    def test_success_returns_events(self) -> None:
        gateway, store, _ = _build_gateway()
        enc_id = uuid4()
        _setup_active_encounter(store, enc_id)

        request = _valid_confirm_diagnosis_request(enc_id)
        result = gateway.handle(request)

        assert result.success is True
        assert len(result.events) >= 1

    def test_domain_error_returns_failure(self) -> None:
        """ConfirmDiagnosis with no active encounter → DomainError."""
        gateway, store, _ = _build_gateway()
        # No encounter setup — encounter not active

        request = _valid_confirm_diagnosis_request(uuid4())
        result = gateway.handle(request)

        assert result.success is False
        assert "not active" in result.error.lower()
        assert result.events == []

    def test_never_raises_exception(self) -> None:
        """Gateway wraps all errors — never throws to caller."""
        gateway, _, _ = _build_gateway()

        # Bad request
        result = gateway.handle({})
        assert result.success is False
        assert isinstance(result.error, str)

    def test_result_has_success_flag(self) -> None:
        from clinical_core.application.gateway import GatewayResult

        gateway, store, _ = _build_gateway()
        enc_id = uuid4()
        _setup_active_encounter(store, enc_id)

        result = gateway.handle(_valid_confirm_diagnosis_request(enc_id))

        assert isinstance(result, GatewayResult)
        assert result.success is True

    def test_events_are_persisted_after_success(self) -> None:
        gateway, store, _ = _build_gateway()
        enc_id = uuid4()
        diag_id = uuid4()
        _setup_active_encounter(store, enc_id)

        request = _valid_confirm_diagnosis_request(enc_id, diagnosis_id=diag_id)
        result = gateway.handle(request)

        assert result.success is True
        assert store.stream_version(diag_id) == 1

    def test_events_dispatched_after_success(self) -> None:
        store = InMemoryEventStore()
        dispatcher = EventDispatcher()

        class SpyHandler:
            def __init__(self):
                self.received = []
            def __call__(self, event):
                self.received.append(event)

        spy = SpyHandler()
        dispatcher.subscribe("clinical.judgment.DiagnosisConfirmed", spy)

        gateway, _, _ = _build_gateway(store=store, dispatcher=dispatcher)
        enc_id = uuid4()
        _setup_active_encounter(store, enc_id)

        request = _valid_confirm_diagnosis_request(enc_id)
        gateway.handle(request)

        assert len(spy.received) == 1

    def test_failed_command_does_not_persist(self) -> None:
        gateway, store, _ = _build_gateway()
        diag_id = uuid4()

        request = _valid_confirm_diagnosis_request(uuid4(), diagnosis_id=diag_id)
        result = gateway.handle(request)

        assert result.success is False
        assert store.stream_version(diag_id) == 0
