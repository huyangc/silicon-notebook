"""Static guard: a statement that binds an id list goes through ``id_binding``.

The rule (``app/repositories/postgres/id_binding.py``,
``app/repositories/sqlite/id_binding.py``, ``docs/development.md`` "Binding id
lists in SQL"): a statement whose id list is sized by the data -- above all a
run's frozen source ceiling, up to every source of a notebook -- binds it
through the backend's ``id_binding`` module.  A list may be bound directly only
when it falls in one of three reviewed classes, and every such call site is
listed below with its class and reason.

What this scans (every ``.py`` under ``app/repositories/postgres`` and
``app/repositories/sqlite`` except the two ``id_binding`` modules), by reading
the AST -- string constants for SQL, calls for placeholder expansion:

* PostgreSQL: ``ANY(%s``, ``ALL(%s``, ``unnest(%s`` inside a SQL string, a
  list passed as one scalar and unpacked by the server
  (``json[b]_array_elements[_text](%s``, ``string_to_array(%s``), and
  ``%s`` placeholder expansion (``",".join("%s" for ...)``, ``["%s"] * n``,
  the ``placeholders(values)`` helper);
* SQLite: ``?`` placeholder expansion (``",".join("?" for ...)``,
  ``"?" * n``) and an inline ``json_each(?)``.

Docstrings are skipped; an expansion over a module-level CONSTANT (a fixed
status set) is skipped.  Sites are identified semantically -- (file,
enclosing function, kind) and the reviewed count of that kind -- never by line
number, so moving code inside a function does not disturb the list; adding a
site, or moving one to another function, does.  Converted statements say
``member_of(col, bound)`` / ``drive_by(col, bound)``; their SQL text carries
``bound.sql`` and never matches.
"""
from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

import pytest

REPOSITORIES = Path(__file__).resolve().parents[1] / "app" / "repositories"
BINDING_MODULES = {"postgres/id_binding.py", "sqlite/id_binding.py"}

_PG_ARRAY = re.compile(r"\b(ANY|ALL)\s*\(\s*%s|\b(unnest)\s*\(\s*%s", re.IGNORECASE)
# A list smuggled in as one scalar and unpacked by the server: a JSON array or
# a joined string.  ``bind_ids`` owns the joined-string form.
_PG_UNPACK = re.compile(
    r"\b(jsonb?_array_elements(?:_text)?|string_to_array)\s*\(\s*%s", re.IGNORECASE
)
_SQLITE_JSON = re.compile(r"json_each\s*\(\s*\?\s*\)", re.IGNORECASE)
_MARK = {"postgres": "%s", "sqlite": "?"}

# The three exemption classes (the same on both backends) plus "not an id
# list" for placeholder expansions that bind column values, not ids.
BOUNDED = "bounded by construction"
BATCHED = "batched key probe"
DRIVEN = "driven by the list, one parameter"
NOT_IDS = "not an id list"
CLASSES = {BOUNDED, BATCHED, DRIVEN, NOT_IDS}

ANY = "ANY(%s)"
PG_EXP = "%s-expansion"
SQ_EXP = "?-expansion"
JSON = "json_each(?)"
JSONB = "jsonb_array_elements_text(%s)"
PG_KINDS = {ANY, "ALL(%s)", "unnest(%s)", PG_EXP, JSONB,
            "jsonb_array_elements(%s)", "json_array_elements(%s)",
            "json_array_elements_text(%s)", "string_to_array(%s)"}


def s(kind: str, klass: str, why: str) -> tuple[str, str, str]:
    return (kind, klass, why)


# Reviewed exemptions: (file, function) -> one entry per site (kind, class,
# reason); the guard compares the count of each kind.
EXEMPT: dict[tuple[str, str], tuple[tuple[str, str, str], ...]] = {
    # ------------------------------------------------------------ PostgreSQL
    ("postgres/_store_utils.py", "placeholders"): (
        s(PG_EXP, NOT_IDS, "the expansion helper itself; each call is listed"),),
    ("postgres/ask_state_store.py", "AskStateStore.recent_user_ask_traces"): (
        s(PG_EXP, BOUNDED, "job ids of the page just read under LIMIT"),),
    ("postgres/ask_state_store.py", "AskStateStore.recent_completed_ask_runs"): (
        s(PG_EXP, BOUNDED, "run ids of the page just read under LIMIT"),),
    ("postgres/ask_state_store.py", "AskStateStore.recent_user_report_traces"): (
        s(PG_EXP, BOUNDED, "report ids of the page just read under LIMIT"),),
    ("postgres/catalog_store.py", "CatalogStore.candidates_by_ids"): (
        s(ANY, BOUNDED, "capped at CATALOG_MAX_CANDIDATE_PAGE"),),
    ("postgres/catalog_store.py", "CatalogStore.mark_candidates_applied"): (
        s(ANY, BOUNDED, "capped at CATALOG_MAX_CANDIDATE_PAGE"),),
    ("postgres/catalog_store.py", "CatalogStore.mark_candidates_dismissed"): (
        s(ANY, BOUNDED, "capped at CATALOG_MAX_CANDIDATE_PAGE"),),
    ("postgres/chunk_store.py", "ChunkStore.chunks_for_element_ids"): (
        s(ANY, BATCHED, "element ids, CHUNK_ELEMENT_LOOKUP_BATCH (500) per statement"),),
    ("postgres/chunk_store.py", "ChunkStore.delete_by_ids"): (
        s(ANY, DRIVEN, "a re-projected knowhow table's chunk primary keys"),),
    ("postgres/chunk_store.py", "ChunkStore.hydrate_rows"): (
        s(PG_EXP, BATCHED, "candidate chunk keys; callers pass one _in_batches window"),),
    ("postgres/chunk_store.py", "ChunkStore.graph_hydrate_rows"): (
        s(PG_EXP, BOUNDED, "the PPR top-chunk window"),),
    ("postgres/chunk_store.py", "ChunkStore.retrieval_contribution_rows"): (
        s(PG_EXP, BATCHED, "candidate chunk keys, one _in_batches window (<= 900)"),),
    ("postgres/chunk_store.py", "ChunkStore.rows_by_ids"): (
        s(PG_EXP, BOUNDED, "evidence chunks of the ranked KG object window"),),
    ("postgres/embedding_store.py", "EmbeddingStore.vector_rows_for_ids"): (
        s(ANY, BATCHED, "vector keys; every caller batches (in_batches / _IN_CHUNK)"),),
    ("postgres/embedding_store.py", "EmbeddingStore.relation_delta_rows"): (
        s(ANY, BATCHED, "delta source ids, callers batch with _in_batches (<= 900)"),),
    ("postgres/embedding_store.py", "EmbeddingStore.knowledge_delta_rows"): (
        s(ANY, BATCHED, "delta source ids, callers batch with _in_batches (<= 900)"),),
    ("postgres/embedding_store.py", "EmbeddingStore.element_delta_rows"): (
        s(ANY, BATCHED, "delta source ids, same batching contract as its siblings"),),
    ("postgres/embedding_store.py", "EmbeddingStore.chunk_delta_rows"): (
        s(ANY, BATCHED, "delta source ids, callers batch with _in_batches (<= 900)"),),
    ("postgres/embedding_store.py", "EmbeddingStore.rows_by_ids"): (
        s(ANY, DRIVEN, "vector primary keys (a knowhow table's unchanged chunks)"),),
    ("postgres/governance_store.py", "GovernanceStore.sweep_orphan_clusters_page"): (
        s(ANY, BOUNDED, "cluster ids of the page just read under LIMIT"),),
    ("postgres/governance_store.py", "GovernanceStore.merge_candidate_pairs_for_canonicals"): (
        s(PG_EXP, BATCHED, "canonical ids, the caller batches with _in_fuse_batches"),),
    ("postgres/governance_store.py", "GovernanceStore.review_queue_rows"): (
        s(ANY, BATCHED, "endpoint ids, _REVIEW_ENDPOINT_LOOKUP_BATCH per statement"),),
    ("postgres/governance_store.py", "GovernanceStore._existing_cluster_members"): (
        s(PG_EXP, BATCHED, "member ids, in_chunk_size per statement"),),
    ("postgres/governance_store.py", "GovernanceStore.promotion_object_rows"): (
        s(PG_EXP, BATCHED, "object ids, the caller batches with _IN_CHUNK"),),
    ("postgres/governance_store.py", "GovernanceStore.notebook_name_rows"): (
        s(PG_EXP, BATCHED, "notebook ids, the caller batches with _IN_CHUNK"),),
    ("postgres/governance_store.py", "GovernanceStore.conflict_relation_evidence_rows"): (
        s(PG_EXP, BATCHED, "relation ids, the caller batches with _batches"),),
    ("postgres/identity_store.py", "IdentityStore.audit_labels_for_user_ids"): (
        s(ANY, BOUNDED, "capped at 512 user ids, 200 per statement"),),
    ("postgres/index_projection_store.py", "IndexProjectionStore.chunk_sources_for_ids"): (
        s(PG_EXP, BATCHED, "chunk ids, in_batches per statement"),),
    ("postgres/index_projection_store.py", "IndexProjectionStore.visible_source_ids"): (
        s(PG_EXP, BATCHED, "source ids, in_batches per statement"),),
    ("postgres/index_projection_store.py", "IndexProjectionStore.delta_chunk_count"): (
        s(PG_EXP, BATCHED, "delta source ids, in_batches per statement"),),
    ("postgres/index_projection_store.py", "IndexProjectionStore.relation_ids_for_source_batch"): (
        s(PG_EXP, BATCHED, "source ids, the caller batches with in_batches"),),
    ("postgres/index_projection_store.py", "IndexProjectionStore.graph_rows"): (
        s(PG_EXP, BATCHED, "source ids, in_batches per statement"),),
    ("postgres/kg_build_job_store.py", "KgBuildJobStore._delete_source_kg"): tuple(
        s(ANY, BOUNDED, "object ids of one delete page (INDEXING_PIPELINE_PUBLISH_DELETE_BATCH)")
        for _ in range(8)),
    ("postgres/kg_build_job_store.py", "KgBuildJobStore._validate_stage_payloads"): (
        s(ANY, DRIVEN, "one staged source's element primary keys"),),
    ("postgres/kg_build_job_store.py", "KgBuildJobStore.publish_indexing_pipeline_success"): (
        s(ANY, BOUNDED, "the source ids of one indexing publish"),),
    ("postgres/knowhow_store.py", "KnowhowStore.knowhow_table_health_inputs"): tuple(
        s(ANY, DRIVEN, "every knowhow table of the notebook, the keys read")
        for _ in range(3)),
    ("postgres/knowhow_store.py", "KnowhowStore.enumerate_knowhow_rows"): (
        s(PG_EXP, BOUNDED, "<= KNOWHOW_ENUMERATION_MAX_TABLE_IDS (8) tables"),
        s(PG_EXP, BOUNDED, "the visible subset of those <= 8 tables"),
        s(PG_EXP, BOUNDED, "<= KNOWHOW_ENUMERATION_MAX_COLUMN_IDS (8) columns"),
        s(PG_EXP, BOUNDED, "row ids of one page (<= KNOWHOW_ENUMERATION_PAGE_SIZE_MAX)"),
        s(PG_EXP, BOUNDED, "<= KNOWHOW_ENUMERATION_MAX_COLUMN_IDS (8) columns"),
    ),
    ("postgres/knowhow_store.py", "KnowhowStore.get_knowhow_table"): (
        s(ANY, DRIVEN, "every row of one table, the (row, column) keys read"),),
    ("postgres/knowhow_store.py", "KnowhowStore.knowhow_anchor_existing_values"): (
        s(ANY, BOUNDED, "capped at CATALOG_MAX_CANDIDATE_PAGE anchor values"),),
    ("postgres/knowhow_store.py", "KnowhowStore.append_knowhow_rows_skipping_existing_anchors"): (
        s(ANY, BOUNDED, "anchor values of one appended candidate page"),),
    ("postgres/knowhow_store.py", "KnowhowStore.update_knowhow_cells"): (
        s(ANY, DRIVEN, "one merged-cell group's rows, (row, column) key probes"),),
    ("postgres/knowledge_store.py", "KnowledgeStore.drain_notebook_graph_rows_page"): tuple(
        s(ANY, BOUNDED, "ids of one drain page, _DELETE_OBJECT_BATCH_SIZE per statement")
        for _ in range(6)),
    ("postgres/knowledge_store.py", "KnowledgeStore.relink_relation_rows_for_objects"): (
        s(PG_EXP, BATCHED, "object ids, the caller batches with _in_relink_batches"),),
    ("postgres/knowledge_store.py", "KnowledgeStore.embedding_rows_for_objects"): (
        s(PG_EXP, BOUNDED, "ANN anchors / one source's new objects"),),
    ("postgres/knowledge_store.py", "KnowledgeStore.valid_object_ids"): (
        s(PG_EXP, BOUNDED, "the hits of one bounded k-NN probe"),),
    ("postgres/knowledge_store.py", "KnowledgeStore.neighbor_relation_rows"): (
        s(ANY, DRIVEN, "one cluster's member objects, endpoint key probes"),
        s(ANY, DRIVEN, "one cluster's member objects, endpoint key probes"),
    ),
    ("postgres/knowledge_store.py", "KnowledgeStore.object_meta_rows_for_notebook"): (
        s(PG_EXP, BOUNDED, "representatives of one neighbourhood / a single id"),),
    ("postgres/knowledge_store.py", "KnowledgeStore.community_context_rows"): (
        s(ANY, DRIVEN, "one community's members, primary-key probes"),
        s(ANY, DRIVEN, "one community's members, endpoint key probes"),
        s(ANY, DRIVEN, "one community's members, endpoint key probes"),
    ),
    ("postgres/knowledge_store.py", "KnowledgeStore.relation_context_rows"): (
        s(PG_EXP, BATCHED, "relation ids, batch_size per statement"),),
    ("postgres/knowledge_store.py", "KnowledgeStore.usable_object_rows_on"): (
        s(PG_EXP, BOUNDED, "the caller's usable status set"),
        s(PG_EXP, BATCHED, "object ids, batch_size (500) per statement"),
    ),
    ("postgres/knowledge_store.py", "KnowledgeStore.graph_object_rows"): (
        s(PG_EXP, BOUNDED, "the caller's usable status set"),),
    ("postgres/knowledge_store.py", "KnowledgeStore.object_evidence_rows"): (
        s(PG_EXP, BATCHED, "object ids; callers batch or pass the ranked window"),),
    ("postgres/knowledge_store.py", "KnowledgeStore.follow_start_row"): (
        s(PG_EXP, BOUNDED, "the caller's usable status set"),
        s(PG_EXP, BOUNDED, "one run's participant notebooks (<= 8)"),
    ),
    ("postgres/knowledge_store.py", "KnowledgeStore.follow_relation_evidence_rows"): (
        s(PG_EXP, BATCHED, "relation ids, the caller batches"),),
    ("postgres/knowledge_store.py", "KnowledgeStore.follow_object_rows"): (
        s(PG_EXP, BATCHED, "object ids, the caller batches"),
        s(PG_EXP, BOUNDED, "the caller's usable status set"),
    ),
    ("postgres/knowledge_store.py", "KnowledgeStore.in_network_relation_rows"): (
        s(PG_EXP, BOUNDED, "the object ids of one reasoning graph window"),),
    ("postgres/knowledge_store.py", "KnowledgeStore.retrieval_objects"): (
        s(PG_EXP, BOUNDED, "the caller's status set"),
        s(PG_EXP, BATCHED, "id filter, batch_size (900) per statement"),
    ),
    ("postgres/knowledge_store.py", "KnowledgeStore.duplicate_member_rows"): (
        s(ANY, BATCHED, "object ids, batch_size per statement"),),
    ("postgres/knowledge_store.py", "KnowledgeStore._element_texts"): (
        s(PG_EXP, BOUNDED, "step elements of <= 500 candidate procedures of one object"),),
    ("postgres/knowledge_store.py", "KnowledgeStore._enrich_evidence"): (
        s(PG_EXP, BOUNDED, "the element ids of one object's evidence list"),),
    ("postgres/knowledge_store.py", "KnowledgeStore.count_knowledge"): (
        s(PG_EXP, BOUNDED, "the caller's status set"),),
    ("postgres/knowledge_store.py", "KnowledgeStore.validate_source_fact_publish"): (
        s(ANY, DRIVEN, "one source's element primary keys"),),
    ("postgres/knowledge_store.py", "KnowledgeStore.validate_stage_source_elements"): (
        s(ANY, DRIVEN, "one source's element primary keys"),),
    ("postgres/knowledge_store.py", "KnowledgeStore.completion_validate_scope"): (
        s(ANY, BOUNDED, "object ids of one relation-completion page"),
        s(ANY, BOUNDED, "evidence element ids of that page"),
    ),
    ("postgres/knowledge_store.py", "KnowledgeStore.completion_existing_keys"): (
        s(ANY, BOUNDED, "endpoint ids of one relation-completion page"),
        s(ANY, BOUNDED, "endpoint ids of one relation-completion page"),
    ),
    ("postgres/knowledge_store.py", "KnowledgeStore.completion_page"): (
        s(ANY, NOT_IDS, "the edge-contract type sets"),
        s(ANY, NOT_IDS, "the edge-contract type sets"),
        s(ANY, NOT_IDS, "the edge-contract type sets"),
        s(ANY, NOT_IDS, "the edge-contract type sets"),
    ),
    ("postgres/knowledge_store.py", "KnowledgeStore.completion_candidate_rows"): (
        s(ANY, BOUNDED, "candidate ids capped at the completion over-fetch"),),
    ("postgres/knowledge_store.py", "KnowledgeStore.completion_element_rows"): (
        s(ANY, BOUNDED, "evidence of one page, COMPLETION_EVIDENCE_PER_OBJECT each"),),
    ("postgres/knowledge_store.py", "KnowledgeStore.prune_cluster_rows_for_source"): (
        s(ANY, BATCHED, "member ids, _DELETE_OBJECT_BATCH_SIZE per statement"),),
    ("postgres/knowledge_store.py", "KnowledgeStore.legacy_typed_table_ids"): (
        s(PG_EXP, NOT_IDS, "the legacy object-type vocabulary"),),
    ("postgres/knowledge_store.py", "KnowledgeStore.delete_object_sources"): (
        s(ANY, BATCHED, "object ids, _DELETE_OBJECT_BATCH_SIZE per statement"),),
    ("postgres/knowledge_store.py", "KnowledgeStore._delete_object_id_batch"): tuple(
        s(ANY, BATCHED, "object ids, capped at _DELETE_OBJECT_BATCH_SIZE")
        for _ in range(3)),
    ("postgres/knowledge_store.py", "KnowledgeStore.object_meta_rows"): (
        s(PG_EXP, BOUNDED, "the ranked search-hit window"),),
    ("postgres/knowledge_store.py", "KnowledgeStore.edge_centrality_source_rows"): (
        s(ANY, BOUNDED, "the top max_nodes ids"),
        s(ANY, BOUNDED, "the top max_nodes ids"),
        s(ANY, BOUNDED, "the top max_nodes ids"),
    ),
    ("postgres/knowledge_store.py", "KnowledgeStore.concept_neighbor_rows"): (
        s(ANY, DRIVEN, "one hub cluster's members, endpoint key probes"),
        s(ANY, DRIVEN, "one hub cluster's members, endpoint key probes"),
        s(ANY, BATCHED, "attached candidates, batch_size per statement"),
        s(PG_EXP, BATCHED, "attached candidates, batch_size per statement"),
    ),
    ("postgres/knowledge_store.py", "KnowledgeStore.concept_cluster_detail_rows"): (
        s("unnest(%s)", BOUNDED, "the checked node ids of one viewer response"),),
    ("postgres/knowledge_store.py", "_element_rows"): (
        s(PG_EXP, BATCHED,
          "element ids: one _ELEMENT_BATCH (900) window or one procedure's steps"),),
    ("postgres/maintenance.py", "PostgresMaintenanceAdapter.count_missing_chunk_vectors"): (
        s(PG_EXP, BOUNDED, "sources in flight in this process (ingest leases)"),),
    ("postgres/maintenance.py", "PostgresMaintenanceAdapter.count_missing_element_vectors"): (
        s(PG_EXP, BOUNDED, "sources in flight in this process (ingest leases)"),),
    ("postgres/maintenance.py", "PostgresMaintenanceAdapter.chunk_texts_by_ids"): (
        s(ANY, BOUNDED, "one page of the caller's discovery walk"),),
    ("postgres/maintenance.py", "PostgresMaintenanceAdapter.element_texts_by_ids"): (
        s(ANY, BOUNDED, "one page of the caller's discovery walk"),),
    ("postgres/maintenance.py", "PostgresMaintenanceAdapter.image_backfill_discard_assets"): (
        s(ANY, BOUNDED, "assets written by one source's image backfill"),),
    ("postgres/maintenance.py", "PostgresMaintenanceAdapter.backfill_source_fact_batch"): (
        s(ANY, BOUNDED, "element ids of one fact batch of one source"),
        s(ANY, BOUNDED, "local object ids of that fact batch"),
    ),
    ("postgres/memory_store.py", "MemoryStore.answer_memory_links"): (
        s(PG_EXP, BOUNDED, "capped at 200 answer ids"),),
    ("postgres/memory_store.py", "MemoryStore._mutate_with_revision"): (
        s(PG_EXP, NOT_IDS, "the expected lifecycle statuses"),),
    ("postgres/memory_store.py", "MemoryStore.promotion_rows_on"): (
        s(ANY, DRIVEN, "the promotion queue's memory primary keys"),),
    ("postgres/memory_store.py", "MemoryStore.transition"): (
        s(PG_EXP, NOT_IDS, "the expected lifecycle statuses"),),
    ("postgres/memory_store.py", "MemoryStore.bulk_delete_memories"): (
        s(PG_EXP, BOUNDED, "capped at 200 memory ids"),),
    ("postgres/memory_store.py", "MemoryStore.list_memories"): (
        s(ANY, BOUNDED, "candidate ids of one page"),),
    ("postgres/memory_store.py", "MemoryStore.memory_retrieval_rows"): (
        s(PG_EXP, NOT_IDS, "the allowed memory statuses"),
        s(ANY, BOUNDED, "lexical candidates capped at lexical_limit (<= 200)"),
    ),
    ("postgres/notebook_delete_job_store.py", "NotebookDeleteJobStore.recreate_for_deleting_notebook"): (
        s(ANY, BOUNDED, "one notebook's failed delete jobs"),
        s(ANY, BOUNDED, "one notebook's failed delete jobs"),
    ),
    ("postgres/notebook_delete_job_store.py", "NotebookDeleteJobStore.delete_direct_page_form_one"): (
        s(ANY, BOUNDED, "ids of the page just read under LIMIT"),),
    ("postgres/notebook_delete_job_store.py", "NotebookDeleteJobStore.delete_knowhow_rows_page"): (
        s(ANY, BOUNDED, "row ids of one delete page"),),
    ("postgres/notebook_delete_job_store.py", "NotebookDeleteJobStore.delete_knowhow_tables_page"): (
        s(ANY, BOUNDED, "table ids of one delete page"),),
    ("postgres/notebook_delete_job_store.py", "NotebookDeleteJobStore.delete_indexing_pipeline_stages_page"): (
        s(ANY, BOUNDED, "job ids of one delete page"),),
    ("postgres/notebook_delete_job_store.py", "NotebookDeleteJobStore.delete_memory_items_page"): tuple(
        s(ANY, BOUNDED, "memory ids of the page just read under LIMIT")
        for _ in range(4)),
    ("postgres/notebook_delete_job_store.py", "NotebookDeleteJobStore._drain_children_by_parent_ids"): (
        s(ANY, BOUNDED, "parent ids of one delete page"),),
    ("postgres/notebook_store.py", "NotebookStore.tier_map"): (
        s(PG_EXP, BOUNDED, "the notebooks of one evidence set (participants)"),),
    ("postgres/notebook_store.py", "NotebookStore._retain_user_activity_before_delete"): (
        s(PG_EXP, NOT_IDS, "INSERT column values"),),
    ("postgres/query_store.py", "QueryStore.knowledge_type_count_rows_for_sources"): (
        s(ANY, DRIVEN, "one notebook's Memory source ids, the keys counted"),
        s(ANY, NOT_IDS, "the caller's status set"),
    ),
    ("postgres/query_store.py", "QueryStore.knowledge_type_count_rows_excluding_memory"): (
        s(ANY, NOT_IDS, "the caller's status set"),),
    ("postgres/query_store.py", "QueryStore.top_concept_names"): (
        s(ANY, NOT_IDS, "the caller's status set"),),
    ("postgres/query_store.py", "QueryStore.knowhow_knowledge_type_rows"): (
        s(ANY, NOT_IDS, "the caller's status set"),),
    ("postgres/query_store.py", "QueryStore.notebook_has_usable_kg"): (
        s(ANY, NOT_IDS, "USABLE_STATUSES"),),
    ("postgres/query_store.py", "QueryStore.notebook_has_usable_base_kg"): (
        s(ANY, NOT_IDS, "USABLE_STATUSES"),),
    ("postgres/query_store.py", "QueryStore.notebook_source_ids_among"): (
        s(ANY, BOUNDED, "sources in flight in this process (ingest leases)"),),
    ("postgres/query_store.py", "QueryStore.list_user_notebooks"): (
        s(PG_EXP, BOUNDED, "one user's own notebooks"),),
    ("postgres/query_store.py", "QueryStore.list_user_activity"): (
        s(PG_EXP, BOUNDED, "one user's own notebooks"),
        s(PG_EXP, BOUNDED, "source ids of one activity page"),
    ),
    ("postgres/query_store.py", "QueryStore.pending_actions_projection_rows"): (
        s(PG_EXP, BOUNDED, "one user's own notebooks"),),
    ("postgres/report_store.py", "ReportStore.export_reports"): (
        s(PG_EXP, BATCHED, "report ids, _in_batches per statement"),),
    ("postgres/retrieval_experience_store.py", "RetrievalExperienceStore.note_adopted"): (
        s(ANY, BOUNDED, "experiences adopted by one run"),),
    ("postgres/retrieval_indexes.py", "inspect_retrieval_indexes"): (
        s(ANY, NOT_IDS, "the two required extension names"),),
    ("postgres/search.py", "knowledge_candidate_documents"): (
        s(ANY, BOUNDED, "the ranked candidate window (<= per-term limit)"),),
    ("postgres/search.py", "chunk_candidate_documents"): (
        s(ANY, BOUNDED, "the ranked candidate window (<= per-term limit)"),),
    ("postgres/search.py", "_memory_match_predicates"): (
        s(ANY, NOT_IDS, "the scope's memory statuses"),),
    ("postgres/sharing_store.py", "SharingStore.readable_notebook_ids"): (
        s(ANY, BOUNDED, "a global run's notebooks (<= global_ask_max_notebooks, 8)"),),
    ("postgres/sharing_store.py", "SharingStore.valid_copied_mount_base_ids"): (
        s(PG_EXP, BATCHED, "base notebook ids, 400 per statement"),),
    ("postgres/source_store.py", "SourceStore.visible_source_ids_by_notebook"): (
        s(ANY, BOUNDED, "a global run's participant notebooks (<= 8)"),),
    ("postgres/source_store.py", "SourceStore.visible_source_scope_snapshot"): (
        s(JSONB, DRIVEN, "the requested source ids with their ordinals, primary-key probes"),),
    ("postgres/source_store.py", "SourceStore.visible_source_owners"): (
        s(JSONB, DRIVEN, "one answer's cited source ids, primary-key probes"),),
    ("postgres/source_store.py", "SourceStore.element_type_count_rows"): (
        s(ANY, BATCHED, "source ids, COUNT_IN_CHUNK (1024) per statement"),
        s(ANY, NOT_IDS, "the requested element types"),
        s(ANY, BATCHED, "source ids, COUNT_IN_CHUNK (1024) per statement"),
    ),
    ("postgres/source_store.py", "SourceStore.source_display_rows"): (
        s(ANY, BATCHED, "source ids, COUNT_IN_CHUNK (1024) per statement"),),
    ("postgres/source_store.py", "SourceStore.evidence_elements"): (
        s(PG_EXP, BATCHED, "element ids, IN_CHUNK per statement"),),
    ("postgres/source_store.py", "SourceStore.evidence_fingerprints"): (
        s(PG_EXP, BATCHED, "element ids, IN_CHUNK per statement"),),
    ("postgres/source_store.py", "SourceStore.passage_evidence_snapshot"): (
        s(PG_EXP, BATCHED, "chunk ids, IN_CHUNK per statement"),),
    ("postgres/source_store.py", "SourceStore.image_asset_rows"): (
        s(PG_EXP, BATCHED, "element ids, IN_CHUNK per statement"),),
    ("postgres/source_store.py", "SourceStore.source_listing_rows"): (
        s(PG_EXP, BATCHED, "source ids, IN_CHUNK per statement"),),
    ("postgres/source_store.py", "SourceStore.report_source_identity_rows"): (
        s(ANY, BOUNDED, "truncated to 1024 source ids"),),
    ("postgres/source_store.py", "SourceStore.source_titles"): (
        s(PG_EXP, BOUNDED, "a selected-source graph's sources (<= 32)"),),
    ("postgres/source_store.py", "SourceStore.sources_from_rows"): (
        s(PG_EXP, BATCHED, "source ids, IN_CHUNK per statement"),),
    ("postgres/source_store.py", "SourceStore.paper_meta_for_sources"): (
        s(PG_EXP, BATCHED, "source ids, IN_CHUNK per statement"),),
    ("postgres/unified_kg_store.py", "UnifiedKgStore.cluster_evidence_rows"): (
        s(PG_EXP, BATCHED, "seed ids of one cluster batch"),),
    ("postgres/unified_kg_store.py", "UnifiedKgStore.reap_derived_generations_page"): (
        s(PG_EXP, NOT_IDS, "the keyset cursor's key columns"),),
    ("postgres/unified_kg_store.py", "UnifiedKgStore.cluster_fold_rows"): (
        s(PG_EXP, BATCHED, "hit ids; callers pass a ranked window or batch by 900"),),
    ("postgres/unified_kg_store.py", "UnifiedKgStore.weak_support_relation_rows"): (
        s(PG_EXP, BOUNDED, "the canonical seeds of one gap probe"),),
    ("postgres/unified_kg_store.py", "UnifiedKgStore.relation_endpoint_name_rows"): (
        s(PG_EXP, BOUNDED, "sample relations of one gap probe (<= probe limit)"),),
    ("postgres/unified_kg_store.py", "UnifiedKgStore.discard_board_dependent_kg_analysis_artifacts"): (
        s(ANY, NOT_IDS, "BOARD_DEPENDENT_ARTIFACT_KINDS"),),
    ("postgres/unified_kg_store.py", "UnifiedKgStore.comention_peers"): (
        s(ANY, BOUNDED, "peer canonical ids, at most `limit`"),),
    ("postgres/unified_kg_store.py", "UnifiedKgStore.relation_provenance_counts"): (
        s(ANY, NOT_IDS, "the fixed provenance bucket names"),),
    # ---------------------------------------------------------------- SQLite
    ("sqlite/ask_state_store.py", "AskStateStore.recent_user_ask_traces"): (
        s(SQ_EXP, BOUNDED, "job ids of the page just read under LIMIT"),),
    ("sqlite/ask_state_store.py", "AskStateStore.recent_completed_ask_runs"): (
        s(SQ_EXP, BOUNDED, "run ids of the page just read under LIMIT"),),
    ("sqlite/ask_state_store.py", "AskStateStore.recent_user_report_traces"): (
        s(SQ_EXP, BOUNDED, "report ids of the page just read under LIMIT"),),
    ("sqlite/catalog_store.py", "CatalogStore.candidates_by_ids"): (
        s(SQ_EXP, BOUNDED, "capped at CATALOG_MAX_CANDIDATE_PAGE"),),
    ("sqlite/catalog_store.py", "CatalogStore.mark_candidates_applied"): (
        s(SQ_EXP, BOUNDED, "capped at CATALOG_MAX_CANDIDATE_PAGE"),),
    ("sqlite/catalog_store.py", "CatalogStore.mark_candidates_dismissed"): (
        s(SQ_EXP, BOUNDED, "capped at CATALOG_MAX_CANDIDATE_PAGE"),),
    ("sqlite/chunk_store.py", "ChunkStore.ids_for_sources"): (
        s(JSON, DRIVEN, "presence probe: the ordinal of each listed id is the output order"),),
    ("sqlite/chunk_store.py", "ChunkStore.chunks_for_element_ids"): (
        s(SQ_EXP, BATCHED, "element ids, CHUNK_ELEMENT_LOOKUP_BATCH (500) per statement"),),
    ("sqlite/chunk_store.py", "ChunkStore.hydrate_rows"): (
        s(SQ_EXP, BATCHED, "candidate chunk keys; callers pass one _in_batches window"),),
    ("sqlite/chunk_store.py", "ChunkStore.graph_hydrate_rows"): (
        s(SQ_EXP, BOUNDED, "the PPR top-chunk window"),),
    ("sqlite/chunk_store.py", "ChunkStore.retrieval_contribution_rows"): (
        s(SQ_EXP, BATCHED, "candidate chunk keys, one _in_batches window (<= 900)"),),
    ("sqlite/chunk_store.py", "ChunkStore.rows_by_ids"): (
        s(SQ_EXP, BOUNDED, "evidence chunks of the ranked KG object window"),),
    ("sqlite/embedding_store.py", "EmbeddingStore.vector_rows_for_ids"): (
        s(SQ_EXP, BATCHED, "vector keys; every caller batches (in_batches / _IN_CHUNK)"),),
    ("sqlite/embedding_store.py", "EmbeddingStore.relation_delta_rows"): (
        s(SQ_EXP, BATCHED, "delta source ids, callers batch with _in_batches (<= 900)"),),
    ("sqlite/embedding_store.py", "EmbeddingStore.knowledge_delta_rows"): (
        s(SQ_EXP, BATCHED, "delta source ids, callers batch with _in_batches (<= 900)"),),
    ("sqlite/embedding_store.py", "EmbeddingStore.element_delta_rows"): (
        s(SQ_EXP, BATCHED, "delta source ids, same batching contract as its siblings"),),
    ("sqlite/embedding_store.py", "EmbeddingStore.chunk_delta_rows"): (
        s(SQ_EXP, BATCHED, "delta source ids, callers batch with _in_batches (<= 900)"),),
    ("sqlite/governance_store.py", "GovernanceStore.sweep_orphan_clusters_page"): (
        s(SQ_EXP, BATCHED, "cluster ids of one page, _SWEEP_DELETE_ID_CHUNK per statement"),),
    ("sqlite/governance_store.py", "GovernanceStore.merge_candidate_pairs_for_canonicals"): (
        s(SQ_EXP, BATCHED, "canonical ids, the caller batches with _in_fuse_batches"),),
    ("sqlite/governance_store.py", "GovernanceStore.review_queue_rows"): (
        s(SQ_EXP, BATCHED, "endpoint ids, one batch per statement"),),
    ("sqlite/governance_store.py", "GovernanceStore._existing_cluster_members"): (
        s(SQ_EXP, BATCHED, "member ids, in_chunk_size per statement"),),
    ("sqlite/governance_store.py", "GovernanceStore.promotion_object_rows"): (
        s(SQ_EXP, BATCHED, "object ids, the caller batches with _IN_CHUNK"),),
    ("sqlite/governance_store.py", "GovernanceStore.notebook_name_rows"): (
        s(SQ_EXP, BATCHED, "notebook ids, the caller batches with _IN_CHUNK"),),
    ("sqlite/governance_store.py", "GovernanceStore.conflict_relation_evidence_rows"): (
        s(SQ_EXP, BATCHED, "relation ids, the caller batches with _batches"),),
    ("sqlite/identity_store.py", "IdentityStore.audit_labels_for_user_ids"): (
        s(SQ_EXP, BOUNDED, "capped user ids, one chunk per statement"),),
    ("sqlite/index_projection_store.py", "IndexProjectionStore.chunk_sources_for_ids"): (
        s(SQ_EXP, BATCHED, "chunk ids, in_batches per statement"),),
    ("sqlite/index_projection_store.py", "IndexProjectionStore.visible_source_ids"): (
        s(SQ_EXP, BATCHED, "source ids, in_batches per statement"),),
    ("sqlite/index_projection_store.py", "IndexProjectionStore.delta_chunk_count"): (
        s(SQ_EXP, BATCHED, "delta source ids, in_batches per statement"),),
    ("sqlite/index_projection_store.py", "IndexProjectionStore.relation_ids_for_source_batch"): (
        s(SQ_EXP, BATCHED, "source ids, the caller batches with in_batches"),),
    ("sqlite/index_projection_store.py", "IndexProjectionStore.graph_rows"): (
        s(SQ_EXP, BATCHED, "source ids, in_batches per statement"),),
    ("sqlite/index_projection_store.py", "IndexProjectionStore.embedding_matrix._rows"): (
        s(SQ_EXP, BATCHED, "vector ids, in_batches per statement"),),
    ("sqlite/kg_build_job_store.py", "KgBuildJobStore._delete_source_kg"): (
        s(SQ_EXP, BOUNDED, "object ids of one delete page (INDEXING_PIPELINE_PUBLISH_DELETE_BATCH)"),),
    ("sqlite/kg_build_job_store.py", "KgBuildJobStore._validate_stage_payloads"): (
        s(JSON, DRIVEN, "one staged source's element primary keys"),),
    ("sqlite/knowhow_store.py", "KnowhowStore.enumerate_knowhow_rows"): (
        s(SQ_EXP, BOUNDED, "<= KNOWHOW_ENUMERATION_MAX_TABLE_IDS (8) tables"),
        s(SQ_EXP, BOUNDED, "the visible subset of those <= 8 tables"),
        s(SQ_EXP, BOUNDED, "<= KNOWHOW_ENUMERATION_MAX_COLUMN_IDS (8) columns"),
        s(SQ_EXP, BOUNDED, "row ids of one page (<= KNOWHOW_ENUMERATION_PAGE_SIZE_MAX)"),
        s(SQ_EXP, BOUNDED, "<= KNOWHOW_ENUMERATION_MAX_COLUMN_IDS (8) columns"),
    ),
    ("sqlite/knowhow_store.py", "KnowhowStore.knowhow_anchor_existing_values"): (
        s(SQ_EXP, BOUNDED, "capped at CATALOG_MAX_CANDIDATE_PAGE anchor values"),),
    ("sqlite/knowhow_store.py", "KnowhowStore.append_knowhow_rows_skipping_existing_anchors"): (
        s(SQ_EXP, BOUNDED, "anchor values of one appended candidate page"),),
    ("sqlite/knowhow_transfer_store.py", "_insert_rows"): (
        s(SQ_EXP, NOT_IDS, "INSERT column values"),),
    ("sqlite/knowledge_store.py", "KnowledgeStore.drain_notebook_graph_rows_page"): (
        s(SQ_EXP, BOUNDED, "fact ids of one drain page"),
        s(SQ_EXP, BATCHED, "ids of one drain page, one batch per statement"),
        s(SQ_EXP, BATCHED, "ids of one drain page, one batch per statement"),
    ),
    ("sqlite/knowledge_store.py", "KnowledgeStore.relink_relation_rows_for_objects"): (
        s(SQ_EXP, BATCHED, "object ids, the caller batches with _in_relink_batches"),),
    ("sqlite/knowledge_store.py", "KnowledgeStore.embedding_rows_for_objects"): (
        s(SQ_EXP, BOUNDED, "ANN anchors / one source's new objects"),),
    ("sqlite/knowledge_store.py", "KnowledgeStore.valid_object_ids"): (
        s(SQ_EXP, BOUNDED, "the hits of one bounded k-NN probe"),),
    ("sqlite/knowledge_store.py", "KnowledgeStore.object_meta_rows_for_notebook"): (
        s(SQ_EXP, BOUNDED, "representatives of one neighbourhood / a single id"),),
    ("sqlite/knowledge_store.py", "KnowledgeStore.relation_context_rows"): (
        s(SQ_EXP, BATCHED, "relation ids, batch_size per statement"),),
    ("sqlite/knowledge_store.py", "KnowledgeStore.neighbor_ids"): (
        s(SQ_EXP, NOT_IDS, "the caller's usable status set"),),
    ("sqlite/knowledge_store.py", "KnowledgeStore.usable_object_rows_on"): (
        s(SQ_EXP, NOT_IDS, "the caller's usable status set"),
        s(SQ_EXP, BATCHED, "object ids, batch_size (500) per statement"),
    ),
    ("sqlite/knowledge_store.py", "KnowledgeStore.graph_object_rows"): (
        s(SQ_EXP, NOT_IDS, "the caller's usable status set"),),
    ("sqlite/knowledge_store.py", "KnowledgeStore.object_evidence_rows"): (
        s(SQ_EXP, BATCHED, "object ids; callers batch or pass the ranked window"),),
    ("sqlite/knowledge_store.py", "KnowledgeStore.follow_start_row"): (
        s(SQ_EXP, NOT_IDS, "the caller's usable status set"),
        s(SQ_EXP, BOUNDED, "one run's participant notebooks (<= 8)"),
    ),
    ("sqlite/knowledge_store.py", "KnowledgeStore.follow_relation_evidence_rows"): (
        s(SQ_EXP, BATCHED, "relation ids, the caller batches"),),
    ("sqlite/knowledge_store.py", "KnowledgeStore.follow_object_rows"): (
        s(SQ_EXP, BATCHED, "object ids, the caller batches"),
        s(SQ_EXP, NOT_IDS, "the caller's usable status set"),
    ),
    ("sqlite/knowledge_store.py", "KnowledgeStore.in_network_relation_rows"): (
        s(SQ_EXP, BOUNDED, "the object ids of one reasoning graph window"),),
    ("sqlite/knowledge_store.py", "KnowledgeStore.retrieval_objects"): (
        s(SQ_EXP, NOT_IDS, "the caller's status set"),
        s(SQ_EXP, BATCHED, "id filter, batch_size (900) per statement"),
    ),
    ("sqlite/knowledge_store.py", "KnowledgeStore.duplicate_member_rows"): (
        s(SQ_EXP, BATCHED, "object ids, batch_size per statement"),),
    ("sqlite/knowledge_store.py", "KnowledgeStore._element_texts"): (
        s(SQ_EXP, BOUNDED, "step elements of <= 500 candidate procedures of one object"),),
    ("sqlite/knowledge_store.py", "KnowledgeStore._enrich_evidence"): (
        s(SQ_EXP, BOUNDED, "the element ids of one object's evidence list"),),
    ("sqlite/knowledge_store.py", "KnowledgeStore.count_knowledge"): (
        s(SQ_EXP, NOT_IDS, "the caller's status set"),),
    ("sqlite/knowledge_store.py", "KnowledgeStore.validate_source_fact_publish"): (
        s(JSON, DRIVEN, "one source's element primary keys"),),
    ("sqlite/knowledge_store.py", "KnowledgeStore.validate_stage_source_elements"): (
        s(JSON, DRIVEN, "one source's element primary keys"),),
    ("sqlite/knowledge_store.py", "KnowledgeStore.completion_validate_scope"): (
        s(SQ_EXP, BOUNDED, "object ids of one relation-completion page"),
        s(SQ_EXP, BOUNDED, "evidence element ids of that page"),
    ),
    ("sqlite/knowledge_store.py", "KnowledgeStore.completion_existing_keys"): (
        s(SQ_EXP, BOUNDED, "endpoint ids of one relation-completion page"),),
    ("sqlite/knowledge_store.py", "KnowledgeStore.completion_page"): (
        s(SQ_EXP, NOT_IDS, "the edge-contract type sets"),
        s(SQ_EXP, NOT_IDS, "the edge-contract type sets"),
        s(SQ_EXP, NOT_IDS, "the edge-contract type sets"),
    ),
    ("sqlite/knowledge_store.py", "KnowledgeStore.completion_candidate_rows"): (
        s(SQ_EXP, BOUNDED, "candidate ids capped at the completion over-fetch"),),
    ("sqlite/knowledge_store.py", "KnowledgeStore.completion_element_rows"): (
        s(SQ_EXP, BATCHED, "evidence of one page, one chunk per statement"),),
    ("sqlite/knowledge_store.py", "KnowledgeStore.prune_cluster_rows_for_source"): (
        s(SQ_EXP, BATCHED, "member ids, one batch per statement"),),
    ("sqlite/knowledge_store.py", "KnowledgeStore.legacy_typed_table_ids"): (
        s(SQ_EXP, NOT_IDS, "the legacy object-type vocabulary"),),
    ("sqlite/knowledge_store.py", "KnowledgeStore.delete_object_sources"): (
        s(SQ_EXP, BATCHED, "object ids, one batch per statement"),),
    ("sqlite/knowledge_store.py", "KnowledgeStore._delete_object_id_batch"): (
        s(SQ_EXP, BATCHED, "object ids, capped at the delete batch size"),),
    ("sqlite/knowledge_store.py", "KnowledgeStore.object_meta_rows"): (
        s(SQ_EXP, BOUNDED, "the ranked search-hit window"),),
    ("sqlite/knowledge_store.py", "KnowledgeStore.concept_cluster_detail_rows"): (
        s(JSON, BOUNDED, "the checked node ids of one viewer response"),),
    ("sqlite/knowledge_store.py", "_element_rows"): (
        s(SQ_EXP, BATCHED,
          "element ids: one _ELEMENT_BATCH (900) window or one procedure's steps"),),
    ("sqlite/knowledge_store.py", "KnowledgeStore.edge_centrality_source_rows"): (
        s(JSON, BOUNDED, "the top max_nodes ids"),
        s(JSON, BOUNDED, "the top max_nodes ids"),
        s(JSON, BOUNDED, "the top max_nodes ids"),
    ),
    ("sqlite/knowledge_store.py", "KnowledgeStore.concept_neighbor_rows"): (
        s(SQ_EXP, BATCHED, "attached candidates, batch_size per statement"),
        s(SQ_EXP, BATCHED, "attached candidates, batch_size per statement"),
    ),
    ("sqlite/maintenance.py", "SQLiteMaintenanceAdapter.chunk_texts_by_ids"): (
        s(SQ_EXP, BATCHED, "one page of the caller's walk, one batch per statement"),),
    ("sqlite/maintenance.py", "SQLiteMaintenanceAdapter.element_texts_by_ids"): (
        s(SQ_EXP, BATCHED, "one page of the caller's walk, one batch per statement"),),
    ("sqlite/maintenance.py", "SQLiteMaintenanceAdapter.count_missing_element_vectors"): (
        s(SQ_EXP, BOUNDED, "sources in flight in this process (ingest leases)"),),
    ("sqlite/maintenance.py", "SQLiteMaintenanceAdapter.count_missing_chunk_vectors"): (
        s(SQ_EXP, BOUNDED, "sources in flight in this process (ingest leases)"),),
    ("sqlite/maintenance.py", "SQLiteMaintenanceAdapter.chunk_notebook_map"): (
        s(SQ_EXP, BATCHED, "chunk ids, 400 per statement"),),
    ("sqlite/maintenance.py", "SQLiteMaintenanceAdapter.image_backfill_discard_assets"): (
        s(SQ_EXP, BOUNDED, "assets written by one source's image backfill"),),
    ("sqlite/maintenance.py", "SQLiteMaintenanceAdapter.backfill_source_fact_batch"): (
        s(JSON, DRIVEN, "element primary keys of one fact batch of one source"),
        s(JSON, DRIVEN, "local object keys of that fact batch"),
    ),
    ("sqlite/maintenance.py", "ReadOnlySQLiteInspector.vectors_for_ids"): (
        s(SQ_EXP, BATCHED, "vector ids, one batch per statement"),),
    ("sqlite/maintenance.py", "ReadOnlySQLiteInspector.vector_blocks._row_batches"): (
        s(SQ_EXP, BATCHED, "vector ids, one batch per statement"),),
    ("sqlite/memory_store.py", "MemoryStore.answer_memory_links"): (
        s(SQ_EXP, BOUNDED, "capped at 200 answer ids"),),
    ("sqlite/memory_store.py", "MemoryStore._mutate_with_revision"): (
        s(SQ_EXP, NOT_IDS, "the expected lifecycle statuses"),),
    ("sqlite/memory_store.py", "MemoryStore.transition"): (
        s(SQ_EXP, NOT_IDS, "the expected lifecycle statuses"),),
    ("sqlite/memory_store.py", "MemoryStore.bulk_delete_memories"): (
        s(SQ_EXP, BOUNDED, "capped at 200 memory ids"),),
    ("sqlite/memory_store.py", "MemoryStore.memory_retrieval_rows"): (
        s(SQ_EXP, NOT_IDS, "the allowed memory statuses"),),
    ("sqlite/notebook_delete_job_store.py", "NotebookDeleteJobStore.recreate_for_deleting_notebook"): (
        s(SQ_EXP, BOUNDED, "one notebook's failed delete jobs"),),
    ("sqlite/notebook_delete_job_store.py", "NotebookDeleteJobStore.delete_fts_shadow_page"): (
        s(SQ_EXP, BOUNDED, "rowids of the page just read under LIMIT"),),
    ("sqlite/notebook_delete_job_store.py", "NotebookDeleteJobStore.delete_direct_page_form_one"): (
        s(SQ_EXP, BOUNDED, "ids of the page just read under LIMIT"),),
    ("sqlite/notebook_delete_job_store.py", "NotebookDeleteJobStore.delete_knowhow_rows_page"): (
        s(SQ_EXP, BOUNDED, "row ids of one delete page"),),
    ("sqlite/notebook_delete_job_store.py", "NotebookDeleteJobStore.delete_knowhow_tables_page"): (
        s(SQ_EXP, BOUNDED, "table ids of one delete page"),),
    ("sqlite/notebook_delete_job_store.py", "NotebookDeleteJobStore.delete_indexing_pipeline_stages_page"): (
        s(SQ_EXP, BOUNDED, "job ids of one delete page"),),
    ("sqlite/notebook_delete_job_store.py", "NotebookDeleteJobStore.delete_memory_items_page"): (
        s(SQ_EXP, BOUNDED, "memory ids of the page just read under LIMIT"),),
    ("sqlite/notebook_delete_job_store.py", "NotebookDeleteJobStore._drain_children_by_parent_ids"): (
        s(SQ_EXP, BOUNDED, "parent ids of one delete page"),),
    ("sqlite/notebook_store.py", "NotebookStore.tier_map"): (
        s(SQ_EXP, BATCHED, "notebook ids, one batch per statement"),),
    ("sqlite/notebook_store.py", "NotebookStore._retain_user_activity_before_delete"): (
        s(SQ_EXP, NOT_IDS, "INSERT column values"),),
    ("sqlite/query_store.py", "QueryStore.knowledge_type_count_rows_for_sources"): (
        s(SQ_EXP, NOT_IDS, "the caller's status set"),
        s(SQ_EXP, BATCHED, "Memory source ids, one batch per statement"),
    ),
    ("sqlite/query_store.py", "QueryStore.knowledge_type_count_rows_excluding_memory"): (
        s(SQ_EXP, NOT_IDS, "the caller's status set"),),
    ("sqlite/query_store.py", "QueryStore.top_concept_names"): (
        s(SQ_EXP, NOT_IDS, "the caller's status set"),),
    ("sqlite/query_store.py", "QueryStore.knowhow_knowledge_type_rows"): (
        s(SQ_EXP, NOT_IDS, "the caller's status set"),),
    ("sqlite/query_store.py", "QueryStore.notebook_source_ids_among"): (
        s(SQ_EXP, BATCHED, "in-flight source ids, one batch per statement"),),
    ("sqlite/query_store.py", "QueryStore.list_user_notebooks"): (
        s(SQ_EXP, BOUNDED, "one user's own notebooks"),),
    ("sqlite/query_store.py", "QueryStore.list_user_activity"): (
        s(SQ_EXP, BOUNDED, "one user's own notebooks"),
        s(SQ_EXP, BOUNDED, "source ids of one activity page"),
    ),
    ("sqlite/query_store.py", "QueryStore.pending_actions_projection_rows"): (
        s(SQ_EXP, BOUNDED, "one user's own notebooks"),),
    ("sqlite/report_store.py", "ReportStore.export_reports"): (
        s(SQ_EXP, BATCHED, "report ids, _in_batches per statement"),),
    ("sqlite/retrieval_experience_store.py", "RetrievalExperienceStore.note_adopted"): (
        s(SQ_EXP, BOUNDED, "experiences adopted by one run"),),
    ("sqlite/sharing_store.py", "SharingStore.readable_notebook_ids"): (
        s(SQ_EXP, BOUNDED, "a global run's notebooks (<= global_ask_max_notebooks, 8)"),),
    ("sqlite/sharing_store.py", "SharingStore.valid_copied_mount_base_ids"): (
        s(SQ_EXP, BATCHED, "base notebook ids, one batch per statement"),),
    ("sqlite/sharing_store.py", "SharingStore.insert_row_values"): (
        s(SQ_EXP, NOT_IDS, "INSERT column values"),),
    ("sqlite/source_store.py", "SourceStore.visible_source_ids_by_notebook"): (
        s(SQ_EXP, BOUNDED, "a global run's participant notebooks (<= 8)"),),
    ("sqlite/source_store.py", "SourceStore.visible_source_scope_snapshot"): (
        s(JSON, DRIVEN, "the requested source ids with their ordinals, primary-key probes"),),
    ("sqlite/source_store.py", "SourceStore.visible_source_owners"): (
        s(JSON, DRIVEN, "one answer's cited source ids, primary-key probes"),),
    ("sqlite/source_store.py", "SourceStore.element_type_count_rows"): (
        s(SQ_EXP, NOT_IDS, "the requested element types"),
        s(SQ_EXP, BATCHED, "source ids, one batch per statement"),
        s(SQ_EXP, BATCHED, "source ids, one batch per statement"),
    ),
    ("sqlite/source_store.py", "SourceStore.source_display_rows"): (
        s(SQ_EXP, BATCHED, "source ids, one batch per statement"),),
    ("sqlite/source_store.py", "SourceStore.evidence_elements"): (
        s(SQ_EXP, BATCHED, "element ids, one batch per statement"),),
    ("sqlite/source_store.py", "SourceStore.evidence_fingerprints"): (
        s(SQ_EXP, BATCHED, "element ids, one batch per statement"),),
    ("sqlite/source_store.py", "SourceStore.passage_evidence_snapshot"): (
        s(SQ_EXP, BATCHED, "chunk ids, one batch per statement"),),
    ("sqlite/source_store.py", "SourceStore.image_asset_rows"): (
        s(SQ_EXP, BATCHED, "element ids, one batch per statement"),),
    ("sqlite/source_store.py", "SourceStore.source_listing_rows"): (
        s(SQ_EXP, BATCHED, "source ids, one batch per statement"),),
    ("sqlite/source_store.py", "SourceStore.report_source_identity_rows"): (
        s(SQ_EXP, BOUNDED, "truncated to 1024 source ids"),),
    ("sqlite/source_store.py", "SourceStore.source_titles"): (
        s(SQ_EXP, BOUNDED, "a selected-source graph's sources (<= 32)"),),
    ("sqlite/source_store.py", "SourceStore.sources_from_rows"): (
        s(SQ_EXP, BATCHED, "source ids, one batch per statement"),),
    ("sqlite/source_store.py", "SourceStore.paper_meta_for_sources"): (
        s(SQ_EXP, BATCHED, "source ids, one batch per statement"),),
    ("sqlite/unified_kg_store.py", "UnifiedKgStore.cluster_evidence_rows"): (
        s(SQ_EXP, BATCHED, "seed ids of one cluster batch"),),
    ("sqlite/unified_kg_store.py", "UnifiedKgStore.reap_derived_generations_page"): (
        s(SQ_EXP, NOT_IDS, "the keyset cursor's key columns"),),
    ("sqlite/unified_kg_store.py", "UnifiedKgStore.cluster_fold_rows"): (
        s(SQ_EXP, BATCHED, "hit ids; callers pass a ranked window or batch by 900"),),
    ("sqlite/unified_kg_store.py", "UnifiedKgStore.weak_support_relation_rows"): (
        s(SQ_EXP, BOUNDED, "the canonical seeds of one gap probe"),),
    ("sqlite/unified_kg_store.py", "UnifiedKgStore.relation_endpoint_name_rows"): (
        s(SQ_EXP, BOUNDED, "sample relations of one gap probe (<= probe limit)"),),
}


# ------------------------------------------------------------------ scanner
def _docstrings(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                ids.add(id(body[0].value))
    return ids


def _is_mark(node: ast.AST, mark: str) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        rest = node.value.replace(",", "").replace(" ", "")
        return bool(rest) and mark in rest and not rest.replace(mark, "")
    if isinstance(node, ast.List) and len(node.elts) == 1:
        return _is_mark(node.elts[0], mark)
    return False


def _is_constant(node: ast.AST) -> bool:
    """An expansion over a module-level CONSTANT (a fixed status set)."""
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "len" and node.args):
        return _is_constant(node.args[0])
    if isinstance(node, ast.Name):
        return node.id.isupper()
    if isinstance(node, ast.Attribute):
        return node.attr.isupper()
    return False


class _Scanner(ast.NodeVisitor):
    def __init__(self, backend: str, docstrings: set[int]) -> None:
        self.backend = backend
        self.mark = _MARK[backend]
        self.docstrings = docstrings
        self.stack: list[str] = []
        self.sites: list[tuple[str, str, int]] = []

    def _scope(self) -> str:
        return ".".join(self.stack) or "<module>"

    def _enter(self, node) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _enter

    def _add(self, kind: str, node: ast.AST) -> None:
        self.sites.append((self._scope(), kind, node.lineno))

    def visit_Constant(self, node: ast.Constant) -> None:
        if not isinstance(node.value, str) or id(node) in self.docstrings:
            return
        if self.backend == "postgres":
            for match in _PG_ARRAY.finditer(node.value):
                word = match.group(1) or match.group(2)
                kind = "unnest(%s)" if word.lower() == "unnest" else f"{word.upper()}(%s)"
                self._add(kind, node)
            for match in _PG_UNPACK.finditer(node.value):
                self._add(f"{match.group(1).lower()}(%s)", node)
        else:
            for _match in _SQLITE_JSON.finditer(node.value):
                self._add(JSON, node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "join" and node.args:
            arg = node.args[0]
            if (isinstance(arg, (ast.GeneratorExp, ast.ListComp))
                    and _is_mark(arg.elt, self.mark)
                    and not _is_constant(arg.generators[0].iter)):
                self._add(f"{self.mark}-expansion", node)
        if (self.backend == "postgres" and node.args
                and ((isinstance(func, ast.Name) and func.id == "placeholders")
                     or (isinstance(func, ast.Attribute) and func.attr == "placeholders"))
                and not _is_constant(node.args[0])):
            self._add(PG_EXP, node)
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.Mult):
            for mark_side, other in ((node.left, node.right), (node.right, node.left)):
                if _is_mark(mark_side, self.mark) and not _is_constant(other):
                    self._add(f"{self.mark}-expansion", node)
        self.generic_visit(node)


def _scan(source: str, backend: str) -> list[tuple[str, str, int]]:
    tree = ast.parse(source)
    scanner = _Scanner(backend, _docstrings(tree))
    scanner.visit(tree)
    return scanner.sites


def _repository_sites() -> dict[tuple[str, str], list[tuple[str, int]]]:
    found: dict[tuple[str, str], list[tuple[str, int]]] = {}
    for backend in ("postgres", "sqlite"):
        for path in sorted((REPOSITORIES / backend).rglob("*.py")):
            relative = f"{backend}/{path.relative_to(REPOSITORIES / backend).as_posix()}"
            if relative in BINDING_MODULES:
                continue
            for scope, kind, line in _scan(path.read_text(encoding="utf-8"), backend):
                found.setdefault((relative, scope), []).append((kind, line))
    return found


_HOW_TO_FIX = (
    "Bind an id list sized by the data through the backend's id_binding module: "
    "PostgreSQL `bound = bind_ids(ids)` + `member_of(col, bound)` / "
    "`not_member_of(col, bound)`, executed by "
    "`execute_ids` / `execute_bound` (app/repositories/postgres/id_binding.py); "
    "SQLite `bound = bind_ids(ids)` + `member_of(col, bound)` / "
    "`not_member_of(col, bound)` / `drive_by(col, bound)` "
    "(app/repositories/sqlite/id_binding.py). If the list is bounded by "
    "construction, batched key probes, or the list drives a one-parameter "
    "key probe, add a reviewed entry to EXEMPT in "
    "backend/tests/test_id_list_binding_guard.py with its class and reason."
)


# ------------------------------------------------------------------- tests
def test_every_id_list_binding_site_is_converted_or_reviewed():
    found = _repository_sites()
    problems: list[str] = []
    for key, sites in sorted(found.items()):
        expected = EXEMPT.get(key)
        kinds = [kind for kind, _line in sites]
        where = ", ".join(f"{kind} at line {line}" for kind, line in sites)
        if expected is None:
            problems.append(f"{key[0]} :: {key[1]} binds an id list directly ({where}).")
        elif Counter(kind for kind, _klass, _why in expected) != Counter(kinds):
            problems.append(
                f"{key[0]} :: {key[1]} changed: found [{where}], reviewed "
                f"{sorted(kind for kind, _k, _w in expected)}."
            )
    for key in sorted(set(EXEMPT) - set(found)):
        problems.append(
            f"{key[0]} :: {key[1]} is listed in EXEMPT but binds no id list any "
            "more; remove the entry."
        )
    assert not problems, "\n".join(problems) + "\n\n" + _HOW_TO_FIX


def test_every_exemption_names_a_class_and_a_reason():
    for key, entries in EXEMPT.items():
        assert entries, key
        for kind, klass, why in entries:
            assert kind in PG_KINDS | {SQ_EXP, JSON}, key
            assert klass in CLASSES, key
            assert why.strip() and len(why) <= 100, key
            backend = key[0].split("/", 1)[0]
            assert (kind in {SQ_EXP, JSON}) == (backend == "sqlite"), key


def _functions_calling(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            calls = {
                (child.func.id if isinstance(child.func, ast.Name)
                 else getattr(child.func, "attr", None))
                for child in ast.walk(node) if isinstance(child, ast.Call)
            }
            if name in calls:
                yield node.name, calls


def test_postgres_statements_that_bind_through_the_module_run_unprepared():
    """A ``bind_ids`` fragment under a prepared generic plan is as opaque as
    the array it replaced: any PostgreSQL function that binds one AND executes
    a statement must run it through ``execute_ids`` / ``execute_bound``.  A
    helper that only builds and returns the binding executes nothing and is
    not a finding."""
    offenders = []
    for path in sorted((REPOSITORIES / "postgres").rglob("*.py")):
        if path.name == "id_binding.py":
            continue
        for function, calls in _functions_calling(path, "bind_ids"):
            executes = "execute" in calls
            if executes and not calls & {"execute_ids", "execute_bound"}:
                offenders.append(f"{path.name}::{function}")
    assert offenders == [], offenders


# ---------------------------------------------------- the scanner itself
@pytest.mark.parametrize(
    ("backend", "source", "kinds"),
    [
        ("postgres", 'def f(db, ids):\n    db.execute("SELECT 1 WHERE x = ANY(%s)", (ids,))\n',
         ["ANY(%s)"]),
        ("postgres", 'def f(db, ids):\n    db.execute("SELECT 1 WHERE x <> all( %s )", (ids,))\n',
         ["ALL(%s)"]),
        ("postgres", 'def f(db, ids):\n    db.execute("SELECT * FROM unnest(%s::text[])", (ids,))\n',
         ["unnest(%s)"]),
        ("postgres", 'def f(db, p):\n    db.execute("SELECT value FROM jsonb_array_elements_text(%s::jsonb)", (p,))\n',
         [JSONB]),
        ("postgres", "def f(db, p):\n    db.execute(\"SELECT 1 WHERE x=ANY(string_to_array(%s,E'\\\\x1f'))\", (p,))\n",
         ["string_to_array(%s)"]),
        ("postgres", 'def f(db, ids):\n    ph = ",".join("%s" for _ in ids)\n', [PG_EXP]),
        ("postgres", 'def f(db, ids):\n    ph = ",".join(["%s"] * len(ids))\n', [PG_EXP]),
        ("postgres", 'def f(db, ids):\n    ph = placeholders(ids)\n', [PG_EXP]),
        ("postgres", 'def f(db):\n    ph = ",".join("%s" for _ in USABLE_STATUSES)\n', []),
        ("postgres", 'def f(db, b):\n    db.execute(f"SELECT 1 WHERE x=ANY({b.sql})", (b.param,))\n', []),
        ("postgres", 'def f():\n    """x = ANY(%s) in prose"""\n', []),
        ("sqlite", 'def f(db, ids):\n    ph = ",".join("?" for _ in ids)\n', [SQ_EXP]),
        ("sqlite", 'def f(db, ids):\n    ph = ",".join("?" * len(ids))\n', [SQ_EXP]),
        ("sqlite", 'def f(db, p):\n    db.execute("SELECT 1 WHERE x IN (SELECT value FROM json_each(?))", (p,))\n',
         [JSON]),
        ("sqlite", 'def f(db):\n    ph = ",".join("?" * len(BOARD_KINDS))\n', []),
    ],
)
def test_the_scanner_recognises_each_form(backend, source, kinds):
    assert [kind for _scope, kind, _line in _scan(source, backend)] == kinds
