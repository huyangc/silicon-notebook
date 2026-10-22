"""EXPLAIN pins (real PostgreSQL) for the public page's window and the share
dialog's counts.

The public page now shows the NEWEST ``MAX_TURNS`` turns of the published
prefix (``public_window``), and the share dialog's numbers are counted on the
server (``GlobalAskStore.share_counts``). What is pinned, on the statements
the stores actually issue (recorded with their parameters):

* the global window read (``_prefix_window``: the public read, the share
  window and the dialog's windows) walks ``idx_global_jobs_conversation``
  BACKWARDS under a ``Limit`` -- no ``Sort``, no sequential scan: it reads
  one window of rows however long the conversation is, in the custom and the
  GENERIC plan;
* the count of older turns and the dialog's one aggregate are bounded to the
  conversation by ``idx_global_jobs_conversation`` (an index condition on
  ``conversation_id``): proportional to the conversation's size, never to the
  table's; the aggregate's anchors are primary-key probes;
* the notebook-scoped public read (``answers``, indexed on
  ``conversation_id``) reads the conversation's rows through that index under
  a ``Limit``.

None of these is an Index Only Scan, so none depends on the visibility map;
the ``bystander_snapshot`` parametrization runs every pin a second time with
a REPEATABLE READ bystander holding the horizon to prove it.
"""
from __future__ import annotations

import re

import pytest

from app.domain.conversation_public_view import MAX_TURNS
from app.repositories.global_ask_store import GlobalAskStore
from app.repositories.postgres.migrator import PostgresMigrator

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_share_window_explain"),
]

_TARGET_TURNS = MAX_TURNS * 6
_OTHER_CONVERSATIONS = 40
_OTHER_TURNS = 500


class _Recorder:
    """Delegates to a real connection and records every ``(sql, params)``."""

    def __init__(self, inner, statements):
        self._inner = inner
        self._statements = statements
        self._entered = None

    def __enter__(self):
        self._entered = self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)

    def execute(self, sql, params=(), *args, **kwargs):
        self._statements.append((sql, tuple(params or ())))
        return (self._entered or self._inner).execute(sql, params, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._entered or self._inner, name)


def _recorded(database, call):
    statements: list = []
    connect = database.connect
    database.connect = lambda *a, **k: _Recorder(connect(*a, **k), statements)
    try:
        call()
    finally:
        database.connect = connect
    return statements


_STAMP = (
    "'2026-01-01T'||lpad((g/3600)::text,2,'0')||':'||lpad((g/60%%60)::text,2,'0')"
    "||':'||lpad((g%%60)::text,2,'0')||'+00:00'"
)


def _seed(postgres_database) -> None:
    with postgres_database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO global_ask_conversations(id,user_id,title,scope_json,submitted_via,"
            "created_at,updated_at) SELECT 'gc-'||c,'u-0','t','{}','web',%s,%s "
            "FROM generate_series(0,%s) c",
            ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", _OTHER_CONVERSATIONS),
        )
        # One user, many conversations (the production shape): the target gc-0
        # holds six pages' worth of turns, forty others one page each, so both
        # the table and the user's own jobs are far larger than gc-0.
        db.execute(
            "INSERT INTO global_ask_jobs(id,conversation_id,user_id,request_json,status,"
            f"payload_json,created_at) SELECT 'gj-0-'||lpad(g::text,5,'0'),'gc-0','u-0','{{}}',"
            "CASE WHEN g%%97=0 THEN 'failed' ELSE 'done' END,"
            f"'{{\"question\":\"q\",\"answer\":{{\"anchors\":[],\"citations\":[]}}}}',{_STAMP} "
            "FROM generate_series(0,%s) g",
            (_TARGET_TURNS - 1,),
        )
        db.execute(
            "INSERT INTO global_ask_jobs(id,conversation_id,user_id,request_json,status,"
            f"payload_json,created_at) SELECT 'gj-'||c||'-'||lpad(g::text,5,'0'),'gc-'||c,"
            f"'u-0','{{}}','done','{{\"question\":\"q\"}}',{_STAMP} "
            "FROM generate_series(1,%s) c, generate_series(0,%s) g",
            (_OTHER_CONVERSATIONS, _OTHER_TURNS - 1),
        )
        db.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) VALUES "
            "('nb-pin','n',%s,%s)", ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        db.execute(
            "INSERT INTO conversations(id,notebook_id,title,created_by,created_at,updated_at) "
            "SELECT 'nc-'||c,'nb-pin','t','u-0',%s,%s FROM generate_series(0,%s) c",
            ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", _OTHER_CONVERSATIONS),
        )
        db.execute(
            "INSERT INTO answers(id,notebook_id,conversation_id,question,payload,created_at) "
            f"SELECT 'na-'||c||'-'||g,'nb-pin','nc-'||c,'q','{{}}',({_STAMP})::timestamptz "
            "FROM generate_series(0,%s) c, generate_series(0,%s) g "
            "WHERE c = 0 OR g < %s",
            (_OTHER_CONVERSATIONS, _TARGET_TURNS - 1, _OTHER_TURNS),
        )
    import psycopg

    with psycopg.connect(postgres_database.settings.database_url, autocommit=True) as raw:
        for table in ("global_ask_jobs", "global_ask_conversations", "answers", "conversations"):
            raw.execute(f"VACUUM (ANALYZE) {table}")


def _explain(connection, sql: str, params: tuple, *, generic: bool) -> str:
    if generic:
        parts = sql.split("%s")
        sql = parts[0] + "".join(f"${index}{part}" for index, part in enumerate(parts[1:], 1))
        rows = connection.execute(f"EXPLAIN (GENERIC_PLAN, COSTS OFF) {sql}").fetchall()
    else:
        rows = connection.execute(f"EXPLAIN (COSTS OFF) {sql}", params).fetchall()
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


def _plans(postgres_database, bystander_snapshot, statements, pick) -> list[str]:
    chosen = [(sql, params) for sql, params in statements if pick(sql)]
    assert chosen, statements
    plans = []
    with postgres_database.connect() as connection:
        bystander_snapshot.assert_visibility_map_blocked(connection, "global_ask_jobs", "answers")
        for sql, params in chosen:
            for generic in (False, True):
                plans.append(_explain(connection, sql, params, generic=generic))
    return plans


def _is_window(sql: str) -> bool:
    return sql.startswith("SELECT id, payload_json, created_at FROM global_ask_jobs") and "LIMIT" in sql


def _is_older_count(sql: str) -> bool:
    return sql.startswith("SELECT COUNT(*) AS n FROM global_ask_jobs")


def _assert_backward_window(plan: str) -> None:
    assert "Limit" in plan, plan
    assert "Index Scan Backward using idx_global_jobs_conversation on global_ask_jobs" in plan, plan
    assert "Sort" not in plan and "Seq Scan" not in plan, plan


def _assert_bounded_to_the_conversation(plan: str) -> None:
    assert "Seq Scan on global_ask_jobs" not in plan, plan
    assert "idx_global_jobs_conversation" in plan, plan
    assert re.search(r"Index Cond: \(+conversation_id = ", plan), plan


@pytest.fixture
def seeded(postgres_database, bystander_snapshot):
    assert PostgresMigrator(postgres_database).migrate()
    _seed(postgres_database)
    with postgres_database.write() as db:
        db.execute(
            "UPDATE global_ask_conversations SET share_token='gshr-pin', "
            "shared_through_at='2026-01-01T00:41:00+00:00', shared_through_id='gj-0-02460' "
            "WHERE id='gc-0'"
        )
        db.execute(
            "UPDATE conversations SET share_token='cshr-pin', "
            "shared_through_at='2026-01-01T00:41:00+00:00', shared_through_id='na-0-2460' "
            "WHERE id='nc-0'"
        )
    return GlobalAskStore(postgres_database, marker="%s")


def test_the_public_window_reads_backwards_under_a_limit(postgres_database, bystander_snapshot, seeded):
    statements = _recorded(postgres_database, lambda: seeded.public_conversation_by_token("gshr-pin"))
    row = seeded.public_conversation_by_token("gshr-pin")
    assert len(row["jobs"]) == MAX_TURNS and row["jobs"][-1]["job_id"] == "gj-0-02460"
    assert row["earlier_turns"] > 0
    for plan in _plans(postgres_database, bystander_snapshot, statements, _is_window):
        _assert_backward_window(plan)
    for plan in _plans(postgres_database, bystander_snapshot, statements, _is_older_count):
        _assert_bounded_to_the_conversation(plan)


def test_the_share_dialog_counts_in_one_aggregate_bounded_to_the_conversation(
    postgres_database, bystander_snapshot, seeded,
):
    def call():
        seeded.share_counts("gc-0", "u-0", through_id="gj-0-02800",
                            watermark_id="gj-0-02460", watermark_at="2026-01-01T00:41:00+00:00")

    statements = _recorded(postgres_database, call)
    aggregates = [sql for sql, _params in statements if sql.startswith("WITH a AS")]
    assert len(aggregates) == 1, statements
    for plan in _plans(postgres_database, bystander_snapshot, statements,
                       lambda sql: sql.startswith("WITH a AS")):
        _assert_bounded_to_the_conversation(plan)
        assert "global_ask_jobs_pkey" in plan, plan
    windows = _plans(postgres_database, bystander_snapshot, statements, _is_window)
    assert len(windows) == 4  # the current and the update window, custom + generic
    for plan in windows:
        _assert_backward_window(plan)


def test_the_notebook_public_window_reads_the_conversation_through_its_index(
    postgres_database, bystander_snapshot, seeded,
):
    from app.repositories.postgres.ask_state_store import AskStateStore as PostgresAskStateStore

    store = object.__new__(PostgresAskStateStore)
    store.database = postgres_database
    statements = _recorded(postgres_database, lambda: store.public_conversation_by_token("cshr-pin"))
    row = store.public_conversation_by_token("cshr-pin")
    assert len(row["turns"]) == MAX_TURNS and row["turns"][-1]["answer_id"] == "na-0-2460"
    assert row["earlier_turns"] == 2461 - MAX_TURNS
    for plan in _plans(postgres_database, bystander_snapshot, statements,
                       lambda sql: sql.startswith("SELECT id, question") or sql.startswith("SELECT COUNT(*) AS n FROM answers")):
        assert "Seq Scan on answers" not in plan, plan
        assert "idx_answers_conversation" in plan, plan
