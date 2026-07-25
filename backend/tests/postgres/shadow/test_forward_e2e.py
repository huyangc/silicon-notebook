from __future__ import annotations

import shutil
import threading
from pathlib import Path

import psycopg
import pytest

from app.core.config import Settings
from app.models.schemas import AskResponse, NotebookCreate, NotebookUpdate
from app.migration.shadow.bulk_copy import copy_snapshot
from app.migration.shadow.capture import install_sqlite_capture
from app.migration.shadow.control import (
    BackupEvidence,
    DiskEvidence,
    ShadowPhase,
    commit_guard_report,
    committed_sqlite_guard_report,
    create_or_reuse_run,
    enable_sqlite_capture_after_guard_reports,
    get_run,
    install_postgres_control_schema,
    preflight,
    prepare_postgres_replication_key_guards,
    update_forward_state,
)
from app.migration.shadow.manifest import MANIFEST
from app.migration.shadow.replicator import Direction, ShadowReplicator
from app.migration.shadow.snapshot import create_snapshot
from app.migration.shadow.verifier import (
    ShadowVerifier,
    VerificationLevel,
    VerificationStatus,
)
from app.repositories.postgres.migrator import PostgresMigrator
from app.services.sqlite_repository import (
    SQLiteRepository,
    reset_request_user,
    set_request_user,
)
from tests.postgres.shadow.test_forward_replicator import clean_control_schema


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_shadow_forward_e2e"),
]
REPO_ROOT = Path(__file__).resolve().parents[4]
FIXTURE = REPO_ROOT / "backend" / "tests" / "fixtures" / "repository_v9"
SECRET = b"forward-shadow-e2e-confirmation-secret"
assert clean_control_schema


def _exercise_business_repository(repo: SQLiteRepository, storage: Path) -> dict:
    user = repo.create_user("z00999999", "shadow-e2e-password")
    token = set_request_user(user)
    try:
        notebook = repo.create_notebook(
            NotebookCreate(name="Forward E2E", purpose="cross-domain writes")
        )
        source_dir = storage / "notebooks" / notebook.id
        source_dir.mkdir(parents=True)
        source_id = repo.eval_insert_source_for_test(
            notebook.id,
            "forward-e2e",
            "Source degeneration stabilizes gain. 负反馈提高稳定性。",
            str(source_dir),
        )
        with repo._connect() as conn:
            element_id = conn.execute(
                "SELECT id FROM source_elements WHERE source_id=? ORDER BY id LIMIT 1",
                (source_id,),
            ).fetchone()[0]
        repo.store_kg(
            notebook.id,
            source_id,
            [
                {
                    "local_id": "e2e-concept",
                    "object_type": "concept",
                    "payload": {"name": "feedback stability", "section_path": "§1"},
                    "evidence": [
                        {
                            "source_id": source_id,
                            "source_title": "forward-e2e",
                            "element_id": element_id,
                            "element_type": "paragraph",
                            "location_label": "§1",
                            "quoted_span": "Source degeneration stabilizes gain.",
                            "confidence": 1.0,
                        }
                    ],
                }
            ],
            [],
        )
        answer_id = repo._runtime.ask_state.save_answer(
            notebook.id,
            None,
            "Why is gain stable?",
            AskResponse(
                conclusion="Feedback stabilizes gain.",
                answer="Feedback stabilizes gain.",
            ),
            user.id,
        )
        memory = repo.create_memory_from_answer(
            notebook.id,
            user.id,
            answer_id,
            "Stable gain",
            "Feedback reduces gain sensitivity.",
            ["analog"],
            extract_kg=False,
        )
        table_id = repo.create_knowhow_table_with_rows(
            notebook.id,
            "Debug matrix",
            "Forward shadow E2E",
            [
                {"name": "Symptom", "role": "anchor"},
                {"name": "Action", "role": "attribute"},
            ],
            [["gain drift", "check feedback network"]],
            created_by=user.id,
        )
        report_id = repo.create_report(notebook.id, "Summarize stability", depth=1)
        share = repo.share_notebook(notebook.id)
        doomed = repo.create_notebook(NotebookCreate(name="Delete during shadow"))
        repo.delete_notebook(doomed.id)
        return {
            "user_id": user.id,
            "notebook_id": notebook.id,
            "source_id": source_id,
            "answer_id": answer_id,
            "memory_id": memory.id,
            "table_id": table_id,
            "report_id": report_id,
            "share_token": share["share_token"],
        }
    finally:
        reset_request_user(token)


def test_frozen_sqlite_fixture_converges_through_outage_and_two_full_verifiers(
    tmp_path, postgres_database, clean_control_schema
):
    run_id = "forward-e2e"
    source_path = tmp_path / "historical.db"
    storage = tmp_path / "storage"
    shutil.copy2(FIXTURE / "baseline.db", source_path)
    shutil.copytree(FIXTURE / "storage", storage)
    settings = Settings(
        database_url=f"sqlite:///{source_path}",
        storage_dir=str(storage),
        event_log_enabled=False,
        llm_log_enabled=False,
        _env_file=None,
    )
    repo = SQLiteRepository(settings)
    source = repo._runtime.database
    with repo._write() as conn:
        conn.execute(
            "UPDATE sources SET file_path=? WHERE id='src-fixture'",
            (str(storage / "notebooks" / "nb-fixture" / "fixture-source.md"),),
        )
    source_conn = source.connect()
    assert source_conn.execute("PRAGMA user_version").fetchone()[0] == 32

    report = preflight(
        run_id=run_id,
        source_url=settings.database_url,
        source_conn=source_conn,
        storage_root=storage,
        target=postgres_database,
        disk_evidence=DiskEvidence("e2e-disk", 10_000_000_000, True),
        backup_evidence=BackupEvidence("e2e-backup", True, True),
        confirmation_secret=SECRET,
    )
    assert PostgresMigrator(postgres_database).migrate() == 10
    install_postgres_control_schema(postgres_database)
    create_or_reuse_run(
        postgres_database,
        report,
        confirmation_token=report.confirmation_token,
        confirmation_secret=SECRET,
    )
    install_sqlite_capture(
        source_conn,
        run_id=run_id,
        schema_epoch=MANIFEST.schema_pair.epoch,
        manifest=MANIFEST,
    )
    sqlite_guards = committed_sqlite_guard_report(
        source_conn,
        source_url=settings.database_url,
        manifest=MANIFEST,
        run_id=run_id,
    )
    postgres_guards = prepare_postgres_replication_key_guards(
        postgres_database, manifest=MANIFEST, run_id=run_id
    )
    state = get_run(postgres_database, run_id)
    state = commit_guard_report(
        postgres_database, report=sqlite_guards, expected_revision=state.revision
    )
    state = commit_guard_report(
        postgres_database, report=postgres_guards, expected_revision=state.revision
    )
    enable_sqlite_capture_after_guard_reports(
        source_conn,
        postgres_database,
        source_url=settings.database_url,
        sqlite_report=sqlite_guards,
        postgres_report=postgres_guards,
        manifest=MANIFEST,
    )
    state = get_run(postgres_database, run_id)
    update_forward_state(
        postgres_database,
        run_id=run_id,
        expected_phase=ShadowPhase.OFF,
        expected_revision=state.revision,
        new_phase=ShadowPhase.SQLITE_TO_POSTGRES,
        progress={"stage": "capture-enabled"},
        source_url=settings.database_url,
        source_conn=source_conn,
    )

    start_writes = threading.Event()
    writes_complete = threading.Event()
    written: dict = {}
    failures: list[BaseException] = []

    def writer() -> None:
        try:
            start_writes.wait(5)
            written.update(_exercise_business_repository(repo, storage))
        except BaseException as exc:
            failures.append(exc)
        finally:
            repo.close_local()
            writes_complete.set()

    writer_thread = threading.Thread(target=writer, name="shadow-e2e-writer")
    writer_thread.start()

    def snapshot_progress(_status: int, _remaining: int, _total: int) -> None:
        start_writes.set()

    snapshot = create_snapshot(
        source,
        tmp_path / "snapshot" / "baseline.sqlite3",
        run_id=run_id,
        pages=1,
        sleep=0.001,
        progress=snapshot_progress,
    )
    start_writes.set()
    assert writes_complete.wait(10)
    writer_thread.join(1)
    assert not failures
    assert written["share_token"]
    copy_snapshot(snapshot, source, postgres_database, MANIFEST, batch_rows=17)

    # Guarantee a suffix event even if every concurrent write landed inside H0.
    user = repo.authenticate_user("z00999999", "shadow-e2e-password")
    assert user is not None
    request_token = set_request_user(user)
    try:
        repo.update_notebook(
            written["notebook_id"],
            NotebookUpdate(purpose="updated after baseline"),
        )
    finally:
        reset_request_user(request_token)

    outage_injected = False

    def outage(boundary: str) -> None:
        nonlocal outage_injected
        if boundary == "before_target" and not outage_injected:
            outage_injected = True
            raise psycopg.OperationalError("injected target outage")

    replicator = ShadowReplicator(
        source,
        postgres_database,
        crash_injector=outage,
        base_backoff_seconds=0,
        max_backoff_seconds=0,
        idle_seconds=0,
    )
    observed_retry = False
    for _ in range(100):
        batch = replicator.run_batch(
            run_id=run_id,
            direction=Direction.SQLITE_TO_POSTGRES,
            max_events=256,
            max_bytes=8 * 1024 * 1024,
        )
        observed_retry = observed_retry or batch.retries > 0
        if batch.events_read == 0:
            break
    else:
        pytest.fail("forward shadow worker did not catch up")
    assert outage_injected and observed_retry

    verifier = ShadowVerifier(
        source,
        postgres_database,
        storage_root=storage,
        chunk_rows=11,
        poll_seconds=0,
    )
    first = verifier.verify(run_id=run_id, level=VerificationLevel.FULL)
    second = verifier.verify(run_id=run_id, level=VerificationLevel.FULL)
    assert first.status is VerificationStatus.COMPLETE
    assert second.status is VerificationStatus.COMPLETE
    assert first.coverage == second.coverage == 1.0
    with postgres_database.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM silicon_shadow.poison_events WHERE run_id=%s",
            (run_id,),
        ).fetchone()["count"] == 0
        target_notebook = conn.execute(
            "SELECT purpose FROM notebooks WHERE id=%s",
            (written["notebook_id"],),
        ).fetchone()
        assert target_notebook["purpose"] == "updated after baseline"
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM memory_items WHERE id=%s",
            (written["memory_id"],),
        ).fetchone()["count"] == 1
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM knowhow_tables WHERE id=%s",
            (written["table_id"],),
        ).fetchone()["count"] == 1
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM reports WHERE id=%s",
            (written["report_id"],),
        ).fetchone()["count"] == 1
    repo.close()
