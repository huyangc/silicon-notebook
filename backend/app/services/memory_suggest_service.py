"""The per-user switch of auto user-memory suggestions (design §6.2, D12; plan G14).

``PATCH /me/memory-mode`` is its one caller. Turning suggestions on is refused
while the deployment cannot run them (``memory_suggest_wiring_active``, the one
wiring predicate). The mode itself has ONE writer,
``MemorySuggestStorePort.set_memory_mode`` (``user_profiles.memory_mode``, the
switch row and -- for ``manual`` -- the owner's job rows, in one transaction).
Only after that commit, and only when asked while turning suggestions off, the
owner's OPEN system suggestions are deleted across every notebook through a
conditional delete that re-checks each row is still open; dismissed or expired
ones (``rejected``) always stay.

Depends on repositories through ``app.repositories.ports`` only (G28).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

from app.domain.memory_suggest_contracts import (
    MEMORY_MODE_MANUAL,
    MEMORY_MODE_SUGGEST,
    MEMORY_MODES,
    ORIGIN_SUGGESTED,
)
from app.models.memory import PaginatedMemories
from app.repositories.ports import MemorySuggestStorePort
from app.services.memory_suggest_wiring import memory_suggest_wiring_active

# One list page and one bulk delete per round: the bulk delete's own bound.
_DELETE_PAGE = 200


class MemorySuggestUnavailableError(Exception):
    """Turning suggestions on while this deployment cannot run them (the
    deployment switch is off, or every signal class is): nothing was written.
    HTTP 409."""


class SuggestionDeletePort(Protocol):
    """What deleting the open suggestions needs from ``MemoryService``."""

    def list_memories(
        self,
        user_id: str,
        notebook_id: str | None = None,
        status: str | None = None,
        origin: str | None = None,
        query: str = "",
        offset: int = 0,
        limit: int = 50,
    ) -> PaginatedMemories: ...

    def delete_open_suggestions(self, user_id: str, memory_ids: Sequence[str]) -> int: ...


@dataclass(frozen=True)
class MemoryModeChange:
    """``MemorySuggestService.set_mode``: the mode now stored and how many
    open suggestions this request deleted (0 unless asked)."""

    memory_mode: str
    deleted_suggestion_count: int


class MemorySuggestService:
    def __init__(
        self,
        store: MemorySuggestStorePort,
        memory: SuggestionDeletePort,
        settings: object,
        now: Callable[[], str],
    ) -> None:
        self.store = store
        self.memory = memory
        self.settings = settings
        self.now = now

    def set_mode(
        self, user_id: str, mode: str, *, delete_unadopted: bool = False
    ) -> MemoryModeChange:
        """Store ``mode`` (``manual`` / ``suggest``) for ``user_id``.

        ``suggest`` while ``memory_suggest_wiring_active`` is false raises
        ``MemorySuggestUnavailableError`` before anything is written; an
        unknown mode raises ``ValueError``; an unknown user ``KeyError`` (from
        the store). ``delete_unadopted`` only acts with ``manual``: after the
        mode change has committed (no run can write a draft any more), every
        ``candidate`` row of the owner that carries a ``suggested_signal`` is
        deleted, in every notebook the owner reads, page by page through
        ``delete_open_suggestions`` -- a conditional delete that re-checks
        "still an open suggestion" on the locked row, so one the owner adopts
        in another tab between the page read and the delete is kept.
        ``rejected`` suggestions are never deleted here."""
        if mode not in MEMORY_MODES:
            raise ValueError("invalid memory_mode")
        if mode == MEMORY_MODE_SUGGEST and not memory_suggest_wiring_active(self.settings):
            raise MemorySuggestUnavailableError()
        self.store.set_memory_mode(user_id, mode, now=self.now)
        deleted = 0
        if mode == MEMORY_MODE_MANUAL and delete_unadopted:
            deleted = self._delete_open_suggestions(user_id)
        return MemoryModeChange(memory_mode=mode, deleted_suggestion_count=deleted)

    def _delete_open_suggestions(self, user_id: str) -> int:
        """Delete the owner's open suggestions; returns how many went. Always
        reads the first page again (the previous one is gone); stops when a
        page is empty or deletes nothing (rows a concurrent delete already
        removed)."""
        deleted = 0
        while True:
            page = self.memory.list_memories(
                user_id, status="candidate", origin=ORIGIN_SUGGESTED,
                offset=0, limit=_DELETE_PAGE,
            )
            ids = [
                item.id for item in page.items
                if item.status == "candidate" and item.suggested_signal
            ]
            if not ids:
                return deleted
            removed = self.memory.delete_open_suggestions(user_id, ids)
            if removed <= 0:
                return deleted
            deleted += removed
