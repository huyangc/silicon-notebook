"""Global Ask refuses a scope with nothing a global run could search.

The single-library entry point refuses an empty retrieval scope with a 409
(``ask_routes._require_ask_available`` / ``_require_non_empty_scope``); the
global entry point now refuses the same condition at submission, before any
job row, conversation row or model call (``GlobalAskService.
_require_searchable``, inside ``_resolve_run_scope``, so the reasoning preview
refuses too). Same composition (``local_universe_non_empty``), per participant,
over what a global run can actually search: its frozen visible-source
ceilings. A library whose only rows are Knowhow cells, confirmed Memory or KG
objects with no evidence in that ceiling is unreachable by a global run and
does not admit it; any library with a visible source does.

The scenarios are backend-neutral (store APIs; raw SQL takes the placeholder),
so the PostgreSQL twin (``tests/postgres/test_global_ask_empty_scope_pg.py``)
runs them verbatim on the canonical backend.
"""
from __future__ import annotations

import json
import time
import uuid
from threading import Event

import pytest

from app.core.config import Settings
from app.core.request_context import reset_request_user, set_request_user
from app.models.global_ask import GlobalAskIntentPreviewRequest, GlobalAskRequest
from app.models.notebooks import NotebookCreate
from app.repositories.ports import ChunkWrite, SourceElementWrite
from app.services.global_ask import SCOPE_EMPTY, GlobalAskError

NOW = "2026-10-19T00:00:00+00:00"
EMPTY_COPY_OPENING = "当前检索范围为空"


# -- world --------------------------------------------------------------------

def _source(repo, notebook_id: str, source_type: str = "markdown") -> str:
    source_id = f"src-{uuid.uuid4().hex[:10]}"
    repo._runtime.source_store.insert_source(
        source_id=source_id, notebook_id=notebook_id, title=source_id,
        source_type=source_type, status="active", parse_status="parsed",
        file_name=f"{source_id}.md", file_path="", file_size=0, file_hash="",
        summary="", doc_type="", memory_id="",
    )
    return source_id


def _chunk(repo, notebook_id: str, source_id: str) -> None:
    element_id = f"el-{source_id}"
    with repo._runtime.chunk_store.database.write() as db:
        repo._runtime.source_store.replace_elements(
            db, source_id,
            [SourceElementWrite(element_id, "paragraph", "p1", "cell text", {})],
            created_at=NOW,
        )
    repo._runtime.chunk_store.replace_source_chunks(
        source_id, notebook_id,
        [ChunkWrite(id=f"ck-{source_id}", text="cell text", section_path="",
                    element_ids=(element_id,))],
        created_at=NOW,
    )


def _kg_object(repo, ph: str, notebook_id: str) -> None:
    with repo._runtime.chunk_store.database.write() as db:
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,"
            "created_at,updated_at) VALUES "
            f"({ph},{ph},'concept','approved','',{ph},{ph},'',{ph},{ph})",
            (f"ko-{uuid.uuid4().hex[:10]}", notebook_id,
             json.dumps({"name": "概念", "definition": "定义"}), "[]", NOW, NOW),
        )


def _confirmed_memory(repo, ph: str, notebook_id: str, user_id: str) -> None:
    with repo._runtime.chunk_store.database.write() as db:
        db.execute(
            "INSERT INTO memory_items"
            "(id,notebook_id,created_by,agent_profile_id,source_answer_id,"
            "origin,status,title,content_md,created_at,updated_at) VALUES "
            f"({ph},{ph},{ph},NULL,NULL,'ask_answer','confirmed',{ph},{ph},{ph},{ph})",
            (f"mem-{uuid.uuid4().hex[:10]}", notebook_id, user_id,
             "记忆", "记忆正文", NOW, NOW),
        )


def build_world(repo, ph: str) -> dict:
    """Alice owns empty, chunk-only (Knowhow), KG-only, Memory-only, sourced (a
    visible source not chunked yet) and full notebooks; Bob owns one full and
    one empty notebook, neither of which Alice can read."""
    identity = repo._runtime.identity
    alice = identity.create_user("a00191019", "password-12")
    bob = identity.create_user("b00191019", "password-12")
    world = {"alice": alice, "bob": bob}
    marker = set_request_user(alice)
    try:
        for name in ("empty_a", "empty_b", "chunk_only", "kg_only",
                     "memory_only", "sourced", "full"):
            world[name] = repo._runtime.catalog.create_notebook(
                NotebookCreate(name=name)
            ).id
    finally:
        reset_request_user(marker)
    marker = set_request_user(bob)
    try:
        world["bobs"] = repo._runtime.catalog.create_notebook(
            NotebookCreate(name="bobs")
        ).id
        world["bobs_empty"] = repo._runtime.catalog.create_notebook(
            NotebookCreate(name="bobs_empty")
        ).id
    finally:
        reset_request_user(marker)
    # chunk_only: a Knowhow cell -- no VISIBLE source (empty ceiling), one chunk.
    _chunk(repo, world["chunk_only"], _source(repo, world["chunk_only"], "knowhow"))
    _kg_object(repo, ph, world["kg_only"])
    _confirmed_memory(repo, ph, world["memory_only"], alice.id)
    # sourced: a visible source still parsing -- ceiling non-empty, no chunk.
    _source(repo, world["sourced"])
    full_source = _source(repo, world["full"])
    _chunk(repo, world["full"], full_source)
    bobs_source = _source(repo, world["bobs"])
    _chunk(repo, world["bobs"], bobs_source)
    _kg_object(repo, ph, world["bobs"])
    return world


def _include(*ids):
    return {"mode": "include", "notebook_ids": list(ids)}


def _no_model(monkeypatch, service):
    """Every engine entry a submission could reach before or instead of a job:
    any call fails the test (the refusal must cost no model call)."""
    for name in ("preview_reasoning_intent", "resolve_reasoning_followup",
                 "validate_reasoning_submission", "ask"):
        monkeypatch.setattr(
            service.ask, name,
            lambda *a, _n=name, **k: pytest.fail(f"refused scope reached {_n}"),
        )


def _refused(call) -> GlobalAskError:
    with pytest.raises(GlobalAskError) as refused:
        call()
    assert refused.value.status_code == 409
    assert refused.value.reason == SCOPE_EMPTY
    assert refused.value.message.startswith(EMPTY_COPY_OPENING)
    return refused.value


def _preview(service, world, scope, **kwargs):
    return service.prepare_intent_preview(
        GlobalAskIntentPreviewRequest(question="有哪些内容", notebook_scope=scope),
        user_id=world["alice"].id, **kwargs,
    )


# -- scenarios ----------------------------------------------------------------

def scenario_all_empty_is_refused_before_anything(repo, world, monkeypatch):
    service = repo._runtime.global_ask_service()
    _no_model(monkeypatch, service)
    scope = _include(world["empty_a"], world["empty_b"])
    for mode in ("chunk", "reasoning"):
        _refused(lambda: service.start(
            GlobalAskRequest(question="有哪些内容", notebook_scope=scope, mode=mode),
            user_id=world["alice"].id,
        ))
    _refused(lambda: service.preview_intent(
        GlobalAskIntentPreviewRequest(question="有哪些内容", notebook_scope=scope),
        user_id=world["alice"].id,
    ))
    assert service.list_conversations(user_id=world["alice"].id) == []
    assert not service._workers and not service._pending


def scenario_hidden_rows_do_not_admit(repo, world, monkeypatch):
    """Knowhow cells, confirmed Memory and an evidence-less KG object sit
    outside every global ceiling (visible sources only): a global run could not
    retrieve them, so they must not admit one."""
    service = repo._runtime.global_ask_service()
    _no_model(monkeypatch, service)
    for library in ("chunk_only", "kg_only", "memory_only"):
        _refused(lambda: _preview(service, world, _include(world["empty_a"], world[library])))
    _refused(lambda: _preview(service, world, _include(
        world["chunk_only"], world["kg_only"], world["memory_only"],
    )))


def scenario_any_visible_source_admits(repo, world):
    service = repo._runtime.global_ask_service()
    for library in ("sourced", "full"):
        prepared = _preview(service, world, _include(world["empty_a"], world[library]))
        assert prepared.override.notebook_ids == (world["empty_a"], world[library])


def scenario_unreadable_content_never_admits(repo, world):
    service = repo._runtime.global_ask_service()
    # ``all`` resolves only what Alice may read; Bob's full notebook is not in
    # it even when the allowlist names it, so it cannot lend the run content.
    allowed = [world["empty_a"], world["empty_b"], world["bobs"]]
    _refused(lambda: _preview(service, world, {"mode": "all"}, allowed_notebook_ids=allowed))
    # Naming one of Bob's is refused as unreadable -- 404 whether it holds
    # content or not, so the admission check is never a probe of what an
    # unreadable library contains.
    for foreign in ("bobs", "bobs_empty"):
        with pytest.raises(GlobalAskError) as named:
            _preview(service, world, _include(world["empty_a"], world[foreign]))
        assert named.value.status_code == 404
    # The MCP allowlist narrows the set the same way: Alice's own full
    # notebook outside the allowlist does not admit it either.
    _refused(lambda: _preview(
        service, world, {"mode": "all"}, allowed_notebook_ids=[world["empty_a"]],
    ))
    admitted = _preview(
        service, world, {"mode": "all"},
        allowed_notebook_ids=[world["empty_a"], world["full"]],
    )
    assert set(admitted.override.notebook_ids) == {world["empty_a"], world["full"]}


def scenario_an_admitted_run_completes(repo, world):
    service = repo._runtime.global_ask_service()
    job = service.start(
        GlobalAskRequest(question="有哪些内容",
                         notebook_scope=_include(world["empty_a"], world["sourced"])),
        user_id=world["alice"].id,
    )
    deadline = time.monotonic() + 15
    while job.job_id in service._events and time.monotonic() < deadline:
        Event().wait(0.01)
    assert service.get_job(job.job_id, user_id=world["alice"].id).status == "done"


def scenario_single_library_unchanged(repo, world):
    from fastapi import HTTPException

    from app.api.ask_routes import _require_ask_available
    from app.models.source_scope import SourceScope

    marker = set_request_user(world["alice"])
    try:
        empty = repo._runtime.catalog.get_notebook(world["empty_a"])
        knowhow = repo._runtime.catalog.get_notebook(world["chunk_only"])
        full = repo._runtime.catalog.get_notebook(world["full"])
    finally:
        reset_request_user(marker)
    with pytest.raises(HTTPException) as refused:
        _require_ask_available(empty)
    assert refused.value.status_code == 409
    # A Knowhow-only notebook (no visible source) still asks by default ...
    assert _require_ask_available(knowhow) == (None, None)
    # ... and unchecking every source of a sourced notebook is still refused.
    with pytest.raises(HTTPException) as unchecked:
        _require_ask_available(
            full, repo, SourceScope(mode="include", source_ids=[]),
        )
    assert unchecked.value.status_code == 409
    assert unchecked.value.detail.startswith(EMPTY_COPY_OPENING)


SCENARIOS = (
    scenario_all_empty_is_refused_before_anything,
    scenario_hidden_rows_do_not_admit,
    scenario_any_visible_source_admits,
    scenario_unreadable_content_never_admits,
    scenario_an_admitted_run_completes,
    scenario_single_library_unchanged,
)


def run(scenario, repo, world, monkeypatch):
    params = scenario.__code__.co_varnames[:scenario.__code__.co_argcount]
    args = {"repo": repo, "world": world, "monkeypatch": monkeypatch}
    return scenario(*(args[name] for name in params))


# -- SQLite -------------------------------------------------------------------

@pytest.fixture
def sqlite_world(tmp_path, monkeypatch):
    from app.services.sqlite_repository import SQLiteRepository

    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings(
        _env_file=None, database_url=f"sqlite:///{tmp_path / 'empty-scope.db'}",
        storage_dir=str(tmp_path / "storage"),
    ))
    try:
        yield repo, build_world(repo, "?")
    finally:
        repo.close()


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.__name__)
def test_global_empty_scope(sqlite_world, monkeypatch, scenario):
    repo, world = sqlite_world
    run(scenario, repo, world, monkeypatch)


def test_the_mcp_error_is_readable_and_not_busy():
    from app.api.mcp_tools.global_ask import global_ask_tool_error
    from app.services.global_ask import _SCOPE_EMPTY_COPY

    mapped = global_ask_tool_error(GlobalAskError(409, _SCOPE_EMPTY_COPY, SCOPE_EMPTY))
    assert mapped.code == "unavailable"
    assert str(mapped.message) == _SCOPE_EMPTY_COPY


def test_the_http_refusal_has_the_single_library_shape(tmp_path, monkeypatch):
    from tests.test_global_ask_intent_route import _client
    from app.api import ask_routes

    client = _client(tmp_path, monkeypatch)
    notebook = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    for path in ("/api/global-ask/ask", "/api/global-ask/intent"):
        response = client.post(path, json={"question": "有哪些内容"})
        assert response.status_code == 409
        assert response.json()["detail"].startswith(EMPTY_COPY_OPENING)
        assert response.headers.get("X-User-Message") == "1"
    stream = client.post("/api/global-ask/intent/stream", json={"question": "有哪些内容"})
    frames = [json.loads(line) for line in stream.text.splitlines() if line.strip()]
    assert frames[-1]["event"] == "error" and frames[-1]["status"] == 409
    assert frames[-1]["message"].startswith(EMPTY_COPY_OPENING)
    assert client.get("/api/global-ask/conversations").json() == []
    # The single-library refusal on the same notebook is the same shape.
    single = client.post(f"/api/notebooks/{notebook}/ask", json={"question": "有哪些内容"})
    assert single.status_code == 409 and single.headers.get("X-User-Message") == "1"
    _source(ask_routes.repository(), notebook)
    assert client.post("/api/global-ask/intent", json={"question": "有哪些内容"}).status_code == 200

