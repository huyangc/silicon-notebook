"""Deterministic, content-safe manifests for selected-source retrieval.

The source-subgraph rollout must prove that graph enrichment never removes or
reorders the direct evidence a selected-source request already receives.  This
module captures that direct lane without changing retrieval, issuing I/O, or
putting evidence text into logs.

Full item ids and content hashes stay in the request-local manifest so tests and
future shadow comparison can detect drift.  ``event_payload`` deliberately
emits only hashes and counts: neither the query, source ids, evidence ids, nor
evidence text is written to the operational log.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


_MANIFEST_VERSION = 1
_SETTINGS_KEYS = (
    "retrieval_top_n",
    "chunk_recall",
    "chunk_mmr_k",
    "chunk_mmr_lambda",
    "chunk_graph_reserve",
    "exact_section_reserve",
    "max_total_tokens",
    "max_entity_tokens",
    "max_relation_tokens",
    "reasoning_top_n_per_query",
    "reasoning_top_n_cap",
    "reasoning_quota_enabled",
    "reasoning_max_steps",
    "reasoning_max_subqueries",
)


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _evidence_rows(item: Any) -> list[dict[str, Any]]:
    rows = []
    for evidence in list(getattr(item, "evidence", None) or []):
        if isinstance(evidence, dict):
            get = evidence.get
        else:
            get = lambda key, default="": getattr(evidence, key, default)
        rows.append({
            "source_id": str(get("source_id", "") or ""),
            "element_id": str(get("element_id", "") or ""),
            "location_label": str(get("location_label", "") or ""),
            "quoted_span": str(get("quoted_span", "") or ""),
        })
    return rows


def _item_id(kind: str, item: Any) -> str:
    field = {
        "knowledge": "object_id",
        "chunk": "chunk_id",
        "element": "element_id",
    }[kind]
    return str(getattr(item, field, "") or "")


def _item_text(kind: str, item: Any) -> str:
    if kind == "knowledge":
        return _canonical({
            "payload": getattr(item, "payload", None) or {},
            "evidence": _evidence_rows(item),
        })
    return str(getattr(item, "text", "") or "")


def _citation_handles(kind: str, item: Any) -> tuple[str, ...]:
    if kind == "knowledge":
        values = [row["element_id"] for row in _evidence_rows(item)]
    elif kind == "chunk":
        values = list(getattr(item, "element_ids", None) or [])
    else:
        values = [getattr(item, "element_id", "")]
    return tuple(dict.fromkeys(str(value) for value in values if str(value)))


@dataclass(frozen=True)
class BaselineEvidenceRef:
    kind: str
    item_id: str
    score: float
    relevance: float
    content_hash: str
    token_count: int
    citation_handles: tuple[str, ...]

    def signature(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "item_id": self.item_id,
            "score": self.score,
            "relevance": self.relevance,
            "content_hash": self.content_hash,
            "token_count": self.token_count,
            "citation_handles": list(self.citation_handles),
        }


def _refs(
    kind: str,
    items: Iterable[Any],
    *,
    handles_by_item: Mapping[str, tuple[str, ...]] | None = None,
) -> tuple[tuple[BaselineEvidenceRef, ...], int]:
    from app.services.retrieval import est_tokens

    out = []
    errors = 0
    for item in list(items or []):
        if isinstance(item, BaselineEvidenceRef):
            out.append(item)
            continue
        try:
            item_id = _item_id(kind, item)
            if not item_id:
                errors += 1
                continue
            text = _item_text(kind, item)
            out.append(BaselineEvidenceRef(
                kind=kind,
                item_id=item_id,
                score=_number(getattr(item, "score", 0.0)),
                relevance=_number(getattr(item, "relevance", 0.0)),
                content_hash=_digest(text),
                token_count=est_tokens(text),
                citation_handles=(
                    (handles_by_item or {}).get(item_id)
                    or _citation_handles(kind, item)
                ),
            ))
        except Exception:
            errors += 1
    return tuple(out), errors


def merge_baseline_candidates(
    kind: str,
    prior: Sequence[Any],
    additional: Sequence[Any],
) -> list[Any]:
    """Stable candidate union across retrieval and post-retrieval directions."""
    out: list[Any] = []
    seen: set[str] = set()
    for value in (*tuple(prior or ()), *tuple(additional or ())):
        try:
            item_id = (
                value.item_id
                if isinstance(value, BaselineEvidenceRef)
                else _item_id(kind, value)
            )
        except Exception:
            # Preserve malformed new values so the builder records a partial
            # capture instead of silently laundering the failure away.
            out.append(value)
            continue
        if not item_id:
            out.append(value)
            continue
        if item_id in seen:
            continue
        seen.add(item_id)
        out.append(value)
    return out


@dataclass(frozen=True)
class BaselinePromptAssembly:
    """Exact evidence surface admitted to the synthesis prompt."""

    context_hash: str
    context_chars: int
    context_tokens: int
    budget_chars: int
    citation_handles: tuple[str, ...]
    evidence_order: tuple[tuple[str, str, str], ...]

    def signature(self) -> dict[str, Any]:
        return {
            "context_hash": self.context_hash,
            "context_chars": self.context_chars,
            "context_tokens": self.context_tokens,
            "budget_chars": self.budget_chars,
            "citation_handles": list(self.citation_handles),
            "evidence_order": [list(value) for value in self.evidence_order],
        }


def _prompt_assembly(
    context_block: str,
    id_map: Mapping[str, Any],
    budget_chars: int,
    ordered_handles: Sequence[str],
) -> BaselinePromptAssembly:
    from app.services.retrieval import est_tokens

    # Handles come from the assembler's structured admission map.  Evidence
    # text is intentionally never parsed: user content may itself contain a
    # line such as ``k2:``, which is data rather than an admitted citation.
    handles = tuple(dict.fromkeys(
        str(handle) for handle in ordered_handles if str(handle) in id_map
    ))
    order = []
    for handle in handles:
        value = id_map.get(handle) or {}
        if not isinstance(value, Mapping):
            continue
        order.append((
            handle,
            str(value.get("object_type") or ""),
            str(value.get("object_id") or ""),
        ))
    return BaselinePromptAssembly(
        context_hash=_digest(context_block),
        context_chars=len(context_block),
        context_tokens=est_tokens(context_block),
        budget_chars=max(0, int(budget_chars)),
        citation_handles=handles,
        evidence_order=tuple(order),
    )


@dataclass(frozen=True)
class RetrievalBaselineManifest:
    version: int
    mode: str
    scope_hash: str
    query_hash: str
    settings_fingerprint: str
    candidate_knowledge: tuple[BaselineEvidenceRef, ...]
    candidate_chunks: tuple[BaselineEvidenceRef, ...]
    candidate_elements: tuple[BaselineEvidenceRef, ...]
    selected_knowledge: tuple[BaselineEvidenceRef, ...]
    selected_chunks: tuple[BaselineEvidenceRef, ...]
    selected_elements: tuple[BaselineEvidenceRef, ...]
    prompt: BaselinePromptAssembly | None = None
    capture_status: str = "complete"
    capture_error_count: int = 0
    generation_fingerprint: str = ""
    baseline_step_usage: int = 0

    def _signature(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "mode": self.mode,
            "scope_hash": self.scope_hash,
            "query_hash": self.query_hash,
            "settings_fingerprint": self.settings_fingerprint,
            "candidate_knowledge": [row.signature() for row in self.candidate_knowledge],
            "candidate_chunks": [row.signature() for row in self.candidate_chunks],
            "candidate_elements": [row.signature() for row in self.candidate_elements],
            "selected_knowledge": [row.signature() for row in self.selected_knowledge],
            "selected_chunks": [row.signature() for row in self.selected_chunks],
            "selected_elements": [row.signature() for row in self.selected_elements],
            "prompt": self.prompt.signature() if self.prompt is not None else None,
            "capture_status": self.capture_status,
            "capture_error_count": self.capture_error_count,
            "generation_fingerprint": self.generation_fingerprint,
            "baseline_step_usage": self.baseline_step_usage,
        }

    @property
    def manifest_hash(self) -> str:
        return _digest(self._signature())

    @property
    def selected_token_count(self) -> int:
        return sum(
            row.token_count
            for group in (
                self.selected_knowledge,
                self.selected_chunks,
                self.selected_elements,
            )
            for row in group
        )

    @property
    def citation_handle_count(self) -> int:
        if self.prompt is not None:
            return len(self.prompt.citation_handles)
        return len({
            handle
            for group in (
                self.selected_knowledge,
                self.selected_chunks,
                self.selected_elements,
            )
            for row in group
            for handle in row.citation_handles
        })

    @property
    def comparison_ready(self) -> bool:
        return bool(
            self.capture_status == "complete"
            and self.prompt is not None
            and self.generation_fingerprint
        )

    def event_payload(self, notebook_id: str, *, site: str) -> dict[str, Any]:
        """Return the redacted operational form of this manifest."""
        return {
            "kind": "selected_source_baseline",
            "notebook_id": notebook_id,
            "site": site,
            "mode": self.mode,
            "manifest_version": self.version,
            "manifest_hash": self.manifest_hash,
            "scope_hash": self.scope_hash,
            "settings_fingerprint": self.settings_fingerprint,
            "candidate_knowledge": len(self.candidate_knowledge),
            "candidate_chunks": len(self.candidate_chunks),
            "candidate_elements": len(self.candidate_elements),
            "selected_knowledge": len(self.selected_knowledge),
            "selected_chunks": len(self.selected_chunks),
            "selected_elements": len(self.selected_elements),
            "selected_tokens": self.selected_token_count,
            "citation_handles": self.citation_handle_count,
            "capture_status": self.capture_status,
            "capture_errors": self.capture_error_count,
            "comparison_ready": self.comparison_ready,
            "baseline_steps": self.baseline_step_usage,
        }


def _scope_signature(notebook_id: str) -> dict[str, Any] | None:
    from app.services.source_scope import current_source_scope

    scope = current_source_scope()
    if scope is None or scope.notebook_id != notebook_id or not scope.restricted:
        return None
    return {
        "notebook_id": notebook_id,
        "mode": scope.mode,
        "source_ids": sorted(scope.source_ids),
        "hidden_source_ids": sorted(scope.hidden_source_ids),
        "narrowed": scope.narrowed,
    }


def _settings_signature(settings: Any) -> dict[str, Any]:
    if settings is None:
        return {}
    return {
        key: getattr(settings, key, None)
        for key in _SETTINGS_KEYS
    }


def build_retrieval_baseline_manifest(
    *,
    notebook_id: str,
    query: str,
    mode: str,
    settings: Any = None,
    candidate_knowledge: Sequence[Any] = (),
    candidate_chunks: Sequence[Any] = (),
    candidate_elements: Sequence[Any] = (),
    selected_knowledge: Sequence[Any] = (),
    selected_chunks: Sequence[Any] = (),
    selected_elements: Sequence[Any] = (),
    final_context_block: str | None = None,
    final_id_map: Mapping[str, Any] | None = None,
    final_ordered_handles: Sequence[str] = (),
    final_budget_chars: int = 0,
    generation_fingerprint: str = "",
    prior_manifest: RetrievalBaselineManifest | None = None,
    capture_error_count: int = 0,
    baseline_step_usage: int = 0,
) -> RetrievalBaselineManifest | None:
    """Capture a narrowed request's existing direct retrieval result.

    Returning ``None`` outside a genuinely narrowed source scope keeps the
    whole-scope path inert and prevents this rollout scaffold from changing its
    allocation/logging profile.
    """
    scope = _scope_signature(notebook_id)
    if scope is None:
        return None
    prompt = None
    handles_by_item: dict[str, tuple[str, ...]] = {}
    admitted_ids: set[str] | None = None
    capture_errors = max(0, int(capture_error_count))
    if prior_manifest is not None:
        capture_errors += max(0, int(prior_manifest.capture_error_count))
        if not generation_fingerprint:
            generation_fingerprint = prior_manifest.generation_fingerprint
    if final_context_block is not None and final_id_map is not None:
        try:
            prompt = _prompt_assembly(
                str(final_context_block), final_id_map, final_budget_chars,
                final_ordered_handles,
            )
            admitted_ids = {
                item_id for _handle, _kind, item_id in prompt.evidence_order if item_id
            }
            for handle, _kind, item_id in prompt.evidence_order:
                if item_id:
                    handles_by_item.setdefault(item_id, tuple())
                    handles_by_item[item_id] = (*handles_by_item[item_id], handle)
        except Exception:
            capture_errors += 1

    def selected(values: Sequence[Any], kind: str) -> list[Any]:
        nonlocal capture_errors
        if admitted_ids is None:
            return list(values or [])
        out = []
        for value in list(values or []):
            try:
                if _item_id(kind, value) in admitted_ids:
                    out.append(value)
            except Exception:
                capture_errors += 1
        return out

    candidate_knowledge_refs, errors = _refs("knowledge", candidate_knowledge)
    capture_errors += errors
    candidate_chunk_refs, errors = _refs("chunk", candidate_chunks)
    capture_errors += errors
    candidate_element_refs, errors = _refs("element", candidate_elements)
    capture_errors += errors
    selected_knowledge_refs, errors = _refs(
        "knowledge", selected(selected_knowledge, "knowledge"),
        handles_by_item=handles_by_item,
    )
    capture_errors += errors
    selected_chunk_refs, errors = _refs(
        "chunk", selected(selected_chunks, "chunk"),
        handles_by_item=handles_by_item,
    )
    capture_errors += errors
    selected_element_refs, errors = _refs(
        "element", selected(selected_elements, "element"),
        handles_by_item=handles_by_item,
    )
    capture_errors += errors
    try:
        settings_fingerprint = _digest(_settings_signature(settings))
    except Exception:
        capture_errors += 1
        settings_fingerprint = _digest({"capture_failed": True})
    return RetrievalBaselineManifest(
        version=_MANIFEST_VERSION,
        mode=str(mode),
        scope_hash=_digest(scope),
        query_hash=_digest(str(query)),
        settings_fingerprint=settings_fingerprint,
        candidate_knowledge=candidate_knowledge_refs,
        candidate_chunks=candidate_chunk_refs,
        candidate_elements=candidate_element_refs,
        selected_knowledge=selected_knowledge_refs,
        selected_chunks=selected_chunk_refs,
        selected_elements=selected_element_refs,
        prompt=prompt,
        capture_status="complete" if capture_errors == 0 else "partial",
        capture_error_count=capture_errors,
        generation_fingerprint=str(generation_fingerprint or ""),
        baseline_step_usage=max(0, int(baseline_step_usage)),
    )
