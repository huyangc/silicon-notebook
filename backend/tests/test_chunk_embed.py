import json
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Hermetic repo with FakeEmbedder but embedder_configured=True.
    Mirrors test_reasoning_retrieval.py::rrepo — EMBED_* MUST be set (else
    embedder_configured is False and every embed path early-returns, so chunk
    vectors never get written), and the network client is replaced by
    FakeEmbedder; LLM keys cleared so answer paths stay offline (the .env
    env_file would otherwise leak real keys)."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    for _k in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
               "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL", "REASONING_LLM_MODEL"):
        monkeypatch.setenv(_k, "")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _seed_source_with_elements(repo, texts):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    import uuid
    sid = f"src-{uuid.uuid4().hex[:8]}"
    now = "2026-01-01T00:00:00"
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (sid, nb.id, "S", "document", "s.md", "/tmp/s.md", 0, "h", "", "", "extracted", now, now))
        for i, t in enumerate(texts, 1):
            db.execute("INSERT INTO source_elements (id,source_id,element_type,location_label,text,metadata,created_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (f"el-{sid}-{i:04d}", sid, "paragraph", f"p{i}", t, "{}", now))
    return nb, sid


def test_chunk_and_embed_writes_chunks_and_vectors(repo):
    nb, sid = _seed_source_with_elements(repo, ["x"*300, "y"*300, "z"*300])
    repo._chunk_and_embed_source(sid)
    with repo._connect() as db:
        nchunks = db.execute("SELECT COUNT(*) c FROM chunks WHERE source_id=?", (sid,)).fetchone()["c"]
        nemb = db.execute("SELECT COUNT(*) c FROM chunk_embeddings WHERE notebook_id=?", (nb.id,)).fetchone()["c"]
        # element_ids 是合法 JSON 且非空
        row = db.execute("SELECT element_ids FROM chunks WHERE source_id=? LIMIT 1", (sid,)).fetchone()
    assert nchunks >= 1
    assert nemb == nchunks           # 每 chunk 一向量
    assert json.loads(row["element_ids"])


def test_chunk_matrix_loads(repo):
    nb, sid = _seed_source_with_elements(repo, ["alpha "*60, "beta "*60])
    repo._chunk_and_embed_source(sid)
    with repo._connect() as db:
        ids, mat = repo._vector_matrix(db, nb.id, "chunk_embeddings", "chunk_id")
    assert len(ids) >= 1 and mat.shape[0] == len(ids)


def test_build_chunks_idempotent(repo):
    nb, sid = _seed_source_with_elements(repo, ["x"*300, "y"*300])
    repo._chunk_and_embed_source(sid)
    repo._chunk_and_embed_source(sid)            # second run must not duplicate
    with repo._connect() as db:
        n = db.execute("SELECT COUNT(*) c FROM chunks WHERE source_id=?", (sid,)).fetchone()["c"]
        ne = db.execute("SELECT COUNT(*) c FROM chunk_embeddings WHERE notebook_id=?", (nb.id,)).fetchone()["c"]
    assert n >= 1 and ne == n                     # chunks replaced, embeddings 1:1, no orphans/dupes


def test_process_source_builds_chunks(repo, monkeypatch):
    """process_source 解析后应 INLINE 产出 chunks(轻摄取, query 立即可用)。
    chunk 构建是同步的(无网络), 故这里无需等后台 embed 线程即可断言行数。"""
    import app.services.parser_chain_execution as parser_execution
    # mock 解析: 返回固定 elements(不依赖真实文件/MinerU)
    monkeypatch.setattr(parser_execution, "parse_builtin_source_file",
                        lambda *a, **k: [type("E", (), {"element_type": "paragraph",
                                         "location_label": "p1", "text": "chunk content " * 30,
                                         "metadata": {}})()])
    # 隔离重步骤: KG 抽取/摘要置 no-op, 聚焦验证 chunk 接线本身。
    monkeypatch.setattr(repo._runtime.source_ingestion, "run_extraction", lambda *a, **k: None)
    monkeypatch.setattr(repo, "_summarize_source", lambda *a, **k: "")
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    import uuid; sid = f"src-{uuid.uuid4().hex[:8]}"; now = "2026-01-01T00:00:00"
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (sid, nb.id, "S", "document", "s.md", "/tmp/s.md", 0, "h", "", "", "queued", now, now))
    repo.process_source(sid)
    with repo._connect() as db:
        n = db.execute("SELECT COUNT(*) c FROM chunks WHERE source_id=?", (sid,)).fetchone()["c"]
    assert n >= 1
