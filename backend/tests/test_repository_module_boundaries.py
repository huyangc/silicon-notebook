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


def test_knowledge_contracts_is_leaf_of_the_dependency_graph():
    modules = _imports(BACKEND / "app/services/knowledge_contracts.py")
    app_imports = {m for m in modules if m.startswith("app.")}
    assert app_imports == set(), app_imports
