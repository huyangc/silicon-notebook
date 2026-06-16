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

    calls = {}
    for mid in ("ask_chunk", "ask_reasoning", "ask_graph"):
        def make(mid):
            return lambda notebook_id, payload: calls.__setitem__("hit", mid) or AskResponse(conclusion=mid)
        monkeypatch.setattr(repo, mid, make(mid))

    assert repo.ask(nb.id, AskRequest(question="q")).conclusion == "ask_chunk"       # 缺省
    assert repo.ask(nb.id, AskRequest(question="q", mode="graph")).conclusion == "ask_graph"
    # P4-5: retired ids "fast"/"global" alias to chunk (保旧会话/书签不 422)
    assert repo.ask(nb.id, AskRequest(question="q", mode="fast")).conclusion == "ask_chunk"
    assert repo.ask(nb.id, AskRequest(question="q", mode="global")).conclusion == "ask_chunk"

    from app.services.ask_modes import UnknownAskMode
    with pytest.raises(UnknownAskMode):
        repo.ask(nb.id, AskRequest(question="q", mode="bogus"))


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
