from __future__ import annotations

import ast
import json
from pathlib import Path

from tests.architecture.repository_callers import (
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
        "completion_advance_state",
        "completion_candidate_rows",
        "completion_element_rows",
        "completion_existing_keys",
        "completion_mark_state_stale",
        "completion_transition_mode_state",
        "completion_page",
        "completion_pending_states",
        "completion_validate_scope",
        "clear_source_graph_state",
        # `embedding_rows`(整个 notebook 的全类型向量)是**刻意缺席**的:增量融合
        # 的无 ANN 桥接分支只消费 concept 向量,读取已收窄成下面这条,这个精确集合
        # 断言就是让「退回全类型读」失败关闭 —— 与 `relink_rows` / `cluster_map_rows`
        # 两条同款登记。
        "concept_embedding_rows",
        "delete_notebook_graph_rows",
        "embedding_rows_for_objects",
        "fts_search",
        "incremental_object_rows",
        "insert_kg_fts_rows",
        "insert_completion_relations",
        "insert_object_chunk",
        "insert_object_source_rows",
        "insert_relation_chunk",
        "insert_source_fact_rows",
        "neighbor_relation_rows",
        "notebook_tier_row",
        "object_meta_rows_for_notebook",
        # `relink_rows` (whole-notebook) is deliberately ABSENT: the lifecycle
        # service pages relink by source now, and this exact-set assertion is what
        # makes a regression to the unbounded read fail closed.
        "relink_object_rows_for_source",
        "relink_orphan_source_ids",
        "relink_relation_rows_for_objects",
        "relink_source_is_live",
        "relink_source_page",
        "source_build_state_page",
        "unified_graph_rows",
        "validate_source_fact_publish",
    },
    "governance_store": {
        # Rebuild must re-read live decisions inside the pending-publication
        # transaction so a decision committed after clustering cannot resurface.
        "decided_seed_pairs_from",
        "delete_clusters",
        "delete_pending_merges",
        "incremental_cluster_rows",
        "insert_clusters",
        "insert_merge_candidate",
        "insert_pending_merge_rows",
        # `merge_candidate_pairs` (whole-notebook) is deliberately ABSENT: the
        # incremental fusion path reads merge candidates bounded by this source's
        # own bridge canonical ids now (PR-C), and this exact-set assertion is
        # what makes a regression to the unbounded read fail closed — mirroring
        # the `relink_rows` note above.
        "merge_candidate_pairs_for_canonicals",
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
        "cluster_fold_rows",
        "cluster_input_facts",
        # `cluster_map_rows`(整表范围读)是**刻意保留**的一处,不是遗漏:增量融合
        # 里无 ANN 的暴力桥接分支要折叠被 `kg_incremental_tier2_max_entities` 界住
        # 的整批既有 concept,定点分批实测比一次范围读慢约 20×(数字与论证登记在
        # `incremental_fuse_source` 的 ex_cmap 处)。有界化的是 ANN 分支,走上面那条
        # `cluster_fold_rows`。
        "cluster_map_rows",
        "communities_count",
        "discard_board_dependent_kg_analysis_artifacts",
        "community_graph_rows",
        "community_member_ids",
        "community_reports",
        "community_rows_for_summary",
        # 与上一条配对:补账本路径在**事务外**读回板块划分,发布事务里复核它还在
        # (codex 第 16 轮 P2),不在就整份放弃发布。
        "board_partition_still_holds",
        "concept_clusters_count",
        "distinct_cluster_count",
        "finish_rebuild_state",
        "insert_canonical_scratch_rows",
        "insert_scratch_rows",
        "mention_edges_count",
        "mention_alias_candidate_batches",
        "mention_seed_rows",
        "cluster_size_histogram",
        # 预计算的新鲜度闸读的是账本**整行**(簇世代盖在 payload 里,刻意不加列),
        # 与 T3 报告共用同一个读 —— 早先那个只取 seq 的窄读已经删掉。
        "kg_analysis_artifact_rows",
        "largest_clusters",
        "relation_provenance_counts",
        "replace_canonical_relations",
        "replace_communities",
        "replace_kg_analysis_artifacts",
        "replace_mention_bridge",
        "source_canonical_rows",
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


def test_api_repository_dependency_uses_the_cached_backend_factory():
    import inspect

    from app.api import deps

    source = inspect.getsource(deps.repository)
    assert hasattr(deps.repository, "cache_clear")
    assert "create_application_repository(get_settings())" in source
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
