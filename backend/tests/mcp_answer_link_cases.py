"""MCP answer links through the real MCP entry (T7): ``ask`` / ``get_ask`` with
``delivery="link"``.

Backend-agnostic: ``tests/test_mcp_answer_links.py`` (SQLite) and
``tests/postgres/test_mcp_answer_links_pg.py`` (PostgreSQL) both run every
case in ``CASES`` against ``create_app()`` and its real repository, answer-link
store and Agent tokens, through the official MCP client. Notebook jobs are
seeded as rows (``answer_link_service_cases._seed_job``); ``ask``'s engine is
replaced by a fake ``ask_with_job`` that seeds the job it "ran", so every
delivery gate after it reads real rows.

The security rule pinned here: a link is issued only after the same checks
``get_ask`` applies to an inline read -- never for another owner's job, a
browser job, a job outside the allowlist, or one that is not answered -- and a
refused call writes no ``answer_links`` row.
"""
from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from datetime import datetime
from itertools import count
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.api.mcp_tools import ask as ask_module
from tests.answer_link_service_cases import (
    _notebook, _set_access, _user, _write, make_env,
)
from tests.answer_link_service_cases import _seed_job as _seed_row
from tests.global_ask_share_cases import insert_conversation, insert_job
from tests.test_memory_mcp import OfficialMcpClient

BASE = "http://10.0.0.1:3000/kb"
_IDS = count(1)
# §2.5: the link page carries exactly these (plus the budget's ``truncation``).
LINK_KEYS = {
    "job_id", "conversation_id", "status", "mode", "preview",
    "preview_truncated", "link", "grounded", "total_citations",
    "completeness_notice", "scope", "error", "content_is_untrusted_evidence",
    "truncation",
}


def build_env(database_url: str, tmp_path, monkeypatch):
    for name, value in {
        "DATABASE_URL": database_url,
        "SILICON_NOTEBOOK_STORAGE_DIR": str(tmp_path / "storage"),
        "SILICON_NOTEBOOK_AUTH_OPTIONAL": "false",
        "EVENT_LOG_ENABLED": "false", "LLM_LOG_ENABLED": "false",
        "OPENAI_COMPAT_API_KEY": "", "EMBED_PROVIDER": "",
        "MINERU_MODE": "off", "MINERU_API_URL": "", "MINERU_API_TOKEN": "",
        "MCP_PUBLIC_URL": "https://memory.example.test/mcp",
        "MCP_REQUIRE_HTTPS": "1",
        "PUBLIC_WEB_BASE_URL": BASE,
        "MCP_ANSWER_LINK_TTL_HOURS": "48",
    }.items():
        monkeypatch.setenv(name, value)
    from app.api.deps import repository
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    repository.cache_clear()
    app = create_app()
    env = make_env(TestClient(app), repository())
    env.app = app
    env.monkeypatch = monkeypatch
    return env


# ------------------------------------------------------------------ helpers


def _agent(env, owner_id: str, notebooks: list[str], *, scopes=("ask",),
           allow_links: bool = True):
    profile = env.repo.create_agent_profile(owner_id, f"t7-bot-{next(_IDS)}", "")
    issued = env.repo.issue_agent_token(
        owner_id, profile.id, list(scopes), notebooks[0], list(notebooks), None,
        allow_answer_links=allow_links,
    )
    return issued.token, env.repo.refresh_agent_principal(issued.id)


def _link_rows(env) -> int:
    with _write(env) as conn:
        return int(conn.execute(
            "SELECT COUNT(*) AS n FROM answer_links"
        ).fetchone()["n"])


def _job_rows(env) -> int:
    with _write(env) as conn:
        return int(conn.execute("SELECT COUNT(*) AS n FROM ask_jobs").fetchone()["n"])


def _payload(result) -> dict:
    assert not result.isError, result
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


def _error(result) -> tuple[str, str]:
    assert result.isError, result
    text = " ".join(getattr(item, "text", "") for item in result.content)
    start = text.index("[") + 1
    return text[start:text.index("]", start)], text


async def run_case(env, case) -> None:
    """One scenario inside ONE app lifespan: leaving it closes the
    PostgreSQL pool, so every row check runs before that."""
    async with env.app.router.lifespan_context(env.app):
        await case(env)


@asynccontextmanager
async def _clients(env, *raw_tokens):
    clients = []
    try:
        for raw in raw_tokens:
            client = OfficialMcpClient(env.app, raw, manage_lifespan=False)
            clients.append(await client.__aenter__())
        yield clients
    finally:
        for client in reversed(clients):
            await client.__aexit__(None, None, None)


def _answer_payload(answer: str) -> dict:
    return {"conclusion": answer, "answer": answer, "evidence_level": "grounded",
            "anchors": [], "citations": []}


def _seed_job(env, notebook_id: str, owner_id: str, *, payload=None, **fields):
    """One seeded notebook job whose stored answer is a valid ``AskResponse``
    (``get_ask`` validates it before paging)."""
    return _seed_row(env, notebook_id, owner_id,
                     payload=payload or _answer_payload("答案正文。"), **fields)


def _fake_engine(env, owner_id: str, *, answer: str = "答案正文。",
                 status: str = "done", during=None) -> list[str]:
    """Replace the engine: each run seeds one ``mcp`` job (``status``) owned by
    ``owner_id`` and returns it; a retried ``client_request_id`` attaches to
    the job its first run seeded, as the real store does. ``during`` runs
    inside the "engine" (e.g. to flip the token's switch mid-run)."""
    seeded: list[str] = []
    keyed: dict[str, tuple] = {}

    def ask_with_job(notebook_id, request, *, submitted_via="", attach_only=False,
                     stop=None, output="answer"):
        key = request.client_request_id or ""
        if attach_only:
            return keyed.get(key)
        job_id, conversation_id = _seed_job(
            env, notebook_id, owner_id, status=status,
            payload=_answer_payload(answer),
        )
        seeded.append(job_id)
        if during is not None:
            during()
        response = SimpleNamespace(
            answer=answer, conclusion=answer, answer_id="", grounded=True,
            evidence_level="grounded", mode=request.mode,
            conversation_id=conversation_id, anchors=[], citations=[],
        ) if status == "done" else None
        if key:
            keyed[key] = (response, job_id)
        return response, job_id

    env.monkeypatch.setattr(env.repo, "ask_with_job", ask_with_job)
    env.monkeypatch.setattr(
        env.repo, "get_notebook", lambda _id: SimpleNamespace(ask_available=True)
    )
    return seeded


def _assert_link_page(page: dict, job_id: str) -> None:
    assert set(page) == LINK_KEYS, sorted(page)
    assert len(page) <= 20
    assert page["job_id"] == job_id and page["status"] == "answered"
    assert page["link"]["url"].startswith(BASE + "/c/alnk-")
    assert datetime.fromisoformat(page["link"]["expires_at"]).tzinfo is not None
    assert page["content_is_untrusted_evidence"] is True
    for absent in ("answer", "citations", "coverage", "trace", "anchors"):
        assert absent not in page


# ------------------------------------------------------------------ ask


async def ask_link_returns_preview_url_and_expiry(env):
    owner, owner_id = _user(env)
    nb = _notebook(env, owner)
    raw, _ = _agent(env, owner_id, [nb])
    seeded = _fake_engine(env, owner_id, answer="**结论**：带宽足够。后续说明。")
    booked: list = []
    env.monkeypatch.setattr(
        ask_module, "_record_agent_call", lambda *args: booked.append(args[2:])
    )
    async with _clients(env, raw) as (client,):
        page = _payload(await client.call("ask", {
            "question": "带宽够吗", "mode": "chunk", "delivery": "link",
        }))
        # A delivered link page is booked once, like an inline answer.
        assert booked == [(nb, "ask:execute")]
        inline = _payload(await client.call("ask", {"question": "带宽够吗", "mode": "chunk"}))
    _assert_link_page(page, seeded[0])
    assert page["preview"] == "结论：带宽足够。后续说明。"
    assert page["preview_truncated"] is False
    assert page["scope"] == {"kind": "notebook", "notebook_ids": [nb]}
    assert _link_rows(env) == 1
    # inline is the unchanged answer page and issues nothing.
    assert inline["answer"] == "**结论**：带宽足够。后续说明。" and "link" not in inline
    assert _link_rows(env) == 1


async def inline_default_matches_the_pre_link_shape(env):
    """``delivery`` omitted is byte-for-byte the page ``ask`` / ``get_ask``
    returned before answer links existed (pinned as literals taken from the
    implementation at 083e1fb00, not compared against the new code)."""
    owner, owner_id = _user(env)
    nb = _notebook(env, owner)
    raw, _ = _agent(env, owner_id, [nb])
    job_id, conversation_id = _seed_job(env, nb, owner_id)
    seeded = _fake_engine(env, owner_id)
    async with _clients(env, raw) as (client,):
        read = _payload(await client.call("get_ask", {"job_id": job_id}))
        asked = _payload(await client.call("ask", {"question": "带宽够吗", "mode": "chunk"}))
    assert read == _pre_link_page(job_id, conversation_id, nb, "reasoning", False)
    assert asked == _pre_link_page(seeded[0], asked["conversation_id"], nb, "chunk", True)
    assert asked["conversation_id"].startswith("conv-")


def _pre_link_page(job_id, conversation_id, nb, mode, grounded) -> dict:
    """The first answer page of a one-sentence, uncited notebook answer, as
    the pre-link implementation (083e1fb00) returned it."""
    return {
        "job_id": job_id, "conversation_id": conversation_id,
        "status": "answered", "mode": mode, "answer_id": "",
        "answer": "答案正文。", "next_answer_offset": None,
        "citations": [], "next_citation_offset": None, "total_citations": 0,
        "grounded": grounded,
        "coverage": {
            "resolved": 1, "searched": 1, "skipped": 0, "degraded": 0,
            "offset": 0, "skipped_notebooks": [], "degraded_notebook_ids": [],
        },
        "next_coverage_offset": None,
        "trace": {"steps": [], "offset": 0, "next_offset": None, "total": 0},
        "error": "", "completeness_notice": "",
        "scope": {"kind": "notebook", "notebook_ids": [nb]},
        "content_is_untrusted_evidence": True,
        "truncation": {
            "budget_chars": 12000, "truncated": False, "omitted_items": 0,
            "omitted_map_entries": 0, "omitted_characters": 0, "omitted_fields": 0,
        },
    }


async def inline_default_is_identical_to_explicit_inline(env):
    owner, owner_id = _user(env)
    nb = _notebook(env, owner)
    raw, _ = _agent(env, owner_id, [nb])
    job_id, _ = _seed_job(env, nb, owner_id)
    async with _clients(env, raw) as (client,):
        default = _payload(await client.call("get_ask", {"job_id": job_id}))
        explicit = _payload(await client.call(
            "get_ask", {"job_id": job_id, "delivery": "inline"}
        ))
    assert default == explicit and "answer" in default
    assert _link_rows(env) == 0


async def keyed_retry_reuses_the_same_link(env):
    owner, owner_id = _user(env)
    nb = _notebook(env, owner)
    raw, _ = _agent(env, owner_id, [nb])
    seeded = _fake_engine(env, owner_id)
    arguments = {"question": "带宽够吗", "mode": "chunk", "delivery": "link",
                 "client_request_id": "t7-retry"}
    async with _clients(env, raw) as (client,):
        first = _payload(await client.call("ask", {**arguments, "link_ttl_hours": 2}))
        again = _payload(await client.call("ask", arguments))
    assert len(seeded) == 1, "the retry attached to the first job"
    assert again["job_id"] == first["job_id"]
    # Reused with its ORIGINAL expiry (link_ttl_hours shapes a new link only).
    assert again["link"] == first["link"]
    assert _link_rows(env) == 1


async def switch_turned_off_mid_run_is_forbidden_and_answer_is_saved(env):
    owner, owner_id = _user(env)
    nb = _notebook(env, owner)
    raw, principal = _agent(env, owner_id, [nb])
    _fake_engine(
        env, owner_id,
        during=lambda: _set_access(env, principal, allow_links=False),
    )
    booked: list = []
    env.monkeypatch.setattr(
        ask_module, "_record_agent_call", lambda *args: booked.append(args)
    )
    async with _clients(env, raw) as (client,):
        refused = await client.call("ask", {
            "question": "带宽够吗", "mode": "chunk", "delivery": "link",
        })
        code, text = _error(refused)
        # The refusal itself names the saved job (no client_request_id was
        # sent, so this is the caller's only handle on it).
        match = re.search(r"job_id: (askjob-[A-Za-z0-9_-]+)", text)
        assert match, text
        saved = _payload(await client.call("get_ask", {"job_id": match.group(1)}))
    assert code == "forbidden" and "Agent 接入" in text and "get_ask" in text
    assert saved["status"] == "answered" and saved["answer"] == "答案正文。"
    assert _link_rows(env) == 0
    # A refused delivery hands nothing back, so the ask is not booked
    # (get_ask books nothing either).
    assert booked == []


async def token_without_answer_links_is_forbidden_before_any_work(env):
    owner, owner_id = _user(env)
    nb = _notebook(env, owner)
    raw, _ = _agent(env, owner_id, [nb], allow_links=False)
    seeded = _fake_engine(env, owner_id)
    job_id, _ = _seed_job(env, nb, owner_id)
    async with _clients(env, raw) as (client,):
        asked = await client.call("ask", {
            "question": "带宽够吗", "mode": "chunk", "delivery": "link",
        })
        read = await client.call("get_ask", {"job_id": job_id, "delivery": "link"})
        inline = _payload(await client.call("get_ask", {"job_id": job_id}))
    for refused in (asked, read):
        code, text = _error(refused)
        assert code == "forbidden"
        assert "允许签发限时回答链接" in text and "Agent 接入" in text
    assert seeded == [], "refused before the engine ran"
    assert inline["status"] == "answered"
    assert _link_rows(env) == 0


async def argument_refusals_come_before_any_work(env):
    owner, owner_id = _user(env)
    nb = _notebook(env, owner)
    raw, _ = _agent(env, owner_id, [nb])
    seeded = _fake_engine(env, owner_id)
    job_id, _ = _seed_job(env, nb, owner_id)
    jobs_before = _job_rows(env)
    ask = {"question": "带宽够吗", "mode": "chunk"}
    async with _clients(env, raw) as (client,):
        results = {
            "bad_delivery": await client.call("ask", {**ask, "delivery": "email"}),
            "evidence": await client.call("ask", {
                **ask, "output": "evidence", "delivery": "link",
            }),
            "ttl_inline": await client.call("ask", {**ask, "link_ttl_hours": 2}),
            "ttl_high": await client.call("ask", {
                **ask, "delivery": "link", "link_ttl_hours": 49,
            }),
            "ttl_zero": await client.call("ask", {
                **ask, "delivery": "link", "link_ttl_hours": 0,
            }),
            "get_bad_delivery": await client.call(
                "get_ask", {"job_id": job_id, "delivery": "page"}
            ),
            "get_ttl_inline": await client.call(
                "get_ask", {"job_id": job_id, "link_ttl_hours": 1}
            ),
            "get_offsets": await client.call(
                "get_ask", {"job_id": job_id, "delivery": "link", "answer_offset": 5}
            ),
        }
        # No base configured: unavailable, also before any work.
        settings = ask_module.get_settings()
        env.monkeypatch.setattr(
            ask_module, "get_settings",
            lambda: settings.model_copy(update={"public_web_base_url": ""}),
        )
        unavailable = [
            await client.call("ask", {**ask, "delivery": "link"}),
            await client.call("get_ask", {"job_id": job_id, "delivery": "link"}),
        ]
    for name, result in results.items():
        assert _error(result)[0] == "invalid_argument", name
    assert "48" in _error(results["ttl_high"])[1]
    assert "仅检索" in _error(results["evidence"])[1]
    for result in unavailable:
        code, text = _error(result)
        assert code == "unavailable" and "PUBLIC_WEB_BASE_URL" in text
    assert seeded == [] and _job_rows(env) == jobs_before
    assert _link_rows(env) == 0


async def clarification_and_unanswered_statuses_keep_the_inline_shape(env):
    owner, owner_id = _user(env)
    nb = _notebook(env, owner)
    raw, _ = _agent(env, owner_id, [nb])
    failed = _fake_engine(env, owner_id, status="failed")
    jobs = {status: _seed_job(env, nb, owner_id, status=status)[0]
            for status in ("running", "failed", "cancelled")}
    async with _clients(env, raw) as (client,):
        # 「分析一下」 always needs clarification (no understanding model here).
        clarify = _payload(await client.call("ask", {
            "question": "分析一下", "mode": "reasoning", "delivery": "link",
        }))
        ran = _payload(await client.call("ask", {
            "question": "带宽够吗", "mode": "chunk", "delivery": "link",
        }))
        reads = {
            status: (
                _payload(await client.call("get_ask", {"job_id": job_id, "delivery": "link"})),
                _payload(await client.call("get_ask", {"job_id": job_id})),
            )
            for status, job_id in jobs.items()
        }
    assert clarify["status"] == "needs_clarification" and "link" not in clarify
    assert ran["job_id"] == failed[0] and ran["status"] == "failed"
    assert "link" not in ran and "answer" in ran
    for status, (linked, inline) in reads.items():
        assert linked == inline, status
        assert "link" not in linked
    assert _link_rows(env) == 0


async def link_preview_is_built_from_the_full_answer(env):
    owner, owner_id = _user(env)
    nb = _notebook(env, owner)
    raw, _ = _agent(env, owner_id, [nb])
    table = "| 参数 | 数值 |\n| --- | --- |\n" + "".join(
        f"| 第{index}项参数名称很长 | {index * 1000} 欧姆 |\n" for index in range(120)
    )
    assert len(table) > 2500
    answer = table + "\n表后结论：阻抗匹配。"
    job_id, _ = _seed_job(env, nb, owner_id, payload=_answer_payload(answer))
    async with _clients(env, raw) as (client,):
        page = _payload(await client.call("get_ask", {"job_id": job_id, "delivery": "link"}))
    _assert_link_page(page, job_id)
    assert "（表格，见链接）" in page["preview"]
    assert "表后结论：阻抗匹配。" in page["preview"]


async def get_ask_link_issues_then_reuses(env):
    owner, owner_id = _user(env)
    nb = _notebook(env, owner)
    raw, principal = _agent(env, owner_id, [nb])
    job_id, _ = _seed_job(env, nb, owner_id)
    async with _clients(env, raw) as (client,):
        first = _payload(await client.call(
            "get_ask", {"job_id": job_id, "delivery": "link", "link_ttl_hours": 3}
        ))
        again = _payload(await client.call(
            "get_ask", {"job_id": job_id, "delivery": "link", "link_ttl_hours": 1}
        ))
    _assert_link_page(first, job_id)
    assert again["link"] == first["link"]
    row = env.store.answer_link(first["link"]["url"].rsplit("/c/", 1)[1])
    assert row["job_id"] == job_id and row["agent_token_id"] == principal.token_id


async def get_ask_link_never_issues_for_a_job_it_could_not_read(env):
    """Another owner's job, a browser job, a job outside the allowlist and a
    Memory-channel job replayed to an ask-only token are refused exactly as
    an inline read is, and none of them leaves an ``answer_links`` row."""
    owner, owner_id = _user(env)
    nb = _notebook(env, owner)
    nb_other = _notebook(env, owner, "另一个库")
    stranger, stranger_id = _user(env)
    nb_stranger = _notebook(env, stranger)
    raw, _ = _agent(env, owner_id, [nb])
    jobs = {
        "foreign": _seed_job(env, nb_stranger, stranger_id)[0],
        "browser": _seed_job(env, nb, owner_id, via="web")[0],
        "outside_allowlist": _seed_job(env, nb_other, owner_id)[0],
        "memory": _seed_job(env, nb, owner_id, memory=True)[0],
        "evidence": _seed_job(env, nb, owner_id, output="evidence")[0],
    }
    expected = {
        "foreign": "not_found", "browser": "not_found",
        "outside_allowlist": "not_found", "memory": "scope_missing",
        "evidence": "invalid_argument",
    }
    async with _clients(env, raw) as (client,):
        results = {
            name: (
                await client.call("get_ask", {"job_id": job_id, "delivery": "link"}),
                await client.call("get_ask", {"job_id": job_id}),
            )
            for name, job_id in jobs.items()
        }
    for name, (linked, inline) in results.items():
        assert _error(linked)[0] == expected[name], name
        assert _error(inline)[0] == expected[name], name
    assert _link_rows(env) == 0


async def global_get_ask_link_rechecks_the_job_first(env):
    owner, owner_id = _user(env)
    nb_a = _notebook(env, owner, "甲")
    nb_b = _notebook(env, owner, "乙")
    raw, principal = _agent(env, owner_id, [nb_a, nb_b])
    store = env.repo._runtime.global_ask_store
    conversation_id, _ = insert_conversation(store, "gconv-t7", owner_id)
    for job_id, via in (("gask-t7-mcp", "mcp"), ("gask-t7-web", "web")):
        insert_job(store, conversation_id, owner_id, job_id, "2026-01-01T00:00:01",
                   payload={
                       "job_id": job_id, "conversation_id": conversation_id,
                       "status": "done", "submitted_via": via,
                       "question": "跨库问题", "created_at": "2026-01-01T00:00:01+00:00",
                       "notebook_scope": {"mode": "include", "notebook_ids": [nb_a, nb_b]},
                       "resolved_notebook_ids": [nb_a, nb_b],
                       "searched_notebook_ids": [nb_a, nb_b],
                       "cited_notebook_ids": [nb_b], "error": None,
                       "response": {
                           "answer_id": "ans-t7", "question": "跨库问题",
                           "answer": "跨库答案", "grounded": True, "citations": [],
                           "created_at": "2026-01-01T00:00:01+00:00",
                           "notebook_scope": {"mode": "include",
                                              "notebook_ids": [nb_a, nb_b]},
                           "resolved_notebook_ids": [nb_a, nb_b],
                           "searched_notebook_ids": [nb_a, nb_b],
                           "cited_notebook_ids": [nb_b],
                       },
                   })
        with store.database.write() as db:
            db.execute(store._sql(
                "UPDATE global_ask_jobs SET submitted_via=? WHERE id=?"), (via, job_id))
    async with _clients(env, raw) as (client,):
        browser = await client.call(
            "get_ask", {"job_id": "gask-t7-web", "delivery": "link"}
        )
        page = _payload(await client.call(
            "get_ask", {"job_id": "gask-t7-mcp", "delivery": "link"}
        ))
        _set_access(env, principal, notebooks=[nb_a])
        narrowed = await client.call(
            "get_ask", {"job_id": "gask-t7-mcp", "delivery": "link"}
        )
    assert _error(browser)[0] == "not_found"
    _assert_link_page(page, "gask-t7-mcp")
    assert page["preview"] == "跨库答案"
    assert page["scope"]["kind"] == "global"
    # Narrowed allowlist: refused like the inline read, no second row.
    assert _error(narrowed)[0] == "not_found"
    assert _link_rows(env) == 1


async def plugin_engine_answer_gets_a_link(env):
    owner, owner_id = _user(env)
    nb = _notebook(env, owner)
    raw, _ = _agent(env, owner_id, [nb])
    seeded = _fake_engine(env, owner_id, answer="插件引擎的回答。")

    class _Engines:
        def mode(self, mode_id):
            return SimpleNamespace(id=mode_id) if mode_id == "niuma.analog" else None

        def is_available(self, mode_id):
            return mode_id == "niuma.analog"

        def registrations(self):
            return (SimpleNamespace(descriptor=SimpleNamespace(mode_id="niuma.analog")),)

    env.monkeypatch.setattr(
        "app.bootstrap.application_extension_runtime",
        lambda: SimpleNamespace(ask_engines=_Engines()),
    )
    async with _clients(env, raw) as (client,):
        page = _payload(await client.call("ask", {
            "question": "插件问题", "mode": "niuma.analog", "delivery": "link",
        }))
    _assert_link_page(page, seeded[0])
    assert page["mode"] == "niuma.analog" and page["preview"] == "插件引擎的回答。"


CASES = {case.__name__: case for case in (
    ask_link_returns_preview_url_and_expiry,
    inline_default_matches_the_pre_link_shape,
    inline_default_is_identical_to_explicit_inline,
    keyed_retry_reuses_the_same_link,
    switch_turned_off_mid_run_is_forbidden_and_answer_is_saved,
    token_without_answer_links_is_forbidden_before_any_work,
    argument_refusals_come_before_any_work,
    clarification_and_unanswered_statuses_keep_the_inline_shape,
    link_preview_is_built_from_the_full_answer,
    get_ask_link_issues_then_reuses,
    get_ask_link_never_issues_for_a_job_it_could_not_read,
    global_get_ask_link_rechecks_the_job_first,
    plugin_engine_answer_gets_a_link,
)}
