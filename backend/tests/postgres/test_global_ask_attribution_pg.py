"""Physical attribution of finished global Ask jobs -- PostgreSQL, the
canonical definition (0078; SQLite twin ``tests/test_global_ask_attribution.py``;
shared cases in ``tests/global_attribution_cases.py``).

Besides the shared expectations this pins the read plan: the attributed arm of
the panel's global-answer count is answered from
``idx_global_ask_job_notebooks_notebook`` and the unattributed arm from the
partial ``idx_global_ask_jobs_unattributed`` with the participation GIN index.
"""
from __future__ import annotations

import json

import pytest

from app.domain.global_ask_attribution import job_attribution
from app.models.global_ask import GlobalAskJob
from app.repositories.global_ask_store import GlobalAskStore
from app.repositories.postgres.query_store import global_answers_count_sql
from tests import global_attribution_cases as cases

pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def repo(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        with repository._runtime.database.write() as db:
            cases.seed_base(db, "%s")
        yield repository
    finally:
        repository.close()


@pytest.fixture
def legacy(repo):
    with repo._runtime.database.write() as db:
        cases.seed_legacy_jobs(db, "%s")
    return repo


def _analytics(repo, notebook_id):
    return repo._runtime.queries.notebook_analytics(notebook_id)


def _reference(repo, notebook_id):
    with repo._runtime.database.connect() as db:
        return cases.live_done_count_via_payload(db, notebook_id, dialect="postgres", q="%s")


def test_completion_writes_the_attribution_of_done_jobs_only(repo):
    cases.create_and_finish(repo._runtime.global_ask_store)
    with repo._runtime.database.connect() as db:
        rows = cases.stored_rows(db)
        flags = cases.attributed_flags(db)
        cases.assert_rows_match_attribution_sql(db, dialect="postgres", q="%s")
    assert rows == cases.expected_rows_sorted()
    done = {job for job, status in cases.statuses().items() if status == "done"}
    assert {job for job, flag in flags.items() if flag} == done - {cases.UNREADABLE_JOB[0]}


def test_completion_rows_are_job_attribution_of_the_saved_job(repo):
    store = repo._runtime.global_ask_store
    job = GlobalAskJob.model_validate_json(cases.all_payloads()["gaj-05"])
    job.status = "running"
    store.create(job, cases.USER, None, "{}", "web", new_conversation=False)
    job.status = "done"
    assert store.save(job, cases.USER)
    assert store.save(job, cases.USER) is False
    with repo._runtime.database.connect() as db:
        assert cases.stored_rows(db)["gaj-05"] == sorted(job_attribution(job))


@pytest.mark.parametrize("notebook_id", sorted(cases.EXPECTED_GLOBAL_ANSWERS))
def test_panel_counts_global_answers_before_and_after_the_backfill(legacy, notebook_id):
    before = cases.answers(_analytics(legacy, notebook_id))
    assert before == cases.expected_answers(notebook_id)
    assert before[1] == _reference(legacy, notebook_id)
    result = cases.backfill(legacy.maintenance)
    assert (result.written, result.unreadable, result.pending_after) == (11, 1, 1)
    assert cases.answers(_analytics(legacy, notebook_id)) == before


def test_half_backfilled_database_counts_the_same(legacy):
    cases.backfill(legacy.maintenance, limit=6, batch_size=4)
    with legacy._runtime.database.connect() as db:
        flags = cases.attributed_flags(db)
    assert 0 < sum(flags.values()) < len(flags)
    for notebook_id in cases.EXPECTED_GLOBAL_ANSWERS:
        assert cases.answers(_analytics(legacy, notebook_id)) == cases.expected_answers(notebook_id)


def test_backfilled_rows_match_the_payload_predicate_row_by_row(legacy):
    cases.backfill(legacy.maintenance)
    with legacy._runtime.database.connect() as db:
        cases.assert_rows_match_attribution_sql(db, dialect="postgres", q="%s")
        assert cases.stored_rows(db) == cases.expected_rows_sorted()


def test_backfill_is_resumable_idempotent_and_reports_unreadable(legacy):
    maintenance = legacy.maintenance
    dry = cases.backfill(maintenance, dry_run=True)
    assert (dry.written, dry.pending_after) == (11, 12)
    reported = []
    first = cases.backfill(maintenance, limit=5, batch_size=2, on_unreadable=reported.append)
    assert (first.scanned, first.written, first.last_id) == (5, 5, "gaj-05")
    resumed = cases.backfill(maintenance, after_id=first.last_id, on_unreadable=reported.append)
    assert (resumed.written, resumed.unreadable, resumed.pending_after) == (6, 1, 1)
    assert reported == [cases.UNREADABLE_JOB[0]]
    again = cases.backfill(maintenance)
    assert (again.scanned, again.written, again.unreadable) == (1, 0, 1)
    assert maintenance.write_global_ask_attributions([("gaj-01", ["nb-gc"])]) == 0
    with legacy._runtime.database.connect() as db:
        assert cases.stored_rows(db)["gaj-01"] == ["nb-ga", "nb-gb"]


def test_expired_and_unfinished_jobs_count_nowhere_in_either_arm(legacy):
    for attributed in (False, True):
        if attributed:
            cases.backfill(legacy.maintenance)
        assert cases.answers(_analytics(legacy, "nb-gb"))[1] == 4
    with legacy._runtime.database.connect() as db:
        rows = cases.stored_rows(db)
    for job_id in ("gaj-10", "gaj-11", "gaj-12", "gaj-13"):
        assert job_id not in rows


def test_purge_and_conversation_delete_cascade_to_the_rows(legacy):
    cases.backfill(legacy.maintenance)
    with legacy._runtime.database.write() as db:
        assert GlobalAskStore.purge_expired_turns(db, "2026-06-01T00:00:00+00:00", marker="%s") == 1
    with legacy._runtime.database.connect() as db:
        rows = cases.stored_rows(db)
    assert "gaj-14" not in rows and "gaj-01" in rows
    assert legacy._runtime.global_ask_store.delete(cases.CONVERSATION, cases.USER)
    with legacy._runtime.database.connect() as db:
        assert cases.stored_rows(db) == {}


def _plan(db, statement, params):
    rows = db.execute("EXPLAIN (FORMAT JSON) " + statement, params).fetchall()
    value = next(iter(rows[0].values())) if isinstance(rows[0], dict) else rows[0][0]
    return json.loads(value) if isinstance(value, str) else value


def _nodes(node):
    yield node
    for child in node.get("Plans", ()):
        yield from _nodes(child)


def test_answers_count_is_answered_from_the_attribution_indexes(legacy):
    """With sequential scans priced out the planner still finds index paths
    for both arms: the attributed arm through the (notebook_id, job_id)
    index, the unattributed arm through the partial index of done jobs at
    attributed = 0 (its predicate proven from the statement's). No sequential
    scan of either table remains."""
    statement, binds = global_answers_count_sql()
    with legacy._runtime.database.write() as db:
        db.execute("SET LOCAL enable_seqscan = off")
        nodes = list(_nodes(_plan(db, statement, ("nb-ga",) * binds)[0]["Plan"]))
    names = {n.get("Index Name") for n in nodes}
    assert "idx_global_ask_job_notebooks_notebook" in names, nodes
    assert names & {"idx_global_ask_jobs_unattributed", "idx_global_ask_jobs_participants"}, nodes
    assert not [n for n in nodes if n["Node Type"] == "Seq Scan"], nodes


class _Rollback(Exception):
    """Raised to roll back the throwaway DROP INDEX transaction."""


def test_unattributed_arm_is_answerable_from_each_of_its_two_indexes(legacy):
    """Like the rating count (0076): the unattributed arm's inner predicates
    match both the partial index and the participation GIN verbatim, so the
    planner can intersect them before the backfill and the arm costs nothing
    once the partial index is empty. Proven with sequential scans priced out
    and the competing index dropped inside a rolled-back transaction (the
    status index would otherwise serve ``status = 'done'``)."""
    statement, binds = global_answers_count_sql()
    for dropped, remaining in (
        (("idx_global_ask_jobs_unattributed", "idx_global_ask_jobs_status_mode_created"),
         "idx_global_ask_jobs_participants"),
        (("idx_global_ask_jobs_participants",), "idx_global_ask_jobs_unattributed"),
    ):
        nodes = []
        with pytest.raises(_Rollback):
            with legacy._runtime.database.write() as db:
                for name in dropped:
                    db.execute(f"DROP INDEX {name}")
                db.execute("SET LOCAL enable_seqscan = off")
                nodes = list(_nodes(_plan(db, statement, ("nb-ga",) * binds)[0]["Plan"]))
                raise _Rollback
        assert any(n.get("Index Name") == remaining for n in nodes), (dropped, nodes)
        assert not [n for n in nodes if n["Node Type"] == "Seq Scan"
                    and n.get("Relation Name") == "global_ask_jobs"], (dropped, nodes)


def test_shadow_catalog_reads_the_live_unattributed_index_as_its_migration(repo):
    from app.migration.shadow.postgres_catalog import (
        EXPECTED_OPERATIONAL_INDEXES,
        _canonical_expression,
    )

    with repo._runtime.database.connect() as db:
        row = db.execute(
            "SELECT pg_get_expr(i.indpred, i.indrelid) AS predicate "
            "FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
            "WHERE c.relname='idx_global_ask_jobs_unattributed'"
        ).fetchone()
    live = row["predicate"] if isinstance(row, dict) else row[0]
    expected = EXPECTED_OPERATIONAL_INDEXES["idx_global_ask_jobs_unattributed"]
    assert _canonical_expression(live, context="predicate") == expected.predicate_tokens
    assert "idx_global_ask_job_notebooks_notebook" in EXPECTED_OPERATIONAL_INDEXES


def test_backfill_cli_neither_seeds_nor_migrates_pg(legacy, postgres_settings, monkeypatch):
    """The CLI composes without owning the schema: no admin-credential
    rewrite (the environment hands it a different password), and against a
    ledger still at 0077 it refuses (exit 3) without applying 0078."""
    from app.services import global_ask_attribution_backfill as module

    monkeypatch.setenv("DATABASE_URL", postgres_settings.database_url)
    monkeypatch.setenv("SILICON_NOTEBOOK_ADMIN_PASSWORD", "a-different-admin-password")
    database = legacy._runtime.database

    def admins():
        with database.connect() as db:
            return [tuple(row.values()) for row in db.execute(
                "SELECT id, password_hash, updated_at FROM users WHERE role='admin' ORDER BY id"
            ).fetchall()]

    before = admins()
    assert before
    assert module.main(["--dry-run"]) == 1
    assert admins() == before
    with database.write() as db:
        # back to a ledger at 0077: 0078 and every later migration go
        db.execute("DELETE FROM silicon_schema_migrations WHERE version >= 78")
    assert module.main([]) == module.EXIT_SCHEMA_NOT_READY
    with database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM silicon_schema_migrations WHERE version >= 78"
        ).fetchone()["c"] == 0
    assert admins() == before


def test_post_readiness_pass_attributes_everything_once_pg(legacy):
    from app.services import global_ask_attribution_backfill as module

    result = module.schedule_attribution_backfill(
        legacy, is_ready=lambda: True, submit=lambda fn, *args, name: fn(*args)
    )
    assert (result.written, result.unreadable, result.pending_after) == (11, 1, 1)
    with legacy._runtime.database.connect() as db:
        assert cases.stored_rows(db) == cases.expected_rows_sorted()


def test_backfill_skips_rows_a_concurrent_stamp_holds_and_never_waits(legacy):
    """A notebook deletion's stamp holds the participating jobs' row locks;
    the backfill page must neither wait for them nor deadlock: it skips them,
    reports them pending, and a later pass attributes them. A
    ``lock_timeout`` on the backfill's pool session would turn any wait into
    an error, so finishing proves it never waited."""
    import threading

    database = legacy._runtime.database
    locked, release = threading.Event(), threading.Event()
    errors = []

    def holder():
        try:
            with database.write() as db:
                GlobalAskStore.stamp_retained_turns(
                    db, "nb-gb", "2999-01-01T00:00:00+00:00", marker="%s"
                )
                locked.set()
                release.wait(120)
        except Exception as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert locked.wait(120)
        first = cases.backfill(legacy.maintenance)
    finally:
        release.set()
        thread.join(120)
    assert not errors
    # The stamp locks the jobs nb-gb takes part in except gaj-14, whose
    # earlier stamp it keeps: 01, 02, 05, 06, 07, 08 and 09 are skipped; 03,
    # 04, 14 and 15 are attributed; 16 is unreadable.
    assert (first.written, first.skipped, first.unreadable) == (4, 7, 1), first
    assert first.pending_after == 8
    second = cases.backfill(legacy.maintenance)
    assert (second.written, second.pending_after) == (7, 1)
    with database.connect() as db:
        cases.assert_rows_match_attribution_sql(db, dialect="postgres", q="%s")


def _stamp_is_waiting_on_a_lock(probe, pid) -> bool:
    row = probe.execute(
        "SELECT wait_event_type FROM pg_stat_activity "
        "WHERE datname = current_database() AND pid = %s "
        "AND query LIKE %s",
        (pid, "%UPDATE global_ask_jobs SET retained_until%"),
    ).fetchone()
    return row is not None and row[0] == "Lock"


def test_stamp_waits_for_a_backfill_page_without_a_deadlock(legacy, postgres_settings, monkeypatch):
    """The other interleaving: the backfill holds its page's row locks (it is
    paused between the UPDATE and the INSERT) while a deletion's stamp runs.
    The stamp waits; the backfill never waits on anything, so the two cannot
    form a cycle -- both finish, no 40P01.

    Handshakes only, no sleeps: the backfill holds its page until the stamp's
    own backend is SEEN waiting on a lock in ``pg_stat_activity`` (its pid,
    this database, its statement), and the stamp's transaction runs with
    ``lock_timeout = 0`` so a slow CI runner cannot turn the wait into a
    timeout. The probe is a separate connection: the test pool has only two."""
    import threading
    import time

    import psycopg

    database = legacy._runtime.database
    store = legacy._runtime.global_ask_store
    holding, release, stamp_pid_known = threading.Event(), threading.Event(), threading.Event()
    original = store._insert_attribution_rows

    def paused(db, pairs):
        holding.set()
        assert release.wait(120), "the stamp was never seen waiting"
        return original(db, pairs)

    monkeypatch.setattr(store, "_insert_attribution_rows", paused)
    outcome, stamped = {}, {}

    def backfill():
        try:
            outcome["result"] = cases.backfill(legacy.maintenance)
        except Exception as exc:  # noqa: BLE001 - surfaced below
            outcome["error"] = exc

    def stamp():
        try:
            with database.write() as db:
                db.execute("SET LOCAL lock_timeout = '0'")
                stamped["pid"] = db.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
                stamp_pid_known.set()
                stamped["n"] = GlobalAskStore.stamp_retained_turns(
                    db, "nb-gb", "2999-01-01T00:00:00+00:00", marker="%s"
                )
        except Exception as exc:  # noqa: BLE001 - surfaced below
            stamped["error"] = exc
        finally:
            stamp_pid_known.set()

    worker = threading.Thread(target=backfill)
    stamper = threading.Thread(target=stamp)
    worker.start()
    seen_waiting = False
    try:
        assert holding.wait(120)
        stamper.start()
        assert stamp_pid_known.wait(120)
        with psycopg.connect(postgres_settings.database_url, autocommit=True) as probe:
            deadline = time.monotonic() + 120
            while "error" not in stamped and time.monotonic() < deadline:
                if _stamp_is_waiting_on_a_lock(probe, stamped["pid"]):
                    seen_waiting = True
                    break
                stamper.join(0.05)
    finally:
        release.set()
        worker.join(120)
        stamper.join(120)
    assert seen_waiting, stamped
    assert not worker.is_alive() and not stamper.is_alive()
    assert "error" not in outcome and "error" not in stamped, (outcome, stamped)
    assert outcome["result"].written == 11
    assert stamped["n"] == 7  # gaj-14 keeps its earlier stamp
