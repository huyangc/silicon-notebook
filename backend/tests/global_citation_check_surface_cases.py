"""Backend-agnostic cases: a global answer's citation check on every stored-row surface.

PR-D's product rule: a failed terminal citation check never voids the answer.
The answer is delivered whole, the turn carries ``citation_check`` (only when
``failed > 0``) and each failed citation/anchor carries ``verification``. Every
surface that reads a finished global turn back from ``global_ask_jobs`` must
therefore hand both through unchanged -- the owner's job and conversation reads
(which the SSE terminal frame reuses), the public share snapshot, the
administrator's activity detail and MCP ``get_global_ask`` -- and a clean turn
must carry neither key anywhere.

Same arrangement as ``global_ask_share_cases``: the store is one implementation
serving SQLite and PostgreSQL, so the cases live here once and both
``tests/test_global_citation_check_surfaces.py`` (SQLite) and
``tests/postgres/test_global_citation_check_surfaces.py`` (PostgreSQL) run them.
Rows are inserted directly as the worker would have persisted them.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from app.api.admin_routes import _global_ask_detail
from app.api.mcp_tools.global_ask import _job_page
from app.models.ask import PublicConversation
from app.services.conversation_public_view import public_conversation_payload
from app.services.global_ask import _public_turn_row
from tests.global_ask_share_cases import insert_conversation, insert_job

CHECK = {
    "outcome": "partial", "checked": 2, "failed": 1,
    "changed": 1, "source_gone": 0, "unverifiable": 0,
}
_NEW_KEYS = {"citation_check", "verification"}


def _source_of(element_id: str) -> str:
    """``el-<tag>`` is cited from ``src-<tag>`` (``test_memory_mcp._seed_cited_source``'s
    naming), so the HTTP drill-down can open a really seeded element."""
    return "src-" + element_id.removeprefix("el-")


def _anchor(key: str, element_id: str, notebook_id: str, **extra: Any) -> dict:
    return {
        "key": key, "object_id": f"obj-{key}", "object_type": "passage",
        "label": f"标签-{key}", "snippet": f"摘录-{key}",
        "source_title": f"资料-{key}", "location_label": f"第 {key} 节",
        "source_id": _source_of(element_id), "element_id": element_id,
        "notebook_id": notebook_id, **extra,
    }


def _citation(element_id: str, notebook_id: str, **extra: Any) -> dict:
    return {
        "label": f"引用-{element_id}", "source_id": _source_of(element_id),
        "element_id": element_id, "location_label": "第 1 页",
        "quoted_span": f"原句-{element_id}", "notebook_id": notebook_id, **extra,
    }


def answer(notebook_id: str, *, partial: bool, flagged: str = "el-flagged",
           clean: str = "el-clean", images: dict | None = None) -> dict:
    """A stored engine answer citing ``flagged`` and ``clean``.

    ``partial`` marks ``flagged`` as ``changed`` on both its anchor and its
    citation and records the summary; otherwise the same answer, unmarked.
    ``images`` maps an element id to the image list its anchor carries.
    """
    mark = {"verification": "changed"} if partial else {}
    images = images or {}
    body = {
        "answer_id": "", "conclusion": "结论", "answer": "甲 [k1],乙 [k2]。",
        "grounded": not partial, "evidence_level": "overview" if partial else "grounded",
        "asked_at": "2026-01-01T00:00:00+08:00",
        "answered_at": "2026-01-01T00:00:02+08:00",
        "anchors": [
            _anchor("k1", flagged, notebook_id, images=images.get(flagged, []), **mark),
            _anchor("k2", clean, notebook_id, images=images.get(clean, [])),
        ],
        "citations": [
            _citation(flagged, notebook_id, **mark),
            _citation(clean, notebook_id),
        ],
    }
    if partial:
        body["citation_check"] = dict(CHECK)
    return body


def job_payload(job_id: str, conversation_id: str, notebook_id: str, body: dict) -> dict:
    """A persisted ``GlobalAskJob`` whose ``answer`` is ``body``."""
    return {
        "job_id": job_id, "conversation_id": conversation_id, "status": "done",
        "question": f"问题-{job_id}", "created_at": "2026-01-01T00:00:01",
        "notebook_scope": {"mode": "all", "notebook_ids": []},
        "resolved_notebook_ids": [notebook_id], "searched_notebook_ids": [notebook_id],
        "cited_notebook_ids": [notebook_id], "mode": "reasoning", "answer": body,
    }


def keys_anywhere(value: Any) -> set[str]:
    """Every mapping key at any depth of a JSON-shaped value."""
    if isinstance(value, dict):
        return set(value).union(*(keys_anywhere(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(keys_anywhere(item) for item in value))
    return set()


def _seed(store) -> tuple[str, str]:
    conversation_id, user_id = insert_conversation(store, "gconv-check", "user-check")
    insert_job(store, conversation_id, user_id, "gjob-partial", "2026-01-01T00:00:01",
               payload=job_payload("gjob-partial", conversation_id, "nb-check",
                                   answer("nb-check", partial=True)))
    insert_job(store, conversation_id, user_id, "gjob-clean", "2026-01-01T00:00:02",
               payload=job_payload("gjob-clean", conversation_id, "nb-check",
                                   answer("nb-check", partial=False)))
    return conversation_id, user_id


def _assert_partial(body: dict) -> None:
    assert body["answer"] == "甲 [k1],乙 [k2]。"
    assert body["citation_check"] == CHECK
    assert [row.get("verification") for row in body["citations"]] == ["changed", None]
    assert [row.get("verification") for row in body["anchors"]] == ["changed", None]


def case_owner_job_read_carries_the_markers(store):
    _, user_id = _seed(store)
    _assert_partial(store.job("gjob-partial", user_id).model_dump(mode="json")["answer"])
    clean = store.job("gjob-clean", user_id).model_dump(mode="json")
    assert not _NEW_KEYS & keys_anywhere(clean)


def case_conversation_page_carries_the_markers(store):
    conversation_id, user_id = _seed(store)
    turns = {job.job_id: job.model_dump(mode="json")
             for job in store.jobs(conversation_id, user_id, 10, 0)}
    _assert_partial(turns["gjob-partial"]["answer"])
    assert not _NEW_KEYS & keys_anywhere(turns["gjob-clean"])


def case_public_snapshot_carries_the_markers(store):
    conversation_id, user_id = _seed(store)
    token = store.share_conversation(conversation_id, user_id)["share_token"]
    row = store.public_conversation_by_token(token)
    projected = PublicConversation(**public_conversation_payload(
        {"title": row["title"], "created_at": row["created_at"],
         "shared_through_at": row["shared_through_at"],
         "turns": [_public_turn_row(job) for job in row["jobs"]]},
        share_token=token, images_enabled=True,
    )).model_dump(mode="json")
    partial, clean = projected["turns"]
    assert partial["answer_md"] == "甲 [k1],乙 [k2]。"
    assert partial["citation_check"] == CHECK
    assert [ref.get("verification") for ref in partial["references"]] == ["changed", None]
    assert partial["references"][0]["snippet"] == "摘录-k1"
    assert not _NEW_KEYS & keys_anywhere(clean)


def case_admin_detail_carries_the_markers(store):
    _, user_id = _seed(store)
    admin = SimpleNamespace(id="admin", role="admin")
    details = {}
    for job_id in ("gjob-partial", "gjob-clean"):
        with store.guarded_admin_job_record(job_id, user_id, reader_id=None) as record:
            details[job_id] = _global_ask_detail(record, viewer=admin).model_dump(mode="json")
    _assert_partial(details["gjob-partial"]["answer"])
    assert not _NEW_KEYS & keys_anywhere(details["gjob-clean"])


def case_mcp_page_carries_the_summary_under_coverage(store):
    _, user_id = _seed(store)
    partial = _job_page(store.job("gjob-partial", user_id))
    assert partial["coverage"]["citation_check"] == CHECK
    assert [row.get("verification") for row in partial["citations"]] == ["changed", None]
    clean = _job_page(store.job("gjob-clean", user_id))
    assert not _NEW_KEYS & keys_anywhere(clean)
    assert "verification" not in json.dumps(clean, ensure_ascii=False)


CASES = [
    case_owner_job_read_carries_the_markers,
    case_conversation_page_carries_the_markers,
    case_public_snapshot_carries_the_markers,
    case_admin_detail_carries_the_markers,
    case_mcp_page_carries_the_summary_under_coverage,
]
CASE_IDS = [case.__name__.removeprefix("case_") for case in CASES]
