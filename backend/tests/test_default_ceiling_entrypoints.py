"""E1-2: every Ask entry runs under the default ceiling -- end to end, real routes.

Alice owns a notebook shared with Bob and mounts a private reference library
of her own on it.  The fixture puts, in the shared notebook: a visible source
(chunk, element and KG object), Alice's own confirmed Memory with its derived
source and KG object, and Bob's confirmed Memory whose derived source carries
an element, a KG object and a relation from the visible object to it; in the
mounted library: a visible source with a KG object, plus a Knowhow projection
and a third member's Memory projection, each with a KG object.

Every entry is driven through its real surface -- the in-process HTTP routes
and the official MCP client -- with only the models stubbed; the stub cites
every evidence key it is shown, so the prompts it received ARE the retrieval
result:

* ① HTTP ``/ask`` with no ``source_scope``, ② ``/ask/stream``, ③ the intent
  precheck ``/ask/intent`` (which runs under the same ceiling, pinned on the
  scope it sees), ④ MCP ``ask_notebook``.  ⑤ the unscoped deep report is
  pinned by ``test_report_api.py::test_report_create_without_any_scope_runs_
  every_phase_under_the_default_ceiling`` and ``test_report_default_ceiling``
  (+ PostgreSQL twin), which drive the same ceiling constructor.
* Alice never gets Bob's Memory-derived content -- not by the element arm, the
  KG arms, the relation or walk channels -- and never the mounted library's
  hidden projections; her own Memory and the mounted library's visible
  sources still take part.
* A mounted library whose source list cannot be read in time is left out and
  the answer says so (``skipped_libraries``, HTTP and MCP).
* A notebook with no mounted library and no other member's Memory: the
  prompts and trace are identical with and without the ceiling installed.

``build_*`` / ``seed_*`` / ``assert_*`` are backend neutral;
``tests/postgres/test_default_ceiling_entrypoints_pg.py`` runs them on
PostgreSQL.  Replacing ``AskService._retrieval_ceiling``'s constructor by a
bare ``source_scope_context(nb, None, None)`` turns ① and ④ red.
"""
from __future__ import annotations

import contextlib
import json
import re

import httpx
import pytest

from app.api.deps import (
    identity_repository,
    notebook_catalog_repository,
    repository,
)
from app.core.request_context import reset_request_user, set_request_user
from app.models.schemas import NotebookCreate
from app.repositories.ports import ChunkWrite, SourceElementWrite
from tests.model_testkit import bind_chat_client
from tests.test_mcp_memory_channel_e2e import (
    FULL_SCOPES,
    NO_MEMORY_SCOPES,
    build_mcp_app,
    token,
)
from tests.test_memory_mcp import OfficialMcpClient, _payload


NOW = "2026-09-30T00:00:00+00:00"
TERM = "quasarlink"
QUESTION = f"What does {TERM} say about latency?"
PASSWORD = "pw"

VISIBLE = ("VISELEMMARK", "VISKGMARK")
OWN_MEMORY = "ASELFKGMARK"
LIBRARY_VISIBLE = "LIBVISKGMARK"
SECRETS = ("BOBELEMSECRET", "BOBKGSECRET", "LIBKHSECRET", "LIBMEMSECRET")


# The reflect turns every reasoning ask walks through before answering, so
# the relation expansion, the element arm, community neighbours and
# derivation chains each really run once (their trace steps are asserted).
REFLECT_TURNS = (
    {"next_action": "expand_graph", "sufficient": False,
     "expand": {"object_id": "ko-visible", "direction": "both"}},
    {"next_action": "search_elements", "sufficient": False,
     "elements_query": f"{TERM} latency element"},
    {"next_action": "expand_community", "sufficient": False,
     "community_focal": f"{TERM} latency visible concept VISKGMARK"},
    {"next_action": "follow_chain", "sufficient": False,
     "follow_chain": {"start_object_id": "ko-visible", "direction": "both"}},
)
WALKED_STEPS = ("expand", "fallback", "expand_community", "follow_chain")  # search_elements records "fallback"


class _Model:
    """Reasoning understanding/planning answered at once; the reflect turns
    walk ``REFLECT_TURNS`` before answering; every other call cites every
    ``k{n}`` key it was shown, and its prompt is recorded."""

    configured = True

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self._turn = 0

    def chat_json(self, messages, schema_hint="", *args, **kwargs):
        schema_hint = schema_hint or ""
        if "normalized_question" in schema_hint:
            self._turn = 0                        # every ask is understood first
            return json.dumps({
                "normalized_question": QUESTION, "intent_type": "explain",
                "result_scope": "ranked", "completeness_required": False,
                "entities": [], "mandatory_topics": [], "ambiguities": [],
                "needs_clarification": False, "confidence": 0.9,
            })
        if "sub_queries" in schema_hint:
            self._turn = 0                        # a new reasoning run
            return json.dumps({"sub_queries": [{"query": f"{TERM} latency"}]})
        if "next_action" in schema_hint:
            text = "\n".join(str(message.get("content") or "") for message in messages)
            self.prompts.append(text)             # reflect prompts carry evidence too
            turn = self._turn
            self._turn += 1
            if turn < len(REFLECT_TURNS):
                return json.dumps(REFLECT_TURNS[turn])
            return json.dumps({"next_action": "answer", "sufficient": True})
        text = "\n".join(str(message.get("content") or "") for message in messages)
        self.prompts.append(text)
        cited = " ".join(f"[{key}]" for key in sorted(set(re.findall(r"\bk\d+\b", text))))
        return json.dumps({"answer": f"{TERM} evidence {cited}", "grounded": True})


def build_app(database_url: str, tmp_path, monkeypatch) -> dict:
    """``build_mcp_app`` (Alice, Bob, the shared notebook, MCP profiles) plus
    the mounted library and a third member, Carol."""
    env = build_mcp_app(database_url, tmp_path, monkeypatch)
    identity = identity_repository()
    env["carol"] = identity.create_user("c00130003", PASSWORD)
    marker = set_request_user(env["alice"])
    try:
        env["library"] = notebook_catalog_repository().create_notebook(
            NotebookCreate(name="Private reference library")
        )
    finally:
        reset_request_user(marker)
    return env


def seed(env: dict, placeholder: str) -> dict:
    repo = repository()
    runtime = repo._runtime
    ph = placeholder
    nb = env["notebook"].id
    lib = env["library"].id
    sources = runtime.source_store

    def source(notebook_id, source_id, source_type, memory_id=""):
        sources.insert_source(
            source_id=source_id, notebook_id=notebook_id, title=f"{TERM} {source_id}",
            source_type=source_type, status="active", parse_status="parsed",
            file_name=f"{source_id}.md", file_path="", file_size=0, file_hash="",
            summary="", doc_type="", memory_id=memory_id,
        )

    def element(source_id, element_id, text):
        with repo._write() as db:
            sources.replace_elements(
                db, source_id,
                [SourceElementWrite(element_id, "paragraph", "p1", text, {})],
                created_at=NOW,
            )

    def kg_object(notebook_id, object_id, source_id, element_id, name):
        evidence = json.dumps([{
            "source_id": source_id, "source_title": source_id,
            "element_id": element_id, "element_type": "paragraph",
            "location_label": "p1", "quoted_span": name, "confidence": 1.0,
        }])
        with repo._write() as db:
            db.execute(
                "INSERT INTO knowledge_objects "
                "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,"
                "created_at,updated_at) VALUES "
                f"({ph},{ph},'concept','approved','',{ph},{ph},{ph},{ph},{ph})",
                (object_id, notebook_id, json.dumps({"name": name, "definition": name}),
                 evidence, source_id, NOW, NOW),
            )
            # What the KG pipeline writes alongside: the reverse source index
            # and, on SQLite, the lexical shadow.
            db.execute(
                "INSERT INTO knowledge_object_sources (object_id,source_id,notebook_id) "
                f"VALUES ({ph},{ph},{ph})",
                (object_id, source_id, notebook_id),
            )
            if ph == "?":
                db.execute(
                    "INSERT INTO kg_objects_fts(object_id,notebook_id,name) VALUES (?,?,?)",
                    (object_id, notebook_id, name),
                )
        repo._embed_objects_batch(notebook_id, [
            {"_oid": object_id, "payload": {"name": name}},
        ])

    def raw_memory(notebook_id, user_id, memory_id):
        with repo._write() as db:
            db.execute(
                "INSERT INTO memory_items(id,notebook_id,created_by,agent_profile_id,"
                "source_answer_id,origin,status,title,content_md,created_at,updated_at) "
                f"VALUES ({ph},{ph},{ph},NULL,NULL,'ask_answer','confirmed',"
                f"{ph},{ph},{ph},{ph})",
                (memory_id, notebook_id, user_id, "memo", "memo body", NOW, NOW),
            )

    runtime.memory_service.kg_ingest_scheduler = lambda fn, item: None
    service = env["service"]
    alice_memory = service.create_memory_candidate(
        nb, env["alice"].id, env["alice_profile"].id, "e12-alice",
        f"{TERM} latency note", f"{TERM} latency own note", [], "test",
    )
    service.confirm_memory(alice_memory.id, env["alice"].id)

    # The shared notebook.
    source(nb, "src-visible", "markdown")
    element("src-visible", "el-visible", f"{TERM} latency visible element VISELEMMARK")
    with repo._write() as db:
        runtime.chunk_store.insert_rows(
            db, nb, "src-visible",
            [ChunkWrite("chunk-visible", f"{TERM} latency visible chunk", "1",
                        ("el-visible",))],
            created_at=NOW,
        )
    kg_object(nb, "ko-visible", "src-visible", "el-visible",
              f"{TERM} latency visible concept VISKGMARK")
    source(nb, "src-memory-alice", "memory", memory_id=alice_memory.id)
    element("src-memory-alice", "el-memory-alice", f"{TERM} latency own element")
    kg_object(nb, "ko-memory-alice", "src-memory-alice", "el-memory-alice",
              f"{TERM} latency own concept ASELFKGMARK")
    raw_memory(nb, env["bob"].id, "mem-bob")
    source(nb, "src-memory-bob", "memory", memory_id="mem-bob")
    element("src-memory-bob", "el-memory-bob",
            f"{TERM} latency bob element BOBELEMSECRET")
    kg_object(nb, "ko-memory-bob", "src-memory-bob", "el-memory-bob",
              f"{TERM} latency bob concept BOBKGSECRET")
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_relations (id,notebook_id,source_id,"
            "source_object_id,target_object_id,edge_type,evidence,created_at) "
            f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
            ("rel-bob", nb, "src-memory-bob", "ko-visible", "ko-memory-bob",
             "kind_of", json.dumps([{
                 "source_id": "src-memory-bob", "element_id": "el-memory-bob",
                 "quoted_span": "BOBKGSECRET relation", "confidence": 1.0,
             }]), NOW),
        )

    # The mounted private library: visible, Knowhow and Carol's Memory.
    source(lib, "src-lib-visible", "markdown")
    element("src-lib-visible", "el-lib-visible", f"{TERM} latency library element")
    kg_object(lib, "ko-lib-visible", "src-lib-visible", "el-lib-visible",
              f"{TERM} latency library concept LIBVISKGMARK")
    source(lib, "src-lib-knowhow", "knowhow")
    element("src-lib-knowhow", "el-lib-knowhow", f"{TERM} latency knowhow row")
    kg_object(lib, "ko-lib-knowhow", "src-lib-knowhow", "el-lib-knowhow",
              f"{TERM} latency knowhow concept LIBKHSECRET")
    raw_memory(lib, env["carol"].id, "mem-carol")
    source(lib, "src-lib-memory", "memory", memory_id="mem-carol")
    element("src-lib-memory", "el-lib-memory", f"{TERM} latency carol element")
    kg_object(lib, "ko-lib-memory", "src-lib-memory", "el-lib-memory",
              f"{TERM} latency carol concept LIBMEMSECRET")
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebook_bases(notebook_id,base_notebook_id,created_at,created_by) "
            f"VALUES ({ph},{ph},{ph},{ph})",
            (nb, lib, NOW, env["alice"].id),
        )
    repo.collection_catalog.invalidate()
    return {"alice_memory": alice_memory.id}


def _bind_model() -> _Model:
    model = _Model()
    for workload in ("reasoning_agent", "evidence_refine", "ask_answer"):
        bind_chat_client(repository(), workload, model)
    return model


def _count_ceiling_reads(monkeypatch) -> list[str]:
    """Every read the Ask service's ceiling readers make, by name."""
    from dataclasses import replace

    service = repository()._runtime.ask_service()
    readers = service.ceiling_readers
    reads: list[str] = []

    def counted(name, read):
        def call(*args, **kwargs):
            reads.append(name)
            return read(*args, **kwargs)
        return call

    monkeypatch.setattr(service, "ceiling_readers", replace(
        readers,
        participants=counted("participants", readers.participants),
        visible=counted("visible", readers.visible),
        hidden=counted("hidden", readers.hidden),
        memory_sources=counted("memory_sources", readers.memory_sources),
    ))
    return reads


async def _login(http: httpx.AsyncClient, username: str) -> dict[str, str]:
    reply = await http.post(
        "/api/auth/login", json={"username": username, "password": PASSWORD},
    )
    assert reply.status_code == 200, reply.text
    return {"Authorization": f"Bearer {reply.json()['token']}"}


async def _intent(http, headers, notebook_id) -> dict:
    reply = await http.post(
        f"/api/notebooks/{notebook_id}/ask/intent",
        json={"question": QUESTION}, headers=headers,
    )
    assert reply.status_code == 200, reply.text
    return reply.json()


def _confirmed(contract: dict) -> dict:
    return {
        "contract": contract,
        "resolved_question": contract.get("resolved_question") or QUESTION,
        "answers": [], "understanding_ms": 1,
    }


async def ask_http(http, headers, notebook_id) -> dict:
    """① ``/ask`` without ``source_scope`` (intent confirmed as the browser
    does after ③)."""
    contract = await _intent(http, headers, notebook_id)
    reply = await http.post(
        f"/api/notebooks/{notebook_id}/ask",
        json={"question": QUESTION, "mode": "reasoning", "intent": _confirmed(contract)},
        headers=headers,
    )
    assert reply.status_code == 200, reply.text
    return reply.json()


async def ask_stream(http, headers, notebook_id) -> dict:
    """② ``/ask/stream``: the final NDJSON frame's response."""
    contract = await _intent(http, headers, notebook_id)
    reply = await http.post(
        f"/api/notebooks/{notebook_id}/ask/stream",
        json={"question": QUESTION, "mode": "reasoning", "intent": _confirmed(contract)},
        headers=headers,
    )
    assert reply.status_code == 200, reply.text
    finals = [
        json.loads(line)["response"] for line in reply.text.splitlines()
        if line.strip() and json.loads(line).get("event") == "final"
    ]
    assert len(finals) == 1, reply.text[:2000]
    return finals[0]


async def ask_mcp(app, raw_token: str, notebook_id: str) -> dict:
    """④ MCP ``ask_notebook``."""
    async with OfficialMcpClient(app, raw_token, manage_lifespan=False) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        return _payload(await client.call(
            "ask_notebook", {"question": QUESTION, "mode": "reasoning"},
        ))


def _assert_alice_sees_only_what_she_may(prompt: str, wire: str, label: str) -> None:
    for marker in VISIBLE:
        assert marker in prompt, f"{label}: the visible source must still take part ({marker})"
    assert OWN_MEMORY in prompt, f"{label}: Alice's own Memory projection must take part"
    assert LIBRARY_VISIBLE in prompt, (
        f"{label}: the mounted library's visible sources must take part"
    )
    for secret in SECRETS:
        assert secret not in prompt, f"{label}: {secret} reached Alice's retrieval"
        assert secret not in wire, f"{label}: {secret} reached Alice's answer"


async def assert_every_entry_runs_under_the_default_ceiling(env: dict, monkeypatch) -> None:
    from app.services import query_intent
    from app.services.source_scope import current_source_scope

    app = env["app"]
    notebook_id = env["notebook"].id
    library_id = env["library"].id
    model = _bind_model()
    understood: list = []
    original_plan = query_intent.plan_query_intent
    reads = _count_ceiling_reads(monkeypatch)

    def recording_plan(*args, **kwargs):
        # ③: the precheck's ceiling is installed but LAZY -- nothing was read
        # to run this model call ...
        understood.append(("reads", len(reads)))
        result = original_plan(*args, **kwargs)
        understood.append(("reads after the model call", len(reads)))
        # ... and anything that does consume the scope in here gets the full
        # default ceiling.
        understood.append(("scope", current_source_scope()))
        return result

    monkeypatch.setattr(query_intent, "plan_query_intent", recording_plan)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://test",
    ) as http:
        headers = await _login(http, env["alice"].username)

        model.prompts.clear()
        reads.clear()
        understood.clear()
        answer = await ask_http(http, headers, notebook_id)
        _assert_alice_sees_only_what_she_may(
            "\n".join(model.prompts), json.dumps(answer, ensure_ascii=False), "① /ask",
        )
        assert "skipped_libraries" not in answer, "a healthy run carries no notice"
        walked = [step["step_type"] for step in answer["reasoning_trace"]]
        for step_type in WALKED_STEPS:
            assert step_type in walked, (step_type, walked)

        # ③ the intent precheck: zero ceiling reads for its model call, and
        # the default ceiling for Alice once the scope is consumed.
        assert understood[0] == ("reads", 0)
        assert understood[1] == ("reads after the model call", 0)
        scope = understood[2][1]
        assert scope is not None and scope.ceilings_total
        assert scope.owner_id == env["alice"].id
        assert "src-memory-bob" not in scope.hidden_source_ids
        assert "src-memory-alice" in scope.hidden_source_ids
        assert scope.source_ceiling_for(library_id) == {"src-lib-visible"}

        model.prompts.clear()
        streamed = await ask_stream(http, headers, notebook_id)
        _assert_alice_sees_only_what_she_may(
            "\n".join(model.prompts), json.dumps(streamed, ensure_ascii=False),
            "② /ask/stream",
        )

        model.prompts.clear()
        mcp = await ask_mcp(app, token(env, "alice", FULL_SCOPES), notebook_id)
        _assert_alice_sees_only_what_she_may(
            "\n".join(model.prompts), json.dumps(mcp, ensure_ascii=False),
            "④ MCP ask_notebook",
        )

        # ④ without memory:read, with Bob's Memory in the notebook and the
        # mounted library's Knowhow/Memory projections: none of Bob's, none of
        # the library's hidden, and not Alice's own Memory either (the channel
        # is closed); the visible source still takes part.
        model.prompts.clear()
        closed = await ask_mcp(app, token(env, "alice", NO_MEMORY_SCOPES), notebook_id)
        prompt = "\n".join(model.prompts)
        wire = json.dumps(closed, ensure_ascii=False)
        assert "VISKGMARK" in prompt, "the visible source still takes part"
        for secret in (*SECRETS, OWN_MEMORY):
            assert secret not in prompt, f"closed channel: {secret} reached the retrieval"
            assert secret not in wire, f"closed channel: {secret} reached the answer"


async def assert_a_skipped_library_is_named_in_the_answer(env: dict, monkeypatch) -> None:
    """The mounted library's visible read fails: it takes no part, and the
    answer -- HTTP and MCP alike -- names it."""
    app = env["app"]
    notebook_id = env["notebook"].id
    library_id = env["library"].id
    model = _bind_model()
    store = repository()._runtime.source_store
    original = store.all_visible_source_ids

    def failing(notebook, *args, **kwargs):
        if notebook == library_id:
            raise RuntimeError("library unavailable")
        return original(notebook, *args, **kwargs)

    monkeypatch.setattr(store, "all_visible_source_ids", failing)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://test",
    ) as http:
        headers = await _login(http, env["alice"].username)
        model.prompts.clear()
        answer = await ask_http(http, headers, notebook_id)
        assert answer["skipped_libraries"] == [{
            "notebook_id": library_id, "name": "Private reference library",
            "included": False,
        }]
        assert LIBRARY_VISIBLE not in "\n".join(model.prompts)

        mcp = await ask_mcp(app, token(env, "alice", FULL_SCOPES), notebook_id)
        assert mcp["skipped_libraries"] == [
            {"notebook_id": library_id, "name": "Private reference library"},
        ]


def _stable(response: dict) -> dict:
    """The answer without the ids and wall clocks that differ run to run."""
    trace = [
        {key: value for key, value in step.items() if key != "duration_ms"}
        for step in response.get("reasoning_trace") or ()
    ]
    return {
        **{key: value for key, value in response.items()
           if key not in {"answer_id", "conversation_id", "answered_at", "asked_at",
                          "reasoning_trace"}},
        "reasoning_trace": trace,
    }


async def assert_a_plain_notebook_is_unchanged(env: dict, monkeypatch) -> None:
    """No mounted library, no other member's Memory: installing the ceiling
    changes neither the prompts nor the answer (push-down: no source list is
    bound, and nothing is filtered)."""
    from app.services.ask_service import AskService

    app = env["app"]
    alice = env["alice"]
    marker = set_request_user(alice)
    try:
        plain = notebook_catalog_repository().create_notebook(
            NotebookCreate(name="Plain notebook")
        )
    finally:
        reset_request_user(marker)
    repo = repository()
    runtime = repo._runtime
    runtime.source_store.insert_source(
        source_id="src-plain", notebook_id=plain.id, title=f"{TERM} plain",
        source_type="markdown", status="active", parse_status="parsed",
        file_name="plain.md", file_path="", file_size=0, file_hash="",
        summary="", doc_type="", memory_id="",
    )
    with repo._write() as db:
        runtime.source_store.replace_elements(
            db, "src-plain",
            [SourceElementWrite("el-plain", "paragraph", "p1",
                                f"{TERM} latency plain element", {})],
            created_at=NOW,
        )
        runtime.chunk_store.insert_rows(
            db, plain.id, "src-plain",
            [ChunkWrite("chunk-plain", f"{TERM} latency plain chunk", "1", ("el-plain",))],
            created_at=NOW,
        )
    model = _bind_model()
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://test",
    ) as http:
        headers = await _login(http, alice.username)
        model.prompts.clear()
        with_ceiling = await ask_http(http, headers, plain.id)
        prompts_with = list(model.prompts)

        @contextlib.contextmanager
        def no_ceiling(self, *args, **kwargs):
            yield

        monkeypatch.setattr(AskService, "_retrieval_ceiling", no_ceiling)
        model.prompts.clear()
        without_ceiling = await ask_http(http, headers, plain.id)
    assert prompts_with and prompts_with == model.prompts
    assert _stable(with_ceiling) == _stable(without_ceiling)


async def assert_a_reader_failure_fails_the_ask(env: dict, monkeypatch) -> None:
    """Spec F1: the active notebook's ceiling read fails -> the ask fails and
    no model is called.  It never runs without a ceiling."""
    app = env["app"]
    notebook_id = env["notebook"].id
    model = _bind_model()
    store = repository()._runtime.source_store
    original = store.all_visible_source_ids

    def failing(notebook, *args, **kwargs):
        if notebook == notebook_id and not kwargs:
            raise RuntimeError("visible read failed")
        return original(notebook, *args, **kwargs)

    monkeypatch.setattr(store, "all_visible_source_ids", failing)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://test",
    ) as http:
        headers = await _login(http, env["alice"].username)
        model.prompts.clear()
        reply = await http.post(
            f"/api/notebooks/{notebook_id}/ask",
            json={"question": QUESTION, "mode": "chunk"}, headers=headers,
        )
    assert reply.status_code >= 500, reply.text
    assert model.prompts == [], "no model call without a ceiling"
    for secret in SECRETS:
        assert secret not in reply.text


def assert_a_stop_during_the_freeze_cancels_the_ask(env: dict, monkeypatch) -> None:
    """Spec R3 / quality V10: a Stop pressed while the ask's ceiling is being
    read ends the ask as cancelled at once -- the read in flight is
    interrupted (it runs under a budget carrying the cancel event), no further
    ceiling read happens and no model is called."""
    import threading
    from dataclasses import replace

    from app.models.ask import AskRequest
    from app.repositories.read_budget import current_read_budget
    from app.services.cancellation import AskCancelled

    notebook_id = env["notebook"].id
    model = _bind_model()
    service = repository()._runtime.ask_service()
    readers = service.ceiling_readers
    cancel = threading.Event()
    later_reads: list[str] = []

    def visible(notebook, *args):
        # The Stop arrives while this read runs; a store checks its budget.
        cancel.set()
        budget = current_read_budget()
        if budget is not None:
            budget.check()
        return readers.visible(notebook, *args)

    def hidden(*args):
        later_reads.append("hidden")
        return readers.hidden(*args)

    monkeypatch.setattr(service, "ceiling_readers", replace(
        readers, visible=visible, hidden=hidden,
    ))
    marker = set_request_user(env["alice"])
    try:
        with pytest.raises(AskCancelled):
            service.ask(
                notebook_id, AskRequest(question=QUESTION, mode="chunk"),
                user_id=env["alice"].id, cancel_event=cancel,
            )
    finally:
        reset_request_user(marker)
    assert later_reads == [], "the interrupted freeze read nothing more"
    assert model.prompts == []


class _ReportModel:
    """Every model call of a 6-section Deep Report, with each prompt recorded
    (the drafting prompts carry the retrieved evidence)."""

    configured = True

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def _record(self, messages) -> None:
        self.prompts.append(
            "\n".join(str(message.get("content") or "") for message in messages)
        )

    def chat_json(self, messages, schema_hint="", *args, **kwargs):
        self._record(messages)
        hint = schema_hint or ""
        if "normalized_question" in hint:
            return json.dumps({
                "normalized_question": QUESTION, "intent_type": "explain",
                "result_scope": "ranked", "completeness_required": False,
                "entities": [], "mandatory_topics": [], "ambiguities": [],
                "needs_clarification": False, "confidence": 0.9,
            })
        if '"sections":[{"title"' in hint:
            return json.dumps({"sections": [
                {"title": f"Section {i}", "scope": f"{TERM} aspect {i}",
                 "sub_queries": [f"{TERM} latency aspect {i}"],
                 "intent_ids": [], "perspectives": [], "tensions": []}
                for i in range(2)]})
        if "verdicts" in hint:
            return json.dumps({"verdicts": [
                {"title": f"Section {i}", "sufficiency": "充足", "gap_note": "",
                 "action": "keep"} for i in range(2)]})
        if '"markdown"' in hint:
            return json.dumps({"markdown": f"{TERM} text [k1]", "grounded": True,
                               "claims": []})
        if "high_level_keywords" in hint:
            return json.dumps({"query": QUESTION, "sub_queries": [{"query": QUESTION}]})
        if '"summary"' in hint:
            return json.dumps({"summary": "s", "coverage": [], "contradictions": []})
        if "sub_queries" in hint:
            return json.dumps({"sub_queries": [{"query": f"{TERM} latency"}]})
        if "next_action" in hint:
            return json.dumps({"next_action": "answer", "sufficient": True})
        if '"answer"' in hint:
            return json.dumps({"answer": f"{TERM} [k1]", "grounded": True})
        return "{}"

    def chat(self, messages, *args, **kwargs):
        self._record(messages)
        return f"{TERM} text"


def _run_unscoped_report(notebook_id: str, user, monkeypatch) -> tuple[dict, list[str]]:
    """Intent, plan and generate of a report created without any scope, each
    through the production coordinator (its default ceiling) and the real
    engine; only the models are stubbed.  Returns the report and prompts."""
    from app.models.schemas import AskRequest  # noqa: F401 - keeps imports honest
    from app.services.model_registry import WORKLOADS

    repo = repository()
    model = _ReportModel()
    for workload, spec in WORKLOADS.items():
        if spec.kind == "chat":
            bind_chat_client(repo, workload, model)
    coordinator = repo.report_execution
    monkeypatch.setattr(
        coordinator, "job_submitter",
        lambda fn, *args, name=None, notify_pending=False, **kwargs: fn(),
    )
    # Post-completion observers (profile consolidation...) run model calls of
    # their own in the background; they are not this report's retrieval.
    monkeypatch.setattr(coordinator, "after_completed", None)
    marker = set_request_user(user)
    try:
        report_id = repo.create_report(notebook_id, QUESTION, depth=2)
        coordinator.start_plan(notebook_id, report_id, QUESTION, "", False, user_id=user.id)
        row = repo.get_report(notebook_id, report_id)
        assert row["status"] == "intent_ready", (row.get("status"), row.get("error"))
        understanding = dict(row.get("understanding") or {})
        understanding["resolved_question"] = QUESTION
        assert repo.claim_report_intent(notebook_id, report_id, understanding)
        coordinator.start_plan(notebook_id, report_id, QUESTION, "", False,
                               user_id=user.id, intent_contract=understanding)
        row = repo.get_report(notebook_id, report_id)
        assert row["status"] == "outline_ready", (row.get("status"), row.get("error"))
        assert repo.claim_report_generation(notebook_id, report_id, None)
        coordinator.start_generate(notebook_id, report_id, QUESTION, 2, user_id=user.id)
        row = repo.get_report(notebook_id, report_id)
    finally:
        reset_request_user(marker)
    return row, model.prompts


def assert_an_unscoped_report_retrieves_only_what_its_creator_may(env, monkeypatch) -> None:
    """⑤ end to end: the report's retrieval -- every drafting prompt and the
    finished report -- carries none of Bob's Memory-derived content nor the
    mounted library's hidden projections; the visible source takes part."""
    row, prompts = _run_unscoped_report(env["notebook"].id, env["alice"], monkeypatch)
    assert row["status"] == "done", (row.get("status"), row.get("error"))
    text = "\n".join(prompts)
    assert any(marker in text for marker in VISIBLE), "the visible source takes part"
    wire = json.dumps(row, ensure_ascii=False, default=str)
    for secret in SECRETS:
        assert secret not in text, f"⑤ report: {secret} reached the report's retrieval"
        assert secret not in wire, f"⑤ report: {secret} reached the report"


def assert_a_plain_notebook_report_is_unchanged(env, monkeypatch) -> None:
    """No mounted library, no other member's Memory: a report's prompts are
    the same with and without the default ceiling."""
    from contextlib import ExitStack

    from app.services.report_execution import ReportExecutionCoordinator

    alice = env["alice"]
    marker = set_request_user(alice)
    try:
        plain = notebook_catalog_repository().create_notebook(
            NotebookCreate(name="Plain report notebook")
        )
    finally:
        reset_request_user(marker)
    runtime = repository()._runtime
    runtime.source_store.insert_source(
        source_id="src-plain-report", notebook_id=plain.id, title=f"{TERM} plain",
        source_type="markdown", status="active", parse_status="parsed",
        file_name="plain.md", file_path="", file_size=0, file_hash="",
        summary="", doc_type="", memory_id="",
    )
    with repository()._write() as db:
        runtime.source_store.replace_elements(
            db, "src-plain-report",
            [SourceElementWrite("el-plain-report", "paragraph", "p1",
                                f"{TERM} latency plain element", {})],
            created_at=NOW,
        )
        runtime.chunk_store.insert_rows(
            db, plain.id, "src-plain-report",
            [ChunkWrite("chunk-plain-report", f"{TERM} latency plain chunk", "1",
                        ("el-plain-report",))],
            created_at=NOW,
        )
    _row, with_ceiling = _run_unscoped_report(plain.id, alice, monkeypatch)
    monkeypatch.setattr(
        ReportExecutionCoordinator, "_default_ceiling",
        lambda self, *args, **kwargs: ExitStack(),
    )
    _row, without_ceiling = _run_unscoped_report(plain.id, alice, monkeypatch)
    assert with_ceiling and with_ceiling == without_ceiling


@pytest.fixture
def sqlite_env(tmp_path, monkeypatch):
    env = build_app(f"sqlite:///{tmp_path / 'e12.db'}", tmp_path, monkeypatch)
    env["seeded"] = seed(env, "?")
    return env


@pytest.mark.anyio
async def test_every_ask_entry_runs_under_the_default_ceiling_on_sqlite(
    sqlite_env, monkeypatch,
):
    await assert_every_entry_runs_under_the_default_ceiling(sqlite_env, monkeypatch)


@pytest.mark.anyio
async def test_a_skipped_mounted_library_is_named_in_the_answer_on_sqlite(
    sqlite_env, monkeypatch,
):
    await assert_a_skipped_library_is_named_in_the_answer(sqlite_env, monkeypatch)


@pytest.mark.anyio
async def test_a_plain_notebook_answers_the_same_with_the_ceiling_on_sqlite(
    sqlite_env, monkeypatch,
):
    await assert_a_plain_notebook_is_unchanged(sqlite_env, monkeypatch)


def test_an_unwired_ask_service_refuses_instead_of_running_unscoped():
    """No readers, no ask: the service never falls back to an unscoped run."""
    from app.models.ask import AskRequest
    from app.services.ask_service import AskService

    service = AskService.__new__(AskService)
    service.event_log = None
    service.ask_chunk = lambda *args, **kwargs: pytest.fail("ran without a ceiling")
    with pytest.raises(RuntimeError, match="ceiling"):
        service.ask("nb", AskRequest(question="q"), user_id="u")


@pytest.mark.anyio
async def test_a_reader_failure_fails_the_ask_on_sqlite(sqlite_env, monkeypatch):
    await assert_a_reader_failure_fails_the_ask(sqlite_env, monkeypatch)


def test_a_stop_during_the_freeze_cancels_the_ask_on_sqlite(sqlite_env, monkeypatch):
    assert_a_stop_during_the_freeze_cancels_the_ask(sqlite_env, monkeypatch)


def test_an_unscoped_report_retrieves_only_what_its_creator_may_on_sqlite(
    sqlite_env, monkeypatch,
):
    assert_an_unscoped_report_retrieves_only_what_its_creator_may(sqlite_env, monkeypatch)


def test_a_plain_notebook_report_is_unchanged_on_sqlite(sqlite_env, monkeypatch):
    assert_a_plain_notebook_report_is_unchanged(sqlite_env, monkeypatch)
