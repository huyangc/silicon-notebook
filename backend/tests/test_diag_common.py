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


def test_http_path_normalization_redacts_opaque_share_tokens():
    common = load_common()
    token = "shr-opaque-share-token-without-digits"
    value = common.normalize_http_path(f"/shared/{token}/preview")
    assert value == "/shared/{token}/preview"
    assert token not in value


def test_http_path_normalization_fails_closed_for_search_terms_and_filenames():
    common = load_common()
    term = "confidential-analogue-design"
    filename = "customer-secret-notes.pdf"
    assert common.normalize_http_path(
        f"/api/notebooks/nb-private123/search/{term}"
    ) == "/api/notebooks/{id}/search/{redacted}"
    assert common.normalize_http_path(
        f"/api/notebooks/nb-private123/sources/{filename}"
    ) == "/api/notebooks/{id}/sources/{redacted}"
    assert term not in common.normalize_http_path(f"/api/search/{term}")
    assert filename not in common.normalize_http_path(f"/api/sources/{filename}")


def test_reader_does_not_parse_a_gzip_line_larger_than_the_hard_byte_bound(tmp_path, monkeypatch):
    common = load_common()
    payload = json.dumps({"id": "oversized", "payload": "x" * 4096}) + "\n"
    with gzip.open(tmp_path / "events-2026-07-21.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write(payload)
    loads = common.json.loads

    def reject_oversized(value, *args, **kwargs):
        assert len(value.encode("utf-8")) <= 128
        return loads(value, *args, **kwargs)

    monkeypatch.setattr(common.json, "loads", reject_oversized)
    result = common.read_channel(tmp_path, "events", max_input_bytes=128)

    assert result.records == ()
    assert result.stats.truncated is True


def test_reader_checks_deadline_before_parsing_input(tmp_path, monkeypatch):
    common = load_common()
    (tmp_path / "events.jsonl").write_text(line("deadline", "2026-07-21T10:00:00"))

    def fail_if_parsed(*args, **kwargs):
        raise AssertionError("deadline must stop before JSON parsing")

    monkeypatch.setattr(common.json, "loads", fail_if_parsed)
    result = common.read_channel(tmp_path, "events", deadline=0)

    assert result.records == ()
    assert result.stats.truncated is True


def test_report_pseudonyms_are_stable_within_one_report_and_reset_between_reports(capsys):
    common = load_common()
    raw_notebook = "customerNotebookAlpha"

    def render():
        print(common.pseudonym("notebook", raw_notebook))
        print(common.pseudonym("notebook", raw_notebook))
        print(common.pseudonym("notebook", "customerNotebookBeta"))

    assert common.run_copy_safe(render) == 0
    first = capsys.readouterr().out
    assert raw_notebook not in first
    assert first.splitlines() == ["notebook#1", "notebook#1", "notebook#2"]

    assert common.run_copy_safe(
        lambda: print(common.pseudonym("notebook", raw_notebook))
    ) == 0
    assert capsys.readouterr().out.splitlines() == ["notebook#1"]
