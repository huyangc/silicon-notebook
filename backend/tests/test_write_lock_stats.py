from app.repositories.sqlite.write_lock_stats import WriteLockStats


def test_waiters_counts_up_and_down():
    s = WriteLockStats()
    assert s.waiters == 0
    s.enter_wait()
    s.enter_wait()
    assert s.waiters == 2
    s.exit_wait()
    assert s.waiters == 1
    s.exit_wait()
    assert s.waiters == 0


def test_snapshot_aggregates_per_site():
    s = WriteLockStats()
    s.record("a.py:1", wait_ms=1.0, hold_ms=10.0)
    s.record("a.py:1", wait_ms=3.0, hold_ms=30.0)
    s.record("b.py:2", wait_ms=5.0, hold_ms=50.0)
    snap = s.snapshot()
    assert snap["sites"]["a.py:1"]["count"] == 2
    assert snap["sites"]["a.py:1"]["hold_max_ms"] == 30.0
    assert snap["sites"]["b.py:2"]["count"] == 1
    assert snap["sites"]["b.py:2"]["wait_max_ms"] == 5.0


def test_p99_reports_bucket_upper_bound():
    """50/50 分布下 p99 必须落在大值那一侧的桶上界(1000ms 桶)。

    刻意不用「99 小 + 1 大」:那种分布的 p99 本来就是小值,断言 p99>=900 是错的。
    """
    s = WriteLockStats()
    for _ in range(50):
        s.record("a.py:1", wait_ms=0.5, hold_ms=0.5)
    for _ in range(50):
        s.record("a.py:1", wait_ms=900.0, hold_ms=900.0)
    p99 = s.snapshot()["sites"]["a.py:1"]["hold_p99_ms"]
    assert p99 == 1000.0


def test_p99_of_an_all_fast_site_stays_small():
    s = WriteLockStats()
    for _ in range(100):
        s.record("a.py:1", wait_ms=0.5, hold_ms=0.5)
    assert s.snapshot()["sites"]["a.py:1"]["hold_p99_ms"] == 1.0


def test_memory_is_bounded_by_bucket_count():
    """1e4 次 record 之后,每个 site 的内存占用不随样本数增长。"""
    s = WriteLockStats()
    from app.repositories.sqlite.write_lock_stats import BUCKETS_MS
    for i in range(10_000):
        s.record("a.py:1", wait_ms=float(i % 7), hold_ms=float(i % 13))
    site = s._sites["a.py:1"]
    assert len(site.wait_buckets) == len(BUCKETS_MS)
    assert len(site.hold_buckets) == len(BUCKETS_MS)


def test_violation_goes_to_sink_immediately():
    seen = []
    s = WriteLockStats(warn_ms=100.0, sink=seen.append)
    s.record("a.py:1", wait_ms=1.0, hold_ms=5.0)
    assert seen == []
    s.record("a.py:1", wait_ms=1.0, hold_ms=250.0)
    assert len(seen) == 1
    assert seen[0]["kind"] == "db_write_lock_slow"
    assert seen[0]["site"] == "a.py:1"
    assert seen[0]["hold_ms"] == 250.0


def test_violation_sink_is_rate_limited_per_site():
    """同一 site 的连续违规不得逐次刷屏:每个刷新窗口内每 site 最多一条。"""
    seen = []
    s = WriteLockStats(warn_ms=100.0, flush_interval_s=1e6, sink=seen.append)
    for _ in range(50):
        s.record("a.py:1", wait_ms=1.0, hold_ms=250.0)
    assert len(seen) == 1


def test_reset_clears_sites():
    s = WriteLockStats()
    s.record("a.py:1", wait_ms=1.0, hold_ms=1.0)
    s.reset()
    assert s.snapshot()["sites"] == {}


def test_throwing_sink_does_not_propagate_and_stats_still_update():
    """sink 只负责观测上报,不能反过来打断被观测的写入:即使它抛异常,
    record() 也不能把异常传出去,统计仍要照常更新。

    warn_ms=flush_interval_s=0.0 让 violation 和 flush 两条 sink 调用路径
    在同一次 record 里都触发,确保两处 try/except 都生效,而不是只测到
    其中一条。
    """
    def bad_sink(payload):
        raise RuntimeError("boom")

    s = WriteLockStats(warn_ms=0.0, flush_interval_s=0.0, sink=bad_sink)
    s.record("a.py:1", wait_ms=1.0, hold_ms=1.0)  # 不得向外抛异常
    snap = s.snapshot()
    assert snap["sites"]["a.py:1"]["count"] == 1
    assert snap["sites"]["a.py:1"]["wait_max_ms"] == 1.0
    assert snap["sites"]["a.py:1"]["hold_max_ms"] == 1.0


def test_unresolved_sites_counts_up_and_resets():
    """Fix 4 (task 8 evidence-gap fix): a degraded frame walk in
    `database._caller_site()` (fallback `"?"`) must be counted, not silently
    absorbed as just another site — otherwise a run with broken attribution
    looks identical to a healthy one."""
    s = WriteLockStats()
    assert s.unresolved_sites == 0
    assert s.snapshot()["unresolved_sites"] == 0
    s.mark_unresolved_site()
    s.mark_unresolved_site()
    assert s.unresolved_sites == 2
    assert s.snapshot()["unresolved_sites"] == 2
    s.reset()
    assert s.unresolved_sites == 0
    assert s.snapshot()["unresolved_sites"] == 0


def test_percentile_of_a_huge_sample_stays_finite():
    """超过最大桶上界(10000ms)的样本落入 inf 哨兵桶,但对外上报的百分位
    必须封顶为最大有限桶(10000.0,语义"≥10s"),否则 json.dumps 会把它
    吐成非法的裸 token `Infinity`,写进 events.jsonl 后外部读者会解析失败。
    """
    import math

    s = WriteLockStats()
    s.record("a.py:1", wait_ms=50_000.0, hold_ms=50_000.0)
    snap = s.snapshot()
    assert math.isfinite(snap["sites"]["a.py:1"]["wait_p99_ms"])
    assert snap["sites"]["a.py:1"]["wait_p99_ms"] == 10000.0
    assert math.isfinite(snap["sites"]["a.py:1"]["hold_p99_ms"])
    assert snap["sites"]["a.py:1"]["hold_p99_ms"] == 10000.0
