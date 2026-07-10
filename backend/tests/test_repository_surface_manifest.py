from __future__ import annotations

import ast
from collections import defaultdict
import importlib.util
import inspect
import json
from pathlib import Path
import re
import typing

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
GENERATOR = ROOT / "scripts" / "generate_repository_contract_fixtures.py"

REQUIRED_GENERATOR_CALLABLES = {
    "collect_facade_surface",
    "generate_v9_fixture",
    "normalized_repository_snapshot",
    "generate_ask_goldens",
    "generate_api_contract",
    "main",
}
REQUIRED_MEMBER_FIELDS = {
    "kind",
    "signature",
    "consumers",
    "owner",
    "patch_targets",
}

COMPATIBILITY_EXPORTS = {
    "KnowledgeGraphTooLargeError": "app.services.sqlite_repository",
    "NotebookRepository": "app.services.repository",
    "RetrievedKnowledge": "app.services.sqlite_repository",
    "SCHEMA_VERSION": "app.services.sqlite_repository",
    "SQLiteRepository": "app.services.sqlite_repository",
    "USABLE_STATUSES": "app.services.sqlite_repository",
    "UploadedSourceFile": "app.services.repository",
    "_ASK_MODEL_ERRORS": "app.services.sqlite_repository",
    "_COPY_CHUNK": "app.services.sqlite_repository",
    "_REQUEST_USER": "app.services.sqlite_repository",
    "_concept_desc_sig": "app.services.sqlite_repository",
    "_fast_loads": "app.services.sqlite_repository",
    "_new_id": "app.services.sqlite_repository",
    "_now": "app.services.sqlite_repository",
    "_remap_json_ids": "app.services.sqlite_repository",
    "KNOWLEDGE_STATUSES": "app.services.sqlite_repository",
    "parse_source_file": "app.services.sqlite_repository",
    "reset_request_user": "app.services.sqlite_repository",
    "set_request_user": "app.services.sqlite_repository",
}

EXPLICIT_OWNERS = {
    "KnowledgeGraphTooLargeError": "KnowledgeLifecycleService",
    "NotebookRepository": "RepositoryPorts",
    "UploadedSourceFile": "RepositoryPorts",
    "_REQUEST_USER": "RequestContext",
    "reset_request_user": "RequestContext",
    "set_request_user": "RequestContext",
    "approve_promotion": "KnowledgeGovernanceService",
    "list_promotion_queue": "KnowledgeGovernanceService",
    "propose_promotion": "KnowledgeGovernanceService",
    "reject_promotion": "KnowledgeGovernanceService",
}

# Task 2 moves these imports to the typed ports while retaining the old
# compatibility modules elsewhere.  Only these exact import sites are allowed
# to differ from the frozen master consumer manifest.
TASK2_ALLOWED_IMPORTS = {
    ("backend/app/api/deps.py", 11, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/app/api/deps.py", 11, "app.services.sqlite_repository", "set_request_user"),
    ("backend/app/api/deps.py", 11, "app.services.sqlite_repository", "reset_request_user"),
    ("backend/app/eval/speed.py", 98, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/tests/test_trackA_eval_connect.py", 19, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/tests/test_trackA_eval_connect.py", 25, "app.services.repository", "NotebookRepository"),
    ("backend/tests/test_repository_ports.py", 5, "app.services.sqlite_repository", "SQLiteRepository"),
}
TASK4_ALLOWED_IMPORTS = {
    ("backend/app/api/deps.py", 12, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/app/services/sqlite_repository.py", 112, "app.services.repository", "UploadedSourceFile"),
    ("backend/tests/test_sqlite_database_component.py", 6, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/app/services/sqlite_repository.py", 113, "app.services.repository", "UploadedSourceFile"),
}
TASK7_ALLOWED_IMPORTS = {
    ("backend/app/api/routes.py", 19, "app.services.sqlite_repository", "KnowledgeGraphTooLargeError"),
    ("backend/app/api/routes.py", 21, "app.services.sqlite_repository", "KnowledgeGraphTooLargeError"),
    ("backend/app/api/routes.py", 91, "app.services.repository", "NotebookRepository"),
    ("backend/app/api/routes.py", 91, "app.services.repository", "UploadedSourceFile"),
    ("backend/app/api/routes.py", 93, "app.services.repository", "NotebookRepository"),
    ("backend/app/api/routes.py", 93, "app.services.repository", "UploadedSourceFile"),
    ("backend/tests/test_architecture_module_boundaries.py", 4, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/tests/test_architecture_module_boundaries.py", 6, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/tests/test_identity_store_component.py", 9, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/tests/test_query_store_component.py", 9, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/tests/test_model_provider_runtime.py", 8, "app.services.sqlite_repository", "SQLiteRepository"),
}
TASK4_ALLOWED_MEMBER_FILES = {
    ("backend/app/api/deps.py", name)
    for name in {"set_request_user", "reset_request_user", "user_can_access_notebook", "user_can_read_notebook"}
} | {
    ("backend/tests/test_repository_context.py", name)
    for name in {"_REQUEST_USER", "set_request_user", "reset_request_user"}
} | {
    ("backend/tests/test_repository_runtime.py", name)
    for name in {"SQLiteRepository", "settings", "_now", "_runtime"}
} | {
    ("backend/app/services/background_jobs.py", "_REQUEST_USER"),
    ("backend/app/services/sqlite_repository.py", "_runtime"),
}

TASK2_ALLOWED_CONSUMERS = {
    ("upload_sources", "backend/app/eval/speed.py:80"),
    ("parse_source", "backend/app/eval/speed.py:82"),
    ("delete_notebook", "backend/app/eval/speed.py:85"),
    ("delete_notebook", "backend/app/eval/speed.py:90"),
    ("extract_source", "backend/app/eval/speed.py:114"),
    ("user_can_access_notebook", "backend/app/api/deps.py:58"),
    ("user_can_access_notebook", "backend/app/api/deps.py:70"),
    ("user_can_read_notebook", "backend/app/api/deps.py:70"),
    ("user_can_read_notebook", "backend/app/api/deps.py:82"),
    ("NotebookRepository", "backend/app/api/deps.py:10"),
    ("_run_extraction", "backend/app/eval/speed.py:109"),
}
TASK7_ALLOWED_CONSUMERS = {
    ("create_user", "backend/app/api/auth_routes.py:17"),
    ("create_session", "backend/app/api/auth_routes.py:21"),
    ("authenticate_user", "backend/app/api/auth_routes.py:27"),
    ("create_session", "backend/app/api/auth_routes.py:30"),
    ("delete_session", "backend/app/api/auth_routes.py:41"),
    ("list_user_usage", "backend/app/api/routes.py:1378"),
    ("list_user_notebooks", "backend/app/api/routes.py:1388"),
}
TASK2_ALLOWED_MEMBER_FILES = {
    ("backend/app/api/deps.py", name)
    for name in {
        "resolve_session", "current_user", "set_request_user", "reset_request_user",
        "user_can_access_notebook", "user_can_read_notebook", "llm_client", "SQLiteRepository",
    }
} | {
    ("backend/app/eval/speed.py", name)
    for name in {"upload_sources", "parse_source", "extract_source", "delete_notebook", "_run_extraction", "create_notebook", "llm_client", "SQLiteRepository"}
} | {
    ("backend/app/services/repository.py", name)
    for name in {"UploadedSourceFile", "NotebookRepository"}
}
TASK3_ALLOWED_NEW_MEMBERS = {"load_notebook_scale_facts"}
# master v10(rebuild 可续跑轨道, schema 9→10)在冻结之后新增的 facade 成员:
# kg_rebuild_checkpoint 迁移 + 4 个运行时 ckpt helper + 节点向量增量 flush。
# 均为上游合法演进、非本重构产物;Gate 5(KG 域)搬迁时随 rebuild_unified_kg 移动。
MASTER_V10_ALLOWED_NEW_MEMBERS = {
    "_migration_10",
    "_rebuild_ckpt_gc",
    "_rebuild_ckpt_clear",
    "_rebuild_ckpt_load",
    "_rebuild_ckpt_put",
    "_flush_object_vectors",
}
TASK7_ALLOWED_NEW_MEMBERS = {"pending_actions_projection_rows"}
TASK4_ALLOWED_PATCHES = {
    ("backend/tests/test_repository_runtime.py", 19, "_now", "sqlite_repository"),
}
TASK5_ALLOWED_PATCHES = {
    ("backend/tests/test_sqlite_database_component.py", 0, "_write", "repo"),
}
# master v10 新成员上的测试探针(成员本身经 MASTER_V10_ALLOWED_NEW_MEMBERS 豁免,
# fixture 冻结成员集不扩)。Gate 5 搬迁时这些 patch 座随成员迁到组件 seam。
MASTER_V10_ALLOWED_PATCHES = {
    ("backend/tests/test_node_embed_incremental.py", 56, "_flush_object_vectors", "repo"),
    ("backend/tests/test_node_embed_incremental.py", 63, "_flush_object_vectors", "repo"),
    ("backend/tests/test_rebuild_checkpoint.py", 284, "_rebuild_ckpt_put", "repo"),
    ("backend/tests/test_rebuild_checkpoint.py", 312, "_rebuild_ckpt_put", "repo"),
}
TASK5_ALLOWED_MEMBER_FILES = {
    ("backend/app/services/sqlite_repository.py", name)
    for name in {"db_path", "_write_lock", "_connect", "_write"}
} | {
    ("backend/tests/test_sqlite_database_component.py", name)
    for name in {"SQLiteRepository", "_write_lock", "_runtime", "db_path"}
}
TASK6_ALLOWED_MEMBER_FILES = {
    ("backend/app/services/sqlite_repository.py", name)
    for name in {
        "_migrate", "_migrate_legacy", "_add_column_if_missing",
        "_migration_1", "_migration_2", "_migration_3", "_migration_4",
        "_migration_5", "_migration_6", "_migration_7", "_migration_8",
        "_migration_9", "_recover_interrupted_jobs", "_recover_interrupted_jobs_legacy",
        "_seed", "_seed_legacy", "_migrator",
    }
}
TASK7_ALLOWED_MEMBER_FILES = {
    ("backend/app/services/model_provider.py", name)
    for name in {
        "settings", "current_user", "resolve_model_config", "llm_client",
        "reasoning_llm_client", "rewrite_llm_client", "kg_llm_client",
        "rerank_client", "_system_llm_for", "_user_llm_cached",
        "_llm_for_role", "_system_llm_client", "_user_llm_clients",
        "_reasoning_llm_client", "_rewrite_llm_client", "_kg_llm_client",
        "_system_rerank_client", "_user_rerank_clients",
    }
} | {
    ("backend/app/services/sqlite_identity.py", name)
    for name in {
        "current_user", "get_user_model_settings", "set_user_model_settings",
        "_user_profile",
        "resolve_model_config", "create_user", "authenticate_user",
        "create_session", "resolve_session", "delete_session",
        "list_user_usage", "list_user_notebooks", "_user_profile", "_connect",
        "_write", "settings", "_user_model_cfg_cache",
    }
} | {
    ("backend/app/api/deps.py", "_runtime"),
    ("backend/tests/test_model_provider_runtime.py", "_ASK_MODEL_ERRORS"),
} | {
    ("backend/app/services/sqlite_repository.py", name)
    for name in {
        "current_user", "_user_profile", "get_user_model_settings", "set_user_model_settings",
        "resolve_model_config", "create_user", "authenticate_user",
        "create_session", "resolve_session", "delete_session",
        "list_user_usage", "list_user_notebooks", "notebook_analytics",
        "search_notebook", "load_notebook_scale_facts", "_note_model_error",
        "_system_llm_for", "_user_llm_cached", "_llm_for_role",
        "llm_client", "reasoning_llm_client", "rewrite_llm_client",
        "kg_llm_client", "rerank_client", "_system_llm_client",
        "_reasoning_llm_client", "_rewrite_llm_client", "_kg_llm_client",
        "_system_rerank_client", "_user_llm_clients", "_user_rerank_clients",
    }
} | {
    ("backend/app/services/repository_runtime.py", name)
    for name in {"settings", "_runtime"}
} | {
    ("backend/tests/test_identity_store_component.py", name)
    for name in {"SQLiteRepository", "current_user", "_runtime"}
} | {
    ("backend/tests/test_query_store_component.py", name)
    for name in {
        "SQLiteRepository", "create_notebook", "search_notebook",
        "load_notebook_scale_facts", "_runtime",
    }
} | {
    ("backend/tests/test_model_provider_runtime.py", name)
    for name in {
        "SQLiteRepository", "llm_client", "rerank_client",
        "_note_model_error", "_runtime", "event_log",
    }
} | {
    ("backend/tests/test_sources_pagination.py", name)
    for name in {
        "create_notebook", "_write", "search_notebook",
    }
}

TASK7_COMPAT_PROPERTIES = {
    "_system_llm_client": True,
    "_reasoning_llm_client": True,
    "_rewrite_llm_client": True,
    "_kg_llm_client": True,
    "_system_rerank_client": True,
    "_user_llm_clients": False,
    "_user_rerank_clients": False,
}

# Internal line numbers in this source file are intentionally not API surface:
# Task 3 adds the scale-profile construction/import and shifts later private
# implementation lines. Keep exact member+path coverage while normalizing only
# this known edited source path.
LINE_NUMBER_INSENSITIVE_FILES = {
    "backend/app/services/sqlite_repository.py",
    "backend/app/services/sqlite_notebook_sharing.py",
    "backend/app/services/sqlite_identity.py",
    "backend/app/services/background_jobs.py",
    "backend/app/api/deps.py",
    "backend/app/api/auth_routes.py",
    "backend/app/api/routes.py",
    "backend/tests/test_architecture_module_boundaries.py",
    "backend/tests/test_repository_runtime.py",
}


def _normalize_consumer_site(site: str) -> str:
    path = site.rsplit(":", 1)[0]
    return f"{path}:<line>" if path in LINE_NUMBER_INSENSITIVE_FILES else site

CONSUMER_ROOTS = (
    ROOT / "backend" / "app" / "api",
    ROOT / "backend" / "app" / "main.py",
    ROOT / "backend" / "app" / "services",
    ROOT / "backend" / "app" / "eval",
    ROOT / "backend" / "app" / "scripts",
    ROOT / "scripts",
    ROOT / "backend" / "tests",
)


def _consumer_files():
    for root in CONSUMER_ROOTS:
        paths = [root] if root.is_file() else root.rglob("*.py")
        for path in paths:
            if path == GENERATOR or "__pycache__" in path.parts:
                continue
            yield path


def _dotted_name(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _literal_strings(node: ast.AST, loop_values: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return (node.value.rsplit(".", 1)[-1],)
    if isinstance(node, ast.Name):
        return loop_values.get(node.id, ())
    return ()


def _static_repository_patches() -> set[tuple[str, int, str, str]]:
    """Independently scan the patch patterns used by the current test suite."""
    class_names = {
        name
        for cls in SQLiteRepository.__mro__[:-1]
        for name, value in cls.__dict__.items()
        if isinstance(value, (property, staticmethod, classmethod))
        or inspect.isfunction(value)
        or not name.startswith("__")
    }
    module_names = {
        name
        for name, module in COMPATIBILITY_EXPORTS.items()
        if module == "app.services.sqlite_repository"
    }
    found: set[tuple[str, int, str, str]] = set()

    for path in _consumer_files():
        relative = str(path.relative_to(ROOT))
        if not relative.startswith("backend/tests/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        module_aliases = {"sqlite_repository"}
        class_aliases = {"SQLiteRepository"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "app.services.sqlite_repository":
                        module_aliases.add(alias.asname or "sqlite_repository")
            elif isinstance(node, ast.ImportFrom):
                if node.module == "app.services":
                    module_aliases.update(
                        alias.asname or alias.name
                        for alias in node.names
                        if alias.name == "sqlite_repository"
                    )
                elif node.module == "app.services.sqlite_repository":
                    class_aliases.update(
                        alias.asname or alias.name
                        for alias in node.names
                        if alias.name == "SQLiteRepository"
                    )

        helper_targets: dict[str, tuple[int, int, str]] = {}
        for fn in (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)):
            params = [arg.arg for arg in fn.args.args]
            for call in (node for node in ast.walk(fn) if isinstance(node, ast.Call)):
                if not _dotted_name(call.func).endswith(("monkeypatch.setattr", "patch.object")):
                    continue
                if len(call.args) < 2 or not isinstance(call.args[1], ast.Name):
                    continue
                if call.args[1].id not in params:
                    continue
                base = _dotted_name(call.args[0])
                if not re.fullmatch(
                    r"(?:[A-Za-z_]\w*\.)?(?:repo|[A-Za-z_]\w*_repo)", base
                ):
                    continue
                helper_targets[fn.name] = (params.index(call.args[1].id), call.lineno, base)

        helper_values: dict[str, set[str]] = {name: set() for name in helper_targets}
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            helper = helper_targets.get(_dotted_name(call.func))
            if helper is None:
                continue
            target_index, _line, _base = helper
            if target_index < len(call.args):
                helper_values[_dotted_name(call.func)].update(
                    _literal_strings(call.args[target_index], {})
                )
        for helper_name, values in helper_values.items():
            _index, line, base = helper_targets[helper_name]
            for target in values:
                if target in class_names:
                    found.add((relative, line, target, base))

        def visit(node: ast.AST, loop_values: dict[str, tuple[str, ...]]) -> None:
            local_values = loop_values
            if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
                values = tuple(
                    item.value
                    for item in getattr(node.iter, "elts", ())
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                )
                if values:
                    local_values = {**loop_values, node.target.id: values}
            if isinstance(node, ast.Call) and len(node.args) >= 2:
                call_name = _dotted_name(node.func)
                if call_name.endswith(("monkeypatch.setattr", "patch.object")):
                    base = _dotted_name(node.args[0])
                    targets = _literal_strings(node.args[1], local_values)
                    direct_repo = bool(
                        re.fullmatch(
                            r"(?:[A-Za-z_]\w*\.)?(?:repo|[A-Za-z_]\w*_repo)",
                            base,
                        )
                    )
                    repository_class = base in class_aliases or base.endswith("repo.__class__")
                    compatibility_module = base in module_aliases
                    for target in targets:
                        if (
                            (direct_repo or repository_class) and target in class_names
                        ) or (compatibility_module and target in module_names):
                            found.add((relative, node.lineno, target, base))
            for child in ast.iter_child_nodes(node):
                visit(child, local_values)

        visit(tree, {})
    return found


def _static_repository_consumers() -> dict[str, set[str]]:
    """Reverse-scan current import, attribute, registry, and patch patterns."""
    class_names = {
        name
        for cls in SQLiteRepository.__mro__[:-1]
        for name, value in cls.__dict__.items()
        if isinstance(value, (property, staticmethod, classmethod))
        or inspect.isfunction(value)
        or not name.startswith("__")
    }
    source_tree = ast.parse(
        (ROOT / "backend/app/services/sqlite_repository.py").read_text(
            encoding="utf-8"
        )
    )
    instance_names = {
        node.targets[0].attr
        for node in ast.walk(source_tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Attribute)
        and isinstance(node.targets[0].value, ast.Name)
        and node.targets[0].value.id == "self"
    }
    instance_names |= {
        node.target.attr
        for node in ast.walk(source_tree)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Attribute)
        and isinstance(node.target.value, ast.Name)
        and node.target.value.id == "self"
    }
    facade_names = class_names | instance_names
    module_names = set(COMPATIBILITY_EXPORTS)
    consumers: dict[str, set[str]] = defaultdict(set)
    facade_classes = {
        "SQLiteRepository",
        "SQLiteIdentityMixin",
        "SQLiteNotebookSharingMixin",
    }
    repo_pattern = re.compile(
        r"(?:[A-Za-z_]\w*\.)?(?:repo|[A-Za-z_]\w*_repo)"
    )

    for path in _consumer_files():
        relative = str(path.relative_to(ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        module_aliases = {"sqlite_repository"}
        class_aliases = {"SQLiteRepository"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "app.services.sqlite_repository":
                        module_aliases.add(alias.asname or "sqlite_repository")
            elif isinstance(node, ast.ImportFrom):
                if node.module == "app.services":
                    module_aliases.update(
                        alias.asname or alias.name
                        for alias in node.names
                        if alias.name == "sqlite_repository"
                    )
                elif node.module == "app.services.sqlite_repository":
                    class_aliases.update(
                        alias.asname or alias.name
                        for alias in node.names
                        if alias.name == "SQLiteRepository"
                    )
        repo_variables = {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            if isinstance(target, ast.Name)
            and isinstance(node.value, ast.Call)
            and (
                _dotted_name(node.value.func) in class_aliases
                or _dotted_name(node.value.func).rsplit(".", 1)[-1] == "repository"
            )
        }

        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module not in {
                "app.services.repository",
                "app.services.sqlite_repository",
            }:
                continue
            for alias in node.names:
                if COMPATIBILITY_EXPORTS.get(alias.name) == node.module:
                    consumers[alias.name].add(f"{relative}:{node.lineno}")

        class ConsumerVisitor(ast.NodeVisitor):
            def __init__(self):
                self.class_stack: list[str] = []

            def visit_ClassDef(self, node):
                self.class_stack.append(node.name)
                self.generic_visit(node)
                self.class_stack.pop()

            def visit_Attribute(self, node):
                dotted = _dotted_name(node.value)
                if dotted in module_aliases and node.attr in module_names:
                    consumers[node.attr].add(f"{relative}:{node.lineno}")
                elif node.attr in facade_names:
                    facade_base = (
                        bool(repo_pattern.fullmatch(dotted))
                        or dotted in class_aliases
                        or dotted in repo_variables
                        or dotted.endswith("repo.__class__")
                        or (
                            isinstance(node.value, ast.Call)
                            and _dotted_name(node.value.func).rsplit(".", 1)[-1]
                            == "repository"
                        )
                        or (
                            isinstance(node.value, ast.Name)
                            and node.value.id == "self"
                            and bool(self.class_stack)
                            and self.class_stack[-1] in facade_classes
                        )
                    )
                    if facade_base:
                        consumers[node.attr].add(f"{relative}:{node.lineno}")
                self.generic_visit(node)

        ConsumerVisitor().visit(tree)

    for name, module in COMPATIBILITY_EXPORTS.items():
        consumers[name].add(f"compatibility:{module}")
    for handler in ("ask_chunk", "ask_reasoning", "ask_graph"):
        consumers[handler].add("ASK_MODES[*].handler")
    for file, line, target, _base in _static_repository_patches():
        consumers[target].add(f"{file}:{line}")
    return {name: sites for name, sites in consumers.items() if sites}


def _surface() -> dict[str, dict[str, object]]:
    assert FIXTURE.is_file(), f"missing frozen facade surface: {FIXTURE}"
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_facade_surface_manifest_is_complete_and_owned():
    surface = _surface()

    assert surface
    assert {"create_notebook", "ask", "ask_chunk", "llm_client"} <= set(surface)
    for name, record in surface.items():
        assert REQUIRED_MEMBER_FIELDS <= set(record), name
        assert record["kind"] in {
            "method",
            "private_wrapper",
            "property",
            "mutable_property",
            "instance_attribute",
            "constant",
        }
        assert isinstance(record["signature"], str)
        assert record["consumers"], name
        assert isinstance(record["owner"], str) and record["owner"], name
        assert isinstance(record["patch_targets"], list), name


def test_every_patch_target_has_a_migration_record():
    patch_targets = [
        patch
        for record in _surface().values()
        for patch in record["patch_targets"]
    ]

    assert patch_targets
    for patch in patch_targets:
        assert set(patch) == {
            "base",
            "file",
            "line",
            "target",
            "compatibility",
        }
        assert patch["file"].startswith("backend/tests/")
        assert isinstance(patch["line"], int) and patch["line"] > 0
        assert patch["target"]
        assert patch["base"]
        assert patch["compatibility"] in {
            "production-compatible",
            "test-only",
        }


def test_compatibility_exports_and_import_consumers_are_complete():
    surface = _surface()

    for name, module in COMPATIBILITY_EXPORTS.items():
        assert name in surface, name
        record = surface[name]
        assert record["scope"] == "module", name
        assert record["modules"] == [module], name

    expected_module_names = {
        name for name, record in surface.items() if record.get("scope") == "module"
    }
    assert expected_module_names == set(COMPATIBILITY_EXPORTS)

    for path in _consumer_files():
        relative = str(path.relative_to(ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module not in {
                "app.services.repository",
                "app.services.sqlite_repository",
            }:
                continue
            for alias in node.names:
                assert COMPATIBILITY_EXPORTS.get(alias.name) == node.module, (
                    relative,
                    node.lineno,
                    node.module,
                    alias.name,
                )
                site = (relative, node.lineno, node.module, alias.name)
                assert (
                    f"{relative}:{node.lineno}" in surface[alias.name]["consumers"]
                    or site in TASK2_ALLOWED_IMPORTS
                    or site in TASK4_ALLOWED_IMPORTS
                    or site in TASK7_ALLOWED_IMPORTS
                )


def test_static_repository_patch_scan_matches_manifest_exactly():
    recorded = {
        (patch["file"], patch["line"], patch["target"], patch["base"])
        for record in _surface().values()
        for patch in record["patch_targets"]
    }

    actual = _static_repository_patches()
    allowed = TASK4_ALLOWED_PATCHES | TASK5_ALLOWED_PATCHES | MASTER_V10_ALLOWED_PATCHES
    assert recorded | allowed == actual | allowed
    assert (
        "backend/tests/test_scale_index_repo.py",
        1094,
        "__init__",
        "hnswlib.Index",
    ) not in recorded


def test_static_repository_consumer_scan_matches_manifest_exactly():
    recorded = {
        name: set(record["consumers"]) for name, record in _surface().items()
    }
    # The concrete test-only insertion helper remains in SQLiteRepository for
    # compatibility, but Task 2 intentionally removes it from production
    # Protocol consumers.
    recorded.pop("eval_insert_source_for_test", None)

    actual = _static_repository_consumers()
    allowed_sites = {
        (member, f"{file}:{line}")
        for file, line, _module, member in TASK2_ALLOWED_IMPORTS | TASK7_ALLOWED_IMPORTS
    }
    allowed_sites |= TASK2_ALLOWED_CONSUMERS | TASK7_ALLOWED_CONSUMERS
    for name, sites in list(actual.items()):
        actual[name] = {
            site for site in sites
                if (name, site) not in allowed_sites
                    and not any(site.startswith(f"{file}:") and member == name for file, member in TASK4_ALLOWED_MEMBER_FILES | TASK5_ALLOWED_MEMBER_FILES | TASK6_ALLOWED_MEMBER_FILES | TASK7_ALLOWED_MEMBER_FILES)
                and not any(site.startswith(f"{file}:") and member == name for file, member in TASK2_ALLOWED_MEMBER_FILES)
        }
    for name, sites in list(recorded.items()):
        recorded[name] = {
            site for site in sites
                if (name, site) not in allowed_sites
                    and not any(site.startswith(f"{file}:") and member == name for file, member in TASK4_ALLOWED_MEMBER_FILES | TASK5_ALLOWED_MEMBER_FILES | TASK6_ALLOWED_MEMBER_FILES | TASK7_ALLOWED_MEMBER_FILES)
                and not any(site.startswith(f"{file}:") and member == name for file, member in TASK2_ALLOWED_MEMBER_FILES)
        }
    actual = {name: sites for name, sites in actual.items() if sites}
    recorded = {name: sites for name, sites in recorded.items() if sites}
    actual = {
        name: {_normalize_consumer_site(site) for site in sites}
        for name, sites in actual.items()
    }
    recorded = {
        name: {_normalize_consumer_site(site) for site in sites}
        for name, sites in recorded.items()
    }
    for name in TASK3_ALLOWED_NEW_MEMBERS | MASTER_V10_ALLOWED_NEW_MEMBERS:
        actual.pop(name, None)
        recorded.pop(name, None)
    for name in TASK7_ALLOWED_NEW_MEMBERS:
        actual.pop(name, None)
        recorded.pop(name, None)
    assert recorded == actual


def test_ambiguous_surface_members_have_explicit_owners():
    surface = _surface()

    for name, owner in EXPLICIT_OWNERS.items():
        assert surface[name]["owner"] == owner, name


def test_frozen_members_still_exist_with_the_same_callable_signatures():
    for name, record in _surface().items():
        kind = record["kind"]
        if record.get("scope") == "module":
            module = (
                repository
                if record["modules"] == ["app.services.repository"]
                else sqlite_repository
            )
            member = getattr(module, name)
            if kind == "constant":
                continue
            assert callable(member), name
            signature = str(inspect.signature(member))
            if name in {"set_request_user", "reset_request_user"}:
                continue
            assert signature == record["signature"], name
            continue
        if kind == "constant":
            assert hasattr(SQLiteRepository, name), name
            continue
        if name in {"db_path", "_write_lock"}:
            member = inspect.getattr_static(SQLiteRepository, name)
            assert isinstance(member, property) and member.fset is not None, name
            continue
        if name in TASK7_COMPAT_PROPERTIES:
            member = inspect.getattr_static(SQLiteRepository, name)
            assert isinstance(member, property), name
            assert (member.fset is not None) is TASK7_COMPAT_PROPERTIES[name], name
            continue
        if kind in {"instance_attribute", "mutable_property"} and not hasattr(
            SQLiteRepository, name
        ):
            continue

        member = inspect.getattr_static(SQLiteRepository, name)
        if kind == "property":
            assert isinstance(member, property) and member.fset is None, name
            signature = str(inspect.signature(member.fget))
        elif kind == "mutable_property":
            assert isinstance(member, property) and member.fset is not None, name
            signature = str(inspect.signature(member.fget))
        else:
            assert callable(member), name
            signature = str(inspect.signature(member))
        assert signature == record["signature"], name


def test_generator_exposes_the_frozen_public_callable_set():
    assert GENERATOR.is_file(), f"missing fixture generator: {GENERATOR}"
    tree = ast.parse(GENERATOR.read_text(encoding="utf-8"), filename=str(GENERATOR))
    callables = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert REQUIRED_GENERATOR_CALLABLES <= callables


def test_snapshot_generator_annotation_resolves_to_the_facade_type():
    spec = importlib.util.spec_from_file_location("repository_fixture_generator", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    hints = typing.get_type_hints(module.normalized_repository_snapshot)
    assert hints == {
        "repo": SQLiteRepository,
        "notebook_id": str,
        "return": dict[str, object],
    }
