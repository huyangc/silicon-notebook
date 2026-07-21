from __future__ import annotations

import gzip
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"


def load_common():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("diag_common", SCRIPTS / "diag_common.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def line(identifier, ts, latency=10):
    return json.dumps({
        "id": identifier,
        "kind": "http",
        "channel": "requests",
        "method": "GET",
        "path": "/api/notebooks/nb-secret/sources",
        "latency_ms": latency,
        "ts": ts,
    }) + "\n"


def test_reads_legacy_daily_gzip_and_per_user_once(tmp_path):
    (tmp_path / "requests.jsonl").write_text(line("legacy", "2026-07-20T09:00:00"))
    duplicate = line("daily", "2026-07-21T09:00:00", 20)
    (tmp_path / "requests-2026-07-21.jsonl").write_text(duplicate + "{broken\n")
    with gzip.open(tmp_path / "requests-2026-07-21.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write(duplicate)
    user = tmp_path / "user-abc"
    user.mkdir()
    (user / "requests-2026-07-21.jsonl").write_text(line("user", "2026-07-21T10:00:00", 30))

    common = load_common()
    result = common.read_channel(
        tmp_path,
        "requests",
        since_hours=48,
        now=datetime.fromisoformat("2026-07-21T12:00:00"),
    )

    assert [row["id"] for row in result.records] == ["legacy", "daily", "user"]
    assert result.stats.files == 4
    assert result.stats.malformed == 1
    assert result.stats.duplicates == 1
    assert result.stats.retained == 3


def test_window_and_limit_keep_only_matching_newest_records(tmp_path):
    rows = [line(str(index), f"2026-07-21T{index:02d}:00:00") for index in range(10)]
    (tmp_path / "events-2026-07-21.jsonl").write_text("".join(rows))
    common = load_common()
    result = common.read_channel(
        tmp_path,
        "events",
        since_hours=4,
        limit=2,
        now=datetime.fromisoformat("2026-07-21T10:00:00"),
    )
    assert [row["id"] for row in result.records] == ["8", "9"]
    assert result.stats.matched == 4
    assert result.stats.retained == 2


def test_http_path_normalization_does_not_return_identifiers():
    common = load_common()
    value = common.normalize_http_path(
        "/api/notebooks/nb-private123/sources/src-private456?token=secret"
    )
    assert value == "/api/notebooks/{id}/sources/{id}"
    assert "private" not in value
    assert "token" not in value
