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


def test_default_report_caps_all_sections(tmp_path, capsys, monkeypatch):
    slow = load_slow()
    monkeypatch.setattr(slow, "report_requests", lambda *args: print("x" * 40_000))
    monkeypatch.setattr(sys, "argv", ["diag_slow.py", "--local", str(tmp_path), "--root", str(tmp_path)])
    assert slow.main() == 0
    output = capsys.readouterr().out

    assert len(output.encode("utf-8")) <= slow.DEFAULT_REPORT_OUTPUT_BYTES
    assert "output_truncated=True" in output
