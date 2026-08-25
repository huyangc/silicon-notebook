from __future__ import annotations

from types import SimpleNamespace

from app.core.config import Settings
from app.models.notebooks import NotebookCreate
from app.services.scale_index_builder import ScaleIndexBuilder
from app.services.sqlite_repository import SQLiteRepository, _now


def _repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'identity.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    return SQLiteRepository(Settings(_env_file=None))


def test_sqlite_migration_57_to_58_preserves_notebook_and_adds_identities(
    tmp_path, monkeypatch
):
    from app.repositories.sqlite import migrations as migrations_module

    monkeypatch.setattr(migrations_module, "SCHEMA_VERSION", 57)
    repo = _repo(tmp_path, monkeypatch)
    notebook_id = "nb-pre-pipeline"
    with repo._connect() as db:
        db.execute(
            "INSERT INTO notebooks (id,name,created_at,updated_at) VALUES (?,?,?,?)",
            (notebook_id, "pre-pipeline", _now(), _now()),
        )
        db.commit()
        notebook_columns = {
            row[1] for row in db.execute("PRAGMA table_info(notebooks)").fetchall()
        }
        assert "indexing_pipeline" not in notebook_columns
        assert db.execute("PRAGMA user_version").fetchone()[0] == 57

    monkeypatch.setattr(migrations_module, "SCHEMA_VERSION", 58)
    assert repo._migrator.migrate() == [58]

    with repo._connect() as db:
        notebook_row = db.execute(
            "SELECT name,indexing_pipeline,indexing_pipeline_version,"
            "indexing_pipeline_generation,indexing_pipeline_job_id "
            "FROM notebooks WHERE id=?",
            (notebook_id,),
        ).fetchone()
        product_columns = {
            row[1]
            for row in db.execute("PRAGMA table_info(unified_kg_state)").fetchall()
        }
        assert db.execute("PRAGMA user_version").fetchone()[0] == 58
    assert dict(notebook_row) == {
        "name": "pre-pipeline",
        "indexing_pipeline": None,
        "indexing_pipeline_version": "builtin.chunk.v1",
        "indexing_pipeline_generation": "",
        "indexing_pipeline_job_id": "",
    }
    assert {"indexing_pipeline_id", "indexing_pipeline_version"} <= product_columns


def test_sqlite_migration_58_to_59_adds_durable_stage_tables(
    tmp_path, monkeypatch
):
    from app.repositories.sqlite import migrations as migrations_module

    monkeypatch.setattr(migrations_module, "SCHEMA_VERSION", 58)
    repo = _repo(tmp_path, monkeypatch)
    with repo._connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 58
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='indexing_pipeline_stages'"
        ).fetchone() is None

    monkeypatch.setattr(migrations_module, "SCHEMA_VERSION", 59)
    assert repo._migrator.migrate() == [59]

    with repo._connect() as db:
        tables = {
            str(row["name"])
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert db.execute("PRAGMA user_version").fetchone()[0] == 59
    assert {
        "indexing_pipeline_stages",
        "indexing_pipeline_stage_sources",
    } <= tables


def test_startup_recovery_discards_unpublished_stage_and_keeps_live_identity(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path, monkeypatch)
    notebook = repo.create_notebook(NotebookCreate(name="staged crash"))
    job = repo._runtime.kg_build_jobs.create_job(
        notebook.id, "recovery-test", "rebuild", 0
    )
    with repo._write() as db:
        db.execute(
            "INSERT INTO unified_kg_state "
            "(notebook_id,dirty,updated_at,indexing_pipeline_id,"
            "indexing_pipeline_version) VALUES (?,?,?,?,?)",
            (notebook.id, 0, _now(), "old.pipeline", "v1"),
        )
        db.execute(
            "INSERT INTO indexing_pipeline_stages "
            "(job_id,notebook_id,pipeline_id,pipeline_version,"
            "pipeline_generation,source_snapshot,created_at,updated_at) "
            "VALUES (?,?,?,?,?,'[]',?,?)",
            (job["id"], notebook.id, "new.pipeline", "v2", "late", _now(), _now()),
        )

    repo._recover_interrupted_jobs()

    with repo._connect() as db:
        identity = db.execute(
            "SELECT indexing_pipeline_id,indexing_pipeline_version "
            "FROM unified_kg_state WHERE notebook_id=?",
            (notebook.id,),
        ).fetchone()
        stage_count = db.execute(
            "SELECT COUNT(*) AS c FROM indexing_pipeline_stages WHERE job_id=?",
            (job["id"],),
        ).fetchone()["c"]
    recovered = repo._runtime.kg_build_jobs.get(job["id"])
    assert dict(identity) == {
        "indexing_pipeline_id": "old.pipeline",
        "indexing_pipeline_version": "v1",
    }
    assert stage_count == 0
    assert recovered["status"] == "failed"
    assert recovered["error_code"] == "worker_interrupted"


def test_postgres_stage_publisher_deletes_orphanable_relation_vectors_first():
    from app.repositories.postgres.kg_build_job_store import (
        KgBuildJobStore as PostgresKgBuildJobStore,
    )

    class _Rows:
        def __init__(self, rows=()):
            self._rows = list(rows)

        def fetchall(self):
            return self._rows

    class _Connection:
        def __init__(self):
            self.calls = []
            self.object_page = True

        def execute(self, sql, params=()):
            self.calls.append((" ".join(sql.split()), params))
            if sql.lstrip().startswith("SELECT DISTINCT ko.id"):
                if self.object_page:
                    self.object_page = False
                    return _Rows([{"id": "old-object"}])
                return _Rows()
            return _Rows()

    connection = _Connection()
    PostgresKgBuildJobStore._delete_source_kg(connection, "nb", "source")
    statements = [sql for sql, _params in connection.calls]
    direct_vector = next(
        index for index, sql in enumerate(statements)
        if sql.startswith("DELETE FROM relation_embeddings")
        and "WHERE source_id=%s" in sql
    )
    direct_relation = next(
        index for index, sql in enumerate(statements)
        if sql == "DELETE FROM knowledge_relations WHERE source_id=%s"
    )
    endpoint_vector = next(
        index for index, sql in enumerate(statements)
        if sql.startswith("DELETE FROM relation_embeddings")
        and "source_object_id=ANY" in sql
    )
    endpoint_relation = next(
        index for index, sql in enumerate(statements)
        if sql.startswith("DELETE FROM knowledge_relations WHERE source_object_id=ANY")
    )
    assert direct_vector < direct_relation
    assert endpoint_vector < endpoint_relation


def test_version_signal_and_facts_include_published_pipeline_identity(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path, monkeypatch)
    notebook = repo.create_notebook(NotebookCreate(name="identity"))
    store = repo._runtime.index_projections

    initial_signal = store.version_signal(notebook.id)
    assert initial_signal[2][-2:] == ("", "builtin.chunk.v1")
    assert store.version_facts(notebook.id)[-3:] == [
        "indexing_pipeline",
        "",
        "builtin.chunk.v1",
    ]

    with repo._write() as db:
        db.execute(
            "INSERT INTO unified_kg_state "
            "(notebook_id,dirty,updated_at,indexing_pipeline_id,"
            "indexing_pipeline_version) VALUES (?,?,?,?,?) "
            "ON CONFLICT(notebook_id) DO UPDATE SET "
            "indexing_pipeline_id=excluded.indexing_pipeline_id,"
            "indexing_pipeline_version=excluded.indexing_pipeline_version",
            (notebook.id, 1, _now(), "test.pipeline", "v2"),
        )

    changed_signal = store.version_signal(notebook.id)
    assert changed_signal != initial_signal
    assert changed_signal[2][-2:] == ("test.pipeline", "v2")
    assert store.version_facts(notebook.id)[-3:] == [
        "indexing_pipeline",
        "test.pipeline",
        "v2",
    ]


def test_fold_refuses_a_manifest_from_another_pipeline_and_runs_full_build():
    events = []
    builder = object.__new__(ScaleIndexBuilder)
    builder.load_scale = lambda _notebook_id: SimpleNamespace(
        manifest={"pipeline_identity": ["old.pipeline", "v1"]}
    )
    builder.projections = SimpleNamespace(
        pipeline_identity=lambda _notebook_id: ("new.pipeline", "v2")
    )
    builder.event_log = SimpleNamespace(emit=events.append)
    builder.build = lambda notebook_id: {"mode": "full", "notebook_id": notebook_id}

    result = builder.fold("nb-identity")

    assert result == {"mode": "full", "notebook_id": "nb-identity"}
    assert events == [
        {
            "kind": "scale_fold_refused",
            "notebook_id": "nb-identity",
            "reason": "pipeline_mismatch",
        }
    ]
