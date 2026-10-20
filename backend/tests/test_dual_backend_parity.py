"""Mechanical half of the dual-backend capability matrix (architecture.md 2.2.2).

SQLite is kept long-term beside PostgreSQL (product decision 2026-10-19), with
PostgreSQL as the canonical definition. Semantic parity stays with each
feature's SQLite test and its PostgreSQL twin under ``tests/postgres/``; this
file pins the facts that can be read straight from the code, without opening a
database or starting a process:

* the schema-version pair is the same number in all three places, and both
  migration sequences are gap-free;
* every member of every bundle seat's port is implemented by both stores
  (instance attributes bound in a constructor anywhere in the MRO count);
* the two stores' class-level public members, excluding instance attributes,
  differ only by the registered backend-only helpers below (a new one must be
  registered; a stale entry fails). Instance attributes are composition wiring
  (database, settings, collaborating stores); the ones a port declares are
  already covered by the port check above;
* every code reference in the architecture matrix still resolves, and every
  row names a distinct capability.

The reference check proves only that a citation still exists (``.py``: an AST
definition; other files: a substring; a bare path: the file). It does not prove
the row's description is right: a PR that changes a cited symbol's behavior
re-checks that row by hand.
"""
from __future__ import annotations

import ast
import inspect
import re
import textwrap
import typing
from functools import cache
from pathlib import Path
from typing import get_type_hints


ROOT = Path(__file__).resolve().parents[2]
ARCHITECTURE = ROOT / "architecture.md"
MATRIX_START = "### 2.2.2 双后端长期并存与能力对等矩阵"
MATRIX_END = "### 2.3 "

# Public members only one backend's store exposes, by bundle seat. Each is a
# backend-private helper no backend-neutral caller reaches through the port.
REGISTERED_BACKEND_ONLY = {
    ("database", "sqlite"): {
        "begin_immediate": "cross-process writer fence; called mostly by the "
        "SQLite stores themselves, plus SQLite-only branches (auth lock, sync)",
        "close_local": "drops the per-thread reused connection; PostgreSQL pools",
    },
    ("database", "postgres"): {
        "offline_maintenance_session": "database-level advisory lock for offline "
        "mutation phases (--confirm-service-stopped)",
    },
    ("sources", "postgres"): {
        "COUNT_IN_CHUNK": "batch width of the PostgreSQL per-source count probe",
    },
    ("embeddings", "postgres"): {
        "vector_pages": "keyset-paged vector read of the offline scale build",
    },
    ("memory", "postgres"): {
        "memory_rows_lock_sql": "FOR SHARE lock statement of the share transaction",
    },
    ("unified_kg", "postgres"): {
        "stream_scratch_rows": "server-side streaming read of cluster scratch rows",
    },
}


@cache
def _seats() -> dict[str, tuple[type, type, type]]:
    from app.repositories.bundle import PersistenceBundle
    from app.repositories.postgres.bundle import PostgresPersistenceBundle
    from app.repositories.sqlite.bundle import SqlitePersistenceBundle

    ports = get_type_hints(PersistenceBundle)
    sqlite = get_type_hints(SqlitePersistenceBundle)
    postgres = get_type_hints(PostgresPersistenceBundle)
    assert set(sqlite) == set(ports) == set(postgres)
    return {seat: (ports[seat], sqlite[seat], postgres[seat]) for seat in ports}


def _assigned_in_init(cls: type) -> set[str]:
    """Names any constructor in the MRO binds on ``self`` (instance data members)."""
    names: set[str] = set()
    for klass in cls.__mro__:
        if klass is not object:
            names |= _assigned_in_own_init(klass)
    return names


def _assigned_in_own_init(cls: type) -> set[str]:
    try:
        source = inspect.getsource(cls)
    except (OSError, TypeError):  # builtin / C-level base: binds nothing in Python
        return set()
    tree = ast.parse(textwrap.dedent(source))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "__init__":
            for inner in ast.walk(node):
                targets = (
                    inner.targets if isinstance(inner, ast.Assign)
                    else [inner.target] if isinstance(inner, ast.AnnAssign)
                    else []
                )
                for target in targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                    ):
                        names.add(target.attr)
    return names


def _missing_members(port: type, store: type) -> set[str]:
    missing = {name for name in typing.get_protocol_members(port) if not hasattr(store, name)}
    return missing - _assigned_in_init(store) if missing else missing


def _public(cls: type) -> set[str]:
    """Class-level public members (``dir`` of the class: no instance attributes)."""
    return {name for name in dir(cls) if not name.startswith("_")}


def test_schema_version_pair_is_one_number_in_every_owner():
    from app.migration.shadow.manifest import RUNNING_SCHEMA_PAIR
    from app.repositories.postgres.migrator import load_migrations
    from app.repositories.postgres.schema_manifest import POSTGRES_SCHEMA_MANIFEST
    from app.repositories.sqlite.migrations import SCHEMA_VERSION, SqliteMigrator

    manifest = POSTGRES_SCHEMA_MANIFEST
    assert SCHEMA_VERSION == manifest.sqlite_version == RUNNING_SCHEMA_PAIR.sqlite_version
    assert manifest.postgres_version == RUNNING_SCHEMA_PAIR.postgres_version

    packaged = load_migrations(ROOT / "backend/app/repositories/postgres/migrations")
    assert [migration.version for migration in packaged] == list(
        range(1, manifest.postgres_version + 1)
    )
    sqlite_steps = {
        int(match.group(1))
        for name in dir(SqliteMigrator)
        if (match := re.fullmatch(r"_migration_(\d+)", name))
    }
    assert sqlite_steps == set(range(1, SCHEMA_VERSION + 1))


def test_every_port_member_is_implemented_by_both_backend_stores():
    gaps = {}
    for seat, (port, sqlite_store, postgres_store) in _seats().items():
        for backend, store in (("sqlite", sqlite_store), ("postgres", postgres_store)):
            if missing := _missing_members(port, store):
                gaps[(seat, backend)] = sorted(missing)
    assert gaps == {}


def test_store_public_surfaces_differ_only_by_registered_backend_helpers():
    actual = {}
    for seat, (_port, sqlite_store, postgres_store) in _seats().items():
        sqlite_public, postgres_public = _public(sqlite_store), _public(postgres_store)
        if only := sqlite_public - postgres_public:
            actual[(seat, "sqlite")] = only
        if only := postgres_public - sqlite_public:
            actual[(seat, "postgres")] = only
    registered = {key: set(reasons) for key, reasons in REGISTERED_BACKEND_ONLY.items()}
    assert actual == registered


def test_missing_member_detection_sees_methods_and_instance_attributes():
    class Port(typing.Protocol):
        handle: object

        def read(self) -> None: ...

    class Complete:
        def __init__(self) -> None:
            self.handle = object()

        def read(self) -> None:
            return None

    class Incomplete:
        def __init__(self) -> None:
            self.other = object()

    class Inherited(Complete):
        """Binds ``handle`` only through the base class constructor."""

    assert _missing_members(Port, Complete) == set()
    assert _missing_members(Port, Inherited) == set()
    assert _missing_members(Port, Incomplete) == {"handle", "read"}


def _matrix_section() -> str:
    text = ARCHITECTURE.read_text(encoding="utf-8")
    assert MATRIX_START in text, "architecture.md lost the dual-backend matrix section"
    return text.split(MATRIX_START, 1)[1].split(MATRIX_END, 1)[0]


def _matrix_references(section: str) -> list[tuple[str, list[str]]]:
    """(capability, cited references) per row, in document order. A list, so a
    repeated capability name is visible instead of overwriting its twin."""
    rows = [
        line for line in section.splitlines()
        if line.startswith("|") and not re.fullmatch(r"\|[-|\s]+\|", line)
    ][1:]  # header row
    references = []
    for row in rows:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        references.append((cells[0], re.findall(r"`([^`]+)`", cells[-1])))
    return references


def _defines(path: Path, dotted: str) -> bool:
    scope: list[ast.stmt] = ast.parse(path.read_text(encoding="utf-8")).body
    for part in dotted.split("."):
        found = None
        for node in scope:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == part:
                    found = node
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(t, ast.Name) and t.id == part for t in targets):
                    found = node
        if found is None:
            return False
        scope = getattr(found, "body", [])
    return True


def _unresolved(reference: str) -> str | None:
    path_text, _, symbol = reference.partition(":")
    path = ROOT / path_text
    if not path.is_file():
        return "missing file"
    if not symbol:
        return None
    if path.suffix == ".py":
        return None if _defines(path, symbol) else "missing symbol"
    return None if symbol in path.read_text(encoding="utf-8") else "missing identifier"


def test_every_matrix_row_cites_code_that_still_exists():
    rows = _matrix_references(_matrix_section())
    assert len(rows) >= 10, rows
    capabilities = [capability for capability, _refs in rows]
    assert len(capabilities) == len(set(capabilities)), capabilities
    assert [capability for capability, refs in rows if not refs] == []
    broken = {
        reference: reason
        for _capability, refs in rows
        for reference in refs
        if (reason := _unresolved(reference))
    }
    assert broken == {}


def test_matrix_parsing_keeps_repeated_capability_rows():
    section = (
        "| 能力 | PG | SQLite | 差异 | 出处 |\n|---|---|---|---|---|\n"
        "| 词法 | a | b | c | `x.py:f` |\n| 词法 | a | b | c | `y.py:g` |\n"
    )
    assert _matrix_references(section) == [("词法", ["x.py:f"]), ("词法", ["y.py:g"])]


def test_reference_resolution_rejects_stale_symbols_and_files():
    assert _unresolved("backend/app/repositories/sqlite/database.py:SqliteDatabase.write") is None
    assert _unresolved("backend/app/repositories/sqlite/database.py:SqliteDatabase.nope")
    assert _unresolved("backend/app/repositories/sqlite/no_such_module.py")
    assert _unresolved(
        "backend/app/repositories/postgres/migrations/0006_search_gin.sql:no_such_index"
    )
