import sqlite3
from app.services.kg.search import fts_search, merge_search_hits


def _fts_db():
    db = sqlite3.connect(":memory:"); db.row_factory = sqlite3.Row
    db.execute("CREATE VIRTUAL TABLE kg_objects_fts USING fts5(object_id UNINDEXED, notebook_id UNINDEXED, name, tokenize='trigram')")
    rows = [("o1","nb","current mirror"),("o2","nb","MOSFET"),("o3","nb","mirror symmetry"),("o4","other","current mirror")]
    db.executemany("INSERT INTO kg_objects_fts (object_id,notebook_id,name) VALUES (?,?,?)", rows)
    return db


def test_fts_search_substring_scoped_to_notebook():
    db = _fts_db()
    hits = fts_search(db, "nb", "mirror", k=10)
    ids = {h["object_id"] for h in hits}
    assert ids == {"o1", "o3"}


def test_merge_dedup_prefers_lexical():
    lex = [{"object_id":"a","score":1.0,"match":"lexical"}]
    sem = [{"object_id":"a","score":0.9,"match":"semantic"},{"object_id":"b","score":0.8,"match":"semantic"}]
    out = merge_search_hits(lex, sem, k=10)
    by = {h["object_id"]: h for h in out}
    assert by["a"]["match"] == "lexical"
    assert "b" in by and by["b"]["match"] == "semantic"
    assert out == sorted(out, key=lambda h: -h["score"])
