#!/usr/bin/env python3
"""诊断:深度报告 / reasoning 为何不(如预期般)引用参考库(只读,不改任何数据)。

在【部署环境】能 import app 的地方运行(通常 backend 目录,或仓库根,只要能加载
到真实 .env + 真实库)。示例:
    cd <repo>/backend && python ../scripts/diag_base_report.py
    # 或:python scripts/diag_base_report.py [active_notebook_id] [查询词]
不传参:active 用「最近一份报告的 notebook」,查询词用「已挂载参考库里一个真实
对象名」。

把整段输出贴回来即可。常见根因(按频率排序 —— 多领域基准库上线后①是目前最
常见的一条,与索引无关):
  1. 该笔记本压根没挂任何参考库(notebook_bases 为空)。多领域基准库上线是
     "干净重来"——所有既有笔记本的挂载数都会清零,联邦检索对它们全部停止,
     直到用户自己去挂一个。修复=编辑该笔记本→「参考库」选择器→挑一个公共
     知识库挂上,和 scale 索引完全无关。
  2. 挂了,但边当前失效(被挂的库降级为个人库,或转让给了别人)——检索侧会
     跳过失效边,效果等同没挂。修复=等对方重新发布,或换一个仍是公共知识库
     的库挂载;同样和索引无关。
  3. 挂载确实生效,但目标库从未单独建过 scale ANN 索引(scale_index_status.
     exists=false / _scale_index 返回 None),又因规模大到 copyable=False 而被
     禁止暴力兜底,则该库的语义召回恒返 0 →联邦只带出 active 笔记本自己。
     这才是"索引"问题,修复=对该参考库建索引(POST /notebooks/{base_id}/
     scale-index/rebuild,或前端「重建索引」)。"""
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

    mnt = repo.maintenance

    # --- 1. notebooks + tier(全局清单,仅供人工核对,不作为诊断依据)---
    # 注意:「系统里存在 tier='base' 的笔记本」和「这个笔记本挂了参考库」是两件
    # 独立的事——多领域基准库之前两者等价(隐式全局唯一 base),现在不再等价,
    # 后者才是决定检索会不会带出参考库的真实开关(见下面 §2b)。
    nbs = mnt.notebook_rows()
    kg_by_nb = mnt.kg_object_counts_by_notebook()
    print("\n== 1. notebooks ==")
    for n in nbs:
        print(f'  {n["id"]}  tier={str(n["tier"]):8}  kg={kg_by_nb.get(n["id"],0):>7}  '
              f'by={n["created_by"]}  name={str(n["name"])[:28]}')
    any_base_tier_exists = any((n["tier"] or "") == "base" for n in nbs)

    # --- 2. 最近报告的引用来源(直接看症状),并从中兜底解析 active_id ---
    active_id = sys.argv[1] if len(sys.argv) > 1 else None
    rep = mnt.latest_done_report()
    refs = []
    print("\n== 2. 最近一份 done 报告的引用 tier 分布 ==")
    if rep:
        refs = json.loads(rep["references_json"] or "[]")
        print(f'  report {rep["id"]}  active_nb={rep["notebook_id"]}')
        print(f'  Q: {str(rep["question"])[:70]}')
        active_id = active_id or rep["notebook_id"]
    else:
        print("  (无已完成报告)")
    active_id = active_id or next((n["id"] for n in nbs if n["tier"] != "base"), (nbs[0]["id"] if nbs else ""))
    if not active_id:
        print("\n!!! 库里一个 notebook 都没有,无法诊断。")
        return

    # --- 2b. 先查这个笔记本自己的挂载集合(notebook_bases)—— 这是本诊断脚本
    # 最容易诊断错的一步:老版本脚本全局取「随便一个 tier='base' 的库」当唯一
    # 猜测对象,上线当天几乎所有笔记本的真实原因都是"根本没挂",却被误诊成
    # "没建索引"。---
    edges = repo.list_notebook_bases(active_id)
    print(f"\n== 2b. {active_id} 的挂载集合(notebook_bases)==")
    if not edges:
        print("  (空)")
        if any_base_tier_exists:
            print("\n!!! 该笔记本没有挂载任何参考库 —— 这是多领域基准库上线后最常见的\n"
                  "    base=0 原因,和 scale 索引无关(索引问题的前提是先挂上库)。系统里\n"
                  "    已经存在公共知识库,只是这个笔记本没挂。\n"
                  "    修复=编辑该笔记本 →「参考库」选择器 → 挑一个公共知识库挂上。")
        else:
            print("\n!!! 该笔记本没有挂载任何参考库,而且系统里目前没有任何 tier='base' 的\n"
                  "    公共知识库可供挂载。\n"
                  "    修复=先让 admin 把某个笔记本发布为公共知识库(分析→设为公共知识库 /\n"
                  "    POST /tier),再回来挂载。")
        return
    for e in edges:
        reason = f'  原因={e["inactive_reason"]}' if not e["active"] else ""
        print(f'  {e["id"]}  tier={str(e["tier"]):8}  active={str(e["active"]):5}  '
              f'name={str(e["name"])[:28]}{reason}')
    active_bases = [e for e in edges if e["active"]]
    if not active_bases:
        print("\n!!! 挂了,但挂载边当前全部失效(被挂库降级为个人库,或转让给了别人)——\n"
              "    检索侧会跳过它们,效果等同没挂,和 scale 索引无关。\n"
              "    修复=等对方重新发布该库,或换一个仍是公共知识库的库挂载。")
        return
    base_ids = {e["id"] for e in active_bases}

    if refs:
        print("\n  (报告引用回看)")
        print("  引用数:", len(refs), " tier 分布:", dict(Counter(x.get("tier", "?") for x in refs)))
        print("  来自 base 的引用:", sum(1 for x in refs if x.get("tier") == "base"),
              " / 来自已挂载参考库:", sum(1 for x in refs if x.get("notebook_id") in base_ids))
        for x in refs[:6]:
            print("     -", x.get("tier", "?"), x.get("source_title") or x.get("label"))

    # --- 3. 每个已挂载且生效的参考库:索引/规模/可暴力性 ---
    for base in active_bases:
        base_id = base["id"]
        print(f"\n== 3. 参考库 = {base_id} ({str(base['name'])[:30]}) ==")
        objs = kg_by_nb.get(base_id, 0)
        emb = mnt.count_knowledge_embeddings(base_id)
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
            idx = mnt.load_scale_index(base_id, allow_stale=True)
            if idx is None:
                print("  scale_index: None  ←←← 该库无可用 ANN 索引(语义召回会死)")
            else:
                print("  scale_index: 有  ann_labels(已索引对象)=",
                      len(getattr(idx, "ann_labels", []) or []), " vs KG对象=", objs,
                      "  ←← 若远小于 KG对象 = 大部分未索引(delta)")
        except Exception as e:
            print("  scale_index 异常:", repr(e))

    # --- 4. 检索探针 ---
    probe_base_id = next(iter(base_ids))
    q = sys.argv[2] if len(sys.argv) > 2 else None
    if not q:
        payload = mnt.sample_approved_object_payload(probe_base_id)
        try: q = (json.loads(payload).get("name") if payload else None)
        except Exception: q = None
    q = q or "transformer"
    print(f"\n== 4. 检索探针  query={q!r}  (取自已挂载参考库的真实对象名) ==")
    try:
        direct = repo.retrieval.retrieve_scored(probe_base_id, q)
        print("  A) retrieve_scored(参考库):", len(direct), "hits",
              "  ←←← 若 0/极少 = 该库自身检索就失效")
        for h in direct[:3]:
            print("       ", round(getattr(h, "relevance", 0), 3), h.object_id, _name(h))
    except Exception as e:
        print("  A) retrieve_scored(参考库) 异常:", repr(e))
    try:
        fed = repo.federated_retrieve(active_id, q)
        nb_ids = Counter(getattr(h, "notebook_id", "?") for h in fed)
        from_base = sum(1 for h in fed if getattr(h, "notebook_id", "") in base_ids)
        print(f"  B) federated_retrieve(active={active_id}):", len(fed), "hits;  来自已挂载参考库:",
              from_base, "  ←←← 若=0 = 联邦没带出任何参考库")
        print("       notebook_id 分布:", dict(nb_ids))
        print("       tier 分布:", dict(Counter(getattr(h, "tier", "?") for h in fed)))
    except Exception as e:
        print("  B) federated_retrieve 异常:", repr(e))

    # --- 5. 用最近报告的真实子查询,复现每节检索:参考库进没进上下文 ---
    # 结论判读:
    #   参考库(fed) 都=0  → 报告的子查询本就检不到参考库(问题太锚定当前文件/措辞不匹配)
    #   参考库(fed)>0 但 参考库(进上下文/top12)=0 → 最终重排/配额把参考库挤掉(检索侧 bug)
    #   参考库进上下文>0 但报告 0 引用 → 撰写 LLM 没引参考库(相关性/prompt,非检索)
    print("\n== 5. 复现最近报告每节子查询的参考库命中(检索侧是否带出参考库)==")
    try:
        rep2 = mnt.latest_done_report()
        outline = json.loads(rep2["outline_json"] or "[]") if rep2 else []
        if not outline:
            print("  (报告无 outline_json,跳过)")
        for sec in outline:
            title = str(sec.get("title", ""))[:16]
            for sq in (sec.get("sub_queries") or [])[:2]:
                try:
                    fed = repo.federated_retrieve(active_id, str(sq))
                    base_fed = sum(1 for h in fed if getattr(h, "notebook_id", "") in base_ids)
                    top = sorted(fed, key=lambda h: getattr(h, "relevance", 0), reverse=True)[:12]
                    base_top = sum(1 for h in top if getattr(h, "notebook_id", "") in base_ids)
                    _blk, idm = mnt.knowledge_context(active_id, top)
                    base_ctx = sum(1 for v in idm.values()
                                   if str(v.get("tier", "")) == "base"
                                   or v.get("object_id") in {h.object_id for h in top
                                                             if getattr(h, "notebook_id", "") in base_ids})
                    print(f"  节[{title}] SQ={str(sq)[:42]!r}: fed总={len(fed)} 参考库={base_fed} "
                          f"| top12里参考库={base_top} | 进上下文参考库={base_ctx}")
                except Exception as e:
                    print(f"  节[{title}] SQ={str(sq)[:30]!r} 探针异常:", repr(e))
    except Exception as e:
        print("  §5 异常:", repr(e))

    # --- 6. PPR 是否把参考库原文 chunk 带进来(scale_ppr 跨层验证)---
    # scale_ppr(active) 会把参考库⊕active splice 成一张合并图跑 PPR,返回的 chunk
    # 可含参考库原文。判读:
    #   参考库原文 chunk > 0 → 参考库原文确实经 PPR 进了报告/reasoning(跨层生效)
    #   scale_ppr 返回 0     → 走了 rustworkx 单库兜底(参考库无索引/bail)→ PPR 不跨层
    print(f"\n== 6. PPR 跨层:参考库原文 chunk 是否进 PPR(active={active_id}, query={q!r})==")
    try:
        chunks = repo.retrieval.ppr_retrieve(active_id, q)
        ids = [c.chunk_id for c in chunks]
        nb_of = mnt.chunk_notebook_map(ids)
        base_ch = sum(1 for cid in ids if nb_of.get(cid) in base_ids)
        print(f"  ppr_retrieve(active) 返回 {len(chunks)} chunk;其中参考库原文: {base_ch}"
              "   ←←← >0 = 参考库原文确经 PPR(scale_ppr 跨层)进来")
        print("   chunk 归属 notebook 分布:",
              dict(Counter(nb_of.get(c.chunk_id, "?") for c in chunks)))
        try:
            ranked = repo.scale_ppr(active_id, q)
            print(f"  scale_ppr(active) 直接返回 {len(ranked)} chunk 排名"
                  "  (0=走了 rustworkx 单库兜底/参考库无索引→PPR 不跨层)")
        except Exception as e:
            print("  scale_ppr 探针异常:", repr(e))
        for c in chunks[:4]:
            tag = "参考库" if nb_of.get(c.chunk_id) in base_ids else "active"
            print(f"     [{tag}] {str(c.source_title)[:28]} · {str(c.text)[:44]}")
    except Exception as e:
        print("  §6 异常:", repr(e))
    print("=" * 60)


if __name__ == "__main__":
    main()
