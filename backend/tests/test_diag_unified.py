"""Tests for the unified diagnostics entry `scripts/diag.py`.

`diag.py` is a thin stdlib dispatcher over the three canonical slow-phenomenon
engines (kept untouched so their line-pinned / surface-manifest guards stay green):

  - `slow`         -> delegates in-process to `scripts/diag_slow.py` (stdlib, app-free)
  - `latency`      -> self-contained stdlib ask_stage percentile view
                      (mirrors `app.eval.ask_latency.aggregate_stage_latencies`)
  - `base-recall`  -> lazily imports `scripts/diag_base_report.py` (which imports app)

Contract under test:
  1. Offline subcommands (`slow`, `latency`, bare) NEVER import the heavy app
     (`app.core.config` / `app.services.sqlite_repository`) — diag_slow's whole
     value is being runnable on a bare host that owns `.local/`.
  2. `latency` aggregation is byte-for-byte identical to the tested canonical
     `app.eval.ask_latency.aggregate_stage_latencies` (drift guard).
  3. Dispatch works: bare invocation == `slow`; unknown subcommand fails clean.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO_ROOT / "scripts"
_DIAG = _SCRIPTS / "diag.py"


def _load_diag():
    """Import scripts/diag.py as a module (scripts/ is not a package)."""
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location("diag", _DIAG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_ask_stage(stage: str, latency_ms: int) -> dict:
    return {"kind": "ask_stage", "stage": stage, "latency_ms": latency_ms,
            "notebook_id": "nb-test", "ts": "2026-07-13T00:00:00", "channel": "events"}


# ---------------------------------------------------------------------------
# 1. file exists & dispatches
# ---------------------------------------------------------------------------

def test_diag_file_exists():
    assert _DIAG.is_file(), "scripts/diag.py must exist"


def test_dispatch_table_has_three_subcommands():
    diag = _load_diag()
    subs = diag.SUBCOMMANDS if hasattr(diag, "SUBCOMMANDS") else {}
    assert set(subs) >= {"slow", "latency", "base-recall"}


def test_unknown_subcommand_fails_clean():
    r = subprocess.run([sys.executable, str(_DIAG), "no-such-cmd"],
                       capture_output=True, text=True)
    assert r.returncode != 0
    assert "no-such-cmd" in (r.stderr + r.stdout)


# ---------------------------------------------------------------------------
# 2. latency aggregation == canonical ask_latency (drift guard)
# ---------------------------------------------------------------------------

def test_latency_aggregation_matches_ask_latency():
    from app.eval.ask_latency import aggregate_stage_latencies

    diag = _load_diag()
    records = (
        [_make_ask_stage("score", v) for v in [10, 20, 30, 40, 100]]
        + [_make_ask_stage("answer_llm", v) for v in [1000, 2000, 3000]]
        + [_make_ask_stage("total", v) for v in [1234]]
        + [{"kind": "other", "x": 1}]  # non ask_stage ignored
    )
    assert diag._aggregate_stage(records) == aggregate_stage_latencies(records)


def test_latency_subcommand_end_to_end(tmp_path):
    log = tmp_path / "events.jsonl"
    recs = [_make_ask_stage("score", v) for v in [10, 20, 30, 40, 100]]
    log.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")

    r = subprocess.run([sys.executable, str(_DIAG), "latency", "--log", str(log)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "score" in r.stdout
    # p50 of [10,20,30,40,100] nearest-rank ceiling -> 30
    assert "30.0" in r.stdout


def test_latency_reads_per_user_subdirs(tmp_path):
    """Mirror ask_latency: global file + per-user subdir files are merged."""
    (tmp_path / "events.jsonl").write_text(
        json.dumps(_make_ask_stage("score", 10)) + "\n", encoding="utf-8")
    sub = tmp_path / "user-abc"
    sub.mkdir()
    (sub / "events.jsonl").write_text(
        json.dumps(_make_ask_stage("score", 90)) + "\n", encoding="utf-8")

    diag = _load_diag()
    records = list(diag._read_ask_stage(str(tmp_path / "events.jsonl"), None))
    latencies = sorted(r["latency_ms"] for r in records)
    assert latencies == [10, 90], "must aggregate global + per-user subdir logs"


# ---------------------------------------------------------------------------
# 3. offline purity — no heavy app import
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("args", [
    "latency --log {log}",
    "slow --root {root}",
    "--root {root}",  # bare -> slow
])
def test_offline_subcommands_do_not_import_app(tmp_path, args):
    log = tmp_path / "events.jsonl"
    log.write_text(json.dumps(_make_ask_stage("score", 5)) + "\n", encoding="utf-8")
    (tmp_path / ".local" / "logs").mkdir(parents=True)
    argv = args.format(log=log, root=tmp_path).split()
    code = (
        "import sys;"
        f"sys.path.insert(0, {str(_SCRIPTS)!r});"
        "import diag;"
        f"diag.main({argv!r});"
        "bad=[m for m in ('app.core.config','app.services.sqlite_repository') if m in sys.modules];"
        "sys.exit('IMPORTED APP: '+repr(bad) if bad else 0)"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=str(_REPO_ROOT))
    assert r.returncode == 0, f"offline subcommand imported app: {r.stderr or r.stdout}"


def test_importing_diag_does_not_import_app():
    """Merely importing diag (before dispatch) must not pull app — base-recall is lazy."""
    code = (
        "import sys;"
        f"sys.path.insert(0, {str(_SCRIPTS)!r});"
        "import diag;"
        "bad=[m for m in ('app.core.config','app.services.sqlite_repository') if m in sys.modules];"
        "sys.exit('IMPORTED APP: '+repr(bad) if bad else 0)"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=str(_REPO_ROOT))
    assert r.returncode == 0, r.stderr or r.stdout


def test_slow_bare_defaults_to_slow(tmp_path):
    (tmp_path / ".local" / "logs").mkdir(parents=True)
    r = subprocess.run([sys.executable, str(_DIAG), "--root", str(tmp_path)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "慢因诊断" in r.stdout
