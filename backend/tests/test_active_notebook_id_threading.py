"""The active notebook id reaches every library-seat floor -- PR-C, C8.

`retrieval.is_active_hit` normalises a hit's stamp through
`foreign_notebook_id`.  With an empty active id a row stamped with the active
notebook's own RAW id (PPR lane, generated-question hydrate, third-party
contributor rows) reads as a peer: the floor miscounts, and its "is there any
foreign row?" inert gate is defeated, so a single-notebook run stops being
byte-identical.  The id is therefore a REQUIRED argument everywhere, and this
file pins (1) that no signature grew a default back, (2) that every production
call site passes a run id -- never a literal -- and is registered here, and
(3) the behaviour on the MMR and quota branches with own-id-stamped rows.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.domain.retrieval import RetrievalSupport, RetrievedChunk
from app.repositories import ports
from app.services import chunk_federation as cf
from app.services import retrieval
from app.services.retrieval_service import RetrievalService

ACTIVE = "nb-active"
APP = Path(__file__).resolve().parents[1] / "app"

# name -> positional index of the active id (None: keyword-only).
_ID_ARGUMENT = {
    "is_active_hit": 1,
    "active_reserve_eligible": 1,
    "enforce_active_floor": None,
    "apply_active_reserve": None,
    "quota_fuse_baseline_first": None,
    "select_chunk_candidates": None,
    "_qualified_active": 1,
    "_withheld_active": None,
    "reasoning_order_for": 2,
    "reasoning_passage_order": None,
    "active_reserve_rule": 2,
    "library_reserve_rules": 2,
    "library_floor_rules": 1,
    "mix_reserve_rules": 3,
}

# Every production call site, as ``file::enclosing function -> callee``.  A new
# one must be added here, after checking it passes the run's real id.
_REGISTERED = {
    "services/ask_service.py::_assemble_reasoning_context -> reasoning_order_for",
    "services/ask_service.py::_assemble_structured_evidence -> reasoning_order_for",
    "services/ask_service.py::_chunk_retrieve -> mix_reserve_rules",
    "services/ask_service.py::_chunk_retrieve -> quota_fuse_baseline_first",
    "services/ask_service.py::_chunk_retrieve -> select_chunk_candidates",
    "services/ask_service.py::_chunk_synthesis_context -> active_reserve_rule",
    "services/chunk_federation.py::_select_baseline_then_supplements -> _withheld_active",
    "services/chunk_federation.py::_withheld_active -> is_active_hit",
    "services/chunk_federation.py::_withheld_active -> _qualified_active",
    "services/chunk_federation.py::_qualified_active -> active_reserve_eligible",
    "services/chunk_federation.py::apply_active_reserve -> enforce_active_floor",
    "services/chunk_federation.py::reasoning_order_for -> reasoning_passage_order",
    "services/reasoning_retrieval.py::_select_chunk_candidates -> select_chunk_candidates",
    "services/reasoning_retrieval.py::_rerank_chunk_selection -> apply_active_reserve",
    "services/report_engine.py::_section_passage_order -> reasoning_order_for",
    "services/retrieval.py::active_reserve_eligible -> is_active_hit",
    "services/retrieval.py::active_reserve_rule -> is_active_hit",
    "services/retrieval.py::active_reserve_rule.<lambda> -> active_reserve_eligible",
    "services/retrieval.py::mix_reserve_rules -> library_floor_rules",
    "services/retrieval.py::library_floor_rules -> active_reserve_rule",
    "services/retrieval.py::library_floor_rules -> library_reserve_rules",
    "services/retrieval.py::reasoning_passage_order -> library_floor_rules",
    "services/retrieval.py::quota_fuse_baseline_first -> enforce_active_floor",
    "services/retrieval.py::enforce_active_floor._active -> is_active_hit",
    "services/retrieval.py::enforce_active_floor -> active_reserve_eligible",
    "services/retrieval_service.py::select_chunk_candidates -> apply_active_reserve",
}


def _aliases(tree) -> dict:
    """``{local name: original}`` for every ``from x import f as g`` of a
    tracked function, so an alias cannot slip a call past the guard."""
    return {
        alias.asname: alias.name
        for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.asname and alias.name in _ID_ARGUMENT
    }


def _call_sites(root=APP):
    sites = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = _aliases(tree)
        stack: list[str] = []

        class _Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                stack.append(node.name)
                self.generic_visit(node)
                stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Lambda(self, node):
                stack.append("<lambda>")
                self.generic_visit(node)
                stack.pop()

            def visit_Call(self, node):
                func = node.func
                name = (func.attr if isinstance(func, ast.Attribute)
                        else getattr(func, "id", None))
                name = aliases.get(name, name)
                if name in _ID_ARGUMENT:
                    where = f"{path.relative_to(root)}::{'.'.join(stack)} -> {name}"
                    sites.append((where, name, node))
                self.generic_visit(node)

        _Visitor().visit(tree)
    return sites


def _id_argument(name, node):
    for keyword in node.keywords:
        if keyword.arg == "active_notebook_id":
            return keyword.value
    index = _ID_ARGUMENT[name]
    if index is not None and len(node.args) > index:
        return node.args[index]
    return None


def test_every_call_site_passes_the_run_id_and_is_registered():
    sites = _call_sites()
    assert {where for where, _name, _node in sites} == _REGISTERED
    for where, name, node in sites:
        value = _id_argument(name, node)
        assert value is not None, f"{where}: active id not passed"
        # The run's id, forwarded by name -- never a literal ("" is the trap).
        assert isinstance(value, ast.Name), (where, ast.unparse(value))
        assert value.id in {"notebook_id", "active_notebook_id"}, (
            where, value.id)


def test_an_import_alias_does_not_hide_a_call_site(tmp_path):
    (tmp_path / "mod.py").write_text(
        "from app.services.retrieval import enforce_active_floor as floor\n"
        "def f(rows):\n"
        "    return floor(rows, rows, 1, active_notebook_id='')\n",
        encoding="utf-8")
    sites = _call_sites(tmp_path)
    assert [where for where, _name, _node in sites] == [
        "mod.py::f -> enforce_active_floor"]


@pytest.mark.parametrize("function", [
    retrieval.is_active_hit,
    retrieval.active_reserve_eligible,
    retrieval.enforce_active_floor,
    retrieval.quota_fuse_baseline_first,
    cf.reasoning_order_for,
    retrieval.reasoning_passage_order,
    retrieval.active_reserve_rule,
    retrieval.library_reserve_rules,
    retrieval.library_floor_rules,
    retrieval.mix_reserve_rules,
    cf.apply_active_reserve,
    RetrievalService.select_chunk_candidates,
    *[
        getattr(port, "select_chunk_candidates")
        for _name, port in inspect.getmembers(ports, inspect.isclass)
        if "select_chunk_candidates" in vars(port)
    ],
], ids=lambda function: function.__qualname__)
def test_the_active_id_has_no_default(function):
    parameter = inspect.signature(function).parameters["active_notebook_id"]
    assert parameter.default is inspect.Parameter.empty


def test_both_candidate_ports_declare_the_keyword():
    declaring = [
        port for _name, port in inspect.getmembers(ports, inspect.isclass)
        if "select_chunk_candidates" in vars(port)
    ]
    assert len(declaring) == 2


# ------------------------------------------------------------------ behaviour
def _row(chunk_id, relevance, *, notebook_id="", origin="semantic"):
    return RetrievedChunk(
        chunk_id=chunk_id, source_id=f"s-{chunk_id}", source_title="t",
        section_path="", text=f"{chunk_id} text", relevance=relevance,
        score=relevance, notebook_id=notebook_id,
        retrieval_supports=(RetrievalSupport(origin, "chunk", chunk_id, relevance),),
    )


def _own_stamped_pool():
    """The finished selection holds rows stamped with the ACTIVE id (a
    contributor / hydrate lane), weaker unstamped own rows wait outside."""
    selected = [_row(f"own-{i}", 0.9 - i * 0.01, notebook_id=ACTIVE)
                for i in range(6)]
    spare = [_row(f"plain-{i}", 0.4) for i in range(3)]
    return selected, selected + spare


def _ids(rows):
    return [row.chunk_id for row in rows]


def test_mmr_floor_reads_own_id_rows_as_active():
    selected, pool = _own_stamped_pool()
    settings = SimpleNamespace(chunk_mmr_k=6, chunk_federation_active_reserve=0.5)
    out = cf.apply_active_reserve(settings, list(selected), pool, 6,
                                  active_notebook_id=ACTIVE)
    assert _ids(out) == _ids(selected)
    # The trap, made visible: an empty id reads them as peers and swaps.
    trapped = cf.apply_active_reserve(settings, list(selected), pool, 6,
                                      active_notebook_id="")
    assert _ids(trapped) != _ids(selected)


def test_quota_floor_reads_own_id_rows_as_active():
    from app.services.retrieval import quota_fuse_baseline_first

    selected, pool = _own_stamped_pool()
    collected = cf.with_active_reserve({row.chunk_id: row for row in pool}, 3)
    groups = [{row.chunk_id: row for row in pool}]
    out, _counts = quota_fuse_baseline_first(
        collected, groups, 6, active_notebook_id=ACTIVE)
    assert _ids(out) == _ids(selected)
    trapped, _counts = quota_fuse_baseline_first(
        collected, groups, 6, active_notebook_id="")
    assert _ids(trapped) != _ids(selected)


def test_retrieval_service_forwards_the_id_to_the_floor(monkeypatch):
    seen = {}

    def _spy(settings, selected, pool, k, *, active_notebook_id):
        seen["id"] = active_notebook_id
        return selected

    monkeypatch.setattr(cf, "apply_active_reserve", _spy)
    service = RetrievalService.__new__(RetrievalService)
    service.candidates = SimpleNamespace(
        settings=SimpleNamespace(),
        _mmr_select_chunks=lambda rows, ids, matrix, k, lambda_: list(rows)[:k],
    )
    selected, pool = _own_stamped_pool()
    service.select_chunk_candidates(pool, [], None, 6, 0.5,
                                    active_notebook_id=ACTIVE)
    assert seen == {"id": ACTIVE}
