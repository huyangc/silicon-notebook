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


# Every ordinary application table in the current SQLite v67 / PostgreSQL v46
# compatibility pair. SQLite FTS virtual tables are rebuilt on PostgreSQL and
# the migration ledger/shadow control tables are adapter-internal. Import and
# shadow preflight use this reverse-totality list to reject unrelated/live
# targets and any newly added business table that lacks a reviewed mapping.
POSTGRES_BUSINESS_TABLES = (
    "agent_access_tokens",
    "agent_notebook_profile",
    "agent_observations",
    "agent_profile_jobs",
    "agent_profiles",
    "agent_token_notebooks",
    "answers",
    "app_settings",
    "ask_jobs",
    "ask_trace_steps",
    "auth_sessions",
    "canonical_relations",
    "catalog_candidates",
    "catalog_jobs",
    "chunk_element_backfills",
    "chunk_elements",
    "chunk_embeddings",
    "chunk_questions",
    "chunks",
    "communities",
    "community_members",
    "concept_clusters",
    "concept_comentions",
    "concept_merge_candidates",
    "concept_whitelist",
    "conversations",
    "element_embeddings",
    "extension_runtime_toggles",
    "extraction_runs",
    "feedback",
    "group_members",
    "groups",
    "indexing_pipeline_stage_sources",
    "indexing_pipeline_stages",
    "kg_analysis_artifacts",
    "kg_build_jobs",
    "kg_canonical_scratch",
    "kg_cluster_scratch",
    "kg_community_edges",
    "kg_conflict_candidates",
    "kg_rebuild_checkpoint",
    "kg_relation_completion_state",
    "kg_source_profiles",
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
    "knowledge_source_fact_elements",
    "knowledge_source_fact_backfills",
    "knowledge_source_facts",
    "memory_embeddings",
    "memory_items",
    "memory_provenance",
    "memory_revisions",
    "mention_edges",
    "merge_review_jobs",
    "model_service_status",
    "notebook_assets",
    "notebook_bases",
    "notebook_delete_files",
    "notebook_delete_jobs",
    "notebook_grants",
    "notebook_members",
    "notebook_object_schemas",
    "notebook_share_requests",
    "notebooks",
    "object_schemas",
    "promotion_candidates",
    "relation_embeddings",
    "retained_user_activity",
    "reports",
    "retrieval_experiences",
    "source_authors",
    "source_elements",
    "source_index_backfills",
    "source_paper_meta",
    "sources",
    "system_model_service_status",
    "unified_kg_state",
    "user_profiles",
    "users",
    "wish_votes",
    "wishes",
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


# Current SQLite databases carry these operational tables for the independent
# forward-shadow path. They are not business data and PostgreSQL intentionally
# owns its migration controls in a separate schema, so the stopped-snapshot
# importer excludes them even when an earlier shadow run left audit rows.
SQLITE_MIGRATION_INTERNAL_TABLES = (
    "shadow_capture_control",
    "shadow_change_log",
)


# Reviewed cross-backend storage transforms.  Keep these classifications next
# to the schema pairing so schema parity, the offline importer, and future
# migration tooling cannot silently disagree about TEXT values that become
# typed PostgreSQL values.
POSTGRES_JSON_COLUMNS = frozenset(
    {
        "agent_access_tokens.scopes_json",
        "agent_notebook_profile.evidence_json",
        "agent_notebook_profile.history_json",
        "answers.payload",
        "ask_jobs.trace_json",
        "ask_trace_steps.step_json",
        "canonical_relations.sample_relation_ids",
        "catalog_candidates.payload",
        "catalog_candidates.reject_info",
        "chunks.element_ids",
        "communities.findings",
        "communities.member_ids",
        "kg_analysis_artifacts.payload",
        "kg_conflict_candidates.resolved_payload",
        "kg_rebuild_checkpoint.payload",
        "indexing_pipeline_stages.source_snapshot",
        "indexing_pipeline_stage_sources.payload",
        "knowledge_objects.evidence",
        "knowledge_objects.payload",
        "knowledge_relations.evidence",
        "knowledge_source_facts.evidence",
        "knowledge_source_facts.payload",
        "memory_items.tags_json",
        "memory_provenance.payload_json",
        "memory_revisions.tags_json",
        "notebooks.expected_questions",
        "notebooks.source_types",
        "notebooks.taxonomy",
        "notebook_object_schemas.fields",
        "notebook_object_schemas.list_fields",
        "object_schemas.fields",
        "object_schemas.list_fields",
        "reports.gaps_json",
        "reports.outline_json",
        "reports.references_json",
        "reports.section_status_json",
        "reports.sections_json",
        "reports.understanding_json",
        # SQLite v54 / PostgreSQL v32: the deployment-global retrieval-experience
        # library. ``situation_json`` is a closed-vocabulary key/value map (the
        # entry's situation fingerprint, and the hash input behind its
        # content-addressed primary key); ``provenance_json`` is a bounded list
        # of opaque run ids and nothing else.
        "retrieval_experiences.provenance_json",
        "retrieval_experiences.situation_json",
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
        "chunk_questions.vector",
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
        "agent_profile_jobs.finished_at",
        "agent_profile_jobs.started_at",
        "ask_jobs.created_at",
        "ask_jobs.updated_at",
        "ask_trace_steps.created_at",
        "catalog_jobs.finished_at",
        "chunk_element_backfills.completed_at",
        "kg_build_jobs.finished_at",
        "knowledge_objects.last_reviewed",
        "merge_review_jobs.started_at",
        "merge_review_jobs.updated_at",
        "retained_user_activity.created_at",
        "retained_user_activity.updated_at",
        "source_index_backfills.completed_at",
        "unified_kg_state.last_rebuild_at",
    }
)


# The schema-complete PostgreSQL baseline is paired with SQLite v68. A future
# SQLite or PostgreSQL migration must add a reviewed compatibility pairing
# rather than assuming that independently numbered schemas remain compatible.
# PostgreSQL v45 / SQLite v66 add visible-source upload attribution; PostgreSQL
# v46 / SQLite v67 add the global wish wall and one-vote-per-user relation;
# PostgreSQL v47 / SQLite v68 (batch 3 W1 PR-2) add
# unified_kg_state.kg_reset_epoch -- a persistent per-notebook "KG reset"
# counter, DEFAULT 0, additive only. PostgreSQL v48 (hot-path fix batch 4)
# stays paired with SQLite v68 deliberately: it adds only the three
# notebook-scoped composite GIN trigram indexes behind the source tab's search
# predicate, and an index-only migration carries no cross-backend shape to
# pair. The accompanying query rewrite (list_sources_page's three-leg UNION)
# does ship on BOTH backends, but SQLite gets no index change -- it has no GIN
# trigram equivalent -- which is the same PostgreSQL-only split migration 0042
# registered for hot-path batch 2.
# PostgreSQL v49 / SQLite v69 (batch 3 W1 PR-3 Phase A; renumbered from 48
# after batch 4's index-only 0048 landed first on master) add three FK/keyset
# indexes (idx_agent_tokens_default_notebook, idx_knowhow_cell_code_column,
# idx_conversations_notebook) plus the notebook_delete_jobs /
# notebook_delete_files delete-job carrier tables -- additive only, no
# existing column/index/FK shape changes.
POSTGRES_SCHEMA_MANIFEST = PostgresSchemaManifest(
    sqlite_version=69,
    postgres_version=49,
)
