"""大库 chunk 暴力回退守卫(镜像 #171 冷矩阵守卫哲学):

_retrieve_chunks 的 ANN 分支不可用(未建 scale 索引 / embed 失败 query_vector=None /
ANN fail-open)时会落进「_gather_chunks 全表拉文本 + score_chunks 逐 chunk 纯 Python
分词」——生产 55 万 KG 级库曾因 .env 丢失静默走到这里,单问磨半小时「思考中」。

Fix:chunk 数 > chunk_bruteforce_max_chunks(默认 20000,0=关)→ 降级为 FTS 词法
候选 + 有界打分(候选内仍关键词+语义融合),发 chunk_bruteforce_skipped 事件;
绝不 _gather_chunks 全表。小库 / 关守卫:字节不变。
"""
import json

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_PROVIDER": "dashscope", "EMBED_BASE_URL": "https://e.test",
                 "EMBED_API_KEY": "k", "EMBED_MODEL": "m", "EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _seed_chunks(repo, n):
    """n 个 chunk(文本含 'bandgap'),带向量与 FTS(直插 chunks 会绕过写路径的
    FTS 维护,故显式 backfill_chunk_fts)。未建 scale 索引 → ANN 分支自然不可用。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    now = "2026-07-01T00:00:00"
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("s1", nb.id, "t", "md", "ready", now, now))
        for i in range(n):
            txt = f"bandgap reference topic {i} body detail"
            db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                       "VALUES (?,?,?,?,?,?,?)", (f"c{i}", nb.id, "s1", txt, "", "[]", now))
            v = repo.embedder.embed_texts([txt])[0]
            db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                       (f"c{i}", nb.id, json.dumps(v), now))
    repo.backfill_chunk_fts(nb.id)
    return nb


def _capture_events(repo, monkeypatch):
    events = []
    orig_emit = repo.event_log.emit

    def spy_emit(event, **kw):
        events.append(event)
        return orig_emit(event, **kw)

    monkeypatch.setattr(repo.event_log, "emit", spy_emit)
    return events


def _spy_gather_chunks(repo, monkeypatch):
    calls = []
    orig = repo.retrieval.candidates._gather_chunks

    def wrapper(db, notebook_id):
        calls.append(notebook_id)
        return orig(db, notebook_id)

    monkeypatch.setattr(repo.retrieval.candidates, "_gather_chunks", wrapper)
    return calls


def _skipped(events):
    return [e for e in events if e.get("kind") == "chunk_bruteforce_skipped"]


def test_over_threshold_degrades_to_fts_and_never_gathers(repo, monkeypatch):
    """超阈值 + 无 ANN → FTS 降级:_gather_chunks 绝不被调、发事件、
    候选内关键词+语义融合仍召回目标 chunk。"""
    nb = _seed_chunks(repo, 3)
    monkeypatch.setattr(repo.settings, "chunk_bruteforce_max_chunks", 1)
    calls = _spy_gather_chunks(repo, monkeypatch)
    events = _capture_events(repo, monkeypatch)

    scored, ids, mat = repo.retrieval.candidates._retrieve_chunks(nb.id, "bandgap")

    assert calls == [], "guarded path must NOT full-scan chunks"
    assert {c.chunk_id for c in scored} <= {f"c{i}" for i in range(3)}
    assert len(scored) >= 1                      # FTS 词法命中 → 候选内打分召回
    assert len(ids) == len(scored) or len(ids) >= 1   # 候选向量矩阵有界
    sk = _skipped(events)
    assert len(sk) == 1
    assert sk[0]["reason"] == "large_library_no_ann"
    assert sk[0]["n_chunks"] == 3 and sk[0]["threshold"] == 1
    assert sk[0]["fts_hits"] >= 1 and sk[0]["embed_ok"] is True


def test_under_threshold_bruteforce_unchanged(repo, monkeypatch):
    """小库(≤阈值):字节不变——仍走 _gather_chunks 全量路径,不发事件。"""
    nb = _seed_chunks(repo, 3)
    monkeypatch.setattr(repo.settings, "chunk_bruteforce_max_chunks", 100)
    calls = _spy_gather_chunks(repo, monkeypatch)
    events = _capture_events(repo, monkeypatch)

    scored, ids, mat = repo.retrieval.candidates._retrieve_chunks(nb.id, "bandgap")

    assert calls == [nb.id]
    assert len(scored) >= 1
    assert _skipped(events) == []


def test_zero_threshold_disables_guard(repo, monkeypatch):
    """阈值 0 = 关守卫:任何规模都保留全量暴力(逃生口)。"""
    nb = _seed_chunks(repo, 3)
    monkeypatch.setattr(repo.settings, "chunk_bruteforce_max_chunks", 0)
    calls = _spy_gather_chunks(repo, monkeypatch)
    events = _capture_events(repo, monkeypatch)

    repo.retrieval.candidates._retrieve_chunks(nb.id, "bandgap")

    assert calls == [nb.id]
    assert _skipped(events) == []


def test_embed_failure_still_bounded(repo, monkeypatch):
    """embed 失败(query_vector=None,.env 丢失场景)+ 超阈值 → 同样走 FTS 降级
    (keyword-only 打分),不磨全库;事件 embed_ok=False。"""
    nb = _seed_chunks(repo, 3)
    monkeypatch.setattr(repo.settings, "chunk_bruteforce_max_chunks", 1)
    monkeypatch.setattr(repo.retrieval.candidates, "_embed_query", lambda q: None)
    calls = _spy_gather_chunks(repo, monkeypatch)
    events = _capture_events(repo, monkeypatch)

    scored, ids, mat = repo.retrieval.candidates._retrieve_chunks(nb.id, "bandgap")

    assert calls == []
    assert len(scored) >= 1                      # 词法命中 + keyword 分兜底
    sk = _skipped(events)
    assert len(sk) == 1 and sk[0]["embed_ok"] is False


def test_fts_miss_returns_empty_with_event(repo, monkeypatch):
    """超阈值且 FTS 零命中(查询词库内不存在)→ ([], [], None) + 事件 fts_hits=0
    (词法不可寻时宁可空手也不磨全库;上层 ask 已有空结果文案)。"""
    nb = _seed_chunks(repo, 3)
    monkeypatch.setattr(repo.settings, "chunk_bruteforce_max_chunks", 1)
    events = _capture_events(repo, monkeypatch)

    scored, ids, mat = repo.retrieval.candidates._retrieve_chunks(nb.id, "zzzqqqvvv")

    assert scored == [] and ids == [] and mat is None
    sk = _skipped(events)
    assert len(sk) == 1 and sk[0]["fts_hits"] == 0


def test_ann_path_untouched_by_guard(repo, monkeypatch):
    """已建 scale 索引(chunk ANN 可用)的大库走 ANN,不触达守卫、不发事件——
    守卫只兜「ANN 不可用」的口子。"""
    nb = _seed_chunks(repo, 3)
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    idx = repo._scale_index(nb.id, allow_stale=True)
    assert idx is not None and idx.chunk_ann_labels, "前置:须产出 chunk ANN"
    monkeypatch.setattr(repo.settings, "chunk_ann_enabled", True)
    monkeypatch.setattr(repo.settings, "chunk_bruteforce_max_chunks", 1)
    events = _capture_events(repo, monkeypatch)

    scored, ids, mat = repo.retrieval.candidates._retrieve_chunks(nb.id, "bandgap")

    assert len(scored) >= 1
    assert _skipped(events) == []
