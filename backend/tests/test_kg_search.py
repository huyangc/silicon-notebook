import sqlite3

from app.repositories.lexical_query import lexical_recall_terms
from app.repositories.sqlite.knowledge_store import KnowledgeStore
from app.services.kg.search import merge_search_hits

# Task 13: the FTS SQL moved to KnowledgeStore; kg/search keeps only the pure
# hit merging. The primitive is connection-taking, so the in-memory fixture
# keeps exercising it directly.
fts_search = KnowledgeStore.fts_search
chunk_fts_search = KnowledgeStore.chunk_fts_search


def _fts_db():
    db = sqlite3.connect(":memory:"); db.row_factory = sqlite3.Row
    db.execute("CREATE VIRTUAL TABLE kg_objects_fts USING fts5(object_id UNINDEXED, notebook_id UNINDEXED, name, tokenize='trigram')")
    db.execute("CREATE VIRTUAL TABLE chunks_fts USING fts5(chunk_id UNINDEXED, notebook_id UNINDEXED, text, tokenize='trigram')")
    rows = [("o1","nb","current mirror"),("o2","nb","MOSFET"),("o3","nb","mirror symmetry"),("o4","other","current mirror")]
    db.executemany("INSERT INTO kg_objects_fts (object_id,notebook_id,name) VALUES (?,?,?)", rows)
    db.executemany(
        "INSERT INTO chunks_fts (chunk_id,notebook_id,text) VALUES (?,?,?)",
        [
            ("c1", "nb", "DeepSeek uses a sparse router for mixture-of-experts"),
            ("c2", "nb", "DeepSeek architecture overview"),
            ("c3", "nb", "深度学习系统采用稀疏路由实现混合专家架构"),
            ("c4", "other", "DeepSeek mixture-of-experts"),
        ],
    )
    return db


def test_lexical_terms_are_shared_across_repository_backends():
    terms = lexical_recall_terms("DeepSeek ZXCV9000 深度学习系统")
    assert terms[0] == "DeepSeek ZXCV9000 深度学习系统"
    assert {"DeepSeek", "ZXCV9000", "深度学习系统", "深度学", "度学习"} <= set(terms)


def test_merge_dedup_prefers_lexical():
    lex = [{"object_id":"a","score":1.0,"match":"lexical"}]
    sem = [{"object_id":"a","score":0.9,"match":"semantic"},{"object_id":"b","score":0.8,"match":"semantic"}]
    out = merge_search_hits(lex, sem, k=10)
    by = {h["object_id"]: h for h in out}
    assert by["a"]["match"] == "lexical"
    assert "b" in by and by["b"]["match"] == "semantic"
    assert out == sorted(out, key=lambda h: -h["score"])
