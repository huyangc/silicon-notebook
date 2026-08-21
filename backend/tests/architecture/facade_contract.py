"""Semantic helpers for validating the backend-neutral RepositoryFacade."""
from __future__ import annotations

import ast
import inspect
from pathlib import Path



ROOT = Path(__file__).resolve().parents[2]
FIXTURE = (
    ROOT
    / "backend"
    / "tests"
    / "fixtures"
    / "repository_contract"
    / "facade_surface.json"
)
FACADE_FILE = "backend/app/services/repository_facade.py"
SQLITE_WRAPPER_FILE = "backend/app/services/sqlite_repository.py"

RUNTIME_COMPONENT_OWNERS = {
    "ask": "AskService",
    "ask_service": "AskService",
    "ask_cancellations": "AskCancellationRegistry",
    "ask_execution": "AskExecutionCoordinator",
    "ask_state": "AskStateStore",
    "candidate_retrieval": "CandidateRetrievalService",
    "catalog": "NotebookCatalogService",
    "chunk_store": "ChunkStore",
    "collection_catalog": "CollectionCatalogService",
    "collection_enumeration": "CollectionEnumerationService",
    "database": "SqliteDatabase",
    "embedding_store": "EmbeddingStore",
    "evidence_context": "EvidenceContextService",
    "governance": "GovernanceStore",
    "graph_retrieval": "GraphRetrievalService",
    "identity": "IdentityStore",
    "index_projections": "IndexProjectionStore",
    "kg_analysis": "KgAnalysisService",
    "kg_mutations": "KgMutationCoordinator",
    "knowhow_history_store": "KnowhowHistoryStore",
    "knowhow_store": "KnowhowStore",
    "knowledge": "KnowledgeStore",
    "knowledge_governance": "KnowledgeGovernanceService",
    "knowledge_lifecycle": "KnowledgeLifecycleService",
    "knowledge_query": "KnowledgeQueryService",
    "models": "ModelProvider",
    "memory_service": "MemoryService",
    "set_unified_cache": "RetrievalSnapshotCache",
    "set_auto_index_checked": "ScaleArtifactRuntime",
    "notebook_store": "NotebookStore",
    "notebook_copies": "NotebookCopyService",
    "notebook_summaries": "NotebookSummaryQuery",
    "pending_actions_service": "PendingActionsService",
    "queries": "QueryStore",
    "report_application": "ReportApplicationService",
    "report_execution": "ReportEngine",
    "report_store": "ReportStore",
    "retrieval": "RetrievalService",
    "retrieval_component": "RetrievalService",
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
    "ask_component": "AskService",
    "evidence_context_component": "EvidenceContextService",
}
CLASS_COMPONENT_OWNERS = {
    "AskStateStore": "AskStateStore",
    "CandidateRetrievalService": "CandidateRetrievalService",
    "GovernanceStore": "GovernanceStore",
    "KnowledgeGovernanceService": "KnowledgeGovernanceService",
    "KnowledgeQueryService": "KnowledgeQueryService",
    "KnowledgeStore": "KnowledgeStore",
    "NotebookSummaryQuery": "NotebookSummaryQuery",
    "RetrievalService": "RetrievalService",
    "SchemaRegistryService": "SchemaRegistryService",
    "SharingStore": "SharingStore",
    "SqliteMigrator": "SqliteDatabase",
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
    "reset_request_user",
    "set_request_user",
}
NON_CALLABLE_INSTANCE_SURFACE = {
    "_kg_building",
    "_kg_building_lock",
    "event_log",
    "mineru_client",
    "mineru_cloud_client",
    "root_dir",
    "settings",
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


def _contains_nested_call(node: ast.AST, *, root_call: ast.Call | None = None) -> bool:
    return any(
        isinstance(child, ast.Call) and child is not root_call
        for child in ast.walk(node)
    )


def _is_call_free_value(node: ast.AST | None) -> bool:
    if isinstance(node, (ast.Name, ast.Constant)):
        return True
    if isinstance(node, ast.Attribute):
        return _is_call_free_value(node.value)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_is_call_free_value(item) for item in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            (key is None or _is_call_free_value(key)) and _is_call_free_value(value)
            for key, value in zip(node.keys, node.values)
        )
    return False


def _direct_expression_owner(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Call):
        if (
            _dotted(node.func) in SCALAR_IDENTITY_ADAPTERS
            and len(node.args) == 1
            and not node.keywords
        ):
            return _direct_expression_owner(node.args[0])
        owner = _component_owner(node.func)
        if (
            owner is None
            or _contains_nested_call(node, root_call=node)
            or not all(_is_call_free_value(arg) for arg in node.args)
            or not all(_is_call_free_value(keyword.value) for keyword in node.keywords)
        ):
            return None
        return owner
    if isinstance(node, ast.Attribute):
        if _contains_nested_call(node):
            return None
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
        if not _is_call_free_value(statement.value):
            return None
        return _component_owner(statement.targets[0])
    if isinstance(statement, ast.AnnAssign):
        if not _is_call_free_value(statement.value):
            return None
        return _component_owner(statement.target)
    return None


def _connect_wrapper_is_exact(node: ast.FunctionDef) -> bool:
    if _decorator_names(node):
        return False
    body = _body_without_docstring(node)
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        return False
    call = body[0].value
    return (
        isinstance(call, ast.Call)
        and _dotted(call.func) == "self._runtime.database.connect"
        and not call.args
        and not call.keywords
        and not _contains_nested_call(call, root_call=call)
    )


def _write_wrapper_is_exact(node: ast.FunctionDef) -> bool:
    if _decorator_names(node) != {"contextmanager"}:
        return False
    body = _body_without_docstring(node)
    if len(body) != 1 or not isinstance(body[0], ast.With):
        return False
    statement = body[0]
    if len(statement.items) != 1:
        return False
    item = statement.items[0]
    call = item.context_expr
    if (
        not isinstance(call, ast.Call)
        or _dotted(call.func) != "self._runtime.database.write"
        or call.args
        or call.keywords
        or _contains_nested_call(call, root_call=call)
        or not isinstance(item.optional_vars, ast.Name)
    ):
        return False
    variable = item.optional_vars.id
    if len(statement.body) != 1:
        return False
    child = statement.body[0]
    return (
        isinstance(child, ast.Expr)
        and isinstance(child.value, ast.Yield)
        and isinstance(child.value.value, ast.Name)
        and child.value.value.id == variable
    )


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
        valid_context_wrapper = (
            node.name == "_connect" and _connect_wrapper_is_exact(node)
        ) or (node.name == "_write" and _write_wrapper_is_exact(node))
        if not valid_context_wrapper and _function_contract_owner(node) is None:
            violations.append((FACADE_FILE, offset + node.lineno, node.name))
    return sorted(violations)


def _class_constant_nodes(cls) -> tuple[dict[str, ast.AST], int]:
    class_node, offset = _facade_class(cls)
    constants: dict[str, ast.AST] = {}
    for node in class_node.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            constants[node.target.id] = node.value
    return constants, offset


def facade_delegate_evidence(cls, owners_by_member) -> dict[str, str]:
    """Derive truthful one-hop owner evidence from facade methods/properties/constants."""
    candidates: dict[str, list[str | None]] = {}
    functions, _offset = _facade_functions(cls)
    for node in functions:
        if node.name in owners_by_member and node.name not in OWNER_CONTRACT_EXCEPTIONS:
            candidates.setdefault(node.name, []).append(_function_contract_owner(node))
    constants, _offset = _class_constant_nodes(cls)
    for name, value in constants.items():
        if name in owners_by_member:
            owner = _direct_expression_owner(value)
            if owner is None and _is_call_free_value(value):
                owner = owners_by_member[name]
            candidates.setdefault(name, []).append(owner)

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
    for name, value in constants.items():
        if name not in owners_by_member:
            continue
        delegate_owner = _direct_expression_owner(value)
        manifest_owner = owners_by_member[name]
        if delegate_owner is None and _is_call_free_value(value):
            delegate_owner = manifest_owner
        if manifest_owner != delegate_owner:
            mismatches.append(
                (
                    FACADE_FILE,
                    _offset + value.lineno,
                    f"{name}:{manifest_owner}->{delegate_owner or '<missing>'}",
                )
            )
    return sorted(mismatches)
