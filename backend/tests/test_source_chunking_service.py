# backend/tests/test_source_chunking_service.py
"""Task 11: SourceChunkingService — chunk construction (build_chunks_for_source
/ chunk_and_embed_source) extracted off the facade.

Invariants under test:
- chunk ids keep the ``ck-`` surrogate prefix and are minted through the
  module ``_new_id`` seam (deterministic-fixture replay depends on it);
- the split honours settings.chunk_target_chars / chunk_overlap_chars and
  element_ids stay non-empty JSON;
- every rebuild bumps the KG dirty counter through the facade's
  ``_mark_unified_kg_dirty`` seat (late-bound: per-instance monkeypatches
  keep observing it);
- chunk replacement stays atomic: an FTS insert failure rolls the whole
  replacement back and the previous chunk rows survive;
- chunk_and_embed_source composes build + embed (one vector per chunk).
"""
import json

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services import sqlite_repository
from app.services.embedding import FakeEmbedder
from app.services.source_chunking import SourceChunkingService
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = sqlite_repository.SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _seed_source_with_elements(repo, texts, element_type="paragraph"):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    import uuid

    sid = f"src-{uuid.uuid4().hex[:8]}"
    now = "2026-01-01T00:00:00"
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
            "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, nb.id, "S", "document", "s.md", "/tmp/s.md", 0, "h", "", "", "extracted", now, now))
        for i, t in enumerate(texts, 1):
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,location_label,text,metadata,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"el-{sid}-{i:04d}", sid, element_type, f"p{i}", t, "{}", now))
    return nb, sid


def _chunk_rows(repo, sid):
    with repo._connect() as db:
        return db.execute(
            "SELECT id, element_ids FROM chunks WHERE source_id=? ORDER BY id", (sid,)
        ).fetchall()


# ------------------------------------------------------------- composition
def test_runtime_composes_chunking_service(repo):
    service = repo._runtime.source_chunking
    assert isinstance(service, SourceChunkingService)
    expected = {"build_chunks_for_source", "chunk_and_embed_source"}
    assert expected <= set(SourceChunkingService.__dict__)
    assert "__getattr__" not in SourceChunkingService.__dict__


# ----------------------------------------------------------- id + split pins
def test_build_chunks_mints_ck_ids_with_json_element_ids(repo):
    nb, sid = _seed_source_with_elements(repo, ["x" * 300, "y" * 300, "z" * 300])

    repo._build_chunks_for_source(sid)

    rows = _chunk_rows(repo, sid)
    assert rows                                              # 900 chars / 600 target → >1 chunk
    assert len(rows) >= 2
    assert all(row["id"].startswith("ck-") for row in rows)
    assert all(json.loads(row["element_ids"]) for row in rows)


def test_build_chunks_id_minting_rides_module_seam(repo, monkeypatch):
    nb, sid = _seed_source_with_elements(repo, ["a" * 300])
    minted = []
    real_new_id = sqlite_repository._new_id

    def recording_new_id(prefix):
        value = real_new_id(prefix)
        minted.append((prefix, value))
        return value

    monkeypatch.setattr(sqlite_repository, "_new_id", recording_new_id)

    repo._build_chunks_for_source(sid)

    assert [prefix for prefix, _ in minted].count("ck") == len(_chunk_rows(repo, sid))


def test_build_chunks_marks_unified_dirty_via_facade_seat(repo, monkeypatch):
    nb, sid = _seed_source_with_elements(repo, ["dirty bump " * 40])
    marked = []
    real_mark = repo._mark_unified_kg_dirty

    def spy_mark(notebook_id):
        marked.append(notebook_id)
        return real_mark(notebook_id)

    monkeypatch.setattr(repo, "_mark_unified_kg_dirty", spy_mark)

    repo._build_chunks_for_source(sid)

    assert marked == [nb.id]


# ----------------------------------------------------------- atomic replace
def test_replace_failure_keeps_previous_chunks_intact(repo, monkeypatch):
    nb, sid = _seed_source_with_elements(repo, ["alpha " * 60, "beta " * 60])
    repo._build_chunks_for_source(sid)
    before = {row["id"] for row in _chunk_rows(repo, sid)}
    assert before

    def broken_fts(connection, rows):
        raise RuntimeError("fts exploded")

    monkeypatch.setattr(repo._runtime.chunk_store, "_insert_fts_rows", broken_fts)

    with pytest.raises(RuntimeError, match="fts exploded"):
        repo._build_chunks_for_source(sid)

    assert {row["id"] for row in _chunk_rows(repo, sid)} == before


# --------------------------------------------------------------- composition
def test_chunk_and_embed_composes_build_then_embed(repo):
    nb, sid = _seed_source_with_elements(repo, ["gamma " * 60, "delta " * 60])

    repo._runtime.source_chunking.chunk_and_embed_source(sid)

    with repo._connect() as db:
        nchunks = db.execute(
            "SELECT COUNT(*) c FROM chunks WHERE source_id=?", (sid,)
        ).fetchone()["c"]
        nvec = db.execute(
            "SELECT COUNT(*) c FROM chunk_embeddings WHERE notebook_id=?", (nb.id,)
        ).fetchone()["c"]
    assert nchunks >= 1
    assert nvec == nchunks


# ----------------------------------------- chunked_at completion marker (H3)
def _chunked_at(repo, sid):
    with repo._connect() as db:
        return db.execute(
            "SELECT chunked_at FROM sources WHERE id=?", (sid,)
        ).fetchone()["chunked_at"]


def _counts(repo, sid):
    with repo._connect() as db:
        n_el = db.execute(
            "SELECT COUNT(*) c FROM source_elements WHERE source_id=?", (sid,)
        ).fetchone()["c"]
        n_ck = db.execute(
            "SELECT COUNT(*) c FROM chunks WHERE source_id=?", (sid,)
        ).fetchone()["c"]
    return n_el, n_ck


def _h3_real_damage(repo, sid):
    """P2 H3 的持久层真损坏判据: elements>0 AND chunks=0 AND chunked_at IS NULL.
    (在途内存租约过滤是 P2 的 Python 后置步骤,不属于本层——这里只测持久层的
    可判定性。)"""
    n_el, n_ck = _counts(repo, sid)
    return n_el > 0 and n_ck == 0 and _chunked_at(repo, sid) is None


def test_zero_chunk_success_sets_chunked_at(repo):
    # 纯标题 md: heading-only elements -> build_chunks 返回 [] -> 0 chunk, 但分块
    # 本身成功 -> chunked_at 打上时刻。这是 build_chunks_for_source 直线代码、无
    # early-return 的确证: 0-chunk 路径也走到 replace_source_chunks(mark_chunked_at=)
    # 的原子打标。
    nb, sid = _seed_source_with_elements(repo, ["A Pure Title"], element_type="heading")
    repo._build_chunks_for_source(sid)
    assert _counts(repo, sid) == (1, 0)         # elements>0, 0 chunk
    assert _chunked_at(repo, sid) is not None    # 分块成功 -> 标记置位


def test_h3_chunked_at_decides_zero_chunk_success_vs_failure(repo, monkeypatch):
    """全设计要证的核心: 两个 parse_status/elements/chunks 完全相同的源(都是
    extracted + 1 element + 0 chunk),只有 chunked_at 相反; H3 真损坏判据只命中
    '分块失败'那支、不命中'分块成功产 0 chunk'那支。"""
    nb_ok, sid_ok = _seed_source_with_elements(repo, ["Title X"], element_type="heading")
    nb_bad, sid_bad = _seed_source_with_elements(repo, ["Title X"], element_type="heading")

    # #1 分块成功产 0 chunk (真实 build_chunks 于纯标题输入)。
    repo._build_chunks_for_source(sid_ok)

    # #2 分块失败: monkeypatch build_chunks 抛异常。process_source 用 best-effort
    # except 吞掉它(:586),这里镜像那次吞咽。
    from app.services import source_chunking as sc_mod

    def _boom(*_a, **_k):
        raise RuntimeError("chunk boom")

    monkeypatch.setattr(sc_mod, "build_chunks", _boom)
    with pytest.raises(RuntimeError, match="chunk boom"):
        repo._build_chunks_for_source(sid_bad)

    # A4 能观测到的两个维度上两支完全相同:
    assert _counts(repo, sid_ok) == _counts(repo, sid_bad) == (1, 0)
    # chunked_at 是区分两支的那一维:
    assert _chunked_at(repo, sid_ok) is not None
    assert _chunked_at(repo, sid_bad) is None
    # 判据只命中真失败:
    assert _h3_real_damage(repo, sid_ok) is False
    assert _h3_real_damage(repo, sid_bad) is True
