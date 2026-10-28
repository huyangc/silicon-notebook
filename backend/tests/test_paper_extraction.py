"""``PaperExtractionService.extract`` (spec 2026-10-28 §6) on SQLite: papers of
the synthetic corpus (``tests/paper_topics_testkit.py``), the deterministic
``TopicEmbedder`` and a fake ``paper_metadata`` model behind the real scheduled
adapter (``paper_card_testkit.scheduled_paper_model``) -- no real model.

``CHUNK_TARGET_CHARS`` is small so every element of a fixture paper is its own
chunk: the per-field passage choice is then observable."""
from __future__ import annotations

import json
import re
import threading

import pytest

from app.core.config import Settings
from app.domain.cancellation import AskCancelled
from app.models.schemas import NotebookCreate
from app.services.model_work import ModelQueueFull
from app.services.paper_extraction import (
    EXTRACTION_SCHEMA_HINT,
    Excerpt,
    ExtractionRequestError,
    verify_extraction,
)
from app.services.sqlite_repository import SQLiteRepository
from tests import paper_card_testkit as card_kit
from tests.model_testkit import RecordingModelProvider, bind_all_embedding_clients
from tests.paper_topics_testkit import TOPIC_EMBED_DIM, TopicEmbedder, seed_paper_topics

_HANDLE_RE = re.compile(r"^\[([ec]\d+)\] (\w+)[^\n]*\n(.*?)(?=\n\n\[[ec]\d+\] |\Z)", re.MULTILINE | re.DOTALL)
_FIELD_RE = re.compile(r"^\[(\d+)\] (.+)$", re.MULTILINE)


def excerpts_of(prompt: str) -> dict[str, str]:
    body = prompt.split("Excerpts:\n", 1)[1]
    return {handle: text for handle, _kind, text in _HANDLE_RE.findall(body)}


def fields_of(prompt: str) -> list[str]:
    head = prompt.split("Fields:\n", 1)[1].split("\n\n", 1)[0]
    return [text for _index, text in _FIELD_RE.findall(head)]


class FakeExtractionModel:
    """``reply(prompt)`` returns the JSON object; the default answers every field
    whose words appear in a passage with that passage's first number."""

    def __init__(self) -> None:
        self.configured = True
        self.model = "fake-extract"
        self.prompts: list[str] = []
        self.hints: list[str] = []
        self.cancel_events: list = []
        self.on_call = None
        self.reply = self.default_reply
        self._lock = threading.Lock()

    @staticmethod
    def default_reply(prompt: str) -> dict:
        values = []
        excerpts = excerpts_of(prompt)
        for index, field in enumerate(fields_of(prompt)):
            words = [w for w in re.findall(r"[a-z]+", field.lower()) if len(w) > 3]
            for handle, text in excerpts.items():
                if handle.startswith("c") and all(w in text.lower() for w in words):
                    values.append({"field_index": index, "value": text.split("\n")[0][:80], "anchor": handle})
                    break
        return {"values": values}

    def chat_json(self, messages, schema_hint, **kwargs):
        prompt = messages[-1]["content"]
        with self._lock:
            self.prompts.append(prompt)
            self.hints.append(schema_hint)
            self.cancel_events.append(kwargs.get("cancel_event"))
        if self.on_call is not None:
            self.on_call(prompt)
        reply = self.reply(prompt)
        return reply if isinstance(reply, str) else json.dumps(reply)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", str(TOPIC_EMBED_DIM))
    monkeypatch.setenv("CHUNK_TARGET_CHARS", "60")
    provider = RecordingModelProvider(parallelism_by_workload={"paper_metadata": 4})
    instance = SQLiteRepository(Settings(), model_provider=provider)
    bind_all_embedding_clients(instance, TopicEmbedder())
    yield instance
    instance.close()


@pytest.fixture
def nb(repo) -> str:
    return repo.create_notebook(NotebookCreate(name="nb")).id


@pytest.fixture
def model(repo):
    fake = FakeExtractionModel()
    with card_kit.scheduled_paper_model(repo, fake):
        yield fake


@pytest.fixture
def service(repo):
    return repo._runtime.paper_extraction


@pytest.fixture
def events(service, monkeypatch):
    seen: list[dict] = []
    original = service._event_log.emit

    def capture(event, **kwargs):
        if event.get("kind") == "paper_extraction":
            seen.append(dict(event))
        else:
            original(event, **kwargs)

    monkeypatch.setattr(service._event_log, "emit", capture)
    return seen


def _extract(service, nb, sids, fields, *, limit=8, allowed=None, cancel_event=None):
    items = [(nb, sid) for sid in sids]
    return service.extract(
        items, fields, allowed=items if allowed is None else allowed,
        limit=limit, cancel_event=cancel_event, actor_id="u-1",
    )


def _elements(repo, sid) -> dict[str, str]:
    """element id -> text."""
    return {e.id: e.text for e in repo._runtime.source_store.source_elements(sid)}


# ------------------------------------------------------------ selection
def test_each_field_selects_its_own_passages(repo, nb, service, model, monkeypatch):
    keys = seed_paper_topics(repo, nb, keys=["det-05"])
    sid = keys["det-05"]
    retrieval = repo.retrieval
    calls: list[tuple] = []
    original = retrieval.section_chunk_similarities

    def spy(notebook_id, source_id, chunk_ids, query_texts):
        calls.append(tuple(query_texts))
        return original(notebook_id, source_id, chunk_ids, query_texts)

    monkeypatch.setattr(retrieval, "section_chunk_similarities", spy)
    fields = ["latency on phone in ms", "quantisation-aware training"]
    [result] = _extract(service, nb, [sid], fields)
    # One similarity call per field (the call takes the max over its texts).
    assert calls == [(fields[0],), (fields[1],)]
    assert result.status == "extracted" and result.lane == "vector"
    prompt = model.prompts[0]
    excerpts = excerpts_of(prompt)
    # The opening two elements ride along as e1/e2: title and abstract.
    assert excerpts["e1"].startswith("Real-Time Object Detection")
    assert excerpts["e2"].startswith("Abstract.")
    passages = [text for handle, text in excerpts.items() if handle.startswith("c")]
    assert any("Latency on phone" in text for text in passages)
    assert any("quantisation-aware training" in text for text in passages)
    assert len(passages) <= 8  # 4 per field, deduplicated
    assert model.hints == [EXTRACTION_SCHEMA_HINT]
    # Values point at the element of the cited passage, and keep the chunk id.
    elements = _elements(repo, sid)
    for value in result.fields:
        assert value.value and value.element_id in elements and value.chunk_id
        assert value.missing == ""


def _long_source(repo, nb, sid, keys) -> None:
    """One source made of several fixture papers back to back (many chunks)."""
    from app.repositories.ports import SourceElementWrite
    from tests.paper_topics_testkit import paper, paper_elements

    store = repo._runtime.source_store
    store.insert_source(
        source_id=sid, notebook_id=nb, title="Collected notes", source_type="document",
        status="parsed", parse_status="parsed", file_name=f"{sid}.pdf",
        file_path=f"/nonexistent/{sid}.pdf", file_size=0, file_hash=f"h-{sid}",
        summary="", doc_type="report",
    )
    elements = [
        SourceElementWrite(id=f"el-{sid}-{index:03d}", element_type=e.element_type,
                           location_label=e.location_label, text=e.text, metadata={})
        for index, e in enumerate(
            element for key in keys for element in paper_elements(paper(key), sid))
    ]
    with repo._write() as db:
        store.replace_elements(db, sid, elements, created_at="2026-01-01T00:00:00+00:00")
    repo._runtime.source_chunking.chunk_and_embed_source(sid)
    repo._runtime.knowledge_query.backfill_chunk_fts(nb)


def test_each_field_keeps_only_its_top_passages_of_a_long_source(repo, nb, service, model):
    _long_source(repo, nb, "long-1", ["det-05", "o-weather", "o-index", "o-splat", "o-compiler"])
    outline, _cut = repo.retrieval.section_outline(nb, "long-1")
    assert len(outline) > 12
    fields = ["latency on phone in ms", "lookup time of the learned index"]
    [result] = _extract(service, nb, ["long-1"], fields)
    passages = [t for h, t in excerpts_of(model.prompts[0]).items() if h.startswith("c")]
    assert len(passages) <= 12  # two fields share the 12 round robin (6 each at most)
    assert any("Latency on phone" in text for text in passages)
    assert any("Lookup time" in text for text in passages)
    assert len(passages) < len(outline)        # a selection, not the whole source
    assert result.lane == "vector"


def test_without_vectors_each_field_falls_back_to_this_sources_fts(repo, nb, service, model):
    keys = seed_paper_topics(repo, nb, keys=["asr-03", "asr-01"], embed=False)
    sid = keys["asr-03"]
    [result] = _extract(service, nb, [sid], ["latency of chunked attention"])
    assert result.status == "extracted" and result.lane == "fts"
    passages = [t for h, t in excerpts_of(model.prompts[0]).items() if h.startswith("c")]
    assert passages and any("Latency" in text for text in passages)
    # The fallback is restricted to this source: nothing of asr-01 shows up.
    assert not any("Conformer" in text for text in passages)


def test_the_outline_bound_is_reported(repo, nb, service, model, monkeypatch):
    keys = seed_paper_topics(repo, nb, keys=["rag-02"])
    monkeypatch.setattr(repo.settings, "reasoning_section_outline_max_rows", 2)
    [result] = _extract(service, nb, [keys["rag-02"]], ["top-20 accuracy"])
    assert result.outline_truncated is True


# ------------------------------------------------------------ verification
def _fixed(reply):
    return lambda prompt: reply(excerpts_of(prompt))


def _table_handle(excerpts: dict) -> str:
    return next(h for h, t in excerpts.items() if h.startswith("c") and t.startswith("|"))


def test_an_anchor_outside_the_input_drops_the_value(repo, nb, service, model, events):
    keys = seed_paper_topics(repo, nb, keys=["det-01"])
    model.reply = lambda prompt: {"values": [{"field_index": 0, "value": "36.2", "anchor": "c99"}]}
    [result] = _extract(service, nb, [keys["det-01"]], ["COCO box AP"])
    assert result.status == "extracted"
    assert (result.fields[0].value, result.fields[0].missing) == ("", "invalid_anchor")
    assert result.dropped == 1


def test_an_ungrounded_number_is_dropped_and_a_grounded_one_kept(repo, nb, service, model):
    keys = seed_paper_topics(repo, nb, keys=["det-01"])
    model.reply = _fixed(lambda ex: {"values": [
        {"field_index": 0, "value": "36.20 box AP on COCO", "anchor": _table_handle(ex)},
        {"field_index": 1, "value": "47.5", "anchor": _table_handle(ex)},
    ]})
    [result] = _extract(service, nb, [keys["det-01"]], ["COCO box AP", "AP small"])
    kept, dropped = result.fields
    assert kept.value == "36.20 box AP on COCO" and kept.chunk_id
    assert "36.2" in _elements(repo, keys["det-01"])[kept.element_id]
    assert (dropped.value, dropped.missing) == ("", "value_not_in_anchor")
    assert result.dropped == 1


def test_an_unstated_field_is_missing_not_failed(repo, nb, service, model):
    keys = seed_paper_topics(repo, nb, keys=["det-01"])
    model.reply = lambda prompt: {"values": []}
    [result] = _extract(service, nb, [keys["det-01"]], ["parameter count"])
    assert result.status == "extracted"
    assert result.fields[0].missing == "not_stated"


def test_verify_extraction_rules():
    excerpts = [
        Excerpt("e1", "element", "el-a", "", "", "Title"),
        Excerpt("c1", "chunk", "el-t", "ch-1", "", "| Ours | 85.2 ± 0.3 | 12 layers |"),
        Excerpt("c2", "chunk", "", "ch-2", "", "no element 4.5"),
    ]
    parsed = {"values": [
        {"field_index": 0, "value": "85.2±0.3 with 12 layers", "anchor": "c1"},
        {"field_index": 0, "value": "ignored second value", "anchor": "c1"},
        {"field_index": 1, "value": "3", "anchor": "c1"},
        {"field_index": 2, "value": "4.5", "anchor": "c2"},
        {"field_index": 3, "value": "Title", "anchor": "el-a"},
        {"field_index": 9, "value": "out of range", "anchor": "c1"},
        "not an object",
    ]}
    values, dropped = verify_extraction(parsed, ["a", "b", "c", "d"], excerpts)
    assert [(v.value, v.element_id, v.chunk_id, v.missing) for v in values] == [
        ("85.2±0.3 with 12 layers", "el-t", "ch-1", ""),
        ("", "", "", "ambiguous_value"),
        ("", "", "", "unmapped_anchor"),
        ("Title", "el-a", "", ""),
    ]
    assert len(dropped["bad_field_index"]) == 1


@pytest.mark.parametrize(("value", "text", "verdict"), [
    ("no numbers here", "anything", ""),
    ("85.2±0.3 with 12 layers", "Ours 85.20 ± 0.3, a 12-layer stack", ""),
    ("12 layers on ImageNet-1k", "12 layers, trained on ImageNet-1k", ""),
    ("3 datasets", "we use 3 datasets", ""),          # a one-digit number inside prose
    ("3", "we use 3 datasets", "ambiguous_value"),    # a bare one-digit value
    ("3x", "3x faster", "ambiguous_value"),
    ("85.2 top-1", "185.23 top-1", "value_not_in_anchor"),
    ("1.2e-3 learning rate", "lr 1.2e-3", ""),
    ("1.2e-3 learning rate", "lr 1.2 and 3", "value_not_in_anchor"),
    ("0.5 and 47.5", "0.5 only", "value_not_in_anchor"),
])
def test_prose_numbers_are_grounded_run_by_run(value, text, verdict):
    from app.services.paper_cards import ground_prose_numbers

    assert ground_prose_numbers(value, text) == verdict


# ------------------------------------------------------------ scope
def test_a_non_paper_is_extracted_too(repo, nb, service, model):
    card_kit.seed_source(repo, nb, "doc-1", doc_type="meeting_notes", filler=2)
    card_kit.set_paper_meta(repo, nb, "doc-1", is_paper=False)
    repo._runtime.source_chunking.chunk_and_embed_source("doc-1")
    model.reply = _fixed(lambda ex: {"values": [
        {"field_index": 0, "value": "85.20", "anchor": _table_handle(ex)},
    ]})
    [result] = _extract(service, nb, ["doc-1"], ["Top-1 of ours"])
    assert result.status == "extracted"
    assert result.fields[0].value == "85.20"


def test_sources_outside_the_allowed_set_or_missing_are_refused(repo, nb, service, model, events):
    keys = seed_paper_topics(repo, nb, keys=["det-01", "det-02"])
    other = repo.create_notebook(NotebookCreate(name="other")).id
    items = [(nb, keys["det-01"]), (nb, keys["det-02"]), (nb, "ghost"), (other, keys["det-01"])]
    results = service.extract(
        items, ["COCO box AP"], allowed=[items[0], items[2], items[3]], limit=8,
    )
    assert [(r.status, r.reason) for r in results] == [
        ("extracted", ""), ("not_found", "out_of_scope"),
        ("not_found", "missing"), ("not_found", "missing"),
    ]
    assert len(model.prompts) == 1


def test_the_limit_defers_the_rest_in_input_order(repo, nb, service, model):
    keys = seed_paper_topics(repo, nb, keys=["det-01", "det-02", "det-03"])
    sids = [keys["det-03"], keys["det-01"], keys["det-02"]]
    results = _extract(service, nb, sids, ["COCO box AP"], limit=2)
    assert [r.status for r in results] == ["extracted", "extracted", "deferred"]
    assert results[2].reason == "limit"


@pytest.mark.parametrize("fields", [
    [], ["  "], ["a", "b", "c", "d", "e"], ["x" * 81],
], ids=["none", "blank", "five", "too_long"])
def test_malformed_requests_are_refused(repo, nb, service, model, fields):
    with pytest.raises(ExtractionRequestError):
        _extract(service, nb, ["s"], fields)


def test_more_than_eight_sources_are_refused(repo, nb, service, model):
    with pytest.raises(ExtractionRequestError):
        _extract(service, nb, [f"s{i}" for i in range(9)], ["field"])


# ------------------------------------------------------------ failures
@pytest.mark.parametrize("error", [
    lambda: ModelQueueFull(),
    lambda: card_kit.StatusError(429),
    lambda: card_kit.StatusError(503),
    lambda: TimeoutError("read timed out"),
], ids=["queue_full", "rate_limited", "unavailable_5xx", "timeout"])
def test_transient_model_failures_defer(repo, nb, service, model, error):
    keys = seed_paper_topics(repo, nb, keys=["det-01"])

    def busy(prompt):
        raise error()

    model.reply = busy
    [result] = _extract(service, nb, [keys["det-01"]], ["COCO box AP"])
    assert (result.status, result.reason) == ("deferred", "model_unavailable")


@pytest.mark.parametrize(("error", "reason"), [
    (lambda: card_kit.StatusError(400), "model_error:provider_error"),
    (lambda: card_kit.StatusError(401), "model_error:provider_auth"),
], ids=["http_400", "auth"])
def test_deterministic_model_failures_fail_without_a_row(repo, nb, service, model, error, reason):
    keys = seed_paper_topics(repo, nb, keys=["det-01"])

    def fail(prompt):
        raise error()

    model.reply = fail
    [result] = _extract(service, nb, [keys["det-01"]], ["COCO box AP"])
    assert (result.status, result.reason) == ("failed", reason)


def test_an_unparsable_reply_fails(repo, nb, service, model, monkeypatch):
    keys = seed_paper_topics(repo, nb, keys=["det-01"])
    model.reply = lambda prompt: "not json at all"
    [result] = _extract(service, nb, [keys["det-01"]], ["COCO box AP"])
    # The scheduled adapter refuses it with a stable code.
    assert (result.status, result.reason) == ("failed", "malformed_response")
    # A client that hands the text through is judged by the service itself.
    monkeypatch.setattr(service._models, "chat", lambda workload: model)
    model.reply = lambda prompt: "[1, 2]"
    [result] = _extract(service, nb, [keys["det-01"]], ["COCO box AP"])
    assert (result.status, result.reason) == ("failed", "malformed")


def test_results_are_not_cached(repo, nb, service, model):
    keys = seed_paper_topics(repo, nb, keys=["det-01"])
    _extract(service, nb, [keys["det-01"]], ["COCO box AP"])
    _extract(service, nb, [keys["det-01"]], ["COCO box AP"])
    assert len(model.prompts) == 2


# ------------------------------------------------------------ concurrency
def test_cancellation_propagates_and_emits_finished_sources(repo, nb, service, model, events, monkeypatch):
    keys = seed_paper_topics(repo, nb, keys=["det-01", "det-02"])
    monkeypatch.setattr(repo.settings, "paper_card_max_workers", 1)
    cancel = threading.Event()
    model.on_call = lambda prompt: cancel.set()
    with pytest.raises(AskCancelled):
        _extract(service, nb, [keys["det-01"], keys["det-02"]], ["COCO box AP"], cancel_event=cancel)
    # One worker: the second source is never sent once the first call cancelled.
    assert len(model.prompts) == 1
    assert all(event is cancel for event in model.cancel_events)
    # Whatever finished before the cancellation is still accounted for.
    assert [e["status"] for e in events] in ([], ["extracted"])


def test_extract_refuses_to_run_inside_a_fanout_slot(repo, nb, service, model):
    from app.services.retrieval_run import retrieval_fanout_slot, retrieval_run

    keys = seed_paper_topics(repo, nb, keys=["det-01"])
    with retrieval_run(run_kind="test", fanout_limit=2):
        with retrieval_fanout_slot():
            with pytest.raises(RuntimeError, match="retrieval_fanout_slot"):
                _extract(service, nb, [keys["det-01"]], ["COCO box AP"])
    assert model.prompts == []


def test_reads_run_inside_fanout_slots_and_parallel_workers(repo, nb, service, model, monkeypatch):
    from app.services.retrieval_run import holding_fanout_slot, retrieval_run

    keys = seed_paper_topics(repo, nb, keys=["det-01", "det-02", "det-03"])
    retrieval = repo.retrieval
    seen: list[bool] = []
    original = retrieval.section_outline

    def spy(notebook_id, source_id):
        seen.append(holding_fanout_slot())
        return original(notebook_id, source_id)

    monkeypatch.setattr(retrieval, "section_outline", spy)
    # fanout_limit=1: the reads take the one slot and release it before the
    # model call, so three sources still finish (no self-deadlock).
    with retrieval_run(run_kind="test", fanout_limit=1):
        results = _extract(service, nb, list(keys.values()), ["COCO box AP"])
    assert [r.status for r in results] == ["extracted"] * 3
    assert seen == [True, True, True]
    assert len(model.prompts) == 3


def test_one_event_per_source_carries_ids_and_codes_only(repo, nb, service, model, events):
    keys = seed_paper_topics(repo, nb, keys=["det-01"])
    _extract(service, nb, [keys["det-01"], "ghost"], ["COCO box AP"], allowed=[(nb, keys["det-01"])])
    assert [(e["source_id"], e["status"], e["reason"]) for e in events] == [
        (keys["det-01"], "extracted", ""), ("ghost", "not_found", "out_of_scope"),
    ]
    first = events[0]
    assert first["actor_id"] == "u-1" and first["fields"] == 1 and first["extracted"] == 1
    text = json.dumps(events)
    assert "36.2" not in text and "COCO box AP" not in text


# ------------------------------------------------------------ review round 1
_FOUR_FIELDS = {
    "latency on phone in ms": "Latency on phone",
    "lookup time of the learned index": "Lookup time",
    "CRPS of precipitation downscaling": "CRPS",
    "PSNR and FPS of gaussian splatting": "PSNR",
}


def _chunk_texts(repo, nb, sid) -> dict[str, str]:
    retrieval = repo.retrieval
    ids = [row[0] for row in retrieval.section_outline(nb, sid)[0]]
    out = {}
    for start in range(0, len(ids), 10):
        for chunk in retrieval.section_chunks(nb, sid, ids[start:start + 10], 10, ""):
            out[chunk.chunk_id] = chunk.text.strip()
    return out


def test_four_fields_share_the_passage_quota_round_robin(repo, nb, service, model):
    _long_source(repo, nb, "long-4", ["det-05", "o-index", "o-weather", "o-splat", "o-compiler"])
    texts = _chunk_texts(repo, nb, "long-4")
    [result] = _extract(service, nb, ["long-4"], list(_FOUR_FIELDS))
    passages = {t.strip() for h, t in excerpts_of(model.prompts[0]).items() if h.startswith("c")}
    assert len(passages) <= 12 and result.status == "extracted"
    for field, marker in _FOUR_FIELDS.items():
        assert any(marker in text for text in passages), field
        sims = repo.retrieval.section_chunk_similarities(nb, "long-4", list(texts), [field])
        top = [cid for cid, _s in sorted(sims.items(), key=lambda kv: (-kv[1], kv[0]))][:12 // 4]
        # Every field keeps at least 12 // n_fields of its own best passages.
        assert all(texts[cid] in passages for cid in top), field


def test_the_section_read_bound_never_drops_a_fields_pick(repo, nb, service, model, monkeypatch):
    _long_source(repo, nb, "long-2", ["det-05", "o-weather", "o-compiler", "o-index"])
    monkeypatch.setattr(repo.settings, "reasoning_section_read_max_chunks", 2)
    _extract(service, nb, ["long-2"], ["latency on phone in ms", "lookup time of the learned index"])
    passages = [t for h, t in excerpts_of(model.prompts[0]).items() if h.startswith("c")]
    assert any("Latency on phone" in text for text in passages)
    assert any("Lookup time" in text for text in passages)


_RESULTS_TEXTS = [
    "Results",
    "We evaluate the detector on COCO with the standard protocol.",
    "Our detector reaches 36.2 box AP on COCO val, 4.6 above the baseline.",
    "Conclusion: feature pyramids help small objects.",
]


def _default_chunked_source(repo, nb, sid, monkeypatch) -> list[str]:
    """Elements chunked with the DEFAULT chunk size: one multi-element chunk.
    Returns the element ids of the passage that holds them."""
    default = Settings.model_fields["chunk_target_chars"].default
    monkeypatch.setattr(repo.settings, "chunk_target_chars", default)
    _plain_source(repo, nb, sid, _RESULTS_TEXTS)
    repo._runtime.source_chunking.chunk_and_embed_source(sid)
    ids = [row[0] for row in repo.retrieval.section_outline(nb, sid)[0]]
    [chunk] = repo.retrieval.section_chunks(nb, sid, ids, 12, "")
    return list(chunk.element_ids)


def test_a_value_cites_the_element_of_the_passage_that_holds_it(repo, nb, service, model, monkeypatch):
    element_ids = _default_chunked_source(repo, nb, "res-1", monkeypatch)
    assert element_ids == [f"el-res-1-{i:03d}" for i in range(4)]  # heading + paragraphs, one chunk
    model.reply = lambda prompt: {"values": [{"field_index": 0, "value": "36.2 box AP", "anchor": "c1"}]}
    [result] = _extract(service, nb, ["res-1"], ["COCO box AP of the detector"])
    value = result.fields[0]
    assert value.value == "36.2 box AP" and value.chunk_id
    assert value.element_id == "el-res-1-002"      # the paragraph with the number, not the heading


def test_an_element_id_in_the_opening_and_a_passage_validates_against_both(repo, nb, service, model, monkeypatch):
    _default_chunked_source(repo, nb, "res-2", monkeypatch)
    # el-res-2-001 is the opening e2 (no number) AND part of the passage c1 (which
    # shows the number): the passage the model saw vouches for the value.
    model.reply = lambda prompt: {"values": [
        {"field_index": 0, "value": "36.2", "anchor": "el-res-2-001"}]}
    [result] = _extract(service, nb, ["res-2"], ["COCO box AP of the detector"])
    assert result.fields[0].value == "36.2", result.fields[0].missing
    assert result.fields[0].element_id == "el-res-2-002"


class _CountingEmbedder(TopicEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.queries: list[str] = []

    def embed_query(self, text):
        self.queries.append(text)
        return super().embed_query(text)


def test_outside_a_run_each_field_is_embedded_once_and_each_outline_read_once(repo, nb, service, model, monkeypatch):
    keys = seed_paper_topics(repo, nb, keys=["det-01", "det-02", "det-03"])
    embedder = _CountingEmbedder()
    bind_all_embedding_clients(repo, embedder)
    chunks = repo._runtime.chunk_store
    outline_reads: list[str] = []
    original = chunks.source_outline_rows

    def counting(db, notebook_id, source_id, max_rows):
        outline_reads.append(source_id)
        return original(db, notebook_id, source_id, max_rows)

    monkeypatch.setattr(chunks, "source_outline_rows", counting)
    fields = ["COCO box AP", "frames per second"]
    results = _extract(service, nb, list(keys.values()), fields)
    assert [r.status for r in results] == ["extracted"] * 3
    assert sorted(q for q in embedder.queries if q in fields) == sorted(fields)
    assert sorted(outline_reads) == sorted(keys.values())


def test_without_vectors_the_first_field_decides_and_the_rest_go_straight_to_fts(repo, nb, service, model, monkeypatch):
    keys = seed_paper_topics(repo, nb, keys=["asr-03"], embed=False)
    retrieval = repo.retrieval
    calls: list = []
    original = retrieval.section_chunk_similarities

    def spy(*args):
        calls.append(args[3])
        return original(*args)

    monkeypatch.setattr(retrieval, "section_chunk_similarities", spy)
    seen: list[dict] = []
    emit = service._event_log.emit
    monkeypatch.setattr(service._event_log, "emit", lambda e, **k: (
        seen.append(dict(e)) if e.get("kind") == "section_vectors_unavailable" else emit(e, **k)))
    [result] = _extract(service, nb, [keys["asr-03"]], ["latency", "word error rate", "chunk size"])
    assert result.lane == "fts"
    assert len(calls) == 1
    assert len(seen) <= 1


def _plain_source(repo, nb, sid, texts, *, doc_type="report") -> None:
    from app.repositories.ports import SourceElementWrite

    store = repo._runtime.source_store
    store.insert_source(
        source_id=sid, notebook_id=nb, title=f"Doc {sid}", source_type="document",
        status="parsed", parse_status="parsed", file_name=f"{sid}.md",
        file_path=f"/nonexistent/{sid}.md", file_size=0, file_hash=f"h-{sid}",
        summary="", doc_type=doc_type,
    )
    elements = [
        SourceElementWrite(id=f"el-{sid}-{i:03d}", element_type="text",
                           location_label=f"p{i}", text=text, metadata={})
        for i, text in enumerate(texts)
    ]
    with repo._write() as db:
        store.replace_elements(db, sid, elements, created_at="2026-01-01T00:00:00+00:00")
    repo._runtime.source_chunking.build_chunks_for_source(sid)


_NOISE = "inference latency budget " * 6


def test_the_fts_fallback_keeps_this_sources_passages_among_stronger_neighbours(repo, nb, service, model):
    _plain_source(repo, nb, "target", [
        "Opening title.", "Opening abstract.", "Unrelated background paragraph.",
        "We measure the inference latency budget at 12 ms on device.",
    ])
    for i in range(20):
        _plain_source(repo, nb, f"noise-{i:02d}", [_NOISE])
    repo._runtime.knowledge_query.backfill_chunk_fts(nb)
    [result] = _extract(service, nb, ["target"], ["inference latency budget"])
    passages = [t for h, t in excerpts_of(model.prompts[0]).items() if h.startswith("c")]
    assert result.lane == "fts"
    assert any("12 ms" in text for text in passages)


def test_the_fts_fallback_ignores_hits_outside_the_bounded_outline(repo, nb, service, model, monkeypatch):
    _plain_source(repo, nb, "capped", [
        "Opening title.", "Opening abstract.",
        "We measure the inference latency budget at 12 ms on device.",
        *[_NOISE for _ in range(14)],
    ])
    repo._runtime.knowledge_query.backfill_chunk_fts(nb)
    monkeypatch.setattr(repo.settings, "reasoning_section_outline_max_rows", 3)
    [result] = _extract(service, nb, ["capped"], ["inference latency budget"])
    passages = [t for h, t in excerpts_of(model.prompts[0]).items() if h.startswith("c")]
    assert result.outline_truncated is True
    assert any("12 ms" in text for text in passages)


def test_an_overlong_value_is_cut_before_grounding_and_never_inside_a_number(repo, nb, service, model):
    keys = seed_paper_topics(repo, nb, keys=["det-01"])
    long_value = "x" * 297 + "36.2 box AP"
    model.reply = _fixed(lambda ex: {"values": [
        {"field_index": 0, "value": long_value, "anchor": _table_handle(ex)}]})
    [result] = _extract(service, nb, [keys["det-01"]], ["COCO box AP"])
    value = result.fields[0]
    # Cut at 300 would leave "36." (a different number); the cut backs off.
    assert value.value == "x" * 297
    assert len(value.value) <= 300


@pytest.mark.parametrize(("value", "text"), [
    ("ResNet-50: 76.1% top-1", "| ResNet-50 | 76.1 | top-1 |"),
    ("mAP@0.5: 42.1", "| mAP@0.5 | 42.1 |"),
    ("COCO 2017: 42.1 AP", "COCO 2017 val. Ours reaches 42.1 AP"),
    ("42.1 / 38.5 (val/test)", "| Ours | 42.1 | 38.5 |"),
])
def test_settings_and_numbers_ground_as_separate_runs(value, text):
    from app.services.paper_cards import ground_prose_numbers

    assert ground_prose_numbers(value, text) == ""


class _Coded(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@pytest.mark.parametrize(("error", "expected"), [
    (_Coded("malformed_response"), ("malformed_response", True)),
    (_Coded("model_context_exceeded"), ("model_error:model_context_exceeded", True)),
    (_Coded("provider_error"), ("model_error:provider_error", True)),
    (_Coded("provider_auth"), ("model_error:provider_auth", False)),
    (ValueError("no code"), ("model_error:ValueError", False)),
])
def test_one_model_failure_classification_for_cards_and_extraction(error, expected):
    from app.services.model_work import model_failure_reason

    assert model_failure_reason(error) == expected



def test_an_extraction_failure_logs_only_the_class_and_code(repo, nb, service, model, monkeypatch):
    secret = "sk-LEAK-0000 https://user:pw@provider.example/v1"
    calls = []

    class Recorder:
        def __getattr__(self, method):
            return lambda msg, *args, **kwargs: calls.append((method, msg % args if args else msg, kwargs))

    card_kit.seed_source(repo, nb, "s1", doc_type="meeting_notes", filler=2)
    monkeypatch.setattr(service._event_log, "logger", Recorder())

    def boom(key, fields, cancel_event):
        try:
            raise ValueError(secret)
        except ValueError as original:
            raise RuntimeError(f"wrapped {secret}") from original

    monkeypatch.setattr(service, "_extract_one", boom)
    [result] = _extract(service, nb, ["s1"], ["accuracy"])
    assert (result.status, result.reason) == ("failed", "error:RuntimeError")
    assert calls and all(method != "exception" and not kwargs.get("exc_info") for method, _t, kwargs in calls)
    assert not any("LEAK" in text or "user:pw" in text for _m, text, _k in calls)
