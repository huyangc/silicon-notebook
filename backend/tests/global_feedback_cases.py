"""Shared seed and expectations for the global 👍 / 👎 aggregation (SQLite
``tests/test_global_ask_feedback_counts.py``; PostgreSQL
``tests/postgres/test_global_ask_feedback_counts_pg.py``).

One notebook-side rating pair and six global jobs that exercise the
attribution rule (``global_ask_attribution``): searched / cited libraries
count; a resolved-only library does not; a run with no federated call falls
back to resolved minus skipped; an unrated job and a job written before the
``feedback`` key existed count nowhere.
"""
from __future__ import annotations

import json

NOW = "2026-10-19T08:00:00+00:00"
USERS = ("u-fb1", "u-fb2")
NOTEBOOKS = ("nb-fa", "nb-fb", "nb-fc")
NOTEBOOK_QUESTION = "放大器为什么振荡？"
GLOBAL_QUESTION = "跨库比较两种滤波器的噪声"

# (job_id, user, resolved, searched, cited, skipped, feedback or None = no key)
GLOBAL_JOBS = (
    ("gfb-1", "u-fb1", ["nb-fa", "nb-fb"], ["nb-fa", "nb-fb"], [], [], "useful"),
    ("gfb-2", "u-fb1", ["nb-fa", "nb-fb"], ["nb-fb"], [], [], "not_useful"),
    ("gfb-3", "u-fb1", ["nb-fa", "nb-fc"], [], [], ["nb-fc"], "not_useful"),
    ("gfb-4", "u-fb1", ["nb-fa"], ["nb-fa"], [], [], ""),
    ("gfb-5", "u-fb1", ["nb-fa"], ["nb-fa"], [], [], None),
    ("gfb-6", "u-fb2", ["nb-fc"], [], ["nb-fc"], [], "useful"),
    # Searched nb-fb, but past its retention stamp (a deleted participant's
    # window ran out): listed nowhere, so counted nowhere.
    ("gfb-7", "u-fb1", ["nb-fb"], ["nb-fb"], [], [], "useful"),
)
EXPIRED_JOBS = ("gfb-7",)

# notebook -> (useful, not_useful, useful_global, not_useful_global)
EXPECTED_ANALYTICS = {
    "nb-fa": (2, 2, 1, 1),   # fb table 1+1; gfb-1 searched, gfb-3 fallback
    "nb-fb": (1, 1, 1, 1),   # gfb-1 and gfb-2 searched it
    "nb-fc": (1, 0, 1, 0),   # gfb-6 cited it; gfb-3 skipped it
}

# Admin overview stats per filter: each global job counts ONCE (it is one
# listed question), whatever number of libraries it is attributed to.
EXPECTED_ADMIN = {
    "all": dict(feedback_useful=3, feedback_not_useful=3,
                global_feedback_useful=2, global_feedback_not_useful=2),
    "global": dict(feedback_useful=2, feedback_not_useful=2,
                   global_feedback_useful=2, global_feedback_not_useful=2),
    "notebook": dict(feedback_useful=1, feedback_not_useful=1,
                     global_feedback_useful=0, global_feedback_not_useful=0),
    "user2": dict(feedback_useful=1, feedback_not_useful=0,
                  global_feedback_useful=1, global_feedback_not_useful=0),
    "query": dict(feedback_useful=1, feedback_not_useful=0,
                  global_feedback_useful=1, global_feedback_not_useful=0),
}


def global_payload(job_id, user, resolved, searched, cited, skipped, feedback, *,
                   question):
    payload = {
        "job_id": job_id, "conversation_id": f"gfbconv-{user}", "status": "done",
        "question": question, "created_at": NOW, "mode": "reasoning",
        "notebook_scope": {"mode": "include", "notebook_ids": list(resolved)},
        "resolved_notebook_ids": list(resolved),
        "searched_notebook_ids": list(searched),
        "cited_notebook_ids": list(cited),
        "skipped_notebooks": [{"notebook_id": nb, "reason": "timeout"} for nb in skipped],
        "answer": None,
    }
    if feedback is not None:
        payload["feedback"] = feedback
        payload["feedback_at"] = NOW if feedback else ""
    return json.dumps(payload, ensure_ascii=False)


def seed(db, q: str) -> None:
    """Insert the whole case set through one connection; ``q`` is the
    backend's placeholder (``?`` / ``%s``)."""
    def run(sql, params):
        db.execute(sql.replace("?", q), params)

    for user in USERS:
        run("INSERT INTO users(id,email,display_name,role,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)", (user, f"{user}@x", user, "user", "active", NOW, NOW))
    for nb in NOTEBOOKS:
        run("INSERT INTO notebooks(id,name,created_by,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)", (nb, f"名-{nb}", "u-fb1", "ready", NOW, NOW))
    run("INSERT INTO answers(id,notebook_id,question,payload,created_at) VALUES(?,?,?,?,?)",
        ("ans-fb1", "nb-fa", NOTEBOOK_QUESTION, "{}", NOW))
    run("INSERT INTO ask_jobs(id,notebook_id,conversation_id,created_by,mode,question,status,"
        "answer_id,error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("ask-fb1", "nb-fa", "", "u-fb1", "chunk", NOTEBOOK_QUESTION, "completed",
         "ans-fb1", "", NOW, NOW))
    for fid, rating in (("fb-1", "useful"), ("fb-2", "not_useful")):
        run("INSERT INTO feedback(id,answer_id,notebook_id,rating,comment,created_at) "
            "VALUES(?,?,?,?,?,?)", (fid, "ans-fb1", "nb-fa", rating, "", NOW))
    for user in USERS:
        run("INSERT INTO global_ask_conversations(id,user_id,title,scope_json,submitted_via,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (f"gfbconv-{user}", user, "", "{}", "web", NOW, NOW))
    for job_id, user, resolved, searched, cited, skipped, feedback in GLOBAL_JOBS:
        question = GLOBAL_QUESTION if job_id == "gfb-1" else f"全局问题 {job_id}"
        run("INSERT INTO global_ask_jobs(id,conversation_id,user_id,client_request_id,"
            "request_json,status,payload_json,created_at,submitted_via,asked_at,updated_at,"
            "error_detail,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, f"gfbconv-{user}", user, None, "{}", "done",
             global_payload(job_id, user, resolved, searched, cited, skipped, feedback,
                            question=question),
             NOW, "web", "", NOW, "", "reasoning"))
    for job_id in EXPIRED_JOBS:
        run("UPDATE global_ask_jobs SET retained_until=? WHERE id=?",
            ("2026-01-01T00:00:00+00:00", job_id))


def analytics_tuple(analytics) -> tuple[int, int, int, int]:
    return (analytics.feedback_useful, analytics.feedback_not_useful,
            analytics.feedback_useful_global, analytics.feedback_not_useful_global)


def admin_feedback(stats) -> dict[str, int]:
    return {key: stats[key] for key in EXPECTED_ADMIN["all"]}


def admin_filters():
    """filter name -> list_admin_questions kwargs."""
    return {
        "all": {},
        "global": {"scope": "global"},
        "notebook": {"scope": "notebook"},
        "user2": {"user_id": "u-fb2"},
        "query": {"query": "滤波器"},
    }
