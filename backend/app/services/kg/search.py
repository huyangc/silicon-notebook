"""KG 节点搜索纯逻辑:词法/语义结果合并。

Task 13:FTS SQL(fts_search / chunk_fts_search)移入
app.repositories.sqlite.knowledge_store.KnowledgeStore;本模块只保留纯合并。"""
from __future__ import annotations
from typing import List, Dict


def merge_search_hits(lexical: List[Dict], semantic: List[Dict], k: int = 30) -> List[Dict]:
    """合并词法 ∪ 语义,按 object_id 去重(词法优先),按 score 降序,截断 k。"""
    by: Dict[str, Dict] = {}
    for h in lexical:
        by[h["object_id"]] = h
    for h in semantic:
        by.setdefault(h["object_id"], h)
    return sorted(by.values(), key=lambda h: -h["score"])[:k]
