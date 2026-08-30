import json

import pytest
from app.services.retrieval import keyword_score
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository
    from app.services.embedding import FakeEmbedder
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def test_kg_object_candidates_core_and_delta(repo, monkeypatch):
    """opt-in delta brute-force: with scale_search_include_delta=True, a KG
    object added AFTER the watermark is still recalled via the delta matmul
    path. (Default is now OFF — see test_indexed_only_principle.py — so this
    test explicitly enables the opt-in to exercise the brute-force branch.)"""
    import json
    from app.models.schemas import NotebookCreate
    monkeypatch.setattr(repo.settings, "scale_search_include_delta", True)
    nb = repo.create_notebook(NotebookCreate(name="base"))
    def add(sid, oid, name, day):
        with repo._write() as db:
            now = f"2026-07-{day:02d}T00:00:00"
            db.execute("INSERT OR IGNORE INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?)", (sid, nb.id, "t", "md", "ready", now, now))
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,"
                       "evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (oid, nb.id, "concept", "approved", "", json.dumps({"name": name}), "[]", sid, now, now))
            v = repo._runtime.models.embedding("retrieval_query_embedding").embed_texts([name])[0]
            db.execute("INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) "
                       "VALUES (?,?,?,?)", (oid, nb.id, json.dumps(v), now))
    add("s1", "o1", "current mirror", 1)
    add("s1", "o2", "bandgap reference", 1)
    repo.rebuild_unified_kg(nb.id); repo.build_scale_index(nb.id)
    add("s2", "o3", "MOSFET amplifier", 2)   # build 后新增 = delta
    # id_filter
    with repo._connect() as db:
        objs = repo._knowledge_objects(db, nb.id, "concept", id_filter={"o1"})
    assert {o["id"] for o in objs} == {"o1"}
    # 候选:ANN 核(o1/o2)⊕ delta(o3)
    idx = repo._scale_index(nb.id, allow_stale=True)
    cand = repo._kg_object_candidates(nb.id, repo._embed_query("MOSFET amplifier"), idx, recall=10)
    assert "o3" in cand                        # delta 对象在候选
    assert set(cand.keys()) & {"o1", "o2"}     # ANN 核也在候选
    assert all(0.0 <= s <= 1.0 for s in cand.values())


def test_retrieve_scored_bounded_when_indexed(repo, monkeypatch):
    """opt-in delta brute-force: with scale_search_include_delta=True, a
    delta KG object is still recalled and the candidate set stays bounded.
    (Default is now OFF — see test_indexed_only_principle.py.)"""
    import json
    from app.models.schemas import NotebookCreate
    monkeypatch.setattr(repo.settings, "scale_search_include_delta", True)
    nb = repo.create_notebook(NotebookCreate(name="base"))
    def add(sid, oid, name, day):
        with repo._write() as db:
            now = f"2026-07-{day:02d}T00:00:00"
            db.execute("INSERT OR IGNORE INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?)", (sid, nb.id, "t", "md", "ready", now, now))
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,"
                       "evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (oid, nb.id, "concept", "approved", "", json.dumps({"name": name}), "[]", sid, now, now))
            v = repo._runtime.models.embedding("retrieval_query_embedding").embed_texts([name])[0]
            db.execute("INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) "
                       "VALUES (?,?,?,?)", (oid, nb.id, json.dumps(v), now))
    for i in range(6):
        add("s1", f"o{i}", f"concept {i}", 1)
    repo.rebuild_unified_kg(nb.id); repo.build_scale_index(nb.id)
    add("s2", "odelta", "delta concept special", 2)
    monkeypatch.setattr(repo.settings, "chunk_recall", 3)
    out = repo._retrieve_scored(nb.id, "delta concept special")
    ids = {o.object_id for o in out}
    assert "odelta" in ids                # delta 对象被召回
    assert len(ids) <= 3 + 1              # 有界:≤ recall(3)核 + delta(1),远小于 7 全量


def test_retrieve_scored_unions_lexical_candidates_with_kg_ann(repo, monkeypatch):
    """A lexical-only exact name outside the mocked ANN window remains recallable."""
    import json
    from app.models.schemas import NotebookCreate

    nb = repo.create_notebook(NotebookCreate(name="base"))
    repo.store_kg(nb.id, None, [
        {
            "local_id": "decoy",
            "object_type": "concept",
            "payload": {"name": "semantic decoy", "section_path": ""},
            "evidence": [],
        },
        {
            "local_id": "lexical",
            "object_type": "concept",
            "payload": {"name": "ZXCV9000 timing controller", "section_path": ""},
            "evidence": [],
        },
    ], [])
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    with repo._connect() as db:
        rows = db.execute(
            "SELECT id, payload FROM knowledge_objects WHERE notebook_id=?", (nb.id,)
        ).fetchall()
    ids_by_name = {json.loads(row["payload"])["name"]: row["id"] for row in rows}
    decoy_id = ids_by_name["semantic decoy"]
    lexical_id = ids_by_name["ZXCV9000 timing controller"]

    monkeypatch.setattr(repo.settings, "chunk_recall", 1)
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "_kg_object_candidates",
        lambda *_args, **_kwargs: {decoy_id: 0.9},
    )

    hits = repo._retrieve_scored(nb.id, "ZXCV9000 timing controller")
    assert lexical_id in {hit.object_id for hit in hits}


def test_source_scoped_kg_candidates_do_not_starve_or_hydrate_third_source(
    repo, monkeypatch
):
    """C may own the global top-K, but cannot consume a selected A+B window."""
    import json
    from app.models.schemas import NotebookCreate

    nb = repo.create_notebook(NotebookCreate(name="manuals"))
    now = "2026-07-01T00:00:00"
    rows = [
        ("ko-c1", "C", "target command target command target command"),
        ("ko-c2", "C", "target command target command"),
        ("ko-a", "A", "target command in manual A"),
        ("ko-b", "B", "target command in manual B"),
    ]
    with repo._write() as db:
        for source_id in ("A", "B", "C"):
            db.execute(
                "INSERT INTO sources "
                "(id,notebook_id,title,source_type,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (source_id, nb.id, f"Manual {source_id}", "md", "ready", now, now),
            )
        for object_id, source_id, name in rows:
            evidence = json.dumps([{
                "source_id": source_id,
                "source_title": f"Manual {source_id}",
                "element_id": f"el-{source_id}",
                "element_type": "paragraph",
                "location_label": "Commands",
                "quoted_span": name,
                "confidence": 1.0,
            }])
            db.execute(
                "INSERT INTO knowledge_objects "
                "(id,notebook_id,object_type,status,owner,payload,evidence,"
                "source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (object_id, nb.id, "concept", "approved", "",
                 json.dumps({"name": name}), evidence, source_id, now, now),
            )
            db.execute(
                "INSERT INTO kg_objects_fts(object_id,notebook_id,name) VALUES (?,?,?)",
                (object_id, nb.id, name),
            )
            db.execute(
                "INSERT INTO knowledge_object_sources "
                "(object_id,source_id,notebook_id) VALUES (?,?,?)",
                (object_id, source_id, nb.id),
            )
        repo._mark_source_index_backfilled(db, nb.id)

    monkeypatch.setattr(repo.settings, "chunk_recall", 2)
    original = repo.retrieval.candidates._knowledge_objects
    hydrated_ids = set()

    def observe_hydration(database, notebook_id, object_type, **kwargs):
        ids = set(kwargs.get("id_filter") or ())
        hydrated_ids.update(ids)
        assert not ids & {"ko-c1", "ko-c2"}
        return original(database, notebook_id, object_type, **kwargs)

    monkeypatch.setattr(
        repo.retrieval.candidates, "_knowledge_objects", observe_hydration
    )
    hits = repo.retrieval.retrieve_scored(
        nb.id, "target command", allowed_source_ids=("A", "B")
    )

    assert {hit.object_id for hit in hits} == {"ko-a", "ko-b"}
    assert hydrated_ids == {"ko-a", "ko-b"}
    assert all(
        evidence.source_id in {"A", "B"}
        for hit in hits for evidence in hit.evidence
    )


def test_source_scoped_kg_uses_evidence_fallback_without_reverse_index(repo):
    """Unknown legacy marker reads authoritative evidence before LIMIT."""
    from app.models.schemas import NotebookCreate

    nb = repo.create_notebook(NotebookCreate(name="legacy"))

    def evidence(source_id):
        return [{
            "source_id": source_id,
            "source_title": source_id,
            "element_id": f"el-{source_id}",
            "element_type": "paragraph",
            "location_label": "Commands",
            "quoted_span": "target command",
            "confidence": 1.0,
        }]

    repo.store_kg(nb.id, None, [
        {
            "local_id": "allowed",
            "object_type": "concept",
            "payload": {"name": "target command allowed"},
            "evidence": evidence("A"),
        },
        {
            "local_id": "blocked",
            "object_type": "concept",
            "payload": {"name": "target command blocked"},
            "evidence": evidence("C"),
        },
    ], [])
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,"
            "created_at,updated_at) VALUES (?,?, 'concept','approved','',?,?,'A',?,?)",
            (
                "malformed-evidence",
                nb.id,
                '{"name":"target command malformed"}',
                "not-json",
                "2026-08-25T00:00:00",
                "2026-08-25T00:00:00",
            ),
        )
        db.execute(
            "INSERT INTO kg_objects_fts(object_id,notebook_id,name) VALUES (?,?,?)",
            ("malformed-evidence", nb.id, "target command malformed"),
        )
        allowed_id = db.execute(
            "SELECT id FROM knowledge_objects "
            "WHERE notebook_id=? AND json_extract(payload,'$.name')=?",
            (nb.id, "target command allowed"),
        ).fetchone()["id"]
        db.execute(
            "UPDATE knowledge_objects SET evidence=? WHERE id=?",
            (json.dumps([evidence("A")[0], "legacy scalar"]), allowed_id),
        )
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,"
            "created_at,updated_at) VALUES (?,?, 'concept','approved','',?,?,'A',?,?)",
            (
                "scalar-array-evidence",
                nb.id,
                '{"name":"target command scalar"}',
                '["legacy scalar"]',
                "2026-08-25T00:00:00",
                "2026-08-25T00:00:00",
            ),
        )
        db.execute(
            "INSERT INTO kg_objects_fts(object_id,notebook_id,name) VALUES (?,?,?)",
            ("scalar-array-evidence", nb.id, "target command scalar"),
        )
        db.execute(
            "DELETE FROM knowledge_object_sources WHERE notebook_id=?", (nb.id,)
        )
        db.execute(
            "UPDATE unified_kg_state SET source_index_backfilled=0 "
            "WHERE notebook_id=?",
            (nb.id,),
        )

    events = []
    repo.event_log.emit = events.append
    hits = repo.retrieval.retrieve_scored(
        nb.id,
        "target command\n\n检索必须服从以下已确认问题契约：\n"
        + "与候选节点无关的固定模板" * 100,
        allowed_source_ids=("A",),
    )
    assert [hit.payload["name"] for hit in hits] == ["target command allowed"]
    fallback = next(
        event for event in events if event.get("kind") == "source_index_fallback"
    )
    assert fallback["candidate_count"] == 1
    # The interactive fallback is read-only; explicit maintenance remains the
    # only way to certify an unknown historical notebook as fully indexed.
    with repo._connect() as db:
        assert not repo._source_index_backfilled(db, nb.id)


def test_all_selected_ceiling_keeps_ann_when_reverse_index_is_unknown(repo):
    """A frozen default scope is a result ceiling, not a selected-source lane."""
    from app.models.schemas import NotebookCreate
    from app.models.source_scope import SourceScope
    from app.services.source_scope import source_scope_context

    nb = repo.create_notebook(NotebookCreate(name="all selected"))
    now = "2026-08-25T00:00:00"
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("A", nb.id, "paper", "pdf", "ready", now, now),
        )
    evidence = [{
        "source_id": "A",
        "source_title": "paper",
        "element_id": "el-a",
        "element_type": "paragraph",
        "location_label": "Abstract",
        "quoted_span": "Cosmos 3 is an omnimodal world model",
        "confidence": 1.0,
    }]
    repo.store_kg(nb.id, "A", [{
        "local_id": "cosmos",
        "object_type": "concept",
        "payload": {"name": "Cosmos 3"},
        "evidence": evidence,
    }], [])
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    with repo._write() as db:
        # Simulate a pre-fix notebook: the maintained rows may be complete, but
        # the historical completion certificate was never latched.
        db.execute(
            "UPDATE unified_kg_state SET source_index_backfilled=0 "
            "WHERE notebook_id=?",
            (nb.id,),
        )

    events = []
    repo.event_log.emit = events.append
    with source_scope_context(
        nb.id, SourceScope(mode="include", source_ids=["A"], narrowed=False)
    ):
        hits = repo.retrieval.candidates.federated_retrieve(nb.id, "cosmos3")

    assert [hit.payload["name"] for hit in hits] == ["Cosmos 3"]
    stage = next(event for event in events if event.get("kind") == "ask_stage")
    assert stage["source_candidates_restricted"] is False
    assert stage["ann_gated"] is True
    assert not any(
        event.get("kind") == "source_index_fallback" for event in events
    )


def test_keyword_coverage_normalizes_compact_model_name_spacing():
    assert keyword_score("cosmos3", "Cosmos 3 is an omnimodal model") == 1.0
    assert keyword_score("Cosmos 3", "cosmos3 is an omnimodal model") == 1.0


def test_model_name_alias_does_not_rewrite_numbered_prose_labels():
    assert keyword_score("chapter 4", "chapter overview") == 1.0
    assert keyword_score(
        "Python version 3 compatibility",
        "Python version compatibility",
    ) == 1.0


def test_model_name_alias_keeps_haystack_stems_for_bare_queries():
    """Aliasing is append-only on the haystack side (PR #601 review P1).

    _tokens runs on BOTH sides of every comparison; dropping the component
    runs there made a document saying "Cosmos 3" invisible to a bare-stem
    query, and stripped ordinary prose verbs ("scored 3 points" lost
    "scored").  Only keyword_basis — the query-side entry — may unit-ify."""
    from app.services.retrieval import _tokens

    assert keyword_score("cosmos", "Cosmos 3 is an omnimodal model") == 1.0
    # Ordinary prose keeps its stem even when a number happens to follow it.
    assert keyword_score("supports languages", "supports 4 languages") == 1.0
    assert keyword_score(
        "team points", "the team scored 3 points yesterday"
    ) == 1.0
    # The haystack token list is a superset: stems stay, the alias is added.
    doc_tokens = set(_tokens("Cosmos 3 is an omnimodal model"))
    assert {"cosmos", "cosmos3"} <= doc_tokens


def test_word_number_query_keeps_component_credit():
    """codex #601 R1 P2: the alias is an OR-arm, never a replacement.

    A word-plus-digit query phrase ("priority 1") must still fully match a
    document that spells it with punctuation the alias regex does not join
    ("priority: 1") — coverage is max(alias hit, component fraction)."""
    assert keyword_score("priority 1", "priority: 1 blocker") == 1.0
    # Cross-spelling still reaches full credit through the alias arm.
    assert keyword_score("priority 1", "priority1 blocker") == 1.0


def test_contract_remains_in_recall_but_not_keyword_basis(repo, monkeypatch):
    from app.models.schemas import NotebookCreate

    nb = repo.create_notebook(NotebookCreate(name="contract"))
    evidence = [{
        "source_id": "A", "source_title": "paper", "element_id": "el-a",
        "element_type": "paragraph", "location_label": "p1",
        "quoted_span": "target command", "confidence": 1.0,
    }]
    repo.store_kg(nb.id, None, [{
        "local_id": "target", "object_type": "concept",
        "payload": {"name": "target command"}, "evidence": evidence,
    }], [])
    with repo._connect() as db:
        object_id = db.execute(
            "SELECT id FROM knowledge_objects WHERE notebook_id=?", (nb.id,)
        ).fetchone()["id"]

    contract_query = (
        "target command\n\n检索必须服从以下已确认问题契约：\n"
        "排除其他产品；用户确认只讨论部署约束"
    )
    embedded = []
    lexical = []
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "_embed_query",
        lambda query: embedded.append(query) or None,
    )

    def lexical_hits(_db, _notebook_id, query, _recall, **_kwargs):
        lexical.append(query)
        return [{"object_id": object_id, "name": "target command", "score": 1.0}]

    monkeypatch.setattr(
        repo.retrieval.candidates, "_lexical_object_hits", lexical_hits
    )
    hits = repo.retrieval.retrieve_scored(
        nb.id, contract_query, allowed_source_ids=("A",)
    )

    assert embedded == [contract_query]
    assert lexical == [contract_query]
    assert [hit.payload["name"] for hit in hits] == ["target command"]


def test_source_scoped_kg_recalls_compact_model_name_against_spaced_node(repo):
    from app.models.schemas import NotebookCreate

    nb = repo.create_notebook(NotebookCreate(name="models"))
    evidence = [{
        "source_id": "A",
        "source_title": "paper",
        "element_id": "el-a",
        "element_type": "paragraph",
        "location_label": "Abstract",
        "quoted_span": "Cosmos 3 is an omnimodal world model",
        "confidence": 1.0,
    }]
    repo.store_kg(nb.id, None, [{
        "local_id": "cosmos",
        "object_type": "concept",
        "payload": {"name": "Cosmos 3"},
        "evidence": evidence,
    }], [])

    hits = repo.retrieval.retrieve_scored(
        nb.id, "cosmos3", allowed_source_ids=("A",)
    )
    assert [hit.payload["name"] for hit in hits] == ["Cosmos 3"]


def test_source_scoped_chunk_fts_filters_before_limit(repo):
    from app.models.schemas import NotebookCreate

    nb = repo.create_notebook(NotebookCreate(name="large manuals"))
    now = "2026-07-01T00:00:00"
    chunks = [
        ("chunk-c1", "C", "target command target command target command"),
        ("chunk-c2", "C", "target command target command"),
        ("chunk-a", "A", "target command in manual A"),
        ("chunk-b", "B", "target command in manual B"),
    ]
    with repo._write() as db:
        for source_id in ("A", "B", "C"):
            db.execute(
                "INSERT INTO sources "
                "(id,notebook_id,title,source_type,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (source_id, nb.id, f"Manual {source_id}", "md", "ready", now, now),
            )
        for chunk_id, source_id, text in chunks:
            db.execute(
                "INSERT INTO chunks "
                "(id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (chunk_id, nb.id, source_id, text, "Commands", "[]", now),
            )
            db.execute(
                "INSERT INTO chunks_fts(chunk_id,notebook_id,text) VALUES (?,?,?)",
                (chunk_id, nb.id, text),
            )

    with repo._connect() as db:
        hits = repo._runtime.knowledge.chunk_fts_search(
            db, nb.id, "target command", k=2,
            allowed_source_ids=("A", "B"),
        )
    assert {hit["chunk_id"] for hit in hits} == {"chunk-a", "chunk-b"}


def test_copyable_selected_element_search_routes_scope_into_bounded_chunks(
    repo, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "notebook_copy_stats",
        lambda _notebook_id: {"copyable": True},
    )
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "_gather_elements",
        lambda *_args, **_kwargs: pytest.fail(
            "selected source search must not materialize every element/vector"
        ),
    )

    def scoped_chunks(notebook_id, query, recall=0, *, allowed_source_ids=None):
        calls.append((notebook_id, query, recall, tuple(allowed_source_ids or ())))
        return [], [], None

    monkeypatch.setattr(repo.retrieval.candidates, "_retrieve_chunks", scoped_chunks)
    assert repo.retrieval.retrieve_elements(
        "nb", "target command", allowed_source_ids=("A", "B")
    ) == []
    assert calls == [(
        "nb", "target command", repo.settings.chunk_recall, ("A", "B")
    )]


def test_copyable_selected_chunk_search_always_uses_bounded_fts(
    repo, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "notebook_copy_stats",
        lambda _notebook_id: {"copyable": True},
    )
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "_gather_chunks",
        lambda *_args, **_kwargs: pytest.fail(
            "selected source search must not materialize every chunk/vector"
        ),
    )
    monkeypatch.setattr(
        repo.retrieval.candidates.chunks,
        "count_row",
        lambda *_args, **_kwargs: pytest.fail(
            "selected source search must not scan the notebook for a count"
        ),
    )

    def bounded(notebook_id, query, query_vector, recall, n_chunks, *,
                allowed_source_ids=None, source_restricted=False):
        calls.append((
            notebook_id, query, recall, n_chunks,
            tuple(allowed_source_ids or ()), source_restricted,
        ))
        return [], [], None

    monkeypatch.setattr(
        repo.retrieval.candidates, "_retrieve_chunks_fts_degraded", bounded
    )
    assert repo.retrieval.candidates._retrieve_chunks(
        "nb", "target command", recall=7, allowed_source_ids=("A", "B")
    ) == ([], [], None)
    # codex #640 R2 P1:一个非 None 的 allowed_source_ids 本身不再是「真收窄」的
    # 证明——``_retrieve_elements`` 把它自己物化出的**上下文天花板**用完全相同的
    # 参数形状传给这同一个方法,所以只看「非 None」推 explicit 会让全选冻结的
    # 元素回退臂重新关闭语料语言闸(见该方法与 ``_lexical_gate_source_scoped``
    # 的说明)。不带 ``producer_explicit=True``、也没有真收窄的 request scope 时,
    # 语料语言闸保持打开(source_restricted=False)。
    assert calls == [("nb", "target command", 7, -1, ("A", "B"), False)]

    # 对照:调用方显式 attest ``producer_explicit=True``(真正的 producer 级
    # 收窄清单)时,豁免照旧生效——这条腿没有回归。
    calls.clear()
    assert repo.retrieval.candidates._retrieve_chunks(
        "nb", "target command", recall=7, allowed_source_ids=("A", "B"),
        producer_explicit=True,
    ) == ([], [], None)
    assert calls == [("nb", "target command", 7, -1, ("A", "B"), True)]


def test_keyword_score_ignores_stopwords():
    # Verbose phrasing must not dilute the score: only content tokens count.
    # Basis after dropping stopwords (what/is/and/are/its) -> {engram, problems};
    # "problems" is a genuine content word absent from the short KG name, so it
    # remains in the denominator. The point is the score is no longer crushed by
    # the function words (raw token basis would be 8 -> 0.125).
    concise = keyword_score("engram", "Engram is a memory module")
    verbose = keyword_score("what is engram and what are its problems", "Engram is a memory module")
    assert concise == 1.0
    # Without stopword filtering this would be 1/8 = 0.125; with filtering the
    # basis is the 2 content tokens (engram hits) -> 0.5.
    assert verbose == 0.5


def test_quoted_phrase_is_one_indivisible_unit_of_the_keyword_basis():
    """引号内不参与分词:整段命中才算命中,散落的词一分不给。

    这条是「保证完整性」在**排序**上的兑现。候选生成把含整句短语的文档捞进来了,
    但如果打分仍按 static/timing/analysis 三个独立词算覆盖率,散落着这三个词的
    文档和真正含该短语的文档同分——引号就只影响召回、不影响谁排在前面。
    """
    exact = keyword_score('"static timing analysis" 原理',
                          "we run static timing analysis 原理 like this")
    scattered = keyword_score('"static timing analysis" 原理',
                              "timing is static; the analysis 原理 follows")
    assert exact == 1.0
    # 基是 {短语, 原理} 两项;散落文档只覆盖到 原理,短语项一分不给。
    assert scattered == 0.5


def test_quoted_phrase_matching_is_case_and_whitespace_insensitive():
    assert keyword_score('"Static Timing  Analysis"',
                         "STATIC   timing\nanalysis appears here") == 1.0


def test_keyword_score_without_quotes_is_unchanged():
    # 无引号查询必须与本特性之前逐位一致(基就是内容 token 集合)。
    assert keyword_score("engram module", "Engram is a memory module") == 1.0
    assert keyword_score("engram absent", "Engram is a memory module") == 0.5


def test_honor_quotes_false_reads_quotes_as_ordinary_words():
    # 治理侧比较的是两段**已存文本**,不是用户查询:文档里引用了别人的话,
    # 不等于它在声明检索约束。
    assert keyword_score('a "static timing analysis" b',
                         "timing is static; the analysis follows",
                         honor_quotes=False) == 1.0


def test_bm25_ranks_a_quoted_phrase_as_one_atomic_term():
    """RRF 路径的排序来自 BM25,所以引号必须在 BM25 里也生效。

    codex #410 round-1 P2:`RETRIEVAL_RRF_ENABLED=true` 时,phrase-aware 的
    relevance 只是随行元数据,真正决定次序的是 BM25;若 BM25 仍按拆开的词打分,
    散落着这三个词的文档会和含整句短语的文档一起排进来——那条路径上引号就等于
    完全没生效。这条同时钉住空白归一:文档里跨换行/多空格的短语仍算命中。
    """
    from app.services.retrieval import bm25_scores

    docs = [
        ("phrase", "we run static timing analysis on the design"),
        ("scattered", "timing is static and the analysis follows separately"),
        ("split", "we run static   timing\nanalysis here"),
    ]
    quoted = bm25_scores('"static timing analysis"', docs)
    assert "scattered" not in quoted, "散落的词不构成短语命中"
    assert set(quoted) == {"phrase", "split"}
    # 去掉引号就回到按词打分,散落文档照常进榜——对照组证明差异确实来自引号。
    assert "scattered" in bm25_scores("static timing analysis", docs)


def test_probe_basis_treats_every_probed_name_as_atomic():
    """通道按「自己探测的名称」打分,每个名称都是整体——不看它长什么样。

    codex #410 round-3 起于多词短语被拆开(散落着 static/timing/analysis 的同节
    兄弟块拿到满分覆盖率);round-8 指出「按有没有空格判断是否原子」是形状猜测:
    `config.yaml`、`静态时序分析` 都不含空格,照样被降级回 token。原子性是**探测
    的性质**——这些名称本来就是以字面子串命中的,覆盖率只有一个诚实答案:正文里
    有没有这个串。
    """
    from app.services.retrieval import probe_keyword_basis

    def covered(terms, text):
        return probe_keyword_basis(terms).coverage(frozenset(), text)

    assert covered(["static timing analysis"], "we run static timing analysis") == 1.0
    assert covered(["static timing analysis"], "timing is static; the analysis") == 0.0
    # 不含空格的两类:标点连接与 CJK —— 同样是整体。
    assert covered(["config.yaml"], "see config.yaml") == 1.0
    assert covered(["config.yaml"], "the config lists a yaml file") == 0.0
    assert covered(["静态时序分析"], "这里做静态时序分析") == 1.0
    assert covered(["静态时序分析"], "静态分析与时序检查") == 0.0
    # 多个名称:按覆盖了几个算。
    assert covered(["set_db", "report_timing"], "set_db 的参数表") == 0.5


def test_fuse_custom_weights_shift_balance():
    from app.services.retrieval import _fuse
    # 默认 0.4/0.6: 语义为 0 时融合分 = keyword * 0.4/(0.4+0.6) = 0.4
    assert abs(_fuse(1.0, 0.0, True) - 0.4) < 1e-9
    # keyword-heavy 0.7/0.3: 同输入下关键词权重更高
    assert abs(_fuse(1.0, 0.0, True, w_keyword=0.7, w_semantic=0.3) - 0.7) < 1e-9


def test_score_knowledge_passes_weights_through():
    from app.services.retrieval import score_knowledge
    objs = [{"id": "o1", "payload": {"name": "RTL synthesis flow"}, "evidence": []}]
    # 纯关键词(无向量)下,提高 w_keyword 不应改变 keyword-only 融合分(归一化抵消),
    # 但调用必须接受参数且不报错,返回命中。
    hits = score_knowledge("RTL synthesis", objs, "claim", w_keyword=0.7, w_semantic=0.3)
    assert hits and hits[0].object_id == "o1"
