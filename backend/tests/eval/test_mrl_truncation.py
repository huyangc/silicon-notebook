"""MRL 截断质量 spike(app/eval/mrl_truncation.py)单测。

覆盖:前缀截断余弦的数学正确性(re-normalize、零前缀安全)、top-K 重合率、
流式分块与全量一致、e2e overlap 评测(无损前缀 → 重合率 1.0;坏行/维度不符
跳过并计数)、gold 评测(注入 FakeEmbedder,零 API)、判据映射。
"""
import json
import sqlite3

import numpy as np
import pytest

from app.eval.mrl_truncation import (
    _TopK,
    run_gold_eval,
    run_overlap_eval,
    topk_overlap,
    truncated_sims,
    verdict_for,
)
from app.services.embedding import FakeEmbedder
from app.services.vector_index import encode_vector


# ── 纯函数 ───────────────────────────────────────────────────────────────────

def test_truncated_sims_prefix_identical():
    q = np.array([1.0, 0.0, 5.0, 5.0], dtype=np.float32)
    block = np.array([[2.0, 0.0, -3.0, 9.0]], dtype=np.float32)
    # 前缀(dim=2)方向相同 → 截断后 re-normalize 余弦 = 1
    sims = truncated_sims(block, q, 2)
    assert sims.shape == (1,)
    assert sims[0] == pytest.approx(1.0, abs=1e-6)


def test_truncated_sims_zero_prefix_safe():
    q = np.array([0.0, 0.0, 1.0], dtype=np.float32)
    block = np.array([[1.0, 1.0, 1.0]], dtype=np.float32)
    sims = truncated_sims(block, q, 2)     # query 前缀全零 → 0,不产 NaN
    assert np.isfinite(sims).all()
    assert sims[0] == 0.0


def test_truncated_sims_full_dim_equals_cosine():
    rng = np.random.default_rng(7)
    q = rng.normal(size=8).astype(np.float32)
    block = rng.normal(size=(5, 8)).astype(np.float32)
    sims = truncated_sims(block, q, 8)
    expect = (block / np.linalg.norm(block, axis=1, keepdims=True)) @ (q / np.linalg.norm(q))
    assert np.allclose(sims, expect, atol=1e-5)


def test_topk_overlap():
    assert topk_overlap(["a", "b", "c"], ["a", "b", "c"], 3) == 1.0
    assert topk_overlap(["a", "b", "c"], ["c", "d", "e"], 3) == pytest.approx(1 / 3)
    assert topk_overlap([], ["a"], 3) == 0.0


def test_topk_streaming_merge_matches_global():
    """分块喂 _TopK 的结果 == 一次性全量 argsort 的 top-K。"""
    rng = np.random.default_rng(3)
    ids = [f"v{i}" for i in range(100)]
    scores = rng.normal(size=100).astype(np.float32)
    acc = _TopK(k=10)
    for lo in range(0, 100, 17):                      # 不整除的块
        acc.update(ids[lo:lo + 17], scores[lo:lo + 17])
    got = acc.ids()
    expect = [ids[i] for i in np.argsort(-scores)[:10]]
    assert got == expect


# ── e2e:overlap 模式 ─────────────────────────────────────────────────────────

def _mk_db(tmp_path, rows, table="chunk_embeddings", id_col="chunk_id"):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute(f"CREATE TABLE {table} ({id_col} TEXT, notebook_id TEXT, vector BLOB)")
    conn.executemany(f"INSERT INTO {table} VALUES (?,?,?)", rows)
    conn.commit()
    conn.close()
    return str(db)


def test_overlap_eval_lossless_prefix_gives_perfect_overlap(tmp_path):
    """后半维度全零 → 截断到前半是无损的 → 各 K 档重合率恒 1.0。"""
    rng = np.random.default_rng(11)
    rows = []
    for i in range(60):
        v = np.zeros(32, dtype=np.float32)
        v[:16] = rng.normal(size=16)
        rows.append((f"c{i}", "nb-1", encode_vector(v)))
    db = _mk_db(tmp_path, rows)

    out = run_overlap_eval(db, "nb-1", "chunk", dims=[16, 8],
                           n_queries=10, k=10, block_rows=13, seed=42)
    assert out["rows"] == 60
    assert out["stored_dim"] == 32
    d16 = out["dims"][16]
    assert d16["mean_overlap@10"] == pytest.approx(1.0)
    assert d16["min_overlap@10"] == pytest.approx(1.0)
    d8 = out["dims"][8]                                # 有损档:合法值域即可
    assert 0.0 <= d8["mean_overlap@10"] <= 1.0


def test_overlap_eval_skips_bad_and_mismatched_rows(tmp_path):
    rng = np.random.default_rng(5)
    rows = [(f"c{i}", "nb-1", encode_vector(rng.normal(size=32).astype(np.float32)))
            for i in range(20)]
    rows.append(("bad-json", "nb-1", "not json"))
    rows.append(("wrong-dim", "nb-1", encode_vector(np.ones(8, dtype=np.float32))))
    rows.append(("other-nb", "nb-2", encode_vector(np.ones(32, dtype=np.float32))))
    db = _mk_db(tmp_path, rows)

    out = run_overlap_eval(db, "nb-1", "chunk", dims=[16],
                           n_queries=5, k=5, block_rows=7, seed=1)
    assert out["rows"] == 22            # nb-1 的行数(含坏行)
    assert out["used"] == 20            # 实际进评测的
    assert out["skipped"] == 2


def test_overlap_eval_empty_table(tmp_path):
    db = _mk_db(tmp_path, [])
    out = run_overlap_eval(db, "nb-1", "chunk", dims=[16],
                           n_queries=5, k=5, block_rows=7, seed=1)
    assert out["rows"] == 0 and out["dims"] == {}


def test_overlap_eval_sample_rows_bounds_corpus(tmp_path):
    """--sample-rows:大库只抽子集做语料;无损前缀性质在子集上同样成立,
    结果确定(同 seed 同子集)。"""
    rng = np.random.default_rng(21)
    rows = []
    for i in range(200):
        v = np.zeros(32, dtype=np.float32)
        v[:16] = rng.normal(size=16)
        rows.append((f"c{i}", "nb-1", encode_vector(v)))
    db = _mk_db(tmp_path, rows)

    out = run_overlap_eval(db, "nb-1", "chunk", dims=[16], n_queries=8,
                           k=5, block_rows=17, seed=7, sample_rows=50)
    assert out["rows"] == 200          # 全库行数照实报
    assert out["sampled"] == 50        # 语料只抽 50
    assert out["used"] == 50
    assert out["dims"][16]["mean_overlap@10"] == pytest.approx(1.0)

    out2 = run_overlap_eval(db, "nb-1", "chunk", dims=[16], n_queries=8,
                            k=5, block_rows=17, seed=7, sample_rows=50)
    assert out2["dims"] == out["dims"]  # 同 seed 可复现


def test_overlap_eval_sample_rows_ge_total_is_full(tmp_path):
    rng = np.random.default_rng(2)
    rows = [(f"c{i}", "nb-1", encode_vector(rng.normal(size=32).astype(np.float32)))
            for i in range(20)]
    db = _mk_db(tmp_path, rows)
    out = run_overlap_eval(db, "nb-1", "chunk", dims=[16], n_queries=5,
                           k=5, block_rows=7, seed=1, sample_rows=500)
    assert out["sampled"] == 20 and out["used"] == 20


# ── e2e:gold 模式(注入 FakeEmbedder,零 API)────────────────────────────────

def test_gold_eval_full_dim_perfect_when_vectors_match(tmp_path):
    emb = FakeEmbedder(dim=32)
    questions = [
        {"id": "q1", "question": "what is a bandgap reference",
         "gold_object_ids": ["ko-1"]},
        {"id": "q2", "question": "how does cascode work",
         "gold_object_ids": ["ko-2"]},
    ]
    rows = [("ko-1", "nb-1", encode_vector(emb.embed_query(questions[0]["question"]))),
            ("ko-2", "nb-1", encode_vector(emb.embed_query(questions[1]["question"])))]
    rng = np.random.default_rng(9)
    rows += [(f"ko-x{i}", "nb-1", encode_vector(rng.normal(size=32).astype(np.float32)))
             for i in range(30)]
    db = _mk_db(tmp_path, rows, table="knowledge_embeddings", id_col="object_id")

    out = run_gold_eval(db, "nb-1", questions, dims=[16], k=12, embedder=emb)
    assert out["n_questions"] == 2
    full = out["dims"][32]                 # 基线 = 存储维
    assert full["mean_recall@12"] == pytest.approx(1.0)
    d16 = out["dims"][16]
    assert "mean_recall@12" in d16 and "mean_mrr" in d16
    assert "regressed" in d16              # 相对基线 recall 变差的题数


# ── 判据映射(决策文档口径)──────────────────────────────────────────────────

def test_verdict_thresholds():
    # 2048 档:recall 降 ≤1pt 且 top-10 重合 ≥0.9 → halfvec 2048
    assert "2048" in verdict_for(2048, recall_drop_pt=0.5, overlap10=0.95, regressed=1)
    # 1024 档:降 ≤3pt → vector 1024
    assert "1024" in verdict_for(1024, recall_drop_pt=2.0, overlap10=0.91, regressed=1)
    # 降 >5pt → 不通过
    v = verdict_for(1024, recall_drop_pt=6.0, overlap10=0.7, regressed=5)
    assert "不通过" in v
