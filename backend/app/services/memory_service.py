"""Owner-private Memory lifecycle orchestration over typed repository ports."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from app.core.event_logging import EventLogger
from app.models.memory import MemoryRevision, MemoryWrite
from app.models.schemas import MemoryRecord, MemoryUpdate, PaginatedMemories
from app.repositories.ports import (
    AskStateStorePort,
    MemoryStorePort,
    NotebookAccessRepository,
)
from app.services.embedding import Embedder


@dataclass(frozen=True)
class MemoryEmbeddingJob:
    item: MemoryRecord
    revision: int


class MemoryService:
    def __init__(
        self,
        store: MemoryStorePort,
        ask_state: AskStateStorePort,
        notebooks: NotebookAccessRepository,
        embedder: Embedder,
        event_log: EventLogger,
        new_id,
        now,
        embedding_scheduler: Callable[[Callable[[MemoryEmbeddingJob], MemoryRecord], MemoryEmbeddingJob], Any] | None = None,
    ) -> None:
        self.store = store
        self.ask_state = ask_state
        self.notebooks = notebooks
        self.embedder = embedder
        self.event_log = event_log
        self.new_id = new_id
        self.now = now
        self.embedding_scheduler = embedding_scheduler or (lambda fn, item: fn(item))

    @staticmethod
    def _patch(patch: MemoryUpdate | Mapping[str, Any] | None) -> tuple[dict, str]:
        if patch is None:
            return {}, ""
        if hasattr(patch, "model_dump"):
            values = patch.model_dump(exclude_none=True)
        else:
            values = dict(patch)
        reason = str(values.pop("reason", "") or "")
        unknown = set(values) - {"title", "content_md", "tags"}
        if unknown:
            raise ValueError(f"unsupported memory fields: {sorted(unknown)}")
        return values, reason

    @staticmethod
    def _snapshot(item: MemoryRecord) -> dict:
        return {
            "title": item.title,
            "content_md": item.content_md,
            "tags": list(item.tags),
            "status": item.status,
            "promotion_state": item.promotion_state,
        }

    def _require_notebook(self, notebook_id: str, user_id: str) -> None:
        if not self.notebooks.user_can_read_notebook(notebook_id, user_id):
            raise PermissionError(notebook_id)

    def _event(self, kind: str, item: MemoryRecord, **extra: Any) -> None:
        self.event_log.emit(
            {
                "kind": kind,
                "memory_id": item.id,
                "notebook_id": item.notebook_id,
                "owner_id": item.created_by,
                "origin": item.origin,
                "status": item.status,
                **extra,
            }
        )

    def _embed(self, job: MemoryEmbeddingJob) -> MemoryRecord:
        item = job.item
        try:
            vectors = self.embedder.embed_texts([f"{item.title}\n{item.content_md}"])
            if len(vectors) != 1 or len(vectors[0]) != int(self.embedder.dim):
                raise ValueError("embedding dimension mismatch")
            applied = self.store.replace_embedding(
                item.id, job.revision, type(self.embedder).__name__, vectors[0]
            )
            if not applied:
                return item
            self._event(
                "memory_embedding",
                item,
                embedding_status="ready",
                dimension=len(vectors[0]),
            )
            return item
        except Exception as exc:  # best-effort: Memory remains usable as text
            applied = self.store.mark_embedding_failed(
                item.id, job.revision, type(exc).__name__
            )
            if not applied:
                return item
            self._event(
                "memory_embedding",
                item,
                embedding_status="failed",
                error_type=type(exc).__name__,
            )
            return item

    def _schedule_embed(self, item: MemoryRecord) -> MemoryRecord:
        revision = self.store.embedding_revision(item.id, item)
        if revision is None:
            return item
        job = MemoryEmbeddingJob(item=item, revision=revision)
        try:
            self.embedding_scheduler(self._embed, job)
        except Exception as exc:
            applied = self.store.mark_embedding_failed(
                item.id, job.revision, type(exc).__name__
            )
            if applied:
                self._event(
                    "memory_embedding",
                    item,
                    embedding_status="failed",
                    error_type=type(exc).__name__,
                )
        return item

    def create_candidate(
        self,
        notebook_id: str,
        user_id: str,
        agent_profile_id: str | None,
        client_request_id: str,
        title: str,
        content_md: str,
        tags: Sequence[str],
        reason: str,
        task_context: Mapping[str, Any] | None = None,
        evidence_refs: Sequence[Mapping[str, Any]] | None = None,
    ) -> MemoryRecord:
        self._require_notebook(notebook_id, user_id)
        if agent_profile_id and not self.store.agent_profile_belongs_to(
            agent_profile_id, user_id
        ):
            raise PermissionError(agent_profile_id)
        existing = self.store.memory_by_agent_request(
            user_id, notebook_id, agent_profile_id, client_request_id
        )
        if existing is not None:
            return existing
        now = self.now()
        write = MemoryWrite(
            id=self.new_id("memory"),
            notebook_id=notebook_id,
            created_by=user_id,
            agent_profile_id=agent_profile_id,
            origin="external_agent",
            status="candidate",
            title=title,
            content_md=content_md,
            tags=list(tags),
            created_at=now,
            updated_at=now,
            provenance={
                "client_request_id": client_request_id,
                "reason": reason,
                "task_context": dict(task_context or {}),
                "evidence_refs": list(evidence_refs or []),
            },
        )
        item = self.store.create_candidate_with_initial_revision(
            write, user_id, "created"
        )
        if item.id != write.id:
            return item
        self._event("memory_lifecycle", item, action="candidate_created")
        return item

    def create_from_answer(
        self,
        notebook_id: str,
        user_id: str,
        answer_id: str,
        title: str,
        content_md: str,
        tags: Sequence[str],
    ) -> MemoryRecord:
        self._require_notebook(notebook_id, user_id)
        existing = self.store.memory_by_answer(user_id, answer_id)
        if existing is not None:
            if existing.notebook_id != notebook_id:
                raise KeyError(answer_id)
            return existing
        try:
            source = self.ask_state.answer_memory_source(answer_id)
        except KeyError:
            raise KeyError(answer_id)
        if source["notebook_id"] != notebook_id:
            raise KeyError(answer_id)
        if not self.notebooks.user_can_read_answer(answer_id, user_id):
            raise PermissionError(answer_id)
        now = self.now()
        write = MemoryWrite(
            id=self.new_id("memory"),
            notebook_id=notebook_id,
            created_by=user_id,
            source_answer_id=answer_id,
            origin="ask_answer",
            status="confirmed",
            title=title,
            content_md=content_md,
            tags=list(tags),
            confirmed_by=user_id,
            confirmed_at=now,
            created_at=now,
            updated_at=now,
            provenance={
                key: source[key]
                for key in (
                    "answer_id",
                    "question",
                    "answer",
                    "conversation_id",
                    "mode",
                    "model",
                    "evidence_level",
                    "anchors",
                    "citations",
                )
            },
        )
        item = self.store.create_answer_with_initial_revision(
            write, user_id, "created"
        )
        if item.id != write.id:
            return item
        self._event("memory_lifecycle", item, action="answer_confirmed")
        return self._schedule_embed(item)

    def update(
        self, memory_id: str, user_id: str, patch: MemoryUpdate | Mapping[str, Any]
    ) -> MemoryRecord:
        current = self.get(memory_id, user_id)
        if current.status not in {"candidate", "confirmed"}:
            raise ValueError(f"cannot edit {current.status} memory")
        values, reason = self._patch(patch)
        if not values:
            return current
        item = self.store.update_with_revision(
            memory_id,
            user_id,
            values,
            expected={"candidate", "confirmed"},
            changed_by=user_id,
            reason=reason or "updated",
        )
        self._event("memory_lifecycle", item, action="updated")
        return self._schedule_embed(item) if item.status == "confirmed" else item

    def confirm(
        self,
        memory_id: str,
        user_id: str,
        patch: MemoryUpdate | Mapping[str, Any] | None = None,
    ) -> MemoryRecord:
        values, reason = self._patch(patch)
        item = self.store.transition_with_revision(
            memory_id,
            user_id,
            {"candidate"},
            "confirmed",
            fields=values,
            changed_by=user_id,
            reason=reason or "confirmed",
        )
        self._event("memory_lifecycle", item, action="confirmed")
        return self._schedule_embed(item)

    def reject(self, memory_id: str, user_id: str) -> MemoryRecord:
        item = self.store.transition_with_revision(
            memory_id,
            user_id,
            {"candidate"},
            "rejected",
            fields=None,
            changed_by=user_id,
            reason="rejected",
        )
        self._event("memory_lifecycle", item, action="rejected")
        return item

    def deprecate(self, memory_id: str, user_id: str) -> MemoryRecord:
        item = self.store.transition_with_revision(
            memory_id,
            user_id,
            {"confirmed"},
            "deprecated",
            fields=None,
            changed_by=user_id,
            reason="deprecated",
        )
        self._event("memory_lifecycle", item, action="deprecated")
        return item

    def get(self, memory_id: str, user_id: str) -> MemoryRecord:
        return self.store.memory_for_user(memory_id, user_id)

    def revisions(self, memory_id: str, user_id: str) -> list[MemoryRevision]:
        return self.store.revisions_for_user(memory_id, user_id)

    def list_memories(
        self,
        user_id: str,
        notebook_id: str | None = None,
        status: str | None = None,
        origin: str | None = None,
        query: str = "",
        offset: int = 0,
        limit: int = 50,
    ) -> PaginatedMemories:
        if notebook_id is not None:
            self._require_notebook(notebook_id, user_id)
        return self.store.list_memories(
            user_id,
            notebook_id=notebook_id,
            status=status,
            origin=origin,
            query=query,
            offset=offset,
            limit=limit,
        )

    def answer_memory_links(
        self, notebook_id: str, user_id: str, answer_ids: Sequence[str]
    ) -> dict[str, str]:
        unique_ids = list(
            dict.fromkeys(str(answer_id) for answer_id in answer_ids if answer_id)
        )
        if len(unique_ids) > 200:
            raise ValueError("answer_ids may contain at most 200 unique values")
        self._require_notebook(notebook_id, user_id)
        return self.store.answer_memory_links(notebook_id, user_id, unique_ids)
