"""T6 验证去过度合并：①cluster_concepts 近线性计时 ②真实 nb-012 实证(垃圾簇消失)。
用法: PYTHONPATH=backend python scripts/validate_overmerge_fix.py
"""
import time
from collections import defaultdict

import numpy as np

from app.repositories.sqlite.maintenance import ReadOnlySQLiteInspector
from app.services.kg_merge import cluster_concepts, _discriminative_conflict, _norm

DB = "/Users/hzf/workspace/silicon_notebook/.local/silicon_notebook.db"
NB = "nb-012fb94249"
DIM = 1024


def synth(n):
    rng = np.random.default_rng(0)
    concepts = [{"object_id": f"o{i}", "name": f"concept number {i}"} for i in range(n)]
    vectors = {f"o{i}": rng.standard_normal(32).astype(float).tolist() for i in range(n)}
    return concepts, vectors


print("=== ① 近线性计时 (合成, 32维) ===")
prev = None
for n in (1000, 2000, 4000):
    c, v = synth(n)
    t = time.perf_counter()
    cluster_concepts(c, v, set(), set())
    el = time.perf_counter() - t
    print(f"  N={n:5d}: {el:.3f}s" + (f"   x{el/prev:.1f} vs 上一档(翻倍N)" if prev else ""))
    prev = el

print("\n=== ② 真实 nb-012 实证 ===")
_insp = ReadOnlySQLiteInspector(DB)
concepts = [{"object_id": oid, "name": name} for oid, name in _insp.concept_id_names(NB)]
vectors = {oid: arr.tolist() for oid, arr in _insp.knowledge_vectors(NB).items()}
vectors = {k: v for k, v in vectors.items() if len(v) == DIM}
print(f"  concepts={len(concepts)}  with_vec={len(vectors)}")

t = time.perf_counter()
res = cluster_concepts(concepts, vectors, set(), set())
print(f"  cluster_concepts: {time.perf_counter()-t:.2f}s")
print(f"  canonical={len(set(res['cluster_map'].values()))}  "
      f"auto_candidates={len(res['auto_candidates'])}  pending={len(res['pending'])}")

# 确定性 cluster_map 是否还有语义大杂烩(按 _norm 分组, 同簇必同 _norm → 应 0)
nm = {c["object_id"]: c["name"] for c in concepts}
mem = defaultdict(set)
for oid, cid in res["cluster_map"].items():
    mem[cid].add(_norm(nm[oid]))
blobs = [(cid, names) for cid, names in mem.items() if len(names) > 1]
print(f"  确定性图中跨_norm大杂烩簇 = {len(blobs)} (向量不再自动并 → 应=0)")

print("\n  auto_candidates 抽样(LLM 复核这些, 看是否还有 drain/source 类):")
for a, b, s in sorted(res["auto_candidates"], key=lambda x: -x[2])[:15]:
    print(f"    {s:.3f}  {a[2:]:34s} ⇄ {b[2:]}")

bad = sum(1 for a, b, s in res["auto_candidates"] + res["pending"]
          if _discriminative_conflict(a[2:], b[2:]))
print(f"\n  候选里仍含对立孪生(护栏漏网, 应=0): {bad}")
