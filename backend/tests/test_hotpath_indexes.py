"""Unit tests for ``app/repositories/postgres/hotpath_indexes.py`` and
``scripts/build_hotpath_indexes.py`` (fake-connection only, no live PG —
G1-tier, hence living at the main test root rather than under
``backend/tests/postgres/``, same placement rule as
``test_postgres_embedding_matrix_keyset.py``).

Contract under test:

  1. Anti-drift — the eight index definitions in ``HOTPATH_INDEX_SPECS``
     (six query-family groups) are parsed straight out of
     ``migrations/0039_hotpath_batch1_indexes.sql`` and compared
     statement-by-statement against the module's own specs, so the two
     hand-authored copies (a migration file cannot import Python) cannot
     silently diverge.
  2. Statement shape — every DDL this module ever emits is a single
     ``CREATE INDEX [CONCURRENTLY] IF NOT EXISTS`` statement (apply mode
     never emits anything else; catalog reads are plain ``SELECT``).
  3. ``inspect_hotpath_indexes`` classifies each index as 存在 (ready) /
     缺失 (missing) / INVALID / UNEXPECTED (same name, different columns or
     predicate) against a fake ``pg_index``/``pg_class`` catalog, with no
     advisory lock taken.
  4. ``install_hotpath_indexes`` builds missing indexes one at a time with
     ``CONCURRENTLY``, skips ones already ready, fails closed on an
     UNEXPECTED (differently-shaped, same-named) index without touching it,
     and on an INVALID index raises with the manual
     ``DROP INDEX CONCURRENTLY`` guidance instead of auto-dropping it.
  5. ``_connect`` always opens the real ``psycopg.connect`` with
     ``autocommit=True`` — required for ``CREATE INDEX CONCURRENTLY`` to run
     outside a transaction — and a generic build failure's message names the
     in-flight index plus the PostgreSQL SQLSTATE, never the raw exception
     text (which can echo SQL/data). See ``backend/tests/postgres/
     test_hotpath_indexes_live.py`` for the live-PostgreSQL half of this
     contract (real ``CONCURRENTLY`` execution, real catalog shape
     comparison) that a fake connection cannot exercise.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.repositories.postgres.hotpath_indexes import (
    HOTPATH_INDEX_SPECS,
    HotpathIndexError,
    HotpathIndexSpec,
    inspect_hotpath_indexes,
    install_hotpath_indexes,
)
import app.repositories.postgres.hotpath_indexes as hotpath_indexes_module


_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION = (
    _REPO_ROOT
    / "backend"
    / "app"
    / "repositories"
    / "postgres"
    / "migrations"
    / "0039_hotpath_batch1_indexes.sql"
)

_STATEMENT_PATTERN = re.compile(
    r"CREATE INDEX IF NOT EXISTS\s+(?P<name>\w+)\s+ON\s+(?P<table>\w+)\(\s*"
    r"(?P<columns>[\s\S]*?)\s*\)(?:\s*WHERE\s+(?P<predicate>[\s\S]*?))?;",
    re.MULTILINE,
)


def _parse_migration_specs() -> list[dict[str, object]]:
    text = _MIGRATION.read_text(encoding="utf-8")
    out = []
    for match in _STATEMENT_PATTERN.finditer(text):
        columns = tuple(c.strip() for c in match.group("columns").split(","))
        predicate = (match.group("predicate") or "").strip()
        out.append(
            {
                "name": match.group("name"),
                "table": match.group("table"),
                "columns": columns,
                "predicate": predicate,
            }
        )
    return out


# ---------------------------------------------------------------------------
# 1. Anti-drift: migration file vs. HOTPATH_INDEX_SPECS
# ---------------------------------------------------------------------------


def test_migration_file_exists_and_is_parseable():
    assert _MIGRATION.is_file()
    parsed = _parse_migration_specs()
    assert len(parsed) == 8, (
        f"expected 8 CREATE INDEX statements in {_MIGRATION.name}, parsed {len(parsed)}"
    )


# The eight batch-1 names this migration (0039) alone is responsible for.
# HOTPATH_INDEX_SPECS grew a batch-2 addition (two more names, living in a
# separate migration file, 0041) with its own reconciliation test --
# see backend/tests/test_hotpath_indexes_batch2.py -- so this test's job is
# scoped to exactly these eight, not to HOTPATH_INDEX_SPECS's total size.
_BATCH1_NAMES = frozenset(
    {
        "idx_clusters_nb_canonical",
        "idx_clusters_nb_canonical_name_lower",
        "idx_extraction_runs_notebook",
        "idx_knowledge_source_fact_elements_notebook",
        "idx_memory_items_notebook",
        "idx_knowledge_relations_nb_source_target_edge",
        "idx_chunks_source_ordinal",
        "idx_sources_nb_hidden_type",
    }
)


def test_all_eight_migration_statements_match_a_spec_verbatim():
    parsed = _parse_migration_specs()
    by_name = {spec.name: spec for spec in HOTPATH_INDEX_SPECS}
    assert {entry["name"] for entry in parsed} == _BATCH1_NAMES, (
        "migration 0039 must declare exactly the eight batch-1 names, no more, "
        f"no less: parsed {[entry['name'] for entry in parsed]}"
    )
    seen = set()
    for entry in parsed:
        name = entry["name"]
        assert name in by_name, f"{name} is in the migration but not in HOTPATH_INDEX_SPECS"
        spec = by_name[name]
        seen.add(name)
        assert entry["table"] == spec.table, name
        assert entry["columns"] == spec.columns, name
        assert entry["predicate"] == spec.predicate, name
    assert seen == _BATCH1_NAMES


# ---------------------------------------------------------------------------
# 2. Statement shape
# ---------------------------------------------------------------------------


def test_every_spec_ddl_is_a_single_create_index_statement():
    for spec in HOTPATH_INDEX_SPECS:
        for concurrently in (False, True):
            text = spec.ddl("public", concurrently=concurrently).as_string(None)
            upper = text.strip().upper()
            assert upper.startswith("CREATE INDEX"), (spec.name, text)
            assert "IF NOT EXISTS" in upper, (spec.name, text)
            assert text.strip().count(";") == 0
            if concurrently:
                assert "CONCURRENTLY" in upper
            else:
                assert "CONCURRENTLY" not in upper
            for keyword in ("INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER", "GRANT"):
                assert keyword not in upper, (spec.name, keyword, text)


def test_index_names_are_unique():
    names = [spec.name for spec in HOTPATH_INDEX_SPECS]
    assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# 3/4. inspect / install against a fake pg_index catalog
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConnection:
    """Serves the three query shapes hotpath_indexes.py issues:
    ``pg_try_advisory_lock`` / ``set_config`` / ``pg_advisory_unlock`` (no-ops
    here), the ``pg_index``/``pg_class`` existence+validity probe, and
    ``CREATE INDEX [CONCURRENTLY] IF NOT EXISTS`` (recorded and reflected
    back into the fake catalog so a subsequent probe sees it as ready).
    """

    def __init__(self, catalog: dict[str, dict]):
        # Shared reference, not a copy: a real database's catalog persists
        # across separate connections, and install_hotpath_indexes opens one
        # connection to build indexes and a second (via the trailing
        # inspect_hotpath_indexes call) to read the result back.
        self.catalog = catalog
        self.calls: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params=None):
        text = statement.as_string(None) if hasattr(statement, "as_string") else str(statement)
        self.calls.append(text)
        upper = text.upper()
        if "PG_TRY_ADVISORY_LOCK" in upper:
            return _FakeResult({"locked": True})
        if "PG_ADVISORY_UNLOCK" in upper:
            return _FakeResult(None)
        if "SET_CONFIG" in upper:
            return _FakeResult(None)
        if "FROM PG_INDEX" in upper:
            schema, name = params
            row = self.catalog.get(name)
            if row is None or row.get("schema", "public") != schema:
                return _FakeResult(None)
            # Tests that only care about validity (存在/缺失/INVALID) build a
            # catalog row without "keys"/"predicate" — default those to the
            # real spec's own shape so they still compare as matching.
            spec = _spec_by_name(row["table"], name)
            default_keys = list(spec.columns) if spec else []
            default_predicate = spec.predicate_shape if spec else ""
            return _FakeResult(
                {
                    "index_name": name,
                    "table_name": row["table"],
                    "table_schema": row.get("schema", "public"),
                    "indisvalid": row.get("indisvalid", True),
                    "indisready": row.get("indisready", True),
                    "keys": row.get("keys", default_keys),
                    "predicate": row.get("predicate", default_predicate),
                }
            )
        if upper.startswith("CREATE INDEX"):
            match = re.search(r'"([A-Za-z0-9_]+)"\s+ON\s+"[^"]+"\."([A-Za-z0-9_]+)"', text)
            assert match, f"could not parse DDL: {text}"
            name, table = match.group(1), match.group(2)
            spec = _spec_by_name(table, name)
            self.catalog[name] = {
                "table": table,
                "indisvalid": True,
                "indisready": True,
                "keys": list(spec.columns) if spec else [],
                "predicate": spec.predicate_shape if spec else "",
            }
            return _FakeResult(None)
        raise AssertionError(f"unexpected statement issued: {text}")


def _spec_by_name(table: str, name: str) -> HotpathIndexSpec | None:
    for spec in HOTPATH_INDEX_SPECS:
        if spec.name == name and spec.table == table:
            return spec
    return None


@pytest.fixture
def fake_connect(monkeypatch):
    connections: list[_FakeConnection] = []

    def _install(catalog: dict[str, dict]):
        def _connect(_database_url):
            conn = _FakeConnection(catalog)
            connections.append(conn)
            return conn

        monkeypatch.setattr(hotpath_indexes_module, "_connect", _connect)
        return connections

    return _install


def test_inspect_reports_missing_ready_and_invalid(fake_connect):
    first = HOTPATH_INDEX_SPECS[0]
    second = HOTPATH_INDEX_SPECS[1]
    catalog = {
        first.name: {"table": first.table, "indisvalid": True, "indisready": True},
        second.name: {"table": second.table, "indisvalid": False, "indisready": False},
    }
    fake_connect(catalog)
    state = inspect_hotpath_indexes("postgresql://fake/db")
    by_name = {row["name"]: row["state"] for row in state["indexes"]}
    assert by_name[first.name] == "存在"
    assert by_name[second.name] == "INVALID"
    for spec in HOTPATH_INDEX_SPECS[2:]:
        assert by_name[spec.name] == "缺失"


def test_inspect_never_takes_the_advisory_lock(fake_connect):
    connections = fake_connect({})
    inspect_hotpath_indexes("postgresql://fake/db")
    calls = connections[0].calls
    assert not any("ADVISORY_LOCK" in call.upper() for call in calls)


def test_install_builds_only_missing_indexes_and_skips_ready_ones(fake_connect):
    ready = HOTPATH_INDEX_SPECS[0]
    catalog = {ready.name: {"table": ready.table, "indisvalid": True, "indisready": True}}
    connections = fake_connect(catalog)
    state = install_hotpath_indexes("postgresql://fake/db")
    assert all(row["state"] == "存在" for row in state["indexes"])
    create_calls = [c for c in connections[0].calls if c.upper().startswith("CREATE INDEX")]
    assert len(create_calls) == len(HOTPATH_INDEX_SPECS) - 1
    for call in create_calls:
        assert "CONCURRENTLY" in call.upper()
        assert f'"{ready.name}"' not in call


def test_install_raises_and_does_not_auto_drop_an_invalid_index(fake_connect):
    broken = HOTPATH_INDEX_SPECS[0]
    catalog = {broken.name: {"table": broken.table, "indisvalid": False, "indisready": False}}
    connections = fake_connect(catalog)
    with pytest.raises(HotpathIndexError, match="invalid_indexes_need_manual_drop"):
        install_hotpath_indexes("postgresql://fake/db")
    calls = connections[0].calls
    assert not any("DROP INDEX" in call.upper() for call in calls)
    # The other seven (still-missing) indexes still get built in the same run.
    create_calls = [c for c in calls if c.upper().startswith("CREATE INDEX")]
    assert len(create_calls) == len(HOTPATH_INDEX_SPECS) - 1


# ---------------------------------------------------------------------------
# 5. Same-named, differently-shaped owned index (P2-2) — fake-connection half.
#    The live-PostgreSQL half (real pg_get_indexdef/pg_get_expr rendering,
#    including PostgreSQL's own IN(...) -> = ANY(ARRAY[...]) canonicalization)
#    lives in backend/tests/postgres/test_hotpath_indexes_live.py.
# ---------------------------------------------------------------------------


def test_inspect_reports_unexpected_for_a_differently_shaped_owned_index(fake_connect):
    spec = HOTPATH_INDEX_SPECS[0]
    catalog = {
        spec.name: {
            "table": spec.table,
            "indisvalid": True,
            "indisready": True,
            "keys": ["some_other_column"],
            "predicate": "",
        }
    }
    fake_connect(catalog)
    state = inspect_hotpath_indexes("postgresql://fake/db")
    by_name = {row["name"]: row["state"] for row in state["indexes"]}
    assert by_name[spec.name] == "UNEXPECTED"


def test_install_rejects_a_differently_shaped_owned_index_before_building_others(
    fake_connect,
):
    spec = HOTPATH_INDEX_SPECS[0]
    catalog = {
        spec.name: {
            "table": spec.table,
            "indisvalid": True,
            "indisready": True,
            "keys": ["some_other_column"],
            "predicate": "",
        }
    }
    connections = fake_connect(catalog)
    with pytest.raises(
        HotpathIndexError, match=f"unexpected_index_definition:{spec.name}"
    ):
        install_hotpath_indexes("postgresql://fake/db")
    # Fail-closed, not skip-and-continue: nothing else gets built this run.
    create_calls = [
        c for c in connections[0].calls if c.upper().startswith("CREATE INDEX")
    ]
    assert create_calls == []


def test_a_matching_predicate_shape_is_not_flagged_unexpected(fake_connect):
    partial_spec = next(
        spec for spec in HOTPATH_INDEX_SPECS if spec.predicate_shape
    )
    catalog = {
        partial_spec.name: {
            "table": partial_spec.table,
            "indisvalid": True,
            "indisready": True,
            "keys": list(partial_spec.columns),
            "predicate": partial_spec.predicate_shape,
        }
    }
    fake_connect(catalog)
    state = inspect_hotpath_indexes("postgresql://fake/db")
    by_name = {row["name"]: row["state"] for row in state["indexes"]}
    assert by_name[partial_spec.name] == "存在"


# ---------------------------------------------------------------------------
# 6. Diagnostics (P2-1): a generic build failure names the in-flight index
#    plus the PostgreSQL SQLSTATE, never the raw exception text.
# ---------------------------------------------------------------------------


class _SqlStateError(Exception):
    def __init__(self, message: str, sqlstate: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


def test_install_failure_message_names_the_index_and_sqlstate_not_raw_text(
    fake_connect, monkeypatch,
):
    target = HOTPATH_INDEX_SPECS[2]
    connections = fake_connect({})
    original_execute = _FakeConnection.execute

    def _boom(self, statement, params=None):
        text = statement.as_string(None) if hasattr(statement, "as_string") else str(statement)
        if text.upper().startswith("CREATE INDEX") and f'"{target.name}"' in text:
            raise _SqlStateError("super secret query text leak", "42P01")
        return original_execute(self, statement, params)

    monkeypatch.setattr(_FakeConnection, "execute", _boom)
    with pytest.raises(HotpathIndexError) as excinfo:
        install_hotpath_indexes("postgresql://fake/db")
    message = str(excinfo.value)
    assert target.name in message
    assert "42P01" in message
    assert "super secret query text leak" not in message
    assert len(connections) == 1


# ---------------------------------------------------------------------------
# 7. Connection contract (P1): psycopg.connect must be given autocommit=True.
#    CREATE INDEX CONCURRENTLY cannot run inside a transaction, and none of
#    the fake-connection tests above ever exercise the real psycopg.connect
#    call, so this is the only place this kwarg is pinned down at all.
# ---------------------------------------------------------------------------


def test_connect_calls_psycopg_connect_with_autocommit_true(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_psycopg_connect(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        raise RuntimeError("stop before any real network attempt")

    monkeypatch.setattr(
        hotpath_indexes_module.psycopg, "connect", _fake_psycopg_connect
    )
    with pytest.raises(HotpathIndexError, match="postgres_connection_failed"):
        hotpath_indexes_module._connect("postgresql://fake/db")
    assert captured["url"] == "postgresql://fake/db"
    assert captured.get("autocommit") is True
