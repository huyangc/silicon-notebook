import concurrent.futures as cf
import re
import threading
import time
from types import SimpleNamespace

import pytest

from app.models.schemas import SourceElement
from app.services import kg_ingest
from app.services.kg.models import Node, Edge, Evidence, KnowledgeGraph
from app.services.kg.parsing import SourceElementQ
from app.services.kg.run_control import KgBuildAborted, KgBuildFailure


def _el(i, text):
    return SourceElement(id=i, source_id="s1", element_type="paragraph",
                         location_label=f"p{i}", text=text)


def _ordered_tokens(s: str):
    """Deterministic stand-in for `kg_ingest._tokens` that preserves
    first-occurrence order (same token content, no hash dependence) instead
    of hash-randomized `set` iteration order. Used only to pin
    dict-insertion order in
    test_quote_binder_fuzzy_tie_break_requires_sorted below -- production
    `_tokens` returns a `set`, whose iteration order for strings is subject
    to PYTHONHASHSEED, which a plain fixture cannot control deterministically
    across processes."""
    seen: list = []
    seen_set: set = set()
    for tok in re.findall(r"\w+", (s or "").lower()):
        if tok not in seen_set:
            seen_set.add(tok)
            seen.append(tok)
    return seen


def test_build_records_binds_and_drops():
    g = KnowledgeGraph(doc_id="doc.md", doc_type="academic",
        nodes=[
            Node(id="C1", type="Concept", name="Engram",
                 evidence=[Evidence(file="doc.md", char_start=0, char_end=6,
                                    line_start=1, line_end=1, quote="Engram")]),
            Node(id="C2", type="Concept", name="Nowhere",
                 evidence=[Evidence(file="doc.md", char_start=0, char_end=3,
                                    line_start=1, line_end=1, quote="zzz")]),
        ],
        edges=[Edge(id="E1", type="about", source_id="C1", target_id="C2")])
    elements = [_el("e1", "Engram is a memory architecture.")]
    objects, relations = kg_ingest.build_records(
        g, source_id="s1", source_title="Doc", elements=elements)
    assert [o["object_type"] for o in objects] == ["concept"]   # C2 dropped (unbound)
    assert objects[0]["payload"]["name"] == "Engram"
    assert objects[0]["evidence"][0]["element_id"] == "e1"
    assert objects[0]["local_id"] == "C1"                        # carried for edge wiring
    assert relations == []                                       # edge dropped: C2 gone


def test_build_records_zero_nodes_short_circuits_without_building_index():
    """An empty graph must not pay for building `_QuoteBinder`'s index at
    all -- pins both the behavior (empty outputs) and, via the
    `_CountingElement` guard below, that `.text` is never read."""
    g = KnowledgeGraph(doc_id="doc.md", doc_type="academic", nodes=[], edges=[])
    elements = [_CountingElement("e0", "some paragraph text here")]
    objects, relations = kg_ingest.build_records(
        g, source_id="s1", source_title="Doc", elements=elements)
    assert objects == []
    assert relations == []
    assert elements[0].text_reads == 0


def test_bind_quote_fuzzy_fallback():
    """Quote that is NOT an exact normalized substring but shares >=60% tokens."""
    # Element text: "Engram is a memory architecture"
    # Quote: "Engram memory architecture model"
    # Exact substring check: "engram memory architecture model" NOT in "engram is a memory architecture" -> fails
    # Token overlap: qt = {engram, memory, architecture, model} (4 tokens)
    #                et = {engram, is, a, memory, architecture}
    #                intersection = {engram, memory, architecture} -> 3/4 = 0.75 >= 0.6 -> binds
    elements = [_el("e1", "Engram is a memory architecture")]
    result = kg_ingest._bind_quote(
        "Engram memory architecture model", elements, "s1", "MyDoc"
    )
    assert result is not None, "fuzzy bind should succeed with 0.75 token overlap"
    assert result["element_id"] == "e1"
    assert result["source_id"] == "s1"
    assert result["source_title"] == "MyDoc"


def test_bind_quote_fuzzy_not_exact():
    """Confirm the fuzzy test quote genuinely fails exact-substring matching."""
    elements = [_el("e1", "Engram is a memory architecture")]
    q = kg_ingest._norm("Engram memory architecture model")
    text_norm = kg_ingest._norm("Engram is a memory architecture")
    assert q not in text_norm, "precondition: quote must NOT be an exact substring"


def test_norm_folds_whitespace_variants():
    """Direct pin on `_norm`'s whitespace-collapsing semantics. The
    differential fixtures elsewhere in this file are blind to a regression
    IN `_norm` itself: both `_bind_quote` (reference) and `_QuoteBinder`
    (new) call the exact same module-level `_norm`, so a change to `_norm`
    passes every diff check trivially -- yet `_QuoteBinder.__init__`'s
    "\\n"-separator equivalence argument depends entirely on `_norm` never
    emitting "\\n"."""
    assert kg_ingest._norm("a\nb") == "a b"
    assert "\n" not in kg_ingest._norm("a\nb\rc\td")
    assert kg_ingest._norm("a　b\xa0c") == "a b c"  # ideographic space / nbsp fold too


class _CountingElement:
    """Minimal element stand-in whose `.text` counts every read. Used to pin
    the "_QuoteBinder reads each element's text a bounded number of times,
    independent of quote count Q" invariant (T3-3) -- a regression to
    per-quote re-scanning is exactly the O(Q*E*len) blowup this rewrite
    exists to kill, and it would only show up as this counter growing with Q,
    not as any wrong answer."""

    __slots__ = ("id", "element_type", "location_label", "_text", "text_reads")

    def __init__(self, id_, text):
        self.id = id_
        self.element_type = "paragraph"
        self.location_label = f"p{id_}"
        self._text = text
        self.text_reads = 0

    @property
    def text(self):
        self.text_reads += 1
        return self._text


def _diff_check(elements, quotes, source_id="s1", source_title="Doc"):
    """Assert `_QuoteBinder.bind` == `_bind_quote` for every quote, using a
    single binder built once (as build_records does) so repeated quotes also
    exercise the memo path."""
    binder = kg_ingest._QuoteBinder(elements, source_id, source_title)
    for q in quotes:
        ref = kg_ingest._bind_quote(q, elements, source_id, source_title)
        new = binder.bind(q)
        assert new == ref, f"quote={q!r}: ref={ref!r} new={new!r}"
    return binder


def test_quote_binder_matches_reference_latin_first_match_wins():
    """Two elements both contain the exact quote (case-sensitive) -- the
    first one in list order must win, matching `_bind_quote`'s ordered scan."""
    els = [_el("e0", "Zeta looked at Alpha bravo charlie carefully."),
           _el("e1", "Alpha bravo charlie is also mentioned here.")]
    binder = _diff_check(els, ["Alpha bravo charlie"])
    assert binder.bind("Alpha bravo charlie")["element_id"] == "e0"


def test_quote_binder_matches_reference_cjk_exact():
    els = [_el("e0", "该电路使用电流镜结构"), _el("e1", "另一处提到电流镜设计")]
    binder = _diff_check(els, ["电流镜结构"])
    assert binder.bind("电流镜结构")["element_id"] == "e0"


def test_quote_binder_matches_reference_mixed_language():
    els = [_el("e0", "The circuit uses 电流镜 topology.")]
    _diff_check(els, ["uses 电流镜 topology"])


def test_quote_binder_boundary_span_is_not_falsely_exact_matched():
    """A quote built from element A's tail + ' ' + element B's head must not
    exact-match under either implementation: `_norm` never emits '\\n', so
    `joined.find(q)` can only match within one segment, never across the
    separator (see the equivalence argument in kg_ingest._QuoteBinder).
    MUT-5 (separator ' ' instead of '\\n') makes this red because a literal
    ' ' inside the quote can then bridge the two segments."""
    els = [_el("e0", "Section one ends with alpha"),
           _el("e1", "beta begins section two")]
    quote = "alpha beta"
    binder = _diff_check(els, [quote])
    # Both elements share exactly 1/2 tokens with the quote (0.5 < 0.6), so
    # neither phase should bind -- this also confirms the boundary wasn't
    # smuggled through the fuzzy phase.
    assert binder.bind(quote) is None


def test_quote_binder_literal_newline_in_quote_does_not_cross_boundary():
    """Same boundary as above, but exercises `_norm(quote)` (inside
    `_bind_uncached`) directly rather than `_norm(el.text)`: the RAW quote
    itself contains a literal '\\n' at the position that would align with
    the joined string's inter-segment separator. `_norm` must fold it to a
    plain space just like it does for element text, so the normalized query
    never contains '\\n' and can never coincidentally match the separator."""
    els = [_el("e0", "Section one ends with alpha"),
           _el("e1", "beta begins section two")]
    quote = "alpha\nbeta"
    binder = _diff_check(els, [quote])
    assert binder.bind(quote) is None


def test_quote_binder_matches_reference_fuzzy_tie_breaks_to_lowest_index():
    """Two elements tie at the maximum token overlap (1.0) after the exact
    phase fails (word order scrambled so no contiguous substring matches);
    the lower element index must win, matching `_bind_quote`'s strict `>`
    scan order."""
    els = [_el("e0", "charlie bravo alpha team"),
           _el("e1", "bravo alpha charlie squad"),
           _el("e2", "completely different text")]
    binder = _diff_check(els, ["alpha bravo charlie"])
    assert binder.bind("alpha bravo charlie")["element_id"] == "e0"


def test_quote_binder_fuzzy_tie_break_requires_sorted(monkeypatch):
    """MUT-B guard: the fuzzy fallback's `counts` tally is a plain `dict`
    built by walking `_tokens(quote)` -- in production a hash-randomized
    `set` of strings, so raw dict-insertion order is NOT reliably ascending
    by element index. Only the explicit `sorted(counts)` in
    `_bind_uncached` reproduces `_bind_quote`'s tie-break (lowest index
    wins, via its ordered `for el in elements` scan + strict `>`).

    To make this deterministic regardless of PYTHONHASHSEED, `_tokens` is
    monkeypatched to `_ordered_tokens` (same token content, first-occurrence
    order, no hash dependence) for this test only. The quote is built
    (verified empirically, see the task notes) so the query's OWN token
    order feeds a token unique to element 10 into `counts` BEFORE any token
    reaches element 3 -- raw dict-insertion order is therefore [10, 3, ...],
    the reverse of ascending index order, even though both tie at the same
    0.8 overlap. `sorted(counts)` must correct that back to element 3
    winning (matching `_bind_quote`); deleting it makes element 10 win
    instead."""
    elements = [_el(f"e{i}", f"filler paragraph number {i} unrelated text")
                for i in range(11)]
    elements[3] = _el("e3", "sharedA sharedB sharedC onlyThree filler")
    elements[10] = _el("e10", "onlyTen sharedA sharedB sharedC filler")
    quote = "onlyTen sharedA sharedB sharedC onlyThree"

    # Reference computed BEFORE patching, with the real `_tokens`:
    # `_bind_quote` loops over `elements` (not tokens) and does its own
    # `qt & et` set intersection, so its result is deterministic and
    # correct regardless of `_tokens`' internal set iteration order --
    # this just confirms the fixture really does tie at 0.8/0.8 with
    # element 3 winning, independent of the monkeypatch below.
    ref = kg_ingest._bind_quote(quote, elements, "s1", "Doc")
    assert ref is not None and ref["element_id"] == "e3"

    monkeypatch.setattr(kg_ingest, "_tokens", _ordered_tokens)
    binder = kg_ingest._QuoteBinder(elements, "s1", "Doc")
    result = binder.bind(quote)
    assert result is not None
    assert result["element_id"] == "e3", (
        "lowest-index element (3) must win the 0.8/0.8 tie; if this now "
        "resolves to e10, `sorted(counts)` was likely removed from the "
        "fuzzy fallback"
    )


def test_quote_binder_matches_reference_overlap_threshold_boundary():
    """Overlap exactly 0.6 binds; 0.59 does not -- pins the fuzzy threshold."""
    tokens = [f"tok{i}" for i in range(100)]
    quote_100 = " ".join(tokens)
    below = [_el("e0", " ".join(tokens[:59]))]     # 59/100 = 0.59 -> no bind
    _diff_check(below, [quote_100])
    assert kg_ingest._QuoteBinder(below, "s1", "Doc").bind(quote_100) is None
    at_threshold = [_el("e0", " ".join(tokens[:60]))]  # 60/100 = 0.60 -> binds
    _diff_check(at_threshold, [quote_100])
    assert kg_ingest._QuoteBinder(at_threshold, "s1", "Doc").bind(quote_100) is not None


def test_quote_binder_matches_reference_short_and_blank_inputs():
    # e3 contains "ab" as a literal substring of "tab": if the length guard
    # were relaxed from `len(q) < 3` to `len(q) < 2`, the 2-char quote "ab"
    # would skip the short-circuit and exact-match inside "tab", incorrectly
    # binding. Without e3, "ab" could never match ANY element regardless of
    # the threshold, so the boundary itself would go untested.
    els = [_el("e0", ""), _el("e1", "   "), _el("e2", "alpha bravo charlie"),
           _el("e3", "a small tab character here")]
    binder = _diff_check(els, ["ab", "a", "", "zzz", "alpha bravo charlie"])
    assert binder.bind("ab") is None


def test_quote_binder_prefers_later_exact_match_over_earlier_fuzzy_candidate():
    """Element 0 would fuzzy-bind on its own (2/3 token overlap, above the
    0.6 threshold) but element 1 has the literal exact substring -- the
    exact phase runs first and must win, exactly as in `_bind_quote` where
    the `for el in elements` exact-substring loop completes (and can return)
    before the fuzzy loop ever starts."""
    els = [_el("e0", "alpha bravo delta echo"),
           _el("e1", "unrelated filler text alpha bravo charlie right here")]
    binder = _diff_check(els, ["alpha bravo charlie"])
    assert binder.bind("alpha bravo charlie")["element_id"] == "e1"


def test_quote_binder_memoizes_repeat_quote_lookups():
    els = [_el("e0", "alpha bravo delta echo"),
           _el("e1", "unrelated filler text alpha bravo charlie right here")]
    binder = kg_ingest._QuoteBinder(els, "s1", "Doc")
    first = binder.bind("alpha bravo charlie")
    second = binder.bind("alpha bravo charlie")
    assert first == second == kg_ingest._bind_quote(
        "alpha bravo charlie", els, "s1", "Doc")


def test_quote_binder_memo_actually_skips_recomputation():
    """The test above only compares repeated `bind()` results for equality,
    which holds trivially even with ZERO caching (the same deterministic
    input always produces the same output) -- it does not prove
    `_bind_uncached` was only invoked once. Subclass `_QuoteBinder` to count
    actual `_bind_uncached` invocations directly."""

    class _CountingBinder(kg_ingest._QuoteBinder):
        __slots__ = ("uncached_calls",)

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.uncached_calls = 0

        def _bind_uncached(self, quote):
            self.uncached_calls += 1
            return super()._bind_uncached(quote)

    els = [_el("e0", "alpha bravo delta echo"),
           _el("e1", "unrelated filler text alpha bravo charlie right here")]
    binder = _CountingBinder(els, "s1", "Doc")
    for _ in range(5):
        binder.bind("alpha bravo charlie")
    assert binder.uncached_calls == 1, (
        f"expected memoization to skip recomputation after the first bind, "
        f"got {binder.uncached_calls} underlying calls for 5 repeat lookups"
    )


def test_quote_binder_memo_keyed_by_raw_quote_not_normalized():
    """Memo keys must be the RAW (un-normalized) quote string. Two quotes
    that normalize identically ("Reset  the device" / "Reset the device",
    differing only in a doubled space) but are lexically distinct must each
    get their own memo entry and their own `quoted_span` in the returned
    evidence, not the first one's cached result -- keying the memo by
    `_norm(quote)` instead of `quote` would make the SECOND lookup return
    the FIRST quote's raw text in `quoted_span`."""
    els = [_el("e0", "Reset the device before calibration.")]
    binder = kg_ingest._QuoteBinder(els, "s1", "Doc")
    r1 = binder.bind("Reset  the device")   # doubled space
    r2 = binder.bind("Reset the device")    # single space; same normalized form
    assert r1 is not None and r2 is not None
    assert r1["quoted_span"] == "Reset  the device"
    assert r2["quoted_span"] == "Reset the device"
    assert r1["quoted_span"] != r2["quoted_span"]


def test_quote_binder_bind_returns_independent_copies_not_shared_instance():
    """Two nodes that quote the SAME span must never end up holding the
    SAME dict instance: `bind()` must return a fresh copy on both the
    cache-miss (first call) and cache-hit (subsequent calls) paths, so a
    caller mutating one node's evidence dict cannot corrupt another node's
    (or the memo's own canonical) copy."""
    els = [_el("e0", "Alpha bravo charlie delta.")]
    binder = kg_ingest._QuoteBinder(els, "s1", "Doc")
    first = binder.bind("Alpha bravo charlie")
    second = binder.bind("Alpha bravo charlie")
    assert first == second
    assert first is not second
    first["quoted_span"] = "MUTATED"
    assert second["quoted_span"] != "MUTATED"


def test_quote_binder_reads_each_elements_text_at_most_twice_regardless_of_quote_count():
    """T3-3 regression guard: `.text` must be read a bounded (<=2) number of
    times per element at binder construction, independent of how many quotes
    Q are later bound. A regression to per-quote re-normalization (the exact
    O(Q*E*len) blowup this class exists to remove) would make this grow
    linearly with Q; Q=50 here is chosen to be far larger than the bound so
    any such regression is unmistakable."""
    n_elements = 6
    n_quotes = 50
    elements = [
        _CountingElement(f"e{i}", f"element number {i} says something unique here {i}")
        for i in range(n_elements)
    ]
    binder = kg_ingest._QuoteBinder(elements, "s1", "Doc")
    for i in range(n_quotes):
        binder.bind(
            f"element number {i % n_elements} says something unique here {i % n_elements}"
        )
        binder.bind(f"totally unrelated query text number {i}")
    for el in elements:
        assert el.text_reads <= 2, (
            f"{el.id} had .text read {el.text_reads} times for Q={n_quotes} quotes "
            "-- expected a constant bounded by construction, not growth with Q"
        )


def test_build_records_builds_index_once_across_nodes_and_edges():
    """T-B2 (move-mutation guard): pins that `build_records` builds exactly
    ONE `_QuoteBinder` for the whole call, not one per node. A regression
    that moves binder construction inside the `for node in graph.nodes`
    loop would still pass every other test in this file (same final
    bindings, same element/text values) but would multiply `.text` reads by
    the node count. Uses 3 nodes (+ edges connecting them + one step per
    node), all quoting elements that repeat across nodes/edges/steps, so a
    per-node binder would push every element's `.text` read count above the
    single-build bound."""
    elements = [
        _CountingElement("e0", "Alpha reset the device before calibration."),
        _CountingElement("e1", "Beta improves the calibration flow overall."),
        _CountingElement("e2", "Gamma finalizes the device calibration steps."),
    ]
    quotes = ["Alpha reset the device", "Beta improves the calibration flow",
              "Gamma finalizes the device calibration steps"]
    from app.services.kg.models import Step

    nodes = [
        Node(id=f"n{i}", type="Concept", name=f"Node {i}",
             evidence=[Evidence(file="doc.md", char_start=0, char_end=5,
                                line_start=1, line_end=1, quote=quotes[i])],
             steps=[Step(name=f"step{i}",
                         evidence=[Evidence(file="doc.md", char_start=0, char_end=5,
                                            line_start=2, line_end=2,
                                            quote=quotes[(i + 1) % 3])])])
        for i in range(3)
    ]
    edges = [
        Edge(id="e01", type="about", source_id="n0", target_id="n1",
             evidence=[Evidence(file="doc.md", char_start=0, char_end=5,
                                line_start=3, line_end=3, quote=quotes[0])]),
        Edge(id="e12", type="about", source_id="n1", target_id="n2",
             evidence=[Evidence(file="doc.md", char_start=0, char_end=5,
                                line_start=3, line_end=3, quote=quotes[1])]),
        Edge(id="e20", type="about", source_id="n2", target_id="n0",
             evidence=[Evidence(file="doc.md", char_start=0, char_end=5,
                                line_start=3, line_end=3, quote=quotes[2])]),
    ]
    g = KnowledgeGraph(doc_id="doc.md", doc_type="academic", nodes=nodes, edges=edges)
    objects, relations = kg_ingest.build_records(
        g, source_id="s1", source_title="Doc", elements=elements)
    assert len(objects) == 3
    assert len(relations) == 3
    for el in elements:
        assert el.text_reads <= 2, (
            f"{el.id} had .text read {el.text_reads} times across "
            f"{len(nodes)} nodes + {len(edges)} edges + 1 step/node -- "
            "expected a single shared index build, not one per node/edge"
        )


def test_build_records_binds_node_steps_to_elements():
    """T3-2: Procedure nodes bind each step's first evidence quote through the
    same binder as node/edge evidence (build_records's node.steps branch,
    otherwise untouched by any other test in this file)."""
    from app.services.kg.models import Step

    g = KnowledgeGraph(doc_id="doc.md", doc_type="academic",
        nodes=[
            Node(id="P1", type="Procedure", name="Calibration flow",
                 evidence=[Evidence(file="doc.md", char_start=0, char_end=10,
                                    line_start=1, line_end=1, quote="Calibration flow")],
                 steps=[
                     Step(name="Step one",
                          evidence=[Evidence(file="doc.md", char_start=0, char_end=6,
                                             line_start=2, line_end=2, quote="Reset the device")]),
                     Step(name="Step two (unbound)",
                          evidence=[Evidence(file="doc.md", char_start=0, char_end=6,
                                             line_start=3, line_end=3, quote="zzz not present")]),
                 ]),
        ],
        edges=[])
    elements = [
        _el("e1", "Calibration flow overview."),
        _el("e2", "Reset the device before proceeding."),
    ]
    objects, _relations = kg_ingest.build_records(
        g, source_id="s1", source_title="Doc", elements=elements)
    assert len(objects) == 1
    steps = objects[0]["payload"]["steps"]
    # Only the bound step survives; the unbound one is silently dropped.
    assert len(steps) == 1
    assert steps[0]["name"] == "Step one"
    assert steps[0]["element_id"] == "e2"
    assert steps[0]["quote"] == "Reset the device"


def test_build_records_binds_relation_quote_to_source_element():
    """Relation evidence keeps raw quote and gains element-level provenance."""
    quote_a = "Premise A is established"
    quote_b = "Conclusion B follows"
    edge_quote = "Premise A therefore leads to Conclusion B"
    elements = [
        _el("e1", quote_a),
        _el("e2", f"{quote_b}. {edge_quote}"),
    ]
    g = KnowledgeGraph(
        doc_id="doc.md", doc_type="academic",
        nodes=[
            Node(id="A", type="Claim", name=quote_a,
                 evidence=[Evidence(file="doc.md", char_start=0, char_end=10,
                                    line_start=1, line_end=1, quote=quote_a)]),
            Node(id="B", type="Claim", name=quote_b,
                 evidence=[Evidence(file="doc.md", char_start=11, char_end=20,
                                    line_start=2, line_end=2, quote=quote_b)]),
        ],
        edges=[Edge(
            id="R", type="derived_from", source_id="A", target_id="B",
            evidence=[Evidence(file="doc.md", char_start=21, char_end=40,
                               line_start=2, line_end=2, quote=edge_quote)],
        )],
    )
    _objects, relations = kg_ingest.build_records(
        g, source_id="s1", source_title="Doc", elements=elements)
    assert relations[0]["evidence"][0]["quote"] == edge_quote
    assert relations[0]["evidence"][0]["element_id"] == "e2"
    assert relations[0]["evidence"][0]["source_title"] == "Doc"


class FakeClient:
    configured = True
    def __init__(self, payload): self._p = payload
    def chat_json(self, messages: list, response_schema_hint: str) -> str: return self._p

ABS = "We propose Engram, a memory architecture. Engram improves perplexity."

def test_extract_graph_grounds_nodes():
    import json
    # ABS is a single prose element (one window) -> ev:0 anchors to it.
    payload = json.dumps({
        "nodes": [
            {"local_id": "a", "type": "Concept", "name": "Engram", "ev": 0},
            {"local_id": "b", "type": "Claim", "name": "Engram improves perplexity",
             "ev": 0},
            {"local_id": "z", "type": "Concept", "name": "Ghost", "ev": 99},
        ],
        "edges": [{"type": "about", "source": "b", "target": "a", "ev": 0}],
    })
    g = kg_ingest.extract_graph(FakeClient(payload), ABS, "doc.md", "academic")
    names = {n.name for n in g.nodes}
    assert "Engram" in names and "Engram improves perplexity" in names
    assert "Ghost" not in names           # bad ev + name not in element -> dropped
    assert len(g.edges) == 1              # edge endpoints survived


def test_extract_graph_drops_and_reports_invalid_endpoint_pair(caplog):
    import json

    payload = json.dumps({
        "nodes": [
            {"local_id": "a", "type": "Formula", "name": "gain equation", "ev": 0},
            {"local_id": "b", "type": "Claim", "name": "gain increases", "ev": 0},
        ],
        # precedes is same-type only; Formula -> Claim is contract-invalid.
        "edges": [{"type": "precedes", "source": "a", "target": "b", "ev": 0}],
    })
    with caplog.at_level("WARNING", logger="app.services.kg.extract"):
        graph = kg_ingest.extract_graph(
            FakeClient(payload), "gain equation; gain increases", "doc.md", "academic"
        )
    assert graph.edges == []
    assert "invalid_endpoint_pair" in caplog.text


class _FlakyClient:
    """Raises APIConnectionError for the first `n_fail` distinct windows it sees
    (keyed by prompt text), returns valid JSON for the rest. The KG node evidence
    is grounded in the shared text so surviving windows contribute a node."""
    configured = True

    def __init__(self, fail_windows: set, payload: str):
        self._fail = set(fail_windows)
        self._payload = payload
        self._seen: list = []

    def chat_json(self, messages: list, response_schema_hint: str) -> str:
        from openai import APIConnectionError
        import httpx
        content = messages[0]["content"]
        # Window index = how many distinct windows already seen
        idx = len(self._seen)
        self._seen.append(content)
        if idx in self._fail:
            raise APIConnectionError(request=httpx.Request("POST", "https://x"))
        return self._payload


def test_extract_graph_counts_failed_windows():
    """Some windows raise a network error (counted as failed), others return valid
    JSON and contribute nodes. failed_windows == # raised, total_windows == #windows,
    surviving windows still produce nodes."""
    import json
    from app.services.kg.windowing import windows_with_elements

    # Build a multi-section markdown so windowing yields >=2 non-empty windows.
    quote = "Engram is a memory architecture"
    section_a = "# Section A\n\n" + quote + "\n\n"
    section_b = "# Section B\n\n" + quote + " indeed.\n\n"
    text = section_a + section_b
    pairs = windows_with_elements(text, "doc.md", None, 40, 5)
    n_windows = len(pairs)
    assert n_windows >= 2, f"need >=2 windows, got {n_windows}"

    # ev:0 anchors to each window's first prose element (contains the quote).
    payload = json.dumps({
        "nodes": [{"local_id": "a", "type": "Concept", "name": "Engram", "ev": 0}],
        "edges": [],
    })
    # Fail the first window only.
    client = _FlakyClient(fail_windows={0}, payload=payload)
    g = kg_ingest.extract_graph(client, text, "doc.md", "academic", n=40, m=5)
    assert g.total_windows == n_windows
    assert g.failed_windows == 1
    # Surviving windows still grounded at least one Engram node.
    assert any(n.name == "Engram" for n in g.nodes)


def test_extract_graph_counts_programming_errors_as_failed_windows():
    """A client/adapter error is not a legitimate empty extraction result."""
    from app.services.kg.windowing import windows_with_elements

    text = "# Section A\n\nA technical fact.\n\n# Section B\n\nAnother fact.\n"
    n_windows = len(windows_with_elements(text, "doc.md", None, 40, 5))
    assert n_windows >= 1

    class BrokenClient:
        configured = True

        def chat_json(self, messages, response_schema_hint):
            raise TypeError("adapter rejected a supported keyword")

    graph = kg_ingest.extract_graph(
        BrokenClient(), text, "doc.md", "academic", n=40, m=5
    )
    assert graph.failed_windows == n_windows
    assert graph.nodes == []


def test_extract_graph_no_failures_zero_count():
    import json
    payload = json.dumps({
        "nodes": [{"local_id": "a", "type": "Concept", "name": "Engram", "ev": 0}],
        "edges": [],
    })
    g = kg_ingest.extract_graph(FakeClient(payload), ABS, "doc.md", "academic")
    assert g.failed_windows == 0
    assert g.total_windows >= 1


def test_extract_graph_cancels_and_drains_windows_after_interrupt(monkeypatch):
    """中断(Ctrl-C/SIGTERM)也要逐层排空:兄弟窗口若不取消并等它们停下,来源 future
    会先完成,上层排空的是**来源** future,于是可能在窗口仍在调模型、仍会写图时就落
    终态、放开跨进程单飞守卫,让新构建与它们重叠(评审 P2)。"""
    total = 12
    elements = [
        SourceElementQ(
            id=f"e-{i}", type="paragraph", file="doc.md",
            line_start=i + 1, line_end=i + 1,
            char_start=i * 20, char_end=i * 20 + 12,
            text=f"technical fact {i}",
        )
        for i in range(total)
    ]
    pairs = [
        (SimpleNamespace(section_path=f"section-{i}"), [elements[i]])
        for i in range(total)
    ]
    monkeypatch.setattr(
        kg_ingest, "windows_with_elements", lambda *_args, **_kwargs: pairs
    )
    monkeypatch.setattr(
        kg_ingest, "should_extract_window", lambda *_args, **_kwargs: (True, "")
    )

    executor = cf.ThreadPoolExecutor(max_workers=2)
    monkeypatch.setattr(
        kg_ingest, "submit_window",
        lambda fn, *args, **kwargs: executor.submit(fn, *args, **kwargs),
    )

    class InterruptThenSlowClient:
        configured = True

        def __init__(self):
            self.lock = threading.Lock()
            self.calls = 0
            self.running = 0

        def chat_json(self, messages, response_schema_hint):
            with self.lock:
                self.calls += 1
                call_number = self.calls
                self.running += 1
            try:
                if call_number == 1:
                    raise KeyboardInterrupt("ctrl-c inside a window")
                time.sleep(0.05)
                return '{"nodes":[],"edges":[]}'
            finally:
                with self.lock:
                    self.running -= 1

    client = InterruptThenSlowClient()
    try:
        with pytest.raises(KeyboardInterrupt):
            kg_ingest.extract_graph(
                client, "ignored", "doc.md", "academic", n=10, m=0
            )
        assert client.running == 0, "兄弟窗口尚未停下就把中断抛了上去"
        assert client.calls < total
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def test_extract_graph_cancels_and_drains_windows_after_task_abort(monkeypatch):
    total = 12
    elements = [
        SourceElementQ(
            id=f"e-{i}", type="paragraph", file="doc.md",
            line_start=i + 1, line_end=i + 1,
            char_start=i * 20, char_end=i * 20 + 12,
            text=f"technical fact {i}",
        )
        for i in range(total)
    ]
    pairs = [
        (SimpleNamespace(section_path=f"section-{i}"), [elements[i]])
        for i in range(total)
    ]
    monkeypatch.setattr(
        kg_ingest, "windows_with_elements", lambda *_args, **_kwargs: pairs
    )
    monkeypatch.setattr(
        kg_ingest, "should_extract_window", lambda *_args, **_kwargs: (True, "")
    )

    executor = cf.ThreadPoolExecutor(max_workers=2)
    monkeypatch.setattr(
        kg_ingest, "submit_window",
        lambda fn, *args, **kwargs: executor.submit(fn, *args, **kwargs),
    )

    class AbortThenSlowClient:
        configured = True

        def __init__(self):
            self.lock = threading.Lock()
            self.calls = 0
            self.running = 0

        def chat_json(self, messages, response_schema_hint):
            with self.lock:
                self.calls += 1
                call_number = self.calls
                self.running += 1
            try:
                if call_number == 1:
                    raise KgBuildAborted(
                        KgBuildFailure("model_unavailable", "model unavailable")
                    )
                time.sleep(0.05)
                return '{"nodes":[],"edges":[]}'
            finally:
                with self.lock:
                    self.running -= 1

    client = AbortThenSlowClient()
    try:
        with pytest.raises(KgBuildAborted):
            kg_ingest.extract_graph(
                client, "ignored", "doc.md", "academic", n=10, m=0
            )
        assert client.running == 0
        assert client.calls < total
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def test_canonicalize_merges_across_windows():
    """Two windows both emit the same Concept 'Engram'; canonicalize must collapse
    to a single node.  The text is long enough (>200 chars in one prose section)
    to produce >=2 windows with n=200, m=20.

    We embed the evidence quote twice in the text so each window can ground its
    Engram node independently; then extract_graph's canonicalize step must merge
    them to exactly one Concept named 'Engram'.
    """
    import json

    # Evidence quote placed near position 0 (window 1: 0-200) and repeated
    # near position ~190 (window 2: 180-380) so both windows contain it.
    # We pad the text between the two occurrences to keep total length > 200.
    quote = "Engram is a memory architecture"
    # 34-char quote at start; padding to push past 200 chars; quote repeated
    padding = " " * (200 - len(quote))   # 166 spaces
    long_text = quote + padding + quote   # total ~234 chars in one paragraph

    payload = json.dumps({
        "nodes": [
            {"local_id": "a", "type": "Concept", "name": "Engram", "ev": 0},
        ],
        "edges": [],
    })
    g = kg_ingest.extract_graph(
        FakeClient(payload), long_text, "doc.md", "academic", n=200, m=20
    )
    engram_nodes = [n for n in g.nodes if n.name == "Engram"]
    # Both windows ground the same Concept; canonicalize must merge to 1.
    assert len(engram_nodes) == 1, (
        f"expected 1 Engram node after canonicalization, got {len(engram_nodes)}"
    )


def test_extract_graph_uses_global_pool():
    import json
    payload = json.dumps({"nodes": [{"local_id": "a", "type": "Concept",
                                      "name": "Engram", "ev": 0}], "edges": []})
    g = kg_ingest.extract_graph(FakeClient(payload), ABS, "doc.md", "academic")
    assert g.total_windows >= 1
    assert any(n.name == "Engram" for n in g.nodes)


def test_drop_noise_concepts_removes_symbols_and_dangling_edges():
    from app.services.kg.models import Node, Edge
    from app.services.kg_ingest import drop_noise_concepts
    nodes = [
        Node(id="n1", type="Concept", name="current mirror"),
        Node(id="n2", type="Concept", name="V_DD"),
        Node(id="n3", type="Claim", name="The current mirror copies the reference current."),
    ]
    edges = [
        Edge(id="e1", type="about", source_id="n3", target_id="n1"),
        Edge(id="e2", type="about", source_id="n3", target_id="n2"),
    ]
    kept_nodes, kept_edges, dropped = drop_noise_concepts(nodes, edges, frozenset())
    assert dropped == 1
    assert {n.id for n in kept_nodes} == {"n1", "n3"}
    assert {e.id for e in kept_edges} == {"e1"}


def test_drop_noise_concepts_keeps_whitelisted():
    from app.services.kg.models import Node
    from app.services.kg_ingest import drop_noise_concepts
    nodes = [Node(id="n1", type="Concept", name="VCO")]
    kept, _e, dropped = drop_noise_concepts(nodes, [], frozenset({"vco"}))
    assert dropped == 0 and len(kept) == 1
