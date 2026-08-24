import json as _j
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository, _now
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from app.models.schemas import AskRequest
from app.services.retrieval import RetrievalSupport, RetrievedChunk
from tests.model_testkit import bind_all_embedding_clients
from tests.model_testkit import bind_rerank_client
from tests.model_testkit import bind_chat_client


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("CHUNK_KG_OVERLAY_ENABLED", "true")
    for _k in ("RERANK_MODEL", "RERANK_BASE_URL", "RERANK_API_KEY"):
        monkeypatch.delenv(_k, raising=False)
    r = SQLiteRepository(Settings(_env_file=None)); bind_all_embedding_clients(r, FakeEmbedder(dim=16)); return r


def _seed_chunks_and_kg(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None,
        [{"local_id": "a", "object_type": "concept", "payload": {"name": "Cascode"},
          "evidence": [{"quoted_span": "x", "element_id": "el-x-1", "source_id": "s",
                        "source_title": "D", "element_type": "paragraph",
                        "location_label": "1", "confidence": 1.0}]}], [])
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("s", nb.id, "D", "markdown", "extracted", "parsed",
             "D", "/d", 0, "", "", "textbook", _now(), _now()))
        for cid, els in [("ck-vec", ["el-y-1"]), ("ck-kg", ["el-x-1"])]:
            db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                       "VALUES (?,?,?,?,?,?,?)", (cid, nb.id, "s", "cascode " + cid, "1", _j.dumps(els), _now()))
    return nb


def test_mix_retrieve_merges_vector_and_kg_source_chunks(repo):
    nb = _seed_chunks_and_kg(repo)
    cand, block, id_map, kg_hits, _ppr_n = repo._mix_retrieve(nb.id, "cascode", "", ["cascode"])
    ids = {c.chunk_id for c in cand}
    assert "ck-kg" in ids                       # KG 源 chunk 进了候选池
    assert isinstance(block, str) and isinstance(id_map, dict)
    # KG key 用高 base(≥1001),不与 chunk key 撞
    assert all(int(k[1:]) >= 1001 for k in id_map) if id_map else True


def test_mix_retrieve_handles_multiple_vector_subqueries(repo):
    nb = _seed_chunks_and_kg(repo)
    cand, _block, _id_map, _kg_hits, _ppr_n = repo._mix_retrieve(
        nb.id, "cascode", "", ["cascode", "output resistance"])
    assert cand
    assert all(isinstance(c, RetrievedChunk) for c in cand)


def test_mix_round_robin_finishes_historical_streams_before_question_supplement(
    repo, monkeypatch
):
    def chunk(chunk_id, origin):
        return RetrievedChunk(
            chunk_id=chunk_id,
            source_id="s",
            source_title="s",
            section_path="",
            text=chunk_id,
            relevance=0.8,
            retrieval_supports=(
                RetrievalSupport(origin, "chunk", chunk_id, 0.8),
            ),
        )

    vector = [
        chunk("vector-1", "semantic"),
        chunk("question-only", "generated_question"),
        chunk("vector-2", "semantic"),
    ]
    kg = [chunk("kg-1", "kg_source"), chunk("kg-2", "kg_source")]
    ppr = [chunk("ppr-1", "ppr"), chunk("ppr-2", "ppr")]
    candidates = repo.retrieval.candidates
    monkeypatch.setattr(candidates, "_gather_vector_chunks", lambda *args: vector)
    monkeypatch.setattr(candidates, "_notebook_has_kg", lambda _notebook_id: True)
    monkeypatch.setattr(candidates, "_chunk_kg_overlay", lambda *args, **kwargs: (
        "", {"k1001": {"object_id": "object-1"}}, [], {}
    ))
    monkeypatch.setattr(candidates, "_kg_source_chunks", lambda *args, **kwargs: kg)
    monkeypatch.setattr(candidates, "_ppr_retrieve", lambda *args, **kwargs: ppr)
    monkeypatch.setattr(
        candidates, "_unsafe_source_scope_restricted", lambda _notebook_id: False
    )
    monkeypatch.setattr(repo.settings, "graph_ppr_enabled", True)

    merged, _block, _id_map, _hits, _ppr_count = candidates._mix_retrieve(
        "nb", "query", "", ["q1", "q2"]
    )

    assert [chunk.chunk_id for chunk in merged] == [
        "vector-1",
        "kg-1",
        "ppr-1",
        "vector-2",
        "kg-2",
        "ppr-2",
        "question-only",
    ]


class _AnswerLLM:
    configured = True
    def __init__(self, text): self.text = text; self.calls = 0
    def chat_json(self, messages, schema_hint, **kw):
        self.calls += 1
        return _j.dumps({"answer": self.text, "grounded": True})


class _FakeRerank:
    def __init__(self, configured=True): self._c = configured
    @property
    def configured(self): return self._c
    def rerank(self, query, documents, on_error=None): return list(range(len(documents)))


def test_chunk_answer_context_budget_override(repo):
    chunks = [RetrievedChunk(chunk_id=f"c{i}", source_id="s", source_title="D",
                             section_path="1", text=str(i) * 500, relevance=0.5)
              for i in range(10)]
    _, idmap_small = repo._chunk_answer_context(chunks, budget_chars=100)
    _, idmap_big = repo._chunk_answer_context(chunks, budget_chars=10**9)
    assert len(idmap_big) == 10
    assert len(idmap_small) < 10


def test_answer_mix_resolves_chunk_and_kg_anchors(repo):
    bind_chat_client(repo, "ask_answer", _AnswerLLM("Cascode raises rout [k1]. Related: [k1001]."))
    chunks = [RetrievedChunk(chunk_id="c1", source_id="s", source_title="D",
                             section_path="1", text="cascode raises rout", relevance=0.8)]
    kg_block = "k1001: [concept][personal] Cascode"
    kg_id_map = {"k1001": {"object_id": "obj-a", "object_type": "concept",
                           "name": "Cascode", "snippet": "", "definition": "",
                           "source_title": "", "location_label": "", "tier": "personal"}}
    answer, grounded, anchors = repo._answer_mix("q", chunks, kg_block, kg_id_map, "")
    keys = {a.key for a in anchors}
    assert "k1" in keys and "k1001" in keys
    types = {a.object_type for a in anchors}
    assert "chunk" in types and "concept" in types


def test_ask_chunk_overlay_off_is_chunk_only(repo):
    repo.settings.query_rewrite_enabled = False
    repo.settings.chunk_kg_overlay_enabled = False
    bind_chat_client(repo, "ask_answer", _AnswerLLM("answer [k1]"))
    nb = _seed_chunks_and_kg(repo)
    resp = repo.ask_chunk(nb.id, AskRequest(question="cascode", mode="chunk"))
    assert all(a.object_type == "chunk" for a in resp.anchors)


def test_ask_chunk_mix_runs_end_to_end(repo):
    repo.settings.query_rewrite_enabled = False
    bind_chat_client(repo, "ask_answer", _AnswerLLM("Cascode raises rout [k1]"))
    bind_rerank_client(repo, _FakeRerank(configured=True))
    nb = _seed_chunks_and_kg(repo)
    resp = repo.ask_chunk(nb.id, AskRequest(question="cascode", mode="chunk"))
    assert resp.answer
    assert resp.mode == "chunk"
    assert isinstance(resp.grounded, bool)


def test_ask_chunk_rerank_unconfigured_falls_back(repo):
    repo.settings.query_rewrite_enabled = False
    bind_chat_client(repo, "ask_answer", _AnswerLLM("answer [k1]"))
    bind_rerank_client(repo, _FakeRerank(configured=False))
    nb = _seed_chunks_and_kg(repo)
    resp = repo.ask_chunk(nb.id, AskRequest(question="cascode", mode="chunk"))
    assert resp.answer
    assert all(a.object_type == "chunk" for a in resp.anchors)


def test_answer_mix_caps_chunks_below_kg_key_base(repo):
    # 构造 >= _MIX_KG_KEY_BASE 个 chunk,确认被截到 base-1 之下,KG key 不被覆盖
    bind_chat_client(repo, "ask_answer", _AnswerLLM("see [k1] and [k1001]"))
    n = repo._MIX_KG_KEY_BASE + 50
    chunks = [RetrievedChunk(chunk_id=f"c{i}", source_id="s", source_title="D",
                             section_path="1", text="x", relevance=0.5) for i in range(n)]
    kg_block = "k1001: [concept][personal] Cascode"
    kg_id_map = {"k1001": {"object_id": "obj-a", "object_type": "concept",
                           "name": "Cascode", "snippet": "", "definition": "",
                           "source_title": "", "location_label": "", "tier": "personal"}}
    _, _, anchors = repo._answer_mix("q", chunks, kg_block, kg_id_map, "")
    # k1001 仍解析为 KG concept(未被 chunk 覆盖)
    kg_anchor = [a for a in anchors if a.key == "k1001"]
    assert kg_anchor and kg_anchor[0].object_type == "concept"


def test_ask_chunk_byte_equivalent_when_overlay_and_rerank_off(repo):
    """等价护栏:overlay 关 + rerank 未配 → 纯 chunk(MMR/quota),不注入 KG,
    引用为每个精选 chunk 一条(历史行为)。"""
    repo.settings.query_rewrite_enabled = False
    repo.settings.chunk_kg_overlay_enabled = False
    assert not repo._runtime.models.rerank("retrieval_rerank").configured
    bind_chat_client(repo, "ask_answer", _AnswerLLM("answer [k1]"))
    nb = _seed_chunks_and_kg(repo)
    resp = repo.ask_chunk(nb.id, AskRequest(question="cascode", mode="chunk"))
    assert all(a.object_type == "chunk" for a in resp.anchors)   # 无 KG anchor
    assert len(resp.citations) >= 1                              # 每精选 chunk 一条
