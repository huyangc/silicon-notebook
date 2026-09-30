"""E1-2 static guard: every Ask entry goes through ONE installation of the
default retrieval ceiling, and nothing else installs a scope.

Plan D1 (``docs/superpowers/plans/2026-09-29-retrieval-permission-
remediation.md``): an unscoped run used to install nothing, and every producer
without an owner predicate of its own then read other members' private Memory
projections.  The fix is structural -- one installation point that every entry
reaches -- so this guard pins the structure, with equality assertions, over
``backend/app``:

* ``source_scope_context`` is called only by the two constructors in
  ``source_scope.py`` and by ``global_run`` (the one subjectless installer);
* ``default_ceiling_context`` only by ``AskService._retrieval_ceiling`` and the
  report worker (``ReportExecutionCoordinator._default_ceiling``);
  ``refreshed_ceiling_context`` only by the report engine's refresh;
* ``AskService._retrieval_ceiling`` wraps exactly the Ask entry points:
  ``ask`` (HTTP, stream, detached jobs, MCP ``ask_notebook``, extension
  engines), ``preview_reasoning_intent`` (both intent prechecks) and the two
  current-user engine calls; the engines themselves are reached only through
  those;
* no installer is imported under another name, and the production
  ``AskService`` is built with ``ceiling_readers``.

A second installer (say, MCP entering ``default_ceiling_context`` again, or a
route entering ``source_scope_context``) fails the equality below; the
behaviour it would break is pinned by ``test_default_ceiling_entrypoints.py``.
The primitives' unscoped branches (``scoped_allowed_source_ids`` with no
scope, ``knowledge_context`` / ``scoped_subgraph_nodes`` without one) stay
tested on their own, and are unreachable in production because of this guard.
"""
from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

from tests.architecture.semantic_source import PythonSourceIndex

_BACKEND = Path(__file__).resolve().parents[1]
_APP = _BACKEND / "app"

_INSTALLERS = (
    "source_scope_context",
    "default_ceiling_context",
    "refreshed_ceiling_context",
)


@lru_cache(maxsize=1)
def _index() -> PythonSourceIndex:
    return PythonSourceIndex.from_paths(_BACKEND, sorted(_APP.rglob("*.py")))


def _call_sites(index: PythonSourceIndex, name: str) -> set[tuple[str, str]]:
    return {
        (finding.key.path, finding.key.scope)
        for finding in index.calls()
        if finding.key.target.rsplit(".", 1)[-1] == name
    }


def test_only_the_constructors_and_global_run_install_a_scope():
    index = _index()
    assert _call_sites(index, "source_scope_context") == {
        ("app/services/source_scope.py", "<module>._fresh_default_ceiling"),
        ("app/services/source_scope.py", "<module>.refreshed_ceiling_context"),
        ("app/services/global_run.py", "<module>.global_ask_run"),
    }
    assert _call_sites(index, "default_ceiling_context") == {
        ("app/services/ask_service.py", "<module>.AskService._retrieval_ceiling"),
        (
            "app/services/report_execution.py",
            "<module>.ReportExecutionCoordinator._default_ceiling",
        ),
    }
    assert _call_sites(index, "refreshed_ceiling_context") == {
        ("app/services/report_engine.py", "<module>.ReportEngine._refreshed_ceiling"),
    }


def test_every_ask_entry_goes_through_the_one_installation():
    index = _index()
    ask = "app/services/ask_service.py"
    assert _call_sites(index, "_retrieval_ceiling") == {
        (ask, "<module>.AskService.ask"),
        (ask, "<module>.AskService.preview_reasoning_intent"),
        (ask, "<module>.AskService.ask_chunk_current"),
        (ask, "<module>.AskService.ask_reasoning_current"),
    }
    # The engines are reached only from inside an installation: ``ask``
    # dispatches by name (``getattr(self, spec.handler)``), the two
    # current-user calls wrap theirs, and nothing calls the extension engine
    # directly.
    assert _call_sites(index, "ask_chunk") == {
        (ask, "<module>.AskService.ask_chunk_current"),
    }
    assert _call_sites(index, "ask_reasoning") == {
        (ask, "<module>.AskService.ask_reasoning_current"),
    }
    assert _call_sites(index, "ask_plugin_engine") == set()


def test_no_installer_is_imported_under_another_name():
    aliased = []
    for path in sorted(_APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in _INSTALLERS and alias.asname:
                        aliased.append((path.relative_to(_BACKEND).as_posix(), alias.name))
    assert aliased == []


def test_the_routes_and_the_mcp_entry_install_nothing_themselves():
    for relative in ("app/api/ask_routes.py", "app/api/mcp_tools/memory_context.py"):
        source = (_BACKEND / relative).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative)
        imported = {
            alias.name
            for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        assert not imported & set(_INSTALLERS), relative


def test_the_production_ask_service_is_built_with_ceiling_readers():
    source = (_APP / "services" / "repository_runtime.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    constructions = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "AskService"
    ]
    assert len(constructions) == 1
    keywords = {keyword.arg: keyword.value for keyword in constructions[0].keywords}
    assert "ceiling_readers" in keywords
    value = keywords["ceiling_readers"]
    assert (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "ceiling_readers"
    ), "the Ask service must read through the runtime's one CeilingReaders wiring"


def test_guard_catches_a_second_installer():
    """Mutation check on the guard itself: a route that enters
    ``source_scope_context`` (the pre-E1-2 intent precheck) is caught."""
    mutated = {
        "app/api/ask_routes.py": (
            "from app.services.source_scope import source_scope_context\n"
            "def run_preview():\n"
            "    with source_scope_context('nb', None, None):\n"
            "        pass\n"
        ),
    }
    index = PythonSourceIndex.from_sources(mutated)
    assert _call_sites(index, "source_scope_context") == {
        ("app/api/ask_routes.py", "<module>.run_preview"),
    }
