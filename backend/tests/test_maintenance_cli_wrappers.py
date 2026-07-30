"""Safety and backend-selection contracts for legacy maintenance wrappers."""
from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys
from contextlib import contextmanager

import pytest

from tests.architecture.repository_callers import production_source_index


ROOT = Path(__file__).resolve().parents[2]
WRAPPER_PATHS = {
    "backend/app/scripts/build_kg.py",
    "backend/app/scripts/recluster_kg.py",
    "backend/app/scripts/reembed_kg.py",
    "backend/app/scripts/backfill_relation_embeddings.py",
    "scripts/build_chunks.py",
    "scripts/backfill_kg_embeddings.py",
    "scripts/reextract_notebook.py",
    "scripts/denoise_reextract_nb.py",
}


def _load_root_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"test_{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("module_name", "argv", "settings_name"),
    [
        ("app.scripts.build_kg", ["nb-x"], "get_settings"),
        ("app.scripts.recluster_kg", ["nb-x"], "get_settings"),
        ("app.scripts.reembed_kg", ["nb-x"], "get_settings"),
        ("app.scripts.backfill_relation_embeddings", ["nb-x"], "get_settings"),
    ],
)
def test_backend_wrappers_reject_unconfirmed_postgres_before_opening_repository(
    monkeypatch, module_name, argv, settings_name
):
    module = importlib.import_module(module_name)
    settings = SimpleNamespace(database_url="postgresql://localhost/silicon_notebook")
    monkeypatch.setattr(module, settings_name, lambda: settings)
    monkeypatch.setattr(sys, "argv", [module_name, *argv])

    assert module.main() == 2


@pytest.mark.parametrize(
    ("script_name", "argv"),
    [
        ("build_chunks", ["nb-x"]),
        ("backfill_kg_embeddings", ["nb-x"]),
        ("reextract_notebook", ["nb-x"]),
        ("denoise_reextract_nb", ["--notebook-id", "nb-x", "--full"]),
    ],
)
def test_root_wrappers_reject_unconfirmed_postgres_before_opening_repository(
    monkeypatch, script_name, argv
):
    module = _load_root_script(script_name)
    settings = SimpleNamespace(database_url="postgresql://localhost/silicon_notebook")
    monkeypatch.setattr(module, "Settings", lambda: settings)
    monkeypatch.setattr(sys, "argv", [script_name, *argv])

    assert module.main() == 2


@pytest.mark.architecture_contract
def test_maintenance_wrappers_use_the_backend_neutral_cli_opener_only():
    imports = production_source_index().imports()
    wrapper_imports = [item for item in imports if item.key.path in WRAPPER_PATHS]

    assert not [
        item
        for item in wrapper_imports
        if item.key.target
        in {"app.services.sqlite_repository", "app.services.sqlite_repository:SQLiteRepository"}
    ]
    opened = {
        item.key.path
        for item in wrapper_imports
        if item.key.target
        == "app.services.maintenance_cli:open_maintenance_cli_repository"
    }
    assert opened == WRAPPER_PATHS


def test_shared_preflight_rejects_postgres_before_constructing_repository(monkeypatch):
    from app.services import maintenance_cli

    settings = SimpleNamespace(database_url="postgresql://localhost/silicon_notebook")
    monkeypatch.setattr(
        maintenance_cli,
        "create_repository",
        lambda _settings: pytest.fail("repository constructed"),
    )

    with pytest.raises(maintenance_cli.MaintenanceCliUsageError):
        with maintenance_cli.open_maintenance_cli_repository(
            settings, confirm_service_stopped=False
        ):
            pytest.fail("entered")


def test_shared_maintenance_opener_holds_lock_and_closes_repository(monkeypatch):
    from app.services import maintenance_cli

    events: list[str] = []

    class _Maintenance:
        @contextmanager
        def offline_maintenance_lock(self):
            events.append("lock")
            try:
                yield
            finally:
                events.append("unlock")

    class _Repository:
        maintenance = _Maintenance()

        def close(self):
            events.append("close")

    monkeypatch.setattr(maintenance_cli, "create_repository", lambda _settings: _Repository())
    settings = SimpleNamespace(database_url="sqlite:////tmp/maintenance-test.db")

    with maintenance_cli.open_maintenance_cli_repository(
        settings, confirm_service_stopped=False
    ):
        events.append("body")

    assert events == ["lock", "body", "unlock", "close"]


def test_reembed_kg_reads_and_embeds_object_payloads_by_keyset_page(monkeypatch, capsys):
    from app.scripts import reembed_kg

    pages = {
        "": [
            {"id": "ko-1", "payload": {"name": "one"}},
            {"id": "ko-2", "payload": {"name": "two"}},
        ],
        "ko-2": [{"id": "ko-3", "payload": {"name": "three"}}],
    }
    embedded: list[list[str]] = []

    class _Maintenance:
        def purge_kg_embeddings(self, notebook_id):
            assert notebook_id == "nb-x"

        def knowledge_object_payload_page(self, notebook_id, **kwargs):
            assert notebook_id == "nb-x"
            assert kwargs["include_deprecated"] is True
            return pages.get(kwargs["after_id"], [])

        def embed_objects_batch(self, notebook_id, items):
            assert notebook_id == "nb-x"
            embedded.append([item["_oid"] for item in items])

        def backfill_relation_embeddings(self, notebook_id):
            assert notebook_id == "nb-x"

        def mark_unified_kg_dirty(self, notebook_id):
            assert notebook_id == "nb-x"

    class _Repository:
        maintenance = _Maintenance()

        @staticmethod
        def configured(workload):
            return workload in {"knowledge_object_embedding", "relation_embedding"}

    @contextmanager
    def _open(_settings, *, confirm_service_stopped):
        assert confirm_service_stopped is False
        yield _Repository()

    monkeypatch.setattr(reembed_kg, "get_settings", lambda: object())
    monkeypatch.setattr(reembed_kg, "open_maintenance_cli_repository", _open)
    monkeypatch.setattr(reembed_kg, "_PAGE_SIZE", 2)
    monkeypatch.setattr(sys, "argv", ["reembed_kg", "nb-x"])

    assert reembed_kg.main() == 0
    assert embedded == [["ko-1", "ko-2"], ["ko-3"]]
    assert "re-embedded 3 objects" in capsys.readouterr().out


def test_backfill_uses_missing_node_count_not_total_embedding_count(monkeypatch):
    module = _load_root_script("backfill_kg_embeddings")
    calls = 0

    class _Maintenance:
        @staticmethod
        def node_embedding_counts(_notebook_id):
            # Legacy/deprecated vectors make this exceed current active objects.
            return (1, 2)

        @staticmethod
        def count_missing_node_vectors(_notebook_id):
            return 1

        @staticmethod
        def backfill_node_embeddings(_notebook_id):
            nonlocal calls
            calls += 1

    class _Repository:
        maintenance = _Maintenance()

        @staticmethod
        def configured(workload):
            return workload == "knowledge_object_embedding"

    @contextmanager
    def _open(_settings, *, confirm_service_stopped):
        assert confirm_service_stopped is False
        yield _Repository()

    monkeypatch.setattr(module, "Settings", lambda: object())
    monkeypatch.setattr(module, "open_maintenance_cli_repository", _open)
    monkeypatch.setattr(module, "MAX_ROUNDS", 1)
    monkeypatch.setattr(module, "SLEEP", 0)
    monkeypatch.setattr(sys, "argv", ["backfill_kg_embeddings", "nb-x"])

    assert module.main() == 1
    assert calls == 1
