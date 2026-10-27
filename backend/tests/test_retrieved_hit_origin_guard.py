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

Every module is parsed (no text pre-filter), constructions are recognised
under any import alias or rebinding, and a stamp or call on a dead path (under
``if False:``, after an unconditional ``return``) does not count as a proof.

Every ``RetrievedChunk`` / ``RetrievedElement`` construction -- registered here
or not, since one that names its library is a producer too -- also states
whether navigation pages (``chunks.navigation``: tables of contents, lists of
figures) are dropped before it reaches a caller (``navigation-filtered``, with
the live call that drops them or reads the read-side switch on its path) or why
it cannot carry one (``navigation-exempt``): the fourth field of
``ORIGIN_REGISTRY`` entries, and ``NAVIGATION_REGISTRY`` for the constructions
that name their library. A new producer fails until it chooses.

Like D9, this is a REGISTRY in which every construction site is accounted for;
it does not prove behaviour. ``test_global_citation_producers_e2e`` is the
behavioural half: every citation and anchor of every healthy global run, over
every producer and reasoning action, names one of the run's participants.
"""
from __future__ import annotations

import ast
import functools
from collections import Counter

import pytest

from tests import test_citation_attestation_guard as d9
from tests.architecture.source_trees import app_source_trees

pytestmark = pytest.mark.xdist_group(name="source_scans_citation")

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
_ELEMENTS_STAMPED = (
    ("passes-kw", CF, "_stamped", "notebook_id"),
    ("calls", CF, "federated_element_candidates", "_stamped"),
    ("calls", RC, "CandidateRetrievalService._peer_retrieve_elements",
     "federated_element_candidates"),
    ("calls", RC, "CandidateRetrievalService.retrieve_elements", "_peer_retrieve_elements"),
)

GA = "services/graph_retrieval.py"
EL = "services/exact_lookup.py"
SGA = "services/source_graph_activation.py"

#: The navigation decision of a construction site:
#: ``("navigation-filtered", proofs)`` -- every proof a live call that drops
#: navigation rows or reads the read-side switch on this site's path -- or
#: ``("navigation-exempt", reason)`` (never for a chunk or element).
_NAV_SCORE_CHUNKS = ("navigation-filtered", (
    ("calls", "services/retrieval.py", "score_chunks", "drop_navigation_rows"),
))
_NAV_ELEMENTS = ("navigation-filtered", (
    ("calls", RC, "CandidateRetrievalService._gather_elements", "drop_navigation_rows"),
    ("calls", RC, "CandidateRetrievalService._retrieve_elements_from_chunks._hydrate_and_rank",
     "_navigation_element_ids"),
))
_NAV_SELECTED_GRAPH = ("navigation-filtered", (
    ("calls", SGA, "SelectedSourceGraphActivationService.run", "navigation_filter_enabled"),
))
_NAV_NOT_A_CHUNK = ("navigation-exempt", "a KG object or relation, not a chunk row")

#: (file, enclosing function, class) -> (classification, proofs, reason, navigation)
ORIGIN_REGISTRY = {
    ("services/retrieval.py", "score_chunks", "RetrievedChunk"): (
        "stamped-downstream", _CHUNKS_STAMPED,
        "chunk scoring for one library; every peer leg of a federated call returns "
        "through _merge_results / _stamped, which stamp the task's library",
        _NAV_SCORE_CHUNKS),
    ("services/exact_lookup.py", "_build_sections", "RetrievedChunk"): (
        "stamped-downstream",
        _CHUNKS_STAMPED + (("if-calls", RC, "CandidateRetrievalService._exact_lookup_chunks",
                            "federated_ask_active"),),
        "exact-identifier sections; in peer mode the lookup runs per library through "
        "the federation's exact arm, whose legs are stamped by _stamped",
        ("navigation-filtered", (
            ("calls", EL, "exact_lookup_sections", "row_navigation"),
            ("calls", EL, "exact_lookup_sections", "drop_navigation_rows"),
        ))),
    ("services/retrieval.py", "score_knowledge", "RetrievedKnowledge"): (
        "stamped-downstream", _KNOWLEDGE_STAMPED,
        "KG scoring for one library; federated_retrieve assigns each hit's library, and "
        "the closing rerank's active-only rescoring keeps the collected hit's origin",
        _NAV_NOT_A_CHUNK),
    ("services/retrieval_candidates.py", "CandidateRetrievalService._rrf_scored",
     "RetrievedKnowledge"): (
        "stamped-downstream", _KNOWLEDGE_STAMPED, "same KG scoring path (RRF fusion)",
        _NAV_NOT_A_CHUNK),
    ("services/retrieval.py", "score_relations", "RetrievedRelation"): (
        "stamped-downstream",
        (("assigns-attr", RC, "CandidateRetrievalService._federated_retrieve_relations_impl",
          "notebook_id"),),
        "relation scoring for one library; federated_retrieve_relations assigns the library",
        _NAV_NOT_A_CHUNK),
    ("services/retrieval.py", "score_elements", "RetrievedElement"): (
        "stamped-downstream", _ELEMENTS_STAMPED,
        "element scoring for one library; in peer mode retrieve_elements federates the "
        "search per library and every leg returns through _stamped",
        _NAV_ELEMENTS),
    ("services/retrieval_candidates.py",
     "CandidateRetrievalService._retrieve_elements_from_chunks._hydrate_and_rank",
     "RetrievedElement"): (
        "stamped-downstream", _ELEMENTS_STAMPED, "same element arm", _NAV_ELEMENTS),
    ("services/source_graph_activation.py",
     "SelectedSourceGraphActivationService._chunk_from_snapshot", "RetrievedChunk"): (
        "global-unreachable",
        (("if-calls", AS, "AskService._activate_selected_source_graph", "subjectless_run_active"),),
        "selected-source graph activation returns before any read in a global run",
        _NAV_SELECTED_GRAPH),
    ("services/source_graph_activation.py", "hydrate_selected_graph_chunk_rows", "RetrievedChunk"): (
        "global-unreachable",
        (("if-calls", AS, "AskService._activate_selected_source_graph", "subjectless_run_active"),),
        "same selected-source graph path",
        ("navigation-filtered", (
            ("calls", SGA, "hydrate_selected_graph_chunk_rows", "row_navigation"),
            ("calls", SGA, "SelectedSourceGraphActivationService.run",
             "navigation_filter_enabled"),
        ))),
    ("services/knowledge_query.py", "KnowledgeQueryService.as_retrieved", "RetrievedKnowledge"): (
        "not-in-engine", (("not-called", "as_retrieved"),),
        "facade projection of one stored object; no global-engine module calls it",
        _NAV_NOT_A_CHUNK),
}

#: The chunk / element constructions that name their library (so need no
#: ``ORIGIN_REGISTRY`` entry), with their navigation decision.
NAVIGATION_REGISTRY = {
    (GA, "GraphRetrievalService._ppr_retrieve", "RetrievedChunk"): ("navigation-filtered", (
        ("calls", GA, "GraphRetrievalService._ppr_retrieve", "drop_navigation_rows"),
    )),
    (GA, "GraphRetrievalService._kg_source_chunks", "RetrievedChunk"): ("navigation-filtered", (
        ("calls", GA, "GraphRetrievalService._kg_source_chunks", "drop_navigation_rows"),
    )),
    (RC, "CandidateRetrievalService._hydrate_generated_question_chunks", "RetrievedChunk"): (
        "navigation-filtered", (
            ("calls", RC, "CandidateRetrievalService._hydrate_generated_question_chunks",
             "drop_navigation_chunks"),
        )),
    (RC, "CandidateRetrievalService._restore_report_chunks", "RetrievedChunk"): (
        "navigation-filtered", (
            ("calls", RC, "CandidateRetrievalService._restore_report_chunks",
             "drop_navigation_rows"),
        )),
    # A section read back whole names its owning library (the ``notebook_id``
    # the caller resolved the address under), and drops navigation pages both
    # by the outline's marker and by the hydrated rows' own column.
    ("services/section_retrieval.py", "section_chunks", "RetrievedChunk"): (
        "navigation-filtered", (
            ("calls", "services/section_retrieval.py", "section_chunks",
             "drop_navigation_rows"),
            ("calls", "services/section_retrieval.py", "section_chunks",
             "navigation_filter_enabled"),
        )),
    # The run's chunk-recall memo rebuilds text-less chunks from what it kept
    # of ``_retrieve_chunks``' own scored output (navigation already dropped
    # by every lane's scoring), and serves them only to the element arm, which
    # drops a navigation page's elements itself.
    ("services/chunk_recall_memo.py", "_rebuilt", "RetrievedChunk"): (
        "navigation-filtered", (
            ("calls", RC, "CandidateRetrievalService._retrieve_chunks", "keep"),
            ("calls", "services/retrieval.py", "score_chunks", "drop_navigation_rows"),
            ("calls", RC,
             "CandidateRetrievalService._retrieve_elements_from_chunks._hydrate_and_rank",
             "_navigation_element_ids"),
        )),
    (RC, "CandidateRetrievalService._admitted_report_elements", "RetrievedElement"): (
        "navigation-filtered", (
            ("calls", RC, "CandidateRetrievalService._admitted_report_elements",
             "_flagged_navigation_elements"),
        )),
}

NAVIGATION_CLASSES = {"RetrievedChunk", "RetrievedElement"}


def scan_tree(relative: str, tree: ast.Module) -> list:
    """``(file, qualname, class, passes_library)`` for every hit construction,
    under any import alias or ``X = RetrievedChunk`` rebinding; ``tree`` is a
    shared, read-only syntax tree."""
    aliases = d9._aliases(tree, tuple(sorted(HIT_CLASSES)))
    sites: list = []

    def visit(node, stack):
        for child in ast.iter_child_nodes(node):
            scope = stack
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                scope = stack + [child.name]
            if isinstance(child, ast.Call):
                name = d9._call_name(child)
                if name in aliases:
                    library = next(
                        (k.value for k in child.keywords if k.arg == "notebook_id"), None,
                    )
                    passes = library is not None and not (
                        isinstance(library, ast.Constant) and not library.value
                    )
                    sites.append((relative, ".".join(stack), aliases[name], passes))
            visit(child, scope)

    visit(tree, [])
    return sites


@functools.lru_cache(maxsize=None)
def _scan_tree_cached(relative: str, tree: ast.Module) -> tuple:
    return tuple(scan_tree(relative, tree))


def scan() -> Counter:
    """Every unstamped construction under ``d9.APP``. No text pre-filter (the
    D9 lesson): every module is parsed, so an alias cannot hide a site."""
    found: Counter = Counter()
    for relative, _source, tree in app_source_trees(d9.APP):
        for _rel, qualname, name, passes in _scan_tree_cached(relative, tree):
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
            if proof[1] in d9._called_names(d9.parse_file(d9.APP / module))
        ]
    if kind in ("passes-kw", "assigns-attr"):
        _kind, relative, qualname, name = proof
        node = d9._function(relative, qualname)
        if node is None:
            return [f"{relative}::{qualname} no longer exists"]
        if kind == "passes-kw":
            ok = any(isinstance(call, ast.Call) and any(k.arg == name for k in call.keywords)
                     for call in d9.live_walk(node))
        else:
            ok = any(
                isinstance(assign, ast.Assign) and any(
                    isinstance(target, ast.Attribute) and target.attr == name
                    for target in assign.targets)
                for assign in d9.live_walk(node)
            )
        return [] if ok else [f"{relative}::{qualname} no longer writes {name}"]
    return d9.proof_problems(proof)


def test_every_hit_construction_names_its_library_or_is_registered():
    assert origin_problems(scan()) == []


@pytest.mark.parametrize("site", sorted(ORIGIN_REGISTRY), ids=lambda site: f"{site[1]}:{site[2]}")
def test_every_origin_proof_still_holds(site):
    classification, proofs, reason, _navigation = ORIGIN_REGISTRY[site]
    assert classification in {"stamped-downstream", "global-unreachable", "not-in-engine"}
    assert proofs and reason
    assert [problem for proof in proofs for problem in _proof_problems(proof)] == []


def navigation_sites() -> set:
    """Every RetrievedChunk / RetrievedElement construction, library named or not."""
    return {
        (relative, qualname, name)
        for relative, _source, tree in app_source_trees(d9.APP)
        for _rel, qualname, name, _passes in _scan_tree_cached(relative, tree)
        if name in NAVIGATION_CLASSES
    }


def navigation_decisions() -> dict:
    decisions = {site: entry[3] for site, entry in ORIGIN_REGISTRY.items()}
    decisions.update(NAVIGATION_REGISTRY)
    return decisions


def test_every_chunk_and_element_construction_chooses_navigation_filtering():
    sites = navigation_sites()
    decisions = navigation_decisions()
    assert not set(ORIGIN_REGISTRY) & set(NAVIGATION_REGISTRY)
    missing = sorted(sites - set(decisions))
    assert missing == [], f"chunk/element constructions with no navigation decision: {missing}"
    stale = sorted(site for site in NAVIGATION_REGISTRY if site not in sites)
    assert stale == [], f"NAVIGATION_REGISTRY entries that no longer exist: {stale}"


@pytest.mark.parametrize(
    "site", sorted(navigation_decisions()), ids=lambda site: f"{site[1]}:{site[2]}",
)
def test_every_navigation_decision_still_holds(site):
    kind, detail = navigation_decisions()[site]
    assert kind in {"navigation-filtered", "navigation-exempt"}
    if kind == "navigation-exempt":
        assert site[2] not in NAVIGATION_CLASSES, "a chunk/element producer cannot be exempt"
        assert isinstance(detail, str) and detail
        return
    assert detail
    assert [problem for proof in detail for problem in _proof_problems(proof)] == []


def test_a_library_naming_chunk_producer_must_still_choose(tmp_path, monkeypatch):
    """A construction that names its library passes the origin guard but not
    this one: it still has to say whether navigation pages are dropped."""
    import sys

    fake = tmp_path / "app"
    target = fake / "services" / "new_leg.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "def make(r, nb):\n    return RetrievedChunk(chunk_id=r.c, notebook_id=nb)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[d9.__name__], "APP", fake)
    site = ("services/new_leg.py", "make", "RetrievedChunk")
    assert site in navigation_sites()
    assert site not in navigation_decisions()


def test_removing_a_navigation_drop_fails(tmp_path, monkeypatch):
    import sys

    relative = "services/retrieval.py"
    source = (d9.APP / relative).read_text(encoding="utf-8")
    needle = "for c in drop_navigation_rows(chunks, enabled=drop_navigation):"
    assert needle in source
    fake = tmp_path / "app"
    target = fake / relative
    target.parent.mkdir(parents=True)
    target.write_text(source.replace(needle, "for c in chunks:", 1), encoding="utf-8")
    monkeypatch.setattr(sys.modules[d9.__name__], "APP", fake)
    assert _proof_problems(_NAV_SCORE_CHUNKS[1][0])


# --- the guard's own teeth ------------------------------------------------

def _scanned_problems(tmp_path, monkeypatch, source: str) -> list:
    """Run the real ``scan()`` over a fake app directory holding one module."""
    import sys

    fake = tmp_path / "app"
    target = fake / "services" / "new_leg.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    monkeypatch.setattr(sys.modules[d9.__name__], "APP", fake)
    return [problem for problem in origin_problems(scan()) if "new_leg" in problem]


@pytest.mark.parametrize("source", [
    "from app.domain.retrieval import RetrievedKnowledge\n"
    "def neighbours(rows):\n"
    "    return [RetrievedKnowledge(object_id=r.id, object_type='claim', payload={})\n"
    "            for r in rows]\n",
    # an import alias: no "RetrievedKnowledge(" text anywhere in the module
    "from app.domain.retrieval import RetrievedKnowledge as K\n"
    "def neighbours(rows):\n"
    "    return [K(object_id=r.id, object_type='claim', payload={}) for r in rows]\n",
    # a rebinding alias through the module
    "from app.domain import retrieval\n"
    "Hit = retrieval.RetrievedChunk\n"
    "def make(r):\n"
    "    return Hit(chunk_id=r.c)\n",
    # a literal empty library is no library
    "def make(r):\n    return RetrievedChunk(chunk_id=r.c, notebook_id='')\n",
], ids=["plain", "import-alias", "rebinding-alias", "literal-empty"])
def test_an_unstamped_hit_construction_fails(tmp_path, monkeypatch, source):
    assert _scanned_problems(tmp_path, monkeypatch, source)


def test_a_stamped_hit_construction_passes(tmp_path, monkeypatch):
    source = "def make(r, nb):\n    return RetrievedChunk(chunk_id=r.c, notebook_id=nb)\n"
    assert _scanned_problems(tmp_path, monkeypatch, source) == []


@pytest.mark.parametrize("replacement", [
    "",
    "                if False:\n                    h.notebook_id = nid\n",
], ids=["removed", "under-if-false"])
def test_removing_a_downstream_stamp_fails(tmp_path, monkeypatch, replacement):
    import sys

    source = (d9.APP / RC).read_text(encoding="utf-8")
    assert "                h.notebook_id = nid\n" in source  # first: _federated_retrieve_impl
    unstamped = source.replace("                h.notebook_id = nid\n", replacement, 1)
    assert unstamped != source
    fake = tmp_path / "app"
    target = fake / RC
    target.parent.mkdir(parents=True)
    target.write_text(unstamped, encoding="utf-8")
    monkeypatch.setattr(sys.modules[d9.__name__], "APP", fake)
    assert _proof_problems(_KNOWLEDGE_STAMPED[0])
