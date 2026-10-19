"""Global Ask 👍 / 👎 in the notebook analytics panel and the admin question
overview (SQLite; the PostgreSQL twin is
``tests/postgres/test_global_ask_feedback_counts_pg.py``).

The rating lives only in ``global_ask_jobs.payload_json``; both statistics
read it beside the notebook ``feedback`` table, through the partial index
``idx_global_ask_jobs_feedback`` (SQLite v96), attributing a global rating to
the libraries the run searched or cited (``global_ask_attribution``).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.repositories.global_ask_feedback_sql import (
    ADMIN_QUESTION_FEEDBACK_SUMS,
    admin_question_feedback_joins,
    rated_predicate,
)
from app.repositories.sqlite.query_store import QueryStore, global_feedback_counts_sql
from app.services.sqlite_repository import SQLiteRepository
from tests.global_feedback_cases import (
    EXPECTED_ADMIN,
    EXPECTED_ANALYTICS,
    NOW,
    admin_feedback,
    admin_filters,
    analytics_tuple,
    seed,
)


@pytest.fixture
def repo(tmp_path):
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 't.db'}",
                        storage_dir=str(tmp_path / "s"))
    repo = SQLiteRepository(settings)
    with repo._write() as db:
        seed(db, "?")
    yield repo
    repo.close()


@pytest.mark.parametrize("notebook_id", sorted(EXPECTED_ANALYTICS))
def test_analytics_counts_global_ratings_by_attribution(repo, notebook_id):
    analytics = repo._runtime.queries.notebook_analytics(notebook_id)
    assert analytics_tuple(analytics) == EXPECTED_ANALYTICS[notebook_id]
    useful, not_useful = analytics.feedback_useful, analytics.feedback_not_useful
    assert analytics.usefulness_rate == round(useful / (useful + not_useful), 3)


def test_low_rated_questions_list_notebook_questions_only(repo):
    """A global 👎 is counted (``feedback_not_useful_global``) but its question
    text never enters the notebook's knowledge-gap list."""
    analytics = repo._runtime.queries.notebook_analytics("nb-fa")
    assert analytics.low_rated_questions == ["放大器为什么振荡？"]
    assert analytics.feedback_not_useful_global == 1
    only_global = repo._runtime.queries.notebook_analytics("nb-fb")
    assert only_global.low_rated_questions == []
    assert only_global.feedback_not_useful_global == 1


def test_only_the_first_rating_counts(repo):
    """``set_feedback`` is first-write-wins; the statistics read the stored
    rating, so a second click neither adds nor flips a count."""
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
    with repo._write() as db:
        db.execute(
            "INSERT INTO retained_user_activity(activity_type,record_id,actor_id,notebook_id,"
            "notebook_name,question,status,created_at,deleted_at,expires_at) "
            "VALUES('ask','ask-gone','u-fb1','nb-gone','已删','删前提问','completed',?,?,?)",
            (NOW, NOW, "2999-01-01T00:00:00+00:00"),
        )
        db.execute(
            "INSERT INTO reports(id,notebook_id,question,created_by,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)", ("rep-fb", "nb-fa", "报告", "u-fb1", "done", NOW, NOW))
    result = repo._runtime.queries.list_admin_questions()
    assert {"ask-gone", "rep-fb"} <= {item["id"] for item in result["items"]}
    assert admin_feedback(result["stats"]) == EXPECTED_ADMIN["all"]


def test_admin_page_rows_are_unchanged_by_the_feedback_columns(repo):
    """The page query keeps the feedback-free CTE: no rating key leaks into an
    item and the rows are the same with or without ratings."""
    items = repo._runtime.queries.list_admin_questions()["items"]
    assert all("answer_id" not in item and "feedback" not in item for item in items)
    assert "answer_id AS answer_id" not in QueryStore._admin_questions_cte(feedback=False)


def test_partial_index_predicate_is_the_counted_predicate(repo):
    """SQLite uses a partial index only when the query repeats its WHERE term,
    so the migration's spelling and ``rated_predicate`` must stay one."""
    with repo._runtime.database.connect() as db:
        sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='idx_global_ask_jobs_feedback'"
        ).fetchone()[0]
    assert sql.rstrip().endswith(f"WHERE {rated_predicate('sqlite')}")


def test_counts_read_the_partial_index_not_the_table(repo):
    statement, binds = global_feedback_counts_sql()
    stats = (
        QueryStore._admin_questions_cte(feedback=True) + "SELECT " + ADMIN_QUESTION_FEEDBACK_SUMS
        + " FROM questions " + admin_question_feedback_joins("sqlite") + "WHERE 1=1"
    )
    with repo._runtime.database.connect() as db:
        plan = " | ".join(
            row[3] for row in db.execute("EXPLAIN QUERY PLAN " + statement, ("nb-fa",) * binds)
        )
        admin_plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + stats)]
    assert "USING INDEX idx_global_ask_jobs_feedback" in plan
    assert "SCAN global_ask_jobs" not in plan
    rated_reads = [line for line in admin_plan if "idx_global_ask_jobs_feedback" in line]
    assert len(rated_reads) == 2, admin_plan


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'fb.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    from app.api import deps
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    deps.repository.cache_clear()
    return TestClient(create_app())


def _register(client, username):
    body = client.post(
        "/api/auth/register", json={"username": username, "password": "pw"}
    ).json()
    return {"Authorization": f"Bearer {body['token']}"}, body["user"]["id"]


def test_analytics_endpoint_counts_without_revealing_other_libraries(client):
    """The panel stays behind notebook read access, and a global rating merged
    into a library carries only numbers: no other participant's id or name."""
    owner, owner_id = _register(client, "d00000071")
    stranger, stranger_id = _register(client, "d00000072")
    mine = client.post("/api/notebooks", headers=owner, json={"name": "我的库"}).json()
    theirs = client.post("/api/notebooks", headers=stranger, json={"name": "别人的秘密库"}).json()
    from app.api.deps import repository
    from tests.global_feedback_cases import global_payload

    with repository()._write() as db:
        db.execute(
            "INSERT INTO global_ask_conversations(id,user_id,title,scope_json,submitted_via,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("gconv-x", stranger_id, "", "{}", "web", NOW, NOW))
        db.execute(
            "INSERT INTO global_ask_jobs(id,conversation_id,user_id,client_request_id,"
            "request_json,status,payload_json,created_at,submitted_via,asked_at,updated_at,"
            "error_detail,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("gask-x", "gconv-x", stranger_id, None, "{}", "done",
             global_payload("gask-x", stranger_id, [mine["id"], theirs["id"]],
                            [mine["id"], theirs["id"]], [], [], "not_useful",
                            question="秘密问题原文"),
             NOW, "web", "", NOW, "", "reasoning"))

    response = client.get(f"/api/notebooks/{mine['id']}/analytics", headers=owner)
    assert response.status_code == 200
    body = response.json()
    assert (body["feedback_not_useful"], body["feedback_not_useful_global"]) == (1, 1)
    assert body["low_rated_questions"] == []
    for leaked in (theirs["id"], "别人的秘密库", "秘密问题原文", "gask-x"):
        assert leaked not in response.text
    # Someone without read access on the library sees no statistics at all.
    outsider, _ = _register(client, "d00000073")
    assert client.get(f"/api/notebooks/{mine['id']}/analytics",
                      headers=outsider).status_code in {403, 404}
