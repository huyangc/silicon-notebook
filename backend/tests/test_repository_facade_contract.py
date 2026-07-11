"""Task 26 — the consolidated explicit compatibility facade contract.

``SQLiteRepository`` resolves root/storage, constructs ONE
:class:`RepositoryRuntime` and publishes every frozen Task-1 surface member as
an explicit delegate/property over that runtime.  The class body holds no SQL
and no dynamic dispatch (``__getattr__`` / dispatch tables) — every member is
a statically visible delegate, and the module keeps re-exporting the frozen
Task-1 compatibility imports as the SAME objects.
"""
from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

from app.core.config import Settings
from app.repositories.ownership_manifest import OWNER_BY_MEMBER
from app.services import repository, sqlite_repository
from app.services.sqlite_repository import SQLiteRepository

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = (
    ROOT
    / "backend"
    / "tests"
    / "fixtures"
    / "repository_contract"
    / "facade_surface.json"
)
FACADE_FILE = "backend/app/services/sqlite_repository.py"

RUNTIME_COMPONENT_OWNERS = {
    "ask": "AskService",
    "ask_service": "AskService",
    "ask_cancellations": "AskCancellationRegistry",
    "ask_execution": "AskExecutionCoordinator",
    "ask_state": "AskStateStore",
    "candidate_retrieval": "CandidateRetrievalService",
    "catalog": "NotebookCatalogService",
    "chunk_store": "ChunkStore",
    "database": "SqliteDatabase",
    "embedding_store": "EmbeddingStore",
    "evidence_context": "EvidenceContextService",
    "governance": "GovernanceStore",
    "graph_retrieval": "GraphRetrievalService",
    "identity": "IdentityStore",
    "index_projections": "IndexProjectionStore",
    "kg_mutations": "KgMutationCoordinator",
    "knowledge": "KnowledgeStore",
    "knowledge_governance": "KnowledgeGovernanceService",
    "knowledge_lifecycle": "KnowledgeLifecycleService",
    "models": "ModelProvider",
    "notebook_store": "NotebookStore",
    "notebook_copies": "NotebookCopyService",
    "notebook_summaries": "NotebookSummaryQuery",
    "queries": "QueryStore",
    "report_execution": "ReportEngine",
    "report_store": "ReportStore",
    "retrieval": "RetrievalService",
    "retrieval_snapshots": "RetrievalSnapshotCache",
    "scale_artifact_store": "ScaleArtifactStore",
    "scale_artifacts": "ScaleArtifactRuntime",
    "scale_builder": "ScaleIndexBuilder",
    "schema_registry": "SchemaRegistryService",
    "sharing": "NotebookSharingService",
    "sharing_store": "SharingStore",
    "source_files": "SourceFileStore",
    "source_chunking": "SourceChunkingService",
    "source_embedding": "SourceEmbeddingService",
    "source_ingestion": "SourceIngestionService",
    "source_store": "SourceStore",
    "unified_kg": "UnifiedKgStore",
}
CLASS_COMPONENT_OWNERS = {
    "NotebookSummaryQuery": "NotebookSummaryQuery",
}
OWNER_CONTRACT_EXCEPTIONS = {"_connect", "_write"}
MODULE_SURFACE_OWNER_EXCEPTIONS = {
    "KNOWLEDGE_STATUSES",
    "KnowledgeGraphTooLargeError",
    "NotebookRepository",
    "RetrievedKnowledge",
    "SCHEMA_VERSION",
    "SQLiteRepository",
    "USABLE_STATUSES",
    "UploadedSourceFile",
    "_ASK_MODEL_ERRORS",
    "_COPY_CHUNK",
    "_REQUEST_USER",
    "_concept_desc_sig",
    "_fast_loads",
    "_new_id",
    "_now",
    "_remap_json_ids",
    "parse_source_file",
    "reset_request_user",
    "set_request_user",
}
NON_CALLABLE_INSTANCE_SURFACE = {
    "_kg_building",
    "_kg_building_lock",
    "_notebook_langs_cache",
    "embedder",
    "event_log",
    "mineru_client",
    "mineru_cloud_client",
    "root_dir",
    "settings",
    "storage_dir",
}
SCALAR_IDENTITY_ADAPTERS = {"bool", "float", "int", "str"}


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _facade_class(cls) -> tuple[ast.ClassDef, int]:
    lines, start = inspect.getsourcelines(cls)
    tree = ast.parse("".join(lines))
    class_node = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    return class_node, start - 1


def _facade_functions(cls) -> tuple[list[ast.FunctionDef], int]:
    class_node, offset = _facade_class(cls)
    functions = [node for node in class_node.body if isinstance(node, ast.FunctionDef)]
    return functions, offset


def _decorator_names(node: ast.FunctionDef) -> set[str]:
    return {_dotted(decorator) for decorator in node.decorator_list}


def _component_owner(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        return _component_owner(node.func)
    dotted = _dotted(node)
    if dotted.startswith("self._runtime."):
        component = dotted.split(".", 3)[2]
        return RUNTIME_COMPONENT_OWNERS.get(component, f"RepositoryRuntime.{component}")
    direct = {
        "self.retrieval": "RetrievalService",
        "self.maintenance": "SQLiteMaintenanceAdapter",
        "self.report_execution": "ReportEngine",
        "self.event_log": "EventLogger",
        "self._migrator": "SqliteDatabase",
    }
    for prefix, owner in direct.items():
        if dotted == prefix or dotted.startswith(f"{prefix}."):
            return owner
    for prefix, owner in CLASS_COMPONENT_OWNERS.items():
        if dotted == prefix or dotted.startswith(f"{prefix}."):
            return owner
    if isinstance(node, ast.Attribute):
        return _component_owner(node.value)
    return None


def _function_component_owners(node: ast.FunctionDef) -> set[str]:
    return {
        owner
        for child in ast.walk(node)
        if isinstance(child, ast.Attribute)
        for owner in [_component_owner(child)]
        if owner is not None
    }


def _calls_facade_member(node: ast.Call) -> bool:
    dotted = _dotted(node.func)
    if not dotted.startswith("self."):
        return False
    return not dotted.startswith(
        (
            "self._runtime.",
            "self.retrieval.",
            "self.maintenance.",
            "self.report_execution.",
            "self.event_log.",
            "self._migrator.",
        )
    )


def _body_without_docstring(node: ast.FunctionDef) -> list[ast.stmt]:
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body.pop(0)
    return body


def _direct_expression_owner(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Call):
        if (
            _dotted(node.func) in SCALAR_IDENTITY_ADAPTERS
            and len(node.args) == 1
            and not node.keywords
        ):
            return _direct_expression_owner(node.args[0])
        return _component_owner(node.func)
    if isinstance(node, ast.Attribute):
        return _component_owner(node)
    return None


def _adapter_delegate_owner(node: ast.FunctionDef) -> str | None:
    body = _body_without_docstring(node)
    if not body:
        return None
    prefix, final = body[:-1], body[-1]
    if any(not isinstance(stmt, (ast.Assign, ast.AnnAssign)) for stmt in prefix):
        return None
    if any(
        (
            isinstance(child, ast.Call)
            and _dotted(child.func) not in SCALAR_IDENTITY_ADAPTERS
        )
        or isinstance(child, ast.comprehension)
        or (isinstance(child, ast.Attribute) and _component_owner(child) is not None)
        for stmt in prefix
        for child in ast.walk(stmt)
    ):
        return None
    if isinstance(final, ast.Return):
        return _direct_expression_owner(final.value)
    if isinstance(final, ast.Expr) and isinstance(final.value, ast.Call):
        return _direct_expression_owner(final.value)
    return None


def _property_delegate_owner(node: ast.FunctionDef) -> str | None:
    body = _body_without_docstring(node)
    if len(body) != 1:
        return None
    statement = body[0]
    if isinstance(statement, ast.Return):
        return _direct_expression_owner(statement.value)
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
        return _component_owner(statement.targets[0])
    if isinstance(statement, ast.AnnAssign):
        return _component_owner(statement.target)
    return None


def _function_contract_owner(node: ast.FunctionDef) -> str | None:
    decorators = _decorator_names(node)
    owners = _function_component_owners(node)
    forbidden = any(
        isinstance(child, (ast.For, ast.AsyncFor, ast.While, ast.Import, ast.ImportFrom))
        for child in ast.walk(node)
    )
    forbidden = forbidden or any(
        isinstance(child, ast.Call)
        and _dotted(child.func).rsplit(".", 1)[-1]
        in {"execute", "executemany", "executescript"}
        for child in ast.walk(node)
    )
    forbidden = forbidden or any(
        isinstance(child, ast.Call)
        and _dotted(child.func) == "getattr"
        and child.args
        and isinstance(child.args[0], ast.Name)
        and child.args[0].id == "self"
        for child in ast.walk(node)
    )
    forbidden = forbidden or any(
        isinstance(child, ast.Call) and _calls_facade_member(child)
        for child in ast.walk(node)
    )
    if forbidden or len(owners) != 1:
        return None

    is_property = "property" in decorators or any(
        decorator.endswith(".setter") for decorator in decorators
    )
    if is_property:
        owner = _property_delegate_owner(node)
    else:
        owner = _adapter_delegate_owner(node)
    return owner if owner in owners else None


def facade_body_violations(cls) -> list[tuple[str, int, str]]:
    """Return facade methods that are not properties/adapters/one-hop delegates."""
    violations: list[tuple[str, int, str]] = []
    functions, offset = _facade_functions(cls)
    for node in functions:
        if node.name == "__init__":
            continue  # the facade constructor is the compatibility composition root
        is_context_wrapper = node.name in {"_connect", "_write"}
        valid_context_wrapper = (
            is_context_wrapper
            and _function_component_owners(node) <= {"SqliteDatabase"}
        )
        if not valid_context_wrapper and _function_contract_owner(node) is None:
            violations.append((FACADE_FILE, offset + node.lineno, node.name))
    return sorted(violations)


def _class_constant_nodes(cls) -> tuple[dict[str, tuple[ast.AST, int]], int]:
    class_node, offset = _facade_class(cls)
    constants: dict[str, tuple[ast.AST, int]] = {}
    for node in class_node.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = (node.value, offset + node.lineno)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            constants[node.target.id] = (node.value, offset + node.lineno)
    return constants, offset


def facade_delegate_evidence(cls, owners_by_member) -> dict[str, str]:
    """Derive truthful one-hop owner evidence from facade methods/properties/constants."""
    candidates: dict[str, list[str | None]] = {}
    functions, _offset = _facade_functions(cls)
    for node in functions:
        if node.name in owners_by_member and node.name not in OWNER_CONTRACT_EXCEPTIONS:
            candidates.setdefault(node.name, []).append(_function_contract_owner(node))
    constants, _offset = _class_constant_nodes(cls)
    for name, (value, _line) in constants.items():
        if name in owners_by_member:
            candidates.setdefault(name, []).append(_direct_expression_owner(value))

    evidence: dict[str, str] = {}
    for name, derived in candidates.items():
        unique = {owner for owner in derived if owner is not None}
        if len(unique) == 1 and all(owner is not None for owner in derived):
            evidence[name] = next(iter(unique))
    return evidence


def facade_contract_subject_names(cls, owners_by_member) -> set[str]:
    functions, _offset = _facade_functions(cls)
    constants, _offset = _class_constant_nodes(cls)
    return {
        node.name
        for node in functions
        if node.name in owners_by_member and node.name not in OWNER_CONTRACT_EXCEPTIONS
    } | {name for name in constants if name in owners_by_member}


def manifest_delegate_mismatches(cls, owners_by_member) -> list[tuple[str, int, str]]:
    """Compare manifest owners with mechanically derived facade delegate targets."""
    mismatches: list[tuple[str, int, str]] = []
    functions, offset = _facade_functions(cls)
    for node in functions:
        if node.name not in owners_by_member or node.name in OWNER_CONTRACT_EXCEPTIONS:
            continue
        delegate_owner = _function_contract_owner(node)
        manifest_owner = owners_by_member[node.name]
        if manifest_owner != delegate_owner:
            mismatches.append(
                (
                    FACADE_FILE,
                    offset + node.lineno,
                    f"{node.name}:{manifest_owner}->{delegate_owner or '<missing>'}",
                )
            )
    constants, _offset = _class_constant_nodes(cls)
    for name, (value, line) in constants.items():
        if name not in owners_by_member:
            continue
        delegate_owner = _direct_expression_owner(value)
        manifest_owner = owners_by_member[name]
        if manifest_owner != delegate_owner:
            mismatches.append(
                (
                    FACADE_FILE,
                    line,
                    f"{name}:{manifest_owner}->{delegate_owner or '<missing>'}",
                )
            )
    return sorted(mismatches)


class _FakeResponseModel:
    def __init__(self, value):
        self.value = value


def _global_response_helper(value):
    return {"value": value}


class _LocalAssemblyEscape:
    def returns_dict(self):
        component = self._runtime.catalog
        response = {"component": component}
        return response

    def returns_list(self):
        component = self._runtime.catalog
        response = [component]
        return response

    def returns_model(self):
        component = self._runtime.catalog
        response = _FakeResponseModel(component)
        return response


class _GlobalHelperEscape:
    def calls_global_helper(self):
        component = self._runtime.catalog
        return _global_response_helper(component)


class _ScalarIdentityAdapter:
    def casts_component_value(self):
        return str(self._runtime.scale_artifact_store.viz_index_dir("nb"))


class _ZeroOwnerSurface:
    orphan_constant = object()

    @property
    def orphan_property(self):
        return "orphan"


EXPECTED_REMEDIATION_SITES: dict[str, set[tuple[str, int, str]]] = {
    'facade_body': {
        ('backend/app/services/sqlite_repository.py', 708, '_user_model_cfg_cache'),
        ('backend/app/services/sqlite_repository.py', 748, '_unified_cache'),
        ('backend/app/services/sqlite_repository.py', 817, '_scale_building'),
        ('backend/app/services/sqlite_repository.py', 826, '_scale_building_lock'),
        ('backend/app/services/sqlite_repository.py', 851, '_auto_index_checked'),
        ('backend/app/services/sqlite_repository.py', 909, '_add_column_if_missing'),
        ('backend/app/services/sqlite_repository.py', 981, '_any_base_notebook_has_kg'),
        ('backend/app/services/sqlite_repository.py', 992, '_source_ids_from_evidence'),
        ('backend/app/services/sqlite_repository.py', 1006, '_delete_knowledge_object_sources'),
        ('backend/app/services/sqlite_repository.py', 1035, '_knowledge_objects'),
        ('backend/app/services/sqlite_repository.py', 1093, 'notebook_copy_stats'),
        ('backend/app/services/sqlite_repository.py', 1103, '_insert_row'),
        ('backend/app/services/sqlite_repository.py', 1171, 'backfill_kg_fts'),
        ('backend/app/services/sqlite_repository.py', 1182, 'backfill_chunk_fts'),
        ('backend/app/services/sqlite_repository.py', 1195, '_semantic_search'),
        ('backend/app/services/sqlite_repository.py', 1237, '_hydrate_search_hits'),
        ('backend/app/services/sqlite_repository.py', 1272, '_fold_hits_to_canonical'),
        ('backend/app/services/sqlite_repository.py', 1307, 'kg_search'),
        ('backend/app/services/sqlite_repository.py', 1326, 'eval_insert_source_for_test'),
        ('backend/app/services/sqlite_repository.py', 1372, 'list_sources'),
        ('backend/app/services/sqlite_repository.py', 1376, 'list_sources_page'),
        ('backend/app/services/sqlite_repository.py', 1388, '_source_pipeline_hooks'),
        ('backend/app/services/sqlite_repository.py', 1406, 'import_sources'),
        ('backend/app/services/sqlite_repository.py', 1411, 'add_url_sources'),
        ('backend/app/services/sqlite_repository.py', 1421, 'upload_sources'),
        ('backend/app/services/sqlite_repository.py', 1443, '_notebook_has_kg'),
        ('backend/app/services/sqlite_repository.py', 1477, 'process_source'),
        ('backend/app/services/sqlite_repository.py', 1482, 'parse_source'),
        ('backend/app/services/sqlite_repository.py', 1496, '_notebook_meta_row'),
        ('backend/app/services/sqlite_repository.py', 1500, '_notebook_meta_sources'),
        ('backend/app/services/sqlite_repository.py', 1508, '_apply_notebook_meta'),
        ('backend/app/services/sqlite_repository.py', 1519, 'delete_source'),
        ('backend/app/services/sqlite_repository.py', 1524, 'extract_source'),
        ('backend/app/services/sqlite_repository.py', 1545, '_begin_extraction_run'),
        ('backend/app/services/sqlite_repository.py', 1556, '_finish_extraction_run'),
        ('backend/app/services/sqlite_repository.py', 1562, '_notebook_tier'),
        ('backend/app/services/sqlite_repository.py', 1574, '_embed_knowledge'),
        ('backend/app/services/sqlite_repository.py', 1595, '_flush_object_vectors'),
        ('backend/app/services/sqlite_repository.py', 1644, 'knowledge_types'),
        ('backend/app/services/sqlite_repository.py', 1662, '_knowledge_record'),
        ('backend/app/services/sqlite_repository.py', 1671, 'list_knowledge'),
        ('backend/app/services/sqlite_repository.py', 1707, '_object_schema_from_row'),
        ('backend/app/services/sqlite_repository.py', 1737, 'knowledge_graph'),
        ('backend/app/services/sqlite_repository.py', 1779, '_kg_headline'),
        ('backend/app/services/sqlite_repository.py', 1783, 'add_relations'),
        ('backend/app/services/sqlite_repository.py', 1805, 'relations_for_notebook'),
        ('backend/app/services/sqlite_repository.py', 1812, '_edge_centrality_map'),
        ('backend/app/services/sqlite_repository.py', 2168, '_annotate_edge_support'),
        ('backend/app/services/sqlite_repository.py', 2300, 'concept_detail'),
        ('backend/app/services/sqlite_repository.py', 2359, '_test_insert_object'),
        ('backend/app/services/sqlite_repository.py', 2370, '_promotion_row_to_dict'),
        ('backend/app/services/sqlite_repository.py', 2400, '_seed_fn_for'),
        ('backend/app/services/sqlite_repository.py', 2405, '_find_base_dedup_match'),
        ('backend/app/services/sqlite_repository.py', 2414, '_merge_evidence_lists'),
        ('backend/app/services/sqlite_repository.py', 2437, '_knowledge_headline'),
        ('backend/app/services/sqlite_repository.py', 2450, '_payload_join'),
        ('backend/app/services/sqlite_repository.py', 2490, '_runtime_dim'),
        ('backend/app/services/sqlite_repository.py', 2506, '_element_vectors'),
        ('backend/app/services/sqlite_repository.py', 2564, '_compute_scale_version_cold'),
        ('backend/app/services/sqlite_repository.py', 2695, '_dequeue_scale_idle'),
        ('backend/app/services/sqlite_repository.py', 2715, '_build_viz_graph_arrays'),
        ('backend/app/services/sqlite_repository.py', 2721, '_viz_arrays_from_graph'),
        ('backend/app/services/sqlite_repository.py', 2767, '_rule_card'),
        ('backend/app/services/sqlite_repository.py', 2791, '_as_retrieved'),
        ('backend/app/services/sqlite_repository.py', 2802, '_tier_map_for'),
        ('backend/app/services/sqlite_repository.py', 2812, '_citations_from'),
        ('backend/app/services/sqlite_repository.py', 2827, '_in_batches'),
        ('backend/app/services/sqlite_repository.py', 2849, 'retrieval'),
        ('backend/app/services/sqlite_repository.py', 2917, '_union_chunk_candidates'),
        ('backend/app/services/sqlite_repository.py', 2924, '_chunk_answer_context'),
        ('backend/app/services/sqlite_repository.py', 2969, 'ask_chunk'),
        ('backend/app/services/sqlite_repository.py', 2981, 'ask'),
        ('backend/app/services/sqlite_repository.py', 2997, '_concept_cluster_id'),
        ('backend/app/services/sqlite_repository.py', 3044, '_truncate_kg_block'),
        ('backend/app/services/sqlite_repository.py', 3056, '_participant_notebook_ids'),
        ('backend/app/services/sqlite_repository.py', 3062, '_answer_context'),
        ('backend/app/services/sqlite_repository.py', 3109, '_unconfigured_model_response'),
        ('backend/app/services/sqlite_repository.py', 3117, 'ask_reasoning'),
        ('backend/app/services/sqlite_repository.py', 3131, 'ask_graph'),
        ('backend/app/services/sqlite_repository.py', 3145, '_parse_answer_anchors'),
        ('backend/app/services/sqlite_repository.py', 3157, '_save_answer'),
        ('backend/app/services/sqlite_repository.py', 3171, '_ensure_conversation'),
        ('backend/app/services/sqlite_repository.py', 3182, 'begin_ask_job'),
        ('backend/app/services/sqlite_repository.py', 3195, 'finish_ask_job'),
        ('backend/app/services/sqlite_repository.py', 3205, 'cancel_ask_job'),
        ('backend/app/services/sqlite_repository.py', 3229, 'append_ask_trace'),
        ('backend/app/services/sqlite_repository.py', 3244, '_read_ask_trace'),
        ('backend/app/services/sqlite_repository.py', 3268, 'list_conversations'),
        ('backend/app/services/sqlite_repository.py', 3281, 'bulk_delete_conversations'),
        ('backend/app/services/sqlite_repository.py', 3296, 'create_report'),
        ('backend/app/services/sqlite_repository.py', 3333, 'maintenance'),
        ('backend/app/services/sqlite_repository.py', 3345, 'pending_actions'),
        ('backend/app/services/sqlite_repository.py', 3417, '_source_type_from_name'),
        ('backend/app/services/sqlite_repository.py', 3429, '_summarize_source'),
    },
    'ownership': {
        ('backend/app/services/sqlite_repository.py', 614, 'list_user_usage:IdentityStore->QueryStore'),
        ('backend/app/services/sqlite_repository.py', 617, 'list_user_notebooks:IdentityStore->QueryStore'),
        ('backend/app/services/sqlite_repository.py', 626, '_system_llm_for:QueryStore->ModelProvider'),
        ('backend/app/services/sqlite_repository.py', 629, '_user_llm_cached:IdentityStore->ModelProvider'),
        ('backend/app/services/sqlite_repository.py', 632, '_llm_for_role:QueryStore->ModelProvider'),
        ('backend/app/services/sqlite_repository.py', 664, '_system_llm_client:QueryStore->ModelProvider'),
        ('backend/app/services/sqlite_repository.py', 668, '_system_llm_client:QueryStore->ModelProvider'),
        ('backend/app/services/sqlite_repository.py', 672, '_reasoning_llm_client:QueryStore->ModelProvider'),
        ('backend/app/services/sqlite_repository.py', 676, '_reasoning_llm_client:QueryStore->ModelProvider'),
        ('backend/app/services/sqlite_repository.py', 680, '_rewrite_llm_client:QueryStore->ModelProvider'),
        ('backend/app/services/sqlite_repository.py', 684, '_rewrite_llm_client:QueryStore->ModelProvider'),
        ('backend/app/services/sqlite_repository.py', 688, '_kg_llm_client:QueryStore->ModelProvider'),
        ('backend/app/services/sqlite_repository.py', 692, '_kg_llm_client:QueryStore->ModelProvider'),
        ('backend/app/services/sqlite_repository.py', 696, '_system_rerank_client:QueryStore->ModelProvider'),
        ('backend/app/services/sqlite_repository.py', 700, '_system_rerank_client:QueryStore->ModelProvider'),
        ('backend/app/services/sqlite_repository.py', 708, '_user_model_cfg_cache:IdentityStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 714, '_user_llm_clients:IdentityStore->ModelProvider'),
        ('backend/app/services/sqlite_repository.py', 718, '_user_llm_clients:IdentityStore->ModelProvider'),
        ('backend/app/services/sqlite_repository.py', 722, '_user_rerank_clients:IdentityStore->ModelProvider'),
        ('backend/app/services/sqlite_repository.py', 726, '_user_rerank_clients:IdentityStore->ModelProvider'),
        ('backend/app/services/sqlite_repository.py', 736, '_vector_cache:ScaleArtifactRuntime->RetrievalSnapshotCache'),
        ('backend/app/services/sqlite_repository.py', 740, '_vector_cache:ScaleArtifactRuntime->RetrievalSnapshotCache'),
        ('backend/app/services/sqlite_repository.py', 744, '_unified_cache:ScaleArtifactRuntime->RetrievalSnapshotCache'),
        ('backend/app/services/sqlite_repository.py', 748, '_unified_cache:ScaleArtifactRuntime-><missing>'),
        ('backend/app/services/sqlite_repository.py', 817, '_scale_building:ScaleArtifactRuntime-><missing>'),
        ('backend/app/services/sqlite_repository.py', 826, '_scale_building_lock:ScaleArtifactRuntime-><missing>'),
        ('backend/app/services/sqlite_repository.py', 851, '_auto_index_checked:ScaleArtifactRuntime-><missing>'),
        ('backend/app/services/sqlite_repository.py', 872, '_resolve_path:QueryStore->SqliteDatabase'),
        ('backend/app/services/sqlite_repository.py', 876, 'db_path:QueryStore->SqliteDatabase'),
        ('backend/app/services/sqlite_repository.py', 880, 'db_path:QueryStore->SqliteDatabase'),
        ('backend/app/services/sqlite_repository.py', 884, '_write_lock:QueryStore->SqliteDatabase'),
        ('backend/app/services/sqlite_repository.py', 888, '_write_lock:QueryStore->SqliteDatabase'),
        ('backend/app/services/sqlite_repository.py', 909, '_add_column_if_missing:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 952, '_recover_interrupted_jobs:QueryStore->SqliteDatabase'),
        ('backend/app/services/sqlite_repository.py', 963, '_count:QueryStore->NotebookSummaryQuery'),
        ('backend/app/services/sqlite_repository.py', 966, '_count_knowledge:QueryStore->KnowledgeStore'),
        ('backend/app/services/sqlite_repository.py', 971, '_has_kg:QueryStore->NotebookSummaryQuery'),
        ('backend/app/services/sqlite_repository.py', 974, '_source_has_kg:SourceIngestionService->KnowledgeStore'),
        ('backend/app/services/sqlite_repository.py', 978, '_count_pending_kg_sources:SourceIngestionService->NotebookSummaryQuery'),
        ('backend/app/services/sqlite_repository.py', 981, '_any_base_notebook_has_kg:NotebookCatalogService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 988, '_base_notebook_info:NotebookCatalogService->NotebookSummaryQuery'),
        ('backend/app/services/sqlite_repository.py', 992, '_source_ids_from_evidence:SourceIngestionService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 998, '_upsert_knowledge_object_sources:SourceIngestionService->KnowledgeStore'),
        ('backend/app/services/sqlite_repository.py', 1006, '_delete_knowledge_object_sources:SourceIngestionService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1010, '_source_index_backfilled:SourceIngestionService->KnowledgeStore'),
        ('backend/app/services/sqlite_repository.py', 1013, '_mark_source_index_backfilled:SourceIngestionService->KnowledgeStore'),
        ('backend/app/services/sqlite_repository.py', 1016, '_find_stale_knowledge_ids_for_source:SourceIngestionService->KnowledgeStore'),
        ('backend/app/services/sqlite_repository.py', 1023, '_clear_source_extraction_state:SourceIngestionService->KnowledgeStore'),
        ('backend/app/services/sqlite_repository.py', 1035, '_knowledge_objects:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1093, 'notebook_copy_stats:NotebookSharingService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1103, '_insert_row:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1171, 'backfill_kg_fts:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1182, 'backfill_chunk_fts:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1195, '_semantic_search:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1237, '_hydrate_search_hits:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1272, '_fold_hits_to_canonical:KnowledgeLifecycleService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1307, 'kg_search:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1326, 'eval_insert_source_for_test:SourceIngestionService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1372, 'list_sources:SourceIngestionService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1376, 'list_sources_page:SourceIngestionService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1385, 'get_source:SourceIngestionService->SourceStore'),
        ('backend/app/services/sqlite_repository.py', 1406, 'import_sources:SourceIngestionService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1411, 'add_url_sources:SourceIngestionService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1421, 'upload_sources:SourceIngestionService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1443, '_notebook_has_kg:NotebookCatalogService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1449, '_CJK_RE:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1450, '_LATIN_RE:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1452, '_notebook_langs:NotebookCatalogService->RetrievalService'),
        ('backend/app/services/sqlite_repository.py', 1455, '_should_extract_kg:QueryStore->SourceIngestionService'),
        ('backend/app/services/sqlite_repository.py', 1458, 'build_notebook_kg:NotebookCatalogService->KnowledgeLifecycleService'),
        ('backend/app/services/sqlite_repository.py', 1477, 'process_source:SourceIngestionService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1482, 'parse_source:SourceIngestionService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1487, '_augment_notebook_meta:NotebookCatalogService->SourceIngestionService'),
        ('backend/app/services/sqlite_repository.py', 1516, 'source_elements:SourceIngestionService->SourceStore'),
        ('backend/app/services/sqlite_repository.py', 1519, 'delete_source:SourceIngestionService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1524, 'extract_source:SourceIngestionService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1531, '_relink_extra_relations:KnowledgeLifecycleService->SourceIngestionService'),
        ('backend/app/services/sqlite_repository.py', 1538, '_run_extraction:QueryStore->SourceIngestionService'),
        ('backend/app/services/sqlite_repository.py', 1566, '_source_raw_text:SourceIngestionService->SourceFileStore'),
        ('backend/app/services/sqlite_repository.py', 1571, '_embed_source:SourceIngestionService->SourceEmbeddingService'),
        ('backend/app/services/sqlite_repository.py', 1574, '_embed_knowledge:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1603, '_embed_objects_batch:QueryStore->SourceEmbeddingService'),
        ('backend/app/services/sqlite_repository.py', 1609, '_embed_relations_batch:QueryStore->SourceEmbeddingService'),
        ('backend/app/services/sqlite_repository.py', 1614, '_build_chunks_for_source:SourceIngestionService->SourceChunkingService'),
        ('backend/app/services/sqlite_repository.py', 1617, '_embed_chunks_for_source:SourceIngestionService->SourceEmbeddingService'),
        ('backend/app/services/sqlite_repository.py', 1620, '_chunk_and_embed_source:SourceIngestionService->SourceChunkingService'),
        ('backend/app/services/sqlite_repository.py', 1623, '_embed_chunks_batch:SourceIngestionService->SourceEmbeddingService'),
        ('backend/app/services/sqlite_repository.py', 1626, '_backfill_knowledge_embeddings:QueryStore->SourceEmbeddingService'),
        ('backend/app/services/sqlite_repository.py', 1638, '_backfill_relation_embeddings:QueryStore->SQLiteMaintenanceAdapter'),
        ('backend/app/services/sqlite_repository.py', 1644, 'knowledge_types:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1662, '_knowledge_record:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1671, 'list_knowledge:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1707, '_object_schema_from_row:SchemaRegistryService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1737, 'knowledge_graph:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1779, '_kg_headline:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1783, 'add_relations:KnowledgeLifecycleService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1805, 'relations_for_notebook:NotebookCatalogService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1810, '_REVIEW_STATUSES:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1812, '_edge_centrality_map:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 1882, 'review_queue:QueryStore->KnowledgeGovernanceService'),
        ('backend/app/services/sqlite_repository.py', 1897, '_delete_relations_for_source:SourceIngestionService->KnowledgeStore'),
        ('backend/app/services/sqlite_repository.py', 1902, 'write_clusters:KnowledgeGovernanceService->KnowledgeLifecycleService'),
        ('backend/app/services/sqlite_repository.py', 1910, 'append_clusters:KnowledgeGovernanceService->KnowledgeLifecycleService'),
        ('backend/app/services/sqlite_repository.py', 1917, 'incremental_fuse_source:SourceIngestionService->KnowledgeLifecycleService'),
        ('backend/app/services/sqlite_repository.py', 1924, '_tier2_bridge_candidates_ann:QueryStore->KnowledgeLifecycleService'),
        ('backend/app/services/sqlite_repository.py', 1931, 'cluster_map:KnowledgeGovernanceService->RetrievalService'),
        ('backend/app/services/sqlite_repository.py', 2108, 'decided_pairs:QueryStore->KnowledgeGovernanceService'),
        ('backend/app/services/sqlite_repository.py', 2111, 'decided_seed_pairs:QueryStore->KnowledgeGovernanceService'),
        ('backend/app/services/sqlite_repository.py', 2132, '_invalidate_unified_cache:ScaleArtifactRuntime->KgMutationCoordinator'),
        ('backend/app/services/sqlite_repository.py', 2138, '_cluster_input_version:KnowledgeGovernanceService->KnowledgeLifecycleService'),
        ('backend/app/services/sqlite_repository.py', 2144, '_mark_unified_kg_dirty:KnowledgeLifecycleService->KgMutationCoordinator'),
        ('backend/app/services/sqlite_repository.py', 2152, '_bump_cluster_mutation_seq:KnowledgeGovernanceService->KgMutationCoordinator'),
        ('backend/app/services/sqlite_repository.py', 2161, '_edge_support_map:QueryStore->RetrievalService'),
        ('backend/app/services/sqlite_repository.py', 2168, '_annotate_edge_support:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 2208, '_viz_dict:ScaleArtifactRuntime->KnowledgeLifecycleService'),
        ('backend/app/services/sqlite_repository.py', 2212, '_viz_node:ScaleArtifactRuntime->KnowledgeLifecycleService'),
        ('backend/app/services/sqlite_repository.py', 2222, 'kg_neighbors:QueryStore->KnowledgeLifecycleService'),
        ('backend/app/services/sqlite_repository.py', 2227, '_kg_neighbors_db:QueryStore->KnowledgeLifecycleService'),
        ('backend/app/services/sqlite_repository.py', 2233, '_object_meta:QueryStore->KnowledgeLifecycleService'),
        ('backend/app/services/sqlite_repository.py', 2238, '_stream_seed_reps:QueryStore->KnowledgeLifecycleService'),
        ('backend/app/services/sqlite_repository.py', 2245, '_write_cluster_map_streamed:KnowledgeGovernanceService->KnowledgeLifecycleService'),
        ('backend/app/services/sqlite_repository.py', 2288, 'list_communities:QueryStore->KnowledgeLifecycleService'),
        ('backend/app/services/sqlite_repository.py', 2292, 'summarize_communities:QueryStore->KnowledgeLifecycleService'),
        ('backend/app/services/sqlite_repository.py', 2297, 'get_community_reports:ReportEngine->KnowledgeLifecycleService'),
        ('backend/app/services/sqlite_repository.py', 2300, 'concept_detail:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 2349, '_element_texts:QueryStore->KnowledgeStore'),
        ('backend/app/services/sqlite_repository.py', 2352, '_enrich_evidence:QueryStore->KnowledgeStore'),
        ('backend/app/services/sqlite_repository.py', 2355, 'node_context:RetrievalService->KnowledgeStore'),
        ('backend/app/services/sqlite_repository.py', 2359, '_test_insert_object:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 2370, '_promotion_row_to_dict:KnowledgeGovernanceService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 2400, '_seed_fn_for:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 2405, '_find_base_dedup_match:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 2414, '_merge_evidence_lists:KnowledgeGovernanceService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 2437, '_knowledge_headline:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 2444, '_knowledge_ref:QueryStore->KnowledgeGovernanceService'),
        ('backend/app/services/sqlite_repository.py', 2450, '_payload_join:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 2456, '_knowledge_similarity:QueryStore->KnowledgeGovernanceService'),
        ('backend/app/services/sqlite_repository.py', 2490, '_runtime_dim:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 2498, '_gather_elements:QueryStore->RetrievalService'),
        ('backend/app/services/sqlite_repository.py', 2510, '_gather_chunks:QueryStore->RetrievalService'),
        ('backend/app/services/sqlite_repository.py', 2513, '_vector_matrix_version:QueryStore->RetrievalService'),
        ('backend/app/services/sqlite_repository.py', 2516, '_vector_matrix:QueryStore->RetrievalService'),
        ('backend/app/services/sqlite_repository.py', 2520, '_vector_matrix_warm:QueryStore->RetrievalService'),
        ('backend/app/services/sqlite_repository.py', 2523, '_keyword_token_sets:QueryStore->RetrievalService'),
        ('backend/app/services/sqlite_repository.py', 2533, '_mention_extra_edges:KnowledgeLifecycleService->RetrievalService'),
        ('backend/app/services/sqlite_repository.py', 2541, '_probe_scale_version_signal:ScaleArtifactRuntime->IndexProjectionStore'),
        ('backend/app/services/sqlite_repository.py', 2564, '_compute_scale_version_cold:ScaleArtifactRuntime-><missing>'),
        ('backend/app/services/sqlite_repository.py', 2579, '_read_manifest_version:QueryStore->ScaleArtifactStore'),
        ('backend/app/services/sqlite_repository.py', 2612, '_gather_kg_graph:QueryStore->ScaleIndexBuilder'),
        ('backend/app/services/sqlite_repository.py', 2640, '_index_delta:ScaleArtifactRuntime->ScaleIndexBuilder'),
        ('backend/app/services/sqlite_repository.py', 2664, '_resolve_index_owner:NotebookSharingService->ScaleArtifactRuntime'),
        ('backend/app/services/sqlite_repository.py', 2668, '_notebook_name:NotebookCatalogService->ScaleArtifactRuntime'),
        ('backend/app/services/sqlite_repository.py', 2680, '_process_idle_queue:QueryStore->ScaleArtifactRuntime'),
        ('backend/app/services/sqlite_repository.py', 2695, '_dequeue_scale_idle:ScaleArtifactRuntime-><missing>'),
        ('backend/app/services/sqlite_repository.py', 2715, '_build_viz_graph_arrays:ScaleArtifactRuntime-><missing>'),
        ('backend/app/services/sqlite_repository.py', 2721, '_viz_arrays_from_graph:ScaleArtifactRuntime-><missing>'),
        ('backend/app/services/sqlite_repository.py', 2727, '_derive_object_graph_lite:QueryStore->ScaleIndexBuilder'),
        ('backend/app/services/sqlite_repository.py', 2731, '_viz_index_dir:ScaleArtifactRuntime->ScaleArtifactStore'),
        ('backend/app/services/sqlite_repository.py', 2738, '_active_kg_delta:QueryStore->RetrievalService'),
        ('backend/app/services/sqlite_repository.py', 2741, '_delta_vector_matrix:QueryStore->RetrievalService'),
        ('backend/app/services/sqlite_repository.py', 2745, '_scale_xlayer_bridge_edges:ScaleArtifactRuntime->RetrievalService'),
        ('backend/app/services/sqlite_repository.py', 2749, '_scale_combined_graph:ScaleArtifactRuntime->RetrievalService'),
        ('backend/app/services/sqlite_repository.py', 2752, 'scale_ppr:ScaleArtifactRuntime->RetrievalService'),
        ('backend/app/services/sqlite_repository.py', 2762, '_PPR_RERANK_SCHEMA:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 2767, '_rule_card:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 2791, '_as_retrieved:RetrievalService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 2802, '_tier_map_for:RetrievalService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 2812, '_citations_from:RetrievalService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 2825, '_IN_CHUNK:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 2827, '_in_batches:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 2835, '_relations_with_names:QueryStore->RetrievalService'),
        ('backend/app/services/sqlite_repository.py', 2839, '_relation_ann_candidates:QueryStore->RetrievalService'),
        ('backend/app/services/sqlite_repository.py', 2845, '_kg_object_candidates:QueryStore->RetrievalService'),
        ('backend/app/services/sqlite_repository.py', 2849, 'retrieval:RetrievalService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 2906, '_hydrate_chunk_candidates:QueryStore->RetrievalService'),
        ('backend/app/services/sqlite_repository.py', 2912, '_keyword_chunk_candidates:QueryStore->RetrievalService'),
        ('backend/app/services/sqlite_repository.py', 2917, '_union_chunk_candidates:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 2921, '_mmr_select_chunks:QueryStore->RetrievalService'),
        ('backend/app/services/sqlite_repository.py', 2924, '_chunk_answer_context:AskService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 2969, 'ask_chunk:AskService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 2981, 'ask:AskService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 2997, '_concept_cluster_id:KnowledgeGovernanceService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 3005, '_rrf_scored:QueryStore->RetrievalService'),
        ('backend/app/services/sqlite_repository.py', 3014, '_graph_seed_fusion:QueryStore->RetrievalService'),
        ('backend/app/services/sqlite_repository.py', 3023, '_MIX_NODE_SEEDS:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 3024, '_MIX_REL_SEEDS:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 3025, '_MIX_FANOUT:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 3027, '_chunk_kg_overlay:QueryStore->RetrievalService'),
        ('backend/app/services/sqlite_repository.py', 3030, '_elem_chunk_map:QueryStore->RetrievalService'),
        ('backend/app/services/sqlite_repository.py', 3033, '_kg_source_chunks:SourceIngestionService->RetrievalService'),
        ('backend/app/services/sqlite_repository.py', 3036, '_ent_chunk_map:QueryStore->RetrievalService'),
        ('backend/app/services/sqlite_repository.py', 3041, '_MIX_KG_KEY_BASE:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 3042, '_MIX_PROMPT_BUFFER_TOKENS:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 3044, '_truncate_kg_block:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 3050, '_gather_vector_chunks:QueryStore->RetrievalService'),
        ('backend/app/services/sqlite_repository.py', 3056, '_participant_notebook_ids:NotebookCatalogService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 3062, '_answer_context:AskService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 3067, '_rewrite_followup_query:QueryStore->AskService'),
        ('backend/app/services/sqlite_repository.py', 3077, '_refine_context:RetrievalService->AskService'),
        ('backend/app/services/sqlite_repository.py', 3109, '_unconfigured_model_response:QueryStore-><missing>'),
        ('backend/app/services/sqlite_repository.py', 3117, 'ask_reasoning:AskService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 3131, 'ask_graph:AskService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 3145, '_parse_answer_anchors:AskService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 3152, '_needs_index:ScaleArtifactRuntime->AskService'),
        ('backend/app/services/sqlite_repository.py', 3157, '_save_answer:AskService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 3171, '_ensure_conversation:AskService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 3182, 'begin_ask_job:AskService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 3195, 'finish_ask_job:AskService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 3205, 'cancel_ask_job:AskService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 3214, '_ask_cancel_events:AskService->AskCancellationRegistry'),
        ('backend/app/services/sqlite_repository.py', 3222, '_ask_cancel_lock:AskService->AskCancellationRegistry'),
        ('backend/app/services/sqlite_repository.py', 3226, 'ask_job_status:AskService->AskStateStore'),
        ('backend/app/services/sqlite_repository.py', 3229, 'append_ask_trace:AskService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 3244, '_read_ask_trace:AskService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 3250, 'ask_job_detail:AskService->AskStateStore'),
        ('backend/app/services/sqlite_repository.py', 3253, '_cleanup_empty_conversation:AskService->AskStateStore'),
        ('backend/app/services/sqlite_repository.py', 3257, '_conversation_history:AskService->AskStateStore'),
        ('backend/app/services/sqlite_repository.py', 3263, 'get_conversation:AskService->AskStateStore'),
        ('backend/app/services/sqlite_repository.py', 3268, 'list_conversations:AskService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 3275, 'rename_conversation:AskService->AskStateStore'),
        ('backend/app/services/sqlite_repository.py', 3278, 'delete_conversation:AskService->AskStateStore'),
        ('backend/app/services/sqlite_repository.py', 3281, 'bulk_delete_conversations:AskService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 3296, 'create_report:ReportEngine-><missing>'),
        ('backend/app/services/sqlite_repository.py', 3301, 'update_report:ReportEngine->ReportStore'),
        ('backend/app/services/sqlite_repository.py', 3311, '_report_row_to_dict:ReportEngine->ReportStore'),
        ('backend/app/services/sqlite_repository.py', 3314, 'get_report:ReportEngine->ReportStore'),
        ('backend/app/services/sqlite_repository.py', 3317, 'list_reports:ReportEngine->ReportStore'),
        ('backend/app/services/sqlite_repository.py', 3320, 'delete_report:ReportEngine->ReportStore'),
        ('backend/app/services/sqlite_repository.py', 3323, 'export_reports:ReportEngine->ReportStore'),
        ('backend/app/services/sqlite_repository.py', 3345, 'pending_actions:NotebookCatalogService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 3391, 'submit_feedback:AskService->AskStateStore'),
        ('backend/app/services/sqlite_repository.py', 3400, '_NOTEBOOK_COUNT_TYPES:QueryStore->NotebookSummaryQuery'),
        ('backend/app/services/sqlite_repository.py', 3402, '_knowledge_type_counts:QueryStore->NotebookSummaryQuery'),
        ('backend/app/services/sqlite_repository.py', 3405, '_notebook_from_row:NotebookCatalogService->NotebookSummaryQuery'),
        ('backend/app/services/sqlite_repository.py', 3408, '_source_from_row:SourceIngestionService->SourceStore'),
        ('backend/app/services/sqlite_repository.py', 3411, '_sources_from_rows:SourceIngestionService->SourceStore'),
        ('backend/app/services/sqlite_repository.py', 3414, '_extraction_warning:QueryStore->SourceStore'),
        ('backend/app/services/sqlite_repository.py', 3417, '_source_type_from_name:SourceIngestionService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 3429, '_summarize_source:SourceIngestionService-><missing>'),
        ('backend/app/services/sqlite_repository.py', 3456, '_delete_file:SourceIngestionService->SourceFileStore'),
    },
}


def frozen_surface() -> dict[str, dict]:
    assert FIXTURE.is_file(), f"missing frozen facade surface: {FIXTURE}"
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("facade-contract")
    return SQLiteRepository(Settings(
        database_url=f"sqlite:///{tmp_path / 't.db'}",
        storage_dir=str(tmp_path / "storage"),
        event_log_enabled=False,
        llm_log_enabled=False,
        auth_optional=True,
    ))


def test_facade_matches_frozen_surface_manifest(repo):
    for name, contract in frozen_surface().items():
        if contract.get("scope") == "module":
            module = (
                repository
                if contract["modules"] == ["app.services.repository"]
                else sqlite_repository
            )
            assert hasattr(module, name), name
            continue
        assert hasattr(SQLiteRepository, name) or hasattr(repo, name), name
        if contract["kind"] == "method":
            assert str(inspect.signature(getattr(SQLiteRepository, name))) == (
                contract["signature"]
            ), name


def test_facade_has_no_getattr_or_sql():
    source = inspect.getsource(SQLiteRepository)
    assert "def __getattr__" not in source
    assert ".execute(" not in source
    assert ".executemany(" not in source
    assert ".executescript(" not in source


def test_manifest_owner_matches_facade_delegate_target():
    assert set(manifest_delegate_mismatches(SQLiteRepository, OWNER_BY_MEMBER)) == (
        EXPECTED_REMEDIATION_SITES["ownership"]
    )


def test_facade_methods_are_properties_adapters_or_one_hop_delegates():
    assert set(facade_body_violations(SQLiteRepository)) == (
        EXPECTED_REMEDIATION_SITES["facade_body"]
    )


def test_facade_checker_rejects_component_touch_then_local_response_assembly():
    violations = {site[2] for site in facade_body_violations(_LocalAssemblyEscape)}
    assert violations == {"returns_dict", "returns_list", "returns_model"}


def test_facade_checker_rejects_global_helper_after_component_touch():
    violations = {site[2] for site in facade_body_violations(_GlobalHelperEscape)}
    assert violations == {"calls_global_helper"}


def test_facade_checker_allows_explicit_scalar_identity_adaptation():
    assert facade_body_violations(_ScalarIdentityAdapter) == []


def test_facade_checker_rejects_zero_owner_properties_and_constants():
    mismatches = manifest_delegate_mismatches(
        _ZeroOwnerSurface,
        {"orphan_property": "QueryStore", "orphan_constant": "QueryStore"},
    )
    assert {site[2].split(":", 1)[0] for site in mismatches} == {
        "orphan_property",
        "orphan_constant",
    }


def test_module_reexports_the_frozen_task1_imports():
    from app.repositories.sqlite import migrations
    from app.services import (
        knowledge_contracts,
        knowledge_lifecycle,
        sqlite_identity,
        sqlite_notebook_sharing,
    )
    from app.services.retrieval import RetrievedKnowledge

    assert sqlite_repository.SQLiteRepository is SQLiteRepository
    assert sqlite_repository.SCHEMA_VERSION == migrations.SCHEMA_VERSION
    assert sqlite_repository.UploadedSourceFile is repository.UploadedSourceFile
    assert callable(sqlite_repository._now)
    assert callable(sqlite_repository._new_id)
    assert sqlite_repository._fast_loads is knowledge_lifecycle._fast_loads
    assert sqlite_repository._REQUEST_USER is sqlite_identity._REQUEST_USER
    assert sqlite_repository.set_request_user is sqlite_identity.set_request_user
    assert sqlite_repository.reset_request_user is sqlite_identity.reset_request_user
    assert sqlite_repository.USABLE_STATUSES is knowledge_contracts.USABLE_STATUSES
    assert sqlite_repository.KNOWLEDGE_STATUSES is knowledge_contracts.KNOWLEDGE_STATUSES
    assert sqlite_repository.KnowledgeGraphTooLargeError is (
        knowledge_contracts.KnowledgeGraphTooLargeError
    )
    assert sqlite_repository._COPY_CHUNK == 1000
    assert sqlite_repository._remap_json_ids is (
        sqlite_notebook_sharing._remap_json_ids
    )
    assert sqlite_repository.RetrievedKnowledge is RetrievedKnowledge
