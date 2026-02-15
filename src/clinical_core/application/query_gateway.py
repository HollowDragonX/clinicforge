"""Query Gateway — single entry point for reading clinical information.

The Query Gateway exposes projection state to external clients. It is the
read-side counterpart to the Command Gateway.

Rules enforced:
- Gateway reads ONLY projections.
- Gateway never accesses aggregates.
- Gateway never reads the event store directly.
- Gateway contains no business logic.

No framework coupling — queries are plain dicts, responses are QueryResult.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class QueryResult:
    """Result of a query gateway invocation.

    Always returned — the gateway never throws exceptions.
    """
    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass
class _QueryRegistration:
    """Internal: maps a query_type to its projection and response mapper."""
    projection: Any
    mapper: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


class QueryGateway:
    """Gateway that receives raw query dicts and returns projection data.

    Usage:
        gateway = QueryGateway()
        gateway.register("PatientSummary", projection=proj, mapper=my_mapper)
        result = gateway.handle({"query_type": "PatientSummary", "params": {...}})
    """

    def __init__(self) -> None:
        self._registrations: dict[str, _QueryRegistration] = {}

    def register(
        self,
        query_type: str,
        projection: Any,
        mapper: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    ) -> None:
        """Register a query type with its projection and response mapper."""
        self._registrations[query_type] = _QueryRegistration(
            projection=projection,
            mapper=mapper,
        )

    def handle(self, request: Any) -> QueryResult:
        """Process a raw query request and return a QueryResult.

        Never raises exceptions — all errors are returned as QueryResult.
        """
        try:
            return self._handle_inner(request)
        except Exception as e:
            return QueryResult(success=False, error=str(e))

    def _handle_inner(self, request: Any) -> QueryResult:
        # Step 1: Validate request envelope
        if not isinstance(request, dict):
            return QueryResult(
                success=False,
                error="Request must be a dict",
            )

        if "query_type" not in request:
            return QueryResult(
                success=False,
                error="Missing required field: query_type",
            )

        query_type = request["query_type"]

        if not isinstance(query_type, str):
            return QueryResult(
                success=False,
                error=f"query_type must be a string, got {type(query_type).__name__}",
            )

        # Step 2: Check query type is registered
        if query_type not in self._registrations:
            return QueryResult(
                success=False,
                error=f"Unknown query type: {query_type}",
            )

        reg = self._registrations[query_type]
        params = request.get("params", {})
        if not isinstance(params, dict):
            params = {}

        # Step 3: Fetch projection state and map to response
        projection_state = reg.projection.state
        response_data = reg.mapper(projection_state, params)

        # Step 4: Return result
        return QueryResult(success=True, data=response_data)
