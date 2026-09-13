"""Offline trace aggregation preserves missing-data and privacy semantics."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "reasoning_trace_analyzer", ROOT / "scripts/analyze_reasoning_trace.py")
analyze = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(analyze)


def test_unknown_values_are_not_zeros_and_quantiles_use_observed_samples():
    rows = [{"total_ms": 10}, {"total_ms": None}, {"total_ms": 0}, {}]
    summary = analyze.summarize_group(rows, min_samples=3)
    assert summary["numeric"]["total_ms"] == {
        "n_observed": 2, "n_missing": 2, "mean": 5.0,
    }
    measured = analyze.summarize_group(rows, min_samples=2)
    assert measured["numeric"]["total_ms"]["p50"] == 0
    assert measured["numeric"]["total_ms"]["p95"] == 10


def test_boolean_counts_keep_missing_separate():
    summary = analyze.summarize_group([
        {"failed": True}, {"failed": False}, {"failed": None}, {},
    ], min_samples=5)
    assert summary["boolean"]["failed"] == {
        "n_observed": 2, "n_missing": 2, "n_true": 1,
    }


def test_failed_runs_and_recovery_counts_remain_in_the_report():
    report = analyze.build_report([
        {"effort": "deep", "status": "done", "total_ms": 12,
         "fallback_count": 1, "fallback_reasons": {"invalid_json": 1}},
        {"effort": "deep", "status": "failed", "total_ms": None,
         "fallback_count": 2, "fallback_reasons": {"provider_error": 2}},
    ], group_by=["effort"], min_samples=5)
    summary = report["groups"][0]["summary"]
    assert summary["n_runs"] == 2
    assert summary["categorical"]["status"] == {"done": 1, "failed": 1}
    assert summary["numeric"]["total_ms"]["n_missing"] == 1
    assert summary["counters"]["fallback_reasons"] == {
        "invalid_json": 1, "provider_error": 2,
    }


def test_citation_attribution_does_not_treat_unobserved_runs_as_zero():
    summary = analyze.summarize_group([
        {"citation_contribution": {"ppr": {
            "steps": 1, "steps_with_ids": 1, "unknown_steps": 0, "cited_hits": 0}}},
        {"citation_contribution": {"ppr": {
            "steps": 1, "steps_with_ids": 0, "unknown_steps": 1, "cited_hits": None}}},
    ], min_samples=5)
    assert summary["citation_contribution"]["ppr"] == {
        "steps": 2, "steps_with_ids": 1, "unknown_steps": 1,
        "cited_hits": 0, "runs_observed": 1, "runs_unknown": 1,
    }


def test_multiple_files_cli_preserves_groups_and_writes_both_formats(tmp_path):
    first, second = tmp_path / "first.jsonl", tmp_path / "second.jsonl"
    first.write_text(json.dumps({"effort": "deep", "total_ms": 12}) + "\n")
    second.write_text(json.dumps({"effort": "standard", "total_ms": 3}) + "\n")
    markdown, data = tmp_path / "out/report.md", tmp_path / "out/report.json"
    assert analyze.main([str(first), str(second), "--group-by", "effort",
                         "--min-samples", "2", "--out-md", str(markdown),
                         "--out-json", str(data)]) == 0
    report = json.loads(data.read_text())
    assert report["n_rows"] == 2
    assert [group["key"] for group in report["groups"]] == [["deep"], ["standard"]]
    assert set(report) == {"n_rows", "group_by", "min_samples", "groups"}
    assert "n=1(unknown)" in markdown.read_text()


@pytest.mark.parametrize("row", [
    {"question": "sensitive material"},
    {"status": "sensitive material"},
    {"fallback_reasons": {"sensitive material": 1}},
    ["sensitive material"],
])
def test_loader_rejects_non_projection_data_without_echoing_content(tmp_path, row):
    source = tmp_path / "invalid.jsonl"
    source.write_text(json.dumps(row))
    with pytest.raises(SystemExit) as error:
        analyze.load_rows([str(source)])
    assert "sensitive material" not in str(error.value)


def test_empty_input_keeps_an_empty_report(tmp_path):
    source = tmp_path / "empty.jsonl"
    source.write_text("\n")
    report = analyze.build_report(analyze.load_rows([str(source)]),
                                  group_by=["effort"], min_samples=5)
    assert report["n_rows"] == 0 and report["groups"] == []


@pytest.mark.parametrize("minimum", ["0", "-1"])
def test_cli_rejects_nonpositive_quantile_threshold(tmp_path, minimum):
    source = tmp_path / "empty.jsonl"
    source.write_text("")
    with pytest.raises(SystemExit):
        analyze.main([str(source), "--min-samples", minimum])


def test_missing_group_value_gets_its_own_unknown_bucket():
    grouped = analyze.group_rows([{}, {"effort": None}, {"effort": "deep"}], ["effort"])
    assert len(grouped[("unknown",)]) == 2
    assert len(grouped[("deep",)]) == 1
