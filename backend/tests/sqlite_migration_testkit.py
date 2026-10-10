"""Helpers for forging faithful pre-current SQLite migration fixtures."""

from __future__ import annotations

import sqlite3


# SQLite v78's authentication objects that v92 dropped, spelled exactly as
# sqlite_master stores them (SQLite strips "IF NOT EXISTS").
V78_AUTH_TABLES = {
    "auth_policy": (
        "CREATE TABLE auth_policy (\n"
        "                 id INTEGER PRIMARY KEY CHECK(id=1),\n"
        "                 mode TEXT NOT NULL DEFAULT 'local' CHECK(mode IN "
        "('local','dual','binding_required','sso_only','retired')),\n"
        "                 revision INTEGER NOT NULL DEFAULT 0,\n"
        "                 provider_id TEXT NOT NULL DEFAULT '', provider_namespace TEXT NOT NULL DEFAULT '',\n"
        "                 config_generation TEXT NOT NULL DEFAULT '', plugin_id TEXT NOT NULL DEFAULT '', "
        "retired_at TEXT, updated_by TEXT NOT NULL DEFAULT ''\n"
        "                )"
    ),
    "external_identities": (
        "CREATE TABLE external_identities (\n"
        "                 provider_namespace TEXT NOT NULL, subject TEXT NOT NULL,\n"
        "                 user_id TEXT NOT NULL REFERENCES users(id), status TEXT NOT NULL DEFAULT 'active',\n"
        "                 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_login_at TEXT,\n"
        "                 PRIMARY KEY(provider_namespace,subject)\n"
        "                )"
    ),
    "auth_policy_audit": (
        "CREATE TABLE auth_policy_audit (\n"
        "                 id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, previous_mode TEXT NOT NULL,\n"
        "                 mode TEXT NOT NULL, revision INTEGER NOT NULL, created_at TEXT NOT NULL\n"
        "                )"
    ),
    "auth_identity_audit": (
        "CREATE TABLE auth_identity_audit (\n"
        "                 id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, target_user_id TEXT NOT NULL,\n"
        "                 action TEXT NOT NULL, provider_namespace TEXT NOT NULL,\n"
        "                 subject TEXT NOT NULL, grant_reference TEXT NOT NULL DEFAULT '',\n"
        "                 created_at TEXT NOT NULL\n"
        "                )"
    ),
}


def rollback_v92(db: sqlite3.Connection) -> None:
    """Restore the v91 authentication schema that SQLite migration 92 dropped.

    The three staged-auth tables come back empty except for the seeded
    'local' policy row every v78-v91 server wrote; ``users.local_login_name``
    comes back with v78's backfill (the username) and its unique index; and
    ``auth_identity_audit`` is rebuilt with its ``grant_reference`` column.
    Callers lower ``user_version`` themselves.
    """
    for name in ("auth_policy", "external_identities", "auth_policy_audit"):
        db.execute(V78_AUTH_TABLES[name])
    db.execute(
        "CREATE UNIQUE INDEX idx_external_identities_active_user\n"
        "                 ON external_identities(user_id,provider_namespace) WHERE status='active'"
    )
    db.execute("INSERT INTO auth_policy(id) VALUES(1)")
    rows = db.execute("SELECT * FROM auth_identity_audit").fetchall()
    db.execute("DROP TABLE auth_identity_audit")
    db.execute(V78_AUTH_TABLES["auth_identity_audit"])
    db.execute(
        "CREATE INDEX idx_auth_identity_audit_created\n"
        "                 ON auth_identity_audit(created_at,id)"
    )
    for row in rows:
        db.execute(
            "INSERT INTO auth_identity_audit"
            "(id,actor_id,target_user_id,action,provider_namespace,subject,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            tuple(row),
        )
    db.execute("ALTER TABLE users ADD COLUMN local_login_name TEXT")
    db.execute("UPDATE users SET local_login_name=username WHERE username<>''")
    db.execute(
        "CREATE UNIQUE INDEX idx_users_local_login_name ON users(local_login_name) "
        "WHERE local_login_name IS NOT NULL"
    )


def rollback_v78(db: sqlite3.Connection) -> None:
    """Remove every schema object introduced by SQLite migration 78.

    Historical migration tests commonly start with a current database and
    remove the objects owned by the migration under test.  When they lower
    ``user_version`` below 78 they must also remove v78: leaving its index on
    ``users`` creates a mixed-generation schema that no deployed database
    could have had and makes the one-way v78 migration fail for the wrong
    reason. v92 is undone first (newest-first).
    """
    rollback_v92(db)
    for table in (
        "auth_identity_audit",
        "auth_policy_audit",
        "auth_transactions",
        "external_identities",
        "auth_policy",
    ):
        db.execute(f"DROP TABLE {table}")
    db.execute("DROP INDEX idx_users_local_login_name")
    db.execute("ALTER TABLE users DROP COLUMN local_login_name")
    db.execute("ALTER TABLE users DROP COLUMN auth_revision")
    for column in (
        "auth_source",
        "absolute_expires_at",
        "provider_namespace",
        "external_subject",
    ):
        db.execute(f"ALTER TABLE auth_sessions DROP COLUMN {column}")
