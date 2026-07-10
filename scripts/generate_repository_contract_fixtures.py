#!/usr/bin/env python3
"""Generate immutable Repository composition-refactor contract fixtures.

This script is deliberately tied to the pre-refactor runtime at ``3334626``.
It is a one-way characterization tool, not a general fixture factory: once any
``backend/app/**/*.py`` path or byte changes, regeneration is refused.
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
import time
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterator, Sequence
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.sqlite_repository import SQLiteRepository

SOURCE_COMMIT = "3334626"
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


def _literal_target(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.rsplit(".", 1)[-1]
    return ""


def _owner_for(name: str) -> str:
    identity = {
        "current_user", "create_user", "authenticate_user", "create_session",
        "resolve_session", "delete_session", "get_user_model_settings",
        "set_user_model_settings", "resolve_model_config", "list_user_usage",
        "list_user_notebooks",
    }
    provider = {
        "llm_client", "reasoning_llm_client", "rewrite_llm_client",
        "kg_llm_client", "rerank_client", "_note_model_error",
    }
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
    if name in {"_connect", "_write", "_migrate", "_seed", "SCHEMA_VERSION"}:
        return "SqliteDatabase"
    return "QueryStore"


def _patch_compatibility(name: str, kind: str) -> str:
    if kind in {"mutable_property", "constant"} or name in {
        "_now", "_new_id", "_COPY_CHUNK", "llm_client", "rerank_client",
    }:
        return "production-compatible"
    return "test-only"


def collect_facade_surface() -> dict[str, dict[str, object]]:
    """Collect the consumer-visible facade and compatibility patch surface."""
    from app.services import sqlite_repository as module
    from app.services.ask_modes import ASK_MODES
    from app.services.sqlite_repository import SQLiteRepository

    class_members: dict[str, object] = {}
    for cls in reversed(SQLiteRepository.__mro__[:-1]):
        for name, value in cls.__dict__.items():
            if isinstance(value, (property, staticmethod, classmethod)) or inspect.isfunction(value):
                class_members[name] = value

    source_tree = ast.parse(
        (REPO_ROOT / "backend/app/services/sqlite_repository.py").read_text(
            encoding="utf-8"
        )
    )
    instance_attributes = {
        node.targets[0].attr
        for node in ast.walk(source_tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Attribute)
        and isinstance(node.targets[0].value, ast.Name)
        and node.targets[0].value.id == "self"
    }
    instance_attributes |= {
        node.target.attr
        for node in ast.walk(source_tree)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Attribute)
        and isinstance(node.target.value, ast.Name)
        and node.target.value.id == "self"
    }

    module_candidates = {
        name
        for name, value in vars(module).items()
        if name in {"SCHEMA_VERSION", "USABLE_STATUSES", "KNOWLEDGE_STATUSES", "_COPY_CHUNK"}
        or (name.startswith("_") and (inspect.isfunction(value) or inspect.isclass(value)))
    }
    candidate_names = set(class_members) | instance_attributes | module_candidates
    consumers: dict[str, set[str]] = defaultdict(set)
    patches: dict[str, list[dict[str, object]]] = defaultdict(list)

    for path in _iter_python_files():
        relative = str(path.relative_to(REPO_ROOT))
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (SyntaxError, UnicodeDecodeError):
            continue
        module_aliases = {"sqlite_repository"}
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
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in candidate_names:
                consumers[node.attr].add(f"{relative}:{node.lineno}")
            elif isinstance(node, (ast.ImportFrom,)) and (
                node.module or ""
            ).endswith("sqlite_repository"):
                for alias in node.names:
                    if alias.name in candidate_names:
                        consumers[alias.name].add(f"{relative}:{node.lineno}")

            if not isinstance(node, ast.Call):
                continue
            call_name = _dotted_name(node.func)
            target_name = ""
            target_base = ""
            if call_name.endswith("monkeypatch.setattr") and len(node.args) >= 2:
                target_name = _literal_target(node.args[1]) or _literal_target(node.args[0])
                target_base = _dotted_name(node.args[0])
            elif call_name.endswith("patch.object") and len(node.args) >= 2:
                target_name = _literal_target(node.args[1])
                target_base = _dotted_name(node.args[0])
            if not target_name:
                continue
            module_target = target_base.split(".", 1)[0] in module_aliases
            module_only = (
                target_name in module_candidates
                and target_name not in class_members
                and target_name not in instance_attributes
            )
            if module_only and not module_target:
                continue
            if (
                target_name not in candidate_names
                and module_target
                and hasattr(module, target_name)
            ):
                candidate_names.add(target_name)
                module_candidates.add(target_name)
            if target_name not in candidate_names:
                continue
            consumers[target_name].add(f"{relative}:{node.lineno}")
            value = class_members.get(target_name, getattr(module, target_name, None))
            patch_kind = (
                "mutable_property"
                if isinstance(value, property) and value.fset is not None
                else "constant"
                if not callable(value)
                else "private_wrapper"
                if target_name.startswith("_")
                else "method"
            )
            patches[target_name].append(
                {
                    "file": relative,
                    "line": node.lineno,
                    "target": target_name,
                    "compatibility": _patch_compatibility(target_name, patch_kind),
                }
            )

    for spec in ASK_MODES.values():
        consumers[spec.handler].add("ASK_MODES[*].handler")

    surface: dict[str, dict[str, object]] = {}
    for name in sorted(candidate_names):
        if not consumers.get(name):
            continue
        scope = "facade"
        signature = ""
        value = class_members.get(name)
        if isinstance(value, property):
            kind = "mutable_property" if value.fset is not None else "property"
            signature = str(inspect.signature(value.fget))
        elif value is not None:
            raw = value.__func__ if isinstance(value, (staticmethod, classmethod)) else value
            kind = "private_wrapper" if name.startswith("_") else "method"
            signature = str(inspect.signature(raw))
        elif name in instance_attributes and not hasattr(module, name):
            kind = "instance_attribute"
            signature = "<instance attribute>"
        else:
            scope = "module"
            value = getattr(module, name)
            if callable(value):
                kind = "private_wrapper" if name.startswith("_") else "method"
                try:
                    signature = str(inspect.signature(value))
                except (TypeError, ValueError):
                    signature = "<opaque callable>"
            else:
                kind = "constant"
                signature = type(value).__name__
        surface[name] = {
            "kind": kind,
            "scope": scope,
            "signature": signature,
            "consumers": sorted(consumers[name]),
            "owner": _owner_for(name),
            "patch_targets": sorted(
                patches.get(name, []), key=lambda item: (item["file"], item["line"])
            ),
        }
    return surface


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
    from app.services import auth_utils
    from app.services import embedding
    from app.services import rerank_client
    from app.services import sqlite_identity
    from app.services import sqlite_notebook_sharing
    from app.services import sqlite_repository

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
        stack.enter_context(mock.patch.object(sqlite_notebook_sharing, "_now", lambda: FIXED_TIME))
        stack.enter_context(mock.patch.object(auth_utils, "hash_password", fixed_hash))
        stack.enter_context(
            mock.patch.object(sqlite_repository, "OpenAICompatibleClient", _FakeChatAdapter)
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
        stack.enter_context(mock.patch.object(time, "perf_counter", fixed_perf))
        stack.enter_context(mock.patch.object(socket, "create_connection", no_network))
        stack.enter_context(mock.patch.object(socket.socket, "connect", no_network))
        yield


def _offline_settings(database: Path, storage: Path, *, required: bool = False):
    from app.core.config import Settings

    return Settings(
        database_url=f"sqlite:///{database}",
        storage_dir=str(storage),
        openai_compat_base_url="",
        openai_compat_api_key="",
        openai_compat_model="",
        reasoning_llm_base_url="",
        reasoning_llm_api_key="",
        reasoning_llm_model="",
        rewrite_llm_base_url="",
        rewrite_llm_api_key="",
        rewrite_llm_model="",
        kg_llm_base_url="",
        kg_llm_api_key="",
        kg_llm_model="",
        embed_provider="",
        embed_model="",
        embed_base_url="",
        embed_api_key="",
        embed_dim=4,
        rerank_model="",
        rerank_base_url="",
        rerank_api_key="",
        mineru_mode="off",
        mineru_api_url="",
        mineru_vlm_server_url="",
        mineru_api_token="",
        scale_index_auto_enabled=False,
        event_log_enabled=False,
        llm_log_enabled=False,
        debug_logs_enabled=False,
        auth_optional=True,
        user_model_config_policy="required" if required else "fallback",
        query_rewrite_enabled=False,
        chunk_kg_overlay_enabled=False,
        graph_ppr_enabled=False,
        kg_query_refine_enabled=False,
        reasoning_ppr_prefetch=False,
        community_layer_enabled=False,
        mention_bridge_enabled=False,
    )


def _new_offline_repo(database: Path, storage: Path, *, required: bool = False):
    from app.services.sqlite_repository import SQLiteRepository

    return SQLiteRepository(_offline_settings(database, storage, required=required))


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


def _seed_ask_repository(repo, *, include_kg: bool = True) -> str:
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
    from app.services import auth_utils

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
    _assert_baseline_sources(REPO_ROOT)
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

Generated once from runtime commit `3334626` by
`scripts/generate_repository_contract_fixtures.py`.

`baseline.db` is written with `sqlite3.Connection.backup()` and needs no WAL
sidecar. `storage/` contains one source file plus minimal loadable scale/viz
artifacts. IDs, timestamps, credentials, tokens, and absolute fixture paths are
normalized only in `expected_snapshot.json`; database rows remain untouched.
Regeneration is refused after any `backend/app/**/*.py` byte or path changes.
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
    cases: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="repository-ask-goldens-") as temporary:
        root = Path(temporary)
        with _deterministic_runtime():
            for case_name, mode, question, include_kg, required in (
                ("chunk", "chunk", "fixture gain", True, False),
                ("reasoning", "reasoning", "fixture gain", True, False),
                ("graph", "graph", "fixture gain", True, False),
                ("unconfigured_model", "reasoning", "fixture gain", True, True),
                ("no_kg", "reasoning", "fixture gain", False, False),
                ("no_hits", "graph", "unrelated-zqxj", True, False),
                ("large_graph_refusal", "graph", "fixture gain", True, False),
            ):
                case_root = root / case_name
                repo = _new_offline_repo(
                    case_root / "fixture.db", case_root / "storage", required=required
                )
                notebook_id = _seed_ask_repository(repo, include_kg=include_kg)
                if case_name == "no_hits":
                    repo.federated_retrieve = lambda *_args, **_kwargs: []
                if case_name == "large_graph_refusal":
                    repo._federated_graph_is_large = lambda *_args, **_kwargs: True
                cases[case_name] = _capture_ask_case(
                    repo, notebook_id, mode, question
                )

    return {"source_commit": SOURCE_COMMIT, "cases": cases}


def generate_ask_goldens(output_path: Path) -> None:
    _assert_baseline_sources(REPO_ROOT)

    _write_json(output_path, collect_ask_goldens())


def _serialization_contract() -> dict[str, object]:
    from app.models.schemas import (
        AskResponse,
        Citation,
        ConversationDetail,
        ConversationTurn,
        Evidence,
        KnowledgeFieldValue,
        KnowledgeRecord,
        NotebookSummary,
        PaginatedKnowledge,
        ReportDetail,
        ShareResponse,
        SharedByMeItem,
        SharedPreview,
        SourceDetail,
        TraceStep,
    )

    evidence = Evidence(**_evidence())
    response = AskResponse(
        answer_id="ans-api",
        conclusion="Fixture conclusion.",
        answer="Fixture conclusion [k1].",
        grounded=True,
        evidence_level="grounded",
        citations=[
            Citation(
                label="Fixture amplifier notes · §1 Gain",
                source_id="src-api",
                element_id="el-api",
                location_label="§1 Gain",
                quoted_span="Fixture evidence.",
            )
        ],
        llm_mode="grounded",
        mode="reasoning",
        conversation_id="conv-api",
        retrieval_query="fixture gain",
        top_relevance=0.9,
        reasoning_trace=[
            TraceStep(
                step_type="retrieve",
                summary="Retrieved one item",
                detail={"found": 1},
                duration_ms=2,
            )
        ],
    )
    return {
        "notebook_summary": NotebookSummary(
            id="nb-api",
            name="API fixture",
            purpose="Freeze serialization",
            primary_domain="Analog IC",
            status="active",
            counts={"sources": 1, "concepts": 1, "claims": 1},
        ).model_dump(mode="json"),
        "source_detail": SourceDetail(
            id="src-api",
            notebook_id="nb-api",
            title="Fixture source",
            type="markdown",
            status="extracted",
            summary="Fixture summary",
            element_count=1,
            file_name="fixture.md",
            file_size=64,
            file_hash="abc123",
            parse_status="parsed",
            file_path="/fixture/storage/fixture.md",
        ).model_dump(mode="json"),
        "knowledge_page": PaginatedKnowledge(
            items=[
                KnowledgeRecord(
                    id="ko-api",
                    object_type="concept",
                    headline="source degeneration",
                    fields=[
                        KnowledgeFieldValue(key="definition", value="Local feedback")
                    ],
                    status="approved",
                    evidence=[evidence],
                )
            ],
            total_count=1,
            offset=0,
            limit=50,
        ).model_dump(mode="json"),
        "ask_job_detail": {
            "job_id": "askjob-api",
            "notebook_id": "nb-api",
            "conversation_id": "conv-api",
            "created_by": "user-api",
            "mode": "reasoning",
            "question": "fixture gain",
            "status": "done",
            "trace": [
                {
                    "step_type": "retrieve",
                    "summary": "Retrieved one item",
                    "detail": {"found": 1},
                    "duration_ms": 2,
                }
            ],
            "answer_id": "ans-api",
            "error": "",
        },
        "conversation_detail": ConversationDetail(
            id="conv-api",
            notebook_id="nb-api",
            title="fixture gain",
            updated_at=FIXED_TIME,
            turn_count=1,
            used_reasoning=True,
            turns=[
                ConversationTurn(
                    answer_id="ans-api",
                    question="fixture gain",
                    response=response,
                    created_at=FIXED_TIME,
                )
            ],
        ).model_dump(mode="json"),
        "report": ReportDetail(
            id="rep-api",
            question="Explain fixture gain",
            status="outline_ready",
            progress="outline ready",
            section_count=1,
            created_at=FIXED_TIME,
            created_by="user-api",
            outline=[{"title": "Evidence", "goal": "Explain feedback"}],
            sections=[],
            gaps=[],
            references=[{"source_id": "src-api", "title": "Fixture source"}],
            depth=2,
            section_status=[{"title": "Evidence", "phase": "排队", "step": 0}],
        ).model_dump(mode="json"),
        "sharing": {
            "share": ShareResponse(
                share_token="shr-api", copyable=True,
                size={"bytes": 64, "sources": 1, "chunks": 1, "nodes": 2, "edges": 1},
            ).model_dump(mode="json"),
            "preview": SharedPreview(
                name="API fixture", owner_display="a00123456", source_count=1,
                node_count=2, edge_count=1, source_titles=["Fixture source"],
                mode="copy",
                size={"bytes": 64, "sources": 1, "chunks": 1, "nodes": 2, "edges": 1},
            ).model_dump(mode="json"),
            "shared_by_me": SharedByMeItem(
                id="nb-api", name="API fixture", share_token="shr-api", mode="copy",
                size={"bytes": 64, "sources": 1, "chunks": 1, "nodes": 2, "edges": 1},
                members=[],
            ).model_dump(mode="json"),
        },
        "errors": {
            "http_404": {"detail": "Notebook not found"},
            "validation_422": {
                "detail": [
                    {
                        "type": "missing",
                        "loc": ["body", "question"],
                        "msg": "Field required",
                        "input": {},
                    }
                ]
            },
            "graph_too_large_413": {
                "detail": "Knowledge graph too large; use /unified-kg"
            },
        },
    }


def generate_api_contract(output_path: Path) -> None:
    _assert_baseline_sources(REPO_ROOT)
    os.environ.setdefault("ALLOW_NO_ENV_FILE", "1")
    from app.main import app

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
            "parse outside transaction",
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
        "sequence": ["validate candidate", "insert or merge base object transaction", "embed best effort", "mark base dirty", "finish promotion state"],
        "commit_boundaries": ["promotion mutation is transactional", "embedding and dirty hooks are post-commit"],
        "failure_boundary": "post-commit embedding is fail-open; state-machine errors still raise",
    },
    "confirm_conflict": {
        "sequence": ["load conflict candidate", "apply status/object mutation transaction", "embed best effort", "mark dirty"],
        "commit_boundaries": ["conflict row/object changes commit together", "embedding and dirty are later"],
        "failure_boundary": "post-mutation embedding failure does not revert confirmed conflict",
    },
    "set_edge_review": {
        "sequence": ["validate review status", "update relation review in one write", "invalidate cache", "mark dirty"],
        "commit_boundaries": ["review row commits before cache/version side effects"],
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixtures-root",
        type=Path,
        default=REPO_ROOT / "backend" / "tests" / "fixtures",
        help="fixture root (default: backend/tests/fixtures)",
    )
    args = parser.parse_args(argv)
    _assert_baseline_sources(REPO_ROOT)

    contract_dir = args.fixtures_root / "repository_contract"
    v9_dir = args.fixtures_root / "repository_v9"
    contract_dir.mkdir(parents=True, exist_ok=True)
    _write_json(contract_dir / "facade_surface.json", collect_facade_surface())
    _write_phase_contracts(contract_dir)
    generate_ask_goldens(contract_dir / "ask_responses.json")
    generate_api_contract(contract_dir / "api_contract.json")
    generate_v9_fixture(v9_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
