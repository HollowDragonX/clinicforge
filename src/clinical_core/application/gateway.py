"""Command Gateway — single entry point for external clients.

The gateway accepts raw request dicts, validates input shape, maps to
command dataclasses, routes to the appropriate handler, and returns
a GatewayResult. It never throws exceptions to the caller.

Rules enforced:
- Gateway accepts commands only.
- Gateway never produces events.
- Gateway never accesses projections directly.
- Gateway performs validation of input shape only.

No framework coupling — requests are plain dicts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID

from clinical_core.domain.aggregate import DomainError
from clinical_core.domain.events import (
    ConnectionStatus,
    ConcurrencyError,
    DomainEvent,
)


@dataclass(frozen=True)
class GatewayResult:
    """Result of a gateway command invocation.

    Always returned — the gateway never throws exceptions.
    """
    success: bool
    events: list[DomainEvent] = field(default_factory=list)
    error: str = ""


@dataclass
class _CommandRegistration:
    """Internal: maps a command_type to its handler and config."""
    handler: Any
    aggregate_id_field: str
    required_fields: list[str]
    uuid_fields: list[str]


# Default field sets for the ConfirmDiagnosis command
_CONFIRM_DIAGNOSIS_REQUIRED = [
    "diagnosis_id", "encounter_id", "patient_id", "condition", "icd_code",
    "occurred_at", "performed_by", "performer_role", "organization_id",
    "facility_id", "device_id", "connection_status", "correlation_id",
]

_CONFIRM_DIAGNOSIS_UUIDS = [
    "diagnosis_id", "encounter_id", "patient_id",
    "performed_by", "organization_id", "facility_id", "correlation_id",
]


class CommandGateway:
    """Gateway that receives raw request dicts and routes to command handlers.

    Usage:
        gateway = CommandGateway()
        gateway.register("ConfirmDiagnosis", handler=diag_handler, aggregate_id_field="diagnosis_id")
        result = gateway.handle({"command_type": "ConfirmDiagnosis", "payload": {...}})
    """

    def __init__(self) -> None:
        self._registrations: dict[str, _CommandRegistration] = {}

    def register(
        self,
        command_type: str,
        handler: Any,
        aggregate_id_field: str,
        required_fields: list[str] | None = None,
        uuid_fields: list[str] | None = None,
    ) -> None:
        """Register a command type with its handler and field config."""
        # Default field definitions per known command type
        if required_fields is None:
            required_fields = _KNOWN_REQUIRED.get(command_type, [])
        if uuid_fields is None:
            uuid_fields = _KNOWN_UUIDS.get(command_type, [])

        self._registrations[command_type] = _CommandRegistration(
            handler=handler,
            aggregate_id_field=aggregate_id_field,
            required_fields=required_fields,
            uuid_fields=uuid_fields,
        )

    def handle(self, request: dict[str, Any]) -> GatewayResult:
        """Process a raw request dict and return a GatewayResult.

        Never raises exceptions — all errors are returned as GatewayResult.
        """
        try:
            return self._handle_inner(request)
        except Exception as e:
            return GatewayResult(success=False, error=str(e))

    def _handle_inner(self, request: dict[str, Any]) -> GatewayResult:
        # Step 1: Validate request envelope
        if "command_type" not in request:
            return GatewayResult(
                success=False,
                error="Missing required field: command_type",
            )
        if "payload" not in request:
            return GatewayResult(
                success=False,
                error="Missing required field: payload",
            )

        command_type = request["command_type"]
        payload = request["payload"]

        # Step 2: Check command type is registered
        if command_type not in self._registrations:
            return GatewayResult(
                success=False,
                error=f"Unknown command type: {command_type}",
            )

        reg = self._registrations[command_type]

        # Step 3: Validate input shape (required fields)
        for field_name in reg.required_fields:
            if field_name not in payload:
                return GatewayResult(
                    success=False,
                    error=f"Missing required field in payload: {field_name}",
                )

        # Step 4: Validate UUID fields
        parsed: dict[str, Any] = dict(payload)
        for field_name in reg.uuid_fields:
            if field_name in parsed:
                try:
                    parsed[field_name] = UUID(str(parsed[field_name]))
                except (ValueError, AttributeError):
                    return GatewayResult(
                        success=False,
                        error=f"Invalid UUID for field: {field_name}",
                    )

        # Step 5: Map request → command
        command = _map_command(command_type, parsed)

        # Step 6: Extract aggregate_id and route to handler
        aggregate_id = parsed[reg.aggregate_id_field]
        if not isinstance(aggregate_id, UUID):
            aggregate_id = UUID(str(aggregate_id))

        try:
            events = reg.handler.handle(command, aggregate_id=aggregate_id)
            return GatewayResult(success=True, events=events)
        except DomainError as e:
            return GatewayResult(success=False, error=str(e))
        except ConcurrencyError as e:
            return GatewayResult(success=False, error=str(e))


# ---------------------------------------------------------------------------
# Command mappers
# ---------------------------------------------------------------------------

def _map_command(command_type: str, parsed: dict[str, Any]) -> Any:
    """Map a parsed payload dict to a frozen command dataclass."""
    mapper = _MAPPERS.get(command_type)
    if mapper is None:
        raise ValueError(f"No mapper registered for command type: {command_type}")
    return mapper(parsed)


def _map_confirm_diagnosis(p: dict[str, Any]) -> Any:
    from clinical_core.domain.diagnosis import ConfirmDiagnosis

    return ConfirmDiagnosis(
        diagnosis_id=_uuid(p, "diagnosis_id"),
        encounter_id=_uuid(p, "encounter_id"),
        patient_id=_uuid(p, "patient_id"),
        condition=p["condition"],
        icd_code=p["icd_code"],
        occurred_at=_datetime(p, "occurred_at"),
        performed_by=_uuid(p, "performed_by"),
        performer_role=p["performer_role"],
        organization_id=_uuid(p, "organization_id"),
        facility_id=_uuid(p, "facility_id"),
        device_id=p["device_id"],
        connection_status=ConnectionStatus(p["connection_status"]),
        correlation_id=_uuid(p, "correlation_id"),
    )


def _uuid(p: dict, key: str) -> UUID:
    v = p[key]
    return v if isinstance(v, UUID) else UUID(str(v))


def _datetime(p: dict, key: str) -> datetime:
    v = p[key]
    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v))


# ---------------------------------------------------------------------------
# Registry tables
# ---------------------------------------------------------------------------

_MAPPERS: dict[str, Callable] = {
    "ConfirmDiagnosis": _map_confirm_diagnosis,
}

_KNOWN_REQUIRED: dict[str, list[str]] = {
    "ConfirmDiagnosis": _CONFIRM_DIAGNOSIS_REQUIRED,
}

_KNOWN_UUIDS: dict[str, list[str]] = {
    "ConfirmDiagnosis": _CONFIRM_DIAGNOSIS_UUIDS,
}
