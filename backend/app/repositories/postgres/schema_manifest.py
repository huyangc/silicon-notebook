"""Explicit cross-backend schema-version pairing for adapter rollout."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PostgresSchemaManifest:
    sqlite_version: int
    postgres_version: int


# Task 4 builds only the PostgreSQL migration substrate. Task 5 adds the first
# schema-complete PostgreSQL business migrations paired with SQLite v23.
POSTGRES_SCHEMA_MANIFEST = PostgresSchemaManifest(
    sqlite_version=23,
    postgres_version=0,
)
