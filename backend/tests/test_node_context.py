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
    ("el-nc-def-dep", NC_IN, "DEPRECATED definer text"),
    ("el-nc-def-rej", NC_IN, "REJECTED relation text"),
    ("el-nc-def-ok", NC_IN, "OK definition text"),
)
# 常量名与 PG 孪生一致:任何一条语句的任何一个绑定参数(字符串长度 / 数组的 JSON
# 长度)都不许超过它。天花板(生产可达 ~49k 个 id)只允许在两条稀有路径上进 SQL。
NODE_CONTEXT_MAX_BOUND_PARAM_CHARS = 256


def _nc_ev(element_id, source_id):
    return {"source_id": source_id, "source_title": source_id, "element_id": element_id,
            "element_type": "paragraph", "location_label": "p",
            "quoted_span": f"quote of {element_id}", "confidence": 1.0}


# 证据里归因不到来源的两种项:缺 source_id 键、source_id 为空串。
NC_UNATTRIBUTED = [{"element_id": "", "quoted_span": "stray"},
                   {"source_id": "", "element_id": "", "quoted_span": "blank"}]
# 来源多于 NODE_CONTEXT_CLUSTER_SOURCES_PROBE 的簇:1030 个不同来源,按 id 排序
# 最后一个是 src-nc-w1029(排在 1025 条探针之外)。
NC_WIDE_SOURCES = [f"src-nc-w{i:04d}" for i in range(1030)]


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
            {"name": "s-bare", "quote": "q-bare"},
        ]}, [_nc_ev("el-nc-step-in", NC_IN)]),
    # 合并对象:目标来自 NC_OUT(source_id 列 = NC_OUT),合并追加了 NC_IN 的证据;
    # payload(section_path、steps)仍是 NC_OUT 的 binder 产出的。
    ("ko-nc-payload-merged", "procedure", {
        "name": "Merged flow", "section_path": "OUT > Flow",
        "steps": [
            {"name": "s-in", "element_id": "el-nc-step-in", "quote": "q-in"},
            {"name": "s-gone2", "element_id": "el-nc-missing2", "quote": "q-gone2"},
            {"name": "s-bare2", "quote": "q-bare2"},
            {"name": "s-out2", "element_id": "el-nc-step-out", "quote": "q-out2"},
        ]}, [_nc_ev("el-nc-step-out", NC_OUT), _nc_ev("el-nc-step-in", NC_IN)]),
    # 无 section 的 legacy 过程(无天花板时是任意 500 行样本;天花板下 keyset 翻页)。
    ("ko-nc-nosec-in", "procedure", {"name": "nosec in"}, [_nc_ev("el-nc-step-in", NC_IN)]),
    ("ko-nc-nosec-out", "procedure", {"name": "nosec out"}, [_nc_ev("el-nc-step-out", NC_OUT)]),
    ("ko-nc-merged", "concept", {"name": "Merged", "section_path": "OUT > Heading"},
     [_nc_ev("el-nc-occ-out", NC_OUT), _nc_ev("el-nc-occ-in", NC_IN)]),
    # Q1 严格:有成员的证据归因不到来源 → 描述不用(两种形态:脏项、空证据)。
    ("ko-nc-unattr", "concept", {"name": "Unattr"}, [_nc_ev("el-nc-occ-in", NC_IN)]),
    ("ko-nc-unattr-peer", "concept", {"name": "Unattr"}, list(NC_UNATTRIBUTED)),
    ("ko-nc-bare", "concept", {"name": "Bare"}, [_nc_ev("el-nc-occ-in", NC_IN)]),
    ("ko-nc-bare-peer", "concept", {"name": "Bare"}, []),
    # 成员有天花板内来源、另带一条归因不到的项:成员本身可归因,描述照用。
    ("ko-nc-partial", "concept", {"name": "Partial"},
     [_nc_ev("el-nc-occ-in", NC_IN), NC_UNATTRIBUTED[0]]),
    ("ko-nc-partial-peer", "concept", {"name": "Partial"}, [_nc_ev("el-nc-def-in", NC_IN)]),
    # 来源多于探针上限的簇(退回 SQL 数组谓词)。
    ("ko-nc-wide", "concept", {"name": "Wide"}, [_nc_ev("el-nc-occ-in", NC_IN)]),
    ("ko-nc-wide-peer", "concept", {"name": "Wide"},
     [_nc_ev("", source_id) for source_id in NC_WIDE_SOURCES]),
    # 台账 B-8:r.id 序第一条的定义者已弃用、第二条关系被拒绝、第三条才合格。
    ("ko-nc-b8", "concept", {"name": "B8"}, [_nc_ev("el-nc-occ-in", NC_IN)]),
    ("ko-nc-definer-dep", "claim", {"name": "definer deprecated"},
     [_nc_ev("el-nc-def-dep", NC_IN)]),
    ("ko-nc-definer-rej", "claim", {"name": "definer rejected"},
     [_nc_ev("el-nc-def-rej", NC_IN)]),
    ("ko-nc-definer-ok", "claim", {"name": "definer ok"}, [_nc_ev("el-nc-def-ok", NC_IN)]),
    # 证据数组里的非对象项(脏数据)不许让 node_context 抛错。
    ("ko-nc-dirty", "concept", {"name": "Dirty"}, ["junk", _nc_ev("el-nc-occ-in", NC_IN)]),
    ("ko-nc-definer-dirty", "claim", {"name": "definer dirty"},
     [7, "junk", _nc_ev("el-nc-def-in", NC_IN)]),
)
# source_id 列缺省取第一条对象型证据的来源;这里是例外(兄弟过程的首条证据在
# 天花板外,但它自己创建于 NC_IN)。
NC_OBJECT_SOURCE = {"ko-nc-p-in": NC_IN}
NC_OBJECT_STATUS = {"ko-nc-definer-dep": "deprecated"}
NC_REJECTED_RELATIONS = {"rel-nc-b8-2"}


def _nc_object_source(object_id, evidence):
    if object_id in NC_OBJECT_SOURCE:
        return NC_OBJECT_SOURCE[object_id]
    first = next((item for item in evidence if isinstance(item, dict)), None)
    return NC_IN if first is None else first.get("source_id", "")
# 故意按 id 逆序插入:没有 ORDER BY 时 rowid 序会先读到 rel-nc-b。
NC_DEFINES = (
    ("rel-nc-name", "ko-nc-nameonly", "ko-nc-named"),
    ("rel-nc-mixed", "ko-nc-definer-in", "ko-nc-mixed"),
    ("rel-nc-b", "ko-nc-definer-in", "ko-nc-def"),
    ("rel-nc-a", "ko-nc-definer-out", "ko-nc-def"),
    ("rel-nc-b8-3", "ko-nc-definer-ok", "ko-nc-b8"),
    ("rel-nc-b8-2", "ko-nc-definer-rej", "ko-nc-b8"),
    ("rel-nc-b8-1", "ko-nc-definer-dep", "ko-nc-b8"),
    ("rel-nc-dirty", "ko-nc-definer-dirty", "ko-nc-dirty"),
)
NC_CLUSTERS = (
    ("K-nc-mixed", "ko-nc-mixed", "MIXED fused description"),
    ("K-nc-mixed", "ko-nc-mixed-peer", "MIXED fused description"),
    ("K-nc-allin", "ko-nc-allin", "ALL-IN fused description"),
    ("K-nc-allin", "ko-nc-allin-peer", "ALL-IN fused description"),
    ("K-nc-unattr", "ko-nc-unattr", "UNATTR fused description"),
    ("K-nc-unattr", "ko-nc-unattr-peer", "UNATTR fused description"),
    ("K-nc-bare", "ko-nc-bare", "BARE fused description"),
    ("K-nc-bare", "ko-nc-bare-peer", "BARE fused description"),
    ("K-nc-partial", "ko-nc-partial", "PARTIAL fused description"),
    ("K-nc-partial", "ko-nc-partial-peer", "PARTIAL fused description"),
    ("K-nc-wide", "ko-nc-wide", "WIDE fused description"),
    ("K-nc-wide", "ko-nc-wide-peer", "WIDE fused description"),
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
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,payload,evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       (oid, nb, otype, NC_OBJECT_STATUS.get(oid, "approved"), json.dumps(payload),
                        json.dumps(evidence), _nc_object_source(oid, evidence), now, now))
        for rid, definer, target in NC_DEFINES:
            db.execute("INSERT INTO knowledge_relations (id,notebook_id,source_id,source_object_id,target_object_id,edge_type,evidence,created_at,review_status) VALUES (?,?,?,?,?,'defines','[]',?,?)",
                       (rid, nb, NC_IN, definer, target, now,
                        "rejected" if rid in NC_REJECTED_RELATIONS else "pending"))
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
        # 与生产 source_ids_from_evidence 同规则:只收对象型、source_id 非空的项。
        for oid, source_id in sorted({
            (oid, item["source_id"]) for oid, _t, _p, ev in NC_OBJECTS for item in ev
            if isinstance(item, dict) and item.get("source_id")
        }):
            db.execute("INSERT INTO knowledge_object_sources (object_id,source_id,notebook_id) VALUES (?,?,?)", (oid, source_id, nb))
        db.execute("INSERT INTO unified_kg_state (notebook_id,updated_at,source_index_backfilled) VALUES (?,?,1) "
                   "ON CONFLICT(notebook_id) DO UPDATE SET source_index_backfilled=1", (nb, now))


def _nc(repo, nb, oid, allowed=None):
    return repo.node_context(nb, oid, allowed_source_ids=allowed)


def _steps(ctx):
    return [(s["name"], s["element_text"], s["section_path"]) for s in ctx["steps"]]


def _assert_node_context_ceiling(repo, nb, allowed):
    """``allowed`` 等价于 {NC_IN} 时的全部断言(各种容器形态、49k 宽清单共用)。"""
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
    # Q1 严格:有成员的证据归因不到任何来源(脏项 / 空证据)→ 描述不用。
    for oid in ("ko-nc-unattr", "ko-nc-bare"):
        scoped = _nc(repo, nb, oid, allowed)
        assert (scoped["definition"], scoped["definition_basis"]) == (None, None), oid
    # 成员本身可归因(另带一条归因不到的项)→ 描述照用。
    partial = _nc(repo, nb, "ko-nc-partial", allowed)
    assert (partial["definition"], partial["definition_basis"]) == (
        "PARTIAL fused description", "cluster_description")
    # 台账 B-8:弃用定义者、被拒关系不供定义文字。
    b8 = _nc(repo, nb, "ko-nc-b8", allowed)
    assert (b8["definition"], b8["definition_element_id"]) == ("OK definition text", "el-nc-def-ok")
    # 证据里的非对象项不抛错。
    dirty = _nc(repo, nb, "ko-nc-dirty", allowed)
    assert [o["source_id"] for o in dirty["occurrences"]] == [NC_IN]
    assert dirty["definition"] == "IN definition text"
    # section_path 只在对象自己的来源在天花板内时返回;name 是实体身份,保留。
    merged = _nc(repo, nb, "ko-nc-merged", allowed)
    assert (merged["name"], merged["section_path"]) == ("Merged", "")
    assert [o["source_id"] for o in merged["occurrences"]] == [NC_IN]
    # legacy steps:全外的兄弟丢掉;留下的兄弟原文取其第一条天花板内证据。
    assert _steps(_nc(repo, nb, "ko-nc-p-in", allowed)) == [
        ("step in", "IN step text", "NC > S")]
    assert _steps(_nc(repo, nb, "ko-nc-nosec-in", allowed)) == [("nosec in", "IN step text", "")]
    # payload steps:按步骤归因后整条去留。元素在外 → 丢(名字也丢);元素缺失 /
    # 无元素 → 归因到对象自己的来源(NC_IN,在内)→ 名字与 quote 都保留。
    payload = _nc(repo, nb, "ko-nc-payload", allowed)
    assert payload["section_path"] == "NC > P"
    assert _steps(payload) == [
        ("s-in", "IN step text", "NC > P"), ("s-gone", "q-gone", "NC > P"),
        ("s-bare", "q-bare", "NC > P")]
    # 合并对象的目标来自 NC_OUT:只剩元素在 NC_IN 的那一步,section 不返回。
    merged_flow = _nc(repo, nb, "ko-nc-payload-merged", allowed)
    assert (merged_flow["name"], merged_flow["section_path"]) == ("Merged flow", "")
    assert _steps(merged_flow) == [("s-in", "IN step text", "")]


def _assert_node_context_out_only(repo, nb):
    """天花板 = {NC_OUT}:对象自己的来源 NC_IN 在外的那一侧。"""
    out_only = _nc(repo, nb, "ko-nc-def", [NC_OUT])
    assert (out_only["definition"], out_only["definition_source_id"]) == ("OUT definition text", NC_OUT)
    payload = _nc(repo, nb, "ko-nc-payload", [NC_OUT])
    assert (payload["section_path"], _steps(payload)) == ("", [("s-out", "OUT step text", "")])
    merged_flow = _nc(repo, nb, "ko-nc-payload-merged", [NC_OUT])
    assert merged_flow["section_path"] == "OUT > Flow"
    assert _steps(merged_flow) == [
        ("s-gone2", "q-gone2", "OUT > Flow"), ("s-bare2", "q-bare2", "OUT > Flow"),
        ("s-out2", "OUT step text", "OUT > Flow")]
    assert _nc(repo, nb, "ko-nc-merged", [NC_OUT])["section_path"] == "OUT > Heading"
    # 兄弟过程 ko-nc-p-in 自己创建于 NC_IN:它的 section 不返回(步骤里也不带)。
    assert _steps(_nc(repo, nb, "ko-nc-p-in", [NC_OUT])) == [
        ("step in", "OUT step text", ""), ("step out", "OUT step text", "")]
    assert _steps(_nc(repo, nb, "ko-nc-nosec-in", [NC_OUT])) == [("nosec out", "OUT step text", "")]


def _assert_node_context_unscoped(repo, nb):
    ctx = _nc(repo, nb, "ko-nc-def")
    assert [o["source_id"] for o in ctx["occurrences"]] == [NC_OUT, NC_IN]
    assert (ctx["definition"], ctx["definition_basis"], ctx["definition_source_id"],
            ctx["definition_element_id"]) == (
        "OUT definition text", "defines_evidence", NC_OUT, "el-nc-def-out")
    named = _nc(repo, nb, "ko-nc-named")
    assert (named["definition"], named["definition_basis"], named["definition_source_id"]) == (
        "NAME-ONLY definer", "defines_name", None)
    for oid, text in (("ko-nc-mixed", "MIXED"), ("ko-nc-unattr", "UNATTR"),
                      ("ko-nc-bare", "BARE"), ("ko-nc-wide", "WIDE")):
        unscoped = _nc(repo, nb, oid)
        assert (unscoped["definition"], unscoped["definition_basis"]) == (
            f"{text} fused description", "cluster_description"), oid
    b8 = _nc(repo, nb, "ko-nc-b8")
    assert (b8["definition"], b8["definition_element_id"]) == ("OK definition text", "el-nc-def-ok")
    assert _nc(repo, nb, "ko-nc-dirty")["definition"] == "IN definition text"
    assert _nc(repo, nb, "ko-nc-merged")["section_path"] == "OUT > Heading"
    assert _steps(_nc(repo, nb, "ko-nc-p-in")) == [
        ("step in", "OUT step text", "NC > S"), ("step out", "OUT step text", "NC > S")]
    assert _steps(_nc(repo, nb, "ko-nc-nosec-in")) == [
        ("nosec out", "OUT step text", ""), ("nosec in", "IN step text", "")]
    assert _steps(_nc(repo, nb, "ko-nc-payload")) == [
        ("s-in", "IN step text", "NC > P"), ("s-out", "OUT step text", "NC > P"),
        ("s-gone", "q-gone", "NC > P"), ("s-bare", "q-bare", "NC > P")]
    assert _steps(_nc(repo, nb, "ko-nc-payload-merged")) == [
        ("s-in", "IN step text", "OUT > Flow"), ("s-gone2", "q-gone2", "OUT > Flow"),
        ("s-bare2", "q-bare2", "OUT > Flow"), ("s-out2", "OUT step text", "OUT > Flow")]


def _assert_node_context_denies_all(repo, nb):
    for oid in ("ko-nc-def", "ko-nc-named", "ko-nc-mixed", "ko-nc-allin", "ko-nc-merged"):
        ctx = _nc(repo, nb, oid, [])
        assert ctx["occurrences"] == []
        assert ctx["definition"] is None and ctx["definition_basis"] is None
        assert ctx["section_path"] == ""
    for oid in ("ko-nc-p-in", "ko-nc-nosec-in", "ko-nc-payload", "ko-nc-payload-merged"):
        assert _nc(repo, nb, oid, [])["steps"] == [], oid


# 天花板的容器形态:store 接受 frozenset / set / tuple / list,空 id 去掉。
NC_CEILING_FORMS = ([NC_IN], (NC_IN,), {NC_IN}, frozenset({NC_IN}), frozenset({NC_IN, ""}))


def test_node_context_applies_the_source_ceiling_on_both_index_branches(repo):
    nb = _seed_node_context_ceiling(repo)
    for backfill in (False, True):
        if backfill:
            # 认证之后走反向索引支,答案一模一样。
            _backfill_node_context_index(repo, nb)
        _assert_node_context_unscoped(repo, nb)
        for form in NC_CEILING_FORMS:
            _assert_node_context_ceiling(repo, nb, form)
        _assert_node_context_out_only(repo, nb)
        _assert_node_context_denies_all(repo, nb)
        assert _nc(repo, nb, "ko-nc-mixed", [NC_IN, NC_OUT])["definition_basis"] == "cluster_description"
        # 天花板覆盖两个来源也救不回归因不到来源的成员。
        assert _nc(repo, nb, "ko-nc-unattr", [NC_IN, NC_OUT])["definition_basis"] is None


def _assert_wide_cluster(repo, nb, base):
    """``base`` 之上加齐宽簇的 1030 个来源:探针读回的前 1025 个(按 id 排序)全在
    天花板内时,只有 keyset 读回的其余来源能暴露排在探针之外的那个。"""
    every = frozenset(base) | frozenset(NC_WIDE_SOURCES)
    assert _nc(repo, nb, "ko-nc-wide", every)["definition"] == "WIDE fused description"
    assert _nc(repo, nb, "ko-nc-wide", every - {NC_WIDE_SOURCES[-1]})["definition_basis"] is None
    assert _nc(repo, nb, "ko-nc-wide", every - {NC_WIDE_SOURCES[0]})["definition_basis"] is None


def test_node_context_cluster_with_more_sources_than_the_probe_reads_the_rest(repo):
    """簇的来源多于 NODE_CONTEXT_CLUSTER_SOURCES_PROBE,两条支。"""
    nb = _seed_node_context_ceiling(repo)
    for backfill in (False, True):
        if backfill:
            _backfill_node_context_index(repo, nb)
        _assert_wide_cluster(repo, nb, [NC_IN])


def _param_chars(param):
    if isinstance(param, (str, bytes)):
        return len(param)
    return len(json.dumps(param, default=str))


def test_node_context_never_binds_the_whole_ceiling(repo):
    """生产上的天花板是整库可见来源(~49k 个 32 字符 id、~1.7 MB)。node_context
    发出的**每一条**语句(簇描述、来源多于探针上限的簇、defines、payload steps、
    有 / 无 section 的 legacy 兄弟)的每一个绑定参数都不许超过
    NODE_CONTEXT_MAX_BOUND_PARAM_CHARS —— 天花板只在 Python 里判。"""
    from contextlib import contextmanager

    nb = _seed_node_context_ceiling(repo)
    wide = frozenset([NC_IN, *(f"{index:032x}" for index in range(49_000))])
    store = repo._runtime.knowledge
    seen = []

    class _Spy:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=()):
            seen.append(tuple(params or ()))
            return self._inner.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    original_connect = store._connect

    @contextmanager
    def spying_connect():
        with original_connect() as connection:
            yield _Spy(connection)

    store._connect = spying_connect
    try:
        for backfill in (False, True):
            if backfill:
                _backfill_node_context_index(repo, nb)
            seen.clear()
            _assert_node_context_ceiling(repo, nb, wide)
            _assert_wide_cluster(repo, nb, wide)
            assert seen
            largest = max(_param_chars(param) for params in seen for param in params)
            assert largest <= NODE_CONTEXT_MAX_BOUND_PARAM_CHARS, largest
    finally:
        del store._connect
