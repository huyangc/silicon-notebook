import json

import pytest
from app.services.retrieval import score_chunks, RetrievedChunk
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository, _now
from app.services.embedding import FakeEmbedder
from app.models.schemas import AskRequest, NotebookCreate


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


def test_score_chunks_uses_semantic_sims():
    chunks = [_ck("c1", "no keyword overlap here")]
    # 仅语义信号(关键词 0): chunk_sims 给高余弦 → 仍能过 floor。
    out = score_chunks("totally different words", chunks,
                       query_vector=[0.1]*4, chunk_sims={"c1": 0.9}, limit=10)
    assert [c.chunk_id for c in out] == ["c1"]
    assert out[0].relevance >= 0.5


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")
    for _k in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
               "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL", "REASONING_LLM_MODEL"):
        monkeypatch.setenv(_k, "")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
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
    repo.llm_client = _FakeLLM("DeepSeek V3 uses MoE routing [k1].")
    resp = repo.ask_chunk(nb.id, AskRequest(question="deepseek experts"))
    assert resp.answer and resp.anchors
    a = resp.anchors[0]
    assert a.object_type == "chunk" and a.object_id.startswith("ck-")
    assert resp.conclusion and "[k1]" not in resp.conclusion   # 标记已剥离


def test_ask_routes_default_mode_to_chunk(repo, monkeypatch):
    sentinel = object()
    monkeypatch.setattr(repo, "ask_chunk", lambda nb, p: sentinel)
    # AskRequest() 默认 mode 应为 "chunk" → ask() 分发到 ask_chunk
    assert AskRequest(question="x").mode == "chunk"
    assert repo.ask("nb-irrelevant", AskRequest(question="x")) is sentinel
