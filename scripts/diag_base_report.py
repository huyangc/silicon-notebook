#!/usr/bin/env python3
"""诊断:深度报告 / reasoning 为何不引用 base 库(只读,不改任何数据)。

在【部署环境】能 import app 的地方运行(通常 backend 目录,或仓库根,只要能加载
到真实 .env + 真实库)。示例:
    cd <repo>/backend && python ../scripts/diag_base_report.py
    # 或:python scripts/diag_base_report.py [active_notebook_id] [查询词]
不传参:active 用「最近一份报告的 notebook」,查询词用「base 里一个真实对象名」。

把整段输出贴回来即可。常见根因:base 是独立 notebook,若从未单独建过 scale ANN
索引(scale_index_status.exists=false / _scale_index 返回 None),又因规模大到
copyable=False 而被禁止暴力兜底,则 base 语义召回恒返 0 →联邦只带出 active
→报告只引用当前 notebook。修复=对 base 那个 notebook 建索引
(POST /notebooks/{base_id}/scale-index/rebuild,或前端「重建索引」)。"""
import sys, os, json
from collections import Counter

# 让 app 可 import(脚本可能被放在 backend/ 之外)
_here = os.path.dirname(os.path.abspath(__file__))
for _cand in (os.path.join(_here, "..", "backend"), _here, os.path.join(_here, ".."),
              os.getcwd(), os.path.join(os.getcwd(), "backend")):
    if os.path.isdir(os.path.join(_cand, "app")):
        sys.path.insert(0, os.path.abspath(_cand)); break

from app.core.config import Settings                     # noqa: E402
from app.services.sqlite_repository import SQLiteRepository  # noqa: E402


def _name(rk):
    try: return str((getattr(rk, "payload", {}) or {}).get("name", ""))[:44]
    except Exception: return ""


def main():
    s = Settings()
    repo = SQLiteRepository(s)
    print("=" * 60)
    print("== 0. 环境 ==")
    print("  DATABASE_URL      :", getattr(s, "database_url", "?"))
    print("  storage_dir       :", getattr(s, "silicon_notebook_storage_dir", getattr(s, "storage_dir", "?")))
    print("  copy_max_rows     :", getattr(s, "notebook_copy_max_rows", "?"),
          " copy_max_bytes:", getattr(s, "notebook_copy_max_bytes", "?"))
    print("  retrieval_top_n   :", getattr(s, "retrieval_top_n", "?"))
    print("  SCALE_SEARCH_INCLUDE_DELTA:", getattr(s, "scale_search_include_delta", "?"))
    print("  SCALE_AUTO_FOLD_ON_ADD    :", getattr(s, "scale_auto_fold_on_add", "?"))

    # --- 1. notebooks + tier ---
    with repo._connect() as db:
        nbs = db.execute("SELECT id,name,tier,created_by FROM notebooks").fetchall()
        kg_by_nb = {r["notebook_id"]: r["c"] for r in db.execute(
            "SELECT notebook_id, COUNT(*) c FROM knowledge_objects GROUP BY notebook_id")}
    print("\n== 1. notebooks ==")
    for n in nbs:
        print(f'  {n["id"]}  tier={str(n["tier"]):8}  kg={kg_by_nb.get(n["id"],0):>7}  '
              f'by={n["created_by"]}  name={str(n["name"])[:28]}')
    bases = [n for n in nbs if n["tier"] == "base"]
    if not bases:
        print("\n!!! 没有任何 tier='base' 的 notebook —— base 当然不会出现。"
              "先把基准库那个 notebook 设为 base(分析→设为基准库 / POST /tier)。")
        return
    base = bases[0]; base_id = base["id"]

    # --- 2. 最近报告的引用来源(直接看症状)---
    print("\n== 2. 最近一份 done 报告的引用 tier 分布 ==")
    active_id = sys.argv[1] if len(sys.argv) > 1 else None
    with repo._connect() as db:
        rep = db.execute("SELECT id,notebook_id,question,references_json,sections_json "
                         "FROM reports WHERE status='done' ORDER BY created_at DESC LIMIT 1").fetchone()
    if rep:
        refs = json.loads(rep["references_json"] or "[]")
        print(f'  report {rep["id"]}  active_nb={rep["notebook_id"]}')
        print(f'  Q: {str(rep["question"])[:70]}')
        print("  引用数:", len(refs), " tier 分布:", dict(Counter(x.get("tier", "?") for x in refs)))
        print("  来自 base 的引用:", sum(1 for x in refs if x.get("tier") == "base"),
              " / 来自 base_notebook_id:", sum(1 for x in refs if x.get("notebook_id") == base_id))
        for x in refs[:6]:
            print("     -", x.get("tier", "?"), x.get("source_title") or x.get("label"))
        active_id = active_id or rep["notebook_id"]
    else:
        print("  (无已完成报告)")
    active_id = active_id or next((n["id"] for n in nbs if n["tier"] != "base"), base_id)

    # --- 3. base 库索引/规模/可暴力性 ---
    print(f"\n== 3. base = {base_id} ({str(base['name'])[:30]}) ==")
    objs = kg_by_nb.get(base_id, 0)
    with repo._connect() as db:
        emb = db.execute("SELECT COUNT(*) c FROM knowledge_embeddings WHERE notebook_id=?",
                         (base_id,)).fetchone()["c"]
    print("  KG 对象:", objs, " 向量:", emb)
    try:
        cs = repo.notebook_copy_stats(base_id)
        print("  copyable(可暴力):", cs.get("copyable"), " size:", cs.get("size"), " rows:", cs.get("rows"))
    except Exception as e:
        print("  notebook_copy_stats 异常:", repr(e))
    try:
        st = repo.scale_index_status(base_id)
        print("  scale_index_status:", json.dumps(st, ensure_ascii=False, default=str)[:300])
    except Exception as e:
        print("  scale_index_status 异常:", repr(e))
    try:
        idx = repo._scale_index(base_id, allow_stale=True)
        if idx is None:
            print("  _scale_index: None  ←←← base 无可用 ANN 索引(语义召回会死)")
        else:
            print("  _scale_index: 有  ann_labels(已索引对象)=",
                  len(getattr(idx, "ann_labels", []) or []), " vs KG对象=", objs,
                  "  ←← 若远小于 KG对象 = base 大部分未索引(delta)")
    except Exception as e:
        print("  _scale_index 异常:", repr(e))

    # --- 4. 检索探针 ---
    q = sys.argv[2] if len(sys.argv) > 2 else None
    if not q:
        with repo._connect() as db:
            row = db.execute("SELECT payload FROM knowledge_objects WHERE notebook_id=? "
                             "AND status='approved' LIMIT 1", (base_id,)).fetchone()
        try: q = (json.loads(row["payload"]).get("name") if row else None)
        except Exception: q = None
    q = q or "transformer"
    print(f"\n== 4. 检索探针  query={q!r}  (取自 base 的真实对象名) ==")
    try:
        direct = repo._retrieve_scored(base_id, q)
        print("  A) _retrieve_scored(base):", len(direct), "hits",
              "  ←←← 若 0/极少 = base 自身检索就失效")
        for h in direct[:3]:
            print("       ", round(getattr(h, "relevance", 0), 3), h.object_id, _name(h))
    except Exception as e:
        print("  A) _retrieve_scored(base) 异常:", repr(e))
    try:
        fed = repo.federated_retrieve(active_id, q)
        nb_ids = Counter(getattr(h, "notebook_id", "?") for h in fed)
        from_base = sum(1 for h in fed if getattr(h, "notebook_id", "") == base_id)
        print(f"  B) federated_retrieve(active={active_id}):", len(fed), "hits;  来自 base:",
              from_base, "  ←←← 若 base=0 = 联邦没带出 base")
        print("       notebook_id 分布:", dict(nb_ids))
        print("       tier 分布:", dict(Counter(getattr(h, "tier", "?") for h in fed)))
    except Exception as e:
        print("  B) federated_retrieve 异常:", repr(e))
    print("=" * 60)


if __name__ == "__main__":
    main()
