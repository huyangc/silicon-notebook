from __future__ import annotations

from pathlib import Path

from scripts.check_architecture_boundaries import (
    boundary_violations,
    import_graph,
    public_class_surface,
    strongly_connected_components,
    surface_digest,
)


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


def test_facade_freeze_detects_both_addition_and_deletion_mutations(tmp_path):
    path = tmp_path / "facade.py"
    _write(
        path,
        "class RepositoryFacade:\n"
        "    def existing(self): pass\n"
        "    def second(self): pass\n",
    )
    baseline = surface_digest(public_class_surface(path, "RepositoryFacade"))

    _write(
        path,
        "class RepositoryFacade:\n"
        "    def existing(self): pass\n"
        "    def second(self): pass\n"
        "    def added(self): pass\n",
    )
    assert surface_digest(public_class_surface(path, "RepositoryFacade")) != baseline

    _write(path, "class RepositoryFacade:\n    def existing(self): pass\n")
    assert surface_digest(public_class_surface(path, "RepositoryFacade")) != baseline


def test_empty_registry_cannot_change_existing_workflow_import_paths():
    root = Path(__file__).resolve().parents[2]
    violations = boundary_violations(root / "backend/app")

    assert not [item for item in violations if "empty registry" in item]
    for path in (root / "backend/app/services").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "app.extensions" not in source
        assert "app.extension_sdk" not in source

