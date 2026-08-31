from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"


def load_slow():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("diag_slow", SCRIPTS / "diag_slow.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_request_report_caps_distinct_path_output(tmp_path, capsys):
    logs = tmp_path / "logs"
    logs.mkdir()
    timestamp = datetime.now().isoformat()
    rows = [
        {
            "id": str(index),
            "kind": "http",
            "method": "GET",
            "path": "/api/diagnostics/" + "/".join(
                ("ask", "knowledge", "memory", "reports", "sources", "search")[(index // (6 ** place)) % 6]
                for place in range(4)
            ),
            "latency_ms": 5000,
            "ts": timestamp,
        }
        for index in range(400)
    ]
    (logs / "requests.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    slow = load_slow()
    slow.report_requests(str(tmp_path), timedelta(hours=1), 3000)
    output = capsys.readouterr().out

    assert len(output.encode("utf-8")) <= slow.REQUEST_REPORT_OUTPUT_BYTES
    assert "output_truncated=True" in output


def test_event_report_never_prints_raw_model_error_text(tmp_path, capsys):
    logs = tmp_path / "logs"
    logs.mkdir()
    secret = "Bearer prod-auth-secret prompt=do-not-print"
    (logs / "events.jsonl").write_text(json.dumps({
        "kind": "model_error",
        "stage": "answer",
        "model": "provider-private-model",
        "error": secret,
        "ts": datetime.now().isoformat(),
    }) + "\n", encoding="utf-8")

    slow = load_slow()
    slow.report_events(str(tmp_path), timedelta(hours=1))
    output = capsys.readouterr().out

    assert secret not in output
    assert "provider-private-model" not in output
    assert "model_error" in output
    assert "present" in output


def test_event_report_aggregates_retrieval_leaf_stages(tmp_path, capsys):
    logs = tmp_path / "logs"
    logs.mkdir()
    timestamp = datetime.now().isoformat()
    rows = [
        {"kind": "ask_stage", "stage": stage, "site": stage,
         "latency_ms": latency, "total_ms": latency, "ts": timestamp}
        for stage, latency in (
            ("chunk_scale_index", 7),
            ("chunk_ann", 11),
            ("chunk_fts", 13),
            ("kg_candidates", 17),
        )
    ]
    (logs / "events.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )

    slow = load_slow()
    slow.report_events(str(tmp_path), timedelta(hours=1))
    output = capsys.readouterr().out

    for stage in ("chunk_scale_index", "chunk_ann", "chunk_fts", "kg_candidates"):
        assert stage in output


def test_default_report_caps_all_sections(tmp_path, capsys, monkeypatch):
    slow = load_slow()
    monkeypatch.setattr(slow, "report_requests", lambda *args: print("x" * 40_000))
    monkeypatch.setattr(sys, "argv", ["diag_slow.py", "--local", str(tmp_path), "--root", str(tmp_path)])
    assert slow.main() == 0
    output = capsys.readouterr().out

    assert len(output.encode("utf-8")) <= slow.DEFAULT_REPORT_OUTPUT_BYTES
    assert "output_truncated=True" in output


def test_db_target_prefers_the_explicit_root_over_the_local_directory(
    tmp_path, monkeypatch
):
    """`--root /repo --local /elsewhere` must resolve config from the root.

    Deriving the root from `local_dir` only holds when .local sits inside the
    repository.  With a custom `--local` the guess lands beside that directory,
    finds no .env, defaults to SQLite, and every guarded section goes back to
    reading a stale file on a PostgreSQL deployment.
    """
    slow = load_slow()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # check.sh 全局设 SILICON_NOTEBOOK_ENV_FILE=""(不读任何 env 文件)以隔离
    # 真实凭据;本用例要验证的正是 .env 读取,所以显式解除该隔离。
    monkeypatch.delenv("SILICON_NOTEBOOK_ENV_FILE", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("DATABASE_URL=postgresql://h/db\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere" / "local"
    elsewhere.mkdir(parents=True)

    assert slow._db_target(str(elsewhere), str(repo)).backend == "postgres"
    # Without the explicit root the custom directory really does resolve to
    # nothing — which is why the root has to be threaded through.
    assert slow._db_target(str(elsewhere)).is_sqlite

    # The in-repo layouts keep working with no root passed.
    for rel in (".local", "backend/.local"):
        nested = repo / rel
        nested.mkdir(parents=True, exist_ok=True)
        assert slow._db_target(str(nested)).backend == "postgres"


def test_artifacts_section_never_reports_the_stale_sqlite_file_on_postgres(
    tmp_path, monkeypatch, capsys
):
    """Saying "skipping SQLite files" and then printing them is a contradiction.

    The earlier fallback restored the fixed path when `resolve_sqlite_file()`
    returned None, so a PostgreSQL deployment still saw that stale database's
    size and its WAL warning right below the skip message.
    """
    slow = load_slow()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SILICON_NOTEBOOK_ENV_FILE", raising=False)
    repo = tmp_path / "repo"
    (repo / ".local").mkdir(parents=True)
    (repo / ".env").write_text("DATABASE_URL=postgresql://h/db\n", encoding="utf-8")
    local_dir = repo / ".local"
    # A leftover SQLite file from before the migration is exactly the trap.
    (local_dir / "silicon_notebook.db").write_bytes(b"stale")
    (local_dir / "silicon_notebook.db-wal").write_bytes(b"stale-wal")

    slow.report_artifacts(str(local_dir), deep=False, root=str(repo))

    out = capsys.readouterr().out
    assert "跳过 SQLite 文件" in out
    assert "silicon_notebook.db" not in out
    assert "WAL" not in out

    # On a real SQLite deployment the same section still reports the file.
    (repo / ".env").write_text("DATABASE_URL=sqlite:///.local/silicon_notebook.db\n",
                               encoding="utf-8")
    slow.report_artifacts(str(local_dir), deep=False, root=str(repo))
    sqlite_out = capsys.readouterr().out
    assert "silicon_notebook.db" in sqlite_out
    assert "跳过 SQLite 文件" not in sqlite_out
