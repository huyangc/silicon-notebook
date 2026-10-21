"""``list_conversations`` / ``get_conversation`` through the real MCP entry.

Backend-agnostic: ``tests/test_mcp_conversations.py`` (SQLite) and
``tests/postgres/test_mcp_conversations_pg.py`` (PostgreSQL) both run every
case in ``CASES`` against ``create_app()`` and its real repository, global Ask
service and Agent tokens, through the official MCP client. Single-notebook
conversations are seeded as ``conversations`` / ``answers`` / ``ask_jobs`` rows,
global ones as ``global_ask_conversations`` / ``global_ask_jobs`` rows; the
cited source elements are real rows, so a ``ref`` a page hands out is opened
through ``read_reference`` for real.

What these pin: a conversation is visible to the member who had it, whichever
surface asked (browser or MCP), and to no one else; a single-notebook
conversation needs its notebook allowlisted and readable now, a global one
needs every library any of its turns used to be; every refusal -- someone
else's, missing, outside the allowlist, no longer readable -- reads exactly
like a conversation that does not exist; the ``read`` tier is checked before
any lookup; ``get_ask`` still does not read a browser job; and the call ledger
books ``history:read`` only for a call that names one notebook and cleared
every gate.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from itertools import count
from types import SimpleNamespace

from app.api.mcp_tools.ask import notebook_view
from app.api.mcp_tools.conversations import conversation_row, turn_page
from app.domain.report_followup import (
    REASON_CLAUSES,
    REASON_PREFIX,
    REASON_REPORT_UNAVAILABLE,
)
from tests.answer_link_service_cases import _notebook, _set_access, _user, _write
from tests.citation_check_testkit import seed_source
from tests.global_ask_history_store_cases import global_job_payload
from tests.global_ask_share_cases import insert_conversation, insert_job
from tests.mcp_answer_link_cases import _agent, _clients, _error, _payload

_IDS = count(1)
NOT_FOUND = "没有找到这个会话"
ROW_KEYS = {
    "conversation_id", "kind", "title", "notebook_ids", "notebook_count",
    "turn_count", "updated_at", "submitted_via",
}
REPORT_TITLE = "光伏组件封装工艺综述"


def _stamp(second: int, minute: int = 0) -> str:
    return f"2026-10-21T08:{minute:02d}:{second:02d}+00:00"


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _answer(text: str = "答案正文。", citations=(), linked_report: dict | None = None) -> dict:
    """A stored notebook answer payload (a valid ``AskResponse``);
    ``linked_report`` is the receipt of the deep report the turn was asked
    about (``_receipt``), left out for a turn that attached none."""
    payload = {"conclusion": text, "answer": text, "evidence_level": "grounded",
               "mode": "reasoning", "anchors": [], "citations": list(citations)}
    if linked_report is not None:
        payload["linked_report"] = linked_report
    return payload


def _receipt(report_id: str, title: str = REPORT_TITLE, *, status: str = "read",
             sections=(), restored: int = 0, gone: int = 0, over_budget: int = 0,
             summary_truncated: bool = False, reason_code: str = "",
             reason: str = "", memory_used=()) -> dict:
    """One stored ``linked_report`` receipt (a valid ``LinkedReportReceipt``
    dump, optional fields left out as the model leaves them out); ``sections``
    are ``(title, read)`` pairs."""
    receipt: dict = {
        "report_id": report_id, "title": title, "status": status,
        "evidence_restored": restored, "evidence_gone": gone,
        "evidence_over_budget": over_budget,
    }
    if sections:
        receipt["sections"] = [
            {"index": index, "title": name, "read": read}
            for index, (name, read) in enumerate(sections, 1)
        ]
    if summary_truncated:
        receipt["summary_truncated"] = True
    if reason_code:
        receipt["reason_code"] = reason_code
    if reason:
        receipt["reason"] = reason
    if memory_used:
        receipt["memory_used"] = list(memory_used)
    return receipt


def _turn(question: str = "带宽够吗", answer: dict | None = None, via: str | None = "web"):
    """One saved answer; ``via`` is its job row's ``submitted_via`` (``None``:
    no job row, as for an older answer)."""
    return SimpleNamespace(question=question, payload=answer or _answer(), via=via)


def _conversation(env, notebook_id: str, owner_id: str, turns=(), *,
                  title: str = "会话", updated_at: str | None = None,
                  running: bool = False) -> str:
    """One single-notebook conversation with ``turns`` saved answers, each
    (optionally) with the job row that produced it, plus an optional job
    still running. Jobs are stamped before their answers, as a real run is."""
    n = next(_IDS)
    cid = f"conv-t3-{n}"
    with _write(env) as db:
        db.execute(
            "INSERT INTO conversations (id, notebook_id, title, created_by, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (cid, notebook_id, title, owner_id, "2026-10-21T07:00:00+00:00",
             updated_at or _stamp(n % 60)),
        )
        for index, turn in enumerate(turns):
            answer_id = f"ans-t3-{n}-{index}"
            asked, answered = (f"2026-10-21T07:{index + 1:02d}:00+00:00",
                               f"2026-10-21T07:{index + 1:02d}:30+00:00")
            db.execute(
                "INSERT INTO answers (id, notebook_id, conversation_id, question, "
                "payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (answer_id, notebook_id, cid, turn.question,
                 json.dumps(turn.payload, ensure_ascii=False), answered),
            )
            if turn.via is not None:
                _job(db, f"askjob-t3-{n}-{index}", notebook_id, cid, owner_id,
                     question=turn.question, status="done", answer_id=answer_id,
                     created_at=asked, via=turn.via)
        if running:
            _job(db, f"askjob-t3-{n}-running", notebook_id, cid, owner_id,
                 question="还在回答的问题", status="running", answer_id="",
                 created_at="2026-10-21T07:59:00+00:00", via="web")
    return cid


def _job(db, job_id, notebook_id, cid, owner_id, *, question, status, answer_id,
         created_at, via) -> None:
    db.execute(
        "INSERT INTO ask_jobs (id, notebook_id, conversation_id, created_by, mode, "
        "question, status, answer_id, created_at, updated_at, submitted_via, "
        "memory_access, output) VALUES "
        "(?, ?, ?, ?, 'reasoning', ?, ?, ?, ?, ?, ?, 0, 'answer')",
        (job_id, notebook_id, cid, owner_id, question, status, answer_id,
         created_at, created_at, via),
    )


def _global(env, owner_id: str, turns, *, title: str | None = None,
            updated_at: str | None = None, via: str = "web",
            scope_ids=()) -> tuple[str, list[str]]:
    """One global conversation; ``turns`` are ``global_job_payload`` keyword
    sets (``resolved``, ``citations``, ``status`` ...), oldest first."""
    store = env.repo._runtime.global_ask_store
    n = next(_IDS)
    cid = f"gconv-t3-{n}"
    insert_conversation(store, cid, owner_id)
    job_ids = []
    for index, turn in enumerate(turns):
        job_id = f"gask-t3-{n}-{index}"
        stamp = f"2026-10-21T07:{index:02d}:00+00:00"
        status = turn.get("status", "done")
        insert_job(store, cid, owner_id, job_id, stamp, status=status,
                   payload=global_job_payload(job_id, cid, created_at=stamp, **turn))
        job_ids.append(job_id)
    with _write(env) as db:
        db.execute(
            "UPDATE global_ask_conversations SET title = ?, updated_at = ?, "
            "scope_json = ?, submitted_via = ? WHERE id = ?",
            (title or f"全局会话 {n}", updated_at or _stamp(n % 60),
             json.dumps({"mode": "include" if scope_ids else "all",
                         "notebook_ids": list(scope_ids)}), via, cid),
        )
        db.execute(
            "UPDATE global_ask_jobs SET submitted_via = ? WHERE conversation_id = ?",
            (via, cid),
        )
    return cid, job_ids


def _grant_group(env, owner_headers: dict, notebook_id: str, member_id: str) -> str:
    """Share ``notebook_id`` with ``member_id`` through a group (viewer)."""
    group_id = env.client.post(
        "/api/groups", json={"name": "组"}, headers=owner_headers
    ).json()["id"]
    assert env.client.put(
        f"/api/groups/{group_id}/members/{member_id}", json={"role": "member"},
        headers=owner_headers,
    ).status_code == 200
    grant = env.client.post(
        f"/api/notebooks/{notebook_id}/grants",
        json={"principal_type": "group", "principal_id": group_id, "role": "viewer"},
        headers=owner_headers,
    )
    assert grant.status_code == 200, grant.text
    return group_id


def _world(env, *, scopes=("read",), notebooks: int = 1):
    """One owner, ``notebooks`` notebooks, one token over all of them."""
    headers, owner_id = _user(env)
    ids = [_notebook(env, headers, f"库{index}") for index in range(notebooks)]
    raw, principal = _agent(env, owner_id, ids, scopes=scopes, allow_links=False)
    return SimpleNamespace(
        headers=headers, owner_id=owner_id, nb=ids[0], notebooks=ids, raw=raw,
        principal=principal,
    )


def _history_rows(env, notebook_id: str) -> int:
    with _write(env) as db:
        return int(db.execute(
            "SELECT COUNT(*) AS n FROM agent_observations "
            "WHERE kind = 'call' AND text = 'history:read' AND notebook_id = ?",
            (notebook_id,),
        ).fetchone()["n"])


async def _all_rows(client, arguments=None, *, limit: int = 20) -> list[dict]:
    rows, offset = [], 0
    while offset is not None:
        page = _payload(await client.call("list_conversations", {
            **(arguments or {}), "limit": limit, "offset": offset,
        }))
        rows.extend(page["items"])
        offset = page["next_offset"]
    return rows


async def _ids(client, arguments=None) -> list[str]:
    return [row["conversation_id"] for row in await _all_rows(client, arguments)]


def _element_citation(source_id: str, element_id: str, notebook_id: str = "") -> dict:
    row = {"label": "论文", "source_id": source_id, "element_id": element_id,
           "location_label": "p0", "quoted_span": "被引用的原文段落"}
    if notebook_id:
        row["notebook_id"] = notebook_id
    return row


# ------------------------------------------------------------------ reading


async def browser_conversation_is_listed_and_readable_through_refs(env):
    world = _world(env, scopes=("read", "ask"))
    with _write(env) as db:
        seed_source(db, "?", notebook_id=world.nb, source_id="src-t3",
                    elements={"el-t3-0": "被引用的原文段落。"})
    cid = _conversation(env, world.nb, world.owner_id, [
        _turn("带宽够吗", _answer("带宽足够 [1]。",
                               [_element_citation("src-t3", "el-t3-0")])),
    ], title="带宽讨论", updated_at=_stamp(5))
    async with _clients(env, world.raw) as (client,):
        listing = _payload(await client.call("list_conversations", {}))
        assert listing["total"] == 1 and listing["next_offset"] is None
        [row] = listing["items"]
        assert set(row) == ROW_KEYS
        assert row["conversation_id"] == cid and row["kind"] == "notebook"
        assert row["title"] == "带宽讨论" and row["notebook_ids"] == [world.nb]
        assert row["notebook_count"] == 1 and row["turn_count"] == 1
        assert row["submitted_via"] == "web"
        assert _instant(row["updated_at"]) == _instant(_stamp(5))

        page = _payload(await client.call("get_conversation", {"conversation_id": cid}))
        assert page["conversation"] == row
        assert page["question"] == "带宽够吗" and page["answer"] == "带宽足够 [1]。"
        assert page["status"] == "answered" and page["error"] == ""
        assert page["submitted_via"] == "web" and page["next_turn_offset"] is None
        assert _instant(page["asked_at"]) == _instant("2026-10-21T07:01:30+00:00")
        assert page["scope"] == {"kind": "notebook", "notebook_ids": [world.nb]}
        [citation] = page["citations"]
        opened = _payload(await client.call("read_reference", {"ref": citation["ref"]}))
        assert opened["kind"] == "el" and opened["text"] == "被引用的原文段落。"

        # get_ask is unchanged: it still does not read a browser-started job.
        job_id = cid.replace("conv-", "askjob-", 1) + "-0"
        assert _error(await client.call("get_ask", {"job_id": job_id}))[0] == "not_found"

        # Whitespace around a good id is ignored; a malformed id is not_found
        # before any lookup.
        padded = _payload(await client.call("get_conversation", {
            "conversation_id": f"  {cid}  ",
        }))
        assert padded["conversation"]["conversation_id"] == cid
        for malformed in ("", "   ", "askjob-1", "rep-1", "conv-" + "x" * 300,
                          "gconv-" + "x" * 300):
            code, text = _error(await client.call("get_conversation", {
                "conversation_id": malformed,
            }))
            assert code == "not_found" and NOT_FOUND in text, malformed


async def submitted_via_tells_web_from_mcp(env):
    world = _world(env)
    mixed = _conversation(env, world.nb, world.owner_id, [
        _turn("第一问", via="web"), _turn("第二问", via="mcp"),
    ], updated_at=_stamp(3))
    # A later job naming the same answer (a retry re-attached in the browser):
    # the turn keeps the entry point of the OLDEST job that names it.
    n = mixed.split("-")[-1]
    with _write(env) as db:
        _job(db, f"askjob-t3-{n}-again", world.nb, mixed, world.owner_id,
             question="第二问", status="done", answer_id=f"ans-t3-{n}-1",
             created_at="2026-10-21T07:05:00+00:00", via="web")
    agent = _conversation(env, world.nb, world.owner_id, [_turn(via="mcp")],
                          updated_at=_stamp(2))
    unrecorded = _conversation(env, world.nb, world.owner_id, [_turn(via=None)],
                               updated_at=_stamp(1))
    global_web, _ = _global(env, world.owner_id, [{"resolved": [world.nb]}],
                            via="web", updated_at=_stamp(4, minute=1))
    global_mcp, _ = _global(env, world.owner_id, [{"resolved": [world.nb]}],
                            via="mcp", updated_at=_stamp(5, minute=1))
    async with _clients(env, world.raw) as (client,):
        rows = {row["conversation_id"]: row for row in await _all_rows(client)}
        assert {key: row["submitted_via"] for key, row in rows.items()} == {
            mixed: "web", agent: "mcp", unrecorded: "",
            global_web: "web", global_mcp: "mcp",
        }
        assert rows[global_web]["kind"] == rows[global_mcp]["kind"] == "global"
        vias = []
        for turn_offset in (0, 1):
            page = _payload(await client.call("get_conversation", {
                "conversation_id": mixed, "turn_offset": turn_offset,
            }))
            vias.append((page["question"], page["submitted_via"]))
        assert vias == [("第一问", "web"), ("第二问", "mcp")]
        page = _payload(await client.call("get_conversation", {"conversation_id": unrecorded}))
        assert page["submitted_via"] == "" and page["conversation"]["submitted_via"] == ""
        page = _payload(await client.call("get_conversation", {"conversation_id": global_mcp}))
        assert page["submitted_via"] == "mcp" and page["conversation"]["submitted_via"] == "mcp"


async def another_users_conversation_is_invisible_and_indistinguishable(env):
    world = _world(env)
    member_headers, member_id = _user(env)
    _grant_group(env, world.headers, world.nb, member_id)
    mine = _conversation(env, world.nb, world.owner_id, [_turn("我的问题")])
    theirs = _conversation(env, world.nb, member_id, [_turn("成员的问题")],
                           title="成员的会话")
    their_global, _ = _global(env, member_id, [{"resolved": [world.nb]}],
                              title="成员的全局会话")
    async with _clients(env, world.raw) as (client,):
        assert await _ids(client) == [mine]
        assert await _ids(client, {"notebook_id": world.nb}) == [mine]
        missing = _error(await client.call("get_conversation", {
            "conversation_id": "conv-nonexistent",
        }))
        missing_global = _error(await client.call("get_conversation", {
            "conversation_id": "gconv-nonexistent",
        }))
        assert missing == missing_global
        assert missing[0] == "not_found" and NOT_FOUND in missing[1]
        # An offset past the end does not tell someone else's conversation
        # apart either: ownership is judged before the offset.
        for other in (theirs, their_global):
            for turn_offset in (0, 99):
                assert _error(await client.call("get_conversation", {
                    "conversation_id": other, "turn_offset": turn_offset,
                })) == missing, (other, turn_offset)
    # The member, through a token of their own, reads their conversation.
    member_raw, _ = _agent(env, member_id, [world.nb], scopes=("read",), allow_links=False)
    async with _clients(env, member_raw) as (client,):
        assert set(await _ids(client)) == {theirs, their_global}
        page = _payload(await client.call("get_conversation", {"conversation_id": theirs}))
        assert page["question"] == "成员的问题"
        assert _error(await client.call("get_conversation", {
            "conversation_id": mine,
        }))[0] == "not_found"


async def notebook_outside_the_allowlist(env):
    headers, owner_id = _user(env)
    allowed = _notebook(env, headers, "在白名单里")
    outside = _notebook(env, headers, "不在白名单里")
    raw, _ = _agent(env, owner_id, [allowed], scopes=("read",), allow_links=False)
    inside = _conversation(env, allowed, owner_id, [_turn()])
    beyond = _conversation(env, outside, owner_id, [_turn()])
    async with _clients(env, raw) as (client,):
        assert await _ids(client) == [inside]
        missing = _error(await client.call("get_conversation", {
            "conversation_id": "conv-nonexistent",
        }))
        for turn_offset in (0, 99):
            assert _error(await client.call("get_conversation", {
                "conversation_id": beyond, "turn_offset": turn_offset,
            })) == missing, turn_offset
        code, _text = _error(await client.call("list_conversations", {
            "notebook_id": outside,
        }))
        assert code == "notebook_not_allowed"


async def owner_lost_read(env):
    owner_headers, _ = _user(env)
    nb = _notebook(env, owner_headers)
    _reader_headers, reader_id = _user(env)
    group_id = _grant_group(env, owner_headers, nb, reader_id)
    raw, _ = _agent(env, reader_id, [nb], scopes=("read",), allow_links=False)
    cid = _conversation(env, nb, reader_id, [_turn("读者的问题")])
    gcid, _ = _global(env, reader_id, [{"resolved": [nb]}])
    async with _clients(env, raw) as (client,):
        assert set(await _ids(client)) == {cid, gcid}

        assert env.client.delete(
            f"/api/groups/{group_id}/members/{reader_id}", headers=owner_headers,
        ).status_code in (200, 204)
        gone = _payload(await client.call("list_conversations", {}))
        assert gone["items"] == [] and gone["total"] == 0
        missing = _error(await client.call("get_conversation", {
            "conversation_id": "conv-nonexistent",
        }))
        for conversation_id in (cid, gcid):
            for turn_offset in (0, 99):
                assert _error(await client.call("get_conversation", {
                    "conversation_id": conversation_id, "turn_offset": turn_offset,
                })) == missing, (conversation_id, turn_offset)
        code, _text = _error(await client.call("list_conversations", {"notebook_id": nb}))
        assert code == "notebook_unreadable"

        assert env.client.put(
            f"/api/groups/{group_id}/members/{reader_id}", json={"role": "member"},
            headers=owner_headers,
        ).status_code == 200
        assert set(await _ids(client)) == {cid, gcid}
        page = _payload(await client.call("get_conversation", {"conversation_id": cid}))
        assert page["question"] == "读者的问题"


async def global_conversation_inside_the_allowlist(env):
    world = _world(env, scopes=("read", "ask"), notebooks=2)
    first, second = world.notebooks
    with _write(env) as db:
        seed_source(db, "?", notebook_id=second, source_id="src-g3",
                    elements={"el-g3-0": "全局引用的原文。"})
    gcid, job_ids = _global(env, world.owner_id, [
        {"resolved": [first], "question": "第一轮", "answer": "第一轮回答"},
        {"resolved": [first, second], "question": "第二轮", "answer": "第二轮回答 [1]",
         "citations": [_element_citation("src-g3", "el-g3-0", second)]},
        {"resolved": [first], "status": "running"},
    ], title="跨库会话", via="web", scope_ids=["nb-deleted-long-ago", first])
    async with _clients(env, world.raw) as (client,):
        [row] = await _all_rows(client)
        assert row["conversation_id"] == gcid and row["kind"] == "global"
        # The libraries the turns used -- not the latest scope, which names a
        # library that is gone.
        assert row["notebook_ids"] == sorted([first, second])
        assert row["notebook_count"] == 2 and row["turn_count"] == 2
        assert row["title"] == "跨库会话" and row["submitted_via"] == "web"

        page = _payload(await client.call("get_conversation", {"conversation_id": gcid}))
        assert page["question"] == "第一轮" and page["answer"] == "第一轮回答"
        assert page["next_turn_offset"] == 1 and page["conversation"] == row
        page = _payload(await client.call("get_conversation", {
            "conversation_id": gcid, "turn_offset": 1,
        }))
        assert page["question"] == "第二轮" and page["next_turn_offset"] is None
        assert page["scope"]["kind"] == "global"
        [citation] = page["citations"]
        opened = _payload(await client.call("read_reference", {"ref": citation["ref"]}))
        assert opened["kind"] == "gel" and opened["text"] == "全局引用的原文。"
        # The running question is not a turn yet.
        code, text = _error(await client.call("get_conversation", {
            "conversation_id": gcid, "turn_offset": 2,
        }))
        assert code == "invalid_argument" and "共 2 轮" in text
        # get_ask still refuses a browser-started global job.
        assert _error(await client.call("get_ask", {"job_id": job_ids[1]}))[0] == (
            "not_found"
        )


async def global_conversation_crossing_the_allowlist_never_shows_its_title(env):
    world = _world(env, notebooks=2)
    first, second = world.notebooks
    raw, _ = _agent(env, world.owner_id, [first], scopes=("read",), allow_links=False)
    secret = "越界会话标题-7f3a"
    crossing, _ = _global(env, world.owner_id, [
        {"resolved": [first]}, {"resolved": [first, second]},
    ], title=secret)
    evidence, _ = _global(env, world.owner_id, [
        {"resolved": [first], "anchors": [second]},
    ], title=secret + "-证据")
    inside, _ = _global(env, world.owner_id, [{"resolved": [first]}], title="白名单内")
    async with _clients(env, raw) as (client,):
        offset, ids = 0, []
        while offset is not None:
            result = await client.call("list_conversations", {"limit": 1, "offset": offset})
            dumped = json.dumps(_payload(result), ensure_ascii=False)
            assert secret not in dumped
            page = _payload(result)
            ids.extend(row["conversation_id"] for row in page["items"])
            offset = page["next_offset"]
        assert ids == [inside]
        assert await _ids(client, {"notebook_id": first}) == [inside]
        missing = _error(await client.call("get_conversation", {
            "conversation_id": "gconv-nonexistent",
        }))
        # Turn 0 is inside the allowlist on its own; the conversation is not.
        # Past its end it reads the same: the turn count is never disclosed.
        for conversation_id in (crossing, evidence):
            for turn_offset in (0, 1, 99):
                refused = _error(await client.call("get_conversation", {
                    "conversation_id": conversation_id, "turn_offset": turn_offset,
                }))
                assert refused == missing, (conversation_id, turn_offset)
                assert secret not in refused[1]
    # A token allowed both libraries sees all three.
    async with _clients(env, world.raw) as (client,):
        assert set(await _ids(client)) == {crossing, evidence, inside}


async def global_turn_vanishing_after_the_judgement_is_not_found(env):
    world = _world(env)
    gcid, _ = _global(env, world.owner_id, [{"resolved": [world.nb]}] * 2)
    store = env.repo._runtime.global_ask_store
    async with _clients(env, world.raw) as (client,):
        assert _payload(await client.call("get_conversation", {
            "conversation_id": gcid, "turn_offset": 1,
        }))["conversation"]["turn_count"] == 2
        missing = _error(await client.call("get_conversation", {
            "conversation_id": "gconv-nonexistent",
        }))
        # Judged with two finished turns, then the turn is gone when read.
        env.monkeypatch.setattr(store, "finished_job_at", lambda *_args, **_kw: None)
        assert _error(await client.call("get_conversation", {
            "conversation_id": gcid, "turn_offset": 1,
        })) == missing


async def downgrade_to_no_read(env):
    world = _world(env, scopes=("read", "ask"))
    cid = _conversation(env, world.nb, world.owner_id, [_turn()])
    gcid, _ = _global(env, world.owner_id, [{"resolved": [world.nb]}])
    tools = {"list_conversations", "get_conversation"}
    async with _clients(env, world.raw) as (client,):
        listed = {tool.name for tool in (await client.session.list_tools()).tools}
        assert tools <= listed
        assert _payload(await client.call("list_conversations", {}))["total"] == 2

        _set_access(env, world.principal, scopes=["ask"])
        listed = {tool.name for tool in (await client.session.list_tools()).tools}
        assert not tools & listed
        for call, arguments in (
            ("list_conversations", {}),
            ("list_conversations", {"notebook_id": world.nb}),
            ("get_conversation", {"conversation_id": cid}),
            ("get_conversation", {"conversation_id": gcid}),
            # The tier is checked before the id is looked up, so a token
            # without it cannot use ids to find out what exists.
            ("get_conversation", {"conversation_id": "conv-nothing"}),
            ("get_conversation", {"conversation_id": "gconv-nothing"}),
        ):
            code, text = _error(await client.call(call, arguments))
            assert code == "scope_missing" and "读取" in text, (call, code, text)
        # An argument error is reported before any authorization or lookup.
        for arguments in ({"turn_offset": -1}, {"answer_offset": -1},
                          {"citation_offset": -1}):
            code, _text = _error(await client.call("get_conversation", {
                "conversation_id": cid, **arguments,
            }))
            assert code == "invalid_argument", arguments
        code, _text = _error(await client.call("list_conversations", {"offset": -1}))
        assert code == "invalid_argument"

        _set_access(env, world.principal, scopes=["read", "ask"])
        listed = {tool.name for tool in (await client.session.list_tools()).tools}
        assert tools <= listed
        assert _payload(await client.call("get_conversation", {
            "conversation_id": cid,
        }))["status"] == "answered"


# ------------------------------------------------------------------- paging


async def list_pages_are_exact(env):
    world = _world(env, notebooks=2)
    first, second = world.notebooks
    ids = {}
    for name, second_of_minute in (("c1", 1), ("c2", 3), ("c3", 5), ("c4", 7), ("c5", 9)):
        ids[name] = _conversation(env, first, world.owner_id, [_turn()],
                                  updated_at=_stamp(second_of_minute))
    ids["s1"] = _conversation(env, second, world.owner_id, [_turn()],
                              updated_at=_stamp(6))
    ids["g1"] = _global(env, world.owner_id, [{"resolved": [first, second]}],
                        updated_at=_stamp(8))[0]
    ids["g2"] = _global(env, world.owner_id, [{"resolved": [first]}],
                        updated_at=_stamp(2))[0]
    expected_all = [
        ids[name] for name in ("c5", "g1", "c4", "s1", "c3", "c2", "g2", "c1")
    ]
    async with _clients(env, world.raw) as (client,):
        seen, offset, next_offsets = [], 0, []
        while offset is not None:
            page = _payload(await client.call("list_conversations", {
                "limit": 3, "offset": offset,
            }))
            assert page["total"] == 8
            seen.extend(item["conversation_id"] for item in page["items"])
            next_offsets.append(page["next_offset"])
            offset = page["next_offset"]
        assert seen == expected_all and next_offsets == [3, 6, None]

        # A notebook filter keeps that notebook's conversations and the global
        # ones that used it -- and no global one that did not.
        assert await _ids(client, {"notebook_id": second}) == [ids["g1"], ids["s1"]]
        assert await _ids(client, {"notebook_id": first}) == [
            ids[name] for name in ("c5", "g1", "c4", "c3", "c2", "g2", "c1")
        ]
        past = _payload(await client.call("list_conversations", {"offset": 50}))
        assert past["items"] == [] and past["next_offset"] is None and past["total"] == 8
        blank = await client.call("list_conversations", {"notebook_id": "   "})
        assert _error(blank)[0] == "invalid_argument"


async def turn_paging_is_exact(env):
    world = _world(env)
    body = "".join(chr(0x4E00 + (index * 7919) % 4000) for index in range(5_000))
    cid = _conversation(env, world.nb, world.owner_id, [
        _turn("第一问", _answer("第一答。")),
        _turn("第二问", _answer(body)),
        _turn("第三问", _answer("第三答。")),
    ])
    async with _clients(env, world.raw) as (client,):
        questions, turn_offset = [], 0
        while turn_offset is not None:
            text, answer_offset = "", 0
            while answer_offset is not None:
                page = _payload(await client.call("get_conversation", {
                    "conversation_id": cid, "turn_offset": turn_offset,
                    "answer_offset": answer_offset,
                }))
                assert page["turn_offset"] == turn_offset
                assert page["conversation"]["turn_count"] == 3
                text += page["answer"]
                answer_offset = page["next_answer_offset"]
            questions.append((page["question"], text))
            turn_offset = page["next_turn_offset"]
        assert questions == [("第一问", "第一答。"), ("第二问", body), ("第三问", "第三答。")]

        code, text = _error(await client.call("get_conversation", {
            "conversation_id": cid, "turn_offset": 3,
        }))
        assert code == "invalid_argument" and "共 3 轮" in text


async def empty_conversation_returns_the_empty_page(env):
    world = _world(env)
    cid = _conversation(env, world.nb, world.owner_id, [], running=True)
    gcid, _ = _global(env, world.owner_id, [{"resolved": [world.nb], "status": "running"}])
    async with _clients(env, world.raw) as (client,):
        rows = {row["conversation_id"]: row for row in await _all_rows(client)}
        assert rows[cid]["turn_count"] == 0 and rows[gcid]["turn_count"] == 0
        for conversation_id in (cid, gcid):
            page = _payload(await client.call("get_conversation", {
                "conversation_id": conversation_id,
            }))
            assert page["status"] == "empty" and page["next_turn_offset"] is None
            assert page["conversation"] == rows[conversation_id]
            assert "answer" not in page and "question" not in page
            code, text = _error(await client.call("get_conversation", {
                "conversation_id": conversation_id, "turn_offset": 1,
            }))
            assert code == "invalid_argument" and "共 0 轮" in text


async def own_memory_citation_opens_through_mem_ref(env):
    world = _world(env)
    memory = env.repo.create_memory_candidate(
        world.nb, world.owner_id, None, "t3-memory", "我的记忆", "记忆正文。", [],
        "测试",
    )
    citation = {"label": "我的记忆", "source_id": "", "element_id": "",
                "location_label": "", "quoted_span": "记忆摘录",
                "memory_id": memory.id}
    cid = _conversation(env, world.nb, world.owner_id, [
        _turn("我记过什么", _answer("你记过 [1]。", [citation])),
    ])
    async with _clients(env, world.raw) as (client,):
        page = _payload(await client.call("get_conversation", {"conversation_id": cid}))
        [delivered] = page["citations"]
        assert delivered["memory_id"] == memory.id
        opened = _payload(await client.call("read_reference", {"ref": delivered["ref"]}))
        assert opened["kind"] == "mem" and opened["text"] == "记忆正文。"


# ------------------------------------------------------------- linked report


def _wire(*payloads) -> str:
    """Everything a caller was sent, as one string to look for leaks in."""
    return json.dumps(payloads, ensure_ascii=False)


async def a_conversation_that_followed_a_report_names_it_and_each_turn_carries_its_receipt(env):
    world = _world(env, scopes=("read", "ask"))
    read = _receipt(
        "rep-t14-a", sections=[("背景", True), ("工艺路线", True), ("附录", False)],
        restored=2, gone=1, over_budget=3, summary_truncated=True,
        memory_used=("mem-t14-secret",),
    )
    skipped = _receipt(
        "rep-t14-b", "另一份报告", status="skipped",
        reason_code=REASON_REPORT_UNAVAILABLE,
        reason=REASON_PREFIX + REASON_CLAUSES[REASON_REPORT_UNAVAILABLE],
    )
    followed = _conversation(env, world.nb, world.owner_id, [
        _turn("先问一个不看报告的", _answer("第一答。")),
        _turn("按报告说", _answer("第二答。", linked_report=read)),
        _turn("再换一份", _answer("第三答。", linked_report=skipped)),
    ], title="追问报告", running=True)
    # A question still being answered is not a turn (B plan D4): the job row
    # names a third report, and neither tool says anything about it.
    n = followed.split("-")[-1]
    with _write(env) as db:
        db.execute("UPDATE ask_jobs SET report_id = ? WHERE id = ?",
                   ("rep-t14-running", f"askjob-t3-{n}-running"))
    never = _conversation(env, world.nb, world.owner_id, [_turn()], title="没看过报告")
    dropped = _conversation(env, world.nb, world.owner_id, [
        _turn("按报告说", _answer("答。", linked_report=read)),
        _turn("不看了", _answer("答二。")),
    ], title="后来不看了")
    gcid, _ = _global(env, world.owner_id, [{"resolved": [world.nb]}])
    async with _clients(env, world.raw) as (client,):
        listing = _payload(await client.call("list_conversations", {}))
        rows = {row["conversation_id"]: row for row in listing["items"]}
        # The row names the report the LAST saved turn followed -- the id and
        # the title the receipt snapshotted, nothing else.
        assert rows[followed]["linked_report"] == {
            "report_id": "rep-t14-b", "title": "另一份报告",
        }
        assert set(rows[followed]) == ROW_KEYS | {"linked_report"}
        # No report on the last turn, a conversation that never followed one,
        # and a cross-notebook conversation: the row keeps its eight keys.
        for key in (never, dropped, gcid):
            assert set(rows[key]) == ROW_KEYS, key
        filtered = _payload(await client.call("list_conversations", {
            "notebook_id": world.nb,
        }))
        assert {row["conversation_id"]: row for row in filtered["items"]}[
            followed
        ] == rows[followed]

        pages = [
            _payload(await client.call("get_conversation", {
                "conversation_id": followed, "turn_offset": offset,
            }))
            for offset in range(3)
        ]
        for page in pages:
            # ``conversation`` repeats the list row, the followed report too.
            assert page["conversation"] == rows[followed]
        scope = {"kind": "notebook", "notebook_ids": [world.nb]}
        assert pages[0]["scope"] == scope, "this turn attached no report"
        assert pages[1]["scope"] == {**scope, "linked_report": {
            "report_id": "rep-t14-a", "title": REPORT_TITLE, "status": "read",
            "sections_read": ["背景", "工艺路线"], "summary_truncated": True,
            "evidence_restored": 2, "evidence_gone": 1, "evidence_over_budget": 3,
        }}
        assert pages[2]["scope"] == {**scope, "linked_report": {
            "report_id": "rep-t14-b", "title": "另一份报告", "status": "skipped",
            "reason": REASON_PREFIX + REASON_CLAUSES[REASON_REPORT_UNAVAILABLE],
            "sections_read": [], "evidence_restored": 0, "evidence_gone": 0,
            "evidence_over_budget": 0,
        }}
        # The pages are still the 17 keys of a turn page (the report rides in
        # two keys that were already there), and nothing was cut to fit: the
        # shapes sit inside the key, depth and text budgets.
        for page in pages:
            assert len(page) == 18, sorted(page)
            assert page["truncation"]["truncated"] is False, page["truncation"]
        assert listing["truncation"]["truncated"] is False

        # The receipt's Memory ids never leave: not as ``memory_used``, not as
        # an id. Nor does the report a running job names.
        wire = _wire(listing, filtered, pages)
        for secret in ("memory_used", "mem-t14-secret", "rep-t14-running"):
            assert secret not in wire, secret

        # A conversation whose last turn attached none shows the report on the
        # turn that followed it only.
        first = _payload(await client.call("get_conversation", {
            "conversation_id": dropped,
        }))
        assert first["scope"]["linked_report"]["report_id"] == "rep-t14-a"
        assert "linked_report" not in first["conversation"]
        second = _payload(await client.call("get_conversation", {
            "conversation_id": dropped, "turn_offset": 1,
        }))
        assert "linked_report" not in second["scope"]
        # A conversation that never followed one has no trace of the field.
        page = _payload(await client.call("get_conversation", {
            "conversation_id": never,
        }))
        assert page["scope"] == scope and "linked_report" not in page["conversation"]
        assert len(page) == 18

        # Reading is the ``read`` tier's: a token that lost it learns nothing
        # about the report -- not from an error either.
        _set_access(env, world.principal, scopes=["ask"])
        for call, arguments in (
            ("list_conversations", {}),
            ("get_conversation", {"conversation_id": followed}),
            ("get_conversation", {"conversation_id": followed, "turn_offset": 1}),
        ):
            code, text = _error(await client.call(call, arguments))
            assert code == "scope_missing", (call, code)
            for secret in ("rep-t14-a", "rep-t14-b", REPORT_TITLE, "另一份报告"):
                assert secret not in text, (call, secret)


async def a_receipt_that_is_not_valid_reads_as_no_report_on_the_row_and_the_turn(env):
    world = _world(env)
    # A skipped receipt may not claim it read sections: ``AskResponse`` reads
    # such a payload as having no receipt, and the list must agree with it.
    invalid = _receipt("rep-t14-bad", status="skipped", sections=[("背景", True)])
    nameless = {"report_id": 7, "title": REPORT_TITLE, "status": "read"}
    cids = [
        _conversation(env, world.nb, world.owner_id, [
            _turn("问", _answer("答。", linked_report=receipt)),
        ])
        for receipt in (invalid, nameless)
    ]
    async with _clients(env, world.raw) as (client,):
        rows = {row["conversation_id"]: row for row in await _all_rows(client)}
        for cid in cids:
            assert set(rows[cid]) == ROW_KEYS, cid
            page = _payload(await client.call("get_conversation", {"conversation_id": cid}))
            assert page["conversation"] == rows[cid]
            assert page["scope"] == {"kind": "notebook", "notebook_ids": [world.nb]}
            assert page["status"] == "answered" and page["answer"] == "答。"


async def a_long_report_title_is_cut_and_counted_wherever_it_appears(env):
    world = _world(env)
    long = "".join(chr(0x4E00 + (index * 7919) % 4000) for index in range(500))
    cut = long[:199] + "…"
    cid = _conversation(env, world.nb, world.owner_id, [
        _turn("按报告说", _answer("答。", linked_report=_receipt("rep-t14-long", long))),
    ])
    async with _clients(env, world.raw) as (client,):
        listing = _payload(await client.call("list_conversations", {}))
        [row] = listing["items"]
        assert row["linked_report"] == {"report_id": "rep-t14-long", "title": cut}
        assert listing["truncation"]["truncated"] is True
        assert listing["truncation"]["omitted_characters"] == 301

        page = _payload(await client.call("get_conversation", {"conversation_id": cid}))
        assert page["conversation"] == row, "the page repeats the row as the list cut it"
        assert page["scope"]["linked_report"]["title"] == cut
        assert page["scope"]["linked_report"]["report_id"] == "rep-t14-long"
        # One cut in ``conversation``, one in ``scope``: each counted.
        assert page["truncation"]["omitted_characters"] == 602
        assert page["answer"] == "答。"


# ------------------------------------------------------------------ read cost


class _RecordingConnection:
    def __init__(self, db, seen):
        self._db, self._seen = db, seen

    def execute(self, sql, params=()):
        self._seen.append(str(sql))
        return self._db.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._db, name)


class _RecordingDatabase:
    """The store's database, recording every statement its reads send."""

    def __init__(self, database, seen):
        self._database, self._seen = database, seen

    @contextmanager
    def connect(self):
        with self._database.connect() as db:
            yield _RecordingConnection(db, self._seen)

    def __getattr__(self, name):
        return getattr(self._database, name)


def _page_from_the_full_read(env, cid: str, index: int) -> dict:
    """What a page was built from before the one-turn read: the whole
    conversation (``get_conversation``), then the same projection."""
    detail = env.repo.get_conversation(cid)
    turn = detail.turns[index]
    row = conversation_row(
        conversation_id=detail.id, kind="notebook", title=detail.title,
        notebook_ids=[detail.notebook_id], turn_count=len(detail.turns),
        updated_at=detail.updated_at, submitted_via=detail.submitted_via,
    )
    view = notebook_view(
        "", detail.notebook_id, turn.response,
        {"status": "done", "conversation_id": cid, "mode": turn.response.mode}, True,
    )
    page = turn_page(
        row, view, question=turn.question, asked_at=turn.asked_at or turn.created_at,
        submitted_via=turn.submitted_via, turn_offset=index,
        turn_count=len(detail.turns),
    )
    return json.loads(json.dumps(page, ensure_ascii=False))


def _refuse(*_args, **_kwargs):
    raise AssertionError("a one-turn read must not load the whole conversation")


async def notebook_turn_reads_only_the_asked_answer(env):
    world = _world(env)
    with _write(env) as db:
        seed_source(db, "?", notebook_id=world.nb, source_id="src-cost",
                    elements={"el-cost-0": "原文。"})
    trace = [{"step_type": "search_chunks", "summary": "检索", "detail": {"n": 1}}]
    turns = [
        _turn(f"第{index}问", {**_answer(f"第{index}答 [1]。", [
            _element_citation("src-cost", "el-cost-0"),
        ]), "reasoning_trace": trace}, via="web" if index else "mcp")
        for index in range(3)
    ]
    cid = _conversation(env, world.nb, world.owner_id, turns, running=True)
    expected = [_page_from_the_full_read(env, cid, index) for index in range(3)]

    # The other turns' answers are made unreadable: the whole-conversation
    # read now fails, and the one-turn read must not notice.
    poison = json.dumps({"not_an_answer": True})
    with _write(env) as db:
        for index in (0, 2):
            db.execute("UPDATE answers SET payload = ? WHERE id = ?",
                       (poison, f"ans-t3-{cid.split('-')[-1]}-{index}"))
    try:
        env.repo.get_conversation(cid)
    except ValueError:
        pass
    else:
        raise AssertionError("the poisoned answers must break the full read")

    store = env.repo._runtime.ask_state
    seen: list[str] = []
    env.monkeypatch.setattr(store, "database", _RecordingDatabase(store.database, seen))
    env.monkeypatch.setattr(env.repo, "get_conversation", _refuse)
    env.monkeypatch.setattr(store, "get_conversation", _refuse)
    async with _clients(env, world.raw) as (client,):
        page = _payload(await client.call("get_conversation", {
            "conversation_id": cid, "turn_offset": 1,
        }))
        # Byte for byte what the whole-conversation read gave.
        assert page == expected[1]
        reads = [sql for sql in seen if "payload" in sql]
        assert reads and all("LIMIT 1" in sql for sql in reads), reads
        assert not any("ask_trace_steps" in sql or "running" in sql for sql in seen), seen

        # Counted, then deleted before it was read: missing, not an empty turn.
        missing = _error(await client.call("get_conversation", {
            "conversation_id": "conv-nonexistent",
        }))
        counted = store.conversation_turn
        env.monkeypatch.setattr(store, "conversation_turn", lambda conversation_id, index: {
            **counted(conversation_id, index), "turn": None,
        })
        assert _error(await client.call("get_conversation", {
            "conversation_id": cid, "turn_offset": 1,
        })) == missing


async def unscoped_list_judges_reads_in_one_batch_and_builds_no_notebook_summary(env):
    world = _world(env, notebooks=3)
    ids = [_conversation(env, notebook, world.owner_id, [_turn()],
                         updated_at=_stamp(index + 1))
           for index, notebook in enumerate(world.notebooks)]
    reads: list[str] = []

    def counted(notebook_id, user_id):
        reads.append(notebook_id)
        return True

    env.monkeypatch.setattr(env.repo, "user_can_read_notebook", counted)
    env.monkeypatch.setattr(env.repo, "get_notebook", _refuse)
    env.monkeypatch.setattr(env.repo, "list_conversations", _refuse)
    async with _clients(env, world.raw) as (client,):
        assert await _ids(client) == list(reversed(ids))
    # One batch read for the whole allowlist, not one read per notebook.
    assert reads == []


# ------------------------------------------------------------------- ledger


async def notebook_read_books_one_history_call(env):
    world = _world(env, notebooks=2)
    first, second = world.notebooks
    _member_headers, member_id = _user(env)
    _grant_group(env, world.headers, first, member_id)
    cid = _conversation(env, first, world.owner_id, [_turn()])
    theirs = _conversation(env, first, member_id, [_turn()])
    gcid, _ = _global(env, world.owner_id, [{"resolved": [first, second]}])
    async with _clients(env, world.raw) as (client,):
        assert _history_rows(env, first) == 0
        await client.call("get_conversation", {"conversation_id": cid})
        assert _history_rows(env, first) == 1

        # Calls that did not clear every gate leave no row.
        await client.call("get_conversation", {"conversation_id": theirs})
        await client.call("get_conversation", {"conversation_id": "conv-missing"})
        await client.call("get_conversation", {"conversation_id": cid, "turn_offset": 5})
        assert _history_rows(env, first) == 1

        # A global read and a list naming no notebook have no single notebook
        # to book on; a list naming one is booked there.
        await client.call("get_conversation", {"conversation_id": gcid})
        await client.call("list_conversations", {})
        assert _history_rows(env, first) == 1 and _history_rows(env, second) == 0
        await client.call("list_conversations", {"notebook_id": first})
        assert _history_rows(env, first) == 2 and _history_rows(env, second) == 0


CASES = {
    case.__name__: case
    for case in (
        browser_conversation_is_listed_and_readable_through_refs,
        submitted_via_tells_web_from_mcp,
        another_users_conversation_is_invisible_and_indistinguishable,
        notebook_outside_the_allowlist,
        owner_lost_read,
        global_conversation_inside_the_allowlist,
        global_conversation_crossing_the_allowlist_never_shows_its_title,
        global_turn_vanishing_after_the_judgement_is_not_found,
        downgrade_to_no_read,
        list_pages_are_exact,
        turn_paging_is_exact,
        empty_conversation_returns_the_empty_page,
        own_memory_citation_opens_through_mem_ref,
        a_conversation_that_followed_a_report_names_it_and_each_turn_carries_its_receipt,
        a_receipt_that_is_not_valid_reads_as_no_report_on_the_row_and_the_turn,
        a_long_report_title_is_cut_and_counted_wherever_it_appears,
        notebook_turn_reads_only_the_asked_answer,
        unscoped_list_judges_reads_in_one_batch_and_builds_no_notebook_summary,
        notebook_read_books_one_history_call,
    )
}
