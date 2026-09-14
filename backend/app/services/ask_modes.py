"""Canonical registry of ask() retrieval modes — the single source of truth for
which modes exist, where each dispatches, and how the API/UI must treat them.

SQLiteRepository.ask() (dispatch) and the API layer (validation + /ask-modes)
both read this module, so a mode is added/renamed in exactly one place; the
cross-stack check scripts/check_ask_modes_contract.py keeps the frontend mode
list (frontend/app/ask-modes.ts) in lock-step.
"""
from __future__ import annotations

from app.domain.ask import AskMode


class UnknownAskMode(ValueError):
    """An ask() mode string not in the registry. The API layer maps this to HTTP
    422 — there is no silent fall-through to the legacy KG path."""

    def __init__(self, mode: str) -> None:
        super().__init__(mode)
        self.mode = mode


# Insertion order = display order for user_facing modes.
# ``requires_kg`` is a hard precondition ("this engine cannot run without a
# knowledge graph"), not a quality hint. Built-in reasoning is False: without a
# graph it retrieves source passages and collection listings instead, and only
# the graph-shaped actions drop out. The field stays on AskMode because plugin
# engine descriptors may still declare True, and the frontend gate honours those.
ASK_MODES: dict[str, AskMode] = {
    "chunk":     AskMode("chunk",     "ask_chunk",     "general", False, False, True),
    "reasoning": AskMode("reasoning", "ask_reasoning", "strict",  True,  False, True),
}

DEFAULT_MODE = "chunk"

# 退役但曾合法的 mode id → 映射到某个内置 id:别名本身不再是 422 的成因,旧会话
# /书签持久化的 mode 与未刷新的旧标签页照常解析。映射目标可以是任一内置 id,不必
# 是同一个:fast/global/graph 是三个退役引擎,落回 chunk;``auto`` 曾是简化界面的
# **请求级选择器**(后端跑分类模型再选引擎),现已下线——简化界面直接提交
# reasoning,所以这个别名也映射 reasoning。别名只归一 id,不再像旧选择器那样把
# retrieval_effort 压成 standard,也不替请求补 intent:旧标签页发来的模糊问题会被
# reasoning 的确定性澄清闸拦成 422,刷新一次即消失。窄例外:仅这四个具名 id;其余
# 未知 mode 仍 UnknownAskMode。
_RETIRED_MODES = {
    "fast": "chunk",
    "global": "chunk",
    "graph": "chunk",
    "auto": "reasoning",
}


def resolve_mode(
    mode: str | None,
    extension_modes: tuple[AskMode, ...] = (),
) -> AskMode:
    """Return the AskMode for `mode` (DEFAULT_MODE when None/empty).
    Raise UnknownAskMode for anything not registered."""
    key = mode or DEFAULT_MODE
    key = _RETIRED_MODES.get(key, key)
    builtin = ASK_MODES.get(key)
    if builtin is not None:
        return builtin
    for extension_mode in extension_modes:
        if extension_mode.id == key:
            return extension_mode
    raise UnknownAskMode(key)


def user_facing_mode_ids() -> list[str]:
    """Mode ids the UI may expose, in registry order."""
    return [m.id for m in ASK_MODES.values() if m.user_facing]


def user_facing_modes(
    extension_modes: tuple[AskMode, ...] = (),
) -> tuple[AskMode, ...]:
    """Built-ins first, then the startup-frozen deployment projection."""

    return (
        *(mode for mode in ASK_MODES.values() if mode.user_facing),
        *(mode for mode in extension_modes if mode.user_facing),
    )
