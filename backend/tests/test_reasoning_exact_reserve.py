"""Reasoning synthesis assembly: the bounded prefix seat for exact-channel chunks.

The exact-identifier channel fetches a manual section whole (main description /
Arguments / Examples). In chunk mode `exact_section_reserve` buys those chunks a
seat in the *final selection*, measured in tokens. Reasoning never goes through
that selector: its hits are merged into `state.chunks` and consumed by
`_answer_reasoning` in relevance order against a *character* budget. Two things
then go wrong without a seat here:

1. tied 1.0 relevance — PPR and lexical lanes also emit 1.0 chunks, and Python's
   stable sort keeps them ahead of the exact hits by insertion order, so one
   good PPR harvest pushes the whole command table past the budget;
2. the cut lands mid-section — the main description makes it into the prompt and
   the Arguments table does not, which is the exact failure the channel exists
   to prevent.

These tests pin the seat (`REASONING_EXACT_RESERVE`) and, just as hard, its
laziness: no identifier, or reserve 0, and the assembled prompt is byte-for-byte
what it was before the feature existed.
"""
from __future__ import annotations

import json

import pytest

from app.domain.retrieval import RetrievedChunk
from app.models.schemas import NotebookCreate
from app.services.ask_service import AskService
from app.services.retrieval import promote_bounded_prefix
from tests.model_testkit import bind_chat_client
from tests.test_exact_lookup import (  # noqa: F401  (repo fixture)
    BREADCRUMB_MANUAL,
    SET_DB_CHUNKS,
    _filler,
    _seed_manual,
    repo,
)

IDENTIFIER_QUESTION = "set_db 命令是怎样的"
NEUTRAL_QUESTION = "这个工具的背景是什么"


class _Echo:
    """A configured answer client that cites nothing.

    These tests read the assembled context, not the answer; an anchor-free body
    keeps `_parse_answer_anchors` out of the picture entirely.
    """

    configured = True

    def chat_json(self, messages, schema_hint, **kwargs):
        return json.dumps({"answer": "结论。", "grounded": False})


def _tied_filler(index):
    """`_filler` with relevance pinned to 1.0 — the adversarial tie.

    The exact channel scores its section 1.0, so 0.9 fillers would sort *below*
    it and the seat would never be exercised. Equal scores are the real-world
    case (PPR normalises its top hit to 1.0 too) and the one where insertion
    order, not merit, decides who makes the budget.
    """
    chunk = _filler(index)
    chunk.relevance = 1.0
    chunk.score = 1.0
    return chunk


_REAL_CHUNK_ANSWER_CONTEXT = AskService._chunk_answer_context


def _spy_assembly(monkeypatch):
    """Capture the chunk list `_answer_reasoning` fed to the context renderer,
    plus what came back.

    Delegates to the module-level original rather than to whatever is currently
    bound on the class: a spy installed while an earlier spy is still in place
    would otherwise chain into it and overwrite that run's capture, quietly
    turning any two-run comparison into a tautology.
    """
    captured: dict = {}

    def _spy(self, chunks, budget_chars=None, notebook_id="", id_offset=0):
        block, id_map = _REAL_CHUNK_ANSWER_CONTEXT(
            self, chunks, budget_chars=budget_chars,
            notebook_id=notebook_id, id_offset=id_offset,
        )
        captured["chunks"] = [chunk.chunk_id for chunk in chunks]
        captured["block"] = block
        captured["id_map"] = dict(id_map)
        captured["budget"] = budget_chars
        return block, id_map

    monkeypatch.setattr(AskService, "_chunk_answer_context", _spy)
    return captured


# A filler line is ~270 chars; three of them fill this and the 37/42/28-char
# set_db chunks arrive after. Deliberately a number that truncates: every test
# below asserts the truncation really happened, so an over-generous budget
# fails loudly instead of passing vacuously.
_CHUNK_CHARS = 850


def _run(repo, notebook_id, question, monkeypatch, *, filler_count=8):
    """Retrieve for `question`, merge the exact hits behind tied fillers, and
    assemble the reasoning prompt. Returns the spy capture.

    Merge order mirrors the retriever's: seed lanes first, exact-channel hits
    appended last (`ReasoningRetriever` appends each `_action_exact_lookup`
    result), which is what makes the tie adversarial.
    """
    bind_chat_client(repo, "ask_answer", _Echo())
    hits = repo.retrieval.exact_lookup_chunks(notebook_id, question)
    chunks = [_tied_filler(i) for i in range(filler_count)] + list(hits)
    # The spy lives only for the one assembly call: a test that runs `_run`
    # twice must get two independent captures.
    with monkeypatch.context() as patch:
        captured = _spy_assembly(patch)
        repo._runtime.ask_service()._answer_reasoning(
            notebook_id, question, [], [], chunks=chunks,
            chunk_context_chars=_CHUNK_CHARS,
        )
    captured["exact_ids"] = [chunk.chunk_id for chunk in hits]
    return captured


def _object_ids(id_map):
    return {entry["object_id"] for entry in id_map.values()}


# ------------------------------------------------------------------ the point
def test_whole_command_section_survives_the_character_budget(repo, monkeypatch):
    """The end the seat exists for: main description + Arguments + Examples all
    reach the synthesis prompt even though tied fillers were ordered ahead."""
    notebook = _seed_manual(repo, BREADCRUMB_MANUAL)
    captured = _run(repo, notebook.id, IDENTIFIER_QUESTION, monkeypatch)

    assert SET_DB_CHUNKS <= set(captured["chunks"]), (
        "the exact hits must reach the assembler at all")
    assert len(captured["id_map"]) < len(captured["chunks"]), (
        "the character budget must actually truncate, or this proves nothing")
    assert SET_DB_CHUNKS <= _object_ids(captured["id_map"]), (
        f"whole set_db section must survive truncation, got "
        f"{sorted(_object_ids(captured['id_map']))}")


def test_without_the_seat_the_section_is_cut(repo, monkeypatch):
    """The control: reserve 0 on the same fixture loses the section.

    Without this, the test above could be passing because the budget was roomy
    rather than because the seat works.
    """
    notebook = _seed_manual(repo, BREADCRUMB_MANUAL)
    monkeypatch.setattr(repo.settings, "reasoning_exact_reserve", 0)
    captured = _run(repo, notebook.id, IDENTIFIER_QUESTION, monkeypatch)

    assert SET_DB_CHUNKS <= set(captured["chunks"])
    assert not (SET_DB_CHUNKS & _object_ids(captured["id_map"])), (
        "reserve 0 must leave the pre-feature outcome in place")


# ------------------------------------------------------------- bounded, lazy
# A six-chunk set_db section: MORE exact hits than the reserve, which is what
# makes the clamp observable at all. On the three-chunk BREADCRUMB_MANUAL
# "promote at most 4" and "promote everything held" produce identical orders.
WIDE_MANUAL = [
    ("ck-main", "Manual > Commands > set_db",
     "[Commands > set_db] set_db 用于设置数据库属性。"),
    ("ck-args", "Manual > Commands > set_db > Arguments",
     "[set_db > Arguments] -name 属性名。-value 属性值。"),
    ("ck-examples", "Manual > Commands > set_db > Examples",
     "[set_db > Examples] 见下方脚本片段。"),
    ("ck-options", "Manual > Commands > set_db > Options",
     "[set_db > Options] -force 覆盖既有属性。"),
    ("ck-errors", "Manual > Commands > set_db > Errors",
     "[set_db > Errors] 属性名不存在时报错。"),
    ("ck-notes", "Manual > Commands > set_db > Notes",
     "[set_db > Notes] 只在设计载入后可用。"),
    ("ck-other", "Manual > Commands > report_timing",
     "[Commands > report_timing] report_timing 输出时序报告。"),
]
_WIDE_RESERVE = 4


def test_the_seat_is_clamped_and_never_grows_the_budget(repo, monkeypatch):
    """Promotion reorders within a bound; it does not buy room.

    Two claims, both measured on a section with six exact hits and a reserve of
    four: the prefix stops at the reserve (hit five stays an ordinary candidate
    competing on relevance), and the rendered block still fits the character
    budget it fitted without the seat.
    """
    notebook = _seed_manual(repo, WIDE_MANUAL)

    def _with_reserve(reserve):
        with monkeypatch.context() as patch:
            patch.setattr(repo.settings, "reasoning_exact_reserve", reserve)
            return _run(repo, notebook.id, IDENTIFIER_QUESTION, patch)

    on = _with_reserve(_WIDE_RESERVE)
    off = _with_reserve(0)

    exact = on["exact_ids"]
    assert len(exact) > _WIDE_RESERVE, (
        f"fixture must out-supply the reserve, got {exact}")
    assert on["chunks"][:_WIDE_RESERVE] == exact[:_WIDE_RESERVE], (
        "the first `reserve` exact hits take the prefix, in their own order")
    assert on["chunks"][_WIDE_RESERVE] not in set(exact), (
        "past the reserve an exact hit is an ordinary candidate again, not "
        f"part of the prefix: {on['chunks']}")

    assert len(on["block"]) <= _CHUNK_CHARS
    assert len(off["block"]) <= _CHUNK_CHARS
    assert set(on["chunks"]) == set(off["chunks"]), (
        "promotion is a permutation — no candidate may appear or vanish")
    assert len(on["chunks"]) == len(off["chunks"])
    # Stability, end to end: everything the prefix did not take keeps exactly
    # the order the reserve-0 run gave it. Without this, a promotion that
    # reshuffled the tail would still pass every assertion above while quietly
    # changing which ordinary chunks make the budget.
    promoted = set(on["chunks"][:_WIDE_RESERVE])
    assert [cid for cid in on["chunks"] if cid not in promoted] == [
        cid for cid in off["chunks"] if cid not in promoted]


def test_reserve_zero_is_byte_for_byte_the_pre_feature_assembly(repo, monkeypatch):
    """`REASONING_EXACT_RESERVE=0` is inert: identical to the same run with the
    provenance marker never set (i.e. the code as it stood before T3)."""
    notebook = _seed_manual(repo, BREADCRUMB_MANUAL)

    def _assemble(reserve, *, unmarked):
        bind_chat_client(repo, "ask_answer", _Echo())
        hits = repo.retrieval.exact_lookup_chunks(notebook.id, IDENTIFIER_QUESTION)
        assert hits, "fixture must produce exact hits"
        if unmarked:
            for chunk in hits:
                chunk.exact_lookup = False
        chunks = [_tied_filler(i) for i in range(8)] + list(hits)
        with monkeypatch.context() as patch:
            patch.setattr(repo.settings, "reasoning_exact_reserve", reserve)
            captured = _spy_assembly(patch)
            repo._runtime.ask_service()._answer_reasoning(
                notebook.id, IDENTIFIER_QUESTION, [], [], chunks=chunks,
                chunk_context_chars=_CHUNK_CHARS,
            )
        return captured

    lazy = _assemble(0, unmarked=False)
    pre_feature = _assemble(4, unmarked=True)

    assert lazy["block"] == pre_feature["block"]
    assert list(lazy["id_map"]) == list(pre_feature["id_map"])
    assert lazy["chunks"] == pre_feature["chunks"]


def test_a_question_without_an_identifier_is_bit_for_bit_neutral(repo, monkeypatch):
    """No identifier → nothing is marked → the assembled prompt is exactly what
    it is with the whole exact channel switched off."""
    notebook = _seed_manual(repo, BREADCRUMB_MANUAL)

    def _neutral(enabled):
        with monkeypatch.context() as patch:
            patch.setattr(repo.settings, "exact_lookup_enabled", enabled)
            return _run(repo, notebook.id, NEUTRAL_QUESTION, patch)

    on = _neutral(True)
    off = _neutral(False)

    assert on["exact_ids"] == [] and off["exact_ids"] == []
    assert on["block"] == off["block"]
    assert list(on["id_map"]) == list(off["id_map"])
    assert on["chunks"] == off["chunks"]


def test_the_neutral_comparison_is_not_a_dead_harness(repo, monkeypatch):
    """The liveness counterpart to the neutrality test, kept separate on purpose.

    Neutrality must hold for reasons that have nothing to do with the seat
    working — fold this assertion into that test and deleting the provenance
    marker would turn the NEUTRALITY test red, which says the opposite of what
    it means. Here, red is the correct reading: the harness stopped
    distinguishing an identifier question from an identifier-free one.
    """
    notebook = _seed_manual(repo, BREADCRUMB_MANUAL)
    neutral = _run(repo, notebook.id, NEUTRAL_QUESTION, monkeypatch)
    identified = _run(repo, notebook.id, IDENTIFIER_QUESTION, monkeypatch)
    assert identified["block"] != neutral["block"]


# ------------------------------------- PR-B fix A: the injected copy is unmarked
#
# `outline_synthesis.plan_outline_sections` injects an unbound exact hit into
# every surviving section as a copy with `exact_lookup=False` (not the marked
# original). This is the assembly-level motive for that choice: a *marked*
# injected chunk rides `promote_bounded_prefix`'s prefix seat ahead of chunks
# the model itself bound to the section, at their expense. Deliberately built
# straight from `RetrievedChunk` rather than through `plan_outline_sections` —
# this test pins what `_answer_reasoning` does with the two shapes, not how
# outline_synthesis produces them (that is `test_reasoning_outline_synthesis`'s
# job).

_MARKER_FREE_BUDGET = 49  # two full 24-char lines fit, a third does not — see below


def _labeled_chunk(chunk_id, label, marked):
    """20-char text, same length regardless of label, so every candidate
    costs the assembler exactly the same number of characters — the budget
    math below (line = 4-char prefix + 20-char text = 24 chars; two lines +
    a separator = 49) depends on that."""
    text = f"{label}{'x' * (20 - len(label))}"
    assert len(text) == 20
    return RetrievedChunk(
        chunk_id=chunk_id, source_id="s1", source_title="t",
        section_path="1", text=text, relevance=1.0, exact_lookup=marked,
    )


def test_an_unmarked_injected_copy_does_not_outrank_the_bound_chunks(
    repo, monkeypatch
):
    """Fix: bound1, bound2 (unmarked) and an unmarked injected copy, all tied
    at relevance 1.0. A 49-char budget fits exactly the first two full lines
    (see `_labeled_chunk`'s docstring). With no marker on the injected chunk,
    `promote_bounded_prefix` has nothing to promote, so the stable sort keeps
    insertion order and the budget goes to the two chunks the model actually
    bound — exactly the outcome fix A exists to guarantee.
    """
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    bind_chat_client(repo, "ask_answer", _Echo())
    bound1 = _labeled_chunk("bound1", "bound1", False)
    bound2 = _labeled_chunk("bound2", "bound2", False)
    injected = _labeled_chunk("inject", "inject", False)

    with monkeypatch.context() as patch:
        captured = _spy_assembly(patch)
        repo._runtime.ask_service()._answer_reasoning(
            notebook.id, IDENTIFIER_QUESTION, [], [],
            chunks=[bound1, bound2, injected],
            chunk_context_chars=_MARKER_FREE_BUDGET,
        )

    assert _object_ids(captured["id_map"]) == {"bound1", "bound2"}, (
        f"the marker-free copy must lose the budget to the bound chunks, "
        f"got {sorted(_object_ids(captured['id_map']))}")


def test_mutation_a_marked_injected_copy_would_evict_a_bound_chunk(
    repo, monkeypatch
):
    """Mutation control for the test above: same three chunks, but the
    injected one keeps its original `exact_lookup=True` marker (as if fix A
    had not stripped it). It now rides the prefix seat ahead of `bound2`,
    which is exactly the regression fix A exists to prevent — this must be
    red before the fix and is confirmed red here by construction, pinning
    that the assertion above is not vacuously true for any three tied chunks.
    """
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    bind_chat_client(repo, "ask_answer", _Echo())
    bound1 = _labeled_chunk("bound1", "bound1", False)
    bound2 = _labeled_chunk("bound2", "bound2", False)
    injected_marked = _labeled_chunk("inject", "inject", True)

    with monkeypatch.context() as patch:
        captured = _spy_assembly(patch)
        repo._runtime.ask_service()._answer_reasoning(
            notebook.id, IDENTIFIER_QUESTION, [], [],
            chunks=[bound1, bound2, injected_marked],
            chunk_context_chars=_MARKER_FREE_BUDGET,
        )

    ids = _object_ids(captured["id_map"])
    assert "inject" in ids, "a marked chunk takes the prefix seat"
    assert ids != {"bound1", "bound2"}, (
        "and by taking it, evicts one of the two chunks the model bound")


# --------------------------- P3-2: sort-then-promote, never promote-then-sort


def test_the_seat_promotes_a_low_relevance_marked_chunk_ahead_of_a_tied_filler(
    repo, monkeypatch
):
    """Pins the ORDER of operations, not just the outcome: `_answer_reasoning`
    sorts by relevance descending first, then promotes marked chunks into a
    bounded prefix — never the reverse. Every fixture above this ties marked
    and unmarked candidates at relevance 1.0 (codex review: that leaves the
    order of the two steps unpinned — swapping them would still pass every
    one of those). Here the marked chunk scores *below* the filler it must
    still precede, which only a sort-then-promote pipeline produces:
    promote-then-sort would let the final relevance sort push the marked
    chunk back behind the filler, undoing the promotion it just did.
    """
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    bind_chat_client(repo, "ask_answer", _Echo())
    filler = RetrievedChunk(
        chunk_id="filler", source_id="s1", source_title="t",
        section_path="1", text="filler text", relevance=1.0,
    )
    marked = RetrievedChunk(
        chunk_id="marked", source_id="s1", source_title="t",
        section_path="1", text="marked text", relevance=0.3,
        exact_lookup=True,
    )

    with monkeypatch.context() as patch:
        captured = _spy_assembly(patch)
        repo._runtime.ask_service()._answer_reasoning(
            notebook.id, IDENTIFIER_QUESTION, [], [],
            chunks=[filler, marked],
        )

    assert captured["chunks"][0] == "marked", (
        f"a marked chunk must lead the prefix even when it scores below the "
        f"filler it precedes — got {captured['chunks']}; ['filler', "
        f"'marked'] means promotion ran before the relevance sort and was "
        f"undone by it")


# ------------------------------------------------------- the pure function
class _Chunk:
    def __init__(self, chunk_id, marked=False):
        self.chunk_id = chunk_id
        self.exact_lookup = marked

    def __repr__(self):  # pragma: no cover - failure output only
        return self.chunk_id


def _marked(chunk):
    return chunk.exact_lookup


def test_promote_bounded_prefix_is_stable_within_both_groups():
    chunks = [
        _Chunk("a"), _Chunk("x", True), _Chunk("b"), _Chunk("y", True),
        _Chunk("c"),
    ]
    out = promote_bounded_prefix(chunks, _marked, 4)
    assert [chunk.chunk_id for chunk in out] == ["x", "y", "a", "b", "c"]


def test_promote_bounded_prefix_clamps_to_the_reserve():
    chunks = [_Chunk(f"m{i}", True) for i in range(5)] + [_Chunk("tail")]
    out = promote_bounded_prefix(chunks, _marked, 2)
    # Beyond the quota a held chunk is an ordinary candidate: it keeps its own
    # position relative to the rest instead of riding along with the prefix.
    assert [chunk.chunk_id for chunk in out] == [
        "m0", "m1", "m2", "m3", "m4", "tail"]
    chunks = [_Chunk("head"), _Chunk("m0", True), _Chunk("m1", True)]
    assert [chunk.chunk_id for chunk in promote_bounded_prefix(chunks, _marked, 1)] == [
        "m0", "head", "m1"]


@pytest.mark.parametrize("reserve", [0, -1, 4])
def test_promote_bounded_prefix_without_a_match_keeps_the_order(reserve):
    chunks = [_Chunk("a"), _Chunk("b"), _Chunk("c")]
    out = promote_bounded_prefix(chunks, _marked, reserve)
    assert [chunk.chunk_id for chunk in out] == ["a", "b", "c"]
    assert out is not chunks, "callers must get their own list"


def test_promote_bounded_prefix_reserve_zero_ignores_every_match():
    chunks = [_Chunk("a"), _Chunk("m", True), _Chunk("b")]
    out = promote_bounded_prefix(chunks, _marked, 0)
    assert [chunk.chunk_id for chunk in out] == ["a", "m", "b"]
