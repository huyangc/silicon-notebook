"""``list_reports`` / ``get_report`` through the real MCP entry.

Backend-agnostic: ``tests/test_mcp_reports.py`` (SQLite) and
``tests/postgres/test_mcp_reports_pg.py`` (PostgreSQL) both run every case in
``CASES`` against ``create_app()`` and its real repository and Agent tokens,
through the official MCP client. Reports are seeded as rows (``created_by``,
``content_md``, ``references_json``, ``sections_json`` and ``status`` given
explicitly); the cited source elements are real rows, so a ``ref`` a report
hands out is opened through ``read_reference`` for real.

What these pin: a report is visible to the member who created it and to no one
else (not even inside a shared notebook, and another member's id answers like a
missing one); the notebook gates -- allowlist, the owner's current read access,
the ``read`` tier -- are re-run on every call; and the call ledger books a
``history:read`` row only for a call that cleared every gate.
"""
from __future__ import annotations

import json
from datetime import datetime
from itertools import count
from types import SimpleNamespace

from app.domain.report_failure_note import SECTION_FAILURE_COPY
from tests.answer_link_service_cases import (
    _notebook, _set_access, _user, _write,
)
from tests.citation_check_testkit import seed_source
from tests.mcp_answer_link_cases import _agent, _clients, _error, _payload

_IDS = count(1)
NOT_FOUND = "没有找到这份报告"
NOTICE = "这份报告没有生成完成，请在网页上重新生成"


def _stamp(second: int, minute: int = 0) -> str:
    return f"2026-10-21T08:{minute:02d}:{second:02d}+00:00"


def _reference(index: int, source_id: str = "src-r", **overrides) -> dict:
    """A citation as the report engine stores it, internals included."""
    row = {
        "key": f"k{index}", "object_id": f"OBJ-{index}", "object_type": "element",
        "label": f"标签{index}", "name": "", "source_title": f"文献{index}",
        "source_file_name": f"paper-{index}.pdf", "location_label": f"p{index}",
        "source_id": source_id, "family_key": f"source:{source_id}",
        "family_identity_uncertain": False, "element_id": f"el-r-{index}",
        "snippet": f"第{index}条被引用的摘录。", "tier": "personal",
        "from_reference_library": False, "provenance": {"stage": "internal"},
    }
    row.update(overrides)
    return row


def _insert_report(
    env, notebook_id: str, owner_id: str, *, report_id: str | None = None,
    created_at: str | None = None, question: str = "带宽够吗", status: str = "done",
    content: str = "", references=(), sections=(), error: str = "",
    section_count: int = 3,
) -> str:
    report_id = report_id or f"rep-t4-{next(_IDS)}"
    created_at = created_at or _stamp(next(_IDS) % 60)
    outline = json.dumps(
        [{"title": f"节{index}"} for index in range(section_count)], ensure_ascii=False
    )
    with _write(env) as db:
        db.execute(
            "INSERT INTO reports (id, notebook_id, question, outline_json, "
            "sections_json, references_json, depth, content_md, status, error, "
            "created_by, created_at, updated_at) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (report_id, notebook_id, question, outline,
             json.dumps(list(sections), ensure_ascii=False),
             json.dumps(list(references), ensure_ascii=False), 2, content, status,
             error, owner_id, created_at, created_at),
        )
    return report_id


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


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ------------------------------------------------------------------ reading


async def own_report_is_listed_and_readable_through_refs(env):
    world = _world(env)
    with _write(env) as db:
        seed_source(
            db, "?", notebook_id=world.nb, source_id="src-r1",
            elements={"el-r1-0": "被引用的原文段落。"},
        )
    memory = env.repo.create_memory_candidate(
        world.nb, world.owner_id, None, "t4-memory", "我的记忆", "记忆正文。", [],
        "测试",
    )
    foreign = _reference(
        3, source_id="src-foreign", element_id="el-foreign",
        memory_id="mem-of-someone-else", memory_owner_id="someone-else",
        snippet="另一位成员存下的摘录",
    )
    references = [
        _reference(1, source_id="src-r1", element_id="el-r1-0"),
        _reference(2, object_type="memory", object_id=memory.id, source_id="",
                   element_id="", memory_id=memory.id,
                   memory_owner_id=world.owner_id, snippet="记忆摘录"),
        foreign,
    ]
    report_id = _insert_report(
        env, world.nb, world.owner_id, report_id="rep-own-1",
        question="带宽够吗", content="## 结论\n\n带宽足够 [k1] [k2] [k3]。\n",
        references=references, created_at=_stamp(5),
    )
    async with _clients(env, world.raw) as (client,):
        listing = _payload(await client.call("list_reports", {}))
        assert listing["total"] == 1 and listing["next_offset"] is None
        [row] = listing["items"]
        assert row == {
            "report_id": report_id, "notebook_id": world.nb, "question": "带宽够吗",
            "status": "done", "depth": 2, "section_count": 3,
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }
        assert _instant(row["created_at"]) == _instant(_stamp(5))

        page = _payload(await client.call("get_report", {
            "report_id": report_id, "notebook_id": row["notebook_id"],
        }))
        assert page["content"] == "## 结论\n\n带宽足够 [k1] [k2] [k3]。\n"
        assert page["next_content_offset"] is None and page["error"] == ""
        assert page["total_references"] == 3 and page["status"] == "done"
        element, own_memory, other = page["references"]
        assert element["label"] == "文献1" and element["snippet"] == "第1条被引用的摘录。"

        opened = _payload(await client.call("read_reference", {"ref": element["ref"]}))
        assert opened["kind"] == "el" and opened["text"] == "被引用的原文段落。"
        opened = _payload(await client.call("read_reference", {"ref": own_memory["ref"]}))
        assert opened["kind"] == "mem" and opened["memory_id"] == memory.id
        assert opened["text"] == "记忆正文。"

        # Another member's recorded memory: the excerpt stays, the handle and
        # every memory identifier do not.
        assert "ref" not in other and other["snippet"] == "另一位成员存下的摘录"
        dumped = json.dumps(page, ensure_ascii=False)
        for internal in ("mem-of-someone-else", "someone-else", "memory_owner_id",
                         "provenance", "family_key", "source_file_name"):
            assert internal not in dumped, internal


async def another_members_report_is_invisible(env):
    world = _world(env)
    _, member_id = _user(env)
    _grant_group(env, world.headers, world.nb, member_id)
    mine = _insert_report(env, world.nb, world.owner_id, report_id="rep-mine",
                          content="我的报告。")
    theirs = _insert_report(env, world.nb, member_id, report_id="rep-theirs",
                            content="别人的报告。")
    async with _clients(env, world.raw) as (client,):
        listing = _payload(await client.call("list_reports", {"notebook_id": world.nb}))
        assert [item["report_id"] for item in listing["items"]] == [mine]
        assert listing["total"] == 1
        everywhere = _payload(await client.call("list_reports", {}))
        assert [item["report_id"] for item in everywhere["items"]] == [mine]

        # A report that exists but is someone else's answers exactly like one
        # that does not exist: same code, same words.
        other = await client.call("get_report", {
            "report_id": theirs, "notebook_id": world.nb,
        })
        missing = await client.call("get_report", {
            "report_id": "rep-does-not-exist", "notebook_id": world.nb,
        })
        assert _error(other) == _error(missing)
        code, text = _error(other)
        assert code == "not_found" and NOT_FOUND in text
        assert "别人的报告" not in text
    # The creator, through a token of their own, does read it.
    member_raw, _ = _agent(env, member_id, [world.nb], scopes=("read",), allow_links=False)
    async with _clients(env, member_raw) as (client,):
        page = _payload(await client.call("get_report", {
            "report_id": theirs, "notebook_id": world.nb,
        }))
        assert page["content"] == "别人的报告。"
        hidden = await client.call("get_report", {
            "report_id": mine, "notebook_id": world.nb,
        })
        assert _error(hidden)[0] == "not_found"


async def report_in_a_notebook_outside_the_allowlist(env):
    headers, owner_id = _user(env)
    allowed = _notebook(env, headers, "在白名单里")
    outside = _notebook(env, headers, "不在白名单里")
    raw, _ = _agent(env, owner_id, [allowed], scopes=("read",), allow_links=False)
    inside_id = _insert_report(env, allowed, owner_id, report_id="rep-inside")
    outside_id = _insert_report(env, outside, owner_id, report_id="rep-outside")
    async with _clients(env, raw) as (client,):
        listing = _payload(await client.call("list_reports", {}))
        assert [item["report_id"] for item in listing["items"]] == [inside_id]
        for call, arguments in (
            ("get_report", {"report_id": outside_id, "notebook_id": outside}),
            ("list_reports", {"notebook_id": outside}),
        ):
            code, _text = _error(await client.call(call, arguments))
            assert code == "notebook_not_allowed", (call, code)


async def owner_lost_read(env):
    owner_headers, _ = _user(env)
    nb = _notebook(env, owner_headers)
    reader_headers, reader_id = _user(env)
    group_id = _grant_group(env, owner_headers, nb, reader_id)
    raw, _ = _agent(env, reader_id, [nb], scopes=("read",), allow_links=False)
    report_id = _insert_report(env, nb, reader_id, report_id="rep-reader",
                               content="读者自己的报告。")
    async with _clients(env, raw) as (client,):
        assert _payload(await client.call("list_reports", {}))["total"] == 1

        assert env.client.delete(
            f"/api/groups/{group_id}/members/{reader_id}", headers=owner_headers,
        ).status_code in (200, 204)
        gone = _payload(await client.call("list_reports", {}))
        assert gone["items"] == [] and gone["total"] == 0
        assert gone["next_offset"] is None
        for call, arguments in (
            ("get_report", {"report_id": report_id, "notebook_id": nb}),
            ("list_reports", {"notebook_id": nb}),
        ):
            code, _text = _error(await client.call(call, arguments))
            assert code == "notebook_unreadable", (call, code)

        assert env.client.put(
            f"/api/groups/{group_id}/members/{reader_id}", json={"role": "member"},
            headers=owner_headers,
        ).status_code == 200
        assert _payload(await client.call("list_reports", {}))["total"] == 1
        page = _payload(await client.call("get_report", {
            "report_id": report_id, "notebook_id": nb,
        }))
        assert page["content"] == "读者自己的报告。"


async def wrong_notebook_id_is_not_found(env):
    world = _world(env, notebooks=2)
    first, second = world.notebooks
    report_id = _insert_report(env, first, world.owner_id, report_id="rep-first",
                               content="第一个库里的报告。")
    async with _clients(env, world.raw) as (client,):
        # The report is in ``first``; naming ``second`` (also allowlisted) does
        # not find it, and says so in the report's own words.
        code, text = _error(await client.call("get_report", {
            "report_id": report_id, "notebook_id": second,
        }))
        assert code == "not_found" and NOT_FOUND in text
        assert "list_reports" in text and "notebook_id" in text
        for malformed in ("", "  ", "not-a-report", "conv-1", "rep-" + "x" * 300):
            code, _text = _error(await client.call("get_report", {
                "report_id": malformed, "notebook_id": first,
            }))
            assert code == "not_found", malformed
        # Whitespace around a good id is ignored.
        page = _payload(await client.call("get_report", {
            "report_id": f"  {report_id}  ", "notebook_id": first,
        }))
        assert page["report_id"] == report_id


async def default_notebook_is_used_when_omitted(env):
    world = _world(env, notebooks=2)
    first, second = world.notebooks
    in_default = _insert_report(env, first, world.owner_id, report_id="rep-default",
                                content="默认库里的报告。")
    elsewhere = _insert_report(env, second, world.owner_id, report_id="rep-elsewhere",
                               content="另一个库里的报告。")
    async with _clients(env, world.raw) as (client,):
        page = _payload(await client.call("get_report", {"report_id": in_default}))
        assert page["notebook_id"] == first
        # Not in the default notebook: found only by naming its notebook.
        assert _error(await client.call("get_report", {"report_id": elsewhere}))[0] == (
            "not_found"
        )
        page = _payload(await client.call("get_report", {
            "report_id": elsewhere, "notebook_id": second,
        }))
        assert page["notebook_id"] == second
        # A list with no notebook_id is every notebook, not the default one.
        listing = _payload(await client.call("list_reports", {}))
        assert {item["report_id"] for item in listing["items"]} == {in_default, elsewhere}
        blank = await client.call("list_reports", {"notebook_id": "   "})
        assert _error(blank)[0] == "invalid_argument"


async def downgrade_to_no_read(env):
    world = _world(env, scopes=("read", "ask"))
    report_id = _insert_report(env, world.nb, world.owner_id, report_id="rep-tier",
                               content="正文。")
    async with _clients(env, world.raw) as (client,):
        listed = {tool.name for tool in (await client.session.list_tools()).tools}
        assert {"list_reports", "get_report"} <= listed
        assert _payload(await client.call("list_reports", {}))["total"] == 1

        _set_access(env, world.principal, scopes=["ask"])
        listed = {tool.name for tool in (await client.session.list_tools()).tools}
        assert "list_reports" not in listed and "get_report" not in listed
        for call, arguments in (
            ("list_reports", {}),
            ("list_reports", {"notebook_id": world.nb}),
            ("get_report", {"report_id": report_id, "notebook_id": world.nb}),
            # The tier is checked before the id is looked up, so a token
            # without it cannot use the id to find out what exists.
            ("get_report", {"report_id": "rep-nothing", "notebook_id": world.nb}),
        ):
            code, text = _error(await client.call(call, arguments))
            assert code == "scope_missing" and "读取" in text, (call, code, text)

        _set_access(env, world.principal, scopes=["read", "ask"])
        listed = {tool.name for tool in (await client.session.list_tools()).tools}
        assert {"list_reports", "get_report"} <= listed
        assert _payload(await client.call("get_report", {
            "report_id": report_id, "notebook_id": world.nb,
        }))["content"] == "正文。"


# ------------------------------------------------------------------- paging


async def list_pages_are_exact(env):
    world = _world(env, notebooks=2)
    first, second = world.notebooks
    _, member_id = _user(env)
    _grant_group(env, world.headers, first, member_id)
    ids = {}
    for name, notebook, second_of_minute in (
        ("r1", first, 1), ("r2", first, 3), ("r3", first, 5), ("r4", first, 7),
        ("r5", first, 9), ("s1", second, 6), ("s2", second, 7),
    ):
        ids[name] = _insert_report(
            env, notebook, world.owner_id, report_id=f"rep-lp-{name}",
            created_at=_stamp(second_of_minute), question=f"问题 {name}",
        )
    _insert_report(env, first, member_id, report_id="rep-lp-other",
                   created_at=_stamp(8))
    expected_one = [ids[name] for name in ("r5", "r4", "r3", "r2", "r1")]
    # Newest first; the tie at second 7 breaks on the larger report_id.
    expected_all = [ids[name] for name in ("r5", "s2", "r4", "s1", "r3", "r2", "r1")]
    async with _clients(env, world.raw) as (client,):
        seen, offset, next_offsets = [], 0, []
        while offset is not None:
            page = _payload(await client.call("list_reports", {
                "notebook_id": first, "limit": 2, "offset": offset,
            }))
            assert page["total"] == 5
            seen.extend(item["report_id"] for item in page["items"])
            next_offsets.append(page["next_offset"])
            offset = page["next_offset"]
        assert seen == expected_one and next_offsets == [2, 4, None]

        seen, offset, next_offsets = [], 0, []
        while offset is not None:
            page = _payload(await client.call("list_reports", {
                "limit": 3, "offset": offset,
            }))
            assert page["total"] == 7
            seen.extend(item["report_id"] for item in page["items"])
            next_offsets.append(page["next_offset"])
            offset = page["next_offset"]
        assert seen == expected_all and next_offsets == [3, 6, None]

        only_second = _payload(await client.call("list_reports", {"notebook_id": second}))
        assert [item["report_id"] for item in only_second["items"]] == [
            ids["s2"], ids["s1"]
        ]
        # Past the end is an empty last page, and a negative offset is refused.
        past = _payload(await client.call("list_reports", {"offset": 50}))
        assert past["items"] == [] and past["next_offset"] is None and past["total"] == 7
        bad = await client.call("list_reports", {"offset": -1})
        assert _error(bad)[0] == "invalid_argument"
        bad = await client.call("get_report", {
            "report_id": ids["r1"], "notebook_id": first, "content_offset": -1,
        })
        assert _error(bad)[0] == "invalid_argument"
        bad = await client.call("get_report", {
            "report_id": ids["r1"], "notebook_id": first, "reference_offset": -1,
        })
        assert _error(bad)[0] == "invalid_argument"


async def content_and_references_page_exactly(env):
    world = _world(env)
    body = "".join(chr(0x4E00 + (index * 7919) % 4000) for index in range(20_000))
    references = [_reference(index) for index in range(45)]
    report_id = _insert_report(
        env, world.nb, world.owner_id, report_id="rep-big", content=body,
        references=references,
    )
    async with _clients(env, world.raw) as (client,):
        text, offset = "", 0
        while offset is not None:
            page = _payload(await client.call("get_report", {
                "report_id": report_id, "notebook_id": world.nb,
                "content_offset": offset, "reference_offset": 45,
            }))
            assert page["content_offset"] == offset and page["references"] == []
            assert page["total_characters"] == 20_000 and page["total_references"] == 45
            text += page["content"]
            offset = page["next_content_offset"]
        assert text == body

        keys, offset = [], 0
        while offset is not None:
            page = _payload(await client.call("get_report", {
                "report_id": report_id, "notebook_id": world.nb,
                "content_offset": 20_000, "reference_offset": offset,
            }))
            assert page["content"] == "" and page["next_content_offset"] is None
            keys.extend(row["key"] for row in page["references"])
            offset = page["next_reference_offset"]
        assert keys == [f"k{index}" for index in range(45)]


async def failed_report_never_returns_the_raw_error(env):
    world = _world(env)
    raw = 'Traceback (most recent call last): File "/srv/app/report.py", line 9 boom'
    content = f"## 第一节\n\n（本节生成失败:{raw}）\n\n## 第二节\n\n正常的正文。\n"
    report_id = _insert_report(
        env, world.nb, world.owner_id, report_id="rep-failed", status="failed",
        error=raw, content=content,
        sections=[{"failed": True, "error": raw}, {"failed": False}],
    )
    chinese = _insert_report(
        env, world.nb, world.owner_id, report_id="rep-failed-zh", status="failed",
        error="模型服务暂时不可用", content="正文。",
        created_at=_stamp(30),
    )
    async with _clients(env, world.raw) as (client,):
        page = _payload(await client.call("get_report", {
            "report_id": report_id, "notebook_id": world.nb,
        }))
        dumped = json.dumps(page, ensure_ascii=False)
        assert "Traceback" not in dumped and "/srv/app" not in dumped
        assert SECTION_FAILURE_COPY["internal"] in page["content"]
        assert "正常的正文。" in page["content"]
        assert page["status"] == "failed" and page["error"] == NOTICE
        assert _payload(await client.call("get_report", {
            "report_id": chinese, "notebook_id": world.nb,
        }))["error"] == "模型服务暂时不可用"

        listing = _payload(await client.call("list_reports", {}))
        assert {item["status"] for item in listing["items"]} == {"failed"}
        for item in listing["items"]:
            assert "error" not in item and "progress" not in item
            assert "created_by" not in item
        assert "Traceback" not in json.dumps(listing, ensure_ascii=False)


# ------------------------------------------------------------------- ledger


async def report_get_books_one_history_call(env):
    world = _world(env, notebooks=2)
    first, second = world.notebooks
    _, member_id = _user(env)
    _grant_group(env, world.headers, first, member_id)
    mine = _insert_report(env, first, world.owner_id, report_id="rep-ledger",
                          content="正文。")
    theirs = _insert_report(env, first, member_id, report_id="rep-ledger-theirs")
    outside = _notebook(env, world.headers, "不在白名单里")
    async with _clients(env, world.raw) as (client,):
        assert _history_rows(env, first) == 0
        await client.call("get_report", {"report_id": mine, "notebook_id": first})
        assert _history_rows(env, first) == 1

        # Calls that did not clear every gate leave no row: someone else's
        # report, a missing one, a notebook outside the allowlist.
        await client.call("get_report", {"report_id": theirs, "notebook_id": first})
        await client.call("get_report", {"report_id": "rep-missing", "notebook_id": first})
        await client.call("get_report", {"report_id": mine, "notebook_id": outside})
        await client.call("get_report", {"report_id": "garbage", "notebook_id": first})
        assert _history_rows(env, first) == 1 and _history_rows(env, outside) == 0

        # A list that names a notebook is booked on that notebook; one that
        # does not name any is not booked (there is no single notebook).
        await client.call("list_reports", {"notebook_id": first})
        assert _history_rows(env, first) == 2
        await client.call("list_reports", {})
        assert _history_rows(env, first) == 2 and _history_rows(env, second) == 0


CASES = {
    case.__name__: case
    for case in (
        own_report_is_listed_and_readable_through_refs,
        another_members_report_is_invisible,
        report_in_a_notebook_outside_the_allowlist,
        owner_lost_read,
        wrong_notebook_id_is_not_found,
        default_notebook_is_used_when_omitted,
        downgrade_to_no_read,
        list_pages_are_exact,
        content_and_references_page_exactly,
        failed_report_never_returns_the_raw_error,
        report_get_books_one_history_call,
    )
}
