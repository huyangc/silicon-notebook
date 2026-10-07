"""Conversation share disclosure and the public Memory marker (E7-5, ruling M4),
defined once.

``test_conversation_share_api.py`` (notebook-scoped cases),
``test_global_ask_share_api.py`` (global cases) and
``test_report_share_disclosure.py`` (the report page marker) run them on
SQLite; ``postgres/test_conversation_share_disclosure_pg.py`` runs every one of
them, unchanged, on PostgreSQL.  Everything goes through the real HTTP routes
with real users (the world of ``report_share_disclosure_cases``: the owner of a
shared notebook, a member Alice, the deployment admin) and real Memory: a
candidate confirmed through the Memory service plus its hidden projection
source from ``ingest_memory_source``.

Conversations are normally minted by the model-backed ``/ask`` pipeline, so
their rows are written directly (in each backend's dialect, through the global
store's ``_sql``) with answer payloads in the stored ``AskResponse`` shape; a
case controls exactly which Memory each turn cites.
"""
from __future__ import annotations

import json
from itertools import count
from typing import Any, Callable

from app.services.share_disclosure import SHARE_DISCLOSURE_REQUIRED
from tests.global_ask_share_cases import insert_conversation, insert_job
from tests.report_share_disclosure_cases import (
    User,
    World,
    make_memory,
    make_report,
    memory_ref,
    share as share_report,
    source_ref,
)

STALE = "这条会话已有变化，请刷新后重新分享。"
NO_ANSWER = "这条会话还没有已完成的回答，暂时无法分享。"
CREATORLESS = "这条会话缺少创建者，无法公开分享。"
_IDS = count(1)


# --- stored answer shapes ----------------------------------------------------


def memory_citation(world: World, key: str) -> dict:
    """A direct Memory citation as ``AskService._memory_citations`` stores it."""
    memory_id = world.memories[key][0]
    return {
        "label": f"Memory · 记忆 {key}", "source_id": "", "element_id": "",
        "location_label": "Memory", "quoted_span": f"记忆 {key} 摘录",
        "tier": "personal", "memory_id": memory_id,
    }


def memory_anchor(world: World, key: str, anchor_key: str) -> dict:
    """A Memory anchor (``object_type == "memory"``) as ``MemoryRetriever`` maps it."""
    return {
        "key": anchor_key, "object_id": world.memories[key][0],
        "object_type": "memory", "label": f"记忆 {key}", "name": f"记忆 {key}",
        "snippet": f"记忆 {key} 摘录", "source_title": "", "location_label": "Memory",
        "tier": "personal",
    }


def source_anchor(source_id: str, anchor_key: str, *, notebook_id: str = "",
                  title: str = "设计说明") -> dict:
    """An element anchor on ``source_id`` (a document, or a Memory projection)."""
    anchor = {
        "key": anchor_key, "object_id": f"el-{anchor_key}-{source_id}",
        "object_type": "element", "label": title, "name": title,
        "snippet": f"{title} 摘录", "source_title": title, "location_label": "段落",
        "source_id": source_id, "element_id": f"el-{anchor_key}-{source_id}",
        "tier": "personal",
    }
    if notebook_id:
        anchor["notebook_id"] = notebook_id
    return anchor


def answer(anchors: list[dict] = (), citations: list[dict] = ()) -> dict:
    """An ``AskResponse`` payload whose body binds every anchor by marker."""
    markers = " ".join(f"[{anchor['key']}]" for anchor in anchors)
    body = f"结论 {markers}。" if markers else "结论 [1]。"
    return {
        "conclusion": body, "answer": body, "evidence_level": "grounded",
        "anchors": list(anchors), "citations": list(citations),
    }


# --- notebook-scoped conversations ------------------------------------------


def seed_conversation(world: World, author: User | str, payloads: list[dict]) -> tuple[str, list[str]]:
    """A conversation of ``author`` in the world's notebook with one answer per
    payload, one second apart; returns ``(conversation id, answer ids)``."""
    creator = author if isinstance(author, str) else author.id
    cid = f"conv-d-{next(_IDS)}"
    sql = world.repo._runtime.global_ask_store._sql
    answer_ids = [f"{cid}-a{index}" for index in range(len(payloads))]
    with world.repo._runtime.database.write() as db:
        db.execute(sql(
            "INSERT INTO conversations (id, notebook_id, title, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)"
        ), (cid, world.notebook, "会话", creator, "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:30+00:00"))
        for index, payload in enumerate(payloads):
            db.execute(sql(
                "INSERT INTO answers (id, notebook_id, conversation_id, question, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)"
            ), (answer_ids[index], world.notebook, cid, f"问题 {index + 1}",
                json.dumps(payload, ensure_ascii=False),
                f"2026-01-01T00:00:{index + 1:02d}+00:00"))
    return cid, answer_ids


def add_answer(world: World, cid: str, payload: dict, second: int) -> str:
    answer_id = f"{cid}-late{second}"
    sql = world.repo._runtime.global_ask_store._sql
    with world.repo._runtime.database.write() as db:
        db.execute(sql(
            "INSERT INTO answers (id, notebook_id, conversation_id, question, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)"
        ), (answer_id, world.notebook, cid, "追问",
            json.dumps(payload, ensure_ascii=False),
            f"2026-01-01T00:00:{second:02d}+00:00"))
    return answer_id


def _share_path(world: World, cid: str) -> str:
    return f"/api/notebooks/{world.notebook}/conversations/{cid}/share"


def disclose(world: World, user: User, cid: str, through_id: str = ""):
    return world.client.get(
        f"{_share_path(world, cid)}/disclosure",
        params={"through_id": through_id}, headers=user.headers,
    )


def publish(world: World, user: User, cid: str, through_id: str = "",
            acknowledged: int | None = None, *, body: Any = "default"):
    if body == "default":
        body = {"expected_through_id": through_id}
        if acknowledged is not None:
            body["acknowledged_memory_count"] = acknowledged
    if body is None:
        return world.client.post(_share_path(world, cid), headers=user.headers)
    return world.client.post(_share_path(world, cid), json=body, headers=user.headers)


def share_state(world: World, cid: str) -> dict:
    return world.repo.conversation_share_state(world.notebook, cid)


def counts(memory_count: int, new_memory_count: int) -> dict:
    return {"memory_count": memory_count, "new_memory_count": new_memory_count}


def required(memory_count: int, new_memory_count: int) -> dict:
    return {"detail": {
        "code": SHARE_DISCLOSURE_REQUIRED,
        "memory_count": memory_count,
        "new_memory_count": new_memory_count,
    }}


def public_page(world: World, token: str):
    return world.client.get(f"/api/public/conversations/{token}")


def case_zero_memory_conversation_publishes_as_before(world: World) -> None:
    cid, (first,) = seed_conversation(world, world.alice, [
        answer([source_anchor(world.doc_source, "k1")]),
    ])
    assert disclose(world, world.alice, cid).json() == counts(0, 0)
    assert disclose(world, world.alice, cid, first).json() == counts(0, 0)
    # The browser's body without an acknowledgement, and no body at all.
    published = publish(world, world.alice, cid, first)
    assert published.status_code == 200, published.text
    assert set(published.json()) == {"share_token", "shared_through_at", "shared_through_id"}
    assert published.json()["shared_through_id"] == first
    again = publish(world, world.alice, cid, body=None)
    assert again.status_code == 200 and again.json() == published.json()
    page = public_page(world, published.json()["share_token"])
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert "is_memory" not in page.text
    assert [ref["title"] for ref in page.json()["turns"][0]["references"]] == ["设计说明"]


def case_count_is_distinct_memory_over_citations_anchors_and_projections(world: World) -> None:
    for key in ("a1", "a2", "a3"):
        make_memory(world, world.alice, key)
    make_memory(world, world.owner, "o1")
    cid, _ids = seed_conversation(world, world.alice, [
        answer(
            [memory_anchor(world, "a2", "k1"),
             source_anchor(world.memories["a3"][1], "k2"),
             source_anchor(world.doc_source, "k3"),
             # Another member's Memory projection is not the author's to disclose.
             source_anchor(world.memories["o1"][1], "k4")],
            [memory_citation(world, "a1")],
        ),
        answer([memory_anchor(world, "a2", "k1")], [memory_citation(world, "a1")]),
    ])
    assert disclose(world, world.alice, cid).json() == counts(3, 3)


def case_a_projection_hit_alone_is_counted(world: World) -> None:
    """Evidence from the author's Memory projection (no stored Memory id) is
    the author's Memory: the count resolves the cited source in one batch."""
    make_memory(world, world.alice, "a1")
    cid, (first,) = seed_conversation(world, world.alice, [
        answer([source_anchor(world.memories["a1"][1], "k1", title="记忆投影")]),
    ])
    assert disclose(world, world.alice, cid, first).json() == counts(1, 1)
    refused = publish(world, world.alice, cid, first)
    assert refused.status_code == 409 and refused.json() == required(1, 1)


def case_unacknowledged_share_is_refused_without_a_token(world: World) -> None:
    make_memory(world, world.alice, "a1")
    cid, (first,) = seed_conversation(world, world.alice, [
        answer(citations=[memory_citation(world, "a1")]),
    ])
    for attempt in (
        publish(world, world.alice, cid, first),                 # missing: not a 422
        publish(world, world.alice, cid, body=None),             # no body at all
        publish(world, world.alice, cid, first, acknowledged=0),
        publish(world, world.alice, cid, first, acknowledged=2),
    ):
        assert attempt.status_code == 409, attempt.text
        assert attempt.json() == required(1, 1)
        assert share_state(world, cid)["share_token"] == ""
    assert publish(world, world.alice, cid, first, acknowledged=-1).status_code == 422
    accepted = publish(world, world.alice, cid, first, acknowledged=1)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["shared_through_id"] == first


def case_new_count_is_relative_to_the_published_watermark(world: World) -> None:
    """``new_memory_count`` counts what the update adds to the page the link
    serves now -- not "count minus acknowledgement"."""
    for key in ("a1", "a2", "a3"):
        make_memory(world, world.alice, key)
    cid, (first,) = seed_conversation(world, world.alice, [
        answer([memory_anchor(world, "a1", "k1"), memory_anchor(world, "a2", "k2")]),
    ])
    assert publish(world, world.alice, cid, first, acknowledged=2).status_code == 200
    second = add_answer(world, cid, answer(
        [memory_anchor(world, "a1", "k1"), memory_anchor(world, "a3", "k2")]
    ), 20)
    assert disclose(world, world.alice, cid, second).json() == counts(3, 1)
    assert disclose(world, world.alice, cid).json() == counts(3, 1)
    # The boundary IS the watermark: nothing new.
    assert disclose(world, world.alice, cid, first).json() == counts(2, 0)
    for wrong in (None, 1, 2):
        refused = publish(world, world.alice, cid, second, acknowledged=wrong)
        assert refused.status_code == 409 and refused.json() == required(3, 1)
        assert share_state(world, cid)["shared_through_id"] == first
    advanced = publish(world, world.alice, cid, second, acknowledged=3)
    assert advanced.status_code == 200 and advanced.json()["shared_through_id"] == second
    assert disclose(world, world.alice, cid, second).json() == counts(3, 0)


def case_disclosure_writes_nothing(world: World) -> None:
    make_memory(world, world.alice, "a1")
    cid, (first,) = seed_conversation(world, world.alice, [
        answer(citations=[memory_citation(world, "a1")]),
    ])
    assert disclose(world, world.alice, cid, first).status_code == 200
    assert share_state(world, cid) == {
        "share_token": "", "shared_through_at": "", "shared_through_id": "",
    }
    published = publish(world, world.alice, cid, first, acknowledged=1).json()
    later = add_answer(world, cid, answer(citations=[memory_citation(world, "a1")]), 20)
    assert disclose(world, world.alice, cid, later).json() == counts(1, 0)
    assert share_state(world, cid) == published


def case_disclosure_has_the_share_gate_and_refusals(world: World) -> None:
    make_memory(world, world.alice, "a1")
    cid, (first,) = seed_conversation(world, world.alice, [
        answer(citations=[memory_citation(world, "a1")]),
    ])
    # Not the creator (the notebook owner included): the share's own 404.
    for user in (world.owner, world.admin):
        assert disclose(world, user, cid).status_code == 404
        assert publish(world, user, cid, first, acknowledged=1).status_code == 404
    other = world.client.post("/api/notebooks", json={"name": "别的库"},
                              headers=world.alice.headers).json()["id"]
    assert world.client.get(
        f"/api/notebooks/{other}/conversations/{cid}/share/disclosure",
        headers=world.alice.headers,
    ).status_code == 404
    creatorless, _ = seed_conversation(world, "", [answer()])
    refused = disclose(world, world.alice, creatorless)
    assert refused.status_code == 409 and refused.json() == {"detail": CREATORLESS}
    stale = disclose(world, world.alice, cid, "ans-gone")
    assert stale.status_code == 409 and stale.json() == {"detail": STALE}
    assert stale.headers.get("X-User-Message") == "1"
    empty, _ = seed_conversation(world, world.alice, [])
    nothing = disclose(world, world.alice, empty)
    assert nothing.status_code == 409 and nothing.json() == {"detail": NO_ANSWER}


def case_public_conversation_page_marks_the_authors_memory(world: World) -> None:
    for key in ("a1", "a2", "a3"):
        make_memory(world, world.alice, key)
    cid, (first, second) = seed_conversation(world, world.alice, [
        answer([memory_anchor(world, "a1", "k1"),
                source_anchor(world.memories["a2"][1], "k2", title="记忆投影"),
                source_anchor(world.doc_source, "k3")]),
        # No anchor bound: the page falls back to the citation list.
        {"conclusion": "见 [1]。", "answer": "见 [1]。", "anchors": [],
         "citations": [memory_citation(world, "a3")]},
    ])
    token = publish(world, world.alice, cid, second, acknowledged=3).json()["share_token"]
    page = public_page(world, token)
    assert page.status_code == 200
    first_turn, second_turn = page.json()["turns"]
    marked = {ref["key"]: ref for ref in first_turn["references"]}
    assert marked["k1"]["is_memory"] is True and marked["k1"]["title"] == "记忆 a1"
    assert marked["k2"]["is_memory"] is True and marked["k2"]["title"] == "记忆投影"
    assert "is_memory" not in marked["k3"]
    (cited,) = second_turn["references"]
    assert cited["is_memory"] is True
    assert cited["title"] == "记忆 a3"           # without the engine's "Memory · "
    assert "Memory · " not in page.text
    assert world.memories["a1"][0] not in page.text


NOTEBOOK_CASES: dict[str, Callable[[World], None]] = {
    name: case for name, case in globals().items()
    if name.startswith("case_") and callable(case)
}


# --- global conversations ----------------------------------------------------


def seed_global(world: World, author: User, payloads: list[dict], *,
                resolved: list[str] | None = None) -> tuple[str, list[str]]:
    store = world.repo._runtime.global_ask_store
    cid = f"gconv-d-{next(_IDS)}"
    insert_conversation(store, cid, author.id)
    job_ids = []
    for index, payload in enumerate(payloads):
        job_ids.append(insert_job(
            store, cid, author.id, f"{cid}-j{index}", f"2026-01-01T00:00:{index + 1:02d}",
            payload=_job(cid, f"{cid}-j{index}", payload,
                         resolved or [world.notebook]),
        ))
    return cid, job_ids


def _job(cid: str, job_id: str, payload: dict, resolved: list[str]) -> dict:
    return {
        "job_id": job_id, "conversation_id": cid, "status": "done",
        "question": "全局问题", "created_at": "2026-01-01T00:00:01",
        "notebook_scope": {"mode": "all", "notebook_ids": []},
        "resolved_notebook_ids": resolved, "cited_notebook_ids": resolved[:1],
        "searched_notebook_ids": resolved, "answer": payload,
    }


def _global_path(cid: str) -> str:
    return f"/api/global-ask/conversations/{cid}/share"


def global_disclose(world: World, user: User, cid: str, through_id: str = ""):
    return world.client.get(f"{_global_path(cid)}/disclosure",
                            params={"through_id": through_id}, headers=user.headers)


def global_publish(world: World, user: User, cid: str, through_id: str = "",
                   acknowledged: int | None = None):
    body: dict[str, Any] = {"expected_through_id": through_id}
    if acknowledged is not None:
        body["acknowledged_memory_count"] = acknowledged
    return world.client.post(_global_path(cid), json=body, headers=user.headers)


def global_case_count_refusal_and_acknowledgement(world: World) -> None:
    make_memory(world, world.alice, "a1")
    make_memory(world, world.alice, "a2")
    cid, (first,) = seed_global(world, world.alice, [
        answer([source_anchor(world.memories["a2"][1], "k1", notebook_id=world.notebook)],
               [memory_citation(world, "a1")]),
    ])
    assert global_disclose(world, world.alice, cid, first).json() == counts(2, 2)
    for wrong in (None, 1, 3):
        refused = global_publish(world, world.alice, cid, first, wrong)
        assert refused.status_code == 409 and refused.json() == required(2, 2)
    store = world.repo._runtime.global_ask_store
    assert store.conversation_share_state(cid, world.alice.id)["share_token"] == ""
    accepted = global_publish(world, world.alice, cid, first, 2)
    assert accepted.status_code == 200 and accepted.json()["shared_through_id"] == first


def global_case_zero_memory_publishes_as_before(world: World) -> None:
    cid, (first,) = seed_global(world, world.alice, [
        answer([source_anchor(world.doc_source, "k1", notebook_id=world.notebook)]),
    ])
    assert global_disclose(world, world.alice, cid).json() == counts(0, 0)
    published = global_publish(world, world.alice, cid, first)
    assert published.status_code == 200, published.text
    assert set(published.json()) == {"share_token", "shared_through_at", "shared_through_id"}
    page = public_page(world, published.json()["share_token"])
    assert page.status_code == 200 and "is_memory" not in page.text


def global_case_new_count_is_relative_to_the_watermark(world: World) -> None:
    for key in ("a1", "a2", "a3"):
        make_memory(world, world.alice, key)
    cid, (first, second) = seed_global(world, world.alice, [
        answer(citations=[memory_citation(world, "a1"), memory_citation(world, "a2")]),
        answer(citations=[memory_citation(world, "a2"), memory_citation(world, "a3")]),
    ])
    assert global_publish(world, world.alice, cid, first, 2).status_code == 200
    assert global_disclose(world, world.alice, cid, second).json() == counts(3, 1)
    assert global_disclose(world, world.alice, cid, first).json() == counts(2, 0)
    refused = global_publish(world, world.alice, cid, second)
    assert refused.status_code == 409 and refused.json() == required(3, 1)
    store = world.repo._runtime.global_ask_store
    assert store.conversation_share_state(cid, world.alice.id)["shared_through_id"] == first
    assert global_publish(world, world.alice, cid, second, 3).status_code == 200


def global_case_disclosure_is_owner_only_and_reads_only(world: World) -> None:
    make_memory(world, world.alice, "a1")
    cid, (first,) = seed_global(world, world.alice, [
        answer(citations=[memory_citation(world, "a1")]),
    ])
    assert global_disclose(world, world.owner, cid).status_code == 404
    assert global_disclose(world, world.alice, "gconv-missing").status_code == 404
    stale = global_disclose(world, world.alice, cid, "job-gone")
    assert stale.status_code == 409 and stale.json() == {"detail": STALE}
    assert global_disclose(world, world.alice, cid, first).status_code == 200
    store = world.repo._runtime.global_ask_store
    assert store.conversation_share_state(cid, world.alice.id)["share_token"] == ""


def global_case_public_page_marks_the_authors_memory(world: World) -> None:
    make_memory(world, world.alice, "a1")
    make_memory(world, world.alice, "a2")
    cid, (first,) = seed_global(world, world.alice, [
        answer([memory_anchor(world, "a1", "k1"),
                source_anchor(world.memories["a2"][1], "k2", notebook_id=world.notebook,
                              title="记忆投影"),
                source_anchor(world.doc_source, "k3", notebook_id=world.notebook)]),
    ])
    token = global_publish(world, world.alice, cid, first, 2).json()["share_token"]
    page = public_page(world, token)
    assert page.status_code == 200
    marked = {ref["key"]: ref for ref in page.json()["turns"][0]["references"]}
    assert marked["k1"]["is_memory"] is True and marked["k1"]["title"] == "记忆 a1"
    assert marked["k2"]["is_memory"] is True
    assert "is_memory" not in marked["k3"]
    assert "created_by" not in page.text and world.alice.id not in page.text


GLOBAL_CASES: dict[str, Callable[[World], None]] = {
    name: case for name, case in globals().items()
    if name.startswith("global_case_") and callable(case)
}


# --- the report page marker --------------------------------------------------


def report_case_public_page_marks_the_authors_memory(world: World) -> None:
    for key in ("a1", "a2", "a3"):
        make_memory(world, world.alice, key)
    recorded = source_ref(world.memories["a2"][1], "k2")
    recorded.update(memory_id=world.memories["a2"][0], memory_owner_id=world.alice.id,
                    label="Memory · 记忆 a2", source_title="")
    rid = make_report(world, world.alice, [
        memory_ref(world.memories["a1"][0], "k1"),
        recorded,
        source_ref(world.memories["a3"][1], "k3"),      # unrecorded projection hit
        source_ref(world.doc_source, "k4"),
    ])
    token = share_report(world, world.alice, rid, 3).json()["share_token"]
    page = world.client.get(f"/api/public/reports/{token}")
    assert page.status_code == 200
    marked = {ref["key"]: ref for ref in page.json()["references"]}
    assert marked["k1"]["is_memory"] is True
    assert marked["k2"]["is_memory"] is True and marked["k2"]["title"] == "记忆 a2"
    assert marked["k3"]["is_memory"] is True
    assert "is_memory" not in marked["k4"]
    assert "Memory · " not in page.text


def report_case_page_without_memory_carries_no_marker(world: World) -> None:
    rid = make_report(world, world.alice, [source_ref(world.doc_source, "k1")])
    token = share_report(world, world.alice, rid).json()["share_token"]
    page = world.client.get(f"/api/public/reports/{token}")
    assert page.status_code == 200 and "is_memory" not in page.text
    assert set(page.json()["references"][0]) == {
        "key", "title", "file_name", "location", "snippet",
        "title_truncated", "snippet_truncated", "file_name_truncated",
    }


REPORT_CASES: dict[str, Callable[[World], None]] = {
    name: case for name, case in globals().items()
    if name.startswith("report_case_") and callable(case)
}
