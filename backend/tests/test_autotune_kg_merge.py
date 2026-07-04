"""_run_shard 用传入的 ann_threads 调 hnswlib.set_num_threads,而非硬编码 1。"""
import numpy as np
import pytest
from app.services import kg_merge


def _reps(names):
    # 造两组明显分离的向量,保证有 ≥1 条近邻候选边(sim≥lo)。
    rng = np.arange(len(names), dtype=np.float32)
    return {n: np.array([1.0, 0.001 * i], dtype=np.float32) for i, n in enumerate(names)}


def test_ann_candidates_passes_thread_count(monkeypatch):
    seen = {}
    real_index = kg_merge.__dict__.get("hnswlib")  # not imported at module top; patch the module

    import hnswlib

    class _SpyIndex(hnswlib.Index):
        def set_num_threads(self, n):
            seen["threads"] = n
            return super().set_num_threads(n)

    monkeypatch.setattr(hnswlib, "Index", _SpyIndex)
    names = [f"c{i}" for i in range(6)]
    kg_merge._ann_candidates(names, _reps(names), k=3, lo=0.0, ann_threads=7)
    assert seen["threads"] == 7


def test_ann_candidates_defaults_to_single_thread(monkeypatch):
    seen = {}
    import hnswlib

    class _SpyIndex(hnswlib.Index):
        def set_num_threads(self, n):
            seen["threads"] = n
            return super().set_num_threads(n)

    monkeypatch.setattr(hnswlib, "Index", _SpyIndex)
    names = [f"c{i}" for i in range(6)]
    kg_merge._ann_candidates(names, _reps(names), k=3, lo=0.0)
    assert seen["threads"] == 1  # 默认零行为变化


def test_cluster_seeds_forwards_ann_threads(monkeypatch):
    captured = {}
    real = kg_merge._ann_candidates

    def spy(*a, **k):
        captured["ann_threads"] = k.get("ann_threads")
        return real(*a, **k)

    monkeypatch.setattr(kg_merge, "_ann_candidates", spy)
    names = ["a", "b", "c"]
    kg_merge.cluster_seeds(
        names, _reps(names), {n: 1 for n in names},
        {n: n for n in names}, set(), set(), ann_threads=5,
    )
    assert captured["ann_threads"] == 5
