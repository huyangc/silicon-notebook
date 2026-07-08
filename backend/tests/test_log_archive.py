import gzip, json
from pathlib import Path
from app.core import event_logging as el


def _write(p: Path, lines):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")


def test_gzip_day_file_atomic_idempotent_removes_plain(tmp_path):
    plain = tmp_path / "llm-2026-07-01.jsonl"
    _write(plain, [{"a": 1}, {"a": 2}])
    el._gzip_day_file(plain)
    gz = tmp_path / "llm-2026-07-01.jsonl.gz"
    assert gz.exists() and not plain.exists()
    with gzip.open(gz, "rt", encoding="utf-8") as fh:
        assert [json.loads(l) for l in fh] == [{"a": 1}, {"a": 2}]
    # 幂等:再调不报错、不改动
    el._gzip_day_file(plain)  # plain 已不在 → no-op
    assert gz.exists()


def test_gzip_missing_or_already_gz_is_noop(tmp_path):
    el._gzip_day_file(tmp_path / "nope-2026-01-01.jsonl")  # 不存在 → 静默
    plain = tmp_path / "llm-2026-01-02.jsonl"
    _write(plain, [{"x": 1}])
    (tmp_path / "llm-2026-01-02.jsonl.gz").write_bytes(b"stub")
    el._gzip_day_file(plain)  # gz 已存在 → 不覆盖、不删明文
    assert plain.exists()


def test_archive_stale_days_skips_today_and_legacy(tmp_path, monkeypatch):
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    # 旧天(应压)、今天(不压)、legacy 无日期(不碰)、per-user 子目录旧天(应压)
    _write(tmp_path / "llm-2026-01-01.jsonl", [{"a": 1}])
    _write(tmp_path / f"llm-{today}.jsonl", [{"a": 2}])
    _write(tmp_path / "llm.jsonl", [{"legacy": 1}])
    _write(tmp_path / "user-abc" / "events-2026-01-01.jsonl", [{"e": 1}])

    class S:  # 最小 settings 替身
        event_log_dir = str(tmp_path)
    el.archive_stale_days(S())
    el._archive_pool.shutdown(wait=True)  # 等后台压缩完成后断言
    # 重新起池供后续测试（shutdown 后不可再 submit）
    import concurrent.futures as _f
    el._archive_pool = _f.ThreadPoolExecutor(max_workers=1, thread_name_prefix="log-archive")

    assert (tmp_path / "llm-2026-01-01.jsonl.gz").exists()
    assert (tmp_path / "user-abc" / "events-2026-01-01.jsonl.gz").exists()
    assert (tmp_path / f"llm-{today}.jsonl").exists()          # 今天不压
    assert not (tmp_path / f"llm-{today}.jsonl.gz").exists()
    assert (tmp_path / "llm.jsonl").exists()                    # legacy 不碰
