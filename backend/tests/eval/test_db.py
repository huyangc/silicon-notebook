import json, sqlite3, pathlib
from app.eval.db import source_of, EvalDB


def test_source_of_takes_first_evidence():
    ev = json.dumps([{"source_id": "src-A", "element_id": "e1"},
                     {"source_id": "src-B"}])
    assert source_of(ev) == "src-A"


def test_source_of_handles_empty_and_bad():
    assert source_of("[]") is None
    assert source_of("") is None
    assert source_of("not json") is None
    assert source_of(None) is None


def _mk_db(tmp_path):
    p = tmp_path / "t.db"
    db = sqlite3.connect(p)
    db.executescript(
        """
        CREATE TABLE knowledge_objects(id TEXT, notebook_id TEXT, object_type TEXT,
          status TEXT, payload TEXT, evidence TEXT);
        CREATE TABLE knowledge_relations(id TEXT, notebook_id TEXT, source_id TEXT,
          source_object_id TEXT, target_object_id TEXT, edge_type TEXT, evidence TEXT);
        """)
    db.execute("INSERT INTO knowledge_objects VALUES(?,?,?,?,?,?)",
               ("ko1", "nb", "concept", "approved",
                json.dumps({"name": "cascode", "section_path": "5"}),
                json.dumps([{"source_id": "src-A", "element_id": "e1"}])))
    db.execute("INSERT INTO knowledge_objects VALUES(?,?,?,?,?,?)",
               ("ko2", "nb", "concept", "approved",
                json.dumps({"name": "Vb1", "section_path": "5"}),
                json.dumps([{"source_id": "src-B", "element_id": "e2"}])))
    db.execute("INSERT INTO knowledge_relations VALUES(?,?,?,?,?,?,?)",
               ("r1", "nb", "src-A", "ko1", "ko2", "about", "[]"))
    db.commit(); db.close()
    return p


def test_evaldb_objects_and_degree(tmp_path):
    p = _mk_db(tmp_path)
    ed = EvalDB(str(p))
    objs = ed.objects("nb", "concept")
    assert len(objs) == 2
    by_name = {o["name"]: o for o in objs}
    assert by_name["cascode"]["source_id"] == "src-A"
    assert by_name["cascode"]["payload"]["section_path"] == "5"
    deg = ed.relation_degree("nb")
    assert deg["ko1"] == 1 and deg["ko2"] == 1
