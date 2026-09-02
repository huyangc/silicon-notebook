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
ASK_MODES: dict[str, AskMode] = {
    "chunk":     AskMode("chunk",     "ask_chunk",     "general", False, False, True),
    "reasoning": AskMode("reasoning", "ask_reasoning", "strict",  True,  True,  True),
}

DEFAULT_MODE = "chunk"
# Request-only selector used by the simplified UI.  It is intentionally not a
# registry entry or user-facing engine, so ``resolve_mode("auto")`` raises.  The
# backend resolves it to one of the two stable built-ins: the synchronous /ask
# path before its durable job exists, the streaming path INSIDE the detached
# worker — there the ``ask_jobs.mode`` row temporarily carries "auto" until
# selection finishes (and keeps it if the job fails/cancels before that), so
# never feed a job row's mode straight into ``resolve_mode``.
AUTO_MODE = "auto"

# 退役但曾合法的 mode id → 映射 chunk(保旧会话/书签持久化的 mode 不 422)。
# 窄例外:仅这三个具名 id;其余未知 mode 仍 UnknownAskMode。
_RETIRED_MODES = {"fast": "chunk", "global": "chunk", "graph": "chunk"}


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
