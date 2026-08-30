"""Store-level coverage for ``ExtensionToggleStorePort`` (deployment-plugin
runtime toggle, T1 storage layer).

Built directly on ``app.repositories.sqlite.extension_toggle_store`` plus a
bare migrated ``SqliteDatabase``, mirroring ``test_catalog_store.py``'s
rationale: this file proves the STORE primitive in isolation — there is no
service layer over it yet (a later task wires the admin route + the
admission-gate refresh). The table has no foreign key in either direction, so
the only seeding these tests need is the ``users`` rows the admin-recheck
write path reads.

The PostgreSQL twin lives in
``backend/tests/postgres/test_extension_toggle_store_conformance.py`` and
only proves the backend-specific parts (real ``boolean`` column, ``FOR
UPDATE`` locking) — every behavioural case below is SQLite-only coverage by
design.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.extension_toggle_store import ExtensionToggleStore
from app.repositories.sqlite.migrations import SCHEMA_VERSION, SqliteMigrator


NOW = "2026-08-29T00:00:00+00:00"


@pytest.fixture
def database(tmp_path: Path) -> SqliteDatabase:
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}")
    db = SqliteDatabase(settings, tmp_path)
    migrated = SqliteMigrator(db, settings).migrate()
    assert migrated, "fresh database must actually run the migration ladder"
    return db


@pytest.fixture
def store(database: SqliteDatabase) -> ExtensionToggleStore:
    return ExtensionToggleStore(database)


def _seed_user(database: SqliteDatabase, *, user_id: str, role: str) -> None:
    with database.write() as db:
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,"
            "updated_at) VALUES (?,?,?,?,?,?,?)",
            (user_id, f"{user_id}@example.test", user_id, role, "active", NOW, NOW),
        )


def test_fresh_database_reaches_current_schema_version_with_the_table_present(
    tmp_path: Path,
):
    """Schema version 63 is where ``extension_runtime_toggles`` itself
    landed (_migration_63); the table must still be present after later
    migrations (currently through v64's unrelated concept_clusters keyset
    index) run on top of it, so this asserts against the live
    ``SCHEMA_VERSION`` head rather than a number that drifts every time an
    unrelated migration is added."""
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'fresh.db'}")
    db = SqliteDatabase(settings, tmp_path)
    applied = SqliteMigrator(db, settings).migrate()
    assert applied and applied[-1] == SCHEMA_VERSION
    with db.connect() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='extension_runtime_toggles'"
        ).fetchone()
    assert version == SCHEMA_VERSION
    assert table is not None


def test_empty_table_means_no_plugin_is_disabled(store: ExtensionToggleStore):
    assert store.extension_runtime_disabled_ids() == frozenset()
    assert store.list_extension_runtime_toggles() == []


def test_set_disabled_then_enabled_flips_membership_without_deleting_the_row(
    store: ExtensionToggleStore, database: SqliteDatabase,
):
    _seed_user(database, user_id="user-admin", role="admin")

    disabled = store.set_extension_runtime_enabled("plugin-a", False, "user-admin")
    assert disabled == {
        "plugin_id": "plugin-a",
        "enabled": False,
        "updated_by": "user-admin",
        "updated_at": disabled["updated_at"],
    }
    assert isinstance(disabled["updated_at"], str) and disabled["updated_at"]
    # Aware UTC, not naive local: a browser's `new Date()` parses a naive
    # string in ITS OWN local timezone, so a naive server-local write would
    # silently drift from the actual instant whenever the two timezones
    # differ. Mirrors the PostgreSQL store's `utc_now()` + `iso_timestamp()`
    # shape byte-for-byte (see extension_toggle_store.py's `_now()` docstring).
    assert disabled["updated_at"].endswith("+00:00")
    assert store.extension_runtime_disabled_ids() == frozenset({"plugin-a"})

    enabled = store.set_extension_runtime_enabled("plugin-a", True, "user-admin")
    assert enabled["enabled"] is True
    assert store.extension_runtime_disabled_ids() == frozenset()
    # The row survives re-enabling — still listed, just enabled=true, not
    # deleted and reinserted (this is what lets a later re-disable recover
    # the same audit history rather than starting a fresh row).
    assert [row["plugin_id"] for row in store.list_extension_runtime_toggles()] == [
        "plugin-a"
    ]


def test_disabled_ids_contains_only_the_disabled_rows(
    store: ExtensionToggleStore, database: SqliteDatabase,
):
    _seed_user(database, user_id="user-admin", role="admin")
    store.set_extension_runtime_enabled("plugin-off", False, "user-admin")
    store.set_extension_runtime_enabled("plugin-on", True, "user-admin")
    assert store.extension_runtime_disabled_ids() == frozenset({"plugin-off"})
    assert {row["plugin_id"] for row in store.list_extension_runtime_toggles()} == {
        "plugin-off", "plugin-on",
    }


def test_list_is_sorted_by_plugin_id(
    store: ExtensionToggleStore, database: SqliteDatabase,
):
    _seed_user(database, user_id="user-admin", role="admin")
    for plugin_id in ("zzz-plugin", "aaa-plugin", "mmm-plugin"):
        store.set_extension_runtime_enabled(plugin_id, False, "user-admin")
    assert [row["plugin_id"] for row in store.list_extension_runtime_toggles()] == [
        "aaa-plugin", "mmm-plugin", "zzz-plugin",
    ]


def test_upsert_updates_audit_fields_in_place_not_a_second_row(
    store: ExtensionToggleStore, database: SqliteDatabase,
):
    _seed_user(database, user_id="user-admin-1", role="admin")
    _seed_user(database, user_id="user-admin-2", role="admin")

    first = store.set_extension_runtime_enabled("plugin-b", False, "user-admin-1")
    assert first["updated_by"] == "user-admin-1"

    second = store.set_extension_runtime_enabled("plugin-b", False, "user-admin-2")
    assert second["updated_by"] == "user-admin-2"
    assert len(store.list_extension_runtime_toggles()) == 1


def test_non_admin_actor_is_rejected_and_nothing_is_written(
    store: ExtensionToggleStore, database: SqliteDatabase,
):
    _seed_user(database, user_id="user-plain", role="user")

    with pytest.raises(PermissionError, match="admin role required"):
        store.set_extension_runtime_enabled("plugin-c", False, "user-plain")
    assert store.list_extension_runtime_toggles() == []
    assert store.extension_runtime_disabled_ids() == frozenset()


def test_unknown_actor_is_rejected_and_nothing_is_written(
    store: ExtensionToggleStore,
):
    with pytest.raises(PermissionError, match="admin role required"):
        store.set_extension_runtime_enabled("plugin-d", False, "user-missing")
    assert store.list_extension_runtime_toggles() == []


@pytest.mark.parametrize("plugin_id", ["", "   ", "\t\n"])
def test_empty_or_whitespace_plugin_id_is_rejected_and_nothing_is_written(
    store: ExtensionToggleStore, database: SqliteDatabase, plugin_id: str,
):
    """Minimal guard, identical on both backends (see the PostgreSQL store's
    same check): this store does not know which plugin ids are actually
    loaded — that stronger "must be in the loaded deployment-plugin set"
    validation belongs to the T4 route layer, which is the only place that
    can see the frozen registry. This just rejects garbage before it can
    ever reach a query."""
    _seed_user(database, user_id="user-admin", role="admin")

    with pytest.raises(ValueError, match="empty plugin_id"):
        store.set_extension_runtime_enabled(plugin_id, False, "user-admin")
    assert store.list_extension_runtime_toggles() == []
    assert store.extension_runtime_disabled_ids() == frozenset()


def test_a_demoted_actor_is_rechecked_at_write_time_not_at_call_time(
    store: ExtensionToggleStore, database: SqliteDatabase,
):
    """Authorization must be read inside the write transaction, not cached
    from an earlier check — mirrors ``identity_store.set_user_role``'s own
    rationale (never trust a role read before the write started)."""
    _seed_user(database, user_id="user-was-admin", role="admin")
    store.set_extension_runtime_enabled("plugin-e", False, "user-was-admin")

    with database.write() as db:
        db.execute("UPDATE users SET role='user' WHERE id=?", ("user-was-admin",))

    with pytest.raises(PermissionError, match="admin role required"):
        store.set_extension_runtime_enabled("plugin-e", True, "user-was-admin")
    # The earlier disable is untouched — the rejected re-enable never wrote.
    assert store.extension_runtime_disabled_ids() == frozenset({"plugin-e"})
