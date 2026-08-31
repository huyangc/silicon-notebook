"""Pure aggregation tests for the production retrieval timing diagnostic."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "scripts"
sys.path.insert(0, str(_SCRIPTS))
_SPEC = importlib.util.spec_from_file_location(
    "diag_retrieval_latency", _SCRIPTS / "diag_retrieval_latency.py"
)
diag = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules[_SPEC.name] = diag
_SPEC.loader.exec_module(diag)


def test_report_groups_leaf_and_components_without_rendering_ids():
    secret_notebook = "nb-sensitive-id"
    records = [
        {
            "kind": "ask_stage",
            "site": "chunk_fts",
            "notebook_id": secret_notebook,
            "status": "timeout",
            "chunk_fts_ms": 1000,
        },
        {
            "kind": "ask_stage",
            "site": "chunk_ann",
            "notebook_id": secret_notebook,
            "ann_open_ms": 10,
            "knn_ms": 20,
            "chunk_fts_ms": 1000,
            "total_ms": 1040,
        },
        {
            "kind": "retrieval_run_stats",
            "run_kind": "report_generation",
            "chunk_fts_timeouts": 1,
            "chunk_fts_circuit_skips": 3,
            "fanout_waits": 2,
            "fanout_wait_ms": 40,
        },
    ]

    report = diag.build_report(
        records,
        {secret_notebook: 1_870_000},
        medium_chunks=100_000,
        large_chunks=500_000,
    )

    assert "large(>=500000)" in report
    assert "timeout=1" in report
    assert "ann_open_ms" in report
    assert "report_generation" in report
    assert "fts_timeouts=1" in report
    assert secret_notebook not in report


def test_unknown_manifest_stays_explicit():
    report = diag.build_report(
        [{
            "kind": "ask_stage",
            "site": "chunk_fts",
            "notebook_id": "nb-missing",
            "status": "ok",
            "latency_ms": 12,
        }],
        {},
    )

    assert "unknown" in report
    assert "ok=1" in report
