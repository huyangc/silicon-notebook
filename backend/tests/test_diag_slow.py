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
            "path": f"/api/diagnostics/segment{index:04d}alpha/segment{index:04d}beta/segment{index:04d}gamma",
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
