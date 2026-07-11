"""Task 24 contract: the Ask mode engines + synthesis live in
app.services.ask_service.AskService — ONE lazily-composed runtime service over
narrow ports (ask-state / retrieval / evidence-context / model clients /
communities / scale profile), never over the facade.

Frozen here (the RED items of the move):

1. non-streaming ``repo.ask()`` never creates an ask_jobs row — durable jobs
   belong exclusively to the streaming AskExecutionCoordinator;
2. the runtime owns ONE AskService (identity-stable across resolutions) and
   the facade's frozen ``ask_chunk``/``ask_reasoning``/``ask_graph``
   signatures adapt ``current_user().id`` into the service's keyword-only
   ``user_id``;
3. per-user model changes resolve per call at the service's model-client port
   — no restart, no rewire (the ``_llm_for_role`` ContextVar chain is intact);
4. module boundary: ask_service.py never imports the facade or the runtime,
   never opens private DB seams and never reads the request ContextVar
   directly (persistence identity is explicit ``user_id``; model identity
   rides the injected provider).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.core.config import Settings
from app.models.schemas import AskRequest, AskResponse, NotebookCreate
from app.services.ask_service import AskService
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository, set_request_user, reset_request_user


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def test_non_streaming_ask_creates_no_job(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))

    response = repo.ask(nb.id, AskRequest(question="q", mode="chunk"))

    assert response.answer_id                       # 答案照存(_save_answer 收口)
    with repo._connect() as db:
        jobs = db.execute("SELECT COUNT(*) AS n FROM ask_jobs").fetchone()["n"]
    assert jobs == 0                                # 非流式 ask 绝不建 durable job


def test_runtime_owns_one_ask_service_and_facade_adapts_identity(repo, monkeypatch):
    service = repo._runtime.ask_service()
    assert isinstance(service, AskService)
    assert service is repo._runtime.ask_service()   # one owner, identity-stable

    nb = repo.create_notebook(NotebookCreate(name="nb"))
    seen: dict = {}

    def fake_chunk(notebook_id, payload, *, user_id, cancel_event=None):
        seen["args"] = (notebook_id, user_id, cancel_event)
        return AskResponse(conclusion="stubbed")

    monkeypatch.setattr(service, "ask_chunk", fake_chunk, raising=False)
    out = repo.ask_chunk(nb.id, AskRequest(question="q", mode="chunk"))

    assert out.conclusion == "stubbed"
    assert seen["args"] == (nb.id, repo.current_user().id, None)


def test_per_user_model_changes_resolve_without_restart(repo):
    service = repo._runtime.ask_service()
    assert service.model_clients is repo._runtime.models    # 同一 provider,一个所有者

    user = repo.current_user()
    token = set_request_user(user)
    try:
        repo.set_user_model_settings(
            user.id, {"llm": {"base_url": "https://u/v1", "api_key": "k", "model": "m-u1"}})
        assert service.model_clients.llm_client.model == "m-u1"
        repo.set_user_model_settings(
            user.id, {"llm": {"base_url": "https://u/v1", "api_key": "k", "model": "m-u2"}})
        assert service.model_clients.llm_client.model == "m-u2"   # 无需重启/重接线
    finally:
        reset_request_user(token)


def test_ask_service_module_never_imports_facade_or_private_db():
    source = (
        Path(__file__).resolve().parents[1] / "app" / "services" / "ask_service.py"
    ).read_text(encoding="utf-8")
    modules = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    assert not any(m.startswith("app.services.sqlite_repository") for m in modules)
    assert not any(m.startswith("app.services.repository_runtime") for m in modules)
    assert "sqlite3" not in modules
    # 持久化身份显式(user_id 由调用方传入);模型身份走注入的 provider —— 本模块
    # 绝不直接碰请求 ContextVar,也绝不开私有 DB 缝。
    assert "_REQUEST_USER" not in source
    assert "._connect(" not in source and "._write(" not in source
