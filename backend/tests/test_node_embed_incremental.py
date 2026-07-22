import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    monkeypatch.setenv("EMBED_BATCH_SIZE", "2")     # 小批,好数 commit
    monkeypatch.setenv("EMBED_COMMIT_BATCHES", "1") # 每批 commit,便于观测增量
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _n_vectors(repo, nb_id):
    with repo._connect() as db:
        return db.execute(
            "SELECT COUNT(*) c FROM knowledge_embeddings WHERE notebook_id=?", (nb_id,)
        ).fetchone()["c"]


def test_node_embed_commits_incrementally_and_resumes(repo, monkeypatch):
    """增量提交:flush 中途抛错也已落库前几组;二次调用只补剩余(missing 续跑)。"""
    from app.services import batch_ingest
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    objs = [{"local_id": f"o{i}", "object_type": "concept",
             "payload": {"name": f"concept number {i}", "section_path": ""},
             "evidence": []} for i in range(10)]
    repo.store_kg(nb.id, None, objs, [])
    # store_kg 会内联即时 embed(既有行为,与本任务无关,早于本 feature 分支就存在)。
    # 清空 knowledge_embeddings 以复现「对象已入库、向量待补」的 backfill 前置态——
    # 这正是本测试要验证的场景(否则 missing 恒空,中断/续跑无从谈起)。
    with repo._write() as db:
        db.execute("DELETE FROM knowledge_embeddings WHERE notebook_id=?", (nb.id,))
    assert _n_vectors(repo, nb.id) == 0

    # 第 3 次 flush 抛错模拟中断(前 2 组已落库)。flush 在主线程、不被 _embed_only 吞、
    # 会传播出 _embed_objects_batch。EMBED_COMMIT_BATCHES=1,batch=2 → 每 2 个一 flush。
    real_flush = repo.__dict__["_runtime"].source_embedding.flush_object_vectors
    calls = {"n": 0}
    def flaky_flush(nb_id, rows):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("模拟中断")
        return real_flush(nb_id, rows)
    monkeypatch.setattr(repo.__dict__["_runtime"].source_embedding, "flush_object_vectors", flaky_flush)

    with pytest.raises(RuntimeError):
        batch_ingest.backfill_node_embeddings(repo, nb.id)
    mid = _n_vectors(repo, nb.id)
    assert 0 < mid < 10                      # 中断前已增量落库前几组

    monkeypatch.setattr(repo.__dict__["_runtime"].source_embedding, "flush_object_vectors", real_flush)  # 恢复
    batch_ingest.backfill_node_embeddings(repo, nb.id)
    assert _n_vectors(repo, nb.id) == 10     # 续跑补齐


def test_node_embed_progress_monotonic(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    objs = [{"local_id": f"o{i}", "object_type": "concept",
             "payload": {"name": f"widget {i}", "section_path": ""}, "evidence": []}
            for i in range(6)]
    repo.store_kg(nb.id, None, objs, [])
    # 同上:清空 store_kg 内联 embed 的既有产物,复现「待补向量」前置态,
    # 让下面 _backfill_knowledge_embeddings 真的有事可做、能观测到多次 progress。
    with repo._write() as db:
        db.execute("DELETE FROM knowledge_embeddings WHERE notebook_id=?", (nb.id,))
    seen = []
    with repo._connect() as db:
        rows = [{"id": r["id"], "payload": __import__("json").loads(r["payload"] or "{}")}
                for r in db.execute(
                    "SELECT id, payload FROM knowledge_objects WHERE notebook_id=?",
                    (nb.id,)).fetchall()]
        repo._backfill_knowledge_embeddings(db, nb.id, rows,
                                            progress=lambda d, t: seen.append((d, t)))
    assert seen and seen[-1][0] == seen[-1][1]           # 末次 done==total
    assert [d for d, _ in seen] == sorted(d for d, _ in seen)  # done 单调不减
