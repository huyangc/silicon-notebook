"""Tests for the unified diagnostics entry ``scripts/diag.py``.

``diag.py`` is a thin dispatcher over the seven canonical diagnostic commands:

  - ``incident`` -> bounded live capture (stdlib, app-free)
  - ``slow`` -> historical slow-path report (stdlib, app-free)
  - ``latency`` -> ask-stage percentiles (stdlib, app-free)
  - ``locks`` -> SQLite write-lock wait/hold distribution by call site
    (self-contained stdlib aggregation of db_write_lock_slow /
    db_write_lock_stats; no separate engine file)
  - ``open`` -> notebook-open latency evidence (stdlib, app-free)
  - ``db`` -> source-side-effect-free SQLite evidence (stdlib, app-free)
  - ``base-recall`` -> source-side-effect-free base-reference metadata (stdlib, app-free)

Contract under test:
  1. The command surface is exactly seven commands, with no obsolete aliases.
  2. All seven host-side commands never import ``app``.
  3. Every command has bounded help that works without cwd/PYTHONPATH assumptions.
  4. Bare/leading-flag invocation remains the compatibility alias for ``slow``.
  5. ``latency`` remains equivalent to the canonical application aggregation.
"""
from __future__ import annotations

import importlib.util
import gzip
import json
import os
import subprocess
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO_ROOT / "scripts"
_DIAG = _SCRIPTS / "diag.py"
_COMMANDS = ("incident", "slow", "latency", "locks", "open", "db", "base-recall")
_OFFLINE_COMMANDS = _COMMANDS
# Repository evidence from `git show HEAD:scripts/diag.py`: these were the
# complete pre-Task 9 unified keys.  All remain approved, so no obsolete alias
# needs compatibility treatment or an invented name.
_PRE_TASK9_COMMANDS = frozenset(("slow", "latency", "base-recall"))
_OBSOLETE_COMMANDS = tuple(sorted(_PRE_TASK9_COMMANDS.difference(_COMMANDS)))


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


def _without_layout_whitespace(value: str) -> str:
    """Undo argparse line wrapping while preserving every non-space byte."""
    return "".join(value.split())


def _assert_paths_absent(result: subprocess.CompletedProcess, *paths: Path) -> None:
    combined = _without_layout_whitespace(result.stdout + result.stderr)
    for path in paths:
        raw = Path(os.path.abspath(os.fspath(path)))
        resolved = Path(os.path.realpath(os.fspath(raw)))
        for candidate in {raw, resolved}:
            assert _without_layout_whitespace(os.fspath(candidate)) not in combined


def _help_command_section(output: str) -> tuple[str, ...]:
    _, section = output.split("子命令:\n", 1)
    rows = section.split("\n\n", 1)[0].splitlines()
    return tuple(
        row.strip().split(maxsplit=1)[0]
        for row in rows
        if len(row) - len(row.lstrip()) == 4
    )


# ---------------------------------------------------------------------------
# 1. file exists & dispatches
# ---------------------------------------------------------------------------

def test_diag_file_exists():
    assert _DIAG.is_file(), "scripts/diag.py must exist"


def test_unknown_subcommand_fails_clean():
    r = subprocess.run([sys.executable, str(_DIAG), "no-such-cmd"],
                       capture_output=True, text=True)
    assert r.returncode == 2
    assert "no-such-cmd" not in (r.stderr + r.stdout)
    assert "未知子命令" in (r.stderr + r.stdout)


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


def test_latency_aggregation_preserves_retrieval_leaf_stage_names():
    diag = _load_diag()
    records = [
        _make_ask_stage("chunk_scale_index", 7),
        _make_ask_stage("chunk_ann", 11),
        _make_ask_stage("chunk_fts", 13),
        _make_ask_stage("kg_candidates", 17),
    ]

    assert set(diag._aggregate_stage(records)) == {
        "chunk_scale_index", "chunk_ann", "chunk_fts", "kg_candidates",
    }


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


def test_latency_reads_rotated_gzip_file_once(tmp_path):
    """A daily gzip log is sufficient input for the offline latency command."""
    with gzip.open(tmp_path / "events-2026-07-13.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(_make_ask_stage("score", 42)) + "\n")

    r = subprocess.run(
        [sys.executable, str(_DIAG), "latency", "--log", str(tmp_path / "events.jsonl")],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.count("score") == 1


# ---------------------------------------------------------------------------
# 3. offline purity — no heavy app import
# ---------------------------------------------------------------------------

def test_bare_and_leading_flag_slow_do_not_import_app(tmp_path):
    (tmp_path / ".local" / "logs").mkdir(parents=True)
    for args in ([], ["--root", str(tmp_path)]):
        code = (
            "import sys;"
            f"sys.path.insert(0, {str(_SCRIPTS)!r});"
            "import diag;"
            f"diag.main({args!r});"
            "bad=[m for m in sys.modules if m == 'app' or m.startswith('app.')];"
            "sys.exit('IMPORTED APP: '+repr(bad) if bad else 0)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr or result.stdout
        assert "慢因诊断" in result.stdout


def test_importing_diag_does_not_import_app():
    """Merely importing the dispatcher must not pull any application module."""
    code = (
        "import sys;"
        f"sys.path.insert(0, {str(_SCRIPTS)!r});"
        "import diag;"
        "bad=[m for m in sys.modules if m == 'app' or m.startswith('app.')];"
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


def _privacy_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "ABSOLUTE-SENSITIVE-ROOT"
    local = root / ".local"
    logs = local / "logs"
    logs.mkdir(parents=True)
    event_rows = [
        {
            "kind": "ask_stage",
            "stage": f"SENSITIVE-RAW-STAGE-{index}",
            "latency_ms": float("nan") if index == 0 else index,
            "notebook_id": "nb-SENSITIVE-PRIVATE-ID",
            "ts": "2026-07-22T00:00:00",
            "channel": "events",
        }
        for index in range(2500)
    ]
    (logs / "events.jsonl").write_text(
        "\n".join(json.dumps(row) for row in event_rows)
        + "\n{SENSITIVE-MALFORMED-CONTENT\n",
        encoding="utf-8",
    )
    (logs / "requests.jsonl").write_text(
        json.dumps({
            "kind": "http",
            "method": "GET",
            "path": "/api/notebooks/nb-SENSITIVE-PRIVATE-ID/sources/src-SENSITIVE-CONTENT",
            "latency_ms": 9000,
            "status": 200,
            "ts": "2026-07-22T00:00:00",
            "channel": "requests",
        }) + "\n",
        encoding="utf-8",
    )
    (logs / "llm.jsonl").write_text(
        json.dumps({
            "kind": "model_error",
            "model": "SENSITIVE-MODEL-CONTENT",
            "error": "Bearer SENSITIVE-AUTH-TOKEN",
            "latency_ms": 6000,
            "ts": "2026-07-22T00:00:00",
            "channel": "llm",
        }) + "\n",
        encoding="utf-8",
    )
    (root / ".env").write_text(
        "PORT=8000\nOPENAI_COMPAT_API_KEY=SENSITIVE-ENV-SECRET\n",
        encoding="utf-8",
    )
    database = local / "silicon_notebook.db"
    database.write_bytes(b"")
    return root, local, logs / "events.jsonl"


def _command_matrix(root: Path, local: Path, events: Path):
    missing_db = local / "silicon_notebook.db"
    unified = {
        "unified-incident": [_DIAG, "incident", "--root", root, "--local", local, "--pid", "2147483647"],
        "unified-slow": [_DIAG, "slow", "--root", root, "--local", local],
        "unified-latency": [_DIAG, "latency", "--log", events],
        "unified-locks": [_DIAG, "locks", "--log", events],
        "unified-open": [_DIAG, "open", "--root", root, "--local", local, "--notebook", "nb-SENSITIVE-PRIVATE-ID"],
        "unified-db": [_DIAG, "db", "--db", missing_db, "--notebook-id", "nb-SENSITIVE-PRIVATE-ID"],
        "unified-base-recall": [_DIAG, "base-recall", "--db", missing_db, "--notebook-id", "nb-SENSITIVE-PRIVATE-ID"],
    }
    direct = {
        "direct-incident": [_SCRIPTS / "diag_incident.py", "--root", root, "--local", local, "--pid", "2147483647"],
        "direct-slow": [_SCRIPTS / "diag_slow.py", "--root", root, "--local", local],
        "direct-open": [_SCRIPTS / "diag_open_latency.py", "--root", root, "--local", local, "--notebook", "nb-SENSITIVE-PRIVATE-ID"],
        "direct-db": [_SCRIPTS / "diag_db.py", "--db", missing_db, "--notebook-id", "nb-SENSITIVE-PRIVATE-ID"],
        "direct-base-recall": [_SCRIPTS / "diag_base_report.py", "--db", missing_db, "--notebook-id", "nb-SENSITIVE-PRIVATE-ID"],
    }
    return {**unified, **direct}
