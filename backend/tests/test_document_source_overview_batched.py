"""The batched positional read behind ``prepare_source_overview`` (debt fix F3).

Three claims, each against the frozen pre-batching function
(``tests/document_overview_legacy_oracle.py``) over the same store:

* every ``SourceOverview`` field is byte-identical -- static documents of every
  shape, a document that shrinks or grows after the first page (positions out
  of range / total changed), a re-parse (generation changed), a deleted source;
* the reads are batched: the first page, then one read per <=100 positions,
  and a list longer than 100 is split, never truncated;
* ``stable_count`` detects at least what the per-offset loop detected. The
  model store below charges the windowed read as two accesses (count, then
  row -- two statements in both adapters) and a positional read as one (one
  statement), and a change is injected after every access in turn.

The in-memory stores answer ``positions=`` through
``app.domain.source_positions.page_at_positions`` -- the contract the real
adapters are tested against in ``test_source_elements_positions.py`` and its
PostgreSQL twin -- so they are not a second implementation of it.
"""
from __future__ import annotations

import json
from threading import Event

import pytest

from app.domain.source_positions import page_at_positions
from app.models.sources import PaginatedSourceElements, SourceElement
from app.services.cancellation import AskCancelled
from app.services.collection_enumeration import SourceItem
from app.services.document_source_overview import prepare_source_overview
from tests.document_overview_legacy_oracle import legacy_prepare_source_overview


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


def _both(make_store, budget, max_elements, coverage="spread", generations=None):
    """Run the current and the frozen function over twin stores."""
    out = []
    for fn in (prepare_source_overview, legacy_prepare_source_overview):
        reader = None
        if generations is not None:
            sequence = iter(generations)
            reader = lambda _source_id, sequence=sequence: next(sequence)  # noqa: E731
        try:
            out.append(_fields(fn(
                make_store(), ITEM, budget, max_elements, active_notebook_id="nb",
                generation_reader=reader, coverage=coverage, key_offset=3,
            )))
        except KeyError as exc:
            out.append(f"KeyError:{exc}")
    return out


@pytest.mark.parametrize("count", [0, 1, 2, 3, 7, 64, 101, 250])
@pytest.mark.parametrize("max_elements", [1, 2, 5, 22, 64, 100, 101, 230])
@pytest.mark.parametrize("coverage", ["spread", "opening"])
@pytest.mark.parametrize("budget", [90, 30_000])
@pytest.mark.parametrize("witnessed", [False, True])
def test_static_documents_are_byte_identical(count, max_elements, coverage, budget, witnessed):
    current, legacy = _both(
        lambda: Store(count), budget, max_elements, coverage,
        generations=["v1", "v1"] if witnessed else None,
    )
    assert current == legacy


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


@pytest.mark.parametrize("hook", [_shrink, _grow, _swap_same_count, _delete])
@pytest.mark.parametrize("count,max_elements", [(2, 2), (40, 5), (250, 64), (300, 230)])
@pytest.mark.parametrize("coverage", ["spread", "opening"])
@pytest.mark.parametrize("generations", [None, ["v1", "v1"], ["v1", "v2"]])
def test_changes_after_the_first_page_are_byte_identical(
    hook, count, max_elements, coverage, generations,
):
    current, legacy = _both(
        lambda: Store(count, hook), 30_000, max_elements, coverage, generations,
    )
    assert current == legacy


def test_shrink_and_grow_hit_the_total_changed_branch_and_delete_raises():
    for hook in (_shrink, _grow):
        current, legacy = _both(lambda hook=hook: Store(40, hook), 30_000, 5)
        assert current == legacy and CHANGED in current
    current, legacy = _both(lambda: Store(40, _delete), 30_000, 5)
    assert current == legacy == "KeyError:'source'"
    current, legacy = _both(lambda: Store(40), 30_000, 5, generations=["v1", "v2"])
    assert current == legacy and "重新解析" in current


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


@pytest.mark.parametrize("count,max_elements", [(12, 4), (60, 6), (300, 120)])
def test_stable_count_detects_at_least_what_the_per_offset_loop_did(count, max_elements):
    legacy_accesses = 2 * (1 + (max_elements - 1))
    current_misses = [
        after for after in range(1, legacy_accesses + 1)
        if _undetected_mix(prepare_source_overview, count, max_elements, after)
    ]
    legacy_misses = [
        after for after in range(1, legacy_accesses + 1)
        if _undetected_mix(legacy_prepare_source_overview, count, max_elements, after)
    ]
    # The batched read never hands back a silently mixed document ...
    assert current_misses == []
    # ... while the per-offset loop did: a change between the last read's
    # count and its row passed every total comparison.
    assert legacy_misses
