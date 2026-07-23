"""Explicit cross-backend schema-version pairing for adapter rollout."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PostgresSchemaManifest:
    sqlite_version: int
    postgres_version: int


# SQLite rowid is an observable ordering key for these business tables. Their
# PostgreSQL counterparts append a BY DEFAULT identity ordinal so snapshot COPY
# can preserve historical rowids explicitly while new writes allocate one.
# Task 6-8 stores must use ordinal anywhere the SQLite implementation uses
# rowid as a tie-break, keyset, head/tail, or first-seen ordering contract. The
# later snapshot copier must advance each identity sequence after explicitly
# copying historical ordinals, before PostgreSQL accepts new business writes.
POSTGRES_ROWID_ORDINAL_TABLES = (
    "answers",
    "chunks",
    "concept_merge_candidates",
    "extraction_runs",
    "kg_build_jobs",
    "knowledge_objects",
    "source_elements",
)


# The schema-complete PostgreSQL baseline is paired with SQLite v29. A future
# SQLite or PostgreSQL migration must add a reviewed compatibility pairing
# rather than assuming that independently numbered schemas remain compatible.
POSTGRES_SCHEMA_MANIFEST = PostgresSchemaManifest(
    sqlite_version=29,
    postgres_version=8,
)
