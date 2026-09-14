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

from app.domain.retrieval import RetrievedChunk, RetrievedElement
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
        call = {
            "chunks": [chunk.chunk_id for chunk in chunks],
            "block": block,
            "id_map": dict(id_map),
            "budget": budget_chars,
            "id_offset": id_offset,
        }
        # 一次装配可以调它两次:本节绑定的 chunk 段一次,尾随的注入段一次
        # (`trailing_chunks`)。`calls` 按调用序保留两者;顶层键仍是最后一次,
        # 供只有一段的既有用例照旧读取。
        captured.setdefault("calls", []).append(call)
        captured.update(call)
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


# ---------------------------- PR-B fix A: the injected copy is a TRAILING block
#
# `outline_synthesis.plan_outline_sections` puts an unbound exact hit into every
# surviving section's `exact_chunks` as a copy with `exact_lookup=False` (not the
# marked original), and `_answer_reasoning` renders those through
# `trailing_chunks`: their own "Exact-lookup passages" block, assembled AFTER the
# section's bound chunk and element blocks and admitted only from the budget
# those two leave behind.
#
# Stripping the marker alone is not enough, which is what these tests pin.
# `_answer_reasoning` still sorts `chunks` by relevance, and an exact hit
# typically scores 1.0 while a section's own bound chunks may score lower — so an
# unmarked copy mixed into the same block still sorts ahead of them and eats the
# whole chunk budget. A section bound to source elements is hit the same way,
# because the chunk block is assembled BEFORE the element block.
#
# Deliberately built straight from `RetrievedChunk` rather than through
# `plan_outline_sections` — these tests pin what `_answer_reasoning` does with
# the two shapes, not how outline_synthesis produces them (that is
# `test_reasoning_outline_synthesis`'s job).

_MARKER_FREE_BUDGET = 49  # two full 24-char lines fit, a third does not — see below


def _labeled_chunk(chunk_id, label, marked, relevance=1.0):
    """20-char text, same length regardless of label, so every candidate
    costs the assembler exactly the same number of characters — the budget
    math below (line = 4-char prefix + 20-char text = 24 chars; two lines +
    a separator = 49) depends on that."""
    text = f"{label}{'x' * (20 - len(label))}"
    assert len(text) == 20
    return RetrievedChunk(
        chunk_id=chunk_id, source_id="s1", source_title="t",
        section_path="1", text=text, relevance=relevance, exact_lookup=marked,
    )


def _assemble(repo, monkeypatch, **kwargs):
    """Run one `_answer_reasoning` assembly and return (spy capture, baseline).

    The baseline sink carries the MERGED `id_map` (every block, after
    `_bounded_context_append` filtered out the keys truncation ate), which is
    the only place the two chunk blocks can be read back together.
    """
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    bind_chat_client(repo, "ask_answer", _Echo())
    baseline: dict = {}
    with monkeypatch.context() as patch:
        captured = _spy_assembly(patch)
        repo._runtime.ask_service()._answer_reasoning(
            notebook.id, IDENTIFIER_QUESTION, [], kwargs.pop("elements", []),
            baseline_sink=baseline, **kwargs,
        )
    return captured, baseline


def test_an_unmarked_injected_copy_does_not_outrank_the_bound_chunks(
    repo, monkeypatch
):
    """(i) The injected copy scores 1.0 and both bound chunks score below it,
    and the 49-char budget fits exactly two full lines (see `_labeled_chunk`).
    Passed as `trailing_chunks`, the copy is assembled after the bound block —
    which has already spent the budget — so the prompt carries the two chunks
    the model actually bound, and not the injected one.
    """
    _captured, baseline = _assemble(
        repo, monkeypatch,
        chunks=[_labeled_chunk("bound1", "bound1", False, relevance=0.4),
                _labeled_chunk("bound2", "bound2", False, relevance=0.5)],
        trailing_chunks=[_labeled_chunk("inject", "inject", False)],
        chunk_context_chars=_MARKER_FREE_BUDGET,
    )

    assert _object_ids(baseline["id_map"]) == {"bound1", "bound2"}, (
        f"the injected copy must take only what the bound block left, "
        f"got {sorted(_object_ids(baseline['id_map']))}")
    assert "Exact-lookup passages" not in baseline["context_block"]


def test_mutation_the_injected_copy_inside_the_bound_block_evicts_a_bound_chunk(
    repo, monkeypatch
):
    """Mutation control for (i): the very same three chunks, but the injected
    copy is handed in through `chunks=` (as it was when the copy was merely
    unmarked and appended to the section's own chunk list). The relevance sort
    now puts its 1.0 ahead of the bound 0.5/0.4 and it eats the budget — the
    regression the trailing block exists to prevent. Red by construction, so
    the assertion above is not vacuously true for any three candidates.
    """
    _captured, baseline = _assemble(
        repo, monkeypatch,
        chunks=[_labeled_chunk("bound1", "bound1", False, relevance=0.4),
                _labeled_chunk("bound2", "bound2", False, relevance=0.5),
                _labeled_chunk("inject", "inject", False)],
        chunk_context_chars=_MARKER_FREE_BUDGET,
    )

    ids = _object_ids(baseline["id_map"])
    assert "inject" in ids, "relevance order alone promotes the injected copy"
    assert ids != {"bound1", "bound2"}, (
        "and by doing so, evicts one of the two chunks the model bound")


# A long element text plus this budget: the element block alone spends the whole
# source partition, so a trailing block assembled after it has nothing left.
_ELEMENT_BUDGET = 200


def _long_element():
    return RetrievedElement(
        element_id="e1", source_id="s1", source_title="t",
        location_label="p1", element_type="paragraph",
        text="元" * 500, score=0.9,
    )


def test_a_section_bound_only_to_elements_still_comes_first(repo, monkeypatch):
    """(ii) The element block is assembled AFTER the bound chunk block but
    BEFORE the trailing one. A section whose only bound evidence is a source
    element must therefore still spend the budget first — moving the trailing
    block back ahead of the elements turns this red.
    """
    _captured, baseline = _assemble(
        repo, monkeypatch,
        elements=[_long_element()],
        trailing_chunks=[_labeled_chunk("inject", "inject", False)],
        chunk_context_chars=_ELEMENT_BUDGET,
    )

    types = {entry["object_type"] for entry in baseline["id_map"].values()}
    assert types == {"element"}, (
        f"the element must keep the whole budget, got {sorted(types)}")
    assert "Exact-lookup passages" not in baseline["context_block"]


def test_with_room_to_spare_the_trailing_block_is_admitted_after_the_bound_keys(
    repo, monkeypatch
):
    """(iii) The other half of the contract: a roomy budget really does admit
    the injected copy, numbered past the bound segment, with the KG segment
    still starting at `_MIX_KG_KEY_BASE`.
    """
    seen: list = []
    original = AskService._answer_context

    def _spy_kg(self, notebook_id, top_hits, id_offset=0, budget_chars=None):
        seen.append(id_offset)
        return original(self, notebook_id, top_hits, id_offset=id_offset,
                        budget_chars=budget_chars)

    monkeypatch.setattr(AskService, "_answer_context", _spy_kg)
    counts: dict = {}
    captured, baseline = _assemble(
        repo, monkeypatch,
        chunks=[_labeled_chunk("bound1", "bound1", False, relevance=0.4),
                _labeled_chunk("bound2", "bound2", False, relevance=0.5)],
        trailing_chunks=[_labeled_chunk("inject", "inject", False)],
        chunk_context_chars=4000, counts_sink=counts,
    )

    # 装进 prompt 的注入块跟绑定块同一条计数口径:它们真的在上下文里,轨迹上
    # 少报一条等于让「合成看见了多少原文」对不上 prompt。
    assert counts["included_chunks"] == 3
    assert len(captured["calls"]) == 2, "bound block, then the trailing block"
    assert captured["calls"][1]["id_offset"] == 2, (
        "the trailing segment is numbered past the bound chunks")
    keys = {entry["object_id"]: key for key, entry in baseline["id_map"].items()}
    assert set(keys) == {"bound1", "bound2", "inject"}
    assert keys["inject"] == "k3"
    assert "[Exact-lookup passages]" in baseline["context_block"]
    assert seen == [AskService._MIX_KG_KEY_BASE]


def test_a_section_with_only_trailing_chunks_still_moves_the_kg_segment(
    repo, monkeypatch
):
    """(iv) A section that bound no chunk at all, only injected ones: the KG
    segment must still start at `_MIX_KG_KEY_BASE`. Keying the offset off
    `chunks` alone would restart KG at k1 and collide with the trailing block's
    own k1 — two different objects behind one anchor, silently.
    """
    seen: list = []
    original = AskService._answer_context

    def _spy_kg(self, notebook_id, top_hits, id_offset=0, budget_chars=None):
        seen.append(id_offset)
        return original(self, notebook_id, top_hits, id_offset=id_offset,
                        budget_chars=budget_chars)

    monkeypatch.setattr(AskService, "_answer_context", _spy_kg)
    captured, baseline = _assemble(
        repo, monkeypatch,
        trailing_chunks=[_labeled_chunk("inject", "inject", False)],
        chunk_context_chars=4000,
    )

    assert captured["calls"][0]["id_offset"] == 0, (
        "with no bound chunks the trailing segment starts the low namespace")
    assert list(baseline["id_map"]) == ["k1"]
    assert seen == [AskService._MIX_KG_KEY_BASE], (
        "trailing chunks alone must still push the KG segment to 1001+")


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
