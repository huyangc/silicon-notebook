"""Cross-cutting architecture invariants introduced by the hardening pass."""
from __future__ import annotations

import ast
import contextvars
from pathlib import Path
import threading
import time

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository


ROOT = Path(__file__).resolve().parents[2]
RAW_TRANSPORT_FILES = {
    "backend/app/core/llm.py",
    "backend/app/services/embedding_dashscope.py",
    "backend/app/services/rerank_client.py",
    "backend/app/services/model_provider.py",
}
OFFLINE_KG_TRANSPORT = "backend/app/services/kg/client.py"
OFFLINE_KG_CALLERS = {
    "backend/app/eval/inference.py",
    "backend/app/scripts/gen_recall_gold.py",
    "scripts/kg_goldgen.py",
    "scripts/kg_goldgen_all.py",
}
RETIRED_MODEL_SYMBOLS = {
    "USER_MODEL_CONFIG_POLICY",
    "user_model_config_policy",
    "LimitedJsonChatClient",
    "activate_model_concurrency",
    "KG_EXTRACT_WORKERS",
    "EMBED_CONCURRENCY",
    "KG_ASK_RESERVE",
}
LEGACY_MODEL_MIGRATION_FILE = "scripts/migrate_legacy_model_env.py"
LEGACY_MODEL_MIGRATION_ENV_SYMBOLS = {
    "USER_MODEL_CONFIG_POLICY",
    "KG_EXTRACT_WORKERS",
    "EMBED_CONCURRENCY",
    "KG_ASK_RESERVE",
}
RETIRED_REPOSITORY_CLIENT_ATTRS = {
    "llm_client",
    "reasoning_llm_client",
    "rewrite_llm_client",
    "kg_llm_client",
    "rerank_client",
    "embedder",
}


def _python_sources(*roots: str):
    for root in roots:
        for path in sorted((ROOT / root).rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path, path.relative_to(ROOT).as_posix()


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _imports_kg_client(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "app.services.kg.client":
            return True
        if isinstance(node, ast.Import):
            if any(alias.name == "app.services.kg.client" for alias in node.names):
                return True
    return False


def _inside_attribute_error_assertion(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, ast.With):
            for item in current.items:
                expr = item.context_expr
                if (
                    isinstance(expr, ast.Call)
                    and _dotted(expr.func) == "pytest.raises"
                    and expr.args
                    and _dotted(expr.args[0]) == "AttributeError"
                ):
                    return True
        current = parents.get(current)
    return False


def _settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 't.db'}",
        storage_dir=str(tmp_path / "storage"),
        event_log_enabled=False,
        llm_log_enabled=False,
        auth_optional=True,
    )


def test_raw_model_transports_are_confined_to_reviewed_boundaries():
    """Raw SDK construction/calls must never bypass the runtime scheduler."""
    offenders: list[str] = []
    raw_constructors = {
        "OpenAICompatibleClient",
        "DashscopeEmbedder",
        "RerankClient",
    }
    for path, relative in _python_sources("backend/app", "scripts"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = _dotted(node.func)
            short = callee.rsplit(".", 1)[-1]
            is_raw = (
                short in raw_constructors
                or callee.endswith(".embeddings.create")
                or short == "_rerank_batch"
            )
            if is_raw and relative not in RAW_TRANSPORT_FILES:
                offenders.append(f"{relative}:{node.lineno}:{callee}")

            if (
                short == "chat_json"
                and isinstance(node.func, ast.Attribute)
                and node.func.value.__class__ is ast.Attribute
                and node.func.value.attr in RETIRED_REPOSITORY_CLIENT_ATTRS
            ):
                offenders.append(f"{relative}:{node.lineno}:unbound-{callee}")

    assert offenders == []


def test_offline_kg_transport_has_no_product_runtime_importers():
    importers: set[str] = set()
    prefixes: dict[str, set[str]] = {}
    for path, relative in _python_sources("backend/app", "scripts"):
        if relative == OFFLINE_KG_TRANSPORT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        if _imports_kg_client(tree):
            importers.add(relative)
            prefixes[relative] = {
                str(node.args[0].value)
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and _dotted(node.func).rsplit(".", 1)[-1] == "make_client"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            }

    assert importers == OFFLINE_KG_CALLERS
    assert prefixes == {
        "backend/app/eval/inference.py": {"EVAL_JUDGE_"},
        "backend/app/scripts/gen_recall_gold.py": {"GOLDGEN_"},
        "scripts/kg_goldgen.py": {"GOLDGEN_"},
        "scripts/kg_goldgen_all.py": {"GOLDGEN_"},
    }


def test_retired_model_configuration_and_gate_symbols_are_absent():
    offenders: list[str] = []
    retired_routes = {"/me/model-settings", "/me/model-services"}
    for path, relative in _python_sources("backend/app", "frontend/app", "scripts"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except SyntaxError:
            # TypeScript/TSX is covered by the front-end architecture contract.
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in RETIRED_MODEL_SYMBOLS:
                offenders.append(f"{relative}:{node.lineno}:{node.id}")
            elif isinstance(node, ast.Attribute) and node.attr in RETIRED_MODEL_SYMBOLS:
                offenders.append(f"{relative}:{node.lineno}:{node.attr}")
            elif isinstance(node, ast.keyword) and node.arg in RETIRED_MODEL_SYMBOLS:
                offenders.append(f"{relative}:{node.lineno}:{node.arg}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                for symbol in RETIRED_MODEL_SYMBOLS | retired_routes:
                    allowed_migration_input = (
                        relative == LEGACY_MODEL_MIGRATION_FILE
                        and symbol in LEGACY_MODEL_MIGRATION_ENV_SYMBOLS
                    )
                    if symbol in node.value and not allowed_migration_input:
                        offenders.append(f"{relative}:{node.lineno}:{symbol}")
    assert offenders == []


def _retired_model_attribute_offenders(tree: ast.AST, relative: str) -> list[str]:
    offenders: list[str] = []
    retired_clients = RETIRED_REPOSITORY_CLIENT_ATTRS - {"embedder"}
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in retired_clients
        ):
            offenders.append(f"{relative}:{node.lineno}:def-{node.name}")
            continue

        if isinstance(node, ast.Attribute):
            receiver = _dotted(node.value)
            direct_repository_receiver = (
                "." not in receiver
                and (
                    receiver in {"r", "repo", "repository", "_repo"}
                    or receiver.endswith("repo")
                    or receiver.endswith("repository")
                )
            )
            if (
                (
                    node.attr in retired_clients
                    or (node.attr == "embedder" and direct_repository_receiver)
                )
                and not _inside_attribute_error_assertion(node, parents)
            ):
                offenders.append(
                    f"{relative}:{node.lineno}:attribute-{receiver}.{node.attr}"
                )
            continue

        if (
            isinstance(node, ast.Call)
            and _dotted(node.func) == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
            and node.args[1].value in RETIRED_REPOSITORY_CLIENT_ATTRS
        ):
            receiver = _dotted(node.args[0])
            repository_receiver = any(
                part in {"r", "repo", "repository", "_repo"}
                or part.endswith("repo")
                or part.endswith("repository")
                or part in {"models", "model_clients", "provider"}
                for part in receiver.split(".")
            )
            if (
                repository_receiver
                and not _inside_attribute_error_assertion(node, parents)
            ):
                offenders.append(
                    f"{relative}:{node.lineno}:getattr-{receiver}.{node.args[1].value}"
                )
    return offenders


def test_retired_repository_model_attributes_cannot_be_read_or_rebound():
    offenders: list[str] = []
    for path, relative in _python_sources("backend/app", "backend/tests", "scripts"):
        if relative == "backend/tests/model_testkit.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        offenders.extend(_retired_model_attribute_offenders(tree, relative))

    assert offenders == []


def test_retired_model_attribute_guard_detects_direct_and_two_step_bypasses():
    tree = ast.parse(
        """
answer = repo.llm_client.chat_json([])
provider = repo._runtime.models
reranker = provider.rerank_client
hidden = getattr(provider, "kg_llm_client")
vector = repository.embedder.embed_query("q")
"""
    )

    offenders = _retired_model_attribute_offenders(tree, "negative.py")

    assert len(offenders) == 4
    assert any("repo.llm_client" in offender for offender in offenders)
    assert any("provider.rerank_client" in offender for offender in offenders)
    assert any("getattr-provider.kg_llm_client" in offender for offender in offenders)
    assert any("repository.embedder" in offender for offender in offenders)


def test_settings_accept_field_names_even_when_fields_have_validation_aliases(tmp_path):
    settings = _settings(tmp_path)
    assert settings.sqlite_path == str(tmp_path / "t.db")
    assert settings.storage_dir == str(tmp_path / "storage")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgres://user:pass@db.example/db", "postgresql://user:pass@db.example/db"),
        ("postgresql://user:pass@db.example/db?sslmode=require", "postgresql://user:pass@db.example/db?sslmode=require"),
    ],
)
def test_postgresql_database_urls_are_accepted_and_legacy_scheme_is_normalized(url, expected):
    assert Settings(database_url=url).database_url == expected


def test_mysql_database_url_fails_closed_without_leaking_credentials():
    raw = "mysql://redacted-user:redacted-password@db.example/db?access_token=redacted-token#fragment"

    with pytest.raises(ValidationError) as captured:
        Settings(database_url=raw)

    diagnostics = (str(captured.value), repr(captured.value.errors()), captured.value.json())
    assert "unsupported database URL scheme: mysql" in diagnostics[0]
    assert "mysql://db.example" in diagnostics[0]
    for diagnostic in diagnostics:
        assert raw not in diagnostic
        assert "redacted-user" not in diagnostic
        assert "redacted-password" not in diagnostic
        assert "access_token=redacted-token" not in diagnostic
        assert "#fragment" not in diagnostic


def test_multi_query_retrieval_copies_context_per_worker(tmp_path, monkeypatch):
    repo = SQLiteRepository(_settings(tmp_path))
    owner = contextvars.ContextVar("owner", default="missing")
    token = owner.set("request-owner")
    calls: list[tuple[str, str]] = []
    lock = threading.Lock()

    def fake_retrieve(notebook_id, query):
        with lock:
            calls.append((query, owner.get()))
        # Keep each Context entered long enough for concurrent reuse to fail.
        time.sleep(0.03)
        return [], [], None

    monkeypatch.setattr(repo.retrieval.candidates, "_retrieve_chunks", fake_retrieve)
    try:
        repo.retrieval.candidates._retrieve_chunks_multi("nb", ["q1", "q2", "q3", "q4"])
    finally:
        owner.reset(token)

    assert sorted(calls) == [
        ("q1", "request-owner"),
        ("q2", "request-owner"),
        ("q3", "request-owner"),
        ("q4", "request-owner"),
    ]


def test_scale_graph_excludes_rejected_relations(tmp_path):
    repo = SQLiteRepository(_settings(tmp_path))
    nb = repo.create_notebook(NotebookCreate(name="graph"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "claim", "payload": {"name": "A"}, "evidence": []},
        {"local_id": "b", "object_type": "claim", "payload": {"name": "B"}, "evidence": []},
    ], [{
        "source_local_id": "a", "target_local_id": "b", "edge_type": "supports", "evidence": [],
    }])
    with repo._connect() as db:
        rel = db.execute(
            "SELECT id, source_object_id, target_object_id FROM knowledge_relations WHERE notebook_id=?",
            (nb.id,),
        ).fetchone()
    repo.set_edge_review(nb.id, rel["id"], "rejected")

    _nodes, edges, _chunks, _kg_nodes, _counts = repo._gather_kg_graph(nb.id)

    assert (rel["source_object_id"], rel["target_object_id"], 1.0) not in edges
    assert (rel["target_object_id"], rel["source_object_id"], 1.0) not in edges

    graph, key_to_idx, _chunk_map = repo.retrieval.graph._ppr_graph(nb.id)
    assert not graph.has_edge(
        key_to_idx[rel["source_object_id"]], key_to_idx[rel["target_object_id"]]
    )


def test_federated_large_guard_includes_base_notebooks(tmp_path, monkeypatch):
    repo = SQLiteRepository(_settings(tmp_path))
    personal = repo.create_notebook(NotebookCreate(name="personal"))
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    repo.replace_notebook_bases(personal.id, [base.id], "user-local")

    monkeypatch.setattr(
        repo.retrieval.candidates,
        "notebook_copy_stats",
        lambda notebook_id: {"copyable": notebook_id != base.id},
    )

    assert repo._federated_graph_is_large(personal.id) is True


def test_session_resolution_does_not_write_on_every_request(tmp_path):
    repo = SQLiteRepository(_settings(tmp_path))
    token = repo.create_session("user-local")
    with repo._connect() as db:
        before = db.execute(
            "SELECT last_seen_at, expires_at FROM auth_sessions WHERE token=?", (token,)
        ).fetchone()

    assert repo.resolve_session(token).id == "user-local"

    with repo._connect() as db:
        after = db.execute(
            "SELECT last_seen_at, expires_at FROM auth_sessions WHERE token=?", (token,)
        ).fetchone()
    assert tuple(after) == tuple(before)


def test_facade_composition_is_flat_and_static(tmp_path):
    """The compatibility wrapper has one neutral facade base and no mixins."""
    from app.services.repository_facade import RepositoryFacade

    assert SQLiteRepository.__mro__ == (
        SQLiteRepository,
        RepositoryFacade,
        object,
    )
    assert "__getattr__" not in SQLiteRepository.__dict__
    assert "__getattribute__" not in SQLiteRepository.__dict__

    repo = SQLiteRepository(_settings(tmp_path))
    assert repo._runtime.settings is repo.settings
