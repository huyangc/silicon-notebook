"""Explicit database ownership for the optional four-worker PostgreSQL lane."""

from __future__ import annotations

import json
from collections.abc import Mapping

from app.core.database_url import database_identity


TARGETS_ENV = "TEST_POSTGRES_TARGETS_JSON"
PARALLEL_WORKERS = 4
TARGET_SPECS = (
    ("primary", "TEST_POSTGRES_URL", "utf8"),
    ("non_c", "TEST_POSTGRES_NON_C_URL", "non-c"),
    ("non_utf", "TEST_POSTGRES_NON_UTF_URL", "non-utf"),
)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate target field")
        result[key] = value
    return result


def parse_target_groups(raw: str) -> list[dict[str, str]]:
    """Reject partial groups and database reuse before any target is contacted.

    One explicit server endpoint avoids ambiguous aliases for the same server;
    every database, including auxiliary targets, must have a unique identity.
    Credentials are deliberately excluded from identities and error messages.
    """
    try:
        groups = json.loads(raw, object_pairs_hook=_unique_object)
    except (TypeError, ValueError):
        raise RuntimeError("PostgreSQL worker targets must be valid JSON") from None
    if not isinstance(groups, list) or len(groups) != PARALLEL_WORKERS:
        raise RuntimeError("PostgreSQL worker targets require exactly four groups")
    keys = {key for key, _, _ in TARGET_SPECS}
    seen: set[tuple[str, int, str]] = set()
    endpoint: tuple[str, int] | None = None
    for group in groups:
        if not isinstance(group, dict) or set(group) != keys:
            raise RuntimeError("PostgreSQL worker target group is incomplete")
        for url in group.values():
            if not isinstance(url, str) or not url:
                raise RuntimeError("PostgreSQL worker target URL is invalid")
            try:
                identity = database_identity(url)
            except ValueError:
                raise RuntimeError("PostgreSQL worker target URL is invalid") from None
            if identity.scheme != "postgresql" or identity.host is None:
                raise RuntimeError("PostgreSQL worker targets must use PostgreSQL")
            current_endpoint = (identity.host.lower(), identity.port or 5432)
            if endpoint is not None and current_endpoint != endpoint:
                raise RuntimeError(
                    "PostgreSQL worker targets require one explicit server endpoint"
                )
            endpoint = current_endpoint
            key = (*current_endpoint, identity.database)
            if key in seen:
                raise RuntimeError("PostgreSQL worker targets reuse a database")
            seen.add(key)
    return groups


def worker_environment(
    groups: list[dict[str, str]], worker_id: str
) -> dict[str, str]:
    """Map only the four launcher-owned worker IDs to their explicit URLs."""
    allowed = tuple(f"gw{index}" for index in range(PARALLEL_WORKERS))
    if worker_id not in allowed:
        raise RuntimeError("PostgreSQL worker identity is outside its target groups")
    group = groups[allowed.index(worker_id)]
    return {env: group[key] for key, env, _ in TARGET_SPECS}


def configured_target_groups(
    environment: Mapping[str, str],
) -> list[dict[str, str]] | None:
    if TARGETS_ENV not in environment:
        return None
    if any(env in environment for _, env, _ in TARGET_SPECS):
        raise RuntimeError("PostgreSQL serial and parallel targets cannot be combined")
    return parse_target_groups(environment[TARGETS_ENV])
