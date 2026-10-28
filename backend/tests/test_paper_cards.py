"""Paper card pure functions (``app.services.paper_cards``) and the card input
sampling (``document_source_overview.sample_paper_card_input``)."""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.ports import SourceElementWrite
from app.services.document_source_overview import (
    PAPER_CARD_MAX_TABLES,
    PAPER_CARD_TABLE_MAX_CHARS,
    sample_paper_card_input,
)
from app.services.paper_cards import (
    CONTRIBUTION_MAX_CHARS,
    DATASET_NAME_MAX_CHARS,
    MAX_CARD_ANCHORS,
    MAX_CONTRIBUTIONS,
    MAX_METRICS,
    METRIC_COMPARED_MAX_CHARS,
    METRIC_UNIT_MAX_CHARS,
    METRIC_VALUE_MAX_CHARS,
    PAPER_CARD_VERSION,
    TLDR_MAX_CHARS,
    CardInputElement,
    card_is_empty,
    ground_value,
    number_tokens,
    paper_card_prompt,
    render_card_input,
    value_grounded,
    verify_paper_card,
)
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import RecordingModelProvider

E1 = CardInputElement("e1", "el-a", "text", "p1", "Gated Widgets. Abstract: we skip tokens.")
E2 = CardInputElement("e2", "el-b", "table", "Table 2", "| Ours | 85.20 | 1,234 |\n| Base | 185.23 |")
ELEMENTS = (E1, E2)


def _metric(value: str, anchor: str = "e2") -> dict:
    return {"name": "acc", "value": value, "unit": "%", "setting": "", "compared_to": "", "anchor": anchor}


# ------------------------------------------------------------------ numbers


@pytest.mark.parametrize(("value", "text", "verdict"), [
    ("85.2", "top-1 85.20 %", ""),                    # trailing zero
    ("85.20", "top-1 85.2", ""),
    ("85.2%", "top-1 85.2", ""),                      # unit separated from number
    ("８５．２", "top-1 85.2", ""),                    # full-width digits
    ("85.2", "top-1 ８５．２％", ""),
    ("1,234", "runs 1234 steps", ""),                 # thousands separator
    ("1234", "runs 1,234 steps", ""),
    ("3.1x", "cuts compute by 3.1× overall", ""),
    ("85.2", "top-1 185.23", "value_not_in_anchor"),  # whole-token match only
    ("85.2", "top-1 85.23", "value_not_in_anchor"),
    ("SOTA", "SOTA everywhere", "no_number"),
    # sign is kept: a leading - or U+2212 must match
    ("-0.5", "BLEU delta -0.5", ""),
    ("−0.5", "BLEU delta -0.5", ""),
    ("-0.5", "BLEU delta −0.5", ""),
    ("-0.5", "BLEU delta 0.5", "value_not_in_anchor"),
    ("0.5", "BLEU delta -0.5", "value_not_in_anchor"),
    ("12", "top-12 accuracy", ""),                     # a hyphen after a letter is not a sign
    # multi-token values must be contiguous (only joiners in between)
    ("1.2e-3", "learning rate 1.2e-3", ""),
    ("1.2e-3", "learning rate 1.2e3", "value_not_in_anchor"),   # exponent sign kept
    ("1.2e-3", "lr 1.2 for 3 epochs", "value_not_in_anchor"),
    ("85.2±0.3", "85.2 ± 0.3 over 5 seeds", ""),
    ("85.2±0.3", "85.2 (seed 0.3)", "value_not_in_anchor"),
    ("3×10^5", "trained for 3 × 10^5 steps", ""),
    ("3×10^5", "3 GPUs and 10^5 steps", "value_not_in_anchor"),
    ("85.2 / 90.1", "85.2/90.1", ""),
    ("85.2 / 90.1", "85.2 and 90.1", "value_not_in_anchor"),
    ("85.2 / 90.1", "85.2 only", "value_not_in_anchor"),
    # single-digit integers alone are too ambiguous to ground
    ("3", "3 runs", "ambiguous_value"),
    ("3x", "3x faster", "ambiguous_value"),
    ("-1", "delta -1", "ambiguous_value"),
    ("5", "top-1 85.2", "ambiguous_value"),
    # contiguous multi-token values are not ambiguous even with single digits
    ("3e-4", "learning rate 3e-4", ""),
    ("1e-5", "weight decay 1e-5", ""),
    ("1e-5", "weight decay 1e5", "value_not_in_anchor"),
    ("3e-4", "3 layers, lr 4", "value_not_in_anchor"),
    # en / em dashes are range separators like "-"
    ("1.5-2.0", "gain of 1.5–2.0 dB", ""),
    ("1.5–2.0", "gain of 1.5-2.0 dB", ""),
    ("1.5—2.0", "gain of 1.5-2.0 dB", ""),
    # a minus straight after a non-ASCII letter keeps its sign
    ("-0.5", "ΔBLEU Δ-0.5", ""),
    ("0.5", "ΔBLEU Δ-0.5", "value_not_in_anchor"),
    # known limitation (documented): section / version numbers can match
    ("2.1", "see Section 2.1", ""),
])
def test_value_grounding(value, text, verdict):
    assert ground_value(value, text) == verdict
    assert value_grounded(value, text) is (verdict == "")


def test_an_ambiguous_metric_is_dropped_with_its_reason():
    card, dropped = verify_paper_card({"metrics": [_metric("3", anchor="e1")]},
                                      (CardInputElement("e1", "el-a", "text", "", "we use 3 seeds"),))
    assert card["metrics"] == []
    assert dropped["metrics"][0]["reason"] == "ambiguous_value"


def test_number_tokens_are_canonical():
    assert number_tokens("v1.2.3 got 007 and 2.50") == ["1.2.3", "7", "2.5"]
    assert number_tokens("delta −0.5, top-1, 10-20, 1.2e-3") == ["-0.5", "1", "10", "20", "1.2", "-3"]
    assert number_tokens("Δ-0.5 and 1.5–2.0") == ["-0.5", "1.5", "2"]


# ------------------------------------------------------------------ verify


def test_valid_card_maps_handles_to_element_ids():
    card, dropped = verify_paper_card({
        "tldr": {"text": "We skip tokens.", "anchors": ["e1"]},
        "problem": {"text": "Waste.", "anchors": ["[E1]"]},   # bracketed / upper-case handle
        "method": {"text": None, "anchors": []},
        "contributions": [{"text": "Router.", "anchors": ["el-a"]}],   # raw element id
        "metrics": [_metric("85.2")],
        "datasets": [{"name": "ImageNet", "anchors": ["e2"]}],
    }, ELEMENTS)
    assert dropped == {}
    assert card["tldr"] == {"text": "We skip tokens.", "anchors": ["el-a"]}
    assert card["problem"]["anchors"] == ["el-a"]
    assert card["method"] is None
    assert card["contributions"] == [{"text": "Router.", "anchors": ["el-a"]}]
    assert card["metrics"][0]["anchor"] == "el-b"
    assert card["datasets"] == [{"name": "ImageNet", "anchors": ["el-b"]}]
    assert list(card) == ["tldr", "problem", "method", "contributions", "metrics", "datasets"]


def test_invalid_anchors_are_removed_and_anchorless_fields_dropped():
    card, dropped = verify_paper_card({
        "tldr": {"text": "Keeps the valid one.", "anchors": ["e1", "e9", "el-zzz"]},
        "problem": {"text": "No valid anchor.", "anchors": ["e7"]},
        "contributions": [{"text": "Gone.", "anchors": []}, {"text": "Kept.", "anchors": ["e2"]}],
        "metrics": [_metric("85.2", anchor="e5")],
        "datasets": [{"name": "X", "anchors": ["nope"]}],
    }, ELEMENTS)
    assert card["tldr"]["anchors"] == ["el-a"]
    assert card["problem"] is None
    assert card["contributions"] == [{"text": "Kept.", "anchors": ["el-b"]}]
    assert card["metrics"] == [] and card["datasets"] == []
    assert {"e9", "el-zzz", "e7", "e5", "nope"} <= set(dropped["invalid_anchors"])
    assert dropped["problem"][0]["reason"] == "no_valid_anchor"
    assert dropped["metrics"][0]["reason"] == "no_valid_anchor"


def test_metric_value_must_be_in_the_cited_element_text():
    card, dropped = verify_paper_card({
        "metrics": [
            _metric("85.2"),                 # in e2
            _metric("85.2", anchor="e1"),    # valid anchor, value not in e1
            _metric("85.23"),                # 185.23 is not 85.23
            _metric("1234"),                 # 1,234 in e2
        ],
    }, ELEMENTS)
    assert [m["value"] for m in card["metrics"]] == ["85.2", "1234"]
    assert [d["reason"] for d in dropped["metrics"]] == ["value_not_in_anchor"] * 2


def test_grounding_uses_the_truncated_text_shown_to_the_model():
    # The full element carried "99.9" after the cut; only the shown text counts.
    shown = CardInputElement("e1", "el-a", "table", "", "| Ours | 85.2 |")
    card, dropped = verify_paper_card({"metrics": [_metric("99.9", anchor="e1")]}, (shown,))
    assert card["metrics"] == []
    assert dropped["metrics"][0]["reason"] == "value_not_in_anchor"


def test_an_overlong_number_is_dropped_not_stored_cut():
    # Codex repro: "0." + 38 zeros + "1234" is in the anchor and grounds as
    # written, but cutting it to 40 characters would store zero.
    tiny = "0." + "0" * 38 + "1234"
    assert len(tiny) > METRIC_VALUE_MAX_CHARS
    anchor = CardInputElement("e1", "el-a", "table", "Table 1", f"| Ours | {tiny} |")
    card, dropped = verify_paper_card({"metrics": [_metric(tiny, anchor="e1")]}, (anchor,))
    assert card["metrics"] == []
    assert [d["reason"] for d in dropped["metrics"]] == ["value_too_long"]


def test_an_overlong_value_is_bounded_before_it_is_grounded():
    # The number survives the bound: the stored value is what was grounded.
    tail = " accuracy on the held out validation split of ImageNet"
    value = "85.2" + tail
    card, dropped = verify_paper_card({"metrics": [_metric(value)]}, ELEMENTS)
    stored = card["metrics"][0]["value"]
    assert len(stored) <= METRIC_VALUE_MAX_CHARS and stored.startswith("85.2 ")
    assert number_tokens(stored) == ["85.2"] and value_grounded(stored, E2.text)
    # A number cut off by the bound loses the metric (it was part of the value).
    cut = "85.2" + " " * (METRIC_VALUE_MAX_CHARS - 6) + "1234"
    card, dropped = verify_paper_card({"metrics": [_metric(cut)]}, ELEMENTS)
    assert card["metrics"] == []
    assert [d["reason"] for d in dropped["metrics"]] == ["value_too_long"]


def test_compared_to_is_never_cut_inside_a_number():
    metric = _metric("85.2")
    metric["compared_to"] = "x" * (METRIC_COMPARED_MAX_CHARS - 2) + " 6.95"
    card, _dropped = verify_paper_card({"metrics": [metric]}, ELEMENTS)
    compared = card["metrics"][0]["compared_to"]
    assert len(compared) <= METRIC_COMPARED_MAX_CHARS
    assert not compared.endswith(("6", "6.", "6.9"))


def test_prose_fields_are_never_cut_inside_a_number():
    # Codex repro: 298 characters + "85.23%" under the 300-character TL;DR
    # bound would be stored ending in "85" -- a different, still-cited result.
    tldr = "x" * (TLDR_MAX_CHARS - 2) + "85.23%"
    contribution = "y" * (CONTRIBUTION_MAX_CHARS - 2) + "85.23%"
    dataset = "z" * (DATASET_NAME_MAX_CHARS - 2) + "85.23"
    metric = _metric("85.2")
    metric["unit"] = "u" * (METRIC_UNIT_MAX_CHARS - 2) + "1.5"
    card, _dropped = verify_paper_card({
        "tldr": {"text": tldr, "anchors": ["e1"]},
        "contributions": [{"text": contribution, "anchors": ["e1"]}],
        "datasets": [{"name": dataset, "anchors": ["e1"]}],
        "metrics": [metric],
    }, ELEMENTS)
    assert card["tldr"]["text"] == "x" * (TLDR_MAX_CHARS - 2)
    assert card["contributions"][0]["text"] == "y" * (CONTRIBUTION_MAX_CHARS - 2)
    assert card["datasets"][0]["name"] == "z" * (DATASET_NAME_MAX_CHARS - 2)
    assert card["metrics"][0]["unit"] == "u" * (METRIC_UNIT_MAX_CHARS - 2)


def test_lengths_truncate_and_counts_cap():
    long_text = "x" * (TLDR_MAX_CHARS + 50)
    card, dropped = verify_paper_card({
        "tldr": {"text": long_text, "anchors": ["e1"]},
        "contributions": [{"text": f"c{i}", "anchors": ["e1"]} for i in range(MAX_CONTRIBUTIONS + 2)],
        "metrics": [_metric("85.2") for _ in range(MAX_METRICS + 1)],
    }, ELEMENTS)
    assert len(card["tldr"]["text"]) == TLDR_MAX_CHARS
    assert len(card["contributions"]) == MAX_CONTRIBUTIONS
    assert len(card["metrics"]) == MAX_METRICS
    assert [d["reason"] for d in dropped["contributions"]] == ["over_limit"] * 2
    assert [d["reason"] for d in dropped["metrics"]] == ["over_limit"]


def test_anchor_cap_keeps_the_field_order():
    many = tuple(
        CardInputElement(f"e{i}", f"el-{i:02d}", "text", "", f"value {i}.5 here")
        for i in range(1, 41)
    )
    card, dropped = verify_paper_card({
        "tldr": {"text": "t", "anchors": [f"e{i}" for i in range(1, 11)]},           # 10
        "metrics": [_metric(f"{i}.5", anchor=f"e{i}") for i in range(11, 21)],        # 10 → 20
        "contributions": [{"text": "c", "anchors": [f"e{i}" for i in range(21, 27)]}],  # 4 of 6 → 24
        "method": {"text": "m", "anchors": ["e1", "e30"]},                             # e1 reused, e30 cut
        "problem": {"text": "p", "anchors": ["e31"]},                                  # cut → dropped
        "datasets": [{"name": "d", "anchors": ["e2"]}],                                # reused
    }, many)
    kept = set()
    for field in ("tldr", "method", "problem"):
        if card[field]:
            kept.update(card[field]["anchors"])
    for item in card["contributions"] + card["datasets"]:
        kept.update(item["anchors"])
    kept.update(m["anchor"] for m in card["metrics"])
    assert len(kept) == MAX_CARD_ANCHORS
    assert len(card["metrics"]) == 10
    assert card["contributions"][0]["anchors"] == [f"el-{i:02d}" for i in range(21, 25)]
    assert card["method"]["anchors"] == ["el-01"]
    assert card["problem"] is None
    assert dropped["problem"][0]["reason"] == "anchor_cap"
    assert card["datasets"][0]["anchors"] == ["el-02"]


def test_all_empty_card_is_detected():
    card, _ = verify_paper_card({"tldr": {"text": "t", "anchors": ["e9"]}}, ELEMENTS)
    assert card_is_empty(card)
    card, _ = verify_paper_card("not an object", ELEMENTS)
    assert card_is_empty(card)


def test_malformed_shapes_degrade_without_raising():
    card, _ = verify_paper_card({
        "tldr": "a bare string",
        "problem": ["list"],
        "contributions": "x",
        "metrics": [{"value": 85.2, "anchor": "e2"}, "junk", {"value": True, "anchor": "e2"}],
        "datasets": [None, {"name": "", "anchors": ["e1"]}],
    }, ELEMENTS)
    assert card["tldr"] is None and card["problem"] is None
    assert [m["value"] for m in card["metrics"]] == ["85.2"]
    assert card["datasets"] == []


def test_prompt_keeps_metrics_to_the_papers_own_method():
    """T7:基线数值(BigGAN-deep 6.95、SchNet 14.0 meV)被当成本文成绩列出,
    setting 填成了另一列的列名。卡片版本随 prompt 一起加一(旧卡读取时失效)。"""
    prompt = paper_card_prompt("[e1] table · Table 1\nx")
    assert "THIS paper's own proposed method ONLY" in prompt
    assert "baseline, a prior method or an ablation row is NOT a metric" in prompt
    assert "\"compared_to\" of the metric it is compared with" in prompt
    assert "never the header of another column" in prompt
    assert PAPER_CARD_VERSION >= 2


def test_the_card_version_follows_the_number_safe_prose_rule():
    """v3: a metric value is bounded (number-safe) before it is grounded; v4:
    every prose field is bounded number-safe too, so v3 cards that may hold a
    cut prose number are invalidated on read."""
    assert PAPER_CARD_VERSION == 4


def test_prompt_carries_the_input_block():
    block = render_card_input(ELEMENTS)
    assert block.startswith("[e1] text · p1\n")
    assert "[e2] table · Table 2\n" in block
    assert block in paper_card_prompt(block)


# ------------------------------------------------------------------ sampling


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings(), model_provider=RecordingModelProvider())
    yield r
    r.close()


def _seed(repo, elements: list[tuple[str, str]]) -> str:
    notebook_id = repo.create_notebook(NotebookCreate(name="nb")).id
    store = repo._runtime.source_store
    store.insert_source(
        source_id="src-1", notebook_id=notebook_id, title="Doc", source_type="document",
        status="parsed", parse_status="parsed", file_name="d.pdf", file_path="/nonexistent/d.pdf",
        file_size=0, file_hash="h", summary="", doc_type="",
    )
    with repo._write() as db:
        store.replace_elements(db, "src-1", [
            SourceElementWrite(id=f"el-{i:04d}", element_type=kind, location_label=f"p{i}",
                               text=text, metadata={})
            for i, (kind, text) in enumerate(elements)
        ], created_at="2026-01-01T00:00:00")
    return notebook_id


def _sample(repo, budget=16000, reader=None):
    store = repo._runtime.source_store
    return sample_paper_card_input(
        store, repo._runtime.database, "src-1", budget,
        generation_reader=reader or store.source_card_basis,
    )


def test_sampling_opening_then_tables_then_spread_including_last(repo):
    elements = [("text", f"opening paragraph {i} " + "o" * 900) for i in range(20)]
    elements += [("table", f"| t{i} | {i}.5 |" + " " + "t" * 3000) for i in range(8)]
    elements += [("text", f"tail paragraph {i} " + "z" * 900) for i in range(40)]
    elements[-1] = ("text", "FINAL conclusion paragraph.")
    _seed(repo, elements)
    sample = _sample(repo)
    assert sample.status == "ok"
    assert sample.basis == repo._runtime.source_store.source_card_basis("src-1")
    assert sample.input_chars <= 16000
    ids = [e.element_id for e in sample.elements]
    # opening: document order from position 0, within 40% of the budget
    assert ids[:3] == ["el-0000", "el-0001", "el-0002"]
    opening = [e for e in sample.elements if e.element_id < "el-0020"]
    assert sum(len(e.text) for e in opening) <= 16000 * 0.4
    # tables: document order, each cut to 1500, at most 6
    tables = [e for e in sample.elements if e.element_type == "table"]
    assert [e.element_id for e in tables] == [f"el-{i:04d}" for i in range(20, 20 + PAPER_CARD_MAX_TABLES)]
    assert all(len(e.text) <= PAPER_CARD_TABLE_MAX_CHARS for e in tables)
    # spread: the last element is always sampled
    assert ids[-1] == f"el-{len(elements) - 1:04d}"
    assert sample.elements[-1].text == "FINAL conclusion paragraph."
    assert [e.handle for e in sample.elements] == [f"e{i}" for i in range(1, len(ids) + 1)]
    assert sample.input_block == render_card_input(sample.elements)


def test_sampling_is_deterministic(repo):
    _seed(repo, [("text", f"p{i} " + "w" * 700) for i in range(60)])
    assert _sample(repo) == _sample(repo)


def test_sampling_short_document_takes_everything_once(repo):
    _seed(repo, [("text", "Title"), ("table", "| a | 1 |"), ("text", "End.")])
    sample = _sample(repo)
    assert [e.element_id for e in sample.elements] == ["el-0000", "el-0001", "el-0002"]


def test_sampling_without_elements_is_empty(repo):
    _seed(repo, [])
    assert _sample(repo).status == "empty"


def test_sampling_reports_a_change_of_the_witness(repo):
    _seed(repo, [("text", "Title"), ("text", "Body")])
    witnesses = iter(["before", "after"])
    sample = _sample(repo, reader=lambda _sid: next(witnesses))
    assert sample.status == "changed"
    assert sample.basis == "before"
    assert sample.elements == ()



# ------------------------------------------------- number-safe input bounds


@pytest.mark.parametrize("where", ["table_cap", "budget_room"])
def test_card_input_never_ends_inside_a_number(where):
    from app.services.document_source_overview import _CardBudget

    padding = "x" * (PAPER_CARD_TABLE_MAX_CHARS - 4)
    text = padding + "85.23 top-1"
    if where == "table_cap":
        budget = _CardBudget(100_000)
        assert budget.fit("el-1", "table", "Table 1", text, limit=100_000, cap=PAPER_CARD_TABLE_MAX_CHARS)
    else:
        budget = _CardBudget(100_000)
        # the remaining-budget cut lands inside "85.23"
        from app.services.paper_cards import card_element_cost
        overhead = card_element_cost("e1", "table", "Table 1", "")
        assert budget.fit("el-1", "table", "Table 1", text, limit=overhead + len(padding) + 4)
    [shown] = budget.elements
    assert not shown.text.endswith("85.2") and "85.2" not in shown.text
    card, dropped = verify_paper_card({"metrics": [_metric("85.2", anchor="e1")]}, (shown,))
    assert card["metrics"] == [] and dropped



@pytest.mark.parametrize(("value", "limit", "expected"), [
    ("1.2e-3", 3, ""),                 # codex round 8 repro
    ("3\u00d710^5", 4, ""),           # codex round 8 repro
    ("lr 1.2E+03 used", 7, "lr"),
    ("lr 3x10^5 steps", 6, "lr"),
    ("err 85.2\u00b10.3 val", 8, "err"),
    ("ab 10^-5 x", 6, "ab"),
    ("acc 85.23 AP", 8, "acc"),
    ("acc 85.23 AP", 9, "acc 85.23"),  # a cut right after the number keeps it
    ("hello world 12", 8, "hello wo"),
])
def test_cap_number_safe_never_splits_a_numeric_expression(value, limit, expected):
    from app.services.paper_cards import cap_number_safe

    assert cap_number_safe(value, limit) == expected



@pytest.mark.parametrize(("value", "text", "verdict"), [
    (".95", "Accuracy is .72", "value_not_in_anchor"),          # codex round 9 repro
    ("85.2\u00b1.9", "85.2\u00b10.1", "value_not_in_anchor"),  # codex round 9 repro
    ("85.2\u00b1.9", "85.2 \u00b1 0.9", ""),
    (".95", "top-1 0.95 on val", ""),
    ("0.95", "top-1 .95 on val", ""),
    ("-.5", "delta of 0.5", "value_not_in_anchor"),
])
def test_leading_dot_decimals_are_numbers(value, text, verdict):
    from app.services.paper_cards import ground_prose_numbers, ground_value

    assert ground_value(value, text) == verdict
    assert ground_prose_numbers(value, text) == verdict
