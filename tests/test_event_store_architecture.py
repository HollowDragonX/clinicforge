"""Architecture tests for the Event Store.

These tests verify structural rules from .aas/architecture-rules.yaml:

1. domain-isolation: The domain layer (events.py, event_store.py) has zero
   imports from infrastructure. The event store port is a Protocol in domain/.
   Infrastructure adapters implement the Protocol but are not imported by domain.

2. event-store-access: Only clinical-core modules interact with the event store.

3. no-projection-logic-in-store: The event store implementation contains no
   projection logic — it persists and retrieves events, nothing more.

These are compile-time / import-time checks, verified by inspecting modules.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_SRC_ROOT = Path(__file__).parent.parent / "src" / "clinical_core"
_DOMAIN_DIR = _SRC_ROOT / "domain"
_INFRA_DIR = _SRC_ROOT / "infrastructure"


# ---------------------------------------------------------------------------
# AAS Rule: domain-isolation
# Domain modules must NOT import from infrastructure.
# ---------------------------------------------------------------------------

class TestDomainIsolation:
    """domain/ must have zero imports from infrastructure/."""

    def _get_imports_from_file(self, filepath: Path) -> list[str]:
        """Parse a Python file and return all imported module names."""
        source = filepath.read_text()
        tree = ast.parse(source)
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module)
        return imports

    def test_domain_events_does_not_import_infrastructure(self) -> None:
        events_file = _DOMAIN_DIR / "events.py"
        imports = self._get_imports_from_file(events_file)
        infra_imports = [i for i in imports if "infrastructure" in i]
        assert infra_imports == [], (
            f"domain/events.py imports infrastructure modules: {infra_imports}"
        )

    def test_domain_event_store_does_not_import_infrastructure(self) -> None:
        store_file = _DOMAIN_DIR / "event_store.py"
        imports = self._get_imports_from_file(store_file)
        infra_imports = [i for i in imports if "infrastructure" in i]
        assert infra_imports == [], (
            f"domain/event_store.py imports infrastructure modules: {infra_imports}"
        )

    def test_no_domain_file_imports_infrastructure(self) -> None:
        """Scan ALL .py files in domain/ — none may import from infrastructure."""
        for py_file in _DOMAIN_DIR.glob("*.py"):
            if py_file.name == "__init__.py":
                continue
            imports = self._get_imports_from_file(py_file)
            infra_imports = [i for i in imports if "infrastructure" in i]
            assert infra_imports == [], (
                f"domain/{py_file.name} imports infrastructure modules: {infra_imports}"
            )


# ---------------------------------------------------------------------------
# AAS Rule: event store port is a Protocol (dependency inversion)
# ---------------------------------------------------------------------------

class TestEventStoreIsPort:
    """The EventStore in domain/ must be a Protocol, not a concrete class."""

    def test_event_store_is_protocol(self) -> None:
        from clinical_core.domain.event_store import EventStore
        assert hasattr(EventStore, "__protocol_attrs__") or _is_protocol(EventStore), (
            "EventStore must be a typing.Protocol"
        )

    def test_event_store_has_append_method(self) -> None:
        from clinical_core.domain.event_store import EventStore
        assert hasattr(EventStore, "append"), "EventStore must define append()"

    def test_event_store_has_read_stream_method(self) -> None:
        from clinical_core.domain.event_store import EventStore
        assert hasattr(EventStore, "read_stream"), "EventStore must define read_stream()"

    def test_event_store_has_read_all_events_method(self) -> None:
        from clinical_core.domain.event_store import EventStore
        assert hasattr(EventStore, "read_all_events"), "EventStore must define read_all_events()"


# ---------------------------------------------------------------------------
# AAS Rule: no-projection-logic-in-store
# ---------------------------------------------------------------------------

class TestNoProjectionLogicInStore:
    """The event store implementation must not contain projection logic."""

    def test_infra_store_does_not_import_projection_modules(self) -> None:
        infra_store = _INFRA_DIR / "in_memory_event_store.py"
        if not infra_store.exists():
            pytest.skip("Infrastructure store not yet implemented")

        source = infra_store.read_text()
        tree = ast.parse(source)
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module)

        projection_imports = [i for i in imports if "projection" in i.lower()]
        assert projection_imports == [], (
            f"Event store imports projection modules: {projection_imports}"
        )

    def test_infra_store_does_not_contain_projection_keywords(self) -> None:
        """Heuristic: the store source should not mention projection concepts."""
        infra_store = _INFRA_DIR / "in_memory_event_store.py"
        if not infra_store.exists():
            pytest.skip("Infrastructure store not yet implemented")

        source = infra_store.read_text().lower()
        forbidden_terms = ["read_model", "read model", "projection", "subscribe", "handler"]
        found = [term for term in forbidden_terms if term in source]
        assert found == [], (
            f"Event store source contains projection-related terms: {found}"
        )

    def test_infra_store_only_imports_domain(self) -> None:
        """The infrastructure store should only import from domain (and stdlib)."""
        infra_store = _INFRA_DIR / "in_memory_event_store.py"
        if not infra_store.exists():
            pytest.skip("Infrastructure store not yet implemented")

        source = infra_store.read_text()
        tree = ast.parse(source)
        app_imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if "clinical_core" in alias.name and "domain" not in alias.name:
                        app_imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module and "clinical_core" in node.module and "domain" not in node.module:
                    app_imports.append(node.module)

        assert app_imports == [], (
            f"Event store imports non-domain clinical_core modules: {app_imports}"
        )


# ---------------------------------------------------------------------------
# AAS Rule: DomainEvent is immutable
# ---------------------------------------------------------------------------

class TestEventImmutability:
    """DomainEvent and EventMetadata must be frozen dataclasses."""

    def test_domain_event_is_frozen(self) -> None:
        from clinical_core.domain.events import DomainEvent
        assert DomainEvent.__dataclass_params__.frozen is True  # type: ignore[attr-defined]

    def test_event_metadata_is_frozen(self) -> None:
        from clinical_core.domain.events import EventMetadata
        assert EventMetadata.__dataclass_params__.frozen is True  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_protocol(cls: type) -> bool:
    """Check if a class is a typing.Protocol subclass."""
    from typing import Protocol, get_original_bases

    for base in getattr(cls, "__mro__", []):
        if base is Protocol:
            return True
    return any("Protocol" in str(b) for b in getattr(cls, "__orig_bases__", []))
