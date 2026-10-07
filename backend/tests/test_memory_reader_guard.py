# backend/tests/test_memory_reader_guard.py
"""D2 reader guard (plan 2026-09-29 §2 D2, ruling M1): every repository read of
``knowledge_objects`` / ``knowledge_relations`` / ``concept_clusters`` by
notebook is registered, by name, with the reason it cannot show one member's
Memory to another.

Knowledge-graph rows derived from a member's Memory belong to that member only.
That holds because every reader that can hand such a row to someone falls into
one of the categories below; a NEW reader that nobody classified is exactly the
place a Memory-derived object would leak. So a new reader fails this guard until
it is registered here, and the failure names its ``file:line``.

What is scanned (by AST, so a comment or a docstring never counts): the Python
modules under ``backend/app/repositories`` (the shared modules and both backends,
``postgres/search.py`` included). A *site* is a function or method (its whole
body, nested helpers included) or a module- or class-level constant (an SQL
constant has no enclosing function: ``knowledge_counts_cache._TYPE_STATUS_HALVES_SQL``
is registered under its own name). A site is a *reader by notebook* when its
string literals (plain, f-string parts, implicit concatenation) contain
``FROM`` / ``JOIN`` one of the three tables (a ``DELETE FROM`` is a write, not a
read) and mention ``notebook_id``. A ``FROM`` / ``JOIN`` whose table is
interpolated (``f"FROM {table_sql}"`` or ``sql.SQL("FROM {}")``) counts as a
read of every table that a module-level catalog the site uses names as a bare
string (PostgreSQL search's ``_SEARCH_TARGETS``), so an interpolated reader is
inventoried too.

Deliberately outside the inventory: a read by primary key (no ``notebook_id``;
e.g. ``object_evidence_rows``, judged by the viewer in ``KgViewerScope``'s Python
twin on the rows it returns) and a method with no SQL of its own that delegates
to a registered site (``type_counts`` / ``count_active_objects`` call the
count-cache statements, registered where they are written).

Registration is by name, never by line number: ``_REGISTRY`` maps a module path
relative to ``backend/app/repositories`` -- ``*/name.py`` means the module of that
name in either backend -- to ``{qualified name: category}``. A registered name
that no longer reads (renamed or removed) fails too, so the list stays exact.

Four categories are also checked mechanically, so a registration cannot claim
more than the code does:

* ``VIEWER_READER`` -- the site takes ``viewer_id`` (``memory_sql.memory_viewer_filter``:
  ``None`` every row, ``""`` no Memory-derived row, a user id no other member's).
* ``OWN_MEMORY`` -- the site reads the viewer's own Memory side
  (``own_memory_source`` / ``memory_source_readable``).
* ``SHARED_TOOLING`` and ``BUILD_READER`` -- the statement leaves every
  Memory-derived row out for everyone (one of ``_EXCLUSION_NAMES``).
* ``COUNT_CACHE`` -- the two-halves count statement splits the Memory side by
  ``memory_source_type_predicate``.

The behaviour itself (two members, one with a confirmed Memory, endpoint by
endpoint) is pinned by ``test_kg_viewer_scope_routes.py``,
``test_memory_kg_store_readers.py`` and their PostgreSQL twins; this guard only
keeps the reader inventory closed.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REPOSITORIES = ROOT / "backend" / "app" / "repositories"

_TABLES = ("knowledge_objects", "knowledge_relations", "concept_clusters")
_READ = re.compile(
    r"(?<!DELETE\s)\b(?:FROM|JOIN)\s+(" + "|".join(_TABLES) + r")\b", re.IGNORECASE,
)
_BY_NOTEBOOK = re.compile(r"\bnotebook_id\b")
# A FROM / JOIN whose table is interpolated: an f-string part ending at the
# placeholder (``f"FROM {table_sql}"``) or a ``sql.SQL("FROM {}")`` template.
_INTERPOLATED_READ = re.compile(
    r"(?<!DELETE\s)\b(?:FROM|JOIN)[ \t]*(?:\{\}|$)", re.IGNORECASE | re.MULTILINE,
)

# -- categories ---------------------------------------------------------------
VIEWER_READER = "viewer_reader"
OWN_MEMORY = "own_memory"
SHARED_TOOLING = "shared_tooling"
BUILD_READER = "build_reader"
COUNT_CACHE = "count_cache"
KEY_BOUNDED = "key_bounded"
CEILING_BOUNDED = "ceiling_bounded"
CALLER_FILTERED = "caller_filtered"
DERIVED_LAYER = "derived_layer"
STATE_PROBE = "state_probe"
MEMORY_SIDE = "memory_side"
COPY = "copy"
MAINTENANCE = "maintenance"

CATEGORIES = {
    VIEWER_READER: (
        "takes viewer_id (memory_sql.memory_viewer_filter); the service passes it "
        "through kg_viewer_scope.store_viewer_kwargs"),
    OWN_MEMORY: "reads the viewer's own Memory side (own_memory_source)",
    SHARED_TOOLING: (
        "one result every member reads (community summaries, duplicate groups, edge "
        "centrality, the edge review queue, conflict detection, top concepts): every "
        "Memory-derived row is left out for everyone, in the statement"),
    BUILD_READER: (
        "graph-build, fusion and artifact-build input: every Memory-derived row is "
        "left out in the statement (E4-2 seed/derived-layer readers, E4-3 Tier-2 "
        "pool, E4-6 artifact builds)"),
    COUNT_CACHE: (
        "knowledge_counts_cache: the shared half plus every member's Memory half "
        "from one statement; no viewer's view is held in the memo"),
    KEY_BOUNDED: (
        "bounded by keys the caller already holds (object, relation, cluster or "
        "candidate ids, one source); the viewer rule is applied to those keys by "
        "the caller (kg_viewer_scope's Python twin) or the keys are the caller's own"),
    CEILING_BOUNDED: (
        "a retrieval-run read whose rows pass the run's source ceiling and "
        "participant set (plan D1) before they are hydrated or cited, or a shape its "
        "caller takes only when neither a ceiling nor a viewer filter applies (the "
        "PostgreSQL KNN lexical arm)"),
    CALLER_FILTERED: (
        "build input whose Memory rows the calling build drops by a Memory id set "
        "read up front (E4-3 Tier-2 bridge pool, E4-6 scale/viz builds)"),
    DERIVED_LAYER: (
        "reads the cluster/community/scratch rows the isolated rebuild writes, which "
        "hold no Memory member (E4-2 seeds, E4-3 fusion, E4-5 migration)"),
    STATE_PROBE: (
        "an existence, size or version probe: returns no row content (gating, cache "
        "keys, thresholds)"),
    MEMORY_SIDE: (
        "selects the Memory-derived rows themselves in order to exclude, subtract or "
        "remove them; never returned to a member"),
    COPY: (
        "deep-copy statement sets; Memory is left out by the copy path (ruling M2), "
        "pinned by test_memory_chunk_write_guard.py and test_notebook_share_copy.py"),
    MAINTENANCE: (
        "writes, deletes, purges, backfills, relink, schema and isolation migration, "
        "rebuild bookkeeping and offline operator tooling: no member viewer"),
}

# A statement that leaves every Memory-derived row out names one of these
# (``memory_sql`` fragments and the store-local helpers built on them).
_EXCLUSION_NAMES = frozenset({
    "memory_derived_in_notebook", "memory_derived_object", "memory_derived_relation",
    "no_memory_member_cluster", "foreign_memory_object_excluded",
    "foreign_memory_relation_excluded", "_not_memory", "_not_memory_object_ref",
    "_NOT_MEMORY_OWNED_SQL", "_NO_MEMORY_MEMBER_CLUSTER_SQL", "_EXCLUDE_MEMORY_ROWS",
    "_NOT_A_MEMORY_OBJECT",
})
_OWN_MEMORY_NAMES = frozenset({"own_memory_source", "memory_source_readable"})
_COUNT_CACHE_NAMES = frozenset({"memory_source_type_predicate"})


# -- the registry ---------------------------------------------------------------
_REGISTRY: dict[str, dict[str, str]] = {
    "*/knowledge_store.py": {
        # viewer-taking readers (E4-4)
        "KnowledgeStore.list_knowledge_page": VIEWER_READER,
        "KnowledgeStore.graph_node_rows": VIEWER_READER,
        "KnowledgeStore.unified_graph_rows": VIEWER_READER,
        "KnowledgeStore.relations_for_notebook": VIEWER_READER,
        "KnowledgeStore.neighbor_relation_rows": VIEWER_READER,
        "KnowledgeStore.object_meta_rows_for_notebook": VIEWER_READER,
        "KnowledgeStore.object_meta_rows": VIEWER_READER,
        "KnowledgeStore.fts_search": VIEWER_READER,
        "_fts_viewer_filter": VIEWER_READER,
        # shared tooling (E4-4, E4-3)
        "KnowledgeStore.community_context_rows": SHARED_TOOLING,
        "KnowledgeStore.duplicate_seed_rows": SHARED_TOOLING,
        "KnowledgeStore.duplicate_member_rows": SHARED_TOOLING,
        "KnowledgeStore.edge_centrality_source_rows": SHARED_TOOLING,
        # build / fusion input
        "KnowledgeStore.incremental_object_rows": BUILD_READER,
        "KnowledgeStore.concept_embedding_rows": CALLER_FILTERED,
        "KnowledgeStore.notebook_object_evidence_rows_paged": CALLER_FILTERED,
        # key-bounded (KG detail, completion, by-id)
        "KnowledgeStore.get_object_row": KEY_BOUNDED,
        # PR-E7b (D-3): the public report page's ownership read -- by the cited
        # object ids, answering only each object's library (no content); which
        # citation may show is decided by report_foreign_memory_ids
        # (SourceStore.source_owners is its sources-table twin, outside the
        # three tables this guard scans)
        "KnowledgeStore.object_owners": KEY_BOUNDED,
        "KnowledgeStore.node_context": KEY_BOUNDED,
        "_NODE_CONTEXT_DEFINES_SQL": KEY_BOUNDED,
        "_node_context_cluster_sql": KEY_BOUNDED,
        "_LEGACY_SIBLINGS_BY_SECTION_SQL": KEY_BOUNDED,
        "_LEGACY_SIBLINGS_UNSECTIONED_SQL": KEY_BOUNDED,
        "_LEGACY_SIBLINGS_UNSECTIONED_PAGE_SQL": KEY_BOUNDED,
        "KnowledgeStore.concept_cluster_detail_rows": KEY_BOUNDED,
        "KnowledgeStore.concept_cluster_member_total": KEY_BOUNDED,
        "KnowledgeStore.concept_neighbor_rows": KEY_BOUNDED,
        "KnowledgeStore.completion_candidate_rows": KEY_BOUNDED,
        "KnowledgeStore.completion_existing_keys": KEY_BOUNDED,
        "KnowledgeStore.completion_validate_scope": KEY_BOUNDED,
        # retrieval legs (source ceiling)
        "KnowledgeStore.retrieval_objects": CEILING_BOUNDED,
        "KnowledgeStore.notebook_object_evidence_rows": CEILING_BOUNDED,
        "KnowledgeStore.graph_object_rows": CEILING_BOUNDED,
        "KnowledgeStore.graph_relation_rows": CEILING_BOUNDED,
        "KnowledgeStore.knowledge_object_page_rows": CEILING_BOUNDED,
        "KnowledgeStore.count_knowledge": CEILING_BOUNDED,
        "KnowledgeStore.follow_start_row": CEILING_BOUNDED,
        "KnowledgeStore.follow_endpoint_rows": CEILING_BOUNDED,
        "KnowledgeStore.follow_object_rows": CEILING_BOUNDED,
        # PR-E2 E2-1: a chain hop's relation evidence, by the hop's relation ids;
        # every item passes the run's evidence boundary before it is cited
        "KnowledgeStore.follow_relation_evidence_rows": CEILING_BOUNDED,
        "KnowledgeStore.in_network_relation_rows": CEILING_BOUNDED,
        "KnowledgeStore.neighbor_ids": CEILING_BOUNDED,
        "KnowledgeStore.relation_connected_object_ids": CEILING_BOUNDED,
        "KnowledgeStore.relation_context_rows": CEILING_BOUNDED,
        "KnowledgeStore.relation_id_rows_for_objects": CEILING_BOUNDED,
        # probes
        "KnowledgeStore.active_object_count": STATE_PROBE,
        "KnowledgeStore.has_kg": STATE_PROBE,
        "KnowledgeStore.any_mounted_has_kg_on": STATE_PROBE,
        "KnowledgeStore.notebook_schema_has_objects": STATE_PROBE,
        "KnowledgeStore.relation_exists": STATE_PROBE,
        "KnowledgeStore.graph_version_rows": STATE_PROBE,
        "KnowledgeStore.object_version_row": STATE_PROBE,
        # maintenance
        "KnowledgeStore._stale_object_ids_for_source_batch": MAINTENANCE,
        "KnowledgeStore.backfill_fts": MAINTENANCE,
        "KnowledgeStore.clear_sources_graph_state": MAINTENANCE,
        "KnowledgeStore.completion_page": MAINTENANCE,
        "KnowledgeStore.delete_notebook_graph_rows": MAINTENANCE,
        "KnowledgeStore.drain_notebook_graph_rows_page": MAINTENANCE,
        "_GRAPH_DRAIN_STEPS": MAINTENANCE,
        "KnowledgeStore.prune_cluster_rows_for_source": MAINTENANCE,
        "KnowledgeStore.relation_endpoint_rows": MAINTENANCE,
        "KnowledgeStore.relink_rows": MAINTENANCE,
        "KnowledgeStore.relink_orphan_source_ids": MAINTENANCE,
        "KnowledgeStore.relink_object_rows_for_source": MAINTENANCE,
        "KnowledgeStore.relink_relation_rows_for_objects": MAINTENANCE,
        "KnowledgeStore.source_build_rows": MAINTENANCE,
        "KnowledgeStore.source_build_state_page": MAINTENANCE,
    },
    "*/query_store.py": {
        "QueryStore.notebook_has_kg": VIEWER_READER,
        "QueryStore.search_notebook": VIEWER_READER,
        "QueryStore.top_concept_names": SHARED_TOOLING,
        "QueryStore.knowledge_type_count_rows_excluding_memory": SHARED_TOOLING,
        "QueryStore.knowledge_type_count_rows_for_sources": KEY_BOUNDED,
        "QueryStore.knowhow_knowledge_type_rows": KEY_BOUNDED,
        "QueryStore.load_notebook_scale_facts": STATE_PROBE,
        "QueryStore.mounted_bases_row": STATE_PROBE,
        "QueryStore.notebook_has_usable_kg": STATE_PROBE,
        "QueryStore.notebook_has_usable_base_kg": STATE_PROBE,
    },
    "postgres/search.py": {
        "notebook_knowledge_rows": VIEWER_READER,
        # interpolated table (``_SEARCH_TARGETS``): the legacy lexical arms
        # that knowledge_candidate_rows_for_terms takes with a viewer filter
        "_candidate_rows_for_terms": VIEWER_READER,
        "mention_claim_rows": BUILD_READER,
        "_knn_candidate_rows_for_terms": CEILING_BOUNDED,
    },
    "*/knowledge_counts_cache.py": {
        "_TYPE_STATUS_HALVES_SQL": COUNT_CACHE,
        "memory_type_status_counts": OWN_MEMORY,
        "_pending_sql": STATE_PROBE,
        "_pending_source_count_query": STATE_PROBE,
    },
    "*/governance_store.py": {
        "GovernanceStore.review_queue_rows": SHARED_TOOLING,
        "GovernanceStore.conflict_resolution_rows": SHARED_TOOLING,
        "GovernanceStore.conflict_relation_rows": SHARED_TOOLING,
        "GovernanceStore.conflict_relation_count": STATE_PROBE,
        "GovernanceStore.incremental_cluster_rows": DERIVED_LAYER,
        "GovernanceStore._existing_cluster_members": DERIVED_LAYER,
        "GovernanceStore.promotion_object_type_row": KEY_BOUNDED,
        "GovernanceStore.locate_approved_base_object": KEY_BOUNDED,
        "GovernanceStore.locate_approved_memory_base_objects": KEY_BOUNDED,
        "GovernanceStore.approve_promotion_in_transaction": MAINTENANCE,
        "GovernanceStore.approve_memory_promotion_in_transaction": MAINTENANCE,
        "GovernanceStore.merge_objects_in_transaction": MAINTENANCE,
        "GovernanceStore.update_object_in_transaction": MAINTENANCE,
        "GovernanceStore.update_edge_review": MAINTENANCE,
        "GovernanceStore.purge_memory_review_rows_on": MAINTENANCE,
        "GovernanceStore.strip_sources_evidence_on": MAINTENANCE,
        "GovernanceStore.sweep_orphan_clusters_page": MAINTENANCE,
        "_base_dedup_rows_for_update": MAINTENANCE,
    },
    "*/unified_kg_store.py": {
        # E4-2 graph-build readers
        "UnifiedKgStore.seed_payload_rows": BUILD_READER,
        "UnifiedKgStore.stream_seed_rows": BUILD_READER,
        "UnifiedKgStore.canonical_relation_seed_rows": BUILD_READER,
        "UnifiedKgStore.mention_seed_rows": BUILD_READER,
        "UnifiedKgStore.community_graph_rows": BUILD_READER,
        "UnifiedKgStore.cluster_size_histogram": BUILD_READER,
        "UnifiedKgStore.largest_clusters": BUILD_READER,
        "UnifiedKgStore.relation_provenance_counts": BUILD_READER,
        "UnifiedKgStore.catchup_window_members": BUILD_READER,
        "_not_memory_object_ref": MEMORY_SIDE,
        # derived layer
        "UnifiedKgStore.cluster_member_rows": DERIVED_LAYER,
        "UnifiedKgStore.cluster_map_rows": DERIVED_LAYER,
        "UnifiedKgStore.cluster_fold_rows": DERIVED_LAYER,
        "UnifiedKgStore.cluster_description_rows": DERIVED_LAYER,
        "UnifiedKgStore.cluster_evidence_rows": DERIVED_LAYER,
        "UnifiedKgStore.source_canonical_rows": DERIVED_LAYER,
        "UnifiedKgStore.resolve_focal": DERIVED_LAYER,
        "UnifiedKgStore.scratch_vector_rows": DERIVED_LAYER,
        "UnifiedKgStore.stream_scratch_rows": DERIVED_LAYER,
        "UnifiedKgStore.relation_endpoint_name_rows": KEY_BOUNDED,
        # retrieval legs
        "UnifiedKgStore.comention_peers": CEILING_BOUNDED,
        "_canonical_support_exists": CEILING_BOUNDED,
        "_object_support_exists": CEILING_BOUNDED,
        # PR-E2 E2-1: the weak-support hint's target probe (``SELECT 1``, judged
        # by the ceiling's or the viewer's condition its caller passes) and the
        # chunk-mix KG overlay's ceiling backstop (source ids of objects the
        # caller holds, judged against the run's ceiling)
        "_WEAK_TARGET_OBJECTS": CEILING_BOUNDED,
        "_weak_target_supported": CEILING_BOUNDED,
        "UnifiedKgStore.object_support_source_rows": CEILING_BOUNDED,
        # probes
        "UnifiedKgStore.cluster_input_facts": STATE_PROBE,
        "UnifiedKgStore.cluster_version_row": STATE_PROBE,
        "UnifiedKgStore.ppr_version_rows": STATE_PROBE,
        "UnifiedKgStore.concept_clusters_count": STATE_PROBE,
        "UnifiedKgStore.distinct_cluster_count": STATE_PROBE,
        # the rebuild's end-state totals (finish_rebuild_state runs them): the
        # shared graph only, so no member's Memory count can be derived
        "_END_STATE_OBJECT_COUNT_SQL": BUILD_READER,
        "_END_STATE_RELATION_COUNT_SQL": BUILD_READER,
        # maintenance
        "UnifiedKgStore.write_cluster_map_generation": MAINTENANCE,
    },
    "*/index_projection_store.py": {
        "IndexProjectionStore.active_object_graph_rows": BUILD_READER,
        "IndexProjectionStore.active_relation_graph_rows": BUILD_READER,
        "IndexProjectionStore.graph_rows": BUILD_READER,
        "_SHARED_CONTENT_FACTS": BUILD_READER,
        "_SHARED_REVIEWED_RELATIONS_SQL": BUILD_READER,
        "IndexProjectionStore.relation_ids_for_source_batch": CALLER_FILTERED,
        "IndexProjectionStore.version_facts": STATE_PROBE,
        "_MEMORY_CLUSTER_CANONICALS_SQL": MEMORY_SIDE,
        "_MEMORY_OBJECT_IDS_SQL": MEMORY_SIDE,
        "_MEMORY_RELATION_IDS_SQL": MEMORY_SIDE,
    },
    "source_subgraph_projection.py": {
        "source_subgraph_rows_on": CEILING_BOUNDED,
        "source_graph_partition_rows_on": CALLER_FILTERED,
    },
    "*/embedding_store.py": {
        "EmbeddingStore.knowledge_delta_rows": CEILING_BOUNDED,
        "EmbeddingStore.relation_delta_rows": CEILING_BOUNDED,
    },
    "*/source_store.py": {
        "SourceStore.source_from_row": STATE_PROBE,
        "SourceStore.sources_from_rows": STATE_PROBE,
    },
    "*/memory_sql.py": {
        "_memory_member_arm": MEMORY_SIDE,
        "memory_member_cluster_keys": MEMORY_SIDE,
    },
    "*/sharing_store.py": {
        "_MEMORY_NODES_SQL": MEMORY_SIDE,
        "_MEMORY_EDGES_SQL": MEMORY_SIDE,
        "_MEMORY_CLUSTERS_BASE_SQL": MEMORY_SIDE,
        "_memory_object_here": MEMORY_SIDE,
        "_COPY_SNAPSHOT_QUERIES": COPY,
        "_MEMORY_SNAPSHOT_TEXT": COPY,
        "SharingStore._copy_limit_violation": COPY,
        "SharingStore.validate_copy": COPY,
    },
    "*/memory_store.py": {
        "MemoryStore._validate_evidence_ref_on": KEY_BOUNDED,
        "MemoryStore.detach_memory_projection_on": MAINTENANCE,
        "MemoryStore.drop_memory_lexical_rows_on": MAINTENANCE,
    },
    "*/memory_isolation_store.py": {
        "MemoryIsolationStore.memory_objects": MAINTENANCE,
        "MemoryIsolationStore.purge_bridge_candidates": MAINTENANCE,
        "_APPROVED_MEMORY_PROMOTIONS_SQL": MAINTENANCE,
        "_CANONICAL_PAGE_SQL": MAINTENANCE,
        "_CENSUS_FACTS_SQL": MAINTENANCE,
        "_DANGLING_PAGE_SQL": MAINTENANCE,
        "_PRE_UPGRADE_SEED_CHECK_SQL": MAINTENANCE,
        "_STALE_MEMBER_PAGE_SQL": MAINTENANCE,
        "_STALE_MENTION_PAGE_SQL": MAINTENANCE,
    },
    "*/kg_build_job_store.py": {
        "KgBuildJobStore._delete_source_kg": MAINTENANCE,
    },
    "sqlite/notebook_delete_job_store.py": {
        # interpolated FTS shadow of knowledge_objects: a rowid page read that
        # only feeds the notebook delete's DELETE
        "NotebookDeleteJobStore.delete_fts_shadow_page": MAINTENANCE,
    },
    "sqlite/migrations.py": {
        "SqliteMigrator._migration_1": MAINTENANCE,
        "SqliteMigrator._migration_29": MAINTENANCE,
        "SqliteMigrator._migration_87": MAINTENANCE,
        # PR-E8: rewrites a public library's stored evidence; reads only that
        # library's candidate objects by id and hands nothing to a reader
        "SqliteMigrator._migration_88": MAINTENANCE,
    },
    "postgres/maintenance.py": {
        name: MAINTENANCE for name in (
            "PostgresMaintenanceAdapter.backfill_node_embeddings",
            "PostgresMaintenanceAdapter.backfill_relation_embeddings",
            "PostgresMaintenanceAdapter.backfill_source_fact_batch",
            "PostgresMaintenanceAdapter.backfill_source_index_batch",
            "PostgresMaintenanceAdapter.begin_source_index_backfill",
            "PostgresMaintenanceAdapter.clear_source_index",
            "PostgresMaintenanceAdapter.count_missing_node_vectors",
            "PostgresMaintenanceAdapter.count_sources_missing_kg",
            "PostgresMaintenanceAdapter.kg_covered_source_ids",
            "PostgresMaintenanceAdapter.kg_target_source_rows_page",
            "PostgresMaintenanceAdapter.knowledge_object_payload_page",
            "PostgresMaintenanceAdapter.node_embedding_counts",
            "PostgresMaintenanceAdapter.partial_kg_source_ids",
            "PostgresMaintenanceAdapter.resume_source_index_backfill_batch",
        )
    },
    "sqlite/maintenance.py": {
        name: MAINTENANCE for name in (
            "ReadOnlySQLiteInspector.concept_id_names",
            "ReadOnlySQLiteInspector.concept_names",
            "SQLiteMaintenanceAdapter.backfill_node_embeddings",
            "SQLiteMaintenanceAdapter.backfill_source_fact_batch",
            "SQLiteMaintenanceAdapter.backfill_source_index_batch",
            "SQLiteMaintenanceAdapter.begin_source_index_backfill",
            "SQLiteMaintenanceAdapter.clear_source_index",
            "SQLiteMaintenanceAdapter.count_missing_node_vectors",
            "SQLiteMaintenanceAdapter.count_sources_missing_kg",
            "SQLiteMaintenanceAdapter.gold_knowledge_object_rows",
            "SQLiteMaintenanceAdapter.kg_covered_source_ids",
            "SQLiteMaintenanceAdapter.kg_object_counts_by_notebook",
            "SQLiteMaintenanceAdapter.kg_target_source_rows_page",
            "SQLiteMaintenanceAdapter.knowledge_object_payload_page",
            "SQLiteMaintenanceAdapter.knowledge_object_payloads",
            "SQLiteMaintenanceAdapter.node_embedding_counts",
            "SQLiteMaintenanceAdapter.partial_kg_source_ids",
            "SQLiteMaintenanceAdapter.resume_source_index_backfill_batch",
            "SQLiteMaintenanceAdapter.sample_approved_object_payload",
            "SQLiteMaintenanceAdapter.sample_knowledge_objects",
        )
    },
}

# Names the plan and the E4-4 / E4-2 / E4-3 reports require to stay registered in
# these categories; a refactor that moves one out has to update this list too.
_REQUIRED = {
    ("*/knowledge_counts_cache.py", "_TYPE_STATUS_HALVES_SQL"): COUNT_CACHE,
    ("*/knowledge_counts_cache.py", "memory_type_status_counts"): OWN_MEMORY,
    ("*/knowledge_store.py", "KnowledgeStore.duplicate_seed_rows"): SHARED_TOOLING,
    ("*/governance_store.py", "GovernanceStore.review_queue_rows"): SHARED_TOOLING,
    ("*/knowledge_store.py", "KnowledgeStore.list_knowledge_page"): VIEWER_READER,
    ("*/unified_kg_store.py", "UnifiedKgStore.seed_payload_rows"): BUILD_READER,
    ("*/unified_kg_store.py", "UnifiedKgStore.canonical_relation_seed_rows"): BUILD_READER,
    ("*/unified_kg_store.py", "UnifiedKgStore.community_graph_rows"): BUILD_READER,
}


# -- the scanner ----------------------------------------------------------------
@dataclass(frozen=True)
class Site:
    path: str        # relative to backend/app/repositories
    qualname: str
    tables: tuple[str, ...]
    params: frozenset[str]
    names: frozenset[str]
    # Where to point a failure message; never part of a site's identity.
    diagnostic_lines: tuple[int, ...] = field(default=(), compare=False)


def _docstring_ids(tree: ast.AST) -> set[int]:
    """ids of string constants that are bare expression statements (docstrings
    and stray string statements): prose, never SQL that runs."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            out.add(id(node.value))
    return out


def _text(node: ast.AST, prose: set[int]) -> str:
    return "\n".join(
        n.value for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in prose
    )


def _names(node: ast.AST) -> frozenset[str]:
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return frozenset(out)


def _params(node: ast.AST) -> frozenset[str]:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return frozenset()
    a = node.args
    return frozenset(x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs))


def _assign_name(node: ast.AST) -> str | None:
    target = node.targets[0] if isinstance(node, ast.Assign) else node.target
    return target.id if isinstance(target, ast.Name) else None


def scan_source(source: str, path: str) -> list[Site]:
    """Every reader-by-notebook site of one module's source text."""
    tree = ast.parse(source)
    prose = _docstring_ids(tree)
    sites: list[Site] = []
    # Module-level catalogs that name one of the tables as a bare string (e.g.
    # PostgreSQL search's ``_SEARCH_TARGETS``): a site that reads FROM an
    # interpolated table and uses such a catalog reads the tables it names.
    catalogs: dict[str, set[str]] = {}
    for child in ast.iter_child_nodes(tree):
        if isinstance(child, (ast.Assign, ast.AnnAssign)):
            name = _assign_name(child)
            named = {
                n.value for n in ast.walk(child)
                if isinstance(n, ast.Constant) and n.value in _TABLES
            }
            if name is not None and named:
                catalogs[name] = named

    def consider(node: ast.AST, qualname: str) -> None:
        text = _text(node, prose)
        tables = {m.group(1).lower() for m in _READ.finditer(text)}
        if _INTERPOLATED_READ.search(text):
            for catalog in _names(node) & catalogs.keys():
                tables |= catalogs[catalog]
        tables = tuple(sorted(tables))
        if tables and _BY_NOTEBOOK.search(text):
            sites.append(Site(path, qualname, tables, _params(node), _names(node),
                              diagnostic_lines=(node.lineno,)))

    def walk(node: ast.AST, prefix: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, [*prefix, child.name])
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                consider(child, ".".join([*prefix, child.name]))
            elif isinstance(child, (ast.Assign, ast.AnnAssign)):
                name = _assign_name(child)
                if name is not None:
                    consider(child, ".".join([*prefix, name]))
            elif isinstance(child, (ast.If, ast.Try, ast.With)):
                walk(child, prefix)

    walk(tree, [])
    return sites


def scan_tree() -> list[Site]:
    sites: list[Site] = []
    for path in sorted(REPOSITORIES.rglob("*.py")):
        rel = path.relative_to(REPOSITORIES).as_posix()
        if "__pycache__" in rel:
            continue
        sites.extend(scan_source(path.read_text(encoding="utf-8"), rel))
    return sites


def _registry_key(path: str) -> list[str]:
    return [path, "*/" + path.rsplit("/", 1)[-1]] if "/" in path else [path]


def category_of(site: Site, registry: dict[str, dict[str, str]] = _REGISTRY) -> str | None:
    for key in _registry_key(site.path):
        found = registry.get(key, {}).get(site.qualname)
        if found is not None:
            return found
    return None


def _where(site: Site) -> str:
    line = site.diagnostic_lines[0] if site.diagnostic_lines else "?"
    return f"backend/app/repositories/{site.path}:{line} {site.qualname}"


@pytest.fixture(scope="module")
def sites() -> list[Site]:
    return scan_tree()


# -- the guard ------------------------------------------------------------------
def test_every_reader_by_notebook_is_registered(sites):
    unregistered = [
        f"{_where(s)} reads {', '.join(s.tables)} by notebook"
        for s in sites if category_of(s) is None
    ]
    assert not unregistered, (
        "unregistered Memory-sensitive readers; classify each in "
        "test_memory_reader_guard._REGISTRY (see CATEGORIES):\n" + "\n".join(unregistered)
    )


def test_every_registration_names_a_live_reader(sites):
    seen: set[tuple[str, str]] = set()
    for s in sites:
        for key in _registry_key(s.path):
            seen.add((key, s.qualname))
    stale = sorted(
        f"{key} {name}"
        for key, names in _REGISTRY.items() for name in names
        if (key, name) not in seen
    )
    assert stale == [], "registered names that no longer read by notebook: " + ", ".join(stale)


def test_every_category_is_known():
    used = {cat for names in _REGISTRY.values() for cat in names.values()}
    assert used <= set(CATEGORIES), used - set(CATEGORIES)


def test_required_registrations_hold():
    for (key, name), category in _REQUIRED.items():
        assert _REGISTRY.get(key, {}).get(name) == category, (key, name)


def test_the_count_cache_constant_is_found_on_both_backends(sites):
    found = {s.path for s in sites if s.qualname == "_TYPE_STATUS_HALVES_SQL"}
    assert found == {"postgres/knowledge_counts_cache.py", "sqlite/knowledge_counts_cache.py"}


def test_mechanical_categories_match_the_code(sites):
    wrong = []
    for s in sites:
        category = category_of(s)
        if category == VIEWER_READER and "viewer_id" not in s.params:
            wrong.append(f"{_where(s)}: {VIEWER_READER} without a viewer_id parameter")
        elif category == OWN_MEMORY and not (s.names & _OWN_MEMORY_NAMES):
            wrong.append(f"{_where(s)}: {OWN_MEMORY} without own_memory_source")
        elif category in (SHARED_TOOLING, BUILD_READER) and not (s.names & _EXCLUSION_NAMES):
            wrong.append(f"{_where(s)}: {category} without a Memory exclusion fragment")
        elif category == COUNT_CACHE and not (s.names & _COUNT_CACHE_NAMES):
            wrong.append(f"{_where(s)}: {COUNT_CACHE} without memory_source_type_predicate")
    assert wrong == []


# -- the scanner is not vacuous ---------------------------------------------------
_SYNTHETIC = '''
"""Module docstring: SELECT id FROM knowledge_objects WHERE notebook_id=? is prose."""
_CONST = "SELECT id FROM knowledge_objects o WHERE o.notebook_id=?"
_BY_ID = "SELECT id FROM knowledge_objects WHERE id=?"


class Store:
    _CLASS_SQL = "SELECT 1 FROM concept_clusters WHERE notebook_id=%s"

    def reader(self, db, notebook_id, viewer_id=None):
        """FROM knowledge_relations WHERE notebook_id -- a docstring, ignored."""
        return db.execute(
            f"SELECT r.id FROM knowledge_relations r "
            f"JOIN knowledge_objects o ON o.id = r.source_object_id "
            f"WHERE r.notebook_id = %s",
            (notebook_id,),
        )

    def writer(self, db, notebook_id):
        db.execute("DELETE FROM knowledge_objects WHERE notebook_id=?", (notebook_id,))

    def prose_only(self):
        """SELECT * FROM knowledge_objects WHERE notebook_id=?"""
        return None
'''


def test_the_scanner_finds_functions_methods_and_constants():
    found = {s.qualname: s for s in scan_source(_SYNTHETIC, "sqlite/synthetic.py")}
    assert set(found) == {"_CONST", "Store._CLASS_SQL", "Store.reader"}
    assert found["Store.reader"].tables == ("knowledge_objects", "knowledge_relations")
    assert "viewer_id" in found["Store.reader"].params


def test_an_unregistered_reader_is_reported_with_its_location():
    site = next(s for s in scan_source(_SYNTHETIC, "sqlite/knowledge_store.py")
                if s.qualname == "Store.reader")
    assert category_of(site) is None
    def_line = _SYNTHETIC.splitlines().index("    def reader(self, db, notebook_id, viewer_id=None):") + 1
    assert _where(site) == f"backend/app/repositories/sqlite/knowledge_store.py:{def_line} Store.reader"
    # the same name registered for either backend is found by name, not by line
    assert category_of(site, {"*/knowledge_store.py": {"Store.reader": KEY_BOUNDED}}) == KEY_BOUNDED


_INTERPOLATED = '''
_TARGETS = {("knowledge_objects", "id"): "knowledge_objects", ("chunks", "id"): "chunks"}
_GRAPH_TABLES = frozenset({"knowledge_relations"})


def reader(db, table, notebook_id):
    target = _TARGETS[(table, "id")]
    return db.execute(f"SELECT id FROM {target} WHERE notebook_id=%s", (notebook_id,))


def templated(db, notebook_id):
    for table in _GRAPH_TABLES:
        db.execute(sql.SQL("SELECT 1 FROM {} WHERE notebook_id=%s").format(table))


def deleter(db, notebook_id):
    for table in _GRAPH_TABLES:
        db.execute(sql.SQL("DELETE FROM {} WHERE notebook_id=%s").format(table))


def untargeted(db, table, notebook_id):
    return db.execute(f"SELECT id FROM {table} WHERE notebook_id=%s", (notebook_id,))
'''


def test_an_interpolated_table_is_read_through_the_catalog_it_comes_from():
    found = {s.qualname: s for s in scan_source(_INTERPOLATED, "postgres/synthetic.py")}
    assert set(found) == {"reader", "templated"}
    assert found["reader"].tables == ("knowledge_objects",)
    assert found["templated"].tables == ("knowledge_relations",)
