# backend/tests/test_sqlite_migrations.py
"""SQLite migration-ledger contracts that don't belong to any single
`_migration_N` step: the upper-bound guard on `SqliteMigrator.migrate()`
(A7) and the equality short-circuit it must not break.

PostgreSQL already fails closed on a future ledger version
(`PostgresMigrator._validate_ledger`, see `tests/postgres/test_migrations.py::
test_unknown_future_ledger_version_fails_closed`). SQLite's `migrate()` used to
tolerate `current > SCHEMA_VERSION` silently (`if current >= SCHEMA_VERSION:
return []`), which meant a database written by a newer build, opened by an
older/rolled-back build, would silently run against a schema it doesn't
understand instead of failing fast at startup. This module pins the SQLite
side of that guard.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.core.config import Settings
from app.repositories.sqlite import migrations as migrations_module
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SCHEMA_VERSION, SqliteMigrator


def _fresh_migrated_database(tmp_path: Path) -> tuple[SqliteDatabase, Settings]:
    db_path = tmp_path / "migrations.db"
    settings = Settings(database_url=f"sqlite:///{db_path}")
    database = SqliteDatabase(settings, root_dir=tmp_path)
    SqliteMigrator(database, settings).migrate()
    return database, settings


def _set_user_version(database: SqliteDatabase, version: int) -> None:
    with database.connect() as db:
        db.execute(f"PRAGMA user_version = {int(version)}")


@pytest.mark.real_sqlite_migration
def test_migrate_returns_empty_list_when_database_is_exactly_current(tmp_path):
    """Equality must keep short-circuiting: no migrations re-run, no raise."""
    database, settings = _fresh_migrated_database(tmp_path)
    try:
        with database.connect() as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert SqliteMigrator(database, settings).migrate() == []
    finally:
        database.close_local()


@pytest.mark.real_sqlite_migration
def test_migrate_raises_when_database_is_stamped_newer_than_schema_version(
    tmp_path,
):
    """A7: a database from a newer build must be refused at startup, not
    silently tolerated. The error names both the database's stamped version
    and the running build's SCHEMA_VERSION so an operator can act on it."""
    database, settings = _fresh_migrated_database(tmp_path)
    try:
        future_version = SCHEMA_VERSION + 1
        _set_user_version(database, future_version)

        with pytest.raises(RuntimeError) as excinfo:
            SqliteMigrator(database, settings).migrate()

        message = str(excinfo.value)
        assert str(future_version) in message
        assert str(SCHEMA_VERSION) in message
    finally:
        database.close_local()


def _v77_database(tmp_path, monkeypatch):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'auth-v77.db'}",
        storage_dir=str(tmp_path / "storage"),
        _env_file=None,
        event_log_enabled=False,
        llm_log_enabled=False,
    )
    database = SqliteDatabase(settings, tmp_path)
    monkeypatch.setattr(migrations_module, "SCHEMA_VERSION", 77)
    assert SqliteMigrator(database, settings).migrate()
    with database.write() as db:
        db.execute(
            "INSERT INTO users "
            "(id,email,display_name,role,status,username,password_hash,"
            "password_salt,password_iterations,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "user-existing",
                "existing@example.invalid",
                "Existing User",
                "user",
                "active",
                "legacy-login",
                "preserved-hash",
                "preserved-salt",
                12345,
                "before",
                "before",
            ),
        )
        db.execute(
            "INSERT INTO auth_sessions "
            "(token,user_id,created_at,expires_at,last_seen_at) VALUES (?,?,?,?,?)",
            ("preserved-session", "user-existing", "before", "later", "before"),
        )
    monkeypatch.setattr(migrations_module, "SCHEMA_VERSION", 78)
    return settings, database


@pytest.mark.legacy_upgrade_contract
@pytest.mark.real_sqlite_migration
def test_v78_recovers_committed_schema_with_v77_stamp_without_data_loss(
    tmp_path, monkeypatch
):
    settings, database = _v77_database(tmp_path, monkeypatch)
    migrator = SqliteMigrator(database, settings)
    assert migrator.migrate() == [78]

    # Reproduce the old failure window exactly: all v78 DDL committed, but the
    # separate version-stamp transaction did not.  The alias represents a
    # value already chosen after backfill and must never be overwritten.
    with database.write() as db:
        db.execute(
            "UPDATE users SET local_login_name='preserved-alias' "
            "WHERE id='user-existing'"
        )
        db.execute("PRAGMA user_version = 77")
    with database.connect() as db:
        user_before = dict(
            db.execute("SELECT * FROM users WHERE id='user-existing'").fetchone()
        )
        session_before = dict(
            db.execute(
                "SELECT * FROM auth_sessions WHERE token='preserved-session'"
            ).fetchone()
        )
    database.close_local()

    reopened = SqliteDatabase(settings, tmp_path)
    assert SqliteMigrator(reopened, settings).migrate() == [78]
    with reopened.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 78
        user_after = dict(
            db.execute("SELECT * FROM users WHERE id='user-existing'").fetchone()
        )
        session_after = dict(
            db.execute(
                "SELECT * FROM auth_sessions WHERE token='preserved-session'"
            ).fetchone()
        )
    assert user_after == user_before
    assert user_after["local_login_name"] == "preserved-alias"
    assert session_after == session_before
    reopened.close_local()


@pytest.mark.legacy_upgrade_contract
@pytest.mark.real_sqlite_migration
def test_v78_schema_and_version_stamp_roll_back_together_on_interruption(
    tmp_path, monkeypatch
):
    settings, database = _v77_database(tmp_path, monkeypatch)
    original_new_connection = database._new_connection
    interrupt = {"armed": True}

    def interrupted_connection(mode="read", operation="sqlite.read"):
        connection = original_new_connection(mode, operation)
        if connection._diag_mode == "write" and interrupt["armed"]:

            def deny_auth_policy(action, name, _column, _database, _source):
                if action == sqlite3.SQLITE_CREATE_TABLE and name == "auth_policy":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            connection.set_authorizer(deny_auth_policy)
        return connection

    monkeypatch.setattr(database, "_new_connection", interrupted_connection)
    migrator = SqliteMigrator(database, settings)
    with pytest.raises(sqlite3.DatabaseError):
        migrator.migrate()

    with database.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 77
        user_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()
        }
        session_columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(auth_sessions)").fetchall()
        }
        assert "local_login_name" not in user_columns
        assert "auth_source" not in session_columns
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='auth_policy'"
        ).fetchone() is None
        password = tuple(
            db.execute(
                "SELECT password_hash,password_salt,password_iterations "
                "FROM users WHERE id='user-existing'"
            ).fetchone()
        )
        session = tuple(
            db.execute(
                "SELECT token,user_id,created_at,expires_at,last_seen_at "
                "FROM auth_sessions WHERE token='preserved-session'"
            ).fetchone()
        )
    assert password == ("preserved-hash", "preserved-salt", 12345)
    assert session == (
        "preserved-session",
        "user-existing",
        "before",
        "later",
        "before",
    )

    interrupt["armed"] = False
    assert migrator.migrate() == [78]
    with database.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 78
        assert db.execute(
            "SELECT local_login_name FROM users WHERE id='user-existing'"
        ).fetchone()[0] == "legacy-login"
    database.close_local()


@pytest.mark.legacy_upgrade_contract
@pytest.mark.real_sqlite_migration
def test_v92_drops_the_staged_auth_schema_and_keeps_credentials(tmp_path):
    """A v91 database (forged from the current schema with rollback_v92) loses
    the staged-auth tables, users.local_login_name and the audit grant
    reference; passwords, sessions and audit rows survive and pending OAuth
    transactions are discarded. A second run is a no-op."""
    from app.domain.auth_utils import hash_password
    from tests.sqlite_migration_testkit import rollback_v92

    database, settings = _fresh_migrated_database(tmp_path)
    password = hash_password("pw")
    with database.write() as db:
        rollback_v92(db)
        db.execute(
            "INSERT INTO users (id,email,display_name,role,status,username,password_hash,"
            "password_salt,password_iterations,created_at,updated_at) "
            "VALUES ('user-existing','e@x','E','user','active','a12345678',?,?,?,'t','t')",
            password,
        )
        db.execute("UPDATE users SET local_login_name='legacy-alias' WHERE id='user-existing'")
        db.execute(
            "INSERT INTO auth_sessions (token,user_id,created_at,expires_at,last_seen_at) "
            "VALUES ('kept-session','user-existing','t','9999','t')"
        )
        db.execute(
            "INSERT INTO auth_identity_audit (id,actor_id,target_user_id,action,"
            "provider_namespace,subject,grant_reference,created_at) "
            "VALUES ('audit-1','user-local','user-existing','account_status:active','','','ref','t')"
        )
        db.execute(
            "INSERT INTO auth_transactions (token_digest,purpose,browser_digest,payload,expires_at) "
            "VALUES ('d','login','b','{}',4102444800)"
        )
        db.execute("PRAGMA user_version = 91")

    assert SqliteMigrator(database, settings).migrate() == [92, 93, 94, 95, 96, 97, 98, 99, 100, 101, 102, 103, 104, 105]
    assert SqliteMigrator(database, settings).migrate() == []
    with database.connect() as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        user_columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
        audit_columns = {row[1] for row in db.execute("PRAGMA table_info(auth_identity_audit)")}
        user = dict(db.execute("SELECT * FROM users WHERE id='user-existing'").fetchone())
        session = db.execute("SELECT user_id FROM auth_sessions WHERE token='kept-session'").fetchone()
        audit = [dict(row) for row in db.execute("SELECT * FROM auth_identity_audit")]
        pending = db.execute("SELECT count(*) FROM auth_transactions").fetchone()[0]
    assert not {"auth_policy", "auth_policy_audit", "external_identities"} & tables
    assert not {"idx_users_local_login_name", "idx_external_identities_active_user"} & indexes
    assert "idx_auth_identity_audit_created" in indexes
    assert "local_login_name" not in user_columns
    assert "grant_reference" not in audit_columns
    assert "idx_users_username_lower" in indexes
    assert user["sso_linked_at"] is None
    assert (user["username"], user["password_hash"], user["password_salt"]) == (
        "a12345678", password[0], password[1],
    )
    assert session[0] == "user-existing"
    assert [row["action"] for row in audit] == ["account_status:active"]
    assert pending == 0
    database.close_local()


@pytest.mark.legacy_upgrade_contract
@pytest.mark.real_sqlite_migration
def test_v92_refuses_usernames_that_differ_only_by_case(tmp_path):
    """idx_users_username_lower keeps the case-insensitive login lookup to one
    row; a database already holding a case clash fails the upgrade with the
    clashing names instead of picking one, and stays at v91."""
    from tests.sqlite_migration_testkit import rollback_v92

    database, settings = _fresh_migrated_database(tmp_path)
    with database.write() as db:
        rollback_v92(db)
        for user_id, name in (("user-a", "a12345678"), ("user-b", "A12345678")):
            db.execute(
                "INSERT INTO users (id,email,display_name,role,status,username,created_at,updated_at) "
                "VALUES (?,?,?,'user','active',?,'t','t')",
                (user_id, user_id + "@x", user_id, name),
            )
        db.execute("PRAGMA user_version = 91")

    with pytest.raises(RuntimeError, match="differ only by letter case.*a12345678"):
        SqliteMigrator(database, settings).migrate()
    with database.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 91
        assert db.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='auth_policy'"
        ).fetchone()[0] == 1
    database.close_local()


@pytest.mark.real_sqlite_migration
def test_v93_adds_the_nullable_profile_deleted_at_without_touching_rows(tmp_path):
    """Agent Profile soft delete is a new nullable column: an existing Profile
    comes through listed (``deleted_at`` NULL) and a second run is a no-op."""
    from tests.sqlite_migration_testkit import rollback_v93

    database, settings = _fresh_migrated_database(tmp_path)
    with database.write() as db:
        db.execute(
            "INSERT INTO users (id,email,display_name,role,status,username,created_at,updated_at) "
            "VALUES ('user-p','p@x','P','user','active','p12345678','t','t')"
        )
        db.execute(
            "INSERT INTO agent_profiles (id,owner_id,name,created_at,updated_at) "
            "VALUES ('agent-p','user-p','Kept','t','t')"
        )
        rollback_v93(db)
        db.execute("PRAGMA user_version = 92")

    assert SqliteMigrator(database, settings).migrate() == [93, 94, 95, 96, 97, 98, 99, 100, 101, 102, 103, 104, 105]
    assert SqliteMigrator(database, settings).migrate() == []
    with database.connect() as db:
        columns = {row[1]: row for row in db.execute("PRAGMA table_info(agent_profiles)")}
        row = db.execute("SELECT name,status,deleted_at FROM agent_profiles").fetchone()
    assert columns["deleted_at"][2] == "TEXT" and columns["deleted_at"][3] == 0
    assert (row["name"], row["status"], row["deleted_at"]) == ("Kept", "active", None)
    database.close_local()


@pytest.mark.legacy_upgrade_contract
@pytest.mark.real_sqlite_migration
def test_v94_adds_answer_links_and_the_token_flag(tmp_path):
    """MCP answer links: a new table with its unique per-(job, issuing token)
    index and expiry index, and agent_access_tokens.allow_answer_links
    defaulting to 0 so an existing token reads as unable to issue links. A
    fresh database and a v93 upgrade end up the same; a second run is a no-op."""
    from tests.sqlite_migration_testkit import rollback_v94

    database, settings = _fresh_migrated_database(tmp_path)

    def shape(db):
        columns = {row[1]: row for row in db.execute("PRAGMA table_info(answer_links)")}
        indexes = {
            row[1]: row[2]
            for row in db.execute("PRAGMA index_list(answer_links)")
            if not row[1].startswith("sqlite_autoindex")
        }
        flag = {row[1]: row for row in db.execute("PRAGMA table_info(agent_access_tokens)")}
        return columns, indexes, flag["allow_answer_links"]

    with database.connect() as db:
        fresh = shape(db)
    columns, indexes, flag = fresh
    assert set(columns) == {
        "token", "job_kind", "job_id", "owner_id", "agent_token_id", "created_at", "expires_at",
    }
    assert indexes == {"uq_answer_links_job_token": 1, "idx_answer_links_expires": 0}
    assert (flag[2], flag[3], flag[4]) == ("INTEGER", 1, "0")

    with database.write() as db:
        rollback_v94(db)
        db.execute("PRAGMA user_version = 93")
        # A token issued before v94 must read back as unable to issue links.
        now = "2026-10-15T00:00:00+00:00"
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("u-v94", "v94@example.test", "u-v94", "user", "active", now, now,
             "t00000094", "", "", 0),
        )
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("nb-v94", "nb-v94", "", "", "ready", "u-v94", now, now),
        )
        db.execute(
            "INSERT INTO agent_profiles(id,owner_id,name,description,status,created_at,"
            "updated_at) VALUES (?,?,?,?,?,?,?)",
            ("agent-v94", "u-v94", "Agent", "", "active", now, now),
        )
        db.execute(
            "INSERT INTO agent_access_tokens(id,agent_profile_id,token_hash,scopes_json,"
            "default_notebook_id,revoked_at,created_at) VALUES (?,?,?,?,?,?,?)",
            ("tk-v94", "agent-v94", "hash-v94", '["read", "ask"]', "nb-v94", None, now),
        )

    assert SqliteMigrator(database, settings).migrate() == [94, 95, 96, 97, 98, 99, 100, 101, 102, 103, 104, 105]
    assert SqliteMigrator(database, settings).migrate() == []
    with database.connect() as db:
        assert shape(db)[1:] == fresh[1:]
        assert set(shape(db)[0]) == set(columns)
        legacy = db.execute(
            "SELECT allow_answer_links FROM agent_access_tokens WHERE id = 'tk-v94'"
        ).fetchone()
        assert legacy[0] == 0
    database.close_local()


@pytest.mark.legacy_upgrade_contract
@pytest.mark.real_sqlite_migration
def test_v95_adds_global_ask_retained_until_without_backfill(tmp_path):
    """Global Ask turn retention: ``global_ask_jobs.retained_until`` is a
    nullable TEXT with no backfill, plus a partial index over the stamped rows.
    A turn that existed before v95 reads back unstamped (its libraries were
    never deleted under the retention rule). A fresh database and a v94 upgrade
    end up the same; a second run is a no-op."""
    from tests.sqlite_migration_testkit import rollback_v95

    database, settings = _fresh_migrated_database(tmp_path)

    def shape(db):
        columns = {row[1]: row for row in db.execute("PRAGMA table_info(global_ask_jobs)")}
        index = db.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'idx_global_ask_jobs_retained'"
        ).fetchone()
        return columns["retained_until"], index[0] if index else None

    with database.connect() as db:
        fresh = shape(db)
    column, index_sql = fresh
    assert (column[2], column[3], column[4]) == ("TEXT", 0, None)
    assert "WHERE retained_until IS NOT NULL" in index_sql

    with database.write() as db:
        rollback_v95(db)
        db.execute("PRAGMA user_version = 94")
        db.execute(
            "INSERT INTO global_ask_conversations(id,user_id,title,scope_json,"
            "submitted_via,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            ("gc-v95", "u-v95", "t", '{"mode":"all","notebook_ids":[]}', "web",
             "2026-10-19T00:00:00+00:00", "2026-10-19T00:00:00+00:00"),
        )
        db.execute(
            "INSERT INTO global_ask_jobs(id,conversation_id,user_id,request_json,status,"
            "payload_json,created_at) VALUES (?,?,?,?,?,?,?)",
            ("gj-v95", "gc-v95", "u-v95", "{}", "done", "{}", "2026-10-19T00:00:00+00:00"),
        )

    assert SqliteMigrator(database, settings).migrate() == [95, 96, 97, 98, 99, 100, 101, 102, 103, 104, 105]
    assert SqliteMigrator(database, settings).migrate() == []
    with database.connect() as db:
        assert shape(db) == fresh
        legacy = db.execute(
            "SELECT retained_until FROM global_ask_jobs WHERE id = 'gj-v95'"
        ).fetchone()
        assert legacy[0] is None
    database.close_local()


@pytest.mark.real_sqlite_migration
def test_v96_adds_the_rated_global_jobs_partial_index(tmp_path):
    """v96 is one partial expression index over the global rating; it indexes
    the rated rows already present when it runs (a v95 database's existing
    ratings are counted right after the upgrade), a fresh database and a v95
    upgrade carry the same definition, and a second run is a no-op."""
    from tests.sqlite_migration_testkit import rollback_v96

    database, settings = _fresh_migrated_database(tmp_path)

    def definition(db):
        row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_global_ask_jobs_feedback'"
        ).fetchone()
        return None if row is None else row[0]

    with database.connect() as db:
        fresh = definition(db)
    assert fresh is not None
    assert "WHERE json_extract(payload_json,'$.feedback') IN ('useful','not_useful')" in fresh

    now = "2026-10-19T00:00:00+00:00"
    with database.write() as db:
        rollback_v96(db)
        db.execute("PRAGMA user_version = 95")
        assert definition(db) is None
        db.execute(
            "INSERT INTO global_ask_conversations(id,user_id,title,scope_json,submitted_via,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("gconv-v96", "u-v96", "", "{}", "web", now, now))
        for job_id, rating in (("g-rated", "useful"), ("g-unrated", "")):
            db.execute(
                "INSERT INTO global_ask_jobs(id,conversation_id,user_id,request_json,status,"
                "payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (job_id, "gconv-v96", "u-v96", "{}", "done", f'{{"feedback":"{rating}"}}', now))

    assert SqliteMigrator(database, settings).migrate() == [96, 97, 98, 99, 100, 101, 102, 103, 104, 105]
    assert SqliteMigrator(database, settings).migrate() == []
    with database.connect() as db:
        assert definition(db) == fresh
        rated = db.execute(
            "SELECT id FROM global_ask_jobs INDEXED BY idx_global_ask_jobs_feedback "
            "WHERE json_extract(payload_json,'$.feedback') IN ('useful','not_useful')"
        ).fetchall()
        assert [row[0] for row in rated] == ["g-rated"]
    database.close_local()


@pytest.mark.real_sqlite_migration
def test_v96_stops_with_the_ids_when_a_payload_is_not_json(tmp_path):
    """A malformed global payload would make the index build fail with an
    opaque error; v96 names the rows and the remedy, leaves the database at
    v95, and upgrades once the row is repaired."""
    from tests.sqlite_migration_testkit import rollback_v96

    database, settings = _fresh_migrated_database(tmp_path)
    now = "2026-10-19T00:00:00+00:00"
    with database.write() as db:
        rollback_v96(db)
        db.execute("PRAGMA user_version = 95")
        db.execute(
            "INSERT INTO global_ask_conversations(id,user_id,title,scope_json,submitted_via,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("gconv-bad", "u-bad", "", "{}", "web", now, now))
        db.execute(
            "INSERT INTO global_ask_jobs(id,conversation_id,user_id,request_json,status,"
            "payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
            ("g-bad", "gconv-bad", "u-bad", "{}", "done", "{not json", now))

    with pytest.raises(RuntimeError) as failure:
        SqliteMigrator(database, settings).migrate()
    message = str(failure.value)
    assert "SQLite v96" in message and "g-bad" in message and "json_valid" in message
    with database.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 95
    with database.write() as db:
        db.execute("UPDATE global_ask_jobs SET payload_json='{}' WHERE id='g-bad'")
    assert SqliteMigrator(database, settings).migrate() == [96, 97, 98, 99, 100, 101, 102, 103, 104, 105]
    database.close_local()


@pytest.mark.legacy_upgrade_contract
@pytest.mark.real_sqlite_migration
def test_v97_adds_the_findings_tables_and_the_code_version_column(tmp_path):
    """KG analysis findings storage: the synced detail table with its three
    capture triggers, the local marks table, and
    unified_kg_state.cluster_code_version defaulting to '' so an existing
    state row reads as "no merge recorded under this code yet". A fresh
    database and a v96 upgrade end up the same; a second run is a no-op."""
    from app.migration.sync.capture import sqlite_trigger_sql
    from tests.sqlite_migration_testkit import rollback_v97

    database, settings = _fresh_migrated_database(tmp_path)

    def shape(db):
        findings = [tuple(row) for row in db.execute("PRAGMA table_info(kg_analysis_findings)")]
        marks = [tuple(row) for row in db.execute("PRAGMA table_info(kg_analysis_marks)")]
        state = {row[1]: tuple(row) for row in db.execute("PRAGMA table_info(unified_kg_state)")}
        triggers = {
            row[0]: row[1]
            for row in db.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='kg_analysis_findings'"
            )
        }
        foreign = {
            table: [tuple(row)[2:] for row in db.execute(f"PRAGMA foreign_key_list({table})")]
            for table in ("kg_analysis_findings", "kg_analysis_marks")
        }
        return findings, marks, state["cluster_code_version"][1:], triggers, foreign

    with database.connect() as db:
        fresh = shape(db)
    findings, marks, code_version, triggers, foreign = fresh
    # PRAGMA table_info: (cid, name, type, notnull, dflt_value, pk)
    assert [(c[1], c[3], c[5]) for c in findings] == [
        ("notebook_id", 1, 1), ("rule_id", 1, 2), ("subject_key", 1, 3),
        ("subject_kind", 1, 0), ("subject_label", 1, 0), ("subject_refs", 1, 0),
        ("severity", 1, 0), ("impact_objects", 1, 0), ("evidence", 1, 0),
        ("first_seen_kg_seq", 1, 0), ("first_seen_cluster_seq", 1, 0),
    ]
    assert [(c[1], c[3], c[5]) for c in marks] == [
        ("notebook_id", 1, 1), ("rule_id", 1, 2), ("subject_key", 1, 3),
        ("kind", 1, 4), ("at_kg_seq", 1, 0), ("actor_user_id", 1, 0),
        ("created_at", 1, 0), ("note", 1, 0),
    ]
    assert code_version == ("cluster_code_version", "TEXT", 1, "''", 0)
    assert triggers == {
        name: sql for name, (_table, sql) in sqlite_trigger_sql(added_at=97).items()
    }
    assert len(triggers) == 3
    for table in foreign:
        assert foreign[table] == [
            ("notebooks", "notebook_id", "id", "NO ACTION", "CASCADE", "NONE")
        ], table
    with database.write() as db:
        db.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at) VALUES "
            "('nb-v97', 'n', '2026-10-19T00:00:00+00:00', '2026-10-19T00:00:00+00:00')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        with database.write() as db:
            db.execute(
                "INSERT INTO kg_analysis_marks (notebook_id, rule_id, subject_key, "
                "kind, at_kg_seq, created_at) VALUES ('nb-v97','F1','src-1','bogus',1,'t')"
            )

    with database.write() as db:
        db.execute("DELETE FROM notebooks WHERE id='nb-v97'")
        db.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at) VALUES "
            "('nb-v96', 'n', '2026-10-19T00:00:00+00:00', '2026-10-19T00:00:00+00:00')"
        )
        db.execute(
            "INSERT INTO unified_kg_state (notebook_id, cluster_input_version, updated_at) "
            "VALUES ('nb-v96', 'v-old', '2026-10-19T00:00:00+00:00')"
        )
        rollback_v97(db)
        db.execute("PRAGMA user_version = 96")

    assert SqliteMigrator(database, settings).migrate() == [97, 98, 99, 100, 101, 102, 103, 104, 105]
    assert SqliteMigrator(database, settings).migrate() == []
    with database.connect() as db:
        upgraded = shape(db)
        assert upgraded[:2] == fresh[:2]
        assert upgraded[3:] == fresh[3:]
        # ALTER TABLE appends the column last; its declaration is the same.
        assert upgraded[2] == code_version
        legacy = db.execute(
            "SELECT cluster_code_version, cluster_input_version FROM unified_kg_state "
            "WHERE notebook_id = 'nb-v96'"
        ).fetchone()
        assert tuple(legacy) == ("", "v-old")
    database.close_local()


@pytest.mark.legacy_upgrade_contract
@pytest.mark.real_sqlite_migration
def test_v102_adds_the_concept_seed_space_columns(tmp_path):
    """concept_merge_candidates.seed_version (INTEGER NOT NULL DEFAULT 1) and
    unified_kg_state.concept_seed_version (INTEGER NOT NULL DEFAULT 2): an
    existing decision row and an existing state row read as minted in seed
    space 1 (the space every merge before this migration was built in), a
    state row created afterwards starts in space 2, the code's. A fresh
    database and a v101 upgrade end up the same; a second run is a no-op and
    never touches a row written since."""
    from tests.sqlite_migration_testkit import rollback_v102

    database, settings = _fresh_migrated_database(tmp_path)

    def shape(db):
        candidates = {row[1]: tuple(row)[1:] for row in db.execute(
            "PRAGMA table_info(concept_merge_candidates)")}
        state = {row[1]: tuple(row)[1:] for row in db.execute(
            "PRAGMA table_info(unified_kg_state)")}
        return candidates["seed_version"], state["concept_seed_version"]

    with database.connect() as db:
        fresh = shape(db)
    assert fresh == (("seed_version", "INTEGER", 1, "1", 0),
                     ("concept_seed_version", "INTEGER", 1, "2", 0))
    with database.write() as db:
        db.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at) VALUES "
            "('nb-v101', 'n', '2026-10-21T00:00:00+00:00', '2026-10-21T00:00:00+00:00')"
        )
        rollback_v102(db)
        db.execute(
            "INSERT INTO unified_kg_state (notebook_id, cluster_input_version, updated_at) "
            "VALUES ('nb-v101', 'v-old', '2026-10-21T00:00:00+00:00')"
        )
        db.execute(
            "INSERT INTO concept_merge_candidates (id, notebook_id, canonical_a, "
            "canonical_b, score, status, created_at, updated_at) VALUES "
            "('mc-old', 'nb-v101', 'K-a b', 'K-c', 0.9, 'rejected', 't', 't')"
        )
        db.execute("PRAGMA user_version = 101")

    assert SqliteMigrator(database, settings).migrate() == [102, 103, 104, 105]
    with database.write() as db:
        db.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at) VALUES "
            "('nb-v102', 'n', '2026-10-21T00:00:00+00:00', '2026-10-21T00:00:00+00:00')"
        )
        db.execute(
            "INSERT INTO unified_kg_state (notebook_id, updated_at) "
            "VALUES ('nb-v102', '2026-10-21T00:00:00+00:00')"
        )
    assert SqliteMigrator(database, settings).migrate() == []
    with database.connect() as db:
        assert shape(db) == fresh
        assert dict(db.execute(
            "SELECT notebook_id, concept_seed_version FROM unified_kg_state "
            "WHERE notebook_id IN ('nb-v101', 'nb-v102')"
        ).fetchall()) == {"nb-v101": 1, "nb-v102": 2}
        assert db.execute(
            "SELECT seed_version FROM concept_merge_candidates WHERE id='mc-old'"
        ).fetchone()[0] == 1
    database.close_local()


@pytest.mark.real_sqlite_migration
def test_v84_installs_only_its_own_capture_set(tmp_path, monkeypatch):
    """The ladder stopped at v84 installs exactly v84's 138 capture triggers,
    and no trigger on a table that only a later migration creates. Mutation
    anchor: render _migration_84 from the current manifest (drop its
    ``at_schema=84``) and this stops with "no such table:
    kg_analysis_findings" -- as does every from-scratch ladder."""
    from app.migration.sync.capture import expected_sqlite_trigger_names

    monkeypatch.setattr(migrations_module, "SCHEMA_VERSION", 84)
    db_path = tmp_path / "v84.db"
    settings = Settings(database_url=f"sqlite:///{db_path}")
    database = SqliteDatabase(settings, root_dir=tmp_path)
    try:
        assert SqliteMigrator(database, settings).migrate()[-1] == 84
        with database.connect() as db:
            installed = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND name LIKE 'sync_capture_%'"
                )
            }
            tables = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
    finally:
        database.close_local()
    assert installed == expected_sqlite_trigger_names(84)
    assert len(installed) == 138
    assert "kg_analysis_findings" not in tables


@pytest.mark.real_sqlite_migration
def test_v98_adds_the_attribution_table_without_backfilling(tmp_path):
    """v98 adds ``global_ask_job_notebooks``, ``global_ask_jobs.attributed`` and
    the two indexes; the done jobs a v97 database already holds stay at
    ``attributed = 0`` with no rows (the offline backfill writes them), a fresh
    database and a v97 upgrade carry the same schema, a second run is a no-op,
    and deleting a job cascades to its rows."""
    from tests.sqlite_migration_testkit import rollback_v98

    database, settings = _fresh_migrated_database(tmp_path)

    def shape(db):
        objects = {
            row[0]: row[1] for row in db.execute(
                "SELECT name, sql FROM sqlite_master WHERE name IN ("
                "'global_ask_job_notebooks','idx_global_ask_job_notebooks_notebook',"
                "'idx_global_ask_jobs_unattributed')"
            )
        }
        columns = {row[1]: tuple(row[2:]) for row in db.execute("PRAGMA table_info(global_ask_jobs)")}
        return objects, columns.get("attributed")

    with database.connect() as db:
        fresh = shape(db)
    objects, column = fresh
    assert set(objects) == {
        "global_ask_job_notebooks", "idx_global_ask_job_notebooks_notebook",
        "idx_global_ask_jobs_unattributed",
    }
    assert column == ("INTEGER", 1, "0", 0)
    assert objects["idx_global_ask_jobs_unattributed"].endswith(
        "WHERE status='done' AND attributed=0"
    )

    now = "2026-10-19T00:00:00+00:00"
    with database.write() as db:
        rollback_v98(db)
        db.execute("PRAGMA user_version = 97")
        assert shape(db) == ({}, None)
        db.execute(
            "INSERT INTO global_ask_conversations(id,user_id,title,scope_json,submitted_via,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("gconv-v98", "u-v98", "", "{}", "web", now, now))
        db.execute(
            "INSERT INTO global_ask_jobs(id,conversation_id,user_id,request_json,status,"
            "payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
            ("g-v98", "gconv-v98", "u-v98", "{}", "done",
             '{"resolved_notebook_ids":["nb-x"],"searched_notebook_ids":["nb-x"]}', now))

    assert SqliteMigrator(database, settings).migrate() == [98, 99, 100, 101, 102, 103, 104, 105]
    assert SqliteMigrator(database, settings).migrate() == []
    with database.connect() as db:
        assert shape(db) == fresh
        assert db.execute("SELECT attributed FROM global_ask_jobs WHERE id='g-v98'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM global_ask_job_notebooks").fetchone()[0] == 0
    with database.write() as db:
        db.execute("INSERT INTO global_ask_job_notebooks(job_id,notebook_id) VALUES('g-v98','nb-x')")
        db.execute("DELETE FROM global_ask_jobs WHERE id='g-v98'")
        assert db.execute("SELECT COUNT(*) FROM global_ask_job_notebooks").fetchone()[0] == 0
    database.close_local()


@pytest.mark.real_sqlite_migration
def test_v99_adds_ask_jobs_report_id(tmp_path):
    """v99 adds ``ask_jobs.report_id`` (TEXT NOT NULL DEFAULT ''): a fresh
    database and a v98 upgrade carry the same column, a turn that already
    exists reads '' (the default is the whole backfill), a row written without
    the column reads '', one written with a report id keeps it, and running
    the step again on an upgraded database neither fails nor changes the
    column (the user_version gate would skip a second ``migrate()``, so the
    step itself is called)."""
    from tests.sqlite_migration_testkit import rollback_v99

    database, settings = _fresh_migrated_database(tmp_path)

    def column(db):
        return {
            row[1]: tuple(row[2:]) for row in db.execute("PRAGMA table_info(ask_jobs)")
        }.get("report_id")

    now = "2026-10-21T00:00:00+00:00"
    with database.connect() as db:
        fresh = column(db)
    # PRAGMA table_info: (cid, name, type, notnull, dflt_value, pk)
    assert fresh == ("TEXT", 1, "''", 0)

    with database.write() as db:
        db.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at) VALUES (?,?,?,?)",
            ("nb-v99", "n", now, now))
        rollback_v99(db)
        db.execute("PRAGMA user_version = 98")
        assert column(db) is None
        db.execute(
            "INSERT INTO ask_jobs (id, notebook_id, question, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            ("job-v98", "nb-v99", "why?", "running", now, now))

    assert SqliteMigrator(database, settings).migrate() == [99, 100, 101, 102, 103, 104, 105]
    assert SqliteMigrator(database, settings).migrate() == []
    # The step re-runs cleanly: add_column_if_missing sees the column and a
    # bare ADD COLUMN would raise "duplicate column name".
    SqliteMigrator(database, settings)._migration_99()
    with database.connect() as db:
        assert column(db) == fresh
        assert db.execute(
            "SELECT report_id FROM ask_jobs WHERE id='job-v98'"
        ).fetchone()[0] == ""
    with database.write() as db:
        db.execute(
            "INSERT INTO ask_jobs (id, notebook_id, question, status, report_id, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            ("job-v99", "nb-v99", "why?", "running", "rep-1", now, now))
        db.execute(
            "INSERT INTO ask_jobs (id, notebook_id, question, status, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?)",
            ("job-plain", "nb-v99", "why?", "running", now, now))
        stored = dict(db.execute("SELECT id, report_id FROM ask_jobs").fetchall())
    assert stored == {"job-v98": "", "job-v99": "rep-1", "job-plain": ""}
    database.close_local()


@pytest.mark.legacy_upgrade_contract
@pytest.mark.real_sqlite_migration
def test_v100_adds_the_notebook_delete_window_without_backfilling(tmp_path):
    """v100 adds the four notebooks deletion-window columns and the partial
    ``idx_notebooks_pending_delete_due``. A notebook (and a deleting one --
    an in-flight delete keeps running) that a v99 database already holds
    comes back outside any window: NULL deadline / requester / instant and
    ``status_before_delete=''``, its status untouched. A fresh database and a
    v99 upgrade carry the same schema; a second run is a no-op."""
    from tests.sqlite_migration_testkit import rollback_v100

    database, settings = _fresh_migrated_database(tmp_path)
    window_columns = (
        "delete_after", "delete_requested_at", "delete_requested_by",
        "status_before_delete",
    )

    def shape(db):
        columns = {row[1]: tuple(row[2:]) for row in db.execute("PRAGMA table_info(notebooks)")}
        index = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='idx_notebooks_pending_delete_due'"
        ).fetchone()
        return {name: columns.get(name) for name in window_columns}, (
            index[0] if index else None
        )

    with database.connect() as db:
        fresh = shape(db)
    columns, index_sql = fresh
    assert columns == {
        "delete_after": ("TEXT", 0, None, 0),
        "delete_requested_at": ("TEXT", 0, None, 0),
        "delete_requested_by": ("TEXT", 0, None, 0),
        "status_before_delete": ("TEXT", 1, "''", 0),
    }
    assert index_sql.endswith("ON notebooks(delete_after) WHERE status='pending_delete'")

    now = "2026-10-22T00:00:00+00:00"
    with database.write() as db:
        rollback_v100(db)
        db.execute("PRAGMA user_version = 99")
        for notebook_id, status in (("nb-v100-ready", "ready"), ("nb-v100-del", "deleting")):
            db.execute(
                "INSERT INTO notebooks (id, name, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (notebook_id, notebook_id, status, now, now),
            )

    assert SqliteMigrator(database, settings).migrate() == [100, 101, 102, 103, 104, 105]
    assert SqliteMigrator(database, settings).migrate() == []
    with database.connect() as db:
        assert shape(db) == fresh
        rows = db.execute(
            "SELECT id, status, delete_after, delete_requested_at, delete_requested_by, "
            "status_before_delete FROM notebooks WHERE id LIKE 'nb-v100-%' ORDER BY id"
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("nb-v100-del", "deleting", None, None, None, ""),
        ("nb-v100-ready", "ready", None, None, None, ""),
    ]
    database.close_local()


@pytest.mark.real_sqlite_migration
def test_v101_adds_the_navigation_marker_columns_without_backfilling(tmp_path):
    """v101 adds ``chunks.navigation`` (INTEGER NOT NULL DEFAULT 0),
    ``sources.navigation_scanned_at`` (nullable TEXT) and
    ``sources.navigation_version`` (INTEGER NOT NULL DEFAULT 0), and no index; a v100 database's
    existing chunks read 0 and its sources stay unscanned (NULL) for the
    post-readiness background pass; a fresh database and a v100 upgrade carry
    the same columns; a second run is a no-op."""
    from tests.sqlite_migration_testkit import rollback_v101

    database, settings = _fresh_migrated_database(tmp_path)

    def shape(db):
        chunks = {row[1]: tuple(row[2:]) for row in db.execute("PRAGMA table_info(chunks)")}
        sources = {row[1]: tuple(row[2:]) for row in db.execute("PRAGMA table_info(sources)")}
        return (chunks.get("navigation"), sources.get("navigation_scanned_at"),
                sources.get("navigation_version"))

    with database.connect() as db:
        fresh = shape(db)
    assert fresh == (
        ("INTEGER", 1, "0", 0), ("TEXT", 0, None, 0), ("INTEGER", 1, "0", 0))

    now = "2026-10-21T00:00:00+00:00"
    with database.write() as db:
        rollback_v101(db)
        db.execute("PRAGMA user_version = 100")
        assert shape(db) == (None, None, None)
    raw = sqlite3.connect(database.db_path)
    try:
        # Rows only, no owners: the migration under test reads no foreign key.
        raw.execute("PRAGMA foreign_keys = OFF")
        with raw:
            raw.execute(
                "INSERT INTO sources(id,notebook_id,title,source_type,file_path,parse_status,"
                "created_at,updated_at,chunked_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ("src-v101", "nb-v101", "t", "md", "f.md", "parsed", now, now, now))
            raw.execute(
                "INSERT INTO chunks(id,notebook_id,source_id,text,created_at) VALUES(?,?,?,?,?)",
                ("ck-v101", "nb-v101", "src-v101", "text", now))
    finally:
        raw.close()

    assert SqliteMigrator(database, settings).migrate() == [101, 102, 103, 104, 105]
    assert SqliteMigrator(database, settings).migrate() == []
    with database.connect() as db:
        assert shape(db) == fresh
        assert db.execute("SELECT navigation FROM chunks WHERE id='ck-v101'").fetchone()[0] == 0
        assert db.execute(
            "SELECT navigation_scanned_at FROM sources WHERE id='src-v101'"
        ).fetchone()[0] is None
        assert db.execute(
            "SELECT navigation_version FROM sources WHERE id='src-v101'"
        ).fetchone()[0] == 0
    database.close_local()


@pytest.mark.legacy_upgrade_contract
@pytest.mark.real_sqlite_migration
def test_v102_adds_the_board_partition_columns_and_backfills_board_keys(tmp_path):
    """Board partition columns (v102, with the concept seed space columns):
    unified_kg_state.community_partition_version / community_partition_mode
    default to 0 / '' -- "built by the algorithm before this one", so the next
    rebuild starts cold -- and communities.board_key is backfilled to each
    row's own id, every generation. A fresh database and a v101 upgrade end up
    the same; a re-run after an interrupted UPDATE finishes the backfill and
    touches nothing else."""
    from tests.sqlite_migration_testkit import rollback_v102

    database, settings = _fresh_migrated_database(tmp_path)

    def shape(db):
        state = {row[1]: tuple(row)[1:] for row in db.execute("PRAGMA table_info(unified_kg_state)")}
        communities = {row[1]: tuple(row)[1:] for row in db.execute("PRAGMA table_info(communities)")}
        return (state["community_partition_version"], state["community_partition_mode"],
                communities["board_key"])

    with database.connect() as db:
        fresh = shape(db)
    # PRAGMA table_info minus cid: (name, type, notnull, dflt_value, pk)
    assert fresh == (
        ("community_partition_version", "INTEGER", 1, "0", 0),
        ("community_partition_mode", "TEXT", 1, "''", 0),
        ("board_key", "TEXT", 1, "''", 0),
    )
    now = "2026-10-21T00:00:00+00:00"
    with database.write() as db:
        db.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at) VALUES "
            "('nb-v101', 'n', ?, ?)", (now, now))
        db.execute(
            "INSERT INTO unified_kg_state (notebook_id, cluster_input_version, updated_at) "
            "VALUES ('nb-v101', 'v-old', ?)", (now,))
        rollback_v102(db)
        for cid, generation in (("cm-a", 1), ("cm-b", 1), ("cm-c", 2)):
            db.execute(
                "INSERT INTO communities (id, notebook_id, level, member_ids, size, created_at, "
                "generation) VALUES (?, 'nb-v101', 0, '[\"K-x\"]', 1, ?, ?)", (cid, now, generation))
        db.execute("PRAGMA user_version = 101")

    assert SqliteMigrator(database, settings).migrate() == [102, 103, 104, 105]
    assert SqliteMigrator(database, settings).migrate() == []
    with database.connect() as db:
        assert shape(db) == fresh
        state = db.execute(
            "SELECT community_partition_version, community_partition_mode FROM unified_kg_state "
            "WHERE notebook_id = 'nb-v101'").fetchone()
        assert tuple(state) == (0, "")
        keys = {row[0]: row[1] for row in db.execute("SELECT id, board_key FROM communities")}
        assert keys == {"cm-a": "cm-a", "cm-b": "cm-b", "cm-c": "cm-c"}

    # An interruption between the ALTERs and the UPDATE leaves defaults behind;
    # re-running the migration finishes the backfill and touches nothing else.
    with database.write() as db:
        db.execute("UPDATE communities SET board_key = '' WHERE id = 'cm-b'")
        db.execute("UPDATE communities SET board_key = 'bk-kept' WHERE id = 'cm-c'")
        db.execute("PRAGMA user_version = 101")
    assert SqliteMigrator(database, settings).migrate() == [102, 103, 104, 105]
    with database.connect() as db:
        keys = {row[0]: row[1] for row in db.execute("SELECT id, board_key FROM communities")}
        assert keys == {"cm-a": "cm-a", "cm-b": "cm-b", "cm-c": "bk-kept"}
    database.close_local()


@pytest.mark.legacy_upgrade_contract
@pytest.mark.real_sqlite_migration
def test_v102_lands_both_halves_and_a_rerun_moves_no_seed_space(tmp_path):
    """``_migration_102`` is the concept seed space (S1) and the board partition
    (S2) in one step: a v101 database gains all five columns at once -- its
    existing state row reads seed space 1 and partition 0 / '', its board
    carries its own id as ``board_key``, its decision reads seed space 1.
    Re-running the step after the board half went missing (an interrupted
    run, the shape the S2 re-run test guards) finishes that half and moves no
    row's seed space: a state row already in space 2 stays there."""
    from tests.sqlite_migration_testkit import rollback_v102

    database, settings = _fresh_migrated_database(tmp_path)
    now = "2026-10-22T00:00:00+00:00"

    def state(db):
        return tuple(db.execute(
            "SELECT concept_seed_version, community_partition_version, "
            "community_partition_mode FROM unified_kg_state WHERE notebook_id = 'nb-v101'"
        ).fetchone())

    with database.write() as db:
        db.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at) VALUES "
            "('nb-v101', 'n', ?, ?)", (now, now))
        rollback_v102(db)
        db.execute(
            "INSERT INTO unified_kg_state (notebook_id, updated_at) VALUES ('nb-v101', ?)",
            (now,))
        db.execute(
            "INSERT INTO concept_merge_candidates (id, notebook_id, canonical_a, "
            "canonical_b, score, status, created_at, updated_at) VALUES "
            "('mc-old', 'nb-v101', 'K-a b', 'K-c', 0.9, 'rejected', ?, ?)", (now, now))
        db.execute(
            "INSERT INTO communities (id, notebook_id, level, member_ids, size, created_at, "
            "generation) VALUES ('cm-a', 'nb-v101', 0, '[\"K-x\"]', 1, ?, 1)", (now,))
        db.execute("PRAGMA user_version = 101")

    assert SqliteMigrator(database, settings).migrate() == [102, 103, 104, 105]
    with database.connect() as db:
        assert state(db) == (1, 0, "")
        assert db.execute("SELECT board_key FROM communities WHERE id = 'cm-a'").fetchone()[0] == "cm-a"
        assert db.execute(
            "SELECT seed_version FROM concept_merge_candidates WHERE id = 'mc-old'"
        ).fetchone()[0] == 1

    with database.write() as db:
        db.execute("ALTER TABLE communities DROP COLUMN board_key")
        db.execute("ALTER TABLE unified_kg_state DROP COLUMN community_partition_mode")
        db.execute("ALTER TABLE unified_kg_state DROP COLUMN community_partition_version")
        db.execute("UPDATE unified_kg_state SET concept_seed_version = 2 WHERE notebook_id = 'nb-v101'")
        db.execute("PRAGMA user_version = 101")
    assert SqliteMigrator(database, settings).migrate() == [102, 103, 104, 105]
    with database.connect() as db:
        assert state(db) == (2, 0, "")
        assert db.execute("SELECT board_key FROM communities WHERE id = 'cm-a'").fetchone()[0] == "cm-a"
    database.close_local()


def _v101_with_unrecorded_merges(database, now: str) -> None:
    """A v101 database holding a deep copy made before the upgrade (concept
    clusters, no state row), a notebook with claim clusters only, a bare
    notebook and one with a state row and a board."""
    from tests.sqlite_migration_testkit import rollback_v102

    with database.write() as db:
        for notebook_id in ("nb-v101", "nb-copy", "nb-claims", "nb-bare"):
            db.execute(
                "INSERT INTO notebooks (id, name, created_at, updated_at) "
                "VALUES (?, 'n', ?, ?)", (notebook_id, now, now))
        rollback_v102(db)
        db.execute(
            "INSERT INTO unified_kg_state (notebook_id, updated_at) VALUES ('nb-v101', ?)",
            (now,))
        db.execute(
            "INSERT INTO communities (id, notebook_id, level, member_ids, size, created_at, "
            "generation) VALUES ('cm-a', 'nb-v101', 0, '[\"K-x\"]', 1, ?, 0)", (now,))
        for cluster_id, notebook_id, object_type in (
                ("cc-1", "nb-copy", "concept"), ("cc-2", "nb-claims", "claim")):
            db.execute(
                "INSERT INTO concept_clusters (id, notebook_id, canonical_id, "
                "member_object_id, canonical_name, object_type, created_at) "
                "VALUES (?, ?, 'K-q former', 'ko-1', 'Q Former', ?, ?)",
                (cluster_id, notebook_id, object_type, now))
        db.execute("PRAGMA user_version = 101")


@pytest.mark.legacy_upgrade_contract
@pytest.mark.real_sqlite_migration
def test_v102_gives_a_copy_without_a_state_row_one_in_seed_space_1(tmp_path):
    """Codex #988 R2: a deep copy made before the upgrade and never written to
    since holds concept clusters built in seed space 1 but no state row, and a
    missing row reads as the code's space 2 -- an upload would mint its new
    seeds beside the copied ones. v102 gives such a notebook a state row in
    space 1 whose every other column is its default (the clean row every
    reader takes for a missing one: not dirty, never rebuilt, generation 0).
    Claim clusters alone, or no clusters, get no row. Mutation anchor: drop the
    INSERT from ``_migration_102`` and ``nb-copy`` has no row."""
    database, settings = _fresh_migrated_database(tmp_path)
    now = "2026-10-23T00:00:00+00:00"
    _v101_with_unrecorded_merges(database, now)

    assert SqliteMigrator(database, settings).migrate() == [102, 103, 104, 105]
    with database.write() as db:
        db.execute("INSERT INTO notebooks (id, name, created_at, updated_at) "
                   "VALUES ('nb-ref', 'n', ?, ?)", (now, now))
        db.execute("INSERT INTO unified_kg_state (notebook_id, updated_at) "
                   "VALUES ('nb-ref', ?)", (now,))
    with database.connect() as db:
        state = {r["notebook_id"]: dict(r) for r in db.execute(
            "SELECT * FROM unified_kg_state")}
    assert set(state) == {"nb-v101", "nb-copy", "nb-ref"}
    assert (state["nb-v101"]["concept_seed_version"],
            state["nb-copy"]["concept_seed_version"],
            state["nb-ref"]["concept_seed_version"]) == (1, 1, 2)

    def rest(row):
        return {k: v for k, v in row.items()
                if k not in ("notebook_id", "updated_at", "concept_seed_version")}

    assert rest(state["nb-copy"]) == rest(state["nb-ref"])
    assert state["nb-copy"]["dirty"] == 0 and state["nb-copy"]["cluster_input_version"] == ""
    database.close_local()


@pytest.mark.legacy_upgrade_contract
@pytest.mark.real_sqlite_migration
def test_v102_is_one_transaction_so_an_interrupted_run_starts_over(tmp_path, monkeypatch):
    """Codex #988 R2: Python's sqlite3 opens no transaction before DDL, so an
    interruption after the ALTER that adds ``concept_seed_version`` (DEFAULT
    2) and before the UPDATE that sets the existing rows to 1 used to commit
    the column -- and the retry, seeing it, skipped the backfill for good:
    every existing library read as space 2. v102 is one BEGIN IMMEDIATE
    transaction with its stamp: the interrupted run leaves v101 behind and
    the retry backfills everything (existing rows in space 1, the copy's
    state row, every board's key). Mutation anchor: drop the BEGIN and the
    column survives the failure."""
    from app.repositories.sqlite import database as sqlite_database

    database, settings = _fresh_migrated_database(tmp_path)
    now = "2026-10-23T00:00:00+00:00"
    _v101_with_unrecorded_merges(database, now)

    real_execute = sqlite_database._Conn.execute

    def interrupted(self, statement, *args):
        if statement.startswith("UPDATE unified_kg_state SET concept_seed_version"):
            raise sqlite3.OperationalError("injected interruption")
        return real_execute(self, statement, *args)

    monkeypatch.setattr(sqlite_database._Conn, "execute", interrupted)
    with pytest.raises(sqlite3.OperationalError, match="injected interruption"):
        SqliteMigrator(database, settings).migrate()
    monkeypatch.setattr(sqlite_database._Conn, "execute", real_execute)

    with database.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 101
        columns = {r[1] for r in db.execute("PRAGMA table_info(unified_kg_state)")}
        assert "concept_seed_version" not in columns
        assert "board_key" not in {r[1] for r in db.execute("PRAGMA table_info(communities)")}

    assert SqliteMigrator(database, settings).migrate() == [102, 103, 104, 105]
    with database.connect() as db:
        assert dict(db.execute(
            "SELECT notebook_id, concept_seed_version FROM unified_kg_state").fetchall()) == {
            "nb-v101": 1, "nb-copy": 1}
        assert db.execute(
            "SELECT board_key FROM communities WHERE id = 'cm-a'").fetchone()[0] == "cm-a"
    database.close_local()
