"""W-CLI T-W2 — the offline scale-build CLI against a real PostgreSQL database.

Three things only a live database can demonstrate:

* the ``migrate=False`` / ``seed=False`` composition seam actually holds — this
  is the review's heaviest finding, because a composition that migrates applies
  DDL the running service never asked for, and one that seeds silently rewrites
  the production admin credential with a fresh salt;
* the migration-ledger preflight refuses a checkout that does not match the
  schema in the database;
* the full ``build → export → import → inspect`` loop produces a consistent,
  re-publishable artifact.
"""
from __future__ import annotations

import json

import psycopg
import pytest

from app.core.config import Settings
from app.repositories.postgres.repository import PostgresRepository
from app.services import batch_ingest as bi
from app.services import maintenance_cli
from app.services import scale_build_cli as cli
from app.services.embedding import FakeEmbedder
from tests.model_testkit import RecordingModelProvider


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_offline_maintenance"),
]


class _KgChat:
    configured = True
    model = "test-kg"

    def chat_json(self, messages, response_schema_hint, **_kwargs):
        return json.dumps({
            "nodes": [
                {
                    "local_id": "concept-1",
                    "type": "Concept",
                    "name": "offline scale build",
                    "ev": 0,
                }
            ],
            "edges": [],
        })


class _PaperChat:
    configured = True
    model = "test-paper"

    def chat_json(self, messages, response_schema_hint, **_kwargs):
        return json.dumps({"is_paper": False})


def _provider() -> RecordingModelProvider:
    embedder = FakeEmbedder(dim=16)
    workloads = (
        "retrieval_query_embedding",
        "source_element_embedding",
        "chunk_embedding",
        "knowledge_object_embedding",
        "relation_embedding",
        "memory_embedding",
        "knowhow_embedding",
    )
    return RecordingModelProvider(
        chat_clients={"kg_extract": _KgChat(), "paper_metadata": _PaperChat()},
        embedding_clients={workload: embedder for workload in workloads},
        parallelism_by_workload={workload: 2 for workload in workloads},
    )


def _query(url: str, sql: str):
    with psycopg.connect(url, autocommit=True) as connection:
        return connection.execute(sql).fetchone()


def _relation_exists(url: str, name: str) -> bool:
    return bool(_query(url, f"SELECT to_regclass('{name}') IS NOT NULL")[0])


# ─────────────────────────────────────────── the schema-ownership seam ──

def test_a_composition_with_migrate_off_never_touches_a_fresh_schema(
    postgres_scope, postgres_settings
):
    """Mutation anchor: make ``migrate`` unconditional and this goes red.

    An off-host builder composes against the LIVE database of a running
    service. Applying this checkout's migrations there is a schema change
    nobody scheduled — the stopped-service gate used to make it impossible, and
    an online companion process has no such gate.
    """
    assert not _relation_exists(postgres_scope.url, "silicon_schema_migrations")

    repository = PostgresRepository(
        postgres_settings, model_provider=_provider(), migrate=False, seed=False
    )
    repository.close()

    assert not _relation_exists(postgres_scope.url, "silicon_schema_migrations")
    assert not _relation_exists(postgres_scope.url, "users")

    # The default is unchanged: a composition that owns the schema still migrates.
    owner = PostgresRepository(postgres_settings, model_provider=_provider())
    owner.close()
    assert _relation_exists(postgres_scope.url, "silicon_schema_migrations")


def test_a_composition_with_seed_off_never_writes_the_admin_credential(
    postgres_scope, postgres_settings
):
    """Mutation anchor: make ``seed`` unconditional and this goes red.

    ``bundle._initialize``'s ``UPDATE users`` is unconditional and re-hashes
    ``settings.admin_password`` with a fresh random salt on every composition.
    An off-host run whose environment lacks ``ADMIN_PASSWORD`` would therefore
    reset the production admin password to the default, silently.
    """
    owner = PostgresRepository(postgres_settings, model_provider=_provider())
    owner.close()
    seeded = _query(
        postgres_scope.url,
        "SELECT count(*) AS n, max(password_hash) AS hash FROM users",
    )
    assert seeded[0] == 1

    other_password = postgres_settings.model_copy(
        update={"admin_password": "a-different-admin-password"}
    )
    companion = PostgresRepository(
        other_password, model_provider=_provider(), migrate=False, seed=False
    )
    companion.close()

    after = _query(
        postgres_scope.url,
        "SELECT count(*) AS n, max(password_hash) AS hash FROM users",
    )
    assert after[0] == 1
    assert after[1] == seeded[1], "the admin credential must be untouched"

    # And the seeding composition still seeds — with the OTHER password, which
    # is exactly the damage the seam exists to prevent off-host.
    reseeded = PostgresRepository(other_password, model_provider=_provider())
    reseeded.close()
    assert _query(postgres_scope.url, "SELECT max(password_hash) FROM users")[0] != (
        seeded[1]
    )


# ───────────────────────────────────────────── the migration ledger gate ──

def test_the_ledger_preflight_refuses_an_unmigrated_database(postgres_scope):
    with pytest.raises(cli.ScaleBuildCliError, match="no silicon_schema_migrations"):
        cli.verify_migration_ledger(postgres_scope.url)


def test_the_ledger_preflight_accepts_a_matching_checkout(
    postgres_scope, postgres_settings
):
    owner = PostgresRepository(postgres_settings, model_provider=_provider())
    owner.close()
    applied, expected = cli.verify_migration_ledger(postgres_scope.url)
    assert applied == expected == cli.packaged_migration_count()


def test_the_ledger_preflight_refuses_a_checkout_that_is_ahead(
    postgres_scope, postgres_settings
):
    """A builder one migration ahead of the live schema reads columns that do
    not exist yet; the artifact it produces is quietly wrong, not broken."""
    owner = PostgresRepository(postgres_settings, model_provider=_provider())
    owner.close()
    with psycopg.connect(postgres_scope.url, autocommit=True) as connection:
        connection.execute(
            "DELETE FROM silicon_schema_migrations "
            "WHERE version = (SELECT max(version) FROM silicon_schema_migrations)"
        )
    with pytest.raises(cli.ScaleBuildCliError, match="migration ledger mismatch"):
        cli.verify_migration_ledger(postgres_scope.url)


# ───────────────────────────────────────────────────────── the CLI loop ──

@pytest.fixture
def indexed_notebook(postgres_settings, tmp_path, monkeypatch):
    """A small real notebook with sources, KG and vectors, ready to index."""
    settings = postgres_settings.model_copy(
        update={
            "storage_dir": str(tmp_path / "storage"),
            "model_services_config": "",
            "embed_dim": 16,
        }
    )
    monkeypatch.setattr(
        maintenance_cli,
        "create_repository",
        lambda _settings: PostgresRepository(settings, model_provider=_provider()),
    )
    monkeypatch.setattr(bi, "Settings", lambda: settings)

    source_dir = tmp_path / "docs"
    source_dir.mkdir()
    (source_dir / "one.md").write_text(
        "# Offline scale build\n\nOffline scale build runs beside the service.",
        encoding="utf-8",
    )
    common = ["--confirm-service-stopped"]
    assert bi.main([
        "ingest", "--input-dir", str(source_dir), "--notebook-name", "Scale CLI",
        "--workers", "1", *common,
    ]) == 0
    probe = PostgresRepository(settings, model_provider=_provider())
    try:
        notebook_id = next(
            notebook.id
            for notebook in probe.list_notebooks()
            if notebook.name == "Scale CLI"
        )
    finally:
        probe.close()
    assert bi.main(["kg", "--notebook-id", notebook_id, *common]) == 0
    assert bi.main(["embed", "--notebook-id", notebook_id, *common]) == 0

    monkeypatch.setattr(cli, "Settings", lambda: settings)
    return notebook_id, settings


def test_build_export_import_inspect_round_trips(
    indexed_notebook, tmp_path, capsys
):
    notebook_id, settings = indexed_notebook

    assert cli.main(["build", "--notebook", notebook_id]) == 0
    built = json.loads(capsys.readouterr().out)
    assert built["mode"] == "full"

    package = tmp_path / "package"
    assert cli.main([
        "export", "--notebook", notebook_id, "--to", str(package)
    ]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert cli.MAIN_ROOT in exported["roots"]
    # The companion root is built alongside the main index whenever the
    # deployment's switch is on, so the export covers the multi-root path.
    assert cli.COMPANION_ROOT in exported["roots"]
    assert (package / cli.MAIN_ROOT / "manifest.json").is_file()
    # The version this artifact was built with rides along, so the importing
    # machine can refuse an hnswlib it cannot verify.
    assert exported["library_versions"]["hnswlib"]

    assert cli.main([
        "import", "--notebook", notebook_id, "--from", str(package)
    ]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["roots"] == [
        root for root in cli.PUBLISH_ORDER if root in exported["roots"]
    ]
    assert imported["version"] == exported["version"]

    assert cli.main(["inspect", "--notebook", notebook_id]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["exists"] is True
    assert receipt["version_matches_database"] is True
    assert receipt["build_claim"] == "free"
    assert receipt["leftovers"] == {}
    assert receipt["manifest"]["version"] == imported["version"]
    assert receipt["manifest"]["dim"] == 16


def test_an_unknown_notebook_is_a_readable_refusal(indexed_notebook, capsys):
    """``status()`` raises ``KeyError``; letting it escape would print a bare
    traceback with the id as the entire message."""
    assert cli.main(["inspect", "--notebook", "nb-does-not-exist"]) == 2
    assert "unknown notebook" in capsys.readouterr().err


def test_a_claim_held_elsewhere_stops_the_build(indexed_notebook, capsys):
    """The whole point of the claim: two writers must never publish over the
    same artifact directory."""
    from pathlib import Path

    from app.repositories.postgres.database import PostgresDatabase

    notebook_id, settings = indexed_notebook
    other = PostgresDatabase(settings, Path(__file__).resolve().parents[3])
    handle = other.try_scale_build_lock(notebook_id)
    assert handle is not None
    try:
        assert cli.main(["build", "--notebook", notebook_id]) == 1
        assert "another process" in capsys.readouterr().err

        assert cli.main(["inspect", "--notebook", notebook_id]) == 0
        assert json.loads(capsys.readouterr().out)["build_claim"] == (
            "held_elsewhere"
        )
    finally:
        handle.release()
        other.close()


def test_an_import_from_a_foreign_deployment_is_refused(
    indexed_notebook, tmp_path, capsys
):
    """The dim gate, end to end: a package built at another embedding width
    would degrade this notebook to silent zero recall."""
    notebook_id, _settings = indexed_notebook
    assert cli.main(["build", "--notebook", notebook_id]) == 0
    capsys.readouterr()

    package = tmp_path / "foreign"
    assert cli.main([
        "export", "--notebook", notebook_id, "--to", str(package)
    ]) == 0
    capsys.readouterr()

    manifest_path = package / cli.MAIN_ROOT / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    published = list(manifest["version"])
    manifest["dim"] = 4096
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert cli.main([
        "import", "--notebook", notebook_id, "--from", str(package)
    ]) == 2
    assert "dimension mismatch" in capsys.readouterr().err

    assert cli.main(["inspect", "--notebook", notebook_id]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["manifest"]["version"] == published
    assert receipt["leftovers"] == {}


def test_sqlite_is_refused_even_with_a_reachable_database(
    indexed_notebook, monkeypatch, capsys, tmp_path
):
    _notebook_id, settings = indexed_notebook
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: Settings(
            database_url=f"sqlite:///{tmp_path / 'x.db'}",
            storage_dir=settings.storage_dir,
        ),
    )
    assert cli.main(["inspect", "--notebook", "nb-1"]) == 2
    assert "requires PostgreSQL" in capsys.readouterr().err
