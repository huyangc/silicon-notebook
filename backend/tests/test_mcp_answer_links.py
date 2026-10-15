"""MCP answer links (T7): ``ask`` / ``get_ask`` with ``delivery="link"``.

The end-to-end scenarios live in ``tests/mcp_answer_link_cases.py`` and run
here on SQLite (and on PostgreSQL in ``tests/postgres/test_mcp_answer_links_pg.py``).
The global ``ask`` path (2-8 notebooks and ``["*"]``) is driven through the
global adapter seam of ``test_global_ask_mcp``.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api.mcp_tools import ask as ask_module
from app.api.mcp_tools._shared import AgentToolError
from tests.mcp_answer_link_cases import CASES, LINK_KEYS, build_env, run_case
from tests.test_global_ask_mcp import BOTH, adapter, job  # noqa: F401 - fixture


@pytest.fixture
def env(tmp_path, monkeypatch):
    from app.api.deps import repository

    built = build_env(f"sqlite:///{tmp_path / 'mcp.db'}", tmp_path, monkeypatch)
    try:
        yield built
    finally:
        repository.cache_clear()


@pytest.mark.anyio
@pytest.mark.parametrize("case", sorted(CASES))
async def test_mcp_answer_link_scenario(env, case):
    await run_case(env, CASES[case])


# ------------------------------------------------------- global ask (adapter)


class _LinkStore:
    def __init__(self):
        self.calls: list[dict] = []
        self.tokens: dict[tuple, str] = {}

    def issue_answer_link(self, **kwargs):
        self.calls.append(kwargs)
        key = (kwargs["job_kind"], kwargs["job_id"], kwargs["agent_token_id"])
        reused = key in self.tokens
        token = self.tokens.setdefault(key, f"alnk-{len(self.tokens) + 1}")
        return {"token": token, "expires_at": "2026-10-22T08:00:00+00:00",
                "reused": reused}


@pytest.fixture
def linked(adapter, monkeypatch):  # noqa: F811 - the imported fixture
    handlers, principal, repo, service = adapter
    principal.allow_answer_links = True
    store = _LinkStore()
    settings = SimpleNamespace(
        public_web_base_url="http://10.0.0.1:3000", mcp_answer_link_ttl_hours=168,
        mcp_answer_link_grace_hours=168, mcp_answer_preview_max_chars=300,
    )
    monkeypatch.setattr(ask_module, "answer_link_repository", lambda: store)
    monkeypatch.setattr(ask_module, "get_settings", lambda: settings)
    return handlers, principal, service, store


@pytest.mark.anyio
@pytest.mark.parametrize("notebooks", [BOTH, ["*"]])
async def test_global_ask_link_for_two_notebooks_and_star(linked, notebooks):
    handlers, principal, service, store = linked
    service.start = lambda payload, **kwargs: job(answer="跨库的**回答**。")
    page = await handlers["ask"](
        "问题", None, notebooks=notebooks, mode="chunk", delivery="link",
        link_ttl_hours=5, client_request_id="g-key",
    )
    assert set(page) == LINK_KEYS
    assert page["job_id"] == "gask-a" and page["scope"]["kind"] == "global"
    assert page["link"] == {"url": "http://10.0.0.1:3000/c/alnk-1",
                            "expires_at": "2026-10-22T08:00:00+00:00"}
    assert page["preview"] == "跨库的回答。"
    [call] = store.calls
    assert call["job_kind"] == "global" and call["job_id"] == "gask-a"
    assert call["owner_id"] == "owner-a" and call["agent_token_id"] == "token-a"
    assert call["ttl_seconds"] == 5 * 3600
    # A keyed retry is replayed to the same job and reuses the same link.
    service.replay = lambda *args, **kwargs: job(answer="跨库的**回答**。")
    again = await handlers["ask"](
        "问题", None, notebooks=notebooks, mode="chunk", delivery="link",
        client_request_id="g-key",
    )
    assert again["link"] == page["link"]


@pytest.mark.anyio
async def test_global_link_is_refused_when_the_switch_is_off_at_delivery(
    linked, monkeypatch,
):
    handlers, principal, service, store = linked
    booked: list = []
    monkeypatch.setattr(ask_module, "_record_agent_call",
                        lambda *args: booked.append(args[2:]))

    def start(payload, **kwargs):
        principal.allow_answer_links = False  # turned off while it ran
        return job(answer="答案")

    service.start = start
    with pytest.raises(AgentToolError) as refused:
        await handlers["ask"]("问题", None, notebooks=BOTH, mode="chunk",
                              delivery="link")
    assert refused.value.code == "forbidden"
    # The refusal names the saved job so it can still be read with get_ask.
    assert "job_id: gask-a" in refused.value.message
    assert "get_ask" in refused.value.message
    assert store.calls == [] and booked == []
    # The switch back on: delivered, and booked per resolved notebook.
    principal.allow_answer_links = True
    service.start = lambda payload, **kwargs: job(answer="答案")
    await handlers["ask"]("问题", None, notebooks=BOTH, mode="chunk", delivery="link")
    assert booked == [("nb-a", "ask:execute")]


@pytest.mark.anyio
async def test_an_unexpected_issue_failure_is_not_reported_as_a_ttl_error(linked):
    """``link_ttl_hours`` is validated before any work; a ``ValueError`` from
    issuing itself is a fault, never relabelled as the caller's argument."""
    handlers, _, service, store = linked

    def broken(**kwargs):
        raise ValueError("store fault")

    store.issue_answer_link = broken
    service.start = lambda payload, **kwargs: job(answer="答案")
    with pytest.raises(ValueError, match="store fault"):
        await handlers["ask"]("问题", None, notebooks=BOTH, mode="chunk",
                              delivery="link", link_ttl_hours=3)


@pytest.mark.anyio
async def test_a_real_keyed_retry_attaches_to_its_job_and_reuses_the_link(env):
    """The real ``ask_with_job`` (attach_only probe, durable job store); only
    the model call is a stand-in. The retry runs no engine, attaches to the
    first job and gets the first link back."""
    from app.services.sqlite_repository import _now
    from tests.answer_link_service_cases import _notebook, _user
    from tests.mcp_answer_link_cases import _agent, _clients, _link_rows, _payload
    from tests.model_testkit import bind_chat_client

    owner, owner_id = _user(env)
    nb = _notebook(env, owner)
    raw, _ = _agent(env, owner_id, [nb])
    now = _now()
    with env.repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("src-t7", nb, "Evidence", "markdown", "extracted", "parsed", "e.md",
             "", 0, "", "", "textbook", now, now),
        )
        db.execute(
            "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
            "element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
            ("chunk-t7", nb, "src-t7", "evidence exists in this notebook", "1",
             "[]", now),
        )
    calls: list = []

    class _AnswerClient:
        configured = True

        def chat_json(self, *args, **kwargs):
            calls.append(1)
            return '{"answer":"Evidence exists [k1].","grounded":true}'

    bind_chat_client(env.repo, "ask_answer", _AnswerClient())
    arguments = {"question": "What evidence exists?", "mode": "chunk",
                 "delivery": "link", "client_request_id": "t7-real-key"}
    async with env.app.router.lifespan_context(env.app):
        async with _clients(env, raw) as (client,):
            first = _payload(await client.call("ask", {**arguments, "link_ttl_hours": 2}))
            engine_calls = len(calls)
            again = _payload(await client.call("ask", arguments))
        assert _link_rows(env) == 1
    assert engine_calls >= 1 and len(calls) == engine_calls, "the retry attached"
    assert first["job_id"].startswith("askjob-") and again["job_id"] == first["job_id"]
    assert again["link"] == first["link"]
    assert first["preview"].startswith("Evidence exists") and "[k1]" not in first["preview"]
    assert env.repo.ask_job_origin(first["job_id"])["submitted_via"] == "mcp"


@pytest.mark.anyio
async def test_global_get_ask_of_a_browser_job_issues_nothing(linked):
    handlers, _, service, store = linked
    browser = job(answer="答案")
    browser.submitted_via = "web"
    service.get_job = lambda job_id, **kwargs: browser
    with pytest.raises(AgentToolError) as refused:
        await handlers["get_ask"]("gask-a", None, delivery="link")
    assert refused.value.code == "not_found"
    assert store.calls == []


@pytest.mark.anyio
async def test_a_failed_global_job_keeps_the_inline_shape(linked):
    handlers, _, service, store = linked
    failed = job()
    failed.status = "failed"
    service.start = lambda payload, **kwargs: failed
    page = await handlers["ask"]("问题", None, notebooks=BOTH, mode="chunk",
                                 delivery="link")
    inline = await handlers["ask"]("问题", None, notebooks=BOTH, mode="chunk")
    assert page == inline and "link" not in page
    assert store.calls == []
