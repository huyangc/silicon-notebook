"""The passage floor: the structured side can no longer starve the reserved
passages of a reasoning synthesis -- PR-C, C4.

The Knowhow preview, the enumeration preview, document reads and the
spreadsheet block are assembled BEFORE the passage segment and share its
character partition (`chunk_context_chars`).  A structured block sized to the
partition used to leave the passages zero characters, voiding every reserved
seat.  `AskService._assemble_structured_evidence` now renders them against
`partition - floor`, `floor = min(partition // 2, sum(len(text) + 120))` over
the reserved-seat prefix (exact prefix + active prefix) of the very order
`_answer_reasoning` renders -- so each block's own coverage disclosure stays
true, and a run with no chunk or no reserved seat renders byte-for-byte as
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
from app.services.retrieval import PASSAGE_FLOOR_LINE_CHARS, passage_floor
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
    assert passage_floor(prefix, 10_000) == 150 + 2 * PASSAGE_FLOOR_LINE_CHARS
    assert passage_floor(prefix, 500) == 250
    assert passage_floor([], 10_000) == 0


def test_a_full_knowhow_block_leaves_the_reserved_prefix_its_characters(service):
    svc, nb = service
    chunks = _foreign_pool()
    order = reasoning_order_for(svc.settings, chunks, nb)
    prefix = {chunk.chunk_id for chunk in order.passages[:order.prefix]}
    assert {"exact-0", "mine-0", "mine-1", "mine-2"} <= prefix

    block, smap, *_ = _structured(svc, _stage(nb, chunks, batch=_batch()))
    floor = passage_floor(order.passages[:order.prefix], PARTITION)
    assert 0 < floor and len(block) <= PARTITION - floor
    # The Knowhow block's own disclosure is still true: partial.
    assert "synthesis_complete=false" in block
    rendered = _render(svc, nb, chunks, block, smap)
    assert prefix <= rendered, prefix - rendered


def test_control_without_the_floor_the_prefix_is_starved(service, monkeypatch):
    svc, nb = service
    monkeypatch.setattr(ask_module, "passage_floor", lambda prefix, chars: 0)
    chunks = _foreign_pool()
    block, smap, *_ = _structured(svc, _stage(nb, chunks, batch=_batch()))
    rendered = _render(svc, nb, chunks, block, smap)
    assert not {"mine-0", "mine-1", "mine-2"} <= rendered


def test_the_floor_covers_the_prefix_only_not_every_passage(service):
    svc, nb = service
    chunks = _foreign_pool()
    block, *_ = _structured(svc, _stage(nb, chunks, batch=_batch()))
    order = reasoning_order_for(svc.settings, chunks, nb)
    everything = passage_floor(order.passages, PARTITION)
    prefix_only = passage_floor(order.passages[:order.prefix], PARTITION)
    assert prefix_only < everything
    assert len(block) > PARTITION - everything


def test_the_map_block_is_held_back_too(service):
    svc, nb = service
    chunks = _foreign_pool()
    plain, *_ = _structured(svc, _stage(nb, chunks, batch=_batch()))
    with_map, *_ = _structured(svc, _stage(nb, chunks, batch=_batch()),
                               map_block="m" * 300)
    assert len(with_map) <= len(plain) - 300


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
    floor = passage_floor(order.passages[:order.prefix], PARTITION)
    block, smap, *_ = _structured(svc, _stage(nb, chunks, sheets=[_sheet()]))
    assert len(block) <= PARTITION - floor
    unbounded, *_ = _structured(svc, _stage(nb, [], sheets=[_sheet()]))
    assert len(unbounded) > PARTITION - floor, "the sheet must really overflow"
    assert "k6001" in smap
    rendered = _render(svc, nb, chunks, block, smap)
    assert {"exact-0", "mine-0"} <= rendered


# ------------------------------------------------ enumeration / document reads
def test_enumeration_and_document_read_budgets_use_the_reduced_partition(
    service, monkeypatch,
):
    """The enumeration sub-budget and the document-read caps are computed
    from ``partition - floor``, not from the whole partition."""
    from app.services import collection_enumeration_answer as enum_module

    svc, nb = service
    seen: dict = {}
    real_sub_budget = enum_module.enumeration_sub_budget

    def _sub_budget(**kwargs):
        seen["enumeration"] = kwargs["chunk_context_chars"]
        return real_sub_budget(**kwargs)

    monkeypatch.setattr(enum_module, "enumeration_sub_budget", _sub_budget)
    monkeypatch.setattr(enum_module, "typed_collection_results",
                        lambda *a, **k: [])
    monkeypatch.setattr(enum_module, "delivered_outcomes", lambda e, t: e)
    monkeypatch.setattr(enum_module, "apply_synthesis_preview_counts",
                        lambda *a, **k: None)
    monkeypatch.setattr(
        enum_module, "enumeration_prompt_block",
        lambda *a, **k: SimpleNamespace(text="", evidence_by_id={}, shown_rows={}))
    monkeypatch.setattr(svc.evidence_context, "collection_item_citations",
                        lambda *a, **k: {})
    real_read = AskService._assemble_document_read_block

    def _read(self, reads, block, smap, cites, client, chars, knowhow_len):
        seen["document_read"] = chars
        return real_read(self, reads, block, smap, cites, client, chars,
                         knowhow_len)

    monkeypatch.setattr(AskService, "_assemble_document_read_block", _read)
    chunks = _foreign_pool()
    stage = _stage(nb, chunks)
    stage.enumerations = [SimpleNamespace(items=[])]
    _structured(svc, stage)
    order = reasoning_order_for(svc.settings, chunks, nb)
    floor = passage_floor(order.passages[:order.prefix], PARTITION)
    assert floor > 0
    assert seen == {"enumeration": PARTITION - floor,
                    "document_read": PARTITION - floor}
    seen.clear()
    _structured(svc, _stage(nb, []))
    assert seen == {"document_read": PARTITION}
