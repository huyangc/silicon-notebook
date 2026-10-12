"""K3: the notebook-lifecycle predicate added to the Memory readers and to the
self-service ask_jobs/reports reads leaves every plan's shape unchanged.

``live_read_access_exists_clause`` is ``read_access_exists_clause`` with
``AND access_nb.status NOT IN ('copying','deleting','importing')`` appended
inside the same EXISTS, on the notebooks row the EXISTS already reached. No
placeholder is added, so the old statement is recovered from the captured
new one by dropping that tail — the pin compares the two on the same data.

* Every statement is captured from the production reader that issues it
  (``repo._runtime.<component>.<method>``) on a seeded world with ~10^4
  Memory rows over a few hundred notebooks, VACUUM ANALYZEd, so the planner
  uses real statistics instead of the empty-table defaults.
* Both the custom plan (``EXPLAIN`` with the bound values) and the generic
  plan (``EXPLAIN (GENERIC_PLAN)``: psycopg prepares at the 5th execution and
  PostgreSQL may switch to the generic plan from about the 11th) are taken.
* Shape = the JSON plan tree without ``Filter`` / ``Join Filter`` (the only
  text the predicate may change): node types, relations, aliases, index
  names, index/hash/merge conditions, join types, sort keys and subplan
  structure must be identical, old vs new.
* Each reader's own measured form is pinned separately (no blanket "uses
  index X" assertion: the owner-wide ``list_memories`` aggregate is a
  sequential scan + hash join under the custom plan on real data and an index
  path under the generic plan).

``MEMORY_LIVE_PLAN_SCALE=large`` (not run in CI) reseeds at production shape
— 3000 notebooks, 500k Memory rows, one heavy user owning 200k — and writes
the per-statement old/new comparison to ``MEMORY_LIVE_PLAN_REPORT`` (default
``memory_live_plan_report.json`` in the test's tmp dir), for the PR.
"""
from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pytest

from app.repositories.postgres.access_sql import NOTEBOOK_LIVE_SQL

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_memory_live_predicate_explain"),
]

_NOW = "2026-10-01T00:00:00+00:00"
LIVE_TAIL = f" AND access_nb.{NOTEBOOK_LIVE_SQL})"
# The bell's report half carries the predicate in its projection instead (no
# LIMIT there, so dropping rows in Python is row-for-row the same result; in
# the EXISTS it flipped that statement's generic plan on the CI-scale world).
LIVE_PROJECTION = f", (nb.{NOTEBOOK_LIVE_SQL}) AS notebook_live"


def old_statement(sql: str) -> str:
    """The statement as it was before K3, recovered from the captured one."""
    if LIVE_TAIL in sql:
        return sql.replace(LIVE_TAIL, ")")
    return sql.replace(LIVE_PROJECTION, "")
HEAVY = "k3-heavy"

SCALES = {
    "ci": dict(notebooks=300, light_users=40, memories=12000, heavy=4800,
               asks=4000, reports=1500),
    "large": dict(notebooks=3000, light_users=500, memories=500000, heavy=200000,
                  asks=60000, reports=20000),
}


@dataclass
class Seeded:
    target_notebook: str
    memory_id: str
    answer_ids: list[str]


def _scale_name() -> str:
    return os.environ.get("MEMORY_LIVE_PLAN_SCALE", "ci")


@pytest.fixture
def repo(postgres_settings, tmp_path):
    from app.repositories.postgres.repository import PostgresRepository

    postgres_settings.storage_dir = str(tmp_path / "postgres-storage")
    postgres_settings.event_log_enabled = False
    postgres_settings.llm_log_enabled = False
    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


def seed(repo: Any, scale: dict[str, int]) -> Seeded:
    """Production-shaped Memory / ask / report rows, all in SQL."""
    database = repo._runtime.database
    n_nb, n_light = scale["notebooks"], scale["light_users"]
    n_mem, n_heavy = scale["memories"], scale["heavy"]
    with database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO users (id,email,display_name,role,status,username,"
            "created_at,updated_at) SELECT u,u||'@x',u,'user','active',u,%s,%s "
            "FROM (SELECT %s AS u UNION ALL "
            "SELECT 'k3-u-'||g FROM generate_series(0,%s::int) g) s",
            (_NOW, _NOW, HEAVY, n_light - 1),
        )
        # Every 10th notebook is the heavy user's; a few are being deleted or
        # imported (the rows the new predicate removes).
        db.execute(
            "INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at) "
            "SELECT 'k3-nb-'||i,'NB '||i,"
            "CASE WHEN i%%10=0 THEN %s ELSE 'k3-u-'||(i%%%s) END,"
            "CASE WHEN i%%197=5 THEN 'deleting' WHEN i%%211=7 THEN 'importing' "
            "ELSE 'ready' END,%s,%s FROM generate_series(0,%s::int) i",
            (HEAVY, n_light, _NOW, _NOW, n_nb - 1),
        )
        db.execute(
            "INSERT INTO notebook_members (notebook_id,user_id,role,added_at) "
            "SELECT 'k3-nb-'||i,'k3-u-'||((i+1)%%%s),'reader',%s "
            "FROM generate_series(0,%s::int) i WHERE i%%10<>0",
            (n_light, _NOW, n_nb - 1),
        )
        db.execute(
            "INSERT INTO notebook_members (notebook_id,user_id,role,added_at) "
            "SELECT 'k3-nb-'||i,%s,'reader',%s FROM generate_series(0,%s::int) i "
            "WHERE i%%20=3",
            (HEAVY, _NOW, n_nb - 1),
        )
        db.execute(
            "INSERT INTO groups (id,name,kind,description,created_by,owner_id,"
            "created_at,updated_at) VALUES ('k3-grp','g','project','',%s,%s,%s,%s)",
            (HEAVY, HEAVY, _NOW, _NOW),
        )
        db.execute(
            "INSERT INTO group_members (group_id,user_id,role,added_at,added_by) "
            "SELECT 'k3-grp',u,'member',%s,%s FROM (SELECT %s AS u UNION ALL "
            "SELECT 'k3-u-'||g FROM generate_series(0,%s::int,7) g) s",
            (_NOW, HEAVY, HEAVY, n_light - 1),
        )
        db.execute(
            "INSERT INTO notebook_grants (id,notebook_id,principal_type,"
            "principal_id,role,created_by,created_at) "
            "SELECT 'k3-gnt-'||i,'k3-nb-'||i,'group','k3-grp','viewer',%s,%s "
            "FROM generate_series(0,%s::int) i WHERE i%%25=7",
            (HEAVY, _NOW, n_nb - 1),
        )
        memory_columns = (
            "INSERT INTO memory_items (id,notebook_id,created_by,source_answer_id,"
            "origin,status,title,content_md,created_at,updated_at) "
        )
        memory_values = (
            "'k3-mem-'||g,{nb},{creator},"
            "CASE WHEN g%%3=0 THEN 'ans-'||g END,'ask_answer',"
            "CASE WHEN g%%10<6 THEN 'confirmed' WHEN g%%10<9 THEN 'candidate' "
            "ELSE 'deprecated' END,"
            "'memory '||g,'body '||md5(g::text)||"
            "CASE WHEN g%%50=0 THEN ' zebra' ELSE '' END,"
            "%s::timestamptz-(g||' seconds')::interval,"
            "%s::timestamptz-(g||' seconds')::interval"
        )
        db.execute(
            memory_columns
            + "SELECT "
            + memory_values.format(nb="hn.a[1+(g*31)%%cardinality(hn.a)]", creator="%s")
            + " FROM generate_series(0,%s::int) g, (SELECT array_agg(n.id ORDER BY n.id) a "
            "FROM notebooks n WHERE n.created_by=%s OR EXISTS (SELECT 1 FROM "
            "notebook_members nm WHERE nm.notebook_id=n.id AND nm.user_id=%s)) hn",
            (HEAVY, _NOW, _NOW, n_heavy - 1, HEAVY, HEAVY),
        )
        db.execute(
            memory_columns
            + "SELECT "
            + memory_values.format(nb="nb.id", creator="nb.created_by")
            + " FROM generate_series(%s::int,%s::int) g JOIN notebooks nb ON nb.id='k3-nb-'||"
            "CASE WHEN (g%%%s)%%10=0 THEN (g%%%s)+1 ELSE g%%%s END",
            (_NOW, _NOW, n_heavy, n_mem - 1, n_nb, n_nb, n_nb),
        )
        db.execute(
            "INSERT INTO memory_provenance (id,memory_id,origin,payload_json,created_at) "
            "SELECT 'prov-'||id,id,'ask_answer','{}'::jsonb,created_at FROM memory_items "
            "WHERE id LIKE 'k3-mem-%%'"
        )
        db.execute(
            "INSERT INTO memory_embeddings (memory_id,model,dimension,vector,updated_at) "
            "SELECT id,'fake',16,decode(md5(id)||md5(id),'hex'),updated_at "
            "FROM memory_items WHERE id LIKE 'k3-mem-%%' AND status='confirmed'"
        )
        for table, rows, extra_cols, extra_vals in (
            ("ask_jobs", scale["asks"], "mode,question,status",
             "'chunk','q?','completed'"),
            ("reports", scale["reports"], "question,status",
             "'r?',CASE WHEN g%%17=0 THEN 'outline_ready' ELSE 'done' END"),
        ):
            db.execute(
                f"INSERT INTO {table} (id,notebook_id,created_by,{extra_cols},"
                "created_at,updated_at) "
                f"SELECT 'k3-{table}-'||g,nb.id,"
                "CASE WHEN g%%5=0 AND (nb.created_by=%s OR nm.user_id IS NOT NULL) "
                "THEN %s ELSE nb.created_by END,"
                f"{extra_vals},"
                "%s::timestamptz-(g||' minutes')::interval,"
                "%s::timestamptz-(g||' minutes')::interval "
                "FROM generate_series(0,%s::int) g "
                "JOIN notebooks nb ON nb.id='k3-nb-'||(g%%%s) "
                "LEFT JOIN notebook_members nm ON nm.notebook_id=nb.id AND nm.user_id=%s",
                (HEAVY, HEAVY, _NOW, _NOW, rows - 1, n_nb, HEAVY),
            )
        target = db.execute(
            "SELECT m.notebook_id FROM memory_items m JOIN notebooks n "
            "ON n.id=m.notebook_id AND n.status='ready' WHERE m.created_by=%s "
            "GROUP BY m.notebook_id ORDER BY COUNT(*) DESC, m.notebook_id LIMIT 1",
            (HEAVY,),
        ).fetchone()["notebook_id"]
        memory_id = db.execute(
            "SELECT id FROM memory_items WHERE created_by=%s AND notebook_id=%s "
            "AND status='confirmed' ORDER BY id LIMIT 1",
            (HEAVY, target),
        ).fetchone()["id"]
        answer_ids = [
            row["source_answer_id"]
            for row in db.execute(
                "SELECT source_answer_id FROM memory_items WHERE created_by=%s "
                "AND notebook_id=%s AND source_answer_id IS NOT NULL "
                "ORDER BY id LIMIT 20",
                (HEAVY, target),
            ).fetchall()
        ]
    import psycopg

    with psycopg.connect(database.settings.database_url, autocommit=True) as raw:
        raw.execute("VACUUM (ANALYZE)")
    return Seeded(target, memory_id, answer_ids)


# ------------------------------------------------------------- the capture
class _Recording:
    def __init__(self, inner, sink):
        self._inner, self._sink = inner, sink

    def execute(self, sql, params=None, *args, **kwargs):
        self._sink.append((str(sql), params))
        return self._inner.execute(sql, params, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@contextmanager
def _recorded(repo, monkeypatch):
    database = repo._runtime.database
    statements: list[tuple[str, Any]] = []
    with monkeypatch.context() as patch:
        for name in ("connect", "write"):
            original = getattr(database, name)

            def recording(*args, _original=original, **kwargs):
                @contextmanager
                def manager():
                    with _original(*args, **kwargs) as db:
                        yield _Recording(db, statements)

                return manager()

            patch.setattr(database, name, recording)
        yield statements


def _swallow(fn, *expected):
    try:
        fn()
    except expected:
        pass


def capture_readers(repo, monkeypatch, seeded: Seeded) -> dict[str, list[tuple[str, Any]]]:
    """Run every reader once and keep each statement carrying the new tail."""
    store = repo._runtime.memory_store
    queries = repo._runtime.queries
    nb, mid = seeded.target_notebook, seeded.memory_id
    stale = store.memory_for_user(mid, HEAVY)
    stale = stale.model_copy(update={"title": stale.title + " (stale)"})

    def promote():
        with repo._runtime.database.write() as db:
            store.propose_promotion_on(db, mid, HEAVY, "p-k3", [], [], stale, _NOW)

    def list_page(**kwargs):
        args = dict(notebook_id=None, status=None, origin=None, query="",
                    offset=0, limit=50)
        args.update(kwargs)
        return store.list_memories(HEAVY, **args)

    readers = {
        "memory_for_user": lambda: store.memory_for_user(mid, HEAVY),
        "answer_memory_links": lambda: store.answer_memory_links(
            nb, HEAVY, seeded.answer_ids
        ),
        "propose_promotion_on": lambda: _swallow(promote, ValueError),
        "update_fields": lambda: store.update_fields(mid, HEAVY, {"title": "t"}),
        "transition": lambda: _swallow(
            lambda: store.transition(mid, HEAVY, {"rejected"}, "deprecated"),
            ValueError,
        ),
        "export": lambda: store.memory_export_snapshot(
            nb, HEAVY, page_size=500, max_bytes=10**9
        ),
        "list_memories.all": lambda: list_page(),
        "list_memories.notebook": lambda: list_page(notebook_id=nb, status="confirmed"),
        "list_memories.search": lambda: list_page(query="zebra"),
        "memory_retrieval_rows": lambda: store.memory_retrieval_rows(
            HEAVY, nb, ("candidate", "confirmed"), "zebra",
            lexical_limit=40, vector_limit=200,
        ),
        "activity.ask_only": lambda: queries.list_user_activity(
            HEAVY, activity_type="ask"
        ),
        "activity.report_only": lambda: queries.list_user_activity(
            HEAVY, activity_type="report"
        ),
        "bell.report_outlines": lambda: queries.pending_actions_projection_rows(HEAVY),
    }
    captured: dict[str, list[tuple[str, Any]]] = {}
    for label, call in readers.items():
        with _recorded(repo, monkeypatch) as statements:
            call()
        live = [
            (sql, params) for sql, params in statements
            if LIVE_TAIL in sql or LIVE_PROJECTION in sql
        ]
        assert live, f"{label}: no statement carries the lifecycle predicate"
        captured[label] = live
    return captured


# ---------------------------------------------------------------- the plans
def _generic_text(sql: str) -> str:
    counter = iter(range(1, 10_000))
    return re.sub(r"%s", lambda _m: f"${next(counter)}", sql).replace("%%", "%")


def _plan(connection, sql: str, params, *, generic: bool) -> Any:
    if generic:
        row = connection.execute(
            "EXPLAIN (GENERIC_PLAN, COSTS OFF, FORMAT JSON) " + _generic_text(sql)
        ).fetchone()
    else:
        row = connection.execute(
            "EXPLAIN (COSTS OFF, FORMAT JSON) " + sql, params
        ).fetchone()
    return row["QUERY PLAN"]


_TEXT_ONLY = {"Filter", "Join Filter"}


def shape(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: shape(v) for k, v in node.items() if k not in _TEXT_ONLY}
    if isinstance(node, list):
        return [shape(item) for item in node]
    return node


def _walk(node: Any):
    if isinstance(node, dict):
        if "Node Type" in node:
            yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def summary(plan: Any) -> list[str]:
    """One line per plan node: type, relation alias, index."""
    lines = []
    for node in _walk(plan):
        parts = [node["Node Type"]]
        if node.get("Join Type"):
            parts.append(node["Join Type"])
        if node.get("Relation Name"):
            parts.append(f"{node['Relation Name']} {node.get('Alias', '')}".strip())
        if node.get("Index Name"):
            parts.append(f"using {node['Index Name']}")
        lines.append(" ".join(parts))
    return lines


def compare_plans(repo, captured) -> dict[str, list[dict[str, Any]]]:
    report: dict[str, list[dict[str, Any]]] = {}
    with repo._runtime.database.connect() as connection:
        for label, statements in captured.items():
            entries = []
            for index, (sql, params) in enumerate(statements):
                old_sql = old_statement(sql)
                assert old_sql != sql, label
                assert LIVE_TAIL not in old_sql and LIVE_PROJECTION not in old_sql
                entry: dict[str, Any] = {"statement": index, "sql": sql[:160]}
                for mode in ("custom", "generic"):
                    old = _plan(connection, old_sql, params, generic=mode == "generic")
                    new = _plan(connection, sql, params, generic=mode == "generic")
                    if LIVE_TAIL in sql:  # a projection is not in a plan
                        assert "deleting" in json.dumps(new), (label, mode)
                    entry[mode] = {
                        "same_shape": shape(old) == shape(new),
                        "old": summary(old),
                        "new": summary(new),
                    }
                entries.append(entry)
            report[label] = entries
    return report


# Measured on the CI-scale world (PostgreSQL 16). One entry per reader and
# statement: indexes the NEW plan must use under (custom, generic). Only the
# facts that make the reader cheap are pinned — the shape equality above is
# the regression guard; these keep a future seed change from silently turning
# the comparison into "seq scan vs seq scan".
_PK = {"pk_memory_items"}
_OWNER_NB = {"idx_memory_owner_notebook_status"}
_AGENT = {"idx_memory_agent_candidate"}
_TRGM = {
    "idx_memory_items_title_trgm",
    "idx_memory_items_content_md_trgm",
    "idx_memory_items_tags_trgm",
}
_EXPECTED_INDEXES: dict[str, dict[int, tuple[set[str], set[str]]]] = {
    "memory_for_user": {0: (_PK, _PK)},
    "answer_memory_links": {0: ({"idx_memory_answer_once"}, _OWNER_NB)},
    "propose_promotion_on": {0: (_PK, _PK)},
    "update_fields": {0: (_PK, _PK), 1: (_PK, _PK)},
    "transition": {0: (_PK, _PK), 1: (_PK, _PK)},
    "export": {0: (_OWNER_NB, _OWNER_NB), 1: (_OWNER_NB, _OWNER_NB)},
    # Owner-wide aggregate / options / count / page.
    "list_memories.all": {i: (_AGENT, _AGENT) for i in range(4)},
    "list_memories.notebook": {
        0: (_AGENT, _AGENT), 1: (_AGENT, _AGENT),
        2: (_OWNER_NB, _OWNER_NB), 3: (_OWNER_NB, _OWNER_NB),
    },
    # postgres/search.py count + page candidates, then the row statements.
    "list_memories.search": {
        0: (_AGENT, _AGENT | _TRGM), 1: (_AGENT, _AGENT | _TRGM),
        2: (_AGENT, _AGENT), 3: (_AGENT, _AGENT), 4: (_PK, _PK),
    },
    "memory_retrieval_rows": {i: (_OWNER_NB, _OWNER_NB) for i in range(3)},
    "activity.ask_only": {
        0: ({"idx_ask_jobs_creator_activity"}, {"idx_ask_jobs_creator_activity"})
    },
    "activity.report_only": {0: ({"pk_notebooks"}, {"pk_notebooks"})},
    "bell.report_outlines": {0: ({"pk_notebooks"}, {"pk_notebooks"})},
}


def _row_counts(repo) -> dict[str, int]:
    with repo._runtime.database.connect() as db:
        counts = {
            table: int(db.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])
            for table in ("notebooks", "memory_items", "memory_embeddings",
                          "ask_jobs", "reports", "notebook_members")
        }
        counts["heavy_user_memory_items"] = int(db.execute(
            "SELECT COUNT(*) AS c FROM memory_items WHERE created_by=%s", (HEAVY,)
        ).fetchone()["c"])
    return counts


@pytest.mark.skipif(_scale_name() != "ci", reason="large scale runs the report test")
def test_live_predicate_keeps_every_reader_plan_shape(repo, monkeypatch):
    seeded = seed(repo, SCALES["ci"])
    captured = capture_readers(repo, monkeypatch, seeded)
    report = compare_plans(repo, captured)
    if os.environ.get("MEMORY_LIVE_PLAN_REPORT"):
        with open(os.environ["MEMORY_LIVE_PLAN_REPORT"], "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
    differing = {
        (label, entry["statement"], mode): (entry[mode]["old"], entry[mode]["new"])
        for label, entries in report.items()
        for entry in entries
        for mode in ("custom", "generic")
        if not entry[mode]["same_shape"]
    }
    assert differing == {}, differing
    for label, per_statement in _EXPECTED_INDEXES.items():
        for index, (custom_indexes, generic_indexes) in per_statement.items():
            entry = report[label][index]
            for mode, expected in (("custom", custom_indexes), ("generic", generic_indexes)):
                used = " ".join(entry[mode]["new"])
                missing = {name for name in expected if f"using {name}" not in used}
                assert not missing, (label, index, mode, missing, entry[mode]["new"])


@pytest.mark.skipif(_scale_name() != "large", reason="opt-in production-scale report")
def test_live_predicate_plan_report_at_production_scale(repo, monkeypatch, tmp_path):
    seeded = seed(repo, SCALES["large"])
    captured = capture_readers(repo, monkeypatch, seeded)
    report: dict[str, Any] = {"_scale": _row_counts(repo)}
    report.update(compare_plans(repo, captured))
    out = os.environ.get("MEMORY_LIVE_PLAN_REPORT") or str(
        tmp_path / "memory_live_plan_report.json"
    )
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    differing = [
        (label, entry["statement"], mode)
        for label, entries in report.items()
        if not label.startswith("_")
        for entry in entries
        for mode in ("custom", "generic")
        if not entry[mode]["same_shape"]
    ]
    assert differing == [], (out, differing)
