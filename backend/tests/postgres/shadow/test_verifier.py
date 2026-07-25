from __future__ import annotations

import struct
import uuid
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from app.core.config import Settings
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
    VerificationHooks,
    VerificationLevel,
    VerificationNotReady,
    VerificationStatus,
)
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SqliteMigrator


pytestmark = pytest.mark.postgres_integration
REPO_ROOT = Path(__file__).resolve().parents[4]
SECRET = b"shadow-verifier-test-secret-32bytes"


@pytest.fixture
def clean_control_schema(postgres_scope):
    with psycopg.connect(postgres_scope.base_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS silicon_shadow CASCADE")
    try:
        yield
    finally:
        with psycopg.connect(postgres_scope.base_url, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS silicon_shadow CASCADE")


def _seed_source(source: SqliteDatabase, storage: Path) -> None:
    source_file = storage / "notebooks" / "verify-notebook" / "reference.md"
    source_file.parent.mkdir(parents=True)
    source_file.write_text(
        "模拟电路 analog circuit design and device noise", encoding="utf-8"
    )
    now = "2026-07-25 00:00:00"
    with source.write() as conn:
        conn.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                "verify-notebook",
                "Verification notebook",
                "cross-adapter checks",
                "analog design",
                "active",
                now,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,file_size,file_hash,summary,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "verify-source",
                "verify-notebook",
                "Analog reference",
                "markdown",
                "ready",
                "parsed",
                "reference.md",
                str(source_file),
                source_file.stat().st_size,
                "f" * 64,
                "模拟电路 analog circuit design",
                now,
                now,
            ),
        )
        chunks = (
            (
                "verify-chunk-1",
                "analog circuit design 模拟 电路 设计 device noise 器件 噪声",
            ),
            (
                "verify-chunk-2",
                "frequency response 频率 响应 stability compensation 稳定性 补偿",
            ),
            (
                "verify-chunk-3",
                "measurement test 测量 测试 procedure workflow 步骤 流程",
            ),
        )
        for chunk_id, text in chunks:
            conn.execute(
                "INSERT INTO chunks(id,notebook_id,source_id,text,section_path,"
                "element_ids,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    chunk_id,
                    "verify-notebook",
                    "verify-source",
                    text,
                    "verification",
                    "[]",
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO chunk_embeddings(chunk_id,notebook_id,vector,created_at) "
                "VALUES(?,?,?,?)",
                (
                    chunk_id,
                    "verify-notebook",
                    struct.pack("<4f", 1.0, 2.0, 3.0, float(len(chunk_id))),
                    now,
                ),
            )
        conn.executemany(
            "INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?)",
            (
                ("verifier-concurrent", "before", now),
                ("verifier-stable", "stable", now),
                ("verifier-sensitive", "private-source-text", now),
            ),
        )


@pytest.fixture
def shadow_run(tmp_path: Path, postgres_database, clean_control_schema):
    run_id = f"verify-{uuid.uuid4().hex}"
    source_path = tmp_path / "verify-source.db"
    storage = tmp_path / "storage"
    storage.mkdir()
    settings = Settings(
        database_url=f"sqlite:///{source_path}",
        storage_dir=str(storage),
        _env_file=None,
    )
    source = SqliteDatabase(settings, REPO_ROOT)
    SqliteMigrator(source, settings).migrate()
    _seed_source(source, storage)
    source_conn = source.connect()
    report = preflight(
        run_id=run_id,
        source_url=settings.database_url,
        source_conn=source_conn,
        storage_root=storage,
        target=postgres_database,
        disk_evidence=DiskEvidence("verify-disk", 10_000_000_000, True),
        backup_evidence=BackupEvidence("verify-backup", True, True),
        confirmation_secret=SECRET,
        now=1_000,
    )
    assert PostgresMigrator(postgres_database).migrate() == 9
    install_postgres_control_schema(postgres_database)
    create_or_reuse_run(
        postgres_database,
        report,
        confirmation_token=report.confirmation_token,
        confirmation_secret=SECRET,
        now=1_000,
    )
    install_sqlite_capture(
        source_conn,
        run_id=run_id,
        schema_epoch=MANIFEST.schema_pair.epoch,
        manifest=MANIFEST,
    )
    sqlite_report = committed_sqlite_guard_report(
        source_conn,
        source_url=settings.database_url,
        run_id=run_id,
        manifest=MANIFEST,
    )
    postgres_report = prepare_postgres_replication_key_guards(
        postgres_database,
        manifest=MANIFEST,
        run_id=run_id,
    )
    state = get_run(postgres_database, run_id)
    state = commit_guard_report(
        postgres_database,
        report=sqlite_report,
        expected_revision=state.revision,
    )
    state = commit_guard_report(
        postgres_database,
        report=postgres_report,
        expected_revision=state.revision,
    )
    enable_sqlite_capture_after_guard_reports(
        source_conn,
        postgres_database,
        source_url=settings.database_url,
        sqlite_report=sqlite_report,
        postgres_report=postgres_report,
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
    snapshot = create_snapshot(
        source,
        tmp_path / "snapshot" / f"{run_id}.sqlite3",
        run_id=run_id,
    )
    copy_snapshot(snapshot, source, postgres_database, MANIFEST, batch_rows=17)
    try:
        yield SimpleNamespace(
            source=source,
            target=postgres_database,
            run_id=run_id,
            storage=storage,
            source_url=settings.database_url,
        )
    finally:
        source.close_local()


def _verifier(run, **kwargs) -> ShadowVerifier:
    return ShadowVerifier(
        run.source,
        run.target,
        storage_root=run.storage,
        chunk_rows=11,
        poll_seconds=0,
        **kwargs,
    )


def _replicate(run) -> None:
    result = ShadowReplicator(
        run.source,
        run.target,
        base_backoff_seconds=0,
        max_backoff_seconds=0,
    ).run_batch(
        run_id=run.run_id,
        direction=Direction.SQLITE_TO_POSTGRES,
        max_events=100,
        max_bytes=1_000_000,
    )
    assert result.checkpoint_after == result.source_max_seq


def test_structural_verification_is_complete_persisted_and_retention_fenced(
    shadow_run,
):
    observed: dict[str, int] = {}

    def assert_barrier(source_barrier: int) -> None:
        with shadow_run.target.connect() as conn:
            row = conn.execute(
                "SELECT source_barrier FROM silicon_shadow.verifier_barriers "
                "WHERE run_id=%s",
                (shadow_run.run_id,),
            ).fetchone()
        assert row is not None
        observed["barrier"] = int(row["source_barrier"])
        assert observed["barrier"] == source_barrier

    report = _verifier(
        shadow_run,
        hooks=VerificationHooks(after_source_snapshot=assert_barrier),
    ).verify(run_id=shadow_run.run_id, level=VerificationLevel.STRUCTURAL)

    assert report.status is VerificationStatus.COMPLETE
    assert report.coverage == 1.0
    assert report.differences == ()
    with shadow_run.target.connect() as conn:
        stored = conn.execute(
            "SELECT status,source_barrier,target_checkpoint FROM "
            "silicon_shadow.verification_runs WHERE verification_id=%s",
            (report.verification_id,),
        ).fetchone()
        barrier = conn.execute(
            "SELECT 1 FROM silicon_shadow.verifier_barriers WHERE verification_id=%s",
            (report.verification_id,),
        ).fetchone()
    assert stored["status"] == "complete"
    assert int(stored["source_barrier"]) == int(stored["target_checkpoint"])
    assert barrier is None


def test_stable_drift_is_redacted_and_later_clean_run_supersedes_it(shadow_run):
    with shadow_run.target.write() as conn:
        conn.execute(
            "UPDATE app_settings SET value=%s WHERE key=%s",
            ("private-target-text", "verifier-sensitive"),
        )
    drift = _verifier(shadow_run).verify(
        run_id=shadow_run.run_id,
        level=VerificationLevel.STRUCTURAL,
    )
    assert drift.status is VerificationStatus.DRIFT
    assert any(item.category == "row_hash" for item in drift.differences)
    serialized = repr(drift)
    assert "private-source-text" not in serialized
    assert "private-target-text" not in serialized
    with shadow_run.target.connect() as conn:
        stored_text = " ".join(
            str(value)
            for row in conn.execute(
                "SELECT table_name,key_hash,category,safe_summary FROM "
                "silicon_shadow.verification_differences WHERE verification_id=%s",
                (drift.verification_id,),
            ).fetchall()
            for value in row.values()
        )
    assert "private-source-text" not in stored_text
    assert "private-target-text" not in stored_text

    with shadow_run.target.write() as conn:
        conn.execute(
            "UPDATE app_settings SET value=%s WHERE key=%s",
            ("private-source-text", "verifier-sensitive"),
        )
    clean = _verifier(shadow_run).verify(
        run_id=shadow_run.run_id,
        level=VerificationLevel.STRUCTURAL,
    )
    assert clean.status is VerificationStatus.COMPLETE
    with shadow_run.target.connect() as conn:
        status = conn.execute(
            "SELECT status FROM silicon_shadow.verification_runs "
            "WHERE verification_id=%s",
            (drift.verification_id,),
        ).fetchone()["status"]
    assert status == "superseded"


def test_concurrent_key_after_source_snapshot_is_excluded(shadow_run):
    def advance(_barrier: int) -> None:
        with shadow_run.source.write() as conn:
            conn.execute(
                "UPDATE app_settings SET value='after' WHERE key='verifier-concurrent'"
            )
        _replicate(shadow_run)

    report = _verifier(
        shadow_run,
        hooks=VerificationHooks(after_source_snapshot=advance),
    ).verify(run_id=shadow_run.run_id, level=VerificationLevel.STRUCTURAL)
    assert report.status is VerificationStatus.COMPLETE
    assert report.concurrent_keys == 1


def test_concurrent_key_does_not_hide_unrelated_stable_drift(shadow_run):
    with shadow_run.target.write() as conn:
        conn.execute(
            "UPDATE app_settings SET value='drift' WHERE key='verifier-stable'"
        )

    def advance(_barrier: int) -> None:
        with shadow_run.source.write() as conn:
            conn.execute(
                "UPDATE app_settings SET value='after' WHERE key='verifier-concurrent'"
            )
        _replicate(shadow_run)

    report = _verifier(
        shadow_run,
        hooks=VerificationHooks(after_source_snapshot=advance),
    ).verify(run_id=shadow_run.run_id, level=VerificationLevel.STRUCTURAL)
    assert report.status is VerificationStatus.DRIFT
    assert report.concurrent_keys == 1
    assert any(item.category == "row_hash" for item in report.differences)


def test_change_after_target_snapshot_cannot_create_false_drift(shadow_run):
    def write_after_pin(_checkpoint: int) -> None:
        with shadow_run.source.write() as conn:
            conn.execute(
                "UPDATE app_settings SET value='future' WHERE key='verifier-concurrent'"
            )

    report = _verifier(
        shadow_run,
        hooks=VerificationHooks(after_target_snapshot=write_after_pin),
    ).verify(run_id=shadow_run.run_id, level=VerificationLevel.STRUCTURAL)
    assert report.status is VerificationStatus.COMPLETE
    assert report.concurrent_keys == 1


def test_checkpoint_timeout_is_persisted_incomplete_and_releases_barrier(shadow_run):
    with shadow_run.source.write() as conn:
        conn.execute(
            "UPDATE app_settings SET value='ahead' WHERE key='verifier-concurrent'"
        )
    with pytest.raises(VerificationNotReady):
        _verifier(shadow_run, wait_timeout_seconds=0).verify(
            run_id=shadow_run.run_id,
            level=VerificationLevel.STRUCTURAL,
        )
    with shadow_run.target.connect() as conn:
        latest = conn.execute(
            "SELECT verification_id,status FROM silicon_shadow.verification_runs "
            "WHERE run_id=%s ORDER BY started_at DESC LIMIT 1",
            (shadow_run.run_id,),
        ).fetchone()
        barrier = conn.execute(
            "SELECT 1 FROM silicon_shadow.verifier_barriers WHERE verification_id=%s",
            (latest["verification_id"],),
        ).fetchone()
    assert latest["status"] == "incomplete"
    assert barrier is None


def test_full_verification_checks_embeddings_and_bilingual_retrieval(shadow_run):
    report = _verifier(shadow_run).verify(
        run_id=shadow_run.run_id,
        level=VerificationLevel.FULL,
    )
    assert report.status is VerificationStatus.COMPLETE
    assert report.embedding_samples == 3
    assert len(report.retrieval) == 6
    assert all(item.recall_at_12 == 1.0 for item in report.retrieval)
    assert all(item.top10_overlap == 1.0 for item in report.retrieval)
    assert all(item.citation_ids_match for item in report.retrieval)
    assert all(item.source_ids_match for item in report.retrieval)


def test_full_verification_reports_embedding_dimension_drift(shadow_run):
    with shadow_run.target.write() as conn:
        conn.execute(
            "UPDATE chunk_embeddings SET vector=%s WHERE chunk_id=%s",
            (struct.pack("<3f", 1.0, 2.0, 3.0), "verify-chunk-1"),
        )
    report = _verifier(shadow_run).verify(
        run_id=shadow_run.run_id,
        level=VerificationLevel.FULL,
    )
    assert report.status is VerificationStatus.DRIFT
    assert any(item.category == "embedding_dimension" for item in report.differences)

    with shadow_run.target.write() as conn:
        conn.execute(
            "UPDATE chunk_embeddings SET vector=%s WHERE chunk_id=%s",
            (
                struct.pack("<4f", 1.0, 2.0, 3.0, float(len("verify-chunk-1"))),
                "verify-chunk-1",
            ),
        )
    structural = _verifier(shadow_run).verify(
        run_id=shadow_run.run_id,
        level=VerificationLevel.STRUCTURAL,
    )
    assert structural.status is VerificationStatus.COMPLETE
    with shadow_run.target.connect() as conn:
        status = conn.execute(
            "SELECT status FROM silicon_shadow.verification_runs "
            "WHERE verification_id=%s",
            (report.verification_id,),
        ).fetchone()["status"]
    assert status == "drift"
    full = _verifier(shadow_run).verify(
        run_id=shadow_run.run_id,
        level=VerificationLevel.FULL,
    )
    assert full.status is VerificationStatus.COMPLETE
    with shadow_run.target.connect() as conn:
        status = conn.execute(
            "SELECT status FROM silicon_shadow.verification_runs "
            "WHERE verification_id=%s",
            (report.verification_id,),
        ).fetchone()["status"]
    assert status == "superseded"


def test_cutover_requires_and_accepts_two_complete_frozen_passes(shadow_run):
    first = _verifier(shadow_run).verify(
        run_id=shadow_run.run_id,
        level=VerificationLevel.FULL,
    )
    assert first.status is VerificationStatus.COMPLETE
    with shadow_run.source.write() as conn:
        conn.execute(
            "UPDATE shadow_capture_control SET write_frozen=1 WHERE singleton=1"
        )
    with shadow_run.target.write() as conn:
        conn.execute(
            "UPDATE silicon_shadow.runs SET phase='cutover_readonly',revision=revision+1 "
            "WHERE run_id=%s",
            (shadow_run.run_id,),
        )

    def unfreeze_after_scan(_source_seen: int) -> None:
        with shadow_run.source.write() as conn:
            conn.execute(
                "UPDATE shadow_capture_control SET write_frozen=0 WHERE singleton=1"
            )

    unfrozen = _verifier(
        shadow_run,
        hooks=VerificationHooks(after_concurrent_scan=unfreeze_after_scan),
    ).verify(run_id=shadow_run.run_id, level=VerificationLevel.CUTOVER)
    assert unfrozen.status is VerificationStatus.DRIFT
    assert any(item.category == "cutover_frozen" for item in unfrozen.differences)
    with shadow_run.source.write() as conn:
        conn.execute(
            "UPDATE shadow_capture_control SET write_frozen=1 WHERE singleton=1"
        )
    second = _verifier(shadow_run).verify(
        run_id=shadow_run.run_id,
        level=VerificationLevel.FULL,
    )
    assert second.status is VerificationStatus.COMPLETE
    cutover = _verifier(shadow_run).verify(
        run_id=shadow_run.run_id,
        level=VerificationLevel.CUTOVER,
    )
    assert cutover.status is VerificationStatus.COMPLETE
    assert cutover.concurrent_keys == 0
    assert cutover.coverage == 1.0
