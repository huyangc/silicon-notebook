"""Immutable table manifest for the temporary forward shadow migration.

The manifest is intentionally data-only.  Later capture/copy/apply tasks consume
the names declared here; they must not infer scope from a live database and
silently accept a newly added table.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from typing import Any

from psycopg import sql

from app.migration.shadow.types import (
    Manifest,
    ReplicationGuardSpec,
    ReplicationKeyKind,
    SchemaPair,
    SchemaValidationReport,
    TableClass,
    TableSpec,
)
from app.repositories.postgres.schema_manifest import (
    POSTGRES_BUSINESS_TABLES,
    POSTGRES_SCHEMA_MANIFEST,
)


RUNNING_SCHEMA_PAIR = SchemaPair(sqlite_version=64, postgres_version=43, epoch=1)

# The old design's (SQLite 24, PostgreSQL 2) COPY-ready pair predates five
# current business tables and is no longer total.  Do not advertise a staging
# pair until a later task defines a current, checksummed COPY-ready schema.
COPY_READY_SCHEMA_PAIRS: frozenset[SchemaPair] = frozenset()

# Declarative transform vocabulary.  A pipeline joins these names with "+";
# later row transformers provide the direction-specific implementation.  The
# schema kept SQLite integer flags as PostgreSQL bigint through epoch 1 for
# every table but one: SQLite v63 / PostgreSQL v41's
# extension_runtime_toggles.enabled is the first column selecting "boolean" —
# a reviewed, table-specific choice (see 0041_extension_runtime_toggles.sql),
# not a change to the bigint convention for every other integer flag.
COLUMN_TRANSFORMS = {
    "identity": "no storage conversion",
    "timestamptz": "SQLite ISO text <-> PostgreSQL timestamptz",
    "jsonb": "SQLite canonical JSON text <-> PostgreSQL jsonb",
    "boolean": "SQLite 0/1 integer <-> PostgreSQL boolean",
    "bytea": "SQLite embedding bytes/text <-> PostgreSQL bytea",
}

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_FILESYSTEM_PATH_COLUMNS = frozenset({"sources.file_path"})
_NON_FILESYSTEM_PATH_COLUMNS = frozenset(
    {"chunks.section_path", "catalog_candidates.section_path"}
)
_FTS5_SHADOW_SUFFIXES = ("data", "idx", "content", "docsize", "config")
_POSTGRES_SPECIAL_TYPES = {
    "bytea": "bytea",
    "jsonb": "jsonb",
    "timestamp with time zone": "timestamptz",
    "boolean": "boolean",
}
_TRANSFORM_ORDER = ("bytea", "jsonb", "timestamptz", "boolean")

# SQLite rowid tables do not make a non-INTEGER PRIMARY KEY column NOT NULL.
# Existing rows are scanned during Task 1 preflight; Task 2 must consume this
# exact declaration to generate INSERT and key-UPDATE NULL guards for the
# observation period.  No trigger is installed in this task.
_SQLITE_NULL_GUARD_KEYS = frozenset(
    {
        "agent_access_tokens.id",
        "agent_profiles.id",
        "answers.id",
        "app_settings.key",
        "ask_jobs.id",
        "auth_sessions.token",
        "catalog_candidates.id",
        "catalog_jobs.id",
        "chunk_embeddings.chunk_id",
        "chunk_questions.id",
        "chunks.id",
        "communities.id",
        "concept_clusters.id",
        "concept_merge_candidates.id",
        "concept_whitelist.term",
        "conversations.id",
        "element_embeddings.element_id",
        "extraction_runs.id",
        "feedback.id",
        "kg_build_jobs.id",
        "kg_conflict_candidates.id",
        "knowhow_cell_code.id",
        "knowhow_cells.id",
        "knowhow_changes.id",
        "knowhow_columns.id",
        "knowhow_milestones.id",
        "knowhow_rows.id",
        "knowhow_tables.id",
        "knowledge_embeddings.object_id",
        "knowledge_objects.id",
        "knowledge_relations.id",
        "memory_embeddings.memory_id",
        "memory_items.id",
        "memory_provenance.id",
        "memory_revisions.id",
        "merge_review_jobs.notebook_id",
        "notebook_assets.id",
        "notebooks.id",
        "object_schemas.object_type",
        "promotion_candidates.id",
        "relation_embeddings.relation_id",
        "reports.id",
        "retrieval_experiences.id",
        "source_authors.id",
        "source_elements.id",
        "source_paper_meta.source_id",
        "sources.id",
        "system_model_service_status.service_id",
        "unified_kg_state.notebook_id",
        "user_profiles.id",
        "users.id",
    }
)


def _table(
    name: str,
    replication_key: tuple[str, ...],
    key_kind: ReplicationKeyKind,
    copy_rank: int,
    transform: str,
    *,
    blob_columns: tuple[str, ...] = (),
    path_columns: tuple[str, ...] = (),
) -> TableSpec:
    return TableSpec(
        name=name,
        table_class=TableClass.REPLICATED,
        replication_key=replication_key,
        key_kind=key_kind,
        copy_rank=copy_rank,
        blob_columns=blob_columns,
        path_columns=path_columns,
        sqlite_null_guard_columns=tuple(
            column
            for column in replication_key
            if f"{name}.{column}" in _SQLITE_NULL_GUARD_KEYS
        ),
        sqlite_to_postgres=transform,
        postgres_to_sqlite=transform,
    )


_TABLES = (
    _table("app_settings", ("key",), ReplicationKeyKind.DECLARED_PK, 1, "timestamptz"),
    _table(
        "community_members",
        ("notebook_id", "level", "canonical_id"),
        ReplicationKeyKind.SHADOW_UNIQUE,
        2,
        "identity",
    ),
    _table(
        "concept_whitelist", ("term",), ReplicationKeyKind.DECLARED_PK, 3, "timestamptz"
    ),
    _table("conversations", ("id",), ReplicationKeyKind.DECLARED_PK, 4, "timestamptz"),
    # seed-first keeps the shadow unique guard from replacing the existing
    # (notebook_id, run_id, seed) operational access path.
    _table(
        "kg_canonical_scratch",
        ("seed", "notebook_id", "run_id"),
        ReplicationKeyKind.SHADOW_UNIQUE,
        5,
        "identity",
    ),
    _table(
        "kg_cluster_scratch",
        ("object_id", "notebook_id", "run_id"),
        ReplicationKeyKind.SHADOW_UNIQUE,
        6,
        "identity",
    ),
    _table(
        "knowledge_embeddings",
        ("object_id",),
        ReplicationKeyKind.DECLARED_PK,
        7,
        "bytea+timestamptz",
        blob_columns=("vector",),
    ),
    _table(
        "knowledge_object_sources",
        ("object_id", "source_id"),
        ReplicationKeyKind.SHADOW_UNIQUE,
        8,
        "identity",
    ),
    _table(
        "object_schemas",
        ("object_type",),
        ReplicationKeyKind.DECLARED_PK,
        9,
        "jsonb+timestamptz",
    ),
    _table(
        "system_model_service_status",
        ("service_id",),
        ReplicationKeyKind.DECLARED_PK,
        10,
        "timestamptz",
    ),
    _table("users", ("id",), ReplicationKeyKind.DECLARED_PK, 11, "timestamptz"),
    _table(
        "agent_profiles", ("id",), ReplicationKeyKind.DECLARED_PK, 12, "timestamptz"
    ),
    _table(
        "auth_sessions", ("token",), ReplicationKeyKind.DECLARED_PK, 13, "timestamptz"
    ),
    _table(
        "model_service_status",
        ("user_id", "service"),
        ReplicationKeyKind.DECLARED_PK,
        14,
        "timestamptz",
    ),
    _table(
        "notebooks", ("id",), ReplicationKeyKind.DECLARED_PK, 15, "jsonb+timestamptz"
    ),
    _table(
        "agent_access_tokens",
        ("id",),
        ReplicationKeyKind.DECLARED_PK,
        16,
        "jsonb+timestamptz",
    ),
    _table(
        "agent_token_notebooks",
        ("token_id", "notebook_id"),
        ReplicationKeyKind.DECLARED_PK,
        17,
        "identity",
    ),
    _table("answers", ("id",), ReplicationKeyKind.DECLARED_PK, 18, "jsonb+timestamptz"),
    _table(
        "ask_jobs", ("id",), ReplicationKeyKind.DECLARED_PK, 19, "jsonb+timestamptz"
    ),
    _table(
        "ask_trace_steps",
        ("job_id", "seq"),
        ReplicationKeyKind.DECLARED_PK,
        20,
        "jsonb+timestamptz",
    ),
    _table(
        "canonical_relations",
        ("notebook_id", "canonical_src", "edge_type", "canonical_tgt"),
        ReplicationKeyKind.DECLARED_PK,
        21,
        "jsonb+timestamptz",
    ),
    _table(
        "communities", ("id",), ReplicationKeyKind.DECLARED_PK, 22, "jsonb+timestamptz"
    ),
    _table(
        "concept_clusters", ("id",), ReplicationKeyKind.DECLARED_PK, 23, "timestamptz"
    ),
    _table(
        "concept_comentions",
        ("notebook_id", "canonical_a", "canonical_b"),
        ReplicationKeyKind.DECLARED_PK,
        24,
        "identity",
    ),
    _table(
        "concept_merge_candidates",
        ("id",),
        ReplicationKeyKind.DECLARED_PK,
        25,
        "timestamptz",
    ),
    _table("feedback", ("id",), ReplicationKeyKind.DECLARED_PK, 26, "timestamptz"),
    _table("kg_build_jobs", ("id",), ReplicationKeyKind.DECLARED_PK, 27, "timestamptz"),
    _table(
        "kg_conflict_candidates",
        ("id",),
        ReplicationKeyKind.DECLARED_PK,
        28,
        "jsonb+timestamptz",
    ),
    _table(
        "kg_rebuild_checkpoint",
        ("notebook_id", "input_version", "stage", "item_key"),
        ReplicationKeyKind.DECLARED_PK,
        29,
        "jsonb+timestamptz",
    ),
    _table(
        "knowhow_tables", ("id",), ReplicationKeyKind.DECLARED_PK, 30, "timestamptz"
    ),
    _table(
        "knowhow_changes", ("id",), ReplicationKeyKind.DECLARED_PK, 31, "timestamptz"
    ),
    _table("knowhow_columns", ("id",), ReplicationKeyKind.DECLARED_PK, 32, "identity"),
    _table(
        "knowhow_milestones", ("id",), ReplicationKeyKind.DECLARED_PK, 33, "timestamptz"
    ),
    _table("knowhow_rows", ("id",), ReplicationKeyKind.DECLARED_PK, 34, "timestamptz"),
    _table(
        "knowhow_cell_code", ("id",), ReplicationKeyKind.DECLARED_PK, 35, "timestamptz"
    ),
    _table("knowhow_cells", ("id",), ReplicationKeyKind.DECLARED_PK, 36, "timestamptz"),
    _table(
        "knowledge_objects",
        ("id",),
        ReplicationKeyKind.DECLARED_PK,
        37,
        "jsonb+timestamptz",
    ),
    _table(
        "memory_items", ("id",), ReplicationKeyKind.DECLARED_PK, 38, "jsonb+timestamptz"
    ),
    _table(
        "memory_embeddings",
        ("memory_id",),
        ReplicationKeyKind.DECLARED_PK,
        39,
        "bytea+timestamptz",
        blob_columns=("vector",),
    ),
    _table(
        "memory_provenance",
        ("id",),
        ReplicationKeyKind.DECLARED_PK,
        40,
        "jsonb+timestamptz",
    ),
    _table(
        "memory_revisions",
        ("id",),
        ReplicationKeyKind.DECLARED_PK,
        41,
        "jsonb+timestamptz",
    ),
    _table(
        "mention_edges",
        ("notebook_id", "claim_object_id", "concept_canonical_id"),
        ReplicationKeyKind.DECLARED_PK,
        42,
        "identity",
    ),
    _table(
        "merge_review_jobs",
        ("notebook_id",),
        ReplicationKeyKind.DECLARED_PK,
        43,
        "timestamptz",
    ),
    _table(
        "notebook_assets", ("id",), ReplicationKeyKind.DECLARED_PK, 44, "timestamptz"
    ),
    _table(
        "notebook_bases",
        ("notebook_id", "base_notebook_id"),
        ReplicationKeyKind.DECLARED_PK,
        45,
        "timestamptz",
    ),
    _table(
        "notebook_members",
        ("notebook_id", "user_id"),
        ReplicationKeyKind.DECLARED_PK,
        46,
        "timestamptz",
    ),
    _table(
        "promotion_candidates",
        ("id",),
        ReplicationKeyKind.DECLARED_PK,
        47,
        "timestamptz",
    ),
    _table("reports", ("id",), ReplicationKeyKind.DECLARED_PK, 48, "jsonb+timestamptz"),
    _table(
        "sources",
        ("id",),
        ReplicationKeyKind.DECLARED_PK,
        49,
        "timestamptz",
        path_columns=("file_path",),
    ),
    _table(
        "kg_relation_completion_state",
        ("source_id", "source_generation", "mode"),
        ReplicationKeyKind.DECLARED_PK,
        50,
        "timestamptz",
    ),
    _table("chunks", ("id",), ReplicationKeyKind.DECLARED_PK, 51, "jsonb+timestamptz"),
    _table(
        "chunk_embeddings",
        ("chunk_id",),
        ReplicationKeyKind.DECLARED_PK,
        52,
        "bytea+timestamptz",
        blob_columns=("vector",),
    ),
    _table(
        "extraction_runs", ("id",), ReplicationKeyKind.DECLARED_PK, 53, "timestamptz"
    ),
    _table(
        "knowledge_relations",
        ("id",),
        ReplicationKeyKind.DECLARED_PK,
        54,
        "jsonb+timestamptz",
    ),
    _table(
        "relation_embeddings",
        ("relation_id",),
        ReplicationKeyKind.DECLARED_PK,
        55,
        "bytea+timestamptz",
        blob_columns=("vector",),
    ),
    _table(
        "source_authors", ("id",), ReplicationKeyKind.DECLARED_PK, 56, "timestamptz"
    ),
    _table(
        "source_elements",
        ("id",),
        ReplicationKeyKind.DECLARED_PK,
        57,
        "jsonb+timestamptz",
    ),
    _table(
        "element_embeddings",
        ("element_id",),
        ReplicationKeyKind.DECLARED_PK,
        58,
        "bytea+timestamptz",
        blob_columns=("vector",),
    ),
    _table(
        "source_paper_meta",
        ("source_id",),
        ReplicationKeyKind.DECLARED_PK,
        59,
        "jsonb+timestamptz",
    ),
    _table(
        "unified_kg_state",
        ("notebook_id",),
        ReplicationKeyKind.DECLARED_PK,
        60,
        "timestamptz",
    ),
    _table(
        "user_profiles",
        ("id",),
        ReplicationKeyKind.DECLARED_PK,
        61,
        "jsonb+timestamptz",
    ),
    # SQLite v36 / PostgreSQL v14: the KG-quality-analysis precompute products.
    # These two numbers are a deliberate historical milestone -- the generation
    # that introduced these three tables -- not a running-version reference, so
    # they must stay pinned and must not follow later schema bumps.
    # All three hang off notebooks (copy rank 15), so any rank after it is safe;
    # they are ranked last among the replicated tables because nothing else
    # references them.
    _table(
        "kg_analysis_artifacts",
        ("notebook_id", "kind"),
        ReplicationKeyKind.DECLARED_PK,
        62,
        "jsonb+timestamptz",
    ),
    _table(
        "kg_community_edges",
        ("notebook_id", "src_community_id", "dst_community_id"),
        ReplicationKeyKind.DECLARED_PK,
        63,
        "identity",
    ),
    _table(
        "kg_source_profiles",
        ("notebook_id", "source_id"),
        ReplicationKeyKind.DECLARED_PK,
        64,
        "identity",
    ),
    TableSpec("chunks_fts", TableClass.REBUILT, (), ReplicationKeyKind.DECLARED_PK, 65),
    TableSpec(
        "kg_objects_fts", TableClass.REBUILT, (), ReplicationKeyKind.DECLARED_PK, 66
    ),
    TableSpec(
        "memory_items_fts", TableClass.REBUILT, (), ReplicationKeyKind.DECLARED_PK, 67
    ),
    TableSpec(
        "shadow_change_log",
        TableClass.SHADOW_INTERNAL,
        (),
        ReplicationKeyKind.DECLARED_PK,
        68,
    ),
    TableSpec(
        "shadow_capture_control",
        TableClass.SHADOW_INTERNAL,
        (),
        ReplicationKeyKind.DECLARED_PK,
        69,
    ),
    # SQLite v39 / PostgreSQL v17: the command-catalog extraction tables. Ranks
    # are appended past the rebuilt/shadow-internal tail rather than slotted at
    # 65/66, because `copy_rank` is only ever used as a sort key over
    # `manifest.replicated` (bulk_copy / replicator / verifier all sort that
    # collection alone). Ranks therefore have to be unique, positive, and
    # FK-consistent among REPLICATED tables — which 70/71 already are, since
    # every parent (notebooks rank 15, sources rank 49) ranks lower — and
    # renumbering five untouched entries to preserve a cosmetic contiguity
    # would be pure churn on a frozen operational manifest.
    _table(
        "catalog_jobs",
        ("id",),
        ReplicationKeyKind.DECLARED_PK,
        70,
        "timestamptz",
    ),
    _table(
        "catalog_candidates",
        ("id",),
        ReplicationKeyKind.DECLARED_PK,
        71,
        "jsonb+timestamptz",
    ),
    # SQLite v40 / PostgreSQL v18: immutable source-generation facts and their
    # normalized element bindings. Both hang from notebooks/sources; elements
    # are validated transactionally by the writer rather than foreign-keyed so
    # a reparse cannot erase provenance before generation cleanup commits.
    _table(
        "knowledge_source_facts",
        ("id",),
        ReplicationKeyKind.DECLARED_PK,
        72,
        "jsonb+timestamptz",
    ),
    _table(
        "knowledge_source_fact_elements",
        ("fact_id", "element_id"),
        ReplicationKeyKind.DECLARED_PK,
        73,
        "timestamptz",
    ),
    # SQLite v41 / PostgreSQL v19: restartable source-fact backfill ledger.
    _table(
        "knowledge_source_fact_backfills",
        ("source_id",),
        ReplicationKeyKind.DECLARED_PK,
        74,
        "timestamptz",
    ),
    # SQLite v42 / PostgreSQL v20: notebook-level reverse-index rebuild cursor.
    _table(
        "source_index_backfills",
        ("notebook_id",),
        ReplicationKeyKind.DECLARED_PK,
        75,
        "timestamptz",
    ),
    # Optional generated-question vectors remain derived data, but an active
    # forward-shadow cutover must preserve the running database exactly.  An
    # operator can still choose to rebuild them later with the offline phase.
    _table(
        "chunk_questions",
        ("id",),
        ReplicationKeyKind.DECLARED_PK,
        76,
        "bytea+timestamptz",
        blob_columns=("vector",),
    ),
    # SQLite v46 / PostgreSQL v24: element -> chunk reverse index and its
    # notebook-level backfill cursor. The reverse rows hang off `chunks`
    # (rank 51) and the ledger off `notebooks` (rank 15), so both parents rank
    # lower and the appended ranks stay FK-consistent.
    _table(
        "chunk_elements",
        ("notebook_id", "element_id", "chunk_id"),
        ReplicationKeyKind.DECLARED_PK,
        77,
        "identity",
    ),
    _table(
        "chunk_element_backfills",
        ("notebook_id",),
        ReplicationKeyKind.DECLARED_PK,
        78,
        "timestamptz",
    ),
    # SQLite v47 / PostgreSQL v25: notebook-owned schema overrides/custom rows.
    # Its notebook parent is rank 15, so the appended rank is FK-consistent.
    _table(
        "notebook_object_schemas",
        ("notebook_id", "object_type"),
        ReplicationKeyKind.DECLARED_PK,
        79,
        "jsonb+timestamptz",
    ),
    # SQLite v49 / PostgreSQL v27: group knowledge sharing P1 (zero behavior
    # change — no reader/writer consumes these tables yet). groups has no FK
    # dependency among these three tables other than users (rank 11), so it
    # ranks first; group_members FKs onto groups (rank 80) and users (rank 11)
    # and must rank higher than groups; notebook_grants FKs onto notebooks
    # (rank 15) and users (rank 11) — its principal_id column is a
    # deliberately bare, unconstrained polymorphic reference with no foreign
    # key (see migrations.py _migration_49) — so it is FK-consistent at any
    # rank above 15 and is placed last for reading order alongside its
    # sibling tables.
    # SQLite v56 / PostgreSQL v34 adds groups.owner_id. SQLite v57 / PostgreSQL
    # v35 then adds the nullable invite capability and its timestamp/audit
    # columns. They stay on the existing aggregate row; the table's timestamp
    # transform already covers invite_created_at and the replication key stays
    # the declared group id.
    _table("groups", ("id",), ReplicationKeyKind.DECLARED_PK, 80, "timestamptz"),
    _table(
        "group_members",
        ("group_id", "user_id"),
        ReplicationKeyKind.DECLARED_PK,
        81,
        "timestamptz",
    ),
    _table(
        "notebook_grants", ("id",), ReplicationKeyKind.DECLARED_PK, 82, "timestamptz"
    ),
    # SQLite v50 / PostgreSQL v28: group knowledge sharing P2 (zero behavior
    # change — no reader/writer consumes this table yet). notebook_share_requests
    # FKs onto notebooks (rank 15), groups (rank 80) and users (rank 11) — all
    # three rank below 83, so it is FK-consistent appended after its P1
    # siblings. Unlike notebook_grants.principal_id, group_id here is a real,
    # non-polymorphic foreign key with no park-strategy tradeoff.
    _table(
        "notebook_share_requests",
        ("id",),
        ReplicationKeyKind.DECLARED_PK,
        83,
        "timestamptz",
    ),
    # SQLite v51 / PostgreSQL v29: Agentic Memory P1 (zero behavior change — no
    # reader/writer consumes these tables yet). Both hang off notebooks
    # (rank 15) and nothing else, so the appended ranks stay FK-consistent, and
    # neither table has any incoming foreign key of its own.
    #
    # The declared replication key of each table is deliberately the exact
    # column list of its composite PRIMARY KEY. That equality is the park
    # precondition: replicator.py's _build_unique_surfaces resolves a surface to
    # REPLICATION_KEY only when the surface's columns equal the spec's
    # replication key, which is what keeps these two tables off the sentinel /
    # nullable / leaf-delete park paths entirely. Changing either key here — or
    # adding a second unique surface to either table in a migration — silently
    # moves them onto a weaker strategy or fails replicator construction closed.
    _table(
        "agent_notebook_profile",
        ("notebook_id", "owner_id", "label"),
        ReplicationKeyKind.DECLARED_PK,
        84,
        "jsonb+timestamptz",
    ),
    # SQLite v53 / PostgreSQL v31 added agent_profile_jobs.claim_token (the
    # claim generation). Nothing here moves: columns are parsed from the .sql
    # by postgres_catalog.py rather than restated in this manifest, the new
    # column joins no unique surface (so the REPLICATION_KEY park above still
    # resolves the same way), and a plain NOT NULL text column needs no
    # pipeline entry.
    _table(
        "agent_profile_jobs",
        ("notebook_id", "owner_id"),
        ReplicationKeyKind.DECLARED_PK,
        85,
        "timestamptz",
    ),
    # SQLite v54 / PostgreSQL v32: Agentic Memory P2's deployment-global
    # retrieval-strategy experience library. It has NO parent at all — no
    # notebook_id, no owner, no foreign key of any kind — so the appended rank
    # is FK-consistent by construction, and it has no incoming foreign key
    # either (it stays a leaf table).
    #
    # The declared replication key is the table's single-column, CONTENT-
    # ADDRESSED primary key, and that equality is the park precondition (see
    # the agent-profile note above): _build_unique_surfaces resolves the one
    # surface to REPLICATION_KEY, so no sentinel column, no nullable column and
    # no leaf delete/reinsert is involved. Adding a second unique surface —
    # notably a UNIQUE index over the situation fingerprint, which is the
    # tempting one — would move this table onto the sentinel/candidate-search
    # park path for nothing: the content-addressed primary key already makes
    # "one row per (situation, action)" true.
    _table(
        "retrieval_experiences",
        ("id",),
        ReplicationKeyKind.DECLARED_PK,
        86,
        "jsonb+timestamptz",
    ),
    # SQLite v55 / PostgreSQL v33: Agentic Memory P3's per-(notebook, owner,
    # agent) append-only observation log (Agentic Memory P3, T1 — zero
    # behavior change, no reader/writer yet). It FKs onto notebooks (rank 15)
    # only, so the appended rank stays FK-consistent, and it has no incoming
    # foreign key of its own (leaf table).
    #
    # The declared replication key is the table's single-column ``id``
    # primary key, matching the SQLite ``TEXT PRIMARY KEY`` declaration
    # verbatim — that equality is the park precondition:
    # _build_unique_surfaces resolves the surface to REPLICATION_KEY, so no
    # sentinel column, no nullable column and no leaf delete/reinsert is
    # involved for the primary key itself. The table's SECOND unique
    # surface — the partial ``idx_agent_observations_request`` idempotency
    # index over (notebook_id, owner_id, agent_profile_id,
    # client_request_id) — parks differently: it is pinned in
    # replicator.py's _UNIQUE_PREDICATES as a NULL park (the nullable
    # client_request_id column IS the park column; a write with no
    # client_request_id simply does not participate in the partial index).
    _table(
        "agent_observations",
        ("id",),
        ReplicationKeyKind.DECLARED_PK,
        87,
        "timestamptz",
    ),
    # Notebook rebuild stages are durable crash-safety state for one live
    # backend, not published products.  Copying or replaying them would carry
    # a worker/job lease across the backend cutover and, more subtly, would
    # turn their FK onto kg_build_jobs into an incoming dependency that makes
    # that table's existing one-running-job unique surface unparkable.
    TableSpec(
        "indexing_pipeline_stages",
        TableClass.LOCAL_EPHEMERAL,
        (),
        ReplicationKeyKind.DECLARED_PK,
        88,
    ),
    TableSpec(
        "indexing_pipeline_stage_sources",
        TableClass.LOCAL_EPHEMERAL,
        (),
        ReplicationKeyKind.DECLARED_PK,
        89,
    ),
    # SQLite v63 / PostgreSQL v41: the deployment-plugin runtime
    # enable/disable switch + audit. No FK of any kind (plugin_id is an
    # EXTENSIONS_CONFIG identifier, not a row in any other table), so the
    # appended rank is FK-consistent by construction and it stays a leaf
    # table. The declared replication key is the table's single-column
    # `plugin_id` primary key, matching the SQLite `TEXT PRIMARY KEY`
    # declaration verbatim — the park precondition (see the agent-profile
    # and retrieval-experience notes above): _build_unique_surfaces resolves
    # the one surface to REPLICATION_KEY, so no sentinel column, no nullable
    # column and no leaf delete/reinsert is involved.
    _table(
        "extension_runtime_toggles",
        ("plugin_id",),
        ReplicationKeyKind.DECLARED_PK,
        90,
        "timestamptz+boolean",
    ),
)

MANIFEST = Manifest(schema_pair=RUNNING_SCHEMA_PAIR, tables=_TABLES)


def _pipeline_names(value: str) -> tuple[str, ...]:
    return tuple(value.split("+"))


def replication_guard_specs(manifest: Manifest) -> tuple[ReplicationGuardSpec, ...]:
    """Return the reviewed physical definitions of all shadow-owned guards.

    The physical community-members permutation avoids stealing an existing hot
    access path.  Protocol key serialization continues to use the logical
    ``TableSpec.replication_key`` order.
    """
    guards: list[ReplicationGuardSpec] = []
    for spec in manifest.replicated:
        if spec.key_kind is not ReplicationKeyKind.SHADOW_UNIQUE:
            continue
        columns = (
            ("level", "canonical_id", "notebook_id")
            if spec.name == "community_members"
            else spec.replication_key
        )
        guards.append(
            ReplicationGuardSpec(
                name=f"shadow_uq_{spec.name}_replication_key",
                table=spec.name,
                columns=columns,
            )
        )
    return tuple(guards)


def validate_manifest(manifest: Manifest) -> None:
    names = [spec.name for spec in manifest.tables]
    if len(names) != len(set(names)):
        raise ValueError("manifest contains a duplicate table name")
    ranks = [spec.copy_rank for spec in manifest.tables]
    if len(ranks) != len(set(ranks)) or any(rank <= 0 for rank in ranks):
        raise ValueError("manifest copy ranks must be unique positive integers")
    for spec in manifest.tables:
        if not _IDENTIFIER.fullmatch(spec.name):
            raise ValueError(f"invalid manifest table name: {spec.name!r}")
        for column in (*spec.replication_key, *spec.blob_columns, *spec.path_columns):
            if not _IDENTIFIER.fullmatch(column):
                raise ValueError(f"invalid manifest column name: {spec.name}.{column}")
        if not set(spec.sqlite_null_guard_columns) <= set(spec.replication_key):
            raise ValueError(
                f"SQLite NULL guards must be stable-key columns: {spec.name}"
            )
        if spec.table_class is TableClass.REPLICATED and not spec.replication_key:
            raise ValueError(f"replicated table has no stable key: {spec.name}")
        if spec.table_class is not TableClass.REPLICATED and spec.replication_key:
            raise ValueError(
                f"non-replicated table declares a replication key: {spec.name}"
            )
        for pipeline in (spec.sqlite_to_postgres, spec.postgres_to_sqlite):
            names_in_pipeline = _pipeline_names(pipeline)
            if not names_in_pipeline or any(
                name not in COLUMN_TRANSFORMS for name in names_in_pipeline
            ):
                raise ValueError(f"unknown transform pipeline: {pipeline!r}")
    if frozenset(manifest.application_names) != frozenset(POSTGRES_BUSINESS_TABLES):
        raise ValueError(
            "manifest does not match the paired PostgreSQL business tables"
        )
    if manifest.schema_pair != RUNNING_SCHEMA_PAIR:
        raise ValueError("manifest uses an unsupported schema pair")
    if (
        manifest.schema_pair.sqlite_version != POSTGRES_SCHEMA_MANIFEST.sqlite_version
        or manifest.schema_pair.postgres_version
        != POSTGRES_SCHEMA_MANIFEST.postgres_version
    ):
        raise ValueError("shadow and repository schema pairs disagree")


def _quote_sqlite(identifier: str) -> str:
    if not _IDENTIFIER.fullmatch(identifier):
        raise ValueError(f"unsafe SQLite identifier: {identifier!r}")
    return f'"{identifier}"'


def _assert_key_rows(
    execute: Any,
    spec: TableSpec,
    *,
    placeholder: str = "?",
) -> None:
    del placeholder  # identifiers are manifest-owned; these scans take no values.
    table = _quote_sqlite(spec.name)
    columns = [_quote_sqlite(column) for column in spec.replication_key]
    null_predicate = " OR ".join(f"{column} IS NULL" for column in columns)
    if execute(f"SELECT 1 FROM {table} WHERE {null_predicate} LIMIT 1").fetchone():
        raise ValueError(f"nullable replication key in {spec.name}")
    column_sql = ", ".join(columns)
    if execute(
        f"SELECT 1 FROM {table} GROUP BY {column_sql} HAVING COUNT(*) > 1 LIMIT 1"
    ).fetchone():
        raise ValueError(f"duplicate replication key in {spec.name}")


def _row_value(row: Any, name: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[name]
    try:
        return row[position]
    except (IndexError, TypeError) as exc:
        raise ValueError(f"SQLite catalog row is missing {name}") from exc


def _sqlite_affinity(declared_type: object) -> str:
    value = str(declared_type or "").upper()
    if "INT" in value:
        return "INTEGER"
    if any(token in value for token in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if not value or "BLOB" in value:
        return "BLOB"
    if any(token in value for token in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def _sqlite_schema_sets(
    conn: sqlite3.Connection, manifest: Manifest
) -> tuple[set[str], set[str], set[str]]:
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    roots = {
        str(_row_value(row, "name", 0))
        for row in rows
        if re.match(
            r"(?is)^\s*CREATE\s+VIRTUAL\s+TABLE\b.*\bUSING\s+fts5\s*\(",
            str(_row_value(row, "sql", 1) or ""),
        )
    }
    table_kinds = {
        str(_row_value(row, "name", 1)): str(_row_value(row, "type", 2))
        for row in conn.execute("PRAGMA table_list").fetchall()
        if str(_row_value(row, "schema", 0)) == "main"
    }
    expected_shadow_names = {
        f"{root}_{suffix}" for root in roots for suffix in _FTS5_SHADOW_SUFFIXES
    }
    # PRAGMA table_list classifies virtual-table-owned auxiliaries as shadow.
    # Requiring both that catalog type and an exact FTS5 helper name avoids the
    # unsafe old prefix rule (e.g. chunks_fts_unreviewed_business is ordinary).
    rebuilt = roots | {
        name
        for name, kind in table_kinds.items()
        if kind == "shadow" and name in expected_shadow_names
    }
    declared_internal = {
        spec.name
        for spec in manifest.tables
        if spec.table_class is TableClass.SHADOW_INTERNAL
    }
    all_user_tables = {
        str(_row_value(row, "name", 0))
        for row in rows
        if not str(_row_value(row, "name", 0)).startswith("sqlite_")
    }
    actual_internal = all_user_tables & declared_internal
    ordinary = all_user_tables - rebuilt - actual_internal
    return ordinary, roots, actual_internal


def _validate_expected_pair(manifest: Manifest, expected_pair: SchemaPair) -> None:
    if expected_pair != manifest.schema_pair:
        raise ValueError(
            "expected schema pair does not match manifest: "
            f"expected={expected_pair}, manifest={manifest.schema_pair}"
        )


def _column_type(column: Mapping[str, Any]) -> str:
    value = column.get("postgres_type", column.get("data_type"))
    if not isinstance(value, str) or not value:
        raise ValueError("PostgreSQL column type metadata is missing")
    return value


def validate_sqlite_schema(
    conn: sqlite3.Connection,
    manifest: Manifest,
    *,
    expected_pair: SchemaPair,
    validate_rows: bool = True,
) -> SchemaValidationReport:
    """Validate the frozen SQLite schema at ``expected_pair`` and, by default, its key rows.

    The generation is never spelled here: ``_validate_expected_pair`` pins it to
    ``manifest.schema_pair``, which ``validate_manifest`` pins to
    ``RUNNING_SCHEMA_PAIR``.  A hardcoded number went stale twice before.


    ``validate_rows=False`` is reserved for short live-source fences that must
    validate catalog/control identity without scanning every business table
    while holding ``BEGIN IMMEDIATE``.  Snapshot and preflight callers retain
    the fail-closed row validation default.
    """
    validate_manifest(manifest)
    _validate_expected_pair(manifest, expected_pair)
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version != expected_pair.sqlite_version:
        raise ValueError(
            f"unsupported SQLite schema version {version}; "
            f"expected {expected_pair.sqlite_version}"
        )
    ordinary, rebuilt_roots, internal_tables = _sqlite_schema_sets(conn, manifest)
    expected_ordinary = set(manifest.application_names)
    if ordinary != expected_ordinary:
        raise ValueError(
            "SQLite ordinary-table totality mismatch: "
            f"missing={sorted(expected_ordinary - ordinary)}, "
            f"unknown={sorted(ordinary - expected_ordinary)}"
        )
    if rebuilt_roots != set(manifest.rebuilt_names):
        raise ValueError("SQLite rebuilt-family totality mismatch")
    expected_internal = {
        spec.name
        for spec in manifest.tables
        if spec.table_class is TableClass.SHADOW_INTERNAL
    }
    if internal_tables != expected_internal:
        raise ValueError("SQLite shadow-internal table totality mismatch")

    ranks = {spec.name: spec.copy_rank for spec in manifest.replicated}
    for spec in manifest.replicated:
        columns = {}
        for row in conn.execute(
            f"PRAGMA table_info({_quote_sqlite(spec.name)})"
        ).fetchall():
            name = str(_row_value(row, "name", 1))
            columns[name] = {
                "name": name,
                "type": str(_row_value(row, "type", 2) or ""),
                "notnull": bool(_row_value(row, "notnull", 3)),
                "pk": int(_row_value(row, "pk", 5)),
            }
        if not set(spec.replication_key) <= set(columns):
            raise ValueError(f"missing replication-key column in {spec.name}")
        declared_pk = tuple(
            str(row["name"])
            for row in sorted(columns.values(), key=lambda row: int(row["pk"]) or 999)
            if int(row["pk"])
        )
        if (
            spec.key_kind is ReplicationKeyKind.DECLARED_PK
            and declared_pk != spec.replication_key
        ):
            raise ValueError(f"declared primary key drift in {spec.name}")
        schema_nullable_keys = {
            column for column in spec.replication_key if not columns[column]["notnull"]
        }
        if schema_nullable_keys != set(spec.sqlite_null_guard_columns):
            raise ValueError(
                f"SQLite NULL-guard classification drift in {spec.name}: "
                f"schema_nullable={sorted(schema_nullable_keys)}, "
                f"declared={sorted(spec.sqlite_null_guard_columns)}"
            )
        if validate_rows:
            _assert_key_rows(conn.execute, spec)
        for foreign_key in conn.execute(
            f"PRAGMA foreign_key_list({_quote_sqlite(spec.name)})"
        ).fetchall():
            parent = str(_row_value(foreign_key, "table", 2))
            if parent in ranks and ranks[parent] >= spec.copy_rank:
                raise ValueError(
                    f"foreign-key copy-rank inversion: {parent}->{spec.name}"
                )

        declared_blobs = {f"{spec.name}.{column}" for column in spec.blob_columns}
        actual_blobs = {
            f"{spec.name}.{name}"
            for name, row in columns.items()
            if _sqlite_affinity(row["type"]) == "BLOB"
        }
        if not actual_blobs <= declared_blobs:
            raise ValueError(f"unclassified BLOB column in {spec.name}")
        if not set(spec.blob_columns) <= set(columns):
            raise ValueError(f"unknown declared BLOB column in {spec.name}")
        if not set(spec.path_columns) <= set(columns):
            raise ValueError(f"unknown declared filesystem path column in {spec.name}")

    qualified_columns = {
        f"{spec.name}.{column}"
        for spec in manifest.replicated
        for column in {
            str(_row_value(row, "name", 1))
            for row in conn.execute(
                f"PRAGMA table_info({_quote_sqlite(spec.name)})"
            ).fetchall()
        }
    }
    path_like = {
        name for name in qualified_columns if name.rsplit(".", 1)[1].endswith("_path")
    }
    if path_like - _FILESYSTEM_PATH_COLUMNS - _NON_FILESYSTEM_PATH_COLUMNS:
        raise ValueError("unclassified filesystem-like path column")
    declared_paths = {
        f"{spec.name}.{column}"
        for spec in manifest.replicated
        for column in spec.path_columns
    }
    if declared_paths != _FILESYSTEM_PATH_COLUMNS:
        raise ValueError("filesystem path manifest drift")
    return SchemaValidationReport(
        schema_version=version,
        ordinary_tables=frozenset(ordinary),
        rebuilt_roots=frozenset(rebuilt_roots),
        sqlite_null_guard_columns=frozenset(
            f"{spec.name}.{column}"
            for spec in manifest.replicated
            for column in spec.sqlite_null_guard_columns
        ),
    )


def validate_postgres_schema(
    *,
    tables: Mapping[str, Mapping[str, Any]],
    schema_version: int,
    manifest: Manifest,
    expected_pair: SchemaPair,
    validate_keys: bool = True,
) -> SchemaValidationReport:
    validate_manifest(manifest)
    _validate_expected_pair(manifest, expected_pair)
    if schema_version != expected_pair.postgres_version:
        raise ValueError(
            f"unsupported PostgreSQL schema version {schema_version}; "
            f"expected {expected_pair.postgres_version}"
        )
    ordinary = set(tables)
    expected = set(manifest.application_names)
    if ordinary != expected:
        raise ValueError(
            "PostgreSQL ordinary-table totality mismatch: "
            f"missing={sorted(expected - ordinary)}, unknown={sorted(ordinary - expected)}"
        )
    if validate_keys:
        ranks = {spec.name: spec.copy_rank for spec in manifest.replicated}
        for spec in manifest.replicated:
            facts = tables[spec.name]
            columns = {str(column["name"]): column for column in facts["columns"]}
            if not set(spec.replication_key) <= set(columns):
                raise ValueError(f"missing PostgreSQL replication key in {spec.name}")
            if any(
                bool(columns[column]["nullable"]) for column in spec.replication_key
            ):
                raise ValueError(f"nullable PostgreSQL replication key in {spec.name}")
            if (
                spec.key_kind is ReplicationKeyKind.DECLARED_PK
                and tuple(facts["primary_key"]) != spec.replication_key
            ):
                raise ValueError(f"PostgreSQL primary-key drift in {spec.name}")
            for foreign_key in facts["foreign_keys"]:
                parent = str(foreign_key["references_table"])
                if parent in ranks and ranks[parent] >= spec.copy_rank:
                    raise ValueError(
                        f"PostgreSQL foreign-key copy-rank inversion: "
                        f"{parent}->{spec.name}"
                    )

            selected = {
                _POSTGRES_SPECIAL_TYPES[column_type]
                for column in facts["columns"]
                if (column_type := _column_type(column)) in _POSTGRES_SPECIAL_TYPES
            }
            expected_transform = (
                "+".join(name for name in _TRANSFORM_ORDER if name in selected)
                or "identity"
            )
            if (
                spec.sqlite_to_postgres != expected_transform
                or spec.postgres_to_sqlite != expected_transform
            ):
                raise ValueError(
                    f"PostgreSQL transform classification drift in {spec.name}"
                )
            actual_bytea = {
                str(column["name"])
                for column in facts["columns"]
                if _column_type(column) == "bytea"
            }
            if actual_bytea != set(spec.blob_columns):
                raise ValueError(
                    f"PostgreSQL BLOB/bytea classification drift in {spec.name}"
                )
            if not set(spec.path_columns) <= set(columns):
                raise ValueError(
                    f"unknown PostgreSQL filesystem path column in {spec.name}"
                )

        qualified_columns = {
            f"{table}.{column['name']}"
            for table, facts in tables.items()
            for column in facts["columns"]
        }
        path_like = {
            name
            for name in qualified_columns
            if name.rsplit(".", 1)[1].endswith("_path")
        }
        if path_like - _FILESYSTEM_PATH_COLUMNS - _NON_FILESYSTEM_PATH_COLUMNS:
            raise ValueError("unclassified PostgreSQL filesystem-like path column")
        declared_paths = {
            f"{spec.name}.{column}"
            for spec in manifest.replicated
            for column in spec.path_columns
        }
        if declared_paths != _FILESYSTEM_PATH_COLUMNS:
            raise ValueError("PostgreSQL filesystem path manifest drift")
    return SchemaValidationReport(
        schema_version=schema_version,
        ordinary_tables=frozenset(ordinary),
    )


def validate_postgres_connection(
    conn: Any,
    manifest: Manifest,
    *,
    schema_version: int,
    expected_pair: SchemaPair,
    business_schema: str | None = None,
) -> SchemaValidationReport:
    """Validate live PG reverse totality and scan every stable key.

    This is deliberately separate from the static reviewed-contract check: the
    guard preflight must observe all rows and fail before capture is enabled.
    """
    validate_manifest(manifest)
    _validate_expected_pair(manifest, expected_pair)
    table_query = (
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema=current_schema() AND table_type='BASE TABLE' "
        "AND table_name <> 'silicon_schema_migrations' ORDER BY table_name"
        if business_schema is None
        else "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema=%s AND table_type='BASE TABLE' "
        "AND table_name <> 'silicon_schema_migrations' ORDER BY table_name"
    )
    table_names = [
        str(row["table_name"])
        for row in conn.execute(
            table_query,
            () if business_schema is None else (business_schema,),
        ).fetchall()
    ]
    tables: dict[str, dict[str, Any]] = {}
    for table in table_names:
        columns = [
            {
                "name": str(row["column_name"]),
                "nullable": str(row["is_nullable"]) == "YES",
                "data_type": str(row["data_type"]),
            }
            for row in conn.execute(
                "SELECT column_name, is_nullable, data_type "
                "FROM information_schema.columns "
                + (
                    "WHERE table_schema=current_schema() AND table_name=%s "
                    if business_schema is None
                    else "WHERE table_schema=%s AND table_name=%s "
                )
                + "ORDER BY ordinal_position",
                (table,) if business_schema is None else (business_schema, table),
            ).fetchall()
            if str(row["column_name"]) != "ordinal"
        ]
        primary_key_row = conn.execute(
            "SELECT ARRAY("
            " SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY k(attnum, ord) "
            " JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum "
            " ORDER BY k.ord) AS columns "
            "FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid "
            "JOIN pg_namespace n ON n.oid=t.relnamespace "
            + (
                "WHERE n.nspname=current_schema() AND t.relname=%s AND c.contype='p'"
                if business_schema is None
                else "WHERE n.nspname=%s AND t.relname=%s AND c.contype='p'"
            ),
            (table,) if business_schema is None else (business_schema, table),
        ).fetchone()
        foreign_keys = [
            {"references_table": str(row["references_table"])}
            for row in conn.execute(
                "SELECT parent.relname AS references_table "
                "FROM pg_constraint c JOIN pg_class child ON child.oid=c.conrelid "
                "JOIN pg_namespace n ON n.oid=child.relnamespace "
                "JOIN pg_class parent ON parent.oid=c.confrelid "
                + (
                    "WHERE n.nspname=current_schema() AND child.relname=%s "
                    if business_schema is None
                    else "WHERE n.nspname=%s AND child.relname=%s "
                )
                + "AND c.contype='f' ORDER BY c.conname",
                (table,) if business_schema is None else (business_schema, table),
            ).fetchall()
        ]
        tables[table] = {
            "columns": columns,
            "primary_key": (
                list(primary_key_row["columns"]) if primary_key_row is not None else []
            ),
            "foreign_keys": foreign_keys,
        }

    report = validate_postgres_schema(
        tables=tables,
        schema_version=schema_version,
        manifest=manifest,
        expected_pair=expected_pair,
    )
    for spec in manifest.replicated:
        if business_schema is None:
            _assert_key_rows(conn.execute, spec, placeholder="%s")
            continue
        table = sql.SQL("{}.{}").format(
            sql.Identifier(business_schema), sql.Identifier(spec.name)
        )
        columns = tuple(sql.Identifier(column) for column in spec.replication_key)
        nulls = sql.SQL(" OR ").join(
            sql.SQL("{} IS NULL").format(column) for column in columns
        )
        if conn.execute(
            sql.SQL("SELECT 1 FROM {} WHERE {} LIMIT 1").format(table, nulls)
        ).fetchone():
            raise ValueError(f"nullable replication key in {spec.name}")
        if conn.execute(
            sql.SQL("SELECT 1 FROM {} GROUP BY {} HAVING COUNT(*) > 1 LIMIT 1").format(
                table, sql.SQL(", ").join(columns)
            )
        ).fetchone():
            raise ValueError(f"duplicate replication key in {spec.name}")
    return report


def manifest_contract(manifest: Manifest) -> dict[str, Any]:
    validate_manifest(manifest)
    return {
        "schema_pair": {
            "sqlite_version": manifest.schema_pair.sqlite_version,
            "postgres_version": manifest.schema_pair.postgres_version,
            "epoch": manifest.schema_pair.epoch,
        },
        "tables": [
            {
                "name": spec.name,
                "table_class": spec.table_class.value,
                "replication_key": list(spec.replication_key),
                "key_kind": spec.key_kind.value,
                "copy_rank": spec.copy_rank,
                "sqlite_to_postgres": spec.sqlite_to_postgres,
                "postgres_to_sqlite": spec.postgres_to_sqlite,
            }
            for spec in manifest.tables
        ],
    }


validate_manifest(MANIFEST)
