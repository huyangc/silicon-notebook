"""KG-analysis findings rule engine — pure, hermetic (no database).

Every test drives the engine exactly the way the publish path will (design
2026-10-19 §7.1): canonical edges → ``CanonicalEdgeTableBuilder`` →
``GraphStructure`` (step ③) → ``FindingsObjectTally`` (step ⑤) →
``compute_findings`` (step ⑥) → lookup rounds → ``finalize_findings`` (step ⑧).
The library is described by a tiny in-memory DSL (``_Library``); the lookups are
answered from it, never from the canonical names the engine judges on.
"""
from __future__ import annotations

import itertools
import json
import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

import numpy as np
import pytest

from app.domain import kg_findings_contracts as contracts
from app.domain.kg_analysis_contracts import (
    ARTIFACT_FINDINGS,
    ARTIFACT_KINDS,
    CLUSTER_SEQ_PAYLOAD_KEY,
)
from app.domain.kg_findings_contracts import (
    ACTION_DELETE_SOURCE,
    ACTION_RELINK,
    ACTION_REPARSE_SOURCE,
    ACTION_REVIEW_MERGE,
    FINDING_EVIDENCE_UNITS,
    FINDINGS_PAYLOAD_UNITS,
    LEDGER_CONTRACT_KEY,
    LEDGER_CONTRACT_VERSION,
    NOTEBOOK_SUBJECT_KEY,
    RULES_VERSION,
    check_findings_comparison,
    check_findings_ledger,
    check_ledger_contract,
    finding_actions,
    ledger_row_predates_findings,
    source_pair_key,
    stamp_ledger_contract,
)
from app.domain.kg_merge_seed import concept_decision_seeds
from app.services import kg_findings
from app.services.kg_analysis_precompute import CanonicalEdgeTableBuilder
from app.services.kg_findings import (
    FindingsObjectTally,
    GraphStructure,
    SourceFacts,
    compute_findings,
    finalize_findings,
)


# ------------------------------------------------------------------ the DSL
@dataclass
class _Library:
    """objects: (source_id, canonical_id, is_concept); relations: (source_id, a, b);
    names: canonical names (internal, judged on); visible: what the store would
    return for ``(None, canonical_id)`` (default: the canonical name); own:
    per-(source, canonical) display names (default ``"<canonical>@<source>"``)."""

    objects: List[Tuple[str, str, bool]] = field(default_factory=list)
    relations: List[Tuple[str, str, str]] = field(default_factory=list)
    names: Dict[str, str] = field(default_factory=dict)
    visible: Dict[str, List[str]] = field(default_factory=dict)
    own: Dict[Tuple[str, str], str] = field(default_factory=dict)
    created: Dict[str, str] = field(default_factory=dict)
    # ``decided_seed_pairs_from``'s shape: {frozenset({seed_a, seed_b}): status}.
    decisions: Dict[FrozenSet[str], str] = field(default_factory=dict)
    # Every member name per canonical (hidden ones too); default: its canonical name.
    member_names: Dict[str, List[str]] = field(default_factory=dict)

    def add_source(self, source_id: str, concepts: Sequence[str], *,
                   chain: bool = True, extra_relations: Sequence[Tuple[str, str]] = (),
                   claims: int = 0) -> None:
        for cid in concepts:
            self.objects.append((source_id, cid, True))
            self.names.setdefault(cid, cid.replace("K-", "Name "))
        for k in range(claims):
            claim = f"KL-{source_id}-{k}"
            self.objects.append((source_id, claim, False))
            self.relations.append((source_id, claim, concepts[0]))
        if chain:
            for a, b in zip(concepts, concepts[1:]):
                self.relations.append((source_id, a, b))
        for a, b in extra_relations:
            self.relations.append((source_id, a, b))

    def drop_source(self, source_id: str) -> "_Library":
        return _Library(
            [o for o in self.objects if o[0] != source_id],
            [r for r in self.relations if r[0] != source_id],
            dict(self.names), dict(self.visible), dict(self.own), dict(self.created),
            dict(self.decisions), dict(self.member_names),
        )


@dataclass
class _Run:
    result: kg_findings.FindingsResult
    rounds: List[kg_findings.LookupRequest]
    seed_calls: List[List[str]]

    def rows(self, rule: str) -> List[dict]:
        return [row for row in self.result.rows if row["rule_id"] == rule]

    def keys(self, rule: str) -> List[str]:
        return [row["subject_key"] for row in self.rows(rule)]

    def status(self, rule: str) -> dict:
        return self.result.payload["rules"][rule]


def _run(lib: _Library, *, cap: int = 1_000_000, object_order: Optional[int] = None,
         backfill_extension: Sequence[str] = (), supply_pages: int = 1) -> _Run:
    can2idx: Dict[str, int] = {}
    builder = CanonicalEdgeTableBuilder()
    relations: Counter = Counter()
    for source_id, a, b in lib.relations:
        relations[source_id] += 1          # every row, self loops included (step ①)
        if a == b:
            continue
        builder.add(can2idx.setdefault(a, len(can2idx)), can2idx.setdefault(b, len(can2idx)))
    table = builder.build(len(can2idx))
    structure = GraphStructure.from_edges(table.src, table.dst, table.n_nodes)
    for cid in backfill_extension:         # what _compute_kg_analysis does on backfill
        can2idx.setdefault(cid, len(can2idx))
    objects = list(lib.objects)
    if object_order is not None:
        random.Random(object_order).shuffle(objects)
    tally = FindingsObjectTally(can2idx, structure)
    for source_id, cid, is_concept in objects:
        tally.add(source_id, cid, is_concept)
    calls: List[List[str]] = []

    def member_seeds(ids):
        calls.append(list(ids))
        # The product's member read: the decision seeds (v1 and v2) of every
        # member name (``concept_decision_seeds``).
        return {cid: set().union(*(concept_decision_seeds(n)
                                   for n in lib.member_names.get(cid, [lib.names[cid]])))
                for cid in ids if cid in lib.names or cid in lib.member_names}

    draft = compute_findings(
        tally=tally, structure=structure, can2idx=can2idx, names=lib.names,
        relations_by_source=relations, decisions=lib.decisions,
        member_seeds=member_seeds, max_candidate_pairs=cap,
    )
    can2idx.clear()                        # the protocol: nothing needs it any more
    rounds = []
    while (request := draft.pending()) is not None:
        rounds.append(request)
        names = []
        for source_id, cid in request.names:
            if source_id is None:
                for name in lib.visible.get(cid, [lib.names.get(cid, cid)]):
                    names.append((None, cid, name))
            else:
                names.append((source_id, cid, lib.own.get((source_id, cid), f"{cid}@{source_id}")))
        sources = {sid: SourceFacts(f"title-{sid}", lib.created.get(sid))
                   for sid in request.sources}
        # Like the product's member-name read: the seeds of EVERY member (hidden
        # ones too) of each (None, canonical) key, for F4's evidence.
        seeds = {cid: set().union(*(concept_decision_seeds(n)
                                    for n in lib.member_names.get(cid, [lib.names[cid]])))
                 for source_id, cid in request.names
                 if source_id is None and (cid in lib.names or cid in lib.member_names)}
        # A paged caller: the answers of one round arrive over several calls.
        for page in range(supply_pages):
            draft.supply(names=names[page::supply_pages],
                         sources={k: v for i, (k, v) in enumerate(sorted(sources.items()))
                                  if i % supply_pages == page},
                         member_seeds=seeds if page == 0 else None)
    return _Run(draft.result(), rounds, calls)


def _mainstream(lib: _Library, count: int = 12) -> None:
    """``count`` ordinary sources: sliding windows of 20 over a ring pool, shift
    14 — each shares 12 of its 20 concepts with its two neighbours (60%), any
    two sources overlap in at most 6 (J ≤ 0.18), one giant component."""
    pool = 14 * count
    for i in range(count):
        concepts = [f"K-core{(14 * i + k) % pool:03d}" for k in range(20)]
        lib.add_source(f"m{i:02d}", concepts, claims=2)


def _truth_library() -> _Library:
    """The truth fixture: an island off-topic file, a bridged off-topic file
    (one shared concept pulls it into the giant component), a normal duplicate
    pair, and a duplicated pair of off-topic files."""
    lib = _Library()
    _mainstream(lib)
    lib.add_source("x-island", [f"K-isl{k:02d}" for k in range(20)], claims=2)
    bridged = [f"K-brg{k:02d}" for k in range(24)]
    lib.add_source("x-bridged", bridged + ["K-core000"], claims=2,
                   extra_relations=[(c, "K-core000") for c in bridged[::6]])
    dup = [f"K-core{k:03d}" for k in range(40, 52)] + [f"K-dup{k}" for k in range(8)]
    lib.add_source("d-a", dup, claims=2)
    lib.add_source("d-b", dup[:-1], claims=2)
    wrong = [f"K-wrong{k:02d}" for k in range(20)]
    lib.add_source("w-a", wrong, claims=2)
    lib.add_source("w-b", wrong, claims=2)
    return lib


# ===================================================================== truth
def test_truth_fixture_reports_exactly_the_planted_problems():
    run = _run(_truth_library())
    # impact desc: the merged duplicate (2 × 22), x-bridged (27), x-island (22)
    assert run.keys("F1") == ["w-a", "x-bridged", "x-island"]
    assert run.keys("F2") == [source_pair_key("d-a", "d-b")]
    assert run.keys("F3") == [] and run.keys("F4") == []
    assert run.status("F1")["sources_hit"] == 4
    assert run.status("F2")["pairs_hit"] == 2
    assert run.status("F2")["pairs_absorbed_into_f1"] == 1
    check_findings_ledger(run.result.payload, run.result.rows)


def test_off_topic_gate_is_sharing_only_giant_share_is_evidence():
    """x-bridged shares 1 of 25 concepts, and that bridge pulls ALL of it into the
    giant component. The old ``giant_share <= 5%`` gate would drop it (4 of the
    12 local hits look like this); the gate is sharing only."""
    run = _run(_truth_library())
    bridged = next(row for row in run.rows("F1") if row["subject_key"] == "x-bridged")
    assert bridged["evidence"]["shared_share"] == pytest.approx(1 / 25)
    assert bridged["evidence"]["giant_share"] == 1.0
    island = next(row for row in run.rows("F1") if row["subject_key"] == "x-island")
    assert island["evidence"]["giant_share"] == 0.0


def test_duplicate_offtopic_pair_yields_one_f1():
    """Two copies of the same wrong file share everything with each other only.
    Without excluding the duplicate partner each "shares" 100% and F1 misses
    both; with it, they become ONE F1 row keyed by the smaller source id, the
    partner attached, one delete per copy — and their F2 pair is not listed."""
    run = _run(_truth_library())
    row = next(row for row in run.rows("F1") if row["subject_key"] == "w-a")
    assert row["subject_refs"] == ["w-a", "w-b"]
    assert [p["source_id"] for p in row["evidence"]["partners"]] == ["w-b"]
    assert row["evidence"]["partners"][0]["label"] == "title-w-b"
    assert row["evidence"]["shared_share"] == 0.0
    # Evidence carries only what the criterion used (§3.1): objects are the impact.
    assert "objects" not in row["evidence"]
    assert "objects" not in row["evidence"]["partners"][0]
    assert row["impact_objects"] == 44                       # both copies' objects
    assert source_pair_key("w-a", "w-b") not in run.keys("F2")
    actions = finding_actions("F1", row["subject_key"], row["subject_refs"], row["evidence"])
    assert [(a["kind"], a["source_id"]) for a in actions] == [
        (ACTION_DELETE_SOURCE, "w-a"), (ACTION_DELETE_SOURCE, "w-b")]


def test_partial_duplicate_keeps_its_own_f2_row():
    """Only one copy off-topic (local: Zamba): it gets an F1 row on its own and
    the pair stays an F2 finding."""
    lib = _Library()
    _mainstream(lib)
    shared = [f"K-half{k:02d}" for k in range(19)]
    lib.add_source("p-a", shared + ["K-pa"])                 # shares only with p-b
    lib.add_source("p-b", shared + ["K-core003", "K-core004", "K-core005", "K-core006",
                                    "K-core007", "K-core008", "K-core009", "K-core010",
                                    "K-core011", "K-core012", "K-core013"])
    run = _run(lib)
    assert run.keys("F1") == ["p-a"]
    assert run.keys("F2") == [source_pair_key("p-a", "p-b")]
    assert run.status("F2")["pairs_absorbed_into_f1"] == 0


def test_unrelated_deletion_keeps_keys():
    lib = _truth_library()
    before = _run(lib)
    after = _run(lib.drop_source("x-island"))
    for rule in ("F1", "F2", "F3", "F4"):
        expected = [k for k in before.keys(rule) if k != "x-island"]
        assert after.keys(rule) == expected, rule
    for rule in ("F1", "F2"):
        for row_before in before.rows(rule):
            if row_before["subject_key"] == "x-island":
                continue
            row_after = next(r for r in after.rows(rule)
                             if r["subject_key"] == row_before["subject_key"])
            assert row_after["evidence"] == row_before["evidence"]


def test_object_row_order_does_not_change_anything():
    """`source_canonical_rows` has no ORDER BY: the two backends may stream the
    objects in different orders. Sources and off-graph concepts are renumbered
    by id, so the rows are byte-identical (the structural half of parity)."""
    lib = _truth_library()
    lib.add_source("z-isolated", [f"K-alone{k}" for k in range(12)], chain=False)
    baseline = json.dumps(_run(lib).result.__dict__, sort_keys=True, default=str)
    for seed in (1, 2, 3):
        assert json.dumps(_run(lib, object_order=seed).result.__dict__,
                          sort_keys=True, default=str) == baseline


# ===================================================================== F1 edges
def _library_with_off_topic(unique: int, shared: int, *, extra_sources: int = 12) -> _Library:
    lib = _Library()
    _mainstream(lib, extra_sources)
    concepts = [f"K-ot{k:02d}" for k in range(unique)] + [f"K-core{k:03d}" for k in range(shared)]
    lib.add_source("ot", concepts)
    return lib


def test_f1_threshold_is_strict():
    exactly = _run(_library_with_off_topic(19, 1))           # 1/20 = 5% exactly
    assert "ot" not in exactly.keys("F1")
    below = _run(_library_with_off_topic(20, 1))             # 1/21 < 5%
    assert "ot" in below.keys("F1")


def test_f1_concept_floor_is_inclusive():
    nine = _run(_library_with_off_topic(9, 0))
    assert "ot" not in nine.keys("F1")
    assert nine.status("F1")["sources_too_few_concepts"] == 1
    ten = _run(_library_with_off_topic(10, 0))
    assert "ot" in ten.keys("F1")
    assert ten.status("F1")["sources_too_few_concepts"] == 0


def test_f1_representatives_are_unique_concepts_by_degree():
    lib = _library_with_off_topic(20, 1)
    hub = "K-ot00"
    lib.relations.extend(("ot", hub, f"K-ot{k:02d}") for k in range(2, 20))
    lib.own[("ot", hub)] = "Own Hub Name"
    run = _run(lib)
    row = next(r for r in run.rows("F1") if r["subject_key"] == "ot")
    assert len(row["evidence"]["samples"]) == 5
    assert row["evidence"]["samples"][0] == "Own Hub Name"
    # Display names are the source's OWN objects', never the canonical name.
    assert all(not s.startswith("Name ") for s in row["evidence"]["samples"])
    assert "K-core000@ot" not in row["evidence"]["samples"]  # shared, not unique


def test_rules_that_need_two_sources_say_they_could_not_check():
    lib = _Library()
    lib.add_source("only", [f"K-c{k}" for k in range(20)])
    run = _run(lib)
    for rule in ("F1", "F2"):
        assert run.status(rule)["status"] == "skipped"
        assert run.status(rule)["reason"] == "fewer_than_two_sources"
    assert run.status("F3")["status"] == "ran"
    check_findings_ledger(run.result.payload, run.result.rows)


def test_empty_library_skips_every_rule():
    run = _run(_Library())
    for rule in ("F1", "F2", "F3", "F4", "F5", "S1"):
        assert run.status(rule)["status"] == "skipped", rule
        assert run.status(rule)["reason"] == "no_objects"
    assert run.result.rows == []
    assert run.result.payload["structure"]["graph_nodes"] == 0
    check_findings_ledger(run.result.payload, run.result.rows)


def test_graph_without_edges_still_judges_f1_without_giant_share():
    lib = _Library()
    lib.add_source("a", [f"K-a{k}" for k in range(12)], chain=False)
    lib.add_source("b", [f"K-b{k}" for k in range(12)], chain=False)
    run = _run(lib)
    assert run.keys("F1") == ["a", "b"]
    # No relation at all: no giant component, so no giant_share key (not null).
    assert all("giant_share" not in row["evidence"] for row in run.rows("F1"))


# ===================================================================== F2
def _random_library(seed: int) -> _Library:
    rng = random.Random(seed)
    lib = _Library()
    pool = [f"K-r{k:03d}" for k in range(rng.randint(30, 90))]
    for s in range(rng.randint(8, 40)):
        size = rng.randint(1, 30)
        if s and rng.random() < 0.3:           # plant near-duplicates of an earlier source
            base = [cid for sid, cid, c in lib.objects if sid == f"s{rng.randrange(s):02d}"]
            concepts = sorted(set(rng.sample(base, k=max(1, int(len(base) * rng.uniform(0.4, 1.0))))
                                  + rng.sample(pool, k=rng.randint(0, 6))))
        else:
            concepts = sorted(set(rng.sample(pool, k=min(size, len(pool)))))
        lib.add_source(f"s{s:02d}", concepts, chain=False)
    return lib


def _brute_force_pairs(lib: _Library) -> Dict[str, Tuple[int, int]]:
    sets: Dict[str, Set[str]] = {}
    for sid, cid, c in lib.objects:
        if c:
            sets.setdefault(sid, set()).add(cid)
    df = Counter(cid for concepts in sets.values() for cid in concepts)
    universe = {sid: {c for c in cs if df[c] <= contracts.UBIQUITOUS_SOURCE_COUNT}
                for sid, cs in sets.items()}
    out = {}
    for a, b in itertools.combinations(sorted(universe), 2):
        inter = len(universe[a] & universe[b])
        union = len(universe[a] | universe[b])
        if union and inter * 20 >= 9 * union:
            out[source_pair_key(a, b)] = (inter, union)
    return out


def _join(lib: _Library) -> Dict[str, Tuple[int, int]]:
    """Run only the drain + F2 join, mapped back to source-id pairs."""
    structure = GraphStructure.from_edges(np.empty(0, np.int32), np.empty(0, np.int32), 0)
    tally = FindingsObjectTally({}, structure)
    for source_id, cid, is_concept in lib.objects:
        tally.add(source_id, cid, is_concept)
    corpus = kg_findings._drain_tally(tally, structure)
    joined = kg_findings._duplicate_pairs(corpus, 10_000_000)
    assert joined.candidates_truncated is False
    return {
        source_pair_key(corpus.source_ids[a], corpus.source_ids[b]): (inter, union)
        for a, b, inter, union in joined.pairs
    }


@pytest.mark.parametrize("seed", range(40))
def test_prefix_join_equals_brute_force(seed, monkeypatch):
    """The prefix-filtering join is exact: on random libraries (planted
    near-duplicates, a small ubiquity threshold so the universe filter bites,
    tiny verification batches) it returns exactly the brute-force all-pairs set,
    pair for pair, with the same overlap and union sizes."""
    monkeypatch.setattr(kg_findings, "UBIQUITOUS_SOURCE_COUNT", 6)
    monkeypatch.setattr(contracts, "UBIQUITOUS_SOURCE_COUNT", 6)
    monkeypatch.setattr(kg_findings, "_VERIFY_ELEMENT_BUDGET", 7)
    monkeypatch.setattr(kg_findings, "_VERIFY_PAIR_CHUNK", 5)
    lib = _random_library(seed)
    assert _join(lib) == _brute_force_pairs(lib)


def _star_universe(big: int, partners: int) -> Tuple[np.ndarray, ...]:
    """Universe CSR: source 0 holds ``big`` concepts; sources 1..partners hold 5
    each, the first shared with source 0. Candidates: (0, i) for every partner —
    one big LEFT source repeated, the shape that blew up per-pair row copies."""
    rows = [np.arange(big, dtype=np.int32)]
    for i in range(partners):
        rows.append(np.asarray([0] + [big + 4 * i + k for k in range(4)], dtype=np.int32))
    sizes = np.asarray([r.size for r in rows], dtype=np.int64)
    indptr = np.zeros(sizes.size + 1, dtype=np.int64)
    np.cumsum(sizes, out=indptr[1:])
    universe = np.concatenate(rows)
    cand_a = np.zeros(partners, dtype=np.int32)
    cand_b = np.arange(1, partners + 1, dtype=np.int32)
    width = big + 4 * partners + 1
    return cand_a, cand_b, universe, indptr, sizes, width


def _verification_peak(big: int, partners: int) -> int:
    import tracemalloc

    args = _star_universe(big, partners)
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        before = tracemalloc.get_traced_memory()[0]
        pairs = kg_findings._verify_pairs(*args)
        peak = tracemalloc.get_traced_memory()[1] - before
    finally:
        tracemalloc.stop()
    assert pairs == []                               # 1 shared of 5 vs `big`: no duplicate
    return peak


def test_verification_memory_is_bounded_by_the_element_budget():
    """One big source with many partners costs its partners' sizes, never its own
    size per partner: the peak does not grow with the big source. (Per-pair row
    copies measured 2.68 GB on 20 × 5000-concept sources / 340 k candidates.)"""
    small = _verification_peak(2_000, 20_000)
    large = _verification_peak(32_000, 20_000)
    assert small < 16_000_000 and large < 16_000_000, (small, large)
    assert large - small < 1_000_000, (small, large)   # only the 8 B/concept key column


def test_verification_steps_respect_the_element_budget(monkeypatch):
    seen = []
    real = np.searchsorted

    def spy(a, v, *args, **kwargs):
        if np.ndim(v) and np.asarray(v).dtype == np.int64 and np.asarray(a).size > 1000:
            seen.append(int(np.size(v)))
        return real(a, v, *args, **kwargs)

    monkeypatch.setattr(kg_findings, "_VERIFY_ELEMENT_BUDGET", 1_000)
    monkeypatch.setattr(np, "searchsorted", spy)
    kg_findings._verify_pairs(*_star_universe(3_000, 2_000))
    assert seen and max(seen) <= 1_000                 # 2,000 × 5 elements, in steps of ≤ 1,000
    assert sum(seen) == 2_000 * 5


def test_candidate_generation_memory_per_pair():
    """Generation is the capped stage; its peak per generated pair is what the
    deployment reference quotes (measured ≈ 25 B)."""
    import tracemalloc

    members = 1_500                                    # one inverted list: C(1500, 2) pairs
    p_src = np.arange(members, dtype=np.int32)
    p_can = np.zeros(members, dtype=np.int32)
    df = np.asarray([members], dtype=np.int32)
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        before = tracemalloc.get_traced_memory()[0]
        a, b, generated, truncated = kg_findings._candidate_pairs(
            p_src, p_can, df, members, 10_000_000)
        peak = tracemalloc.get_traced_memory()[1] - before
    finally:
        tracemalloc.stop()
    assert generated == members * (members - 1) // 2 == a.size and not truncated
    assert peak / generated <= 32, peak / generated


def test_generation_count_includes_repeats_and_is_what_the_cap_bounds():
    """Three sources sharing two prefix concepts: THREE distinct candidates, SIX
    generations (three per inverted list). The cap and the ledger's limit are
    about generations."""
    p_src = np.asarray([0, 1, 2, 0, 1, 2], dtype=np.int32)
    p_can = np.asarray([0, 0, 0, 1, 1, 1], dtype=np.int32)
    df = np.asarray([3, 3], dtype=np.int32)
    a, b, generated, truncated = kg_findings._candidate_pairs(p_src, p_can, df, 3, 10)
    assert (list(zip(a.tolist(), b.tolist())), generated, truncated) == (
        [(0, 1), (0, 2), (1, 2)], 6, False)
    a, b, generated, truncated = kg_findings._candidate_pairs(p_src, p_can, df, 3, 5)
    assert (a.size, generated, truncated) == (3, 3, True)   # the second list is cut


def test_the_random_join_fixtures_are_not_trivial(monkeypatch):
    monkeypatch.setattr(contracts, "UBIQUITOUS_SOURCE_COUNT", 6)
    found = [len(_brute_force_pairs(_random_library(seed))) for seed in range(40)]
    assert sum(1 for n in found if n) >= 20 and max(found) >= 3


def test_jaccard_exactly_on_the_threshold_is_a_duplicate():
    lib = _Library()
    common = [f"K-c{k}" for k in range(9)]
    lib.add_source("a", common + [f"K-a{k}" for k in range(5)], chain=False)   # 14
    lib.add_source("b", common + [f"K-b{k}" for k in range(6)], chain=False)   # 15
    assert 9 / (14 + 15 - 9) == 0.45
    run = _run(lib)
    assert run.status("F2")["pairs_hit"] == 1
    lib.objects.append(("b", "K-b-extra", True))                             # 9/21
    assert _run(lib).status("F2")["pairs_hit"] == 0


def test_ubiquitous_concepts_leave_the_jaccard_universe(monkeypatch):
    """Concepts in MORE than the threshold of sources say nothing about whether
    two files are the same one; at exactly the threshold they still count."""
    monkeypatch.setattr(kg_findings, "UBIQUITOUS_SOURCE_COUNT", 3)
    lib = _Library()
    generic = [f"K-g{k}" for k in range(10)]
    for sid in ("a", "b", "c"):
        lib.add_source(sid, generic + [f"K-{sid}{k}" for k in range(6)], chain=False)
    at_threshold = _run(lib)                 # generic in 3 sources: kept, J = 10/22
    assert at_threshold.status("F2")["pairs_hit"] == 3
    lib.add_source("d", generic + [f"K-d{k}" for k in range(6)], chain=False)
    above = _run(lib)                        # generic in 4 sources: dropped, J = 0
    assert above.status("F2")["pairs_hit"] == 0


def test_f2_keeps_the_copy_with_more_objects_then_the_earlier_import():
    # Core concepts: the pair belongs to the library (otherwise it would be a
    # duplicated pair of OFF-TOPIC files and become one F1 row instead), spread
    # over many mainstream windows so neither copy duplicates a mainstream source.
    concepts = [f"K-core{k:03d}" for k in range(0, 150, 15)]
    lib = _Library()
    _mainstream(lib)
    lib.add_source("a", concepts, claims=1)
    lib.add_source("b", concepts, claims=3)
    run = _run(lib)
    row = run.rows("F2")[0]
    assert [(s["source_id"], s["keep"]) for s in row["evidence"]["sources"]] == [
        ("a", False), ("b", True)]
    assert row["impact_objects"] == 11
    assert [a["source_id"] for a in finding_actions(
        "F2", row["subject_key"], row["subject_refs"], row["evidence"])] == ["a"]
    tie = _Library()
    _mainstream(tie)
    tie.add_source("a", concepts)
    tie.add_source("b", concepts)
    tie.created = {"a": "2026-05-02T00:00:00", "b": "2026-05-01T00:00:00"}
    row = _run(tie).rows("F2")[0]
    assert [(s["source_id"], s["keep"]) for s in row["evidence"]["sources"]] == [
        ("a", False), ("b", True)]
    tie.created = {}
    row = _run(tie).rows("F2")[0]            # no dates: the smaller id stays
    assert [s["keep"] for s in row["evidence"]["sources"]] == [True, False]


def test_candidate_cap_is_disclosed_never_silent():
    lib = _truth_library()
    run = _run(lib, cap=1)
    f2, f1 = run.status("F2"), run.status("F1")
    assert f2["candidates_truncated"] is True and f2["truncated"] is True
    assert f2["candidate_pair_generation_limit"] == 1
    assert f2["candidate_pair_generations"] <= 1
    assert f1["partner_exclusion_incomplete"] is True and f1["truncated"] is True
    check_findings_ledger(run.result.payload, run.result.rows)
    full = _run(lib)
    assert full.status("F2")["candidates_truncated"] is False
    assert full.status("F1")["truncated"] is False


def test_persistence_cap_is_disclosed(monkeypatch):
    monkeypatch.setattr(kg_findings, "MAX_PERSISTED_FINDINGS", 1)
    run = _run(_truth_library())
    status = run.status("F1")
    assert (status["total"], status["persisted"], status["truncated"]) == (3, 1, True)
    # The window is impact desc, then key asc: the merged duplicate (44 objects).
    assert run.keys("F1") == ["w-a"]
    check_findings_ledger(run.result.payload, run.result.rows)


# ===================================================================== F3
def _parse_damage_library() -> _Library:
    lib = _Library()
    _mainstream(lib)
    broken = [f"K-broken{k}" for k in range(6)]
    concepts = broken + [f"K-core{k:03d}" for k in range(60, 74)]     # 6 / 20 = 30%
    lib.add_source("frag", concepts)
    for k, cid in enumerate(broken):
        lib.names[cid] = "L l a m a " + str(k)
    lib.own.update({("frag", cid): f"L l a m a {k} (own)" for k, cid in enumerate(broken)})
    return lib


def test_f3_fragmented_names_hit_and_take_precedence_over_f1():
    lib = _parse_damage_library()
    off = [f"K-off{k:02d}" for k in range(14)]
    lib.add_source("frag-off", [f"K-fo{k}" for k in range(6)] + off)
    for k in range(6):
        lib.names[f"K-fo{k}"] = "Q w e n " + str(k)
    run = _run(lib)
    assert run.keys("F3") == ["frag", "frag-off"]
    assert "frag-off" not in run.keys("F1")                   # F3 wins (§4.3)
    assert run.status("F1")["sources_deferred_to_f3"] == 1
    rows = {row["subject_key"]: row for row in run.rows("F3")}
    assert rows["frag-off"]["evidence"]["also_off_topic"] is True
    assert rows["frag"]["evidence"]["also_off_topic"] is False
    evidence = rows["frag"]["evidence"]
    assert evidence["signals"] == ["fragmented_names"]
    assert evidence["fragmented_names"] == 6 and evidence["fragmented_share"] == 0.3
    assert rows["frag"]["impact_objects"] == 6               # F3a: broken names' objects
    assert all(s.endswith("(own)") for s in evidence["samples"])
    assert [a["kind"] for a in finding_actions(
        "F3", "frag", ["frag"], evidence)] == [ACTION_REPARSE_SOURCE]


def test_f3_fragmented_share_floor_and_count_floor():
    lib = _Library()
    _mainstream(lib)
    lib.add_source("edge", [f"K-e{k}" for k in range(5)] + [f"K-core{k:03d}" for k in range(45)])
    for k in range(5):
        lib.names[f"K-e{k}"] = "a b c d " + str(k)
    run = _run(lib)                                          # 5 / 50 = 10%, 5 names
    assert run.keys("F3") == ["edge"]
    lib.objects.append(("edge", "K-core099", True))          # 5 / 51 < 10%
    assert _run(lib).keys("F3") == []
    four = _Library()
    _mainstream(four)
    four.add_source("edge", [f"K-e{k}" for k in range(4)] + [f"K-core{k:03d}" for k in range(16)])
    for k in range(4):
        four.names[f"K-e{k}"] = "a b c d " + str(k)
    assert _run(four).keys("F3") == []                       # 20% but only 4 names


def test_f3_low_relations_is_relative_to_the_median():
    lib = _Library()
    _mainstream(lib)                                         # 21 relations / 22 objects
    lib.add_source("thin", [f"K-core{k:03d}" for k in range(30, 50)], chain=False,
                   extra_relations=[("K-core030", "K-core031")])
    run = _run(lib)
    assert run.keys("F3") == ["thin"]
    row = run.rows("F3")[0]
    assert row["evidence"]["signals"] == ["low_relations"]
    assert row["impact_objects"] == 20                       # F3b: the whole source
    assert row["evidence"]["median_relations_per_object"] == pytest.approx(21 / 22)


def _yield_library(isolated_target: int, *, isolated_others: int = 10,
                   relations_target: int = 200, relations_others: int = 200,
                   objects_target: int = 200) -> _Library:
    """Three sources of 200 objects plus a target, overlapping enough to share
    their concepts. Every relation row counts toward F3b (self loops pad the
    counts without touching the graph). The target differs only in its size,
    isolated objects and relation rows."""
    lib = _Library()
    shape = [(isolated_others, relations_others, 200)] * 3 + [
        (isolated_target, relations_target, objects_target)]
    for i, (isolated, relations, size) in enumerate(shape):
        sid = f"s{i}"
        linked = [f"K-core{k:03d}" for k in range(i * 40, i * 40 + size - isolated)]
        lib.add_source(sid, linked, chain=False)
        lib.relations.extend((sid, a, b) for a, b in zip(linked, linked[1:]))
        padding = relations - (len(linked) - 1)
        assert padding >= 0
        lib.relations.extend((sid, linked[0], linked[0]) for _ in range(padding))
        for k in range(isolated):
            lib.objects.append((sid, f"K-alone{i}-{k}", True))
    return lib


def test_f3_isolated_share_is_strictly_above_three_medians_and_floored():
    assert _run(_yield_library(30)).keys("F3") == []         # exactly 3 × the 5% median
    # A float trap: 0.29 * 3 == 0.8699999999999999 < 0.87. The exact tie stays a
    # non-hit only because the comparison is rational.
    assert 0.29 * 3 < 0.87
    assert _run(_yield_library(174, isolated_others=58)).keys("F3") == []
    run = _run(_yield_library(31))
    assert run.keys("F3") == ["s3"]
    assert run.rows("F3")[0]["evidence"]["signals"] == ["high_isolated"]
    assert run.rows("F3")[0]["impact_objects"] == 200        # F3b: the whole source
    # 19% of 100 objects is far above both cuts, but only 19 isolated objects.
    small = dict(isolated_others=1, objects_target=100, relations_target=100)
    assert _run(_yield_library(19, **small)).keys("F3") == []
    assert _run(_yield_library(20, **small)).keys("F3") == ["s3"]


def test_f3_isolated_share_floor_holds_when_the_median_is_zero():
    """A median of 0 used to make "20 isolated objects" a hit at any share. The
    cut is max(3 × median, 10%), strictly."""
    big = dict(isolated_others=0, objects_target=1000, relations_target=1000)
    assert _run(_yield_library(30, **big)).keys("F3") == []          # 3%: not a hit any more
    assert _run(_yield_library(100, **big)).keys("F3") == []         # exactly 10%
    run = _run(_yield_library(101, **big))
    assert run.keys("F3") == ["s3"]
    evidence = run.rows("F3")[0]["evidence"]
    assert evidence["median_isolated_share"] == 0.0
    assert evidence["isolated_share_threshold"] == 0.1


def test_f3_low_relations_needs_twenty_objects_and_ignores_tiny_sources_in_the_median():
    lib = _Library()
    _mainstream(lib, 3)                                   # 22 objects, 21 relations each
    for k in range(10):                                   # tiny, relation-less notes
        lib.add_source(f"tiny{k}", [f"K-tiny{k}-{j}" for j in range(5)], chain=False)
    lib.add_source("t-thin", [f"K-core{k:03d}" for k in range(19)], chain=False)
    run = _run(lib)
    # 19 objects, no relation: below the object floor, not judged.
    assert run.keys("F3") == []
    # The median is over the judged sources only (the tiny ones would drag it to 0).
    assert run.status("F3")["median_relations_per_object"] == pytest.approx(21 / 22)
    lib.objects.append(("t-thin", "K-core019", True))     # 20 objects now
    assert _run(lib).keys("F3") == ["t-thin"]
    nothing_judged = _Library()
    nothing_judged.add_source("a", ["K-a0", "K-a1"])
    nothing_judged.add_source("b", ["K-b0", "K-b1"])
    status = _run(nothing_judged).status("F3")
    assert status["median_relations_per_object"] is None and status["total"] == 0


def test_f3_low_relations_is_strictly_below_half_the_median():
    exactly = _yield_library(10, relations_target=200, relations_others=400)
    assert _run(exactly).keys("F3") == []                    # 1.0 = half of 2.0
    run = _run(_yield_library(10, relations_target=199, relations_others=400))
    assert run.keys("F3") == ["s3"]
    row = run.rows("F3")[0]
    assert row["evidence"]["signals"] == ["low_relations"]
    assert row["evidence"]["median_relations_per_object"] == 2.0


# ===================================================================== F4 / S1
def _merge_library() -> _Library:
    lib = _Library()
    _mainstream(lib)
    spellings = {
        "K-q former": "Q-Former", "K-qformer": "Qformer",           # narrow (S1)
        "K-llama 3 1": "Llama 3.1", "K-llama 31": "Llama 31",       # wide + narrow
        "K-llama31": "Llama31",
    }
    lib.add_source("n-a", ["K-q former", "K-llama 3 1"] + [f"K-core{k:03d}" for k in range(18)])
    lib.add_source("n-b", ["K-qformer", "K-llama 31", "K-llama31"] +
                   [f"K-core{k:03d}" for k in range(20, 37)])
    lib.names.update(spellings)
    return lib


def test_narrow_wide_split():
    """Narrow = v1 seeds differ, v2 seeds equal (S1 merges it, counted for "needs
    development"); wide = v2 seeds differ, squash keys equal (F4, for a human).
    One squash group can hold both halves."""
    run = _run(_merge_library())
    s1 = run.status("S1")
    assert s1["narrow_groups"] == 2              # {Q-Former, Qformer}, {Llama 31, Llama31}
    assert s1["narrow_group_objects"] == 4
    assert sorted(sorted(e["names"]) for e in s1["examples"]) == [
        ["Llama 31", "Llama31"], ["Q-Former", "Qformer"]]
    assert run.keys("F4") == ["llama31"]
    row = run.rows("F4")[0]
    assert row["evidence"]["canonicals"] == 3 and row["evidence"]["spellings"] == 2
    assert {m["name"] for m in row["evidence"]["members"]} == {"Llama 3.1", "Llama 31", "Llama31"}
    assert row["subject_refs"] == ["n-a", "n-b"] and row["evidence"]["member_sources"] == 2
    assert [a["kind"] for a in finding_actions(
        "F4", row["subject_key"], row["subject_refs"], row["evidence"])] == [ACTION_REVIEW_MERGE]


def test_f4_and_s1_skip_what_the_user_rejected_or_deferred():
    """Decisions arrive keyed by v1 SEED pair (``decided_seed_pairs_from``)."""
    lib = _merge_library()
    lib.decisions = {frozenset(("llama 3 1", "llama 31")): "rejected",  # across the classes
                     frozenset(("q former", "qformer")): "deferred",
                     frozenset(("llama 31", "llama31")): "confirmed"}   # not "apart"
    run = _run(lib)
    assert run.keys("F4") == []
    assert run.status("F4")["groups_decided_apart"] == 1
    assert run.status("S1")["narrow_groups"] == 1               # Llama 31 / Llama31 still open
    assert run.status("S1")["groups_decided_apart"] == 1
    assert run.seed_calls == [sorted(["K-llama 3 1", "K-llama 31", "K-llama31",
                                      "K-q former", "K-qformer"])]


def test_f4_matches_decisions_minted_in_seed_space_2():
    """After a library's first v2 rebuild its decisions are keyed by v2 seeds
    (``llama3 1``); they keep F4 quiet exactly like the v1 keys did."""
    lib = _merge_library()
    lib.decisions = {frozenset(("llama3 1", "llama31")): "rejected"}
    run = _run(lib)
    assert run.keys("F4") == []
    assert run.status("F4")["groups_decided_apart"] == 1


def test_f4_reads_an_override_seed_as_its_v1_seed():
    """A decision the recompute kept on the old seed names ``~v1:<seed>``; it
    matches the members whose v1 seed it carries."""
    lib = _merge_library()
    lib.decisions = {frozenset(("~v1:llama 3 1", "llama31")): "deferred"}
    run = _run(lib)
    assert run.keys("F4") == []


def test_f4_and_s1_leave_out_canonicals_kept_on_their_old_seed():
    """A canonical the decision recompute kept on its v1 seed (``K-~v1:…``) is
    apart by the user's own rejection: it never makes an F4 group or an S1
    narrow class, whatever its name collides with."""
    lib = _Library()
    _mainstream(lib)
    lib.add_source("n-a", ["K-~v1:llama 3 1", "K-~v1:q former"] +
                   [f"K-core{k:03d}" for k in range(18)])
    lib.add_source("n-b", ["K-llama31", "K-~v1:qformer"] +
                   [f"K-core{k:03d}" for k in range(20, 37)])
    lib.names.update({"K-~v1:llama 3 1": "Llama 3.1", "K-llama31": "Llama31",
                      "K-~v1:q former": "Q-Former", "K-~v1:qformer": "Qformer"})
    run = _run(lib)
    assert run.keys("F4") == []
    assert run.status("S1")["narrow_groups"] == 0


def test_decisions_survive_a_canonical_id_drift():
    """A canonical id moves when its cluster's minimum member changes; the stored
    decision does not. Same members, new canonical ids: the rejection still
    holds because it is matched through the members' seeds — including a member
    that only a hidden source carries."""
    lib = _Library()
    _mainstream(lib)
    lib.add_source("n-a", ["K-aaa", "K-core000", "K-core001"] + [f"K-core{k:03d}" for k in range(2, 12)])
    lib.add_source("n-b", ["K-bbb"] + [f"K-core{k:03d}" for k in range(20, 32)])
    lib.names.update({"K-aaa": "Llama 3.1", "K-bbb": "Llama31"})
    # The cluster ids drifted ("K-aaa", "K-bbb"); the members' seeds did not.
    lib.member_names = {"K-aaa": ["Llama 3.1", "LLAMA 3.1"],
                        "K-bbb": ["Llama31", "llama31 (hidden knowhow copy)"]}
    lib.decisions = {frozenset(("llama 3 1", "llama31")): "rejected"}
    run = _run(lib)
    assert run.keys("F4") == []
    assert run.status("F4")["groups_decided_apart"] == 1
    lib.decisions = {}
    assert _run(lib).keys("F4") == ["llama31"]


def test_sources_of_is_one_vectorised_lookup(monkeypatch):
    """Two ``searchsorted`` calls for any number of indices — one scalar call per
    index converted the whole sorted array each time (quadratic in a wide group)."""
    lib = _Library()
    for s in range(40):
        lib.add_source(f"s{s:02d}", [f"K-c{(s * 7 + k) % 300:03d}" for k in range(60)],
                       chain=False)
    structure = GraphStructure.from_edges(np.empty(0, np.int32), np.empty(0, np.int32), 0)
    tally = FindingsObjectTally({}, structure)
    for source_id, cid, is_concept in lib.objects:
        tally.add(source_id, cid, is_concept)
    corpus = kg_findings._drain_tally(tally, structure)
    wanted = set(range(corpus.stride))
    calls = []
    real = np.searchsorted

    def spy(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(np, "searchsorted", spy)
    found = kg_findings._sources_of(corpus, wanted)
    assert len(calls) == 2
    monkeypatch.undo()
    expected: Dict[int, List[int]] = {}
    for src, can in zip(corpus.inc_src.tolist(), corpus.inc_can.tolist()):
        expected.setdefault(can, []).append(src)
    assert found == {k: sorted(v) for k, v in expected.items()}


def test_paged_supply_equals_one_supply():
    """``supply`` accumulates within a round; judging happens at the next
    ``pending()``. Feeding a round in three pages must give the same result as
    feeding it at once — F4 groups and S1 examples included."""
    for library in (_merge_library(), _truth_library()):
        once = _run(library)
        paged = _run(library, supply_pages=3)
        assert json.dumps(paged.result.__dict__, sort_keys=True) == json.dumps(
            once.result.__dict__, sort_keys=True)
    assert _run(_merge_library(), supply_pages=3).status("S1")["examples"]


def test_corpus_is_freed_once_compute_returns(monkeypatch):
    """The draft lives through the lookup rounds; the drained corpus (the big
    arrays) must not: no closure may hold it."""
    import gc
    import weakref

    refs = []
    real = kg_findings._drain_tally

    def recording(tally, structure):
        corpus = real(tally, structure)
        refs.append(weakref.ref(corpus))
        return corpus

    monkeypatch.setattr(kg_findings, "_drain_tally", recording)
    lib = _merge_library()
    lib.names["K-llama 3 1"] = "Llama 3.1"
    structure = GraphStructure.from_edges(np.empty(0, np.int32), np.empty(0, np.int32), 0)
    tally = FindingsObjectTally({}, structure)
    for source_id, cid, is_concept in lib.objects:
        tally.add(source_id, cid, is_concept)
    draft = compute_findings(
        tally=tally, structure=structure, can2idx={}, names=lib.names,
        relations_by_source={}, decisions={}, member_seeds=lambda ids: {},
        max_candidate_pairs=1_000)
    gc.collect()
    assert draft.pending() is not None                  # F4 / S1 candidates are alive
    assert refs and refs[0]() is None


def test_s1_examples_look_ahead_instead_of_three_per_round():
    lib = _Library()
    _mainstream(lib)
    concepts = []
    for k in range(15):
        a, b = f"K-x{k} a", f"K-x{k}a"
        lib.names.update({a: f"Xq{k} A", b: f"Xq{k}A"})
        concepts += [a, b]
        if k < 12:                                   # visible names show ONE spelling
            lib.visible.update({a: [f"Xq{k}A"], b: [f"Xq{k}A"]})
    lib.add_source("n", concepts, chain=False)
    run = _run(lib)
    assert run.status("S1")["narrow_groups"] == 15
    assert len(run.status("S1")["examples"]) == 3
    assert len(run.rounds) == 1                       # 15 ≤ the lookahead: one round


def test_member_seeds_are_asked_in_bounded_batches(monkeypatch):
    monkeypatch.setattr(kg_findings, "_MEMBER_SEED_BATCH", 2)
    lib = _merge_library()
    lib.decisions = {frozenset(("unrelated", "other")): "rejected"}   # opens the gate
    run = _run(lib)
    assert [len(batch) for batch in run.seed_calls] == [2, 2, 1]
    assert sorted(cid for batch in run.seed_calls for cid in batch) == sorted(
        ["K-llama 3 1", "K-llama 31", "K-llama31", "K-q former", "K-qformer"])


def test_member_seed_reads_are_skipped_without_an_apart_decision():
    """Member seeds only build the "decided apart" adjacency, so a library with
    no rejected or deferred decision needs none of them — the gate is exact (a
    confirmed decision never keeps a group out of F4 / S1)."""
    lib = _merge_library()
    assert _run(lib).seed_calls == []
    lib.decisions = {frozenset(("llama 3 1", "llama 31")): "confirmed"}
    run = _run(lib)
    assert run.seed_calls == []
    assert run.keys("F4") == ["llama31"]
    lib.decisions = {frozenset(("llama 3 1", "llama 31")): "deferred"}
    assert _run(lib).seed_calls != []


def test_f4_evidence_carries_the_member_seeds_capped_per_canonical(monkeypatch):
    """The read side matches stored decisions by these seeds without reading
    member objects; a canonical with more seeds than the cap keeps the first N
    (sorted) and says so — a missed seed only leaves the group open."""
    monkeypatch.setattr(kg_findings, "MAX_MEMBER_SEEDS_PER_CANONICAL", 2)
    lib = _merge_library()
    lib.member_names = {"K-llama 3 1": ["Llama 3.1", "Llama 3.1 instruct", "llama three one"]}
    row = _run(lib).rows("F4")[0]
    seeds = row["evidence"]["member_seeds"]
    assert set(seeds) == {"K-llama 3 1", "K-llama 31", "K-llama31"}
    assert seeds["K-llama 3 1"] == sorted(set().union(*(
        concept_decision_seeds(n)
        for n in ["Llama 3.1", "Llama 3.1 instruct", "llama three one"])))[:2]
    # both spaces' seeds of every member: a decision of either space names it
    assert seeds["K-llama 31"] == sorted(concept_decision_seeds("Llama 31"))
    assert seeds["K-llama 31"] == ["llama 31", "llama31"]
    assert row["evidence"]["seeds_truncated"] is True


def test_f4_rejudges_on_visible_names_and_promotes_the_next_group(monkeypatch):
    """A canonical name can come from a hidden (knowhow) object. The group is
    re-judged on the visible members' own names; if they no longer collide the
    group is dropped and the next candidate fills the window. The hidden name
    never reaches the payload (§8.4)."""
    monkeypatch.setattr(kg_findings, "MAX_PERSISTED_FINDINGS", 1)
    lib = _Library()
    _mainstream(lib)
    lib.add_source("h-a", ["K-gpt 4 0", "K-gpt40"] + [f"K-core{k:03d}" for k in range(18)])
    lib.add_source("h-b", ["K-gpt 4 0", "K-gpt40", "K-llama 3 1", "K-llama31"] +
                   [f"K-core{k:03d}" for k in range(20, 30)])
    # "GPT_4_0" is the canonical name (written by a hidden knowhow object): it
    # collides with "GPT-4.0" on the squash key, but no visible member carries it.
    lib.names.update({"K-gpt 4 0": "GPT-4.0", "K-gpt40": "GPT_4_0",
                      "K-llama 3 1": "Llama 3.1", "K-llama31": "Llama31"})
    lib.visible = {"K-gpt40": ["Generative Pretrained Forty"]}
    run = _run(lib)
    assert run.keys("F4") == ["llama31"]
    status = run.status("F4")
    assert (status["total"], status["persisted"], status["groups_unreproducible"]) == (1, 1, 1)
    # F4's lookahead asks past the one free slot in the first round, so the
    # promoted group was already answered: no second round.
    assert len(run.rounds) == 1
    dumped = json.dumps(run.result.payload, ensure_ascii=False) + json.dumps(
        run.result.rows, ensure_ascii=False)
    assert "GPT_4_0" not in dumped


def test_a_cap_filled_mid_lookahead_still_reports_the_rest_as_truncated(monkeypatch):
    """codex #941 P2: with the lookahead, one round asks for more groups than the
    free slots. When the cap fills part-way (an earlier group dropped, the next
    kept), the groups after it were asked but never judged — they must still
    count (``total > persisted``, ``truncated``), otherwise the ledger claims the
    list is complete, opens a cross-build comparison, and the skipped groups
    read as "resolved" next time."""
    monkeypatch.setattr(kg_findings, "MAX_PERSISTED_FINDINGS", 1)
    lib = _Library()
    _mainstream(lib)
    lib.add_source("h-a", ["K-gpt 4 0", "K-gpt40"] + [f"K-core{k:03d}" for k in range(18)])
    lib.add_source("h-b", ["K-gpt 4 0", "K-gpt40", "K-llama 3 1", "K-llama31",
                           "K-mistral 7 1", "K-mistral71"] +
                   [f"K-core{k:03d}" for k in range(20, 30)])
    lib.names.update({"K-gpt 4 0": "GPT-4.0", "K-gpt40": "GPT_4_0",
                      "K-llama 3 1": "Llama 3.1", "K-llama31": "Llama31",
                      "K-mistral 7 1": "Mistral 7.1", "K-mistral71": "Mistral71"})
    lib.visible = {"K-gpt40": ["Generative Pretrained Forty"]}   # gpt: unreproducible
    run = _run(lib)
    assert len(run.rounds) == 1                       # all three asked in one round
    assert run.keys("F4") == ["llama31"]              # gpt dropped, llama fills the cap
    status = run.status("F4")
    # mistral was asked, never judged: still found, so the cap is not silent
    assert (status["total"], status["persisted"], status["truncated"]) == (2, 1, True)
    assert status["groups_unreproducible"] == 1


def test_the_s1_window_keeps_its_examples_and_counts_unchanged_at_its_cap():
    """The S1 window shares the class: its counts come from the full narrow-group
    pass, never from the window, and a full window only bounds the examples."""
    lib = _merge_library()
    for k in range(5):     # five more narrow groups (v1 seeds differ, v2 equal)
        lib.add_source(f"s1-{k}", [f"K-x{k} y", f"K-x{k}y"] +
                       [f"K-core{j:03d}" for j in range(40 + 3 * k, 52 + 3 * k)])
        lib.names.update({f"K-x{k} y": f"X{k} Y", f"K-x{k}y": f"X{k}Y"})
    status = _run(lib).status("S1")
    assert status["narrow_groups"] == 7
    assert len(status["examples"]) == 3


# ===================================================================== F5
def test_f5_is_one_notebook_row_with_top_sources():
    lib = _truth_library()
    lib.add_source("z-iso", [f"K-alone{k}" for k in range(7)], chain=False)
    lib.add_source("y-iso", [f"K-lone{k}" for k in range(7)], chain=False)
    run = _run(lib)
    row = run.rows("F5")[0]
    assert row["subject_key"] == NOTEBOOK_SUBJECT_KEY
    assert row["impact_objects"] == 14
    assert [s["source_id"] for s in row["evidence"]["top_sources"]] == ["y-iso", "z-iso"]
    assert row["evidence"]["top_sources"][0]["label"] == "title-y-iso"
    assert [a["kind"] for a in finding_actions(
        "F5", row["subject_key"], row["subject_refs"], row["evidence"])] == [ACTION_RELINK]


def test_in_graph_is_bounded_by_n_nodes_not_by_can2idx():
    """The backfill path extends ``can2idx`` with board members that are no
    longer graph nodes; they must still count as isolated."""
    lib = _Library()
    lib.add_source("a", ["K-x", "K-y"])
    lib.add_source("b", ["K-gone"], chain=False)
    run = _run(lib, backfill_extension=["K-gone"])
    assert run.rows("F5")[0]["impact_objects"] == 1
    assert run.result.payload["structure"]["graph_nodes"] == 2


def test_graph_structure_components_degree_and_giant_tie():
    builder = CanonicalEdgeTableBuilder()
    for a, b in [(0, 1), (2, 3), (1, 0)]:
        builder.add(a, b)
    table = builder.build(4)
    structure = GraphStructure.from_edges(table.src, table.dst, table.n_nodes)
    assert structure.components == 2 and structure.giant_nodes == 2
    assert structure.giant_label == structure.component[0]     # tie → lowest label
    assert structure.degree.tolist() == [1, 1, 1, 1]
    assert structure.in_giant == bytes([1, 1, 0, 0])
    empty = GraphStructure.from_edges(np.empty(0, np.int32), np.empty(0, np.int32), 0)
    assert (empty.n_nodes, empty.components, empty.giant_label) == (0, 0, -1)


def test_tally_is_one_shot():
    structure = GraphStructure.from_edges(np.empty(0, np.int32), np.empty(0, np.int32), 0)
    tally = FindingsObjectTally({}, structure)
    tally.add("s", "K-a", True)
    kwargs = dict(structure=structure, can2idx={}, names={}, relations_by_source={},
                  decisions={}, member_seeds=lambda ids: {}, max_candidate_pairs=10)
    compute_findings(tally=tally, **kwargs)
    with pytest.raises(RuntimeError):
        compute_findings(tally=tally, **kwargs)


def test_result_refuses_while_lookups_are_outstanding():
    lib = _truth_library()
    can2idx: Dict[str, int] = {}
    structure = GraphStructure.from_edges(np.empty(0, np.int32), np.empty(0, np.int32), 0)
    tally = FindingsObjectTally(can2idx, structure)
    for source_id, cid, c in lib.objects:
        tally.add(source_id, cid, c)
    draft = compute_findings(tally=tally, structure=structure, can2idx=can2idx,
                             names=lib.names, relations_by_source={},
                             decisions={}, member_seeds=lambda ids: {}, max_candidate_pairs=10)
    with pytest.raises(RuntimeError):
        draft.result()


# ===================================================================== order
def test_rows_are_ordered_by_severity_impact_then_key():
    lib = _truth_library()
    # Twenty relation-less objects: below half the median relations/object → F3b.
    lib.add_source("z-iso", [f"K-alone{k}" for k in range(20)], chain=False)
    rows = _run(lib).result.rows
    assert [row["rule_id"] for row in rows] == ["F1", "F1", "F1", "F2", "F3", "F5"]
    f1 = [row for row in rows if row["rule_id"] == "F1"]
    assert [row["impact_objects"] for row in f1] == sorted(
        (row["impact_objects"] for row in f1), reverse=True)
    for row in rows:
        assert row["severity"] == contracts.RULE_SEVERITY[row["rule_id"]]
        assert row["subject_kind"] == contracts.RULE_SUBJECT_KIND[row["rule_id"]]


# ===================================================================== finalize
def _old_ledger(payload: dict, seq: int = 7) -> dict:
    return {"kg_mutation_seq": seq, "payload": {**payload, CLUSTER_SEQ_PAYLOAD_KEY: 3},
            "created_at": "2026-10-18T00:00:00Z"}


def test_finalize_without_previous_build():
    run = _run(_truth_library())
    rows, payload = finalize_findings(run.result, [], None, kg_seq=9, cluster_seq=4)
    assert all(r["first_seen_kg_seq"] == 9 and r["first_seen_cluster_seq"] == 4 for r in rows)
    comparison = payload["comparison"]
    assert comparison["comparable"] is False and comparison["reason"] == "no_previous"
    check_findings_comparison(comparison)


def test_finalize_compares_by_subject_key_and_inherits_first_seen():
    lib = _truth_library()
    old_run = _run(lib)
    old_rows, old_payload = finalize_findings(old_run.result, [], None, kg_seq=7, cluster_seq=3)
    lib = lib.drop_source("x-island")                                  # handled
    lib.add_source("x-new", [f"K-fresh{k:02d}" for k in range(15)])    # new
    new_run = _run(lib)
    rows, payload = finalize_findings(new_run.result, old_rows, _old_ledger(old_payload),
                                      kg_seq=9, cluster_seq=4)
    by_key = {(r["rule_id"], r["subject_key"]): r for r in rows}
    assert by_key[("F1", "w-a")]["first_seen_kg_seq"] == 7            # persisting
    assert by_key[("F1", "x-new")]["first_seen_kg_seq"] == 9          # new
    comparison = payload["comparison"]
    assert comparison["comparable"] is True
    assert (comparison["resolved"], comparison["new"]) == (1, 1)
    assert comparison["persisting"] == len(old_rows) - 1
    assert comparison["previous_built_at_seq"] == 7
    assert comparison["previous_built_at_cluster_seq"] == 3
    assert [s["subject_key"] for s in comparison["resolved_samples"]] == ["x-island"]
    assert comparison["resolved_samples"][0]["subject_label"] == "title-x-island"
    check_findings_comparison(comparison)


def test_finalize_refuses_to_compare_across_rule_versions():
    run = _run(_truth_library())
    old_rows, old_payload = finalize_findings(run.result, [], None, kg_seq=7, cluster_seq=3)
    old_payload = {**old_payload, "rules_version": RULES_VERSION - 1}
    rows, payload = finalize_findings(run.result, old_rows, _old_ledger(old_payload),
                                      kg_seq=9, cluster_seq=4)
    assert payload["comparison"]["comparable"] is False
    assert payload["comparison"]["reason"] == "rules_version_changed"
    assert payload["comparison"]["resolved"] is None
    assert all(r["first_seen_kg_seq"] == 9 for r in rows)            # not inherited


def test_finalize_refuses_to_compare_a_truncated_rule(monkeypatch):
    run = _run(_truth_library())
    old_rows, old_payload = finalize_findings(run.result, [], None, kg_seq=7, cluster_seq=3)
    monkeypatch.setattr(kg_findings, "MAX_PERSISTED_FINDINGS", 1)
    truncated = _run(_truth_library())
    rows, payload = finalize_findings(truncated.result, old_rows, _old_ledger(old_payload),
                                      kg_seq=9, cluster_seq=4)
    comparison = payload["comparison"]
    assert comparison["comparable"] is False
    assert comparison["by_rule"]["F1"] == {"comparable": False}
    assert comparison["by_rule"]["F5"]["comparable"] is True
    assert comparison["resolved"] is None                 # no headline over a subset
    # …and the other direction: the OLD side truncated.
    rows, payload = finalize_findings(run.result, rows, _old_ledger(payload),
                                      kg_seq=10, cluster_seq=4)
    assert payload["comparison"]["by_rule"]["F1"] == {"comparable": False}
    check_findings_comparison(payload["comparison"])


def test_rules_skipped_on_both_sides_compare_as_zero():
    """A single-source library skips F1/F2 on every build; that must not make the
    headline comparison null forever. Ran on one side only stays incomparable."""
    single = _Library()
    single.add_source("only", [f"K-c{k}" for k in range(20)])
    first = _run(single)
    old_rows, old_payload = finalize_findings(first.result, [], None, kg_seq=1, cluster_seq=1)
    _rows, payload = finalize_findings(_run(single).result, old_rows,
                                       _old_ledger(old_payload), kg_seq=2, cluster_seq=1)
    comparison = payload["comparison"]
    assert comparison["by_rule"]["F1"] == {"comparable": True, "resolved": 0,
                                           "persisting": 0, "new": 0}
    assert comparison["comparable"] is True
    assert (comparison["resolved"], comparison["persisting"], comparison["new"]) == (0, 0, 0)
    check_findings_comparison(comparison)
    grown = single.drop_source("none")
    grown.add_source("second", [f"K-d{k}" for k in range(20)])
    _rows, payload = finalize_findings(_run(grown).result, old_rows,
                                       _old_ledger(old_payload), kg_seq=3, cluster_seq=1)
    assert payload["comparison"]["by_rule"]["F1"] == {"comparable": False}
    assert payload["comparison"]["comparable"] is False


def test_resolved_samples_are_capped(monkeypatch):
    monkeypatch.setattr(kg_findings, "MAX_RESOLVED_SAMPLES", 2)
    run = _run(_truth_library())
    old_rows, old_payload = finalize_findings(run.result, [], None, kg_seq=7, cluster_seq=3)
    lib = _Library()
    _mainstream(lib)
    _rows, payload = finalize_findings(_run(lib).result, old_rows, _old_ledger(old_payload),
                                       kg_seq=9, cluster_seq=4)
    assert payload["comparison"]["resolved"] == 4
    assert [s["subject_key"] for s in payload["comparison"]["resolved_samples"]] == ["w-a", "x-bridged"]


# ===================================================================== contracts
def _numeric_leaves(node, out: Set[str], name: Optional[str] = None) -> Set[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            _numeric_leaves(value, out, key)
    elif isinstance(node, list):
        for value in node:
            _numeric_leaves(value, out, name)
    elif isinstance(node, (int, float)) and not isinstance(node, bool) and name:
        out.add(name)
    return out


def _all_rules_run() -> Tuple[List[dict], dict]:
    lib = _truth_library()
    merge = _merge_library()
    for obj in merge.objects:
        if obj[0] in ("n-a", "n-b"):
            lib.objects.append(obj)
    lib.relations.extend(r for r in merge.relations if r[0] in ("n-a", "n-b"))
    lib.names.update(merge.names)
    frag = _parse_damage_library()
    lib.objects.extend(o for o in frag.objects if o[0] == "frag")
    lib.relations.extend(r for r in frag.relations if r[0] == "frag")
    lib.names.update({k: v for k, v in frag.names.items() if k.startswith("K-broken")})
    lib.add_source("thin", [f"K-core{k:03d}" for k in range(100, 120)], chain=False,
                   extra_relations=[("K-core100", "K-core101")])
    lib.add_source("z-iso", [f"K-alone{k}" for k in range(3)], chain=False)
    run = _run(lib)
    old_rows, old_payload = finalize_findings(run.result, [], None, kg_seq=7, cluster_seq=3)
    return finalize_findings(run.result, old_rows, _old_ledger(old_payload),
                             kg_seq=8, cluster_seq=3)


def test_every_numeric_leaf_has_a_unit():
    rows, payload = _all_rules_run()
    assert {row["rule_id"] for row in rows} == {"F1", "F2", "F3", "F4", "F5"}
    missing = _numeric_leaves(payload, set()) - set(FINDINGS_PAYLOAD_UNITS)
    assert missing == set(), f"findings 账本的这些数值字段没有单位:{sorted(missing)}"
    for row in rows:
        missing = _numeric_leaves(row["evidence"], set()) - set(
            FINDING_EVIDENCE_UNITS[row["rule_id"]])
        assert missing == set(), f"{row['rule_id']} 证据的这些数值字段没有单位:{sorted(missing)}"
    check_findings_ledger(payload, rows)


def test_one_field_name_one_unit_across_payload_and_evidence():
    seen: Dict[str, str] = dict(FINDINGS_PAYLOAD_UNITS)
    for rule, units in FINDING_EVIDENCE_UNITS.items():
        for name, unit in units.items():
            assert seen.setdefault(name, unit) == unit, (rule, name)


def test_shared_unit_codes_match_the_kg_analysis_vocabulary():
    from app.services import kg_analysis

    assert contracts.SHARED_KG_ANALYSIS_UNITS == (
        kg_analysis.UNIT_OBJECTS, kg_analysis.UNIT_CANONICAL, kg_analysis.UNIT_SOURCES,
        kg_analysis.UNIT_RATIO, kg_analysis.UNIT_SEQ,
    )


def test_thresholds_and_version_travel_in_the_payload():
    run = _run(_truth_library())
    assert run.result.payload["rules_version"] == RULES_VERSION
    assert run.result.payload["thresholds"] == contracts.RULE_THRESHOLDS
    assert run.result.payload["thresholds"]["f1_shared_max"] == contracts.F1_SHARED_MAX


def test_check_findings_ledger_rejects_mismatches():
    rows, payload = _all_rules_run()
    with pytest.raises(ValueError, match="persisted"):
        check_findings_ledger(payload, rows[1:])
    with pytest.raises(ValueError, match="主键重复"):
        check_findings_ledger(payload, rows + rows[:1])
    broken = json.loads(json.dumps(payload))
    broken["rules"]["F5"]["total"] = 5
    with pytest.raises(ValueError, match="truncated"):
        check_findings_ledger(broken, rows)
    del broken["rules"]["S1"]
    with pytest.raises(ValueError, match="缺少规则状态"):
        check_findings_ledger(broken, rows)
    with pytest.raises(ValueError, match="rules_version"):
        check_findings_ledger({**payload, "rules_version": True}, rows)
    wrong = [dict(rows[0], severity="low")] + rows[1:]
    with pytest.raises(ValueError, match="严重度"):
        check_findings_ledger(payload, wrong)


def test_ledger_contract_stamp():
    payloads = {"a": {"x": 1}, "b": {}}
    stamped = stamp_ledger_contract(payloads)
    assert all(p[LEDGER_CONTRACT_KEY] == LEDGER_CONTRACT_VERSION for p in stamped.values())
    assert payloads["a"] == {"x": 1}                          # input untouched
    check_ledger_contract(stamped)
    with pytest.raises(ValueError):
        check_ledger_contract({**stamped, "b": {}})
    with pytest.raises(ValueError):
        check_ledger_contract({"a": {LEDGER_CONTRACT_KEY: True}})
    assert ledger_row_predates_findings({"x": 1}) is True
    assert ledger_row_predates_findings(stamped["a"]) is False


# ------------------------------------------------------- dismissal lifecycle
def _dismissed(rule_id, key):
    return {"rule_id": rule_id, "subject_key": key, "kind": "dismissed"}


_RAN = {"status": "ran", "total": 1, "persisted": 1, "truncated": False}
_ALL_RAN = {rule: dict(_RAN) for rule in ("F1", "F2", "F3", "F4", "F5")}
_EXISTS = {"exists": True}


def test_a_dismissal_goes_only_when_its_subject_source_is_gone():
    """§8.3: pruned only when the subject no longer EXISTS — F1/F3 their source,
    F2 either source — never just because the subject left the detail."""
    marks = [_dismissed("F1", "s-del"), _dismissed("F1", "s-kept"),
             _dismissed("F3", "s-del"), _dismissed("F2", "s-kept|s-del"),
             _dismissed("F2", "s-kept|s-kept2"),
             {"rule_id": "F5", "subject_key": "notebook", "kind": "relink_attempted"}]
    states = {"s-kept": _EXISTS, "s-kept2": _EXISTS, "s-del": {"exists": False}}
    assert kg_findings.dismissal_source_ids(marks) == ["s-del", "s-kept", "s-kept2"]
    assert kg_findings.stale_dismissals(marks, _ALL_RAN, states) == [
        ("F1", "s-del", "dismissed"), ("F3", "s-del", "dismissed"),
        ("F2", "s-kept|s-del", "dismissed")]


def test_a_dismissal_survives_a_subject_that_merely_left_the_detail():
    """Failure modes of "not in the new detail ⇒ prune" (each was silently
    dropping the user's judgement): the KG deleted and not re-extracted yet,
    the source caught mid-reparse — the source row is still there, so the mark
    stays even though no row reports the subject."""
    marks = [_dismissed("F1", "s-a"), _dismissed("F3", "s-b")]
    states = {"s-a": _EXISTS, "s-b": {"exists": True, "status": "parsing"}}
    assert kg_findings.stale_dismissals(marks, _ALL_RAN, states) == []


def test_a_truncated_or_skipped_rule_prunes_nothing():
    """A rule cut at its cap or skipped (fewer than two comparable sources) sees
    only part of the library: even a deleted source keeps its dismissal."""
    marks = [_dismissed("F1", "s-del"), _dismissed("F2", "s-del|s-x")]
    states = {"s-del": {"exists": False}, "s-x": _EXISTS}
    truncated = dict(_ALL_RAN, F1={**_RAN, "truncated": True},
                     F2={"status": "skipped", "reason": "fewer_than_two_sources", "total": 0})
    assert kg_findings.stale_dismissals(marks, truncated, states) == []
    assert kg_findings.stale_dismissals(marks, {}, states) == []


def test_f4_and_f5_dismissals_are_never_pruned_at_publication():
    marks = [_dismissed("F4", "llama31"), _dismissed("F5", "notebook")]
    assert kg_findings.dismissal_source_ids(marks) == []
    assert kg_findings.stale_dismissals(marks, _ALL_RAN, {}) == []


def test_a_malformed_pair_key_is_unknown_not_gone():
    from app.domain.kg_findings_contracts import source_pair_ids

    assert source_pair_ids("a|b") == ("a", "b")
    assert source_pair_ids("a") == () and source_pair_ids("a|") == ()
    assert kg_findings.stale_dismissals([_dismissed("F2", "broken")], _ALL_RAN, {}) == []


def test_findings_kind_is_registered_with_its_producer():
    """Registered in the same commit as the producer (`_compute_kg_analysis`
    + the publish step): a required artifact that reads ``concept_clusters``
    (cluster-dependent) but NOT the board partition — a board recast does not
    void it, and the backfill path stamps it with an integer merge generation."""
    from app.domain.kg_analysis_contracts import (
        BOARD_DEPENDENT_ARTIFACT_KINDS,
        CLUSTER_DEPENDENT_ARTIFACT_KINDS,
        OPTIONAL_ARTIFACT_KINDS,
        REQUIRED_ARTIFACT_KINDS,
    )
    from app.services.kg_analysis_precompute import stamp_cluster_seq

    assert ARTIFACT_FINDINGS == "findings"
    assert ARTIFACT_FINDINGS in ARTIFACT_KINDS
    assert ARTIFACT_FINDINGS in REQUIRED_ARTIFACT_KINDS
    assert ARTIFACT_FINDINGS not in OPTIONAL_ARTIFACT_KINDS
    assert ARTIFACT_FINDINGS in CLUSTER_DEPENDENT_ARTIFACT_KINDS
    assert ARTIFACT_FINDINGS not in BOARD_DEPENDENT_ARTIFACT_KINDS
    stamped = stamp_cluster_seq({ARTIFACT_FINDINGS: {}}, 7, partition_rebuilt=False)
    assert stamped[ARTIFACT_FINDINGS][CLUSTER_SEQ_PAYLOAD_KEY] == 7


# ===================================================================== settings
def test_candidate_pair_budget_is_a_validated_setting(monkeypatch):
    from pydantic import ValidationError

    from app.core.config import Settings

    assert Settings.model_fields["kg_findings_max_candidate_pairs"].default == 5_000_000
    monkeypatch.setenv("KG_FINDINGS_MAX_CANDIDATE_PAIRS", "1234")
    assert Settings().kg_findings_max_candidate_pairs == 1234
    for bad in ("0", "100000001"):
        monkeypatch.setenv("KG_FINDINGS_MAX_CANDIDATE_PAIRS", bad)
        with pytest.raises(ValidationError):
            Settings()


def test_candidate_pair_budget_is_registered_in_both_deployment_references():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for name in ("docs/deployment-and-configuration.md",
                 "docs/deployment-and-configuration_zh.md"):
        text = (root / name).read_text(encoding="utf-8")
        assert "KG_FINDINGS_MAX_CANDIDATE_PAIRS" in text, name
