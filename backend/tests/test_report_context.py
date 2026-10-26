"""Hermetic behavior tests for report context reduction and source continuity."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.core.model_context import admit_request
from app.domain.cancellation import AskCancelled
from app.services.report_context import (
    EvidenceUnit,
    ReportContextBudgetExceeded,
    ReportContextOverflow,
    ReportContextSession,
)


class CharacterBudget:
    counting_method = "conservative_test"
    max_output_tokens = 100

    def __init__(self, maximum):
        self.max_input_tokens = maximum

    def input_tokens(self, messages, schema_hint):
        return sum(len(message["content"]) for message in messages)

    def fits(self, messages, schema_hint):
        return self.input_tokens(messages, schema_hint) <= self.max_input_tokens


class FakeClient:
    def __init__(self, limit, response=None, *, cache=False, attempts=1):
        self.context_budget = CharacterBudget(limit)
        self.response = response
        self.cache = cache
        self.attempts = attempts
        self.calls = []
        self.sent = 0

    def chat_json(self, messages, schema_hint, **kwargs):
        self.calls.append(messages)
        if not self.cache:
            for _ in range(self.attempts):
                admit_request()
                self.sent += 1
        value = self.response(messages) if callable(self.response) else self.response
        return json.dumps(value)


def settings(**kwargs):
    return SimpleNamespace(**{
        "report_context_extra_calls_max": 24,
        "report_context_compression_max_levels": 2,
        "report_context_compress_max_tokens": 8192,
        **kwargs,
    })


def render(units):
    return [{"role": "user", "content": "Fixed confirmed question and outline.\n" + "\n".join(
        f"[{unit.handle}] {unit.text}" for unit in units)}]


def card(handle, quote, **kwargs):
    return {"statement": "Supported finding", "evidence": [{"handle": handle, "quote": quote}],
            "conditions": [], "counterevidence": [], "inference": False, "general": False,
            **kwargs}


def project(session, target, units, compressor, **kwargs):
    return session.compress_to_fit(target, "{}", render, units,
                                   compressor_factory=lambda _: compressor,
                                   stage="draft", **kwargs)


def test_request_that_fits_is_byte_identical_and_uses_no_compressor():
    units = (EvidenceUnit("a", "  原文 1.2 V\n\nline\t table  "),)
    compressor = FakeClient(5000)
    session = ReportContextSession(settings())
    projection = project(session, FakeClient(1000), units, compressor)
    assert projection.units is units
    assert render(projection.units) == render(units)
    assert not compressor.calls
    assert session.receipt()["extra_calls"] == 0
    assert session.receipt()["estimated_counting"]


def test_one_exhausted_material_does_not_block_fresh_material_compression():
    units = (EvidenceUnit("a", "Already compact", level=2, raw_text=True),
             EvidenceUnit("b", "raw material " * 100 + "fresh fact"))
    compressor = FakeClient(10000, {"cards": [card("b", "fresh fact")]})
    session = ReportContextSession(settings())
    projection = project(session, FakeClient(500), units, compressor)
    assert set(projection.source_handles) == {"a", "b"}
    assert next(unit for unit in projection.units if unit.handle == "a").level == 2
    assert any(unit.level == 1 and unit.original_handles() == ("b",) for unit in projection.units)
    assert session.receipt()["extra_calls"] == 1


def test_fixed_user_contract_never_shortened_even_when_empty_evidence_cannot_fit():
    compressor = FakeClient(5000)
    with pytest.raises(ReportContextOverflow, match="研究问题"):
        project(ReportContextSession(settings()), FakeClient(1),
                (EvidenceUnit("a", "source"),), compressor)
    assert not compressor.calls


def test_exact_dedup_retains_both_independent_source_handles():
    units = (EvidenceUnit("a", "x" * 200), EvidenceUnit("b", "x" * 200))
    compressor = FakeClient(5000)
    result = project(ReportContextSession(settings()), FakeClient(300), units, compressor)
    assert result.source_handles == ("a", "b")
    assert not result.omitted_handles
    assert len(result.units) == 1
    assert not compressor.calls


def test_raw_exact_duplicates_keep_genuine_source_aliases_after_compression():
    original = "The specific result is 1.2 V. " + "background " * 200
    units = (EvidenceUnit("a", original), EvidenceUnit("b", original))
    compressor = FakeClient(10000, {"cards": [card("a", "The specific result is 1.2 V.")]})
    result = project(ReportContextSession(settings()), FakeClient(800), units, compressor)
    assert result.source_handles == ("a", "b")
    assert result.units[0].quotes == (
        ("a", "The specific result is 1.2 V."),
        ("b", "The specific result is 1.2 V."),
    )


@pytest.mark.parametrize("second_handle", ["a", "b"])
def test_multistage_compression_keeps_each_original_quote_bound_to_its_handle(second_handle):
    shared = "The specific result is 1.2 V."
    other = "The other condition is 25 C."
    units = (
        EvidenceUnit("a", shared + " first source details " * 100),
        EvidenceUnit("b", other + " second source details " * 100 + shared),
    )
    replies = []

    def response(messages):
        replies.append(messages)
        if len(replies) == 1:
            # Both raw sources contain the shared sentence, but layer one
            # forwards it under A only. B forwards a different original quote.
            return {"cards": [card("a", shared, statement="summary " * 150,
                                   evidence=[{"handle": "a", "quote": shared},
                                             {"handle": "b", "quote": other}])]}
        return {"cards": [card(second_handle, shared)]}

    session = ReportContextSession(settings())
    compressor = FakeClient(10000, response)
    if second_handle == "b":
        with pytest.raises(ReportContextOverflow):
            project(session, FakeClient(800), units, compressor)
        assert session.receipt()["stages"]["draft"]["invalid_items"] == 1
    else:
        result = project(session, FakeClient(800), units, compressor)
        assert result.max_level == 2
        assert result.source_handles == ("a",)
        assert result.omitted_handles == ("b",)
        assert result.units[0].quotes == (("a", shared),)
    assert len(replies) == 2


def test_compression_preserves_original_quotes_conditions_and_inference_markers():
    unit = EvidenceUnit("a", "Measured 1.2 V at 25 C. " + "details. " * 200,
                        conditions=("Only at 25 C",), counterevidence=("Not established at 80 C",),
                        inference=True, general=True, provenance=(("version", "v3"),))
    compressor = FakeClient(5000, {"cards": [card("a", "Measured 1.2 V at 25 C.")]})
    session = ReportContextSession(settings())
    result = project(session, FakeClient(800), (unit,), compressor)
    assert result.compressed
    assert result.max_level == 1
    assert result.source_handles == ("a",)
    assert result.partial_handles == ("a",)
    reduced = result.units[0]
    assert reduced.quotes == (("a", "Measured 1.2 V at 25 C."),)
    assert reduced.conditions == unit.conditions
    assert reduced.counterevidence == unit.counterevidence
    assert reduced.inference and reduced.general
    assert reduced.provenance == unit.provenance
    assert reduced.payload()["original_handles"] == ("a",)
    receipt = session.receipt()
    assert receipt["extra_calls"] == 1
    assert receipt["max_level"] == 1
    assert receipt["stages"]["draft"]["compressed"]
    assert "Measured" not in json.dumps(receipt)
    assert "v3" not in json.dumps(receipt)


@pytest.mark.parametrize("bad_card", [
    card("outside", "Original"),
    card("a", "A fabricated paraphrase"),
    card("a", ""),
    card("a", "Original", conditions="not a list"),
    card("a", "Original", inference="false"),
])
def test_illegal_source_or_nonliteral_quote_never_enters_projection(bad_card):
    units = (EvidenceUnit("a", "Original " + "long " * 200), EvidenceUnit("b", "valid short source"))
    compressor = FakeClient(5000, {"cards": [bad_card]})
    session = ReportContextSession(settings())
    result = project(session, FakeClient(400), units, compressor)
    assert result.source_handles == ("b",)
    assert result.omitted_handles == ("a",)
    assert not result.compressed
    assert session.receipt()["stages"]["draft"]["invalid_items"] == 1


def test_layer_limit_and_inherited_provenance_cannot_be_reset_by_model():
    unit = EvidenceUnit("a", "Original " + "long " * 200)
    compressor = FakeClient(5000, {"cards": [card("a", "Original", statement="x" * 400)]})
    session = ReportContextSession(settings())
    with pytest.raises(ReportContextOverflow):
        project(session, FakeClient(200), (unit,), compressor)
    assert compressor.sent == 2
    assert session.receipt()["max_level"] == 2
    assert session.receipt()["stages"]["draft"]["omitted_items"] == 1


def test_derived_input_level_prevents_additional_model_layers():
    compressor = FakeClient(5000)
    unit = EvidenceUnit("a", "x" * 1000, level=2)
    with pytest.raises(ReportContextOverflow):
        project(ReportContextSession(settings()), FakeClient(200), (unit,), compressor)
    assert not compressor.calls


def test_second_layer_cannot_quote_original_text_that_first_layer_did_not_receive():
    calls = []

    def response(messages):
        calls.append(messages)
        quote = "Original" if len(calls) == 1 else "hidden second fact"
        return {"cards": [card("a", quote, statement="x" * 400)]}

    units = (EvidenceUnit("a", "Original; hidden second fact " + "long " * 200),
             EvidenceUnit("b", "retained fallback"))
    compressor = FakeClient(5000, response)
    session = ReportContextSession(settings())
    result = project(session, FakeClient(200), units, compressor)
    assert result.source_handles == ("b",)
    assert result.omitted_handles == ("a",)
    assert session.receipt()["stages"]["draft"]["invalid_items"] == 1
    assert len(calls) == 2


def test_quota_exhaustion_selects_balanced_whole_units_and_discloses_omissions():
    units = (EvidenceUnit("a1", "x" * 50, group="A"),
             EvidenceUnit("a2", "z" * 50, group="A"),
             EvidenceUnit("b1", "y" * 50, group="B"))
    compressor = FakeClient(5000)
    session = ReportContextSession(settings(report_context_extra_calls_max=0))
    result = project(session, FakeClient(170), units, compressor)
    assert result.source_handles == ("a1", "b1")
    assert result.omitted_handles == ("a2",)
    assert not compressor.calls
    assert session.receipt()["stages"]["draft"]["omitted_items"] == 1


def test_valid_summary_cannot_silently_drop_an_unrepresented_research_group():
    units = (EvidenceUnit("a", "Original " + "long " * 200, group="A"),
             EvidenceUnit("b", "Critical contradictory result", group="B"))
    compressor = FakeClient(5000, {"cards": [card("a", "Original")]})
    result = project(ReportContextSession(settings()), FakeClient(600), units, compressor)
    assert set(result.source_handles) == {"a", "b"}
    assert any(unit.text == "Critical contradictory result" for unit in result.units)
    assert not result.omitted_handles


def test_admission_counts_each_failed_retry_and_stops_before_next_send():
    compressor = FakeClient(5000, {"cards": [card("a", "Original")]}, attempts=3)
    session = ReportContextSession(settings(report_context_extra_calls_max=2))
    units = (EvidenceUnit("a", "Original " + "long " * 200), EvidenceUnit("b", "short fallback"))
    result = project(session, FakeClient(300), units, compressor)
    assert result.source_handles == ("b",)
    assert compressor.sent == 2
    assert session.receipt()["extra_calls"] == 2


def test_cache_only_compression_does_not_consume_extra_transport_budget():
    compressor = FakeClient(5000, {"cards": [card("a", "Original")]}, cache=True)
    session = ReportContextSession(settings())
    result = project(session, FakeClient(500),
                     (EvidenceUnit("a", "Original " + "long " * 200),), compressor)
    assert result.compressed
    assert session.receipt()["extra_calls"] == 0


def test_reportwide_admission_is_atomic_across_parallel_chapters_and_restarts():
    notifications = []
    seed = {"extra_calls": 2, "stages": {"planning_intent": {"input_items": 1}}}
    session = ReportContextSession(settings(report_context_extra_calls_max=7), seed,
                                   on_change=notifications.append)

    def admit(_):
        try:
            session.admit_attempt("draft")
            return True
        except ReportContextBudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=8) as executor:
        assert sum(executor.map(admit, range(50))) == 5
    assert session.receipt()["extra_calls"] == 7
    assert len(notifications) == 5
    assert session.receipt()["stages"]["planning_intent"]["input_items"] == 1


def test_cancellation_stops_before_any_extra_request():
    cancel = threading.Event()
    cancel.set()
    compressor = FakeClient(5000)
    session = ReportContextSession(settings(), cancel_event=cancel)
    with pytest.raises(AskCancelled):
        project(session, FakeClient(200), (EvidenceUnit("a", "x" * 1000),), compressor)
    assert not compressor.calls


def test_oversized_source_extracts_whole_sentences_and_keeps_decimal_units():
    received = []

    def response(messages):
        payload = json.loads(messages[-1]["content"])
        received.extend(item["text"] for item in payload)
        return {"cards": [card("a", "At 1.2 V the result is stable.")]}

    # Separate sentences keep the compressor request bounded; no source prefix
    # or table row is cut at a character count.
    text = "At 1.2 V the result is stable. " + "Complete result. " * 500
    session = ReportContextSession(settings())
    result = project(session, FakeClient(700), (EvidenceUnit("a", text),), FakeClient(1800, response))
    assert result.source_handles == ("a",)
    assert result.partial_handles == ("a",)
    assert any("At 1.2 V" in item for item in received)
    assert not any(item.endswith("1.") for item in received)
