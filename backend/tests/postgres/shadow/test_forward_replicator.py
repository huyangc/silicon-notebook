from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from app.core.config import Settings
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
from app.migration.shadow.metrics import ShadowMetrics
from app.migration.shadow.replicator import (
    CapacityExceeded,
    Direction,
    OrderingBlocked,
    PoisonEventRecorded,
    ShadowReplicator,
    _MAX_DEFERRED_STATEMENT_FACTOR,
    _StatementBudget,
)
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SqliteMigrator


pytestmark = pytest.mark.postgres_integration
REPO_ROOT = Path(__file__).resolve().parents[4]
SECRET = b"forward-replicator-test-secret-32b"


@pytest.fixture
def clean_control_schema(postgres_scope):
    with psycopg.connect(postgres_scope.base_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS silicon_shadow CASCADE")
    try:
        yield
    finally:
        with psycopg.connect(postgres_scope.base_url, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS silicon_shadow CASCADE")


@pytest.fixture
def forward_run(tmp_path: Path, postgres_database, clean_control_schema):
    run_id = f"forward-{uuid.uuid4().hex}"
    path = tmp_path / "forward-source.db"
    storage = tmp_path / "storage"
    storage.mkdir()
    settings = Settings(database_url=f"sqlite:///{path}", _env_file=None)
    source = SqliteDatabase(settings, REPO_ROOT)
    SqliteMigrator(source, settings).migrate()
    source_conn = source.connect()
    report = preflight(
        run_id=run_id,
        source_url=settings.database_url,
        source_conn=source_conn,
        storage_root=storage,
        target=postgres_database,
        disk_evidence=DiskEvidence("forward-disk", 10_000_000_000, True),
        backup_evidence=BackupEvidence("forward-backup", True, True),
        confirmation_secret=SECRET,
        now=1_000,
    )
    assert PostgresMigrator(postgres_database).migrate() == 11
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
        postgres_database, manifest=MANIFEST, run_id=run_id
    )
    state = get_run(postgres_database, run_id)
    state = commit_guard_report(
        postgres_database, report=sqlite_report, expected_revision=state.revision
    )
    state = commit_guard_report(
        postgres_database, report=postgres_report, expected_revision=state.revision
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
        progress={"stage": "forward", "baseline_seq": 0, "applied_seq": 0},
        source_url=settings.database_url,
        source_conn=source_conn,
    )
    with postgres_database.write() as conn:
        conn.execute(
            "INSERT INTO silicon_shadow.checkpoints(run_id,direction,last_seq,revision) "
            "VALUES (%s,'sqlite_to_postgres',0,0)",
            (run_id,),
        )
    try:
        yield source, postgres_database, run_id
    finally:
        source.close_local()


def _replicator(forward_run, **kwargs) -> ShadowReplicator:
    source, target, _run_id = forward_run
    return ShadowReplicator(
        source,
        target,
        base_backoff_seconds=0,
        max_backoff_seconds=0,
        **kwargs,
    )


def _run(replicator: ShadowReplicator, run_id: str, **kwargs):
    return replicator.run_batch(
        run_id=run_id,
        direction=Direction.SQLITE_TO_POSTGRES,
        max_events=kwargs.get("max_events", 100),
        max_bytes=kwargs.get("max_bytes", 1_000_000),
    )


def _target_setting(target, key: str):
    with target.connect() as conn:
        return conn.execute(
            "SELECT key,value FROM app_settings WHERE key=%s", (key,)
        ).fetchone()


def _checkpoint(target, run_id: str) -> int:
    with target.connect() as conn:
        row = conn.execute(
            "SELECT last_seq FROM silicon_shadow.checkpoints "
            "WHERE run_id=%s AND direction='sqlite_to_postgres'",
            (run_id,),
        ).fetchone()
    return int(row["last_seq"])


def test_forward_insert_update_delete_key_update_and_current_hydration(forward_run):
    source, target, run_id = forward_run
    replicator = _replicator(forward_run)

    with source.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('alpha','one',CURRENT_TIMESTAMP)"
        )
    first = _run(replicator, run_id)
    assert (first.events_read, first.rows_applied, first.checkpoint_after) == (1, 1, 1)
    assert _target_setting(target, "alpha")["value"] == "one"

    with source.write() as conn:
        conn.execute("UPDATE app_settings SET value='two' WHERE key='alpha'")
        conn.execute("UPDATE app_settings SET value='three' WHERE key='alpha'")
    updated = _run(replicator, run_id)
    # Raw seqs remain contiguous, while repeated stable keys coalesce to the
    # final current row and a single target apply.
    assert (updated.events_read, updated.rows_applied) == (2, 1)
    assert _target_setting(target, "alpha")["value"] == "three"

    with source.write() as conn:
        conn.execute("UPDATE app_settings SET key='beta' WHERE key='alpha'")
    key_update = _run(replicator, run_id)
    assert (key_update.events_read, key_update.rows_applied) == (2, 2)
    assert _target_setting(target, "alpha") is None
    assert _target_setting(target, "beta")["value"] == "three"

    with source.write() as conn:
        conn.execute("DELETE FROM app_settings WHERE key='beta'")
    deleted = _run(replicator, run_id)
    assert deleted.events_read == 1
    assert _target_setting(target, "beta") is None

    # The final delete replaces the earlier same-key upsert while both raw seqs
    # still advance the checkpoint contiguously.
    with source.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('gone','x',CURRENT_TIMESTAMP)"
        )
        conn.execute("DELETE FROM app_settings WHERE key='gone'")
    converged = _run(replicator, run_id)
    assert (converged.events_read, converged.rows_applied) == (2, 1)
    assert _target_setting(target, "gone") is None
    assert _checkpoint(target, run_id) == 8


def test_forward_batch_is_bounded_and_single_oversize_event_makes_progress(forward_run):
    source, target, run_id = forward_run
    replicator = _replicator(forward_run)
    with source.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('small','x',CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('large',?,CURRENT_TIMESTAMP)",
            ("y" * 20_000,),
        )
    first = _run(replicator, run_id, max_bytes=1_000)
    assert (first.events_read, first.checkpoint_after, first.oversized_event) == (
        1,
        1,
        False,
    )
    assert _target_setting(target, "small") is not None
    assert _target_setting(target, "large") is None
    second = _run(replicator, run_id, max_bytes=1_000)
    assert (second.events_read, second.checkpoint_after, second.oversized_event) == (
        1,
        2,
        True,
    )
    assert second.hydrated_bytes > 1_000
    assert _target_setting(target, "large") is not None


def test_repeated_oversize_identity_coalesces_bytes_and_advances_all_raw_seqs(
    forward_run,
):
    source, target, run_id = forward_run
    replicator = _replicator(forward_run)
    with source.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('coalesced-large','small',CURRENT_TIMESTAMP)"
        )
    assert _run(replicator, run_id).checkpoint_after == 1
    with source.write() as conn:
        conn.execute(
            "UPDATE app_settings SET value=? WHERE key='coalesced-large'",
            ("x" * 20_000,),
        )
        conn.execute(
            "UPDATE app_settings SET value=? WHERE key='coalesced-large'",
            ("y" * 20_000,),
        )

    result = _run(replicator, run_id, max_bytes=1_000)

    assert (result.events_read, result.rows_applied, result.checkpoint_after) == (
        2,
        1,
        3,
    )
    assert result.oversized_event
    assert result.hydrated_bytes > 20_000
    assert _target_setting(target, "coalesced-large")["value"] == "y" * 20_000


def test_interleaved_stable_keys_sort_by_last_seq_and_delete_upsert_coalesces(
    forward_run,
):
    source, target, run_id = forward_run
    with source.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('coalesce-a','initial',CURRENT_TIMESTAMP)"
        )
    replicator = _replicator(forward_run)
    assert _run(replicator, run_id).checkpoint_after == 1
    with source.write() as conn:
        conn.execute("UPDATE app_settings SET value='middle' WHERE key='coalesce-a'")
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('coalesce-b','other',CURRENT_TIMESTAMP)"
        )
        conn.execute("UPDATE app_settings SET value='final' WHERE key='coalesce-a'")
    observed: list[str] = []
    apply_statement = replicator._apply_statement

    def observe(conn, schema, item):
        if item.spec.name == "app_settings":
            observed.append(str(item.event.key_values[0]))
        return apply_statement(conn, schema, item)

    replicator._apply_statement = observe
    result = _run(replicator, run_id)
    assert (result.events_read, result.rows_applied, result.checkpoint_after) == (
        3,
        2,
        4,
    )
    assert observed == ["coalesce-b", "coalesce-a"]

    observed.clear()
    with source.write() as conn:
        conn.execute("DELETE FROM app_settings WHERE key='coalesce-a'")
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('coalesce-a','reborn',CURRENT_TIMESTAMP)"
        )
    result = _run(replicator, run_id)
    assert (result.events_read, result.rows_applied, result.checkpoint_after) == (
        2,
        1,
        6,
    )
    assert observed == ["coalesce-a"]
    assert _target_setting(target, "coalesce-a")["value"] == "reborn"


def test_metrics_are_redacted_and_duplicate_replay_is_a_noop(forward_run):
    source, target, run_id = forward_run
    metrics = ShadowMetrics()
    replicator = _replicator(forward_run, metrics=metrics)
    with source.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('private-key','private-value',CURRENT_TIMESTAMP)"
        )
    applied = _run(replicator, run_id)
    replay = _run(replicator, run_id)
    assert replay.events_read == 0
    assert (
        replay.checkpoint_before == replay.checkpoint_after == applied.checkpoint_after
    )
    encoded = json.dumps([item.__dict__ for item in metrics.snapshot()])
    assert "private-key" not in encoded and "private-value" not in encoded
    assert "table" not in encoded and "worker" not in encoded
    assert len(metrics.snapshot()) == 2
    assert _target_setting(target, "private-key")["value"] == "private-value"


@pytest.mark.parametrize(
    ("mutation", "category"),
    [
        ("gap", "gap"),
        ("run", "event_contract"),
        ("epoch", "event_contract"),
        ("table", "event_contract"),
        ("operation", "event_contract"),
        ("key", "event_contract"),
    ],
)
def test_invalid_source_event_records_one_poison_and_never_advances(
    forward_run, mutation: str, category: str
):
    source, target, run_id = forward_run
    with source.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('bad','x',CURRENT_TIMESTAMP)"
        )
        if mutation == "gap":
            conn.execute("DELETE FROM shadow_change_log WHERE seq=1")
        elif mutation == "run":
            conn.execute("UPDATE shadow_change_log SET run_id='other' WHERE seq=1")
        elif mutation == "epoch":
            conn.execute("UPDATE shadow_change_log SET schema_epoch=99 WHERE seq=1")
        elif mutation == "table":
            conn.execute(
                "UPDATE shadow_change_log SET table_name='unknown' WHERE seq=1"
            )
        elif mutation == "operation":
            conn.execute("PRAGMA ignore_check_constraints=ON")
            conn.execute("UPDATE shadow_change_log SET operation='skip' WHERE seq=1")
        else:
            conn.execute("UPDATE shadow_change_log SET key_json='{}' WHERE seq=1")
    with pytest.raises(PoisonEventRecorded) as error:
        _run(_replicator(forward_run), run_id)
    assert error.value.source_seq == 1
    assert error.value.category == category
    assert _checkpoint(target, run_id) == 0
    with target.connect() as conn:
        poison = conn.execute(
            "SELECT source_seq,category,safe_summary FROM silicon_shadow.poison_events "
            "WHERE run_id=%s",
            (run_id,),
        ).fetchall()
    assert len(poison) == 1
    assert int(poison[0]["source_seq"]) == 1
    assert "bad" not in poison[0]["safe_summary"]
    with pytest.raises(PoisonEventRecorded, match="existing"):
        _run(_replicator(forward_run), run_id)


def test_conversion_poison_uses_the_actual_failing_seq_and_rolls_back_prefix(
    forward_run,
):
    source, target, run_id = forward_run
    with source.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('valid','x',CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) VALUES('invalid','x','not-time')"
        )
    with pytest.raises(PoisonEventRecorded) as error:
        _run(_replicator(forward_run), run_id)
    assert (error.value.source_seq, error.value.category) == (2, "conversion")
    assert _checkpoint(target, run_id) == 0
    assert _target_setting(target, "valid") is None


def test_ordinal_hydration_preserves_sqlite_rowid(forward_run):
    source, target, run_id = forward_run
    with source.write() as conn:
        conn.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "VALUES('notebook-1','N',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO answers(id,notebook_id,question,payload,created_at) "
            "VALUES('answer-1','notebook-1','q','{}',CURRENT_TIMESTAMP)"
        )
        rowid = int(
            conn.execute("SELECT rowid FROM answers WHERE id='answer-1'").fetchone()[0]
        )
    replicator = _replicator(forward_run)
    original = replicator._apply_statement
    observed: list[int] = []

    def observe(conn, schema, item):
        if item.spec.name == "answers" and item.row is not None:
            observed.append(int(item.row[-1]))
        return original(conn, schema, item)

    replicator._apply_statement = observe
    result = _run(replicator, run_id)
    assert result.events_read == 2
    assert observed == [rowid]
    with target.connect() as conn:
        target_ordinal = conn.execute(
            "SELECT ordinal FROM answers WHERE id='answer-1'"
        ).fetchone()["ordinal"]
    assert int(target_ordinal) == rowid


def test_source_reader_never_mutates_live_sqlite(forward_run):
    source, _target, run_id = forward_run
    with source.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('audit','x',CURRENT_TIMESTAMP)"
        )
    before = (
        source.connect()
        .execute("SELECT total_changes(),COUNT(*) FROM shadow_change_log")
        .fetchone()
    )
    _run(_replicator(forward_run), run_id)
    after = (
        source.connect()
        .execute("SELECT total_changes(),COUNT(*) FROM shadow_change_log")
        .fetchone()
    )
    assert tuple(before) == tuple(after)


def test_max_events_advances_only_the_real_accepted_prefix(forward_run):
    source, target, run_id = forward_run
    with source.write() as conn:
        for key in ("limit-a", "limit-b"):
            conn.execute(
                "INSERT INTO app_settings(key,value,updated_at) "
                "VALUES(?, 'x', CURRENT_TIMESTAMP)",
                (key,),
            )
    replicator = _replicator(forward_run)
    first = _run(replicator, run_id, max_events=1)
    assert (first.events_read, first.checkpoint_after) == (1, 1)
    assert _target_setting(target, "limit-a") is not None
    assert _target_setting(target, "limit-b") is None
    second = _run(replicator, run_id, max_events=1)
    assert (second.events_read, second.checkpoint_after) == (1, 2)
    assert _target_setting(target, "limit-b") is not None


def test_delete_does_not_transform_a_future_invalid_reinsert(forward_run):
    source, target, run_id = forward_run
    replicator = _replicator(forward_run)
    with source.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('delete-before-invalid','old',CURRENT_TIMESTAMP)"
        )
    assert _run(replicator, run_id).checkpoint_after == 1

    with source.write() as conn:
        conn.execute("DELETE FROM app_settings WHERE key='delete-before-invalid'")
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('delete-before-invalid','new','not-time')"
        )

    deleted = _run(replicator, run_id, max_events=1)
    assert (deleted.events_read, deleted.checkpoint_after) == (1, 2)
    assert _target_setting(target, "delete-before-invalid") is None

    with pytest.raises(PoisonEventRecorded) as error:
        _run(replicator, run_id, max_events=1)
    assert (error.value.source_seq, error.value.category) == (3, "conversion")
    assert _checkpoint(target, run_id) == 2


def test_delete_bytes_ignore_a_future_large_reinsert(forward_run):
    source, target, run_id = forward_run
    replicator = _replicator(forward_run)
    with source.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('delete-before-large','old',CURRENT_TIMESTAMP)"
        )
    assert _run(replicator, run_id).checkpoint_after == 1

    with source.write() as conn:
        conn.execute("DELETE FROM app_settings WHERE key='delete-before-large'")
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES(?,?,CURRENT_TIMESTAMP)",
            ("delete-before-large", "x" * 2_000_000),
        )

    deleted = _run(replicator, run_id, max_events=1, max_bytes=128)
    assert (deleted.events_read, deleted.checkpoint_after) == (1, 2)
    assert deleted.hydrated_bytes == 0
    assert not deleted.oversized_event
    assert _target_setting(target, "delete-before-large") is None


def test_fk_parent_current_state_is_hydrated_across_batch_boundary(forward_run):
    source, target, run_id = forward_run
    replicator = _replicator(forward_run)
    with source.write() as conn:
        conn.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "VALUES('old-parent','old',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO answers(id,notebook_id,question,payload,created_at) "
            "VALUES('moving-child','old-parent','q','{}',CURRENT_TIMESTAMP)"
        )
    assert _run(replicator, run_id).checkpoint_after == 2
    with source.write() as conn:
        conn.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "VALUES('new-parent','new',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "UPDATE answers SET notebook_id='new-parent' WHERE id='moving-child'"
        )
        first = conn.execute(
            "SELECT table_name,key_json,operation FROM shadow_change_log WHERE seq=3"
        ).fetchone()
        second = conn.execute(
            "SELECT table_name,key_json,operation FROM shadow_change_log WHERE seq=4"
        ).fetchone()
        conn.execute(
            "UPDATE shadow_change_log SET table_name=?,key_json=?,operation=? WHERE seq=3",
            tuple(second),
        )
        conn.execute(
            "UPDATE shadow_change_log SET table_name=?,key_json=?,operation=? WHERE seq=4",
            tuple(first),
        )
    first_batch = _run(replicator, run_id, max_events=1)
    assert first_batch.checkpoint_after == 3
    with target.connect() as conn:
        assert (
            conn.execute(
                "SELECT notebook_id FROM answers WHERE id='moving-child'"
            ).fetchone()["notebook_id"]
            == "new-parent"
        )
        assert (
            conn.execute("SELECT name FROM notebooks WHERE id='new-parent'").fetchone()[
                "name"
            ]
            == "new"
        )


@pytest.mark.parametrize("mutation", ["wrong-run", "wrong-epoch", "delete"])
def test_fk_parent_hydration_never_uses_future_log_metadata(forward_run, mutation: str):
    source, target, run_id = forward_run
    with source.write() as conn:
        conn.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "VALUES('metadata-parent','parent',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO answers(id,notebook_id,question,payload,created_at) "
            "VALUES('metadata-child','metadata-parent','q','{}',CURRENT_TIMESTAMP)"
        )
        first = conn.execute(
            "SELECT table_name,key_json,operation FROM shadow_change_log WHERE seq=1"
        ).fetchone()
        second = conn.execute(
            "SELECT table_name,key_json,operation FROM shadow_change_log WHERE seq=2"
        ).fetchone()
        conn.execute(
            "UPDATE shadow_change_log SET table_name=?,key_json=?,operation=? WHERE seq=1",
            tuple(second),
        )
        conn.execute(
            "UPDATE shadow_change_log SET table_name=?,key_json=?,operation=? WHERE seq=2",
            tuple(first),
        )
        if mutation == "wrong-run":
            conn.execute("UPDATE shadow_change_log SET run_id='other' WHERE seq=2")
        elif mutation == "wrong-epoch":
            conn.execute("UPDATE shadow_change_log SET schema_epoch=99 WHERE seq=2")
        else:
            conn.execute("UPDATE shadow_change_log SET operation='delete' WHERE seq=2")
    result = _run(_replicator(forward_run), run_id, max_events=1)
    assert result.checkpoint_after == 1
    with target.connect() as conn:
        assert (
            conn.execute(
                "SELECT notebook_id FROM answers WHERE id='metadata-child'"
            ).fetchone()["notebook_id"]
            == "metadata-parent"
        )
        assert (
            conn.execute(
                "SELECT name FROM notebooks WHERE id='metadata-parent'"
            ).fetchone()["name"]
            == "parent"
        )


def test_fk_dependency_hydration_has_no_change_log_suffix_scan(
    forward_run, monkeypatch
):
    source, _target, run_id = forward_run
    with source.write() as conn:
        conn.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "VALUES('budget-parent','parent',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO answers(id,notebook_id,question,payload,created_at) "
            "VALUES('budget-child','budget-parent','q','{}',CURRENT_TIMESTAMP)"
        )
        first = conn.execute(
            "SELECT table_name,key_json,operation FROM shadow_change_log WHERE seq=1"
        ).fetchone()
        second = conn.execute(
            "SELECT table_name,key_json,operation FROM shadow_change_log WHERE seq=2"
        ).fetchone()
        conn.execute(
            "UPDATE shadow_change_log SET table_name=?,key_json=?,operation=? WHERE seq=1",
            tuple(second),
        )
        conn.execute(
            "UPDATE shadow_change_log SET table_name=?,key_json=?,operation=? WHERE seq=2",
            tuple(first),
        )
    statements: list[str] = []
    from app.migration.shadow import replicator as replicator_module

    original_open = replicator_module.open_fresh_live_sqlite

    def traced_open(database):
        conn, binding = original_open(database)
        conn.set_trace_callback(statements.append)
        return conn, binding

    monkeypatch.setattr(replicator_module, "open_fresh_live_sqlite", traced_open)
    _run(_replicator(forward_run), run_id, max_events=1)
    log_reads = [
        statement for statement in statements if "FROM shadow_change_log" in statement
    ]
    event_reads = [statement for statement in log_reads if "key_json" in statement]
    assert len(event_reads) == 1
    assert "WHERE seq>0 ORDER BY seq LIMIT 1" in event_reads[0]
    # Continuity checks may read the high-water mark and probe exactly the
    # adjacent sequence, but FK hydration must never inspect suffix payloads.
    assert any("COALESCE(MAX(seq),0)" in statement for statement in log_reads)
    assert any(
        "SELECT 1 FROM shadow_change_log WHERE seq=2" in statement
        for statement in log_reads
    )


def test_parent_child_prefix_is_not_coalesced_away(forward_run):
    source, target, run_id = forward_run
    with source.write() as conn:
        conn.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "VALUES('parent','one',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO answers(id,notebook_id,question,payload,created_at) "
            "VALUES('child','parent','q','{}',CURRENT_TIMESTAMP)"
        )
        conn.execute("UPDATE notebooks SET name='two' WHERE id='parent'")
    result = _run(_replicator(forward_run), run_id, max_events=2)
    assert (result.events_read, result.checkpoint_after) == (2, 2)
    with target.connect() as conn:
        assert (
            conn.execute("SELECT name FROM notebooks WHERE id='parent'").fetchone()[
                "name"
            ]
            == "two"
        )
        assert (
            conn.execute("SELECT notebook_id FROM answers WHERE id='child'").fetchone()[
                "notebook_id"
            ]
            == "parent"
        )


def test_fk_dependency_beyond_prefix_is_not_lost(forward_run):
    source, target, run_id = forward_run
    with source.write() as conn:
        conn.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "VALUES('old','old',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO answers(id,notebook_id,question,payload,created_at) "
            "VALUES('child','old','q','{}',CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "VALUES('future','one',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
        conn.execute("UPDATE answers SET notebook_id='future' WHERE id='child'")
        conn.execute("UPDATE notebooks SET name='two' WHERE id='future'")
        conn.execute("DELETE FROM shadow_change_log WHERE seq=4")
        conn.execute("UPDATE shadow_change_log SET seq=4 WHERE seq=5")
    result = _run(_replicator(forward_run), run_id, max_events=2)
    assert result.checkpoint_after == 2
    with target.connect() as conn:
        assert (
            conn.execute("SELECT notebook_id FROM answers WHERE id='child'").fetchone()[
                "notebook_id"
            ]
            == "future"
        )
        assert (
            conn.execute("SELECT name FROM notebooks WHERE id='future'").fetchone()[
                "name"
            ]
            == "two"
        )


def test_unique_release_before_acquire_is_applied_in_order(forward_run):
    source, target, run_id = forward_run
    with source.write() as conn:
        for user_id, email in (
            ("unique-a", "a@example.test"),
            ("unique-b", "b@example.test"),
        ):
            conn.execute(
                "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at) "
                "VALUES(?,?,?,'user','active',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                (user_id, email, user_id),
            )
    replicator = _replicator(forward_run)
    assert _run(replicator, run_id).checkpoint_after == 2
    with source.write() as conn:
        conn.execute(
            "UPDATE users SET email='released@example.test' WHERE id='unique-a'"
        )
        conn.execute("UPDATE users SET email='a@example.test' WHERE id='unique-b'")
    assert _run(replicator, run_id).checkpoint_after == 4
    with target.connect() as conn:
        rows = conn.execute(
            "SELECT id,email FROM users WHERE id IN ('unique-a','unique-b') ORDER BY id"
        ).fetchall()
    assert [(row["id"], row["email"]) for row in rows] == [
        ("unique-a", "released@example.test"),
        ("unique-b", "a@example.test"),
    ]


def test_ordering_blocked_never_poison_or_advance(forward_run):
    source, target, run_id = forward_run
    with source.write() as conn:
        for user_id, email in (
            ("swap-a", "swap-a@example.test"),
            ("swap-b", "swap-b@example.test"),
        ):
            conn.execute(
                "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at) "
                "VALUES(?,?,?,'user','active',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                (user_id, email, user_id),
            )
    replicator = _replicator(forward_run)
    assert _run(replicator, run_id).checkpoint_after == 2
    with source.write() as conn:
        conn.execute(
            "UPDATE users SET email='temporary@example.test' WHERE id='swap-a'"
        )
        conn.execute("UPDATE users SET email='swap-a@example.test' WHERE id='swap-b'")
        conn.execute("UPDATE users SET email='swap-b@example.test' WHERE id='swap-a'")
    with pytest.raises(OrderingBlocked):
        _run(replicator, run_id, max_events=1)
    assert _checkpoint(target, run_id) == 2
    with target.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS count FROM silicon_shadow.poison_events WHERE run_id=%s",
                (run_id,),
            ).fetchone()["count"]
            == 0
        )
        rows = conn.execute(
            "SELECT id,email FROM users WHERE id IN ('swap-a','swap-b') ORDER BY id"
        ).fetchall()
    assert [(row["id"], row["email"]) for row in rows] == [
        ("swap-a", "swap-a@example.test"),
        ("swap-b", "swap-b@example.test"),
    ]
    converged = _run(replicator, run_id, max_events=3)
    assert converged.checkpoint_after == 5
    with target.connect() as conn:
        rows = conn.execute(
            "SELECT id,email FROM users WHERE id IN ('swap-a','swap-b') ORDER BY id"
        ).fetchall()
    assert [(row["id"], row["email"]) for row in rows] == [
        ("swap-a", "swap-b@example.test"),
        ("swap-b", "swap-a@example.test"),
    ]


def test_suffix_gap_poison_precedes_an_ordering_blocked_unique_prefix(forward_run):
    source, target, run_id = forward_run
    with source.write() as conn:
        for user_id, email in (
            ("gap-swap-a", "gap-swap-a@example.test"),
            ("gap-swap-b", "gap-swap-b@example.test"),
        ):
            conn.execute(
                "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at) "
                "VALUES(?,?,?,'user','active',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                (user_id, email, user_id),
            )
    replicator = _replicator(forward_run)
    assert _run(replicator, run_id).checkpoint_after == 2

    with source.write() as conn:
        conn.execute(
            "UPDATE users SET email='gap-swap-temp@example.test' WHERE id='gap-swap-a'"
        )
        conn.execute(
            "UPDATE users SET email='gap-swap-a@example.test' WHERE id='gap-swap-b'"
        )
        conn.execute(
            "UPDATE users SET email='gap-swap-b@example.test' WHERE id='gap-swap-a'"
        )
        second = conn.execute(
            "SELECT run_id,table_name,key_json,operation,schema_epoch "
            "FROM shadow_change_log WHERE seq=4"
        ).fetchone()
        third = conn.execute(
            "SELECT run_id,table_name,key_json,operation,schema_epoch "
            "FROM shadow_change_log WHERE seq=5"
        ).fetchone()
        conn.execute(
            "UPDATE shadow_change_log SET run_id=?,table_name=?,key_json=?,"
            "operation=?,schema_epoch=? WHERE seq=3",
            tuple(second),
        )
        conn.execute(
            "UPDATE shadow_change_log SET run_id=?,table_name=?,key_json=?,"
            "operation=?,schema_epoch=? WHERE seq=4",
            tuple(third),
        )
        conn.execute("DELETE FROM shadow_change_log WHERE seq=5")

    with pytest.raises(PoisonEventRecorded) as error:
        _run(replicator, run_id, max_events=100)
    assert (error.value.source_seq, error.value.category) == (5, "gap")
    assert _checkpoint(target, run_id) == 2
    with target.connect() as conn:
        rows = conn.execute(
            "SELECT id,email FROM users WHERE id IN ('gap-swap-a','gap-swap-b') "
            "ORDER BY id"
        ).fetchall()
    assert [(row["id"], row["email"]) for row in rows] == [
        ("gap-swap-a", "gap-swap-a@example.test"),
        ("gap-swap-b", "gap-swap-b@example.test"),
    ]


def test_five_independent_unique_swaps_converge_within_pass_budget(forward_run):
    source, target, run_id = forward_run
    with source.write() as conn:
        for group in range(5):
            for side in ("a", "b"):
                user_id = f"five-{group}-{side}"
                conn.execute(
                    "INSERT INTO users"
                    "(id,email,display_name,role,status,created_at,updated_at) "
                    "VALUES(?,?,?,'user','active',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                    (user_id, f"{user_id}@example.test", user_id),
                )
    replicator = _replicator(forward_run)
    assert _run(replicator, run_id).checkpoint_after == 10

    with source.write() as conn:
        for group in range(5):
            conn.execute(
                "UPDATE users SET email=? WHERE id=?",
                (f"five-{group}-temp@example.test", f"five-{group}-a"),
            )
            conn.execute(
                "UPDATE users SET email=? WHERE id=?",
                (f"five-{group}-a@example.test", f"five-{group}-b"),
            )
            conn.execute(
                "UPDATE users SET email=? WHERE id=?",
                (f"five-{group}-b@example.test", f"five-{group}-a"),
            )

    result = _run(replicator, run_id)
    assert result.checkpoint_after == 25
    with target.connect() as conn:
        rows = conn.execute(
            "SELECT id,email FROM users WHERE id LIKE 'five-%' ORDER BY id"
        ).fetchall()
    assert [(row["id"], row["email"]) for row in rows] == [
        (
            f"five-{group}-{side}",
            f"five-{group}-{'b' if side == 'a' else 'a'}@example.test",
        )
        for group in range(5)
        for side in ("a", "b")
    ]


def test_two_unique_surfaces_swap_and_restore_every_parked_value(
    forward_run, monkeypatch
):
    source, target, run_id = forward_run
    consumed: list[int] = []
    original_consume = _StatementBudget.consume

    def observe_consume(budget):
        original_consume(budget)
        consumed.append(budget.used)

    monkeypatch.setattr(_StatementBudget, "consume", observe_consume)
    with source.write() as conn:
        for user_id, email, username in (
            ("double-a", "double-a@example.test", "a00100001"),
            ("double-b", "double-b@example.test", "b00100002"),
        ):
            conn.execute(
                "INSERT INTO users"
                "(id,email,display_name,role,status,created_at,updated_at,username) "
                "VALUES(?,?,?,'user','active',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,?)",
                (user_id, email, user_id, username),
            )
    replicator = _replicator(forward_run)
    assert _run(replicator, run_id).checkpoint_after == 2
    with source.write() as conn:
        conn.execute(
            "UPDATE users SET email='double-temp@example.test' WHERE id='double-a'"
        )
        conn.execute(
            "UPDATE users SET email='double-a@example.test' WHERE id='double-b'"
        )
        conn.execute(
            "UPDATE users SET email='double-b@example.test' WHERE id='double-a'"
        )
        conn.execute("UPDATE users SET username='c00100003' WHERE id='double-a'")
        conn.execute("UPDATE users SET username='a00100001' WHERE id='double-b'")
        conn.execute("UPDATE users SET username='b00100002' WHERE id='double-a'")
    with pytest.raises(OrderingBlocked):
        _run(replicator, run_id, max_events=1)
    result = _run(replicator, run_id, max_events=6)
    assert result.checkpoint_after == 8
    with target.connect() as conn:
        rows = conn.execute(
            "SELECT id,email,username FROM users "
            "WHERE id IN ('double-a','double-b') ORDER BY id"
        ).fetchall()
        residue = conn.execute(
            "SELECT COUNT(*) AS count FROM users "
            "WHERE email LIKE '__shadow_park%' OR username LIKE '__shadow_park%'"
        ).fetchone()["count"]
    assert [(row["id"], row["email"], row["username"]) for row in rows] == [
        ("double-a", "double-b@example.test", "b00100002"),
        ("double-b", "double-a@example.test", "a00100001"),
    ]
    assert residue == 0
    assert max(consumed) < _MAX_DEFERRED_STATEMENT_FACTOR * result.rows_applied


def test_bigint_parking_finds_gap_after_eight_occupied_min_values(forward_run):
    source, target, run_id = forward_run
    minimum = -(2**63)
    with source.write() as conn:
        conn.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "VALUES('ordinal-parent','parent',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
        for index in range(8):
            conn.execute(
                "INSERT INTO answers(rowid,id,notebook_id,question,payload,created_at) "
                "VALUES(?,?, 'ordinal-parent','q','{}',CURRENT_TIMESTAMP)",
                (minimum + index, f"ordinal-{index}"),
            )
    replicator = _replicator(forward_run)
    assert _run(replicator, run_id).checkpoint_after == 9
    with source.write() as conn:
        conn.execute("UPDATE answers SET rowid=100 WHERE id='ordinal-0'")
        conn.execute("UPDATE answers SET rowid=? WHERE id='ordinal-1'", (minimum,))
        conn.execute("UPDATE answers SET rowid=? WHERE id='ordinal-0'", (minimum + 1,))
    with pytest.raises(OrderingBlocked):
        _run(replicator, run_id, max_events=1)
    assert _run(replicator, run_id, max_events=3).checkpoint_after == 12
    with target.connect() as conn:
        rows = conn.execute(
            "SELECT id,ordinal FROM answers WHERE id IN ('ordinal-0','ordinal-1') "
            "ORDER BY id"
        ).fetchall()
        gap_residue = conn.execute(
            "SELECT COUNT(*) AS count FROM answers WHERE ordinal=%s", (minimum + 8,)
        ).fetchone()["count"]
    assert [(row["id"], row["ordinal"]) for row in rows] == [
        ("ordinal-0", minimum + 1),
        ("ordinal-1", minimum),
    ]
    assert gap_residue == 0


@pytest.mark.parametrize("capacity", ["candidate", "pass"])
def test_unique_parking_capacity_exhaustion_is_non_poison(
    forward_run, monkeypatch, capacity: str
):
    source, target, run_id = forward_run
    with source.write() as conn:
        for user_id, email in (
            ("capacity-a", "capacity-a@example.test"),
            ("capacity-b", "capacity-b@example.test"),
        ):
            conn.execute(
                "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at) "
                "VALUES(?,?,?,'user','active',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                (user_id, email, user_id),
            )
    replicator = _replicator(forward_run)
    assert _run(replicator, run_id).checkpoint_after == 2
    with source.write() as conn:
        conn.execute(
            "UPDATE users SET email='capacity-temp@example.test' WHERE id='capacity-a'"
        )
        conn.execute(
            "UPDATE users SET email='capacity-a@example.test' WHERE id='capacity-b'"
        )
        conn.execute(
            "UPDATE users SET email='capacity-b@example.test' WHERE id='capacity-a'"
        )

    expected = OrderingBlocked
    if capacity == "candidate":

        def no_candidate(*_args, **_kwargs):
            raise CapacityExceeded("candidate search capacity")

        monkeypatch.setattr(replicator, "_parking_candidate", no_candidate)
        expected = CapacityExceeded
    else:
        monkeypatch.setattr("app.migration.shadow.replicator._MAX_DEFERRED_PASSES", 0)

    with pytest.raises(expected):
        _run(replicator, run_id, max_events=3)
    assert _checkpoint(target, run_id) == 2
    with target.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS count FROM silicon_shadow.poison_events "
                "WHERE run_id=%s",
                (run_id,),
            ).fetchone()["count"]
            == 0
        )


def test_unparkable_target_only_unique_drift_is_actual_seq_poison(forward_run):
    source, target, run_id = forward_run
    with source.write() as conn:
        conn.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at) "
            "VALUES('source-user','source@example.test','source','user','active',"
            "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
    replicator = _replicator(forward_run)
    assert _run(replicator, run_id).checkpoint_after == 1
    with target.write() as conn:
        conn.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at) "
            "VALUES('target-only','drift@example.test','target','user','active',"
            "clock_timestamp(),clock_timestamp())"
        )
    with source.write() as conn:
        conn.execute(
            "UPDATE users SET email='drift@example.test' WHERE id='source-user'"
        )
    with pytest.raises(PoisonEventRecorded) as error:
        _run(replicator, run_id)
    assert (error.value.source_seq, error.value.category) == (2, "constraint")
    assert _checkpoint(target, run_id) == 1


def test_run_forever_widens_past_event_257_and_converges_unique_swap(
    forward_run, monkeypatch
):
    source, target, run_id = forward_run
    with source.write() as conn:
        for user_id, email in (
            ("wide-a", "wide-a@example.test"),
            ("wide-b", "wide-b@example.test"),
        ):
            conn.execute(
                "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at) "
                "VALUES(?,?,?,'user','active',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                (user_id, email, user_id),
            )
    replicator = _replicator(forward_run)
    assert _run(replicator, run_id).checkpoint_after == 2
    with source.write() as conn:
        conn.execute(
            "UPDATE users SET email='wide-temporary@example.test' WHERE id='wide-a'"
        )
        for index in range(256):
            conn.execute(
                "INSERT INTO app_settings(key,value,updated_at) "
                "VALUES(?, 'x', CURRENT_TIMESTAMP)",
                (f"wide-filler-{index}",),
            )
        conn.execute("UPDATE users SET email='wide-a@example.test' WHERE id='wide-b'")
        conn.execute("UPDATE users SET email='wide-b@example.test' WHERE id='wide-a'")

    stop = threading.Event()
    calls: list[int] = []
    original = replicator.run_batch

    def observe(**kwargs):
        calls.append(kwargs["max_events"])
        result = original(**kwargs)
        if result.events_read:
            stop.set()
        return result

    monkeypatch.setattr(replicator, "run_batch", observe)
    replicator.run_forever(
        run_id=run_id,
        direction=Direction.SQLITE_TO_POSTGRES,
        stop=stop,
    )
    assert calls == [256, 512]
    assert _checkpoint(target, run_id) == 261
    with target.connect() as conn:
        rows = conn.execute(
            "SELECT id,email FROM users WHERE id IN ('wide-a','wide-b') ORDER BY id"
        ).fetchall()
    assert [(row["id"], row["email"]) for row in rows] == [
        ("wide-a", "wide-b@example.test"),
        ("wide-b", "wide-a@example.test"),
    ]


def test_fk_dependency_bytes_count_toward_batch_limit(forward_run):
    source, _target, run_id = forward_run
    with source.write() as conn:
        conn.execute(
            "INSERT INTO notebooks(id,name,purpose,created_at,updated_at) "
            "VALUES('byte-parent','parent',?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
            ("x" * 20_000,),
        )
        conn.execute(
            "INSERT INTO answers(id,notebook_id,question,payload,created_at) "
            "VALUES('byte-child','byte-parent','q','{}',CURRENT_TIMESTAMP)"
        )
        first = conn.execute(
            "SELECT table_name,key_json,operation FROM shadow_change_log WHERE seq=1"
        ).fetchone()
        second = conn.execute(
            "SELECT table_name,key_json,operation FROM shadow_change_log WHERE seq=2"
        ).fetchone()
        conn.execute(
            "UPDATE shadow_change_log SET table_name=?,key_json=?,operation=? WHERE seq=1",
            tuple(second),
        )
        conn.execute(
            "UPDATE shadow_change_log SET table_name=?,key_json=?,operation=? WHERE seq=2",
            tuple(first),
        )
    result = _run(_replicator(forward_run), run_id, max_events=1, max_bytes=1_000)
    assert result.oversized_event
    assert result.hydrated_bytes > 20_000
    assert result.rows_applied == 2


def test_fk_dependency_closure_has_a_hard_row_cap(forward_run, monkeypatch):
    source, target, run_id = forward_run
    replicator = _replicator(forward_run)
    with source.write() as conn:
        conn.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "VALUES('cap-parent','parent',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO answers(id,notebook_id,question,payload,created_at) "
            "VALUES('cap-child','cap-parent','q','{}',CURRENT_TIMESTAMP)"
        )
        first = conn.execute(
            "SELECT table_name,key_json,operation FROM shadow_change_log WHERE seq=1"
        ).fetchone()
        second = conn.execute(
            "SELECT table_name,key_json,operation FROM shadow_change_log WHERE seq=2"
        ).fetchone()
        conn.execute(
            "UPDATE shadow_change_log SET table_name=?,key_json=?,operation=? WHERE seq=1",
            tuple(second),
        )
        conn.execute(
            "UPDATE shadow_change_log SET table_name=?,key_json=?,operation=? WHERE seq=2",
            tuple(first),
        )
    monkeypatch.setattr(
        "app.migration.shadow.replicator._MAX_DEPENDENCY_ROWS_PER_EVENT", 1
    )
    with pytest.raises(CapacityExceeded):
        _run(replicator, run_id, max_events=1)
    assert _checkpoint(target, run_id) == 0
    with target.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS count FROM silicon_shadow.poison_events WHERE run_id=%s",
                (run_id,),
            ).fetchone()["count"]
            == 0
        )


def test_target_statement_budget_exhaustion_is_non_poison(forward_run, monkeypatch):
    source, target, run_id = forward_run
    with source.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('statement-budget','x',CURRENT_TIMESTAMP)"
        )
    replicator = _replicator(forward_run)
    monkeypatch.setattr("app.migration.shadow.replicator._MAX_TARGET_STATEMENTS", 0)
    with pytest.raises(OrderingBlocked, match="statement safety budget"):
        _run(replicator, run_id)
    assert _checkpoint(target, run_id) == 0
    with target.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS count FROM silicon_shadow.poison_events WHERE run_id=%s",
                (run_id,),
            ).fetchone()["count"]
            == 0
        )


def test_oversized_integer_key_poison_is_bound_to_second_event(forward_run):
    source, target, run_id = forward_run
    with source.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('prefix','x',CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO shadow_change_log"
            "(seq,run_id,table_name,key_json,operation,schema_epoch) "
            "VALUES(2,?,'ask_trace_steps',?,'upsert',?)",
            (
                run_id,
                json.dumps({"job_id": "missing", "seq": 2**80}),
                MANIFEST.schema_pair.epoch,
            ),
        )
    with pytest.raises(PoisonEventRecorded) as error:
        _run(_replicator(forward_run), run_id)
    assert (error.value.source_seq, error.value.category) == (2, "conversion")
    assert _checkpoint(target, run_id) == 0
    assert _target_setting(target, "prefix") is None


def test_live_sqlite_control_mismatch_is_identity_poison(forward_run):
    source, target, run_id = forward_run
    with source.write() as conn:
        conn.execute(
            "UPDATE shadow_capture_control SET run_id='other' WHERE singleton=1"
        )
    with pytest.raises(PoisonEventRecorded) as error:
        _run(_replicator(forward_run), run_id)
    assert (error.value.source_seq, error.value.category) == (1, "identity")
    assert _checkpoint(target, run_id) == 0


def test_target_snapshot_rejects_progress_checkpoint_mismatch(forward_run):
    _source, target, run_id = forward_run
    with target.write() as conn:
        conn.execute(
            "UPDATE silicon_shadow.runs SET progress=%s::jsonb WHERE run_id=%s",
            (
                json.dumps({"stage": "forward", "baseline_seq": 0, "applied_seq": 1}),
                run_id,
            ),
        )

    with pytest.raises(PoisonEventRecorded) as error:
        _run(_replicator(forward_run), run_id)

    assert (error.value.source_seq, error.value.category) == (1, "schema")
    assert _checkpoint(target, run_id) == 0


def test_target_apply_rechecks_progress_checkpoint_before_business_write(
    forward_run, monkeypatch
):
    source, target, run_id = forward_run
    with source.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('progress-mismatch','x',CURRENT_TIMESTAMP)"
        )
    replicator = _replicator(forward_run)
    read_source = replicator._read_source

    def corrupt_after_read(*args, **kwargs):
        result = read_source(*args, **kwargs)
        with target.write() as conn:
            conn.execute(
                "UPDATE silicon_shadow.runs SET progress=%s::jsonb WHERE run_id=%s",
                (
                    json.dumps(
                        {"stage": "forward", "baseline_seq": 0, "applied_seq": 1}
                    ),
                    run_id,
                ),
            )
        return result

    monkeypatch.setattr(replicator, "_read_source", corrupt_after_read)
    with pytest.raises(PoisonEventRecorded) as error:
        _run(replicator, run_id)

    assert (error.value.source_seq, error.value.category) == (1, "schema")
    assert _checkpoint(target, run_id) == 0
    assert _target_setting(target, "progress-mismatch") is None


def test_live_sqlite_identity_is_checked_before_schema_or_capture(
    forward_run, monkeypatch
):
    _source, target, run_id = forward_run
    from app.migration.shadow import replicator as replicator_module

    monkeypatch.setattr(
        replicator_module,
        "fingerprint_sqlite",
        lambda *_args: SimpleNamespace(identity_hash="different"),
    )
    monkeypatch.setattr(
        replicator_module,
        "validate_sqlite_schema",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("schema validation must not run")
        ),
    )
    with pytest.raises(PoisonEventRecorded) as error:
        _run(_replicator(forward_run), run_id)
    assert (error.value.source_seq, error.value.category) == (1, "identity")
    assert _checkpoint(target, run_id) == 0


def test_live_sqlite_path_or_inode_change_during_source_read_is_identity_poison(
    forward_run, monkeypatch
):
    source, target, run_id = forward_run
    from app.migration.shadow import replicator as replicator_module

    with source.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('source-binding-change','x',CURRENT_TIMESTAMP)"
        )
    monkeypatch.setattr(
        replicator_module,
        "assert_live_sqlite_path",
        lambda *_args: (_ for _ in ()).throw(ValueError("opaque binding failure")),
    )

    with pytest.raises(PoisonEventRecorded) as error:
        _run(_replicator(forward_run), run_id)

    assert (error.value.source_seq, error.value.category) == (1, "identity")
    assert _checkpoint(target, run_id) == 0
    assert _target_setting(target, "source-binding-change") is None


def test_live_sqlite_disappearance_at_open_is_identity_poison_marker(
    forward_run, monkeypatch
):
    _source, target, run_id = forward_run
    from app.migration.shadow import replicator as replicator_module

    monkeypatch.setattr(
        replicator_module,
        "open_fresh_live_sqlite",
        lambda *_args: (_ for _ in ()).throw(
            sqlite3.OperationalError("unable to open database file")
        ),
    )

    with pytest.raises(PoisonEventRecorded) as error:
        _run(_replicator(forward_run), run_id)

    assert (error.value.source_seq, error.value.category) == (1, "identity")
    assert _checkpoint(target, run_id) == 0


@pytest.mark.parametrize("failure", ["schema", "capture"])
def test_live_sqlite_schema_and_capture_failures_are_typed_schema_poison(
    forward_run, monkeypatch, failure: str
):
    _source, target, run_id = forward_run
    from app.migration.shadow import replicator as replicator_module

    monkeypatch.setattr(
        replicator_module,
        "validate_sqlite_schema" if failure == "schema" else "validate_sqlite_capture",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("opaque")),
    )
    with pytest.raises(PoisonEventRecorded) as error:
        _run(_replicator(forward_run), run_id)
    assert (error.value.source_seq, error.value.category) == (1, "schema")
    assert _checkpoint(target, run_id) == 0


def test_constraint_poison_uses_actual_second_event_and_rolls_back_prefix(forward_run):
    source, target, run_id = forward_run
    # Deliberately construct corrupt source evidence with no current FK parent.
    # The verified source snapshot therefore has no parent to hydrate.
    conn = source.connect()
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "INSERT INTO app_settings(key,value,updated_at) "
        "VALUES('prefix','x',CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "INSERT INTO answers(id,notebook_id,question,payload,created_at) "
        "VALUES('orphan','missing-parent','q','{}',CURRENT_TIMESTAMP)"
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(PoisonEventRecorded) as error:
        _run(_replicator(forward_run), run_id)
    assert (error.value.source_seq, error.value.category) == (2, "constraint")
    assert _checkpoint(target, run_id) == 0
    assert _target_setting(target, "prefix") is None


@pytest.mark.parametrize(
    "violation",
    [psycopg.errors.CheckViolation("check"), psycopg.errors.NotNullViolation("null")],
)
def test_permanent_integrity_violation_is_immediate_poison(
    forward_run, monkeypatch, violation
):
    source, target, run_id = forward_run
    with source.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('permanent-constraint','x',CURRENT_TIMESTAMP)"
        )
    replicator = _replicator(forward_run)
    monkeypatch.setattr(
        replicator,
        "_apply_statement",
        lambda *_args: (_ for _ in ()).throw(violation),
    )
    with pytest.raises(PoisonEventRecorded) as error:
        _run(replicator, run_id)
    assert (error.value.source_seq, error.value.category) == (1, "constraint")
    assert _checkpoint(target, run_id) == 0
    with target.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS count FROM silicon_shadow.poison_events WHERE run_id=%s",
                (run_id,),
            ).fetchone()["count"]
            == 1
        )


@pytest.mark.parametrize(
    ("failure", "category"),
    [
        ("catalog", "schema"),
        ("programming", "schema"),
        ("identity", "identity"),
    ],
)
def test_trusted_run_target_validation_failure_records_next_seq_poison(
    forward_run, monkeypatch, failure: str, category: str
):
    _source, target, run_id = forward_run
    replicator = _replicator(forward_run)
    if failure in {"catalog", "programming"}:
        error = (
            ValueError("catalog drift")
            if failure == "catalog"
            else psycopg.ProgrammingError("undefined table")
        )
        monkeypatch.setattr(
            replicator,
            "_validate_target_catalog",
            lambda *_args: (_ for _ in ()).throw(error),
        )
    else:
        monkeypatch.setattr(
            replicator,
            "_validate_bound_target_state",
            lambda *_args: (_ for _ in ()).throw(ValueError("identity drift")),
        )
    with pytest.raises(PoisonEventRecorded) as caught:
        _run(replicator, run_id)
    assert (caught.value.source_seq, caught.value.category) == (1, category)
    assert _checkpoint(target, run_id) == 0
