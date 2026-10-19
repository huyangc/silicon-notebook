"""Global Ask 👍 / 👎 in the notebook analytics panel and the admin question
overview -- PostgreSQL, the canonical definition (SQLite twin:
``tests/test_global_ask_feedback_counts.py``; shared cases in
``tests/global_feedback_cases.py``).

Besides the shared expectations this pins the read path: both counts reach
the rated rows through the partial index ``idx_global_ask_jobs_feedback``
(0076), the analytics count parses each rated payload once, and the admin
count reads the rated ids by index condition without touching any payload.
"""
from __future__ import annotations

import json

import pytest

from app.repositories.postgres.query_store import (
    QueryStore,
    global_feedback_counts_sql,
)
from app.repositories.global_ask_feedback_sql import (
    ADMIN_QUESTION_FEEDBACK_SUMS,
    admin_question_feedback_joins,
)
from tests.global_feedback_cases import (
    EXPECTED_ADMIN,
    EXPECTED_ANALYTICS,
    NOW,
    admin_feedback,
    admin_filters,
    analytics_tuple,
    seed,
)

pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def repo(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        with repository._runtime.database.write() as db:
            seed(db, "%s")
        yield repository
    finally:
        repository.close()


@pytest.mark.parametrize("notebook_id", sorted(EXPECTED_ANALYTICS))
def test_analytics_counts_global_ratings_by_attribution(repo, notebook_id):
    analytics = repo._runtime.queries.notebook_analytics(notebook_id)
    assert analytics_tuple(analytics) == EXPECTED_ANALYTICS[notebook_id]
    useful, not_useful = analytics.feedback_useful, analytics.feedback_not_useful
    assert analytics.usefulness_rate == round(useful / (useful + not_useful), 3)


def test_low_rated_questions_list_notebook_questions_only(repo):
    analytics = repo._runtime.queries.notebook_analytics("nb-fa")
    assert analytics.low_rated_questions == ["放大器为什么振荡？"]
    assert analytics.feedback_not_useful_global == 1
    only_global = repo._runtime.queries.notebook_analytics("nb-fb")
    assert only_global.low_rated_questions == []
    assert only_global.feedback_not_useful_global == 1


def test_only_the_first_rating_counts(repo):
    store = repo._runtime.global_ask_store
    assert store.set_feedback("gfb-4", "u-fb1", "useful").feedback == "useful"
    assert store.set_feedback("gfb-4", "u-fb1", "not_useful").feedback == "useful"
    assert analytics_tuple(repo._runtime.queries.notebook_analytics("nb-fa")) == (3, 2, 2, 1)
    stats = repo._runtime.queries.list_admin_questions()["stats"]
    assert admin_feedback(stats)["global_feedback_useful"] == 3
    assert admin_feedback(stats)["global_feedback_not_useful"] == 2


@pytest.mark.parametrize("name", sorted(EXPECTED_ADMIN))
def test_admin_stats_sum_notebook_and_global_ratings_under_the_filter(repo, name):
    stats = repo._runtime.queries.list_admin_questions(**admin_filters()[name])["stats"]
    assert admin_feedback(stats) == EXPECTED_ADMIN[name]


def test_admin_stats_reports_and_retained_rows_carry_no_rating(repo):
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO retained_user_activity(activity_type,record_id,actor_id,notebook_id,"
            "notebook_name,question,status,created_at,deleted_at,expires_at) "
            "VALUES('ask','ask-gone','u-fb1','nb-gone','已删','删前提问','completed',%s,"
            "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP + INTERVAL '1 day')",
            (NOW,),
        )
        db.execute(
            "INSERT INTO reports(id,notebook_id,question,created_by,status,created_at,updated_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)", ("rep-fb", "nb-fa", "报告", "u-fb1", "done", NOW, NOW))
    result = repo._runtime.queries.list_admin_questions()
    assert {"ask-gone", "rep-fb"} <= {item["id"] for item in result["items"]}
    assert admin_feedback(result["stats"]) == EXPECTED_ADMIN["all"]


def _plan(db, statement, params):
    rows = db.execute("EXPLAIN (FORMAT JSON) " + statement, params).fetchall()
    value = next(iter(rows[0].values())) if isinstance(rows[0], dict) else rows[0][0]
    return json.loads(value) if isinstance(value, str) else value


def _nodes(node):
    yield node
    for child in node.get("Plans", ()):
        yield from _nodes(child)


def _admin_stats() -> str:
    """The admin stats statement as ``list_admin_questions`` builds it, with
    no filter."""
    return (
        QueryStore._admin_questions_cte(feedback=True)
        + "SELECT COUNT(*) AS total," + ADMIN_QUESTION_FEEDBACK_SUMS
        + " FROM questions " + admin_question_feedback_joins("postgres") + "WHERE 1=1"
    )


def test_admin_stats_never_reads_a_global_payload(repo):
    """Counting the overview must not parse global payloads: with no text
    filter, the scan of the global arm projects no ``payload_json`` (the
    question stays an unused, dropped output). A join inside that arm once
    made it project the payload and parse every global job (+90% on a
    3000-job corpus)."""
    with repo._runtime.database.connect() as db:
        rows = db.execute("EXPLAIN (VERBOSE, FORMAT JSON) " + _admin_stats()).fetchall()
    value = next(iter(rows[0].values())) if isinstance(rows[0], dict) else rows[0][0]
    plan = json.loads(value) if isinstance(value, str) else value
    arm_scans = [
        n for n in _nodes(plan[0]["Plan"])
        if n.get("Relation Name") == "global_ask_jobs" and n.get("Alias") == "g"
    ]
    assert arm_scans, plan
    assert all(
        not any("payload_json" in column for column in n.get("Output", ())) for n in arm_scans
    ), arm_scans


def test_counts_can_only_be_answered_from_the_partial_index(repo):
    """With sequential scans priced out, the planner must still find a path:
    it can only be the partial index, i.e. both statements' predicates prove
    the index predicate. The analytics count recomputes no index condition on
    a heap row (``Index Cond`` on the rated set) and the admin read of the
    rated ids is two index conditions, one per rating."""
    statement, binds = global_feedback_counts_sql()
    with repo._runtime.database.write() as db:
        db.execute("SET LOCAL enable_seqscan = off")
        analytics = [n for n in _nodes(_plan(db, statement, ("nb-fa",) * binds)[0]["Plan"])]
        admin = [n for n in _nodes(_plan(db, _admin_stats(), ())[0]["Plan"])]

    assert any(n.get("Index Name") == "idx_global_ask_jobs_feedback" for n in analytics), analytics
    assert not [n for n in analytics if n["Node Type"] == "Seq Scan"
                and n.get("Relation Name") == "global_ask_jobs"], analytics
    rated_reads = [n for n in admin if n.get("Index Name") == "idx_global_ask_jobs_feedback"]
    assert sorted("'not_useful'" in n["Index Cond"] for n in rated_reads) == [False, True], admin
