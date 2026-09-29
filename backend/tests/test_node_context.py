import json, pytest, datetime
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.models.schemas import NotebookCreate

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())

def _src_with_elements(repo, nb, texts):
    from uuid import uuid4
    sid = f"src-{uuid4().hex[:8]}"; now = datetime.datetime.now().isoformat()
    ids = []
    with repo._connect() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,file_name,file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) VALUES (?,?,?,'markdown','extracted','parsed','d.md','',0,'','','academic_paper',?,?)", (sid, nb, "Doc", now, now))
        for i, t in enumerate(texts):
            eid = f"el-{uuid4().hex[:8]}"; ids.append(eid)
            db.execute("INSERT INTO source_elements (id,source_id,element_type,location_label,text,metadata,created_at) VALUES (?,?,?,?,?, '{}', ?)", (eid, sid, "paragraph", f"p{i}", t, f"{now}-{i:03d}"))
    return sid, ids

def test_node_context_concept_sentence_and_definition(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid, eids = _src_with_elements(repo, nb.id, [
        "As shown in Figure 1, Engram is a conditional memory module.",
        "Engram is defined as a structured store separating memory.",
    ])
    def ev(eid, span): return {"source_id": sid, "source_title": "Doc", "element_id": eid, "element_type": "paragraph", "location_label": "p", "quoted_span": span, "confidence": 1.0}
    repo.store_kg(nb.id, sid, [
        {"local_id":"c","object_type":"concept","payload":{"name":"Engram","section_path":"1"},"evidence":[ev(eids[0],"Engram")]},
        {"local_id":"k","object_type":"claim","payload":{"name":"Engram is a structured store","section_path":"1"},"evidence":[ev(eids[1],"Engram is defined as")]},
    ], [{"source_local_id":"k","target_local_id":"c","edge_type":"defines","evidence":[]}])
    with repo._connect() as db:
        cid = next(r["id"] for r in db.execute("SELECT id,object_type FROM knowledge_objects WHERE notebook_id=?", (nb.id,)).fetchall() if r["object_type"]=="concept")
    ctx = repo.node_context(nb.id, cid)
    assert ctx["object_type"] == "concept"
    assert "conditional memory module" in ctx["occurrences"][0]["element_text"]
    assert "structured store" in (ctx["definition"] or "")

def test_node_context_procedure_steps_doc_order(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid, eids = _src_with_elements(repo, nb.id, [
        "First, we extract and compress suffix N-grams.",
        "Subsequently, embeddings are modulated by the hidden state.",
        "Finally, the result is refined via a lightweight convolution.",
    ])
    def ev(eid): return {"source_id": sid, "source_title": "Doc", "element_id": eid, "element_type": "paragraph", "location_label": "p", "quoted_span": "x", "confidence": 1.0}
    repo.store_kg(nb.id, sid, [
        {"local_id":"p2","object_type":"procedure","payload":{"name":"modulate","section_path":"2.2"},"evidence":[ev(eids[1])]},
        {"local_id":"p1","object_type":"procedure","payload":{"name":"extract","section_path":"2.2"},"evidence":[ev(eids[0])]},
        {"local_id":"p3","object_type":"procedure","payload":{"name":"refine","section_path":"2.2"},"evidence":[ev(eids[2])]},
    ], [])
    with repo._connect() as db:
        pid = next(r["id"] for r in db.execute("SELECT id FROM knowledge_objects WHERE notebook_id=? AND json_extract(payload,'$.name')='extract'", (nb.id,)).fetchall())
    ctx = repo.node_context(nb.id, pid)
    names = [s["name"] for s in ctx["steps"]]
    assert names == ["extract", "modulate", "refine"]
    assert "suffix N-grams" in ctx["steps"][0]["element_text"]

def test_element_texts_does_not_scan_entire_notebook(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid, eids = _src_with_elements(repo, nb.id, ["A target sentence.", "Another sentence."])

    executed = []
    original_connect = repo._connect

    class TrackingConnection:
        def __init__(self, inner):
            self.inner = inner
        def __enter__(self):
            self.conn = self.inner.__enter__()
            return self
        def __exit__(self, *args):
            return self.inner.__exit__(*args)
        def execute(self, sql, params=()):
            executed.append(" ".join(sql.split()))
            return self.conn.execute(sql, params)
        def __getattr__(self, name):
            return getattr(self.conn, name)

    monkeypatch.setattr(repo, "_connect", lambda: TrackingConnection(original_connect()))
    with repo._connect() as db:
        texts, ordinal = repo._element_texts(db, [eids[0]])

    assert texts[eids[0]] == "A target sentence."
    assert ordinal == {}
    assert not any("ORDER BY se.created_at ASC, se.id ASC" in sql for sql in executed)


def test_concept_detail_includes_element_text(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid, eids = _src_with_elements(repo, nb.id, ["As shown, Engram is a conditional memory module."])
    ev = {"source_id": sid, "source_title": "Doc", "element_id": eids[0], "element_type": "paragraph", "location_label": "p", "quoted_span": "Engram", "confidence": 1.0}
    repo.store_kg(nb.id, sid, [{"local_id":"c","object_type":"concept","payload":{"name":"Engram","section_path":"1"},"evidence":[ev]}], [])
    repo.rebuild_unified_kg(nb.id)
    cid = list(repo.cluster_map(nb.id).values())[0]
    d = repo.concept_detail(nb.id, cid)
    assert any("conditional memory module" in (e.get("element_text") or "") for e in d["evidence"])


def test_formula_evidence_metadata_survives_context_enrichment(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    formula = r"C _ {l} = 2 \sigma (\tilde {C} _ {l}).\tag{7}"
    sid, eids = _src_with_elements(repo, nb.id, [formula])
    with repo._connect() as db:
        db.execute(
            "UPDATE source_elements SET element_type='formula', location_label='eq. 7' WHERE id=?",
            (eids[0],),
        )

    evidence = {
        "source_id": sid,
        "source_title": "2606.19348v1.pdf",
        "element_id": eids[0],
        # Persisted metadata can be stale; SourceElement is authoritative.
        "element_type": "paragraph",
        "location_label": "old location",
        "quoted_span": formula,
        "confidence": 0.98,
    }
    repo.store_kg(
        nb.id,
        sid,
        [{
            "local_id": "f",
            "object_type": "formula",
            "payload": {"name": formula, "section_path": "2"},
            "evidence": [evidence],
        }],
        [],
    )
    with repo._connect() as db:
        object_id = db.execute(
            "SELECT id FROM knowledge_objects WHERE notebook_id=?",
            (nb.id,),
        ).fetchone()["id"]

    occurrence = repo.node_context(nb.id, object_id)["occurrences"][0]
    assert occurrence == {
        **evidence,
        "element_type": "formula",
        "location_label": "eq. 7",
        "element_text": formula,
    }

    repo.rebuild_unified_kg(nb.id)
    canonical_id = repo.cluster_map(nb.id)[object_id]
    detail_evidence = repo.concept_detail(nb.id, canonical_id)["evidence"][0]
    assert detail_evidence["element_type"] == "formula"
    assert detail_evidence["element_text"] == formula


# ---------------------------------------------------------------------------
# PR-A·A1:``node_context`` 认来源天花板 —— PG 孪生
# (tests/postgres/test_knowledge_store_conformance.py 同名场景)是规范定义。
NC_IN, NC_OUT = "src-nc-in", "src-nc-out"
NC_ELEMENTS = (
    ("el-nc-occ-out", NC_OUT, "OUT occurrence text"),
    ("el-nc-occ-in", NC_IN, "IN occurrence text"),
    ("el-nc-def-out", NC_OUT, "OUT definition text"),
    ("el-nc-def-in", NC_IN, "IN definition text"),
    ("el-nc-step-out", NC_OUT, "OUT step text"),
    ("el-nc-step-in", NC_IN, "IN step text"),
)


def _nc_ev(element_id, source_id):
    return {"source_id": source_id, "source_title": source_id, "element_id": element_id,
            "element_type": "paragraph", "location_label": "p",
            "quoted_span": f"quote of {element_id}", "confidence": 1.0}


NC_OBJECTS = (
    ("ko-nc-def", "concept", {"name": "Defined"},
     [_nc_ev("el-nc-occ-out", NC_OUT), _nc_ev("el-nc-occ-in", NC_IN)]),
    ("ko-nc-definer-out", "claim", {"name": "definer out"}, [_nc_ev("el-nc-def-out", NC_OUT)]),
    ("ko-nc-definer-in", "claim", {"name": "definer in"}, [_nc_ev("el-nc-def-in", NC_IN)]),
    ("ko-nc-named", "concept", {"name": "Named"}, [_nc_ev("el-nc-occ-in", NC_IN)]),
    ("ko-nc-nameonly", "claim", {"name": "NAME-ONLY definer"}, []),
    ("ko-nc-mixed", "concept", {"name": "Mixed"}, [_nc_ev("el-nc-occ-in", NC_IN)]),
    ("ko-nc-mixed-peer", "concept", {"name": "Mixed"}, [_nc_ev("el-nc-occ-out", NC_OUT)]),
    ("ko-nc-allin", "concept", {"name": "AllIn"}, [_nc_ev("el-nc-occ-in", NC_IN)]),
    ("ko-nc-allin-peer", "concept", {"name": "AllIn"}, [_nc_ev("el-nc-def-in", NC_IN)]),
    ("ko-nc-p-in", "procedure", {"name": "step in", "section_path": "NC > S"},
     [_nc_ev("el-nc-step-out", NC_OUT), _nc_ev("el-nc-step-in", NC_IN)]),
    ("ko-nc-p-out", "procedure", {"name": "step out", "section_path": "NC > S"},
     [_nc_ev("el-nc-step-out", NC_OUT)]),
    ("ko-nc-payload", "procedure", {
        "name": "Payload flow", "section_path": "NC > P",
        "steps": [
            {"name": "s-in", "element_id": "el-nc-step-in", "quote": "q-in"},
            {"name": "s-out", "element_id": "el-nc-step-out", "quote": "q-out"},
            {"name": "s-gone", "element_id": "el-nc-missing", "quote": "q-gone"},
        ]}, [_nc_ev("el-nc-step-in", NC_IN)]),
)
# 故意按 id 逆序插入:没有 ORDER BY 时 rowid 序会先读到 rel-nc-b。
NC_DEFINES = (
    ("rel-nc-name", "ko-nc-nameonly", "ko-nc-named"),
    ("rel-nc-mixed", "ko-nc-definer-in", "ko-nc-mixed"),
    ("rel-nc-b", "ko-nc-definer-in", "ko-nc-def"),
    ("rel-nc-a", "ko-nc-definer-out", "ko-nc-def"),
)
NC_CLUSTERS = (
    ("K-nc-mixed", "ko-nc-mixed", "MIXED fused description"),
    ("K-nc-mixed", "ko-nc-mixed-peer", "MIXED fused description"),
    ("K-nc-allin", "ko-nc-allin", "ALL-IN fused description"),
    ("K-nc-allin", "ko-nc-allin-peer", "ALL-IN fused description"),
)


def _seed_node_context_ceiling(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    now = datetime.datetime.now().isoformat()
    with repo._connect() as db:
        for sid in (NC_IN, NC_OUT):
            db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,file_name,file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) VALUES (?,?,?,'markdown','extracted','parsed','d.md','',0,'','','academic_paper',?,?)", (sid, nb, sid, now, now))
        for i, (eid, sid, text) in enumerate(NC_ELEMENTS):
            db.execute("INSERT INTO source_elements (id,source_id,element_type,location_label,text,metadata,created_at) VALUES (?,?,'paragraph','p',?,'{}',?)", (eid, sid, text, f"{now}-{i:03d}"))
        for oid, otype, payload, evidence in NC_OBJECTS:
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,payload,evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                       (oid, nb, otype, json.dumps(payload), json.dumps(evidence), evidence[0]["source_id"] if evidence else NC_IN, now, now))
        for rid, definer, target in NC_DEFINES:
            db.execute("INSERT INTO knowledge_relations (id,notebook_id,source_id,source_object_id,target_object_id,edge_type,evidence,created_at) VALUES (?,?,?,?,?,'defines','[]',?)",
                       (rid, nb, NC_IN, definer, target, now))
        for i, (canonical, member, description) in enumerate(NC_CLUSTERS):
            db.execute("INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,canonical_description,created_at) VALUES (?,?,?,?,'N',?,?)",
                       (f"cluster-nc-{i}", nb, canonical, member, description, now))
        # 反向索引未认证、且一行都没有 → 权威支才答得对。
        db.execute("DELETE FROM knowledge_object_sources WHERE notebook_id=?", (nb,))
        db.execute("UPDATE unified_kg_state SET source_index_backfilled=0 WHERE notebook_id=?", (nb,))
    return nb


def _backfill_node_context_index(repo, nb):
    now = datetime.datetime.now().isoformat()
    with repo._connect() as db:
        for oid, source_id in sorted({(oid, item["source_id"]) for oid, _t, _p, ev in NC_OBJECTS for item in ev}):
            db.execute("INSERT INTO knowledge_object_sources (object_id,source_id,notebook_id) VALUES (?,?,?)", (oid, source_id, nb))
        db.execute("INSERT INTO unified_kg_state (notebook_id,updated_at,source_index_backfilled) VALUES (?,?,1) "
                   "ON CONFLICT(notebook_id) DO UPDATE SET source_index_backfilled=1", (nb, now))


def _nc(repo, nb, oid, allowed=None):
    return repo.node_context(nb, oid, allowed_source_ids=allowed)


def _assert_node_context_ceiling(repo, nb, allowed):
    ctx = _nc(repo, nb, "ko-nc-def", allowed)
    assert [o["source_id"] for o in ctx["occurrences"]] == [NC_IN]
    assert (ctx["definition"], ctx["definition_basis"], ctx["definition_source_id"],
            ctx["definition_element_id"]) == (
        "IN definition text", "defines_evidence", NC_IN, "el-nc-def-in")
    named = _nc(repo, nb, "ko-nc-named", allowed)
    assert named["definition"] is None and named["definition_basis"] is None
    mixed = _nc(repo, nb, "ko-nc-mixed", allowed)
    assert (mixed["definition"], mixed["definition_basis"]) == (
        "IN definition text", "defines_evidence")
    allin = _nc(repo, nb, "ko-nc-allin", allowed)
    assert (allin["definition"], allin["definition_basis"]) == (
        "ALL-IN fused description", "cluster_description")
    assert _nc(repo, nb, "ko-nc-p-in", allowed)["steps"] == [
        {"name": "step in", "element_text": "IN step text", "section_path": "NC > S"}]
    assert [(s["name"], s["element_text"]) for s in _nc(repo, nb, "ko-nc-payload", allowed)["steps"]] == [
        ("s-in", "IN step text"), ("s-out", ""), ("s-gone", "")]


def _assert_node_context_unscoped(repo, nb):
    ctx = _nc(repo, nb, "ko-nc-def")
    assert [o["source_id"] for o in ctx["occurrences"]] == [NC_OUT, NC_IN]
    assert (ctx["definition"], ctx["definition_basis"], ctx["definition_source_id"],
            ctx["definition_element_id"]) == (
        "OUT definition text", "defines_evidence", NC_OUT, "el-nc-def-out")
    named = _nc(repo, nb, "ko-nc-named")
    assert (named["definition"], named["definition_basis"], named["definition_source_id"]) == (
        "NAME-ONLY definer", "defines_name", None)
    mixed = _nc(repo, nb, "ko-nc-mixed")
    assert (mixed["definition"], mixed["definition_basis"]) == (
        "MIXED fused description", "cluster_description")
    assert [(s["name"], s["element_text"]) for s in _nc(repo, nb, "ko-nc-p-in")["steps"]] == [
        ("step in", "OUT step text"), ("step out", "OUT step text")]
    assert [(s["name"], s["element_text"]) for s in _nc(repo, nb, "ko-nc-payload")["steps"]] == [
        ("s-in", "IN step text"), ("s-out", "OUT step text"), ("s-gone", "q-gone")]


def _assert_node_context_denies_all(repo, nb):
    for oid in ("ko-nc-def", "ko-nc-named", "ko-nc-mixed", "ko-nc-allin"):
        ctx = _nc(repo, nb, oid, [])
        assert ctx["occurrences"] == []
        assert ctx["definition"] is None and ctx["definition_basis"] is None
    assert _nc(repo, nb, "ko-nc-p-in", [])["steps"] == []
    assert [s["element_text"] for s in _nc(repo, nb, "ko-nc-payload", [])["steps"]] == ["", "", ""]


def test_node_context_applies_the_source_ceiling_on_both_index_branches(repo):
    nb = _seed_node_context_ceiling(repo)
    _assert_node_context_unscoped(repo, nb)
    _assert_node_context_ceiling(repo, nb, [NC_IN])
    _assert_node_context_denies_all(repo, nb)
    assert _nc(repo, nb, "ko-nc-mixed", [NC_IN, NC_OUT])["definition_basis"] == "cluster_description"

    _backfill_node_context_index(repo, nb)
    _assert_node_context_unscoped(repo, nb)
    _assert_node_context_ceiling(repo, nb, [NC_IN])
    _assert_node_context_denies_all(repo, nb)
    assert _nc(repo, nb, "ko-nc-mixed", [NC_IN, NC_OUT])["definition_basis"] == "cluster_description"
    out_only = _nc(repo, nb, "ko-nc-def", [NC_OUT])
    assert (out_only["definition"], out_only["definition_source_id"]) == ("OUT definition text", NC_OUT)


def test_node_context_binds_a_whole_library_ceiling_as_one_parameter(repo):
    """5,000 个来源 id 恒为一个 ``json_each(?)`` 参数:逐个占位符会撞
    ``SQLITE_MAX_VARIABLE_NUMBER``(默认 32766 之前的构建是 999)。"""
    import sqlite3

    nb = _seed_node_context_ceiling(repo)
    wide = [NC_IN] + [f"s-filler-{i}" for i in range(5000)]
    for backfill in (False, True):
        if backfill:
            _backfill_node_context_index(repo, nb)
        # 同线程复用同一条连接:把变量上限压到 50,逐个占位符的实现必然抛错。
        with repo._connect() as db:
            previous = db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 50)
        try:
            _assert_node_context_ceiling(repo, nb, wide)
        finally:
            with repo._connect() as db:
                db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous)
