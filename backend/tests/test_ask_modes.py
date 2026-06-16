import pytest
from app.services.ask_modes import (
    ASK_MODES, DEFAULT_MODE, AskMode, UnknownAskMode,
    resolve_mode, user_facing_mode_ids,
)


def test_registry_has_expected_modes_and_flags():
    assert set(ASK_MODES) == {"chunk", "reasoning", "graph", "fast", "global"}
    assert ASK_MODES["chunk"].handler == "ask_chunk"
    assert ASK_MODES["chunk"].requires_kg is False
    assert ASK_MODES["reasoning"].handler == "ask_reasoning"
    assert ASK_MODES["reasoning"].streaming is True
    assert ASK_MODES["reasoning"].requires_kg is True
    assert ASK_MODES["graph"].handler == "ask_graph"
    assert ASK_MODES["graph"].streaming is False        # ask_graph 暂无 on_trace
    assert ASK_MODES["fast"].handler == "ask_fast"


def test_user_facing_subset_is_chunk_and_strict_engines():
    assert user_facing_mode_ids() == ["chunk", "reasoning", "graph"]
    assert ASK_MODES["fast"].user_facing is False
    assert ASK_MODES["global"].user_facing is False


def test_resolve_known_default_and_unknown():
    assert resolve_mode("graph") is ASK_MODES["graph"]
    assert resolve_mode(None) is ASK_MODES[DEFAULT_MODE]   # 缺省 → chunk
    assert resolve_mode("") is ASK_MODES[DEFAULT_MODE]
    with pytest.raises(UnknownAskMode) as exc:
        resolve_mode("bogus")
    assert exc.value.mode == "bogus"
