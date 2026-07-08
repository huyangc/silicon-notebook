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
