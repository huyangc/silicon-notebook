from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.core.config import Settings
from app.migration.shadow import capture
from app.migration.shadow.capture import (
    disable_sqlite_capture,
    install_sqlite_capture,
    validate_sqlite_capture,
)
from app.migration.shadow.manifest import MANIFEST
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SCHEMA_VERSION, SqliteMigrator


REPO_ROOT = Path(__file__).resolve().parents[3]
RUN_ID = "shadow-run-1"


@pytest.fixture
def database(tmp_path: Path):
    path = tmp_path / "capture.db"
    settings = Settings(database_url=f"sqlite:///{path}", _env_file=None)
    database = SqliteDatabase(settings, REPO_ROOT)
    SqliteMigrator(database, settings).migrate()
    try:
        with database.connect() as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            yield conn
    finally:
        database.close_local()


def _enable(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE shadow_capture_control SET enabled=1 WHERE singleton=1 AND run_id=?",
        (RUN_ID,),
    )
    conn.commit()


def _events(conn: sqlite3.Connection) -> list[tuple]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT table_name,key_json,operation,run_id,schema_epoch "
            "FROM shadow_change_log ORDER BY seq"
        )
    ]


def test_v31_adds_only_inert_shadow_internal_tables(database):
    tables = {
        row[0]
        for row in database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'shadow_%'"
        )
    }
    assert tables == {"shadow_change_log", "shadow_capture_control"}
    assert (
        database.execute("SELECT COUNT(*) FROM shadow_capture_control").fetchone()[0]
        == 0
    )
    assert (
        database.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'shadow_%'"
        ).fetchone()[0]
        == 0
    )


def test_capture_is_transactional_key_only_and_orders_key_updates(database):
    install_sqlite_capture(database, run_id=RUN_ID, schema_epoch=1, manifest=MANIFEST)
    assert tuple(
        database.execute(
            "SELECT enabled,write_frozen,apply_active "
            "FROM shadow_capture_control WHERE singleton=1"
        ).fetchone()
    ) == (0, 0, 0)
    _enable(database)

    database.execute(
        "INSERT INTO app_settings(key,value,updated_at) VALUES ('old','secret','t0')"
    )
    database.execute("UPDATE app_settings SET value='new-secret' WHERE key='old'")
    database.execute("UPDATE app_settings SET key='new' WHERE key='old'")
    database.execute("DELETE FROM app_settings WHERE key='new'")
    database.commit()

    events = _events(database)
    assert [(row[0], json.loads(row[1]), row[2]) for row in events] == [
        ("app_settings", {"key": "old"}, "upsert"),
        ("app_settings", {"key": "old"}, "upsert"),
        ("app_settings", {"key": "old"}, "delete"),
        ("app_settings", {"key": "new"}, "upsert"),
        ("app_settings", {"key": "new"}, "delete"),
    ]
    assert all(row[3:] == (RUN_ID, 1) for row in events)
    raw_log = "\n".join(str(row) for row in events)
    assert "secret" not in raw_log


def test_business_rollback_also_rolls_back_capture(database):
    install_sqlite_capture(database, run_id=RUN_ID, schema_epoch=1, manifest=MANIFEST)
    _enable(database)
    database.execute("BEGIN")
    database.execute(
        "INSERT INTO app_settings(key,value,updated_at) VALUES ('rolled','x','t')"
    )
    database.rollback()
    assert (
        database.execute("SELECT 1 FROM app_settings WHERE key='rolled'").fetchone()
        is None
    )
    assert _events(database) == []


def test_disabled_capture_logs_nothing_and_disable_checks_identity(database):
    install_sqlite_capture(database, run_id=RUN_ID, schema_epoch=1, manifest=MANIFEST)
    database.execute(
        "INSERT INTO app_settings(key,value,updated_at) VALUES ('off','x','t')"
    )
    database.commit()
    assert _events(database) == []
    with pytest.raises(ValueError, match="identity"):
        disable_sqlite_capture(database, run_id="another-run")
    disable_sqlite_capture(database, run_id=RUN_ID)


def test_frozen_writes_fail_closed_but_in_transaction_lease_is_allowed(database):
    install_sqlite_capture(database, run_id=RUN_ID, schema_epoch=1, manifest=MANIFEST)
    database.execute(
        "UPDATE shadow_capture_control SET enabled=1,write_frozen=1 WHERE singleton=1"
    )
    database.commit()
    with pytest.raises(sqlite3.IntegrityError, match="shadow writes are frozen"):
        database.execute(
            "INSERT INTO app_settings(key,value,updated_at) VALUES ('blocked','x','t')"
        )
    database.rollback()

    database.execute("BEGIN IMMEDIATE")
    database.execute(
        "UPDATE shadow_capture_control SET apply_active=1 WHERE singleton=1"
    )
    database.execute(
        "INSERT INTO app_settings(key,value,updated_at) VALUES ('leased','x','t')"
    )
    database.execute(
        "UPDATE shadow_capture_control SET apply_active=0 WHERE singleton=1"
    )
    database.commit()
    assert database.execute("SELECT 1 FROM app_settings WHERE key='leased'").fetchone()


def test_task1_nullable_key_declaration_is_a_runtime_insert_and_update_guard(database):
    install_sqlite_capture(database, run_id=RUN_ID, schema_epoch=1, manifest=MANIFEST)
    with pytest.raises(sqlite3.IntegrityError, match="must not be NULL"):
        database.execute(
            "INSERT INTO app_settings(key,value,updated_at) VALUES (NULL,'x','t')"
        )
    database.rollback()
    database.execute(
        "INSERT INTO app_settings(key,value,updated_at) VALUES ('key','x','t')"
    )
    database.commit()
    with pytest.raises(sqlite3.IntegrityError, match="must not be NULL"):
        database.execute("UPDATE app_settings SET key=NULL WHERE key='key'")
    database.rollback()


def test_embedding_capture_never_logs_blob_payload(database):
    install_sqlite_capture(database, run_id=RUN_ID, schema_epoch=1, manifest=MANIFEST)
    _enable(database)
    payload = b"unique-binary-payload"
    database.execute("PRAGMA foreign_keys=OFF")
    database.execute(
        "INSERT INTO chunk_embeddings(chunk_id,notebook_id,vector,created_at) "
        "VALUES ('chunk-1','nb-1',?,'t')",
        (payload,),
    )
    database.commit()
    row = database.execute(
        "SELECT key_json FROM shadow_change_log WHERE table_name='chunk_embeddings'"
    ).fetchone()
    assert json.loads(row[0]) == {"chunk_id": "chunk-1"}
    assert "unique-binary-payload" not in row[0]
    assert {
        column[1] for column in database.execute("PRAGMA table_info(shadow_change_log)")
    } == {
        "seq",
        "run_id",
        "table_name",
        "key_json",
        "operation",
        "schema_epoch",
        "captured_at",
    }


def _replicated_cascade_edges(database) -> set[tuple[str, str]]:
    replicated = set(MANIFEST.replicated_names)
    return {
        (str(foreign_key[2]), child)
        for child in replicated
        for foreign_key in database.execute(f'PRAGMA foreign_key_list("{child}")')
        if str(foreign_key[2]) in replicated
        and str(foreign_key[6]).upper() == "CASCADE"
    }


def test_schema_derived_fk_cascades_have_exact_child_before_parent_events(database):
    install_sqlite_capture(database, run_id=RUN_ID, schema_epoch=1, manifest=MANIFEST)
    _enable(database)
    database.execute("PRAGMA foreign_keys=ON")
    cascade_edges = _replicated_cascade_edges(database)
    assert {
        ("users", "auth_sessions"),
        ("notebooks", "sources"),
        (
            "sources",
            "source_elements",
        ),
    } <= cascade_edges

    database.execute(
        "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
        "username,password_hash,password_salt,password_iterations) "
        "VALUES ('cascade-user','cascade@example.test','x','user','active','t','t',"
        "'z00123456','h','s',1)"
    )
    database.execute(
        "INSERT INTO auth_sessions(token,user_id,created_at,expires_at,last_seen_at) "
        "VALUES ('cascade-token','cascade-user','t','t','t')"
    )
    database.commit()
    database.execute("DELETE FROM shadow_change_log")
    database.commit()
    database.execute("DELETE FROM users WHERE id='cascade-user'")
    database.commit()
    assert [(row[0], json.loads(row[1]), row[2]) for row in _events(database)] == [
        ("auth_sessions", {"token": "cascade-token"}, "delete"),
        ("users", {"id": "cascade-user"}, "delete"),
    ]

    database.execute(
        "INSERT INTO notebooks(id,name,created_at,updated_at) "
        "VALUES ('cascade-nb','cascade','t','t')"
    )
    database.execute(
        "INSERT INTO sources"
        "(id,notebook_id,title,source_type,file_name,created_at,updated_at) "
        "VALUES ('cascade-source','cascade-nb','source','md','source.md','t','t')"
    )
    database.execute(
        "INSERT INTO source_elements"
        "(id,source_id,element_type,location_label,text,created_at) "
        "VALUES ('cascade-element','cascade-source','text','body','content','t')"
    )
    database.commit()
    database.execute("DELETE FROM shadow_change_log")
    database.commit()
    database.execute("DELETE FROM notebooks WHERE id='cascade-nb'")
    database.commit()
    assert [(row[0], json.loads(row[1]), row[2]) for row in _events(database)] == [
        ("source_elements", {"id": "cascade-element"}, "delete"),
        ("sources", {"id": "cascade-source"}, "delete"),
        ("notebooks", {"id": "cascade-nb"}, "delete"),
    ]


@pytest.mark.parametrize(
    ("sql", "rows"),
    [
        (
            "INSERT INTO community_members"
            "(canonical_id,notebook_id,level,community_id,canonical_name,centrality) "
            "VALUES ('c','nb',0,?,'',0)",
            [("one",), ("two",)],
        ),
        (
            "INSERT INTO kg_canonical_scratch"
            "(notebook_id,run_id,seed,canonical_id) VALUES ('nb','run','seed',?)",
            [("one",), ("two",)],
        ),
        (
            "INSERT INTO kg_cluster_scratch(notebook_id,run_id,object_id,seed) "
            "VALUES ('nb','run','object',?)",
            [("one",), ("two",)],
        ),
        (
            "INSERT INTO knowledge_object_sources(object_id,source_id,notebook_id) "
            "VALUES ('object','source',?)",
            [("one",), ("two",)],
        ),
    ],
)
def test_duplicate_guard_failure_rolls_back_control_indexes_and_triggers(
    database, sql, rows
):
    database.executemany(sql, rows)
    database.commit()
    with pytest.raises(ValueError, match="duplicate replication key"):
        install_sqlite_capture(
            database, run_id=RUN_ID, schema_epoch=1, manifest=MANIFEST
        )
    assert (
        database.execute("SELECT COUNT(*) FROM shadow_capture_control").fetchone()[0]
        == 0
    )
    assert (
        database.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'shadow_uq_%'"
        ).fetchone()[0]
        == 0
    )
    assert (
        database.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'shadow_%'"
        ).fetchone()[0]
        == 0
    )


def test_install_failure_after_guard_creation_is_atomic_and_retryable(
    database, monkeypatch
):
    original = capture._execute_install_statement
    calls = 0

    def fail_after_first(conn, sql):
        nonlocal calls
        calls += 1
        original(conn, sql)
        if calls == 2:
            raise RuntimeError("injected install failure")

    monkeypatch.setattr(capture, "_execute_install_statement", fail_after_first)
    with pytest.raises(RuntimeError, match="injected"):
        install_sqlite_capture(
            database, run_id=RUN_ID, schema_epoch=1, manifest=MANIFEST
        )
    assert (
        database.execute("SELECT COUNT(*) FROM shadow_capture_control").fetchone()[0]
        == 0
    )
    assert (
        database.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'shadow_uq_%'"
        ).fetchone()[0]
        == 0
    )
    monkeypatch.setattr(capture, "_execute_install_statement", original)
    install_sqlite_capture(database, run_id=RUN_ID, schema_epoch=1, manifest=MANIFEST)
    install_sqlite_capture(database, run_id=RUN_ID, schema_epoch=1, manifest=MANIFEST)
    validate_sqlite_capture(database, MANIFEST)


@pytest.mark.parametrize(
    ("enabled", "write_frozen"),
    [(1, 0), (0, 1), (1, 1)],
)
def test_same_run_crash_resume_preserves_enabled_and_frozen_state(
    database, enabled, write_frozen
):
    install_sqlite_capture(database, run_id=RUN_ID, schema_epoch=1, manifest=MANIFEST)
    database.execute(
        "UPDATE shadow_capture_control SET enabled=?,write_frozen=? WHERE singleton=1",
        (enabled, write_frozen),
    )
    repaired_trigger = "shadow_capture_e1_app_settings_insert"
    database.execute(f'DROP TRIGGER "{repaired_trigger}"')
    database.commit()
    before = tuple(database.execute("SELECT * FROM shadow_capture_control").fetchone())

    install_sqlite_capture(database, run_id=RUN_ID, schema_epoch=1, manifest=MANIFEST)
    validate_sqlite_capture(database, MANIFEST)

    after = tuple(database.execute("SELECT * FROM shadow_capture_control").fetchone())
    assert after == before
    assert database.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
        (repaired_trigger,),
    ).fetchone()


def test_same_run_crash_resume_fails_closed_on_persisted_apply_lease(database):
    install_sqlite_capture(database, run_id=RUN_ID, schema_epoch=1, manifest=MANIFEST)
    database.execute(
        "UPDATE shadow_capture_control "
        "SET enabled=1,write_frozen=1,apply_active=1 WHERE singleton=1"
    )
    database.commit()
    before = tuple(database.execute("SELECT * FROM shadow_capture_control").fetchone())

    with pytest.raises(ValueError, match="persisted apply-active lease"):
        install_sqlite_capture(
            database, run_id=RUN_ID, schema_epoch=1, manifest=MANIFEST
        )

    after = tuple(database.execute("SELECT * FROM shadow_capture_control").fetchone())
    assert after == before


def test_reinstall_rejects_a_different_run_without_mutating_control(database):
    install_sqlite_capture(database, run_id=RUN_ID, schema_epoch=1, manifest=MANIFEST)
    database.execute(
        "UPDATE shadow_capture_control SET enabled=1,write_frozen=1 WHERE singleton=1"
    )
    database.commit()
    before = tuple(database.execute("SELECT * FROM shadow_capture_control").fetchone())

    with pytest.raises(ValueError, match="different run identity"):
        install_sqlite_capture(
            database, run_id="shadow-run-2", schema_epoch=1, manifest=MANIFEST
        )

    after = tuple(database.execute("SELECT * FROM shadow_capture_control").fetchone())
    assert after == before


@pytest.mark.parametrize("damage", ["missing", "modified", "stale"])
def test_structural_validator_rejects_trigger_drift(database, damage):
    install_sqlite_capture(database, run_id=RUN_ID, schema_epoch=1, manifest=MANIFEST)
    name = "shadow_capture_e1_app_settings_insert"
    database.execute(f'DROP TRIGGER "{name}"')
    if damage == "modified":
        database.execute(
            f'CREATE TRIGGER "{name}" AFTER INSERT ON app_settings BEGIN SELECT 1; END'
        )
    elif damage == "stale":
        database.execute(
            "CREATE TRIGGER shadow_capture_e0_app_settings_insert "
            "AFTER INSERT ON app_settings BEGIN SELECT 1; END"
        )
    database.commit()
    with pytest.raises(ValueError, match="trigger"):
        validate_sqlite_capture(database, MANIFEST)


def test_structural_validator_preserves_capture_table_literal_case(database):
    install_sqlite_capture(database, run_id=RUN_ID, schema_epoch=1, manifest=MANIFEST)
    name = "shadow_capture_e1_app_settings_insert"
    stored_sql = database.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
    ).fetchone()[0]
    assert "'app_settings'" in stored_sql
    database.execute(f'DROP TRIGGER "{name}"')
    database.execute(stored_sql.replace("'app_settings'", "'APP_SETTINGS'"))
    database.commit()
    with pytest.raises(ValueError, match="trigger definition mismatch"):
        validate_sqlite_capture(database, MANIFEST)


def test_structural_validator_rejects_an_unknown_shadow_guard(database):
    install_sqlite_capture(database, run_id=RUN_ID, schema_epoch=1, manifest=MANIFEST)
    database.execute(
        "CREATE UNIQUE INDEX shadow_uq_unknown_replication_key ON app_settings(value)"
    )
    database.commit()
    with pytest.raises(ValueError, match="guard set mismatch.*extra"):
        validate_sqlite_capture(database, MANIFEST)
