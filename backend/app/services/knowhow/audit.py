"""Bounded read-time projection of legacy user ids to audit labels.

This module never writes resolved labels back.  In particular,
``knowhow_cell_code.updated_by`` participates in the table fingerprint and
must retain its historical stored bytes.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable


AUDIT_RESOLVE_LIMIT = 512


def resolve_audit_labels(
    repo: Any, candidates: Iterable[str], *, limit: int = AUDIT_RESOLVE_LIMIT
) -> dict[str, str]:
    """Resolve at most ``limit`` exact live user ids in one bounded store call."""

    bounded = max(0, min(int(limit), AUDIT_RESOLVE_LIMIT))
    ordered: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        value = str(candidate or "")
        if not value or value in seen:
            continue
        seen.add(value)
        if len(ordered) < bounded:
            ordered.append(value)
    resolved = repo.audit_labels_for_user_ids(ordered) if ordered else {}
    return {value: str(resolved.get(value) or value) for value in ordered}


def _updated_by_values(value: Any) -> list[str]:
    values: list[str] = []
    if isinstance(value, dict):
        updated_by = value.get("updated_by")
        if isinstance(updated_by, str) and updated_by:
            values.append(updated_by)
        for child in value.values():
            values.extend(_updated_by_values(child))
    elif isinstance(value, list):
        for child in value:
            values.extend(_updated_by_values(child))
    return values


def _replace_updated_by(value: Any, labels: dict[str, str]) -> None:
    if isinstance(value, dict):
        updated_by = value.get("updated_by")
        if isinstance(updated_by, str):
            value["updated_by"] = labels.get(updated_by, updated_by)
        for child in value.values():
            _replace_updated_by(child, labels)
    elif isinstance(value, list):
        for child in value:
            _replace_updated_by(child, labels)


def _agent_before_values(payload: Any) -> list[str]:
    """Collect updater labels only from semantic ``before`` snapshots.

    Agent actor/profile labels and ``after``/``current`` payloads are authored
    by the current Agent and must remain opaque free text, even when they happen
    to equal a live user id.  A ``before`` subtree, however, describes the
    displaced historical value and may legitimately contain a legacy user id.
    """

    values: list[str] = []
    if isinstance(payload, dict):
        for key, child in payload.items():
            if key == "before":
                values.extend(_updated_by_values(child))
            elif key not in {"after", "current"}:
                values.extend(_agent_before_values(child))
    elif isinstance(payload, list):
        for child in payload:
            values.extend(_agent_before_values(child))
    return values


def _replace_agent_before_values(payload: Any, labels: dict[str, str]) -> None:
    if isinstance(payload, dict):
        for key, child in payload.items():
            if key == "before":
                _replace_updated_by(child, labels)
            elif key not in {"after", "current"}:
                _replace_agent_before_values(child, labels)
    elif isinstance(payload, list):
        for child in payload:
            _replace_agent_before_values(child, labels)


def project_history_page(repo: Any, page: dict) -> dict:
    """Resolve one timeline page's changes and milestones with one batch."""

    projected = deepcopy(page)
    candidates = [
        change.get("actor", "")
        for change in projected.get("changes", [])
        if change.get("origin") != "agent"
    ]
    candidates.extend(
        milestone.get("created_by", "")
        for milestone in projected.get("milestones", [])
    )
    labels = resolve_audit_labels(repo, candidates)
    for change in projected.get("changes", []):
        if change.get("origin") != "agent":
            actor = change.get("actor", "")
            change["actor"] = labels.get(actor, actor)
    for milestone in projected.get("milestones", []):
        creator = milestone.get("created_by", "")
        milestone["created_by"] = labels.get(creator, creator)
    return projected


def project_change(repo: Any, change: dict) -> dict:
    """Resolve a single change actor and nested code attachment labels."""

    projected = deepcopy(change)
    if projected.get("origin") == "agent":
        # Agent actor and its after/current updater are free text.  Only a
        # displaced before snapshot can carry a legacy human identity id.
        labels = resolve_audit_labels(
            repo, _agent_before_values(projected.get("payload"))
        )
        _replace_agent_before_values(projected.get("payload"), labels)
        return projected
    candidates = _updated_by_values(projected.get("payload"))
    candidates.append(projected.get("actor", ""))
    labels = resolve_audit_labels(repo, candidates)
    actor = projected.get("actor", "")
    projected["actor"] = labels.get(actor, actor)
    _replace_updated_by(projected.get("payload"), labels)
    return projected


def project_cell_history(repo: Any, entries: list[dict]) -> list[dict]:
    projected = deepcopy(entries)
    labels = resolve_audit_labels(
        repo,
        (
            entry.get("actor", "")
            for entry in projected
            if entry.get("origin") != "agent"
        ),
    )
    for entry in projected:
        if entry.get("origin") != "agent":
            actor = entry.get("actor", "")
            entry["actor"] = labels.get(actor, actor)
    return projected


def project_nested_updated_by(repo: Any, value: Any) -> Any:
    """Resolve all displayed ``updated_by`` fields in one response batch."""

    projected = deepcopy(value)
    labels = resolve_audit_labels(repo, _updated_by_values(projected))
    _replace_updated_by(projected, labels)
    return projected


__all__ = [
    "AUDIT_RESOLVE_LIMIT",
    "project_cell_history",
    "project_change",
    "project_history_page",
    "project_nested_updated_by",
    "resolve_audit_labels",
]
