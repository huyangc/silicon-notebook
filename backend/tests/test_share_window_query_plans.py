"""SQLite query-plan pins for the public page's window and the share dialog's
counts (no ANALYZE, as in production). The PostgreSQL twin is
``postgres/test_share_window_explain_pins.py``.

On the statements the store actually issues (recorded with their parameters):

* every window read (``_prefix_window``: the public read and the dialog's
  windows) SEARCHes ``idx_global_jobs_conversation`` with the conversation
  AND the row-value keyset as its index constraint, in index order -- no
  ``TEMP B-TREE`` sort, no full scan;
* the count of older turns and the dialog's one aggregate are bounded to the
  conversation on that index (``conversation_id=?``); the aggregate's anchors
  are primary-key probes.

The plan text differs between SQLite versions (CI runs 3.45, this machine
3.53); the patterns accept both spellings.
"""
from __future__ import annotations

import json
import re

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository

_KEYSET_SEARCH = re.compile(
    r"SEARCH (\w+ )?USING (COVERING )?INDEX idx_global_jobs_conversation "
    r"\(conversation_id=\? AND \(created_at,id\)<"
)
_CONVERSATION_SEARCH = re.compile(
    r"SEARCH (j|global_ask_jobs) USING (COVERING )?INDEX idx_global_jobs_conversation \(conversation_id=\?"
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'plans.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings())
    store = repo._runtime.global_ask_store
    stamp = lambda i: f"2026-01-01T{i // 3600:02d}:{i // 60 % 60:02d}:{i % 60:02d}+00:00"  # noqa: E731
    with store.database.write() as db:
        for c in range(6):
            db.execute(
                "INSERT INTO global_ask_conversations(id,user_id,title,scope_json,submitted_via,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (f"gc-{c}", "u-0", "t", "{}", "web", stamp(0), stamp(0)),
            )
            for g in range(1200 if c == 0 else 300):
                db.execute(
                    "INSERT INTO global_ask_jobs(id,conversation_id,user_id,request_json,status,"
                    "payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
                    (f"gj-{c}-{g:05d}", f"gc-{c}", "u-0", "{}", "done",
                     json.dumps({"question": "q", "answer": {"anchors": [], "citations": []}}), stamp(g)),
                )
        db.execute(
            "UPDATE global_ask_conversations SET share_token='gshr-pin', shared_through_at=?, "
            "shared_through_id='gj-0-00900' WHERE id='gc-0'", (stamp(900),),
        )
    yield store
    repo.close()


def _recorded(database, call):
    statements: list = []
    connect = database.connect

    class Recorder:
        def __init__(self, inner):
            self._inner = inner

        def __enter__(self):
            self._db = self._inner.__enter__()
            return self

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

        def execute(self, sql, params=(), *args, **kwargs):
            statements.append((sql, tuple(params or ())))
            return self._db.execute(sql, params, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._db, name)

    database.connect = lambda *a, **k: Recorder(connect(*a, **k))
    try:
        call()
    finally:
        database.connect = connect
    return statements


def _plan(store, sql, params) -> str:
    with store.database.connect() as db:
        rows = db.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    return "\n".join(str(row[3]) for row in rows)


def _windows(statements):
    return [(sql, params) for sql, params in statements
            if sql.startswith("SELECT id, payload_json, created_at FROM global_ask_jobs") and "LIMIT" in sql]


def test_the_public_window_searches_the_conversation_index_from_the_bound(store):
    statements = _recorded(store.database, lambda: store.public_conversation_by_token("gshr-pin"))
    windows = _windows(statements)
    assert windows, statements
    for sql, params in windows:
        plan = _plan(store, sql, params)
        assert _KEYSET_SEARCH.search(plan), plan
        assert "TEMP B-TREE" not in plan and "SCAN" not in plan, plan
    counts = [(sql, params) for sql, params in statements if sql.startswith("SELECT COUNT(*) AS n")]
    assert counts, statements
    for sql, params in counts:
        plan = _plan(store, sql, params)
        assert _KEYSET_SEARCH.search(plan), plan
        assert "SCAN" not in plan, plan
    row = store.public_conversation_by_token("gshr-pin")
    assert row["jobs"][-1]["job_id"] == "gj-0-00900" and row["earlier_turns"] == 401


def test_the_share_dialog_counts_in_one_aggregate_on_the_conversation_index(store):
    def call():
        store.share_counts("gc-0", "u-0", through_id="gj-0-01100",
                           watermark_id="gj-0-00900", watermark_at="")

    statements = _recorded(store.database, call)
    [(aggregate, params)] = [(sql, p) for sql, p in statements if sql.startswith("WITH a AS")]
    plan = _plan(store, aggregate, params)
    assert _CONVERSATION_SEARCH.search(plan), plan
    assert not re.search(r"SCAN (j|global_ask_jobs)\b", plan), plan
    assert "USING INDEX sqlite_autoindex_global_ask_jobs_1" in plan or "PRIMARY KEY" in plan, plan
    windows = _windows(statements)
    assert len(windows) == 2, statements
    for sql, wparams in windows:
        wplan = _plan(store, sql, wparams)
        assert _KEYSET_SEARCH.search(wplan), wplan
        assert "TEMP B-TREE" not in wplan and "SCAN" not in wplan, wplan
    # The aggregate's sizes are passed down: no COUNT runs for the dialog.
    assert not [sql for sql, _p in statements if sql.startswith("SELECT COUNT(*) AS n")], statements
