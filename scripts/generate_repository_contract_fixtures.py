#!/usr/bin/env python3
"""Generate the Repository composition-refactor contract fixtures.

``main`` regenerates the living contracts (ask, api, phase, repository_v9 projection)
against the current runtime. ``facade_surface.json`` is an explicitly rebaselined
current-state semantic contract; ``repository_v9/baseline.db`` remains frozen at
``SOURCE_COMMIT`` and changes only under ``--rebaseline`` (see README).
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import inspect
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator, Mapping, Sequence
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.sqlite_repository import SQLiteRepository

SOURCE_COMMIT = "3334626"
SURFACE_SOURCE_COMMIT = "842de5d22dbdc423d6572fe0650911dafa910c35"
FIXED_TIME = "2024-01-02T03:04:05"
FIXED_EXPIRY = "2024-02-01T03:04:05"
FIXED_PASSWORD_SALT = "00" * 16

CONSUMER_ROOTS = (
    REPO_ROOT / "backend" / "app" / "api",
    REPO_ROOT / "backend" / "app" / "main.py",
    REPO_ROOT / "backend" / "app" / "services",
    REPO_ROOT / "backend" / "app" / "eval",
    REPO_ROOT / "backend" / "app" / "scripts",
    REPO_ROOT / "scripts",
    REPO_ROOT / "backend" / "tests",
)

TEST_CONSUMER_PREFIX = "backend/tests/"

# These exports are intentionally explicit: later refactor gates must preserve
# both compatibility modules even when the implementation owners move.
COMPATIBILITY_EXPORTS = {
    "KnowledgeGraphTooLargeError": ("app.services.sqlite_repository", "KnowledgeLifecycleService"),
    "NotebookRepository": ("app.services.repository", "RepositoryPorts"),
    "RetrievedKnowledge": ("app.services.sqlite_repository", "RetrievalService"),
    "SCHEMA_VERSION": ("app.services.sqlite_repository", "SqliteDatabase"),
    "SQLiteRepository": ("app.services.sqlite_repository", "RepositoryFacade"),
    "USABLE_STATUSES": ("app.services.sqlite_repository", "KnowledgeGovernanceService"),
    "UploadedSourceFile": ("app.services.repository", "RepositoryPorts"),
    "_ASK_MODEL_ERRORS": ("app.services.sqlite_repository", "ModelProvider"),
    "_COPY_CHUNK": ("app.services.sqlite_repository", "NotebookSharingService"),
    "_REQUEST_USER": ("app.services.sqlite_repository", "RequestContext"),
    "_concept_desc_sig": ("app.services.sqlite_repository", "KnowledgeLifecycleService"),
    "_fast_loads": ("app.services.sqlite_repository", "KnowledgeLifecycleService"),
    "_new_id": ("app.services.sqlite_repository", "RepositoryCompatibilitySeams"),
    "_now": ("app.services.sqlite_repository", "RepositoryCompatibilitySeams"),
    "_remap_json_ids": ("app.services.sqlite_repository", "NotebookSharingService"),
    "KNOWLEDGE_STATUSES": ("app.services.sqlite_repository", "KnowledgeGovernanceService"),
    "reset_request_user": ("app.services.sqlite_repository", "RequestContext"),
    "set_request_user": ("app.services.sqlite_repository", "RequestContext"),
}

AMBIGUOUS_MEMBER_OWNERS = {
    "approve_promotion": "KnowledgeGovernanceService",
    "list_promotion_queue": "KnowledgeGovernanceService",
    "propose_promotion": "KnowledgeGovernanceService",
    "reject_promotion": "KnowledgeGovernanceService",
}

ATTRIBUTE_MEMBER_OWNERS = {
    "_IN_CHUNK": "RepositoryCompatibilitySeams",
    "_kg_building": "KnowledgeLifecycleService",
    "_kg_building_lock": "KnowledgeLifecycleService",
    "_migrator": "SqliteDatabase",
    "_runtime": "RepositoryRuntime",
    "event_log": "EventLogger",
    "maintenance": "SQLiteMaintenanceAdapter",
    "checkup": "CheckupService",
    "mineru_client": "SourceIngestionService",
    "mineru_cloud_client": "SourceIngestionService",
    "root_dir": "RepositoryFacade",
    "settings": "RepositoryFacade",
}

SQLITE_WRAPPER_ALLOWED_MEMBERS = {
    "__init__", "db_path", "_write_lock", "_connect", "close_local",
    "_clear_source_extraction_state", "_knowledge_objects", "_migrate",
    "_migrate_legacy", "_add_column_if_missing", "_migration_1",
    "_migration_2", "_migration_3", "_migration_4", "_migration_5",
    "_migration_6", "_migration_7", "_migration_8", "_migration_9",
    "_migration_10", "_migration_19", "_migration_27", "_run_migration",
    "_recover_interrupted_jobs",
    "_recover_interrupted_jobs_legacy", "_seed", "_seed_legacy",
    "maintenance", "checkup", "eval_insert_source_for_test",
    "_backfill_relation_embeddings", "_source_ids_from_evidence",
    "_delete_knowledge_object_sources", "_insert_row", "_seed_fn_for",
    "_merge_evidence_lists", "_read_ask_trace",
}

SQLITE_WRAPPER_ALLOWED_BACKEND_IMPORTS = {
    "app.repositories.sqlite.ask_state_store",
    "app.repositories.sqlite.bundle",
    "app.repositories.sqlite.governance_store",
    "app.repositories.sqlite.knowledge_store",
    "app.repositories.sqlite.maintenance",
    "app.repositories.sqlite.migrations",
    "app.repositories.sqlite.sharing_store",
}


def _assert_baseline_sources(repo_root: Path) -> None:
    subprocess.check_call(
        ["git", "rev-parse", "--verify", f"{SOURCE_COMMIT}^{{commit}}"],
        cwd=repo_root,
        stdout=subprocess.DEVNULL,
    )
    baseline_paths = {
        line
        for line in subprocess.check_output(
            ["git", "ls-tree", "-r", "--name-only", SOURCE_COMMIT, "backend/app"],
            cwd=repo_root,
            text=True,
        ).splitlines()
        if line.endswith(".py")
    }
    current_paths = {
        str(path.relative_to(repo_root))
        for path in (repo_root / "backend" / "app").rglob("*.py")
    }
    if current_paths != baseline_paths:
        raise SystemExit("refuse fixture regeneration after backend/app path changes")
    for relative in sorted(baseline_paths):
        baseline = subprocess.check_output(
            ["git", "show", f"{SOURCE_COMMIT}:{relative}"],
            cwd=repo_root,
        )
        if (repo_root / relative).read_bytes() != baseline:
            raise SystemExit(
                f"refuse fixture regeneration after runtime change: {relative}"
            )


def _storage_manifest(storage: Path) -> list[dict[str, object]]:
    return [
        {
            "path": str(path.relative_to(storage)),
            "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(p for p in storage.rglob("*") if p.is_file())
    ]


def _artifact_manifest(root: Path, path: Path) -> dict[str, object]:
    return {
        "path": str(path.relative_to(root)),
        "size": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _iter_python_files() -> Iterator[Path]:
    seen: set[Path] = set()
    generator_path = Path(__file__).resolve()
    for root in CONSUMER_ROOTS:
        paths = [root] if root.is_file() else root.rglob("*.py") if root.exists() else []
        for path in paths:
            if path.resolve() == generator_path or path in seen or "__pycache__" in path.parts:
                continue
            seen.add(path)
            yield path


def _dotted_name(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _semantic_scopes(tree: ast.AST) -> dict[ast.AST, str]:
    scopes: dict[ast.AST, str] = {}

    class ScopeVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack = ["<module>"]

        @property
        def current(self) -> str:
            return ".".join(self.stack)

        def visit(self, node: ast.AST) -> None:
            scopes[node] = self.current
            super().visit(node)

        def _visit_scope(self, node: ast.AST, name: str) -> None:
            self.stack.append(name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._visit_scope(node, node.name)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_scope(node, node.name)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_scope(node, node.name)

        def visit_Lambda(self, node: ast.Lambda) -> None:
            self._visit_scope(node, "<lambda>")

    ScopeVisitor().visit(tree)
    return scopes


def _semantic_site(
    *,
    path: str,
    scope: str,
    kind: str,
    target: str,
) -> dict[str, str]:
    return {
        "path": path,
        "scope": scope,
        "kind": kind,
        "target": target,
    }


def _site_key(site: dict[str, str]) -> tuple[str, str, str, str]:
    return (
        site["path"],
        site["scope"],
        site["kind"],
        site["target"],
    )


def _literal_target(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.rsplit(".", 1)[-1]
    return ""


def _owner_for(name: str) -> str:
    explicit_export = COMPATIBILITY_EXPORTS.get(name)
    if explicit_export is not None:
        return explicit_export[1]
    if name in AMBIGUOUS_MEMBER_OWNERS:
        return AMBIGUOUS_MEMBER_OWNERS[name]
    if name in ATTRIBUTE_MEMBER_OWNERS:
        return ATTRIBUTE_MEMBER_OWNERS[name]
    identity = {
        "current_user", "create_user", "authenticate_user", "create_session",
        "resolve_session", "delete_session", "list_user_usage",
        "list_user_notebooks",
    }
    provider = {"_note_model_error"}
    sharing_fragments = (
        "share", "member", "copy_notebook", "_sweep_stuck_copies",
        "user_can_", "_owner", "find_notebook_by_share_token",
        "notebook_copy_stats", "join_shared", "leave_notebook",
    )
    ask_fragments = (
        "ask", "conversation", "answer", "feedback", "_parse_answer",
        "_save_answer", "_ensure_conversation", "_cleanup_empty_conversation",
    )
    report_fragments = ("report",)
    source_fragments = (
        "source", "upload", "parse_", "process_source", "import_sources",
        "add_url", "_delete_file", "_build_chunks", "_embed_chunks",
    )
    scale_fragments = (
        "scale", "viz", "index", "_vector_cache", "_unified_cache",
        "_invalidate_unified_cache", "_auto_index",
    )
    retrieval_fragments = (
        "retrieve", "retrieval", "ppr", "follow_chain", "_embed_query",
        "_knowledge_vectors", "_answer_context", "_chunk_answer_context",
        "_context", "citation", "anchor", "_tier_map", "_federated",
    )
    governance_fragments = (
        "duplicate", "merge", "promotion", "conflict", "edge_review",
        "update_knowledge", "whitelist", "cluster",
    )
    lifecycle_fragments = (
        "store_kg", "rebuild", "relink", "unified", "canonical", "mention",
        "community", "delete_notebook_kg", "add_relations", "incremental_fuse",
    )
    schema_fragments = ("schema",)
    notebook_fragments = ("notebook", "pending_actions", "search_notebook")

    if name in identity or name.startswith(("_user_", "auth_")):
        return "IdentityStore"
    if name in provider:
        return "ModelProvider"
    if any(part in name for part in sharing_fragments):
        return "NotebookSharingService"
    if any(part in name for part in report_fragments):
        return "ReportEngine"
    if any(part in name for part in ask_fragments):
        return "AskService"
    if any(part in name for part in source_fragments):
        return "SourceIngestionService"
    if any(part in name for part in scale_fragments):
        return "ScaleArtifactRuntime"
    if any(part in name for part in retrieval_fragments):
        return "RetrievalService"
    if any(part in name for part in governance_fragments):
        return "KnowledgeGovernanceService"
    if any(part in name for part in lifecycle_fragments):
        return "KnowledgeLifecycleService"
    if any(part in name for part in schema_fragments):
        return "SchemaRegistryService"
    if any(part in name for part in notebook_fragments):
        return "NotebookCatalogService"
    if name in {"_connect", "_write", "_migrate", "_run_migration", "_seed", "SCHEMA_VERSION"} or name.startswith("_migration_"):
        return "SqliteDatabase"
    raise KeyError(f"unmapped repository surface owner: {name}")


def _patch_compatibility(name: str, kind: str) -> str:
    if kind in {"mutable_property", "constant"} or name in {
        "_now", "_new_id", "_COPY_CHUNK",
    }:
        return "production-compatible"
    return "test-only"


def _owners_at_commit(commit: str) -> dict[str, str]:
    relative = "backend/app/repositories/ownership_manifest.py"
    source = subprocess.check_output(
        ["git", "show", f"{commit}:{relative}"],
        cwd=REPO_ROOT,
        text=True,
    )
    namespace: dict[str, object] = {}
    exec(compile(source, f"{commit}:{relative}", "exec"), namespace)
    return dict(namespace["OWNER_BY_MEMBER"])


def _validate_sqlite_wrapper_contract() -> None:
    """Fail fixture generation if SQLite compatibility grows backend behavior."""
    path = REPO_ROOT / "backend/app/services/sqlite_repository.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    wrapper = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SQLiteRepository"
    )
    actual_members = {
        node.name
        for node in wrapper.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if actual_members != SQLITE_WRAPPER_ALLOWED_MEMBERS:
        raise AssertionError(
            "SQLiteRepository wrapper member drift: "
            f"expected={sorted(SQLITE_WRAPPER_ALLOWED_MEMBERS)!r}, "
            f"actual={sorted(actual_members)!r}"
        )
    wrapper_source = ast.get_source_segment(source, wrapper) or ""
    forbidden_sql = {
        token for token in (".execute(", ".executemany(", ".executescript(")
        if token in wrapper_source
    }
    if forbidden_sql:
        raise AssertionError(
            f"SQLiteRepository wrapper contains SQL execution: {sorted(forbidden_sql)!r}"
        )
    backend_imports = {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("app.repositories.sqlite")
    }
    if backend_imports != SQLITE_WRAPPER_ALLOWED_BACKEND_IMPORTS:
        raise AssertionError(
            "SQLiteRepository wrapper backend import drift: "
            f"expected={sorted(SQLITE_WRAPPER_ALLOWED_BACKEND_IMPORTS)!r}, "
            f"actual={sorted(backend_imports)!r}"
        )


def collect_facade_surface(
    *,
    owner_by_member: Mapping[str, str] | None = None,
) -> dict[str, dict[str, object]]:
    """Collect the consumer-visible facade and compatibility patch surface."""
    _validate_sqlite_wrapper_contract()
    if owner_by_member is None:
        from app.repositories.ownership_manifest import OWNER_BY_MEMBER

        owner_by_member = OWNER_BY_MEMBER
    from app.services import repository as repository_module
    from app.services import sqlite_repository as module
    from app.services.ask_modes import ASK_MODES
    from app.services.repository_facade import RepositoryFacade
    from app.services.sqlite_repository import SQLiteRepository
    from tests.architecture.facade_contract import facade_delegate_evidence

    class_members: dict[str, object] = {}
    for cls in reversed(SQLiteRepository.__mro__[:-1]):
        for name, value in cls.__dict__.items():
            if (
                isinstance(value, (property, staticmethod, classmethod))
                or inspect.isfunction(value)
                or not name.startswith("__")
            ):
                class_members[name] = value

    source_trees = tuple(
        ast.parse((REPO_ROOT / relative).read_text(encoding="utf-8"))
        for relative in (
            "backend/app/services/repository_facade.py",
            "backend/app/services/sqlite_repository.py",
        )
    )
    instance_attributes = {
        node.targets[0].attr
        for source_tree in source_trees
        for node in ast.walk(source_tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Attribute)
        and isinstance(node.targets[0].value, ast.Name)
        and node.targets[0].value.id == "self"
    }
    instance_attributes |= {
        node.target.attr
        for source_tree in source_trees
        for node in ast.walk(source_tree)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Attribute)
        and isinstance(node.target.value, ast.Name)
        and node.target.value.id == "self"
    }

    compatibility_modules = {
        "app.services.repository": repository_module,
        "app.services.sqlite_repository": module,
    }
    module_candidates = set(COMPATIBILITY_EXPORTS)
    candidate_names = set(class_members) | instance_attributes | module_candidates
    delegate_owners = {
        name: owner
        for name, owner in facade_delegate_evidence(
            RepositoryFacade,
            {name: "" for name in candidate_names},
        ).items()
        if owner
    }
    consumers: dict[
        str,
        dict[tuple[str, str, str, str], dict[str, str]],
    ] = defaultdict(dict)
    patches: dict[
        str,
        dict[tuple[str, str, str, str], dict[str, str]],
    ] = defaultdict(dict)

    def add_consumer(
        name: str,
        *,
        path: str,
        scope: str,
        kind: str,
        target: str | None = None,
    ) -> None:
        site = _semantic_site(
            path=path,
            scope=scope,
            kind=kind,
            target=target or name,
        )
        consumers[name][_site_key(site)] = site

    for path in _iter_python_files():
        relative = path.relative_to(REPO_ROOT).as_posix()
        track_ordinary_consumers = not relative.startswith(TEST_CONSUMER_PREFIX)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (SyntaxError, UnicodeDecodeError):
            continue
        scopes = _semantic_scopes(tree)
        module_aliases = {"sqlite_repository"}
        class_aliases = {"SQLiteRepository", "RepositoryFacade"}
        for import_node in ast.walk(tree):
            if isinstance(import_node, ast.Import):
                for alias in import_node.names:
                    if alias.name == "app.services.sqlite_repository":
                        module_aliases.add(alias.asname or alias.name.split(".")[-1])
            elif (
                isinstance(import_node, ast.ImportFrom)
                and import_node.module == "app.services"
            ):
                for alias in import_node.names:
                    if alias.name == "sqlite_repository":
                        module_aliases.add(alias.asname or alias.name)
            elif (
                isinstance(import_node, ast.ImportFrom)
                and import_node.module in {
                    "app.services.repository_facade",
                    "app.services.sqlite_repository",
                }
            ):
                for alias in import_node.names:
                    if alias.name in {"RepositoryFacade", "SQLiteRepository"}:
                        class_aliases.add(alias.asname or alias.name)

        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module not in {
                "app.services.repository",
                "app.services.sqlite_repository",
            }:
                continue
            for alias in node.names:
                export = COMPATIBILITY_EXPORTS.get(alias.name)
                if (
                    track_ordinary_consumers
                    and export is not None
                    and export[0] == node.module
                ):
                    add_consumer(
                        alias.name,
                        path=relative,
                        scope=scopes[node],
                        kind="import",
                        target=f"{node.module}:{alias.name}",
                    )

        repo_base_pattern = re.compile(
            r"(?:[A-Za-z_]\w*\.)?(?:repo|[A-Za-z_]\w*_repo)"
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
                or _dotted_name(node.value.func).rsplit(".", 1)[-1]
                == "repository"
            )
        }
        facade_classes = {
            "RepositoryFacade",
            "SQLiteRepository",
            "SQLiteIdentityMixin",
            "SQLiteNotebookSharingMixin",
        }

        class ConsumerVisitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.class_stack: list[str] = []

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self.class_stack.append(node.name)
                self.generic_visit(node)
                self.class_stack.pop()

            def visit_Attribute(self, node: ast.Attribute) -> None:
                if node.attr not in candidate_names:
                    self.generic_visit(node)
                    return
                base = _dotted_name(node.value)
                module_target = base in module_aliases
                facade_target = (
                    bool(repo_base_pattern.fullmatch(base))
                    or base in class_aliases
                    or base in repo_variables
                    or base.endswith("repo.__class__")
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
                if module_target and node.attr in module_candidates:
                    if track_ordinary_consumers:
                        add_consumer(
                            node.attr,
                            path=relative,
                            scope=scopes[node],
                            kind="attribute",
                        )
                elif facade_target and (
                    node.attr in class_members or node.attr in instance_attributes
                ):
                    if track_ordinary_consumers:
                        add_consumer(
                            node.attr,
                            path=relative,
                            scope=scopes[node],
                            kind="attribute",
                        )
                self.generic_visit(node)

        ConsumerVisitor().visit(tree)

        def add_patch(
            target_name: str,
            target_base: str,
            node: ast.AST,
        ) -> None:
            module_target = target_base in module_aliases
            facade_target = (
                bool(repo_base_pattern.fullmatch(target_base))
                or target_base in class_aliases
                or target_base.endswith("repo.__class__")
            )
            if module_target:
                if target_name not in module_candidates:
                    return
            elif not facade_target or (
                target_name not in class_members
                and target_name not in instance_attributes
            ):
                return
            export = COMPATIBILITY_EXPORTS.get(target_name)
            exported_value = (
                getattr(compatibility_modules[export[0]], target_name)
                if export is not None
                else None
            )
            value = class_members.get(target_name, exported_value)
            raw_value = (
                value.__func__
                if isinstance(value, (staticmethod, classmethod))
                else value
            )
            patch_kind = (
                "mutable_property"
                if isinstance(value, property) and value.fset is not None
                else "constant"
                if not callable(raw_value)
                else "private_wrapper"
                if target_name.startswith("_")
                else "method"
            )
            site = {
                **_semantic_site(
                    path=relative,
                    scope=scopes[node],
                    kind="patch",
                    target=target_name,
                ),
                "base": target_base,
                "compatibility": _patch_compatibility(target_name, patch_kind),
            }
            patches[target_name][_site_key(site)] = site
            add_consumer(
                target_name,
                path=relative,
                scope=scopes[node],
                kind="patch",
            )

        helper_targets: dict[str, tuple[int, ast.FunctionDef, str]] = {}
        for function in (
            node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        ):
            params = [arg.arg for arg in function.args.args]
            for call in (
                node for node in ast.walk(function) if isinstance(node, ast.Call)
            ):
                if not _dotted_name(call.func).endswith(
                    ("monkeypatch.setattr", "patch.object")
                ):
                    continue
                if len(call.args) < 2 or not isinstance(call.args[1], ast.Name):
                    continue
                if call.args[1].id not in params:
                    continue
                target_base = _dotted_name(call.args[0])
                if not repo_base_pattern.fullmatch(target_base):
                    continue
                helper_targets[function.name] = (
                    params.index(call.args[1].id),
                    function,
                    target_base,
                )

        helper_values: dict[str, set[str]] = {
            name: set() for name in helper_targets
        }
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            helper = helper_targets.get(_dotted_name(call.func))
            if helper is None:
                continue
            target_index, _function, _base = helper
            if target_index >= len(call.args):
                continue
            target = _literal_target(call.args[target_index])
            if target:
                helper_values[_dotted_name(call.func)].add(target)
        for helper_name, values in helper_values.items():
            _index, function, target_base = helper_targets[helper_name]
            for target in values:
                add_patch(target, target_base, function)

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
                    target_base = _dotted_name(node.args[0])
                    targets: tuple[str, ...] = ()
                    target = _literal_target(node.args[1])
                    if target:
                        targets = (target,)
                    elif isinstance(node.args[1], ast.Name):
                        targets = local_values.get(node.args[1].id, ())
                    for target_name in targets:
                        add_patch(target_name, target_base, node)
            for child in ast.iter_child_nodes(node):
                visit(child, local_values)

        visit(tree, {})

    for spec in ASK_MODES.values():
        add_consumer(
            spec.handler,
            path="backend/app/services/ask_modes.py",
            scope="<module>.ASK_MODES",
            kind="registry",
        )

    for name, (module_name, _owner) in COMPATIBILITY_EXPORTS.items():
        add_consumer(
            name,
            path=module_name,
            scope="<module>",
            kind="compatibility",
        )

    surface: dict[str, dict[str, object]] = {}
    for name in sorted(candidate_names):
        if not consumers.get(name) and not patches.get(name):
            continue
        scope = "facade"
        signature = ""
        value = class_members.get(name)
        if isinstance(value, property):
            kind = "mutable_property" if value.fset is not None else "property"
            signature = str(inspect.signature(value.fget))
        elif value is not None:
            if isinstance(value, (staticmethod, classmethod)) or callable(value):
                raw = (
                    value.__func__
                    if isinstance(value, (staticmethod, classmethod))
                    else value
                )
                kind = "private_wrapper" if name.startswith("_") else "method"
                signature = str(inspect.signature(raw))
            else:
                kind = "constant"
                signature = type(value).__name__
        elif name in instance_attributes and not hasattr(module, name):
            kind = "instance_attribute"
            signature = "<instance attribute>"
        elif name in module_candidates:
            scope = "module"
            module_name = COMPATIBILITY_EXPORTS[name][0]
            value = getattr(compatibility_modules[module_name], name)
            if inspect.isclass(value):
                kind = "constant"
                signature = type(value).__name__
            elif callable(value):
                kind = "private_wrapper" if name.startswith("_") else "method"
                try:
                    signature = str(inspect.signature(value))
                except (TypeError, ValueError):
                    signature = "<opaque callable>"
            else:
                kind = "constant"
                signature = type(value).__name__
        else:
            continue
        owner = delegate_owners.get(name)
        if owner is None:
            owner = (
                owner_by_member[name]
                if name in owner_by_member
                else _owner_for(name)
            )
        surface[name] = {
            "kind": kind,
            "scope": scope,
            "signature": signature,
            "consumers": [
                consumers[name][key]
                for key in sorted(consumers[name])
            ],
            "owner": owner,
            "patch_targets": [
                patches[name][key]
                for key in sorted(patches[name])
            ],
        }
        if scope == "module":
            surface[name]["modules"] = [COMPATIBILITY_EXPORTS[name][0]]
    return surface


def _render_consumer_site(site: dict[str, str]) -> str:
    return (
        "ConsumerSite("
        f"path={site['path']!r}, "
        f"scope={site['scope']!r}, "
        f"kind={site['kind']!r}, "
        f"target={site['target']!r}"
        ")"
    )


def _render_consumer_sites(sites: list[dict[str, str]]) -> list[str]:
    return [f"            {_render_consumer_site(site)}," for site in sites]


def write_ownership_surface(
    path: Path,
    surface: dict[str, dict[str, object]],
) -> None:
    source = path.read_text(encoding="utf-8")
    start = source.index("SURFACE_MEMBERS = (")
    end = source.index("\ndef _unique_nonempty_owners", start)
    lines = ["SURFACE_MEMBERS = ("]
    for name, record in sorted(surface.items()):
        lines.extend(
            [
                "    SurfaceMember(",
                f"        name={name!r},",
                f"        owner={record['owner']!r},",
                f"        kind={record['kind']!r},",
                "        consumers=(",
                *_render_consumer_sites(record["consumers"]),
                "        ),",
                "        patches=(",
                *_render_consumer_sites(
                    [
                        {
                            "path": patch["path"],
                            "scope": patch["scope"],
                            "kind": patch["kind"],
                            "target": patch["target"],
                        }
                        for patch in record["patch_targets"]
                    ]
                ),
                "        ),",
                "    ),",
            ]
        )
    lines.append(")")
    path.write_text(
        source[:start] + "\n".join(lines) + source[end:],
        encoding="utf-8",
    )


class _FakeChatAdapter:
    configured = True
    base_url = "fake://offline"
    api_key = "fixture"
    model = "fixture-chat"

    def __init__(self, *_args, **_kwargs):
        pass

    def chat_json(self, _messages, schema_hint="", **_kwargs):
        hint = str(schema_hint)
        if "sub_queries" in hint:
            return json.dumps(
                {"sub_queries": [{"query": "fixture gain", "types": ["claim"]}]}
            )
        if "next_action" in hint:
            return json.dumps({"next_action": "answer", "sufficient": True})
        if "valid" in hint and "reason" in hint:
            return json.dumps({"valid": True, "reason": "fixture evidence agrees"})
        if "answer" in hint:
            return json.dumps(
                {"answer": "Fixture evidence establishes stable gain [k1].", "grounded": True}
            )
        return "{}"


class _FakeRerankAdapter:
    configured = False
    model = "fixture-rerank"

    def __init__(self, *_args, **_kwargs):
        pass

    def rerank(self, _query, documents, on_error=None):
        return list(range(len(documents)))


class _FakeMinerUAdapter:
    configured = False
    mode = "off"
    last_error = ""

    def __init__(self, *_args, **_kwargs):
        pass


@contextlib.contextmanager
def _deterministic_runtime() -> Iterator[None]:
    from app.domain import auth_utils  # B3: repositories now lazy-import hash_password from here
    from app.services import embedding
    from app.services import model_provider
    from app.services import rerank_client
    from app.services import sqlite_identity
    from app.services import sqlite_notebook_sharing
    from app.services import sqlite_repository
    from app.repositories.sqlite import identity_store

    original_hash_password = auth_utils.hash_password
    ids = defaultdict(int)
    perf = [1000.0]

    def next_id(prefix: str) -> str:
        ids[prefix] += 1
        return f"{prefix}-fixture-{ids[prefix]:04d}"

    def fixed_hash(password: str, *, salt=None, iterations=200_000):
        return original_hash_password(
            password, salt=FIXED_PASSWORD_SALT, iterations=iterations
        )

    def fixed_perf() -> float:
        perf[0] += 0.001
        return perf[0]

    def no_network(*_args, **_kwargs):
        raise AssertionError("fixture generation attempted network access")

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(sqlite_repository, "_new_id", next_id))
        stack.enter_context(mock.patch.object(sqlite_repository, "_now", lambda: FIXED_TIME))
        stack.enter_context(mock.patch.object(sqlite_identity, "_now", lambda: FIXED_TIME))
        stack.enter_context(mock.patch.object(sqlite_identity, "_session_expiry", lambda *_: FIXED_EXPIRY))
        stack.enter_context(mock.patch.object(identity_store, "_now", lambda: FIXED_TIME))
        stack.enter_context(mock.patch.object(identity_store, "_session_expiry", lambda *_: FIXED_EXPIRY))
        stack.enter_context(mock.patch.object(sqlite_notebook_sharing, "_now", lambda: FIXED_TIME))
        stack.enter_context(mock.patch.object(auth_utils, "hash_password", fixed_hash))
        stack.enter_context(
            mock.patch.object(model_provider, "OpenAICompatibleClient", _FakeChatAdapter)
        )
        stack.enter_context(
            mock.patch.object(sqlite_repository, "MinerUClient", _FakeMinerUAdapter)
        )
        stack.enter_context(
            mock.patch.object(sqlite_repository, "MinerUCloudClient", _FakeMinerUAdapter)
        )
        stack.enter_context(
            mock.patch.object(
                embedding,
                "make_embedder",
                lambda settings: embedding.FakeEmbedder(dim=settings.embed_dim),
            )
        )
        stack.enter_context(mock.patch.object(rerank_client, "RerankClient", _FakeRerankAdapter))
        stack.enter_context(mock.patch.object(model_provider, "RerankClient", _FakeRerankAdapter))
        stack.enter_context(mock.patch.object(time, "perf_counter", fixed_perf))
        stack.enter_context(
            mock.patch.dict(
                os.environ,
                {"FIXTURE_MODEL_API_KEY": "fixture-secret"},
            )
        )
        stack.enter_context(mock.patch.object(socket, "create_connection", no_network))
        stack.enter_context(mock.patch.object(socket.socket, "connect", no_network))
        yield


def _offline_settings(
    database: Path,
    storage: Path,
    *,
    configured: bool = False,
    missing_ask_answer: bool = False,
):
    from app.core.config import Settings
    from app.services.model_registry import WORKLOADS

    model_services_config = ""
    if configured:
        config_path = database.parent / "fixture-model-services.toml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        bindings = "\n".join(
            f'{workload_id} = "fixture_chat"'
            for workload_id, workload in WORKLOADS.items()
            if workload.kind == "chat"
            and (not missing_ask_answer or workload_id != "ask_answer")
        )
        config_path.write_text(
            '''[services.fixture_chat]
display_name = "FixtureChat"
kind = "chat"
protocol = "openai"
base_url = "https://fixture.invalid/v1"
model = "fixture-chat"
api_key_env = "FIXTURE_MODEL_API_KEY"
max_concurrency = 2

[bindings]
'''
            + bindings
            + "\n",
            encoding="utf-8",
        )
        model_services_config = str(config_path)

    return Settings(
        database_url=f"sqlite:///{database}",
        storage_dir=str(storage),
        model_services_config=model_services_config,
        embed_dim=4,
        mineru_mode="off",
        mineru_api_url="",
        mineru_vlm_server_url="",
        mineru_api_token="",
        scale_index_auto_enabled=False,
        event_log_enabled=False,
        llm_log_enabled=False,
        debug_logs_enabled=False,
        auth_optional=True,
        query_rewrite_enabled=False,
        chunk_kg_overlay_enabled=False,
        graph_ppr_enabled=False,
        kg_query_refine_enabled=False,
        reasoning_ppr_prefetch=False,
        community_layer_enabled=False,
        mention_bridge_enabled=False,
    )


def _new_offline_repo(
    database: Path,
    storage: Path,
    *,
    configured: bool = False,
    missing_ask_answer: bool = False,
):
    from app.services.sqlite_repository import SQLiteRepository

    return SQLiteRepository(
        _offline_settings(
            database,
            storage,
            configured=configured,
            missing_ask_answer=missing_ask_answer,
        )
    )


def _evidence() -> dict[str, object]:
    return {
        "source_id": "src-fixture",
        "source_title": "Fixture amplifier notes",
        "element_id": "el-fixture-0001",
        "element_type": "paragraph",
        "location_label": "§1 Gain",
        "quoted_span": "A source-degenerated stage stabilizes voltage gain.",
        "confidence": 0.98,
    }


def _seed_ask_repository(
    repo, *, include_kg: bool = True, include_source: bool = True
) -> str:
    notebook_id = "nb-ask-fixture"
    evidence = _evidence()
    now = FIXED_TIME
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebooks "
            "(id,name,purpose,primary_domain,status,created_by,created_at,updated_at,tier) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                notebook_id,
                "Ask fixture",
                "Freeze Ask behavior",
                "Analog IC",
                "active",
                "user-local",
                now,
                now,
                "personal",
            ),
        )
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,parse_status,file_name,file_path,"
            "file_size,file_hash,summary,doc_type,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "src-fixture",
                notebook_id,
                "Fixture amplifier notes",
                "markdown",
                "extracted",
                "parsed",
                "fixture.md",
                "",
                64,
                "fixture-hash",
                "Stable gain evidence.",
                "textbook",
                now,
                now,
            ),
        )
        db.execute(
            "INSERT INTO source_elements "
            "(id,source_id,element_type,location_label,text,metadata,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                "el-fixture-0001",
                "src-fixture",
                "paragraph",
                "§1 Gain",
                evidence["quoted_span"],
                "{}",
                now,
            ),
        )
        db.execute(
            "INSERT INTO chunks "
            "(id,notebook_id,source_id,text,section_path,element_ids,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                "chunk-fixture-0001",
                notebook_id,
                "src-fixture",
                "Fixture gain is stable because source degeneration adds feedback.",
                "§1 Gain",
                json.dumps(["el-fixture-0001"]),
                now,
            ),
        )
        db.execute(
            "INSERT INTO chunks_fts(chunk_id,notebook_id,text) VALUES (?,?,?)",
            (
                "chunk-fixture-0001",
                notebook_id,
                "Fixture gain is stable because source degeneration adds feedback.",
            ),
        )
        if include_kg:
            db.executemany(
                "INSERT INTO knowledge_objects "
                "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,"
                "created_at,updated_at,last_reviewed) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        "ko-fixture-concept",
                        notebook_id,
                        "concept",
                        "approved",
                        "fixture-owner",
                        json.dumps({"name": "fixture gain", "definition": "stable gain"}),
                        json.dumps([evidence], ensure_ascii=False),
                        "src-fixture",
                        now,
                        now,
                        now,
                    ),
                    (
                        "ko-fixture-claim",
                        notebook_id,
                        "claim",
                        "approved",
                        "fixture-owner",
                        json.dumps(
                            {"name": "fixture gain remains stable", "statement": "feedback stabilizes gain"}
                        ),
                        json.dumps([evidence], ensure_ascii=False),
                        "src-fixture",
                        now,
                        now,
                        now,
                    ),
                ],
            )
            db.executemany(
                "INSERT INTO kg_objects_fts(object_id,notebook_id,name) VALUES (?,?,?)",
                [
                    ("ko-fixture-concept", notebook_id, "fixture gain"),
                    ("ko-fixture-claim", notebook_id, "fixture gain remains stable"),
                ],
            )
            db.execute(
                "INSERT INTO knowledge_relations "
                "(id,notebook_id,source_id,source_object_id,target_object_id,edge_type,"
                "evidence,created_at,review_status) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "rel-fixture",
                    notebook_id,
                    "src-fixture",
                    "ko-fixture-concept",
                    "ko-fixture-claim",
                    "supports",
                    json.dumps([evidence], ensure_ascii=False),
                    now,
                    "verified",
                ),
            )
        if not include_source:
            # 造一个**零源**笔记本:三类可枚举集合(元素/知识对象/来源)计数全为零,
            # 那是「本笔记本尚未构建知识图谱」早退真正管的场景。
            # 用「先照常插入、再删掉」而不是把上面整段包进 if:那会把 ~60 行现有
            # 代码整体缩进一级,而这个生成器的守卫对行位移敏感(见 AGENTS 契约夹具
            # 一节)。每个案例都是全新数据库,所以删除与从未插入等价。
            for statement in (
                "DELETE FROM chunks_fts WHERE notebook_id = ?",
                "DELETE FROM chunks WHERE notebook_id = ?",
                "DELETE FROM source_elements WHERE source_id IN "
                "(SELECT id FROM sources WHERE notebook_id = ?)",
                "DELETE FROM sources WHERE notebook_id = ?",
            ):
                db.execute(statement, (notebook_id,))
    return notebook_id


def _normalization_map(values: object) -> dict[str, str]:
    strings: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, str):
            strings.add(value)
        elif isinstance(value, dict):
            for key, item in value.items():
                if key.endswith("_id") or key == "id":
                    visit(item)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(values)
    prefixes = (
        "user", "profile", "nb", "src", "el", "chunk", "ko", "rel",
        "conv", "ans", "askjob", "rep", "shr", "session",
    )
    grouped: dict[str, list[str]] = defaultdict(list)
    for value in strings:
        prefix = value.split("-", 1)[0]
        if "-" in value and prefix in prefixes:
            grouped[prefix].append(value)
    return {
        value: f"<{prefix}-id-{index}>"
        for prefix in sorted(grouped)
        for index, value in enumerate(sorted(set(grouped[prefix])), start=1)
    }


def _normalize_value(value: object, id_map: dict[str, str]) -> object:
    if isinstance(value, bytes):
        return {
            "encoding": "little-endian-float32-blob",
            "size": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }
    if isinstance(value, str):
        if value in id_map:
            return id_map[value]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T.*", value):
            return "<timestamp>"
        if os.path.isabs(value):
            marker = "/storage/"
            suffix = value.split(marker, 1)[1] if marker in value else Path(value).name
            return f"<fixture-storage>/{suffix}"
        stripped = value.strip()
        if stripped[:1] in {"{", "["}:
            try:
                return _normalize_value(json.loads(value), id_map)
            except (TypeError, ValueError):
                pass
        return value
    if isinstance(value, list):
        return [_normalize_value(item, id_map) for item in value]
    if isinstance(value, tuple):
        return [_normalize_value(item, id_map) for item in value]
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, item in value.items():
            if key in {"password_hash", "password_salt"}:
                out[key] = f"<{key}>"
            elif key in {"token", "share_token"} and item:
                out[key] = f"<{key}>"
            elif key.endswith("_at") and item:
                out[key] = "<timestamp>"
            else:
                out[key] = _normalize_value(item, id_map)
        return out
    return value


REPRESENTATIVE_TABLES = (
    "users",
    "user_profiles",
    "auth_sessions",
    "notebooks",
    "notebook_members",
    "sources",
    "source_elements",
    "chunks",
    "element_embeddings",
    "chunk_embeddings",
    "knowledge_objects",
    "knowledge_object_sources",
    "knowledge_relations",
    "knowledge_embeddings",
    "relation_embeddings",
    "conversations",
    "answers",
    "ask_jobs",
    "ask_trace_steps",
    "reports",
    "unified_kg_state",
)


def _rows_for_table(db: sqlite3.Connection, table: str) -> list[dict[str, object]]:
    columns = db.execute(f"PRAGMA table_info({table})").fetchall()
    primary = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
    order = ", ".join(primary) if primary else "rowid"
    return [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY {order}")]


def normalized_repository_snapshot(repo: SQLiteRepository, notebook_id: str) -> dict[str, object]:
    """Return the frozen semantic snapshot without rewriting database rows."""
    from app.models.schemas import UserProfile
    from app.services.sqlite_repository import reset_request_user, set_request_user

    with repo._connect() as db:
        table_rows = {table: _rows_for_table(db, table) for table in REPRESENTATIVE_TABLES}
        schema_rows = db.execute(
            "SELECT type,name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        ).fetchall()
        user_version = db.execute("PRAGMA user_version").fetchone()[0]

    id_map = _normalization_map(table_rows)
    rows = _normalize_value(table_rows, id_map)
    owner = UserProfile(
        id="user-fixture",
        email="a00123456@users.silicon-notebook.local",
        display_name="Fixture Owner",
        role="user",
        username="a00123456",
    )
    token = set_request_user(owner)
    try:
        reads = {
            "notebook": repo.get_notebook(notebook_id).model_dump(mode="json"),
            "sources": [item.model_dump(mode="json") for item in repo.list_sources(notebook_id)],
            "source_detail": repo.get_source("src-fixture").model_dump(mode="json"),
            "source_elements": [
                item.model_dump(mode="json") for item in repo.source_elements("src-fixture")
            ],
            "knowledge_types": [
                item.model_dump(mode="json") for item in repo.knowledge_types(notebook_id)
            ],
            "concepts": repo.list_knowledge(notebook_id, "concept").model_dump(mode="json"),
            "conversation": repo.get_conversation("conv-fixture").model_dump(mode="json"),
            "ask_job": repo.ask_job_detail("askjob-fixture"),
            "report": repo.get_report(notebook_id, "rep-fixture"),
            "sharing": {
                "preview": repo.shared_preview(notebook_id),
                "by_me": repo.shared_by_me("user-fixture"),
            },
        }
        context = {
            "node": repo.node_context(notebook_id, "ko-fixture-concept"),
            "search": repo.search_notebook(notebook_id, "gain").model_dump(mode="json"),
            "source_files": _storage_manifest(Path(repo.storage_dir)),
        }
    finally:
        reset_request_user(token)

    ask_metadata = {
        "conversations": rows["conversations"],
        "answers": rows["answers"],
        "jobs": rows["ask_jobs"],
        "trace": rows["ask_trace_steps"],
    }
    return {
        "schema": {
            "user_version": user_version,
            "objects": [
                {
                    "type": row["type"],
                    "name": row["name"],
                    "sql_sha256": hashlib.sha256((row["sql"] or "").encode()).hexdigest(),
                }
                for row in schema_rows
            ],
        },
        "rows": rows,
        "reads": _normalize_value(reads, id_map),
        "context": _normalize_value(context, id_map),
        "ask_metadata": ask_metadata,
    }


def _seed_v9_rows(repo, final_storage: Path) -> str:
    from app.models.schemas import AskResponse, Citation, ModelError, TraceStep
    from app.domain import auth_utils  # B3: must see the same patched hash_password as _deterministic_runtime

    notebook_id = "nb-fixture"
    source_path = final_storage / "notebooks" / notebook_id / "fixture-source.md"
    source_bytes = (
        b"# Fixture amplifier\n\n"
        b"A source-degenerated stage stabilizes voltage gain.\n\n"
        b"The claim is supported by the concept.\n"
    )
    staged_source = Path(repo.storage_dir) / "notebooks" / notebook_id / source_path.name
    staged_source.parent.mkdir(parents=True, exist_ok=True)
    staged_source.write_bytes(source_bytes)
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    password_hash, password_salt, password_iterations = auth_utils.hash_password(
        "fixture-password"
    )
    evidence = _evidence()
    concept_payload = {
        "name": "source degeneration",
        "definition": "Local feedback that stabilizes amplifier gain.",
        "section_path": "§1 Gain",
    }
    claim_payload = {
        "name": "source degeneration stabilizes gain",
        "statement": "Local feedback reduces gain sensitivity.",
        "section_path": "§1 Gain",
    }
    response = AskResponse(
        answer_id="ans-fixture",
        conclusion="Source degeneration stabilizes gain.",
        answer="Source degeneration stabilizes gain [k1].",
        grounded=True,
        evidence_level="grounded",
        citations=[
            Citation(
                label="Fixture amplifier notes · §1 Gain",
                source_id="src-fixture",
                element_id="el-fixture-0001",
                location_label="§1 Gain",
                quoted_span=str(evidence["quoted_span"]),
            )
        ],
        llm_mode="grounded",
        mode="reasoning",
        conversation_id="conv-fixture",
        retrieval_query="Why is fixture gain stable?",
        top_relevance=0.91,
        reasoning_trace=[
            TraceStep(
                step_type="retrieve",
                summary="Retrieved fixture evidence",
                detail={"found": 2},
                duration_ms=4,
            )
        ],
        model_errors=[
            ModelError(
                stage="rerank",
                model="fixture-rerank",
                message="RuntimeError: deterministic fallback",
            )
        ],
    ).model_dump(mode="json")
    vector_text = json.dumps([1.0, 0.0, 0.5, 0.25])
    vector_blob = sqlite3.Binary(
        __import__("numpy").asarray([0.25, 0.5, 0.75, 1.0], dtype="<f4").tobytes()
    )

    with repo._write() as db:
        db.executemany(
            "INSERT INTO users "
            "(id,email,display_name,role,status,username,password_hash,password_salt,"
            "password_iterations,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "user-fixture",
                    "a00123456@users.silicon-notebook.local",
                    "Fixture Owner",
                    "user",
                    "active",
                    "a00123456",
                    password_hash,
                    password_salt,
                    password_iterations,
                    FIXED_TIME,
                    FIXED_TIME,
                ),
                (
                    "user-reader",
                    "b00654321@users.silicon-notebook.local",
                    "Fixture Reader",
                    "user",
                    "active",
                    "b00654321",
                    password_hash,
                    password_salt,
                    password_iterations,
                    FIXED_TIME,
                    FIXED_TIME,
                ),
            ],
        )
        db.executemany(
            "INSERT INTO user_profiles "
            "(id,user_id,memory_mode,domain_focus,model_settings,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                (
                    "profile-fixture",
                    "user-fixture",
                    "manual",
                    json.dumps(["Analog IC"]),
                    "{}",
                    FIXED_TIME,
                    FIXED_TIME,
                ),
                (
                    "profile-reader",
                    "user-reader",
                    "manual",
                    "[]",
                    "{}",
                    FIXED_TIME,
                    FIXED_TIME,
                ),
            ],
        )
        db.execute(
            "INSERT INTO auth_sessions "
            "(token,user_id,created_at,expires_at,last_seen_at) VALUES (?,?,?,?,?)",
            (
                "session-fixture-token",
                "user-fixture",
                FIXED_TIME,
                FIXED_EXPIRY,
                FIXED_TIME,
            ),
        )
        db.execute(
            "INSERT INTO notebooks "
            "(id,name,purpose,primary_domain,status,created_by,created_at,updated_at,"
            "target_users,expected_questions,source_types,taxonomy,access_scope,template,"
            "purpose_auto,tier,is_shared,share_token) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                notebook_id,
                "Repository contract fixture",
                "Freeze schema-v9 repository composition behavior.",
                "Analog IC",
                "active",
                "user-fixture",
                FIXED_TIME,
                FIXED_TIME,
                "circuit designers",
                json.dumps(["Why is gain stable?"]),
                json.dumps(["markdown"]),
                json.dumps(["amplifier", "feedback"]),
                "private",
                "",
                0,
                "personal",
                1,
                "shr-fixture-token",
            ),
        )
        db.execute(
            "INSERT INTO notebook_members(notebook_id,user_id,role,added_at) VALUES (?,?,?,?)",
            (notebook_id, "user-reader", "reader", FIXED_TIME),
        )
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,parse_status,file_name,file_path,"
            "source_url,file_size,file_hash,summary,error_message,doc_type,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "src-fixture",
                notebook_id,
                "Fixture amplifier notes",
                "markdown",
                "extracted",
                "parsed",
                source_path.name,
                str(source_path),
                "",
                len(source_bytes),
                source_hash,
                "A deterministic mixed-format repository fixture.",
                "",
                "textbook",
                FIXED_TIME,
                FIXED_TIME,
            ),
        )
        db.executemany(
            "INSERT INTO source_elements "
            "(id,source_id,element_type,location_label,text,metadata,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                (
                    "el-fixture-0001",
                    "src-fixture",
                    "paragraph",
                    "§1 Gain",
                    evidence["quoted_span"],
                    json.dumps({"section_path": ["Fixture amplifier", "Gain"]}),
                    FIXED_TIME,
                ),
                (
                    "el-fixture-0002",
                    "src-fixture",
                    "formula",
                    "§2 Formula",
                    "A_v = -g_m R_D / (1 + g_m R_S)",
                    json.dumps({"latex": "A_v=-g_mR_D/(1+g_mR_S)"}),
                    FIXED_TIME,
                ),
            ],
        )
        db.executemany(
            "INSERT INTO chunks "
            "(id,notebook_id,source_id,text,section_path,element_ids,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                (
                    "chunk-fixture-0001",
                    notebook_id,
                    "src-fixture",
                    evidence["quoted_span"],
                    "§1 Gain",
                    json.dumps(["el-fixture-0001"]),
                    FIXED_TIME,
                ),
                (
                    "chunk-fixture-0002",
                    notebook_id,
                    "src-fixture",
                    "A_v = -g_m R_D / (1 + g_m R_S)",
                    "§2 Formula",
                    json.dumps(["el-fixture-0002"]),
                    FIXED_TIME,
                ),
            ],
        )
        db.executemany(
            "INSERT INTO chunks_fts(chunk_id,notebook_id,text) VALUES (?,?,?)",
            [
                ("chunk-fixture-0001", notebook_id, evidence["quoted_span"]),
                ("chunk-fixture-0002", notebook_id, "A_v g_m R_D R_S"),
            ],
        )
        db.executemany(
            "INSERT INTO element_embeddings "
            "(element_id,source_id,notebook_id,vector,created_at) VALUES (?,?,?,?,?)",
            [
                ("el-fixture-0001", "src-fixture", notebook_id, vector_text, FIXED_TIME),
                ("el-fixture-0002", "src-fixture", notebook_id, vector_blob, FIXED_TIME),
            ],
        )
        db.executemany(
            "INSERT INTO chunk_embeddings(chunk_id,notebook_id,vector,created_at) "
            "VALUES (?,?,?,?)",
            [
                ("chunk-fixture-0001", notebook_id, vector_blob, FIXED_TIME),
                ("chunk-fixture-0002", notebook_id, vector_text, FIXED_TIME),
            ],
        )
        db.executemany(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,"
            "created_at,updated_at,last_reviewed) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "ko-fixture-concept",
                    notebook_id,
                    "concept",
                    "approved",
                    "fixture-owner",
                    json.dumps(concept_payload, ensure_ascii=False),
                    json.dumps([evidence], ensure_ascii=False),
                    "src-fixture",
                    FIXED_TIME,
                    FIXED_TIME,
                    FIXED_TIME,
                ),
                (
                    "ko-fixture-claim",
                    notebook_id,
                    "claim",
                    "reviewed",
                    "fixture-owner",
                    json.dumps(claim_payload, ensure_ascii=False),
                    json.dumps([evidence], ensure_ascii=False),
                    "src-fixture",
                    FIXED_TIME,
                    FIXED_TIME,
                    FIXED_TIME,
                ),
            ],
        )
        db.executemany(
            "INSERT INTO knowledge_object_sources(object_id,source_id,notebook_id) "
            "VALUES (?,?,?)",
            [
                ("ko-fixture-concept", "src-fixture", notebook_id),
                ("ko-fixture-claim", "src-fixture", notebook_id),
            ],
        )
        db.executemany(
            "INSERT INTO kg_objects_fts(object_id,notebook_id,name) VALUES (?,?,?)",
            [
                ("ko-fixture-concept", notebook_id, concept_payload["name"]),
                ("ko-fixture-claim", notebook_id, claim_payload["name"]),
            ],
        )
        db.execute(
            "INSERT INTO knowledge_relations "
            "(id,notebook_id,source_id,source_object_id,target_object_id,edge_type,"
            "evidence,created_at,review_status) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "rel-fixture",
                notebook_id,
                "src-fixture",
                "ko-fixture-concept",
                "ko-fixture-claim",
                "supports",
                json.dumps([evidence], ensure_ascii=False),
                FIXED_TIME,
                "verified",
            ),
        )
        db.executemany(
            "INSERT INTO knowledge_embeddings(object_id,notebook_id,vector,created_at) "
            "VALUES (?,?,?,?)",
            [
                ("ko-fixture-concept", notebook_id, vector_blob, FIXED_TIME),
                ("ko-fixture-claim", notebook_id, vector_text, FIXED_TIME),
            ],
        )
        db.execute(
            "INSERT INTO relation_embeddings(relation_id,notebook_id,vector,created_at) "
            "VALUES (?,?,?,?)",
            ("rel-fixture", notebook_id, vector_blob, FIXED_TIME),
        )
        db.execute(
            "INSERT INTO conversations "
            "(id,notebook_id,title,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (
                "conv-fixture",
                notebook_id,
                "Why is gain stable?",
                "user-fixture",
                FIXED_TIME,
                FIXED_TIME,
            ),
        )
        db.execute(
            "INSERT INTO answers(id,notebook_id,question,payload,created_at,conversation_id) "
            "VALUES (?,?,?,?,?,?)",
            (
                "ans-fixture",
                notebook_id,
                "Why is fixture gain stable?",
                json.dumps(response, ensure_ascii=False),
                FIXED_TIME,
                "conv-fixture",
            ),
        )
        db.execute(
            "INSERT INTO ask_jobs "
            "(id,notebook_id,conversation_id,created_by,mode,question,status,trace_json,"
            "answer_id,error,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "askjob-fixture",
                notebook_id,
                "conv-fixture",
                "user-fixture",
                "reasoning",
                "Why is fixture gain stable?",
                "done",
                "",
                "ans-fixture",
                "",
                FIXED_TIME,
                FIXED_TIME,
            ),
        )
        db.execute(
            "INSERT INTO ask_trace_steps(job_id,seq,step_json,created_at) VALUES (?,?,?,?)",
            (
                "askjob-fixture",
                0,
                json.dumps(
                    {
                        "step_type": "retrieve",
                        "summary": "Retrieved fixture evidence",
                        "detail": {"found": 2},
                        "duration_ms": 4,
                    },
                    ensure_ascii=False,
                ),
                FIXED_TIME,
            ),
        )
        db.execute(
            "INSERT INTO reports "
            "(id,notebook_id,question,outline_json,sections_json,gaps_json,references_json,"
            "depth,section_status_json,content_md,status,progress,error,created_by,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "rep-fixture",
                notebook_id,
                "Explain gain stabilization",
                json.dumps([{"title": "Evidence", "goal": "Explain feedback"}]),
                json.dumps([{"title": "Evidence", "content": "Grounded summary."}]),
                json.dumps(["No measured corner data"]),
                json.dumps([{"source_id": "src-fixture", "title": "Fixture notes"}]),
                2,
                json.dumps([{"title": "Evidence", "phase": "完成", "step": 1}]),
                "# Gain stabilization\n\nGrounded summary.",
                "done",
                "complete",
                "",
                "user-fixture",
                FIXED_TIME,
                FIXED_TIME,
            ),
        )
        db.execute(
            "INSERT INTO unified_kg_state "
            "(notebook_id,dirty,kg_mutation_seq,cluster_mutation_seq,cluster_input_version,"
            "last_rebuild_at,object_count,relation_count,cluster_count,updated_at,"
            "community_seq,canonical_rel_seq,mention_seq,source_index_backfilled) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                notebook_id,
                0,
                3,
                1,
                "fixture-cluster-v1",
                FIXED_TIME,
                2,
                1,
                0,
                FIXED_TIME,
                -1,
                -1,
                -1,
                1,
            ),
        )
    return notebook_id


def _normalize_zip_metadata(path: Path) -> None:
    """Rewrite npz ZIP headers with a fixed timestamp for byte-stable hashes."""
    with zipfile.ZipFile(path, "r") as source:
        entries = [
            (info.filename, source.read(info.filename), info.compress_type)
            for info in source.infolist()
        ]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as target:
        for filename, content, compress_type in sorted(entries):
            info = zipfile.ZipInfo(filename, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = compress_type
            info.external_attr = 0o600 << 16
            target.writestr(info, content)
    path.write_bytes(buffer.getvalue())


def _create_index_artifacts(repo, notebook_id: str) -> None:
    import numpy as np
    import scipy.sparse as sp

    from app.services.kg import scale_index, viz_index

    node_ids = ["ko-fixture-concept", "ko-fixture-claim"]
    transition = sp.csr_matrix(
        np.asarray([[0.0, 1.0], [1.0, 0.0]], dtype=np.float32)
    )
    version = repo._scale_index_version(notebook_id)
    scale_dir = Path(repo.storage_dir) / "kg_index" / notebook_id
    scale_index.save_scale_index(
        str(scale_dir),
        node_ids=node_ids,
        transition=transition,
        idf=[1.0, 1.0],
        chunk_index=[0, 1],
        ann_vectors=np.asarray(
            [[1.0, 0.0, 0.5, 0.25], [0.25, 0.5, 0.75, 1.0]],
            dtype=np.float32,
        ),
        ann_labels=node_ids,
        manifest={
            "version": version,
            "built_at": FIXED_TIME,
            "dim": 4,
            "n_nodes": 2,
            "n_edges": 1,
            "n_chunks": 2,
        },
    )
    viz_dir = Path(repo.storage_dir) / "kg_viz" / notebook_id
    viz_index.save_viz_index(
        str(viz_dir),
        viz_ids=node_ids,
        viz_adj=transition,
        viz_deg=np.asarray([1, 1], dtype=np.int32),
        viz_types=["concept", "claim"],
        viz_names=["source degeneration", "source degeneration stabilizes gain"],
        viz_payload={
            "edges": [
                {
                    "from_id": "ko-fixture-concept",
                    "to_id": "ko-fixture-claim",
                    "relation": "supports",
                }
            ]
        },
        manifest={
            "version": version,
            "built_at": FIXED_TIME,
            "n_nodes": 2,
            "n_edges": 1,
        },
    )
    for path in list(scale_dir.glob("*.npz")) + list(viz_dir.glob("*.npz")):
        _normalize_zip_metadata(path)
    assert scale_index.load_scale_index(str(scale_dir)) is not None
    assert viz_index.load_viz_index(str(viz_dir)) is not None


def generate_v9_fixture(output_dir: Path) -> None:
    _assert_baseline_sources(REPO_ROOT)  # --rebaseline only: guarded to SOURCE_COMMIT
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    final_storage = output_dir / "storage"

    with tempfile.TemporaryDirectory(prefix="repository-v9-fixture-") as temporary:
        staging = Path(temporary)
        staging_storage = staging / "storage"
        with _deterministic_runtime():
            repo = _new_offline_repo(staging / "source.db", staging_storage)
            notebook_id = _seed_v9_rows(repo, final_storage)
            _create_index_artifacts(repo, notebook_id)
            snapshot = normalized_repository_snapshot(repo, notebook_id)

            staged_backup = staging / "baseline.db"
            source = sqlite3.connect(repo.db_path)
            destination = sqlite3.connect(staged_backup)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()

        staged_storage_copy = staging / "storage-copy"
        shutil.copytree(staging_storage, staged_storage_copy)
        if final_storage.exists():
            shutil.rmtree(final_storage)
        shutil.move(str(staged_storage_copy), final_storage)

        final_database = output_dir / "baseline.db"
        for suffix in ("-wal", "-shm"):
            Path(f"{final_database}{suffix}").unlink(missing_ok=True)
        staged_backup.replace(final_database)
        snapshot_path = output_dir / "expected_snapshot.json"
        _write_json(snapshot_path, snapshot)
        manifest = {
            "source_commit": SOURCE_COMMIT,
            "schema_version": 9,
            "database": _artifact_manifest(output_dir, final_database),
            "expected_snapshot": _artifact_manifest(output_dir, snapshot_path),
            "storage_files": _storage_manifest(final_storage),
        }
        _write_json(output_dir / "manifest.json", manifest)

    (output_dir / "README.md").write_text(
        """# Repository schema-v9 baseline fixture

`baseline.db` is a frozen schema-v9 database, produced once from pre-refactor
runtime commit `3334626`. The current runtime cannot recreate it, so it is
never rewritten; `main` only replays it to refresh the derived projection.

`baseline.db` uses `sqlite3.Connection.backup()` (no WAL sidecar); `storage/`
holds one source plus minimal scale/viz artifacts; rows are never rewritten.
`expected_snapshot.json`/`manifest.json` are refreshed by replaying the frozen
db through the current code. Regenerate `baseline.db` itself via `--rebaseline`.
""",
        encoding="utf-8",
    )


def _capture_ask_case(repo, notebook_id: str, mode: str, question: str) -> dict[str, object]:
    from app.models.schemas import AskRequest

    response = repo.ask(notebook_id, AskRequest(question=question, mode=mode))
    response_json = response.model_dump(mode="json")
    with repo._connect() as db:
        row = db.execute(
            "SELECT payload FROM answers WHERE id = ?", (response.answer_id,)
        ).fetchone()
    payload = json.loads(row["payload"])
    pair = {"response": response_json, "answers_payload": payload}
    return _normalize_value(pair, _normalization_map(pair))


def collect_ask_goldens() -> dict[str, object]:
    """Replay deterministic Ask cases without writing or baseline guarding."""
    from app.repositories.sqlite import knowledge_counts_cache

    cases: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="repository-ask-goldens-") as temporary:
        root = Path(temporary)
        with _deterministic_runtime():
            # ``include_source`` 区分两种「没有图」:``no_kg`` 是**纯散文库**
            # (有文档、零可枚举元素、零知识对象),它现在照常进循环——文档本身
            # 就是可枚举集合之一;``no_collections`` 是**零源**库,三类计数全为零,
            # 那才是「本笔记本尚未构建知识图谱」早退真正管的场景。两个案例都留着,
            # 因为这一组 golden 声称覆盖「每一条早退」。
            for case_name, mode, question, include_kg, include_source in (
                ("chunk", "chunk", "fixture gain", True, True),
                ("reasoning", "reasoning", "fixture gain", True, True),
                ("graph", "graph", "fixture gain", True, True),
                ("unconfigured_model", "reasoning", "fixture gain", True, True),
                ("no_kg", "reasoning", "fixture gain", False, True),
                ("no_collections", "reasoning", "fixture gain", False, False),
                ("no_hits", "graph", "unrelated-zqxj", True, True),
                ("large_graph_refusal", "graph", "fixture gain", True, True),
            ):
                case_root = root / case_name
                # Every case builds a FRESH database but reuses the same
                # notebook id, and the KG count memo is process-global, keyed on
                # (notebook_id, kg_mutation_seq).  The deterministic runtime
                # makes both parts of that key identical across cases, so
                # without this drop the KG-less case reads the previous case's
                # graph counts — the goldens would then freeze one case's
                # numbers into another's.
                knowledge_counts_cache.invalidate()
                repo = _new_offline_repo(
                    case_root / "fixture.db",
                    case_root / "storage",
                    configured=True,
                    missing_ask_answer=case_name == "unconfigured_model",
                )
                notebook_id = _seed_ask_repository(
                    repo, include_kg=include_kg, include_source=include_source
                )
                # Task 24: the graph engine lives in AskService and consumes the
                # retrieval port directly — seat the replay stubs on the canonical
                # candidate-retrieval owner (facade instance patches no longer sit
                # on the engine's call path). Frozen goldens stay byte-identical.
                if case_name == "no_hits":
                    repo.retrieval.candidates.federated_retrieve = (
                        lambda *_args, **_kwargs: [])
                if case_name == "large_graph_refusal":
                    repo.retrieval.candidates._federated_graph_is_large = (
                        lambda *_args, **_kwargs: True)
                cases[case_name] = _capture_ask_case(
                    repo, notebook_id, mode, question
                )

    return {"source_commit": SOURCE_COMMIT, "cases": cases}


def generate_ask_goldens(output_path: Path) -> None:
    # Living contract: re-blessed against the current runtime (no baseline guard).

    _write_json(output_path, collect_ask_goldens())


def _normalize_api_serialization(
    value: object,
    replacements: dict[str, str],
    temporary_root: Path,
    key: str = "",
) -> object:
    if key in {"answered_at", "created_at", "updated_at"} and isinstance(value, str):
        return FIXED_TIME
    if key == "created_label" and isinstance(value, str):
        return "fixture-date"
    if isinstance(value, dict):
        return {
            item_key: _normalize_api_serialization(
                item_value, replacements, temporary_root, item_key
            )
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [
            _normalize_api_serialization(item, replacements, temporary_root, key)
            for item in value
        ]
    if isinstance(value, str):
        normalized = value.replace(str(temporary_root), "<fixture-root>")
        for actual, stable in sorted(
            replacements.items(), key=lambda item: len(item[0]), reverse=True
        ):
            normalized = normalized.replace(actual, stable)
        return normalized
    return value


def _api_modules_binding_repository() -> list[object]:
    """Every already-imported ``app.api.*`` module holding a ``repository`` name.

    Scans sys.modules rather than importing anything: the caller has already
    imported ``app.api.routes``, which imports every router module, so the set is
    complete by then and this never triggers a fresh import as a side effect.
    Sorted by module name purely so the patch order is deterministic.
    """
    import sys

    found = [
        module
        for name, module in sorted(sys.modules.items())
        if name.startswith("app.api.")
        and module is not None
        and "repository" in vars(module)
    ]
    if not found:  # pragma: no cover — defensive: means the import above changed
        raise RuntimeError(
            "no app.api.* module exposes `repository`; the route composition "
            "changed and _serialization_contract() would silently build its "
            "payloads against the real repository instead of the fixture one"
        )
    return found


def _serialization_contract() -> dict[str, object]:
    """Capture representative payloads from the real repository and routes."""
    from fastapi.testclient import TestClient

    from app import main as app_main
    # Importing `routes` is what pulls every route module into sys.modules —
    # it aggregates all the routers — which is what makes the discovery in
    # _api_modules_binding_repository() below complete.
    from app.api import deps, routes  # noqa: F401 — imported for its import side effect
    from app.core import event_logging
    from app.models.schemas import AskRequest
    from app.services.repository import UploadedSourceFile

    with tempfile.TemporaryDirectory(prefix="repository-api-contract-") as temporary:
        root = Path(temporary)
        repo = _new_offline_repo(root / "api.db", root / "storage")
        repo.settings.viz_sync_build_max_objects = 1

        def fixture_repository():
            return repo

        fixture_repository.cache_info = lambda: SimpleNamespace(currsize=1)
        fixture_repository.cache_clear = lambda: None

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(app_main, "get_settings", lambda: repo.settings))
            stack.enter_context(mock.patch.object(app_main, "repository", fixture_repository))
            stack.enter_context(mock.patch.object(deps, "get_settings", lambda: repo.settings))
            stack.enter_context(mock.patch.object(deps, "repository", fixture_repository))
            # `app.api.routes` used to own a module-level `repository`; it is now a
            # pure router aggregator and the factory lives in app/api/deps.py.
            # Patching `deps` alone does NOT reach the routes: every route module
            # does `from app.api.deps import repository`, which binds the name in
            # that module's own namespace, and nothing resolves it through
            # Depends(repository) (0 such usages vs 140 direct `repository()`
            # calls). So patch each module that actually holds the name, and
            # discover them at runtime rather than hardcoding a list — that is
            # exactly how the old `routes` line silently rotted into a crash when
            # routes.py was split.
            for module in _api_modules_binding_repository():
                stack.enter_context(mock.patch.object(module, "repository", fixture_repository))
            stack.enter_context(
                mock.patch.object(event_logging._archive_pool, "submit", lambda *_a, **_k: None)
            )
            api_app = app_main.create_app()

            with TestClient(api_app) as client:
                created = client.post(
                    "/api/notebooks",
                    json={
                        "name": "API fixture",
                        "purpose": "Freeze serialization",
                        "primary_domain": "Analog IC",
                    },
                )
                created.raise_for_status()
                notebook_id = created.json()["id"]

                source = repo.upload_sources(
                    notebook_id,
                    [
                        UploadedSourceFile(
                            file_name="fixture.md",
                            content_type="text/markdown",
                            content=(
                                b"# Gain\n\nSource degeneration stabilizes fixture gain."
                            ),
                        )
                    ],
                    scheduler=lambda _source_id: None,
                )[0]
                repo.process_source(source.id)
                element = repo.source_elements(source.id)[0]
                evidence = [
                    {
                        "source_id": source.id,
                        "source_title": source.title,
                        "element_id": element.id,
                        "element_type": element.element_type,
                        "location_label": element.location_label,
                        "quoted_span": element.text,
                        "confidence": 0.98,
                    }
                ]
                repo.store_kg(
                    notebook_id,
                    source.id,
                    [
                        {
                            "local_id": "concept",
                            "object_type": "concept",
                            "payload": {
                                "name": "source degeneration",
                                "definition": "Local feedback",
                            },
                            "evidence": evidence,
                        },
                        {
                            "local_id": "claim",
                            "object_type": "claim",
                            "payload": {
                                "name": "Source degeneration stabilizes gain",
                                "statement": "Source degeneration stabilizes gain",
                            },
                            "evidence": evidence,
                        },
                    ],
                    [
                        {
                            "source_local_id": "claim",
                            "target_local_id": "concept",
                            "edge_type": "about",
                            "evidence": evidence,
                        }
                    ],
                )

                ask_response = client.post(
                    f"/api/notebooks/{notebook_id}/ask",
                    json={"question": "fixture gain", "mode": "chunk"},
                )
                ask_response.raise_for_status()
                ask_payload = ask_response.json()
                job_payload = AskRequest(
                    question="trace fixture gain",
                    mode="reasoning",
                    conversation_id=ask_payload["conversation_id"],
                )
                job_id, conversation_id = repo.begin_ask_job(
                    notebook_id, job_payload, "reasoning", threading.Event()
                )
                repo.append_ask_trace(
                    job_id,
                    {
                        "step_type": "retrieve",
                        "summary": "Retrieved one item",
                        "detail": {"found": 1},
                        "duration_ms": 2,
                    },
                )
                repo.finish_ask_job(
                    job_id, "done", answer_id=ask_payload["answer_id"]
                )

                report_id = repo.create_report(
                    notebook_id, "Explain fixture gain", depth=2
                )
                repo.update_report(
                    notebook_id,
                    report_id,
                    status="outline_ready",
                    progress="outline ready",
                    outline=[{"title": "Evidence", "goal": "Explain feedback"}],
                    references=[{"source_id": source.id, "title": source.title}],
                    section_status=[
                        {"title": "Evidence", "phase": "排队", "step": 0}
                    ],
                )

                share_response = client.post(
                    f"/api/notebooks/{notebook_id}/share"
                )
                share_response.raise_for_status()
                share = share_response.json()

                def json_response(method: str, path: str) -> object:
                    response = client.request(method, path)
                    response.raise_for_status()
                    return response.json()

                serialization: dict[str, object] = {
                    "notebook_summary": json_response(
                        "GET", f"/api/notebooks/{notebook_id}"
                    ),
                    "source_detail": json_response(
                        "GET", f"/api/sources/{source.id}"
                    ),
                    "knowledge_page": json_response(
                        "GET",
                        f"/api/notebooks/{notebook_id}/knowledge?type=claim",
                    ),
                    "ask_response": ask_payload,
                    "ask_job_detail": json_response(
                        "GET",
                        f"/api/notebooks/{notebook_id}/ask/jobs/{job_id}",
                    ),
                    "conversation_detail": json_response(
                        "GET", f"/api/conversations/{conversation_id}"
                    ),
                    "report": json_response(
                        "GET",
                        f"/api/notebooks/{notebook_id}/reports/{report_id}",
                    ),
                    "sharing": {
                        "share": share,
                        "preview": json_response(
                            "GET", f"/api/shared/{share['share_token']}"
                        ),
                        "shared_by_me": next(
                            item
                            for item in json_response(
                                "GET", "/api/notebooks/shared-by-me"
                            )
                            if item["id"] == notebook_id
                        ),
                    },
                }
                error_responses = {
                    "http_404": client.get(
                        "/api/notebooks/nb-does-not-exist"
                    ),
                    "validation_422": client.post(
                        f"/api/notebooks/{notebook_id}/ask", json={}
                    ),
                    "graph_too_large_413": client.get(
                        f"/api/notebooks/{notebook_id}/graph"
                    ),
                }
                serialization["errors"] = {
                    name: {
                        "status_code": response.status_code,
                        "body": response.json(),
                    }
                    for name, response in error_responses.items()
                }

        with repo._connect() as db:
            object_ids = [
                row["id"]
                for row in db.execute(
                    "SELECT id FROM knowledge_objects WHERE notebook_id=? "
                    "ORDER BY object_type,id",
                    (notebook_id,),
                ).fetchall()
            ]
        replacements = {
            notebook_id: "nb-api",
            source.id: "src-api",
            element.id: "el-api",
            ask_payload["answer_id"]: "ans-api",
            conversation_id: "conv-api",
            job_id: "askjob-api",
            report_id: "rep-api",
            share["share_token"]: "shr-api",
            **{
                object_id: f"ko-api-{index}"
                for index, object_id in enumerate(object_ids, 1)
            },
        }
        return _normalize_api_serialization(
            serialization, replacements, root
        )


def generate_api_contract(output_path: Path) -> None:
    # Living contract: re-blessed against the current runtime (no baseline guard).
    os.environ.setdefault("ALLOW_NO_ENV_FILE", "1")
    from app.core import readiness
    from app.main import app

    # The readiness gate 503s every app route until warm-up flips ready (tests do
    # this in conftest). As a plain script, mark ready up-front so create_app()'s
    # lifespan serves routes immediately instead of returning 503.
    readiness.mark_ready()

    _write_json(
        output_path,
        {
            "source_commit": SOURCE_COMMIT,
            "openapi": app.openapi(),
            "serialization": _serialization_contract(),
        },
    )


TRANSACTION_PHASES: dict[str, dict[str, object]] = {
    "process_source": {
        "sequence": [
            "set parsing",
            "parse and optional in-memory element enrichment outside transaction",
            "replace elements and source-derived state in one write",
            "set parsed",
            "best-effort chunk build",
            "background embedding",
            "foreground extraction",
            "set extracted",
            "join embedding thread",
            "enqueue existing-index fold",
        ],
        "commit_boundaries": [
            "status transitions are separate writes",
            "element replacement commits before chunk/embed/extraction",
            "chunk, embeddings, extraction and final status are later checkpoints",
        ],
        "failure_boundary": "pipeline errors record failed source state; chunk/embed/dirty hooks fail open",
    },
    "store_kg": {
        "sequence": [
            "preallocate object and relation ids",
            "commit object chunks of 1000",
            "commit relation chunks of 1000",
            "embed objects and relations best effort",
            "invalidate retrieval caches",
            "mark unified KG dirty and bump mutation sequence",
        ],
        "commit_boundaries": [
            "each 1000-object block is a transaction",
            "each 1000-relation block is a transaction",
            "partial source state is an accepted crash window",
        ],
        "failure_boundary": "later chunks and post-commit hooks never roll back already committed chunks",
    },
    "delete_source": {
        "sequence": [
            "load source",
            "clear source-derived KG and embeddings plus delete source in one write",
            "delete local file after commit",
            "invalidate caches",
            "mark unified KG dirty",
        ],
        "commit_boundaries": ["database cleanup is atomic", "filesystem deletion is post-commit"],
        "failure_boundary": "a file-delete failure cannot roll back committed database cleanup",
    },
    "parse_source": {
        "sequence": ["delegate synchronously to process_source", "return final source projection"],
        "commit_boundaries": ["inherits every process_source checkpoint"],
        "failure_boundary": "inherits process_source failed-state recording and best-effort phases",
    },
    "update_knowledge": {
        "sequence": ["update row transaction", "re-embed edited payload best effort", "invalidate cache", "mark dirty"],
        "commit_boundaries": ["row update commits before embedding/invalidation"],
        "failure_boundary": "embedding failure is swallowed; committed row remains and dirty/invalidation still run",
    },
    "merge_knowledge": {
        "sequence": ["merge evidence and deprecate source in one write", "mark dirty", "invalidate cache"],
        "commit_boundaries": ["target evidence, reverse index and source deprecation are atomic"],
        "failure_boundary": "post-commit dirty/cache failure cannot roll back merge",
    },
    "approve_promotion": {
        "sequence": [
            "validate candidate",
            "commit base object/provenance and candidate approval in one transaction",
            "embed fresh base object best effort",
            "invalidate unified cache",
            "mark base dirty",
        ],
        "commit_boundaries": [
            "base mutation and candidate approved state commit together",
            "embedding, invalidation and dirty hooks are post-commit",
        ],
        "failure_boundary": (
            "transaction failure rolls back base mutation and candidate state together; "
            "post-commit hook failure cannot revert approval"
        ),
    },
    "confirm_conflict": {
        "sequence": [
            "load conflict candidate",
            "apply resolution through its independently committed mutation primitive",
            "set candidate applied in a separate transaction",
        ],
        "commit_boundaries": [
            "resolution mutation commits before candidate status transaction",
            "candidate status is a later independent commit",
        ],
        "failure_boundary": (
            "candidate-status failure leaves the already committed resolution mutation applied"
        ),
    },
    "set_edge_review": {
        "sequence": [
            "validate review status",
            "update relation review in one write",
            "mark dirty",
            "invalidate cache",
        ],
        "commit_boundaries": [
            "review row commits before dirty and cache side effects"
        ],
        "failure_boundary": "missing relation raises; post-commit side-effect failure does not revert review",
    },
    "copy_notebook": {
        "sequence": ["sweep only stale own copies", "copy source directory", "insert copying sentinel", "copy each table in configured chunks", "validate counts and references", "publish original status"],
        "commit_boundaries": ["sentinel and every table chunk are separate commits", "filesystem copy precedes database sentinel", "failure compensates only destination"],
        "failure_boundary": "partial destination is removed; source and unrelated live sentinels are untouched",
    },
    "streaming_ask": {
        "sequence": ["begin conversation and running job transaction", "detached retrieval/model outside transaction", "persist trace steps individually", "save answer transaction", "finish job transaction", "failed/cancel cleanup transaction"],
        "commit_boundaries": ["begin, trace, answer and finish are independent short transactions", "answer may exist before job finish after a crash"],
        "failure_boundary": "transport disconnect leaves detached worker running; explicit cancel alone sets cancellation event",
    },
    "migration_recovery_seed": {
        "sequence": ["apply missing versioned migrations serially", "recover running jobs", "seed and upgrade built-ins"],
        "commit_boundaries": ["each migration version is stamped after its migration", "recovery and seed use their existing connection commits"],
        "failure_boundary": "startup completes these serial phases before serving requests; they bypass online mutation hooks",
    },
}


def _mutation_contract(
    semantic_mutation: bool,
    cache_invalidation: bool,
    unified_dirty: bool,
    version_bump: bool,
    index_scheduling: bool,
    exemption: str = "",
) -> dict[str, object]:
    return {
        "semantic_mutation": semantic_mutation,
        "cache_invalidation": cache_invalidation,
        "unified_dirty": unified_dirty,
        "version_bump": version_bump,
        "index_scheduling": index_scheduling,
        "exemption": exemption,
    }


MUTATION_PHASES = {
    "process_source": _mutation_contract(True, True, True, True, True),
    "store_kg": _mutation_contract(True, True, True, True, False),
    "delete_source": _mutation_contract(True, True, True, True, False),
    "parse_source": _mutation_contract(True, True, True, True, True),
    "update_knowledge": _mutation_contract(True, True, True, True, False),
    "merge_knowledge": _mutation_contract(True, True, True, True, False),
    "approve_promotion": _mutation_contract(True, True, True, True, False),
    "confirm_conflict": _mutation_contract(True, True, True, True, False),
    "set_edge_review": _mutation_contract(True, True, True, True, False),
    "copy_notebook": _mutation_contract(
        True,
        False,
        False,
        False,
        False,
        "deep copy preserves source versions and must not add dirty/version side effects",
    ),
    "streaming_ask": _mutation_contract(
        False,
        False,
        False,
        False,
        False,
        "Ask state is durable application state, not a KG semantic mutation",
    ),
    "migration_recovery_seed": _mutation_contract(
        True,
        False,
        False,
        False,
        False,
        "startup fixture/migration/recovery/seed writes bypass the online mutation coordinator",
    ),
}


ERROR_POLICIES = {
    "append_ask_trace": {
        "policy": "best_effort",
        "observable_result": "log exception and keep Ask worker running; missing job is a no-op",
    },
    "report_corpus_map": {
        "policy": "best_effort",
        "observable_result": "individual map failures become section/report diagnostics without aborting unrelated maps",
    },
    "model_error_recording": {
        "policy": "record_and_continue",
        "observable_result": "emit structured model_error and append request-local Ask model_errors when a sink exists",
    },
    "update_report_missing": {
        "policy": "return_none",
        "observable_result": "zero-row UPDATE returns normally and creates no report",
    },
    "delete_report_missing": {
        "policy": "return_none",
        "observable_result": "zero-row DELETE returns normally",
    },
    "source_chunk_build": {
        "policy": "best_effort",
        "observable_result": "log chunk-build failure and continue embedding/extraction pipeline",
    },
    "source_embedding": {
        "policy": "best_effort",
        "observable_result": "background failure is logged and does not prevent extracted status",
    },
    "source_extraction": {
        "policy": "record_and_continue",
        "observable_result": "pipeline exception records source failed status, summary and error_message",
    },
}


def _write_phase_contracts(output_dir: Path) -> None:
    _write_json(output_dir / "transaction_phases.json", TRANSACTION_PHASES)
    _write_json(output_dir / "mutation_phases.json", MUTATION_PHASES)
    _write_json(output_dir / "error_policies.json", ERROR_POLICIES)


def refresh_v9_snapshot(output_dir: Path) -> None:
    """Refresh the living v9 projection by replaying the frozen ``baseline.db``.

    The committed schema-v9 ``baseline.db`` and its ``storage/`` are read-only
    ground truth: they are copied to a scratch dir, opened by the current runtime
    (which applies migrations up to the live schema), and the normalized snapshot
    is written to ``expected_snapshot.json``. ``manifest.json`` is refreshed too,
    but its ``database`` hash still covers the untouched frozen db — only the
    ``expected_snapshot`` hash moves. ``baseline.db`` is never rewritten.
    """
    output_dir = output_dir.resolve()
    final_database = output_dir / "baseline.db"
    final_storage = output_dir / "storage"
    if not final_database.is_file():
        raise SystemExit(
            f"missing frozen v9 baseline: {final_database} "
            "(use --rebaseline from the SOURCE_COMMIT checkout to create it)"
        )
    with tempfile.TemporaryDirectory(prefix="repository-v9-refresh-") as temporary:
        staging = Path(temporary)
        database = staging / "baseline.db"
        storage = staging / "storage"
        shutil.copyfile(final_database, database)
        shutil.copytree(final_storage, storage)
        with _deterministic_runtime():
            repo = _new_offline_repo(database, storage)
            snapshot = normalized_repository_snapshot(repo, "nb-fixture")
    snapshot_path = output_dir / "expected_snapshot.json"
    _write_json(snapshot_path, snapshot)
    manifest = {
        "source_commit": SOURCE_COMMIT,
        "schema_version": 9,
        "database": _artifact_manifest(output_dir, final_database),
        "expected_snapshot": _artifact_manifest(output_dir, snapshot_path),
        "storage_files": _storage_manifest(final_storage),
    }
    _write_json(output_dir / "manifest.json", manifest)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--fixtures-root",
        type=Path,
        default=REPO_ROOT / "backend" / "tests" / "fixtures",
        help="fixture root (default: backend/tests/fixtures)",
    )
    parser.add_argument(
        "--rebaseline",
        action="store_true",
        help=(
            "also regenerate the frozen-at-baseline artifacts (facade_surface.json "
            "and repository_v9/baseline.db). Guarded to SOURCE_COMMIT, so it only "
            "runs from an untouched baseline checkout; omit for the normal workflow."
        ),
    )
    parser.add_argument(
        "--rebaseline-surface",
        action="store_true",
        help=(
            "regenerate only the current semantic facade_surface.json contract; "
            "never touches the frozen repository_v9 database or storage"
        ),
    )
    parser.add_argument(
        "--rebaseline-callers",
        action="store_true",
        help=(
            "regenerate only the semantic repository caller-boundary contract; "
            "never touches runtime or frozen repository_v9 artifacts"
        ),
    )
    args = parser.parse_args(argv)
    selected_rebaseline_modes = sum(
        bool(value)
        for value in (
            args.rebaseline,
            args.rebaseline_surface,
            args.rebaseline_callers,
        )
    )
    if selected_rebaseline_modes > 1:
        parser.error("choose only one rebaseline mode")

    contract_dir = args.fixtures_root / "repository_contract"
    v9_dir = args.fixtures_root / "repository_v9"
    contract_dir.mkdir(parents=True, exist_ok=True)

    if args.rebaseline_callers:
        from tests.architecture.repository_callers import collect_caller_contract

        _write_json(
            contract_dir / "caller_boundaries.json",
            collect_caller_contract(),
        )
        return 0

    if args.rebaseline_surface:
        surface = collect_facade_surface(
            owner_by_member=_owners_at_commit(SURFACE_SOURCE_COMMIT),
        )
        _write_json(contract_dir / "facade_surface.json", surface)
        write_ownership_surface(
            REPO_ROOT / "backend" / "app" / "repositories" / "ownership_manifest.py",
            surface,
        )
        return 0

    # Living characterization contracts: re-blessed against the current runtime.
    _write_phase_contracts(contract_dir)
    generate_ask_goldens(contract_dir / "ask_responses.json")
    generate_api_contract(contract_dir / "api_contract.json")

    if args.rebaseline:
        # Frozen-at-baseline artifacts: only valid from the SOURCE_COMMIT checkout.
        # facade_surface.json is otherwise maintained via the surface-manifest
        # allowlists, and baseline.db cannot be reproduced by the current runtime.
        _assert_baseline_sources(REPO_ROOT)
        _write_json(contract_dir / "facade_surface.json", collect_facade_surface())
        generate_v9_fixture(v9_dir)
    else:
        # baseline.db stays frozen; only its derived projection is refreshed.
        refresh_v9_snapshot(v9_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
