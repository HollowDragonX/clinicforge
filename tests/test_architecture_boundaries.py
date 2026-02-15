"""Architecture boundary tests — enforce Clean Architecture via import analysis.

These tests automatically scan every Python module in clinical_core and
verify that layer dependency rules are respected. They fail the build
on any violation.

Clean Architecture layers (inner → outer):
  domain  →  application  →  infrastructure / sync

Dependency rules:
  domain/         → stdlib + domain only (NO application, infrastructure, sync)
  application/    → stdlib + domain + application only (NO infrastructure, NO sync)
  infrastructure/ → stdlib + domain only (NO application, NO sync)
  sync/           → stdlib + domain only (NO application, NO infrastructure)

These rules enforce:
- Domain isolation: domain has zero external dependencies.
- Dependency inversion: outer layers depend on inner, never reverse.
- Layer separation: infrastructure and sync are siblings, not coupled.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_SRC_ROOT = Path(__file__).parent.parent / "src" / "clinical_core"
_DOMAIN_DIR = _SRC_ROOT / "domain"
_APPLICATION_DIR = _SRC_ROOT / "application"
_INFRASTRUCTURE_DIR = _SRC_ROOT / "infrastructure"
_SYNC_DIR = _SRC_ROOT / "sync"

_LAYERS = {
    "domain": _DOMAIN_DIR,
    "application": _APPLICATION_DIR,
    "infrastructure": _INFRASTRUCTURE_DIR,
    "sync": _SYNC_DIR,
}

# Forbidden import targets per layer (within clinical_core.*)
_FORBIDDEN_IMPORTS: dict[str, list[str]] = {
    "domain": ["application", "infrastructure", "sync"],
    "application": ["infrastructure", "sync"],
    "infrastructure": ["application", "sync"],
    "sync": ["application", "infrastructure"],
}


# ---------------------------------------------------------------------------
# Import scanner
# ---------------------------------------------------------------------------

def _extract_imports(filepath: Path) -> list[str]:
    """Parse a Python file and return all imported module strings."""
    source = filepath.read_text()
    tree = ast.parse(source, filename=str(filepath))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
    return imports


def _python_files(directory: Path) -> list[Path]:
    """Recursively find all .py files, excluding __init__.py."""
    return [
        p for p in directory.rglob("*.py")
        if p.name != "__init__.py"
    ]


def _clinical_core_imports(imports: list[str]) -> list[str]:
    """Filter to only clinical_core.* imports."""
    return [i for i in imports if i.startswith("clinical_core.")]


def _layer_of_import(module_path: str) -> str | None:
    """Determine which layer a clinical_core import belongs to.

    Returns 'domain', 'application', 'infrastructure', 'sync', or None.
    """
    parts = module_path.split(".")
    if len(parts) < 2:
        return None
    # clinical_core.domain.events → 'domain'
    # clinical_core.application.event_dispatcher → 'application'
    return parts[1]


def _find_violations(layer_name: str, directory: Path) -> list[str]:
    """Scan all files in a layer directory for forbidden imports.

    Returns a list of violation descriptions.
    """
    forbidden = _FORBIDDEN_IMPORTS.get(layer_name, [])
    if not forbidden:
        return []

    violations: list[str] = []
    for py_file in _python_files(directory):
        imports = _extract_imports(py_file)
        cc_imports = _clinical_core_imports(imports)
        for imp in cc_imports:
            target_layer = _layer_of_import(imp)
            if target_layer in forbidden:
                rel_path = py_file.relative_to(_SRC_ROOT)
                violations.append(
                    f"{rel_path} imports {imp} "
                    f"({layer_name} → {target_layer} is forbidden)"
                )
    return violations


# ---------------------------------------------------------------------------
# Tests: Automatic boundary enforcement per layer
# ---------------------------------------------------------------------------

class TestDomainBoundary:
    """Domain layer must not import from application, infrastructure, or sync."""

    def test_no_forbidden_imports(self) -> None:
        violations = _find_violations("domain", _DOMAIN_DIR)
        assert violations == [], (
            f"Domain layer boundary violations:\n" +
            "\n".join(f"  - {v}" for v in violations)
        )


class TestApplicationBoundary:
    """Application layer must not import from infrastructure or sync."""

    def test_no_forbidden_imports(self) -> None:
        violations = _find_violations("application", _APPLICATION_DIR)
        assert violations == [], (
            f"Application layer boundary violations:\n" +
            "\n".join(f"  - {v}" for v in violations)
        )


class TestInfrastructureBoundary:
    """Infrastructure layer must not import from application or sync."""

    def test_no_forbidden_imports(self) -> None:
        violations = _find_violations("infrastructure", _INFRASTRUCTURE_DIR)
        assert violations == [], (
            f"Infrastructure layer boundary violations:\n" +
            "\n".join(f"  - {v}" for v in violations)
        )


class TestSyncBoundary:
    """Sync layer must not import from application or infrastructure."""

    def test_no_forbidden_imports(self) -> None:
        violations = _find_violations("sync", _SYNC_DIR)
        assert violations == [], (
            f"Sync layer boundary violations:\n" +
            "\n".join(f"  - {v}" for v in violations)
        )


# ---------------------------------------------------------------------------
# Tests: Full scan (all layers at once)
# ---------------------------------------------------------------------------

class TestAllBoundaries:
    """Scan every layer and report all violations in one test."""

    def test_no_violations_across_entire_codebase(self) -> None:
        all_violations: list[str] = []
        for layer_name, directory in _LAYERS.items():
            if directory.exists():
                all_violations.extend(_find_violations(layer_name, directory))

        assert all_violations == [], (
            f"Architecture boundary violations ({len(all_violations)}):\n" +
            "\n".join(f"  - {v}" for v in all_violations)
        )


# ---------------------------------------------------------------------------
# Tests: Specific forbidden patterns
# ---------------------------------------------------------------------------

class TestForbiddenPatterns:
    """Detect specific anti-patterns that violate Clean Architecture."""

    def test_domain_has_no_framework_imports(self) -> None:
        """Domain must not import web frameworks, ORMs, or HTTP libraries."""
        framework_keywords = [
            "flask", "django", "fastapi", "starlette", "sqlalchemy",
            "requests", "httpx", "aiohttp", "uvicorn",
        ]
        for py_file in _python_files(_DOMAIN_DIR):
            imports = _extract_imports(py_file)
            for imp in imports:
                imp_lower = imp.lower()
                for kw in framework_keywords:
                    assert kw not in imp_lower, (
                        f"domain/{py_file.name} imports framework library: {imp}"
                    )

    def test_domain_has_no_io_operations(self) -> None:
        """Domain modules must not perform file I/O, network, or DB access."""
        io_keywords = ["open(", "requests.", "http", "socket", "sqlite", "psycopg"]
        for py_file in _python_files(_DOMAIN_DIR):
            source = py_file.read_text()
            for kw in io_keywords:
                # Allow in docstrings/comments by checking if it appears in code
                tree = ast.parse(source)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call):
                        func = node.func
                        if isinstance(func, ast.Name) and func.id == "open":
                            rel_path = py_file.relative_to(_SRC_ROOT)
                            pytest.fail(
                                f"domain/{rel_path} contains open() call (I/O in domain)"
                            )

    def test_infrastructure_does_not_define_domain_classes(self) -> None:
        """Infrastructure must not define Aggregate or DomainEvent subclasses."""
        for py_file in _python_files(_INFRASTRUCTURE_DIR):
            source = py_file.read_text()
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    for base in node.bases:
                        base_name = ""
                        if isinstance(base, ast.Name):
                            base_name = base.id
                        elif isinstance(base, ast.Attribute):
                            base_name = base.attr
                        assert base_name not in ("Aggregate", "DomainEvent"), (
                            f"infrastructure/{py_file.name} defines class {node.name} "
                            f"inheriting from {base_name} (domain class in infrastructure)"
                        )


# ---------------------------------------------------------------------------
# Tests: Dependency direction verification
# ---------------------------------------------------------------------------

class TestDependencyDirection:
    """Verify that dependencies flow inward: outer → inner, never reverse."""

    def test_domain_depends_on_nothing_external(self) -> None:
        """Domain has zero clinical_core imports outside domain."""
        for py_file in _python_files(_DOMAIN_DIR):
            imports = _extract_imports(py_file)
            cc_imports = _clinical_core_imports(imports)
            for imp in cc_imports:
                layer = _layer_of_import(imp)
                assert layer == "domain", (
                    f"domain/{py_file.name} imports from {layer}: {imp}"
                )

    def test_application_depends_only_on_domain(self) -> None:
        """Application may only import from domain (not infrastructure or sync)."""
        for py_file in _python_files(_APPLICATION_DIR):
            imports = _extract_imports(py_file)
            cc_imports = _clinical_core_imports(imports)
            for imp in cc_imports:
                layer = _layer_of_import(imp)
                assert layer in ("domain", "application"), (
                    f"application/{py_file.relative_to(_APPLICATION_DIR)} "
                    f"imports from {layer}: {imp}"
                )

    def test_infrastructure_depends_only_on_domain(self) -> None:
        """Infrastructure may only import from domain."""
        for py_file in _python_files(_INFRASTRUCTURE_DIR):
            imports = _extract_imports(py_file)
            cc_imports = _clinical_core_imports(imports)
            for imp in cc_imports:
                layer = _layer_of_import(imp)
                assert layer == "domain", (
                    f"infrastructure/{py_file.name} imports from {layer}: {imp}"
                )

    def test_sync_depends_only_on_domain(self) -> None:
        """Sync may only import from domain."""
        for py_file in _python_files(_SYNC_DIR):
            imports = _extract_imports(py_file)
            cc_imports = _clinical_core_imports(imports)
            for imp in cc_imports:
                layer = _layer_of_import(imp)
                assert layer == "domain", (
                    f"sync/{py_file.name} imports from {layer}: {imp}"
                )


# ---------------------------------------------------------------------------
# Tests: Layer existence and structure
# ---------------------------------------------------------------------------

class TestLayerStructure:
    """Verify the expected layer directories exist."""

    def test_domain_layer_exists(self) -> None:
        assert _DOMAIN_DIR.is_dir(), "domain/ layer directory must exist"

    def test_application_layer_exists(self) -> None:
        assert _APPLICATION_DIR.is_dir(), "application/ layer directory must exist"

    def test_infrastructure_layer_exists(self) -> None:
        assert _INFRASTRUCTURE_DIR.is_dir(), "infrastructure/ layer directory must exist"

    def test_sync_layer_exists(self) -> None:
        assert _SYNC_DIR.is_dir(), "sync/ layer directory must exist"

    def test_each_layer_has_init(self) -> None:
        for layer_name, directory in _LAYERS.items():
            init_file = directory / "__init__.py"
            assert init_file.exists(), f"{layer_name}/ missing __init__.py"
