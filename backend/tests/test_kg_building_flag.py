"""WS1: KG 重抽的进行中标志 self._kg_building —— get_notebook 实时回填 kg_building，
build_notebook_kg 期间置位、finally 清位（重启后集合天然空=未构建，无需 reconcile）。"""
import types
import pytest

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


def test_get_notebook_reflects_kg_building_set(repo):
    nb = repo.create_notebook(NotebookCreate(name="t"))
    assert repo.get_notebook(nb.id).kg_building is False
    repo._kg_building.add(nb.id)
    assert repo.get_notebook(nb.id).kg_building is True
    repo._kg_building.discard(nb.id)
    assert repo.get_notebook(nb.id).kg_building is False


def test_kg_building_set_during_build_and_cleared_after(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="t"))
    repo.llm_client = types.SimpleNamespace(configured=True)  # 无 sources → 不真抽取
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
    repo.llm_client = types.SimpleNamespace(configured=False)  # 入口即 RuntimeError
    with pytest.raises(RuntimeError):
        repo.build_notebook_kg(nb.id)
    assert nb.id not in repo._kg_building          # 异常路径 finally 仍清位
    assert repo.get_notebook(nb.id).kg_building is False


def test_kg_building_set_during_rebuild_delete_phase(repo, monkeypatch):
    """rebuild=delete+build：标志必须覆盖 delete 阶段（否则大库 delete>6s 时前端轮询过早停）。"""
    nb = repo.create_notebook(NotebookCreate(name="t"))
    repo.llm_client = types.SimpleNamespace(configured=True)  # build 不 RuntimeError；无 sources → 快
    seen = {}
    orig_delete = repo._runtime.knowledge_lifecycle.delete_notebook_kg
    def spy_delete(nid):
        seen["during_delete"] = nid in repo._kg_building
        return orig_delete(nid)
    monkeypatch.setattr(repo._runtime.knowledge_lifecycle, "delete_notebook_kg", spy_delete)
    repo.rebuild_notebook_kg(nb.id)
    assert seen["during_delete"] is True          # delete 阶段标志已置位
    assert nb.id not in repo._kg_building           # rebuild 结束后清位
    assert repo.get_notebook(nb.id).kg_building is False
