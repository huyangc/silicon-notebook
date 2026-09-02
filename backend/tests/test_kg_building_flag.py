"""WS1: KG 重抽的进行中标志 self._kg_building —— get_notebook 实时回填 kg_building，
build_notebook_kg 期间置位、finally 清位（重启后集合天然空=未构建，无需 reconcile）。"""
import types
import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_all_embedding_clients
from tests.model_testkit import bind_chat_client


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def test_get_notebook_reflects_kg_building_set(repo):
    nb = repo.create_notebook(NotebookCreate(name="t"))
    assert repo.get_notebook(nb.id).kg_building is False
    repo._kg_building.add(nb.id)
    assert repo.get_notebook(nb.id).kg_building is True
    repo._kg_building.discard(nb.id)
    assert repo.get_notebook(nb.id).kg_building is False


def test_kg_building_set_during_build_and_cleared_after(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="t"))
    bind_chat_client(repo, "kg_extract", _ProbeLLM())  # 无 sources，仅执行入口探测
    seen = {}
    orig = repo._mark_unified_kg_dirty
    def spy(nid):
        seen["during"] = nid in repo._kg_building
        return orig(nid)
    monkeypatch.setattr(repo, "_mark_unified_kg_dirty", spy)
    repo.build_notebook_kg(nb.id)
    assert seen["during"] is True                 # 构建期间置位
    assert nb.id not in repo._kg_building          # finally 清位
    assert repo.get_notebook(nb.id).kg_building is False


def test_kg_building_cleared_on_failure(repo):
    nb = repo.create_notebook(NotebookCreate(name="t"))
    bind_chat_client(repo, "kg_extract", types.SimpleNamespace(configured=False))  # 入口即 RuntimeError
    with pytest.raises(RuntimeError):
        repo.build_notebook_kg(nb.id)
    assert nb.id not in repo._kg_building          # 异常路径 finally 仍清位
    assert repo.get_notebook(nb.id).kg_building is False


def test_kg_building_set_during_rebuild_delete_phase(repo, monkeypatch):
    """rebuild=delete+build：标志必须覆盖 delete 阶段（否则大库 delete>6s 时前端轮询过早停）。"""
    nb = repo.create_notebook(NotebookCreate(name="t"))
    bind_chat_client(repo, "kg_extract", _ProbeLLM())  # 无 sources，仅执行入口探测
    seen = {}
    orig_delete = repo._runtime.knowledge_lifecycle.delete_notebook_kg
    def spy_delete(nid, **kwargs):
        seen["during_delete"] = nid in repo._kg_building
        return orig_delete(nid, **kwargs)
    monkeypatch.setattr(repo._runtime.knowledge_lifecycle, "delete_notebook_kg", spy_delete)
    repo.rebuild_notebook_kg(nb.id)
    assert seen["during_delete"] is True          # delete 阶段标志已置位
    assert nb.id not in repo._kg_building           # rebuild 结束后清位
    assert repo.get_notebook(nb.id).kg_building is False


def test_get_notebook_reflects_paper_meta_backfilling(repo):
    """summary.paper_meta_backfilling 镜像 kg_building 的 wiring：反映
    SourceIngestionService 进程内 _paper_meta_backfilling dict 的 membership
    （paper-meta backfill status Task 1 建的 dict + facade delegate；Task 2 把它
    接进 NotebookSummary，get_notebook 实时回填）。"""
    nb = repo.create_notebook(NotebookCreate(name="t"))
    svc = repo._runtime.source_ingestion
    assert repo.get_notebook(nb.id).paper_meta_backfilling is False
    with svc._paper_meta_backfilling_lock:
        svc._paper_meta_backfilling[nb.id] = {"total": 3, "done": 1}
    try:
        assert repo.get_notebook(nb.id).paper_meta_backfilling is True
    finally:
        with svc._paper_meta_backfilling_lock:
            svc._paper_meta_backfilling.pop(nb.id, None)
    assert repo.get_notebook(nb.id).paper_meta_backfilling is False


def test_paper_meta_backfilling_guard_when_source_ingestion_missing(repo):
    """catalog.source_ingestion 未接线（尚未 wire_source_ingestion）时,
    _paper_meta_backfilling helper 走 `is not None` 短路→安全返回 False,
    不能 AttributeError."""
    import weakref
    nb = repo.create_notebook(NotebookCreate(name="t"))
    catalog = repo._runtime.catalog
    catalog.source_ingestion = None                       # 未接线分支
    assert repo.get_notebook(nb.id).paper_meta_backfilling is False

    class _Doomed:
        pass
    d = _Doomed()
    dead_ref = weakref.ref(d)
    del d
    assert dead_ref() is None                             # 确认 GC 掉了
    catalog.source_ingestion = dead_ref                   # 弱引用已死分支
    assert repo.get_notebook(nb.id).paper_meta_backfilling is False


def test_get_notebook_hydrates_latest_durable_kg_job(repo):
    nb = repo.create_notebook(NotebookCreate(name="n"))
    job = repo._runtime.kg_build_jobs.create_job(
        nb.id,
        "user-local",
        "incremental",
        5,
    )
    summary = repo.get_notebook(nb.id)
    assert summary.kg_building is True
    assert summary.kg_build is not None
    assert summary.kg_build.job_id == job["id"]
    assert summary.kg_build.stage == "probing"


def test_terminal_durable_job_does_not_keep_building_true(repo):
    nb = repo.create_notebook(NotebookCreate(name="n"))
    job = repo._runtime.kg_build_jobs.create_job(
        nb.id,
        "user-local",
        "incremental",
        5,
    )
    repo._runtime.kg_build_jobs.finish(
        job["id"],
        "failed",
        error_code="model_unavailable",
        error_message="safe message",
    )
    summary = repo.get_notebook(nb.id)
    assert summary.kg_building is False
    assert summary.kg_build is not None
    assert summary.kg_build.status == "failed"
    assert summary.kg_build.user_message == "safe message"


class _ProbeLLM:
    configured = True

    def chat_json(self, messages, response_schema_hint, **kwargs):
        return '{"ok":true}'
