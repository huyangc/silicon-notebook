"""Helpers for forging faithful pre-current SQLite migration fixtures."""

from __future__ import annotations

import sqlite3


def rollback_v78(db: sqlite3.Connection) -> None:
    """Remove every schema object introduced by SQLite migration 78.

    Historical migration tests commonly start with a current database and
    remove the objects owned by the migration under test.  When they lower
    ``user_version`` below 78 they must also remove v78: leaving its index on
    ``users`` creates a mixed-generation schema that no deployed database
    could have had and makes the one-way v78 migration fail for the wrong
    reason.
    """
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
