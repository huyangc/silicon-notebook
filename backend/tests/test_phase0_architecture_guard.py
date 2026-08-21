from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
GUARD_PATH = ROOT / "scripts" / "check_architecture_boundaries.py"
_SPEC = importlib.util.spec_from_file_location("phase0_architecture_guard", GUARD_PATH)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - impossible checkout
    raise RuntimeError(f"cannot load architecture guard from {GUARD_PATH}")
_GUARD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_GUARD)

boundary_violations = _GUARD.boundary_violations
facade_surface_additions = _GUARD.facade_surface_additions
import_graph = _GUARD.import_graph
public_class_surface = _GUARD.public_class_surface
repository_service_import_violations = (
    _GUARD.repository_service_import_violations
)
strongly_connected_components = _GUARD.strongly_connected_components


def _write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def test_guard_detects_contract_moved_back_into_services(tmp_path):
    app = tmp_path / "app"
    _write(app / "__init__.py", "")
    _write(app / "repositories/__init__.py", "")
    _write(
        app / "repositories/ports.py",
        "from app.services.retrieval import RetrievedChunk\n",
    )
    _write(app / "services/__init__.py", "")
    _write(
        app / "services/retrieval.py",
        "from app.repositories.ports import RepositoryPort\n",
    )

    violations = boundary_violations(app)
    components = strongly_connected_components(import_graph(app))

    assert any("imports service" in item for item in violations)
    assert components == [
        ("app.repositories.ports", "app.services.retrieval")
    ]


def test_guard_detects_domain_contract_moved_to_an_adapter(tmp_path):
    app = tmp_path / "app"
    _write(app / "__init__.py", "")
    _write(app / "domain/__init__.py", "")
    _write(
        app / "domain/retrieval.py",
        "from app.repositories.sqlite.query_store import QueryStore\n",
    )

    assert boundary_violations(app) == [
        "app.domain.retrieval imports forbidden app.repositories.sqlite.query_store"
    ]


def test_facade_freeze_rejects_addition_but_allows_surface_reduction(tmp_path):
    path = tmp_path / "facade.py"
    _write(
        path,
        "class RepositoryFacade:\n"
        "    def existing(self): pass\n"
        "    def second(self): pass\n",
    )
    allowed = public_class_surface(path, "RepositoryFacade")

    _write(
        path,
        "class RepositoryFacade:\n"
        "    def existing(self): pass\n"
        "    def second(self): pass\n"
        "    def added(self): pass\n",
    )
    assert facade_surface_additions(path, "RepositoryFacade", allowed) == ["added"]

    _write(path, "class RepositoryFacade:\n    def existing(self): pass\n")
    assert facade_surface_additions(path, "RepositoryFacade", allowed) == []


def test_repository_service_reverse_import_ceiling_only_allows_reduction(tmp_path):
    app = tmp_path / "app"
    _write(app / "repositories/sqlite/query_store.py", "")
    _write(app / "repositories/postgres/query_store.py", "")

    assert repository_service_import_violations(
        app, {"sqlite": 0, "postgres": 0}
    ) == []

    _write(
        app / "repositories/sqlite/query_store.py",
        "from app.services.retrieval import RetrievedChunk\n",
    )
    assert repository_service_import_violations(
        app, {"sqlite": 0, "postgres": 0}
    ) == ["repositories/sqlite service imports grew: 1 > 0"]


def test_empty_registry_cannot_change_existing_workflow_import_paths():
    violations = boundary_violations(ROOT / "backend/app")

    assert not [item for item in violations if "empty registry" in item]
    for path in (ROOT / "backend/app/services").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "app.extensions" not in source
        assert "app.extension_sdk" not in source


def test_guard_rejects_existing_workflow_consuming_empty_registry(tmp_path):
    app = tmp_path / "app"
    _write(app / "services/workflow.py", "from app.extensions import registry\n")

    assert boundary_violations(app) == [
        "app.services.workflow consumes the Phase-0 empty registry; "
        "existing workflows must stay untouched"
    ]


def test_phase0_boundary_is_present_in_all_agent_entry_documents():
    for name in ("README.md", "README_zh.md", "AGENTS.md", "CLAUDE.md"):
        normalized = (
            (ROOT / name)
            .read_text(encoding="utf-8")
            .casefold()
            .replace("_", " ")
            .replace("-", " ")
        )
        assert "phase 0" in normalized, name
        assert "extension sdk" in normalized, name
