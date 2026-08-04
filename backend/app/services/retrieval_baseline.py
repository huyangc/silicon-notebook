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
from typing import Any, Iterable, Sequence


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


def _refs(kind: str, items: Iterable[Any]) -> tuple[BaselineEvidenceRef, ...]:
    from app.services.retrieval import est_tokens

    out = []
    for item in list(items or []):
        item_id = _item_id(kind, item)
        if not item_id:
            continue
        text = _item_text(kind, item)
        out.append(BaselineEvidenceRef(
            kind=kind,
            item_id=item_id,
            score=_number(getattr(item, "score", 0.0)),
            relevance=_number(getattr(item, "relevance", 0.0)),
            content_hash=_digest(text),
            token_count=est_tokens(text),
            citation_handles=_citation_handles(kind, item),
        ))
    return tuple(out)


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
    try:
        return RetrievalBaselineManifest(
            version=_MANIFEST_VERSION,
            mode=str(mode),
            scope_hash=_digest(scope),
            query_hash=_digest(str(query)),
            settings_fingerprint=_digest(_settings_signature(settings)),
            candidate_knowledge=_refs("knowledge", candidate_knowledge),
            candidate_chunks=_refs("chunk", candidate_chunks),
            candidate_elements=_refs("element", candidate_elements),
            selected_knowledge=_refs("knowledge", selected_knowledge),
            selected_chunks=_refs("chunk", selected_chunks),
            selected_elements=_refs("element", selected_elements),
            baseline_step_usage=max(0, int(baseline_step_usage)),
        )
    except Exception:
        # This is an observability scaffold.  A malformed legacy payload or an
        # unexpected test double must not turn a previously successful answer
        # into an exception merely because manifest capture was enabled.
        return None


def emit_retrieval_baseline(
    event_log: Any,
    manifest: RetrievalBaselineManifest | None,
    notebook_id: str,
    *,
    site: str,
) -> None:
    """Emit diagnostics fail-soft; observability must never affect an answer."""
    if manifest is None or event_log is None:
        return
    try:
        event_log.emit(manifest.event_payload(notebook_id, site=site))
    except Exception:
        logger = getattr(event_log, "logger", None)
        if logger is not None:
            try:
                logger.exception(
                    "selected-source baseline manifest emit failed at %s", site
                )
            except Exception:
                pass
