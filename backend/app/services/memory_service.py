"""Owner-private Memory lifecycle orchestration over typed repository ports."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from app.core.event_logging import EventLogger
from app.domain.agent_tools import AGENT_SCOPES
from app.models.identity import (
    AgentPrincipal,
    AgentProfile,
    AgentTokenIssued,
    AgentTokenSummary,
)
from app.models.memory import (
    MemoryRevision,
    MemoryWrite,
    MemoryRecord,
    MemoryUpdate,
    PaginatedMemories,
)
from app.repositories.ports import (
    AskStateStorePort,
    MemoryStorePort,
    NotebookAccessRepository,
)
from app.services.embedding import Embedder
from app.core.memory_inputs import (
    normalize_client_request_id,
    normalize_content,
    normalize_evidence_refs,
    normalize_reason,
    normalize_tags,
    normalize_task_context,
    normalize_title,
)


@dataclass(frozen=True)
class MemoryEmbeddingJob:
    item: MemoryRecord
    revision: int


_AGENT_TOKEN_RE = re.compile(r"^snm_([^.]+)\.(.+)$")
_TOKEN_TOUCH_SECONDS = 300


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _is_expired(expires_at: str | None, now: str) -> bool:
    if not expires_at:
        return False
    try:
        expiry = _parse_time(expires_at)
        current = _parse_time(now)
    except (TypeError, ValueError):
        return True
    if (
        expiry.tzinfo is None
        or expiry.utcoffset() is None
        or current.tzinfo is None
        or current.utcoffset() is None
    ):
        return True
    return expiry.astimezone(timezone.utc) <= current.astimezone(timezone.utc)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _normalize_expiry(value: str) -> str:
    try:
        parsed = _parse_time(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("expires_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("expires_at must include a timezone offset")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


class _TransferRejected(ValueError):
    """PR review round 8 P2-A: a per-item ``MemoryService.transfer`` rejection
    that carries a machine-readable ``error_code`` alongside its human
    message, so the frontend can route THIS ONE rejection to its own
    branded, non-retryable notice instead of the generic "N 条失败，请稍后
    重试" bucket every other exception ``transfer()``'s outer except handler
    also catches falls into (that generic message is actively wrong here:
    retrying a move blocked on an active promotion proposal can never
    succeed until the proposal is resolved in the approval center).

    A thin ``ValueError`` subclass, not a new taxonomy — every existing
    behavior that treats this as a plain ``ValueError`` (the outer except
    below, ``str(exc)`` for the ``error`` field) keeps working completely
    unchanged; only the new ``error_code`` attribute is additive, read via
    ``getattr(exc, "error_code", None)`` so every OTHER exception type
    that same handler catches naturally yields ``None``."""

    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


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
        kg_ingest_scheduler: Callable[[Callable[[tuple[str, str]], None], tuple[str, str]], Any] | None = None,
    ) -> None:
        self.store = store
        self.ask_state = ask_state
        self.notebooks = notebooks
        self.embedder = embedder
        self.event_log = event_log
        self.new_id = new_id
        self.now = now
        self.embedding_scheduler = embedding_scheduler or (lambda fn, item: fn(item))
        self.kg_ingest_scheduler = kg_ingest_scheduler or (lambda fn, item: fn(item))
        self.promotion_service: Any | None = None
        self.memory_kg: Any | None = None

    def set_promotion_service(self, service: Any) -> None:
        self.promotion_service = service

    def set_memory_kg_service(self, service: Any) -> None:
        self.memory_kg = service

    def _maybe_schedule_kg(self, item: MemoryRecord, extract_kg: bool) -> None:
        if (
            self.memory_kg is None
            or not extract_kg
            or not self.memory_kg.memory_kg_eligible(item.notebook_id)
        ):
            return
        self.kg_ingest_scheduler(self._kg_ingest_job, (item.id, item.created_by))

    def _kg_ingest_job(self, key: tuple[str, str]) -> None:
        # Task 2's ingest_memory_source emits its own precise memory_kg
        # events (extracted/failed); this job adds no duplicate event.
        memory_id, user_id = key
        try:
            item = self.store.memory_for_user(memory_id, user_id)
            if item.status != "confirmed":
                return  # deprecated/rejected before we ran: nothing created
            self.memory_kg.ingest_memory_source(
                item.notebook_id, item.id, item.title, item.content_md
            )
        except KeyError:
            # Memory gone/cross-owner, or ingest's documented notebook-
            # not-found guard (raised BEFORE any insert): nothing to clean up.
            return
        # A concurrent deprecate OR a full cross-notebook MOVE can land WHILE
        # the (seconds-long) ingest above runs. deprecate: its own
        # remove_memory_source was a no-op — the derived source did not exist
        # yet — and confirmed->deprecated is one-way, so it can never fire
        # again. move: it tears down that same not-yet-existing source (also
        # a no-op) and then DELETES the memory row outright — so this
        # recheck itself can now raise KeyError instead of merely observing
        # a non-confirmed status. Either way, the source ingest just
        # (re)built above must not survive as an orphan: only a still-
        # confirmed memory means there is genuinely nothing to clean up. This
        # KeyError is NOT the pre-ingest "nothing created yet" case above —
        # ingest already ran — so it must trigger cleanup, not a silent return.
        try:
            still_confirmed = (
                self.store.memory_for_user(memory_id, user_id).status == "confirmed"
            )
        except KeyError:
            still_confirmed = False
        if not still_confirmed:
            self.memory_kg.remove_memory_source(memory_id)

    @staticmethod
    def _promotion_candidates(item: MemoryRecord) -> list[dict[str, Any]]:
        """Derive review candidates from shareable Memory text, never provenance."""
        candidates: list[dict[str, Any]] = []
        seen_concepts: set[str] = set()
        for raw_tag in item.tags[:8]:
            name = str(raw_tag).strip()
            normalized = name.casefold()
            if name and normalized not in seen_concepts:
                seen_concepts.add(normalized)
                candidates.append(
                    {"object_type": "concept", "payload": {"name": name}}
                )

        candidates.append(
            {
                "object_type": "claim",
                "payload": {
                    "name": item.title.strip() or item.content_md.strip()[:120],
                    "statement": item.content_md.strip(),
                },
            }
        )

        formula_matches = re.findall(
            r"\$\$(.+?)\$\$|\\\[(.+?)\\\]|(?<!\$)\$([^\n$]+)\$(?!\$)",
            item.content_md,
            flags=re.DOTALL,
        )
        formulas = [next(part for part in match if part).strip() for match in formula_matches]
        for index, expression in enumerate(formulas[:4], start=1):
            candidates.append(
                {
                    "object_type": "formula",
                    "payload": {
                        "name": item.title if len(formulas) == 1 else f"{item.title} {index}",
                        "expression": expression,
                    },
                }
            )

        steps = [
            match.group(1).strip()
            for line in item.content_md.splitlines()
            if (match := re.match(r"^\s*(?:\d+[.)]|[-*])\s+(.+?)\s*$", line))
        ]
        if len(steps) >= 2:
            candidates.append(
                {
                    "object_type": "procedure",
                    "payload": {"name": item.title, "steps": steps[:20]},
                }
            )
        return candidates

    def propose_promotion(
        self, memory_id: str, user_id: str, *, target_base_id: str = ""
    ) -> dict:
        item = self.get(memory_id, user_id)
        if item.status != "confirmed":
            raise ValueError("only confirmed Memory can be promoted")
        if item.promotion_state not in {"none", "proposed"}:
            raise ValueError(f"Memory promotion is already {item.promotion_state}")
        if self.promotion_service is None:
            raise RuntimeError("Memory promotion service is not wired")
        return self.promotion_service.propose_memory_promotion(
            item, self._promotion_candidates(item), user_id,
            target_base_id=target_base_id,
        )

    def create_agent_profile(
        self, owner_id: str, name: str, description: str = ""
    ) -> AgentProfile:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("profile name is required")
        return self.store.create_agent_profile(
            owner_id, clean_name, description.strip()
        )

    def list_agent_profiles(
        self, owner_id: str, offset: int = 0, limit: int = 100
    ) -> list[AgentProfile]:
        return self.store.list_agent_profiles(owner_id, offset, limit)

    def update_agent_profile(
        self,
        profile_id: str,
        owner_id: str,
        patch: Mapping[str, Any] | Any,
    ) -> AgentProfile:
        values = (
            patch.model_dump(exclude_none=True)
            if hasattr(patch, "model_dump")
            else dict(patch)
        )
        unknown = set(values) - {"name", "description", "status"}
        if unknown:
            raise ValueError(f"unsupported profile fields: {sorted(unknown)}")
        if "name" in values:
            values["name"] = str(values["name"]).strip()
            if not values["name"]:
                raise ValueError("profile name is required")
        if "description" in values:
            values["description"] = str(values["description"]).strip()
        if values.get("status") not in {None, "active", "revoked"}:
            raise ValueError("invalid profile status")
        return self.store.update_agent_profile(profile_id, owner_id, values)

    def issue_agent_token(
        self,
        owner_id: str,
        agent_profile_id: str,
        scopes: Sequence[str],
        default_notebook_id: str,
        notebook_ids: Sequence[str],
        expires_at: str | None,
    ) -> AgentTokenIssued:
        clean_scopes = list(dict.fromkeys(str(scope) for scope in scopes))
        if not clean_scopes:
            raise ValueError("at least one scope is required")
        invalid = set(clean_scopes) - AGENT_SCOPES
        if invalid:
            raise ValueError(f"unsupported agent scopes: {sorted(invalid)}")
        clean_notebooks = list(
            dict.fromkeys(str(notebook_id) for notebook_id in notebook_ids if notebook_id)
        )
        if not clean_notebooks:
            clean_notebooks = [default_notebook_id]
        if not default_notebook_id or default_notebook_id not in clean_notebooks:
            raise ValueError("default notebook must be in notebook allowlist")
        if expires_at:
            expires_at = _normalize_expiry(expires_at)
        for notebook_id in clean_notebooks:
            if not self.notebooks.user_can_read_notebook(notebook_id, owner_id):
                raise PermissionError(notebook_id)
        token_id = self.new_id("token")
        raw_token = f"snm_{token_id}.{secrets.token_urlsafe(32)}"
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        summary = self.store.create_agent_token(
            token_id,
            owner_id,
            agent_profile_id,
            token_hash,
            clean_scopes,
            default_notebook_id,
            clean_notebooks,
            expires_at,
        )
        return AgentTokenIssued(
            id=summary.id,
            token=raw_token,
            agent_profile_id=summary.agent_profile_id,
            scopes=summary.scopes,
            default_notebook_id=summary.default_notebook_id,
            notebook_ids=summary.notebook_ids,
            expires_at=summary.expires_at,
            created_at=summary.created_at,
        )

    def list_agent_tokens(
        self, owner_id: str, offset: int = 0, limit: int = 100
    ) -> list[AgentTokenSummary]:
        return self.store.list_agent_tokens(owner_id, offset, limit)

    def revoke_agent_token(
        self, owner_id: str, token_id: str
    ) -> AgentTokenSummary:
        return self.store.revoke_agent_token(token_id, owner_id)

    def resolve_agent_token(self, raw_token: str) -> AgentPrincipal | None:
        match = _AGENT_TOKEN_RE.fullmatch(raw_token or "")
        if match is None:
            return None
        token_id = match.group(1)
        row = self.store.agent_token_auth_row(token_id)
        supplied_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        expected_hash = str(row["token_hash"]) if row else "0" * 64
        if not hmac.compare_digest(supplied_hash, expected_hash) or row is None:
            return None
        principal = self._principal_from_auth_row(token_id, row)
        if principal is None:
            return None
        now = _utc_now()
        current = _parse_time(now)
        touch_before = (
            current - timedelta(seconds=_TOKEN_TOUCH_SECONDS)
        ).isoformat().replace("+00:00", "Z")
        self.store.touch_agent_token(token_id, now, touch_before)
        return principal

    def refresh_agent_principal(self, token_id: str) -> AgentPrincipal | None:
        """Refresh live state for an already bearer-authenticated token id.

        This does not authenticate a raw credential. It is only for a transport
        that has already verified and bound the opaque token to its session.
        """
        return self._principal_from_auth_row(
            token_id, self.store.agent_token_auth_row(token_id)
        )

    def _principal_from_auth_row(
        self, token_id: str, row: Mapping[str, Any] | None
    ) -> AgentPrincipal | None:
        now = _utc_now()
        if (
            row is None
            or row["profile_status"] != "active"
            or row["revoked_at"] is not None
            or _is_expired(row["expires_at"], now)
        ):
            return None
        return AgentPrincipal(
            profile_id=row["agent_profile_id"],
            profile_name=row["profile_name"],
            owner_id=row["owner_id"],
            scopes=[
                str(scope) for scope in json.loads(row["scopes_json"] or "[]")
            ],
            default_notebook_id=row["default_notebook_id"],
            notebook_ids=list(row["notebook_ids"]),
            token_id=token_id,
        )

    def require_agent_access(
        self, principal: AgentPrincipal, scope: str, notebook_id: str
    ) -> None:
        row = self.store.agent_token_auth_row(principal.token_id)
        if (
            row is None
            or row["owner_id"] != principal.owner_id
            or row["agent_profile_id"] != principal.profile_id
            or row["profile_status"] != "active"
            or row["revoked_at"] is not None
            or _is_expired(row["expires_at"], _utc_now())
        ):
            raise PermissionError(notebook_id)
        current_scopes = {
            str(item) for item in json.loads(row["scopes_json"] or "[]")
        }
        current_notebooks = set(row["notebook_ids"])
        if (
            scope not in current_scopes
            or notebook_id not in current_notebooks
            or not self.notebooks.user_can_read_notebook(
                notebook_id, principal.owner_id
            )
        ):
            raise PermissionError(notebook_id)

    @staticmethod
    def _patch(patch: MemoryUpdate | Mapping[str, Any] | None) -> tuple[dict, str]:
        if patch is None:
            return {}, ""
        if hasattr(patch, "model_dump"):
            values = patch.model_dump(exclude_none=True)
        else:
            values = dict(patch)
        reason = normalize_reason(values.pop("reason", "") or "")
        values.pop("extract_kg", None)
        unknown = set(values) - {"title", "content_md", "tags"}
        if unknown:
            raise ValueError(f"unsupported memory fields: {sorted(unknown)}")
        if "title" in values:
            values["title"] = normalize_title(values["title"])
        if "content_md" in values:
            values["content_md"] = normalize_content(values["content_md"])
        if "tags" in values:
            values["tags"] = normalize_tags(values["tags"])
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
        title = normalize_title(title)
        content_md = normalize_content(content_md)
        tags = normalize_tags(tags)
        reason = normalize_reason(reason)
        client_request_id = normalize_client_request_id(client_request_id)
        task_context = normalize_task_context(task_context)
        evidence_refs = normalize_evidence_refs(evidence_refs)
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
                "task_context": task_context,
                "evidence_refs": evidence_refs,
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
        extract_kg: bool = True,
    ) -> MemoryRecord:
        title = normalize_title(title)
        content_md = normalize_content(content_md)
        tags = normalize_tags(tags)
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
        self._maybe_schedule_kg(item, extract_kg)
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
        if item.status != "confirmed":
            return item
        if (
            self.memory_kg is not None
            and self.memory_kg.memory_source_id(item.id) is not None
            and self.memory_kg.memory_kg_eligible(item.notebook_id)
        ):
            # Re-extract edited content, but only while the notebook is still
            # eligible: if it became base (or KG was turned off) after the
            # source was created, "base never auto-extracts" bars re-ingest —
            # the stale derived source is left untouched until a rebuild.
            self.kg_ingest_scheduler(self._kg_ingest_job, (item.id, item.created_by))
        return self._schedule_embed(item)

    def confirm(
        self,
        memory_id: str,
        user_id: str,
        patch: MemoryUpdate | Mapping[str, Any] | None = None,
    ) -> MemoryRecord:
        # dict-or-model read, mirroring _patch's normalization (the signature
        # accepts a plain Mapping, where getattr would be blind).
        extract_kg = (
            patch.get("extract_kg")
            if isinstance(patch, Mapping)
            else getattr(patch, "extract_kg", None)
        )
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
        self._maybe_schedule_kg(item, extract_kg is not False)
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
        if self.memory_kg is not None:
            self.memory_kg.remove_memory_source(item.id)
        return item

    def delete(self, memory_id: str, user_id: str) -> None:
        item = self.get(memory_id, user_id)
        self.store.delete_memory(memory_id, user_id)
        self._event("memory_lifecycle", item, action="deleted")

    def bulk_delete(self, user_id: str, memory_ids: Sequence[str]) -> int:
        return self.store.bulk_delete_memories(user_id, memory_ids)

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

    def transfer(
        self,
        user_id: str,
        memory_ids: "Sequence[str]",
        target_notebook_id: str,
        mode: str,
        extract_kg: bool = True,
    ) -> list[dict]:
        if mode not in {"copy", "move"}:
            raise ValueError(f"unknown transfer mode: {mode}")
        # 目标必须当前用户 owner（memory 私有；两端都是我）
        if not self.notebooks.user_can_access_notebook(target_notebook_id, user_id):
            raise PermissionError(target_notebook_id)
        results: list[dict] = []
        for memory_id in memory_ids:
            try:
                source = self.store.memory_for_user(memory_id, user_id)
                if source.notebook_id == target_notebook_id:
                    raise ValueError("源与目标不能是同一个 notebook")
                if source.status != "confirmed":
                    raise ValueError("只能传输 confirmed 状态的 memory")
                # P1-2（PR review round 5，与下面紧邻的 round 3 P1-2 是两件不同
                # 的事，编号撞了纯属巧合）：move 专属的一道新校验，挡在
                # promotion_state == "proposed" 的 memory 前面。这条 memory 在
                # Track-F 基础知识库审批队列里有一行活的 promotion_candidates
                # （governance_store.insert_promotion_candidate 写
                # object_id=item.id, object_type='memory'；knowledge_
                # governance.py 的 propose_memory_promotion 调用链确认了这一
                # 点）。那张表没有指向 memory_items 的外键（migrations.py 的
                # DDL：object_id 只是 TEXT NOT NULL，没有 REFERENCES）——move
                # 删掉这行 memory 之后，那条 promotion_candidates 行会变成孤儿：
                # 继续躺在 admin 的审批队列里，approve 时因为 memory_id 查不到
                # 而失败（memory_store.promotion_rows_on 的 SELECT 找不到行），
                # 且没有任何 UI 路径能清理它。
                #
                # 只挡 'proposed'，不挡 'none'/'approved'/'rejected'——状态
                # 名先核实过，不是猜的：memory_items.promotion_state 自己的
                # CHECK 约束只有这四个取值（migrations.py），且没有任何写路径
                # 会把 'under_review' 写进这一列（那是 promotion_candidates.
                # status 自己独有的中间态——governance_store.
                # active_promotion_for_object 判"是否有活跃候选"用的是
                # `status NOT IN ('approved','rejected')`，proposed/
                # under_review 两个候选状态都算活跃，但 memory 行侧只会看到
                # 'proposed'，因为没有代码把 under_review 同步下来）。换句话
                # 说，从这条 memory 自己的字段看，'proposed' 已经覆盖了
                # promotion_candidates 侧全部「活跃、没有外键、删了就变孤儿」
                # 的状态，不存在一个 under_review 候选能靠某种 memory 侧状态
                # 蒙混过关。'approved' 刻意放行：那一刻 knowledge_objects 里
                # 已经落了一份独立存在的 base KG 对象（governance_store.
                # approve_memory_promotion_in_transaction），不再依赖这条
                # memory 行本身存活，删掉它不会孤立任何东西。
                #
                # 只挡 move：copy 的源分毫不动，promotion_candidates 那行还
                # 好好指着原地不动的源，继续可审批；新副本是全新 memory，
                # provenance 走 imported_from 单独嵌套（见下面的注释），天然
                # promotion_state='none'，不会被这条判据碰到。
                #
                # 位置选在这里（早于下面 create_copy_with_initial_revision
                # 建副本）：这是个纯拒绝分支，不涉及任何并发时序判据，越早
                # 挡掉越好——不需要等到收尾阶段才发现，也不会有副作用需要撤销
                # （下面还没有任何写发生）。抛出的 ValueError 落进本方法既有的
                # 宽 except（下面第二段/循环体外层）转成
                # status="failed"/ok=False——复用现成的错误上报路径，不发明
                # 新的结果形状。
                if mode == "move" and source.promotion_state == "proposed":
                    # PR review round 8 P2-A：_TransferRejected（普通
                    # ValueError 的薄子类）带上 error_code="promotion_proposed"
                    # ——前端靠它把这一条从"N 条失败，请稍后重试"的通用兜底里
                    # 摘出来，单独给一条"去待确认中心处理提案"的可操作提示
                    # （重试对这一条永远无效，通用文案在这里是误导）。
                    raise _TransferRejected(
                        "该 Memory 正在提升到公共知识库的审批队列中，无法移动；"
                        "请先在审批中心处理该提案后再移动",
                        error_code="promotion_proposed",
                    )
                # P1-2（PR review round 3）：move 收尾用的并发编辑判据在这里、
                # 尽量早地捕获——不能晚于下面 create_copy_with_initial_
                # revision 用 source 建副本那一步。位置理由和 knowhow 那边
                # move_table 捕获 source_fingerprint 一致（越早越好：即便并
                # 发编辑恰好卡在这次捕获和下面建副本之间，副本拷到的也已经
                # 是编辑后的新内容，届时收尾那次复核顶多多算一次"变了"，误
                # 判进 copied_source_not_removed——安全的方向，不会把这次
                # 误判成"没变"而删掉新内容）。embedding_revision 是这个文件
                # 既有的乐观并发原语（_schedule_embed 同一模式）；source 刚
                # 读出来，字段必然仍然匹配，取到的就是它此刻的 revision 号
                # ——留给下面 mode == "move" 分支的原子删除做版本判据。
                #
                # PR review round 7 P1-A：这个同一个 revision 号现在还多担一
                # 份职责——原样传进下面的 create_copy_with_initial_revision，
                # 让它在自己的事务内核对「源此刻的 embedding_status=='ready'
                # 且 revision 与这里捕获的一致」才把向量拷给副本并标 ready。
                # 不发明第二个判据：这里已经是「拷贝这一刻，source 的内容对应
                # 哪个 revision」的唯一诚实答案，删源判据和拷向量判据没有理由
                # 用两个不同的数字。
                source_revision = self.store.embedding_revision(source.id, source)
                now = self.now()
                provenance = {
                    "imported_from": {
                        "notebook_id": source.notebook_id,
                        "memory_id": source.id,
                        "action": mode,
                        # 源 provenance 原样留档，但**必须**嵌在 imported_from
                        # 之下、绝不铺到顶层。两个理由缺一不可：
                        # ① 不能丢：ask_answer 出身的 memory，它的 answer_id/
                        #    question/citations/evidence_level 正是当初 confirm
                        #    它的依据。整份换掉会得到一条 status='confirmed'、
                        #    却不带任何佐证的 memory；move 还会删源 → 永久丢失。
                        # ② 不能铺到顶层：里面的 anchors/citations 指向的是**源**
                        #    notebook 的行，在目标 notebook 里根本解析不了。顶层
                        #    的键会被下游当作本 notebook 的活引用去渲染/跳转，
                        #    嵌一层才把语义钉死成"它在别处的来历存档"。
                        "source_provenance": dict(source.provenance or {}),
                    }
                }
                write = MemoryWrite(
                    id=self.new_id("memory"),
                    notebook_id=target_notebook_id,
                    created_by=user_id,
                    source_answer_id=None,
                    origin=source.origin,
                    status="confirmed",
                    title=source.title,
                    content_md=source.content_md,
                    tags=list(source.tags),
                    confirmed_by=user_id,
                    confirmed_at=now,
                    created_at=now,
                    updated_at=now,
                    provenance=provenance,
                )
                copied = self.store.create_copy_with_initial_revision(
                    write, source.id, user_id, f"从 {source.notebook_id} {mode}",
                    source_revision,
                )
                if copied.id != write.id:
                    # 兜底，防的是数据丢失而不是"不好看"：store 在插入撞唯一键时
                    # 会返回**已存在的那一行**（幂等语义）。真撞上了就意味着副本
                    # 根本没建成，而 copied 很可能就是源自己——下面 move 那步会把
                    # 它删掉，于是唯一一份数据凭空消失、还报成功。上面强制
                    # source_answer_id=None 已经堵掉了已知的撞键路径
                    # （idx_memory_answer_once），这里再结构性地兜一层：宁可整条
                    # 报失败（new_id=None，删源绝不会执行）。
                    raise ValueError("复制未生成新记录（疑似唯一键冲突），已中止")
                # ↓↓↓ 上一行返回时副本**已经 COMMIT**。从这里到本条目结束，每
                # 一步都是"既成事实之后的收尾"，所以整段收进一个宽 except：
                # 任何一步抛出都既不能让异常冒出 transfer()（会丢掉整批已处理
                # 条目的结果——包括前面几条已经删了源的 move），也不能落进外层
                # 那个 handler 被报成 new_id=None（"什么都没发生"）——那是谎报，
                # 副本就躺在目标 notebook 里。收尾分两类：
                #   ① 派生工作 _event/_maybe_schedule_kg/_schedule_embed。它们
                #      会真的抛：memory_kg_eligible 是两次无保护的 DB 读；
                #      kg_ingest_scheduler→ThreadPoolExecutor.submit 在池关闭/
                #      耗尽时抛 RuntimeError；_schedule_embed 里的
                #      embedding_revision 也在它自身 try 之前。一次成功的复制
                #      不该因为这些尽力而为的派生步骤而变成失败。
                #   ② move 的源清理（顺序见下面 AMENDMENT 1）。
                cleanup_error: str | None = None
                source_removed = False
                # round 10 P1-B（镜像 knowhow 侧 SourceCleanupFailed.reason）：
                # copied_source_not_removed 这个 status 目前把两种截然不同的
                # 成因压成同一句"源未删除"人话——(a) 下面的原子条件删除返回
                # False：源被有意保留下来保护一份并发编辑/状态流转/审批提案，
                # 不是清理失败；(b) 收尾这段真的抛了异常（remove_memory_
                # source/_maybe_schedule_kg/_schedule_embed……）：源确实原封
                # 未动。前端要对这两种给出截然不同的指引——(a) 时絮叨"删除源"
                # 会连带丢失那份被保留下来的编辑，只能是"弃用源"或"重新发起
                # 搬迁"；(b) 时源没有任何变化，弃用/删除都安全。默认
                # "cleanup_error"，只在原子删除真的返回 False 那一刻扳成
                # "source_changed"——和 knowhow 侧同一个"局部变量而非 isinstance
                # 判特定异常类型"的理由：即便扳过之后、真正 raise 之前的收尾
                # （下面重新排队 KG 抽取）自己又抛出别的异常，源确实是因为原子
                # 删除判定"变了"才被保留的，不该因为收尾工作本身失手就误判成
                # "删除安全"的 cleanup_error。
                retain_reason = "cleanup_error"
                try:
                    self._event(
                        "memory_lifecycle", copied, action=f"transfer_{mode}",
                        # 复审 Minor：这条事件此前只记了副本(copied)一侧，日志
                        # 里查不到"某条 memory 离开了源 notebook"——per-user 事件
                        # 日志是这里唯一的排障面，缺源信息等于缺了一半故事。
                        source_id=source.id, source_notebook_id=source.notebook_id,
                    )
                    self._maybe_schedule_kg(copied, extract_kg)
                    if copied.embedding_status != "ready":
                        self._schedule_embed(copied)
                    if mode == "move":
                        # AMENDMENT 1（来自 knowhow 表移动任务的教训）：先拆派生
                        # KG 源，再删 memory 行——顺序不能颠倒。sources.memory_id
                        # 不是外键，删除不会级联；若先删 memory 行、随后拆源那步
                        # 再失败，就会永久留下一条 memory_id 指向已删行的派生源：
                        # 在源 notebook 里仍被检索到，且没有任何 UI 路径能删它。
                        #
                        # 这个顺序保证的**只是**"没有东西变得不可达/不可回收"：
                        # 无论哪步失败，源 memory 行本身都还在，副本也在，用户看
                        # 得见也删得掉。它不保证无代价——如果 remove_memory_source
                        # 成功而 delete_memory 失败，派生 KG 源已经没了，源
                        # notebook 对这条 memory 的 KG/chunk 检索会一直降级到下次
                        # 重试或重建；而且重试整个 move 会再造一份副本（transfer
                        # 没有幂等键）。宁可留下需要人工对账的重复，也不留不可回收
                        # 的孤儿。
                        #
                        # P1-3（PR review round 2，数据丢失）：source 是这个循环
                        # 顶部读的一份快照，写副本用的就是它——从那一刻到这里之
                        # 间，用户完全可能编辑了标题/正文，或把它 deprecate 掉。
                        # 目标侧那份副本已经带着旧快照定死在库里；这里若照旧删
                        # 源，并发那次改动就随源一起永久消失（旧内容还可能反过
                        # 来配上并发改动触发的新向量，对不上号）。
                        #
                        # P1-2（PR review round 3：check-then-act 仍是竞态）：
                        # round 2 在这里加了一次 embedding_revision 复核，但复核
                        # 和真正的 delete_memory 是两条独立语句，中间还隔着
                        # remove_memory_source 这次真实的 DB 写——并发编辑完全
                        # 可能恰好卡在「复核通过」之后、「删源行」之前，这次复
                        # 核对它完全失明，行还是会被无条件删掉，编辑照样永久
                        # 丢失，且不会有任何异常：ok=True、status="moved"，悄
                        # 悄把新内容连同整条 memory 一起吞掉。
                        # delete_memory_if_unchanged 把「复核」和「删」折进
                        # DELETE 语句自己的 WHERE 子句——同一条 SQL 语句下，
                        # status='confirmed' AND revision 匹配要么一次性都满
                        # 足、要么整条语句零行生效，不存在"复核通过但状态已经
                        # 变了"这种窗口。判据用 source_revision（上面循环顶部
                        # 刚读到 source 时就近捕获的 revision 号），不是
                        # updated_at：最初试的就是 updated_at，但
                        # app.services.sqlite_repository._now 把时间戳截到整
                        # 秒（`datetime.now().replace(microsecond=0)`）——
                        # "副本提交"和"并发编辑落地"这两件事完全可能落在同一
                        # 秒内（这里的测试就在同一进程里背靠背发生），届时新
                        # 旧 updated_at 字符串相同，检查会把已经改过的行误判
                        # 成"没变"而删掉（本改动开发过程中用这个文件自己的用
                        # 例真实跑出过这个假阴性，才切到 revision）。revision
                        # 不存在这个问题：_mutate_with_revision 的
                        # _append_revision_on 每次调用都严格 +1 插一条新行，
                        # 不管改的是标题/正文/tags 还是仅仅状态流转（deprecate
                        # 走的也是 transition_with_revision），必定推高
                        # MAX(revision)，没有并列的可能。
                        # 返回 False 说明源在这最后一刻仍然对不上——两边都留：
                        # 副本已在目标（create_copy_with_initial_revision 早就
                        # COMMIT 了），源连同它的并发改动原样留在原地（即便上
                        # 一行 remove_memory_source 已经跑过、源的派生 KG 源没
                        # 了，也不是不可回收的孤儿——并发那次 update() 会在源
                        # 仍 eligible 时重新调度 KG 抽取，见 update() 定义）。
                        # 落进上面这个 try 自己的收尾 except：cleanup_error 被
                        # 记录、source_removed 保持 False，下面复用既有的
                        # copied_source_not_removed 结果契约（不发明新形状）
                        # ——两边都留，不丢失。
                        if self.memory_kg is not None:
                            self.memory_kg.remove_memory_source(source.id)
                        if not self.store.delete_memory_if_unchanged(
                            source.id, user_id, source_revision
                        ):
                            # round 10 P1-B：源确凿是因为原子删除的 WHERE 子句
                            # 未命中才被保留的——在任何后续收尾工作（下面的
                            # 重新排队 KG 抽取）有机会自己出错之前，先诚实记
                            # 下这一点。
                            retain_reason = "source_changed"
                            # P2-1（PR review round 4）：remove_memory_source 在
                            # 上一行已经真的把源的 KG 派生源删掉了——不管这次
                            # 原子删除最终判定"变了"保留源 memory 与否，那个
                            # 派生源都已经不在了。保留分支尤其致命：并发那次
                            # update()（正是它让下面这次原子删除判定"变了"）
                            # 自己执行时会去查 memory_source_id(...)，而这一
                            # 刻它已经是 None（就是上一行删的）——于是并发编
                            # 辑自己也会认为"没有派生源可重抽"而放弃重抽（见
                            # update() 自身的 memory_source_id(...) is not
                            # None 门控）。两边都不补，这条 confirmed memory
                            # 就永久从 KG 里消失，直到有人手工操作。
                            #
                            # 这里重新读一次源的最新状态——不是循环顶部那份
                            # 旧快照 source，它的 title/content/status 都可能
                            # 已经被这次并发编辑/状态流转带走——只有它此刻仍
                            # 然是 confirmed（不是被同一窗口内的并发 deprecate
                            # /reject 带走的状态）才重新排队抽取：一次 deprecate
                            # 是刻意让派生源消失的决定，重新排队会把它顶回
                            # 去，那是错的。复用 confirm() 已经在用的同一条
                            # _maybe_schedule_kg/_kg_ingest_job 路径（extract_kg
                            # 固定 True——这里恢复的是源"此刻本就该有"的 KG
                            # 状态，与这次 transfer() 调用方对新副本的抽取偏好
                            # 无关），不发明新机制。
                            try:
                                fresh_source = self.store.memory_for_user(
                                    source.id, user_id
                                )
                                if fresh_source.status == "confirmed":
                                    self._maybe_schedule_kg(fresh_source, True)
                            except KeyError:
                                pass  # 整行都没了：没有什么可重新抽取的
                            # 括注措辞（round 8 P1-B）：delete_memory_if_
                            # unchanged 的 WHERE 子句现在有两类互相独立的判据
                            # 会让它返回 False——revision 不匹配（这条分支原本
                            # 唯一覆盖的场景），或者 promotion_state 恰好在原
                            # 子删除那一刻是 'proposed'（新判据；那种情况下
                            # revision 完全可能对得上，就是这条 bug 本身的成因
                            # ——见 delete_memory_if_unchanged 自己的文档）。
                            # 不再点名"revision 复核未命中"这一个具体机制，
                            # 换成不预设成因的措辞，两类原因下都不失实。
                            raise RuntimeError(
                                "源 memory 在复制完成后被并发编辑、状态变更或"
                                "提交了知识库提升提案（原子删除条件未命中），"
                                "为避免丢失改动或孤立该提案，源未删除"
                            )
                        source_removed = True
                        # P1-C（PR review round 6）：一个在制品的 _kg_ingest_
                        # job 可能恰好在上面这次 remove_memory_source 已经真
                        # 实跑完、delete_memory_if_unchanged 还没落地这个窗口
                        # 里完成自己的 ingest_memory_source——它的 post-ingest
                        # 复检（_kg_ingest_job 自身逻辑）读到的 memory 此刻仍
                        # 是 confirmed（因为上一行的原子删除这一刻还没发生），
                        # 于是它自己判定"still_confirmed=True"、不会调用它自
                        # 己的收尾 remove_memory_source。而上一行的原子删除随
                        # 后按预期成功（这是一次没有并发编辑本体的普通
                        # move，revision 仍然匹配）——memory 行没了，但窗口期
                        # 内刚冒出来的这个派生源（sources.memory_id 指向一条
                        # 已经不存在的 memory）从此不再被任何东西引用，永久
                        # 留在库里、继续参与检索，直到有人手工清理。
                        #
                        # 一行保险：既然 memory 行现在确凿已经删除，再无条件
                        # 补一次 remove_memory_source 总是安全的——它自己的
                        # docstring 承诺"无派生源时幂等 no-op"，普通（无此窗
                        # 口竞态）的 move 会在这里第二次查到 None、直接返回，
                        # 不产生任何额外副作用；只有真撞上这个窗口时才有事可
                        # 删。失败与否都落进本方法既有的收尾 except（下面几
                        # 行）——不发明新的错误上报路径，且不会把已经确凿成
                        # 功的 move 结果改判成失败（source_removed 在这一行
                        # 之前就已置 True）。
                        if self.memory_kg is not None:
                            self.memory_kg.remove_memory_source(source.id)
                except Exception as exc:  # noqa: BLE001 — 见上：副本已提交，收尾
                    # 失败必须逐条上报，不能冒泡、也不能伪装成"没创建"。
                    cleanup_error = str(exc)
                    self._event(
                        "memory_lifecycle",
                        copied,
                        action=f"transfer_{mode}_followup_failed",
                        source_id=source.id,
                        error_type=type(exc).__name__,
                        error=cleanup_error,
                    )
                if mode == "copy":
                    # copy 模式没有"源清理"这一说：收尾失败最多丢掉派生工作
                    # （KG 抽取/嵌入调度），副本本身完整可用，仍算成功；失败细节
                    # 已进事件日志（与 _embed 标记 embedding 失败的既有惯例一致）。
                    outcome, ok, error, result_error_code = "copied", True, None, None
                elif source_removed:
                    outcome, ok, error, result_error_code = "moved", True, None, None
                else:
                    # move 承诺了"源会消失"，而它没消失——不能报成功。
                    outcome, ok = "copied_source_not_removed", False
                    error = f"复制已成功，但源未删除：{cleanup_error}"
                    # round 10 P1-B：这条 status 现在把 retain_reason 原样带出
                    # 去——"source_changed"（源保留是有意为之，前端绝不能引导
                    # 删除）还是 "cleanup_error"（源确实原封未动，删除/弃用都
                    # 安全）。ok=True 的两条分支上面固定 None：error 为空时
                    # error_code 不该非空（同 round 8 P2-A 定的那条不变量）。
                    result_error_code = retain_reason
                results.append(
                    {
                        "source_id": memory_id,
                        "new_id": copied.id,
                        "ok": ok,
                        "error": error,
                        "error_code": result_error_code,
                        "status": outcome,
                    }
                )
            except Exception as exc:  # noqa: BLE001 — 同下方收尾 except 一样的
                # 理由：这段循环体前半段（读源/校验/写副本）任何异常逃出去都会
                # 中止整个 for 循环——不只丢当前条目，连它之前已经成功、甚至
                # 已经删了源的 move 条目的结果也会被一起吞掉（transfer() 直接
                # 向上抛出，调用方连 results 都拿不到）。真实触发面不止
                # KeyError/ValueError：create_copy_with_initial_revision 在并发
                # 写者下可能吃到 sqlite3.OperationalError("database is locked")
                # （比如背景 KG/embed 任务同时在写），必须和下面的收尾 except
                # 一样宽，否则这一条会变成裸 500 而不是 per-item failed。
                results.append(
                    {
                        "source_id": memory_id,
                        "new_id": None,
                        "ok": False,
                        "error": str(exc),
                        # round 8 P2-A：只有 _TransferRejected 这一个异常类型
                        # 携带 error_code——这段 except 捕获的其它一切（普通
                        # ValueError 校验失败、并发 OperationalError……）都没
                        # 有这个属性，getattr 的默认值让它们诚实地留 None，
                        # 不是发明一个"未分类"的假 code。
                        "error_code": getattr(exc, "error_code", None),
                        "status": "failed",
                    }
                )
        return results
