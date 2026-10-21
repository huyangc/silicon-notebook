"""MCP ``ask(report_id=...)`` through the real MCP entry (plan 2026-10-21 T10).

Backend-agnostic: ``tests/test_mcp_ask_linked_report.py`` (SQLite) and
``tests/postgres/test_mcp_ask_linked_report_pg.py`` (PostgreSQL) run every case
in ``CASES`` against ``create_app()`` and its real repository, Agent tokens and
Ask engine, through the official MCP client. Only the model seats are
replaced -- the answer and rewrite models are recording fakes and the chunk
retrieval hands back a fixed passage -- so everything between the tool call
and the stored receipt is production code: the report gate, the verdict under
the real ceiling, the restore, the prompt, the receipt, the job row.

The world is the report restore's (``tests/report_reference_restore_cases.py``):
Alice owns the notebook ``nb`` (sources ``src-a`` / ``src-b``, her Memory
``mem-alice``) that mounts ``lib`` (``lib-src`` and its knowledge object);
``other`` is a notebook of hers nobody mounted; Bob is another member of ``nb``.

What is pinned here (the Agent-facing face of the follow-up):

* the receipt rides in ``scope.linked_report`` of an answer page (never a new
  top-level key: ``answer_page`` already spends ``OUTPUT_MAPPING_LIMIT``) and at
  the top level of a retrieval-only response, in one shape that never carries
  the owner's Memory ids; ``get_ask`` re-reads it, a running job shows only its
  ``report_id``;
* each refusal's code and wording, the single-notebook rule, a refused report
  leaving nothing behind, and the idempotency key being tied to the report;
* an extension engine and a closed Memory channel answer without reading the
  report, and say so;
* the response stays inside the output budget and the mapping-size cap.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from itertools import count
from types import SimpleNamespace
from typing import Any

from app.api.mcp_tools import ask as ask_module
from app.api.mcp_tools._shared import (
    OUTPUT_MAPPING_LIMIT,
    TEXT_LIMIT,
    TITLE_CHARS,
    TOTAL_TEXT_LIMIT,
)
from app.domain.report_followup import (
    REASON_CLAUSE_MEMORY_CLOSED_CHANNEL,
    REASON_CLAUSES,
    REASON_ENGINE_UNSUPPORTED,
    REASON_LIBRARY_UNAVAILABLE,
    REASON_PREFIX,
)
from app.domain.retrieval import RetrievedChunk
from app.models.ask import QueryIntentContract
from app.models.reports import REPORT_QUESTION_MAX_CHARS
from app.services.ask_service import AskService, _ChunkRetrieval
from app.services.report_followup import REPORT_BLOCK_HEADER
from tests.answer_link_service_cases import _seed_job, _write, make_env
from tests.model_testkit import bind_chat_client
from tests.report_reference_restore_cases import NOW, build_world
from tests.test_memory_mcp import OfficialMcpClient

TITLE = "两种退火工艺的差异与依据"
QUESTION = "两种工艺的依据是什么？"
SECTIONS = ("工艺甲", "工艺乙")
NOT_FOUND = "没有找到这份报告（不存在、不属于此凭证的主人，或不在这个笔记本里）"
NOT_READY = "这份报告还没有生成完成，完成后才能就此追问"
TOO_LONG = "report_id 过长"
SINGLE_NOTEBOOK = (
    "report_id 只能用于单个笔记本的提问：报告属于一个笔记本，跨笔记本提问不读取报告"
)
KEY_REUSED = "这个 client_request_id 已用于另一个问题，请换一个新的 client_request_id"
RECEIPT_KEYS = {
    "report_id", "title", "status", "sections_read", "evidence_restored", "evidence_gone",
    "evidence_over_budget",
}
# What an answer page carries in chunk mode besides ``truncation``: 18 keys of
# every page plus ``anchors``. Pinned so a key lost to the mapping cap is seen.
ANSWER_KEYS = {
    "job_id", "conversation_id", "status", "mode", "answer_id", "answer",
    "next_answer_offset", "citations", "next_citation_offset", "total_citations",
    "grounded", "coverage", "next_coverage_offset", "trace", "error",
    "completeness_notice", "scope", "content_is_untrusted_evidence", "anchors",
}

_IDS = count(1)


def _ref(key: str, object_type: str, object_id: str, *, source_id: str = "",
         element_id: str = "") -> dict:
    return {
        "key": key, "object_type": object_type, "object_id": object_id,
        "source_id": source_id, "element_id": element_id,
    }


#: Four items the restore reads back (a passage, an element, a mounted
#: library's knowledge object, a relation read back as its evidence element),
#: one it skips (external material) and one whose passage no longer exists.
REFERENCES = [
    _ref("k1", "chunk", "c-a", source_id="src-a"),
    _ref("k2", "element", "el-b1", source_id="src-b", element_id="el-b1"),
    _ref("k3", "concept", "kg-lib", source_id="lib-src"),
    _ref("k4", "relation", "rel-a", source_id="src-a", element_id="el-a2"),
    _ref("k6", "external", "https://example.org/paper"),
    _ref("k8", "chunk", "c-gone", source_id="src-a"),
]
MEMORY_REFERENCE = _ref("k5", "memory", "mem-alice")


def _content(title: str = TITLE, sections: tuple[str, ...] = SECTIONS) -> str:
    bodies = [
        "甲的温度窗口较窄 [k1]，其依据见 [k2, k4]。已删段落 [k8]。",
        "乙沿用参考库的概念 [k3]，并结合个人记忆 [k5]。外部资料 [k6]。",
    ]
    lines = [f"# {title}", "", "## 执行摘要", "两种工艺在温度窗口上不同。", "",
             "## 资料基础", "本报告依据笔记本中的资料写成。", ""]
    for index, section in enumerate(sections):
        lines += [f"## {section}", bodies[index % 2], ""]
    return "\n".join(lines + ["## 参考文献", "[k1] 甲段落"])


# ------------------------------------------------------------------- the env


def build_env(database_url: str, tmp_path, monkeypatch, *, postgres: bool):
    for name, value in {
        "DATABASE_URL": database_url,
        "SILICON_NOTEBOOK_STORAGE_DIR": str(tmp_path / "storage"),
        "SILICON_NOTEBOOK_AUTH_OPTIONAL": "false",
        "EVENT_LOG_ENABLED": "false", "LLM_LOG_ENABLED": "false",
        "OPENAI_COMPAT_API_KEY": "", "EMBED_PROVIDER": "",
        "MINERU_MODE": "off", "MINERU_API_URL": "", "MINERU_API_TOKEN": "",
        "MCP_PUBLIC_URL": "https://memory.example.test/mcp",
        "MCP_REQUIRE_HTTPS": "1",
        # ``delivery="link"`` (the long-title case): a deployment that can build
        # a link, with the preview at its largest allowed size.
        "PUBLIC_WEB_BASE_URL": "http://10.0.0.1:3000/kb",
        "MCP_ANSWER_PREVIEW_MAX_CHARS": "2000",
    }.items():
        monkeypatch.setenv(name, value)
    # The turn's own Memory retrieval is off, so the only Memory a prompt can
    # hold is what the report itself read back.
    monkeypatch.setattr(AskService, "_memory_hits", lambda self, *args, **kwargs: [])
    from app.api.deps import repository
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    repository.cache_clear()
    app = create_app()
    repo = repository()
    env = make_env(None, repo)
    env.app = app
    env.monkeypatch = monkeypatch
    env.world = build_world(repo, postgres=postgres)
    return env


async def run_case(env, case) -> None:
    """One scenario inside ONE app lifespan: leaving it closes the PostgreSQL
    pool, so every row check runs before that."""
    async with env.app.router.lifespan_context(env.app):
        await case(env)


# ------------------------------------------------------------------- helpers


def _token(
    env, notebooks: list[str], scopes: tuple[str, ...] = ("read", "ask"), *,
    allow_links: bool = False,
) -> str:
    """A raw Agent token of Alice's (``read`` opens the private-Memory channel,
    ``ask`` alone leaves it closed)."""
    owner_id = env.world.alice.id
    profile = env.repo.create_agent_profile(owner_id, f"t10-bot-{next(_IDS)}", "")
    issued = env.repo.issue_agent_token(
        owner_id, profile.id, list(scopes), notebooks[0], list(notebooks), None,
        allow_answer_links=allow_links,
    )
    return issued.token


def _recording(env, *names: str) -> list[str]:
    """Wrap these facade methods so each call is logged by name (and still
    runs); the log is returned."""
    calls: list[str] = []

    def wrap(label, original):
        def wrapper(*args, **kwargs):
            calls.append(label)
            return original(*args, **kwargs)
        return wrapper

    for name in names:
        env.monkeypatch.setattr(env.repo, name, wrap(name, getattr(env.repo, name)))
    return calls


def _reasoning_engine(env, contract_of=None) -> dict[str, list]:
    """Replace the reasoning understanding and the engine behind ``ask``: the
    understanding returns ``contract_of(question)`` (a clear contract by
    default) and logs the report it was told about; the engine logs
    ``(report_id, confirmed)`` of each request it is handed and answers. The
    log is returned."""
    seen: dict[str, list] = {"preview": [], "requests": []}

    def preview(notebook_id, question, history="", cancel_event=None, **kwargs):
        seen["preview"].append(kwargs.get("report_id"))
        if contract_of is not None:
            return contract_of(question)
        return QueryIntentContract(objective=question, resolved_question=question)

    def ask_with_job(notebook_id, request, *, submitted_via="", attach_only=False,
                     stop=None, output="answer"):
        if attach_only:
            return None
        seen["requests"].append((request.report_id, request.intent is not None))
        return SimpleNamespace(
            answer_id="", answer="答案。", conclusion="答案。", grounded=True,
            evidence_level="grounded", mode=request.mode, conversation_id="conv-x",
            anchors=[], citations=[],
        ), "askjob-not-stored"

    env.monkeypatch.setattr(env.repo, "preview_reasoning_intent", preview)
    env.monkeypatch.setattr(env.repo, "ask_with_job", ask_with_job)
    return seen


@asynccontextmanager
async def _client(env, raw_token: str):
    client = OfficialMcpClient(env.app, raw_token, manage_lifespan=False)
    await client.__aenter__()
    try:
        yield client
    finally:
        await client.__aexit__(None, None, None)


def _payload(result) -> dict:
    assert not result.isError, result
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


def _error(result) -> tuple[str, str]:
    """``(code, message)`` of a failed tool call."""
    assert result.isError, result
    text = " ".join(getattr(item, "text", "") for item in result.content)
    start = text.index("[") + 1
    end = text.index("]", start)
    return text[start:end], text[end + 1:].strip()


def _size(page: dict) -> int:
    return len(json.dumps(
        page, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8"))


def _count(env, table: str, where: str = "", params: tuple = ()) -> int:
    with _write(env) as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} {where}", params
        ).fetchone()
    return int(row["n"])


def _job_report_id(env, job_id: str) -> str:
    with _write(env) as conn:
        row = conn.execute(
            "SELECT report_id FROM ask_jobs WHERE id=?", (job_id,)
        ).fetchone()
    return row["report_id"]


def insert_report(
    env, report_id: str, *, created_by: str | None = None,
    notebook_id: str | None = None, status: str = "done",
    references: list | None = None, memory_used: tuple[str, ...] = (),
    title: str = TITLE, sections: tuple[str, ...] = SECTIONS,
    content_md: str | None = None,
) -> str:
    """A stored deep report: Alice's finished report of ``nb`` by default."""
    world = env.world
    outline = json.dumps([{"title": item} for item in sections], ensure_ascii=False)
    world.execute(
        "INSERT INTO reports (id,notebook_id,question,outline_json,sections_json,"
        "references_json,content_md,status,created_by,understanding_json,"
        "created_at,updated_at) VALUES "
        "({ph},{ph},{ph},{js},{js},{js},{ph},{ph},{ph},{js},{ph},{ph})",
        (
            report_id, notebook_id or world.nb, title, outline, outline,
            json.dumps(REFERENCES if references is None else references),
            content_md or _content(title, sections), status, created_by or world.alice.id,
            json.dumps({"_memory_used": list(memory_used)}), NOW, NOW,
        ),
    )
    return report_id


class _Chat:
    """A configured chat fake: records each prompt, answers ``reply``."""

    configured = True
    model = "fake"

    def __init__(self, reply: dict) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    def chat_json(self, messages, schema_hint, **kwargs):
        self.prompts.append(messages[-1]["content"])
        return json.dumps(self.reply, ensure_ascii=False)


def models(env, answer: str = "乙段落如是说[k1]。", rewritten: str = "两种工艺的依据") -> _Chat:
    """The chunk path's model seats and its retrieval: the turn itself
    retrieves ``c-b`` (k1); everything after the retrieval is production code.
    ``rewritten`` is the follow-up rewrite -- the query that picks which
    sections of a long report are read. Returns the answer model, whose
    prompts show what the turn read."""
    answer_llm = _Chat({"answer": answer, "grounded": True})
    bind_chat_client(env.repo, "ask_answer", answer_llm)
    bind_chat_client(env.repo, "query_rewrite", _Chat({"query": rewritten}))
    chunk = RetrievedChunk(
        chunk_id="c-b", source_id="src-b", source_title="src-b 标题",
        section_path="节", text="乙段落原文", element_ids=["el-b1"], relevance=0.9,
    )

    def retrieve(self, notebook_id, retrieval_query, history, style_block, **kwargs):
        return _ChunkRetrieval(
            selected=[chunk], historical_selected=[chunk], kg_block="",
            kg_id_map={}, kg_hits=[], overlay_on=False,
            baseline_chunk_candidates=[chunk], baseline_kg_truncated=False,
            recalled=1, source_graph_status=None,
        )

    env.monkeypatch.setattr(AskService, "_chunk_retrieve", retrieve)
    return answer_llm


def _ask_args(report_id: str | None = None, **extra: Any) -> dict:
    args: dict[str, Any] = {"question": QUESTION, "mode": "chunk", **extra}
    if report_id is not None:
        args["report_id"] = report_id
    return args


def _assert_answer_page(page: dict) -> None:
    """The page keeps every key it had before and stays inside both caps."""
    assert ANSWER_KEYS <= set(page), sorted(ANSWER_KEYS - set(page))
    assert "linked_report" not in page, "the receipt is nested in scope, never a new key"
    assert len(set(page) - {"truncation"}) <= OUTPUT_MAPPING_LIMIT
    assert _size(page) <= TOTAL_TEXT_LIMIT


# ------------------------------------------------------------------- the cases


async def readable_report_receipt_rides_in_scope_and_get_ask_rereads_it(env):
    """A readable report: the answer page's ``scope.linked_report`` is the
    receipt's projection (the section titles read, the counts, nothing about
    Memory), the report really entered the answer prompt, the job row names it,
    and ``get_ask`` returns the same projection."""
    insert_report(env, "rep-read")
    answer_llm = models(env)
    async with _client(env, _token(env, [env.world.nb])) as client:
        page = _payload(await client.call(
            "ask", _ask_args("rep-read", notebooks=[env.world.nb])
        ))
        assert page["status"] == "answered", page
        _assert_answer_page(page)
        assert page["scope"] == {
            "kind": "notebook", "notebook_ids": [env.world.nb],
            "linked_report": {
                "report_id": "rep-read", "title": TITLE, "status": "read",
                "sections_read": list(SECTIONS),
                "evidence_restored": 4, "evidence_gone": 1, "evidence_over_budget": 0,
            },
        }
        assert set(page["scope"]["linked_report"]) == RECEIPT_KEYS
        prompt = answer_llm.prompts[0]
        assert REPORT_BLOCK_HEADER in prompt
        assert "甲段落原文" in prompt, "the report's cited passage was read back"
        assert _job_report_id(env, page["job_id"]) == "rep-read"
        reread = _payload(await client.call("get_ask", {"job_id": page["job_id"]}))
        _assert_answer_page(reread)
        assert reread["scope"] == page["scope"]
        # The projection never carries the stored receipt's Memory ids.
        for text in (json.dumps(page, ensure_ascii=False),
                     json.dumps(reread, ensure_ascii=False)):
            assert "memory_used" not in text


async def a_report_written_from_memory_is_read_but_its_memory_ids_stay_home(env):
    """A report that used the owner's Memory, asked by a token that may read it:
    the report IS read (the Memory is among what is restored), and the stored
    receipt's ``memory_used`` -- the Memory ids -- appears nowhere in the page,
    neither in the receipt projection nor in the rest of it."""
    insert_report(
        env, "rep-memory-open", references=[*REFERENCES, MEMORY_REFERENCE],
        memory_used=("mem-alice",),
    )
    answer_llm = models(env)
    async with _client(env, _token(env, [env.world.nb])) as client:
        page = _payload(await client.call("ask", _ask_args("rep-memory-open")))
        job_id = page["job_id"]
        reread = _payload(await client.call("get_ask", {"job_id": job_id}))
    receipt = page["scope"]["linked_report"]
    assert receipt["status"] == "read" and receipt["evidence_restored"] == 5
    assert set(receipt) == RECEIPT_KEYS
    assert "mem-alice 原文" in answer_llm.prompts[0], "the Memory was read back"
    # The stored receipt does carry the ids (the share disclosure counts them) ...
    with _write(env) as conn:
        stored = conn.execute(
            "SELECT payload FROM answers WHERE id=?", (page["answer_id"],)
        ).fetchone()["payload"]
    assert "mem-alice" in (stored if isinstance(stored, str) else json.dumps(stored))
    # ... and no MCP page does.
    for text in (json.dumps(page, ensure_ascii=False),
                 json.dumps(reread, ensure_ascii=False)):
        assert "mem-alice" not in text and "memory_used" not in text


async def no_report_and_a_blank_report_id_change_nothing(env):
    """Without ``report_id`` -- or with a blank one, which reads as none -- the
    page's scope is exactly what it was and nothing about a report is stored."""
    insert_report(env, "rep-unused")
    answer_llm = models(env)
    async with _client(env, _token(env, [env.world.nb])) as client:
        for report_id in (None, "", "   "):
            page = _payload(await client.call("ask", _ask_args(report_id)))
            assert page["status"] == "answered"
            assert page["scope"] == {"kind": "notebook", "notebook_ids": [env.world.nb]}
            assert _job_report_id(env, page["job_id"]) == ""
    assert all(REPORT_BLOCK_HEADER not in prompt for prompt in answer_llm.prompts)


async def a_running_job_shows_only_its_report_id(env):
    """``get_ask`` on a job still running: the answer does not exist yet, so
    there is no receipt -- the page names the report the job row carries."""
    insert_report(env, "rep-running")
    nb, owner_id = env.world.nb, env.world.alice.id
    job_id, _ = _seed_job(env, nb, owner_id, status="running")
    plain_id, _ = _seed_job(env, nb, owner_id, status="running")
    with _write(env) as conn:
        conn.execute(
            "UPDATE ask_jobs SET report_id=? WHERE id=?", ("rep-running", job_id)
        )
    async with _client(env, _token(env, [nb])) as client:
        page = _payload(await client.call("get_ask", {"job_id": job_id}))
        assert page["status"] == "running"
        assert page["scope"] == {
            "kind": "notebook", "notebook_ids": [nb],
            "linked_report": {"report_id": "rep-running"},
        }
        plain = _payload(await client.call("get_ask", {"job_id": plain_id}))
        assert plain["scope"] == {"kind": "notebook", "notebook_ids": [nb]}


async def a_report_that_cannot_be_followed_is_refused_before_anything_happens(env):
    """Missing, another member's and another notebook's report are ONE
    ``not_found`` (no existence probe); an unfinished one is
    ``invalid_argument``; an over-long id is refused by length. Each refusal
    runs no model and leaves no job, conversation or answer behind."""
    world = env.world
    insert_report(env, "rep-bob", created_by=world.bob.id)
    insert_report(env, "rep-lib", notebook_id=world.lib)
    insert_report(env, "rep-running", status="running")
    answer_llm = models(env)
    before = {table: _count(env, table) for table in ("ask_jobs", "conversations", "answers")}
    async with _client(env, _token(env, [world.nb])) as client:
        messages = set()
        for report_id in ("rep-nope", "rep-bob", "rep-lib"):
            code, message = _error(await client.call("ask", _ask_args(report_id)))
            assert code == "not_found", (report_id, code, message)
            messages.add(message)
        assert messages == {NOT_FOUND}, "one answer for missing, not yours and elsewhere"
        code, message = _error(await client.call("ask", _ask_args("rep-running")))
        assert (code, message) == ("invalid_argument", NOT_READY)
        code, message = _error(await client.call("ask", _ask_args("r" * 201)))
        assert (code, message) == ("invalid_argument", TOO_LONG)
        # The bound is the model's: 200 characters pass the length rail and
        # then simply name no report.
        code, _ = _error(await client.call("ask", _ask_args("r" * 200)))
        assert code == "not_found"
    assert answer_llm.prompts == []
    assert before == {
        table: _count(env, table) for table in ("ask_jobs", "conversations", "answers")
    }


async def report_id_with_several_notebooks_star_or_a_global_conversation_is_refused(env):
    """The report belongs to one notebook: 2-8 notebooks, ``["*"]`` and a
    ``gconv-`` conversation are each ``invalid_argument`` with the one
    sentence, before any repository, report or global-Ask work."""
    world = env.world
    insert_report(env, "rep-multi")
    models(env)
    calls = _recording(env, "get_report", "ask_with_job", "preview_reasoning_intent")
    env.monkeypatch.setattr(
        ask_module, "global_ask_service",
        lambda: calls.append("global_ask_service") or SimpleNamespace(),
    )
    token = _token(env, [world.nb, world.other])
    async with _client(env, token) as client:
        for extra in (
            {"notebooks": [world.nb, world.other]},
            {"notebooks": ["*"]},
            {"conversation_id": "gconv-anything"},
        ):
            code, message = _error(await client.call(
                "ask", _ask_args("rep-multi", **extra)
            ))
            assert (code, message) == ("invalid_argument", SINGLE_NOTEBOOK), extra
    assert calls == []


async def a_notebook_outside_the_allowlist_is_refused_before_any_report_is_looked_up(env):
    """The token's own authority over the notebook comes first: asked about a
    notebook outside its allowlist, a call names no report to anyone -- the
    owner's finished report there, an unfinished one and an id that exists
    nowhere are refused with exactly what the same call WITHOUT a report gets,
    and no report is read. (A report gate that ran first would answer "not
    found" / "not finished" to a token that may not even see the notebook, and
    tell it which of the ids exist.)"""
    world = env.world
    insert_report(env, "rep-other-done", notebook_id=world.other)
    insert_report(env, "rep-other-running", notebook_id=world.other, status="running")
    models(env)
    calls = _recording(env, "get_report")
    async with _client(env, _token(env, [world.nb])) as client:
        plain = _error(await client.call("ask", _ask_args(notebooks=[world.other])))
        assert plain not in {("not_found", NOT_FOUND), ("invalid_argument", NOT_READY)}
        for report_id in ("rep-other-done", "rep-other-running", "rep-nope"):
            refused = _error(await client.call(
                "ask", _ask_args(report_id, notebooks=[world.other])
            ))
            assert refused == plain, report_id
    assert calls == []


async def a_report_is_not_read_when_the_memory_channel_is_closed(env):
    """A report written from the owner's Memory, asked by a token without the
    ``read`` tier: the turn answers without reading it, and the page says why in
    the token's own terms -- without the Memory ids."""
    insert_report(
        env, "rep-memory", references=[*REFERENCES, MEMORY_REFERENCE],
        memory_used=("mem-alice",),
    )
    answer_llm = models(env)
    async with _client(env, _token(env, [env.world.nb], scopes=("ask",))) as client:
        page = _payload(await client.call("ask", _ask_args("rep-memory")))
    assert page["status"] == "answered" and page["answer"]
    _assert_answer_page(page)
    assert page["scope"]["linked_report"] == {
        "report_id": "rep-memory", "title": TITLE, "status": "skipped",
        "reason": REASON_PREFIX + REASON_CLAUSE_MEMORY_CLOSED_CHANNEL,
        "sections_read": [], "evidence_restored": 0, "evidence_gone": 0, "evidence_over_budget": 0,
    }
    assert all(REPORT_BLOCK_HEADER not in prompt for prompt in answer_llm.prompts)
    assert "mem-alice" not in json.dumps(page, ensure_ascii=False)


async def a_report_citing_an_unusable_library_is_skipped_with_its_reason(env):
    """The conflict wording reaches the page as the receipt's ``reason``, and
    only then: a read receipt has no ``reason`` key at all."""
    insert_report(env, "rep-unmounted", references=[
        _ref("k1", "chunk", "c-other", source_id="other-src"),
    ])
    insert_report(env, "rep-fine")
    models(env)
    async with _client(env, _token(env, [env.world.nb])) as client:
        skipped = _payload(await client.call("ask", _ask_args("rep-unmounted")))
        read = _payload(await client.call("ask", _ask_args("rep-fine")))
    assert skipped["scope"]["linked_report"] == {
        "report_id": "rep-unmounted", "title": TITLE, "status": "skipped",
        "reason": REASON_PREFIX + REASON_CLAUSES[REASON_LIBRARY_UNAVAILABLE].format(n=1),
        "sections_read": [], "evidence_restored": 0, "evidence_gone": 0, "evidence_over_budget": 0,
    }
    assert skipped["status"] == "answered"
    assert "reason" not in read["scope"]["linked_report"]
    assert read["scope"]["linked_report"]["status"] == "read"


async def an_extension_engine_answers_without_the_report_and_says_so(env):
    """A deployment engine plugin cannot read a report (it takes no report
    port): the call is not refused -- the receipt is ``skipped`` with the
    engine reason. The report gate still applies to the id."""
    from app.extensions.bootstrap import build_extension_runtime
    from tests.test_ask_engine_plugin import _bundle, _Provider

    runtime = build_extension_runtime((_bundle("alpha", _Provider("alpha.kg")),))
    env.monkeypatch.setattr(
        "app.bootstrap.application_extension_runtime",
        lambda: SimpleNamespace(ask_engines=runtime.ask_engines),
    )
    env.monkeypatch.setattr(env.repo._runtime.ask_component, "ask_engine_host", runtime.ask_engines)
    insert_report(env, "rep-engine")
    async with _client(env, _token(env, [env.world.nb])) as client:
        page = _payload(await client.call(
            "ask", {"question": QUESTION, "mode": "alpha.kg", "report_id": "rep-engine"}
        ))
        assert page["status"] == "answered" and page["answer"], page
        assert page["scope"]["linked_report"] == {
            "report_id": "rep-engine", "title": TITLE, "status": "skipped",
            "reason": REASON_PREFIX + REASON_CLAUSES[REASON_ENGINE_UNSUPPORTED],
            "sections_read": [], "evidence_restored": 0, "evidence_gone": 0, "evidence_over_budget": 0,
        }
        code, message = _error(await client.call(
            "ask", {"question": QUESTION, "mode": "alpha.kg", "report_id": "rep-nope"}
        ))
        assert (code, message) == ("not_found", NOT_FOUND)


async def a_retried_key_is_tied_to_its_report(env):
    """Same key, same report: the job it already ran, no second model call.
    Same key under another report -- or none -- is the existing key-reuse
    refusal; the original job is untouched."""
    insert_report(env, "rep-a")
    insert_report(env, "rep-b")
    answer_llm = models(env)
    async with _client(env, _token(env, [env.world.nb])) as client:
        first = _payload(await client.call(
            "ask", _ask_args("rep-a", client_request_id="t10-key")
        ))
        assert first["scope"]["linked_report"]["report_id"] == "rep-a"
        calls = len(answer_llm.prompts)
        replay = _payload(await client.call(
            "ask", _ask_args("rep-a", client_request_id="t10-key")
        ))
        assert replay["job_id"] == first["job_id"]
        assert replay["scope"] == first["scope"]
        assert len(answer_llm.prompts) == calls
        for report_id in ("rep-b", None):
            code, message = _error(await client.call(
                "ask", _ask_args(report_id, client_request_id="t10-key")
            ))
            assert (code, message) == ("invalid_argument", KEY_REUSED), report_id
    assert len(answer_llm.prompts) == calls
    assert _job_report_id(env, first["job_id"]) == "rep-a"


async def a_retrieval_only_call_carries_the_receipt_and_the_restored_evidence(env):
    """``output="evidence"``: the receipt is a top-level ``linked_report`` of
    the same shape, the report's cited passage is among the items, and the page
    keeps its own key budget; a closed Memory channel reads as ``skipped``."""
    insert_report(env, "rep-evidence")
    models(env)
    async with _client(env, _token(env, [env.world.nb])) as client:
        page = _payload(await client.call(
            "ask", _ask_args("rep-evidence", output="evidence")
        ))
        assert page["status"] == "retrieved"
        assert page["linked_report"] == {
            "report_id": "rep-evidence", "title": TITLE, "status": "read",
            "sections_read": list(SECTIONS), "evidence_restored": 4,
            "evidence_gone": 1, "evidence_over_budget": 0,
        }
        assert "scope" not in page and len(page) <= OUTPUT_MAPPING_LIMIT
        texts = " ".join(str(item.get("text") or "") for item in page["items"])
        assert "甲段落原文" in texts and "乙段落原文" in texts
        code, message = _error(await client.call(
            "ask", _ask_args("rep-nope", output="evidence")
        ))
        assert (code, message) == ("not_found", NOT_FOUND)
        plain = _payload(await client.call("ask", _ask_args(output="evidence")))
        assert "linked_report" not in plain
    insert_report(
        env, "rep-evidence-memory", references=[*REFERENCES, MEMORY_REFERENCE],
        memory_used=("mem-alice",),
    )
    async with _client(env, _token(env, [env.world.nb], scopes=("ask",))) as client:
        closed = _payload(await client.call(
            "ask", _ask_args("rep-evidence-memory", output="evidence")
        ))
    assert closed["linked_report"]["status"] == "skipped"
    assert closed["linked_report"]["sections_read"] == []
    assert "mem-alice" not in json.dumps(closed, ensure_ascii=False)


async def the_understanding_step_is_told_about_the_report(env):
    """Reasoning mode: the report gate runs BEFORE the understanding model
    call (a refused report costs none), and the understanding step and the
    confirmed request both carry the report."""
    insert_report(env, "rep-reasoning")
    seen = _reasoning_engine(env)
    async with _client(env, _token(env, [env.world.nb])) as client:
        code, message = _error(await client.call("ask", {
            "question": QUESTION, "mode": "reasoning", "report_id": "rep-nope",
        }))
        assert (code, message) == ("not_found", NOT_FOUND)
        assert seen == {"preview": [], "requests": []}
        page = _payload(await client.call("ask", {
            "question": QUESTION, "mode": "reasoning", "report_id": "rep-reasoning",
        }))
        assert page["status"] == "answered"
    assert seen == {"preview": ["rep-reasoning"], "requests": [("rep-reasoning", True)]}


async def a_clarification_handle_is_good_only_for_the_report_it_was_issued_for(env):
    """A question that attached a report was understood with that report's
    outline, so its handle answers only the same report: the same question
    under another report -- or under none -- is the handle refusal (which says
    to repeat ``report_id``), nothing runs, and the right report still works.
    A handle issued for a question WITHOUT a report is as it always was."""
    insert_report(env, "rep-a")
    insert_report(env, "rep-b")
    seen = _reasoning_engine(env, lambda question: QueryIntentContract(
        objective=question, resolved_question=question, needs_clarification=True,
        ambiguities=[{"id": "ambiguity-1", "question": "哪个对象？",
                      "required": True, "options": ["A", "B"]}],
    ))
    answers = [{"id": "ambiguity-1", "answer": "A"}]

    def args(report_id: str | None, token: str | None = None) -> dict:
        args: dict[str, Any] = {"question": QUESTION, "mode": "reasoning"}
        if report_id is not None:
            args["report_id"] = report_id
        if token is not None:
            args["intent"] = {"intent_token": token, "answers": answers}
        return args

    async with _client(env, _token(env, [env.world.nb])) as client:
        issued = _payload(await client.call("ask", args("rep-a")))
        assert issued["status"] == "needs_clarification"
        assert "report_id" in issued["next_step"]
        for report_id in ("rep-b", None):
            code, message = _error(await client.call(
                "ask", args(report_id, issued["intent_token"])
            ))
            assert code == "invalid_argument", report_id
            assert "intent_token 无效或已过期" in message and "report_id" in message
        assert seen["requests"] == [], "a refused handle runs nothing"
        page = _payload(await client.call("ask", args("rep-a", issued["intent_token"])))
        assert page["status"] == "answered"
        assert seen["requests"] == [("rep-a", True)]
        # The other way round: a handle of a question without a report is not
        # good for a question that names one -- and still is for its own.
        plain = _payload(await client.call("ask", args(None)))
        code, message = _error(await client.call(
            "ask", args("rep-a", plain["intent_token"])
        ))
        assert code == "invalid_argument" and "intent_token 无效或已过期" in message
        page = _payload(await client.call("ask", args(None, plain["intent_token"])))
        assert page["status"] == "answered"
        assert seen["requests"] == [("rep-a", True), (None, True)]


async def evidence_left_out_for_space_is_counted_not_hidden(env):
    """The report cites more than the turn's evidence budget for it can hold:
    the items read back but left out are ``evidence_over_budget`` -- the number
    the web receipt stored, not a silent zero -- so an Agent can tell "left out
    for space" from "has no support"."""
    env.monkeypatch.setattr(
        env.repo._runtime.ask_component.settings, "ask_report_evidence_budget_chars", 2000
    )
    for number in (1, 2, 3):
        env.world.execute(
            "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
            "element_ids,created_at) VALUES ({ph},{ph},{ph},{ph},'节',{js},{ph})",
            (f"c-big{number}", env.world.nb, "src-a",
             f"第{number}大段：" + f"工艺参数{number}的记录。" * 200, json.dumps([]), NOW),
        )
    references = [_ref(f"k{n}", "chunk", f"c-big{n}", source_id="src-a") for n in (1, 2, 3)]
    content = "\n".join([
        f"# {TITLE}", "", "## 执行摘要", "三段记录。", "", "## 资料基础", "资料。", "",
        "## 大段记录", "第一段 [k1]，第二段 [k2]，第三段 [k3]。", "", "## 参考文献", "[k1] 甲",
    ])
    insert_report(env, "rep-over", sections=("大段记录",), references=references,
                  content_md=content)
    models(env)
    async with _client(env, _token(env, [env.world.nb])) as client:
        page = _payload(await client.call("ask", _ask_args("rep-over")))
    receipt = page["scope"]["linked_report"]
    with _write(env) as conn:
        stored = conn.execute(
            "SELECT payload FROM answers WHERE id=?", (page["answer_id"],)
        ).fetchone()["payload"]
    stored = json.loads(stored) if isinstance(stored, str) else stored
    assert receipt["status"] == "read"
    assert receipt["evidence_over_budget"] >= 1
    assert receipt["evidence_over_budget"] == stored["linked_report"]["evidence_over_budget"]
    assert receipt["evidence_restored"] == stored["linked_report"]["evidence_restored"] >= 1


async def a_report_too_long_for_one_turn_names_only_what_the_turn_read(env):
    """A report that does not fit the turn's report budget: ``sections_read``
    lists the sections whose text entered the prompt (the one the question is
    about), not the report's whole outline -- and a summary that alone
    overflows the budget is flagged ``summary_truncated`` (the key exists only
    then)."""
    env.monkeypatch.setattr(
        env.repo._runtime.ask_component.settings, "ask_report_context_max_chars", 2000
    )
    titles = ("低温回火", "高温退火", "表面渗氮")
    themes = {"低温回火": "回火保温时间与内应力的释放", "高温退火": "高温退火的工艺参数与炉温曲线",
              "表面渗氮": "渗氮层深度与硬度梯度"}

    def content(summary: str) -> str:
        lines = [f"# {TITLE}", "", "## 执行摘要", summary, "", "## 资料基础", "资料。", ""]
        for title in titles:
            lines += [f"## {title}", (themes[title] + "。") * 100, ""]
        return "\n".join(lines + ["## 参考文献", "[k1] 甲段落"])

    insert_report(env, "rep-big-body", sections=titles, references=[],
                  content_md=content("三道工艺各有取舍。"))
    insert_report(env, "rep-big-summary", sections=titles, references=[],
                  content_md=content("摘要" * 1500))
    answer_llm = models(env, rewritten="高温退火的工艺参数")
    async with _client(env, _token(env, [env.world.nb])) as client:
        body = _payload(await client.call("ask", _ask_args("rep-big-body")))
        summary = _payload(await client.call("ask", _ask_args("rep-big-summary")))
    receipt = body["scope"]["linked_report"]
    assert (receipt["status"], receipt["sections_read"]) == ("read", ["高温退火"])
    assert "summary_truncated" not in receipt
    block = answer_llm.prompts[0]
    assert themes["高温退火"] in block
    assert themes["低温回火"] not in block and themes["表面渗氮"] not in block
    receipt = summary["scope"]["linked_report"]
    assert (receipt["status"], receipt["sections_read"]) == ("read", [])
    assert receipt["summary_truncated"] is True


async def a_very_long_report_title_cannot_squeeze_the_answer_or_break_the_link(env):
    """The longest title a report may have (``REPORT_QUESTION_MAX_CHARS``) and
    the most sections a report has, with an answer of several pages: the title
    is cut to ``TITLE_CHARS`` like a conversation's (the cut disclosed in
    ``truncation``, the report id never cut), so the answer keeps its full page
    -- the title is not what the budget takes room from -- and the page stays
    inside the byte budget and the key cap. The link delivery, which refuses to
    deliver a clipped preview or link, still delivers."""
    title = "题" * REPORT_QUESTION_MAX_CHARS
    sections = tuple(f"第{index}节的标题" for index in range(1, 13))
    insert_report(env, "rep-long", title=title, sections=sections)
    answer = "乙段落如是说[k1]。" + "答" * (3 * TEXT_LIMIT)
    models(env, answer=answer)
    async with _client(env, _token(env, [env.world.nb], allow_links=True)) as client:
        page = _payload(await client.call("ask", _ask_args("rep-long")))
        assert page["status"] == "answered"
        _assert_answer_page(page)
        assert page["answer"] == answer[:TEXT_LIMIT], "a title halved the answer's page"
        assert page["next_answer_offset"] == TEXT_LIMIT
        receipt = page["scope"]["linked_report"]
        assert receipt["report_id"] == "rep-long" and receipt["status"] == "read"
        assert receipt["title"] == title[:TITLE_CHARS - 1] + "…"
        assert page["truncation"]["truncated"] is True
        assert page["truncation"]["omitted_characters"] >= len(title) - TITLE_CHARS
        assert 0 < len(receipt["sections_read"]) <= OUTPUT_MAPPING_LIMIT
        second = _payload(await client.call("get_ask", {
            "job_id": page["job_id"], "answer_offset": TEXT_LIMIT,
        }))
        assert second["answer"] == answer[TEXT_LIMIT:2 * TEXT_LIMIT]
        evidence = _payload(await client.call(
            "ask", _ask_args("rep-long", output="evidence")
        ))
        assert evidence["linked_report"]["title"] == receipt["title"]
        assert len(evidence) <= OUTPUT_MAPPING_LIMIT
        linked = _payload(await client.call(
            "ask", _ask_args("rep-long", delivery="link")
        ))
        assert linked["status"] == "answered" and linked["link"]["url"]
        assert linked["preview"] and "…" not in linked["preview"][:-1]
        assert linked["scope"]["linked_report"]["title"] == receipt["title"]


CASES = {
    case.__name__: case for case in (
        readable_report_receipt_rides_in_scope_and_get_ask_rereads_it,
        a_report_written_from_memory_is_read_but_its_memory_ids_stay_home,
        no_report_and_a_blank_report_id_change_nothing,
        a_running_job_shows_only_its_report_id,
        a_report_that_cannot_be_followed_is_refused_before_anything_happens,
        report_id_with_several_notebooks_star_or_a_global_conversation_is_refused,
        a_notebook_outside_the_allowlist_is_refused_before_any_report_is_looked_up,
        a_report_is_not_read_when_the_memory_channel_is_closed,
        a_report_citing_an_unusable_library_is_skipped_with_its_reason,
        an_extension_engine_answers_without_the_report_and_says_so,
        a_retried_key_is_tied_to_its_report,
        a_retrieval_only_call_carries_the_receipt_and_the_restored_evidence,
        the_understanding_step_is_told_about_the_report,
        a_clarification_handle_is_good_only_for_the_report_it_was_issued_for,
        evidence_left_out_for_space_is_counted_not_hidden,
        a_report_too_long_for_one_turn_names_only_what_the_turn_read,
        a_very_long_report_title_cannot_squeeze_the_answer_or_break_the_link,
    )
}
