"""E6-3 (M3, N-6) end to end: a private library mounted on a shared notebook
counts only for the viewers who may read it.

Alice owns the notebook ``a``, shared with Bob, and mounts her own PRIVATE
library ``b`` on it.  ``b`` holds a source whose element and chunk carry an
exact identifier, two knowledge-graph objects joined by a relation, its
concept clusters, and an image asset.  Every one of ``b``'s strings carries the
marker ``BLIB``.

Through the real application (``create_app()``: HTTP routes, the official MCP
client, the real Ask / report / Global Ask services and stores; only the
models are stubbed, and the stub cites every evidence key it is shown, so the
prompts it received ARE the retrieval result):

* Bob asks (reasoning, walking graph expansion, element search, community
  peers, a chain and a chunk search; and chunk mode): ``b`` contributes
  NOTHING to any prompt or to the answer.  Alice asks the same and ``b`` is
  there -- the positive control that the seeding is reachable on those
  channels.
* Bob reads ``b``'s source through the source proxy
  (``/notebooks/a/sources/{id}``, its elements and element page), ``b``'s
  asset through the asset proxy, and ``b``'s element through MCP
  ``get_cited_element``: 404 / error every time.  Alice: served.
* Bob's notebook summary names no ``b`` and flags no ``b`` knowledge graph
  (N-6); Alice's does.
* Once Bob is given read access to ``b``, all of the above flips for him.
* The graph caches: Bob's federated / PPR graph is not Alice's (a different
  key, built separately, without ``b``); Carol, another member who may not
  read ``b`` either, shares Bob's entry; once Bob may read ``b`` he shares
  Alice's (cache hits counted).
* Background runs take the viewer from the run's actor, never from the
  thread's ambient request: a report, a Global Ask job and a detached
  (reattachable) ask run for Bob read nothing of ``b`` even when the ambient
  request user is Alice, and run for Alice with the ambient user Bob read it.

``build_world`` / ``assert_*`` are backend neutral;
``tests/postgres/test_mount_viewer_e2e_pg.py`` runs them on PostgreSQL.
"""
from __future__ import annotations

import json
import re
import threading
import time

import httpx
import pytest

from app.api.deps import identity_repository, notebook_sharing_repository, repository
from app.core.request_context import reset_request_user, set_request_user
from app.repositories.ports import ChunkWrite, SourceElementWrite
from tests.model_testkit import bind_chat_client
from tests.test_default_ceiling_entrypoints import _ReportModel, build_app
from tests.test_mcp_memory_channel_e2e import FULL_SCOPES, token
from tests.test_memory_mcp import OfficialMcpClient, _payload


NOW = "2026-10-08T00:00:00+00:00"
TERM = "quasarlink"
EXACT = "QZX-4471B"
QUESTION = f"What does {TERM} say about latency and {EXACT}?"
PASSWORD = "pw"
MARK = "BLIB"
LIB_NAME = "Private reference library"
PNG = b"\x89PNG\r\n\x1a\n" + b"mount-viewer-asset" * 4

OWN = {
    "source": "src-a", "element": "el-a", "chunk": "chunk-a",
    "object": "ko-a", "text": f"{TERM} latency shared notebook passage VISMARK",
}
LIB = {
    "source": "src-b", "element": "el-b", "chunk": "chunk-b",
    "object": "ko-b1", "object2": "ko-b2", "relation": "rel-b",
}


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------

def build_world(database_url: str, tmp_path, monkeypatch, placeholder: str) -> dict:
    """``build_app`` (Alice, Bob, Carol, the shared notebook ``a`` with Bob a
    member, Alice's private library ``b``) plus Carol as a second member of
    ``a``, the seeding below, and ``b`` mounted on ``a`` by Alice."""
    from app.api.deps import notebook_catalog_repository
    from app.models.schemas import NotebookCreate

    env = build_app(database_url, tmp_path, monkeypatch)
    sharing = notebook_sharing_repository()
    sharing.add_member(env["notebook"].id, env["carol"].id)
    # ``e``: a shared notebook of Alice's with nothing of its own but ``b``
    # mounted -- whether one may ask in it at all (``ask_available``) hangs on
    # ``b``'s knowledge graph alone.
    marker = set_request_user(env["alice"])
    try:
        env["empty"] = notebook_catalog_repository().create_notebook(
            NotebookCreate(name="Mount-only notebook")
        )
    finally:
        reset_request_user(marker)
    sharing.add_member(env["empty"].id, env["bob"].id)
    env["carol_profile"] = env["service"].create_agent_profile(
        env["carol"].id, "Carol agent", "",
    )
    _seed(env, placeholder)
    return env


def _seed(env: dict, ph: str) -> None:
    repo = repository()
    runtime = repo._runtime
    sources = runtime.source_store
    a, b = env["notebook"].id, env["library"].id

    def source(notebook_id, source_id):
        sources.insert_source(
            source_id=source_id, notebook_id=notebook_id,
            title=f"{TERM} {source_id}", source_type="markdown",
            status="active", parse_status="parsed", file_name=f"{source_id}.md",
            file_path="", file_size=0, file_hash="", summary="", doc_type="",
            memory_id="",
        )

    def passage(notebook_id, source_id, element_id, chunk_id, text):
        with repo._write() as db:
            sources.replace_elements(
                db, source_id,
                [SourceElementWrite(element_id, "paragraph", "p1", text, {})],
                created_at=NOW,
            )
            runtime.chunk_store.insert_rows(
                db, notebook_id, source_id,
                [ChunkWrite(chunk_id, text, "1", (element_id,))],
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
                (object_id, notebook_id,
                 json.dumps({"name": name, "definition": name}),
                 evidence, source_id, NOW, NOW),
            )
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

    # The shared notebook: one source, one passage, one concept.
    source(a, OWN["source"])
    passage(a, OWN["source"], OWN["element"], OWN["chunk"], OWN["text"])
    kg_object(a, OWN["object"], OWN["source"], OWN["element"],
              f"{TERM} latency shared concept VISKGMARK")

    # Alice's private library b.
    lib_text = f"{TERM} latency library passage {MARK}CHUNK {EXACT}"
    source(b, LIB["source"])
    passage(b, LIB["source"], LIB["element"], LIB["chunk"], lib_text)
    kg_object(b, LIB["object"], LIB["source"], LIB["element"],
              f"{TERM} latency library concept {MARK}KG")
    kg_object(b, LIB["object2"], LIB["source"], LIB["element"],
              f"{TERM} latency library mechanism {MARK}KGTWO")
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_relations (id,notebook_id,source_id,"
            "source_object_id,target_object_id,edge_type,evidence,created_at) "
            f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
            (LIB["relation"], b, LIB["source"], LIB["object"], LIB["object2"],
             "derived_from", json.dumps([{
                 "source_id": LIB["source"], "element_id": LIB["element"],
                 "quoted_span": f"{TERM} {MARK}RELQUOTE", "confidence": 1.0,
             }]), NOW),
        )
    for notebook_id in (a, b):
        repo.backfill_chunk_fts(notebook_id)
        repo.rebuild_unified_kg(notebook_id)
    from app.services.knowhow.assets import AssetService

    env["asset_id"] = AssetService(repo).save_source_image(
        b, LIB["source"], "b.png", "image/png", PNG, env["alice"].id,
    )["id"]
    with repo._write() as db:
        for mounting in (a, env["empty"].id):
            db.execute(
                "INSERT INTO notebook_bases(notebook_id,base_notebook_id,created_at,"
                f"created_by) VALUES ({ph},{ph},{ph},{ph})",
                (mounting, b, NOW, env["alice"].id),
            )
    repo.collection_catalog.invalidate()


def grant_bob_the_library(env: dict) -> None:
    notebook_sharing_repository().add_member(env["library"].id, env["bob"].id)


# ---------------------------------------------------------------------------
# The models
# ---------------------------------------------------------------------------

REFLECT_TURNS = (
    {"next_action": "expand_graph", "sufficient": False,
     "expand": {"object_id": LIB["object"], "direction": "both"}},
    {"next_action": "search_elements", "sufficient": False,
     "elements_query": f"{TERM} latency {EXACT}"},
    {"next_action": "expand_community", "sufficient": False,
     "community_focal": f"{TERM} latency library concept {MARK}KG"},
    {"next_action": "follow_chain", "sufficient": False,
     "follow_chain": {"start_object_id": LIB["object"], "direction": "both"}},
    {"next_action": "search_chunks", "sufficient": False,
     "chunks_query": f"{TERM} latency", "chunks_keywords": f"{TERM} {EXACT}"},
)


class _Model:
    """Understanding and planning answered at once; the reflect turns walk
    ``REFLECT_TURNS``; every other call cites every ``k{n}`` key it was shown.
    Every prompt that carries evidence is recorded."""

    configured = True
    model = "fake"

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self._turn = 0
        self._lock = threading.Lock()

    def _record(self, messages) -> str:
        text = "\n".join(str(message.get("content") or "") for message in messages)
        with self._lock:
            self.prompts.append(text)
        return text

    def chat_json(self, messages, schema_hint="", *args, **kwargs):
        hint = schema_hint or ""
        if "normalized_question" in hint:
            self._turn = 0
            return json.dumps({
                "normalized_question": QUESTION, "intent_type": "explain",
                "result_scope": "ranked", "completeness_required": False,
                "entities": [], "mandatory_topics": [], "ambiguities": [],
                "needs_clarification": False, "confidence": 0.9,
            })
        if "sub_queries" in hint:
            self._turn = 0
            return json.dumps({"sub_queries": [{"query": f"{TERM} latency {EXACT}"}]})
        if "next_action" in hint:
            self._record(messages)
            turn = self._turn
            self._turn += 1
            if turn < len(REFLECT_TURNS):
                return json.dumps(REFLECT_TURNS[turn])
            return json.dumps({"next_action": "answer", "sufficient": True})
        if '"operation"' in hint:
            return json.dumps({"operation": "profile"})
        text = self._record(messages)
        cited = " ".join(
            f"[{key}]" for key in sorted(set(re.findall(r"\bk\d+\b", text)))
        )
        return json.dumps({
            "answer": f"{TERM} evidence {cited}", "grounded": True,
            "conclusion": TERM, "anchors": [], "summary": TERM, "sections": [],
        })

    def chat(self, messages, *args, **kwargs):
        self._record(messages)
        return f"{TERM} text"

    def text(self) -> str:
        with self._lock:
            return "\n".join(self.prompts)


def bind_model() -> _Model:
    from app.services.model_registry import WORKLOADS

    model = _Model()
    repo = repository()
    for workload, spec in WORKLOADS.items():
        if spec.kind == "chat":
            bind_chat_client(repo, workload, model)
    return model


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

async def _login(http, username: str) -> dict[str, str]:
    reply = await http.post(
        "/api/auth/login", json={"username": username, "password": PASSWORD},
    )
    assert reply.status_code == 200, reply.text
    return {"Authorization": f"Bearer {reply.json()['token']}"}


async def _ask(http, headers, notebook_id, mode) -> dict:
    payload: dict = {"question": QUESTION, "mode": mode}
    if mode == "reasoning":
        reply = await http.post(
            f"/api/notebooks/{notebook_id}/ask/intent",
            json={"question": QUESTION}, headers=headers,
        )
        assert reply.status_code == 200, reply.text
        contract = reply.json()
        payload["intent"] = {
            "contract": contract,
            "resolved_question": contract.get("resolved_question") or QUESTION,
            "answers": [], "understanding_ms": 1,
        }
    reply = await http.post(
        f"/api/notebooks/{notebook_id}/ask", json=payload, headers=headers,
    )
    assert reply.status_code == 200, reply.text
    return reply.json()


async def _asks(http, headers, env, model, *, reads_b: bool) -> tuple[str, str]:
    """Reasoning + chunk asks; ``(every prompt, every answer on the wire)``.

    The reasoning run really walks its channels: element search, community
    peers and a chunk search always; graph expansion and the chain from
    ``b``'s object only for a viewer to whom ``b`` exists."""
    model.prompts.clear()
    answers = []
    for mode in ("reasoning", "chunk"):
        answers.append(await _ask(http, headers, env["notebook"].id, mode))
    walked = {step["step_type"] for step in answers[0]["reasoning_trace"]}
    expected = {"expand_community", "search_chunks", "exact_lookup"}
    if reads_b:
        expected |= {"expand", "follow_chain"}
    assert expected <= walked, (expected - walked, walked)
    # A ``reflect`` step echoes the scripted model's own decision, and the
    # community step its chosen focal name -- the stub talking, not retrieval
    # (the peers it FOUND stay in the dump).
    focal = REFLECT_TURNS[2]["community_focal"]
    for answer in answers:
        trace = []
        for step in answer.get("reasoning_trace") or ():
            if step.get("step_type") == "reflect":
                continue
            detail = step.get("detail")
            if isinstance(detail, dict):
                step["detail"] = {
                    key: value for key, value in detail.items() if value != focal
                }
            trace.append(step)
        answer["reasoning_trace"] = trace
    return model.text(), json.dumps(answers, ensure_ascii=False)


async def _cited_element(app, raw_token, notebook_id):
    async with OfficialMcpClient(app, raw_token, manage_lifespan=False) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        return await client.call("get_cited_element", {
            "source_id": LIB["source"], "element_id": LIB["element"],
        })


async def _reads(http, headers, env) -> dict:
    """Every read surface of b through a, as ``{surface: status or flag}``."""
    a, b = env["notebook"].id, env["library"].id
    summary = await http.get(f"/api/notebooks/{a}", headers=headers)
    assert summary.status_code == 200, summary.text
    body = summary.json()
    empty = await http.get(f"/api/notebooks/{env['empty'].id}", headers=headers)
    assert empty.status_code == 200, empty.text
    base = f"/api/notebooks/{a}/sources/{LIB['source']}"
    return {
        "source": (await http.get(base, headers=headers)).status_code,
        "elements": (await http.get(f"{base}/elements", headers=headers)).status_code,
        "elements_page": (
            await http.get(f"{base}/elements-page", headers=headers)
        ).status_code,
        "asset": (
            await http.get(f"/api/notebooks/{a}/assets/{env['asset_id']}", headers=headers)
        ).status_code,
        "summary_names_b": (
            any(ref["id"] == b for ref in body["base_notebooks"])
            or LIB_NAME in summary.text
        ),
        "summary_flags_b_kg": b in (body.get("base_kg_notebook_ids") or []),
        "summary_base_kg_available": bool(body.get("base_kg_available")),
        "mount_only_ask_available": bool(empty.json().get("ask_available")),
    }


_HIDDEN = {
    "source": 404, "elements": 404, "elements_page": 404, "asset": 404,
    "summary_names_b": False, "summary_flags_b_kg": False,
    "summary_base_kg_available": False, "mount_only_ask_available": False,
}
_SHOWN = {
    "source": 200, "elements": 200, "elements_page": 200, "asset": 200,
    "summary_names_b": True, "summary_flags_b_kg": True,
    "summary_base_kg_available": True, "mount_only_ask_available": True,
}


# ---------------------------------------------------------------------------
# Assertions (backend neutral)
# ---------------------------------------------------------------------------

async def assert_the_mount_counts_only_for_its_readers(env: dict) -> None:
    app = env["app"]
    a, b = env["notebook"].id, env["library"].id
    model = bind_model()
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://test",
    ) as http:
        alice = await _login(http, env["alice"].username)
        bob = await _login(http, env["bob"].username)

        # Bob may not read b: every channel, every read surface, nothing.
        prompts, wire = await _asks(http, bob, env, model, reads_b=False)
        assert "VISKGMARK" in prompts or "VISMARK" in prompts, (
            "Bob's own notebook still answers"
        )
        assert MARK not in prompts, "b reached Bob's retrieval"
        for leaked in (MARK, b):
            at = wire.find(leaked)
            assert at < 0, ("b reached Bob's answer", wire[max(0, at - 300):at + 100])
        assert await _reads(http, bob, env) == _HIDDEN
        refused = await _cited_element(app, token(env, "bob", FULL_SCOPES), a)
        assert refused.isError
        assert MARK not in json.dumps(
            [item.text for item in refused.content], ensure_ascii=False,
        )

        # Alice, the mounter: unchanged.
        prompts, wire = await _asks(http, alice, env, model, reads_b=True)
        for marker in (f"{MARK}CHUNK", f"{MARK}KG"):
            assert marker in prompts, f"Alice's retrieval lost {marker}"
        assert await _reads(http, alice, env) == _SHOWN
        served = await _cited_element(app, token(env, "alice", FULL_SCOPES), a)
        assert MARK in json.dumps(_payload(served), ensure_ascii=False)

        # Bob is given read access to b: the mount now counts for him too.
        grant_bob_the_library(env)
        prompts, wire = await _asks(http, bob, env, model, reads_b=True)
        for marker in (f"{MARK}CHUNK", f"{MARK}KG"):
            assert marker in prompts, f"Bob, now a reader of b, lost {marker}"
        assert await _reads(http, bob, env) == _SHOWN
        served = await _cited_element(app, token(env, "bob", FULL_SCOPES), a)
        assert MARK in json.dumps(_payload(served), ensure_ascii=False)


class _CacheTally:
    """Counts graph builds per cache key through the shared vector cache."""

    def __init__(self, cache, monkeypatch) -> None:
        self.builds: list[str] = []
        self.calls: list[str] = []
        real = cache.get

        def get(key, version, load):
            self.calls.append(key)

            def counted():
                self.builds.append(key)
                return load()

            return real(key, version, counted)

        monkeypatch.setattr(cache, "get", get)


def assert_graph_caches_follow_the_effective_set(env: dict, monkeypatch) -> None:
    """Fed / PPR graph caches: never shared across different effective sets,
    shared across equal ones (cache hits counted)."""
    from app.services.retrieval_run import retrieval_run

    graph = repository().retrieval.graph
    a = env["notebook"].id
    lib_objects = {LIB["object"], LIB["object2"]}
    tally = _CacheTally(graph._vector_cache, monkeypatch)

    def nodes(user, bystander, family) -> set:
        """The graph's object ids, built in a run whose actor is ``user``
        while the ambient request user is ``bystander``."""
        marker = set_request_user(bystander)
        try:
            with retrieval_run(run_kind="ask_reasoning", actor_id=user.id):
                if family == "fed_rxgraph":
                    return set(graph._federated_rx_graph(a)[2])
                return set(graph._ppr_graph(a)[1])
        finally:
            reset_request_user(marker)

    for family in ("fed_rxgraph", "ppr_graph"):
        tally.builds.clear()
        tally.calls.clear()
        alice_nodes = nodes(env["alice"], env["bob"], family)
        bob_nodes = nodes(env["bob"], env["alice"], family)
        carol_nodes = nodes(env["carol"], env["alice"], family)
        built = [key for key in tally.builds if key.endswith(f":{family}")]
        assert len(built) == 2 and built[0] != built[1], (family, tally.builds)
        alice_key, bob_key = built
        assert alice_key == f"{a}:{family}", "the mounter keeps the historical key"
        assert lib_objects <= alice_nodes, f"{family}: Alice's graph spans b"
        assert not lib_objects & bob_nodes, f"{family}: Bob's graph holds none of b"
        # Carol may not read b either: the same effective set, served Bob's
        # entry (a hit, not a build).
        assert carol_nodes == bob_nodes
        assert tally.calls.count(bob_key) == 2, (family, tally.calls)
    grant_bob_the_library(env)
    for family in ("fed_rxgraph", "ppr_graph"):
        tally.builds.clear()
        tally.calls.clear()
        assert lib_objects <= nodes(env["bob"], env["alice"], family)
        own = [key for key in tally.calls if key.endswith(f":{family}")]
        built = [key for key in tally.builds if key.endswith(f":{family}")]
        assert own == [f"{a}:{family}"] and built == [], (
            f"{family}: Bob, now a reader of b, is served Alice's entry",
            tally.calls, tally.builds,
        )


# ---------------------------------------------------------------------------
# Background runs: the run's actor, not the ambient request
# ---------------------------------------------------------------------------

def _report_as(env, actor, bystander, monkeypatch) -> tuple[dict, str]:
    """Intent, plan and generate of an unscoped report for ``actor`` through
    the production coordinator, the worker steps run while the ambient request
    user is ``bystander``."""
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
    monkeypatch.setattr(coordinator, "after_completed", None)
    a = env["notebook"].id
    marker = set_request_user(actor)
    try:
        report_id = repo.create_report(a, QUESTION, depth=2)
    finally:
        reset_request_user(marker)
    marker = set_request_user(bystander)
    try:
        coordinator.start_plan(a, report_id, QUESTION, "", False, user_id=actor.id)
        row = repo.get_report(a, report_id)
        assert row["status"] == "intent_ready", (row.get("status"), row.get("error"))
        understanding = dict(row.get("understanding") or {})
        understanding["resolved_question"] = QUESTION
        assert repo.claim_report_intent(a, report_id, understanding)
        coordinator.start_plan(a, report_id, QUESTION, "", False,
                               user_id=actor.id, intent_contract=understanding)
        row = repo.get_report(a, report_id)
        assert row["status"] == "outline_ready", (row.get("status"), row.get("error"))
        assert repo.claim_report_generation(a, report_id, None)
        coordinator.start_generate(a, report_id, QUESTION, 2, user_id=actor.id)
        row = repo.get_report(a, report_id)
    finally:
        reset_request_user(marker)
    assert row["status"] == "done", (row.get("status"), row.get("error"))
    return row, "\n".join(model.prompts)


def assert_a_report_reads_the_mount_by_its_author(env, monkeypatch) -> None:
    row, prompts = _report_as(env, env["bob"], env["alice"], monkeypatch)
    assert MARK not in prompts, "b reached Bob's report retrieval"
    assert MARK not in json.dumps(row, ensure_ascii=False, default=str)
    _row, prompts = _report_as(env, env["alice"], env["bob"], monkeypatch)
    assert MARK in prompts, "Alice's report must still read her own mount"


def _global_as(env, actor, bystander, notebook_ids) -> tuple[object, str]:
    from app.models.global_ask import GlobalAskRequest

    repo = repository()
    model = bind_model()
    service = repo._runtime.global_ask_service()
    marker = set_request_user(bystander)
    try:
        job = service.start(
            GlobalAskRequest(
                question=QUESTION, mode="reasoning",
                notebook_scope={"mode": "include", "notebook_ids": list(notebook_ids)},
            ),
            user_id=actor.id,
        )
    finally:
        reset_request_user(marker)
    deadline = time.monotonic() + 60
    while job.job_id in service._events and time.monotonic() < deadline:
        threading.Event().wait(0.02)
    result = service.get_job(job.job_id, user_id=actor.id)
    assert result.status == "done", result.error
    return result, model.text()


def assert_a_global_ask_reads_by_its_actor(env) -> None:
    a, b = env["notebook"].id, env["library"].id
    result, prompts = _global_as(env, env["bob"], env["alice"], [a])
    assert MARK not in prompts, "b reached Bob's Global Ask"
    assert b not in json.dumps(
        result.answer.model_dump(mode="json"), ensure_ascii=False,
    )
    _result, prompts = _global_as(env, env["alice"], env["bob"], [a, b])
    assert MARK in prompts, "Alice's Global Ask over b must read it"


def _detached_as(env, actor, bystander) -> tuple[dict, str]:
    """A detached (reattachable) ask -- the worker the stream route starts and
    a returning client re-attaches to -- for ``actor``, started while the
    ambient request user is ``bystander``; drained to its final frame."""
    from app.models.schemas import AskRequest
    from app.services.ask_modes import resolve_mode

    repo = repository()
    model = bind_model()
    payload = AskRequest(question=QUESTION, mode="chunk",
                         client_request_id=f"reattach-{actor.id}")
    marker = set_request_user(bystander)
    try:
        events = repo.start_ask_stream(
            env["notebook"].id, payload, resolve_mode("chunk", ()),
            user_id=actor.id,
        )
    finally:
        reset_request_user(marker)
    final = None
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        event = events.get(timeout=60)
        if event is None:
            break
        if event.get("event") == "final":
            final = event.get("response")
    assert final is not None, "the detached run delivered its answer"
    return final, model.text()


def assert_a_detached_ask_reads_by_its_actor(env) -> None:
    final, prompts = _detached_as(env, env["bob"], env["alice"])
    assert MARK not in prompts, "b reached Bob's detached run"
    assert MARK not in json.dumps(final, ensure_ascii=False, default=str)
    _final, prompts = _detached_as(env, env["alice"], env["bob"])
    assert MARK in prompts, "Alice's detached run must read her own mount"


def assert_no_actor_no_request_reads_only_public_libraries(env, monkeypatch) -> None:
    """A path with no run actor and no request user (a worker thread, a CLI,
    the startup warm-up) resolves no viewer -- not the seeded local account
    ``identity.current_user()`` would fall back to: only public libraries and
    ``everyone`` grants, so ``b`` is gone even from its mounter's notebook.
    The notebook summary such a path reads names no ``b`` either."""
    from app.services.retrieval_run import current_viewer_id

    # The seeded local account may read b: a fallback to it (what
    # ``identity.current_user()`` answers off a request) would show b.
    notebook_sharing_repository().add_member(env["library"].id, "user-local")
    store = repository()._runtime.notebook_store
    seen = []

    def worker():
        seen.append(current_viewer_id())
        seen.append(store.participant_notebook_ids(
            env["notebook"].id, viewer_id=current_viewer_id(),
        ))
        summary = repository().get_notebook(env["notebook"].id)
        seen.append([ref.id for ref in summary.base_notebooks])
        seen.append(list(summary.base_kg_notebook_ids))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert seen == ["", [env["notebook"].id], [], []]


def assert_a_join_summary_is_the_joiners(env) -> None:
    """The summary the join hands back is the joiner's own (N-6), equal to
    the one their next GET reads: Carol, who may read ``b``, joins ``e``."""
    sharing = notebook_sharing_repository()
    sharing.add_member(env["library"].id, env["carol"].id)
    joined = sharing.join_shared(env["empty"].id, env["carol"].id)
    marker = set_request_user(env["carol"])
    try:
        fetched = repository().get_notebook(env["empty"].id)
    finally:
        reset_request_user(marker)
    assert [ref.id for ref in joined.base_notebooks] == [env["library"].id]
    assert joined.base_notebooks == fetched.base_notebooks
    assert joined.base_kg_notebook_ids == fetched.base_kg_notebook_ids
    assert joined.ask_available == fetched.ask_available


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------

@pytest.fixture
def sqlite_env(tmp_path, monkeypatch):
    env = build_world(f"sqlite:///{tmp_path / 'mount.db'}", tmp_path, monkeypatch, "?")
    try:
        yield env
    finally:
        repository().close()
        repository.cache_clear()


@pytest.mark.anyio
async def test_a_private_mount_counts_only_for_its_readers_on_sqlite(sqlite_env):
    await assert_the_mount_counts_only_for_its_readers(sqlite_env)


def test_graph_caches_follow_the_effective_set_on_sqlite(sqlite_env, monkeypatch):
    assert_graph_caches_follow_the_effective_set(sqlite_env, monkeypatch)


def test_a_report_reads_the_mount_by_its_author_on_sqlite(sqlite_env, monkeypatch):
    assert_a_report_reads_the_mount_by_its_author(sqlite_env, monkeypatch)


def test_a_global_ask_reads_by_its_actor_on_sqlite(sqlite_env):
    assert_a_global_ask_reads_by_its_actor(sqlite_env)


def test_a_detached_ask_reads_by_its_actor_on_sqlite(sqlite_env):
    assert_a_detached_ask_reads_by_its_actor(sqlite_env)


def test_no_actor_and_no_request_reads_only_public_libraries_on_sqlite(
    sqlite_env, monkeypatch,
):
    assert_no_actor_no_request_reads_only_public_libraries(sqlite_env, monkeypatch)


def test_a_join_summary_is_the_joiners_on_sqlite(sqlite_env):
    assert_a_join_summary_is_the_joiners(sqlite_env)
