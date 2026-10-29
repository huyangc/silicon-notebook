"""Drift guard of the KG attrs Gate 0 English slice (plan G0-T3).

``app/eval/kg_attrs_gate0/en_slice.json`` records what the CURRENT rules
produced on pool A when the slice was built: every concept's alias hits under
all four rules, every formula's context and the product reply with its
verification. Gate 0 results are only valid for the rules they measured, so:

1. recomputing every record with today's code must give the recorded result;
2. ``rules_signature()`` (normalized AST of the rules, verification, prompt,
   bounds and the reused matchers) must equal the recorded one.

Either failing means the rules or the verification changed: regenerate the
fixture and re-run Gate 0 on samples nobody has seen.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.core.config import Settings
from app.domain import kg_attrs
from app.eval import kg_attrs_calibration as cal

_FIXTURE = Path(cal.__file__).resolve().parent / "kg_attrs_gate0" / "en_slice.json"
_DATA = json.loads(_FIXTURE.read_text(encoding="utf-8"))
_STALE = (
    "规则或核验改了，须重新生成夹具（cd backend && PYTHONPATH=. python -m "
    "app.eval.kg_attrs_calibration extract-en），并在没见过的样本上重跑 Gate 0"
)


def test_rules_signature_matches_the_fixture():
    assert kg_attrs.rules_signature() == _DATA["rules_signature"], _STALE


def test_fixture_parameters_are_the_measured_ones():
    params = _DATA["params"]
    settings = Settings(_env_file=None)
    assert (params["window_n"], params["window_m"]) == (cal.EXTRACT_WINDOW_N, cal.EXTRACT_WINDOW_M)
    assert (params["max_chars"], params["pair_window"]) == (
        settings.kg_attrs_context_chars, settings.kg_attrs_pair_window), _STALE


def test_every_concept_recomputes_to_its_recorded_hits():
    elements = _DATA["elements"]
    drifted = []
    for concept in _DATA["concepts"]:
        items = [{"element_id": eid, "element_text": elements[eid], "element_read": True}
                 for eid in concept["evidence"]]
        hits = kg_attrs.trace_concept_aliases(
            concept["name"], items, exclude_names=(), rules=kg_attrs.ALIAS_RULES)
        if hits != concept["hits"]:
            drifted.append(f"{concept['book']}/{concept['chapter']}: {concept['name']}")
    assert not drifted, f"{_STALE}: {drifted[:10]}"


def test_every_formula_recomputes_to_its_recorded_context_and_verification():
    elements = _DATA["elements"]
    params = _DATA["params"]
    drifted = []
    for formula in _DATA["formulas"]:
        segments = cal.slice_segments(formula, elements, max_chars=params["max_chars"])
        verification = cal.verification_record(
            formula["name"], segments, formula["reply"], pair_window=params["pair_window"])
        context = [[eid, len(text)] for eid, text in segments]
        if context != formula["context"] or verification != formula["verification"]:
            drifted.append(f"{formula['book']}/{formula['chapter']}: {formula['name'][:60]}")
    assert not drifted, f"{_STALE}: {drifted[:10]}"


def test_fixture_is_pool_a_only_and_self_contained():
    referenced = {eid for c in _DATA["concepts"] for eid in c["evidence"]}
    referenced |= {f["element_id"] for f in _DATA["formulas"]}
    referenced |= {f["next_element_id"] for f in _DATA["formulas"] if f["next_element_id"]}
    assert referenced <= set(_DATA["elements"])
    assert {(c["book"]) for c in _DATA["chapters"]} == {"cmos", "engram"}
    assert all(eid.startswith("el-en-") for eid in _DATA["elements"])
    assert len(_DATA["chapters"]) == 14
