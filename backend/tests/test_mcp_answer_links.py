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
async def test_global_link_is_refused_when_the_switch_is_off_at_delivery(linked):
    handlers, principal, service, store = linked

    def start(payload, **kwargs):
        principal.allow_answer_links = False  # turned off while it ran
        return job(answer="答案")

    service.start = start
    with pytest.raises(AgentToolError) as refused:
        await handlers["ask"]("问题", None, notebooks=BOTH, mode="chunk",
                              delivery="link")
    assert refused.value.code == "forbidden"
    assert store.calls == []


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
