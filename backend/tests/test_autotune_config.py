"""按核数解析 kg_cluster_ann_threads:未设→min(cpu_count,32);显式→原样。"""
import os
import pytest
from app.core.config import Settings


def test_ann_threads_auto_from_cpu_count_capped_at_32(monkeypatch):
    monkeypatch.delenv("KG_CLUSTER_ANN_THREADS", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 64)
    assert Settings().kg_cluster_ann_threads == 32  # min(64, 32)


def test_ann_threads_auto_tracks_small_machines(monkeypatch):
    monkeypatch.delenv("KG_CLUSTER_ANN_THREADS", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 16)
    assert Settings().kg_cluster_ann_threads == 16  # min(16, 32)


def test_ann_threads_explicit_env_wins(monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 64)
    monkeypatch.setenv("KG_CLUSTER_ANN_THREADS", "4")
    assert Settings().kg_cluster_ann_threads == 4


def test_ann_threads_zero_means_auto(monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    monkeypatch.setenv("KG_CLUSTER_ANN_THREADS", "0")
    assert Settings().kg_cluster_ann_threads == 8  # 0 sentinel → min(8,32)
