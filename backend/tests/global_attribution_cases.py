"""Shared cases for the physical global Ask attribution (SQLite v98 /
PostgreSQL 0078): ``global_ask_job_notebooks`` + ``global_ask_jobs.attributed``.

Used by ``tests/test_global_ask_attribution.py`` (SQLite) and
``tests/postgres/test_global_ask_attribution_pg.py`` (PostgreSQL). Every case
is a global job shape the attribution rule (``global_ask_attribution``) has to
get right; the expectations are written by hand and the parity checks compare
the stored rows with ``attribution_sql`` row by row, so the two spellings of
the rule cannot drift apart without a red test.
"""
from __future__ import annotations

import json

from app.domain.global_ask_attribution import attribution_sql
from app.domain.global_ask_retention import live_turn_sql
from app.models.global_ask import GlobalAskJob
from app.services.global_ask_attribution_backfill import run_backfill

NOW = "2026-10-19T08:00:00+00:00"
USER = "u-ga1"
CONVERSATION = "gaconv-1"
NOTEBOOKS = ("nb-ga", "nb-gb", "nb-gc", "nb-gd", "nb-ge")
NOTEBOOK_QUESTION = "笔记本内的提问"

# (job_id, status, resolved, searched, cited, skipped)
JOBS = (
    ("gaj-01", "done", ["nb-ga", "nb-gb"], ["nb-ga", "nb-gb"], [], []),
    ("gaj-02", "done", ["nb-ga", "nb-gb"], ["nb-gb"], [], []),
    # No federated call: participants minus the skipped ones.
    ("gaj-03", "done", ["nb-ga", "nb-gc"], [], [], ["nb-gc"]),
    ("gaj-04", "done", ["nb-gc"], [], ["nb-gc"], []),
    ("gaj-05", "done", ["nb-ga", "nb-gb", "nb-gc"], [], [], []),
    # Searched a library outside the participants: attributed to nothing.
    ("gaj-06", "done", ["nb-ga", "nb-gb"], ["nb-gd"], [], []),
    # Cited beats skipped.
    ("gaj-07", "done", ["nb-ga", "nb-gb"], [], ["nb-gb"], ["nb-gb"]),
    # A duplicated participant is one row.
    ("gaj-08", "done", ["nb-ga", "nb-ga", "nb-gb"], ["nb-ga"], [], []),
    # Every participant skipped: attributed, but to no library.
    ("gaj-09", "done", ["nb-ga", "nb-gb"], [], [], ["nb-ga", "nb-gb"]),
    # Not finished: no answer, counted nowhere, never given rows.
    ("gaj-10", "failed", ["nb-ga"], ["nb-ga"], [], []),
    ("gaj-11", "cancelled", ["nb-ga"], ["nb-ga"], [], []),
    ("gaj-12", "interrupted", ["nb-ga"], ["nb-ga"], [], []),
    ("gaj-13", "running", ["nb-ga"], ["nb-ga"], [], []),
    # Done but past its retention stamp: counted nowhere.
    ("gaj-14", "done", ["nb-gb"], ["nb-gb"], [], []),
)
EXPIRED_JOBS = ("gaj-14",)
# A payload written before the coverage keys existed: the absent lists read
# as empty, so the fallback attributes it to its participant.
LEGACY_JOB = ("gaj-15", ["nb-gd"])
# A payload whose ``searched_notebook_ids`` is JSON null does not read back as
# a ``GlobalAskJob``; the backfill reports it and leaves it at attributed=0,
# where the payload predicate still counts it (null reads as empty there).
UNREADABLE_JOB = ("gaj-16", ["nb-ge"])

# job -> attributed libraries (participant order), done jobs only.
EXPECTED_ROWS = {
    "gaj-01": ["nb-ga", "nb-gb"],
    "gaj-02": ["nb-gb"],
    "gaj-03": ["nb-ga"],
    "gaj-04": ["nb-gc"],
    "gaj-05": ["nb-ga", "nb-gb", "nb-gc"],
    "gaj-06": [],
    "gaj-07": ["nb-gb"],
    "gaj-08": ["nb-ga"],
    "gaj-09": [],
    "gaj-14": ["nb-gb"],
    "gaj-15": ["nb-gd"],
}
# notebook -> global answers counted on its analytics panel (live done jobs).
EXPECTED_GLOBAL_ANSWERS = {
    "nb-ga": 4,  # 01, 03, 05, 08
    "nb-gb": 4,  # 01, 02, 05, 07 (14 expired)
    "nb-gc": 2,  # 04, 05
    "nb-gd": 1,  # 15
    "nb-ge": 1,  # 16, through the payload predicate only
}
NOTEBOOK_ANSWERS = {"nb-ga": 1}


def payload(job_id, status, resolved, searched, cited, skipped) -> dict:
    return {
        "job_id": job_id, "conversation_id": CONVERSATION, "status": status,
        "question": f"全局问题 {job_id}", "created_at": NOW, "mode": "reasoning",
        "notebook_scope": {"mode": "include", "notebook_ids": list(dict.fromkeys(resolved))},
        "resolved_notebook_ids": list(resolved),
        "searched_notebook_ids": list(searched),
        "cited_notebook_ids": list(cited),
        "skipped_notebooks": [{"notebook_id": nb, "reason": "timeout"} for nb in skipped],
        "answer": None,
    }


def all_payloads() -> dict[str, str]:
    """job id -> stored payload text, every case."""
    payloads = {job[0]: json.dumps(payload(*job), ensure_ascii=False) for job in JOBS}
    legacy_id, legacy_resolved = LEGACY_JOB
    legacy = payload(legacy_id, "done", legacy_resolved, [], [], [])
    for key in ("searched_notebook_ids", "cited_notebook_ids", "skipped_notebooks"):
        legacy.pop(key)
    payloads[legacy_id] = json.dumps(legacy, ensure_ascii=False)
    broken_id, broken_resolved = UNREADABLE_JOB
    broken = payload(broken_id, "done", broken_resolved, [], [], [])
    broken["searched_notebook_ids"] = None
    payloads[broken_id] = json.dumps(broken, ensure_ascii=False)
    return payloads


def statuses() -> dict[str, str]:
    result = {job[0]: job[1] for job in JOBS}
    result[LEGACY_JOB[0]] = "done"
    result[UNREADABLE_JOB[0]] = "done"
    return result


def seed_base(db, q: str) -> None:
    """Users, notebooks, the conversation and one in-notebook answer."""
    def run(sql, params):
        db.execute(sql.replace("?", q), params)

    run("INSERT INTO users(id,email,display_name,role,status,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?)", (USER, f"{USER}@x", USER, "user", "active", NOW, NOW))
    for nb in NOTEBOOKS:
        run("INSERT INTO notebooks(id,name,created_by,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)", (nb, f"名-{nb}", USER, "ready", NOW, NOW))
    run("INSERT INTO answers(id,notebook_id,question,payload,created_at) VALUES(?,?,?,?,?)",
        ("ans-ga1", "nb-ga", NOTEBOOK_QUESTION, "{}", NOW))
    run("INSERT INTO global_ask_conversations(id,user_id,title,scope_json,submitted_via,"
        "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (CONVERSATION, USER, "", "{}", "web", NOW, NOW))


def seed_legacy_jobs(db, q: str) -> None:
    """Every case as a row written BEFORE the migration: ``attributed = 0``
    and no attribution rows (the shape an upgraded database holds)."""
    def run(sql, params):
        db.execute(sql.replace("?", q), params)

    texts = all_payloads()
    for job_id, status in statuses().items():
        run("INSERT INTO global_ask_jobs(id,conversation_id,user_id,client_request_id,"
            "request_json,status,payload_json,created_at,submitted_via,asked_at,updated_at,"
            "error_detail,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, CONVERSATION, USER, None, "{}", status, texts[job_id], NOW, "web", "",
             NOW, "", "reasoning"))
    for job_id in EXPIRED_JOBS:
        run("UPDATE global_ask_jobs SET retained_until=? WHERE id=?",
            ("2026-01-01T00:00:00+00:00", job_id))


def stamp_expired(db, q: str) -> None:
    for job_id in EXPIRED_JOBS:
        db.execute(f"UPDATE global_ask_jobs SET retained_until={q} WHERE id={q}",
                   ("2026-01-01T00:00:00+00:00", job_id))


def create_and_finish(store) -> None:
    """Every readable case through the real write path: ``create`` (running)
    then ``save`` with the case's terminal status."""
    texts = all_payloads()
    # One running job per conversation: the one that stays running goes last.
    ordered = sorted(statuses().items(), key=lambda item: item[1] == "running")
    for job_id, status in ordered:
        if job_id == UNREADABLE_JOB[0]:
            continue
        job = GlobalAskJob.model_validate_json(texts[job_id])
        job.status = "running"
        store.create(job, USER, None, "{}", "web", new_conversation=False)
        if status != "running":
            job.status = status
            assert store.save(job, USER) is True


def stored_rows(db) -> dict[str, list[str]]:
    rows = db.execute(
        "SELECT job_id, notebook_id FROM global_ask_job_notebooks ORDER BY job_id, notebook_id"
    ).fetchall()
    result: dict[str, list[str]] = {}
    for row in rows:
        result.setdefault(row["job_id"], []).append(row["notebook_id"])
    return result


def attributed_flags(db) -> dict[str, int]:
    return {
        row["id"]: int(row["attributed"])
        for row in db.execute("SELECT id, attributed FROM global_ask_jobs").fetchall()
    }


def assert_rows_match_attribution_sql(db, *, dialect: str, q: str) -> None:
    """The stored rows of every ``attributed = 1`` job are exactly the
    libraries ``attribution_sql`` attributes it to -- for every notebook of
    the universe, so a library the predicate rejects never has a row either."""
    fragment, binds = attribution_sql(dialect=dialect, marker=q)
    rows = stored_rows(db)
    flags = attributed_flags(db)
    checked = 0
    for job_id, flag in flags.items():
        if not flag:
            assert job_id not in rows, job_id
            continue
        for notebook_id in NOTEBOOKS:
            hit = db.execute(
                f"SELECT 1 FROM global_ask_jobs WHERE id={q} AND {fragment}",
                (job_id, *([notebook_id] * binds)),
            ).fetchone() is not None
            assert hit == (notebook_id in rows.get(job_id, [])), (job_id, notebook_id)
            checked += 1
    assert checked


def expected_rows_sorted() -> dict[str, list[str]]:
    return {job: sorted(ids) for job, ids in EXPECTED_ROWS.items() if ids}


def backfill(maintenance, **kwargs):
    return run_backfill(maintenance, **kwargs)


def answers(analytics) -> tuple[int, int]:
    return analytics.answers_total, analytics.answers_global


def expected_answers(notebook_id: str) -> tuple[int, int]:
    global_count = EXPECTED_GLOBAL_ANSWERS[notebook_id]
    return NOTEBOOK_ANSWERS.get(notebook_id, 0) + global_count, global_count


def live_done_count_via_payload(db, notebook_id: str, *, dialect: str, q: str) -> int:
    """The reference count: every live done job the payload predicate
    attributes to ``notebook_id`` -- what the panel counted before v98."""
    fragment, binds = attribution_sql(dialect=dialect, marker=q)
    row = db.execute(
        f"SELECT COUNT(*) AS c FROM global_ask_jobs WHERE status='done' "
        f"AND {live_turn_sql(dialect=dialect)} AND {fragment}",
        (notebook_id,) * binds,
    ).fetchone()
    return int(row["c"])
