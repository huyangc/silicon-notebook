"""`memory_sql.memory_derived_in_notebook` -- the notebook-scoped form of the
"derived from Memory" classifier the E4-2 graph-build readers use (E5-1 added
the same fragment for the copy path; the two definitions are byte-identical).

Pinned here, on both backends' text and on SQLite behaviour (the PostgreSQL
behaviour is in ``tests/postgres/test_memory_kg_seed_exclusion_pg.py``):

* zero positional parameters, whatever the alias; no foreign placeholder style;
* an outer alias that would capture the inner ``ds`` (any case) or is not a bare
  identifier is refused;
* the PostgreSQL text equals the SQLite text;
* on rows whose source lives in their own notebook (a write invariant) it agrees
  with ``memory_derived_object``; a row pointing at another notebook's Memory
  source is not classified by it (the extra condition only carries the outer
  notebook filter into the inner query).
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.repositories.postgres import memory_sql as pg_memory_sql
from app.repositories.sqlite import memory_sql
from app.services.sqlite_repository import SQLiteRepository

NOW = "2026-09-30T00:00:00"


def test_zero_parameters_and_identical_text_on_both_backends():
    for alias in ("o", "kr", "x1", "Outer_2"):
        text = memory_sql.memory_derived_in_notebook(alias)
        assert text.count("?") == 0 and "%s" not in text
        assert pg_memory_sql.memory_derived_in_notebook(alias) == text


@pytest.mark.parametrize("bad", ["ds", "DS", "Ds", "o.x", '"o"', "o\n", "", "1o"])
def test_an_alias_that_captures_the_inner_table_or_is_not_bare_is_refused(bad):
    for module in (memory_sql, pg_memory_sql):
        with pytest.raises(ValueError):
            module.memory_derived_in_notebook(bad)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings())
    database = repo._runtime.database
    with database.write() as conn:
        conn.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at) "
            "VALUES ('u','u@x.test','u','user','active',?,?)", (NOW, NOW))
        for nb in ("nb1", "nb2"):
            conn.execute(
                "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (nb, nb, "", "", "draft", "u", NOW, NOW))
        for sid, nb, kind in (("m1", "nb1", "memory"), ("k1", "nb1", "knowhow"),
                              ("u1", "nb1", "md")):
            conn.execute(
                "INSERT INTO sources(id,notebook_id,title,source_type,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)", (sid, nb, sid, kind, NOW, NOW))
        for oid, nb, sid in (("o-mem", "nb1", "m1"), ("o-kh", "nb1", "k1"),
                             ("o-up", "nb1", "u1"), ("o-none", "nb1", ""),
                             ("o-orphan", "nb1", "gone"), ("o-cross", "nb2", "m1")):
            conn.execute(
                "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
                "evidence,source_id,created_at,updated_at) "
                "VALUES (?,?,'concept','approved','{}','[]',?,?,?)",
                (oid, nb, sid, NOW, NOW))
    with database.connect() as conn:
        yield conn


def _ids(conn, predicate: str) -> set[str]:
    return {r[0] for r in conn.execute(
        f"SELECT o.id FROM knowledge_objects o WHERE {predicate}").fetchall()}


def test_classifies_this_notebooks_memory_rows_only(db):
    here = _ids(db, memory_sql.memory_derived_in_notebook("o"))
    anywhere = _ids(db, memory_sql.memory_derived_object("o"))
    assert here == {"o-mem"}
    assert anywhere == {"o-mem", "o-cross"}
    # its negation keeps knowhow, ordinary, source-less and orphan rows
    kept = _ids(db, "NOT " + memory_sql.memory_derived_in_notebook("o"))
    assert kept == {"o-kh", "o-up", "o-none", "o-orphan", "o-cross"}
