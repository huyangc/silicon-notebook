"""PostgreSQL conformance for ``ExtensionToggleStorePort`` (deployment-plugin
runtime toggle, T1 storage layer).

Scope is deliberately narrow, mirroring ``test_catalog_store_conformance.py``'s
own rationale: this store has no service-layer consumer yet on either
backend — the full behavioural matrix (empty table, upsert, admin recheck,
audit fields) lives once in
``backend/tests/test_extension_toggle_store.py`` (SQLite). This file only
proves what is genuinely backend-specific:

- ``enabled`` round-trips as a real PostgreSQL ``boolean`` column — this
  repository's usual "SQLite INTEGER 0/1 flag -> PostgreSQL bigint"
  convention does NOT apply here (see
  ``0041_extension_runtime_toggles.sql``'s header for why);
- the admin recheck locks the actor row with a REAL ``FOR UPDATE`` row lock
  (proved below by a NOWAIT probe from a second connection — SQLite's
  process-wide write mutex makes the equivalent race structurally
  impossible there, so that side has no twin) instead of SQLite's process-
  wide write mutex;
- the demoted-actor recheck race (mirrors
  ``test_extension_toggle_store.py::test_a_demoted_actor_is_rechecked_at_write_time_not_at_call_time``)
  is re-proved here because PostgreSQL's per-connection isolation makes it a
  genuinely different code path (``FOR UPDATE`` + a second connection's
  concurrent ``UPDATE``) from SQLite's single process-wide writer lock.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from psycopg import errors

from app.repositories.postgres.extension_toggle_store import (
    ACTOR_ADMIN_ROLE_LOCK_SQL,
    ExtensionToggleStore,
)

NOW = "2026-08-29T00:00:00+00:00"

pytestmark = pytest.mark.postgres_integration


def _seed_user(database, *, user_id: str, username: str, role: str) -> None:
    mark = "%s"
    with database.write() as connection:
        connection.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) "
            f"VALUES ({','.join([mark] * 11)})",
            (
                user_id, f"{username}@example.test", username, role, "active",
                NOW, NOW, username, "", "", 0,
            ),
        )


@pytest.fixture
def store(request) -> ExtensionToggleStore:
    database = request.getfixturevalue("postgres_database")
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(database).migrate() == 43
    return ExtensionToggleStore(database)


def test_no_rows_means_every_plugin_is_enabled(store):
    assert store.extension_runtime_disabled_ids() == frozenset()
    assert store.list_extension_runtime_toggles() == []


def test_set_disabled_then_enabled_flips_membership_without_deleting_the_row(store):
    _seed_user(store.database, user_id="user-admin", username="a00000001", role="admin")

    disabled = store.set_extension_runtime_enabled("plugin-a", False, "user-admin")
    assert disabled == {
        "plugin_id": "plugin-a",
        "enabled": False,
        "updated_by": "user-admin",
        "updated_at": disabled["updated_at"],
    }
    assert isinstance(disabled["updated_at"], str) and disabled["updated_at"]
    # Aware UTC, not naive local: mirrors the SQLite store's `_now()` shape
    # byte-for-byte (see that module's docstring for why — a naive server-
    # local write would let an admin's browser `new Date()` silently
    # misread the instant whenever the browser and server timezones differ).
    assert disabled["updated_at"].endswith("+00:00")
    assert store.extension_runtime_disabled_ids() == frozenset({"plugin-a"})

    enabled = store.set_extension_runtime_enabled("plugin-a", True, "user-admin")
    assert enabled["enabled"] is True
    assert store.extension_runtime_disabled_ids() == frozenset()
    # The row survives re-enabling: still listed, just enabled=true, not
    # deleted and reinserted.
    assert [row["plugin_id"] for row in store.list_extension_runtime_toggles()] == [
        "plugin-a"
    ]


def test_list_is_sorted_by_plugin_id(store):
    _seed_user(store.database, user_id="user-admin", username="a00000002", role="admin")
    for plugin_id in ("zzz-plugin", "aaa-plugin", "mmm-plugin"):
        store.set_extension_runtime_enabled(plugin_id, False, "user-admin")
    assert [row["plugin_id"] for row in store.list_extension_runtime_toggles()] == [
        "aaa-plugin", "mmm-plugin", "zzz-plugin",
    ]


def test_upsert_updates_audit_fields_in_place_not_a_second_row(store):
    _seed_user(store.database, user_id="user-admin-1", username="a00000003", role="admin")
    _seed_user(store.database, user_id="user-admin-2", username="a00000004", role="admin")

    first = store.set_extension_runtime_enabled("plugin-b", False, "user-admin-1")
    assert first["updated_by"] == "user-admin-1"

    second = store.set_extension_runtime_enabled("plugin-b", False, "user-admin-2")
    assert second["updated_by"] == "user-admin-2"
    assert len(store.list_extension_runtime_toggles()) == 1


def test_non_admin_actor_is_rejected_and_nothing_is_written(store):
    _seed_user(store.database, user_id="user-plain", username="a00000005", role="user")

    with pytest.raises(PermissionError):
        store.set_extension_runtime_enabled("plugin-c", False, "user-plain")
    assert store.list_extension_runtime_toggles() == []
    assert store.extension_runtime_disabled_ids() == frozenset()


def test_unknown_actor_is_rejected_and_nothing_is_written(store):
    with pytest.raises(PermissionError):
        store.set_extension_runtime_enabled("plugin-d", False, "user-missing")
    assert store.list_extension_runtime_toggles() == []


@pytest.mark.parametrize("plugin_id", ["", "   ", "\t\n"])
def test_empty_or_whitespace_plugin_id_is_rejected_and_nothing_is_written(
    store, plugin_id: str,
):
    """PG twin of the same-named SQLite test — this guard is backend-neutral
    business logic (identical in both stores), so this only re-proves it
    holds on this backend too rather than adding new behavioural coverage."""
    _seed_user(store.database, user_id="user-admin-guard", username="a00000009", role="admin")

    with pytest.raises(ValueError, match="empty plugin_id"):
        store.set_extension_runtime_enabled(plugin_id, False, "user-admin-guard")
    assert store.list_extension_runtime_toggles() == []
    assert store.extension_runtime_disabled_ids() == frozenset()


def test_enabled_column_is_a_real_postgres_boolean_not_the_bigint_flag_convention(
    store,
):
    _seed_user(store.database, user_id="user-admin-3", username="a00000006", role="admin")
    store.set_extension_runtime_enabled("plugin-e", False, "user-admin-3")
    with store.database.connect() as connection:
        row = connection.execute(
            "SELECT pg_typeof(enabled) AS t FROM extension_runtime_toggles "
            "WHERE plugin_id=%s",
            ("plugin-e",),
        ).fetchone()
    assert str(row["t"]) == "boolean"


def test_a_demoted_actor_is_rechecked_at_write_time_not_at_call_time(store):
    """PG twin of
    ``test_extension_toggle_store.py::test_a_demoted_actor_is_rechecked_at_write_time_not_at_call_time``.

    Authorization must be read inside the write transaction, not cached from
    an earlier check — mirrors ``identity_store.set_user_role``'s own
    rationale (never trust a role read before the write started). This is a
    genuinely separate code path from the SQLite version: PostgreSQL has no
    process-wide write mutex, so the only thing that can make this recheck
    race-free is the ``FOR UPDATE`` lock itself.
    """
    _seed_user(store.database, user_id="user-was-admin", username="a00000007", role="admin")
    store.set_extension_runtime_enabled("plugin-f", False, "user-was-admin")

    with store.database.write() as connection:
        connection.execute(
            "UPDATE users SET role='user' WHERE id=%s", ("user-was-admin",)
        )

    with pytest.raises(PermissionError):
        store.set_extension_runtime_enabled("plugin-f", True, "user-was-admin")
    # The earlier disable is untouched — the rejected re-enable never wrote.
    assert store.extension_runtime_disabled_ids() == frozenset({"plugin-f"})


def test_set_extension_runtime_enabled_holds_a_real_row_lock_on_the_actor(store):
    """The admin recheck's ``SELECT ... FOR UPDATE`` must be a REAL row lock,
    not a plain read the query text merely looks like it should be — a typo
    dropping ``FOR UPDATE`` would still pass every other test in this file
    (they only ever run one writer at a time) and would silently reopen the
    exact TOCTOU race ``identity_store.set_user_role``'s own ``FOR UPDATE``
    closes: a concurrent demotion could commit between this store's recheck
    read and its toggle write.

    Mirrors
    ``test_admin_grant_chain_lock.py::test_the_group_chain_statement_locks_both_links``:
    a NOWAIT probe from a second connection is the only reliable way to
    observe a HELD row lock (``pg_locks`` only shows a row while something is
    *waiting* on it, never while it is merely held). The holder thread runs
    the exact SQL text ``set_extension_runtime_enabled`` executes
    (``ACTOR_ADMIN_ROLE_LOCK_SQL``, imported from the store module so this
    test cannot drift from what production actually runs) inside a write
    transaction it keeps open on purpose, rather than calling the store
    method itself — the method's transaction commits the instant it returns,
    which is too fast to deterministically race a probe against; holding the
    identical statement open under test control is the only way to make the
    window observable without changing production code.
    """
    _seed_user(store.database, user_id="user-lock-target", username="a00000008", role="admin")

    lock_acquired = threading.Event()
    allow_release = threading.Event()

    def hold_the_actor_lock() -> str:
        with store.database.write() as connection:
            row = connection.execute(
                ACTOR_ADMIN_ROLE_LOCK_SQL, ("user-lock-target",)
            ).fetchone()
            assert row is not None
            lock_acquired.set()
            assert allow_release.wait(timeout=10)
        return "released"

    def probe_is_blocked() -> bool:
        """True = the probe could NOT get the lock (i.e. it is held)."""
        try:
            with store.database.write() as connection:
                connection.execute(
                    "SELECT 1 FROM users WHERE id=%s FOR UPDATE NOWAIT",
                    ("user-lock-target",),
                )
            return False
        except errors.LockNotAvailable:
            return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        holder = executor.submit(hold_the_actor_lock)
        try:
            assert lock_acquired.wait(timeout=10)
            assert executor.submit(probe_is_blocked).result(timeout=10), (
                "set_extension_runtime_enabled's actor-role read did not "
                "hold a FOR UPDATE lock on the actor row"
            )
        finally:
            allow_release.set()
        assert holder.result(timeout=15) == "released"
