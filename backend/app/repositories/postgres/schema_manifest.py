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


# Every ordinary application table in the current SQLite v31 / PostgreSQL v9
# compatibility pair.  SQLite FTS virtual tables are rebuilt on PostgreSQL and
# the migration ledger/shadow control tables are adapter-internal.  Keeping the
# reverse-totality set beside the version pair prevents either the adapter or
# shadow tooling from silently accepting a newly added business table.
POSTGRES_BUSINESS_TABLES = (
    "agent_access_tokens",
    "agent_profiles",
    "agent_token_notebooks",
    "answers",
    "app_settings",
    "ask_jobs",
    "ask_trace_steps",
    "auth_sessions",
    "canonical_relations",
    "chunk_embeddings",
    "chunks",
    "communities",
    "community_members",
    "concept_clusters",
    "concept_comentions",
    "concept_merge_candidates",
    "concept_whitelist",
    "conversations",
    "element_embeddings",
    "extraction_runs",
    "feedback",
    "kg_build_jobs",
    "kg_canonical_scratch",
    "kg_cluster_scratch",
    "kg_conflict_candidates",
    "kg_rebuild_checkpoint",
    "knowhow_cell_code",
    "knowhow_cells",
    "knowhow_changes",
    "knowhow_columns",
    "knowhow_milestones",
    "knowhow_rows",
    "knowhow_tables",
    "knowledge_embeddings",
    "knowledge_object_sources",
    "knowledge_objects",
    "knowledge_relations",
    "memory_embeddings",
    "memory_items",
    "memory_provenance",
    "memory_revisions",
    "mention_edges",
    "merge_review_jobs",
    "model_service_status",
    "notebook_assets",
    "notebook_bases",
    "notebook_members",
    "notebooks",
    "object_schemas",
    "promotion_candidates",
    "relation_embeddings",
    "reports",
    "source_authors",
    "source_elements",
    "source_paper_meta",
    "sources",
    "system_model_service_status",
    "unified_kg_state",
    "user_profiles",
    "users",
)


# The schema-complete PostgreSQL baseline is paired with SQLite v31. A future
# SQLite or PostgreSQL migration must add a reviewed compatibility pairing
# rather than assuming that independently numbered schemas remain compatible.
POSTGRES_SCHEMA_MANIFEST = PostgresSchemaManifest(
    sqlite_version=31,
    postgres_version=9,
)
