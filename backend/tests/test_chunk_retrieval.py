import contextvars
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from app.services.retrieval import score_chunks, RetrievedChunk
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository, _now
from app.services.embedding import FakeEmbedder
from app.models.schemas import AskRequest, NotebookCreate
from tests.model_testkit import bind_all_embedding_clients
from tests.model_testkit import bind_chat_client


def _ck(cid, text):
    return {"chunk_id": cid, "source_id": "s1", "source_title": "Doc",
            "section_path": "1", "text": text, "element_ids": ["e1"]}


def test_score_chunks_keyword_only_filters_floor():
    chunks = [_ck("c1", "deepseek mixture of experts routing"),
              _ck("c2", "unrelated cooking recipe tomato")]
    out = score_chunks("deepseek experts routing", chunks, query_vector=None, chunk_sims=None, limit=10)
    ids = [c.chunk_id for c in out]
    assert "c1" in ids and "c2" not in ids      # c2 低于 RELEVANCE_FLOOR 被丢
    assert all(isinstance(c, RetrievedChunk) for c in out)
    assert out[0].relevance > 0 and out[0].object_id == out[0].chunk_id


def test_score_chunks_caps_to_limit_sorted():
    chunks = [_ck(f"c{i}", f"shared term token{i}") for i in range(20)]
    out = score_chunks("shared term", chunks, query_vector=None, chunk_sims=None, limit=5)
    assert len(out) == 5
    assert all(out[i].score >= out[i+1].score for i in range(len(out)-1))


def test_score_chunks_deduplicates_same_source_text_before_limit():
    chunks = [
        _ck("header-1", "Cosmos 3: Omnimodal World Models for Physical AI"),
        _ck("header-2", "  Cosmos 3: Omnimodal\nWorld Models for Physical AI "),
        _ck("abstract", "Cosmos 3 introduces an omnimodal architecture."),
    ]

    out = score_chunks("Cosmos 3 omnimodal", chunks, limit=2)

    assert len(out) == 2
    assert sum(chunk.chunk_id.startswith("header-") for chunk in out) == 1
    assert "abstract" in {chunk.chunk_id for chunk in out}


def test_score_chunks_uses_semantic_sims():
    chunks = [_ck("c1", "no keyword overlap here")]
    # 仅语义信号(关键词 0): chunk_sims 给高余弦 → 仍能过 floor。
    out = score_chunks("totally different words", chunks,
                       query_vector=[0.1]*4, chunk_sims={"c1": 0.9}, limit=10)
    assert [c.chunk_id for c in out] == ["c1"]
    assert out[0].relevance >= 0.5


def test_score_chunks_lexical_only_candidate_is_keyword_renormalized():
    chunks = [_ck("lexical", "ZXCV9000 timing controller")]
    keyword_only = score_chunks(
        "ZXCV9000 controller", chunks, query_vector=None, chunk_sims=None, limit=10
    )
    mixed_window = score_chunks(
        "ZXCV9000 controller",
        chunks,
        query_vector=[0.1] * 4,
        chunk_sims={"semantic-other": 0.9},
        limit=10,
    )
    assert keyword_only and mixed_window
    assert mixed_window[0].score == pytest.approx(keyword_only[0].score)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    for _k in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
               "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL", "REASONING_LLM_MODEL"):
        monkeypatch.setenv(_k, "")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _seed_chunks(repo, texts):
    """建 notebook+source+elements, 走 P1 的 build+embed 真路径产出 chunks。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    import uuid
    sid = f"src-{uuid.uuid4().hex[:8]}"; now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, nb.id, "Doc", "document", "s.md", "/tmp/s.md", 0, "h", "", "", "extracted", now, now))
        for i, t in enumerate(texts, 1):
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,location_label,text,metadata,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"el-{sid}-{i:04d}", sid, "paragraph", f"p{i}", t, "{}", now))
    repo._chunk_and_embed_source(sid)
    return nb, sid


def _add_chunked_source(repo, notebook_id, texts, *, source_id=None):
    """Add one post-index source through the production chunk writer."""
    import uuid

    sid = source_id or f"src-{uuid.uuid4().hex[:8]}"
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,file_name,file_path,file_size,"
            "file_hash,summary,doc_type,parse_status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sid, notebook_id, sid, "document", f"{sid}.md", f"/tmp/{sid}.md",
                0, f"hash-{sid}", "", "", "extracted", now, now,
            ),
        )
        for index, text in enumerate(texts, 1):
            db.execute(
                "INSERT INTO source_elements "
                "(id,source_id,element_type,location_label,text,metadata,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    f"el-{sid}-{index:04d}", sid, "paragraph", f"p{index}",
                    text, "{}", now,
                ),
            )
    repo._chunk_and_embed_source(sid)
    return sid


def test_retrieve_chunks_returns_scored_with_matrix(repo):
    nb, _ = _seed_chunks(repo, ["deepseek v3 mixture of experts " * 20,
                                "tomato soup cooking recipe " * 20])
    scored, ids, mat = repo._retrieve_chunks(nb.id, "deepseek experts")
    assert scored and scored[0].relevance > 0
    assert len(ids) >= 1 and mat.shape[0] == len(ids)


def test_mmr_select_caps_and_subsets(repo):
    nb, _ = _seed_chunks(repo, [f"shared topic alpha detail {i} " * 20 for i in range(8)])
    scored, ids, mat = repo._retrieve_chunks(nb.id, "shared topic alpha")
    picked = repo._mmr_select_chunks(scored, ids, mat, k=3, lambda_=0.5)
    assert len(picked) <= 3
    assert {p.chunk_id for p in picked} <= {c.chunk_id for c in scored}


def test_retrieve_chunks_uses_ann_when_enabled(repo, monkeypatch):
    # 10 个 chunk;开 chunk_ann_enabled + recall=3 时只对 ANN∪FTS 有界候选
    # (各≤3,合计≤6)打分,非全表 10 条。
    nb, _ = _seed_chunks(repo, [f"topic {i} content detail body " * 10 for i in range(10)])
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    idx = repo._scale_index(nb.id)
    assert idx is not None and idx.chunk_ann_labels, "前置:build_scale_index 须产出 chunk ANN"

    monkeypatch.setattr(repo.settings, "chunk_ann_enabled", True)
    monkeypatch.setattr(repo.settings, "chunk_recall", 3)

    # 拦 sqlite_repository 内 score_chunks 绑定(_retrieve_chunks_ann 内局部导入)记录收到的候选数
    seen = {}
    import app.services.retrieval as rmod
    real = rmod.score_chunks
    def spy(query, chunks, *a, **k):
        seen["n"] = len(chunks)
        return real(query, chunks, *a, **k)
    monkeypatch.setattr(rmod, "score_chunks", spy)
    import app.services.sqlite_repository as srepo
    if hasattr(srepo, "score_chunks"):
        monkeypatch.setattr(srepo, "score_chunks", spy)

    scored, ids, mat = repo._retrieve_chunks(nb.id, "topic 3 content")
    assert seen.get("n", 10) <= 6            # 只对 ANN∪FTS 候选打分,非全部 10 条
    assert isinstance(scored, list)
    # ids 为 ANN∪FTS 候选子集且 ≤ 2*recall
    assert len(ids) <= 6
    with repo._connect() as db:
        lexical_ids = {
            hit["chunk_id"]
            for hit in repo._runtime.knowledge.chunk_fts_search(
                db, nb.id, "topic 3 content", k=3
            )
        }
    assert {c.chunk_id for c in scored} <= set(idx.chunk_ann_labels) | lexical_ids


def test_report_ann_skips_generic_fts_and_reports_pure_leaf_timings(
    repo, monkeypatch
):
    import numpy as np
    from app.services.retrieval_run import retrieval_run

    nb, _ = _seed_chunks(repo, ["indexed report evidence " * 20])
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    idx = repo._scale_index(nb.id, allow_stale=True)
    calls = []

    class _Ann:
        @staticmethod
        def set_ef(_value):
            return None

        @staticmethod
        def knn_query(_query, *, k, **kwargs):
            calls.append(kwargs)
            return (
                np.asarray([[0]], dtype=np.int64),
                np.asarray([[0.0]], dtype=np.float32),
            )

    monkeypatch.setattr(repo.retrieval.candidates, "_open_scale_ann", lambda *_: _Ann())
    monkeypatch.setattr(
        repo._runtime.knowledge,
        "chunk_fts_search",
        lambda *_a, **_k: pytest.fail("report ANN-only lane called generic FTS"),
    )
    events = []
    monkeypatch.setattr(repo.event_log, "emit", events.append)

    with retrieval_run(run_kind="report_planning"):
        out = repo._retrieve_chunks_ann(
            nb.id,
            "indexed report evidence",
            repo._embed_query("indexed report evidence"),
            idx,
            recall=1,
            allowed_source_ids=tuple(idx.chunk_ann_source_names),
        )

    assert out is not None and out[0]
    assert calls == [{}]  # all indexed source codes are covered: no callback
    timing = next(event for event in events if event.get("site") == "chunk_ann")
    assert timing["stage"] == "chunk_ann"
    assert timing["latency_ms"] == timing["total_ms"]
    assert timing["source_filter_mode"] == "vacuous"
    assert timing["lexical_mode"] == "report_ann_only"
    assert timing["chunk_fts_ms"] == 0
    assert {
        "ann_prepare_ms", "ann_open_ms", "knn_ms", "delta_ms",
        "hydrate_ms", "score_ms",
    } <= timing.keys()


def test_chunk_fts_timeout_opens_run_circuit_and_skips_the_next_probe(
    repo, monkeypatch
):
    from app.repositories.ports import ChunkLexicalSearchTimeout
    from app.services.retrieval_run import retrieval_run

    calls = []

    def _timeout(*_args, **_kwargs):
        calls.append("called")
        raise ChunkLexicalSearchTimeout("secret database diagnostic")

    monkeypatch.setattr(repo._runtime.knowledge, "chunk_fts_search", _timeout)
    events = []
    monkeypatch.setattr(repo.event_log, "emit", events.append)

    with retrieval_run(run_kind="report_planning") as run:
        with pytest.raises(ChunkLexicalSearchTimeout):
            repo.retrieval.candidates._chunk_fts_hits(
                object(), "nb-timeout", "private query", k=5
            )
        assert repo.retrieval.candidates._chunk_fts_hits(
            object(), "nb-timeout", "another private query", k=5
        ) == []
        assert run.chunk_fts_timeouts == 1
        assert run.chunk_fts_circuit_skips == 1

    assert calls == ["called"]
    assert [event["status"] for event in events] == [
        "timeout",
        "skipped_circuit_open",
    ]
    assert "secret database diagnostic" not in json.dumps(events)


def test_lexical_timeout_is_not_a_model_error(repo, monkeypatch):
    """词法臂超时不上「本次回答可能不完整」横幅:``_chunk_fts_hits`` 已记
    ask_stage=timeout 并关掉本轮词法臂,候选仍由其它臂产出;把它记成 model_error
    会显示成没有服务身份的「模型服务调用失败」。别的词法异常同样不记 model_error
    (关键词补召回臂缺席不影响结果,2026-09-29),只发一条带异常类名的
    ``ask_stage/chunk_keyword_union`` 事件,结果照常为空。"""
    from app.repositories.ports import ChunkLexicalSearchTimeout

    nb, _ = _seed_chunks(repo, ["engram memory architecture " * 20])
    noted = []

    class _Sink:
        def note_model_error(self, *args, **kwargs):
            noted.append(args)

    monkeypatch.setattr(repo.retrieval.candidates, "model_error_sink", _Sink())

    def _timeout(*_args, **_kwargs):
        raise ChunkLexicalSearchTimeout("secret database diagnostic")

    monkeypatch.setattr(repo.retrieval.candidates, "_chunk_fts_hits", _timeout)
    assert repo.retrieval.candidates._keyword_chunk_candidates(
        nb.id, "engram memory"
    ) == []
    assert noted == []

    def _broken(*_args, **_kwargs):
        raise RuntimeError("legacy lib missing chunks_fts")

    events = []
    monkeypatch.setattr(repo.retrieval.candidates, "_chunk_fts_hits", _broken)
    monkeypatch.setattr(repo.retrieval.candidates, "event_log",
                        type("L", (), {"emit": staticmethod(events.append)})())
    assert repo.retrieval.candidates._keyword_chunk_candidates(
        nb.id, "engram memory"
    ) == []
    assert noted == []
    assert [(e["stage"], e["status"], e["error_type"]) for e in events] == [
        ("chunk_keyword_union", "failed_open", "RuntimeError")]


def test_single_path_keyword_arm_reads_no_vectors_and_scores_as_before(
    repo, monkeypatch,
):
    """单库关键词臂只取文本行(2026-09-29):与改前「连向量矩阵一起 hydrate、再
    丢掉矩阵」逐项同结果——同一批 FTS 命中,chunk 字段与关键词打分一个不差——
    而且整个调用不再读 ``chunk_embeddings``。"""
    from app.services.retrieval import score_chunks

    nb, _ = _seed_chunks(repo, [
        "engram memory architecture overview " * 25,
        "kv cache compression and engram lookup " * 25,
        "unrelated routing table text " * 25,
    ])
    candidates = repo.retrieval.candidates
    needle = "engram memory"
    recall = candidates.settings.chunk_recall

    fts_hits = []
    real_fts = candidates._chunk_fts_hits

    def _capture_fts(*args, **kwargs):
        hits = real_fts(*args, **kwargs)
        fts_hits.append([h["chunk_id"] for h in hits])
        return hits

    vector_tables = []
    real_rows_by_ids = candidates.embeddings.rows_by_ids

    def _spy_rows_by_ids(db, table, *args, **kwargs):
        vector_tables.append(table)
        return real_rows_by_ids(db, table, *args, **kwargs)

    monkeypatch.setattr(candidates, "_chunk_fts_hits", _capture_fts)
    monkeypatch.setattr(candidates.embeddings, "rows_by_ids", _spy_rows_by_ids)
    after = candidates._keyword_chunk_candidates(nb.id, needle)

    assert vector_tables == []                       # 不再读 chunk_embeddings
    [hit_ids] = fts_hits
    assert len(hit_ids) >= 2 and after
    # 改前的形状:hydrate_chunk_candidates(文本 + 向量)→ 丢矩阵 → 同一个打分。
    before_rows, _ids, _mat = candidates.hydrate_chunk_candidates(hit_ids)
    # 探针本身有效:改前那条 hydrate 确实会读 chunk_embeddings。
    assert vector_tables and set(vector_tables) == {"chunk_embeddings"}
    before = score_chunks(needle, before_rows, None, None, limit=recall)
    assert candidates._hydrate_chunk_texts(hit_ids) == before_rows

    def _fields(chunk):
        return (chunk.chunk_id, chunk.source_id, chunk.source_title,
                chunk.section_path, chunk.text, list(chunk.element_ids),
                chunk.score, chunk.relevance)

    assert [_fields(c) for c in after] == [_fields(c) for c in before]


def test_ann_union_lexical_timeout_is_not_a_model_error(repo, monkeypatch):
    """ANN∪FTS 联合检索里词法臂超时:ANN 候选照常返回,不记 model_error(同上一条,
    这是横幅里那 27 条「模型服务调用失败」真正走的路径)。"""
    from app.repositories.ports import ChunkLexicalSearchTimeout

    query = "engram memory architecture"
    nb, _ = _seed_chunks(repo, [f"{query} " * 20])
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    idx = repo._scale_index(nb.id, allow_stale=True)
    noted = []

    class _Sink:
        def note_model_error(self, *args, **kwargs):
            noted.append(args)

    monkeypatch.setattr(repo.retrieval.candidates, "model_error_sink", _Sink())
    fts_calls = []

    def _timeout(*_args, **_kwargs):
        fts_calls.append("called")
        raise ChunkLexicalSearchTimeout("secret database diagnostic")

    monkeypatch.setattr(repo._runtime.knowledge, "chunk_fts_search", _timeout)
    result = repo._retrieve_chunks_ann(
        nb.id, query, repo._embed_query(query), idx, recall=2
    )
    assert result is not None
    assert fts_calls == ["called"]
    assert noted == []


def test_ann_union_lexical_failure_is_an_event_not_a_model_error(
    repo, monkeypatch
):
    """ANN∪FTS 联合检索里词法半发生非超时异常(例如旧库缺词法索引),而 ANN 已覆盖
    全部在范围来源:语义半的候选照常返回,答案不受影响,所以不进
    ``_ASK_MODEL_ERRORS``、不上「模型服务调用失败」横幅;改记一条 ``ask_stage``
    事件(stage=chunk_fts、status=failed_open、recall_role=supplement、只带异常
    类名),不含异常消息。

    只有带来源范围时才能这样判:无范围的直接调用见下一条(recall_role=unknown)。"""
    from app.core.ask_context import _ASK_MODEL_ERRORS

    query = "engram memory architecture"
    nb, sid = _seed_chunks(repo, [f"{query} " * 20])
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    idx = repo._scale_index(nb.id, allow_stale=True)
    fts_calls = []

    def _broken(*_args, **_kwargs):
        fts_calls.append("called")
        raise RuntimeError("secret database diagnostic")

    monkeypatch.setattr(repo._runtime.knowledge, "chunk_fts_search", _broken)
    events = []
    monkeypatch.setattr(repo.event_log, "emit", events.append)
    sink = []
    token = _ASK_MODEL_ERRORS.set(sink)
    try:
        result = repo._retrieve_chunks_ann(
            nb.id, query, repo._embed_query(query), idx, recall=2,
            allowed_source_ids=(sid,),
        )
    finally:
        _ASK_MODEL_ERRORS.reset(token)

    assert fts_calls == ["called"]
    assert result is not None and result[0]  # semantic half still answers
    assert sink == []
    assert not [e for e in events if e.get("kind") == "model_error"]
    arm = [e for e in events if e.get("site") == "chunk_ann_union"]
    assert arm == [{
        "kind": "ask_stage",
        "stage": "chunk_fts",
        "site": "chunk_ann_union",
        "notebook_id": nb.id,
        "status": "failed_open",
        "lexical_mode": "non_report_union",
        "recall_role": "supplement",
        "error_type": "RuntimeError",
    }]
    assert "secret database diagnostic" not in json.dumps(events)


def test_ann_union_lexical_failure_banners_when_scope_is_unknown(
    repo, monkeypatch
):
    """直接的服务调用方不给来源范围(``allowed`` 为 None、也不是报告运行):ANN 跑
    全库,不对 sidecar 做范围规划,无法知道 ANN 建好后有没有新来源只能靠词法召回。
    覆盖未知时按唯一召回处理——照旧记 ``chunk_fts`` model_error 上横幅,事件
    recall_role=unknown,仍不含异常消息。

    这条原先断言「无横幅」(与带范围的 supplement 同判):那样会在 ANN 之后新加
    的来源整体丢失时把失败压掉(codex #801 P2)。HTTP 请求总会冻结出 include
    范围,不走这条。"""
    from app.core.ask_context import _ASK_MODEL_ERRORS

    query = "engram memory architecture"
    nb, _sid = _seed_chunks(repo, [f"{query} " * 20])
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    idx = repo._scale_index(nb.id, allow_stale=True)

    def _broken(*_args, **_kwargs):
        raise RuntimeError("secret database diagnostic")

    monkeypatch.setattr(repo._runtime.knowledge, "chunk_fts_search", _broken)
    events = []
    monkeypatch.setattr(repo.event_log, "emit", events.append)
    sink = []
    token = _ASK_MODEL_ERRORS.set(sink)
    try:
        result = repo._retrieve_chunks_ann(
            nb.id, query, repo._embed_query(query), idx, recall=2,
        )
    finally:
        _ASK_MODEL_ERRORS.reset(token)

    assert result is not None and result[0]  # semantic half still answers
    assert [entry["stage"] for entry in sink] == ["chunk_fts"]
    arm = [e for e in events if e.get("site") == "chunk_ann_union"]
    assert arm == [{
        "kind": "ask_stage",
        "stage": "chunk_fts",
        "site": "chunk_ann_union",
        "notebook_id": nb.id,
        "status": "failed_open",
        "lexical_mode": "non_report_union",
        "recall_role": "unknown",
        "error_type": "RuntimeError",
    }]
    assert "secret database diagnostic" not in json.dumps(arm)


@pytest.mark.parametrize(
    ("run_kind", "lexical_mode"),
    [("ask_chunk", "non_report_union"), ("report_planning", "report_delta_fallback")],
)
def test_ann_union_lexical_failure_banners_when_fts_is_sole_recall(
    repo, monkeypatch, run_kind, lexical_mode
):
    """在范围内有一个尚未 fold 进 ANN 的来源(delta 暴力补召回默认关):它只能靠
    词法半召回,词法半一坏它就整体缺席,答案真受影响——照旧记 ``chunk_fts``
    model_error 上横幅,同时记事件(recall_role=sole),事件里仍不含异常消息。"""
    from app.core.ask_context import _ASK_MODEL_ERRORS
    from app.services.retrieval_run import retrieval_run

    nb, old_source = _seed_chunks(
        repo, ["indexed union evidence baseline " * 20]
    )
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    delta_source = _add_chunked_source(
        repo, nb.id, ["DELTA9000 fresh union evidence " * 20]
    )
    repo.backfill_chunk_fts(nb.id)
    idx = repo._scale_index(nb.id, allow_stale=True)
    assert delta_source not in idx.chunk_ann_source_names
    assert not repo.settings.scale_search_include_delta

    def _broken(*_args, **_kwargs):
        raise RuntimeError("secret database diagnostic")

    monkeypatch.setattr(repo._runtime.knowledge, "chunk_fts_search", _broken)
    events = []
    monkeypatch.setattr(repo.event_log, "emit", events.append)
    query = "indexed union evidence DELTA9000"
    sink = []
    token = _ASK_MODEL_ERRORS.set(sink)
    try:
        with retrieval_run(run_kind=run_kind):
            result = repo._retrieve_chunks_ann(
                nb.id, query, repo._embed_query(query), idx, recall=10,
                allowed_source_ids=(old_source, delta_source),
            )
    finally:
        _ASK_MODEL_ERRORS.reset(token)

    assert result is not None
    assert [entry["stage"] for entry in sink] == ["chunk_fts"]
    arm = [e for e in events if e.get("site") == "chunk_ann_union"]
    assert arm == [{
        "kind": "ask_stage",
        "stage": "chunk_fts",
        "site": "chunk_ann_union",
        "notebook_id": nb.id,
        "status": "failed_open",
        "lexical_mode": lexical_mode,
        "recall_role": "sole",
        "error_type": "RuntimeError",
    }]
    assert "secret database diagnostic" not in json.dumps(arm)


def test_ann_union_lexical_failure_is_supplement_when_delta_covers_new_source(
    repo, monkeypatch
):
    """同一个未 fold 来源,但 ``scale_search_include_delta`` 打开且暴力补召回
    成功:它已有语义候选,词法半只是补召回——不上横幅,只记 supplement 事件。"""
    from app.core.ask_context import _ASK_MODEL_ERRORS

    monkeypatch.setattr(repo.settings, "scale_search_include_delta", True)
    nb, old_source = _seed_chunks(
        repo, ["indexed union evidence baseline " * 20]
    )
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    delta_source = _add_chunked_source(
        repo, nb.id, ["DELTA9000 fresh union evidence " * 20]
    )
    idx = repo._scale_index(nb.id, allow_stale=True)
    assert delta_source not in idx.chunk_ann_source_names

    def _broken(*_args, **_kwargs):
        raise RuntimeError("secret database diagnostic")

    monkeypatch.setattr(repo._runtime.knowledge, "chunk_fts_search", _broken)
    events = []
    monkeypatch.setattr(repo.event_log, "emit", events.append)
    query = "fresh union evidence DELTA9000"
    sink = []
    token = _ASK_MODEL_ERRORS.set(sink)
    try:
        result = repo._retrieve_chunks_ann(
            nb.id, query, repo._embed_query(query), idx, recall=10,
            allowed_source_ids=(old_source, delta_source),
        )
    finally:
        _ASK_MODEL_ERRORS.reset(token)

    assert result is not None
    assert delta_source in {chunk.source_id for chunk in result[0]}
    assert sink == []
    arm = [e for e in events if e.get("site") == "chunk_ann_union"]
    assert [(e["lexical_mode"], e["recall_role"]) for e in arm] == [
        ("non_report_union", "supplement")
    ]


def test_ann_union_lexical_failure_is_sole_when_delta_returns_no_rows(
    repo, monkeypatch
):
    """``scale_search_include_delta`` 打开、两个未 fold 新来源都在范围里:一个有
    chunk 向量(delta 暴力补召回取回语义候选),一个缺 chunk_embeddings(delta 查询
    不报错但零行)。覆盖只按实际取回的来源算:前者从唯一召回集合里扣除,后者仍只能
    靠词法半——词法半一坏它整体缺席,判 sole、照旧上横幅(codex #801 P2)。"""
    from app.core.ask_context import _ASK_MODEL_ERRORS

    monkeypatch.setattr(repo.settings, "scale_search_include_delta", True)
    nb, old_source = _seed_chunks(
        repo, ["indexed union evidence baseline " * 20]
    )
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    embedded_source = _add_chunked_source(
        repo, nb.id, ["DELTA9000 fresh union evidence " * 20]
    )
    unembedded_source = _add_chunked_source(
        repo, nb.id, ["DELTA9001 vectorless union evidence " * 20]
    )
    with repo._write() as db:
        db.execute(
            "DELETE FROM chunk_embeddings WHERE chunk_id IN "
            "(SELECT id FROM chunks WHERE source_id=?)",
            (unembedded_source,),
        )
    idx = repo._scale_index(nb.id, allow_stale=True)
    assert embedded_source not in idx.chunk_ann_source_names
    assert unembedded_source not in idx.chunk_ann_source_names
    candidates = repo.retrieval.candidates
    assert set(candidates._index_delta(nb.id)["delta_sources"]) >= {
        embedded_source, unembedded_source,
    }

    def _broken(*_args, **_kwargs):
        raise RuntimeError("secret database diagnostic")

    monkeypatch.setattr(repo._runtime.knowledge, "chunk_fts_search", _broken)
    judged = []
    real_note = candidates._note_ann_union_lexical_failure

    def _spy_note(*args, **kwargs):
        judged.append(kwargs["delta_covered_sources"])
        return real_note(*args, **kwargs)

    monkeypatch.setattr(candidates, "_note_ann_union_lexical_failure", _spy_note)
    events = []
    monkeypatch.setattr(repo.event_log, "emit", events.append)
    query = "fresh union evidence DELTA9000 vectorless DELTA9001"
    sink = []
    token = _ASK_MODEL_ERRORS.set(sink)
    try:
        result = repo._retrieve_chunks_ann(
            nb.id, query, repo._embed_query(query), idx, recall=10,
            allowed_source_ids=(old_source, embedded_source, unembedded_source),
        )
    finally:
        _ASK_MODEL_ERRORS.reset(token)

    assert result is not None
    returned = {chunk.source_id for chunk in result[0]}
    assert embedded_source in returned
    assert unembedded_source not in returned
    assert [entry["stage"] for entry in sink] == ["chunk_fts"]
    arm = [e for e in events if e.get("site") == "chunk_ann_union"]
    assert [(e["lexical_mode"], e["recall_role"]) for e in arm] == [
        ("non_report_union", "sole")
    ]
    # 有行的那个新来源确实从唯一召回集合里扣除,判 sole 只因零行的那个。
    assert judged == [frozenset({embedded_source})]


@pytest.mark.parametrize("control", ["cancelled", "retrieval_control"])
def test_ann_union_lexical_control_flow_is_re_raised(repo, monkeypatch, control):
    """取消与检索控制流不是「词法失败」:照抛给调用方,不记事件、不记横幅。"""
    from app.domain.retrieval_control import RetrievalControlError
    from app.services.cancellation import AskCancelled

    exc_type = AskCancelled if control == "cancelled" else RetrievalControlError
    query = "engram memory architecture"
    nb, _ = _seed_chunks(repo, [f"{query} " * 20])
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    idx = repo._scale_index(nb.id, allow_stale=True)

    def _control(*_args, **_kwargs):
        raise exc_type()

    monkeypatch.setattr(repo.retrieval.candidates, "_chunk_fts_hits", _control)
    events = []
    monkeypatch.setattr(repo.event_log, "emit", events.append)
    with pytest.raises(exc_type):
        repo._retrieve_chunks_ann(
            nb.id, query, repo._embed_query(query), idx, recall=2
        )
    assert not [e for e in events if e.get("site") == "chunk_ann_union"]
    assert not [e for e in events if e.get("kind") == "model_error"]


@pytest.mark.parametrize("control", ["cancelled", "retrieval_control"])
def test_keyword_arm_control_flow_is_re_raised(repo, monkeypatch, control):
    """单库关键词补召回臂同理:取消与检索控制流照抛,不变成 failed_open 事件。"""
    from app.domain.retrieval_control import RetrievalControlError
    from app.services.cancellation import AskCancelled

    exc_type = AskCancelled if control == "cancelled" else RetrievalControlError
    nb, _ = _seed_chunks(repo, ["engram memory architecture " * 20])

    def _control(*_args, **_kwargs):
        raise exc_type()

    monkeypatch.setattr(repo.retrieval.candidates, "_chunk_fts_hits", _control)
    events = []
    monkeypatch.setattr(repo.retrieval.candidates, "event_log",
                        type("L", (), {"emit": staticmethod(events.append)})())
    with pytest.raises(exc_type):
        repo.retrieval.candidates._keyword_chunk_candidates(
            nb.id, "engram memory"
        )
    assert not [e for e in events if e.get("site") == "chunk_keyword_union"]


def test_ask_chunk_lexical_failure_shows_no_chunk_fts_banner(repo, monkeypatch):
    """同一条原则落到响应上:词法半坏掉时 ``AskResponse.model_errors`` 里没有
    ``chunk_fts``(前端 ModelErrorPanel 只要列表非空就上横幅),答案照常产出。

    请求带上 HTTP 入口那样冻结的全选 include 范围——无范围的直接调用覆盖未知,
    按唯一召回处理(见 ``..._banners_when_scope_is_unknown``)。"""
    from app.models.source_scope import SourceScope
    from app.services.source_scope import source_scope_context

    query = "engram memory architecture"
    nb, sid = _seed_chunks(repo, [f"{query} " * 20])
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    repo.settings.query_rewrite_enabled = False
    repo.settings.chunk_kg_overlay_enabled = False
    bind_chat_client(repo, "ask_answer", _FakeLLM("Engram stores memory [k1]."))
    fts_calls = []

    def _broken(*_args, **_kwargs):
        fts_calls.append("called")
        raise RuntimeError("secret database diagnostic")

    monkeypatch.setattr(repo._runtime.knowledge, "chunk_fts_search", _broken)
    events = []
    monkeypatch.setattr(repo.event_log, "emit", events.append)

    scope = SourceScope(mode="include", source_ids=[sid], narrowed=False)
    with source_scope_context(nb.id, scope):
        resp = repo.ask_chunk(
            nb.id, AskRequest(question=query, mode="chunk", source_scope=scope)
        )

    assert fts_calls
    assert {
        e["recall_role"] for e in events if e.get("site") == "chunk_ann_union"
    } == {"supplement"}
    assert "chunk_fts" not in [e.stage for e in resp.model_errors]
    assert resp.answer
    assert resp.citations


@pytest.mark.parametrize("query", ["set_db 命令", 'compare "timing exception"'])
def test_report_ann_keeps_exact_channel_without_generic_fts_union(
    repo, monkeypatch, query
):
    from app.services.retrieval_run import retrieval_run

    nb, _ = _seed_chunks(repo, [f"manual entry {query} " * 20])
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    idx = repo._scale_index(nb.id, allow_stale=True)
    monkeypatch.setattr(
        repo._runtime.knowledge,
        "chunk_fts_search",
        lambda *_args, **_kwargs: pytest.fail(
            "report ANN-only lane called generic FTS for an exact query"
        ),
    )
    with retrieval_run(run_kind="report_planning"):
        assert repo._retrieve_chunks_ann(
            nb.id,
            query,
            repo._embed_query(query),
            idx,
            recall=2,
        ) is not None

    # The independent channel is covered by test_exact_lookup.py; this guard
    # pins only that exact syntax cannot re-enable the expensive generic union.


def test_source_scoped_ann_filters_before_topk(repo, monkeypatch):
    """An excluded row may be the nearest vector but must not occupy Top-K."""
    nb = repo.create_notebook(NotebookCreate(name="scoped ann"))
    now = "2026-07-01T00:00:00"
    query = "summarize this paper"
    vector = repo._embed_query(query)
    with repo._write() as db:
        for source_id in ("selected", "excluded"):
            db.execute(
                "INSERT INTO sources "
                "(id,notebook_id,title,source_type,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (source_id, nb.id, source_id, "md", "ready", now, now),
            )
        for chunk_id, source_id in (
            ("c-selected", "selected"),
            ("c-excluded", "excluded"),
        ):
            db.execute(
                "INSERT INTO chunks "
                "(id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (chunk_id, nb.id, source_id, "untranslated body", "", "[]", now),
            )
            db.execute(
                "INSERT INTO chunk_embeddings "
                "(chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                (chunk_id, nb.id, json.dumps(vector), now),
            )
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    idx = repo._scale_index(nb.id, allow_stale=True)

    assert idx.chunk_ann_source_names is not None
    assert len(idx.chunk_ann_source_codes) == len(idx.chunk_ann_labels)
    monkeypatch.setattr(
        repo._runtime.knowledge, "chunk_fts_search", lambda *_a, **_k: []
    )
    out = repo._retrieve_chunks_ann(
        nb.id,
        query,
        vector,
        idx,
        recall=1,
        allowed_source_ids=("selected",),
    )

    assert out is not None
    assert [chunk.source_id for chunk in out[0]] == ["selected"]


def test_source_scoped_ann_without_sidecar_requests_bounded_fts_fallback(repo):
    legacy = SimpleNamespace(
        chunk_ann_labels=["c1"],
        manifest={"dim": 16},
    )

    assert repo._retrieve_chunks_ann(
        "nb",
        "query",
        [0.25] * 16,
        legacy,
        recall=1,
        allowed_source_ids=("selected",),
    ) is None


def test_source_scoped_ann_keeps_filter_for_partial_or_unknown_code_coverage(
    repo, monkeypatch
):
    import numpy as np

    calls = []

    class _Ann:
        @staticmethod
        def set_ef(_value):
            return None

        @staticmethod
        def knn_query(_query, *, k, **kwargs):
            calls.append(kwargs)
            return (
                np.asarray([[0]], dtype=np.int64),
                np.asarray([[0.0]], dtype=np.float32),
            )

    partial = SimpleNamespace(
        chunk_ann_labels=["allowed-chunk", "denied-chunk"],
        chunk_ann_source_names=["allowed", "denied"],
        chunk_ann_source_codes=np.asarray([0, 1], dtype=np.int32),
        chunk_ann_source_counts=np.asarray([1, 1], dtype=np.int64),
        manifest={"dim": 16},
    )
    monkeypatch.setattr(repo.retrieval.candidates, "_open_scale_ann", lambda *_: _Ann())
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "_hydrate_chunk_candidates",
        lambda _ids: ([], [], None),
    )

    assert repo._retrieve_chunks_ann(
        "nb",
        "query",
        [0.25] * 16,
        partial,
        recall=1,
        allowed_source_ids=("allowed",),
    ) is not None
    assert callable(calls[0].get("filter"))

    malformed = SimpleNamespace(
        chunk_ann_labels=["unknown-code"],
        chunk_ann_source_names=["allowed"],
        chunk_ann_source_codes=np.asarray([1], dtype=np.int32),
        chunk_ann_source_counts=np.asarray([1], dtype=np.int64),
        manifest={"dim": 16},
    )
    assert repo._retrieve_chunks_ann(
        "nb",
        "query",
        [0.25] * 16,
        malformed,
        recall=1,
        allowed_source_ids=("allowed",),
    ) is None


def test_source_scoped_ann_without_any_eligible_source_requests_fts_fallback(
    repo, monkeypatch
):
    """A selected post-index source is uncovered, not an empty ANN success."""
    import numpy as np

    class _Ann:
        @staticmethod
        def set_ef(_value):
            return None

        @staticmethod
        def knn_query(*_args, **_kwargs):
            pytest.fail("no eligible ANN row should issue KNN")

    index = SimpleNamespace(
        chunk_ann_labels=["old-chunk"],
        chunk_ann_source_names=["old-source"],
        chunk_ann_source_codes=np.asarray([0], dtype=np.int32),
        chunk_ann_source_counts=np.asarray([1], dtype=np.int64),
        manifest={"dim": 16},
    )
    monkeypatch.setattr(
        repo.retrieval.candidates, "_open_scale_ann", lambda *_: _Ann()
    )

    assert repo._retrieve_chunks_ann(
        "nb",
        "query",
        [0.25] * 16,
        index,
        recall=1,
        allowed_source_ids=("new-source",),
    ) is None


def test_report_unscoped_ann_applies_actor_source_ceiling_before_knn(
    repo, monkeypatch
):
    """A foreign hidden source cannot enter through the ANN core."""
    import numpy as np
    from app.services.retrieval_run import retrieval_run

    observed = []

    class _Ann:
        @staticmethod
        def set_ef(_value):
            return None

        @staticmethod
        def knn_query(_query, *, k, **kwargs):
            source_filter = kwargs.get("filter")
            observed.append(source_filter)
            labels = [label for label in (0, 1) if source_filter(label)]
            return (
                np.asarray([labels[:k]], dtype=np.int64),
                np.asarray([[0.0] * min(k, len(labels))], dtype=np.float32),
            )

    index = SimpleNamespace(
        chunk_ann_labels=["foreign-private", "visible-chunk"],
        chunk_ann_source_names=["visible-source", "foreign-private-source"],
        chunk_ann_source_codes=np.asarray([1, 0], dtype=np.int32),
        chunk_ann_source_counts=np.asarray([1, 1], dtype=np.int64),
        manifest={"dim": 16},
    )
    monkeypatch.setattr(
        repo.retrieval.candidates.sources,
        "all_visible_source_ids",
        lambda _notebook_id: ["visible-source"],
    )
    monkeypatch.setattr(
        repo.retrieval.candidates.sources,
        "hidden_source_ids",
        lambda _notebook_id, actor_id: [] if actor_id == "actor-b" else pytest.fail(
            "wrong report actor"
        ),
    )
    monkeypatch.setattr(
        repo.retrieval.candidates, "_open_scale_ann", lambda *_args: _Ann()
    )
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "_hydrate_chunk_candidates",
        lambda chunk_ids: (
            [
                {
                    "chunk_id": "visible-chunk",
                    "source_id": "visible-source",
                    "source_title": "visible",
                    "section_path": "",
                    "text": "visible evidence",
                    "element_ids": [],
                }
            ] if chunk_ids == ["visible-chunk"] else pytest.fail(
                f"unauthorized ANN ids reached hydration: {chunk_ids}"
            ),
            ["visible-chunk"],
            np.asarray([[1.0] + [0.0] * 15], dtype=np.float32),
        ),
    )
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "_chunk_fts_hits",
        lambda *_args, **_kwargs: pytest.fail("complete report ANN called FTS"),
    )

    with retrieval_run(run_kind="report_generation", actor_id="actor-b"):
        out = repo._retrieve_chunks_ann(
            "shared-library", "visible evidence", [1.0] + [0.0] * 15,
            index, recall=1,
        )

    assert out is not None
    assert [chunk.source_id for chunk in out[0]] == ["visible-source"]
    assert len(observed) == 1 and callable(observed[0])
    assert observed[0](0) is False  # foreign private source
    assert observed[0](1) is True   # actor-authorized visible source


def test_report_unscoped_ann_authority_probe_failure_fails_closed(
    repo, monkeypatch
):
    from app.services.retrieval_run import retrieval_run

    index = SimpleNamespace(chunk_ann_labels=["chunk"], manifest={"dim": 16})
    probe_calls = []

    def _fail_probe(_notebook_id):
        probe_calls.append("visible")
        raise RuntimeError("probe failed")

    monkeypatch.setattr(
        repo.retrieval.candidates.sources,
        "all_visible_source_ids",
        _fail_probe,
    )
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "_open_scale_ann",
        lambda *_args: pytest.fail("authority failure issued unscoped KNN"),
    )

    with retrieval_run(run_kind="report_planning", actor_id="actor"):
        assert repo._retrieve_chunks_ann(
            "shared-library", "query", [0.25] * 16, index, recall=1
        ) == ([], [], None)
        assert repo._retrieve_chunks_ann(
            "shared-library", "another query", [0.25] * 16, index, recall=1
        ) == ([], [], None)

    assert probe_calls == ["visible"]


@pytest.mark.parametrize(
    "failure_mode", ("embedding", "no_index", "legacy_sidecar", "ann_open")
)
def test_report_unscoped_baseline_fallback_keeps_actor_source_ceiling(
    repo, monkeypatch, failure_mode
):
    """Every ANN-unavailable branch must reach FTS with the actor ceiling.

    ``copyable=False`` below is load-bearing, not scenery: FTS degradation is
    the LARGE-library lane. A ceiling-bearing report run against a small
    library takes the bounded brute-force lane instead -- covered by
    ``test_report_unscoped_baseline_fallback_ceils_the_bruteforce_lane``.
    Before the scoped-vector-lane fix, ``allowed_source_ids is not None``
    short-circuited to FTS for BOTH library sizes, which is exactly the
    defect that made every UI-issued question skip chunk vector recall.
    """
    from app.services.retrieval_run import retrieval_run

    visible_source = "visible-source"
    foreign_private = "foreign-private-source"
    index = SimpleNamespace(
        chunk_ann_labels=["visible-chunk"],
        chunk_ann_source_names=[visible_source],
        chunk_ann_source_codes=[0],
        chunk_ann_source_counts=[1],
        manifest={"dim": 16},
    )
    if failure_mode == "legacy_sidecar":
        index = SimpleNamespace(
            chunk_ann_labels=["visible-chunk"], manifest={"dim": 16}
        )

    monkeypatch.setattr(
        repo.retrieval.candidates.sources,
        "all_visible_source_ids",
        lambda _notebook_id: [visible_source],
    )
    monkeypatch.setattr(
        repo.retrieval.candidates.sources,
        "hidden_source_ids",
        lambda _notebook_id, actor_id: [] if actor_id == "actor" else pytest.fail(
            "wrong report actor"
        ),
    )
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "_embed_query",
        lambda _query: None if failure_mode == "embedding" else [0.25] * 16,
    )
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "_scale_index",
        lambda *_args, **_kwargs: None if failure_mode == "no_index" else index,
    )
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "_open_scale_ann",
        lambda *_args: None if failure_mode == "ann_open" else pytest.fail(
            "unexpected ANN open"
        ),
    )
    observed = []

    def _bounded_fts(*_args, **kwargs):
        allowed = kwargs.get("allowed_source_ids")
        observed.append(tuple(allowed) if allowed is not None else None)
        # Model the producer boundary: a missing ceiling would admit the
        # foreign private row, while the effective actor ceiling cannot.
        return (
            ([foreign_private], [], None)
            if allowed is None else ([], [], None)
        )

    monkeypatch.setattr(
        repo.retrieval.candidates,
        "_retrieve_chunks_fts_degraded",
        _bounded_fts,
    )
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "notebook_copy_stats",
        lambda _notebook_id: {"copyable": False, "size": {}},
    )

    with retrieval_run(run_kind="report_generation", actor_id="actor"):
        out = repo.retrieval.candidates._retrieve_chunks_baseline(
            "shared-library", "query", drifted=False
        )

    assert out == ([], [], None)
    assert observed == [(visible_source,)]


def test_report_unscoped_baseline_fallback_ceils_the_bruteforce_lane(
    repo, monkeypatch
):
    """Small library, no ANN: the report actor ceiling binds the BRUTE lane.

    Sibling of the FTS test above. A report run carries no browser scope, so
    ``_chunk_source_ceiling`` materializes the actor's authorized universe;
    that list must keep bounding candidate generation on the lane a small
    library actually takes (``_gather_chunks(..., allowed_source_ids=...)``),
    not only on the large-library FTS lane.
    """
    from app.services.retrieval_run import retrieval_run

    visible_source = "visible-source"

    monkeypatch.setattr(
        repo.retrieval.candidates.sources,
        "all_visible_source_ids",
        lambda _notebook_id: [visible_source],
    )
    monkeypatch.setattr(
        repo.retrieval.candidates.sources,
        "hidden_source_ids",
        lambda _notebook_id, _actor_id: [],
    )
    monkeypatch.setattr(
        repo.retrieval.candidates, "_embed_query", lambda _query: [0.25] * 16
    )
    monkeypatch.setattr(
        repo.retrieval.candidates, "_scale_index", lambda *_a, **_kw: None
    )
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "notebook_copy_stats",
        lambda _notebook_id: {"copyable": True, "size": {}},
    )
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "_retrieve_chunks_fts_degraded",
        lambda *_a, **_kw: pytest.fail("small library must not degrade to FTS"),
    )

    gathered = []
    orig_gather = repo.retrieval.candidates._gather_chunks

    def _spy_gather(db, notebook_id, allowed_source_ids=None):
        gathered.append(
            tuple(allowed_source_ids) if allowed_source_ids is not None else None
        )
        return orig_gather(db, notebook_id, allowed_source_ids)

    monkeypatch.setattr(
        repo.retrieval.candidates, "_gather_chunks", _spy_gather
    )

    with retrieval_run(run_kind="report_generation", actor_id="actor"):
        repo.retrieval.candidates._retrieve_chunks_baseline(
            "shared-library", "query", drifted=False
        )

    assert gathered == [(visible_source,)], (
        "the brute-force lane must receive the same actor ceiling the FTS "
        f"lane does, got {gathered}"
    )


def test_report_unscoped_authority_failure_blocks_baseline_and_contributors(
    repo, monkeypatch
):
    from app.services.retrieval_run import retrieval_run

    monkeypatch.setattr(
        repo.retrieval.candidates.sources,
        "all_visible_source_ids",
        lambda _notebook_id: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "_retrieve_chunks_baseline",
        lambda *_args, **_kwargs: pytest.fail("failed authority reached baseline"),
    )
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "_run_chunk_candidate_contributors",
        lambda *_args, **_kwargs: pytest.fail("failed authority reached contributor"),
    )

    with retrieval_run(run_kind="report_planning", actor_id="actor"):
        assert repo.retrieval.candidates._retrieve_chunks(
            "shared-library", "query"
        ) == ([], [], None)


def test_report_unscoped_empty_actor_ceiling_cannot_fall_back_to_foreign_ann(
    repo, monkeypatch
):
    from app.services.retrieval_run import retrieval_run

    monkeypatch.setattr(
        repo.retrieval.candidates.sources,
        "all_visible_source_ids",
        lambda _notebook_id: [],
    )
    monkeypatch.setattr(
        repo.retrieval.candidates.sources,
        "hidden_source_ids",
        lambda _notebook_id, _actor_id: [],
    )
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "_embed_query",
        lambda _query: [0.25] * 16,
    )
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "_scale_index",
        lambda *_args, **_kwargs: pytest.fail("deny-all opened the index"),
    )
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "_retrieve_chunks_fts_degraded",
        lambda *_args, **_kwargs: pytest.fail("deny-all called FTS"),
    )

    with retrieval_run(run_kind="report_generation", actor_id="actor"):
        assert repo.retrieval.candidates._retrieve_chunks_baseline(
            "shared-library", "query", drifted=False
        ) == ([], [], None)


def test_report_unscoped_ann_source_ceiling_is_singleflight_across_leaves(
    repo, monkeypatch
):
    import numpy as np
    import app.services.retrieval_run as retrieval_run_module
    from app.services.retrieval_run import retrieval_run

    entered = threading.Event()
    release = threading.Event()
    waiter_blocked = threading.Event()
    probe_calls = []
    real_pending = retrieval_run_module._PendingEmbedding

    class _ObservedReady:
        def __init__(self, ready):
            self._ready = ready

        def wait(self, timeout=None):
            waiter_blocked.set()
            return self._ready.wait(timeout)

        def set(self):
            return self._ready.set()

    monkeypatch.setattr(
        retrieval_run_module,
        "_PendingEmbedding",
        lambda ready: real_pending(_ObservedReady(ready)),
    )

    def _visible(_notebook_id):
        probe_calls.append("visible")
        entered.set()
        assert release.wait(timeout=2)
        return ["visible-source"]

    class _Ann:
        @staticmethod
        def set_ef(_value):
            return None

        @staticmethod
        def knn_query(_query, *, k, **_kwargs):
            return (
                np.asarray([[0]], dtype=np.int64),
                np.asarray([[0.0]], dtype=np.float32),
            )

    index = SimpleNamespace(
        chunk_ann_labels=["visible-chunk"],
        chunk_ann_source_names=["visible-source"],
        chunk_ann_source_codes=np.asarray([0], dtype=np.int32),
        chunk_ann_source_counts=np.asarray([1], dtype=np.int64),
        manifest={"dim": 16},
    )
    monkeypatch.setattr(
        repo.retrieval.candidates.sources, "all_visible_source_ids", _visible
    )
    monkeypatch.setattr(
        repo.retrieval.candidates.sources,
        "hidden_source_ids",
        lambda _notebook_id, _actor_id: [],
    )
    monkeypatch.setattr(
        repo.retrieval.candidates, "_open_scale_ann", lambda *_args: _Ann()
    )
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "_hydrate_chunk_candidates",
        lambda _ids: ([], [], None),
    )
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "_chunk_fts_hits",
        lambda *_args, **_kwargs: pytest.fail("complete report ANN called FTS"),
    )

    def _leaf(query):
        return repo._retrieve_chunks_ann(
            "shared-library", query, [0.25] * 16, index, recall=1
        )

    with retrieval_run(run_kind="report_generation", actor_id="actor"):
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(contextvars.copy_context().run, _leaf, "first")
            assert entered.wait(timeout=2)
            second = pool.submit(contextvars.copy_context().run, _leaf, "second")
            assert waiter_blocked.wait(timeout=2)
            release.set()
            assert first.result(timeout=2) is not None
            assert second.result(timeout=2) is not None

    assert probe_calls == ["visible"]


def test_embed_query_singleflight_waiter_propagates_cancellation(
    repo, monkeypatch
):
    import app.services.retrieval_run as retrieval_run_module
    from app.services.cancellation import AskCancelled
    from app.services.retrieval_run import retrieval_run

    cancel_event = threading.Event()
    owner_entered = threading.Event()
    release_owner = threading.Event()
    waiter_blocked = threading.Event()
    embed_calls = []
    real_pending = retrieval_run_module._PendingEmbedding

    class _ObservedReady:
        def __init__(self, ready):
            self._ready = ready

        def wait(self, timeout=None):
            waiter_blocked.set()
            return self._ready.wait(timeout)

        def set(self):
            return self._ready.set()

    monkeypatch.setattr(
        retrieval_run_module,
        "_PendingEmbedding",
        lambda ready: real_pending(_ObservedReady(ready)),
    )

    def _embed(_query):
        embed_calls.append("owner")
        owner_entered.set()
        assert release_owner.wait(timeout=2)
        return [0.25] * 16

    monkeypatch.setattr(repo.retrieval.candidates.embedder, "embed_query", _embed)

    with retrieval_run(
        run_kind="report_generation", cancel_event=cancel_event
    ):
        with ThreadPoolExecutor(max_workers=2) as pool:
            owner = pool.submit(
                contextvars.copy_context().run, repo._embed_query, "same query"
            )
            assert owner_entered.wait(timeout=2)
            waiter = pool.submit(
                contextvars.copy_context().run, repo._embed_query, "same query"
            )
            assert waiter_blocked.wait(timeout=2)
            cancel_event.set()
            with pytest.raises(AskCancelled):
                waiter.result(timeout=2)
            release_owner.set()
            assert owner.result(timeout=2) == [0.25] * 16

    assert embed_calls == ["owner"]


def test_report_ann_source_scope_plan_is_singleflight_across_leaves(
    repo, monkeypatch
):
    import numpy as np
    import app.services.retrieval_run as retrieval_run_module
    from app.services.cancellation import AskCancelled
    from app.services.retrieval_run import retrieval_run

    cancel_event = threading.Event()
    owner_entered = threading.Event()
    release_owner = threading.Event()
    waiter_blocked = threading.Event()
    plan_calls = []
    real_pending = retrieval_run_module._PendingEmbedding

    class _ObservedReady:
        def __init__(self, ready):
            self._ready = ready

        def wait(self, timeout=None):
            waiter_blocked.set()
            return self._ready.wait(timeout)

        def set(self):
            return self._ready.set()

    monkeypatch.setattr(
        retrieval_run_module,
        "_PendingEmbedding",
        lambda ready: real_pending(_ObservedReady(ready)),
    )

    class _BlockingSourceNames:
        def __len__(self):
            return 1

        def __iter__(self):
            plan_calls.append("sidecar")
            owner_entered.set()
            assert release_owner.wait(timeout=2)
            return iter(("indexed-source",))

    class _Ann:
        @staticmethod
        def set_ef(_value):
            return None

        @staticmethod
        def knn_query(_query, *, k, **_kwargs):
            return (
                np.asarray([[0]], dtype=np.int64),
                np.asarray([[0.0]], dtype=np.float32),
            )

    index = SimpleNamespace(
        chunk_ann_labels=["indexed-chunk"],
        chunk_ann_source_names=_BlockingSourceNames(),
        chunk_ann_source_codes=np.asarray([0], dtype=np.int32),
        chunk_ann_source_counts=np.asarray([1], dtype=np.int64),
        manifest={"dim": 16, "version": "same-generation"},
    )
    monkeypatch.setattr(
        repo.retrieval.candidates, "_open_scale_ann", lambda *_args: _Ann()
    )
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "_hydrate_chunk_candidates",
        lambda _ids: ([], [], None),
    )
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "_chunk_fts_hits",
        lambda *_args, **_kwargs: pytest.fail("empty missing source called FTS"),
    )

    def _leaf(query):
        return repo._retrieve_chunks_ann(
            "shared-library",
            query,
            [0.25] * 16,
            index,
            recall=1,
            allowed_source_ids=("indexed-source",),
        )

    with retrieval_run(
        run_kind="report_generation", cancel_event=cancel_event
    ):
        with ThreadPoolExecutor(max_workers=2) as pool:
            owner = pool.submit(contextvars.copy_context().run, _leaf, "first")
            assert owner_entered.wait(timeout=2)
            waiter = pool.submit(contextvars.copy_context().run, _leaf, "second")
            assert waiter_blocked.wait(timeout=2)
            cancel_event.set()
            with pytest.raises(AskCancelled):
                waiter.result(timeout=2)
            release_owner.set()
            assert owner.result(timeout=2) is not None

    assert plan_calls == ["sidecar"]


def test_report_source_ceiling_stays_frozen_across_index_reload(
    repo, monkeypatch
):
    import numpy as np
    from app.services.retrieval_run import retrieval_run

    probe_calls = []

    def _visible(_notebook_id):
        probe_calls.append("visible")
        return (
            ["source-a"]
            if len(probe_calls) == 1 else ["source-a", "source-added-later"]
        )

    class _Ann:
        @staticmethod
        def set_ef(_value):
            return None

        def __init__(self, row_count):
            self.row_count = row_count

        def knn_query(self, _query, *, k, **kwargs):
            source_filter = kwargs.get("filter")
            labels = list(range(self.row_count))
            if source_filter is not None:
                labels = [label for label in labels if source_filter(label)]
            labels = labels[:k]
            return (
                np.asarray([labels], dtype=np.int64),
                np.asarray([[0.0] * len(labels)], dtype=np.float32),
            )

    first_index = SimpleNamespace(
        chunk_ann_labels=["chunk-a"],
        chunk_ann_source_names=["source-a"],
        chunk_ann_source_codes=np.asarray([0], dtype=np.int32),
        chunk_ann_source_counts=np.asarray([1], dtype=np.int64),
        manifest={"dim": 16, "version": "first"},
    )
    reloaded_index = SimpleNamespace(
        chunk_ann_labels=["chunk-a", "chunk-added-later"],
        chunk_ann_source_names=["source-a", "source-added-later"],
        chunk_ann_source_codes=np.asarray([0, 1], dtype=np.int32),
        chunk_ann_source_counts=np.asarray([1, 1], dtype=np.int64),
        manifest={"dim": 16, "version": "second"},
    )
    monkeypatch.setattr(
        repo.retrieval.candidates.sources, "all_visible_source_ids", _visible
    )
    monkeypatch.setattr(
        repo.retrieval.candidates.sources,
        "hidden_source_ids",
        lambda _notebook_id, _actor_id: [],
    )
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "_open_scale_ann",
        lambda idx, _kind: _Ann(len(idx.chunk_ann_labels)),
    )

    def _hydrate(ids):
        rows = [
            {
                "chunk_id": chunk_id,
                "source_id": (
                    "source-added-later"
                    if chunk_id == "chunk-added-later" else "source-a"
                ),
                "source_title": "source",
                "section_path": "",
                "text": "evidence",
                "element_ids": [],
            }
            for chunk_id in ids
        ]
        return rows, list(ids), np.ones((len(ids), 16), dtype=np.float32)

    monkeypatch.setattr(
        repo.retrieval.candidates, "_hydrate_chunk_candidates", _hydrate
    )
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "_chunk_fts_hits",
        lambda *_args, **_kwargs: pytest.fail("complete frozen scope called FTS"),
    )

    with retrieval_run(run_kind="report_generation", actor_id="actor"):
        first = repo._retrieve_chunks_ann(
            "shared-library", "query", [0.25] * 16, first_index, recall=2
        )
        second = repo._retrieve_chunks_ann(
            "shared-library", "query", [0.25] * 16, reloaded_index, recall=2
        )

    assert probe_calls == ["visible"]
    assert {chunk.source_id for chunk in first[0]} == {"source-a"}
    assert {chunk.source_id for chunk in second[0]} == {"source-a"}


def test_report_mixed_indexed_and_delta_scope_restores_bounded_fts(
    repo, monkeypatch
):
    """An eligible old source must not hide a selected post-watermark source."""
    from app.services.retrieval_run import retrieval_run

    nb, old_source = _seed_chunks(
        repo, ["indexed report evidence baseline " * 20]
    )
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    delta_source = _add_chunked_source(
        repo, nb.id, ["DELTA9000 fresh report evidence " * 20]
    )
    excluded_source = _add_chunked_source(
        repo, nb.id, ["DELTA9000 excluded report evidence " * 20]
    )
    repo.backfill_chunk_fts(nb.id)
    idx = repo._scale_index(nb.id, allow_stale=True)
    assert old_source in idx.chunk_ann_source_names
    assert delta_source not in idx.chunk_ann_source_names

    real_hits = repo.retrieval.candidates._chunk_fts_hits
    calls = []

    def _spy(*args, **kwargs):
        calls.append(kwargs.get("allowed_source_ids"))
        return real_hits(*args, **kwargs)

    monkeypatch.setattr(repo.retrieval.candidates, "_chunk_fts_hits", _spy)
    events = []
    monkeypatch.setattr(repo.event_log, "emit", events.append)
    allowed = (old_source, delta_source)
    query = "indexed report evidence DELTA9000"
    with retrieval_run(run_kind="report_planning"):
        out = repo._retrieve_chunks_ann(
            nb.id,
            query,
            repo._embed_query(query),
            idx,
            recall=10,
            allowed_source_ids=allowed,
        )

    assert out is not None
    sources = {chunk.source_id for chunk in out[0]}
    assert {old_source, delta_source} <= sources
    assert excluded_source not in sources
    assert calls == [(delta_source,)]
    timing = next(event for event in events if event.get("site") == "chunk_ann")
    assert timing["lexical_mode"] == "report_delta_fallback"
    assert timing["chunk_fts_ms"] >= 0


def test_report_unscoped_sidecar_missing_source_restores_bounded_fts(
    repo, monkeypatch
):
    """A mounted source absent from the immutable sidecar gets bounded FTS."""
    from app.services.retrieval_run import retrieval_run

    nb, _old_source = _seed_chunks(
        repo, ["indexed mounted-library baseline " * 20]
    )
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    delta_source = _add_chunked_source(
        repo, nb.id, ["MOUNTDELTA9000 fresh mounted evidence " * 20]
    )
    repo.backfill_chunk_fts(nb.id)
    idx = repo._scale_index(nb.id, allow_stale=True)

    real_hits = repo.retrieval.candidates._chunk_fts_hits
    calls = []

    def _spy(*args, **kwargs):
        calls.append(kwargs.get("allowed_source_ids"))
        return real_hits(*args, **kwargs)

    monkeypatch.setattr(repo.retrieval.candidates, "_chunk_fts_hits", _spy)
    real_visible = repo.retrieval.candidates.sources.all_visible_source_ids
    real_hidden = repo.retrieval.candidates.sources.hidden_source_ids
    source_universe_calls = []

    def _visible(notebook_id):
        source_universe_calls.append(("visible", notebook_id))
        return real_visible(notebook_id)

    def _hidden(notebook_id, actor_id):
        source_universe_calls.append(("hidden", notebook_id, actor_id))
        return real_hidden(notebook_id, actor_id)

    monkeypatch.setattr(
        repo.retrieval.candidates.sources, "all_visible_source_ids", _visible
    )
    monkeypatch.setattr(
        repo.retrieval.candidates.sources, "hidden_source_ids", _hidden
    )
    events = []
    monkeypatch.setattr(repo.event_log, "emit", events.append)
    query = "MOUNTDELTA9000 mounted evidence"
    actor_id = repo.current_user().id
    with retrieval_run(run_kind="report_generation", actor_id=actor_id):
        out = repo._retrieve_chunks_ann(
            nb.id,
            query,
            repo._embed_query(query),
            idx,
            recall=10,
        )
        repeated = repo._retrieve_chunks_ann(
            nb.id,
            query,
            repo._embed_query(query),
            idx,
            recall=10,
        )

    assert out is not None
    assert repeated is not None
    assert delta_source in {chunk.source_id for chunk in out[0]}
    assert calls == [(delta_source,), (delta_source,)]
    assert source_universe_calls == [
        ("visible", nb.id),
        ("hidden", nb.id, actor_id),
    ]
    timing = next(event for event in events if event.get("site") == "chunk_ann")
    assert timing["lexical_mode"] == "report_delta_fallback"
    assert timing["chunk_fts_ms"] >= 0


def test_report_empty_source_missing_from_sidecar_does_not_trigger_fts(
    repo, monkeypatch
):
    from app.services.retrieval_run import retrieval_run

    nb, indexed_source = _seed_chunks(repo, ["indexed evidence " * 20])
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    empty_source = _add_chunked_source(repo, nb.id, [])
    idx = repo._scale_index(nb.id, allow_stale=True)
    monkeypatch.setattr(
        repo.retrieval.candidates,
        "_chunk_fts_hits",
        lambda *_args, **_kwargs: pytest.fail("empty source triggered FTS"),
    )

    with retrieval_run(run_kind="report_generation"):
        out = repo._retrieve_chunks_ann(
            nb.id,
            "indexed evidence",
            repo._embed_query("indexed evidence"),
            idx,
            recall=5,
            allowed_source_ids=(indexed_source, empty_source),
        )

    assert out is not None and out[0]


def test_report_empty_source_is_reprobed_after_chunk_commit_without_kg_bump(
    repo, monkeypatch
):
    from app.repositories.ports import ChunkWrite
    from app.services.retrieval_run import retrieval_run

    nb, indexed_source = _seed_chunks(repo, ["indexed baseline " * 20])
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    pending_source = _add_chunked_source(repo, nb.id, [])
    idx = repo._scale_index(nb.id, allow_stale=True)
    real_hits = repo.retrieval.candidates._chunk_fts_hits
    calls = []

    def _spy(*args, **kwargs):
        calls.append(kwargs.get("allowed_source_ids"))
        return real_hits(*args, **kwargs)

    monkeypatch.setattr(repo.retrieval.candidates, "_chunk_fts_hits", _spy)
    allowed = (indexed_source, pending_source)
    with retrieval_run(run_kind="report_generation"):
        first = repo._retrieve_chunks_ann(
            nb.id,
            "indexed baseline",
            repo._embed_query("indexed baseline"),
            idx,
            recall=5,
            allowed_source_ids=allowed,
        )
        assert first is not None and first[0]
        assert calls == []

        with repo._connect() as db:
            before_seq = db.execute(
                "SELECT kg_mutation_seq FROM unified_kg_state "
                "WHERE notebook_id=?",
                (nb.id,),
            ).fetchone()["kg_mutation_seq"]
        now = _now()
        repo._runtime.chunk_store.replace_source_chunks(
            pending_source,
            nb.id,
            (
                ChunkWrite(
                    id="late-chunk",
                    text="LATECHUNK9000 report evidence " * 20,
                    section_path="late",
                    element_ids=(),
                ),
            ),
            created_at=now,
            mark_chunked_at=now,
        )
        with repo._connect() as db:
            after_seq = db.execute(
                "SELECT kg_mutation_seq FROM unified_kg_state "
                "WHERE notebook_id=?",
                (nb.id,),
            ).fetchone()["kg_mutation_seq"]
        assert after_seq == before_seq

        second = repo._retrieve_chunks_ann(
            nb.id,
            "LATECHUNK9000 report evidence",
            repo._embed_query("LATECHUNK9000 report evidence"),
            idx,
            recall=5,
            allowed_source_ids=allowed,
        )

    assert second is not None
    assert pending_source in {chunk.source_id for chunk in second[0]}
    assert calls == [(pending_source,)]


def test_sqlite_chunk_fts_source_scope_uses_one_json_bind(repo):
    import sqlite3

    nb, first_source = _seed_chunks(repo, ["JSONSCOPE9000 evidence " * 20])
    source_ids = [first_source]
    for _index in range(4):
        source_ids.append(_add_chunked_source(
            repo, nb.id, ["JSONSCOPE9000 evidence " * 20]
        ))
    repo.backfill_chunk_fts(nb.id)

    with repo._connect() as db:
        previous = db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 4)
        try:
            hits = repo._runtime.knowledge.chunk_fts_search(
                db,
                nb.id,
                "JSONSCOPE9000 evidence",
                k=10,
                allowed_source_ids=source_ids,
            )
        finally:
            db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous)

    assert hits


def test_retrieve_chunks_ann_includes_post_build_delta(repo, monkeypatch):
    """opt-in delta brute-force: with scale_search_include_delta=True, a source
    added AFTER the watermark (not in chunk_ann.bin) is still recalled via the
    delta matmul path. (Default is now OFF — see test_scale_delta_policy.py — so
    this test explicitly enables the opt-in to exercise the brute-force branch.)"""
    import json
    from app.models.schemas import NotebookCreate
    monkeypatch.setattr(repo.settings, "scale_search_include_delta", True)
    nb = repo.create_notebook(NotebookCreate(name="base"))
    def add_source(sid, pairs, day):  # pairs: [(chunk_id, text)]
        with repo._write() as db:
            now = f"2026-07-{day:02d}T00:00:00"
            db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?)", (sid, nb.id, "t", "md", "ready", now, now))
            for cid, txt in pairs:
                db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                           "VALUES (?,?,?,?,?,?,?)", (cid, nb.id, sid, txt, "", "[]", now))
                v = repo._runtime.models.embedding("retrieval_query_embedding").embed_texts([txt])[0]
                db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                           (cid, nb.id, json.dumps(v), now))
    # 建索引时的存量
    add_source("s1", [("c1", "alpha topic"), ("c2", "beta topic")], 1)
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    # build 之后新上传一个 source(delta)——它不在 chunk_ann.bin 里
    add_source("s2", [("c3", "gamma delta topic")], 2)
    monkeypatch.setattr(repo.settings, "chunk_ann_enabled", True)

    idx = repo._scale_index(nb.id, allow_stale=True)
    assert idx is not None and getattr(idx, "chunk_ann_labels", None)
    assert "c3" not in set(idx.chunk_ann_labels)  # 前提:c3 确实不在存量 ANN
    out = repo._retrieve_chunks_ann(nb.id, "gamma delta topic", repo._embed_query("gamma delta topic"), idx, recall=10)
    assert out is not None
    scored, ids, mat = out
    assert "c3" in {c.chunk_id for c in scored}   # ⊕ delta:新上传的 c3 被召回


def test_chunk_ann_unions_lexical(repo, monkeypatch):
    """纯词法命中的 chunk 经 FTS 被召回(ANN 语义漏它)。
    显式布置向量隔离出「纯词法」通道:
      · 8 个填充 chunk:向量与 query 近正交(cosine≈0,ANN top-8 独占但语义弱到过不了
        RELEVANCE_FLOOR → score_chunks 丢弃),文本不含罕见词;
      · 目标 c_lex:向量与 query 反向(cosine=-1,ANN 必漏,排在 8 个填充之后),
        但文本含罕见词法词 XZQW9000 与 query 字面匹配(keyword=1.0)。
    纯 ANN(语义)→ 候选 8 填充全被 floor 丢、c_lex 根本没进候选 → 空;
    只有 FTS 词法∪ 把 c_lex 补进候选,keyword 分兜排序 → 被召回。"""
    import json as _json
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="lex"))
    now = "2026-07-01T00:00:00"
    query = "XZQW9000"                                   # 罕见词法词即整条 query(keyword=1.0)
    qv = repo._embed_query(query)                        # query 向量
    far = [-x for x in qv]                               # 与 query 反向(cosine=-1,语义最远)
    half = len(qv) // 2
    mid = qv[:half] + far[half:]                          # 半正半反 → 与 query cosine≈0(语义弱)
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("s1", nb.id, "t", "md", "ready", now, now))
        # 8 个填充 chunk:向量近正交(ANN 命中但语义分过不了 floor),文本不含罕见词
        rows = [(f"c{i}", "alpha beta topic detail body filler", mid) for i in range(8)]
        # 目标 chunk:向量反向(ANN 必漏),但文本含罕见词法词 XZQW9000
        rows.append(("c_lex", "XZQW9000 unrelated bandgap widget spec", far))
        for cid, txt, v in rows:
            db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                       "VALUES (?,?,?,?,?,?,?)", (cid, nb.id, "s1", txt, "", "[]", now))
            db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                       (cid, nb.id, _json.dumps(v), now))
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    repo.backfill_chunk_fts(nb.id)

    monkeypatch.setattr(repo.settings, "chunk_ann_enabled", True)
    idx = repo._scale_index(nb.id, allow_stale=True)
    assert idx is not None and idx.chunk_ann_labels, "前置:build_scale_index 须产出 chunk ANN"
    assert "c_lex" in set(idx.chunk_ann_labels), "前置:c_lex 在存量 ANN(非 delta 路径召回)"

    # recall=8:ANN top-8 全被 8 个填充 chunk 占满(语义弱),c_lex(反向向量)语义漏
    out = repo._retrieve_chunks_ann(nb.id, query, qv, idx, recall=8)
    assert out is not None
    scored = out[0]
    assert "c_lex" in {c.chunk_id for c in scored}   # 词法命中被并入并打分排序


class _FakeLLM:
    """配置好的假 LLM:chat_json 回定长 JSON, 内含 [k1] 标记。"""
    configured = True
    def __init__(self, answer): self._answer = answer
    def chat_json(self, messages, schema_hint, **kw):
        return json.dumps({"answer": self._answer, "grounded": True})


def test_ask_chunk_deterministic_without_llm(repo):
    # fixture 清了 LLM key → llm_client.configured False → 走确定性兜底。
    nb, _ = _seed_chunks(repo, ["deepseek v3 mixture of experts routing " * 20,
                                "deepseek v2 dense baseline architecture " * 20])
    resp = repo.ask_chunk(nb.id, AskRequest(question="deepseek experts routing"))
    assert resp.answer == "" and "passage" in resp.conclusion.lower()
    assert resp.anchors == [] and resp.citations          # 有引用, 无 anchor
    assert resp.citations[0].source_id and resp.evidence_level == "inferred"


def test_ask_chunk_binds_anchor_to_chunk_with_llm(repo, monkeypatch):
    nb, _ = _seed_chunks(repo, ["deepseek v3 mixture of experts routing " * 20])
    bind_chat_client(repo, "ask_answer", _FakeLLM("DeepSeek V3 uses MoE routing [k1]."))
    resp = repo.ask_chunk(nb.id, AskRequest(question="deepseek experts"))
    assert resp.answer and resp.anchors
    a = resp.anchors[0]
    assert a.object_type == "chunk" and a.object_id.startswith("ck-")
    assert resp.conclusion and "[k1]" not in resp.conclusion   # 标记已剥离


def test_ask_routes_default_mode_to_chunk(repo, monkeypatch):
    sentinel = object()
    service = repo.__dict__["_runtime"].ask_component
    monkeypatch.setattr(service, "ask_chunk", lambda nb, p, **kwargs: sentinel)
    # AskRequest() 默认 mode 应为 "chunk" → ask() 分发到 ask_chunk
    assert AskRequest(question="x").mode == "chunk"
    assert service.ask(
        "nb-irrelevant", AskRequest(question="x"), user_id=repo.current_user().id
    ) is sentinel


def test_ask_chunk_comparison_balances_both_entities(repo, monkeypatch):
    # 种两实体 chunk;假 expand 出 2 子查询;断言两实体都进 selected
    nb, _ = _seed_chunks(repo, ["DeepSeek-V2 uses MLA attention " * 20,
                                "DeepSeek-V2 dense baseline " * 20,
                                "DeepSeek-V3 MoE 671B improvements " * 20,
                                "DeepSeek-V3 MTP training " * 20])
    import app.services.query_rewrite as qr
    monkeypatch.setattr(qr, "expand_query", lambda *a, **k: qr.ExpandedQuery(
        query="V3 vs V2", sub_queries=[qr.SubQuerySpec("DeepSeek-V3 improvements"),
                                       qr.SubQuerySpec("DeepSeek-V2 features")]))
    bind_chat_client(repo, "ask_answer", _FakeLLM("V3 improves on V2 [k1][k2]."))
    resp = repo.ask_chunk(nb.id, AskRequest(question="deepseekv3相比deepseekv2有什么改进"))
    srcs = " ".join((a.snippet or "") + (a.name or "") for a in resp.anchors).lower()
    cites = " ".join(c.quoted_span.lower() for c in resp.citations)
    assert "v2" in (srcs + cites) and "v3" in (srcs + cites)   # 两实体都被代表


def test_ask_chunk_single_subquery_still_works(repo, monkeypatch):
    nb, _ = _seed_chunks(repo, ["alpha topic " * 30, "beta topic " * 30])
    import app.services.query_rewrite as qr
    monkeypatch.setattr(qr, "expand_query", lambda *a, **k: qr.ExpandedQuery(
        query="alpha", sub_queries=[qr.SubQuerySpec("alpha topic")]))
    resp = repo.ask_chunk(nb.id, AskRequest(question="alpha"))
    assert resp.citations   # 单子查询走 MMR,正常返回


def test_chunk_ann_enabled_default_on():
    """默认开:有索引的库自动走 ANN 核⊕delta;小库无索引自然回退暴力(零影响)。"""
    from app.core.config import Settings
    settings = Settings(_env_file=None)
    assert settings.chunk_ann_enabled is True
    assert settings.chunk_fts_with_ann_enabled is False


def test_report_chunk_fts_union_can_be_restored_by_env(monkeypatch):
    from app.core.config import Settings

    monkeypatch.setenv("CHUNK_FTS_WITH_ANN_ENABLED", "true")
    assert Settings(_env_file=None).chunk_fts_with_ann_enabled is True


def test_chunk_fts_backfill_and_search(repo):
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="b"))
    with repo._write() as db:
        now = "2026-07-01T00:00:00"
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("s1", nb.id, "t", "md", "ready", now, now))
        for cid, txt in [("c1", "XZQW9000 special widget spec"),
                         ("c2", "unrelated bandgap text")]:
            db.execute(
                "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (cid, nb.id, "s1", txt, "", "[]", now))
    n = repo.backfill_chunk_fts(nb.id)
    assert n == 2
    # Task 13: the chunk FTS SQL moved to KnowledgeStore (kg/search keeps
    # only pure hit merging); the primitive stays connection-taking.
    chunk_fts_search = repo._runtime.knowledge.chunk_fts_search
    with repo._connect() as db:
        hits = chunk_fts_search(db, nb.id, "XZQW9000", k=10)
    assert "c1" in {h["chunk_id"] for h in hits}   # 罕见词法词命中
    assert "c2" not in {h["chunk_id"] for h in hits}
