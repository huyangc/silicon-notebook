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
from app.models.memory import MemoryRevision, MemoryWrite
from app.models.schemas import (
    AgentPrincipal,
    AgentProfile,
    AgentTokenIssued,
    AgentTokenSummary,
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
from app.services.memory_inputs import (
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


AGENT_SCOPES = frozenset(
    {
        "knowledge:read",
        "memory:read",
        "memory:read_candidates",
        "memory:propose",
        "ask:execute",
        # PR-2+3 Task 10: knowhow-tables agent surface — writes a cell-level
        # code attachment (design doc §⑥-4). Reads of the same surface use
        # the pre-existing "knowledge:read" scope; this scope gates ONLY
        # PUT/DELETE .../cells/{col}/code, never a read.
        "knowhow:code",
    }
)
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

    def propose_promotion(self, memory_id: str, user_id: str) -> dict:
        item = self.get(memory_id, user_id)
        if item.status != "confirmed":
            raise ValueError("only confirmed Memory can be promoted")
        if item.promotion_state not in {"none", "proposed"}:
            raise ValueError(f"Memory promotion is already {item.promotion_state}")
        if self.promotion_service is None:
            raise RuntimeError("Memory promotion service is not wired")
        return self.promotion_service.propose_memory_promotion(
            item, self._promotion_candidates(item), user_id
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
                    write, source.id, user_id, f"从 {source.notebook_id} {mode}"
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
                        if self.memory_kg is not None:
                            self.memory_kg.remove_memory_source(source.id)
                        self.store.delete_memory(source.id, user_id)
                        source_removed = True
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
                    outcome, ok, error = "copied", True, None
                elif source_removed:
                    outcome, ok, error = "moved", True, None
                else:
                    # move 承诺了"源会消失"，而它没消失——不能报成功。
                    outcome, ok = "copied_source_not_removed", False
                    error = f"复制已成功，但源未删除：{cleanup_error}"
                results.append(
                    {
                        "source_id": memory_id,
                        "new_id": copied.id,
                        "ok": ok,
                        "error": error,
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
                        "status": "failed",
                    }
                )
        return results
