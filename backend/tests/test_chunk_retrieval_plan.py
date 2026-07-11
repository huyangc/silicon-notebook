"""W2.2 Step 1: ChunkRetrievalPlan 装配点自测。

验证 `_build_chunk_retrieval_plan` 忠实快照 chunk-path flag（无默认漂移、无字段读错），
且 strategy 复刻 ask_chunk 的 overlay_on 三元 AND 与 if/elif/else 顺序。这是把散落 flag
读取收进 plan 的安全前提——plan 读错即此测试红。
"""
import dataclasses

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository


class _IdentityRerank:
    configured = True

    def rerank(self, query, documents, on_error=None):
        return list(range(len(documents)))


class _UnconfiguredRerank:
    configured = False


def _repo(tmp_path):
    repo = SQLiteRepository(Settings(
        database_url=f"sqlite:///{tmp_path}/t.db", storage_dir=str(tmp_path / "s")))
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    return repo, nb.id


def test_plan_is_faithful_frozen_snapshot_of_orchestration_knobs(tmp_path, monkeypatch):
    """plan 只快照编排层 knob（mmr_k/mmr_lambda/fuse_k）；候选生成层全局 flag 刻意不收
    （见 ChunkRetrievalPlan docstring），故此处不断言 recall/ann/bruteforce/delta/ppr。"""
    repo, nb = _repo(tmp_path)
    s = repo.settings
    monkeypatch.setattr(s, "chunk_mmr_k", 9)
    monkeypatch.setattr(s, "chunk_mmr_lambda", 0.42)

    plan = repo.retrieval.candidates._build_chunk_retrieval_plan(nb, ["q"])

    assert plan.mmr_k == 9
    assert plan.mmr_lambda == 0.42
    assert plan.fuse_k == 9          # 复刻 multi 分支 quota_fuse 复用 chunk_mmr_k
    # frozen：不可变快照
    with __import__("pytest").raises(dataclasses.FrozenInstanceError):
        plan.mmr_k = 1


def test_plan_strategy_single_vs_multi_when_overlay_off(tmp_path, monkeypatch):
    repo, nb = _repo(tmp_path)
    monkeypatch.setattr(repo.settings, "chunk_kg_overlay_enabled", False)  # overlay_on 必 False
    assert repo.retrieval.candidates._build_chunk_retrieval_plan(nb, ["q"]).strategy == "single"
    assert repo.retrieval.candidates._build_chunk_retrieval_plan(nb, ["q1", "q2"]).strategy == "multi"
    assert repo.retrieval.candidates._build_chunk_retrieval_plan(nb, ["q"]).overlay_on is False


def test_plan_strategy_mix_takes_priority_when_overlay_on(tmp_path, monkeypatch):
    repo, nb = _repo(tmp_path)
    repo.rerank_client = _IdentityRerank()
    monkeypatch.setattr(repo.retrieval.candidates, "_notebook_has_kg", lambda _nb: True)
    monkeypatch.setattr(repo.retrieval.candidates, "_any_base_notebook_has_kg", lambda: False)
    # 即便 2 个子查询，overlay 优先 → mix（复刻 if overlay/elif multi/else single 顺序）
    plan = repo.retrieval.candidates._build_chunk_retrieval_plan(nb, ["q1", "q2"])
    assert plan.overlay_on is True
    assert plan.strategy == "mix"


def test_plan_overlay_off_when_rerank_unconfigured(tmp_path, monkeypatch):
    repo, nb = _repo(tmp_path)
    monkeypatch.setattr(repo.retrieval.candidates, "_notebook_has_kg", lambda _nb: True)
    monkeypatch.setattr(repo.retrieval.candidates, "_any_base_notebook_has_kg", lambda: False)
    repo.rerank_client = _UnconfiguredRerank()   # 三元 AND 断在 rerank.configured
    plan = repo.retrieval.candidates._build_chunk_retrieval_plan(nb, ["q"])
    assert plan.overlay_on is False
    assert plan.strategy == "single"
