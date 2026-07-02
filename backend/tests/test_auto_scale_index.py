"""大库自动建检索索引:maybe_auto_index + 写路径/读路径两类触发点 + 配置开关。

复用「大」的定义 = notebook_copy_stats()["copyable"] is False(与分享/拷贝阈值一致,
sqlite_repository.py:1379-1392)。入队走既有 trigger_scale_index_rebuild,自带
_scale_building/_scale_idle_queue 去重 —— 本文件只测 maybe_auto_index 本身的判定/
调用契约,不重测 trigger 内部去重(已在 test_scale_index_repo.py 覆盖)。
"""
import json
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_PROVIDER": "dashscope", "EMBED_BASE_URL": "https://e.test",
                 "EMBED_API_KEY": "k", "EMBED_MODEL": "m", "EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _seed_nb_with_chunk(repo, name="nb"):
    """A tiny notebook with one source/chunk/concept so eligibility (chunk-count
    based) can be tuned via notebook_copy_max_rows / index_suggest_chunk_threshold
    without needing thousands of real rows."""
    nb = repo.create_notebook(NotebookCreate(name=name))
    with repo._write() as db:
        now = "2026-07-01T00:00:00"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", (f"s-{nb.id}", nb.id, "t", "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)", (f"c-{nb.id}", nb.id, f"s-{nb.id}", "x", "", "[]", now))
        v = repo.embedder.embed_texts(["x"])[0]
        db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                   (f"c-{nb.id}", nb.id, json.dumps(v), now))
    return nb


def test_large_nb_upload_triggers_idle_enqueue(repo, monkeypatch):
    """copyable=False(一切库皆「大」)+ suggest 阈值也压低 → maybe_auto_index 应
    调用 trigger_scale_index_rebuild(when='idle')一次。"""
    monkeypatch.setattr(repo.settings, "notebook_copy_max_rows", 0)
    monkeypatch.setattr(repo.settings, "index_suggest_chunk_threshold", 0)
    nb = _seed_nb_with_chunk(repo)
    calls = []
    monkeypatch.setattr(repo, "trigger_scale_index_rebuild",
                         lambda nbid, when="now", mode="auto": calls.append((nbid, when, mode)))
    repo.maybe_auto_index(nb.id)
    assert len(calls) == 1
    assert calls[0][0] == nb.id
    assert calls[0][1] == "idle"


def test_small_nb_no_trigger(repo, monkeypatch):
    """默认阈值下的小库 → 不触发。"""
    nb = _seed_nb_with_chunk(repo)
    calls = []
    monkeypatch.setattr(repo, "trigger_scale_index_rebuild",
                         lambda nbid, when="now", mode="auto": calls.append((nbid, when, mode)))
    repo.maybe_auto_index(nb.id)
    assert calls == []


def test_indexed_fresh_no_trigger(repo, monkeypatch):
    """大库但索引已存在且新鲜(state=indexed)→ 不触发。"""
    monkeypatch.setattr(repo.settings, "notebook_copy_max_rows", 0)
    monkeypatch.setattr(repo.settings, "index_suggest_chunk_threshold", 0)
    nb = _seed_nb_with_chunk(repo)
    # rebuild_unified_kg's own auto-index tail would otherwise queue a build here
    # (state=suggested pre-index) — stub it out so this test can build the index
    # manually and observe a clean "indexed" state before exercising maybe_auto_index.
    monkeypatch.setattr(repo, "trigger_scale_index_rebuild", lambda *a, **k: None)
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    assert repo.scale_index_status(nb.id)["state"] == "indexed"
    calls = []
    monkeypatch.setattr(repo, "trigger_scale_index_rebuild",
                         lambda nbid, when="now", mode="auto": calls.append((nbid, when, mode)))
    repo._auto_index_checked.discard(nb.id)  # force re-evaluation past once-set
    repo.maybe_auto_index(nb.id)
    assert calls == []


def test_auto_disabled_no_trigger(repo, monkeypatch):
    monkeypatch.setattr(repo.settings, "notebook_copy_max_rows", 0)
    monkeypatch.setattr(repo.settings, "index_suggest_chunk_threshold", 0)
    monkeypatch.setattr(repo.settings, "scale_index_auto_enabled", False)
    nb = _seed_nb_with_chunk(repo)
    calls = []
    monkeypatch.setattr(repo, "trigger_scale_index_rebuild",
                         lambda nbid, when="now", mode="auto": calls.append((nbid, when, mode)))
    repo.maybe_auto_index(nb.id)
    assert calls == []


def test_retrieval_fallback_triggers_once(repo, monkeypatch):
    """大库无索引,连续两次走 KG 对象检索(_retrieve_scored,无 ANN 核 → 全量回退)
    → maybe_auto_index 只实际评估/入队一次(once-set 生效);且检索返回值不受影响。"""
    monkeypatch.setattr(repo.settings, "notebook_copy_max_rows", 0)
    monkeypatch.setattr(repo.settings, "index_suggest_chunk_threshold", 0)
    nb = _seed_nb_with_chunk(repo)
    with repo._write() as db:
        now = "2026-07-01T00:00:00"
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,"
                   "evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("o1", nb.id, "concept", "approved", "", json.dumps({"name": "MOSFET"}),
                    "[]", f"s-{nb.id}", now, now))
    calls = []
    monkeypatch.setattr(repo, "trigger_scale_index_rebuild",
                         lambda nbid, when="now", mode="auto": calls.append((nbid, when, mode)))
    out1 = repo._retrieve_scored(nb.id, "MOSFET")
    out2 = repo._retrieve_scored(nb.id, "MOSFET")
    assert len(calls) == 1
    assert isinstance(out1, list) and isinstance(out2, list)


def test_when_now_spawns_build(repo, monkeypatch):
    monkeypatch.setattr(repo.settings, "notebook_copy_max_rows", 0)
    monkeypatch.setattr(repo.settings, "index_suggest_chunk_threshold", 0)
    monkeypatch.setattr(repo.settings, "scale_index_auto_when", "now")
    nb = _seed_nb_with_chunk(repo)
    calls = []
    monkeypatch.setattr(repo, "trigger_scale_index_rebuild",
                         lambda nbid, when="now", mode="auto": calls.append((nbid, when, mode)))
    repo.maybe_auto_index(nb.id)
    assert len(calls) == 1
    assert calls[0][1] == "now"


def test_once_set_blocks_repeat_then_dirty_rearms(repo, monkeypatch):
    """写路径(_mark_unified_kg_dirty)每次 KG 变更都应 discard 该 nb,让下一轮上传
    重新评估(即便上一轮判定「不需要」已入 once-set)。"""
    nb = _seed_nb_with_chunk(repo)
    calls = []
    monkeypatch.setattr(repo, "trigger_scale_index_rebuild",
                         lambda nbid, when="now", mode="auto": calls.append((nbid, when, mode)))
    repo.maybe_auto_index(nb.id)  # small nb -> no trigger, but added to once-set
    assert nb.id in repo._auto_index_checked
    repo._mark_unified_kg_dirty(nb.id)
    assert nb.id not in repo._auto_index_checked


def test_ineligible_or_in_progress_trigger_exception_swallowed(repo, monkeypatch):
    """trigger_scale_index_rebuild 抛异常(不 eligible/并发冲突等)时,maybe_auto_index
    必须吞掉、不向上抛出。"""
    monkeypatch.setattr(repo.settings, "notebook_copy_max_rows", 0)
    monkeypatch.setattr(repo.settings, "index_suggest_chunk_threshold", 0)
    nb = _seed_nb_with_chunk(repo)

    def _boom(nbid, when="now", mode="auto"):
        raise ValueError("notebook too small and not base-tier; scale index not applicable")

    monkeypatch.setattr(repo, "trigger_scale_index_rebuild", _boom)
    repo.maybe_auto_index(nb.id)  # must not raise
    assert nb.id in repo._auto_index_checked


def test_scale_index_auto_when_literal_valid_values(monkeypatch):
    """SCALE_INDEX_AUTO_WHEN 合法取值(idle/now)都应正常构造。"""
    monkeypatch.setenv("SCALE_INDEX_AUTO_WHEN", "now")
    s = Settings()
    assert s.scale_index_auto_when == "now"


def test_scale_index_auto_when_literal_rejects_invalid(monkeypatch):
    """非法取值(如拼错的 SCALE_INDEX_AUTO_WHEN=nwo)必须在 Settings() 构造期就
    ValidationError 快速失败,而不是静默落入 trigger_scale_index_rebuild 的 "now"
    分支(该分支是立即后台重建,代价远高于预期的 "idle" 低峰重建)。"""
    monkeypatch.setenv("SCALE_INDEX_AUTO_WHEN", "nwo")
    with pytest.raises(ValidationError):
        Settings()


def test_large_chunk_light_unindexed_triggers(repo, monkeypatch):
    """大库(按 copyable 定义)但 chunk 数未过 index_suggest_chunk_threshold(即
    scale_index_status 判定为 unindexed)也应触发 —— 产品意图是「大 → 自动建」,
    大的定义(字节/chunks+nodes)与 chunk 阈值是两把不同的尺子,不应让 chunk 少
    的大库永远停在 unindexed 不触发。"""
    monkeypatch.setattr(repo.settings, "notebook_copy_max_rows", 0)  # 一律 copyable=False("大")
    # index_suggest_chunk_threshold 保持默认(高),使 scale_index_status 判定为 unindexed
    nb = _seed_nb_with_chunk(repo)
    assert repo.scale_index_status(nb.id)["state"] == "unindexed"
    calls = []
    monkeypatch.setattr(repo, "trigger_scale_index_rebuild",
                         lambda nbid, when="now", mode="auto": calls.append((nbid, when, mode)))
    repo.maybe_auto_index(nb.id)
    assert len(calls) == 1
    assert calls[0][0] == nb.id


def test_batch_burst_o1_early_exit_skips_copy_stats(repo, monkeypatch):
    """批量摄取模拟:第一次 maybe_auto_index 入队(idle)后,清空 once-set(模拟
    _mark_unified_kg_dirty 的逐源 discard),第二次调用必须命中 _scale_building/
    _scale_idle_queue 的 O(1) 早退,不再调用 notebook_copy_stats(避免每源都重跑
    5 个 COUNT + scale_index_status)。"""
    monkeypatch.setattr(repo.settings, "notebook_copy_max_rows", 0)
    monkeypatch.setattr(repo.settings, "index_suggest_chunk_threshold", 0)
    nb = _seed_nb_with_chunk(repo)

    # First call: real trigger_scale_index_rebuild runs (when=idle -> queued), no stubbing.
    repo.maybe_auto_index(nb.id)
    assert nb.id in repo._scale_idle_queue

    # Simulate the per-source once-set discard from _mark_unified_kg_dirty.
    repo._auto_index_checked.discard(nb.id)

    calls = []
    orig = repo.notebook_copy_stats
    monkeypatch.setattr(repo, "notebook_copy_stats",
                         lambda *a, **k: calls.append(a) or orig(*a, **k))
    repo.maybe_auto_index(nb.id)
    assert calls == []
    assert nb.id in repo._auto_index_checked
