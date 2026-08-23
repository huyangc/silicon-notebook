from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
GUARD_PATH = ROOT / "scripts" / "check_architecture_boundaries.py"
BASELINE_PATH = ROOT / "scripts" / "architecture_boundary_baseline.json"
_SPEC = importlib.util.spec_from_file_location("phase0_architecture_guard", GUARD_PATH)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - impossible checkout
    raise RuntimeError(f"cannot load architecture guard from {GUARD_PATH}")
_GUARD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_GUARD)

boundary_violations = _GUARD.boundary_violations
core_models_service_import_edges = _GUARD.core_models_service_import_edges
core_models_service_import_violations = (
    _GUARD.core_models_service_import_violations
)
facade_surface_additions = _GUARD.facade_surface_additions
function_length = _GUARD.function_length
function_length_violations = _GUARD.function_length_violations
import_graph = _GUARD.import_graph
public_class_surface = _GUARD.public_class_surface
repository_service_import_violations = (
    _GUARD.repository_service_import_violations
)
strongly_connected_components = _GUARD.strongly_connected_components


def _write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def test_guard_detects_contract_moved_back_into_services(tmp_path):
    app = tmp_path / "app"
    _write(app / "__init__.py", "")
    _write(app / "repositories/__init__.py", "")
    _write(
        app / "repositories/ports.py",
        "from app.services.retrieval import RetrievedChunk\n",
    )
    _write(app / "services/__init__.py", "")
    _write(
        app / "services/retrieval.py",
        "from app.repositories.ports import RepositoryPort\n",
    )

    violations = boundary_violations(app)
    components = strongly_connected_components(import_graph(app))

    assert any("imports service" in item for item in violations)
    assert components == [
        ("app.repositories.ports", "app.services.retrieval")
    ]


def test_guard_detects_member_style_service_imports(tmp_path):
    app = tmp_path / "app"
    _write(app / "repositories/ports.py", "from app import services\n")
    _write(app / "domain/retrieval.py", "from app import services\n")

    assert boundary_violations(app) == [
        "app.domain.retrieval imports forbidden app.services",
        "app.repositories.ports imports service app.services",
    ]


def test_guard_detects_domain_contract_moved_to_an_adapter(tmp_path):
    app = tmp_path / "app"
    _write(app / "__init__.py", "")
    _write(app / "domain/__init__.py", "")
    _write(
        app / "domain/retrieval.py",
        "from app.repositories.sqlite.query_store import QueryStore\n",
    )

    assert boundary_violations(app) == [
        "app.domain.retrieval imports forbidden app.repositories.sqlite.query_store"
    ]


def test_guard_keeps_application_stage_contracts_out_of_implementations(tmp_path):
    app = tmp_path / "app"
    _write(
        app / "application/ask.py",
        "from app import services\n"
        "from app.repositories.sqlite import query_store\n",
    )

    assert boundary_violations(app) == [
        "app.application.ask imports forbidden implementation app.repositories.sqlite",
        "app.application.ask imports forbidden implementation app.services",
    ]


def test_application_stage_guard_is_an_allowlist_not_an_implementation_denylist(
    tmp_path,
):
    app = tmp_path / "app"
    _write(
        app / "application/ask.py",
        "from .. import bootstrap\n"
        "from app import main\n"
        "from app.extension_sdk import PluginManifest\n",
    )

    assert boundary_violations(app) == [
        "app.application.ask imports forbidden implementation app.bootstrap",
        "app.application.ask imports forbidden implementation app.extension_sdk",
        "app.application.ask imports forbidden implementation app.main",
        "app.application.ask imports the extension composition surface "
        "outside an approved root",
    ]


def test_application_stage_guard_accepts_only_stable_contract_layers(tmp_path):
    app = tmp_path / "app"
    _write(
        app / "application/ask.py",
        "from app.core.ask_retrieval_policy import AskRetrievalLimits\n"
        "from app.domain.cancellation import CancelEvent\n"
        "from app.models.ask import AskResponse\n"
        "import app.application as application_contracts\n"
        "import app.models.ask as ask_models\n"
        "from . import values\n",
    )
    _write(app / "application/values.py", "VALUE = 1\n")

    assert boundary_violations(app) == []


def test_application_stage_guard_rejects_bare_app_package_escape(tmp_path):
    app = tmp_path / "app"
    _write(
        app / "application/ask.py",
        "import app\n"
        "BAD = app.services\n",
    )

    assert boundary_violations(app) == [
        "app.application.ask imports forbidden implementation app",
    ]


@pytest.mark.parametrize(
    "statement,escape",
    [
        ("import app.application", "app.services"),
        ("import app.models.ask", "app.bootstrap"),
    ],
)
def test_application_stage_guard_rejects_unaliased_submodule_root_binding(
    tmp_path, statement, escape,
):
    app = tmp_path / "app"
    _write(
        app / "application/ask.py",
        f"{statement}\n"
        f"BAD = {escape}\n",
    )

    assert boundary_violations(app) == [
        "app.application.ask imports forbidden implementation app",
    ]


def test_facade_freeze_rejects_addition_but_allows_surface_reduction(tmp_path):
    path = tmp_path / "facade.py"
    _write(
        path,
        "class RepositoryFacade:\n"
        "    def existing(self): pass\n"
        "    def second(self): pass\n",
    )
    allowed = public_class_surface(path, "RepositoryFacade")

    _write(
        path,
        "class RepositoryFacade:\n"
        "    def existing(self): pass\n"
        "    def second(self): pass\n"
        "    def added(self): pass\n",
    )
    assert facade_surface_additions(path, "RepositoryFacade", allowed) == ["added"]

    _write(
        path,
        "class RepositoryFacade(Mixin):\n"
        "    def existing(self): pass\n"
        "    def second(self): pass\n"
        "    added = existing\n",
    )
    assert facade_surface_additions(path, "RepositoryFacade", allowed) == [
        "<base:Mixin>",
        "added",
    ]

    _write(path, "class RepositoryFacade:\n    def existing(self): pass\n")
    assert facade_surface_additions(path, "RepositoryFacade", allowed) == []


def test_repository_service_reverse_import_ceiling_only_allows_reduction(tmp_path):
    app = tmp_path / "app"
    _write(app / "repositories/sqlite/query_store.py", "")
    _write(app / "repositories/postgres/query_store.py", "")
    _write(app / "repositories/filesystem/artifact_store.py", "")

    assert repository_service_import_violations(
        app, {"sqlite": 0, "postgres": 0, "other": 0}
    ) == []

    _write(
        app / "repositories/sqlite/query_store.py",
        "from app.services.retrieval import RetrievedChunk\n",
    )
    _write(
        app / "repositories/postgres/query_store.py",
        "from ...services.retrieval import RetrievedChunk\n",
    )
    _write(
        app / "repositories/filesystem/artifact_store.py",
        "from app import services\n",
    )
    assert repository_service_import_violations(
        app, {"sqlite": 0, "postgres": 0, "other": 0}
    ) == [
        "repositories/other service imports grew: 1 > 0",
        "repositories/postgres service imports grew: 1 > 0",
        "repositories/sqlite service imports grew: 1 > 0",
    ]


def test_core_models_service_import_allowlist_only_allows_reduction(tmp_path):
    app = tmp_path / "app"
    _write(app / "core/x.py", "from app.services.y import thing\n")

    # A new reverse edge that isn't on the allowlist is rejected.
    assert core_models_service_import_violations(app, set()) == [
        "core/models gained a service import: app.core.x -> app.services.y"
    ]

    # The same edge, allowlisted and still present, is silent.
    assert (
        core_models_service_import_violations(app, {"app.core.x -> app.services.y"})
        == []
    )

    # The allowlist has zero slack: an entry the code no longer needs must
    # be removed from the baseline in the same change, not left stale.
    _write(app / "core/x.py", "VALUE = 1\n")
    assert core_models_service_import_violations(
        app, {"app.core.x -> app.services.y"}
    ) == [
        "core/models service import allowlist is stale, remove: "
        "app.core.x -> app.services.y "
        "(update scripts/architecture_boundary_baseline.json :: "
        "core_models_service_imports.allowed)"
    ]


def test_core_models_service_import_allowlist_covers_type_checking_imports(tmp_path):
    app = tmp_path / "app"
    _write(
        app / "models/z.py",
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from app.services.y import Thing\n",
    )

    assert core_models_service_import_violations(app, set()) == [
        "core/models gained a service import: app.models.z -> app.services.y"
    ]


def test_baseline_ceilings_and_allowlist_have_not_collapsed_to_empty():
    """A baseline read as ``{}`` (missing file, truncated write, bad key)
    would make every ceiling/allowlist check vacuously pass -- both loops in
    ``function_length_violations``/``core_models_service_import_violations``
    iterate the *baseline*, not the repository, so an empty baseline finds
    nothing to check. This reads the real checked-in baseline and pins it to
    a non-trivial lower bound and to the exact known allowlist, so a
    baseline that quietly lost its contents fails loudly here instead of the
    real guard run silently no-op'ing.
    """
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))

    assert len(baseline["function_length_ceiling"]) >= 22
    assert set(baseline["core_models_service_imports"]["allowed"]) == {
        "app.core.llm -> app.services.cancellation",
        "app.models.agent_profile -> app.services.agent_profile_block",
    }


def test_core_models_service_import_edges_normalize_to_the_submodule(tmp_path):
    """Every spelling that reaches the same submodule must produce the same
    edge, and a reference that cannot be attributed to one submodule must
    normalize to its own edge that no submodule-qualified allowlist entry can
    ever satisfy.

    This is the shape named in review: a bare package import or a bare
    ``import app`` followed by attribute access could otherwise land on the
    allowlist and then silently authorize importing *any* member of
    ``app.services``, not just the one originally reviewed.
    """
    app = tmp_path / "app"
    _write(
        app / "core/from_package.py",
        "from app.services import cancellation\n",
    )
    _write(
        app / "core/from_submodule.py",
        "from app.services.cancellation import AskCancelled\n",
    )
    _write(app / "core/plain_import.py", "import app.services.cancellation\n")
    _write(app / "core/deep_submodule.py", "from app.services.kg.ppr import run\n")
    _write(app / "core/bare_package.py", "import app.services\n")
    _write(app / "core/star_import.py", "from app.services import *\n")
    _write(
        app / "core/bare_app_attribute.py",
        "import app\nBAD = app.services.cancellation.AskCancelled\n",
    )
    # ``from app import services`` binds the whole package under a local name;
    # the relative spelling inside app/core resolves to the same base.
    _write(app / "core/from_app_member.py", "from app import services\n")
    _write(app / "core/relative_app_member.py", "from .. import services as svc\n")
    _write(app / "core/from_app_other.py", "from app import models\n")

    edges = core_models_service_import_edges(app)

    # All three spellings that name ``cancellation`` specifically collapse to
    # the identical, submodule-qualified edge.
    assert "app.core.from_package -> app.services.cancellation" in edges
    assert "app.core.from_submodule -> app.services.cancellation" in edges
    assert "app.core.plain_import -> app.services.cancellation" in edges

    # A reference three levels deep still normalizes to the first submodule.
    assert "app.core.deep_submodule -> app.services.kg" in edges

    # References that cannot be attributed to one submodule get their own
    # edge instead of silently vanishing or aliasing a real submodule edge.
    assert "app.core.bare_package -> app.services" in edges
    assert "app.core.star_import -> app.services" in edges
    assert "app.core.bare_app_attribute -> app" in edges
    assert "app.core.from_app_member -> app.services" in edges
    assert "app.core.relative_app_member -> app.services" in edges
    # Importing a non-services member from ``app`` is not a services edge.
    assert not any(edge.startswith("app.core.from_app_other ->") for edge in edges)

    # None of the unattributable edges can ever satisfy a submodule-qualified
    # allowlist entry -- confirm the violation check actually rejects them
    # rather than the allowlist accidentally matching by string prefix.
    allowed = {"app.core.bare_package -> app.services.cancellation"}
    violations = core_models_service_import_violations(app, allowed)
    assert any(
        "app.core.bare_package -> app.services" in violation
        for violation in violations
        if "gained" in violation
    )


def test_core_models_service_import_allowlist_covers_plain_imports_too(tmp_path):
    """A plain top-level import must be caught exactly like a TYPE_CHECKING one.

    Pairs with the TYPE_CHECKING test above as a contrast arm: a change that
    accidentally scoped detection to only the inside of ``if TYPE_CHECKING:``
    blocks would still pass that test but fail this one, and a change that
    broke plain-import detection while leaving the TYPE_CHECKING branch alone
    would fail this one while that test stayed green.
    """
    app = tmp_path / "app"
    _write(app / "models/z.py", "from app.services.y import Thing\n")

    assert core_models_service_import_violations(app, set()) == [
        "core/models gained a service import: app.models.z -> app.services.y"
    ]


def test_function_length_ceiling_only_allows_reduction(tmp_path):
    path = tmp_path / "pkg" / "mod.py"
    key = "pkg/mod.py::widget"
    _write(path, "def widget():\n" + "    pass\n" * 9)  # 10 lines total

    assert function_length_violations(tmp_path, {key: 10}) == []

    _write(path, "def widget():\n" + "    pass\n" * 10)  # 11 lines total
    assert function_length_violations(tmp_path, {key: 10}) == [
        f"function grew: {key} 11 > 10"
    ]

    # The ceiling has zero slack: a function that shrank below its recorded
    # ceiling must have the baseline lowered in the same change.
    assert function_length_violations(tmp_path, {key: 12}) == [
        f"function length ceiling is stale, lower it: {key} 11 < 12 "
        "(update scripts/architecture_boundary_baseline.json :: "
        "function_length_ceiling)"
    ]

    # A renamed (or removed) target is a loud failure, not a silent pass.
    _write(path, "def renamed():\n" + "    pass\n" * 10)
    assert function_length_violations(tmp_path, {key: 11}) == [
        f"function length ceiling target not found: {key} "
        "(update scripts/architecture_boundary_baseline.json :: "
        "function_length_ceiling)"
    ]


def test_function_length_excludes_decorator_lines(tmp_path):
    path = tmp_path / "pkg" / "mod.py"
    _write(path, "@decorator\ndef widget():\n    pass\n")

    assert function_length(tmp_path, "pkg/mod.py::widget") == 2


def test_function_length_resolves_nested_and_method_qualnames(tmp_path):
    path = tmp_path / "pkg" / "mod.py"
    _write(
        path,
        "class Outer:\n"
        "    def method(self):\n"
        "        pass\n"
        "\n"
        "def register():\n"
        "    def inner():\n"
        "        pass\n"
        "    return inner\n",
    )

    assert (
        function_length_violations(
            tmp_path,
            {
                "pkg/mod.py::Outer.method": 2,
                "pkg/mod.py::register.inner": 2,
            },
        )
        == []
    )


def test_function_length_resolves_async_qualnames(tmp_path):
    """``find_qualname_node`` must match ``async def`` targets too.

    Every existing qualname-resolution test above uses plain ``def``, so a
    change that dropped ``ast.AsyncFunctionDef`` from ``find_qualname_node``
    (or from ``public_class_surface``'s own function-node check) would leave
    them all green. This covers an async method and an async function nested
    inside an async function, mirroring the sync nesting case above.
    """
    path = tmp_path / "pkg" / "amod.py"
    _write(
        path,
        "class Outer:\n"
        "    async def method(self):\n"
        "        pass\n"
        "\n"
        "async def register():\n"
        "    async def inner():\n"
        "        pass\n"
        "    return inner\n",
    )

    assert function_length(tmp_path, "pkg/amod.py::Outer.method") == 2
    assert function_length(tmp_path, "pkg/amod.py::register.inner") == 2
    assert (
        function_length_violations(
            tmp_path,
            {
                "pkg/amod.py::Outer.method": 2,
                "pkg/amod.py::register.inner": 2,
            },
        )
        == []
    )


def test_extension_composition_is_statically_isolated_from_workflows():
    violations = boundary_violations(ROOT / "backend/app")

    assert not [item for item in violations if "extension composition" in item]
    for path in (ROOT / "backend/app/services").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "app.extensions" not in source
        assert "app.extension_sdk" not in source


def test_guard_rejects_workflow_consuming_extension_composition(tmp_path):
    app = tmp_path / "app"
    _write(app / "services/workflow.py", "from app import extensions\n")

    assert boundary_violations(app) == [
        "app.services.workflow imports the extension composition surface "
        "outside an approved root"
    ]


def test_guard_rejects_plugin_importing_core_implementations(tmp_path):
    app = tmp_path / "app"
    _write(
        app / "extensions/builtin/unsafe.py",
        "from app.services import repository_facade\n"
        "from app.repositories.sqlite import query_store\n",
    )

    assert boundary_violations(app) == [
        "app.extensions.builtin.unsafe imports forbidden plugin dependency "
        "app.repositories.sqlite",
        "app.extensions.builtin.unsafe imports forbidden plugin dependency "
        "app.services",
    ]


def test_guard_allows_feature_plugin_sdk_and_rejects_other_app_layers(tmp_path):
    app = tmp_path / "app"
    _write(
        app / "features/search/plugin.py",
        "from app.extension_sdk import ExtensionManifest\n"
        "from app.domain import retrieval\n"
        "from app.features import search\n"
        "from app.features.search import adapter\n",
    )
    assert boundary_violations(app) == []

    _write(
        app / "features/search/plugin.py",
        "from app.services import ask_service\n"
        "from app.features.other import plugin\n",
    )
    assert boundary_violations(app) == [
        "app.features.search.plugin imports forbidden plugin dependency "
        "app.features.other",
        "app.features.search.plugin imports forbidden plugin dependency "
        "app.services",
    ]


def test_guard_rejects_main_importing_extension_runtime_directly(tmp_path):
    app = tmp_path / "app"
    _write(app / "main.py", "from app import extensions\n")

    assert boundary_violations(app) == [
        "app.main imports the extension composition surface outside an approved root"
    ]


def test_guard_allows_only_named_application_composition_root(tmp_path):
    app = tmp_path / "app"
    _write(app / "bootstrap.py", "from app import extensions\n")
    _write(app / "repositories/factory.py", "from app import extensions\n")
    _write(app / "repositories/other_factory.py", "from app import extensions\n")

    assert boundary_violations(app) == [
        "app.repositories.factory imports the extension composition "
        "surface outside an approved root",
        "app.repositories.other_factory imports the extension composition "
        "surface outside an approved root"
    ]


def test_registry_composition_does_not_change_route_topology(monkeypatch):
    from app import main as app_main

    baseline = app_main.create_app()
    sentinel = object()
    monkeypatch.setattr(
        app_main,
        "application_extension_runtime",
        lambda: type(
            "Runtime",
            (),
            {"registry": sentinel, "agent_tools": None},
        )(),
    )
    composed = app_main.create_app()

    def route_signature(app):
        return [
            (
                route.path,
                tuple(sorted(getattr(route, "methods", ()) or ())),
                route.name,
            )
            for route in app.routes
        ]

    assert composed.state.extension_registry is sentinel
    assert route_signature(composed) == route_signature(baseline)


def test_extension_boundary_is_present_in_all_agent_entry_documents():
    for name in ("README.md", "README_zh.md", "AGENTS.md", "CLAUDE.md"):
        normalized = (
            (ROOT / name)
            .read_text(encoding="utf-8")
            .casefold()
            .replace("_", " ")
            .replace("-", " ")
        )
        assert "extension sdk" in normalized, name
        assert "retrieval" in normalized, name
        assert "host" in normalized, name
        assert "capability" in normalized, name
        assert "subagent review" in normalized, name
        assert "ci" in normalized, name


def test_ask_application_stage_boundary_is_in_all_agent_entry_documents():
    for name in ("README.md", "README_zh.md", "AGENTS.md", "CLAUDE.md"):
        normalized = (
            (ROOT / name)
            .read_text(encoding="utf-8")
            .casefold()
            .replace("_", " ")
            .replace("-", " ")
        )
        assert "application" in normalized, name
        assert "stage" in normalized, name
        assert "retrieval run" in normalized, name
        assert "connection" in normalized or "连接" in normalized, name


def test_report_stage_and_post_terminal_boundary_is_in_all_agent_documents():
    for name in ("README.md", "README_zh.md", "AGENTS.md", "CLAUDE.md"):
        normalized = (
            (ROOT / name)
            .read_text(encoding="utf-8")
            .casefold()
            .replace("_", " ")
            .replace("-", " ")
        )
        assert "report" in normalized, name
        assert "stage" in normalized, name
        assert "report.audit" in normalized, name
        assert "report.completed observer" in normalized, name
        assert "done" in normalized, name


def test_report_completion_has_no_engine_direct_service_path():
    engine = (ROOT / "backend/app/services/report_engine.py").read_text(
        encoding="utf-8"
    )
    execution = (ROOT / "backend/app/services/report_execution.py").read_text(
        encoding="utf-8"
    )
    assert "note_report_completed" not in engine
    assert "app.extensions" not in execution
    assert "app.extension_sdk" not in execution
    assert "after_completed" in execution
