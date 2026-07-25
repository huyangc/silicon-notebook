from __future__ import annotations

import ast
import json
from pathlib import Path

from tests.architecture.repository_callers import (
    FACADE_IMPORT_TARGETS,
    collect_caller_contract,
    private_repository_sites,
    production_source_index,
)
from tests.architecture.semantic_source import PythonSourceIndex


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = (
    ROOT
    / "backend"
    / "tests"
    / "fixtures"
    / "repository_contract"
    / "caller_boundaries.json"
)
ENTRY_FIELDS = {"path", "scope", "kind", "target", "count", "reason"}
RETIRED_RETRIEVAL_PRIVATES = {
    "_answer_context",
    "_chunk_answer_context",
    "_ppr_retrieve",
    "_retrieve_scored",
}
LIFECYCLE_STORE_CALLS = {
    "knowledge": {
        "active_object_count",
        "community_context_rows",
        "clear_source_graph_state",
        "delete_notebook_graph_rows",
        "embedding_rows",
        "embedding_rows_for_objects",
        "incremental_object_rows",
        "insert_kg_fts_rows",
        "insert_object_chunk",
        "insert_object_source_rows",
        "insert_relation_chunk",
        "neighbor_relation_rows",
        "notebook_tier_row",
        "object_meta_rows_for_notebook",
        "relink_rows",
        "source_build_rows",
        "sources_with_elements",
        "unified_graph_rows",
    },
    "governance_store": {
        "delete_clusters",
        "delete_pending_merges",
        "incremental_cluster_rows",
        "insert_clusters",
        "insert_merge_candidate",
        "insert_pending_merge_rows",
        "merge_candidate_pairs",
        "sweep_orphan_clusters",
        "valid_object_ids",
    },
    "unified_kg": {
        "canonical_relation_seed_rows",
        "canonical_relations_count",
        "checkpoint_clear",
        "checkpoint_gc",
        "checkpoint_load",
        "checkpoint_put",
        "clear_canonical_scratch_run",
        "clear_mention_bridge",
        "clear_scratch_run",
        "cluster_description_rows",
        "cluster_evidence_rows",
        "cluster_input_facts",
        "communities_count",
        "community_graph_rows",
        "community_member_ids",
        "community_reports",
        "community_rows_for_summary",
        "concept_clusters_count",
        "distinct_cluster_count",
        "finish_rebuild_state",
        "insert_canonical_scratch_rows",
        "insert_scratch_rows",
        "mention_edges_count",
        "mention_alias_candidate_batches",
        "mention_seed_rows",
        "replace_canonical_relations",
        "replace_communities",
        "replace_mention_bridge",
        "scratch_vector_rows",
        "seed_payload_rows",
        "set_community_seq",
        "set_community_summary",
        "state_row",
        "stream_seed_rows",
        "swap_cluster_map_from_scratch",
    },
}


def _contract() -> dict[str, list[dict[str, object]]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_caller_contract_uses_semantic_identity_and_reasoned_counts():
    contract = _contract()

    assert set(contract) == {
        "facade_imports",
        "independent_private",
        "independent_sql",
        "sqlite_connect",
    }
    for category, entries in contract.items():
        assert entries, category
        for entry in entries:
            assert set(entry) == ENTRY_FIELDS, (category, entry)
            assert entry["path"]
            assert entry["scope"]
            assert entry["kind"] == category
            assert entry["target"]
            assert isinstance(entry["count"], int) and entry["count"] > 0
            assert entry["reason"]
            assert "line" not in entry


def test_caller_contract_matches_live_semantic_boundaries():
    assert _contract() == collect_caller_contract()


def test_facade_import_targets_cover_module_and_symbol_import_forms():
    assert FACADE_IMPORT_TARGETS == {
        "app.services.sqlite_repository",
        "app.services.sqlite_repository:SQLiteRepository",
        "app.services:sqlite_repository",
    }


def test_api_repository_dependency_uses_the_cached_backend_factory():
    import inspect

    from app.api import deps

    source = inspect.getsource(deps.repository)
    assert hasattr(deps.repository, "cache_clear")
    assert "create_repository(get_settings())" in source
    assert "SQLiteRepository" not in source


def test_lifecycle_service_is_sql_free_and_uses_exact_store_seams():
    path = ROOT / "backend" / "app" / "services" / "knowledge_lifecycle.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    actual = {owner: set() for owner in LIFECYCLE_STORE_CALLS}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        seat = node.func.value
        if (
            isinstance(seat, ast.Attribute)
            and isinstance(seat.value, ast.Name)
            and seat.value.id == "self"
            and seat.attr in actual
        ):
            actual[seat.attr].add(node.func.attr)

    assert actual == LIFECYCLE_STORE_CALLS
    assert not [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"execute", "executemany", "executescript"}
    ]

    from app.repositories import ports

    protocol_by_owner = {
        "knowledge": ports.KnowledgeStorePort,
        "governance_store": ports.GovernanceStorePort,
        "unified_kg": ports.UnifiedKgStorePort,
    }
    missing = {}
    for owner, calls in actual.items():
        protocol = protocol_by_owner[owner]
        declared = {
            name
            for base in protocol.__mro__
            for name, value in base.__dict__.items()
            if callable(value)
        }
        if calls - declared:
            missing[owner] = sorted(calls - declared)
    assert missing == {}


def test_retired_retrieval_privates_have_no_production_callers():
    assert {
        finding.key
        for finding in private_repository_sites()
        if finding.key.target in RETIRED_RETRIEVAL_PRIVATES
    } == set()


def test_eval_insert_source_helper_has_no_production_consumer():
    assert {
        finding.key
        for finding in production_source_index().attributes()
        if finding.key.path != "backend/app/services/sqlite_repository.py"
        and finding.key.target.rsplit(".", 1)[-1]
        == "eval_insert_source_for_test"
    } == set()


def test_diag_slow_stays_host_safe_and_read_only():
    path = ROOT / "scripts" / "diag_slow.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(not alias.name.startswith("app") for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("app")
    for line in source.splitlines():
        if "sqlite3.connect(" in line:
            assert "mode=ro" in line
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            statement = node.value.lstrip().upper()
            assert not statement.startswith(
                ("INSERT INTO", "UPDATE ", "DELETE FROM", "REPLACE INTO")
            )


def test_domain_routes_use_narrow_ports_not_notebook_repository():
    paths = sorted((ROOT / "backend" / "app" / "api").glob("*_routes.py"))
    offenders: list[tuple[str, str]] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for argument in (*node.args.args, *node.args.kwonlyargs):
                annotation = argument.annotation
                name = ""
                if isinstance(annotation, ast.Name):
                    name = annotation.id
                elif isinstance(annotation, ast.Attribute):
                    name = annotation.attr
                if name == "NotebookRepository":
                    offenders.append((f"{path.name}:{node.name}", argument.arg))

    assert offenders == []


def test_portable_application_ports_exclude_maintenance_operations():
    from app.repositories.ports import NotebookRepository, SQLiteMaintenancePort

    maintenance_operations = (
        "delete_notebook_kg",
        "backfill_kg_fts",
        "backfill_chunk_fts",
        "build_scale_index",
        "fold_scale_index_delta",
    )
    for name in maintenance_operations:
        assert hasattr(SQLiteMaintenancePort, name), name
    for name in (
        "backfill_kg_fts",
        "backfill_chunk_fts",
        "fold_scale_index_delta",
        "build_scale_index",
        "eval_insert_source_for_test",
        "maintenance",
    ):
        assert not hasattr(NotebookRepository, name), name


def test_maintenance_adapter_implements_the_port():
    from app.repositories.sqlite.maintenance import (
        ReadOnlySQLiteInspector,
        SQLiteMaintenanceAdapter,
    )

    for name in (
        "delete_notebook_kg",
        "backfill_kg_fts",
        "backfill_chunk_fts",
        "build_scale_index",
        "fold_scale_index_delta",
    ):
        assert name in SQLiteMaintenanceAdapter.__dict__, name
    source = (
        ROOT
        / "backend"
        / "app"
        / "repositories"
        / "sqlite"
        / "maintenance.py"
    ).read_text(encoding="utf-8")
    assert "mode=ro" in source
    for name in ("connect", "table_count", "vector_blocks", "detect_vector_dim"):
        assert name in ReadOnlySQLiteInspector.__dict__, name


def test_facade_exposes_the_maintenance_adapter():
    import inspect

    from app.services.sqlite_repository import SQLiteRepository

    assert isinstance(
        inspect.getattr_static(SQLiteRepository, "maintenance"),
        property,
    )


def test_closed_remediation_modules_stay_closed():
    paths = (
        ROOT / "backend" / "app" / "services" / "scale_index_builder.py",
        ROOT / "backend" / "app" / "services" / "communities.py",
        ROOT / "backend" / "app" / "services" / "report_engine.py",
    )
    index = PythonSourceIndex.from_paths(ROOT, paths)

    assert {
        finding.key
        for finding in index.calls()
        if finding.key.path.endswith("scale_index_builder.py")
        and finding.key.target.rsplit(".", 1)[-1]
        in {"execute", "executemany", "executescript"}
    } == set()
    assert {
        finding.key
        for finding in index.attributes()
        if finding.key.path.endswith(("communities.py", "report_engine.py"))
        and finding.key.target.rsplit(".", 1)[-1] == "_runtime"
    } == set()
