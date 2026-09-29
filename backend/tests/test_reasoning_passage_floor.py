"""The passage floor: the structured side can no longer starve the reserved
passages of a reasoning synthesis -- PR-C, C4.

The Knowhow preview, the enumeration preview, document reads and the
spreadsheet block are assembled BEFORE the passage segment and share its
character partition (`chunk_context_chars`).  A structured block sized to the
partition used to leave the passages zero characters, voiding every reserved
seat.  `AskService._assemble_structured_evidence` computes
`floor = min(partition // 2, heading + rendered cost of the reserved prefix)`
(exact prefix + active prefix of the very order `_answer_reasoning` renders,
measured the way the passage segment renders it) and `room = partition -
floor - map block`.  The structured side renders as it always has; only when
that historical rendering is longer than `room` is it rendered again with
`room` as an extra ceiling -- so each block's own coverage disclosure stays
true, and every run whose historical structured side leaves the prefix whole
(no chunk, no reserved seat, or simply enough room) is byte-for-byte as
before.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.domain.retrieval import RetrievalSupport, RetrievedChunk
from app.models.schemas import NotebookCreate
from app.services import ask_service as ask_module
from app.services.ask_service import AskService
from app.services.chunk_federation import reasoning_order_for
from app.services.retrieval import passage_floor

HEADING = len(f"\n\n[{ask_module._REASONING_CHUNK_HEADING}]\n")
from app.services.structured_retrieval import (
    StructuredEnumeration,
    structured_prompt_block,
)
from tests.model_testkit import bind_chat_client
from tests.test_exact_lookup import repo  # noqa: F401  (fixture)

PARTITION = 6000


class _Echo:
    configured = True
    model = "m"

    def chat_json(self, messages, schema_hint, **kwargs):
        return json.dumps({"answer": "结论。", "grounded": False})


def _p(chunk_id, relevance, *, notebook_id="", exact=False, length=300):
    return RetrievedChunk(
        chunk_id=chunk_id, source_id=f"s-{chunk_id}", source_title="t",
        section_path="", text=(chunk_id + " ").ljust(length, "x"),
        relevance=relevance, score=relevance, notebook_id=notebook_id,
        retrieval_supports=(RetrievalSupport("semantic", "chunk", chunk_id,
                                             relevance),),
        exact_lookup=exact,
    )


def _foreign_pool():
    foreign = [_p(f"ref-{i}", 0.9 - i * 0.01, notebook_id="ref") for i in range(12)]
    mine = [_p(f"mine-{i}", 0.3 - i * 0.01) for i in range(3)]
    exact = [_p("exact-0", 1.0, notebook_id="ref", exact=True, length=200)]
    return foreign + mine + exact


def _batch(rows=400):
    """A Knowhow table whose preview alone would fill the whole partition."""
    table = SimpleNamespace(
        title="表", columns=[SimpleNamespace(id="c", name="C")],
        rows=[SimpleNamespace(row_id=i, cells={"c": f"value {i} ".ljust(60, "v")})
              for i in range(rows)])
    return StructuredEnumeration(
        result_sets=[table], known_total_rows=rows, returned_rows=rows,
        complete=True)


def _sheet(rows=80):
    return SimpleNamespace(
        source_title="wb", sheet="S", range="A1:A80", operation="filter",
        coverage=SimpleNamespace(scanned_rows=rows, total_rows=rows),
        columns=[SimpleNamespace(name="A")],
        rows=[SimpleNamespace(cells={"A": "cell ".ljust(90, "c")}, citation=None)
              for _ in range(rows)],
        source_id="s-wb", source_file_name="wb.xlsx")


def _stage(notebook_id, chunks, *, batch=None, sheets=()):
    limits = SimpleNamespace(
        chunk_context_chars=PARTITION, inline_answer_rows=10_000,
        cell_excerpt_chars=200, structured_payload_chars=1_000_000)
    return SimpleNamespace(
        prepared=SimpleNamespace(limits=limits, notebook_id=notebook_id),
        structured_batch=batch, enumerations=[], document_reads=[],
        spreadsheet_results=list(sheets), chunks=list(chunks))


def _floor(prefix):
    return passage_floor(prefix, PARTITION, heading_chars=HEADING)


def _structured(service, stage, map_block=""):
    return service._assemble_structured_evidence(stage, _Echo(), map_block)


@pytest.fixture
def service(repo):  # noqa: F811
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    bind_chat_client(repo, "ask_answer", _Echo())
    svc = repo._runtime.ask_service()
    svc.settings.spreadsheet_analysis_prompt_rows = 1000
    return svc, notebook.id


def _render(svc, notebook_id, chunks, structured_block, structured_map):
    captured: dict = {}
    real = AskService._chunk_answer_context

    def _spy(self, chunks, budget_chars=None, notebook_id="", id_offset=0):
        block, id_map = real(self, chunks, budget_chars=budget_chars,
                             notebook_id=notebook_id, id_offset=id_offset)
        captured["rendered"] = {entry["object_id"] for entry in id_map.values()}
        return block, id_map

    AskService._chunk_answer_context = _spy
    try:
        svc._answer_reasoning(
            notebook_id, "q", [], [], chunks=chunks,
            chunk_context_chars=PARTITION, structured_block=structured_block,
            structured_map=structured_map)
    finally:
        AskService._chunk_answer_context = real
    return captured.get("rendered", set())


# --------------------------------------------------------------------- floor
def test_floor_is_the_prefix_cost_capped_at_half_the_partition():
    prefix = [_p("a", 0.5, length=100), _p("b", 0.5, length=50)]
    # The renderer's own measure: "k1: " + 100, newline, "k2: " + 50.
    assert passage_floor(prefix, 10_000, heading_chars=HEADING) == (
        HEADING + 4 + 100 + 1 + 4 + 50)
    assert passage_floor(prefix, 300, heading_chars=HEADING) == 150
    assert passage_floor([], 10_000, heading_chars=HEADING) == 0


def test_a_full_knowhow_block_leaves_the_reserved_prefix_its_characters(service):
    svc, nb = service
    chunks = _foreign_pool()
    order = reasoning_order_for(svc.settings, chunks, nb)
    prefix = {chunk.chunk_id for chunk in order.passages[:order.prefix]}
    assert {"exact-0", "mine-0", "mine-1", "mine-2"} <= prefix

    block, smap, *_ = _structured(svc, _stage(nb, chunks, batch=_batch()))
    floor = _floor(order.passages[:order.prefix])
    assert 0 < floor and len(block) <= PARTITION - floor
    # The Knowhow block's own disclosure is still true: partial.
    assert "synthesis_complete=false" in block
    rendered = _render(svc, nb, chunks, block, smap)
    assert prefix <= rendered, prefix - rendered


def test_control_without_the_floor_the_prefix_is_starved(service, monkeypatch):
    svc, nb = service
    monkeypatch.setattr(ask_module, "passage_floor", lambda prefix, chars, heading_chars: 0)
    chunks = _foreign_pool()
    block, smap, *_ = _structured(svc, _stage(nb, chunks, batch=_batch()))
    rendered = _render(svc, nb, chunks, block, smap)
    assert not {"mine-0", "mine-1", "mine-2"} <= rendered


def test_the_floor_covers_the_prefix_only_not_every_passage(service):
    svc, nb = service
    chunks = _foreign_pool()
    block, *_ = _structured(svc, _stage(nb, chunks, batch=_batch()))
    order = reasoning_order_for(svc.settings, chunks, nb)
    everything = _floor(order.passages)
    prefix_only = _floor(order.passages[:order.prefix])
    assert prefix_only < everything
    assert len(block) > PARTITION - everything


def test_the_map_block_is_held_back_too(service):
    svc, nb = service
    chunks = _foreign_pool()
    plain, *_ = _structured(svc, _stage(nb, chunks, batch=_batch()))
    with_map, *_ = _structured(svc, _stage(nb, chunks, batch=_batch()),
                               map_block="m" * 300)
    order = reasoning_order_for(svc.settings, chunks, nb)
    floor = _floor(order.passages[:order.prefix])
    assert len(plain) <= PARTITION - floor
    assert len(with_map) <= PARTITION - floor - 302 < len(plain)


# --------------------------------------------------------------- byte identity
def _unfloored(batch):
    return structured_prompt_block(batch, inline_rows=10_000,
                                   cell_excerpt_chars=200, budget_chars=PARTITION)


def test_no_chunks_renders_the_structured_side_unchanged(service):
    svc, nb = service
    block, *_ = _structured(svc, _stage(nb, [], batch=_batch()))
    assert block == _unfloored(_batch())


def test_seatless_run_renders_the_structured_side_unchanged(service):
    """Single notebook, no exact hit: no reserved seat, floor 0."""
    svc, nb = service
    chunks = [_p(f"mine-{i}", 0.5 - i * 0.01) for i in range(10)]
    order = reasoning_order_for(svc.settings, chunks, nb)
    assert order.prefix == 0
    block, *_ = _structured(svc, _stage(nb, chunks, batch=_batch()))
    assert block == _unfloored(_batch())
    sheets, *_ = _structured(svc, _stage(nb, chunks, sheets=[_sheet()]))
    sheets_nochunk, *_ = _structured(svc, _stage(nb, [], sheets=[_sheet()]))
    assert sheets == sheets_nochunk


# ---------------------------------------------------------------- spreadsheet
def test_spreadsheet_block_respects_the_reduced_partition(service):
    svc, nb = service
    chunks = _foreign_pool()
    order = reasoning_order_for(svc.settings, chunks, nb)
    floor = _floor(order.passages[:order.prefix])
    block, smap, *_ = _structured(svc, _stage(nb, chunks, sheets=[_sheet()]))
    assert len(block) <= PARTITION - floor
    unbounded, *_ = _structured(svc, _stage(nb, [], sheets=[_sheet()]))
    assert len(unbounded) > PARTITION - floor, "the sheet must really overflow"
    assert "k6001" in smap
    rendered = _render(svc, nb, chunks, block, smap)
    assert {"exact-0", "mine-0"} <= rendered


# ------------------------------------------------ enumeration / document reads
def _fake_roster(monkeypatch, svc, seen):
    """A roster that fills whatever budget it is given (a long collection)."""
    from app.services import collection_enumeration_answer as enum_module

    monkeypatch.setattr(enum_module, "typed_collection_results",
                        lambda *a, **k: [])
    monkeypatch.setattr(enum_module, "delivered_outcomes", lambda e, t: e)
    monkeypatch.setattr(enum_module, "apply_synthesis_preview_counts",
                        lambda *a, **k: None)

    def _block(*args, budget_chars, **kwargs):
        seen.setdefault("enumeration", []).append(budget_chars)
        return SimpleNamespace(text="R" * budget_chars, evidence_by_id={},
                               shown_rows={})

    monkeypatch.setattr(enum_module, "enumeration_prompt_block", _block)
    monkeypatch.setattr(svc.evidence_context, "collection_item_citations",
                        lambda *a, **k: {})


def _fake_reads(monkeypatch, size):
    from app.services import document_read_answer

    preview = SimpleNamespace(text="D" * size, citations=[], evidence_by_id={})
    monkeypatch.setattr(document_read_answer, "document_read_prompt_block",
                        lambda reads, roster_map: preview)
    monkeypatch.setattr(ask_module, "document_read_block_reserve",
                        lambda reads: size + 2 if reads else 0)


def _single_notebook_exact_chunks():
    """No mounted library, two exact hits: a real exact prefix, floor > 0."""
    return [_p("exact-0", 1.0, exact=True), _p("exact-1", 1.0, exact=True)] + [
        _p(f"mine-{i}", 0.5 - i * 0.01) for i in range(5)]


def test_half_partition_caps_are_not_shrunk_by_the_floor(service, monkeypatch):
    """Single notebook, knob 0, exact prefix present, no Knowhow block: a
    roster filling its half partition renders byte-for-byte as it did before
    the floor existed (the no-chunk path is the historical code path)."""
    svc, nb = service
    svc.settings.chunk_federation_active_reserve = 0.0
    seen: dict = {}
    _fake_roster(monkeypatch, svc, seen)
    chunks = _single_notebook_exact_chunks()
    order = reasoning_order_for(svc.settings, chunks, nb)
    assert _floor(order.passages[:order.prefix]) > 0

    stage = _stage(nb, chunks)
    stage.enumerations = [SimpleNamespace(items=[])]
    floored, *_ = _structured(svc, stage, map_block="m" * 150)
    historical_stage = _stage(nb, [])
    historical_stage.enumerations = [SimpleNamespace(items=[])]
    historical, *_ = _structured(svc, historical_stage, map_block="m" * 150)
    assert floored == historical
    assert len(floored) == PARTITION // 2
    assert seen["enumeration"] == [PARTITION // 2, PARTITION // 2]


def test_document_read_half_cap_is_not_shrunk_by_the_floor(service, monkeypatch):
    svc, nb = service
    svc.settings.chunk_federation_active_reserve = 0.0
    size = PARTITION // 2 - 50            # fits C//2, not (C - floor - map)//2
    _fake_reads(monkeypatch, size)
    stage = _stage(nb, _single_notebook_exact_chunks())
    stage.document_reads = [object()]
    block, *rest = _structured(svc, stage, map_block="m" * 150)
    assert block == "D" * size
    assert rest[-1] is False               # document_read_block_dropped


def test_the_floor_still_holds_when_roster_and_knowhow_would_crowd(
    service, monkeypatch,
):
    """Knowhow + a half-partition roster would leave the prefix nothing: the
    roster is re-rendered against what the floor leaves, and the reserved
    passages reach the prompt."""
    svc, nb = service
    seen: dict = {}
    _fake_roster(monkeypatch, svc, seen)
    chunks = _foreign_pool()
    order = reasoning_order_for(svc.settings, chunks, nb)
    floor = _floor(order.passages[:order.prefix])
    stage = _stage(nb, chunks, batch=_batch(rows=45))   # ~3000-char Knowhow
    stage.enumerations = [SimpleNamespace(items=[])]
    block, smap, *_ = _structured(svc, stage)
    assert len(block) <= PARTITION - floor
    # Historical rendering, then the crowded rendering (historical budget
    # first, then the budget ``room`` leaves).
    assert len(seen["enumeration"]) == 3 and seen["enumeration"][2] < seen[
        "enumeration"][0], "the historical rendering crowded, so it re-rendered"
    prefix = {chunk.chunk_id for chunk in order.passages[:order.prefix]}
    assert prefix <= _render(svc, nb, chunks, block, smap)


def test_a_document_read_that_would_crowd_is_dropped_whole(service, monkeypatch):
    svc, nb = service
    chunks = _foreign_pool()
    order = reasoning_order_for(svc.settings, chunks, nb)
    floor = _floor(order.passages[:order.prefix])
    _fake_reads(monkeypatch, PARTITION // 2 - 50)
    stage = _stage(nb, chunks, batch=_batch(rows=40))
    stage.document_reads = [object()]
    block, *rest = _structured(svc, stage)
    assert "D" * 10 not in block and rest[-1] is True
    assert len(block) <= PARTITION - floor


def _long_active_chunks():
    """Three long exact hits: the floor reaches its half-partition cap."""
    return [_p(f"exact-{i}", 1.0, exact=True, length=1200) for i in range(3)]


def test_a_document_read_crowded_only_by_the_floor_leaves_a_notice(
    service, monkeypatch,
):
    """Fits the historical caps, not the room the floor leaves: dropped whole,
    and the prompt says excerpts exist instead of going silent."""
    svc, nb = service
    size = PARTITION // 2 - 50
    _fake_reads(monkeypatch, size)
    stage = _stage(nb, _long_active_chunks())
    stage.document_reads = [object()]
    block, *rest = _structured(svc, stage, map_block="m" * 300)
    assert rest[-1] is True
    assert block == ask_module.DOCUMENT_READ_OMITTED_NOTICE
    historical = _stage(nb, [])
    historical.document_reads = [object()]
    kept, *rest = _structured(svc, historical, map_block="m" * 300)
    assert kept == "D" * size and rest[-1] is False


def test_a_spreadsheet_block_left_out_for_space_leaves_a_notice(service):
    svc, nb = service
    notice = ask_module.SPREADSHEET_OMITTED_NOTICE
    sheet = _sheet()
    sheet.source_title = "wb" * 200          # the first line alone is ~500
    smap: dict = {}
    out = svc._add_sheet_prompt("K", smap, [sheet], _Echo(),
                                max_chars=len(notice) + 10)
    assert out == f"K\n\n{notice}" and smap == {}
    # Not even the notice fits: nothing is written.
    assert svc._add_sheet_prompt("K", {}, [sheet], _Echo(),
                                 max_chars=len(notice) - 1) == "K"
    # No floor (max_chars None): the historical byte cap only, no notice.
    assert notice not in svc._add_sheet_prompt("K", {}, [sheet], _Echo())


def test_no_notice_when_nothing_is_left_out(service):
    svc, nb = service
    block, *_ = _structured(svc, _stage(nb, _foreign_pool(), sheets=[_sheet(rows=3)]))
    assert ask_module.SPREADSHEET_OMITTED_NOTICE not in block
    assert block.startswith("k6001: [spreadsheet]")


def test_seatless_run_with_a_map_block_is_unchanged(service):
    """No reserved seat: the map block's length is never deducted."""
    svc, nb = service
    chunks = [_p(f"mine-{i}", 0.5 - i * 0.01) for i in range(10)]
    block, *_ = _structured(svc, _stage(nb, chunks, batch=_batch()),
                            map_block="m" * 300)
    assert block == _unfloored(_batch())


def test_own_id_ppr_passages_do_not_create_a_floor(service):
    """Behavioural twin of the C8 guard: single notebook, PPR passages stamped
    with the notebook's own id; with the real id no active prefix exists, so
    no floor and the structured side is unchanged."""
    from app.domain.retrieval import RetrievalSupport as Support

    svc, nb = service
    ppr = [RetrievedChunk(
        chunk_id=f"ppr-{i}", source_id=f"s-{i}", source_title="t",
        section_path="", text=f"ppr-{i} ".ljust(300, "p"), relevance=1.0 - i / 10,
        score=1.0 - i / 10, notebook_id=nb,
        retrieval_supports=(Support("ppr", "ppr", "", 1.0 - i / 10),))
        for i in range(4)]
    chunks = ppr + [_p(f"seed-{i}", 0.4 - i * 0.01) for i in range(4)]
    block, *_ = _structured(svc, _stage(nb, chunks, batch=_batch()))
    assert block == _unfloored(_batch())


# ------------------------------------ the document-read reservation (review)
def _stage_at(nb, chunks, batch, partition):
    limits = SimpleNamespace(
        chunk_context_chars=partition, inline_answer_rows=10_000,
        cell_excerpt_chars=200, structured_payload_chars=1_000_000)
    return SimpleNamespace(
        prepared=SimpleNamespace(limits=limits, notebook_id=nb),
        structured_batch=batch, enumerations=[SimpleNamespace(items=[])],
        document_reads=[object()], spreadsheet_results=[], chunks=list(chunks))


def test_no_reservation_for_a_document_read_that_cannot_go_in(service, monkeypatch):
    """Crowded: the document read does not fit ``room`` after the Knowhow
    block, so the roster must not give up space for it -- the roster keeps
    what ``room`` leaves instead of being emptied for a block that is then
    dropped anyway."""
    svc, nb = service
    seen: dict = {}
    _fake_roster(monkeypatch, svc, seen)
    _fake_reads(monkeypatch, 7000)
    chunks = [_p(f"exact-{i}", 1.0, exact=True, length=6000) for i in range(3)]
    stage = _stage_at(nb, chunks, _batch(rows=130), 30000)
    block, _smap, _t, enum_dropped, _c, doc_dropped = _structured(
        svc, stage, "m" * 300)
    room = 30000 - 15000 - 302            # floor capped at half the partition
    assert doc_dropped and not enum_dropped
    assert "R" * 20 in block and "D" * 20 not in block
    assert len(block) == room, "no room left unused"


def test_a_document_read_that_fits_keeps_its_reservation(service, monkeypatch):
    """Crowded, and the document read does fit after the Knowhow block: the
    roster yields exactly its share, and the read goes in."""
    svc, nb = service
    seen: dict = {}
    _fake_roster(monkeypatch, svc, seen)
    _fake_reads(monkeypatch, 3000)
    chunks = [_p(f"exact-{i}", 1.0, exact=True, length=6000) for i in range(3)]
    stage = _stage_at(nb, chunks, _batch(rows=110), 30000)
    block, _smap, _t, enum_dropped, _c, doc_dropped = _structured(
        svc, stage, "m" * 300)
    room = 30000 - 15000 - 302
    assert not doc_dropped and not enum_dropped
    assert "D" * 3000 in block and "R" * 20 in block
    assert len(block) <= room


def test_an_uncrowded_run_keeps_the_historical_structured_side(service, monkeypatch):
    """Single notebook, no reference library, Knowhow above half the
    partition, roster + a document read the historical partition cap refuses:
    the historical structured side leaves the exact prefix whole, so nothing
    changes -- the roster is not squeezed to let the read in."""
    svc, nb = service
    seen: dict = {}
    _fake_roster(monkeypatch, svc, seen)
    _fake_reads(monkeypatch, 5000)
    chunks = [_p("exact-0", 1.0, exact=True, length=300)]
    stage = _stage_at(nb, chunks, _batch(rows=250), 30000)
    got = _structured(svc, stage, "m" * 300)
    historical = _structured(svc, _stage_at(nb, [], _batch(rows=250), 30000),
                             "m" * 300)
    assert got[0] == historical[0] and got[5] is historical[5] is True
    assert "R" * 20 in got[0]


def test_the_document_read_notice_is_only_written_when_it_fits(service, monkeypatch):
    """Room so tight that even the one-line notice does not fit: nothing is
    written, and the structured side stays within ``room``."""
    svc, nb = service
    size = PARTITION // 2 - 50
    _fake_reads(monkeypatch, size)
    stage = _stage(nb, _long_active_chunks(), batch=_batch(rows=35))
    stage.document_reads = [object()]
    block, *rest = _structured(svc, stage, map_block="m" * 300)
    room = PARTITION - PARTITION // 2 - 302
    assert rest[-1] is True
    assert ask_module.DOCUMENT_READ_OMITTED_NOTICE not in block
    assert len(block) <= room
