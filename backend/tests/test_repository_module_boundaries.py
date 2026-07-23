"""Task 13 module boundaries: the knowledge-domain persistence stores are
facade-independent components; the service layers that used to reach into the
facade's raw connections consume them instead."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"

STORE_MODULES = (
    "app/repositories/sqlite/knowledge_store.py",
    "app/repositories/sqlite/governance_store.py",
    "app/repositories/sqlite/unified_kg_store.py",
    "app/services/schema_registry.py",
    "app/services/knowledge_contracts.py",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_knowledge_stores_never_import_the_facade():
    for rel in STORE_MODULES:
        modules = _imports(BACKEND / rel)
        assert not any(
            m.startswith("app.services.sqlite_repository") for m in modules
        ), rel
        assert not any(
            m.startswith("app.services.repository_runtime") for m in modules
        ), rel


def test_kg_search_module_keeps_only_pure_hit_merging():
    source = (BACKEND / "app/services/kg/search.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    assert functions == {"merge_search_hits"}
    assert ".execute(" not in source  # no SQL left behind


def test_communities_module_consumes_unified_store_not_connect():
    source = (BACKEND / "app/services/communities.py").read_text(encoding="utf-8")
    assert "_connect" not in source
    assert ".execute(" not in source
    assert "self.unified_kg" in source and "_runtime" not in source


def test_stores_own_the_plan_frozen_primitives():
    from app.repositories.sqlite.governance_store import GovernanceStore
    from app.repositories.sqlite.knowledge_store import KnowledgeStore
    from app.repositories.sqlite.unified_kg_store import UnifiedKgStore
    from app.services.schema_registry import SchemaRegistryService

    for name in (
        "insert_object_chunk", "insert_relation_chunk", "insert_kg_fts_rows",
        "replace_object_sources", "get_object_row", "add_relations",
        "relations_for_notebook", "fts_search", "chunk_fts_search",
        "backfill_fts", "clear_source_extraction_state",
        "begin_extraction_run", "finish_extraction_run",
    ):
        assert name in KnowledgeStore.__dict__, name
    for name in (
        "update_edge_review", "delete_clusters", "insert_clusters",
        "write_merge_candidate", "set_merge_decision",
        "write_conflict_candidate", "set_conflict_status",
        "get_conflict_candidate", "decided_pairs", "decided_seed_pairs",
        "concept_whitelist_terms", "approve_promotion_in_transaction",
        "update_object_in_transaction", "merge_objects_in_transaction",
    ):
        assert name in GovernanceStore.__dict__, name
    for name in (
        "state_row", "mark_dirty", "bump_cluster_seq", "cluster_input_facts",
        "cluster_map_rows", "clear_scratch_run", "finish_rebuild_state",
        "replace_canonical_relations", "replace_mention_bridge",
        "replace_communities", "checkpoint_gc", "checkpoint_clear",
        "checkpoint_load", "checkpoint_put",
    ):
        assert name in UnifiedKgStore.__dict__, name
    for name in (
        "effective_schemas", "list_object_schemas", "create_object_schema",
        "update_object_schema", "delete_object_schema", "propose_schemas",
    ):
        assert name in SchemaRegistryService.__dict__, name


def test_facade_keeps_frozen_signature_delegates():
    from app.services.repository_facade import RepositoryFacade
    from app.services.sqlite_repository import SQLiteRepository

    for name in (
        "add_relations", "relations_for_notebook", "backfill_kg_fts",
        "knowledge_types", "list_knowledge", "knowledge_graph",
        "effective_schemas", "propose_schemas",
        "write_merge_candidate", "set_merge_decision", "pending_merges",
        "write_conflict_candidate", "set_conflict_status",
        "get_conflict_candidate", "decided_pairs", "decided_seed_pairs",
        "concept_whitelist_terms", "approve_promotion", "reject_promotion",
        "update_knowledge", "merge_knowledge",
        "_rebuild_ckpt_gc", "_rebuild_ckpt_clear",
        "_rebuild_ckpt_load", "_rebuild_ckpt_put",
        "_mark_unified_kg_dirty", "_bump_cluster_mutation_seq",
        "_clear_source_extraction_state", "_source_ids_from_evidence",
    ):
        owner = (
            SQLiteRepository
            if name in {"_clear_source_extraction_state", "_source_ids_from_evidence"}
            else RepositoryFacade
        )
        assert name in owner.__dict__, name


def test_knowledge_contracts_is_leaf_of_the_dependency_graph():
    modules = _imports(BACKEND / "app/services/knowledge_contracts.py")
    app_imports = {m for m in modules if m.startswith("app.")}
    assert app_imports == set(), app_imports
