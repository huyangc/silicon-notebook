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
ports_method_count_violations = _GUARD.ports_method_count_violations
ports_protocol_method_count = _GUARD.ports_protocol_method_count
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


def test_facade_surface_additions_report_only_new_seats(tmp_path):
    """The additions half of the surface freeze; the stale half (a retired
    name left on the allowlist) is ``facade_surface_stale``, pinned below."""
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


_PORTS_SOURCE = (
    "from typing import Protocol\n"
    "\n"
    "class BaseMemory(Protocol):\n"
    "    def kept(self): ...\n"
    "\n"
    "class MemoryRepository(BaseMemory, Protocol):\n"
    "    def other(self): ...\n"
)
_DEPS_SOURCE = (
    "from typing import cast\n"
    "from app.repositories.ports import MemoryRepository\n"
    "\n"
    "def repository():\n"
    "    return object()\n"
    "\n"
    "def memory_service() -> MemoryRepository:\n"
    "    return cast(MemoryRepository, repository())\n"
)


def _facade_root(
    tmp_path, *, facade_body, allowed, app_files=None, ports=_PORTS_SOURCE,
    deps=_DEPS_SOURCE,
):
    """A minimal checkout: the three repository classes, a ports module, a
    deps module binding the facade to a protocol, the baseline sections the
    facade checks read, and any extra modules a case needs."""
    _write(
        tmp_path / "backend/app/services/repository_facade.py",
        "def _repository_from_weakref(ref):\n"
        "    return ref()\n"
        "\n"
        f"class RepositoryFacade:\n{facade_body}",
    )
    _write(
        tmp_path / "backend/app/services/sqlite_repository.py",
        "class SQLiteRepository(RepositoryFacade):\n"
        "    def close_local(self): pass\n",
    )
    _write(
        tmp_path / "backend/app/services/postgres_repository.py",
        "class PostgresRepository(RepositoryFacade):\n"
        "    def close(self): pass\n",
    )
    _write(tmp_path / "backend/app/repositories/ports.py", ports)
    _write(tmp_path / "backend/app/api/deps.py", deps)
    _write(
        tmp_path / "scripts/architecture_boundary_baseline.json",
        json.dumps(
            {
                "facade_public_surface": {
                    "RepositoryFacade": {"allowed_names": sorted(allowed)},
                    "SQLiteRepository": {
                        "allowed_names": ["<base:RepositoryFacade>", "close_local"]
                    },
                },
                "repository_service_import_ceiling": {
                    "sqlite": 0, "postgres": 0, "other": 0,
                },
                "core_models_service_imports": {"allowed": []},
                "facade_protocol_binding_gaps": {},
                "ports_protocol_method_count": 2,
                "function_length_ceiling": {},
            }
        ),
    )
    for rel, source in (app_files or {}).items():
        _write(tmp_path / rel, source)
    return tmp_path


_LIVE_BODY = (
    "    def kept(self): pass\n"
    "    def other(self): pass\n"
)
_LIVE_ALLOWED = {"kept", "other"}


def test_facade_surface_allowlist_has_zero_slack(tmp_path):
    root = _facade_root(tmp_path, facade_body=_LIVE_BODY, allowed=_LIVE_ALLOWED)
    assert _GUARD.facade_violations(root) == []

    # A retired method whose name stays on the allowlist is a violation:
    # the allowlist must equal the live surface, not merely contain it.
    root = _facade_root(
        tmp_path, facade_body=_LIVE_BODY, allowed=_LIVE_ALLOWED | {"retired"}
    )
    assert _GUARD.facade_violations(root) == [
        "RepositoryFacade facade surface allowlist is stale, remove: retired "
        "(update scripts/architecture_boundary_baseline.json :: "
        "facade_public_surface.RepositoryFacade.allowed_names)"
    ]

    root = _facade_root(
        tmp_path, facade_body=_LIVE_BODY + "    def added(self): pass\n",
        allowed=_LIVE_ALLOWED,
    )
    assert _GUARD.facade_violations(root) == [
        "RepositoryFacade gained public facade seats: added"
    ]


def test_check_runs_every_facade_guard(tmp_path, monkeypatch):
    """``check()`` -- what the script and the contracts lane run -- reports
    the surface, dynamic-reference and protocol-binding violations, so none
    of them can be unhooked while today's tree happens to be clean."""
    monkeypatch.setattr(_GUARD, "DYNAMIC_FACADE_REFERENCE_EXEMPTIONS", {})
    root = _facade_root(tmp_path, facade_body=_LIVE_BODY, allowed=_LIVE_ALLOWED)
    assert _GUARD.check(root) == []

    root = _facade_root(
        tmp_path,
        facade_body=(
            "    def __init__(self, ref):\n"
            "        self._seam = lambda: _repository_from_weakref(ref).gone_seam()\n"
            "    def kept(self): pass\n"
        ),
        allowed={"kept", "other"},
        app_files={
            "backend/app/services/consumer.py":
                "def probe(repo):\n    return getattr(repo, 'gone_probe', None)\n",
        },
    )
    assert _GUARD.check(root) == [
        "RepositoryFacade facade surface allowlist is stale, remove: other "
        "(update scripts/architecture_boundary_baseline.json :: "
        "facade_public_surface.RepositoryFacade.allowed_names)",
        "facade weak-reference seam reads a missing RepositoryFacade attribute: "
        "gone_seam",
        "literal getattr/hasattr on a facade receiver names a missing repository "
        "attribute: backend/app/services/consumer.py :: probe :: gone_probe",
        "protocol MemoryRepository is bound to the facade but declares methods "
        "RepositoryFacade lacks: other",
    ]


def test_weakref_seam_must_name_a_live_facade_attribute(tmp_path, monkeypatch):
    monkeypatch.setattr(_GUARD, "DYNAMIC_FACADE_REFERENCE_EXEMPTIONS", {})
    seam = (
        "    def __init__(self, ref):\n"
        "        self._seam = lambda nb: _repository_from_weakref(ref).{name}(nb)\n"
        + _LIVE_BODY
        + "    def _private(self, nb): pass\n"
    )
    for live in ("kept", "_private"):
        root = _facade_root(
            tmp_path, facade_body=seam.format(name=live), allowed=_LIVE_ALLOWED
        )
        assert _GUARD.dynamic_facade_reference_violations(root) == []

    # A retired name, and a name only one wrapper defines: the seam is wired
    # in RepositoryFacade.__init__ and serves both backends.
    for missing in ("retired", "close_local", "close"):
        root = _facade_root(
            tmp_path, facade_body=seam.format(name=missing), allowed=_LIVE_ALLOWED
        )
        assert _GUARD.dynamic_facade_reference_violations(root) == [
            "facade weak-reference seam reads a missing RepositoryFacade "
            f"attribute: {missing}"
        ]


def test_weakref_seam_through_the_runtime_must_name_a_live_component_member(
    tmp_path, monkeypatch
):
    """A seam may skip the facade (``_repository_from_weakref(ref)._runtime
    .<component>.<member>``, as ``incremental_fuse_source`` does since S2
    batch C); the member must still exist on the component's class, found
    from a ``self.<component> = <Class>(...)`` assignment or a property's
    return annotation, bases included."""
    monkeypatch.setattr(_GUARD, "DYNAMIC_FACADE_REFERENCE_EXEMPTIONS", {})
    seam = (
        "    def __init__(self, ref):\n"
        "        self._runtime = None\n"
        "        self._seam = lambda nb: _repository_from_weakref(ref)"
        "._runtime.{component}.{member}(nb)\n"
        + _LIVE_BODY
    )
    runtime = {
        "backend/app/services/repository_runtime.py": (
            "from app.services.lifecycle import Lifecycle\n"
            "from app.services.asker import Asker\n"
            "class RepositoryRuntime:\n"
            "    def wire(self):\n"
            "        self.lifecycle = Lifecycle()\n"
            "    @property\n"
            "    def asker(self) -> Asker:\n"
            "        return Asker()\n"
        ),
        "backend/app/services/lifecycle.py": (
            "class Base:\n    def inherited(self, nb): pass\n"
            "class Lifecycle(Base):\n    def fuse(self, nb): pass\n"
        ),
        "backend/app/services/asker.py": "class Asker:\n    def ask(self, nb): pass\n",
    }
    for component, member in (
        ("lifecycle", "fuse"), ("lifecycle", "inherited"), ("asker", "ask"),
    ):
        root = _facade_root(
            tmp_path,
            facade_body=seam.format(component=component, member=member),
            allowed=_LIVE_ALLOWED, app_files=runtime,
        )
        assert _GUARD.dynamic_facade_reference_violations(root) == []

    root = _facade_root(
        tmp_path, facade_body=seam.format(component="lifecycle", member="gone"),
        allowed=_LIVE_ALLOWED, app_files=runtime,
    )
    assert _GUARD.dynamic_facade_reference_violations(root) == [
        "facade weak-reference seam reads a missing Lifecycle attribute: "
        "_runtime.lifecycle.gone"
    ]
    root = _facade_root(
        tmp_path, facade_body=seam.format(component="nowhere", member="fuse"),
        allowed=_LIVE_ALLOWED, app_files=runtime,
    )
    assert _GUARD.dynamic_facade_reference_violations(root) == [
        "facade weak-reference seam reads a runtime component whose class "
        "cannot be resolved: _runtime.nowhere"
    ]


_RUNTIME_SEAM_FILES = {
    "backend/app/services/repository_runtime.py": (
        "from app.services.lifecycle import Lifecycle\n"
        "from app.services.decoy import Decoy\n"
        "class RepositoryRuntime:\n"
        "    def helper(self):\n"
        "        lifecycle = Decoy()\n"
        "        return lifecycle\n"
        "    def wire(self):\n"
        "        self.lifecycle = Lifecycle()\n"
    ),
    "backend/app/services/lifecycle.py": "class Lifecycle:\n    def fuse(self, nb): pass\n",
    "backend/app/services/decoy.py": "class Decoy:\n    def other(self, nb): pass\n",
}


def test_runtime_component_ignores_a_same_named_local_in_a_method(
    tmp_path, monkeypatch
):
    """Inside a method only ``self.<component>`` binds the component: a local
    ``lifecycle = Decoy()`` in an earlier method must not be taken for it."""
    monkeypatch.setattr(_GUARD, "DYNAMIC_FACADE_REFERENCE_EXEMPTIONS", {})
    body = (
        "    def __init__(self, ref):\n"
        "        self._runtime = None\n"
        "        self._seam = lambda nb: _repository_from_weakref(ref)"
        "._runtime.lifecycle.fuse(nb)\n"
        + _LIVE_BODY
    )
    root = _facade_root(
        tmp_path, facade_body=body, allowed=_LIVE_ALLOWED,
        app_files=_RUNTIME_SEAM_FILES,
    )
    assert _GUARD.dynamic_facade_reference_violations(root) == []


def test_weakref_seam_shapes_the_checks_cannot_read_fail(tmp_path, monkeypatch):
    """A ``_repository_from_weakref(...)`` result must be read as
    ``.<facade member>`` or ``._runtime.<component>.<member>``, followed by at
    most a call; an alias, a walrus, a ``getattr``, a subscript or a deeper
    chain hides the names the seam checks resolve."""
    monkeypatch.setattr(_GUARD, "DYNAMIC_FACADE_REFERENCE_EXEMPTIONS", {})
    live = (
        "    def __init__(self, ref):\n"
        "        self._runtime = None\n"
        "        self._a = lambda nb: _repository_from_weakref(ref).kept(nb)\n"
        "        self._b = lambda: _repository_from_weakref(ref).other\n"
        "        self._c = lambda nb: _repository_from_weakref(ref)"
        "._runtime.lifecycle.fuse(nb)\n"
        + _LIVE_BODY
    )
    root = _facade_root(
        tmp_path, facade_body=live, allowed=_LIVE_ALLOWED,
        app_files=_RUNTIME_SEAM_FILES,
    )
    assert _GUARD.dynamic_facade_reference_violations(root) == []

    hidden = live + (
        "    def alias(self, ref):\n"
        "        r = _repository_from_weakref(ref)\n"
        "        return r.kept()\n"
        "    def walrus(self, ref):\n"
        "        return (r := _repository_from_weakref(ref)).kept()\n"
        "    def probe(self, ref):\n"
        "        return getattr(_repository_from_weakref(ref), 'kept')()\n"
        "    def deeper(self, ref):\n"
        "        return _repository_from_weakref(ref).kept.inner()\n"
        "    def deeper_runtime(self, ref):\n"
        "        return _repository_from_weakref(ref)._runtime.lifecycle.fuse.inner()\n"
        "    def runtime_only(self, ref):\n"
        "        return _repository_from_weakref(ref)._runtime\n"
        "    def indexed(self, ref):\n"
        "        return _repository_from_weakref(ref).kept[0]\n"
    )
    root = _facade_root(
        tmp_path, facade_body=hidden, allowed=_LIVE_ALLOWED,
        app_files=_RUNTIME_SEAM_FILES,
    )
    prefix = (
        "facade weak-reference seam has a shape the seam checks cannot read: "
        "RepositoryFacade."
    )
    assert _GUARD.dynamic_facade_reference_violations(root) == sorted([
        prefix + "alias :: _repository_from_weakref(ref)",
        prefix + "deeper :: _repository_from_weakref(ref).kept.inner",
        prefix + "deeper_runtime :: "
                 "_repository_from_weakref(ref)._runtime.lifecycle.fuse.inner",
        prefix + "indexed :: _repository_from_weakref(ref).kept",
        prefix + "probe :: _repository_from_weakref(ref)",
        prefix + "runtime_only :: _repository_from_weakref(ref)._runtime",
        prefix + "walrus :: _repository_from_weakref(ref)",
    ])


def test_literal_getattr_probe_must_name_a_live_repository_attribute(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(_GUARD, "DYNAMIC_FACADE_REFERENCE_EXEMPTIONS", {})
    body = "    def __init__(self):\n        self.settings = None\n" + _LIVE_BODY
    probes = (
        "def probe(repo, repository, other, holder, _repo):\n"
        "    getattr(repo, 'kept', None)\n"
        "    getattr(repository, 'settings', None)\n"
        "    hasattr(holder.repository, 'close')\n"
        "    getattr(repo, 'close_local', None)\n"
        "    getattr(_repo, 'other', None)\n"
        "    getattr(other, 'retired', None)\n"
    )
    rel = "backend/app/services/consumer.py"
    root = _facade_root(
        tmp_path, facade_body=body, allowed=_LIVE_ALLOWED | {"settings"},
        app_files={rel: probes},
    )
    # live on the facade, on its self-assigned attributes, or on a wrapper;
    # a non-facade receiver (``other``) is out of scope.
    assert _GUARD.dynamic_facade_reference_violations(root) == []

    root = _facade_root(
        tmp_path, facade_body=body, allowed=_LIVE_ALLOWED | {"settings"},
        app_files={
            rel: probes + "    getattr(repo, 'retired', None)\n",
            "scripts/tool.py": "class Tool:\n    def run(self):\n"
                               "        hasattr(self._repo, 'gone')\n",
            # spellings a call-shape text filter would miss: a comment and a
            # line break before the receiver, a parenthesized receiver, and a
            # bare ``_repo`` local name
            "backend/app/services/spelled.py": (
                "def a(repo):\n"
                "    return getattr(  # why\n"
                "        repo, 'gone_a', None)\n"
                "def b(repo):\n"
                "    return getattr((repo), 'gone_b', None)\n"
                "def c(_repo):\n"
                "    return getattr(_repo, 'gone_c', None)\n"
            ),
        },
    )
    assert _GUARD.dynamic_facade_reference_violations(root) == [
        "literal getattr/hasattr on a facade receiver names a missing "
        f"repository attribute: {rel} :: probe :: retired",
        "literal getattr/hasattr on a facade receiver names a missing "
        "repository attribute: backend/app/services/spelled.py :: a :: gone_a",
        "literal getattr/hasattr on a facade receiver names a missing "
        "repository attribute: backend/app/services/spelled.py :: b :: gone_b",
        "literal getattr/hasattr on a facade receiver names a missing "
        "repository attribute: backend/app/services/spelled.py :: c :: gone_c",
        "literal getattr/hasattr on a facade receiver names a missing "
        "repository attribute: scripts/tool.py :: Tool.run :: gone",
    ]


def test_facade_internal_getattr_on_self_must_name_a_facade_attribute(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(_GUARD, "DYNAMIC_FACADE_REFERENCE_EXEMPTIONS", {})
    body = _LIVE_BODY + "    def probe(self):\n        return getattr(self, '{name}', None)\n"
    root = _facade_root(
        tmp_path, facade_body=body.format(name="kept"),
        allowed=_LIVE_ALLOWED | {"probe"},
    )
    assert _GUARD.dynamic_facade_reference_violations(root) == []

    # inside RepositoryFacade only facade names count: ``close`` lives on
    # the PostgreSQL wrapper alone
    for missing in ("retired", "close"):
        root = _facade_root(
            tmp_path, facade_body=body.format(name=missing),
            allowed=_LIVE_ALLOWED | {"probe"},
        )
        assert _GUARD.dynamic_facade_reference_violations(root) == [
            "literal getattr/hasattr on a facade receiver names a missing "
            "repository attribute: backend/app/services/repository_facade.py :: "
            f"RepositoryFacade.probe :: {missing}"
        ]


def test_dynamic_reference_exemptions_are_scoped_and_have_zero_slack(
    tmp_path, monkeypatch
):
    rel = "backend/app/services/startup_warmup.py"
    monkeypatch.setattr(
        _GUARD,
        "DYNAMIC_FACADE_REFERENCE_EXEMPTIONS",
        {(rel, "clear", "cache_clear"): "receiver is the cached factory"},
    )
    probe = "def {fn}(repository):\n    getattr(repository, 'cache_clear', None)\n"
    root = _facade_root(
        tmp_path, facade_body=_LIVE_BODY, allowed=_LIVE_ALLOWED,
        app_files={rel: probe.format(fn="clear")},
    )
    assert _GUARD.dynamic_facade_reference_violations(root) == []

    # the same name in another function of the same file, or in another
    # file, is not covered by the exemption
    root = _facade_root(
        tmp_path, facade_body=_LIVE_BODY, allowed=_LIVE_ALLOWED,
        app_files={
            rel: probe.format(fn="clear") + probe.format(fn="other_use"),
            "backend/app/services/elsewhere.py": probe.format(fn="clear"),
        },
    )
    assert _GUARD.dynamic_facade_reference_violations(root) == [
        "literal getattr/hasattr on a facade receiver names a missing repository "
        "attribute: backend/app/services/elsewhere.py :: clear :: cache_clear",
        "literal getattr/hasattr on a facade receiver names a missing repository "
        f"attribute: {rel} :: other_use :: cache_clear",
    ]

    root = _facade_root(
        tmp_path, facade_body=_LIVE_BODY, allowed=_LIVE_ALLOWED,
        app_files={
            rel: "def clear(repository):\n    pass\n",
            "backend/app/services/elsewhere.py": "",
        },
    )
    assert _GUARD.dynamic_facade_reference_violations(root) == [
        "dynamic facade reference exemption is stale, remove: "
        f"{rel} :: clear :: cache_clear"
    ]


def test_protocols_bound_to_the_facade_must_match_it(tmp_path):
    root = _facade_root(tmp_path, facade_body=_LIVE_BODY, allowed=_LIVE_ALLOWED)
    assert _GUARD.facade_protocol_bindings(root) == [
        ("backend/app/api/deps.py", "memory_service", "MemoryRepository"),
    ]
    assert _GUARD.facade_protocol_binding_violations(root, {}) == []

    # retiring a facade method a bound protocol (here through a protocol
    # base) still declares
    root = _facade_root(
        tmp_path, facade_body="    def other(self): pass\n", allowed={"other"}
    )
    assert _GUARD.facade_protocol_binding_violations(root, {}) == [
        "protocol MemoryRepository is bound to the facade but declares methods "
        "RepositoryFacade lacks: kept"
    ]
    # a registered gap is accepted, and must be removed once it closes
    assert _GUARD.facade_protocol_binding_violations(
        root, {"MemoryRepository": ["kept"]}
    ) == []
    root = _facade_root(tmp_path, facade_body=_LIVE_BODY, allowed=_LIVE_ALLOWED)
    assert _GUARD.facade_protocol_binding_violations(
        root, {"MemoryRepository": ["kept"], "Unbound": ["x"]}
    ) == [
        "facade protocol gap list for MemoryRepository is stale, remove: kept "
        "(update scripts/architecture_boundary_baseline.json :: "
        "facade_protocol_binding_gaps)",
        "facade protocol gap list names an unbound protocol, remove: Unbound "
        "(update scripts/architecture_boundary_baseline.json :: "
        "facade_protocol_binding_gaps)",
    ]

    # the ``-> P: return repository()`` form binds too
    root = _facade_root(
        tmp_path, facade_body="    def other(self): pass\n", allowed={"other"},
        deps=(
            "from app.repositories.ports import MemoryRepository\n"
            "def repository():\n"
            "    return object()\n"
            "def source_repository() -> MemoryRepository:\n"
            "    return repository()\n"
        ),
    )
    assert _GUARD.facade_protocol_binding_violations(root, {}) == [
        "protocol MemoryRepository is bound to the facade but declares methods "
        "RepositoryFacade lacks: kept"
    ]


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


@pytest.mark.parametrize(
    "source",
    [
        "import importlib\n"
        "def codec():\n"
        "    return importlib.import_module('app.services.kg.scale_index')\n",
        "from importlib import import_module\n"
        "CODEC = import_module('app.services.kg')\n",
        "def codec():\n"
        "    return __import__('app.services.kg.viz_index')\n",
    ],
)
def test_repository_service_ceiling_counts_literal_dynamic_imports(tmp_path, source):
    """Every ceiling is 0 now, so the remaining way to re-grow a reverse edge
    without a reviewer noticing is spelling it at run time. A literal
    ``import_module`` / ``__import__`` of ``app.services...`` counts exactly
    like an import statement.

    Mutation anchor: drop the dynamic-import branch from
    ``repository_service_import_counts`` and every case goes green.
    """
    app = tmp_path / "app"
    _write(app / "repositories/filesystem/artifact_store.py", source)

    assert repository_service_import_violations(
        app, {"sqlite": 0, "postgres": 0, "other": 0}
    ) == ["repositories/other service imports grew: 1 > 0"]


def test_repository_service_ceiling_ignores_unrelated_dynamic_imports(tmp_path):
    """A driver probe (``read_budget``'s fail-soft psycopg import) or a
    non-literal module name is not a services edge and must not count."""
    app = tmp_path / "app"
    _write(
        app / "repositories/read_budget.py",
        "from importlib import import_module\n"
        "def probe(name):\n"
        "    import_module('psycopg.errors')\n"
        "    return import_module(name)\n",
    )

    assert repository_service_import_violations(
        app, {"sqlite": 0, "postgres": 0, "other": 0}
    ) == []


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
    would make the function-length ceiling check vacuously pass --
    ``function_length_violations`` iterates the *baseline* ceilings, not the
    repository, so an empty baseline finds nothing to check. This reads the
    real checked-in baseline and pins the function-length half to a
    non-trivial lower bound.

    The core/models allowlist half works differently, and is legitimately
    empty now -- both debt edges it used to carry (``app.core.llm ->
    app.services.cancellation`` and ``app.models.agent_profile ->
    app.services.agent_profile_block``) were fixed by moving the shared
    definitions into ``app.domain``/``app.models`` respectively. Unlike the
    function-length loop, ``core_models_service_import_violations`` scans the
    *live repository* (``core_models_service_import_edges``) and diffs it
    against the allowlist in both directions, so an empty allowlist does not
    make that check vacuously pass -- it would flag any real live edge as a
    fresh violation instead. What an empty allowlist cannot protect against
    on its own is a *normalization* regression, where a real edge gets
    stringified into a different key on each read and the diff silently
    no-ops both ways; that guarantee is pinned separately by
    ``test_core_models_service_import_edges_normalize_to_the_submodule``.
    This test's own job is narrower: assert the live edge set really is
    empty right now, using the same ``core_models_service_import_edges`` the
    guard itself calls, so a baseline that went empty for the wrong reason
    (someone deleting the allowlist entries rather than the edges actually
    being gone) still fails loudly here.
    """
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))

    assert len(baseline["function_length_ceiling"]) >= 18
    assert set(baseline["core_models_service_imports"]["allowed"]) == set()
    assert core_models_service_import_edges(ROOT / "backend" / "app") == set()


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


def _write_ports(root: Path, source: str) -> None:
    _write(root / "backend" / "app" / "repositories" / "ports.py", source)


def test_ports_protocol_method_count_ceiling_only_allows_reduction(tmp_path):
    """Mirrors ``test_function_length_ceiling_only_allows_reduction``: zero
    slack in both directions on ``ports.py``'s total Protocol method count,
    same rationale as the hot-function ceiling above it -- an only-grow
    ceiling would let a later regression spend headroom a prior shrink left
    unclaimed.
    """
    _write_ports(
        tmp_path,
        "from typing import Protocol\n\n\n"
        "class Store(Protocol):\n"
        "    def a(self): ...\n"
        "    def b(self): ...\n",
    )  # 2 protocol methods

    assert ports_method_count_violations(tmp_path, 2) == []

    _write_ports(
        tmp_path,
        "from typing import Protocol\n\n\n"
        "class Store(Protocol):\n"
        "    def a(self): ...\n"
        "    def b(self): ...\n"
        "    def c(self): ...\n",
    )  # 3 protocol methods
    assert ports_method_count_violations(tmp_path, 2) == [
        "ports.py gained protocol methods: 3 > 2 "
        "(update scripts/architecture_boundary_baseline.json :: "
        "ports_protocol_method_count)"
    ]

    # The ceiling has zero slack: fewer methods than recorded must have the
    # baseline lowered in the same change.
    assert ports_method_count_violations(tmp_path, 4) == [
        "ports.py protocol method ceiling is stale, lower it: 3 < 4 "
        "(update scripts/architecture_boundary_baseline.json :: "
        "ports_protocol_method_count)"
    ]

    # Fourth arm: a plain (non-Protocol) class's methods must NOT count
    # toward the ceiling -- e.g. the handful of exception classes this file
    # also defines (``ConversationBusyError`` and friends). The ceiling
    # below is exactly the 3 Protocol methods above; if ``PlainError``'s
    # method leaked into the count too, this would trip the growth branch
    # instead of matching the unchanged ceiling.
    _write_ports(
        tmp_path,
        "from typing import Protocol\n\n\n"
        "class Store(Protocol):\n"
        "    def a(self): ...\n"
        "    def b(self): ...\n"
        "    def c(self): ...\n\n\n"
        "class PlainError(Exception):\n"
        "    def extra_method(self): ...\n",
    )
    assert ports_method_count_violations(tmp_path, 3) == []


def test_ports_protocol_method_count_ignores_module_level_functions(tmp_path):
    """Module-level ``def`` (e.g. ``project_run_row``) never counts -- only a
    method nested directly inside a ``ClassDef`` that subclasses ``Protocol``
    does."""
    _write_ports(
        tmp_path,
        "from typing import Protocol\n\n\n"
        "def free_function():\n"
        "    pass\n\n\n"
        "class Store(Protocol):\n"
        "    def a(self): ...\n"
        "    def b(self): ...\n",
    )
    assert ports_method_count_violations(tmp_path, 2) == []

    # Adding a THIRD module-level function still must not move the count.
    _write_ports(
        tmp_path,
        "from typing import Protocol\n\n\n"
        "def free_function():\n"
        "    pass\n\n\n"
        "def another_free_function():\n"
        "    pass\n\n\n"
        "class Store(Protocol):\n"
        "    def a(self): ...\n"
        "    def b(self): ...\n",
    )
    assert ports_method_count_violations(tmp_path, 2) == []


def test_ports_protocol_method_count_resolves_protocol_import_aliases(tmp_path):
    """An aliased ``Protocol`` import must not dodge the ceiling (codex #566
    R2 P2): ``from typing import Protocol as P``, ``import typing_extensions
    as te`` + ``te.Protocol``, and the subscripted ``P[int]`` all count; a
    plain class whose base merely *looks* unrelated does not."""
    _write_ports(
        tmp_path,
        "from typing import Protocol as P\n"
        "import typing_extensions as te\n\n\n"
        "class A(P):\n"
        "    def a(self): ...\n\n\n"
        "class B(te.Protocol):\n"
        "    def b(self): ...\n"
        "    def b2(self): ...\n\n\n"
        "class C(P[int]):\n"
        "    def c(self): ...\n\n\n"
        "class Plain:\n"
        "    def not_counted(self): ...\n",
    )
    assert ports_method_count_violations(tmp_path, 4) == []
    assert ports_method_count_violations(tmp_path, 3) == [
        "ports.py gained protocol methods: 4 > 3 "
        "(update scripts/architecture_boundary_baseline.json :: ports_protocol_method_count)"
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
    _write(app / "services/repository_factory.py", "from app import extensions\n")
    _write(app / "repositories/other_factory.py", "from app import extensions\n")

    assert boundary_violations(app) == [
        "app.repositories.other_factory imports the extension composition "
        "surface outside an approved root",
        "app.services.repository_factory imports the extension composition "
        "surface outside an approved root",
    ]


def test_registry_composition_does_not_change_route_topology(monkeypatch):
    """An *empty* plugin topology adds no routes and moves no existing one.

    Scope note: this asserts the zero-plugin case only — that the registry
    composition seam itself is route-neutral. What a *non-empty* topology is
    allowed to mount (prefix, router-level auth, notebook gates, refusals) is
    covered by ``backend/tests/test_extension_plugin_routes.py``, not here.

    The stand-in must therefore answer the two reads ``create_app`` now makes
    of the registry — ``contributions(point)`` and ``manifests()`` — plus carry
    an empty ``plugin_settings``. A bare ``object()`` would raise instead of
    proving anything.
    """

    from app import main as app_main

    baseline = app_main.create_app()

    class _EmptyRegistry:
        def contributions(self, point):
            return ()

        def manifests(self):
            return ()

    sentinel = _EmptyRegistry()
    monkeypatch.setattr(
        app_main,
        "application_extension_runtime",
        lambda: type(
            "Runtime",
            (),
            {"registry": sentinel, "plugin_settings": {}},
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


def test_extension_boundary_is_present_in_canonical_development_documents():
    for name in (
        "docs/development.md",
        "docs/development_zh.md",
    ):
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


def test_ask_application_stage_boundary_is_in_canonical_development_documents():
    for name in (
        "docs/development.md",
        "docs/development_zh.md",
    ):
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


def test_report_stage_and_post_terminal_boundary_is_in_canonical_development_documents():
    for name in (
        "docs/development.md",
        "docs/development_zh.md",
    ):
        normalized = (
            (ROOT / name)
            .read_text(encoding="utf-8")
            .casefold()
            .replace("_", " ")
            .replace("-", " ")
        )
        assert "report" in normalized, name
        assert "stage" in normalized, name
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
