"""Every retrieved-hit construction either names the library it read, or is
registered with where that library is stamped (or why it cannot reach a global
Ask).

In a global Ask there is no "current library": every hit must carry the library
it came from, or its citation reaches the terminal check with a blank origin
and is reported unattributed -- a healthy answer marked 「无法核对」. Three such
gaps were found one at a time (KG-overlay passages, rescored KG hits,
``expand_graph`` neighbours); this guard closes the class statically, next to
D9 (``test_citation_attestation_guard``) which checks that every citation's
element is attested.

The scan covers ``backend/app`` for every construction of a retrieved hit
(``RetrievedKnowledge`` / ``RetrievedChunk`` / ``RetrievedElement`` /
``RetrievedRelation`` / ``ChainHop`` / ``InferredChain``), keyed by file +
enclosing function + class. A construction that passes a non-literal-empty
``notebook_id=`` names its library and needs no entry. Any other one must be in
``ORIGIN_REGISTRY`` -- and every entry must still exist -- with one of:

``stamped-downstream``  the hit only reaches a global run through a function
                        that stamps its library; the proof names that function
                        and what it writes;
``global-unreachable``  the gate that keeps it out of a global run;
``not-in-engine``       no module of the global engine calls it.

``test_global_citation_producers_e2e`` is the behavioural half: every citation
and anchor of every healthy global run, over every producer and reasoning
action, names one of the run's participants.
"""
from __future__ import annotations

import ast
from collections import Counter

import pytest

from tests import test_citation_attestation_guard as d9

HIT_CLASSES = {
    "RetrievedKnowledge", "RetrievedChunk", "RetrievedElement", "RetrievedRelation",
    "ChainHop", "InferredChain",
}

RC = "services/retrieval_candidates.py"
CF = "services/chunk_federation.py"
RR = "services/reasoning_retrieval.py"
AS = "services/ask_service.py"

_CHUNKS_STAMPED = (
    ("passes-kw", CF, "_merge_results", "notebook_id"),
    ("passes-kw", CF, "_stamped", "notebook_id"),
)
_KNOWLEDGE_STAMPED = (
    ("assigns-attr", RC, "CandidateRetrievalService._federated_retrieve_impl", "notebook_id"),
    ("calls", RR, "ReasoningRetriever._closing_rerank", "_rescored_keeping_origin"),
    ("calls", RR, "outline_truncated_kg_evidence", "_rescored_keeping_origin"),
)
_ELEMENT_ARM_CLOSED = d9._ELEMENT_ARM_CLOSED

#: (file, enclosing function, class) -> (classification, proofs, reason)
ORIGIN_REGISTRY = {
    ("services/retrieval.py", "score_chunks", "RetrievedChunk"): (
        "stamped-downstream", _CHUNKS_STAMPED,
        "chunk scoring for one library; every peer leg of a federated call returns "
        "through _merge_results / _stamped, which stamp the task's library"),
    ("services/exact_lookup.py", "_build_sections", "RetrievedChunk"): (
        "stamped-downstream",
        _CHUNKS_STAMPED + (("if-calls", RC, "CandidateRetrievalService._exact_lookup_chunks",
                            "federated_ask_active"),),
        "exact-identifier sections; in peer mode the lookup runs per library through "
        "the federation's exact arm, whose legs are stamped by _stamped"),
    ("services/retrieval.py", "score_knowledge", "RetrievedKnowledge"): (
        "stamped-downstream", _KNOWLEDGE_STAMPED,
        "KG scoring for one library; federated_retrieve assigns each hit's library, and "
        "the closing rerank's active-only rescoring keeps the collected hit's origin"),
    ("services/retrieval_candidates.py", "CandidateRetrievalService._rrf_scored",
     "RetrievedKnowledge"): (
        "stamped-downstream", _KNOWLEDGE_STAMPED, "same KG scoring path (RRF fusion)"),
    ("services/retrieval.py", "score_relations", "RetrievedRelation"): (
        "stamped-downstream",
        (("assigns-attr", RC, "CandidateRetrievalService._federated_retrieve_relations_impl",
          "notebook_id"),),
        "relation scoring for one library; federated_retrieve_relations assigns the library"),
    ("services/retrieval.py", "score_elements", "RetrievedElement"): (
        "global-unreachable", _ELEMENT_ARM_CLOSED, "single-library element search is closed in peer mode"),
    ("services/retrieval_candidates.py",
     "CandidateRetrievalService._retrieve_elements_from_chunks._hydrate_and_rank",
     "RetrievedElement"): (
        "global-unreachable", _ELEMENT_ARM_CLOSED, "same element arm"),
    ("services/source_graph_activation.py",
     "SelectedSourceGraphActivationService._chunk_from_snapshot", "RetrievedChunk"): (
        "global-unreachable",
        (("if-calls", AS, "AskService._activate_selected_source_graph", "subjectless_run_active"),),
        "selected-source graph activation returns before any read in a global run"),
    ("services/source_graph_activation.py", "hydrate_selected_graph_chunk_rows", "RetrievedChunk"): (
        "global-unreachable",
        (("if-calls", AS, "AskService._activate_selected_source_graph", "subjectless_run_active"),),
        "same selected-source graph path"),
    ("services/knowledge_query.py", "KnowledgeQueryService.as_retrieved", "RetrievedKnowledge"): (
        "not-in-engine", (("not-called", "as_retrieved"),),
        "facade projection of one stored object; no global-engine module calls it"),
}


def scan_source(relative: str, source: str) -> list:
    """``(file, qualname, class, passes_library)`` for every hit construction."""
    tree = ast.parse(source)
    sites: list = []

    def visit(node, stack):
        for child in ast.iter_child_nodes(node):
            scope = stack
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                scope = stack + [child.name]
            if isinstance(child, ast.Call):
                func = child.func
                name = func.id if isinstance(func, ast.Name) else (
                    func.attr if isinstance(func, ast.Attribute) else "")
                if name in HIT_CLASSES:
                    library = next(
                        (k.value for k in child.keywords if k.arg == "notebook_id"), None,
                    )
                    passes = library is not None and not (
                        isinstance(library, ast.Constant) and not library.value
                    )
                    sites.append((relative, ".".join(stack), name, passes))
            visit(child, scope)

    visit(tree, [])
    return sites


def scan() -> Counter:
    found: Counter = Counter()
    for path in sorted(d9.APP.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if not any(f"{name}(" in source for name in HIT_CLASSES):
            continue
        for relative, qualname, name, passes in scan_source(str(path.relative_to(d9.APP)), source):
            if not passes:
                found[(relative, qualname, name)] += 1
    return found


def origin_problems(found: Counter, registry=ORIGIN_REGISTRY) -> list:
    problems = [
        f"retrieved-hit construction {site} names no library and is not registered"
        for site in sorted(set(found) - set(registry))
    ]
    problems += [
        f"registered hit construction {site} no longer exists (or now names its library); remove it"
        for site in sorted(set(registry) - set(found))
    ]
    return problems


def _proof_problems(proof) -> list:
    kind = proof[0]
    if kind == "not-called":
        return [
            f"{module} calls {proof[1]}"
            for module in d9.GLOBAL_ENGINE_MODULES
            if proof[1] in d9._called_names(ast.parse((d9.APP / module).read_text(encoding="utf-8")))
        ]
    if kind in ("passes-kw", "assigns-attr"):
        _kind, relative, qualname, name = proof
        node = d9._function(relative, qualname)
        if node is None:
            return [f"{relative}::{qualname} no longer exists"]
        if kind == "passes-kw":
            ok = any(isinstance(call, ast.Call) and any(k.arg == name for k in call.keywords)
                     for call in ast.walk(node))
        else:
            ok = any(
                isinstance(assign, ast.Assign) and any(
                    isinstance(target, ast.Attribute) and target.attr == name
                    for target in assign.targets)
                for assign in ast.walk(node)
            )
        return [] if ok else [f"{relative}::{qualname} no longer writes {name}"]
    return d9.proof_problems(proof)


def test_every_hit_construction_names_its_library_or_is_registered():
    assert origin_problems(scan()) == []


@pytest.mark.parametrize("site", sorted(ORIGIN_REGISTRY), ids=lambda site: f"{site[1]}:{site[2]}")
def test_every_origin_proof_still_holds(site):
    classification, proofs, reason = ORIGIN_REGISTRY[site]
    assert classification in {"stamped-downstream", "global-unreachable", "not-in-engine"}
    assert proofs and reason
    assert [problem for proof in proofs for problem in _proof_problems(proof)] == []


# --- the guard's own teeth ------------------------------------------------

def test_an_unstamped_hit_construction_fails():
    source = (
        "from app.domain.retrieval import RetrievedKnowledge\n"
        "def neighbours(rows):\n"
        "    return [RetrievedKnowledge(object_id=r.id, object_type='claim', payload={})\n"
        "            for r in rows]\n"
    )
    found = scan()
    found.update(
        (relative, qualname, name)
        for relative, qualname, name, passes in scan_source("services/new_leg.py", source)
        if not passes
    )
    assert any("new_leg" in problem for problem in origin_problems(found))


def test_a_literal_empty_library_counts_as_no_library():
    source = "def make(r):\n    return RetrievedChunk(chunk_id=r.c, notebook_id='')\n"
    [(_relative, _qualname, _name, passes)] = scan_source("services/new_leg.py", source)
    assert passes is False


def test_removing_a_downstream_stamp_fails(tmp_path, monkeypatch):
    import sys

    source = (d9.APP / RC).read_text(encoding="utf-8")
    unstamped = source.replace("                h.notebook_id = nid\n", "", 1)
    assert unstamped != source
    fake = tmp_path / "app"
    target = fake / RC
    target.parent.mkdir(parents=True)
    target.write_text(unstamped, encoding="utf-8")
    monkeypatch.setattr(sys.modules[d9.__name__], "APP", fake)
    assert _proof_problems(_KNOWLEDGE_STAMPED[0])
