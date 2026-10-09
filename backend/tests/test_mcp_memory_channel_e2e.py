"""E1-3: ``memory:read`` and the default ceiling through the REAL MCP tool entry.

Two members share one notebook.  Alice has a confirmed Memory whose derived
source carries an element and a knowledge-graph object; Bob has none.  Every
call goes through the official in-process MCP client, the real ``ask``
/ ``search`` (``include="formal"``) handlers, the real Ask service and the real
stores -- only the answer model is a stub, and it cites every evidence key it
was shown, so the prompt it received IS the retrieval result.

What must hold (each assertion names the mutation it catches in the report):

* Alice without ``memory:read``: no Memory citation or anchor, none of her
  Memory's text, element or KG object in the prompt, and the Memory store is
  never queried (the channel is closed inside the worker thread).
* Alice with ``memory:read``: her Memory is retrieved as before.
* Bob, full scopes: none of Alice's Memory-derived content, ever -- the ask
  runs under the default ceiling, whose hidden half is the asker's own.
* ``search`` (formal) without ``memory:read``: no Memory item and no
  Memory store query; with it, the Memory item is returned.  Deliberately
  NOT asserted: that search's knowledge-graph leg omits Memory-derived
  objects.  It does not today -- it returns ``MEMKGSECRET`` to every token,
  Alice's and Bob's, with or without ``memory:read`` -- and nothing on this
  branch changes that leg.

``build_mcp_app`` / ``seed_shared_notebook`` / ``assert_*`` are backend
neutral; ``tests/postgres/test_mcp_memory_channel_pg.py`` runs the same
assertions on PostgreSQL.
"""
from __future__ import annotations

import json
import re

import pytest

from app.api.deps import (
    identity_repository,
    mcp_memory_repository,
    notebook_catalog_repository,
    notebook_sharing_repository,
    repository,
)
from app.core.config import get_settings
from app.core.request_context import reset_request_user, set_request_user
from app.models.schemas import NotebookCreate
from app.repositories.ports import ChunkWrite, SourceElementWrite
from tests.model_testkit import bind_chat_client
from tests.test_memory_mcp import OfficialMcpClient, _payload


NOW = "2026-09-29T00:00:00+00:00"
TERM = "zephyrloop"
QUESTION = f"What does {TERM} say about bandwidth?"
VISIBLE_TEXT = f"{TERM} bandwidth visible chunk evidence"
VISIBLE_ELEMENT = f"{TERM} bandwidth visible element VISELEMMARK"
VISIBLE_KG = f"{TERM} bandwidth visible concept VISKGMARK"
MEMORY_BODY = f"{TERM} bandwidth private note MEMITEMSECRET"
MEMORY_ELEMENT = f"{TERM} bandwidth memory element MEMELEMSECRET"
MEMORY_KG = f"{TERM} bandwidth memory concept MEMKGSECRET"
MEMORY_SECRETS = ("MEMITEMSECRET", "MEMELEMSECRET", "MEMKGSECRET")
MIXED_MEMORY_QUOTE = f"{TERM} bandwidth mixed occurrence MIXMEMQUOTE"
FULL_SCOPES = ["read", "ask", "contribute"]
# Without the ``read`` tier the Memory channel stays closed. Since the tier
# merge reading Memory is part of ``read``, so the closed variant is an
# ask-only token (``ask`` needs the ``ask`` tier alone).
NO_MEMORY_SCOPES = ["ask"]


def build_mcp_app(database_url: str, tmp_path, monkeypatch) -> dict:
    """``create_app()`` on ``database_url`` with Alice, Bob and one shared notebook."""
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "")
    monkeypatch.setenv("EMBED_PROVIDER", "")
    monkeypatch.setenv("MINERU_MODE", "off")
    monkeypatch.setenv("MINERU_API_URL", "")
    monkeypatch.setenv("MINERU_API_TOKEN", "")
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://memory.example.test/mcp")
    monkeypatch.setenv("MCP_REQUIRE_HTTPS", "1")
    get_settings.cache_clear()
    repository.cache_clear()

    from app.main import create_app

    app = create_app()
    identity = identity_repository()
    alice = identity.create_user("a00129001", "pw")
    bob = identity.create_user("b00129002", "pw")
    marker = set_request_user(alice)
    try:
        notebook = notebook_catalog_repository().create_notebook(
            NotebookCreate(name="Shared loop notebook")
        )
    finally:
        reset_request_user(marker)
    notebook_sharing_repository().add_member(notebook.id, bob.id)
    service = mcp_memory_repository()
    return {
        "app": app, "service": service, "alice": alice, "bob": bob,
        "notebook": notebook,
        "alice_profile": service.create_agent_profile(alice.id, "Claude Code", ""),
        "bob_profile": service.create_agent_profile(bob.id, "Codex", ""),
    }


def token(env: dict, user: str, scopes: list[str]) -> str:
    notebook_id = env["notebook"].id
    return env["service"].issue_agent_token(
        env[user].id, env[f"{user}_profile"].id, scopes,
        notebook_id, [notebook_id], None,
    ).token


def seed_shared_notebook(env: dict, placeholder: str) -> dict:
    """A visible source (chunk + element + KG object) and Alice's confirmed
    Memory with its derived source, one element and one KG object."""
    repo = repository()
    runtime = repo._runtime
    notebook_id = env["notebook"].id
    alice = env["alice"].id
    sources = runtime.source_store
    ph = placeholder

    def insert_source(source_id: str, source_type: str, memory_id: str = ""):
        sources.insert_source(
            source_id=source_id, notebook_id=notebook_id, title=f"{TERM} {source_id}",
            source_type=source_type, status="active", parse_status="parsed",
            file_name=f"{source_id}.md", file_path="", file_size=0, file_hash="",
            summary="", doc_type="", memory_id=memory_id,
        )

    def insert_object(object_id: str, source_id: str, element_id: str, name: str,
                      also: tuple[tuple[str, str, str], ...] = ()):
        occurrences = [(source_id, element_id, name), *also]
        evidence = json.dumps([{
            "source_id": occurrence_source, "source_title": occurrence_source,
            "element_id": occurrence_element, "element_type": "paragraph",
            "location_label": "p1", "quoted_span": quote, "confidence": 1.0,
        } for occurrence_source, occurrence_element, quote in occurrences])
        with repo._write() as db:
            db.execute(
                "INSERT INTO knowledge_objects "
                "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,"
                f"created_at,updated_at) VALUES ({ph},{ph},'concept','approved','',"
                f"{ph},{ph},{ph},{ph},{ph})",
                (object_id, notebook_id, json.dumps({"name": name, "definition": name}),
                 evidence, source_id, NOW, NOW),
            )
            # The KG extraction pipeline writes the object's reverse source
            # index and, on SQLite, its lexical shadow (PostgreSQL searches the
            # payload itself).  A closed Memory channel binds the ceiling, and
            # the bound KG reads judge an object through exactly these, so
            # without them the visible object is unreachable there.
            for occurrence_source in dict.fromkeys(o[0] for o in occurrences):
                db.execute(
                    "INSERT INTO knowledge_object_sources "
                    f"(object_id,source_id,notebook_id) VALUES ({ph},{ph},{ph})",
                    (object_id, occurrence_source, notebook_id),
                )
            if ph == "?":
                db.execute(
                    "INSERT INTO kg_objects_fts(object_id,notebook_id,name) "
                    "VALUES (?,?,?)",
                    (object_id, notebook_id, name),
                )

    # Alice's Memory: created and confirmed through the real service (its
    # recall index is the service's), with the KG ingest job disabled so the
    # derived source below is the only one and nothing races the assertions.
    runtime.memory_service.kg_ingest_scheduler = lambda fn, item: None
    service = env["service"]
    memory = service.create_memory_candidate(
        notebook_id, alice, env["alice_profile"].id, "e13-memory",
        f"{TERM} bandwidth memory", MEMORY_BODY, [], "test",
    )
    service.confirm_memory(memory.id, alice)

    insert_source("src-visible", "markdown")
    insert_source("src-memory-alice", "memory", memory_id=memory.id)
    with repo._write() as db:
        sources.replace_elements(
            db, "src-visible",
            [SourceElementWrite("el-visible", "paragraph", "p1", VISIBLE_ELEMENT, {})],
            created_at=NOW,
        )
        sources.replace_elements(
            db, "src-memory-alice",
            [SourceElementWrite("el-memory", "paragraph", "p1", MEMORY_ELEMENT, {})],
            created_at=NOW,
        )
        runtime.chunk_store.insert_rows(
            db, notebook_id, "src-visible",
            [ChunkWrite("chunk-visible", VISIBLE_TEXT, "1", ("el-visible",))],
            created_at=NOW,
        )
    # ``node_context`` scenario: the visible object ALSO cites Alice's Memory
    # element (mixed evidence).  Whoever may not read that Memory must get the
    # object with the visible occurrence only -- the re-read binds the ceiling
    # (``ceiling_binds``: withheld Memory, or another member's Memory present).
    insert_object("ko-visible", "src-visible", "el-visible", VISIBLE_KG, also=(
        ("src-memory-alice", "el-memory", MIXED_MEMORY_QUOTE),
    ))
    insert_object("ko-memory", "src-memory-alice", "el-memory", MEMORY_KG)
    # Raw rows skip the extraction pipeline's embed step; without vectors the
    # KG arms cannot see either object and the test would prove nothing.
    repo._embed_objects_batch(notebook_id, [
        {"_oid": "ko-visible", "payload": {"name": VISIBLE_KG}},
        {"_oid": "ko-memory", "payload": {"name": MEMORY_KG}},
    ])
    repo.collection_catalog.invalidate()
    return {"memory_id": memory.id}


class _ReasoningModel:
    """A reasoning run's model: a clear understanding contract, one sub-query,
    reflect answers at once, and the synthesis cites EVERY evidence key it was
    shown -- so the recorded synthesis prompts are the retrieval result."""

    configured = True

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def chat_json(self, messages, schema_hint="", *args, **kwargs):
        schema_hint = schema_hint or ""
        if "normalized_question" in schema_hint:
            return json.dumps({
                "normalized_question": QUESTION, "intent_type": "explain",
                "result_scope": "ranked", "completeness_required": False,
                "entities": [], "mandatory_topics": [], "ambiguities": [],
                "needs_clarification": False, "confidence": 0.9,
            })
        if "sub_queries" in schema_hint:
            return json.dumps({"sub_queries": [{"query": f"{TERM} bandwidth"}]})
        if "next_action" in schema_hint:
            return json.dumps({"next_action": "answer", "sufficient": True})
        text = "\n".join(str(message.get("content") or "") for message in messages)
        self.prompts.append(text)
        keys = sorted(set(re.findall(r"\bk\d+\b", text)))
        cited = " ".join(f"[{key}]" for key in keys)
        return json.dumps({"answer": f"{TERM} evidence {cited}", "grounded": True})


class _MemoryStoreCalls:
    """Counts every Memory recall query that reaches the store."""

    def __init__(self, monkeypatch) -> None:
        self.calls = 0
        store = repository()._runtime.memory_retriever.store
        original = store.memory_retrieval_rows

        def counted(*args, **kwargs):
            self.calls += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(store, "memory_retrieval_rows", counted)


def _wire_text(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


async def _ask(app, raw_token: str, notebook_id: str, question: str) -> dict:
    async with OfficialMcpClient(app, raw_token, manage_lifespan=False) as client:
        return _payload(await client.call(
            "ask",
            {"question": question, "mode": "reasoning", "notebook_id": notebook_id},
        ))


async def _search(app, raw_token: str, notebook_id: str, query: str) -> dict:
    async with OfficialMcpClient(app, raw_token, manage_lifespan=False) as client:
        return _payload(await client.call(
            "search", {"query": query, "notebook_id": notebook_id}
        ))


def _has_memory_item(answer: dict) -> bool:
    return any(c.get("memory_id") for c in answer["citations"]) or any(
        a.get("object_type") == "memory" for a in answer["anchors"]
    )


async def assert_memory_channel_through_mcp(env: dict, monkeypatch) -> None:
    ids = env["seeded"]
    notebook_id = env["notebook"].id
    model = _ReasoningModel()
    for workload in ("reasoning_agent", "evidence_refine", "ask_answer"):
        bind_chat_client(repository(), workload, model)
    store_calls = _MemoryStoreCalls(monkeypatch)
    app = env["app"]

    async def ask_as(user: str, scopes: list[str]) -> tuple[dict, str]:
        model.prompts.clear()
        answer = await _ask(app, token(env, user, scopes), notebook_id, QUESTION)
        return answer, "\n".join(model.prompts)

    async with app.router.lifespan_context(app):
        # --- Alice WITHOUT memory:read ---------------------------------------
        closed, prompt = await ask_as("alice", NO_MEMORY_SCOPES)
        assert store_calls.calls == 0, "Memory 通道关闭时不得查询 Memory 存储"
        assert not _has_memory_item(closed), "无 memory:read 时引用与锚点都不得出现 Memory 条目"
        for secret in MEMORY_SECRETS:
            assert secret not in prompt, f"{secret} 进了无 memory:read 的检索结果"
            assert secret not in _wire_text(closed)
        # The element and KG arms did run -- over the visible source only.
        assert "VISELEMMARK" in prompt and "VISKGMARK" in prompt
        assert _source_ids(closed) == {"src-visible"}
        # node_context: the mixed object arrives without its Memory occurrence.
        assert "MIXMEMQUOTE" not in prompt and "MIXMEMQUOTE" not in _wire_text(closed)

        # --- Alice WITH memory:read: her Memory is retrieved as before --------
        opened, prompt = await ask_as("alice", FULL_SCOPES)
        assert store_calls.calls > 0
        for secret in MEMORY_SECRETS:
            assert secret in prompt, f"{secret}: 本人有 memory:read 时 Memory 应照旧参与"
        assert any(
            c.get("memory_id") == ids["memory_id"] for c in opened["citations"]
        ), opened["citations"]
        assert "src-memory-alice" in _source_ids(opened)
        # ... and with it the mixed object keeps its Memory occurrence (the
        # control that shows the node_context assertions are not vacuous).
        assert "MIXMEMQUOTE" in _wire_text(opened)

        # --- Bob, full scopes: never Alice's Memory ---------------------------
        calls_before = store_calls.calls
        foreign, prompt = await ask_as("bob", FULL_SCOPES)
        assert store_calls.calls > calls_before  # Bob's channel is open ...
        assert not _has_memory_item(foreign)  # ... and he has no Memory here
        for secret in MEMORY_SECRETS:
            assert secret not in prompt, f"{secret} 泄漏给了另一位成员"
            assert secret not in _wire_text(foreign)
        assert "VISELEMMARK" in prompt and "VISKGMARK" in prompt
        assert _source_ids(foreign) == {"src-visible"}
        assert "MIXMEMQUOTE" not in prompt and "MIXMEMQUOTE" not in _wire_text(foreign)


async def _ask_evidence(app, raw_token: str, notebook_id: str, question: str) -> dict:
    async with OfficialMcpClient(app, raw_token, manage_lifespan=False) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        return _payload(await client.call("ask_notebook", {
            "question": question, "mode": "reasoning", "output": "evidence",
        }))


def _evidence_text(payload: dict) -> str:
    return "\n".join(item["text"] for item in payload["items"])


async def assert_evidence_channel_through_mcp(env: dict, monkeypatch) -> None:
    """``output="evidence"`` rides the SAME Memory channel and ceiling as the
    answer mode: the evidence IS what the synthesis would have read, so the
    assertions mirror ``assert_memory_channel_through_mcp`` over the wire's
    ``items``; and nothing is synthesised or saved."""
    ids = env["seeded"]
    notebook_id = env["notebook"].id
    planner = _ReasoningModel()
    synthesis = _ReasoningModel()
    bind_chat_client(repository(), "reasoning_agent", planner)
    for workload in ("evidence_refine", "ask_answer"):
        bind_chat_client(repository(), workload, synthesis)
    store_calls = _MemoryStoreCalls(monkeypatch)
    app = env["app"]
    conversations_before = len(repository().list_conversations(notebook_id))

    async with app.router.lifespan_context(app):
        closed = await _ask_evidence(
            app, token(env, "alice", NO_MEMORY_SCOPES), notebook_id, QUESTION
        )
        assert closed["status"] == "retrieved" and closed["output"] == "evidence"
        assert store_calls.calls == 0, "Memory 通道关闭时不得查询 Memory 存储"
        assert "memory" not in closed["counts"]["by_kind"]
        assert not any(
            item["kind"] == "memory" or item.get("object_type") == "memory"
            for item in closed["items"]
        )
        for secret in MEMORY_SECRETS + ("MIXMEMQUOTE",):
            assert secret not in _evidence_text(closed)
            assert secret not in _wire_text(closed)
        assert "VISELEMMARK" in _evidence_text(closed) or "VISKGMARK" in _evidence_text(closed)

        opened = await _ask_evidence(
            app, token(env, "alice", FULL_SCOPES), notebook_id, QUESTION
        )
        assert store_calls.calls > 0
        text = _evidence_text(opened)
        for secret in MEMORY_SECRETS:
            assert secret in text, f"{secret}: 本人有 memory:read 时 Memory 应照旧参与"
        assert any(
            item.get("memory_id") == ids["memory_id"] for item in opened["items"]
        ), opened["items"]

        calls_before = store_calls.calls
        foreign = await _ask_evidence(
            app, token(env, "bob", FULL_SCOPES), notebook_id, QUESTION
        )
        assert store_calls.calls > calls_before
        for secret in MEMORY_SECRETS + ("MIXMEMQUOTE",):
            assert secret not in _evidence_text(foreign), f"{secret} 泄漏给了另一位成员"
            assert secret not in _wire_text(foreign)

        # Read while the lifespan is still open: on PostgreSQL leaving it
        # closes the repository's pool.
        assert synthesis.prompts == [], "证据模式不得调用回答/精炼模型"
        assert len(repository().list_conversations(notebook_id)) == conversations_before


def _source_ids(answer: dict) -> set[str]:
    return {
        row["source_id"]
        for row in (*answer["citations"], *answer["anchors"])
        if row.get("source_id")
    }


async def assert_search_channel_through_mcp(env: dict, monkeypatch) -> None:
    notebook_id = env["notebook"].id
    memory_id = env["seeded"]["memory_id"]
    store_calls = _MemoryStoreCalls(monkeypatch)
    app = env["app"]
    async with app.router.lifespan_context(app):
        # ``search`` needs ``read``, and since the tier merge
        # ``read`` also opens the Memory channel, so a token alone can no
        # longer reach the closed branch. It is still live (the second live
        # check can refuse when the token is narrowed between the two), so
        # it is closed here at that check and must then query nothing.
        from app.api.mcp_tools import memory_context

        with monkeypatch.context() as patched:
            patched.setattr(
                memory_context, "_memory_read_allowed", lambda *_args: False
            )
            closed = await _search(
                app, token(env, "alice", ["read"]), notebook_id, TERM
            )
        assert store_calls.calls == 0, "Memory 通道关闭时不得查询 Memory 存储"
        assert not any(item.get("memory_id") for item in closed["items"])
        assert all(item["type"] != "memory" for item in closed["items"])
        assert "MEMITEMSECRET" not in _wire_text(closed)
        assert "MEMELEMSECRET" not in _wire_text(closed)
        assert any(item.get("source_id") == "src-visible" for item in closed["items"])

        opened = await _search(
            app, token(env, "alice", ["read"]),
            notebook_id, TERM,
        )
        assert store_calls.calls > 0
        assert any(item.get("memory_id") == memory_id for item in opened["items"])


@pytest.fixture
def sqlite_env(tmp_path, monkeypatch):
    env = build_mcp_app(f"sqlite:///{tmp_path / 'e13.db'}", tmp_path, monkeypatch)
    env["seeded"] = seed_shared_notebook(env, "?")
    return env


@pytest.mark.anyio
async def test_mcp_ask_memory_channel_and_ceiling_on_sqlite(sqlite_env, monkeypatch):
    await assert_memory_channel_through_mcp(sqlite_env, monkeypatch)


@pytest.mark.anyio
async def test_search_formal_memory_channel_on_sqlite(sqlite_env, monkeypatch):
    await assert_search_channel_through_mcp(sqlite_env, monkeypatch)


@pytest.mark.anyio
async def test_ask_notebook_evidence_memory_channel_and_ceiling_on_sqlite(
    sqlite_env, monkeypatch
):
    await assert_evidence_channel_through_mcp(sqlite_env, monkeypatch)
