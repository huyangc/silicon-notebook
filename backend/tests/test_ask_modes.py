import threading

import pytest
from app.services.ask_modes import (
    ASK_MODES, DEFAULT_MODE, AskMode, UnknownAskMode,
    resolve_mode, user_facing_mode_ids,
)


def test_ask_dispatches_by_registry(monkeypatch, tmp_path):
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository
    from app.models.schemas import AskRequest, AskResponse, NotebookCreate

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "")
    repo = SQLiteRepository(Settings())
    nb = repo.create_notebook(NotebookCreate(name="nb"))

    calls = {}; ask_service = repo.__dict__["_runtime"].ask_component
    for mid in ("ask_chunk", "ask_reasoning", "ask_graph"):
        def make(mid):
            return lambda notebook_id, payload, **kwargs: calls.__setitem__("hit", mid) or AskResponse(conclusion=mid)
        monkeypatch.setattr(ask_service, mid, make(mid))

    service = repo.__dict__["_runtime"].ask_component
    user_id = repo.current_user().id
    assert service.ask(nb.id, AskRequest(question="q"), user_id=user_id).conclusion == "ask_chunk"
    assert service.ask(nb.id, AskRequest(question="q", mode="graph"), user_id=user_id).conclusion == "ask_graph"
    # P4-5: retired ids "fast"/"global" alias to chunk (保旧会话/书签不 422)
    assert service.ask(nb.id, AskRequest(question="q", mode="fast"), user_id=user_id).conclusion == "ask_chunk"
    assert service.ask(nb.id, AskRequest(question="q", mode="global"), user_id=user_id).conclusion == "ask_chunk"

    from app.services.ask_modes import UnknownAskMode
    with pytest.raises(UnknownAskMode):
        service.ask(nb.id, AskRequest(question="q", mode="bogus"), user_id=user_id)


def test_registry_has_expected_modes_and_flags():
    # P4-5: fast/global 已从注册表移除
    assert set(ASK_MODES) == {"chunk", "reasoning", "graph"}
    assert ASK_MODES["chunk"].handler == "ask_chunk"
    assert ASK_MODES["chunk"].requires_kg is False
    assert ASK_MODES["reasoning"].handler == "ask_reasoning"
    assert ASK_MODES["reasoning"].streaming is True
    assert ASK_MODES["reasoning"].requires_kg is True
    assert ASK_MODES["graph"].handler == "ask_graph"
    assert ASK_MODES["graph"].streaming is False        # ask_graph 暂无 on_trace


def test_user_facing_subset_is_chunk_and_strict_engines():
    assert user_facing_mode_ids() == ["chunk", "reasoning", "graph"]


def test_resolve_known_default_and_unknown():
    assert resolve_mode("graph") is ASK_MODES["graph"]
    assert resolve_mode(None) is ASK_MODES[DEFAULT_MODE]   # 缺省 → chunk
    assert resolve_mode("") is ASK_MODES[DEFAULT_MODE]
    with pytest.raises(UnknownAskMode) as exc:
        resolve_mode("bogus")
    assert exc.value.mode == "bogus"


def test_ask_service_dispatches_by_the_same_registry(monkeypatch):
    """Task 24: AskService.ask 与 facade 同一注册表派发 —— mode id、退役别名
    (fast/global→chunk)与 UnknownAskMode 语义逐字一致,user_id 关键字直达 handler。"""
    from app.models.schemas import AskRequest, AskResponse
    from app.services.ask_service import AskService

    service = AskService.__new__(AskService)   # dispatch 不触任何端口
    calls = {}
    events = []
    service.event_log = type(
        "_Log", (), {"emit": lambda _self, event: events.append(event)}
    )()

    def make(mid):
        def handler(notebook_id, payload, *, user_id,
                    on_trace=None, cancel_event=None, seed_ids=None,
                    job_id=None):
            from app.services.retrieval_run import current_retrieval_run

            calls["hit"] = (mid, user_id)
            calls["run_cancel"] = current_retrieval_run().cancel_event
            return AskResponse(conclusion=mid)
        return handler

    for mid in ("ask_chunk", "ask_reasoning", "ask_graph"):
        monkeypatch.setattr(service, mid, make(mid), raising=False)

    cancel_event = threading.Event()
    assert service.ask(
        "nb", AskRequest(question="q"), user_id="u1", job_id="job-safe-id",
        cancel_event=cancel_event,
    ).conclusion == "ask_chunk"
    assert calls["hit"] == ("ask_chunk", "u1")
    assert calls["run_cancel"] is cancel_event
    assert events[0]["kind"] == "retrieval_run_stats"
    assert events[0]["correlation_id"] == "job-safe-id"
    assert service.ask("nb", AskRequest(question="q", mode="graph"), user_id="u1").conclusion == "ask_graph"
    assert service.ask("nb", AskRequest(question="q", mode="reasoning"), user_id="u1").conclusion == "ask_reasoning"
    # 退役 id 映射与 facade 完全一致(保旧会话/书签不 422)
    assert service.ask("nb", AskRequest(question="q", mode="fast"), user_id="u1").conclusion == "ask_chunk"
    assert service.ask("nb", AskRequest(question="q", mode="global"), user_id="u1").conclusion == "ask_chunk"
    with pytest.raises(UnknownAskMode):
        service.ask("nb", AskRequest(question="q", mode="bogus"), user_id="u1")
