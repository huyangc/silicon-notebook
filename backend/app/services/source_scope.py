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
    narrowed: bool | None = None
    # The boundary also decides WHOSE hidden sources these are, and the two
    # kinds are not alike: Knowhow projections are notebook-wide, so every
    # member's ceiling admits them, while a Memory projection belongs to its
    # ``memory_items.created_by`` and only that user's ceiling may admit it.
    # That filter lives in the SQL of ``scope_source_ids`` -- another member's
    # Memory source id never reaches this dataclass -- so nothing here needs
    # (or may add) a second owner test.
    hidden_source_ids: frozenset[str] = frozenset()
    # The identity that hidden half was read for, so the drift probe can re-read
    # the same partition in the same frame.  Empty for direct service-layer
    # construction, whose ``narrowed is None`` short-circuits that comparison.
    owner_id: str = ""

    @property
    def ceiling_active(self) -> bool:
        """Whether active-notebook evidence is bounded by a frozen snapshot.

        This is deliberately distinct from ``restricted``.  An API-resolved
        include-list that happened to contain the whole visible universe is
        not a narrowed run, so graph channels may stay enabled, but the frozen
        list must still constrain source-partitioned candidate generation and
        final evidence if sources change while the run is in flight.
        """
        return self.mode == "include" or bool(self.source_ids)

    @property
    def restricted(self) -> bool:
        if self.narrowed is not None:
            return self.narrowed
        # exclude [] is the UI/default representation of "all local sources"
        # and must remain byte-for-byte compatible with historical direct
        # callers whose scope predates the server-computed narrowed bit.
        return self.ceiling_active

    def allows(self, notebook_id: str, source_id: str) -> bool:
        # Mounted base libraries are independent participants and are never
        # governed by the active notebook's source checkboxes.
        if notebook_id and notebook_id != self.notebook_id:
            return True
        if not self.ceiling_active:
            return True
        if not source_id:
            return False
        if self.mode == "include":
            return source_id in self.source_ids or source_id in self.hidden_source_ids
        return source_id not in self.source_ids


_CURRENT_SOURCE_SCOPE: ContextVar[ActiveSourceScope | None] = ContextVar(
    "current_source_scope", default=None
)


def _scope_dict(scope: Any) -> dict[str, Any] | None:
    if scope is None:
        return None
    if hasattr(scope, "model_dump"):
        raw = scope.model_dump()
        # Pydantic intentionally excludes server-only hidden ids from public
        # serialization.  The live request context still needs the validated
        # snapshot carried by the model object itself.
        hidden = getattr(scope, "hidden_source_ids", None)
        if hidden is not None:
            raw["hidden_source_ids"] = list(hidden)
        owner_id = getattr(scope, "scope_owner_id", None)
        if owner_id:
            raw["owner_id"] = str(owner_id)
        return raw
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
        narrowed=(
            bool(raw["narrowed"])
            if raw.get("narrowed") is not None else None
        ),
        hidden_source_ids=frozenset(
            str(value) for value in raw.get("hidden_source_ids") or []
        ),
        owner_id=str(raw.get("owner_id") or ""),
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
    return {
        "mode": scope.mode,
        "source_ids": sorted(scope.source_ids),
        "narrowed": scope.narrowed,
    }


def source_scope_restricted() -> bool:
    scope = current_source_scope()
    return bool(scope and scope.restricted)


def source_scope_ceiling_active() -> bool:
    scope = current_source_scope()
    return bool(scope and scope.ceiling_active)


def scoped_conversation_history(history: str) -> str:
    """Prevent prior answers from crossing into a newly narrowed source run."""
    return "" if source_scope_restricted() else history


def source_allowed(notebook_id: str, source_id: str) -> bool:
    scope = current_source_scope()
    return True if scope is None else scope.allows(notebook_id, source_id)


def scoped_allowed_source_ids(
    notebook_id: str, explicit: Iterable[str] | None = None
) -> tuple[str, ...] | None:
    """Intersect a producer's allow-list with the active checkbox ceiling.

    HTTP requests freeze checkbox exclusions to ``include`` before entering a
    worker, so the common path always returns an allow-list that SQL/FTS can
    apply before LIMIT.  The exclude branch remains for direct service callers:
    it can narrow an existing explicit list, while result-boundary filtering
    remains the fail-closed fallback when no universe was supplied.
    """
    allowed = (
        tuple(dict.fromkeys(str(value) for value in explicit if str(value)))
        if explicit is not None else None
    )
    scope = current_source_scope()
    if (
        scope is None
        or not scope.ceiling_active
        or notebook_id != scope.notebook_id
    ):
        return allowed
    if scope.mode == "include":
        ceiling = scope.source_ids | scope.hidden_source_ids
        if allowed is None:
            return tuple(sorted(ceiling))
        return tuple(value for value in allowed if value in ceiling)
    if allowed is not None:
        return tuple(value for value in allowed if value not in scope.source_ids)
    return None


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
    if scope is None or not scope.ceiling_active:
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
    if (
        scope is None
        or not scope.ceiling_active
        or notebook_id != scope.notebook_id
    ):
        return True
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except Exception:
            raw = []
    return bool(filter_evidence(notebook_id, raw or []))


def source_scope_visible_universe_matches(
    notebook_id: str,
    current_visible_source_ids: Iterable[str],
    current_hidden_source_ids: Iterable[str] | None = None,
) -> bool:
    """Check whether an all-selected graph run still sees frozen participants.

    Narrowed runs are already unsafe for whole-graph channels.  Legacy scopes
    without the server-computed bit keep their historical behavior.  For a
    server-resolved all-selected include snapshot, any visible-source drift or
    hidden-projection drift disables non-partitioned graph/PPR/relation/exact
    channels before I/O. ``None`` keeps compatibility for bounded test doubles
    that predate the hidden-participant probe; production supplies both sets.

    Both sets must be read for the SAME identity the freeze used — the hidden
    half is owner-scoped (Memory is private to its creator), so a live read
    taken as a different user, or with no owner filter at all, would differ
    from the frozen snapshot on every request in a shared notebook and pin
    these channels off permanently.  The caller owns that: it passes
    ``ActiveSourceScope.owner_id``.
    """
    scope = current_source_scope()
    if (
        scope is None
        or notebook_id != scope.notebook_id
        or scope.narrowed is None
        or scope.narrowed
        or scope.mode != "include"
    ):
        return True
    visible_matches = set(
        str(value) for value in current_visible_source_ids
    ) == set(scope.source_ids)
    if not visible_matches or current_hidden_source_ids is None:
        return visible_matches
    return set(str(value) for value in current_hidden_source_ids) == set(
        scope.hidden_source_ids
    )
