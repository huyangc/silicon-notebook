"""Unit pins for ``scripts/audit_component_callers.py``.

The full census over the real tree runs as ``--check`` in the contracts lane
(``scripts/check_contracts.sh``); these pin the pieces that decide what counts
as a reference and when the gate goes red, without the ~8s whole-tree scan.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "audit_component_callers", ROOT / "scripts" / "audit_component_callers.py"
)
audit = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules.setdefault(_SPEC.name, audit)  # dataclasses resolve their module
_SPEC.loader.exec_module(audit)


def _name_arg(source: str) -> ast.AST:
    call = ast.parse(source, mode="eval").body
    assert isinstance(call, ast.Call)
    return call.args[1]


def test_run_time_names_built_from_literal_pieces_reference_every_fitting_member():
    chain = audit._template_regex(_name_arg('getattr(jobs, f"delete_{chain}_page")'))
    assert chain is not None
    assert chain.match("delete_source_elements_page")
    assert not chain.match("delete_source_elements")
    migration = audit._template_regex(_name_arg('getattr(self, "_migration_" + str(v))'))
    assert migration is not None and migration.match("_migration_56")
    # A name with no literal piece at all cannot be attributed to anything.
    assert audit._template_regex(_name_arg("getattr(obj, name)")) is None
    assert audit._template_regex(_name_arg('getattr(obj, f"{name}")')) is None


def _refs(source: str, rel: str = "backend/app/x.py", index=None):
    collector = audit._RefCollector(rel, index or {})
    collector.visit(ast.parse(source))
    return collector.refs


def test_references_carry_their_enclosing_member_and_skip_docstrings():
    source = '''
class A:
    """mentions helper in prose only"""

    def m(self):
        """calls helper, says the docstring"""
        self.helper()
        return getattr(self, "probe")

    @property
    def p(self):
        return 1

    @p.setter
    def p(self, value):
        self._p = value
'''
    index = {("A", "m"): "backend/app/x.py::A.m", ("A", "p"): "backend/app/x.py::A.p"}
    refs = _refs(source, index=index)
    helper = [r for r in refs if r.name == "helper"]
    assert [r.owner for r in helper] == ["backend/app/x.py::A.m"]
    assert [r.owner for r in refs if r.name == "probe"] == ["backend/app/x.py::A.m"]
    # ``@p.setter`` defines the property, it does not read it.
    assert not [r for r in refs if r.name == "p"]


def test_dotted_entry_point_strings_reference_their_last_segment():
    refs = _refs('ENTRY = "pkg.module:Factory.build"\n')
    assert {"pkg", "module", "Factory", "build"} <= {r.name for r in refs}


def _report(*candidates):
    return {
        "summary": {},
        "candidates": [
            {"member": key, "bucket": bucket, "extension_surface": surface, "reason": ""}
            for key, bucket, surface in candidates
        ],
        "exempt": [],
    }


OBSERVED = "backend/app/services/pending_bus.py::PendingBus._buffered_count"
UNMARKED = "backend/app/repositories/sqlite/unified_kg_store.py::UnifiedKgStore.community_overview"


def test_check_passes_when_every_candidate_is_registered_once():
    report = _report((OBSERVED, "test-only", False))
    allowlist = {OBSERVED: {"member": OBSERVED, "category": "test-observation", "reason": "r"}}
    assert audit.check(report, allowlist) == []


@pytest.mark.parametrize(
    ("candidates", "allowlist", "fragment"),
    [
        pytest.param(
            [("backend/app/a.py::A.new", "test-only", False)], {},
            "test-only component member", id="unregistered-new-member",
        ),
        pytest.param(
            [], {"backend/app/a.py::A.gone": {"category": "test-observation", "reason": "r"}},
            "no longer a candidate", id="stale-entry",
        ),
        pytest.param(
            [("backend/app/a.py::A.port", "test-only", False)],
            {"backend/app/a.py::A.port": {"category": "extension-port", "reason": "r"}},
            "not extension surface", id="extension-port-off-surface",
        ),
        pytest.param(
            [("backend/app/a.py::A.x", "test-only", False)],
            {"backend/app/a.py::A.x": {"category": "keep-forever", "reason": "r"}},
            "unknown category", id="unknown-category",
        ),
        pytest.param(
            [("backend/app/a.py::A.x", "test-only", False)],
            {"backend/app/a.py::A.x": {"category": "pending-decision", "reason": " "}},
            "without a reason", id="missing-reason",
        ),
        pytest.param(
            [(UNMARKED, "test-only", False)],
            {UNMARKED: {"category": "test-observation", "reason": "r"}},
            "carries no", id="observation-without-marker",
        ),
    ],
)
def test_check_fails_on_every_drift(candidates, allowlist, fragment):
    problems = audit.check(_report(*candidates), allowlist)
    assert problems and any(fragment in problem for problem in problems), problems


def test_observation_marker_is_read_from_the_comment_above_the_def():
    assert audit.carries_observation_marker(OBSERVED)
    assert not audit.carries_observation_marker(UNMARKED)


def test_allowlist_is_sorted_unique_and_fully_described():
    data = json.loads(audit.ALLOWLIST_PATH.read_text(encoding="utf-8"))
    keys = [entry["member"] for entry in data["members"]]
    assert keys == sorted(keys)
    assert len(keys) == len(set(keys))
    for entry in data["members"]:
        assert entry["category"] in audit.CATEGORIES, entry
        assert entry["reason"].strip(), entry
        rel = entry["member"].split("::", 1)[0]
        assert (ROOT / rel).is_file(), entry


# ---------------------------------------------------------------- classification


def _member(cls: str, name: str) -> "audit.Member":
    return audit.Member("backend/app/x.py", "app.x", cls, name, 1)


def _ref(name: str, owner: "audit.Member | None", rel: str = "backend/app/x.py") -> "audit.Ref":
    return audit.Ref(name, None, rel, 1, owner.key if owner else None)


def _classify(members, prod, tests, kept=()):
    return audit.classify_members(
        members, prod, tests,
        exempt_reason=lambda _m: None, importable=lambda _m: True, kept=set(kept),
    )


def test_a_member_called_only_from_a_test_only_member_is_a_candidate_too():
    a, b, c = _member("A", "a"), _member("B", "b"), _member("C", "c")
    # production: b's body calls a, module code uses c; only tests call b.
    prod = [_ref("a", b), _ref("c", None)]
    tests = [_ref("b", None, "backend/tests/test_x.py")]
    verdicts = _classify([a, b, c], prod, tests)
    assert verdicts[b.key].bucket == "test-only"
    assert verdicts[a.key].bucket == "unreferenced"  # its one caller is dead
    assert verdicts[c.key].bucket == "keep"


def test_the_fixed_point_follows_a_chain_of_any_length():
    """c is called only by b, b only by a, a only by tests: one pass would
    still see b's body as live and keep c; only the fixed point files it."""
    a, b, c = _member("A", "a"), _member("B", "b"), _member("C", "c")
    prod = [_ref("b", a), _ref("c", b)]
    tests = [_ref("a", None, "backend/tests/test_x.py")]
    verdicts = _classify([a, b, c], prod, tests)
    assert verdicts[a.key].bucket == "test-only"
    assert verdicts[b.key].bucket == "unreferenced"
    assert verdicts[c.key].bucket == "unreferenced"


def test_a_registered_member_keeps_what_it_calls_alive():
    a, b = _member("A", "a"), _member("B", "b")
    prod = [_ref("a", b)]
    tests = [_ref("b", None, "backend/tests/test_x.py")]
    verdicts = _classify([a, b], prod, tests, kept={b.key})
    assert verdicts[b.key].bucket == "test-only"  # still listed, registered
    assert verdicts[a.key].bucket == "keep"


def test_recursion_is_not_a_production_caller():
    a = _member("A", "a")
    verdicts = _classify([a], [_ref("a", a)], [_ref("a", None, "backend/tests/t.py")])
    assert verdicts[a.key].bucket == "test-only"


@pytest.mark.parametrize(
    ("decorator", "registers"),
    [
        ("field_validator", True), ("pydantic.model_validator", True),
        ("computed_field", True), ("router.get", True), ("app.command", True),
        ("atexit.register", True),
        ("get", False), ("contextmanager", False), ("contextlib.asynccontextmanager", False),
        ("functools.lru_cache", False), ("cache", False), ("functools.wraps", False),
    ],
)
def test_only_known_registering_decorators_exempt_a_member(decorator, registers):
    assert audit.is_registering_decorator(decorator) is registers


# ---------------------------------------------------------------- receivers


def _synthetic(source: str, monkeypatch):
    """Load ``source`` as an app module and return what the census needs to
    read it: its members, the receiver-aware production references and the
    by-name ones (the S2-D reading)."""
    return _synthetic_files([("app", source)], monkeypatch)


def _synthetic_files(sources, monkeypatch):
    """``_synthetic`` over several modules: ``(package, source)`` pairs, all
    named ``<package>._census_fixture_<tag>`` with one shared tag (so
    ``app.repositories.sqlite`` / ``app.repositories.postgres`` give adapter
    twins; a package used twice adds ``_<i>``).  ``MOD<i>`` in a source stands for the i-th module's name; every
    file is indexed before any reference is read, as ``prepare`` does.
    Members are keyed ``<i>:Class.name`` past the first module."""
    import types as _types
    import uuid

    tag = uuid.uuid4().hex[:12]
    packages = [package for package, _ in sources]
    names = [
        f"{package}._census_fixture_{tag}" + (f"_{i}" if packages.count(package) > 1 else "")
        for i, package in enumerate(packages)
    ]
    loaded = []
    for name, (_package, source) in zip(names, sources):
        for i, other in enumerate(names):
            source = source.replace(f"MOD{i}", other)
        rel = f"backend/{name.replace('.', '/')}.py"
        module = _types.ModuleType(name)
        module.__file__ = str(ROOT / rel)
        monkeypatch.setitem(sys.modules, name, module)
        exec(compile(source, rel, "exec"), module.__dict__)
        loaded.append((rel, ast.parse(source)))
    by_key: dict = {}
    order: list = []
    for rel, tree in loaded:
        audit.members_of_tree(rel, tree, by_key, order)
    members = [by_key[k] for k in order]
    index = audit.ReceiverIndex(members)
    for rel, tree in loaded:
        index.contexts[rel] = audit.FileContext(index, rel, tree)
    refs: list = []
    by_name_refs: list = []
    for rel, tree in loaded:
        keys = {(m.qualname, m.name): m.key for m in members if m.rel == rel}
        collector = audit._RefCollector(rel, keys, context=index.contexts[rel])
        collector.visit(tree)
        refs.extend(collector.refs)
        by_name = audit._RefCollector(rel, keys, receivers=False)
        by_name.visit(tree)
        by_name_refs.extend(by_name.refs)
    rels = [rel for rel, _ in loaded]
    labelled = {
        (m.qualname + "." + m.name if m.rel == rels[0] else f"{rels.index(m.rel)}:{m.qualname}.{m.name}"): m
        for m in members
    }
    return labelled, refs, by_name_refs


def _test_ref(member) -> "audit.Ref":
    return audit.Ref(member.name, None, "backend/tests/test_x.py", 1, None)


RECEIVERS = '''
class Store:
    def fetch(self):
        return 1


class Cache:
    def fetch(self):
        return 2


class Service:
    def __init__(self, store: Store) -> None:
        self.store = store

    def run(self):
        return self.store.fetch()


def build() -> Service:
    return Service(Store())


def main() -> int:
    return build().run()
'''


def test_same_name_members_of_different_classes_are_told_apart(monkeypatch):
    """``Store.fetch`` and ``Cache.fetch`` share a name. Production reaches
    ``Store.fetch`` through an annotated constructor parameter stored on
    ``self``; only a test calls ``Cache.fetch``. Read by name, the live one
    hides the dead one; read through the receiver, they separate."""
    members, refs, by_name_refs = _synthetic(RECEIVERS, monkeypatch)
    store, cache = members["Store.fetch"], members["Cache.fetch"]
    tests = [_test_ref(cache)]
    verdicts = _classify(list(members.values()), refs, tests)
    assert verdicts[store.key].bucket == "keep"
    assert verdicts[cache.key].bucket == "test-only"
    masked = _classify(list(members.values()), by_name_refs, tests)
    assert masked[cache.key].bucket == "keep"  # the S2-D blind spot


def test_an_unresolved_receiver_counts_for_every_member_of_the_name(monkeypatch):
    members, refs, _ = _synthetic(
        RECEIVERS + '''

def anything(thing):
    return thing.fetch()
''',
        monkeypatch,
    )
    fetches = [r for r in refs if r.name == "fetch" and r.targets is None]
    assert [r.fallback for r in fetches] == ["receiver type unknown"]
    verdicts = _classify(list(members.values()), refs, [_test_ref(members["Cache.fetch"])])
    assert verdicts[members["Cache.fetch"].key].bucket == "keep"
    assert verdicts[members["Store.fetch"].key].bucket == "keep"


def test_a_construction_site_types_an_injected_attribute(monkeypatch):
    """An unannotated ``__init__`` parameter takes the type of what every
    production construction passes; an annotated one is widened by them
    (a partial fake handed in as a Protocol)."""
    members, refs, _ = _synthetic(
        RECEIVERS.replace("store: Store", "store")
        + '''

def build_cached() -> Service:
    return Service(Cache())
''',
        monkeypatch,
    )
    verdicts = _classify(list(members.values()), refs, [])
    assert verdicts[members["Store.fetch"].key].bucket == "keep"
    assert verdicts[members["Cache.fetch"].key].bucket == "keep"
    fetch = next(r for r in refs if r.name == "fetch")
    assert fetch.targets == {members["Store.fetch"].key, members["Cache.fetch"].key}


def test_a_protocol_receiver_reaches_its_implementors_and_their_declarations(monkeypatch):
    members, refs, _ = _synthetic(
        '''
from typing import Protocol


class Port(Protocol):
    def fetch(self) -> int: ...
    def put(self) -> None: ...


class Full:
    def fetch(self):
        return 1

    def put(self):
        return None


class Unrelated:
    def fetch(self):
        return 2


def use(port: Port) -> int:
    return port.fetch()


def direct() -> int:
    return Full().put() or 0
''',
        monkeypatch,
    )
    verdicts = _classify(list(members.values()), refs, [_test_ref(members["Unrelated.fetch"])])
    assert verdicts[members["Port.fetch"].key].bucket == "keep"
    assert verdicts[members["Full.fetch"].key].bucket == "keep"
    assert verdicts[members["Unrelated.fetch"].key].bucket == "test-only"
    # ``Full().put()`` reaches ``Full.put`` and the declaration it implements.
    assert verdicts[members["Port.put"].key].bucket == "keep"


def test_a_bare_name_references_a_member_only_inside_its_class_body(monkeypatch):
    members, refs, _ = _synthetic(
        '''
class A:
    def helper(self):
        return 1

    alias = helper


class B:
    def helper(self):
        return 2


def module_function():
    helper = 3  # a local that happens to share the name
    return helper
''',
        monkeypatch,
    )
    verdicts = _classify(list(members.values()), refs, [_test_ref(members["B.helper"])])
    assert verdicts[members["A.helper"].key].bucket == "keep"
    assert verdicts[members["B.helper"].key].bucket == "test-only"


def test_a_registered_member_keeps_the_protocol_declaration_it_implements(monkeypatch):
    members, refs, _ = _synthetic(
        '''
from typing import Protocol


class Port(Protocol):
    def fetch(self) -> int: ...


class Impl:
    def fetch(self):
        return 1
''',
        monkeypatch,
    )
    impl, port = members["Impl.fetch"], members["Port.fetch"]
    receivers = audit.ReceiverIndex(list(members.values()))
    extra = audit.registered_declaration_refs({impl.key}, list(members.values()), receivers)
    assert [(r.owner, r.targets) for r in extra] == [(impl.key, frozenset({port.key}))]
    tests = [_test_ref(impl)]
    unregistered = _classify(list(members.values()), refs, tests)
    assert unregistered[port.key].bucket == "test-only"  # the test's by-name reference
    registered = _classify(list(members.values()), refs + extra, tests, kept={impl.key})
    assert registered[port.key].bucket == "keep"


TWO_FETCHES = '''
class Store:
    def fetch(self):
        return 1


class Cache:
    def fetch(self):
        return 2
'''


def _fetch_refs(refs):
    return [r for r in refs if r.name == "fetch"]


def _buckets(members, refs, *test_only_names):
    tests = [_test_ref(members[name]) for name in test_only_names]
    verdicts = _classify(list(members.values()), refs, tests)
    return {name: verdicts[m.key].bucket for name, m in members.items()}


HOLDER = TWO_FETCHES + '''

class Holder:
    def __init__(self) -> None:
        self.store = None

    def run(self):
        return self.store.fetch()


def main():
    return Holder().run()
'''


def test_a_receiver_pinned_to_nothing_but_none_counts_by_name(monkeypatch):
    """Every binding of ``self.store`` the census sees is ``None``; the
    object it holds when read arrives some way the census does not read as
    a binding (``setattr`` on another object). Pinned to the empty set, the
    read used to reach no member at all."""
    members, refs, _ = _synthetic(
        HOLDER + '''

def wire(holder: Holder) -> None:
    setattr(holder, "store", Store())
''',
        monkeypatch,
    )
    assert [(r.targets, r.fallback) for r in _fetch_refs(refs)] == [(None, "receiver type empty")]
    buckets = _buckets(members, refs, "Store.fetch", "Cache.fetch")
    assert buckets["Store.fetch"] == buckets["Cache.fetch"] == "keep"


def test_a_store_through_an_untyped_receiver_makes_the_attribute_unknown(monkeypatch):
    """``holder.store = Store()`` with ``holder`` unannotated may be a store
    to ``Holder.store``: the census cannot tell, so ``self.store`` is not
    pinned to its visible bindings (``Cache``) but counts by name."""
    members, refs, _ = _synthetic(
        HOLDER.replace("self.store = None", "self.store = Cache()") + '''

def wire(holder):
    holder.store = Store()
''',
        monkeypatch,
    )
    assert [(r.targets, r.fallback) for r in _fetch_refs(refs)] == [(None, "receiver type unknown")]
    assert _buckets(members, refs, "Store.fetch")["Store.fetch"] == "keep"


MODULE_STORE_REGISTRY = TWO_FETCHES + '''

CURRENT = None
'''
MODULE_STORE_USER = '''
import MOD0 as registry


def install() -> None:
    registry.CURRENT = registry.Store()


def use() -> int:
    return registry.CURRENT.fetch()
'''


def test_a_store_into_a_module_global_types_it(monkeypatch):
    """``registry.CURRENT = Store()`` from another module: the census holds
    the ``None`` placeholder the module was loaded with, and the store is
    what the global holds when production reads it."""
    members, refs, _ = _synthetic_files(
        [("app", MODULE_STORE_REGISTRY), ("app", MODULE_STORE_USER)], monkeypatch
    )
    assert [r.targets for r in _fetch_refs(refs)] == [frozenset({members["Store.fetch"].key})]
    buckets = _buckets(members, refs, "Cache.fetch")
    assert buckets["Store.fetch"] == "keep"
    assert buckets["Cache.fetch"] == "test-only"


def test_a_module_global_stored_through_an_untyped_receiver_is_unknown(monkeypatch):
    members, refs, _ = _synthetic_files(
        [
            ("app", MODULE_STORE_REGISTRY),
            ("app", MODULE_STORE_USER + "\n\ndef swap(target):\n    target.CURRENT = target.Cache()\n"),
        ],
        monkeypatch,
    )
    assert [(r.targets, r.fallback) for r in _fetch_refs(refs)] == [(None, "receiver type unknown")]
    assert _buckets(members, refs, "Cache.fetch")["Cache.fetch"] == "keep"


def test_a_module_global_rebound_from_a_function_is_unknown(monkeypatch):
    members, refs, _ = _synthetic_files(
        [
            ("app", MODULE_STORE_REGISTRY + '''

def swap() -> None:
    global CURRENT
    CURRENT = Cache()
'''),
            ("app", MODULE_STORE_USER),
        ],
        monkeypatch,
    )
    assert [(r.targets, r.fallback) for r in _fetch_refs(refs)] == [(None, "receiver type unknown")]
    assert _buckets(members, refs, "Cache.fetch")["Cache.fetch"] == "keep"


INJECTED = TWO_FETCHES + '''

class Service:
    def __init__(self, store):
        self.store = store

    def run(self):
        return self.store.fetch()


def build() -> Service:
    return Service(Store())


def main():
    return build().run()
'''


@pytest.mark.parametrize(
    "construction",
    [
        pytest.param("Factory = Service\n\n\ndef other():\n    return Factory(Cache())\n", id="alias"),
        pytest.param(
            "import functools\n\n\ndef other():\n    return functools.partial(Service, Cache())()\n",
            id="partial",
        ),
        pytest.param(
            'MAKERS = {"service": Service}\n\n\ndef other():\n    return MAKERS["service"](Cache())\n',
            id="registry",
        ),
        pytest.param("def other(service: Service):\n    return type(service)(Cache())\n", id="type-of"),
        pytest.param("def other(service: Service):\n    return service.__class__(Cache())\n", id="dunder-class"),
        pytest.param("def register(cls):\n    return cls\n\n\n@register\nclass Sub(Service):\n    pass\n", id="decorator"),
    ],
)
def test_a_class_constructed_other_than_by_its_name_leaves_its_parameters_unknown(
    monkeypatch, construction
):
    """``Service(Store())`` is the one call spelled ``Service``. Built any
    other way, the class may receive a ``Cache`` the census never sees, so
    its unannotated ``store`` parameter stays unknown and ``fetch`` counts
    by name (with only the spelled call, it reaches ``Store.fetch`` alone:
    ``test_a_construction_site_types_an_injected_attribute``)."""
    members, refs, _ = _synthetic(INJECTED + "\n\n" + construction, monkeypatch)
    assert [(r.targets, r.fallback) for r in _fetch_refs(refs)] == [(None, "receiver type unknown")]
    assert _buckets(members, refs, "Cache.fetch")["Cache.fetch"] == "keep"


def test_passing_positions_do_not_count_as_constructions(monkeypatch):
    """Naming the class where its object is not handed on -- an annotation,
    ``isinstance``, a class attribute, an ``except`` -- keeps the spelled
    calls the only constructions."""
    members, refs, _ = _synthetic(
        INJECTED
        + '''

class Other(Exception):
    pass


def check(value: "Service | None", items: list[Service]) -> bool:
    typed: Service = value
    try:
        return isinstance(typed, (Service, Store)) and Service.run is not None
    except Other:
        return False
''',
        monkeypatch,
    )
    assert [r.targets for r in _fetch_refs(refs)] == [frozenset({members["Store.fetch"].key})]
    assert _buckets(members, refs, "Cache.fetch")["Cache.fetch"] == "test-only"


def test_a_base_typed_receiver_reaches_every_subclass_override(monkeypatch):
    members, refs, _ = _synthetic(
        '''
class Base:
    def fetch(self):
        return 1


class Child(Base):
    def fetch(self):
        return 2


class Other:
    def fetch(self):
        return 3


def use(base: Base) -> int:
    return base.fetch()
''',
        monkeypatch,
    )
    buckets = _buckets(members, refs, "Child.fetch", "Other.fetch")
    assert buckets["Base.fetch"] == buckets["Child.fetch"] == "keep"
    assert buckets["Other.fetch"] == "test-only"


def test_an_adapter_class_reaches_its_twin_in_the_other_adapter(monkeypatch):
    """``repositories/sqlite/x.py::C.m`` and ``repositories/postgres/x.py::C.m``
    are one member as far as callers go: production holds one adapter or the
    other, and code typed against one is the code path of both."""
    store = '''
class Store:
    def fetch(self):
        return 1

    def unused(self):
        return 0
'''
    members, refs, _ = _synthetic_files(
        [
            ("app.repositories.sqlite", store + "\n\ndef use() -> int:\n    return Store().fetch()\n"),
            ("app.repositories.postgres", store),
            ("app", "class Cache:\n    def fetch(self):\n        return 2\n"),
        ],
        monkeypatch,
    )
    buckets = _buckets(members, refs, "1:Store.fetch", "2:Cache.fetch")
    assert buckets["Store.fetch"] == buckets["1:Store.fetch"] == "keep"
    assert buckets["2:Cache.fetch"] == "test-only"
    assert buckets["1:Store.unused"] == "unreferenced"


def test_a_class_with_getattr_counts_by_name(monkeypatch):
    members, refs, _ = _synthetic(
        TWO_FETCHES
        + '''

class Proxy:
    def __getattr__(self, name):
        return getattr(Store(), name)


def use(proxy: Proxy) -> int:
    return proxy.fetch()
''',
        monkeypatch,
    )
    assert [(r.targets, r.fallback) for r in _fetch_refs(refs)] == [(None, "__getattr__")]
    buckets = _buckets(members, refs, "Store.fetch", "Cache.fetch")
    assert buckets["Store.fetch"] == buckets["Cache.fetch"] == "keep"


def test_a_data_attribute_of_the_pinned_type_reaches_no_member(monkeypatch):
    members, refs, _ = _synthetic(
        TWO_FETCHES
        + '''

class Settings:
    def __init__(self) -> None:
        self.fetch = 0


def use(settings: Settings) -> int:
    return settings.fetch
''',
        monkeypatch,
    )
    # The store in ``__init__`` and the read: both resolve to the instance
    # attribute, not to a method of that name elsewhere.
    assert [r.targets for r in _fetch_refs(refs)] == [frozenset(), frozenset()]
    buckets = _buckets(members, refs, "Store.fetch")
    assert buckets["Store.fetch"] == buckets["Cache.fetch"] == "test-only"


def test_a_return_annotation_types_the_call(monkeypatch):
    members, refs, _ = _synthetic(
        TWO_FETCHES
        + '''

def make() -> Store:
    return Store()


def use() -> int:
    return make().fetch()
''',
        monkeypatch,
    )
    assert [r.targets for r in _fetch_refs(refs)] == [frozenset({members["Store.fetch"].key})]
    assert _buckets(members, refs, "Cache.fetch")["Cache.fetch"] == "test-only"


def test_the_fixture_generator_is_scanned_for_calls_but_not_for_strings():
    source = 'repo.begin_ask_job(nb)\nOWNERS = {"stale_member": "Store"}\n'
    data = audit._RefCollector("scripts/x.py", {}, receivers=True, strings_are_data=True)
    data.visit(ast.parse(source))
    assert {r.name for r in data.refs} == {"begin_ask_job"}
    plain = audit._RefCollector("scripts/x.py", {}, receivers=True)
    plain.visit(ast.parse(source))
    assert {"begin_ask_job", "stale_member"} <= {r.name for r in plain.refs}
    assert "scripts/generate_repository_contract_fixtures.py" in audit.STRING_DATA_FILES


def test_the_facade_layer_is_censused_and_only_its_pinned_surface_is_exempt():
    facade = audit.facade_classes()
    assert ("app.services.repository_facade", "RepositoryFacade") in facade
    assert ("app.services.sqlite_repository", "SQLiteRepository") in facade
    assert ("app.services.postgres_repository", "PostgresRepository") in facade
    surface = audit.facade_surface_names()
    by_key: dict = {}
    order: list = []
    tree = audit._parse(ROOT / audit.FACADE_REL)
    audit.members_of_tree(audit.FACADE_REL, tree, by_key, order)
    members = [by_key[k] for k in order if by_key[k].qualname == "RepositoryFacade"]
    assert members, "RepositoryFacade members are part of the census"
    pinned = next(m for m in members if m.name in surface)
    unpinned = next(m for m in members if m.name not in surface and m.name.startswith("_"))
    assert audit.exemption_reason(pinned, facade, surface) == (
        "facade public surface (facade_surface.json)"
    )
    assert audit.exemption_reason(unpinned, facade, surface) is None


def test_extension_surface_needs_the_whole_protocol_not_one_shared_name():
    from typing import Protocol

    class Port(Protocol):
        def search(self) -> None: ...
        def fetch(self) -> None: ...

    class Full:
        def search(self):
            return None

        def fetch(self):
            return None

    class SharesOneName:
        def search(self):
            return None

    protocol_members = [(Port, audit._own_members(Port))]
    member = audit.Member("backend/app/services/x.py", "app.services.x", "C", "search", 1)
    assert audit._is_extension_surface(member, Full, protocol_members, set())
    assert not audit._is_extension_surface(member, SharesOneName, protocol_members, set())
