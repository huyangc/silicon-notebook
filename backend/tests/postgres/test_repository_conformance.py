from __future__ import annotations

import threading
import time

import pytest

from app.core.request_context import reset_request_user, set_request_user
from app.domain.report_export import ReportExportSource
from app.models.ask import AskRequest
from app.models.notebooks import NotebookCreate
from app.models.sources import SourceImportFile, SourceImportRequest
from app.repositories.bundle import PersistenceBundle
from app.repositories.ports import UploadedSourceFile


pytestmark = pytest.mark.postgres_integration


def test_postgres_repository_boots_empty_schema_and_wires_complete_bundle(
    postgres_settings,
):
    from app.repositories.postgres.repository import PostgresRepository
    from app.repositories.factory import create_repository

    repository = create_repository(postgres_settings)
    assert isinstance(repository, PostgresRepository)
    try:
        runtime = repository._runtime
        assert runtime.database.__class__.__module__.startswith(
            "app.repositories.postgres."
        )
        for store in (
            runtime.identity,
            runtime.notebook_store,
            runtime.sharing_store,
            runtime.source_store,
            runtime.chunk_store,
            runtime.embedding_store,
            runtime.knowledge,
            runtime.governance,
            runtime.index_projections,
            runtime.kg_build_jobs,
            runtime.knowhow_store,
            runtime.knowhow_transfer_store,
            runtime.memory_store,
            runtime.queries,
            runtime.report_store,
            runtime.ask_state,
            runtime.unified_kg,
        ):
            assert store.__class__.__module__.startswith("app.repositories.postgres.")
        with repository._runtime.database.connect() as connection:
            assert connection.execute("SELECT count(*) AS n FROM notebooks").fetchone()[
                "n"
            ] == 0
            assert connection.execute(
                "SELECT count(*) AS n FROM users WHERE id='user-local' AND role='admin'"
            ).fetchone()["n"] == 1
            assert connection.execute("SELECT count(*) AS n FROM users").fetchone()[
                "n"
            ] == 1
    finally:
        repository.close()

    with pytest.raises(Exception):
        with repository._runtime.database.connect():
            pass

    # Keep the runtime protocol assertion close to the real boot. It catches a
    # missing store before a route first touches it in production.
    assert repository._runtime.database._closed is True


def test_postgres_bundle_factory_returns_the_backend_neutral_protocol(
    postgres_settings,
):
    from pathlib import Path

    from app.repositories.postgres.bundle import PostgresPersistenceBundleFactory
    from app.services.repository_runtime import RepositoryCompatibilitySeams

    factory = PostgresPersistenceBundleFactory()
    bundle = factory.create(
        settings=postgres_settings,
        root_dir=Path(__file__).resolve().parents[3],
        seams=RepositoryCompatibilitySeams(
            new_id=lambda prefix: f"{prefix}-test",
            now=lambda: "2026-07-23T00:00:00+00:00",
            copy_chunk_size=lambda: 1000,
            remap_json_ids=lambda value, _maps: value,
            in_chunk_size=lambda: 900,
        ),
    )
    try:
        assert isinstance(bundle, PersistenceBundle)
    finally:
        bundle.database.close()


def test_postgres_bundle_closes_pool_when_migration_fails(
    postgres_settings,
    monkeypatch,
):
    from pathlib import Path

    from app.repositories.postgres.bundle import PostgresPersistenceBundleFactory
    from app.repositories.postgres.migrator import PostgresMigrator
    from app.services.repository_runtime import RepositoryCompatibilitySeams

    monkeypatch.setattr(
        PostgresMigrator,
        "migrate",
        lambda _self: (_ for _ in ()).throw(RuntimeError("migration failed")),
    )
    factory = PostgresPersistenceBundleFactory()
    with pytest.raises(RuntimeError, match="migration failed"):
        factory.create(
            settings=postgres_settings,
            root_dir=Path(__file__).resolve().parents[3],
            seams=RepositoryCompatibilitySeams(
                new_id=lambda prefix: f"{prefix}-test",
                now=lambda: "2026-07-23T00:00:00+00:00",
                copy_chunk_size=lambda: 1000,
                remap_json_ids=lambda value, _maps: value,
                in_chunk_size=lambda: 900,
            ),
        )
    assert factory._database is not None
    assert factory._database._closed is True


def test_complete_postgres_repository_smoke_from_empty_schema(
    postgres_settings,
    tmp_path,
):
    """Exercise the production facade, not individual stores, on a fresh PG schema."""
    from app.repositories.postgres.repository import PostgresRepository
    from app.services.knowhow.api import build_projector

    postgres_settings.storage_dir = str(tmp_path / "postgres-storage")
    postgres_settings.event_log_enabled = False
    postgres_settings.llm_log_enabled = False
    postgres_settings.kg_auto_extract = False
    postgres_settings.notebook_copy_max_rows = 0
    repository = PostgresRepository(postgres_settings)
    owner = repository.create_user("a00123456", "pw123456")
    reader = repository.create_user("b00654321", "pw123456")
    assert repository.authenticate_user("A00123456", "pw123456").id == owner.id
    session = repository.create_session(owner.id)
    assert repository.resolve_session(session).id == owner.id

    owner_token = set_request_user(owner)
    try:
        notebook = repository.create_notebook(
            NotebookCreate(name="PostgreSQL boot smoke")
        )
        imported = repository.import_sources(
            notebook.id,
            SourceImportRequest(
                files=[
                    SourceImportFile(
                        file_name="boot-evidence.md",
                        file_size=24,
                        mime_type="text/markdown",
                    )
                ]
            ),
        )
        assert len(imported) == 1
        assert repository.search_notebook(notebook.id, "boot-evidence").hits
        warm_progress = []
        assert repository.warm_open_path_caches(
            lambda done, total: warm_progress.append((done, total))
        ) == 1
        assert warm_progress == [(1, 1)]

        answer = repository.ask_chunk(
            notebook.id,
            AskRequest(question="What evidence is available?", mode="chunk"),
        )
        assert answer.answer_id
        assert answer.llm_mode == "deterministic"

        repository._runtime.memory_service.embedding_scheduler = (
            lambda function, item: function(item)
        )
        memory = repository.create_memory_from_answer(
            notebook.id,
            owner.id,
            answer.answer_id,
            "Boot memory",
            answer.answer or answer.conclusion,
            ["boot"],
            extract_kg=False,
        )
        assert memory.status == "confirmed"
        assert repository.get_memory(memory.id, owner.id).id == memory.id

        table_id = repository.create_knowhow_table(
            notebook.id,
            "Boot knowhow",
            "projection smoke",
            [
                {"name": "Topic", "role": "anchor"},
                {"name": "Procedure", "role": "procedure"},
            ],
            created_by=owner.id,
        )
        table = repository.get_knowhow_table(table_id)
        column_ids = {column["name"]: column["id"] for column in table["columns"]}
        repository.add_knowhow_row(
            table_id,
            {
                column_ids["Topic"]: "Power integrity",
                column_ids["Procedure"]: "Check the rail before signoff.",
            },
        )
        assert build_projector(repository).project_table(table_id, embed=False) == notebook.id
        projected = repository.get_knowhow_table(table_id)
        assert projected["hidden_source_id"]
        assert {row["projection_status"] for row in projected["rows"]} == {"synced"}
        with repository._runtime.database.connect() as connection:
            assert connection.execute(
                "SELECT count(*) AS n FROM knowledge_objects WHERE source_id=%s",
                (projected["hidden_source_id"],),
            ).fetchone()["n"] == 2

        report_id = repository.create_report(notebook.id, "Boot report", depth=1)
        retry_outline = [{"title": "Boot", "scope": "s", "sub_queries": ["q"]}]
        repository.update_report(
            notebook.id,
            report_id,
            status="failed",
            outline=retry_outline,
            understanding={
                "confirmed": True,
                "credibility": {"synthesis_status": "failed_model"},
            },
            sections=[{"title": "old", "markdown": "stale"}],
            section_status=[{"title": "old", "phase": "失败", "step": 0}],
            gaps=["old"],
            references=[{"key": "k1"}],
            content_md="# stale",
            error="pool timeout",
        )
        assert repository.claim_report_generation(notebook.id, report_id)
        assert repository.claim_report_generation(notebook.id, report_id) is False
        claimed_report = repository.get_report(notebook.id, report_id)
        generation_started_at = claimed_report["generation_started_at"]
        assert generation_started_at
        assert claimed_report["outline"] == retry_outline
        assert claimed_report["sections"] == []
        assert claimed_report["section_status"] == []
        assert claimed_report["gaps"] == []
        assert claimed_report["references"] == []
        assert claimed_report["content_md"] == ""
        assert claimed_report["error"] == ""
        assert claimed_report["understanding"]["confirmed"] is True
        assert "credibility" not in claimed_report["understanding"]
        repository.update_report(
            notebook.id,
            report_id,
            status="done",
            progress="complete",
            content_md="# PostgreSQL report",
        )
        completed_report = repository.get_report(notebook.id, report_id)
        assert completed_report["status"] == "done"
        assert completed_report["generation_started_at"] == generation_started_at
        assert completed_report["updated_at"] >= generation_started_at
        assert [item["id"] for item in repository.list_reports(
            notebook.id, created_by=None
        )] == [
            report_id
        ]
        # Per-creator narrowing (P1-T3b) is a SQL predicate in each backend's
        # own dialect, so the PostgreSQL spelling needs its own coverage: a
        # mis-spaced clause or a lost parameter only shows up here.
        assert [item["id"] for item in repository.list_reports(
            notebook.id, created_by=owner.id
        )] == [report_id]
        assert repository.list_reports(notebook.id, created_by=reader.id) == []
        assert repository.export_reports(
            notebook.id, [report_id, report_id], created_by=owner.id
        ) == [
            ReportExportSource(report_id, "Boot report", "# PostgreSQL report"),
            ReportExportSource(report_id, "Boot report", "# PostgreSQL report"),
        ]
        assert repository.export_reports(
            notebook.id, [report_id], created_by=reader.id
        ) == []

        share = repository.share_notebook(notebook.id)
        assert repository.find_notebook_by_share_token(share["share_token"]) == notebook.id
        joined = repository.join_shared(notebook.id, reader.id)
        assert joined.access == "reader"
        assert repository.user_can_read_notebook(notebook.id, reader.id)

        repository.delete_report(notebook.id, report_id)
        repository.delete_notebook(notebook.id)
        with pytest.raises(KeyError):
            repository.get_notebook(notebook.id)
        with repository._runtime.database.connect() as connection:
            for table_name in (
                "sources",
                "memory_items",
                "knowhow_tables",
                "reports",
                "notebook_members",
            ):
                assert connection.execute(
                    f"SELECT count(*) AS n FROM {table_name} WHERE notebook_id=%s",
                    (notebook.id,),
                ).fetchone()["n"] == 0
    finally:
        reset_request_user(owner_token)
        repository.close()


def _project_dynamic_knowhow(repository, notebook_id: str, owner_id: str) -> str:
    from app.services.knowhow.api import build_projector

    table_id = repository.create_knowhow_table(
        notebook_id,
        "Dynamic retrieval",
        "postgres dialect probe",
        [
            {"name": "Topic", "role": "anchor"},
            {"name": "Voltage check", "role": "procedure"},
        ],
        created_by=owner_id,
    )
    table = repository.get_knowhow_table(table_id)
    columns = {column["name"]: column["id"] for column in table["columns"]}
    repository.add_knowhow_row(
        table_id,
        {
            columns["Topic"]: "Power integrity",
            columns["Voltage check"]: "Measure the rail before signoff",
        },
    )
    build_projector(repository).project_table(table_id, embed=False)
    return table_id


def test_postgres_knowhow_node_retrieval_uses_source_scoped_type_query(
    postgres_settings,
):
    """The flag-on production retrieval path must never execute SQLite `?` SQL."""
    from app.repositories.postgres.repository import PostgresRepository

    postgres_settings.knowhow_kg_node_retrieval_enabled = True
    repository = PostgresRepository(postgres_settings)
    owner = repository.create_user("c00123456", "pw123456")
    token = set_request_user(owner)
    try:
        notebook = repository.create_notebook(NotebookCreate(name="PG dynamic KG"))
        _project_dynamic_knowhow(repository, notebook.id, owner.id)

        candidates = repository.retrieval.candidates
        assert set(candidates._knowhow_object_types(notebook.id)) == {
            "Topic",
            "Voltage check",
        }
        hits = repository._retrieve_scored(notebook.id, "rail before signoff")
        assert any(hit.object_type == "Voltage check" for hit in hits)
    finally:
        reset_request_user(token)
        repository.close()


def test_postgres_restart_recovers_every_interrupted_job_and_preserves_terminal_rows(
    postgres_settings,
):
    from app.repositories.postgres._store_utils import jsonb
    from app.repositories.postgres.repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    owner = repository.create_user("d00123456", "pw123456")
    token = set_request_user(owner)
    try:
        notebook = repository.create_notebook(NotebookCreate(name="restart recovery"))
        sources = repository.import_sources(
            notebook.id,
            SourceImportRequest(
                files=[
                    SourceImportFile(
                        file_name="running.md", file_size=1, mime_type="text/markdown"
                    ),
                    SourceImportFile(
                        file_name="terminal.md", file_size=1, mime_type="text/markdown"
                    ),
                ]
            ),
        )
        with repository._runtime.database.write() as db:
            db.execute(
                "INSERT INTO notebooks "
                "(id,name,status,created_by,created_at,updated_at) "
                "VALUES (%s,'terminal','draft',%s,'2020-01-01T00:00:00Z','2020-01-01T00:00:00Z')",
                (f"{notebook.id}-terminal", owner.id),
            )
            db.execute(
                "INSERT INTO merge_review_jobs "
                "(notebook_id,status,total,done,started_at,updated_at,error) VALUES "
                "(%s,'running',2,1,'2020-01-01T00:00:00Z','2020-01-01T00:00:00Z',''),"
                "(%s,'done',1,1,'2020-01-01T00:00:00Z','2020-01-01T00:00:00Z','kept')",
                (notebook.id, f"{notebook.id}-terminal"),
            )
            db.execute(
                "INSERT INTO ask_jobs "
                "(id,notebook_id,status,trace_json,error,created_at,updated_at) VALUES "
                "('ask-running',%s,'running',%s,'','2020-01-01T00:00:00Z','2020-01-01T00:00:00Z'),"
                "('ask-done',%s,'done',%s,'kept','2020-01-01T00:00:00Z','2020-01-01T00:00:00Z')",
                (notebook.id, jsonb([{"step": "kept"}]), notebook.id, jsonb([])),
            )
            db.execute(
                "INSERT INTO knowhow_tables "
                "(id,notebook_id,title,created_by,created_at,updated_at) "
                "VALUES ('recover-table',%s,'T',%s,'2020-01-01T00:00:00Z','2020-01-01T00:00:00Z')",
                (notebook.id, owner.id),
            )
            for row_id, status in (
                ("row-syncing", "syncing"),
                ("row-pending", "pending"),
                ("row-synced", "synced"),
                ("row-failed", "failed"),
            ):
                db.execute(
                    "INSERT INTO knowhow_rows "
                    "(id,table_id,position,projection_status,created_at,updated_at) "
                    "VALUES (%s,'recover-table',0,%s,'2020-01-01T00:00:00Z','2020-01-01T00:00:00Z')",
                    (row_id, status),
                )
            db.execute(
                "UPDATE sources SET status='parsing',parse_status='extracting',"
                "error_message='old',updated_at='2020-01-01T00:00:00Z' WHERE id=%s",
                (sources[0].id,),
            )
            db.execute(
                "UPDATE sources SET status='extracted',parse_status='parsed',"
                "error_message='kept',updated_at='2020-01-01T00:00:00Z' WHERE id=%s",
                (sources[1].id,),
            )
            db.execute(
                "INSERT INTO extraction_runs "
                "(id,notebook_id,source_id,run_type,status,error_message,created_at,updated_at) VALUES "
                "('extract-running',%s,%s,'kg','running','','2020-01-01T00:00:00Z','2020-01-01T00:00:00Z'),"
                "('extract-done',%s,%s,'kg','done','kept','2020-01-01T00:00:00Z','2020-01-01T00:00:00Z')",
                (notebook.id, sources[0].id, notebook.id, sources[1].id),
            )
            db.execute(
                "INSERT INTO kg_build_jobs "
                "(id,notebook_id,mode,status,stage,error_code,error_message,created_at,updated_at,finished_at) VALUES "
                "('kg-running',%s,'incremental','running','extracting','','','2020-01-01T00:00:00Z','2020-01-01T00:00:00Z',NULL),"
                "('kg-done',%s,'incremental','done','finished','kept','kept','2020-01-01T00:00:00Z','2020-01-01T00:00:00Z','2020-01-01T00:00:00Z')",
                (notebook.id, notebook.id),
            )
            db.execute(
                "INSERT INTO indexing_pipeline_stages "
                "(job_id,notebook_id,pipeline_id,pipeline_version,"
                "pipeline_generation,source_snapshot,created_at,updated_at) "
                "VALUES ('kg-running',%s,'test.pipeline','v2','late',%s,"
                "'2020-01-01T00:00:00Z','2020-01-01T00:00:00Z')",
                (notebook.id, jsonb([])),
            )
            db.execute(
                "INSERT INTO catalog_jobs "
                "(id,notebook_id,source_id,created_by,status,failure_reason,diagnostic,"
                "created_at,updated_at,finished_at) VALUES "
                "('catalog-running',%s,%s,%s,'running','','','2020-01-01T00:00:00Z',"
                "'2020-01-01T00:00:00Z',NULL),"
                "('catalog-done',%s,%s,%s,'succeeded','','','2020-01-01T00:00:00Z',"
                "'2020-01-01T00:00:00Z','2020-01-01T00:00:00Z')",
                (
                    notebook.id, sources[0].id, owner.id,
                    notebook.id, sources[1].id, owner.id,
                ),
            )
        repository.close()

        restarted = PostgresRepository(postgres_settings)
        try:
            # Construction/migration is safe for offline tooling. Only the
            # server lifecycle claims abandoned process-owned work.
            restarted._recover_interrupted_jobs()
            with restarted._runtime.database.connect() as db:
                merge_rows = {
                    row["notebook_id"]: row
                    for row in db.execute("SELECT * FROM merge_review_jobs").fetchall()
                }
                ask_rows = {
                    row["id"]: row
                    for row in db.execute("SELECT * FROM ask_jobs").fetchall()
                }
                row_states = {
                    row["id"]: row["projection_status"]
                    for row in db.execute(
                        "SELECT id,projection_status FROM knowhow_rows"
                    ).fetchall()
                }
                source_rows = {
                    row["id"]: row
                    for row in db.execute(
                        "SELECT id,status,parse_status,error_message,updated_at FROM sources"
                    ).fetchall()
                }
                extraction_rows = {
                    row["id"]: row
                    for row in db.execute("SELECT * FROM extraction_runs").fetchall()
                }
                kg_rows = {
                    row["id"]: row
                    for row in db.execute("SELECT * FROM kg_build_jobs").fetchall()
                }
                catalog_rows = {
                    row["id"]: row
                    for row in db.execute("SELECT * FROM catalog_jobs").fetchall()
                }
                stage_count = int(
                    db.execute(
                        "SELECT COUNT(*) AS c FROM indexing_pipeline_stages"
                    ).fetchone()["c"]
                )
            assert merge_rows[notebook.id]["status"] == "failed"
            assert merge_rows[notebook.id]["error"] == "中断:服务重启"
            assert merge_rows[f"{notebook.id}-terminal"]["status"] == "done"
            assert merge_rows[f"{notebook.id}-terminal"]["error"] == "kept"
            assert ask_rows["ask-running"]["status"] == "interrupted"
            assert ask_rows["ask-running"]["error"] == "中断:服务重启"
            assert ask_rows["ask-running"]["trace_json"] == [{"step": "kept"}]
            assert ask_rows["ask-done"]["status"] == "done"
            assert ask_rows["ask-done"]["error"] == "kept"
            assert row_states == {
                "row-syncing": "failed",
                "row-pending": "failed",
                "row-synced": "synced",
                "row-failed": "failed",
            }
            assert source_rows[sources[0].id]["status"] == "parsed"
            assert source_rows[sources[0].id]["parse_status"] == "parsed"
            assert source_rows[sources[0].id]["error_message"] == ""
            assert source_rows[sources[0].id]["updated_at"].year > 2020
            assert source_rows[sources[1].id]["status"] == "extracted"
            assert source_rows[sources[1].id]["error_message"] == "kept"
            assert source_rows[sources[1].id]["updated_at"].year == 2020
            assert extraction_rows["extract-running"]["status"] == "failed"
            assert extraction_rows["extract-running"]["error_message"].startswith(
                "worker_interrupted:"
            )
            assert extraction_rows["extract-running"]["updated_at"].year > 2020
            assert extraction_rows["extract-done"]["status"] == "done"
            assert extraction_rows["extract-done"]["error_message"] == "kept"
            assert extraction_rows["extract-done"]["updated_at"].year == 2020
            assert kg_rows["kg-running"]["status"] == "failed"
            assert kg_rows["kg-running"]["stage"] == "finished"
            assert kg_rows["kg-running"]["error_code"] == "worker_interrupted"
            assert kg_rows["kg-running"]["updated_at"].year > 2020
            assert kg_rows["kg-running"]["finished_at"].year > 2020
            assert kg_rows["kg-done"]["status"] == "done"
            assert kg_rows["kg-done"]["error_code"] == "kept"
            assert kg_rows["kg-done"]["updated_at"].year == 2020
            assert stage_count == 0
            assert catalog_rows["catalog-running"]["status"] == "failed"
            assert catalog_rows["catalog-running"]["diagnostic"] == "worker_interrupted"
            # 措辞钉死: 与 app/services/catalog_job.py 的 INTERRUPTED_MESSAGE 同口径
            # 用「识别」而非内部叫法「抽取」——这条 SQL 字面量绕过 user_error()/前端
            # 词汇门,原样上屏。
            failure_reason = catalog_rows["catalog-running"]["failure_reason"]
            assert "抽取" not in failure_reason
            assert "识别" in failure_reason
            assert catalog_rows["catalog-running"]["updated_at"].year > 2020
            assert catalog_rows["catalog-running"]["finished_at"].year > 2020
            assert catalog_rows["catalog-done"]["status"] == "succeeded"
            assert catalog_rows["catalog-done"]["failure_reason"] == ""
            assert catalog_rows["catalog-done"]["updated_at"].year == 2020
        finally:
            restarted.close()
    finally:
        reset_request_user(token)
        repository.close()


def test_startup_warm_failure_immediately_closes_real_postgres_pool(
    postgres_settings, monkeypatch
):
    from app.api import deps
    from app.core import config, readiness
    from app.repositories.postgres.repository import PostgresRepository
    from app.services import startup_warmup

    repository = PostgresRepository(postgres_settings)
    cleared = []

    def cached_repository():
        return repository

    cached_repository.cache_clear = lambda: cleared.append(True)
    monkeypatch.setattr(deps, "repository", cached_repository)
    monkeypatch.setattr(config, "get_settings", lambda: postgres_settings)
    monkeypatch.setattr(
        repository,
        "warm_open_path_caches",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("warm failed")),
    )
    readiness.reset()
    lease = None
    try:
        lease = startup_warmup.begin_lifecycle()
        assert lease is not None
        startup_warmup.run_startup(lease)
        assert readiness.snapshot()["phase"] == "error"
        assert startup_warmup.begin_lifecycle() is None
        assert repository._runtime.database._closed is True
        assert cleared == [True]
        with pytest.raises(Exception):
            with repository._runtime.database.connect():
                pass
    finally:
        startup_warmup.close_repository(lease, None)
        repository.close()
    assert readiness.snapshot()["phase"] == "stopped"


def test_concurrent_startup_constructs_and_closes_one_real_postgres_pool(
    postgres_settings, monkeypatch
):
    from app.api import deps
    from app.core import readiness
    from app.repositories.postgres.repository import PostgresRepository
    from app.services import startup_warmup

    factory_constructed = threading.Event()
    release_factory = threading.Event()
    created = []
    cleared = []
    results = []

    def repository_factory():
        repository = PostgresRepository(postgres_settings)
        created.append(repository)
        factory_constructed.set()
        assert release_factory.wait(timeout=5)
        return repository

    repository_factory.cache_clear = lambda: cleared.append(True)
    monkeypatch.setattr(deps, "repository", repository_factory)
    monkeypatch.setattr(
        startup_warmup,
        "_reproject_legacy_knowhow_tables",
        lambda _repo: None,
    )

    lease = startup_warmup.begin_lifecycle()
    assert lease is not None
    winner = threading.Thread(
        target=lambda: results.append(startup_warmup.run_startup(lease))
    )
    winner.start()
    assert factory_constructed.wait(timeout=5)

    loser_lease = startup_warmup.begin_lifecycle()
    assert loser_lease is None
    assert startup_warmup.run_startup(loser_lease) is None
    startup_warmup.close_repository(loser_lease, None)
    assert len(created) == 1
    assert created[0]._runtime.database._closed is False
    assert readiness.snapshot()["phase"] == "migrating"

    release_factory.set()
    winner.join(timeout=10)
    assert not winner.is_alive()
    assert results == [created[0]]
    assert readiness.is_ready() is True
    assert created[0]._runtime.database._closed is False

    startup_warmup.close_repository(lease, created[0])
    assert created[0]._runtime.database._closed is True
    assert cleared == [True]
    assert readiness.snapshot()["phase"] == "stopped"


def test_postgres_last_knowhow_table_delete_sweeps_asset_row_and_file(
    postgres_settings, tmp_path, monkeypatch
):
    from app.api import knowhow_routes
    from app.repositories.postgres.repository import PostgresRepository
    from app.services import background_jobs
    from app.services.knowhow import api as knowhow_api
    from app.services.knowhow.assets import AssetService

    postgres_settings.storage_dir = str(tmp_path / "pg-assets")
    repository = PostgresRepository(postgres_settings)
    owner = repository.create_user("e00123456", "pw123456")
    token = set_request_user(owner)
    try:
        notebook = repository.create_notebook(NotebookCreate(name="asset GC"))
        table_id = _project_dynamic_knowhow(repository, notebook.id, owner.id)
        asset = AssetService(repository).save(
            notebook.id, "orphan.png", "image/png", b"png", owner.id
        )
        table = repository.get_knowhow_table(table_id)
        row = table["rows"][0]
        column = next(c for c in table["columns"] if c["name"] == "Voltage check")
        repository.update_knowhow_cell(
            row["id"], column["id"], f"![orphan](asset://{asset['id']})", [asset["id"]]
        )
        asset_path = AssetService(repository).path_for(asset)
        assert asset_path.is_file()

        monkeypatch.setattr(knowhow_routes, "repository", lambda: repository)
        monkeypatch.setattr(
            background_jobs,
            "submit",
            lambda function, **_kwargs: function(),
        )
        knowhow_routes.delete_knowhow_table(notebook.id, table_id)

        assert repository.get_notebook_asset(asset["id"]) is None
        assert asset_path.exists() is False
        assert knowhow_api.maybe_sweep_orphan_assets(
            repository, notebook.id, min_interval=0, background=False
        )
    finally:
        reset_request_user(token)
        repository.close()


def test_delete_source_does_not_wait_on_notebook_capacity_lock(
    postgres_settings, tmp_path
):
    """W4-2 (WR-7): ``delete_source``'s teardown transaction no longer calls
    ``source_exists_for_update_tx`` with a ``notebook_id`` (see
    ``source_ingestion.delete_source``), so it must never take the
    ``notebooks`` row's ``FOR NO KEY UPDATE`` capacity lock. Pin this
    BEHAVIORALLY, not just by call shape (the SQLite-backed
    ``test_delete_source_does_not_pass_notebook_id_to_the_lock_probe`` pins
    the call shape but SQLite discards the argument either way and cannot
    observe a real lock wait):

    hold that lock open on a separate connection while deleting a source in
    the SAME notebook on the repository's own pool — the delete must
    complete promptly instead of queueing behind the held lock.

    Mutation check (performed manually while implementing this task): passing
    ``source.notebook_id`` back into the ``source_exists_for_update_tx`` call
    at the ``delete_source`` call site makes this test fail — on this
    fixture's ``postgres_lock_timeout_seconds=1``, ``delete_source`` raises
    ``psycopg.errors.LockNotAvailable`` while waiting on the held lock rather
    than merely running slow; a deployment with a longer/no lock timeout
    would instead observe the ``elapsed < release_delay / 2`` assertion
    below fail once the held lock is released.
    """
    from app.repositories.postgres.repository import PostgresRepository

    postgres_settings.storage_dir = str(tmp_path / "pg-delete-lock")
    postgres_settings.event_log_enabled = False
    postgres_settings.llm_log_enabled = False
    postgres_settings.kg_auto_extract = False
    repository = PostgresRepository(postgres_settings)
    owner = repository.create_user("f00123456", "pw123456")
    token = set_request_user(owner)
    try:
        notebook = repository.create_notebook(NotebookCreate(name="Lock race"))
        uploaded = repository.upload_sources(
            notebook.id,
            [
                UploadedSourceFile(
                    file_name="a.md",
                    content_type="text/markdown",
                    content=b"# T\n\nbody",
                )
            ],
            scheduler=lambda source_id: None,
        )
        source_id = uploaded[0].id

        lock_acquired = threading.Event()
        release_lock = threading.Event()
        release_delay = 3.0
        holder_errors: list[BaseException] = []

        def hold_notebook_capacity_lock() -> None:
            try:
                with repository._runtime.database.write() as connection:
                    connection.execute(
                        "SELECT 1 FROM notebooks WHERE id=%s FOR NO KEY UPDATE",
                        (notebook.id,),
                    )
                    lock_acquired.set()
                    # Either the test releases us early (fix is behaving) or
                    # we time out and release on our own so the test cannot
                    # hang forever if something goes wrong.
                    release_lock.wait(timeout=release_delay)
            except BaseException as exc:  # pragma: no cover - surfaced below
                holder_errors.append(exc)
                lock_acquired.set()

        holder = threading.Thread(target=hold_notebook_capacity_lock)
        holder.start()
        try:
            assert lock_acquired.wait(timeout=10), "lock holder never started"
            assert not holder_errors, holder_errors

            started = time.monotonic()
            repository.delete_source(source_id)
            elapsed = time.monotonic() - started
        finally:
            release_lock.set()
            holder.join(timeout=10)
        assert not holder_errors, holder_errors

        # The removed notebook-row lock means delete_source never waits on
        # connection A's still-open transaction. Under this fixture's 1s
        # lock_timeout a retaken lock actually surfaces FIRST as
        # LockNotAvailable out of delete_source above; this wall-clock
        # ceiling is the backstop for deployments without a lock timeout.
        # Measured headroom is ~250x (0.006s), so a trip here can also just
        # mean a stalled pool or CI host — read it as "delete_source waited
        # on something", not proof of the notebooks-row lock specifically.
        assert elapsed < release_delay / 2, (
            f"delete_source took {elapsed:.2f}s — it waited on something "
            "(the held notebooks-row capacity lock, a stalled pool, or a "
            "stalled CI host); the removed lock is the prime suspect"
        )

        with repository._runtime.database.connect() as connection:
            assert connection.execute(
                "SELECT 1 FROM sources WHERE id=%s", (source_id,)
            ).fetchone() is None
    finally:
        reset_request_user(token)
        repository.close()
