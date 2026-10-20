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


def rollback_v102(db: sqlite3.Connection) -> None:
    """Remove what SQLite migration 102 added:
    ``concept_merge_candidates.seed_version`` and
    ``unified_kg_state.concept_seed_version``. Callers lower ``user_version``
    themselves."""
    db.execute("ALTER TABLE concept_merge_candidates DROP COLUMN seed_version")
    db.execute("ALTER TABLE unified_kg_state DROP COLUMN concept_seed_version")


def rollback_v101(db: sqlite3.Connection) -> None:
    """Remove what SQLite migration 101 added: ``chunks.navigation``,
    ``sources.navigation_scanned_at`` and ``sources.navigation_version``.
    v102 is undone first (newest-first). Callers lower ``user_version``
    themselves."""
    rollback_v102(db)
    db.execute("ALTER TABLE chunks DROP COLUMN navigation")
    db.execute("ALTER TABLE sources DROP COLUMN navigation_scanned_at")
    db.execute("ALTER TABLE sources DROP COLUMN navigation_version")


def rollback_v99(db: sqlite3.Connection) -> None:
    """Remove ``ask_jobs.report_id``, the column SQLite migration 99 added.
    v100 is undone first (newest-first). Callers lower ``user_version``
    themselves."""
    rollback_v100(db)
    db.execute("ALTER TABLE ask_jobs DROP COLUMN report_id")


def rollback_v100(db: sqlite3.Connection) -> None:
    """Remove what SQLite migration 100 added: the four notebooks deletion-window
    columns and ``idx_notebooks_pending_delete_due``. v101 is undone first
    (newest-first). Callers lower ``user_version`` themselves."""
    rollback_v101(db)
    db.execute("DROP INDEX idx_notebooks_pending_delete_due")
    for column in (
        "status_before_delete", "delete_requested_by", "delete_requested_at",
        "delete_after",
    ):
        db.execute(f"ALTER TABLE notebooks DROP COLUMN {column}")


def rollback_v98(db: sqlite3.Connection) -> None:
    """Remove what SQLite migration 98 added: the ``global_ask_job_notebooks``
    table with its index, ``idx_global_ask_jobs_unattributed`` and
    ``global_ask_jobs.attributed``. v99 is undone first (newest-first). Callers
    lower ``user_version`` themselves."""
    rollback_v99(db)
    db.execute("DROP INDEX idx_global_ask_jobs_unattributed")
    db.execute("DROP INDEX idx_global_ask_job_notebooks_notebook")
    db.execute("DROP TABLE global_ask_job_notebooks")
    db.execute("ALTER TABLE global_ask_jobs DROP COLUMN attributed")


def rollback_v97(db: sqlite3.Connection) -> None:
    """Remove what SQLite migration 97 added: ``kg_analysis_findings`` with its
    three change-capture triggers, ``kg_analysis_marks`` and
    ``unified_kg_state.cluster_code_version``. v98 is undone first
    (newest-first). Callers lower ``user_version`` themselves."""
    from app.migration.sync.capture import sqlite_trigger_sql

    rollback_v98(db)

    for name in sorted(sqlite_trigger_sql(added_at=97)):
        db.execute(f'DROP TRIGGER "{name}"')
    db.execute("DROP TABLE kg_analysis_marks")
    db.execute("DROP TABLE kg_analysis_findings")
    db.execute("ALTER TABLE unified_kg_state DROP COLUMN cluster_code_version")


def rollback_v96(db: sqlite3.Connection) -> None:
    """Remove ``idx_global_ask_jobs_feedback``, the partial index SQLite
    migration 96 added. v97 is undone first (newest-first). Callers lower
    ``user_version`` themselves."""
    rollback_v97(db)
    db.execute("DROP INDEX idx_global_ask_jobs_feedback")


def rollback_v95(db: sqlite3.Connection) -> None:
    """Remove what SQLite migration 95 added: ``global_ask_jobs.retained_until``
    and its partial index. v96 is undone first (newest-first). Callers lower
    ``user_version`` themselves."""
    rollback_v96(db)
    db.execute("DROP INDEX idx_global_ask_jobs_retained")
    db.execute("ALTER TABLE global_ask_jobs DROP COLUMN retained_until")


def rollback_v94(db: sqlite3.Connection) -> None:
    """Remove what SQLite migration 94 added: the ``answer_links`` table with
    its two indexes and ``agent_access_tokens.allow_answer_links``. v95 is
    undone first (newest-first). Callers lower ``user_version`` themselves."""
    rollback_v95(db)
    db.execute("DROP INDEX idx_answer_links_expires")
    db.execute("DROP INDEX uq_answer_links_job_token")
    db.execute("DROP TABLE answer_links")
    db.execute("ALTER TABLE agent_access_tokens DROP COLUMN allow_answer_links")


def rollback_v93(db: sqlite3.Connection) -> None:
    """Remove ``agent_profiles.deleted_at``, the column SQLite migration 93
    added. v94 is undone first (newest-first). Callers lower ``user_version``
    themselves."""
    rollback_v94(db)
    db.execute("ALTER TABLE agent_profiles DROP COLUMN deleted_at")


def rollback_v92(db: sqlite3.Connection) -> None:
    """Restore the v91 authentication schema that SQLite migration 92 dropped.

    The three staged-auth tables come back empty except for the seeded
    'local' policy row every v78-v91 server wrote; ``users.local_login_name``
    comes back with v78's backfill (the username) and its unique index; and
    ``auth_identity_audit`` is rebuilt with its ``grant_reference`` column.
    v92's additions -- ``users.sso_linked_at`` and ``idx_users_username_lower``
    -- are removed. v93 is undone first (newest-first). Callers lower
    ``user_version`` themselves.
    """
    rollback_v93(db)
    db.execute("DROP INDEX idx_users_username_lower")
    db.execute("ALTER TABLE users DROP COLUMN sso_linked_at")
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
