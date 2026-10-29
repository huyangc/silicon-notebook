"""Pure functions and review-page safety of the KG attrs Gate 0 tool (plan G0-T2)."""
from __future__ import annotations

import json
import re
import subprocess
import unicodedata
from html.parser import HTMLParser

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.eval import kg_attrs_calibration as cal

# --- Settings ---------------------------------------------------------------

def test_attrs_settings_defaults_and_positive_bounds():
    settings = Settings(_env_file=None)
    assert (settings.kg_attrs_context_chars, settings.kg_attrs_pair_window) == (1200, 120)
    for alias in ("KG_ATTRS_CONTEXT_CHARS", "KG_ATTRS_PAIR_WINDOW"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **{alias: 0})


# --- statistics -------------------------------------------------------------

@pytest.mark.parametrize("correct, n, expected", [(96, 100, 0.9016), (92, 100, 0.8500)])
def test_wilson_lower_reference_values(correct, n, expected):
    assert round(cal.wilson_lower(correct, n), 4) == expected


def test_wilson_lower_95_of_100_misses_the_alias_line():
    assert cal.wilson_lower(95, 100) < 0.90
    assert cal.wilson_lower(0, 0) == 0.0


def test_k_min_and_early_failure():
    assert cal.k_min(100, 0.90) == 96
    assert cal.k_min(100, 0.85) == 92
    assert (cal.failed_early(4, 0.90), cal.failed_early(5, 0.90)) == (False, True)
    assert (cal.failed_early(8, 0.85), cal.failed_early(9, 0.85)) == (False, True)


@pytest.mark.parametrize("correct, wrong, line, verdict", [
    (96, 4, 0.90, "pass"),
    (95, 5, 0.90, "fail"),
    (92, 8, 0.85, "pass"),
    (50, 0, 0.90, "insufficient"),
    (10, 5, 0.90, "fail"),          # failed early before reaching 100
    (143, 7, 0.90, "pass"),         # n=150 needs 143
])
def test_precision_verdict(correct, wrong, line, verdict):
    assert cal.precision_verdict(correct, wrong, line) == verdict


def test_unsure_counts_as_wrong():
    assert cal.count_verdicts(["correct", "unsure", "incorrect", None, "correct"]) == (2, 2)


# --- keys -------------------------------------------------------------------

def test_align_key_is_nfc_text_hash_plus_name_and_normalized_symbol():
    composed = "café (VCO)"
    decomposed = unicodedata.normalize("NFD", composed)
    assert cal.align_key(composed, "VCO") == cal.align_key(decomposed, "VCO")
    assert cal.align_key(composed, "VCO") != cal.align_key(composed, "PLL")
    assert cal.align_key("t", "f", r"\mathrm{g}_{m}") == cal.align_key("t", "f", "g_m")
    assert json.loads(cal.align_key("t", "f", "g_m"))[2] == "g_m"


def test_pair_group_is_shared_across_objects_with_the_same_context():
    segments = [["e1", "x = y"], ["e2", "where y is height"]]
    assert cal.pair_group_key(segments, "y", "height") == cal.pair_group_key(segments, "y ", " height")
    assert cal.pair_group_key(segments, "y", "height") != cal.pair_group_key(segments[:1], "y", "height")
    assert cal.card_key(segments, "x = y") != cal.card_key(segments, "x = z")


# --- sampling ---------------------------------------------------------------

def _cands(spec):
    return [{"id": f"{s}-{i}", "s": s} for s, count in spec.items() for i in range(count)]


def test_stratified_sample_caps_then_relaxes():
    cands = _cands({"a": 50, "b": 5, "c": 3})
    first = cal.stratified_sample(cands, 18, stratum=lambda c: c["s"], seed=7)
    counts = {s: sum(c["s"] == s for c in first) for s in "abc"}
    assert counts == {"a": 10, "b": 5, "c": 3}
    more = cal.stratified_sample(cands, 30, stratum=lambda c: c["s"], seed=7)
    assert sum(c["s"] == "a" for c in more) == 22          # relaxed past 10 and 20
    assert more[:18] == first                              # top-up continues the sequence
    assert len(cal.stratified_sample(cands, 999, stratum=lambda c: c["s"], seed=7)) == 58


def test_stratified_sample_is_seeded():
    cands = _cands({"a": 30, "b": 30})
    one = cal.stratified_sample(cands, 10, stratum=lambda c: c["s"], seed=1)
    assert one == cal.stratified_sample(cands, 10, stratum=lambda c: c["s"], seed=1)
    assert one != cal.stratified_sample(cands, 10, stratum=lambda c: c["s"], seed=2)


def test_blind_keys_take_a_tenth_rounded_up():
    keys = [f"k{i}" for i in range(31)]
    blind = cal.blind_keys(keys, seed=3)
    assert len(blind) == 4 and blind <= set(keys)
    assert blind == cal.blind_keys(list(reversed(keys)), seed=3)


# --- labels -----------------------------------------------------------------

def test_parse_labels_accepts_only_keys_verdicts_and_counts():
    ok = {"labels": [{"key": "a", "verdict": "correct"}, {"key": "b", "missing": 0},
                     {"key": "c", "verdict": "unsure", "missing": 2}]}
    assert cal.parse_labels(ok) == {"a": {"verdict": "correct"}, "b": {"missing": 0},
                                    "c": {"verdict": "unsure", "missing": 2}}


@pytest.mark.parametrize("payload", [
    {"labels": [{"key": "a", "verdict": "correct", "text": "source"}]},
    {"labels": [{"key": "a", "verdict": "maybe"}]},
    {"labels": [{"key": "a", "missing": -1}]},
    {"labels": [{"key": "a", "missing": True}]},
    {"labels": [{"key": "a", "missing": 1.5}]},
    {"labels": [{"verdict": "correct"}]},
    {"labels": {}},
    {"labels": [], "notes": "x"},
])
def test_parse_labels_refuses_anything_else(payload):
    with pytest.raises(ValueError):
        cal.parse_labels(payload)


def test_merge_labels_later_wins_per_field():
    merged = cal.merge_labels({"a": {"verdict": "correct", "missing": 1}}, {"a": {"verdict": "unsure"}})
    assert merged == {"a": {"verdict": "unsure", "missing": 1}}


# --- recall -----------------------------------------------------------------

def _pair(group, origin):
    return {"group": group, "origin": origin, "symbol": "s", "meaning": "m"}


def test_formula_recall_pooled_estimate():
    cards = [
        {"key": "c1", "pairs": [_pair("g1", "kept"), _pair("g2", "rejected"), _pair("g3", "draft")]},
        {"key": "c2", "pairs": [_pair("g4", "kept")]},
        {"key": "c3", "pairs": [_pair("g5", "kept")]},               # missing count not filled
        {"key": "c4", "pairs": [_pair("g6", "draft")]},              # all wrong, missing 0
    ]
    checks = [{"key": "e1"}, {"key": "e2"}, {"key": "e3"}]
    labels = {
        "g1": {"verdict": "correct"}, "g2": {"verdict": "correct"}, "g3": {"verdict": "unsure"},
        "g4": {"verdict": "incorrect"}, "g5": {"verdict": "correct"}, "g6": {"verdict": "incorrect"},
        "c1": {"missing": 1}, "c2": {"missing": 2}, "c4": {"missing": 0},
        "e1": {"missing": 1}, "e2": {"missing": 0},
    }
    out = cal.formula_recall(cards, checks, labels, hint_empty_scanned=10)
    # counted cards c1, c2, c4: H=1, G=2, M=3; M_hat = mean(1,0)*10 = 5
    assert (out["H"], out["G"], out["M"], out["M_hat"]) == (1, 2, 3, 5.0)
    assert out["recall"] == round(1 / 10, 4)
    assert out["recall_without_m_hat"] == round(1 / 5, 4)
    assert out["denominator_formulas"] == 2                      # c4 has G=0 and M=0
    assert out["formula_hit_rate"] == 0.5
    assert out["empty_checks_filled"] == 2


def test_recall_verdict_needs_both_hundreds():
    base = {"denominator_formulas": 100, "recall": 0.7}
    assert cal.recall_verdict(base, 100) == "pass"
    assert cal.recall_verdict(base, 99) == "insufficient"
    assert cal.recall_verdict({**base, "denominator_formulas": 99}, 100) == "insufficient"
    assert cal.recall_verdict({**base, "recall": 0.69}, 100) == "fail"


def test_combine_verdicts():
    assert cal.combine_verdicts("pass", "pass") == "pass"
    assert cal.combine_verdicts("pass", "insufficient") == "insufficient"
    assert cal.combine_verdicts("untested", "insufficient") == "untested"
    assert cal.combine_verdicts("fail", "untested") == "fail"


# --- report -----------------------------------------------------------------

def _items():
    aliases = [{"key": f"a{i}", "rule": "R1", "name": "n", "alias": "A", "segments": [["e", "t"]],
                "blind": i < 10, "prelabel": {"verdict": "correct", "reason": ""}} for i in range(100)]
    cards = [{"key": "c-en", "language": "en", "formula": "x=y", "segments": [["e", "where y is h"]],
              "blind": False,
              "pairs": [{"group": "g1", "symbol": "y", "meaning": "h", "origin": "kept"},
                        {"group": "g2", "symbol": "x", "meaning": "q", "origin": "rejected",
                         "reason": "too_far"}]}]
    return {"run": "r", "seed": 1, "alias_items": aliases, "formula_cards": cards, "empty_checks": [],
            "formula_scan": {"en": {"hint_empty": 0}, "zh": {"untested": True}}}


def test_build_report_judges_rows_and_marks_untested_chinese():
    labels = {f"a{i}": {"verdict": "correct" if i < 96 else "unsure"} for i in range(100)}
    labels.update({"g1": {"verdict": "correct"}, "g2": {"verdict": "correct"}, "c-en": {"missing": 0}})
    report = cal.build_report(_items(), labels)
    r1 = report["alias"][0]
    assert (r1["rule"], r1["n"], r1["correct"], r1["verdict"]) == ("R1", 100, 96, "pass")
    assert [r["verdict"] for r in report["alias"][1:]] == ["insufficient"] * 3
    assert report["alias_rules_passed"] == ["R1"]
    zh = report["formula_precision"][1]
    assert (zh["rule"], zh["verdict"]) == ("zh", "untested")
    assert report["formula_overall"]["zh"] == "untested" and report["formula_ships"] is False
    assert report["reject_reasons"] == [{"reason": "too_far", "count": 1, "share": 1.0, "judged": 1,
                                         "judged_correct_rate": 1.0, "flag": True}]
    agreement = report["prelabel_agreement"]
    assert (agreement["all_n"], agreement["blind_n"]) == (100, 10)
    md = cal.render_report_md(report)
    for heading in ("## 判定", "## 拒绝原因分布", "## 预标一致性", "## 别名召回参照（只报告）"):
        assert heading in md
    json.dumps(report)                                            # report.json is plain JSON


# --- review page safety -----------------------------------------------------

_FORBIDDEN = ("http://", "https://", "<script src", "fetch(", "XMLHttpRequest", "WebSocket",
              "sendBeacon", "innerHTML")


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags: list[tuple[str, dict]] = []
        self.data: dict[str, str] = {}
        self._script_id = None

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "script":
            self._script_id = dict(attrs).get("id") or f"script{len(self.tags)}"

    def handle_endtag(self, tag):
        if tag == "script":
            self._script_id = None

    def handle_data(self, data):
        if self._script_id:
            self.data[self._script_id] = self.data.get(self._script_id, "") + data


_HOSTILE = {"run": "r", "blocks": [], "cards": [
    {"kind": "alias", "key": "k", "name": "</script><script>alert(1)</script>",
     "alias": "<img src=x onerror=alert(1)>", "segments": [["e", "see https://example.org <b>x</b>"]]}]}


def _page():
    return cal.render_review_html(_HOSTILE)


def test_csp_meta_is_the_first_element_of_head():
    tags = _Tags()
    tags.feed(_page())
    names = [t for t, _ in tags.tags]
    head = names.index("head")
    first_tag, attrs = tags.tags[head + 1]
    assert first_tag == "meta" and attrs["http-equiv"] == "Content-Security-Policy"
    assert attrs["content"] == (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "connect-src 'none'; img-src 'none'; form-action 'none'"
    )


def test_embedded_data_escapes_every_angle_bracket():
    tags = _Tags()
    tags.feed(_page())
    scripts = [attrs for tag, attrs in tags.tags if tag == "script"]
    assert scripts == [{"type": "application/json", "id": "kg-data"}, {}]   # nothing injected
    raw = tags.data["kg-data"]
    assert "<" not in raw
    assert json.loads(raw) == _HOSTILE


def test_template_and_script_carry_no_network_or_html_sinks():
    for part in cal.page_template_parts():
        for needle in _FORBIDDEN:
            assert needle not in part, needle
    script = cal.page_template_parts()[3]
    assert "textContent" in script


def test_export_carries_only_key_verdict_missing():
    script = cal.page_template_parts()[3]
    body = re.search(r"function exportRecords\(\) \{(.*?)\n\}", script, re.S).group(1)
    assigned = set(re.findall(r"record\.(\w+) =", body)) | set(re.findall(r"\{(\w+): key\}", body))
    assert assigned == {"key", "verdict", "missing"}
    assert "note" not in body and "DATA" not in body
    assert f'link.download = "{cal.LABELS_FILE_NAME}"' in script


def test_review_page_data_hides_marks_on_blind_cards_and_orders_blocks():
    items = _items()
    items["formula_cards"][0]["blind"] = True
    items["formula_cards"][0]["pairs"][0]["prelabel"] = {"verdict": "correct"}
    data = cal.review_page_data(items, seed=5)
    assert [b["id"] for b in data["blocks"]] == [
        "alias-R1", "alias-R2", "alias-R3", "alias-R4", "formula-en", "formula-zh", "empty-en", "empty-zh"]
    formula = next(c for c in data["cards"] if c["kind"] == "pairs")
    assert all(p["origin_label"] is None and p["prelabel"] is None for p in formula["pairs"])
    blind_alias = [c for c in data["cards"] if c["kind"] == "alias" and c["blind"]]
    assert blind_alias and all(c["prelabel"] is None for c in blind_alias)
    alias_block = data["blocks"][0]
    assert (alias_block["k_min"], alias_block["max_wrong"]) == (96, 4)
    assert sorted(alias_block["order"]) == list(range(100)) and alias_block["order"] != list(range(100))


# --- output location --------------------------------------------------------

def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.mark.process_contract
def test_local_output_dir_requires_a_git_ignored_path(tmp_path):
    _git(tmp_path, "init", "-q")
    with pytest.raises(SystemExit):
        cal.local_output_dir(tmp_path)
    (tmp_path / ".gitignore").write_text(".local/\n", encoding="utf-8")
    out = cal.local_output_dir(tmp_path)
    assert out == tmp_path / ".local" / "kg_attrs_gate0" and out.is_dir()


@pytest.mark.process_contract
def test_main_checkout_root_is_the_parent_of_the_common_git_dir(tmp_path):
    _git(tmp_path, "init", "-q")
    assert cal.main_checkout_root(tmp_path).resolve() == tmp_path.resolve()
