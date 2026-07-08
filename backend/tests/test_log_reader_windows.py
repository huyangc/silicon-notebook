import json
from pathlib import Path
from app.services import log_reader as lr


def test_valid_date_param():
    assert lr.valid_date_param("2026-07-08")
    assert lr.valid_date_param("legacy")
    assert not lr.valid_date_param("2026-7-8")
    assert not lr.valid_date_param("../etc")
    assert not lr.valid_date_param("")
    # $ 换行绕过必须被拒（CVE 类缺陷修复）
    assert not lr.valid_date_param("2026-07-08\n")
    assert not lr.valid_date_param("2026-07-08\nx")


def test_available_days_sorted_desc_with_legacy(tmp_path):
    (tmp_path / "llm-2026-07-01.jsonl").write_text("{}\n")
    (tmp_path / "llm-2026-07-03.jsonl.gz").write_bytes(b"x")
    (tmp_path / "llm.jsonl").write_text("{}\n")           # legacy
    (tmp_path / "events-2026-07-02.jsonl").write_text("{}\n")  # 别的 channel 不混入
    assert lr.available_days(tmp_path, "llm") == ["2026-07-03", "2026-07-01", "legacy"]


def test_resolve_day_path_prefers_plain_then_gz_then_legacy(tmp_path):
    (tmp_path / "llm-2026-07-01.jsonl").write_text("{}\n")
    (tmp_path / "llm-2026-07-02.jsonl.gz").write_bytes(b"x")
    (tmp_path / "llm.jsonl").write_text("{}\n")
    assert lr.resolve_day_path(tmp_path, "llm", "2026-07-01") == (tmp_path / "llm-2026-07-01.jsonl", False)
    assert lr.resolve_day_path(tmp_path, "llm", "2026-07-02") == (tmp_path / "llm-2026-07-02.jsonl.gz", True)
    assert lr.resolve_day_path(tmp_path, "llm", "legacy") == (tmp_path / "llm.jsonl", False)
    # 不存在的天 → 按明文空处理（path 不存在、非 gz）
    p, gz = lr.resolve_day_path(tmp_path, "llm", "2026-07-09")
    assert not p.exists() and gz is False


def _write_lines(p, objs):
    p.write_text("".join(json.dumps(o) + "\n" for o in objs), encoding="utf-8")


def test_plain_window_seq_is_byte_offset_and_monotonic(tmp_path):
    p = tmp_path / "llm-2026-07-08.jsonl"
    _write_lines(p, [{"i": 0}, {"i": 1}, {"i": 2}])
    recs, malformed, trunc = lr._load_plain_window(
        p, since=None, before=None, max_records=100, max_bytes=1 << 20)
    assert [r["i"] for r in recs] == [0, 1, 2]
    seqs = [r["seq"] for r in recs]
    assert seqs == sorted(seqs) and seqs[0] == 0     # 首行偏移 0
    assert malformed == 0 and trunc is False


def test_plain_window_tail_truncates_by_bytes(tmp_path):
    p = tmp_path / "llm-2026-07-08.jsonl"
    _write_lines(p, [{"i": i, "pad": "x" * 50} for i in range(200)])
    recs, _, trunc = lr._load_plain_window(
        p, since=None, before=None, max_records=100000, max_bytes=300)  # 极小字节预算
    assert trunc is True                              # 丢了更旧的
    assert recs[-1]["i"] == 199                        # 保到最新
    assert recs[0]["seq"] > 0                          # 尾读起点不在 0


def test_plain_window_since_returns_only_newer(tmp_path):
    p = tmp_path / "llm-2026-07-08.jsonl"
    _write_lines(p, [{"i": 0}, {"i": 1}, {"i": 2}])
    all_recs, _, _ = lr._load_plain_window(p, since=None, before=None, max_records=100, max_bytes=1 << 20)
    cut = all_recs[1]["seq"]                            # 第二行的偏移
    newer, _, _ = lr._load_plain_window(p, since=cut, before=None, max_records=100, max_bytes=1 << 20)
    assert [r["i"] for r in newer] == [2]              # 只回严格更新的


def test_plain_window_before_returns_older(tmp_path):
    p = tmp_path / "llm-2026-07-08.jsonl"
    _write_lines(p, [{"i": 0}, {"i": 1}, {"i": 2}])
    all_recs, _, _ = lr._load_plain_window(p, since=None, before=None, max_records=100, max_bytes=1 << 20)
    cut = all_recs[2]["seq"]
    older, _, _ = lr._load_plain_window(p, since=None, before=cut, max_records=100, max_bytes=1 << 20)
    assert [r["i"] for r in older] == [0, 1]
