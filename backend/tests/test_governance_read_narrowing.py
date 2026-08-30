# backend/tests/test_governance_read_narrowing.py
"""P1 批 A+B — 治理路径读取瘦身的等价性与有界性护栏。

批 A(review_queue):对象读取从"全 notebook 全部对象"收窄到"关系端点对象",
关系读取的 evidence 正文列换成 SQL 侧派生的锚点信号,排序改 heapq.nlargest。
批 B(promotion):base 去重语料读取去掉 evidence 列,命中后按 id 单行补读。

两批都是**纯读取瘦身**,HTTP 响应必须逐位不变——所以主证据是把改造前的实现
原样写成 oracle 逐项对照;另加一条"对象读取行数有界"的护栏,让恢复全量读的
变异真的报红(等价 oracle 对无损收窄是永远绿的,单靠它证明不了收窄发生过)。
"""
import json

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.sqlite.governance_store import (
    GovernanceStore,
    _review_endpoint_ids,
)
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


# ── the pre-change implementation, kept verbatim as an oracle ────────────────

def _old_review_queue(repo, notebook_id: str, limit: int = 200) -> list:
    """`KnowledgeGovernanceService.review_queue` exactly as it read before the
    P1 批 A narrowing: full-notebook object scan, evidence JSON parsed in
    Python, `items.sort(...)[:limit]`.

    ⚠ A second hand-copy of (an earlier state of) the same read path lives in
    ``test_query_hotpath_cache.test_review_queue_output_matches_uncached_oracle``
    — that one pins centrality caching, this one pins the read narrowing.
    Deliberately not shared: each is only meaningful as a frozen copy of the
    state its own test predates.  When the service changes, check whether both
    need to move.
    """
    from app.services.kg.edge_trust import (
        compute_trust_score, corroboration_counts, corroboration_score_from_count,
    )

    with repo._connect() as db:
        rel_rows = db.execute(
            "SELECT kr.id, kr.source_object_id, kr.target_object_id, "
            "kr.edge_type, kr.evidence, kr.source_id, kr.review_status, "
            "ko_s.object_type AS src_type, ko_t.object_type AS tgt_type "
            "FROM knowledge_relations kr "
            "LEFT JOIN knowledge_objects ko_s ON ko_s.id = kr.source_object_id "
            "LEFT JOIN knowledge_objects ko_t ON ko_t.id = kr.target_object_id "
            "WHERE kr.notebook_id = ? AND kr.review_status != 'rejected'",
            (notebook_id,),
        ).fetchall()
        obj_rows = db.execute(
            "SELECT id, object_type, payload FROM knowledge_objects "
            "WHERE notebook_id = ?", (notebook_id,),
        ).fetchall()

    node_types, node_names = {}, {}
    for r in obj_rows:
        node_types[r["id"]] = r["object_type"]
        node_names[r["id"]] = json.loads(r["payload"] or "{}").get("name", "")

    rels = [{
        "id": r["id"],
        "source_object_id": r["source_object_id"],
        "target_object_id": r["target_object_id"],
        "edge_type": r["edge_type"],
        "evidence": json.loads(r["evidence"] or "[]"),
        "source_id": r["source_id"],
        "review_status": r["review_status"],
        "_src_type": r["src_type"] or "",
        "_tgt_type": r["tgt_type"] or "",
        "_src_name": node_names.get(r["source_object_id"], ""),
        "_tgt_name": node_names.get(r["target_object_id"], ""),
    } for r in rel_rows]

    corr_counts = corroboration_counts(rels, node_names)
    edge_centrality = repo._edge_centrality_map(notebook_id)

    items = []
    for rel in rels:
        rid = rel["id"]
        corr_score = corroboration_score_from_count(corr_counts.get(rid, 1))
        trust = compute_trust_score(rel, node_types, corr_score)
        ec = edge_centrality.get(rid, 0.0)
        items.append({
            "rel_id": rid,
            "notebook_id": notebook_id,
            "edge_type": rel["edge_type"],
            "source_object_id": rel["source_object_id"],
            "target_object_id": rel["target_object_id"],
            "source_name": rel["_src_name"],
            "target_name": rel["_tgt_name"],
            "source_type": rel["_src_type"],
            "target_type": rel["_tgt_type"],
            "trust_score": trust,
            "edge_centrality": ec,
            "review_priority": ec * (1.0 - trust),
            "review_status": rel["review_status"],
        })
    items.sort(key=lambda x: x["review_priority"], reverse=True)
    return items[:limit]


# ── fixtures ────────────────────────────────────────────────────────────────

def _seed_graph_with_isolated_objects(repo) -> tuple:
    """A connected KG plus objects that sit on NO relation at all.

    The isolated ones are the whole point of 批 A: they were read (payload and
    all) on every review_queue call and never consulted.
    """
    nb = repo.create_notebook(NotebookCreate(name="narrowing"))
    repo.store_kg(nb.id, None, [
        {"local_id": "C1", "object_type": "Claim",
         "payload": {"name": "Claim Alpha"}, "evidence": []},
        {"local_id": "C2", "object_type": "Concept",
         "payload": {"name": "Concept Beta"}, "evidence": []},
        {"local_id": "F1", "object_type": "Formula",
         "payload": {"name": "Formula Gamma"}, "evidence": []},
        {"local_id": "P1", "object_type": "Procedure",
         "payload": {"name": "Procedure Delta"}, "evidence": []},
        {"local_id": "C3", "object_type": "Concept",
         "payload": {"name": "Concept Epsilon"}, "evidence": []},
        # Two structurally identical, disconnected pairs — their edges get the
        # same centrality, hence the same review_priority, which is exactly the
        # tie case where nlargest and a stable sort could diverge.
        {"local_id": "T1", "object_type": "Claim",
         "payload": {"name": "Twin One"}, "evidence": []},
        {"local_id": "T2", "object_type": "Concept",
         "payload": {"name": "Twin Two"}, "evidence": []},
        {"local_id": "T3", "object_type": "Claim",
         "payload": {"name": "Twin Three"}, "evidence": []},
        {"local_id": "T4", "object_type": "Concept",
         "payload": {"name": "Twin Four"}, "evidence": []},
    ], [
        {"source_local_id": "C1", "target_local_id": "C2",
         "edge_type": "defines",
         "evidence": [{"file": "f1", "char_start": 0, "char_end": 10,
                       "line_start": 1, "line_end": 1,
                       "quote": "alpha defines beta"}]},
        {"source_local_id": "F1", "target_local_id": "P1",
         "edge_type": "used_in", "evidence": []},
        {"source_local_id": "C1", "target_local_id": "P1",
         "edge_type": "used_in", "evidence": []},
        {"source_local_id": "T1", "target_local_id": "T2",
         "edge_type": "defines", "evidence": []},
        {"source_local_id": "T3", "target_local_id": "T4",
         "edge_type": "defines", "evidence": []},
    ])
    isolated = [
        repo._test_insert_object(nb.id, "concept", {"name": f"Lonely {i}"})
        for i in range(4)
    ]
    return nb.id, isolated


def _endpoint_ids(repo, notebook_id) -> set:
    with repo._connect() as db:
        rows = db.execute(
            "SELECT source_object_id, target_object_id FROM knowledge_relations "
            "WHERE notebook_id=? AND review_status != 'rejected'",
            (notebook_id,),
        ).fetchall()
    return {r["source_object_id"] for r in rows} | {r["target_object_id"] for r in rows}


# ── 批 A: 等价性 ────────────────────────────────────────────────────────────

def test_review_queue_matches_pre_narrowing_oracle(repo):
    nb_id, isolated = _seed_graph_with_isolated_objects(repo)
    assert isolated, "fixture must contain objects that are on no relation"
    assert repo.review_queue(nb_id) == _old_review_queue(repo, nb_id)


def test_review_queue_matches_oracle_when_an_edge_is_rejected(repo):
    """Rejecting an edge can strand its endpoints — the narrowed object read
    must follow the same non-rejected predicate the relation read uses."""
    nb_id, _ = _seed_graph_with_isolated_objects(repo)
    victim = repo.review_queue(nb_id)[0]["rel_id"]
    repo.set_edge_review(nb_id, victim, "rejected")
    assert repo.review_queue(nb_id) == _old_review_queue(repo, nb_id)


# ── 批 A: 有界性护栏(变异 = 恢复全量读 ⇒ 本条报红) ──────────────────────

def test_review_queue_rows_reads_only_endpoint_objects(repo):
    nb_id, isolated = _seed_graph_with_isolated_objects(repo)
    endpoints = _endpoint_ids(repo, nb_id)
    with repo._connect() as db:
        _rel_rows, obj_rows = GovernanceStore.review_queue_rows(db, nb_id)

    got = {r["id"] for r in obj_rows}
    assert got == endpoints
    assert len(obj_rows) == len(endpoints), "endpoint objects must not be duplicated"
    # The isolated objects exist in the notebook and must NOT have been read.
    assert set(isolated) & got == set()
    with repo._connect() as db:
        total = db.execute(
            "SELECT COUNT(*) AS n FROM knowledge_objects WHERE notebook_id=?",
            (nb_id,),
        ).fetchone()["n"]
    assert len(obj_rows) < total, "read must be strictly narrower than the notebook"


def test_review_queue_rows_drops_evidence_column(repo):
    """The relation projection must carry the derived anchor signal, never the
    evidence body (that is the read the SQL pushdown exists to avoid)."""
    nb_id, _ = _seed_graph_with_isolated_objects(repo)
    with repo._connect() as db:
        rel_rows, _obj = GovernanceStore.review_queue_rows(db, nb_id)
    assert rel_rows
    for row in rel_rows:
        keys = set(row.keys())
        assert "evidence" not in keys
        assert "has_anchor" in keys


def _captured_endpoint_lookups() -> list:
    """Run ``review_queue_rows`` against a fake connection and return the
    endpoint-lookup statements it issued, whitespace-normalised.

    No real database: the fake answers the relation read with two endpoints so
    the endpoint lookup is actually issued, and records every statement.  Two
    SQL-text guards below read this — each pins a different property of the
    same statement, and neither may be relaxed to accommodate the other."""
    relation_rows = [{"source_object_id": "ko-a", "target_object_id": "ko-b"}]

    class _Cursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class _SqlCapture:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params=None):
            text = " ".join(str(sql).split())
            self.statements.append(text)
            return _Cursor(
                relation_rows if "FROM knowledge_relations" in text else []
            )

    capture = _SqlCapture()
    GovernanceStore.review_queue_rows(capture, "nb-shape")
    lookups = [
        sql for sql in capture.statements
        if "FROM knowledge_objects" in sql and " IN (" in sql
    ]
    assert lookups, f"expected an endpoint lookup, got: {capture.statements}"
    return lookups


def test_endpoint_lookup_sql_keeps_no_notebook_predicate():
    """The endpoint lookup must issue a BARE ``id IN (...)``.

    Without ``ANALYZE`` — which this repository never runs on production
    databases — SQLite plans ``WHERE notebook_id=? AND id IN (...)`` as
    ``idx_knowledge_objects_nb_* (notebook_id=?)``, walking every object of the
    notebook once per batch (measured on a 200k-row database: 0.138s →
    14.155s, a ×103 regression on the very read this narrowing exists to
    shrink).  A bare ``id IN (...)`` takes the primary key with or without
    statistics.  Same conclusion, recipe and guard style as
    ``query_store.notebook_source_ids`` / ``maintenance.chunk_texts_by_ids`` and
    ``test_canonical_relations.py::test_relation_support_rows_issues_row_value_in_not_or_chain``.

    This is pinned as SQL TEXT because a behaviour test cannot see it: at
    fixture scale SQLite picks the primary key either way, so EXPLAIN cannot
    tell the two spellings apart — which is precisely how the regression got
    past one round of review.  ``notebook_id`` must instead be projected, so
    the filter is still applied (see the equivalence tests above).
    """
    assert _review_endpoint_ids(
        [{"source_object_id": "ko-a", "target_object_id": "ko-b"}]
    ) == ["ko-a", "ko-b"]
    for sql in _captured_endpoint_lookups():
        assert "notebook_id = ?" not in sql and "notebook_id=?" not in sql, (
            "the endpoint lookup must NOT carry a notebook_id predicate — "
            f"it degrades to a per-batch notebook scan without ANALYZE: {sql}")
        assert "notebook_id" in sql.split("FROM")[0], (
            "notebook_id must still be PROJECTED so the caller can filter on "
            f"it: {sql}")


def test_endpoint_lookup_sql_projects_only_the_name():
    """R3 T-A1: the endpoint lookup must extract ``payload.name`` in SQL, never
    project the whole ``payload`` document.

    Pinned as SQL TEXT for the same reason as the predicate guard above: the
    equivalence oracles stay green either way (the narrowing is lossless), so
    only the query shape can witness that the narrowing is still in place.  The
    predicate ban above is NOT relaxed by this — both guards read the same
    statements, each asserting its own property."""
    for sql in _captured_endpoint_lookups():
        projection = sql.split("FROM")[0]
        assert "json_extract(payload, '$.name')" in projection, (
            "payload.name must be extracted in SQL (R3 T-A1): " f"{sql}")
        assert "payload" not in projection.replace(
            "json_extract(payload, '$.name')", ""
        ), (
            "the endpoint lookup must NOT also project the whole payload "
            f"document — that is the read this narrowing removes: {sql}")


def test_review_queue_rows_filters_cross_notebook_endpoints(repo):
    """The Python-side notebook_id comparison must actually be applied: an
    endpoint that lives in another notebook stays unread, exactly as the old
    SQL predicate made it."""
    nb_id, _ = _seed_graph_with_isolated_objects(repo)
    other = repo.create_notebook(NotebookCreate(name="other"))
    stranger = repo._test_insert_object(other.id, "concept", {"name": "Stranger"})
    with repo._write() as db:
        db.execute(
            "UPDATE knowledge_relations SET target_object_id=? "
            "WHERE notebook_id=? AND id=(SELECT id FROM knowledge_relations "
            "WHERE notebook_id=? ORDER BY id LIMIT 1)",
            (stranger, nb_id, nb_id),
        )
    with repo._connect() as db:
        _rel, obj_rows = GovernanceStore.review_queue_rows(db, nb_id)
    assert stranger not in {r["id"] for r in obj_rows}
    assert repo.review_queue(nb_id) == _old_review_queue(repo, nb_id)


def test_review_queue_rows_paginates_endpoint_lookup(repo, monkeypatch):
    """More endpoints than one page must still come back exactly once each."""
    import app.repositories.sqlite.governance_store as gs

    monkeypatch.setattr(gs, "_REVIEW_ENDPOINT_LOOKUP_BATCH", 2)
    nb_id, _ = _seed_graph_with_isolated_objects(repo)
    endpoints = _endpoint_ids(repo, nb_id)
    assert len(endpoints) > 2, "fixture must span more than one page"
    with repo._connect() as db:
        _rel, obj_rows = GovernanceStore.review_queue_rows(db, nb_id)
    ids = [r["id"] for r in obj_rows]
    assert sorted(ids) == sorted(endpoints)
    assert len(ids) == len(set(ids))


# ── 批 A: 锚点信号下推逐位等价 ──────────────────────────────────────────

_ANCHOR_CASES = [
    ("[]", 0.0),                                    # 无 evidence
    ('[{"file": "f"}]', 0.0),                       # 有条目、无 quote 键
    ('[{"quote": ""}]', 0.0),                       # 空 quote
    ('[{"quote": "   "}]', 0.0),                    # 纯半角空格
    ('[{"quote": "\\n\\t"}]', 0.0),                 # ASCII 空白(裸 trim() 会判 1.0)
    ('[{"quote": "\\u3000"}]', 0.0),                # 全角空格(裸 trim() 会判 1.0)
    ('[{"quote": "\\u00a0"}]', 0.0),                # NBSP
    ('[{"quote": "x"}]', 1.0),                      # 正常 quote
    ('[{"quote": "\\u3000x\\u3000"}]', 1.0),        # 两端全角空格包着实内容
    ('[{"quote": ""}, {"quote": "y"}]', 1.0),       # 第二条命中
    ('["not an object"]', 0.0),                     # 非 dict 条目
    ('[123]', 0.0),                                 # 非 dict 标量条目
    ('{"quote": "x"}', 0.0),                        # 顶层不是数组
    ('null', 0.0),                                  # JSON null
    ("", 0.0),                                      # 空串(走 `or "[]"` 分支)
]

# 旧路径在这些形状上 **抛异常**(整个 review_queue 500),不存在可保真的响应:
# 畸形 TEXT 让服务层的 json.loads 炸,非字符串 quote 让 `.strip()` 炸。
# 下推版按 0.0 计分并正常返回——登记为健壮性修复,不是行为回退。
_ANCHOR_CASES_OLD_PATH_RAISED = [
    "not json at all",
    '[{"quote": null}]',
    '[{"quote": 5}]',
]


@pytest.mark.parametrize("raw_evidence,expected", _ANCHOR_CASES)
def test_anchor_pushdown_matches_python_evidence_anchor_score(
    repo, raw_evidence, expected
):
    """SQL `has_anchor` must equal `evidence_anchor_score` for every evidence
    shape a live column can hold — including the whitespace forms that separate
    `str.strip()` from a bare SQL `trim()`."""
    from app.services.kg.edge_trust import evidence_anchor_score

    nb_id, _ = _seed_graph_with_isolated_objects(repo)
    with repo._write() as db:
        db.execute("UPDATE knowledge_relations SET evidence=? WHERE notebook_id=?",
                   (raw_evidence, nb_id))
    with repo._connect() as db:
        rel_rows, _obj = GovernanceStore.review_queue_rows(db, nb_id)

    # The Python function is the authority; `expected` documents intent and
    # would catch a change to the function itself.
    python_score = evidence_anchor_score({"evidence": raw_evidence})
    assert python_score == expected
    assert rel_rows
    for row in rel_rows:
        assert (1.0 if row["has_anchor"] else 0.0) == python_score


@pytest.mark.parametrize("raw_evidence,_expected", _ANCHOR_CASES)
def test_review_queue_trust_scores_match_oracle_for_every_evidence_shape(
    repo, raw_evidence, _expected
):
    nb_id, _ = _seed_graph_with_isolated_objects(repo)
    with repo._write() as db:
        db.execute("UPDATE knowledge_relations SET evidence=? WHERE notebook_id=?",
                   (raw_evidence, nb_id))
    assert repo.review_queue(nb_id) == _old_review_queue(repo, nb_id)


@pytest.mark.parametrize("raw_evidence", _ANCHOR_CASES_OLD_PATH_RAISED)
def test_anchor_pushdown_survives_shapes_that_crashed_the_old_read(
    repo, raw_evidence
):
    """Store level only: `_edge_centrality_map` still parses evidence in Python
    (untouched by this batch), so malformed TEXT can still fail the endpoint
    further downstream — the point here is that the anchor read itself no longer
    contributes a failure mode and scores such rows 0.0."""
    nb_id, _ = _seed_graph_with_isolated_objects(repo)
    with repo._write() as db:
        db.execute("UPDATE knowledge_relations SET evidence=? WHERE notebook_id=?",
                   (raw_evidence, nb_id))
    with pytest.raises(Exception):
        _old_review_queue(repo, nb_id)
    with repo._connect() as db:
        rel_rows, _obj = GovernanceStore.review_queue_rows(db, nb_id)
    assert rel_rows
    assert all(not row["has_anchor"] for row in rel_rows)


# ── R3 T-A1: payload.name 的 SQL 提取 ────────────────────────────────────

def test_endpoint_name_extraction_matches_the_payload_read(repo):
    """The names the queue scores with must be exactly what parsing the whole
    payload used to yield — for the ordinary string case, that is the oracle
    comparison above; here we also pin that the projection really carries the
    name (a store-level read that a NULL-everywhere regression would break
    while every equivalence oracle stayed green, since both sides would then
    score ``""``)."""
    nb_id, _ = _seed_graph_with_isolated_objects(repo)
    with repo._connect() as db:
        _rel, obj_rows = GovernanceStore.review_queue_rows(db, nb_id)
        payloads = {
            r["id"]: json.loads(r["payload"] or "{}")
            for r in db.execute(
                "SELECT id, payload FROM knowledge_objects WHERE notebook_id=?",
                (nb_id,),
            ).fetchall()
        }
    assert obj_rows
    assert {r["id"]: r["name"] for r in obj_rows} == {
        r["id"]: payloads[r["id"]]["name"] for r in obj_rows
    }
    assert all(r["name"] for r in obj_rows), "fixture names must be non-empty"


def test_review_queue_scores_a_non_string_name_that_crashed_the_old_read(repo):
    """Registered robustness change (R3 T-A1, same class as the anchor
    pushdown's): a numeric ``payload.name`` reached ``edge_trust._norm`` as an
    ``int`` and raised — a 500 for the whole queue.  It now participates in
    scoring as its TEXT form, identical to what PostgreSQL's ``->>`` renders."""
    nb_id, _ = _seed_graph_with_isolated_objects(repo)
    with repo._write() as db:
        db.execute(
            'UPDATE knowledge_objects SET payload=\'{"name": 42}\' '
            "WHERE notebook_id=?",
            (nb_id,),
        )
    with pytest.raises(AttributeError):
        _old_review_queue(repo, nb_id)
    items = repo.review_queue(nb_id)
    assert items
    assert {i["source_name"] for i in items} == {"42"}
    assert {i["target_name"] for i in items} == {"42"}


# ── 批 A: nlargest 与稳定排序切片逐位等价 ───────────────────────────────

@pytest.mark.parametrize("limit", [0, 1, 2, 3, 5, 200, 10_000, -1, -2])
def test_review_queue_limit_matches_stable_sort_slice(repo, limit):
    nb_id, _ = _seed_graph_with_isolated_objects(repo)
    assert repo.review_queue(nb_id, limit=limit) == _old_review_queue(
        repo, nb_id, limit=limit
    )


def test_review_queue_limit_slice_exercises_priority_ties(repo):
    """The equivalence above is only meaningful if ties actually occur — a tie
    is where `nlargest` and a stable sort could diverge."""
    nb_id, _ = _seed_graph_with_isolated_objects(repo)
    priorities = [i["review_priority"] for i in repo.review_queue(nb_id)]
    assert len(priorities) > len(set(priorities)), "fixture must produce ties"


# ── 批 B: promotion 命中后补读 evidence 与全量读逐位一致 ─────────────────

def _set_evidence(repo, object_id, evidence):
    with repo._write() as db:
        db.execute("UPDATE knowledge_objects SET evidence=? WHERE id=?",
                   (json.dumps(evidence, ensure_ascii=False), object_id))


def _evidence_of(repo, object_id):
    with repo._connect() as db:
        row = db.execute("SELECT evidence FROM knowledge_objects WHERE id=?",
                         (object_id,)).fetchone()
    return json.loads(row["evidence"] or "[]")


def test_promotion_merge_uses_matched_row_evidence(repo):
    """Dedup corpus rows no longer carry evidence; the merge must still read the
    MATCHED row's evidence (not a neighbour's, not an empty list)."""
    from app.repositories.sqlite.governance_store import merge_evidence_lists

    nb = repo.create_notebook(NotebookCreate(name="personal"))
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    repo.replace_notebook_bases(nb.id, [base.id], "user-local")

    # Two same-type base objects: a decoy that must NOT be merged into, and the
    # real match. Distinct evidence tells them apart in the assertion.
    decoy = repo._test_insert_object(base.id, "claim", {"name": "Unrelated claim."})
    _set_evidence(repo, decoy, [{"source_id": "s-decoy", "element_id": "e-decoy",
                                 "quoted_span": "decoy"}])
    existing = repo._test_insert_object(
        base.id, "claim", {"name": "Cascode raises output resistance."})
    base_evidence = [
        {"source_id": "s-base", "element_id": "e-base", "quoted_span": "base span"},
        {"source_id": "s-both", "element_id": "e-both", "quoted_span": "shared"},
    ]
    _set_evidence(repo, existing, base_evidence)

    oid = repo._test_insert_object(
        nb.id, "claim", {"name": "cascode raises output resistance",
                         "section_path": "1"})
    src_evidence = [
        {"source_id": "s-both", "element_id": "e-both", "quoted_span": "shared"},
        {"source_id": "s-src", "element_id": "e-src", "quoted_span": "src span"},
    ]
    _set_evidence(repo, oid, src_evidence)

    cand = repo.propose_promotion(nb.id, oid)
    result = repo.approve_promotion(cand["id"])

    assert result["base_object_id"] == existing
    assert _evidence_of(repo, existing) == merge_evidence_lists(
        base_evidence, src_evidence
    )
    # The decoy row was in the (now thinner) corpus read and must be untouched.
    assert _evidence_of(repo, decoy) == [
        {"source_id": "s-decoy", "element_id": "e-decoy", "quoted_span": "decoy"}
    ]


def test_base_dedup_evidence_reads_the_requested_row(repo):
    from app.repositories.sqlite.governance_store import base_dedup_evidence

    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    a = repo._test_insert_object(base.id, "claim", {"name": "A"})
    b = repo._test_insert_object(base.id, "claim", {"name": "B"})
    _set_evidence(repo, a, [{"source_id": "sa"}])
    _set_evidence(repo, b, [{"source_id": "sb"}])
    with repo._connect() as db:
        assert base_dedup_evidence(db, a) == [{"source_id": "sa"}]
        assert base_dedup_evidence(db, b) == [{"source_id": "sb"}]
        assert base_dedup_evidence(db, "ko-missing") == []
