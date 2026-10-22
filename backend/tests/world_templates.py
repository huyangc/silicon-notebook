"""Seeded world templates: build an expensive test world once per process, copy it per test.

Not a test module (pytest only collects ``test_*.py``); ``test_world_templates.py``
pins every rule below against deliberately broken templates.

Usage from a function-scoped fixture::

    template = world_template(tmp_path_factory, "memory-purge", build, reopen,
                              env={"EMBED_DIM": "8"})
    root, payload = template.materialize(tmp_path)

``build(root)`` seeds the world under ``root`` through the real production code
and returns a pure-data payload (ids, strings, numbers, tuples, lists, dicts).
``reopen(root)`` constructs the repository the way a test will and returns it
(or ``None`` after closing it itself); the template closes whatever it returns.

Build (once per process per key)
--------------------------------
* Runs inside ``MonkeyPatch.context()``. The environment is first reset to the
  process baseline captured when this module was imported, so the build does
  not inherit whatever the first requesting test happened to setenv. Then
  ``DATABASE_URL`` / ``SILICON_NOTEBOOK_STORAGE_DIR`` point at the build root,
  the log variables point at a sibling log directory outside the world, and
  the caller's ``env`` is applied. ``env`` is part of the template identity:
  the same ``key`` with a different ``env``, layout, builder or reopener is a
  loud error, never a silently shared world. Callbacks are compared by code
  object, defaults, closure-cell contents and bound ``__self__``, not by
  name: two closures from one factory share a qualified name.
* The conftest autouse patches (schema-template copy, fast default password
  hashing) are active exactly as when every test built its own world.
* The payload must be pure data and survive a JSON round trip. Live objects
  (repositories, services, dataclasses, ``Path``) are rejected: they would be
  shared across tests and point at the build root.
* Afterwards the ``deps.repository`` singleton is closed, ``get_settings`` and
  ``deps.repository`` caches are cleared, every SQLite file under the root
  gets ``PRAGMA wal_checkpoint(TRUNCATE)`` and its WAL must be empty, and no
  file descriptor of this process may still point into the root (retried once
  after ``gc.collect()`` so an unreachable repository is not blamed). A
  builder that leaves a repository open fails here.

Build-time hard checks
----------------------
1. Reopen stability: a full-world digest (every real table's rows, sorted;
   the schema; the persistent header fields ``user_version``,
   ``application_id`` and ``journal_mode``; every non-SQLite file's bytes)
   is taken, ``reopen(root)`` runs
   and is closed, and the digest must not change. Open-time normalisation
   (interrupted-job recovery, auth-transaction purge, seed inserts) is caught
   at build time instead of silently differing from the world the test meant.
   The one column set the constructor rewrites on *every* open — the seeded
   admin's credential re-derivation (``_seed``: fresh salt and hash for the
   same configured password, plus ``updated_at``) — is masked; see
   ``OPEN_TIME_REWRITES``.
2. Layout. ``relocatable`` (default): the world is copied to the test's
   ``tmp_path``; no SQLite value, no storage file and no payload value may
   contain the build root path. ``pinned``: for worlds that store absolute
   paths (uploaded sources record ``sources.file_path``); the world is built
   at ``<basetemp>/worlds/<slug>/live`` and copied once to ``pristine``. Every
   materialize removes ``live`` and copies ``pristine`` back, so the embedded
   paths stay valid and no test can reach ``pristine`` through a stored path.
3. Pristine integrity: ``(relpath, size, mtime_ns)`` of every file and the
   directory set are recorded and re-checked on every materialize.

Per test (``materialize``)
--------------------------
Identity-keyed process caches are reset before the copy: every test of one
worker now sees the SAME notebook/user ids, whereas before each test had fresh
random ids that no stale cache entry could ever hit. The reset then restores
exactly what the build itself left for the world's own notebook ids (diffed
around ``build``; see ``_RESTORE_POLICY``): count memos whose version comes
from the world's own history, the asset-sweep throttle at the same age, the
orphan-sweep marks. A self-built world started its test with that warm state,
and tests rely on it (a stale import-time memo is what catches a count gate
that stops checking versions); entries for any other id are never restored. ``PROCESS_CACHE_INVENTORY``
classifies every module-level cache container and ``functools`` cache in
``backend/app``; ``test_world_templates.py`` rescans the tree and fails when a
new one appears unclassified. Only the ``IDENTITY_RESET`` category is keyed by
notebook/user id and therefore needs a reset in ``_IDENTITY_RESETS``.

A test materializes one copy per world unless it resets caches itself between
two copies of the same world (``reset_identity_keyed_process_caches``).
"""
from __future__ import annotations

import copy
import functools
import gc
import hashlib
import importlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Mapping

import pytest


RELOCATABLE = "relocatable"
PINNED = "pinned"
_LAYOUTS = (RELOCATABLE, PINNED)

WORLD_DB_NAME = "world.db"
WORLD_STORAGE_NAME = "storage"

_SQLITE_HEADER = b"SQLite format 3\x00"
_HEADER_PRAGMAS = ("user_version", "application_id", "journal_mode")
_SIDE_FILE_SUFFIXES = ("-wal", "-shm", "-journal")

# Captured when the first test module imports this helper (collection time,
# after conftest's process-level defaults). Builds start from this, not from
# the environment of whichever test triggered the build.
_BASELINE_ENV: dict[str, str] = dict(os.environ)

# ``SqliteMigrator._seed`` runs on every repository construction and
# re-derives the seeded admin's password hash with a fresh salt (same
# configured password) and bumps ``updated_at``. Today every test that opens a
# repository on its world gets that rewrite too, so it is not a world change.
# Anything else that differs after a reopen is.
OPEN_TIME_REWRITES: dict[str, tuple[str, str, frozenset[str]]] = {
    "users": (
        "id",
        "user-local",
        frozenset({"password_hash", "password_salt", "updated_at"}),
    ),
}


class WorldTemplateError(AssertionError):
    """A world template violated one of the build or materialize rules."""


# --------------------------------------------------------------------------
# Process-cache inventory
# --------------------------------------------------------------------------

IDENTITY_RESET = "identity-keyed, reset on materialize"
CONTENT_KEYED = "content/path-keyed"
OWNER_SCOPED = "keyed by a live owner object (weak/identity), new per test"
RUNTIME = "in-flight only"
FACTORY = "factory singleton"

#: Every module-level cache container and ``functools`` cache in
#: ``backend/app`` (``module:name``), with the reason it is or is not a hazard
#: for worlds whose ids repeat across the tests of one worker.
PROCESS_CACHE_INVENTORY: dict[str, str] = {
    # Count memos keyed by notebook id + (kg_reset_epoch, kg_mutation_seq).
    # Two copies of one world share both, so a stale entry would be served.
    "app.repositories.sqlite.knowledge_counts_cache:_MEMO": IDENTITY_RESET,
    "app.repositories.sqlite.knowledge_counts_cache:_PENDING": IDENTITY_RESET,
    "app.repositories.sqlite.knowledge_counts_cache:_VISIBLE_PENDING": IDENTITY_RESET,
    "app.repositories.sqlite.knowledge_counts_cache:_CHUNKS": IDENTITY_RESET,
    "app.repositories.postgres.knowledge_counts_cache:_MEMO": IDENTITY_RESET,
    "app.repositories.postgres.knowledge_counts_cache:_PENDING": IDENTITY_RESET,
    "app.repositories.postgres.knowledge_counts_cache:_VISIBLE_PENDING": IDENTITY_RESET,
    "app.repositories.postgres.knowledge_counts_cache:_CHUNKS": IDENTITY_RESET,
    "app.repositories.postgres.knowledge_counts_cache:_EPOCHS": IDENTITY_RESET,
    # 300 s per-notebook throttle: a later test would silently skip its sweep.
    "app.services.knowhow.api:_LAST_ASSET_SWEEP": IDENTITY_RESET,
    # "Swept orphan clusters once for this notebook in this process": a later
    # test would skip the incremental-fusion orphan sweep for the same id.
    "app.services.knowledge_lifecycle:_ORPHAN_SWEEP_DONE": IDENTITY_RESET,
    # Path-keyed, and each test has its own log directory.
    "app.core.event_logging:_last_write_day": CONTENT_KEYED,
    # Keyed by the resolved LLM-cache file path; the cache is off in tests.
    "app.core.cache:_INSTANCES": CONTENT_KEYED,
    # Keyed by (PostgreSQL database name, table OID); templates are SQLite.
    "app.repositories.postgres.search:_KNN_INDEX_CACHE": CONTENT_KEYED,
    # Keyed by manifest path + (inode, mtime_ns, size).
    "app.services.release_notes:_cache": CONTENT_KEYED,
    # Static token table built at import.
    "app.services.kg.conflict_detect:_TOKEN_GROUPS": CONTENT_KEYED,
    # One row encoder per table name, built from the migration DDL contract.
    "app.services.notebook_transfer.export:_row_encoder": CONTENT_KEYED,
    # Re-embed run records keyed by a random per-run id, never by notebook id, so
    # two copies of one world cannot read each other's runs.
    "app.services.notebook_reembed:_runs": CONTENT_KEYED,
    # Owned by a weak reference to the experience store; each test's
    # repository is a new store object.
    "app.services.reasoning_retrieval:_EXPERIENCE_CACHE": OWNER_SCOPED,
    # WeakKeyDictionary keyed by repository; conftest drains it per test.
    "app.services.knowhow.api:_SCHEDULERS": OWNER_SCOPED,
    # Present only while a request / run / job is in flight.
    "app.api.mcp_tools.ask:_WAITERS": RUNTIME,
    "app.services.notebook_reembed:_active_runs": RUNTIME,
    # Named claims held only for the duration of a ``try_claim`` block (the
    # deletion-window transfer's one-at-a-time claim).
    "app.repositories.sqlite.database:_PROCESS_CLAIMS": RUNTIME,
    "app.services.plugin_ask_engine:_CANCELLATION_STATES": RUNTIME,
    "app.services.plugin_ask_engine:_RETRIEVAL_STATES": RUNTIME,
    "app.services.plugin_ask_engine:_MODEL_STATES": RUNTIME,
    "app.services.plugin_ask_engine:_TRACE_STATES": RUNTIME,
    # Maintenance pools by pool name; conftest drains and resets them.
    "app.services.background_jobs:_executors": RUNTIME,
    # functools caches.
    "app.core.config:get_settings": FACTORY,
    "app.api.deps:repository": FACTORY,
    "app.api.deps:require_notebook_capability": FACTORY,
    "app.extensions.bootstrap:default_extension_runtime": FACTORY,
}


def _clear_orphan_sweep_memo(module: ModuleType) -> None:
    # No public reset exists; tests already clear private module caches under
    # their lock (``reasoning_retrieval._EXPERIENCE_CACHE``).
    with module._ORPHAN_SWEEP_DONE_LOCK:
        module._ORPHAN_SWEEP_DONE.clear()


#: Reset for every module owning an ``IDENTITY_RESET`` entry. A module that is
#: not imported yet holds no state, so it is skipped rather than imported.
_IDENTITY_RESETS: dict[str, Callable[[ModuleType], None]] = {
    "app.repositories.sqlite.knowledge_counts_cache": lambda module: module.invalidate(),
    "app.repositories.postgres.knowledge_counts_cache": lambda module: module.invalidate(),
    "app.services.knowhow.api": lambda module: module.reset_asset_sweep_throttle(),
    "app.services.knowledge_lifecycle": _clear_orphan_sweep_memo,
}


def reset_identity_keyed_process_caches() -> None:
    """Drop every process cache keyed by notebook/user id (see inventory)."""
    for module_name, reset in _IDENTITY_RESETS.items():
        module = sys.modules.get(module_name)
        if module is not None:
            reset(module)


# How an identity-keyed entry the build left behind is carried onto every
# copy. Before templates, a test built its world in its own body, so it began
# with whatever the build warmed (a count memo keyed by the import-time
# version, an occupied 300 s sweep throttle, ...). ``materialize`` resets these
# caches and then restores exactly the build's own entries for the world's
# own notebook ids, so each copy starts where a self-built world started.
VERSION_GATED = "(version, value) per notebook id; restored when the version is from the world's history"
MONOTONIC_STAMP = "time.monotonic() stamp per notebook id; restored at the same age"
MEMBER = "notebook id membership; restored as is"
RESET_ONLY = "reset only (PostgreSQL state cannot belong to a SQLite template)"

_RESTORE_POLICY: dict[str, str] = {
    "app.repositories.sqlite.knowledge_counts_cache:_MEMO": VERSION_GATED,
    "app.repositories.sqlite.knowledge_counts_cache:_PENDING": VERSION_GATED,
    "app.repositories.sqlite.knowledge_counts_cache:_VISIBLE_PENDING": VERSION_GATED,
    "app.repositories.sqlite.knowledge_counts_cache:_CHUNKS": VERSION_GATED,
    "app.repositories.postgres.knowledge_counts_cache:_MEMO": RESET_ONLY,
    "app.repositories.postgres.knowledge_counts_cache:_PENDING": RESET_ONLY,
    "app.repositories.postgres.knowledge_counts_cache:_VISIBLE_PENDING": RESET_ONLY,
    "app.repositories.postgres.knowledge_counts_cache:_CHUNKS": RESET_ONLY,
    "app.repositories.postgres.knowledge_counts_cache:_EPOCHS": RESET_ONLY,
    "app.services.knowhow.api:_LAST_ASSET_SWEEP": MONOTONIC_STAMP,
    "app.services.knowledge_lifecycle:_ORPHAN_SWEEP_DONE": MEMBER,
}

#: The lock each restorable container is mutated under in production.
_CONTAINER_LOCKS: dict[str, str] = {
    "app.repositories.sqlite.knowledge_counts_cache": "_LOCK",
    "app.services.knowhow.api": "_ASSET_SWEEP_LOCK",
    "app.services.knowledge_lifecycle": "_ORPHAN_SWEEP_DONE_LOCK",
}

#: Injectable clock for the stamp rebase (pinned with a fake clock).
_monotonic = time.monotonic


def _restorable_state() -> dict[str, Any]:
    """A shallow snapshot of every restorable container that is loaded."""
    state: dict[str, Any] = {}
    for entry, policy in _RESTORE_POLICY.items():
        if policy == RESET_ONLY:
            continue
        module_name, name = entry.split(":")
        module = sys.modules.get(module_name)
        if module is None:
            state[entry] = set() if policy == MEMBER else {}
            continue
        with getattr(module, _CONTAINER_LOCKS[module_name]):
            container = getattr(module, name)
            state[entry] = set(container) if policy == MEMBER else dict(container)
    return state


@dataclass(frozen=True)
class _WarmEntry:
    entry: str
    key: str
    value: Any  # VERSION_GATED: the cached tuple; MONOTONIC_STAMP: age in seconds


def _world_notebook_versions(root: Path) -> dict[str, set[tuple[int, int]]]:
    """Notebook id -> the ``(kg_reset_epoch, kg_mutation_seq)`` keys it has in
    the world's SQLite files (``(0, 0)`` without a state row, as the memo's
    ``_version_key`` reads it)."""
    versions: dict[str, set[tuple[int, int]]] = {}
    for path in _sqlite_files(root):
        connection = sqlite3.connect(str(path))
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "notebooks" not in tables:
                continue
            state: dict[str, tuple[int, int]] = {}
            if "unified_kg_state" in tables:
                for notebook_id, seq, epoch in connection.execute(
                    "SELECT notebook_id, kg_mutation_seq, kg_reset_epoch "
                    "FROM unified_kg_state"
                ):
                    state[notebook_id] = (int(epoch or 0), int(seq or 0))
            for (notebook_id,) in connection.execute("SELECT id FROM notebooks"):
                versions.setdefault(notebook_id, set()).add(
                    state.get(notebook_id, (0, 0))
                )
        finally:
            connection.close()
    return versions


def _from_world_history(
    version: tuple[int, int], world_versions: set[tuple[int, int]]
) -> bool:
    """A memo version this world's own history can have produced.

    ``(kg_reset_epoch, kg_mutation_seq)`` only grows, so a key at or below
    the pristine database's version was written while the build moved the
    world to it -- e.g. counts read at import time, before the projection
    bumped the seq. A self-built world started its tests with exactly that
    stale-but-present entry (a correct gate treats it as a miss; a gate that
    ignores the version serves it, which is what tests must keep catching).
    A key above every version of the id in the world cannot be the world's.
    """
    return any(version <= current for current in world_versions)


def _warm_entries(
    before: dict[str, Any], after: dict[str, Any], build_end: float, root: Path
) -> tuple[_WarmEntry, ...]:
    """The build's own entries for the world's own ids, in cache order."""
    versions = _world_notebook_versions(root)
    warm: list[_WarmEntry] = []
    for entry, current in after.items():
        policy = _RESTORE_POLICY[entry]
        previous = before[entry]
        if policy == MEMBER:
            for key in sorted(current - previous):
                if key in versions:
                    warm.append(_WarmEntry(entry, key, None))
            continue
        for key, value in current.items():
            if key not in versions or (key in previous and previous[key] is value):
                continue
            if policy == VERSION_GATED:
                if _from_world_history(tuple(value[0]), versions[key]):
                    warm.append(_WarmEntry(entry, key, copy.deepcopy(value)))
            else:
                warm.append(_WarmEntry(entry, key, build_end - value))
    return tuple(warm)


def _restore_warm_entries(warm: tuple[_WarmEntry, ...]) -> None:
    now = _monotonic()
    for item in warm:
        module_name, name = item.entry.split(":")
        module = importlib.import_module(module_name)
        policy = _RESTORE_POLICY[item.entry]
        with getattr(module, _CONTAINER_LOCKS[module_name]):
            container = getattr(module, name)
            if policy == MEMBER:
                container.add(item.key)
            elif policy == VERSION_GATED:
                container[item.key] = copy.deepcopy(item.value)
            else:
                container[item.key] = now - item.value


# Searched by their literal right-hand side first (a ``^``-anchored multiline
# pattern costs ~6x more over the 16 MB tree), then the line head must be a
# bare module-level ``name`` / ``name: annotation``.
_EMPTY_CONTAINER = re.compile(
    r"=[ \t]*(?:\{\}|dict\(\)|set\(\)|\[\]|list\(\)"
    r"|(?:collections\.)?(?:OrderedDict|defaultdict|deque)\("
    r"|(?:weakref\.)?(?:WeakKeyDictionary|WeakValueDictionary|WeakSet)\()"
)
_MODULE_LEVEL_TARGET = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)[ \t]*(?::[^=\n]*)?")
_FUNCTOOLS_DECORATOR = re.compile(r"@(?:functools\.)?(?:lru_cache|cache)\b")
_DECORATED_DEF = re.compile(
    r"[^\n]*\n(?:[ \t]*@[^\n]*\n)*[ \t]*(?:async[ \t]+)?def[ \t]+([A-Za-z_][A-Za-z0-9_]*)"
)


def scan_process_caches(app_root: Path) -> set[str]:
    """Text scan of ``app_root`` for module-level cache containers and
    ``functools`` caches (any indentation), as ``package.module:name``."""
    found: set[str] = set()
    package_root = app_root.parent
    for path in sorted(app_root.rglob("*.py")):
        relative = path.relative_to(package_root).with_suffix("")
        parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
        module = ".".join(parts)
        source = path.read_text(encoding="utf-8")
        for match in _EMPTY_CONTAINER.finditer(source):
            line_start = source.rfind("\n", 0, match.start()) + 1
            target = _MODULE_LEVEL_TARGET.fullmatch(source, line_start, match.start())
            if target is not None:
                found.add(f"{module}:{target.group(1)}")
        if "cache" not in source:
            continue
        for match in _FUNCTOOLS_DECORATOR.finditer(source):
            line_start = source.rfind("\n", 0, match.start()) + 1
            if source[line_start : match.start()].strip(" \t"):
                continue  # not a decorator line
            definition = _DECORATED_DEF.match(source, match.end())
            if definition is not None:
                found.add(f"{module}:{definition.group(1)}")
    return found


# --------------------------------------------------------------------------
# Digest, path scan, closing
# --------------------------------------------------------------------------


def _is_sqlite_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER
    except OSError:
        return False


def _world_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


def _sqlite_files(root: Path) -> list[Path]:
    return [path for path in _world_files(root) if _is_sqlite_file(path)]


def _masked_row(table: str, columns: list[str], row: tuple) -> tuple:
    rewrite = OPEN_TIME_REWRITES.get(table)
    if rewrite is None:
        return row
    key_column, key_value, masked = rewrite
    if key_column not in columns or row[columns.index(key_column)] != key_value:
        return row
    return tuple(
        "<rewritten on open>" if column in masked else value
        for column, value in zip(columns, row)
    )


def _contains(value: Any, needles: tuple[bytes, ...]) -> bool:
    if isinstance(value, str):
        data = value.encode("utf-8", "surrogatepass")
    elif isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
    else:
        return False
    return any(needle in data for needle in needles)


def _scan_sqlite(
    path: Path, needles: tuple[bytes, ...] = ()
) -> tuple[dict[str, str], list[str]]:
    """Per-table digests (plus ``<schema>``) and ``table.column`` path hits."""
    digests: dict[str, str] = {}
    hits: list[str] = []
    connection = sqlite3.connect(str(path))
    try:
        schema = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "ORDER BY type, name"
        ).fetchall()
        digests["<schema>"] = hashlib.sha256(repr(schema).encode()).hexdigest()
        # The persistent header fields: the migrator stamps ``user_version``
        # (a reopen that only bumps it changes no row) and the database keeps
        # WAL mode in its header; ``application_id`` is unused by the app but
        # equally persistent. Per-connection pragmas are not world state.
        digests["<header>"] = hashlib.sha256(
            repr(
                tuple(
                    connection.execute(f"PRAGMA {name}").fetchone()[0]
                    for name in _HEADER_PRAGMAS
                )
            ).encode()
        ).hexdigest()
        tables = [
            name
            for kind, name, _table, sql in schema
            if kind == "table"
            and not str(sql or "").lstrip().upper().startswith("CREATE VIRTUAL")
        ]
        for table in tables:
            cursor = connection.execute(f'SELECT * FROM "{table}"')
            columns = [description[0] for description in cursor.description]
            row_hashes = []
            for row in cursor:
                if needles:
                    for column, value in zip(columns, row):
                        if _contains(value, needles):
                            hits.append(f"{table}.{column}")
                row_hashes.append(
                    hashlib.sha256(
                        repr(_masked_row(table, columns, row)).encode()
                    ).hexdigest()
                )
            row_hashes.sort()
            digests[table] = hashlib.sha256(
                "\n".join(row_hashes).encode()
            ).hexdigest()
    finally:
        connection.close()
    return digests, sorted(set(hits))


def _world_digests(root: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    for path in _world_files(root):
        relative = path.relative_to(root).as_posix()
        if relative.endswith(_SIDE_FILE_SUFFIXES):
            # Side files of a closed database must be absent or empty; their
            # content belongs to the main file after the checkpoint.
            if path.stat().st_size:
                raise WorldTemplateError(
                    f"{relative} is not empty; close the world before digesting it"
                )
            continue
        if _is_sqlite_file(path):
            table_digests, _hits = _scan_sqlite(path)
            for table, digest in table_digests.items():
                digests[f"{relative}::{table}"] = digest
        else:
            digests[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def _combined(digests: Mapping[str, str]) -> str:
    return hashlib.sha256(json.dumps(sorted(digests.items())).encode()).hexdigest()


def world_digest(root: Path) -> str:
    """One digest of a closed world: every real table's sorted rows, the
    schema, and every other file's bytes (open-time rewrites masked)."""
    return _combined(_world_digests(Path(root)))


def _root_needles(root: Path) -> tuple[bytes, ...]:
    variants = {str(root), os.path.realpath(root)}
    for variant in list(variants):
        if variant.startswith("/private/"):
            variants.add(variant[len("/private"):])
    return tuple(sorted(variant.encode("utf-8") for variant in variants))


def _embedded_root_paths(root: Path, payload: Any) -> list[str]:
    needles = _root_needles(root)
    found: list[str] = []
    for path in _world_files(root):
        relative = path.relative_to(root).as_posix()
        if _is_sqlite_file(path):
            _digests, hits = _scan_sqlite(path, needles)
            found.extend(f"{relative}::{hit}" for hit in hits)
        elif any(needle in path.read_bytes() for needle in needles):
            found.append(relative)
    # Walk the strings themselves: ``json.dumps`` escapes non-ASCII, so a
    # root such as ``.../世界/...`` would never match its UTF-8 needle there.
    found.extend(
        f"<payload>{where}"
        for where, text in _payload_strings(payload)
        if _contains(text, needles)
    )
    return found


def _payload_strings(value: Any, where: str = "") -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(where, value)]
    if isinstance(value, (list, tuple)):
        return [
            item
            for index, element in enumerate(value)
            for item in _payload_strings(element, f"{where}[{index}]")
        ]
    if isinstance(value, dict):
        return [
            item
            for key, element in value.items()
            for item in [(f"{where}<key>", key), *_payload_strings(element, f"{where}[{key!r}]")]
        ]
    return []


def _open_paths_under(root: Path) -> list[str]:
    """Paths under ``root`` that this process still holds a descriptor for."""
    prefixes = tuple(
        needle.decode("utf-8").rstrip("/") + "/" for needle in _root_needles(root)
    )
    if os.path.isdir("/proc/self/fd"):  # Linux (CI)
        fd_dir = "/proc/self/fd"

        def resolve(fd: int) -> str:
            return os.readlink(f"{fd_dir}/{fd}")

    elif sys.platform == "darwin":  # macOS (local gate)
        import fcntl

        fd_dir = "/dev/fd"

        def resolve(fd: int) -> str:
            raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
            return raw.split(b"\0", 1)[0].decode("utf-8", "surrogateescape")

    else:
        raise WorldTemplateError(
            f"no descriptor table to prove the world is closed on {sys.platform}"
        )
    held: list[str] = []
    for name in os.listdir(fd_dir):
        try:
            target = resolve(int(name))
        except (OSError, ValueError):
            continue
        if target.startswith(prefixes):
            held.append(target)
    return sorted(set(held))


def _close_process_singletons() -> None:
    from app.api import deps
    from app.core.config import get_settings

    try:
        if deps.repository.cache_info().currsize:
            deps.repository().close()
    finally:
        deps.repository.cache_clear()
        get_settings.cache_clear()


def _close_and_flush(root: Path, stage: str) -> None:
    """Checkpoint every SQLite file and prove nothing still holds the world.

    An unreachable repository still waiting for the cycle collector holds its
    connection open; one ``gc.collect()`` (~0.1 s with the app imported) is
    paid only when the first attempt finds something held.
    """
    _close_process_singletons()
    try:
        _checkpoint_and_prove_closed(root, stage)
    except WorldTemplateError:
        gc.collect()
        _checkpoint_and_prove_closed(root, stage)
    for path in _world_files(root):
        if path.name.endswith(_SIDE_FILE_SUFFIXES) and not path.stat().st_size:
            path.unlink()


def _checkpoint_and_prove_closed(root: Path, stage: str) -> None:
    for path in _sqlite_files(root):
        # timeout=0: report a pinned reader instead of waiting the 5 s default.
        connection = sqlite3.connect(str(path), timeout=0)
        try:
            busy, _log, _done = connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
        finally:
            connection.close()
        wal = path.with_name(path.name + "-wal")
        wal_size = wal.stat().st_size if wal.exists() else 0
        if busy or wal_size:
            raise WorldTemplateError(
                f"{stage}: WAL of {path.relative_to(root)} could not be "
                f"checkpointed (busy={busy}, wal bytes={wal_size}); a "
                "connection still holds a read snapshot -- close every "
                "repository the build opened"
            )
    held = _open_paths_under(root)
    if held:
        raise WorldTemplateError(
            f"{stage}: this process still holds file descriptors inside the "
            f"world: {held}; close every repository/connection the build opened"
        )


def _close_returned(handle: Any) -> None:
    close = getattr(handle, "close", None)
    if handle is not None and callable(close):
        close()


# --------------------------------------------------------------------------
# Payload, environment, integrity
# --------------------------------------------------------------------------


_SCALARS = (type(None), bool, int, float, str)


def _assert_pure_data(value: Any, where: str = "payload") -> None:
    kind = type(value)
    if kind in _SCALARS:
        return
    if kind in (list, tuple):
        for index, item in enumerate(value):
            _assert_pure_data(item, f"{where}[{index}]")
        return
    if kind is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise WorldTemplateError(f"{where} has a non-str key {key!r}")
            _assert_pure_data(item, f"{where}[{key!r}]")
        return
    raise WorldTemplateError(
        f"{where} is a {kind.__name__}, not pure data; return ids/strings/"
        "tuples and rebuild live objects per test"
    )


def _listified(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return [_listified(item) for item in value]
    if isinstance(value, dict):
        return {key: _listified(item) for key, item in value.items()}
    return value


def _check_payload(payload: Any) -> None:
    _assert_pure_data(payload)
    try:
        round_trip = json.loads(json.dumps(payload))
    except (TypeError, ValueError) as error:
        raise WorldTemplateError(f"payload is not JSON data: {error}") from error
    if round_trip != _listified(payload):
        raise WorldTemplateError("payload does not survive a JSON round trip")


def _apply_build_env(
    patch: pytest.MonkeyPatch, root: Path, logs: Path, env: Mapping[str, str]
) -> None:
    for name in list(os.environ):
        if name not in _BASELINE_ENV:
            patch.delenv(name)
    for name, value in _BASELINE_ENV.items():
        if os.environ.get(name) != value:
            patch.setenv(name, value)
    patch.setenv("DATABASE_URL", f"sqlite:///{root / WORLD_DB_NAME}")
    patch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(root / WORLD_STORAGE_NAME))
    patch.setenv("EVENT_LOG_DIR", str(logs))
    patch.setenv("LLM_LOG_PATH", str(logs / "llm.jsonl"))
    for name, value in env.items():
        patch.setenv(name, value)


def _file_state(root: Path) -> tuple[frozenset[str], dict[str, tuple[int, int]]]:
    directories: set[str] = set()
    files: dict[str, tuple[int, int]] = {}
    for current, dir_names, file_names in os.walk(root):
        base = Path(current)
        for name in dir_names:
            directories.add((base / name).relative_to(root).as_posix())
        for name in file_names:
            path = base / name
            stat = path.stat()
            files[path.relative_to(root).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return frozenset(directories), files


def world_env(root: Path) -> dict[str, str]:
    """The database/storage variables a test sets to open its copy."""
    return {
        "DATABASE_URL": f"sqlite:///{Path(root) / WORLD_DB_NAME}",
        "SILICON_NOTEBOOK_STORAGE_DIR": str(Path(root) / WORLD_STORAGE_NAME),
    }


# --------------------------------------------------------------------------
# Template
# --------------------------------------------------------------------------


@dataclass
class WorldTemplate:
    key: str
    layout: str
    directory: Path
    pristine: Path
    live: Path | None
    payload: Any
    digest: str
    _integrity: tuple[frozenset[str], dict[str, tuple[int, int]]] = field(repr=False)
    warm: tuple[_WarmEntry, ...] = ()

    def verify_pristine(self) -> None:
        directories, files = _file_state(self.pristine)
        expected_directories, expected_files = self._integrity
        if directories == expected_directories and files == expected_files:
            return
        changed = sorted(
            name
            for name in set(files) | set(expected_files)
            if files.get(name) != expected_files.get(name)
        )
        changed += sorted(directories ^ expected_directories)
        raise WorldTemplateError(
            f"world template {self.key!r}: pristine copy was modified "
            f"({changed}); a test wrote through a path into the template"
        )

    def materialize(self, tmp_path: Path, name: str = "world") -> tuple[Path, Any]:
        """Reset identity-keyed caches to what the build left for this world,
        verify the template, copy it.

        ``relocatable`` copies into ``tmp_path / name``; ``pinned`` replaces
        the shared ``live`` directory the stored absolute paths point at.
        Returns ``(root, payload)`` with a private copy of the payload.
        """
        reset_identity_keyed_process_caches()
        _restore_warm_entries(self.warm)
        self.verify_pristine()
        if self.layout == PINNED:
            assert self.live is not None
            if self.live.exists():
                shutil.rmtree(self.live)
            shutil.copytree(self.pristine, self.live)
            root = self.live
        else:
            root = Path(tmp_path) / name
            if root.exists():
                raise WorldTemplateError(f"{root} already exists")
            shutil.copytree(self.pristine, root)
        return root, copy.deepcopy(self.payload)


_MATCH_DEPTH = 8


def _values_match(left: Any, right: Any, depth: int = 0) -> bool:
    """Equal values, or functions that are the same callback (see below).

    Anything whose ``==`` raises or is not a plain bool (arrays) only
    matches itself -- the conservative answer is "a different world".
    """
    if left is right:
        return True
    if callable(left) and callable(right):
        return _callables_match(left, right, depth + 1)
    if isinstance(left, (list, tuple)) and type(left) is type(right):
        return len(left) == len(right) and all(
            _values_match(a, b, depth + 1) for a, b in zip(left, right)
        )
    if isinstance(left, dict) and type(left) is type(right):
        return left.keys() == right.keys() and all(
            _values_match(left[name], right[name], depth + 1) for name in left
        )
    try:
        equal = left == right
    except Exception:  # noqa: BLE001 - incomparable means "not the same"
        return False
    return equal if type(equal) is bool else False


def _cell_contents(function: Any) -> tuple[Any, ...]:
    contents = []
    for cell in function.__closure__ or ():
        try:
            contents.append(cell.cell_contents)
        except ValueError:  # an empty cell
            contents.append(_EMPTY_CELL)
    return tuple(contents)


_EMPTY_CELL = object()


def _callables_match(left: Any, right: Any, depth: int = 0) -> bool:
    """Whether two callbacks build the same world.

    A qualified name is not enough: every closure from one factory, every
    lambda on one line and every bound method of one class share it. The
    same code object with equal defaults, equal closure-cell contents and an
    equal bound ``__self__`` is the same callback, so a fixture that recreates
    an identical closure per test still reuses its world; anything else that
    is not the very same object is a different world.
    """
    if left is right:
        return True
    if depth > _MATCH_DEPTH:
        return False
    if isinstance(left, functools.partial) or isinstance(right, functools.partial):
        return (
            isinstance(left, functools.partial)
            and isinstance(right, functools.partial)
            and _callables_match(left.func, right.func, depth + 1)
            and _values_match(left.args, right.args, depth + 1)
            and _values_match(left.keywords, right.keywords, depth + 1)
        )
    left_function = getattr(left, "__func__", None)
    right_function = getattr(right, "__func__", None)
    if left_function is not None or right_function is not None:  # bound methods
        return (
            left_function is not None
            and right_function is not None
            and _values_match(left.__self__, right.__self__, depth + 1)
            and _callables_match(left_function, right_function, depth + 1)
        )
    left_code = getattr(left, "__code__", None)
    right_code = getattr(right, "__code__", None)
    if left_code is None or right_code is None or left_code != right_code:
        return False
    return (
        _values_match(left.__defaults__, right.__defaults__, depth + 1)
        and _values_match(left.__kwdefaults__, right.__kwdefaults__, depth + 1)
        and _values_match(_cell_contents(left), _cell_contents(right), depth + 1)
    )


@dataclass(frozen=True)
class _Spec:
    layout: str
    env: tuple[tuple[str, str], ...]
    build: str
    reopen: str
    build_callable: Callable[[Path], Any] = field(compare=False, repr=False)
    reopen_callable: Callable[[Path], Any] = field(compare=False, repr=False)

    def matches(self, other: "_Spec") -> bool:
        return (
            self == other
            and _callables_match(self.build_callable, other.build_callable)
            and _callables_match(self.reopen_callable, other.reopen_callable)
        )


_TEMPLATES: dict[tuple[str, str], tuple[_Spec, WorldTemplate]] = {}


def _callable_name(function: Callable[..., Any]) -> str:
    return f"{getattr(function, '__module__', '?')}.{getattr(function, '__qualname__', repr(function))}"


def _slug(key: str, spec: _Spec) -> str:
    readable = re.sub(r"[^A-Za-z0-9]+", "-", key).strip("-")[:40] or "world"
    identity = json.dumps([key, spec.layout, spec.env, spec.build, spec.reopen])
    return f"{readable}-{hashlib.sha256(identity.encode()).hexdigest()[:10]}"


def world_template(
    tmp_path_factory: pytest.TempPathFactory,
    key: str,
    build: Callable[[Path], Any],
    reopen: Callable[[Path], Any],
    *,
    layout: str = RELOCATABLE,
    env: Mapping[str, str] | None = None,
) -> WorldTemplate:
    """Return the process-wide template for ``key``, building it on first use."""
    if layout not in _LAYOUTS:
        raise ValueError(f"layout must be one of {_LAYOUTS}, not {layout!r}")
    env = dict(env or {})
    for name, value in env.items():
        if type(name) is not str or type(value) is not str:
            raise WorldTemplateError(f"env {name!r}={value!r} must be str to str")
    spec = _Spec(
        layout=layout,
        env=tuple(sorted(env.items())),
        build=_callable_name(build),
        reopen=_callable_name(reopen),
        build_callable=build,
        reopen_callable=reopen,
    )
    base = tmp_path_factory.getbasetemp()
    registry_key = (str(base), key)
    cached = _TEMPLATES.get(registry_key)
    if cached is not None:
        cached_spec, template = cached
        if not cached_spec.matches(spec):
            raise WorldTemplateError(
                f"world key {key!r} was already built as {cached_spec} by "
                f"different callbacks or settings; refusing to reuse it for "
                f"{spec} (fold the difference into the key)"
            )
        return template

    from app.api import deps

    if deps.repository.cache_info().currsize:
        raise WorldTemplateError(
            f"world {key!r}: deps.repository is already cached for this test; "
            "request the world fixture before anything that opens the app repository"
        )

    directory = base / "worlds" / _slug(key, spec)
    if directory.exists():
        shutil.rmtree(directory)
    pristine = directory / "pristine"
    live = directory / "live" if layout == PINNED else None
    root = live if live is not None else pristine
    logs = directory / "build-logs"
    root.mkdir(parents=True)
    try:
        template = _build(key, spec, env, build, reopen, directory, pristine, live, root, logs)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    _TEMPLATES[registry_key] = (spec, template)
    return template


def _build(
    key: str,
    spec: _Spec,
    env: Mapping[str, str],
    build: Callable[[Path], Any],
    reopen: Callable[[Path], Any],
    directory: Path,
    pristine: Path,
    live: Path | None,
    root: Path,
    logs: Path,
) -> WorldTemplate:
    from app.core.config import get_settings

    warm_before = _restorable_state()
    with pytest.MonkeyPatch.context() as patch:
        _apply_build_env(patch, root, logs, env)
        get_settings.cache_clear()
        try:
            payload = build(root)
        finally:
            _close_process_singletons()
    warm_after = _restorable_state()
    build_end = _monotonic()
    _check_payload(payload)
    _close_and_flush(root, f"world {key!r} build")
    warm = _warm_entries(warm_before, warm_after, build_end, root)
    before = _world_digests(root)

    with pytest.MonkeyPatch.context() as patch:
        _apply_build_env(patch, root, logs, env)
        get_settings.cache_clear()
        try:
            _close_returned(reopen(root))
        finally:
            _close_process_singletons()
    _close_and_flush(root, f"world {key!r} reopen")
    after = _world_digests(root)
    if after != before:
        changed = sorted(
            name
            for name in set(before) | set(after)
            if before.get(name) != after.get(name)
        )
        raise WorldTemplateError(
            f"world {key!r} is not reopen-stable: opening it the way a test "
            f"does rewrote {changed}; build the world in its post-open state"
        )

    if spec.layout == RELOCATABLE:
        embedded = _embedded_root_paths(root, payload)
        if embedded:
            raise WorldTemplateError(
                f"world {key!r} is laid out as relocatable but embeds its build "
                f"root {root} in {embedded}; use layout='pinned'"
            )
    else:
        shutil.copytree(root, pristine)

    return WorldTemplate(
        key=key,
        layout=spec.layout,
        directory=directory,
        pristine=pristine,
        live=live,
        payload=copy.deepcopy(payload),
        digest=_combined(after),
        _integrity=_file_state(pristine),
        warm=warm,
    )
