"""KG 节点搜索纯逻辑:FTS5 词法查询 + 词法/语义结果合并。DB/ANN 由调用方提供。"""
from __future__ import annotations
from typing import List, Dict


def fts_search(db, notebook_id: str, q: str, k: int = 30) -> List[Dict]:
    """FTS5 MATCH(kg_objects_fts, trigram)。notebook 维度过滤。返回
    [{object_id, name, score, match:'lexical'}]。q 空 → []。"""
    needle = (q or "").strip()
    if not needle:
        return []
    rows = db.execute(
        "SELECT object_id, name, bm25(kg_objects_fts) AS rank "
        "FROM kg_objects_fts WHERE notebook_id=? AND kg_objects_fts MATCH ? "
        "ORDER BY rank LIMIT ?",
        (notebook_id, '"' + needle.replace('"', '""') + '"', k)).fetchall()
    return [{"object_id": r["object_id"], "name": r["name"],
             "score": -float(r["rank"]), "match": "lexical"} for r in rows]


def merge_search_hits(lexical: List[Dict], semantic: List[Dict], k: int = 30) -> List[Dict]:
    """合并词法 ∪ 语义,按 object_id 去重(词法优先),按 score 降序,截断 k。"""
    by: Dict[str, Dict] = {}
    for h in lexical:
        by[h["object_id"]] = h
    for h in semantic:
        by.setdefault(h["object_id"], h)
    return sorted(by.values(), key=lambda h: -h["score"])[:k]
