"""The batched positional read behind ``prepare_source_overview`` (debt fix F3).

What a reader of the overview can observe, asserted as behaviour:

* which elements are cited -- spread is evenly spaced and ends on the last
  element, opening is the first ``max_elements`` -- one citation per sampled
  position when the budget allows, in document order;
* the reads: the first page, then one positional read per <=100 positions; a
  longer list is split, never truncated;
* every visible branch when the document moves under the read: positions out
  of range or a changed total (the "changed" note, also for a change that
  is undone before the last read), a re-parse (the generation note), a deleted
  source (``KeyError``), cancellation;
* ``stable_count`` never hands back a silently mixed document: a model store
  charges the windowed read as two accesses (count, then row -- two
  statements in both adapters) and a positional read as one, and a change is
  injected after every access in turn.

The byte-for-byte comparison with the pre-batching function was run once as
PR evidence, not kept as a frozen copy here. The in-memory stores answer
``positions=`` through ``app.domain.source_positions.page_at_positions`` --
the contract the real adapters are tested against in
``test_source_elements_positions.py`` and its PostgreSQL twin -- so they are
not a second implementation of it.
"""
from __future__ import annotations

import json
import math
from threading import Event

import pytest

from app.domain.source_positions import page_at_positions
from app.models.sources import PaginatedSourceElements, SourceElement
from app.services.cancellation import AskCancelled
from app.services.collection_enumeration import SourceItem
from app.services.document_source_overview import prepare_source_overview


ITEM = SourceItem("source", "文档", "文档", "", "nb", "personal")
CHANGED = "读取期间文档内容发生变化，请重试。"


def _element(index: int, text: str, tag: str = "") -> SourceElement:
    return SourceElement(
        id=f"e{tag}{index:04d}", source_id="source", element_type="paragraph",
        location_label=f"第 {index // 7 + 1} 页", text=text,
        metadata={"section_path": f"章 {index // 20} / 段 {index}"} if index % 3 else {},
    )


def _texts(count: int) -> list[str]:
    return [f"正文{index}:" + "原文内容。" * (1 + (index * 7) % 40) for index in range(count)]


class Store:
    """``source_elements_page`` over a mutable element list.

    ``after_call(n)`` runs after the n-th adapter call (1-based), the seam the
    concurrent-change scenarios use. ``deleted`` makes every later call raise
    ``KeyError`` like both adapters do for a vanished source row.
    """

    def __init__(self, count: int, hook=None):
        self.elements = [_element(i, t) for i, t in enumerate(_texts(count))]
        self.hook = hook
        self.calls: list[tuple] = []
        self.deleted = False

    def source_elements_page(self, source_id, offset=0, limit=1, *, positions=None):
        assert source_id == "source"
        if self.deleted:
            raise KeyError(source_id)
        if positions is not None:
            self.calls.append(("positions", list(positions)))
            page = page_at_positions(self.elements, positions)
        else:
            self.calls.append(("offset", offset, limit))
            page = PaginatedSourceElements(
                items=self.elements[offset:offset + limit],
                total_count=len(self.elements), offset=offset, limit=limit,
            )
        if self.hook is not None:
            self.hook(self, len(self.calls))
        return page


def _fields(result) -> str:
    return json.dumps([
        result.context_block,
        json.dumps(result.id_map, ensure_ascii=False),
        [citation.model_dump_json() for citation in result.citations],
        result.coverage_note,
    ], ensure_ascii=False)


def _run(store, budget, max_elements, coverage="spread", generations=None):
    reader = None
    if generations is not None:
        sequence = iter(generations)
        reader = lambda _source_id: next(sequence)  # noqa: E731
    return prepare_source_overview(
        store, ITEM, budget, max_elements, active_notebook_id="nb",
        generation_reader=reader, coverage=coverage, key_offset=3,
    )


def _evenly_spaced(indexes: list[int]) -> bool:
    gaps = [b - a for a, b in zip(indexes, indexes[1:])]
    return not gaps or max(gaps) - min(gaps) <= 1


@pytest.mark.parametrize("count", [0, 1, 2, 3, 7, 64, 101, 250])
@pytest.mark.parametrize("max_elements", [1, 2, 5, 22, 64, 100, 101, 230])
@pytest.mark.parametrize("coverage", ["spread", "opening"])
@pytest.mark.parametrize("witnessed", [False, True])
def test_the_sampled_elements_are_the_promised_positions(count, max_elements, coverage, witnessed):
    store = Store(count)
    result = _run(store, 10_000_000, max_elements, coverage,
                  generations=["v1", "v1"] if witnessed else None)
    cited = [int(citation.element_id[1:]) for citation in result.citations]
    sampled = min(count, max_elements)
    assert len(cited) == sampled
    assert cited == sorted(set(cited))
    if coverage == "opening":
        assert cited == list(range(sampled))
    elif sampled:
        assert cited[-1] == count - 1                 # spread ends on the last element
        assert sampled == 1 or cited[0] == 0
        assert _evenly_spaced(cited)
    assert list(result.id_map) == [f"k{3 + index + 1}" for index in range(len(cited))]
    later = len([index for index in cited if index])
    assert len(store.calls) == 1 + math.ceil(later / 100)
    if not count:
        assert "没有可读取的原文" in result.coverage_note
    elif witnessed and count <= max_elements:
        assert f"已读取全部 {count} 个原文元素" in result.coverage_note
    else:
        assert f"{sampled}/{count}" in result.coverage_note


@pytest.mark.parametrize("count,max_elements", [(7, 5), (250, 64), (300, 230)])
def test_a_tight_budget_never_overflows_and_keeps_document_order(count, max_elements):
    result = _run(Store(count), 400, max_elements)
    assert len(result.context_block) <= 400
    cited = [int(citation.element_id[1:]) for citation in result.citations]
    assert cited == sorted(set(cited))


def _shrink(store, call):
    if call == 1:  # concurrent re-chunk deletes the tail after the first page
        del store.elements[len(store.elements) // 2:]


def _grow(store, call):
    if call == 1:
        store.elements.append(_element(9999, "后来追加的原文", tag="x"))


def _swap_same_count(store, call):
    if call == 1:  # a rewrite that keeps the count: only the generation can see it
        store.elements = [_element(i, f"新版本{i}", tag="n") for i in range(len(store.elements))]


def _delete(store, call):
    if call == 1:
        store.deleted = True


REPARSED = "读取期间文档重新解析，无法确认原文一致性，请重试。"


def _empty(result) -> bool:
    return not result.context_block and not result.id_map and not result.citations


@pytest.mark.parametrize("hook", [_shrink, _grow])
@pytest.mark.parametrize("count,max_elements", [(2, 2), (40, 5), (250, 64), (300, 230)])
@pytest.mark.parametrize("coverage", ["spread", "opening"])
def test_a_changed_total_after_the_first_page_voids_the_overview(hook, count, max_elements, coverage):
    result = _run(Store(count, hook), 30_000, max_elements, coverage)
    assert result.coverage_note == CHANGED and _empty(result)
    # a re-parse witness that also moved wins: it is checked first
    result = _run(Store(count, hook), 30_000, max_elements, coverage, ["v1", "v2"])
    assert result.coverage_note == REPARSED and _empty(result)


@pytest.mark.parametrize("count,max_elements", [(2, 2), (40, 5), (300, 230)])
def test_a_deleted_source_raises_key_error(count, max_elements):
    with pytest.raises(KeyError):
        _run(Store(count, _delete), 30_000, max_elements)


@pytest.mark.parametrize("count,max_elements", [(40, 5), (300, 230)])
def test_a_same_count_rewrite_is_caught_by_the_generation_witness(count, max_elements):
    result = _run(Store(count, _swap_same_count), 30_000, max_elements, "spread", ["v1", "v2"])
    assert result.coverage_note == REPARSED and _empty(result)
    # without a witness the count cannot see it, and the note never claims a
    # complete reading
    result = _run(Store(count, _swap_same_count), 30_000, max_elements)
    assert "已读取全部" not in result.coverage_note


def test_positions_after_the_first_page_go_in_one_read():
    store = Store(100)
    prepare_source_overview(store, ITEM, 30_000, 5, active_notebook_id="nb")
    assert store.calls == [("offset", 0, 1), ("positions", [24, 49, 74, 99])]


def test_singleton_spread_reads_only_the_last_position_in_one_read():
    store = Store(100)
    result = prepare_source_overview(store, ITEM, 30_000, 1, active_notebook_id="nb")
    assert store.calls == [("offset", 0, 1), ("positions", [99])]
    assert [citation.element_id for citation in result.citations] == ["e0099"]


def test_more_than_100_positions_are_split_never_truncated():
    store = Store(300)
    result = prepare_source_overview(
        store, ITEM, 10_000_000, 250, active_notebook_id="nb", coverage="opening",
    )
    reads = [call[1] for call in store.calls if call[0] == "positions"]
    assert [len(read) for read in reads] == [100, 100, 49]
    assert sum(reads, []) == list(range(1, 250))
    assert len(result.citations) == 250


def test_a_change_between_two_batches_voids_the_overview():
    def between_batches(store, call):
        if call == 2:  # after the first positional read, before the second
            store.elements.append(_element(9999, "追加", tag="x"))

    store = Store(300, between_batches)
    result = prepare_source_overview(
        store, ITEM, 10_000_000, 250, active_notebook_id="nb", coverage="opening",
    )
    assert len([call for call in store.calls if call[0] == "positions"]) == 3
    assert result.coverage_note == CHANGED and not result.citations


def test_a_change_undone_before_the_last_batch_still_voids_the_overview():
    """ABA: the total moves after the first positional read and is restored
    after the second, so the LAST read sees the first page's total again. The
    middle read saw another one -- every batch must be compared, not only the
    last."""
    extra = _element(9999, "短暂出现的段落", tag="x")

    def aba(store, call):
        if call == 2:
            store.elements.append(extra)
        elif call == 3:
            store.elements.remove(extra)

    store = Store(300, aba)
    result = prepare_source_overview(
        store, ITEM, 10_000_000, 250, active_notebook_id="nb", coverage="opening",
    )
    reads = [call for call in store.calls if call[0] == "positions"]
    assert len(reads) == 3 and len(store.elements) == 300
    assert result.coverage_note == CHANGED and _empty(result)


def test_cancellation_is_checked_before_every_read():
    event = Event()

    def cancel_after_first_page(store, call):
        if call == 1:
            event.set()

    store = Store(300, cancel_after_first_page)
    with pytest.raises(AskCancelled):
        prepare_source_overview(store, ITEM, 30_000, 230, event, active_notebook_id="nb")
    assert len(store.calls) == 1


# ------------------------------------------------------- stable_count model
class Accesses(Store):
    """Charges each storage access separately: the windowed read is a count
    statement then a row statement; a positional read is one statement. A
    change can be injected after any access (``change_after``)."""

    def __init__(self, count: int, change_after: int):
        super().__init__(count)
        self.change_after = change_after
        self.accesses = 0
        self.changed = False

    def _accessed(self):
        """One access has completed; apply the change if it is due now."""
        self.accesses += 1
        if self.accesses == self.change_after and not self.changed:
            self.changed = True
            _change(self.elements)

    def source_elements_page(self, source_id, offset=0, limit=1, *, positions=None):
        if positions is not None:
            page = page_at_positions(self.elements, positions)
            self._accessed()
            return page
        total = len(self.elements)
        self._accessed()
        items = self.elements[offset:offset + limit]
        self._accessed()
        return PaginatedSourceElements(
            items=items, total_count=total, offset=offset, limit=limit,
        )


def _change(elements: list) -> None:
    # One element inserted at the front: the count moves and every position
    # now names a different element.
    elements.insert(0, _element(0, "插入的新段落", tag="z"))


def _undetected_mix(fn, count, max_elements, change_after) -> bool:
    """True when the overview carries evidence that is neither a whole reading
    of the document before the change nor a whole reading after it, and does
    not say the document changed."""
    def read(store):
        return _fields(fn(store, ITEM, 30_000, max_elements, active_notebook_id="nb"))

    before = read(Accesses(count, 10**9))
    changed = Accesses(count, 10**9)
    _change(changed.elements)
    after = read(changed)
    store = Accesses(count, change_after)
    result = read(store)
    return CHANGED not in result and result not in (before, after)


@pytest.mark.parametrize("count,max_elements", [(12, 4), (60, 6), (300, 120), (300, 230)])
def test_a_change_at_any_access_never_yields_a_silently_mixed_overview(count, max_elements):
    """Every access the read makes -- the first page's count and row, then
    each positional read -- is followed in turn by one count-changing write.
    The result is a whole reading of the document before the change, a whole
    reading after it, or the "changed" note; never a mixture without the note.
    (The per-offset loop this replaced did hand back such a mixture: a change
    between its last count and its last row passed every total comparison --
    see the PR evidence.)"""
    accesses = 2 + math.ceil((max_elements - 1) / 100)
    assert [
        after for after in range(1, accesses + 2)
        if _undetected_mix(prepare_source_overview, count, max_elements, after)
    ] == []
