"""Shared drivers for the PR-D graph-side producer race tests (D5, D6, D4 minting).

Three citation producers that cite ``source_elements`` rows they did not read
in full register them through ``services.evidence_attestation``:

* KG objects -- ``EvidenceContextService.knowledge_context`` (the anchor of
  every admitted object) and ``citations_from`` (the KG evidence cards) --
  pointer-register the occurrence element (``attest_pointers``);
* derived chains -- ``ReasoningRetriever.follow_chain`` -- pointer-register
  each hop's primary evidence element (``retrieval_service.
  attest_chain_evidence``);
* enumerated-row citations -- ``collection_item_citations`` -- hash the FULL
  element text its own hydration read returned (``attest_read``) and ask about
  an element row whose hydration missed.

Each driver runs the REAL producer against a REAL source store inside the
shape a global run installs (peer source scope, run plan, attestation seat),
then the REAL terminal check (``GlobalCitationCheck``) re-reads the store after
one mutation. What is stubbed is only what is not being raced: the knowledge
graph's own rows (``node_context`` / the follow_chain walk return fixed
occurrences that point at the seeded elements). The SQLite file
``tests/test_kg_attestation_races.py`` and the PostgreSQL twin
``tests/postgres/test_kg_attestation_races_pg.py`` drive the same rows through
the same functions; ``marker`` is ``?`` or ``%s``.
"""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

NOW = "2026-09-29T00:00:00Z"

SOURCE = "src-kg-attest"
CITED, OTHER = "el-kg-attest-0000", "el-kg-attest-0001"
#: Never inserted: a KG pointer that was already dangling before the question.
DANGLING = "el-kg-attest-gone"
DANGLING_2 = "el-kg-attest-gone-2"
#: Longer than any snippet the graph stores: a snapshot must be the digest of
#: the whole stored body, never of the quote a KG occurrence carries.
TEXTS = {
    CITED: "首段 · 低温下增益随温度下降而上升 🔋，零下四十度仍满足指标；"
           "第二句继续说明测试条件。\r\n第三行。",
    OTHER: "次段 · 常温基线与对照组的测量方法，另附误差分析。",
}
SNIPPET = TEXTS[CITED][:10]

#: mutation (between retrieval and the terminal read) -> the cited verdict.
RACE_EXPECTATIONS = {
    "none": None,
    "update": "changed",
    "delete": "source_gone",
    "reinsert": None,
}


def seed(database, marker: str, notebook_id: str) -> None:
    """One source holding the two elements above."""
    def values(count):
        return ",".join(marker for _ in range(count))

    with database.write() as db:
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,created_at,updated_at) "
            f"VALUES({values(6)})",
            (SOURCE, notebook_id, "Original", "markdown", NOW, NOW),
        )
        for index, (element_id, text) in enumerate(TEXTS.items()):
            db.execute(
                "INSERT INTO source_elements(id,source_id,element_type,location_label,"
                f"text,created_at) VALUES({values(6)})",
                (element_id, SOURCE, "paragraph", f"p{index}", text, NOW),
            )


def mutate(database, marker: str, mutation: str) -> None:
    """Apply one race mutation to the cited element."""
    with database.write() as db:
        if mutation == "update":
            db.execute(
                f"UPDATE source_elements SET text={marker} WHERE id={marker}",
                (TEXTS[CITED] + "（修订）", CITED),
            )
        elif mutation in {"delete", "reinsert"}:
            db.execute(f"DELETE FROM source_elements WHERE id={marker}", (CITED,))
            if mutation == "reinsert":
                db.execute(
                    "INSERT INTO source_elements(id,source_id,element_type,"
                    "location_label,text,created_at) VALUES("
                    + ",".join(marker for _ in range(6)) + ")",
                    (CITED, SOURCE, "paragraph", "p0", TEXTS[CITED], NOW),
                )
        elif mutation == "update_other":
            db.execute(
                f"UPDATE source_elements SET text={marker} WHERE id={marker}",
                (TEXTS[OTHER] + "（修订）", OTHER),
            )


class CountingReader:
    """The attestation seat's reader: the real store, every batched read counted."""

    def __init__(self, sources):
        self.sources = sources
        self.reads: list[tuple] = []

    def evidence_fingerprints(self, element_ids):
        self.reads.append(tuple(element_ids))
        return self.sources.evidence_fingerprints(element_ids)


@contextmanager
def global_run(sources, notebook_id: str):
    """What ``GlobalAskService`` installs around a run: a subjectless (peer)
    source scope, the run plan whose ``on_evidence`` folds into ``_RunState``,
    and the attestation seat reading through the real store."""
    from app.services.evidence_attestation import evidence_attestation_seat
    from app.services.federated_run import FederatedRunPlan, federated_run_plan
    from app.services.global_ask import _RunState
    from app.services.source_scope import source_scope_context

    state = _RunState((notebook_id,))
    events: list = []
    reader = CountingReader(sources)
    plan = FederatedRunPlan(
        phase_timeout_seconds=30.0, notebook_timeout_seconds=10.0,
        executor=None, window=lambda: 1, cancel=None,
        on_library=lambda *_: None, on_evidence=state.record_evidence,
        on_evidence_groups=state.record_evidence_groups,
    )
    with source_scope_context(
        notebook_id, None, None,
        notebook_source_ceilings={notebook_id: [SOURCE]}, subjectless=True,
    ), federated_run_plan(plan), evidence_attestation_seat(reader, emit=events.append):
        yield SimpleNamespace(state=state, events=events, reader=reader)


def terminal_check(sources, run, notebook_id: str, *, citations=(), anchors=()):
    """The real terminal read over the run's accumulated evidence."""
    from app.models.ask import AskResponse
    from app.services.global_citation_check import GlobalCitationCheck, apply_outcome

    response = AskResponse(
        conclusion="结论", answer="答案", grounded=True, evidence_level="grounded",
        citations=list(citations), anchors=list(anchors),
    )
    outcome = GlobalCitationCheck(sources, notebook_timeout_seconds=10.0).run(
        response, evidence=run.state.evidence_snapshot(),
        siblings=run.state.sibling_snapshot(),
        source_ceiling={notebook_id: frozenset({SOURCE})},
    )
    apply_outcome(response, outcome)
    return response


# ---------------------------------------------------------------------------
# Graph stubs (the KG rows are not what is raced)
# ---------------------------------------------------------------------------

class _Notebooks:
    def __init__(self, notebook_id: str):
        self.notebook_id = notebook_id

    def tier_map(self, notebook_ids):
        return dict.fromkeys(notebook_ids, "personal")

    def participant_notebook_ids(self, active_notebook_id):
        return [self.notebook_id]


def occurrence(element_id: str) -> dict:
    """What a knowledge store's ``node_context`` returns per evidence entry:
    the KG's stored quote (a snippet), never the element's full text."""
    return {
        "source_id": SOURCE, "element_id": element_id, "element_type": "paragraph",
        "location_label": "p0", "section_path": "§1", "element_text": SNIPPET,
        "quoted_span": SNIPPET, "source_title": "Original",
    }


class Knowledge:
    """``EvidenceKnowledgeContextPort`` over fixed occurrences per object."""

    def __init__(self, occurrences_by_object: dict):
        self.occurrences_by_object = occurrences_by_object

    def cluster_fold(self, notebook_id, object_ids):
        return {}

    def node_context(self, notebook_id, object_id, **_kwargs):
        return {
            "id": object_id,
            "occurrences": [dict(row) for row in self.occurrences_by_object[object_id]],
            "definition": None, "steps": None,
        }

    def in_network_relations(self, participant_ids, object_ids):
        return []

    def relation_support_counts(self, notebook_id, triples):
        return {triple: 1 for triple in triples}


def evidence_service(sources, notebook_id: str, knowledge=None):
    from app.core.config import Settings
    from app.services.evidence_context import EvidenceContextService

    return EvidenceContextService(
        notebooks=_Notebooks(notebook_id), sources=sources,
        knowledge=knowledge or Knowledge({}), settings=Settings(_env_file=None),
    )


def kg_hit(object_id: str, element_id: str, notebook_id: str, *, name: str = ""):
    from app.models.common import Evidence
    from app.services.retrieval import RetrievedKnowledge

    return RetrievedKnowledge(
        object_id=object_id, object_type="concept",
        payload={"name": name or f"对象 {object_id}"},
        evidence=[Evidence(
            source_id=SOURCE, source_title="Original", element_id=element_id,
            element_type="paragraph", location_label="p0", quoted_span=SNIPPET,
            confidence=0.9,
        )],
        notebook_id=notebook_id, tier="personal", relevance=0.9,
    )


# ---------------------------------------------------------------------------
# The three real producers
# ---------------------------------------------------------------------------

def kg_object_references(sources, notebook_id: str, element_id: str = CITED):
    """D5: one KG object whose evidence names ``element_id``. Returns the
    evidence card (``citations_from``) and the anchor (``knowledge_context`` ->
    ``parse_anchors``)."""
    hit = kg_hit("ko-1", element_id, notebook_id)
    service = evidence_service(
        sources, notebook_id, Knowledge({"ko-1": [occurrence(element_id)]}),
    )
    citations = service.citations_from(
        [hit], {element_id}, "KG evidence", notebook_id=notebook_id,
    )
    _block, id_map = service.knowledge_context(notebook_id, [hit])
    anchors = service.parse_anchors("结论 [k1]", id_map)
    return citations, anchors, id_map


def chain_result(notebook_id: str, element_ids=(CITED, OTHER)):
    """A two-hop derived chain whose hops' primary evidence names ``element_ids``."""
    from app.domain.graph import ChainHop, FollowChainResult, InferredChain

    def hop(index: int, element_id: str) -> ChainHop:
        return ChainHop(
            relation_id=f"rel-{index}", notebook_id=notebook_id, tier="personal",
            source_object_id=f"ko-{index}", target_object_id=f"ko-{index + 1}",
            edge_type="part_of", source_name=f"对象 {index}",
            target_name=f"对象 {index + 1}", review_status="verified",
            evidence=[{
                "source_id": SOURCE, "element_id": element_id,
                "quote": SNIPPET, "source_title": "Original",
                "location_label": "p0",
            }],
        )

    hops = tuple(hop(index, element_id) for index, element_id in enumerate(element_ids))
    return FollowChainResult(inferences=[InferredChain(
        source_object_id="ko-0", via_object_id="ko-1", target_object_id="ko-2",
        source_name="对象 0", via_name="对象 1", target_name="对象 2",
        inferred_edge_type="part_of", hops=hops, notebook_id=notebook_id,
    )])


def follow_chain_via_reasoning(result):
    """``ReasoningRetriever.follow_chain`` itself -- the registration point --
    over a retrieval port returning ``result``; the candidate filter passes
    everything, as it does for chains inside the run's ceiling."""
    from app.services.reasoning_retrieval import ReasoningRetriever

    calls: list = []

    class _Retrieval:
        def follow_chain(self, *args, **kwargs):
            calls.append((args, kwargs))
            return result

    retriever = SimpleNamespace(
        retrieval=_Retrieval(), _filter_candidates=lambda _kind, items: items,
    )
    return ReasoningRetriever.follow_chain(retriever, "nb", "ko-0")


def chain_anchors(sources, notebook_id: str, element_ids=(CITED, OTHER)):
    """D6: the chain's anchors as an answer citing both hops produces them."""
    from app.services.kg.follow_chain import render_follow_chain_context

    result = follow_chain_via_reasoning(chain_result(notebook_id, element_ids))
    _block, id_map = render_follow_chain_context(
        result.inferences, active_notebook_id=notebook_id,
    )
    anchors = evidence_service(sources, notebook_id).parse_anchors(
        "结论 [k2001] [k2002]", id_map,
    )
    return anchors, id_map


def kg_row(notebook_id: str, evidence_element_ids):
    return SimpleNamespace(
        object_id="ko-row", object_type="concept", name="对象",
        evidence_element_ids=tuple(evidence_element_ids),
        notebook_id=notebook_id, tier="personal",
    )


def element_row(notebook_id: str, element_id: str):
    return SimpleNamespace(
        element_id=element_id, source_id=SOURCE, source_title="Original",
        location_label="p0", text=SNIPPET, notebook_id=notebook_id, tier="personal",
    )


def collection_references(sources, notebook_id: str, items):
    """D4 minting: ``collection_item_citations`` over enumerated rows, plus the
    reverse binding each cited row becomes (``_preview_evidence``) turned into
    anchors by ``parse_anchors``."""
    from app.services.collection_enumeration_answer import (
        _preview_evidence, enumerated_item_id,
    )

    service = evidence_service(sources, notebook_id)
    citations = service.collection_item_citations(items, active_notebook_id=notebook_id)
    id_map = {}
    for index, item in enumerate(items, start=5001):
        collection = "elements" if getattr(item, "element_id", "") else "concepts"
        id_map[f"k{index}"] = _preview_evidence(
            collection, item, citations.get(enumerated_item_id(item)),
        )
    anchors = service.parse_anchors(" ".join(f"[{key}]" for key in id_map), id_map)
    return citations, anchors


# ---------------------------------------------------------------------------
# Scenarios, shared verbatim by the SQLite file and the PostgreSQL twin
# ---------------------------------------------------------------------------

def _verification(reference):
    return getattr(reference, "verification", None)


def _assert_full_text_snapshot(run, element_id: str) -> None:
    """The published snapshot is the digest of the stored body, not the snippet."""
    from app.domain.evidence_fingerprint import element_text_sha

    assert run.state.evidence_snapshot()[element_id] == (
        SOURCE, element_text_sha(TEXTS[element_id]),
    )


def kg_object_race(sources, database, marker, notebook_id, mutation):
    """D5: the KG evidence card and the object's anchor, both on ``CITED``."""
    seed(database, marker, notebook_id)
    with global_run(sources, notebook_id) as run:
        citations, anchors, _id_map = kg_object_references(sources, notebook_id)
    assert [c.element_id for c in citations] == [CITED]
    assert [a.element_id for a in anchors] == [CITED]
    _assert_full_text_snapshot(run, CITED)
    mutate(database, marker, mutation)
    response = terminal_check(
        sources, run, notebook_id, citations=citations, anchors=anchors,
    )
    expected = RACE_EXPECTATIONS[mutation]
    assert [_verification(c) for c in response.citations] == [expected]
    assert [_verification(a) for a in response.anchors] == [expected]
    return response


def kg_anchor_registered_on_its_own(sources, database, marker, notebook_id):
    """The anchor's element is registered by ``knowledge_context`` itself, not
    borrowed from a card: the card names ``OTHER`` (the retrieval-side
    evidence), the re-read occurrence names ``CITED``. Editing ``CITED``
    flags the anchor and leaves the card alone."""
    seed(database, marker, notebook_id)
    hit = kg_hit("ko-1", OTHER, notebook_id)
    service = evidence_service(
        sources, notebook_id, Knowledge({"ko-1": [occurrence(CITED)]}),
    )
    with global_run(sources, notebook_id) as run:
        citations = service.citations_from(
            [hit], {OTHER}, "KG evidence", notebook_id=notebook_id,
        )
        _block, id_map = service.knowledge_context(notebook_id, [hit])
    anchors = service.parse_anchors("结论 [k1]", id_map)
    assert [c.element_id for c in citations] == [OTHER]
    assert [a.element_id for a in anchors] == [CITED]
    untouched = terminal_check(
        sources, run, notebook_id, citations=citations, anchors=anchors,
    )
    assert _verification(untouched.anchors[0]) is None
    assert _verification(untouched.citations[0]) is None
    mutate(database, marker, "update")
    response = terminal_check(
        sources, run, notebook_id, citations=citations, anchors=anchors,
    )
    assert _verification(response.anchors[0]) == "changed"
    assert _verification(response.citations[0]) is None


def kg_dangling_pointer_is_not_minted(sources, database, marker, notebook_id):
    """J2: an occurrence already dangling before the question mints no card,
    and the object's anchor keeps its source but names no element."""
    seed(database, marker, notebook_id)
    with global_run(sources, notebook_id) as run:
        citations, anchors, _id_map = kg_object_references(
            sources, notebook_id, DANGLING,
        )
    assert citations == []
    assert [(a.source_id, a.element_id) for a in anchors] == [(SOURCE, "")]
    assert DANGLING not in run.state.evidence_snapshot()
    response = terminal_check(sources, run, notebook_id, anchors=anchors)
    assert _verification(response.anchors[0]) is None
    assert response.citation_check is None


def kg_one_read_per_call(sources, database, marker, notebook_id, *, hits=30):
    """ONE batched pointer read per ``knowledge_context`` call, over exactly
    the objects admitted under the budget -- never the candidate pool."""
    seed(database, marker, notebook_id)
    pool = [kg_hit(f"ko-{i}", f"el-pool-{i:02d}", notebook_id) for i in range(hits)]
    service = evidence_service(sources, notebook_id, Knowledge({
        hit.object_id: [occurrence(hit.evidence[0].element_id)] for hit in pool
    }))
    with global_run(sources, notebook_id) as run:
        _block, id_map = service.knowledge_context(notebook_id, pool, budget_chars=160)
    admitted = [value["object_id"] for value in id_map.values()]
    assert 0 < len(admitted) < hits
    assert run.reader.reads == [tuple(
        f"el-pool-{object_id.removeprefix('ko-').zfill(2)}" for object_id in admitted
    )]


#: mutation -> the anchor's verdict when the object has two occurrences and
#: its anchor names the FIRST (``CITED``); ``update_other`` edits the second.
MULTI_OCCURRENCE_EXPECTATIONS = {
    "none": None,
    "update": "changed",
    "update_other": None,
}


def kg_registers_the_occurrence_it_writes(sources, database, marker, notebook_id,
                                          mutation):
    """An object with several occurrences from different elements: the ONE
    element registered is exactly the one written to ``evidence_by_id`` (the
    anchor's), so an edit of that element is detected and an edit of another
    occurrence's element is not reported on this anchor. ``knowledge_context``
    runs alone here, so nothing else can have registered the anchor's element."""
    seed(database, marker, notebook_id)
    hit = kg_hit("ko-1", CITED, notebook_id)
    service = evidence_service(sources, notebook_id, Knowledge({
        "ko-1": [occurrence(CITED), occurrence(OTHER)],
    }))
    with global_run(sources, notebook_id) as run:
        _block, id_map = service.knowledge_context(notebook_id, [hit])
    anchors = service.parse_anchors("结论 [k1]", id_map)
    assert id_map["k1"]["element_id"] == CITED
    assert run.reader.reads == [(CITED,)]
    assert set(run.state.evidence_snapshot()) == {CITED}
    _assert_full_text_snapshot(run, CITED)
    mutate(database, marker, mutation)
    response = terminal_check(sources, run, notebook_id, anchors=anchors)
    assert [a.element_id for a in response.anchors] == [CITED]
    assert [_verification(a) for a in response.anchors] == [
        MULTI_OCCURRENCE_EXPECTATIONS[mutation],
    ]


def chain_race(sources, database, marker, notebook_id, mutation):
    """D6: both hops of a derived chain become anchors; hop 1 cites ``CITED``."""
    seed(database, marker, notebook_id)
    with global_run(sources, notebook_id) as run:
        anchors, _id_map = chain_anchors(sources, notebook_id)
    assert [a.element_id for a in anchors] == [CITED, OTHER]
    assert run.reader.reads == [(CITED, OTHER)]
    _assert_full_text_snapshot(run, CITED)
    _assert_full_text_snapshot(run, OTHER)
    mutate(database, marker, mutation)
    response = terminal_check(sources, run, notebook_id, anchors=anchors)
    assert [_verification(a) for a in response.anchors] == [
        RACE_EXPECTATIONS[mutation], None,
    ]
    return response


def chain_dangling_pointer_is_not_minted(sources, database, marker, notebook_id):
    """J2: a hop whose primary evidence was already dangling keeps its relation
    and source, names no element, and passes at source level (J3)."""
    seed(database, marker, notebook_id)
    with global_run(sources, notebook_id) as run:
        anchors, _id_map = chain_anchors(sources, notebook_id, (DANGLING, OTHER))
    assert [(a.source_id, a.element_id) for a in anchors] == [
        (SOURCE, ""), (SOURCE, OTHER),
    ]
    assert DANGLING not in run.state.evidence_snapshot()
    response = terminal_check(sources, run, notebook_id, anchors=anchors)
    assert [_verification(a) for a in response.anchors] == [None, None]


def chain_registration_never_mutates_its_input(sources, database, marker, notebook_id):
    """The chains handed to ``attest_chain_evidence`` are shared structures
    (the retrieval port's result): a dead primary is cleared on a COPY, the
    input chains, hops and evidence entries compare equal to a deep copy
    taken before the call, and with nothing dead the very same list object
    comes back."""
    import copy

    seed(database, marker, notebook_id)
    with global_run(sources, notebook_id):
        dead = chain_result(notebook_id, (DANGLING, OTHER))
        given = dead.inferences
        before = copy.deepcopy(given)
        attested = follow_chain_via_reasoning(dead).inferences
        live = chain_result(notebook_id, (CITED, OTHER))
        live_given = live.inferences
        live_attested = follow_chain_via_reasoning(live).inferences
    assert given == before
    assert given[0].hops[0].evidence[0]["element_id"] == DANGLING
    assert attested is not given
    assert attested[0].hops[0].evidence[0]["element_id"] == ""
    assert attested[0].hops[1] is given[0].hops[1]
    assert live_attested is live_given


def collection_race(sources, database, marker, notebook_id, mutation):
    """D4 minting: a KG row whose first evidence id dangles cites the next
    live one (``CITED``); an element row cites ``OTHER``. Full text in hand
    from the hydration read, so no pointer read at all."""
    seed(database, marker, notebook_id)
    items = [kg_row(notebook_id, (DANGLING, CITED)), element_row(notebook_id, OTHER)]
    with global_run(sources, notebook_id) as run:
        citations, anchors = collection_references(sources, notebook_id, items)
    assert {key: c.element_id for key, c in citations.items()} == {
        "ko-row": CITED, OTHER: OTHER,
    }
    assert [a.element_id for a in anchors] == [CITED, OTHER]
    assert run.reader.reads == []
    _assert_full_text_snapshot(run, CITED)
    _assert_full_text_snapshot(run, OTHER)
    mutate(database, marker, mutation)
    response = terminal_check(
        sources, run, notebook_id, citations=citations.values(), anchors=anchors,
    )
    expected = RACE_EXPECTATIONS[mutation]
    assert [_verification(c) for c in response.citations] == [expected, None]
    assert [_verification(a) for a in response.anchors] == [expected, None]
    return response


def collection_dangling_element_row(sources, database, marker, notebook_id):
    """J2: element rows whose elements were already gone are asked about in
    ONE batched read and not minted in a global run; outside one they are
    minted exactly as before (single-notebook asks are unchanged, J8)."""
    seed(database, marker, notebook_id)
    items = [
        element_row(notebook_id, DANGLING), element_row(notebook_id, OTHER),
        element_row(notebook_id, DANGLING_2),
    ]
    with global_run(sources, notebook_id) as run:
        citations, anchors = collection_references(sources, notebook_id, items)
    assert list(citations) == [OTHER]
    assert run.reader.reads == [(DANGLING, DANGLING_2)]
    assert [a.element_id for a in anchors if a.element_id] == [OTHER]
    response = terminal_check(
        sources, run, notebook_id, citations=citations.values(), anchors=anchors,
    )
    assert response.citation_check is None
    outside, _anchors = collection_references(sources, notebook_id, items)
    assert list(outside) == [DANGLING, OTHER, DANGLING_2]
