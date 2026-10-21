"""Backend-agnostic store cases for the Agent history reads of global conversations.

``GlobalAskStore`` is one implementation serving both backends, so the three
reads behind MCP ``list_conversations`` / ``get_conversation`` --
``all_conversations``, ``job_scope_rows`` and ``finished_job_at`` -- are pinned
by ONE case list that ``tests/test_global_ask_history_store.py`` (SQLite) and
``tests/postgres/test_global_ask_history_store_pg.py`` (PostgreSQL) both run.

The central pin is ``case_scope_projection_matches_the_snapshot_rule``: the
narrowed payload ``job_scope_rows`` rebuilds from ten JSON paths must give
``global_ask._snapshot_notebook_ids`` exactly the answer the whole stored
payload gives, on every payload shape a row can carry. That function is the
only definition of "which libraries a turn uses"; the store adds no rule.

``global_job_payload`` is the shared factory for a COMPLETE stored job payload
(``insert_job``'s default body does not pass ``GlobalAskJob.model_validate_json``);
the service cases and the MCP tool cases seed through it. Global conversation
ids always carry the ``gconv-`` prefix: ``insert_conversation``'s default
``conv-g`` would be routed as a notebook conversation by the MCP tools.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.services.global_ask import _snapshot_notebook_ids
from tests.global_ask_share_cases import insert_conversation, insert_job


def global_job_payload(job_id, conversation_id, *, status="done", question=None,
                       created_at="2026-01-01T00:00:00+00:00", resolved=(), cited=None,
                       anchors=(), citations=(), answer=None, error=None, mode="chunk"):
    """A complete stored ``GlobalAskJob`` payload, for ``insert_job(payload=...)``.

    ``resolved`` is what the turn ran over. ``anchors`` / ``citations`` list one
    entry per anchor / citation: a notebook id string (``""`` for evidence that
    names no library, like an external page) or a full dict used as given.
    ``cited`` defaults to the libraries the citations name. Only a ``done``
    turn carries an ``answer`` (``answer`` overrides its text).
    """
    resolved = list(resolved)

    def anchor(index, entry):
        if isinstance(entry, dict):
            return entry
        row = {"key": f"k{index + 1}", "object_id": f"OBJ-{job_id}-{index}",
               "object_type": "element", "label": f"锚点 {index + 1}",
               "snippet": "锚点摘录", "source_title": "论文"}
        if entry:
            row.update(notebook_id=entry, source_id=f"src-{entry}",
                       element_id=f"el-{entry}-{index}")
        return row

    def citation(index, entry):
        if isinstance(entry, dict):
            return entry
        row = {"label": f"来源 {index + 1}", "source_id": f"src-{entry or 'ext'}",
               "element_id": f"el-{entry or 'ext'}-{index}", "location_label": "p. 1",
               "quoted_span": f"摘录 {index + 1}"}
        if entry:
            row["notebook_id"] = entry
        return row

    cited_rows = [citation(index, entry) for index, entry in enumerate(citations)]
    if cited is None:
        cited = [row["notebook_id"] for row in cited_rows if row.get("notebook_id")]
    body = {
        "job_id": job_id, "conversation_id": conversation_id, "status": status,
        "question": question or f"问题 {job_id}", "created_at": created_at,
        "notebook_scope": {"mode": "include", "notebook_ids": resolved},
        "resolved_notebook_ids": resolved, "searched_notebook_ids": resolved,
        "cited_notebook_ids": list(dict.fromkeys(cited)), "error": error, "mode": mode,
    }
    if status == "done":
        text = answer if answer is not None else f"回答 {job_id}"
        body["answer"] = {
            "answer_id": f"ans-{job_id}", "conclusion": text, "answer": text,
            "grounded": True, "evidence_level": "grounded", "mode": mode,
            "conversation_id": conversation_id,
            "anchors": [anchor(index, entry) for index, entry in enumerate(anchors)],
            "citations": cited_rows,
        }
    return body


def _instant(minutes):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def _stamp(store, job_id, retained_until):
    with store.database.write() as db:
        db.execute(store._sql(
            "UPDATE global_ask_jobs SET retained_until=? WHERE id=?"
        ), (retained_until, job_id))


def _conversation(store, conversation_id, user_id, *, updated_at=None, title=None):
    insert_conversation(store, conversation_id, user_id)
    if updated_at is not None or title is not None:
        with store.database.write() as db:
            db.execute(store._sql(
                "UPDATE global_ask_conversations SET updated_at=COALESCE(?,updated_at),"
                "title=COALESCE(?,title) WHERE id=?"
            ), (updated_at, title, conversation_id))
    return conversation_id


def _job(store, conversation_id, user_id, job_id, created_at, *, status="done", **fields):
    return insert_job(store, conversation_id, user_id, job_id, created_at, status=status,
                      payload=global_job_payload(job_id, conversation_id, status=status,
                                                 created_at=created_at, **fields))


# ------------------------------------------------------------------ job_scope_rows


# Raw stored payloads, deliberately NOT all valid ``GlobalAskJob`` bodies: the
# projection reads what is stored, whatever its shape.
_SHAPES = {
    # Today's shape: top-level lists plus an ``answer`` whose anchors and
    # citations name libraries; one anchor is external evidence (no library).
    "sp-new": ({
        "resolved_notebook_ids": ["nb-a", "nb-b"], "cited_notebook_ids": ["nb-b"],
        "question": "正文不应被读出", "trace": [{"text": "轨迹"}],
        "answer": {
            "answer": "很长的回答正文",
            "anchors": [{"key": "k1", "notebook_id": "nb-c", "snippet": "摘录"},
                        {"key": "k2", "tier": "external", "url": "https://example.test/x"}],
            "citations": [{"notebook_id": "nb-d", "quoted_span": "摘录"}],
        },
    }, {"nb-a", "nb-b", "nb-c", "nb-d"}),
    # The legacy ``response`` shape, written before the top-level lists existed.
    "sp-legacy": ({
        "question": "旧问题",
        "response": {"answer": "旧回答", "resolved_notebook_ids": ["nb-e"],
                     "cited_notebook_ids": ["nb-f"],
                     "citations": [{"notebook_id": "nb-g", "quoted_span": "旧摘录"}]},
    }, {"nb-e", "nb-f", "nb-g"}),
    # Empty top-level lists fall back to the nested ones.
    "sp-empty-top": ({
        "resolved_notebook_ids": [], "cited_notebook_ids": [],
        "answer": {"resolved_notebook_ids": ["nb-h"], "cited_notebook_ids": ["nb-i"],
                   "anchors": []},
    }, {"nb-h", "nb-i"}),
    # Evidence that names no library contributes nothing.
    "sp-external": ({
        "resolved_notebook_ids": ["nb-j"],
        "answer": {"anchors": [{"key": "k1", "tier": "external"}],
                   "citations": [{"notebook_id": "", "tier": "external"}]},
    }, {"nb-j"}),
    # ``answer`` / ``response`` stored as null.
    "sp-null-answer": ({
        "resolved_notebook_ids": ["nb-k"], "answer": None, "response": None,
    }, {"nb-k"}),
    # ``anchors`` an object, ``citations`` a string: neither is an array.
    "sp-not-arrays": ({
        "resolved_notebook_ids": ["nb-l"],
        "answer": {"anchors": {"notebook_id": "nb-x"}, "citations": "nb-y"},
    }, {"nb-l"}),
    # Odd elements: a top-level list stored as a STRING that spells an array
    # (not a list, so the nested one wins), nulls and a number inside lists,
    # anchors that are not objects or whose library is null.
    "sp-odd": ({
        "resolved_notebook_ids": "[\"nb-z\"]", "cited_notebook_ids": [None, "nb-m", 7],
        "answer": {"resolved_notebook_ids": ["nb-n"],
                   "anchors": ["nb-o", None, {"notebook_id": None}, {"notebook_id": "nb-p"}]},
    }, {"nb-n", "nb-m", "7", "nb-p"}),
}


def case_scope_projection_matches_the_snapshot_rule(store):
    conversation_id, user_id = _conversation(store, "gconv-sp", "user-sp"), "user-sp"
    for index, (job_id, (payload, _)) in enumerate(_SHAPES.items()):
        insert_job(store, conversation_id, user_id, job_id,
                   f"2026-01-01T00:00:{index:02d}+00:00", payload=payload)

    rows = {row["job_id"]: row for row in store.job_scope_rows(user_id)}

    assert set(rows) == set(_SHAPES)
    for job_id, (payload, expected) in _SHAPES.items():
        narrowed = rows[job_id]["payload"]
        # Narrowed: only the paths the rule reads, never the body or trace.
        assert set(narrowed) == {"resolved_notebook_ids", "cited_notebook_ids",
                                 "answer", "response"}, job_id
        for shape in ("answer", "response"):
            assert set(narrowed[shape]) == {"resolved_notebook_ids", "cited_notebook_ids",
                                            "anchors", "citations"}, job_id
        assert "正文" not in json.dumps(narrowed, ensure_ascii=False), job_id
        assert _snapshot_notebook_ids([narrowed]) == _snapshot_notebook_ids([payload]), job_id
        assert _snapshot_notebook_ids([narrowed]) == expected, job_id


def case_scope_rows_skip_expired_turns_and_filter_by_conversation(store):
    _conversation(store, "gconv-s1", "user-a")
    _conversation(store, "gconv-s2", "user-a")
    _conversation(store, "gconv-s3", "user-b")
    # Inserted out of order; the tie at :02 is broken by id.
    _job(store, "gconv-s2", "user-a", "s2-a", "2026-01-01T00:00:01+00:00",
         status="running", resolved=["nb-2"])
    _job(store, "gconv-s1", "user-a", "s1-d", "2026-01-01T00:00:03+00:00", resolved=["nb-1"])
    _job(store, "gconv-s1", "user-a", "s1-c", "2026-01-01T00:00:02+00:00", resolved=["nb-1"])
    _job(store, "gconv-s1", "user-a", "s1-b", "2026-01-01T00:00:02+00:00", resolved=["nb-9"],
         status="failed")
    _job(store, "gconv-s1", "user-a", "s1-a", "2026-01-01T00:00:01+00:00", resolved=["nb-1"])
    _job(store, "gconv-s3", "user-b", "s3-a", "2026-01-01T00:00:01+00:00", resolved=["nb-3"])
    _stamp(store, "s1-c", _instant(-5))      # expired: gone from every read
    _stamp(store, "s1-d", _instant(60))      # retained, still live

    def seen(rows):
        return [(row["conversation_id"], row["job_id"], row["status"]) for row in rows]

    assert seen(store.job_scope_rows("user-a")) == [
        ("gconv-s1", "s1-a", "done"), ("gconv-s1", "s1-b", "failed"),
        ("gconv-s1", "s1-d", "done"), ("gconv-s2", "s2-a", "running"),
    ]
    assert seen(store.job_scope_rows("user-a", conversation_id="gconv-s2")) == [
        ("gconv-s2", "s2-a", "running"),
    ]
    assert store.job_scope_rows("user-a", conversation_id="gconv-s3") == []
    assert seen(store.job_scope_rows("user-b")) == [("gconv-s3", "s3-a", "done")]
    assert store.job_scope_rows("user-nobody") == []


# ------------------------------------------------------------------ finished_job_at


def case_finished_job_at_counts_oldest_first_and_skips_running(store):
    conversation_id, user_id = _conversation(store, "gconv-f", "user-f"), "user-f"
    # Insertion order disagrees with the canonical order on purpose.
    _job(store, conversation_id, user_id, "f-4", "2026-01-01T00:00:05+00:00", resolved=["nb-1"])
    _job(store, conversation_id, user_id, "f-run", "2026-01-01T00:00:04+00:00",
         status="running", resolved=["nb-1"])
    _job(store, conversation_id, user_id, "f-3", "2026-01-01T00:00:02+00:00",
         status="cancelled", resolved=["nb-1"])
    _job(store, conversation_id, user_id, "f-x", "2026-01-01T00:00:03+00:00", resolved=["nb-1"])
    _job(store, conversation_id, user_id, "f-2", "2026-01-01T00:00:02+00:00",
         status="interrupted", resolved=["nb-1"])
    _job(store, conversation_id, user_id, "f-1", "2026-01-01T00:00:01+00:00", resolved=["nb-1"])
    _stamp(store, "f-x", _instant(-5))

    read = [store.finished_job_at(conversation_id, user_id, index) for index in range(5)]

    assert [job.job_id if job else None for job in read] == ["f-1", "f-2", "f-3", "f-4", None]
    assert [job.status for job in read[:4]] == ["done", "interrupted", "cancelled", "done"]
    # A whole job, read the way every other job read does.
    assert read[0].answer.answer == "回答 f-1" and read[0].question == "问题 f-1"
    assert read[1].error == "服务已重启，请重新提交问题。"


def case_finished_job_at_past_the_end_is_none(store):
    _conversation(store, "gconv-p1", "user-p")
    _job(store, "gconv-p1", "user-p", "p1-run", "2026-01-01T00:00:01+00:00",
         status="running", resolved=["nb-1"])
    _conversation(store, "gconv-p2", "user-p")
    _job(store, "gconv-p2", "user-p", "p2-a", "2026-01-01T00:00:01+00:00", resolved=["nb-1"])
    _job(store, "gconv-p2", "user-p", "p2-b", "2026-01-01T00:00:02+00:00", resolved=["nb-1"])
    _stamp(store, "p2-b", _instant(-5))

    assert store.finished_job_at("gconv-p1", "user-p", 0) is None
    assert store.finished_job_at("gconv-p2", "user-p", 0).job_id == "p2-a"
    assert store.finished_job_at("gconv-p2", "user-p", 1) is None
    assert store.finished_job_at("gconv-p2", "someone-else", 0) is None
    assert store.finished_job_at("gconv-missing", "user-p", 0) is None


# ------------------------------------------------------------------ all_conversations


def case_all_conversations_is_the_unpaged_list(store):
    user_id = "user-l"
    _conversation(store, "gconv-l1", user_id, updated_at="2026-01-01T00:00:01+00:00")
    _conversation(store, "gconv-l2", user_id, updated_at="2026-01-01T00:00:05+00:00")
    # Tied with l2: broken by id, descending.
    _conversation(store, "gconv-l3", user_id, updated_at="2026-01-01T00:00:05+00:00")
    # Automatic title naming a first turn that has expired: shown as the
    # earliest live question on every list.
    _conversation(store, "gconv-l4", user_id, updated_at="2026-01-01T00:00:03+00:00",
                  title="问题 l4-a")
    _conversation(store, "gconv-gone", user_id, updated_at="2026-01-01T00:00:09+00:00")
    _conversation(store, "gconv-other", "user-m", updated_at="2026-01-01T00:00:09+00:00")
    for conversation_id in ("gconv-l1", "gconv-l2", "gconv-l3"):
        _job(store, conversation_id, user_id, f"{conversation_id}-a",
             "2026-01-01T00:00:01+00:00", resolved=["nb-1"])
    _job(store, "gconv-l4", user_id, "l4-a", "2026-01-01T00:00:01+00:00", resolved=["nb-1"])
    _job(store, "gconv-l4", user_id, "l4-b", "2026-01-01T00:00:02+00:00", resolved=["nb-1"])
    _job(store, "gconv-gone", user_id, "gone-a", "2026-01-01T00:00:01+00:00", resolved=["nb-1"])
    _job(store, "gconv-other", "user-m", "other-a", "2026-01-01T00:00:01+00:00",
         resolved=["nb-1"])
    _stamp(store, "l4-a", _instant(-5))
    _stamp(store, "gone-a", _instant(-5))

    unpaged = store.all_conversations(user_id)

    assert [item.id for item in unpaged] == ["gconv-l3", "gconv-l2", "gconv-l4", "gconv-l1"]
    assert unpaged == store.list_conversations(user_id, 100, 0)
    assert unpaged == [
        item for offset in range(0, 4, 2)
        for item in store.list_conversations(user_id, 2, offset)
    ]
    assert {item.id: item.title for item in unpaged}["gconv-l4"] == "问题 l4-b"
    assert store.all_conversations("user-nobody") == []


CASES = [
    case_scope_projection_matches_the_snapshot_rule,
    case_scope_rows_skip_expired_turns_and_filter_by_conversation,
    case_finished_job_at_counts_oldest_first_and_skips_running,
    case_finished_job_at_past_the_end_is_none,
    case_all_conversations_is_the_unpaged_list,
]
CASE_IDS = [case.__name__.removeprefix("case_") for case in CASES]
