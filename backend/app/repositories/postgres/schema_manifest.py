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


# Every ordinary application table in the paired schema. SQLite FTS virtual
# tables are intentionally absent because PostgreSQL rebuilds search indexes.
# Import preflight uses this allow-list to reject an unrelated or live target
# before it writes schema or data.
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


# Deployed SQLite databases can retain these pre-retirement tables even after
# reaching the current user_version. No current service reads them and the
# PostgreSQL schema intentionally has no counterpart. The importer may ignore
# them only when every table is empty; a non-empty legacy table fails closed so
# historical user data is never silently discarded.
SQLITE_RETIRED_TABLES = (
    "article_claims",
    "articles",
    "derived_rule_candidates",
    "extraction_candidates",
)


# Reviewed cross-backend storage transforms.  Keep these classifications next
# to the schema pairing so schema parity, the offline importer, and future
# migration tooling cannot silently disagree about TEXT values that become
# typed PostgreSQL values.
POSTGRES_JSON_COLUMNS = frozenset(
    {
        "agent_access_tokens.scopes_json",
        "answers.payload",
        "ask_jobs.trace_json",
        "ask_trace_steps.step_json",
        "canonical_relations.sample_relation_ids",
        "chunks.element_ids",
        "communities.findings",
        "communities.member_ids",
        "kg_conflict_candidates.resolved_payload",
        "kg_rebuild_checkpoint.payload",
        "knowledge_objects.evidence",
        "knowledge_objects.payload",
        "knowledge_relations.evidence",
        "memory_items.tags_json",
        "memory_provenance.payload_json",
        "memory_revisions.tags_json",
        "notebooks.expected_questions",
        "notebooks.source_types",
        "notebooks.taxonomy",
        "object_schemas.fields",
        "object_schemas.list_fields",
        "reports.gaps_json",
        "reports.outline_json",
        "reports.references_json",
        "reports.section_status_json",
        "reports.sections_json",
        "source_elements.metadata",
        "source_paper_meta.keywords",
        "source_paper_meta.raw_json",
        "user_profiles.domain_focus",
        "user_profiles.model_settings",
    }
)

POSTGRES_BYTEA_COLUMNS = frozenset(
    {
        "chunk_embeddings.vector",
        "element_embeddings.vector",
        "knowledge_embeddings.vector",
        "memory_embeddings.vector",
        "relation_embeddings.vector",
    }
)

POSTGRES_EMPTY_JSON_LIST_SENTINELS = frozenset(
    {
        "ask_jobs.trace_json",
        "notebooks.expected_questions",
        "notebooks.source_types",
        "notebooks.taxonomy",
    }
)

# SQLite historically stored an empty string for these optional timestamps.
# PostgreSQL uses NULL; repository row mappers restore the domain-facing empty
# value where the old contract requires it.
POSTGRES_EMPTY_TIME_SENTINELS = frozenset(
    {
        "ask_jobs.created_at",
        "ask_jobs.updated_at",
        "ask_trace_steps.created_at",
        "kg_build_jobs.finished_at",
        "knowledge_objects.last_reviewed",
        "merge_review_jobs.started_at",
        "merge_review_jobs.updated_at",
        "unified_kg_state.last_rebuild_at",
    }
)


# The schema-complete PostgreSQL baseline is paired with SQLite v31. A future
# SQLite or PostgreSQL migration must add a reviewed compatibility pairing
# rather than assuming that independently numbered schemas remain compatible.
POSTGRES_SCHEMA_MANIFEST = PostgresSchemaManifest(
    sqlite_version=31,
    postgres_version=10,
)
