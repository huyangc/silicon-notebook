"""MRL 截断质量 spike:回答「把 4096 维向量本地截断到 1024/2048 + re-normalize,
检索质量掉多少」——pgvector 迁移 P2 的前置 gate,也是 SQLite 栈 R1 续命
(内存 ÷4)的前置。判据与阈值出处:
docs/superpowers/specs/2026-07-03-pgvector-migration-review.md。

两种模式(都只读 DB,绝不写):

  overlap 模式(默认,零 API 调用):从表内采样 N 条向量当查询,对比
  「原维 top-K 排名」vs「截断维 top-K 排名」的重合率(邻居保持率)。
  任意 notebook 可跑,无需 gold、无需 embed 端点。

  gold 模式(--gold,需 embed 端点):对 recall_gold.yaml 的问题各 embed 一次
  (原生维),按暴力余弦对比各维度档的 recall@12 / MRR 相对衰减。
  绝对值偏保守(不做 canonical 折叠);判据只看相对差,不受影响。

用法(部署机,从 backend/ 目录跑,读仓库根 .env):
  python -m app.eval.mrl_truncation                        # 自动挑最大库,knowledge+chunk
  python -m app.eval.mrl_truncation --notebook nb-xxx --tables knowledge,chunk,relation
  python -m app.eval.mrl_truncation --gold app/eval/recall_gold.yaml --notebook nb-b37185f4ae

流式分块(默认 20k 行/块),峰值内存 = 单块矩阵,大库安全。
"""
from __future__ import annotations

import argparse
import sqlite3
from typing import Dict, List, Optional, Sequence

import numpy as np

from app.eval.retrieval_metrics import mrr, recall_at_k
from app.services.vector_index import decode_vector

TABLES = {
    "knowledge": ("knowledge_embeddings", "object_id"),
    "chunk": ("chunk_embeddings", "chunk_id"),
    "relation": ("relation_embeddings", "relation_id"),
    "element": ("element_embeddings", "element_id"),
}
DEFAULT_DIMS = (2048, 1024, 512)


# ── 纯函数 ───────────────────────────────────────────────────────────────────

def truncated_sims_many(block: np.ndarray, Q: np.ndarray, dim: int) -> np.ndarray:
    """block(n×D) 各行与 Q(m×D) 各查询的「前 dim 维截断 + re-normalize」余弦,
    返回 (n, m)。一次 GEMM 而非逐查询 matvec —— 大库(几十万行 × 数百查询)
    的可行性全靠这个批量化。零前缀安全(该行/该查询整列为 0)。"""
    bp = block[:, :dim]
    qp = Q[:, :dim]
    bn = np.linalg.norm(bp, axis=1)                     # (n,)
    qn = np.linalg.norm(qp, axis=1)                     # (m,)
    dots = bp @ qp.T                                    # (n, m) — 单次 GEMM
    denom = bn[:, None] * qn[None, :]
    with np.errstate(divide="ignore", invalid="ignore"):
        sims = np.where(denom > 0, dots / denom, 0.0)
    return sims.astype(np.float32)


def truncated_sims(block: np.ndarray, q: np.ndarray, dim: int) -> np.ndarray:
    """单查询薄封装(测试/小用例);批量路径用 truncated_sims_many。"""
    return truncated_sims_many(block, q[None, :], dim)[:, 0]


def topk_overlap(a_ids: Sequence[str], b_ids: Sequence[str], k: int) -> float:
    """两个排名前 k 的集合重合率(|∩|/k)。"""
    if k <= 0:
        return 0.0
    return len(set(list(a_ids)[:k]) & set(list(b_ids)[:k])) / k


class _TopK:
    """流式 top-K 累积器:分块 update,末尾 ids() 给出按分降序的前 K id。"""

    def __init__(self, k: int):
        self.k = k
        self._scores = np.empty(0, dtype=np.float32)
        self._ids: List[str] = []

    def update(self, ids: Sequence[str], scores: np.ndarray) -> None:
        if len(ids) == 0:
            return
        merged = np.concatenate([self._scores, np.asarray(scores, dtype=np.float32)])
        merged_ids = self._ids + list(ids)
        if len(merged) > self.k:
            keep = np.argpartition(-merged, self.k - 1)[: self.k]
        else:
            keep = np.arange(len(merged))
        self._scores = merged[keep]
        self._ids = [merged_ids[i] for i in keep]

    def ids(self) -> List[str]:
        order = np.argsort(-self._scores, kind="stable")
        return [self._ids[i] for i in order]


def verdict_for(dim: int, recall_drop_pt: float, overlap10: float, regressed: int) -> str:
    """按决策文档判据给单维度档结论(gold 口径;overlap-only 时 recall_drop 传 0 由
    调用方注明)。2048:降 ≤1pt 且 top-10 重合 ≥0.9;1024:降 ≤3pt;>5pt 一票不通过。"""
    if recall_drop_pt > 5.0:
        return f"{dim} 维:不通过(recall@12 相对降 {recall_drop_pt:.1f}pt > 5pt)"
    if dim >= 2048 and recall_drop_pt <= 1.0 and overlap10 >= 0.9:
        return f"{dim} 维:通过 → halfvec {dim}(降 {recall_drop_pt:.1f}pt,top-10 重合 {overlap10:.2f},回归 {regressed} 题)"
    if dim < 2048 and recall_drop_pt <= 3.0:
        return f"{dim} 维:通过 → vector {dim}(降 {recall_drop_pt:.1f}pt,top-10 重合 {overlap10:.2f},回归 {regressed} 题)"
    return f"{dim} 维:边界(降 {recall_drop_pt:.1f}pt,top-10 重合 {overlap10:.2f},回归 {regressed} 题)——升档或人工复核"


# ── DB 侧 ────────────────────────────────────────────────────────────────────

def _ro(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _iter_vec_blocks(conn, table: str, id_col: str, notebook_id: str,
                     stored_dim: int, block_rows: int):
    """流式产出 (ids, matrix[float32, 未归一]) 块;坏行/维度不符行计数返回。
    生成器最后一次 yield 后,skipped 数由调用方经列表捕获(这里用可变 dict)。"""
    cur = conn.execute(
        f"SELECT {id_col} AS vid, vector FROM {table} WHERE notebook_id = ?",
        (notebook_id,))
    skipped = 0
    while True:
        rows = cur.fetchmany(block_rows)
        if not rows:
            break
        ids, vecs = [], []
        for r in rows:
            try:
                v = decode_vector(r["vector"])
            except Exception:  # noqa: BLE001 — 坏行跳过计数,不拖垮评测
                skipped += 1
                continue
            if v is None or v.shape[0] != stored_dim:
                skipped += 1
                continue
            ids.append(r["vid"])
            vecs.append(v)
        if ids:
            yield ids, np.vstack(vecs), skipped
            skipped = 0
    if skipped:
        yield [], np.empty((0, stored_dim), dtype=np.float32), skipped


def _detect_dim(conn, table: str, id_col: str, notebook_id: str) -> Optional[int]:
    """取首个可解码向量的维度作为存储维(全表应同维;不同维行按 skip 计)。"""
    cur = conn.execute(
        f"SELECT vector FROM {table} WHERE notebook_id = ? LIMIT 20", (notebook_id,))
    for r in cur.fetchall():
        try:
            v = decode_vector(r["vector"])
        except Exception:  # noqa: BLE001
            continue
        if v is not None and v.shape[0] > 0:
            return int(v.shape[0])
    return None


def embedding_counts(db_path: str, notebook_id: str) -> Dict[str, int]:
    """四张 embeddings 表在该 notebook 的行数(顺带回答部署容量三问之一)。"""
    out = {}
    with _ro(db_path) as conn:
        for key, (table, _)  in TABLES.items():
            try:
                out[key] = conn.execute(
                    f"SELECT COUNT(*) AS c FROM {table} WHERE notebook_id=?",
                    (notebook_id,)).fetchone()["c"]
            except sqlite3.Error:
                out[key] = -1   # 表不存在(旧库)
    return out


def biggest_notebook(db_path: str) -> Optional[str]:
    with _ro(db_path) as conn:
        try:
            row = conn.execute(
                "SELECT notebook_id, COUNT(*) AS c FROM knowledge_embeddings "
                "GROUP BY notebook_id ORDER BY c DESC LIMIT 1").fetchone()
        except sqlite3.Error:
            return None
    return row["notebook_id"] if row else None


# ── overlap 模式 ─────────────────────────────────────────────────────────────

def run_overlap_eval(db_path: str, notebook_id: str, table_key: str,
                     dims: Sequence[int], n_queries: int = 200, k: int = 50,
                     block_rows: int = 20_000, seed: int = 42) -> dict:
    """邻居保持率评测:采样 n_queries 条向量当查询,流式对比原维 vs 各截断维的
    top-K 排名重合率。返回 {rows, used, skipped, stored_dim, dims:{dim:metrics}}。"""
    table, id_col = TABLES[table_key]
    with _ro(db_path) as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM {table} WHERE notebook_id=?",
            (notebook_id,)).fetchone()["c"]
        if total == 0:
            return {"rows": 0, "used": 0, "skipped": 0, "stored_dim": None, "dims": {}}
        stored_dim = _detect_dim(conn, table, id_col, notebook_id)
        if stored_dim is None:
            return {"rows": total, "used": 0, "skipped": total, "stored_dim": None, "dims": {}}
        dims = [d for d in dims if d < stored_dim]

        # 采样查询向量:先取全部 id(轻),种子随机挑,再按 id 取向量
        all_ids = [r["vid"] for r in conn.execute(
            f"SELECT {id_col} AS vid FROM {table} WHERE notebook_id=?", (notebook_id,))]
        rng = np.random.default_rng(seed)
        q_ids = list(rng.choice(all_ids, size=min(n_queries, len(all_ids)), replace=False))
        q_vecs = []
        kept_q_ids = []
        for lo in range(0, len(q_ids), 500):
            batch = q_ids[lo:lo + 500]
            ph = ",".join("?" for _ in batch)
            for r in conn.execute(
                    f"SELECT {id_col} AS vid, vector FROM {table} WHERE {id_col} IN ({ph})",
                    batch):
                try:
                    v = decode_vector(r["vector"])
                except Exception:  # noqa: BLE001
                    continue
                if v is not None and v.shape[0] == stored_dim:
                    kept_q_ids.append(r["vid"])
                    q_vecs.append(v)
        if not q_vecs:
            return {"rows": total, "used": 0, "skipped": total, "stored_dim": stored_dim, "dims": {}}
        Q = np.vstack(q_vecs)

        # 流式累积:每个 (维度档∪基线) × 每个查询一个 TopK
        eval_dims = [stored_dim] + list(dims)
        acc = {d: [_TopK(k) for _ in kept_q_ids] for d in eval_dims}
        used = skipped = 0
        q_pos = {qid: qi for qi, qid in enumerate(kept_q_ids)}
        for ids, block, skip in _iter_vec_blocks(conn, table, id_col, notebook_id,
                                                 stored_dim, block_rows):
            skipped += skip
            if not ids:
                continue
            used += len(ids)
            # 本块里出现的查询自身位置(排除自检索)
            self_hits = [(row, q_pos[vid]) for row, vid in enumerate(ids) if vid in q_pos]
            for d in eval_dims:
                sims = truncated_sims_many(block, Q, d)      # (n, m) 单次 GEMM
                for row, qi in self_hits:
                    sims[row, qi] = -np.inf
                for qi in range(len(kept_q_ids)):
                    acc[d][qi].update(ids, sims[:, qi])

    metrics: Dict[int, dict] = {}
    base_rank = [acc[stored_dim][qi].ids() for qi in range(len(kept_q_ids))]
    for d in dims:
        o10 = [topk_overlap(base_rank[qi], acc[d][qi].ids(), min(10, k))
               for qi in range(len(kept_q_ids))]
        oK = [topk_overlap(base_rank[qi], acc[d][qi].ids(), k)
              for qi in range(len(kept_q_ids))]
        metrics[d] = {
            "mean_overlap@10": float(np.mean(o10)),
            "min_overlap@10": float(np.min(o10)),
            f"mean_overlap@{k}": float(np.mean(oK)),
            "n_queries": len(kept_q_ids),
        }
    return {"rows": total, "used": used, "skipped": skipped,
            "stored_dim": stored_dim, "dims": metrics}


# ── gold 模式 ────────────────────────────────────────────────────────────────

def run_gold_eval(db_path: str, notebook_id: str, questions: List[dict],
                  dims: Sequence[int], k: int = 12, embedder=None,
                  block_rows: int = 20_000) -> dict:
    """gold 集暴力余弦评测:对 gold_object_ids 的题算各维度档 recall@k / MRR。
    embedder 缺省用 make_embedder(get_settings())(需 embed 端点);测试注入 Fake。
    绝对值不做 canonical 折叠(偏保守);判据只看相对差。"""
    qs = [q for q in questions if q.get("gold_object_ids")]
    if not qs:
        return {"n_questions": 0, "dims": {}}
    if embedder is None:
        from app.core.config import get_settings
        from app.services.embedding import make_embedder
        embedder = make_embedder(get_settings())
    q_vecs = np.asarray([embedder.embed_query(q["question"]) for q in qs],
                        dtype=np.float32)

    table, id_col = TABLES["knowledge"]
    with _ro(db_path) as conn:
        stored_dim = _detect_dim(conn, table, id_col, notebook_id)
        if stored_dim is None:
            return {"n_questions": len(qs), "dims": {}}
        if q_vecs.shape[1] != stored_dim:
            raise SystemExit(
                f"embed 维度 {q_vecs.shape[1]} != 库内存储维 {stored_dim} —— "
                f"gold 模式要求 embed 端点按原生维出向量(EMBED_DIM 配置核对)")
        dims = [d for d in dims if d < stored_dim]
        eval_dims = [stored_dim] + list(dims)
        acc = {d: [_TopK(max(k, 50)) for _ in qs] for d in eval_dims}
        for ids, block, _skip in _iter_vec_blocks(conn, table, id_col, notebook_id,
                                                  stored_dim, block_rows):
            if not ids:
                continue
            for d in eval_dims:
                sims = truncated_sims_many(block, q_vecs, d)
                for qi in range(len(qs)):
                    acc[d][qi].update(ids, sims[:, qi])

    out: Dict[int, dict] = {}
    per_q_base: List[float] = []
    for d in eval_dims:
        recalls, mrrs = [], []
        for qi, q in enumerate(qs):
            ranked = acc[d][qi].ids()
            recalls.append(recall_at_k(ranked, q["gold_object_ids"], k) or 0.0)
            mrrs.append(mrr(ranked, q["gold_object_ids"]) or 0.0)
        row = {"mean_recall@12": float(np.mean(recalls)),
               "mean_mrr": float(np.mean(mrrs))}
        if d == stored_dim:
            per_q_base = recalls
        else:
            row["regressed"] = int(sum(1 for qi in range(len(qs))
                                       if recalls[qi] < per_q_base[qi]))
            row["recall_drop_pt"] = float(
                (np.mean(per_q_base) - np.mean(recalls)) * 100)
        out[d] = row
    return {"n_questions": len(qs), "stored_dim": stored_dim, "dims": out}


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="", help="SQLite 路径(缺省用 Settings.sqlite_path)")
    ap.add_argument("--notebook", default="", help="notebook id(缺省=knowledge 向量最多的库)")
    ap.add_argument("--tables", default="knowledge,chunk",
                    help="overlap 模式评测哪些表(逗号分隔;行数统计恒覆盖全部四张)")
    ap.add_argument("--dims", default=",".join(str(d) for d in DEFAULT_DIMS))
    ap.add_argument("--queries", type=int, default=200, help="overlap 模式采样查询数")
    ap.add_argument("--k", type=int, default=50, help="overlap 模式 top-K")
    ap.add_argument("--gold", default="", help="recall_gold.yaml 路径 → 追加 gold 模式(需 embed 端点)")
    ap.add_argument("--block-rows", type=int, default=20_000)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args(argv)

    db = args.db
    if not db:
        from app.core.config import get_settings
        db = get_settings().sqlite_path
    nb = args.notebook or biggest_notebook(db)
    if not nb:
        print(f"(在 {db} 里找不到任何 knowledge_embeddings——用 --db/--notebook 显式指定)")
        return 1
    dims = [int(d) for d in args.dims.split(",") if d.strip()]

    print(f"MRL 截断质量 spike  db={db}\nnotebook={nb}  dims={dims}")
    counts = embedding_counts(db, nb)
    print("\n[embeddings 行数](-1=表不存在)")
    for key, c in counts.items():
        print(f"  {TABLES[key][0]:<22} {c}")

    for key in [t.strip() for t in args.tables.split(",") if t.strip()]:
        if key not in TABLES:
            print(f"  (未知表 {key},跳过)")
            continue
        print(f"\n[overlap 模式:{key}](采样 {args.queries} 查询,top-{args.k})")
        out = run_overlap_eval(db, nb, key, dims, n_queries=args.queries,
                               k=args.k, block_rows=args.block_rows, seed=args.seed)
        if not out["dims"]:
            print(f"  行 {out['rows']},可用 {out.get('used', 0)} —— 跳过(空表/无可解码向量/维度都不小于存储维)")
            continue
        print(f"  行 {out['rows']},进评测 {out['used']},跳过 {out['skipped']},存储维 {out['stored_dim']}")
        for d, m in sorted(out["dims"].items(), reverse=True):
            print(f"  截断 {d:>5}: overlap@10 mean={m['mean_overlap@10']:.3f} "
                  f"min={m['min_overlap@10']:.3f}  overlap@{args.k} mean={m[f'mean_overlap@{args.k}']:.3f}")

    if args.gold:
        import yaml
        with open(args.gold, encoding="utf-8") as fh:
            questions = yaml.safe_load(fh)
        print(f"\n[gold 模式](题目文件 {args.gold})")
        out = run_gold_eval(db, nb, questions, dims)
        if not out["dims"]:
            print("  (无带 gold_object_ids 的题或库内无向量)")
        else:
            base = out["dims"][out["stored_dim"]]
            print(f"  题数 {out['n_questions']},基线 {out['stored_dim']} 维: "
                  f"recall@12={base['mean_recall@12']:.3f} MRR={base['mean_mrr']:.3f} (无 canonical 折叠,绝对值偏保守)")
            for d in sorted([d for d in out["dims"] if d != out["stored_dim"]], reverse=True):
                m = out["dims"][d]
                print(f"  截断 {d:>5}: recall@12={m['mean_recall@12']:.3f} "
                      f"(Δ{m['recall_drop_pt']:+.1f}pt) MRR={m['mean_mrr']:.3f} 回归 {m['regressed']} 题")
                print(f"    → {verdict_for(d, m['recall_drop_pt'], 1.0, m['regressed'])}"
                      f"(top-10 重合请以 overlap 模式为准)")

    print("\n=== 完 — 把以上整段输出贴回即可 " + "=" * 40)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
