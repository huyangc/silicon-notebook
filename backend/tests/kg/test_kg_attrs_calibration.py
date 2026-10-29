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
    (121, 9, 0.85, "fail"),         # judged 130 with 9 wrong: same verdict as stopping at the 9th wrong
    (0, 9, 0.85, "fail"),
    (141, 9, 0.85, "fail"),         # early failure is final, more items never flip it
    (147, 3, 0.90, "pass"),         # past n=100 within the early-failure allowance
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
    key = cal.pair_group_key(segments, "x = y", "y", "height")
    assert key == cal.pair_group_key(segments, "x=y", "y ", " height")
    assert key != cal.pair_group_key(segments[:1], "x = y", "y", "height")
    assert key != cal.pair_group_key(segments, "x = z", "y", "height")      # N(formula) is part of it
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
    # counted cards c1, c2, c4. H=1 (g1). G=3: g1, g2 correct plus the draft g3
    # judged unsure, counted as a true pair the product missed. M=3; M_hat=5.
    assert (out["H"], out["G"], out["M"], out["M_hat"]) == (1, 3, 3, 5.0)
    assert out["recall"] == round(1 / 11, 4)
    assert out["recall_without_m_hat"] == round(1 / 6, 4)
    assert out["denominator_formulas"] == 2                      # c4 has G=0 and M=0
    assert out["formula_hit_rate"] == 0.5
    assert out["empty_checks_filled"] == 2


def test_recall_unsure_kept_pair_is_wrong_and_cards_count_once():
    cards = [{"key": "c1", "pairs": [_pair("g1", "kept"), _pair("g2", "rejected")]},
             {"key": "c1", "pairs": [_pair("g1", "kept"), _pair("g2", "rejected")]}]   # second notebook
    labels = {"g1": {"verdict": "unsure"}, "g2": {"verdict": "unsure"}, "c1": {"missing": 0}}
    out = cal.formula_recall(cards, [], labels, hint_empty_scanned=0)
    # kept+unsure: not H, not G; rejected+unsure: in G. One card, not two.
    assert (out["H"], out["G"], out["denominator_formulas"]) == (0, 1, 1)


def test_recall_verdict_needs_both_hundreds():
    base = {"denominator_formulas": 100, "recall": 0.7, "empty_checks_filled": 3, "empty_checks_drawn": 3}
    assert cal.recall_verdict(base, 100) == "pass"
    assert cal.recall_verdict(base, 99) == "insufficient"
    assert cal.recall_verdict({**base, "denominator_formulas": 99}, 100) == "insufficient"
    assert cal.recall_verdict({**base, "recall": 0.69}, 100) == "fail"


def test_recall_is_insufficient_until_every_drawn_spot_check_is_filled():
    cards = [{"key": f"c{i}", "pairs": [_pair(f"g{i}", "kept")]} for i in range(100)]
    labels = {f"g{i}": {"verdict": "correct"} for i in range(100)} | {f"c{i}": {"missing": 0} for i in range(100)}
    checks = [{"key": "e1"}, {"key": "e2"}]
    labels["e1"] = {"missing": 0}
    recall = cal.formula_recall(cards, checks, labels, hint_empty_scanned=50)
    assert (recall["empty_checks_filled"], recall["empty_checks_drawn"]) == (1, 2)
    # mutation: an unfilled check read as 0 missed -> "pass".
    assert cal.recall_verdict(recall, 100) == "insufficient"
    labels["e2"] = {"missing": 0}
    assert cal.recall_verdict(cal.formula_recall(cards, checks, labels, hint_empty_scanned=50), 100) == "pass"


def _formula_record(**over):
    return {"service": "general", "model": "m", "fingerprint": "abcd", "workload": "w", **over}


def test_fixture_replies_are_reused_only_for_the_measured_configuration():
    fixture = {"params": {"max_chars": 1200, "pair_window": 120},
               "models": {"formula": _formula_record()}}
    assert cal.fixture_provenance_problems(fixture, max_chars=1200, pair_window=120, formula=_formula_record()) == []
    for kwargs in ({"max_chars": 800, "pair_window": 120, "formula": _formula_record()},
                   {"max_chars": 1200, "pair_window": 100, "formula": _formula_record()},
                   {"max_chars": 1200, "pair_window": 120, "formula": _formula_record(fingerprint="ffff")},
                   {"max_chars": 1200, "pair_window": 120, "formula": _formula_record(model="other")}):
        assert cal.fixture_provenance_problems(fixture, **kwargs)


def test_top_up_refuses_a_different_run_or_scan_order():
    fixture = {"params": {"max_chars": 1200, "pair_window": 120}}
    identity = cal.run_identity(seed=1, fixture=fixture, max_chars=1200, pair_window=120,
                                formula=_formula_record(), egress=False)
    lists = {"en": [_formula(0, "t"), _formula(1, "t")], "zh": []}
    saved = {"identity": identity,
             "scan_state": {lang: {"order": cal.scan_order_sha(fs)} for lang, fs in lists.items()}}
    assert cal.resume_problems(saved, identity, lists) == []
    other_seed = cal.run_identity(seed=2, fixture=fixture, max_chars=1200, pair_window=120,
                                  formula=_formula_record(), egress=False)
    assert cal.resume_problems(saved, other_seed, lists) == ["seed differs from the saved run"]
    other_fixture = cal.run_identity(seed=1, fixture={**fixture, "x": 1}, max_chars=1200, pair_window=120,
                                     formula=_formula_record(), egress=False)
    assert cal.resume_problems(saved, other_fixture, lists) == ["fixture_sha differs from the saved run"]
    reordered = {"en": list(reversed(lists["en"])), "zh": []}
    assert cal.resume_problems(saved, identity, reordered) == ["the en scan order differs from the saved run"]
    assert cal.resume_problems({}, identity, lists)                       # a saved run without identity


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
    # formula blocks: no in-page stop hint (precision counts kept pairs only,
    # which a blind card must not reveal); report carries the stop status.
    formula_block = data["blocks"][4]
    assert (formula_block["k_min"], formula_block["max_wrong"]) == (None, None)


def test_review_page_carries_only_opaque_ids():
    data = cal.review_page_data(_items(), seed=5)
    ids = {c["key"] for c in data["cards"]} | {p["group"] for c in data["cards"] for p in c.get("pairs", [])}
    assert all(re.fullmatch(r"[0-9a-f]{20}", i) for i in ids)
    assert cal.opaque_id("a1") in ids and "a1" not in ids


def test_blind_card_pairs_are_shuffled():
    items = _items()
    card = items["formula_cards"][0]
    card["pairs"] = [{"group": f"g{i}", "symbol": f"s{i}", "meaning": "m", "origin": "kept" if i < 5 else "draft"}
                     for i in range(10)]
    plain = next(c for c in cal.review_page_data(items, seed=5)["cards"] if c["kind"] == "pairs")
    card["blind"] = True
    blind = next(c for c in cal.review_page_data(items, seed=5)["cards"] if c["kind"] == "pairs")
    assert [p["symbol"] for p in plain["pairs"]] == [f"s{i}" for i in range(10)]
    assert sorted(p["symbol"] for p in blind["pairs"]) == sorted(p["symbol"] for p in plain["pairs"])
    assert [p["symbol"] for p in blind["pairs"]] != [p["symbol"] for p in plain["pairs"]]


def test_report_stop_status_uses_the_verdict_rule():
    assert cal.stop_hint(10, 9, 0.85) == "failed"
    assert cal.stop_hint(121, 9, 0.85) == "failed"
    assert cal.stop_hint(92, 8, 0.85) == "reached"
    assert cal.stop_hint(50, 3, 0.85) == "continue"


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


# --- extract-en plumbing (plan G0-T3) ---------------------------------------

class _FakeClient:
    def __init__(self, reply="", status="miss", error=None):
        self.reply, self.status, self.error = reply, status, error
        self.calls = []

    def chat_json(self, messages, hint, **kwargs):
        self.calls.append((messages, hint, kwargs))
        if self.error:
            raise self.error
        kwargs["call_stats"]["status"] = self.status
        return self.reply


def test_budget_books_before_sending_and_refunds_cache_hits():
    budget = cal.Budget(max_calls=2, max_chars=100)
    budget.reserve(60)
    budget.reserve(30)
    budget.refund(30)                                   # answered from the cache
    assert (budget.calls, budget.chars, budget.cache_hits) == (1, 60, 1)
    with pytest.raises(cal.BudgetExceeded):
        budget.reserve(41)
    budget.reserve(40)
    assert (budget.calls, budget.chars) == (2, 100)


def test_budget_is_a_hard_cap_under_parallel_senders():
    import threading

    budget = cal.Budget(max_calls=10, max_chars=10_000)
    barrier = threading.Barrier(40)
    admitted = []

    def sender():
        barrier.wait()
        try:
            budget.reserve(1)
            admitted.append(1)
        except cal.BudgetExceeded:
            pass

    threads = [threading.Thread(target=sender) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # mutation: check-then-book outside one lock -> more than 10 get through.
    assert len(admitted) == 10 and budget.calls == 10


def test_call_formula_is_the_product_call():
    reply = '{"variables": []}'
    client = _FakeClient(reply=reply, status="cache_hit")
    budget = cal.Budget(10, 10_000)
    assert cal.call_formula(client, "y = x", [["e1", "where x is t"]], budget) == (reply, None)
    _messages, hint, kwargs = client.calls[0]
    assert hint == '{"variables":[{"symbol":"","meaning":""}]}'
    assert kwargs["thinking_mode"] == "disabled"
    assert kwargs["response_validator"] is cal.kg_attrs.formula_variables_reply_ok
    assert (budget.calls, budget.cache_hits) == (0, 1)
    failing = _FakeClient(error=RuntimeError("boom"))
    assert cal.call_formula(failing, "y", [["e", "t"]], budget) == (None, "RuntimeError")
    assert budget.calls == 1
    assert cal.verification_record("y", [["e", "t"]], None, pair_window=120) == {
        "status": "failed", "kept": [], "rejected": []}


class _Workload:
    def __init__(self, wid, kind="chat"):
        self.id, self.kind = wid, kind


class _Service:
    def __init__(self, sid):
        self.id = sid


class _Registry:
    def __init__(self, bindings):
        self.bindings = bindings

    def workloads_for(self, service_id):
        return tuple(_Workload(w, "embedding" if w.endswith("_embedding") else "chat")
                     for w, s in self.bindings.items() if s == service_id)

    def service_for(self, workload_id):
        return _Service(self.bindings[workload_id])


def test_formula_workload_routes_to_the_named_service():
    registry = _Registry({"query_rewrite": "general", "kg_extract": "general", "x": "other"})
    assert cal.formula_workload(registry, "general") == "kg_extract"
    with pytest.raises(SystemExit):
        cal.formula_workload(registry, "missing")
    assert cal.formula_workload(registry, "general", explicit="query_rewrite") == "query_rewrite"
    with pytest.raises(SystemExit):
        cal.formula_workload(registry, "general", explicit="x")      # bound elsewhere
    embedding = _Registry({"kg_extract": "general", "chunk_embedding": "general"})
    with pytest.raises(SystemExit):
        cal.formula_workload(embedding, "general", explicit="chunk_embedding")   # not a chat workload


class _El:
    def __init__(self, eid, text):
        self.id, self.text = eid, text


def test_chapter_records_builds_concepts_and_formula_contexts():
    elements = [_El("e0", "A Phase Locked Loop (PLL) locks."), _El("e1", "y = a x"),
                _El("e2", "where a is the gain")]
    objects = [
        {"object_type": "concept", "payload": {"name": "Phase Locked Loop"},
         "evidence": [{"element_id": "e0"}]},
        {"object_type": "formula", "payload": {"name": "y = a x"}, "evidence": [{"element_id": "e1"}]},
        {"object_type": "formula", "payload": {"name": "z"}, "evidence": [{"element_id": "e2"}]},
        {"object_type": "claim", "payload": {"name": "c"}, "evidence": [{"element_id": "e0"}]},
        {"object_type": "concept", "payload": {"name": "Unbound"}, "evidence": [{"element_id": "zz"}]},
    ]
    concepts, formulas, used = cal.chapter_records("b", "c", elements, objects, max_chars=12)
    assert [(c["name"], c["hits"]) for c in concepts] == [
        ("Phase Locked Loop", [{"rule": "R2", "text": "PLL", "element_id": "e0"}])]
    assert formulas[0]["next_element_id"] == "e2" and formulas[1]["next_element_id"] is None
    assert formulas[0]["segments"] == [["e1", "y = a x"], ["e2", "where"]]
    assert formulas[0]["context"] == [["e1", 7], ["e2", 5]]
    assert set(used) == {"e0", "e1", "e2"}
    assert cal.slice_segments(formulas[0], used, max_chars=12) == formulas[0]["segments"]


# --- prepare: pools, scanning, budget (plan G0-T4) ---------------------------

@pytest.mark.parametrize("url", [
    "postgresql://127.0.0.1:5432/db",
    "postgresql://localhost/db",
    "postgresql://user@[::1]:5432/db",
    "postgresql:///db",
    "postgresql:///db?host=/tmp",
    "host=localhost dbname=db",
])
def test_local_database_url_accepted(url):
    cal.check_local_database_url(url)


@pytest.mark.parametrize("url", [
    "postgresql://db.example.org/db",
    "postgresql://10.0.0.5:5432/db",
    "postgresql://localhost,db.example.org/db",
    "host=localhost hostaddr=10.0.0.5 dbname=db",
])
def test_remote_database_url_refused(url):
    with pytest.raises(SystemExit):
        cal.check_local_database_url(url)


@pytest.mark.parametrize("text, hint", [
    ("where g is the gain", True), ("Here, x denotes", True), ("somewhere else", False),
    ("其中 g 为增益", True), ("式中各量", True), ("y 表示输出", True), ("no words", False),
])
def test_hint_words(text, hint):
    assert cal.has_hint_word([["e", text]]) is hint


def _fixture():
    return {
        "params": {"max_chars": 50, "pair_window": 120, "window_n": 8000, "window_m": 450},
        "elements": {"el-en-a": "A Phase Locked Loop (PLL) here.", "el-en-b": "y = a x",
                     "el-en-c": "where a is the gain"},
        "concepts": [{"book": "cmos", "chapter": "ch01", "name": "Phase Locked Loop",
                      "evidence": ["el-en-a"], "hits": [{"rule": "R2", "text": "PLL", "element_id": "el-en-a"}]}],
        "formulas": [{"book": "cmos", "chapter": "ch01", "name": "y = a x", "element_id": "el-en-b",
                      "next_element_id": "el-en-c", "reply": '{"variables": []}', "error": None}],
    }


def test_pool_a_reads_the_fixture():
    concepts, formulas = cal.pool_a(_fixture())
    assert concepts[0]["pool"] == "A" and concepts[0]["texts"] == {"el-en-a": "A Phase Locked Loop (PLL) here."}
    assert formulas[0]["segments"] == [["el-en-b", "y = a x"], ["el-en-c", "where a is the gain"]]
    assert formulas[0]["reply"] == '{"variables": []}'


def _local_concept(pool, stratum, alias):
    return {"pool": pool, "language": "zh" if pool == "C" else "en", "stratum": stratum, "name": "放大器",
            "hits": [{"rule": "R4", "text": alias, "element_id": "e1"}], "texts": {"e1": f"放大器（{alias}）"}}


def test_alias_candidates_mark_local_items_by_egress():
    concepts = cal.pool_a(_fixture())[0] + [_local_concept("C", ("nb", "s"), "AMP")]
    off = cal.alias_candidates(concepts, egress=False)
    assert [c["prelabel_allowed"] for c in off["R2"]] == [True]
    assert [c["prelabel_allowed"] for c in off["R4"]] == [False]
    assert [c["prelabel_allowed"] for c in cal.alias_candidates(concepts, egress=True)["R4"]] == [True]
    assert off["R4"][0]["segments"] == [["e1", "放大器（AMP）"]]


def test_sample_aliases_top_up_keeps_existing_items():
    concepts = [_local_concept("C", ("nb", f"s{i}"), f"A{chr(65 + i)}") for i in range(20)]
    candidates = cal.alias_candidates(concepts, egress=True)
    first = cal.sample_aliases(candidates, per_rule=5, seed=1)
    assert len(first) == 5
    more = cal.sample_aliases(candidates, per_rule=8, seed=1, existing=first)
    assert more[:5] == first and len(more) == 8 and len({m["key"] for m in more}) == 8


def _formula(i, text, language="en", pool="B"):
    return {"pool": pool, "language": language, "ref": f"f{i}", "name": "y = a x",
            "segments": [[f"e{i}", "y = a x"], [f"n{i}", text]]}


def test_scan_formulas_pools_drafts_and_stops_at_targets():
    good = '{"variables": [{"symbol": "a", "meaning": "the gain"}]}'
    formulas = [_formula(i, f"where a is the gain {i}") for i in range(5)] + [_formula(9, "plain words")]
    drafted = []

    def product(formula):
        return good, None

    def draft(formula):
        drafted.append(formula["ref"])
        return [{"symbol": "a", "meaning": "the gain"}, {"symbol": "y", "meaning": "plain"}]

    state = cal.scan_formulas(formulas, product=product, draft=draft, pair_window=120,
                              max_formulas=100, target_pooled=2, target_kept_pairs=2)
    assert state["scanned"] == 2 and state["position"] == 2
    card = state["cards"][0]
    # the draft's duplicate of the kept pair collapses into the product entry
    assert [(p["symbol"], p["origin"]) for p in card["pairs"]] == [("a", "kept"), ("y", "draft")]
    assert drafted == ["f0", "f1"]
    resumed = cal.scan_formulas(formulas, product=product, draft=draft, pair_window=120,
                                max_formulas=100, target_pooled=10, target_kept_pairs=10,
                                start=state["position"], state=state)
    assert resumed["scanned"] == 6 and resumed["pooled"] == 6


def test_same_document_in_two_notebooks_is_one_item():
    # pool B and pool C copies of one formula and context: one scan, one card.
    copies = [_formula(0, "where a is the gain"), {**_formula(0, "where a is the gain"), "ref": "other-nb"}]
    calls = []
    state = cal.scan_formulas(copies, product=lambda f: calls.append(f["ref"]) or ('{"variables": []}', None),
                              draft=lambda f: [{"symbol": "a", "meaning": "the gain"}],
                              pair_window=120, max_formulas=100)
    assert calls == ["f0"] and state["scanned"] == 1 and len(state["cards"]) == 1
    # alias candidates: the same element text and concept in two notebooks, pool A preferred
    twin = [_local_concept("B", ("nb1", "s"), "AMP"), _local_concept("C", ("nb2", "s"), "AMP")]
    a_copy = {**_local_concept("C", ("en_slice", "x"), "AMP"), "pool": "A"}
    out = cal.alias_candidates(twin + [a_copy], egress=False)["R4"]
    assert len(out) == 1 and out[0]["pool"] == "A"
    report = cal.build_report({"alias_items": out + out, "formula_cards": [], "empty_checks": [],
                               "formula_scan": {}}, {out[0]["key"]: {"verdict": "correct"}})
    assert report["alias"][3]["n"] == 1                      # counted once


def test_budget_stop_keeps_the_scan_position():
    formulas = [_formula(i, f"where a is the gain {i}") for i in range(4)]
    state = {"cards": [], "hint_empty": [], "status": cal.Counter(), "scanned": 0, "kept_pairs": 0, "pooled": 0}

    def product(formula):
        if formula["ref"] == "f2":
            raise cal.BudgetExceeded("cap")
        return '{"variables": [{"symbol": "a", "meaning": "the gain"}]}', None

    with pytest.raises(cal.BudgetExceeded):
        cal.scan_formulas(formulas, product=product, draft=lambda f: [], pair_window=120,
                          max_formulas=100, target_pooled=99, state=state)
    assert (state["scanned"], state["position"], len(state["cards"])) == (2, 2, 2)


def test_assign_blind_keeps_existing_marks_and_samples_new_items_only():
    old = [{"key": f"o{i}", "blind": i == 0} for i in range(10)]
    new = [{"key": f"n{i}"} for i in range(20)]
    cal.assign_blind(old + new, seed=1)
    assert [e["blind"] for e in old] == [True] + [False] * 9
    assert sum(e["blind"] for e in new) == 2


class _Info:
    def __init__(self, host, hostaddr=""):
        self.host, self.hostaddr = host, hostaddr


@pytest.mark.parametrize("host, hostaddr, ok", [
    ("127.0.0.1", "", True), ("/tmp", "", True), ("db.example.org", "", False), ("localhost", "10.0.0.5", False)])
def test_connection_host_is_rechecked_after_connect(host, hostaddr, ok):
    conn = type("C", (), {"info": _Info(host, hostaddr)})()
    if ok:
        cal.assert_local_connection(conn)
    else:
        with pytest.raises(SystemExit):
            cal.assert_local_connection(conn)


class _Judge:
    configured = True
    model = "judge-model"

    def __init__(self, fail_kinds=()):
        self.fail_kinds = fail_kinds

    def chat_json(self, messages, hint):
        kind = {cal.DRAFT_HINT: "draft", cal.ALIAS_PRELABEL_HINT: "alias", cal.PAIR_PRELABEL_HINT: "pair"}[hint]
        if kind in self.fail_kinds:
            raise RuntimeError("upstream")
        return {"draft": '{"pairs": []}', "alias": '{"verdict": "correct", "reason": ""}',
                "pair": '{"judgements": [{"i": 0, "verdict": "correct", "reason": ""}]}'}[kind]


def _prepare_args(**over):
    base = dict(max_calls=1000, max_input_chars=10_000_000, allow_local_content_egress=False, top_up=False,
                top_up_formulas=20, max_formulas_per_language=900, seed=1)
    return type("A", (), {**base, **over})()


def _pool_a_inputs():
    fixture = _fixture()
    fixture["formulas"][0]["reply"] = '{"variables": [{"symbol": "a", "meaning": "the gain"}]}'
    concepts, formulas = cal.pool_a(fixture)
    alias = cal.sample_aliases(cal.alias_candidates(concepts, egress=False), per_rule=5, seed=1)
    return fixture, concepts, {"en": formulas, "zh": []}, alias


def test_execute_prepare_records_failed_judge_calls_and_goes_on():
    fixture, concepts, lists, alias = _pool_a_inputs()
    items = cal._execute_prepare(_prepare_args(), fixture, None, _Judge(fail_kinds=("alias", "draft")),
                                 lists, alias, concepts=concepts, previous=None, max_chars=50,
                                 pair_window=120, hosts={"formula": "f.host", "judge": "j.host"})
    assert items["incomplete"] is None
    assert items["alias_items"][0]["prelabel"] is None                    # failed -> None, retried on top-up
    assert items["formula_cards"][0]["pairs"][0]["prelabel"]["verdict"] == "correct"
    assert items["egress_log"]["failures"] == {"draft:RuntimeError": 1, "alias_prelabel:RuntimeError": 1}
    assert "fingerprint_full" not in json.dumps(items)


def test_execute_prepare_saves_finished_work_at_the_budget_cap():
    fixture, concepts, lists, alias = _pool_a_inputs()
    items = cal._execute_prepare(_prepare_args(max_calls=1), fixture, None, _Judge(), lists, alias,
                                 concepts=concepts, previous=None, max_chars=50, pair_window=120,
                                 hosts={"formula": "f.host", "judge": "j.host"})
    assert items["incomplete"] and len(items["formula_cards"]) == 1          # draft sent, then cap
    assert items["scan_state"]["en"]["position"] == 1


def test_execute_prepare_refuses_local_text_without_egress():
    fixture, concepts, lists, alias = _pool_a_inputs()
    lists["en"][0]["pool"] = "B"
    product = _FakeClient(reply='{"variables": []}')
    with pytest.raises(AssertionError):
        cal._execute_prepare(_prepare_args(), fixture, product, _Judge(), lists, [], concepts=concepts,
                             previous=None, max_chars=50, pair_window=120,
                             hosts={"formula": "f.host", "judge": "j.host"})
    assert product.calls == []                     # the product call itself is refused, not only drafts


def test_scan_formulas_records_hint_but_empty_and_skips_draft_without_hint():
    empty = '{"variables": []}'
    formulas = [_formula(0, "where nothing is said"), _formula(1, "plain words")]
    drafted = []
    state = cal.scan_formulas(
        formulas, product=lambda f: (empty, None), draft=lambda f: drafted.append(f["ref"]) or [],
        pair_window=120, max_formulas=100)
    assert drafted == ["f0"]                           # hint word -> draft asked; no hint -> not
    assert [c["ref"] for c in state["hint_empty"]] == ["f0"]
    assert state["status"] == {"none_stated": 2} and state["cards"] == []
    failed = cal.scan_formulas([_formula(0, "x")], product=lambda f: (None, "Timeout"),
                               draft=lambda f: [], pair_window=120, max_formulas=1)
    assert failed["status"] == {"failed": 1}


@pytest.mark.parametrize("raw, expected", [
    ('{"pairs": [{"symbol": " a ", "meaning": "gain"}, {"symbol": "", "meaning": "x"}, {"symbol": 1}]}',
     [{"symbol": "a", "meaning": "gain"}]),
    ("nope", []), ('{"pairs": {}}', []), (None, []),
])
def test_parse_draft_reply(raw, expected):
    assert cal.parse_draft_reply(raw) == expected


def test_parse_verdict_and_judgements():
    assert cal.parse_verdict('{"verdict": "unsure", "reason": "r"}') == {"verdict": "unsure", "reason": "r"}
    assert cal.parse_verdict('{"verdict": "maybe"}') is None
    raw = '{"judgements": [{"i": 1, "verdict": "correct", "reason": ""}, {"i": 5, "verdict": "correct"},' \
          ' {"i": 0, "verdict": "bad"}]}'
    assert cal.parse_judgements(raw, 2) == [None, {"verdict": "correct", "reason": ""}]


def test_scan_lists_scan_local_pools_only_with_egress():
    a = [_formula(0, "t", pool="A")]
    local = [_formula(1, "t", pool="B"), _formula(2, "t", language="zh", pool="C")]
    off = cal._scan_lists(a, local, seed=1, egress=False)
    assert [f["ref"] for f in off["en"]] == ["f0"] and off["zh"] == []
    on = cal._scan_lists(a, local, seed=1, egress=True)
    assert sorted(f["ref"] for f in on["en"]) == ["f0", "f1"] and [f["ref"] for f in on["zh"]] == ["f2"]


def test_plan_calls_and_budget_table_split_host_pool_kind():
    alias = cal.alias_candidates([_local_concept("C", ("nb", "s"), "AMP")], egress=True)["R4"]
    lists = {"en": [_formula(0, "t", pool="A"), _formula(1, "t", pool="B")],
             "zh": [_formula(2, "t", language="zh", pool="C")]}
    rows = cal.plan_calls(alias, lists, formula_host="api.model", judge_host="judge.host", max_formulas=900)
    table = cal.budget_table(rows)
    keys = {(r["host"], r["pool"], r["kind"]): r for r in table}
    assert ("api.model", "A", "formula") not in keys            # pool A replies come from the fixture
    assert keys[("api.model", "B", "formula")]["calls"] == 1
    assert keys[("judge.host", "C", "alias_prelabel")]["needs_egress"] is True
    assert keys[("judge.host", "A", "draft")]["needs_egress"] is False
    assert all(r["chars"] > 0 for r in table)
    rendered = cal.render_budget_table(table)
    assert rendered.splitlines()[0].startswith("| 端点主机 | 池 | 调用类别")
    assert f"| 合计 | | | {sum(r['calls'] for r in table)} |" in rendered


def test_evidence_items_follow_b11():
    found = {"own": {"id": "own", "notebook_id": "nb", "text": "full text", "source_id": "s"},
             "far": {"id": "far", "notebook_id": "other", "text": "secret", "source_id": "t"}}
    row = {"notebook_id": "nb", "evidence": [
        {"element_id": "own", "quoted_span": "q1"}, {"element_id": "far", "quoted_span": "q2"},
        {"element_id": "gone", "quoted_span": "q3"}, {"element_id": "", "quoted_span": "q4"}, "junk"]}
    assert cal._evidence_items(row, found) == [
        {"element_id": "own", "element_text": "full text", "element_read": True},
        {"element_id": "", "element_text": "q2", "element_read": False},
        {"element_id": "gone", "element_text": "q3", "element_read": False},
        {"element_id": "", "element_text": "q4", "element_read": False},
    ]


def test_alias_recall_reference_counts_abbreviation_shaped_gold(tmp_path):
    gold = tmp_path / "fangan" / "testcases" / "book" / "ch"
    gold.mkdir(parents=True)
    (gold / "gold.yaml").write_text(
        "canonicalization:\n- canonical: x\n  aliases: [PLL, phase locked loop, VCO]\n", encoding="utf-8")
    concepts = [{"pool": "A", "hits": [{"rule": "R2", "text": "PLL"}]},
                {"pool": "B", "hits": [{"rule": "R2", "text": "VCO"}]}]
    out = cal.alias_recall_reference(concepts, repo_root=tmp_path)
    assert out == {"gold_aliases": 3, "abbreviation_shaped": 2,
                   "hit_by_rule": {"R1": 0, "R2": 1, "R3": 0, "R4": 0}, "hit_any_rule": 1}


# --- render / ingest / report (plan G0-T5) ------------------------------------

_SECRET = "LOCAL-ONLY-SOURCE-TEXT"


def _run_dir(tmp_path):
    items = _items()
    items["alias_items"][0]["segments"] = [["e", _SECRET]]
    items["alias_items"][1]["pool"] = "B"
    items["alias_items"][1]["name"] = _SECRET
    for alias in items["alias_items"]:
        alias.setdefault("pool", "A")
    items["formula_cards"][0]["pool"] = "A"
    (tmp_path / "items.json").write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return items


def test_write_review_renders_the_items(tmp_path):
    _run_dir(tmp_path)
    page = cal.write_review(tmp_path).read_text(encoding="utf-8")
    tags = _Tags()
    tags.feed(page)
    data = json.loads(tags.data["kg-data"])
    assert data["run"] == "r" and len(data["cards"]) == 101
    stored = json.loads((tmp_path / "items.json").read_text(encoding="utf-8"))
    assert stored["ids"][cal.opaque_id("a1")] == "a1"                  # the id -> key map stays local


def test_ingest_merges_known_keys_only(tmp_path):
    _run_dir(tmp_path)
    exported = tmp_path / "kg_attrs_labels.json"
    oid = cal.opaque_id
    exported.write_text(json.dumps({"labels": [
        {"key": oid("a1"), "verdict": "correct"}, {"key": oid("g1"), "verdict": "unsure"},
        {"key": oid("c-en"), "missing": 2}, {"key": oid("nope"), "verdict": "correct"},
        {"key": "a2", "verdict": "correct"}]}), encoding="utf-8")       # a raw key is not an id
    assert cal.ingest_labels(tmp_path, exported) == (3, 2)
    exported.write_text(json.dumps({"labels": [{"key": oid("a1"), "verdict": "incorrect"}]}), encoding="utf-8")
    assert cal.ingest_labels(tmp_path, exported) == (1, 0)
    stored = json.loads((tmp_path / "labels.json").read_text(encoding="utf-8"))
    assert stored == {"a1": {"verdict": "incorrect"}, "g1": {"verdict": "unsure"}, "c-en": {"missing": 2}}
    exported.write_text(json.dumps({"labels": [{"key": "a1", "verdict": "correct", "text": "x"}]}),
                        encoding="utf-8")
    with pytest.raises(ValueError):
        cal.ingest_labels(tmp_path, exported)


def test_report_json_carries_no_source_text_and_examples_stay_in_pool_a(tmp_path):
    _run_dir(tmp_path)
    labels = {f"a{i}": {"verdict": "incorrect"} for i in range(8)}
    labels["g1"] = {"verdict": "incorrect"}
    labels["g2"] = {"verdict": "correct"}
    (tmp_path / "labels.json").write_text(json.dumps(labels), encoding="utf-8")
    report = cal.write_report(tmp_path)
    assert _SECRET not in (tmp_path / "report.json").read_text(encoding="utf-8")
    assert report["alias"][0]["verdict"] == "fail"
    examples = cal.error_examples(json.loads((tmp_path / "items.json").read_text(encoding="utf-8")), labels)
    alias_examples = [e for e in examples if e.startswith("别名 R1")]
    assert len(alias_examples) == cal.MAX_ERROR_EXAMPLES
    assert not any(_SECRET in e for e in examples)              # pool B item never shown
    assert any(e.startswith("公式保留但判错") for e in examples)
    assert any(e.startswith("公式误杀（too_far）") for e in examples)
    assert "## 错例" in (tmp_path / "report.md").read_text(encoding="utf-8")


def test_cli_wires_every_subcommand():
    parser = cal.build_parser()
    for command in ("extract-en", "render", "report"):
        assert parser.parse_args([command]).run.__name__ == f"run_{command.replace('-', '_')}"
    args = parser.parse_args(["prepare", "--database-url", "postgresql:///x", "--dry-run"])
    assert (args.run.__name__, args.dry_run, args.confirm_budget, args.allow_local_content_egress) == (
        "run_prepare", True, False, False)
    assert (args.max_calls, args.max_input_chars, args.per_rule) == (4000, 20_000_000, 100)
    assert parser.parse_args(["ingest", "--labels", "f.json"]).run.__name__ == "run_ingest"
