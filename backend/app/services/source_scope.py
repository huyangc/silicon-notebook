"""Request-local imported-source retrieval scope.

The context variable is set by Ask/report orchestration and copied into their
worker threads. Retrieval owners consult it at result boundaries, so the scope
applies consistently to chunk, KG, relation, element, and PPR evidence without
changing persisted scale-index artifacts.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, Iterable, Iterator


@dataclass(frozen=True)
class ActiveSourceScope:
    notebook_id: str
    mode: str
    source_ids: frozenset[str]

    @property
    def restricted(self) -> bool:
        # exclude [] is the UI/default representation of "all local sources"
        # and must remain byte-for-byte compatible with the historical path.
        return self.mode == "include" or bool(self.source_ids)

    def allows(self, notebook_id: str, source_id: str) -> bool:
        # Mounted base libraries are independent participants and are never
        # governed by the active notebook's source checkboxes.
        if notebook_id and notebook_id != self.notebook_id:
            return True
        if not self.restricted:
            return True
        if not source_id:
            return False
        if self.mode == "include":
            return source_id in self.source_ids
        return source_id not in self.source_ids


_CURRENT_SOURCE_SCOPE: ContextVar[ActiveSourceScope | None] = ContextVar(
    "current_source_scope", default=None
)


def _scope_dict(scope: Any) -> dict[str, Any] | None:
    if scope is None:
        return None
    if hasattr(scope, "model_dump"):
        return scope.model_dump()
    return dict(scope)


@contextmanager
def source_scope_context(notebook_id: str, scope: Any) -> Iterator[None]:
    raw = _scope_dict(scope)
    if raw is None:
        yield
        return
    current = ActiveSourceScope(
        notebook_id=notebook_id,
        mode=str(raw.get("mode") or "exclude"),
        source_ids=frozenset(str(value) for value in raw.get("source_ids") or []),
    )
    token = _CURRENT_SOURCE_SCOPE.set(current)
    try:
        yield
    finally:
        _CURRENT_SOURCE_SCOPE.reset(token)


def current_source_scope() -> ActiveSourceScope | None:
    return _CURRENT_SOURCE_SCOPE.get()


def current_source_scope_payload() -> dict[str, Any] | None:
    scope = current_source_scope()
    if scope is None:
        return None
    return {"mode": scope.mode, "source_ids": sorted(scope.source_ids)}


def source_scope_restricted() -> bool:
    scope = current_source_scope()
    return bool(scope and scope.restricted)


def scoped_conversation_history(history: str) -> str:
    """Prevent prior answers from crossing into a newly narrowed source run."""
    return "" if source_scope_restricted() else history


def source_allowed(notebook_id: str, source_id: str) -> bool:
    scope = current_source_scope()
    return True if scope is None else scope.allows(notebook_id, source_id)


def _evidence_source_id(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("source_id") or "")
    return str(getattr(value, "source_id", "") or "")


def filter_evidence(notebook_id: str, evidence: Iterable[Any]) -> list[Any]:
    return [
        item for item in evidence
        if source_allowed(notebook_id, _evidence_source_id(item))
    ]


def filter_retrieval_items(
    active_notebook_id: str, kind: str, items: Iterable[Any]
) -> list[Any]:
    scope = current_source_scope()
    values = list(items)
    if scope is None or not scope.restricted:
        return values
    out: list[Any] = []
    for item in values:
        origin = str(
            (item.get("notebook_id") if isinstance(item, dict)
             else getattr(item, "notebook_id", ""))
            or active_notebook_id
        )
        if kind == "element":
            if scope.allows(active_notebook_id, str(getattr(item, "source_id", "") or "")):
                out.append(item)
            continue
        if kind == "chunk":
            if scope.allows(origin, str(getattr(item, "source_id", "") or "")):
                out.append(item)
            continue
        if kind in {"knowledge", "relation"}:
            raw_evidence = (
                item.get("evidence", ()) if isinstance(item, dict)
                else getattr(item, "evidence", ())
            ) or ()
            evidence = filter_evidence(origin, raw_evidence)
            if origin != active_notebook_id or evidence:
                if isinstance(item, dict):
                    out.append({**item, "evidence": evidence})
                    continue
                try:
                    item = replace(item, evidence=evidence)
                except TypeError:
                    item.evidence = evidence
                out.append(item)
            continue
        out.append(item)
    return out


def evidence_json_allowed(notebook_id: str, raw: Any) -> bool:
    scope = current_source_scope()
    if scope is None or not scope.restricted or notebook_id != scope.notebook_id:
        return True
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except Exception:
            raw = []
    return bool(filter_evidence(notebook_id, raw or []))
