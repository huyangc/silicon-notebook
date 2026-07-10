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
                assert f"{relative}:{node.lineno}" in surface[alias.name]["consumers"]


def test_static_repository_patch_scan_matches_manifest_exactly():
    recorded = {
        (patch["file"], patch["line"], patch["target"], patch["base"])
        for record in _surface().values()
        for patch in record["patch_targets"]
    }

    assert recorded == _static_repository_patches()
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

    assert recorded == _static_repository_consumers()


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
            assert str(inspect.signature(member)) == record["signature"], name
            continue
        if kind == "constant":
            assert hasattr(SQLiteRepository, name), name
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
