import pytest

from app.core.config import Settings
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _seed(repo, n):
    """只造 concept —— 单一类型使 insert_scratch_rows 的调用次数完全可预期
    (n/1000 批),让下面的守卫能同时挡住「合并成一个事务」和「把提交移出循环」
    两种退化。混类型会让计数变成 4 个类型的和,两种退化都可能蒙混过关。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": f"o{i}", "object_type": "concept",
         "payload": {"name": f"c-{i // 8}", "section_path": ""},
         "evidence": []}
        for i in range(n)
    ], [])
    return nb.id


def test_scratch_staging_uses_a_fresh_transaction_per_batch(repo, monkeypatch):
    """判据:每批 scratch 插入必须落在**不同的写连接**上。

    write() 每次新建连接、用完即 close,所以「同一个 conn 收了多批」等价于
    「这些批共处一个事务」。用连接身份判定,而不是数 write() 样本数 —— 后者会被
    同文件里的其他写点(clear_scratch_run / 簇写入 / finish_rebuild_state)污染,
    在改造前就已经 >1,守卫会假绿。

    列表持有连接对象本身(而非 id),防止对象被回收后 id 复用导致误判。
    """
    from app.repositories.sqlite.unified_kg_store import UnifiedKgStore

    seen_conns = []
    original = UnifiedKgStore.insert_scratch_rows

    def _spy(db, rows):
        seen_conns.append(db)
        return original(db, rows)

    monkeypatch.setattr(UnifiedKgStore, "insert_scratch_rows",
                        staticmethod(_spy))

    nb = _seed(repo, 5_000)
    repo.rebuild_unified_kg(nb, force=True)

    assert len(seen_conns) >= 5, (
        "5000 个 concept 应产生 5 批(批大小 1000);批数不足说明提交被移出了循环",
        len(seen_conns))
    assert len({id(c) for c in seen_conns}) == len(seen_conns), (
        "多个 scratch 批共用同一个写连接 = 仍在一个大事务里",
        len(seen_conns), len({id(c) for c in seen_conns}))


def test_clustering_result_is_bit_identical_after_batching(repo):
    """分批提交不得改变聚类结果。同名对象必须仍然并进同一个 canonical。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept",
         "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept",
         "payload": {"name": "mosfet", "section_path": ""}, "evidence": []},
        {"local_id": "c", "object_type": "concept",
         "payload": {"name": "BJT", "section_path": ""}, "evidence": []},
    ], [])
    repo.rebuild_unified_kg(nb.id, force=True)
    cmap = repo.cluster_map(nb.id)
    assert len(cmap) == 3
    assert len(set(cmap.values())) == 2


def test_scratch_rows_are_cleaned_up_after_rebuild(repo):
    """分批提交后,中断/正常结束的 scratch 清理语义不变。"""
    nb = _seed(repo, 3_000)
    repo.rebuild_unified_kg(nb, force=True)
    with repo._connect() as db:
        left = db.execute(
            "SELECT COUNT(*) AS c FROM kg_cluster_scratch WHERE notebook_id=?",
            (nb,)).fetchone()["c"]
    assert left == 0


def test_seed_computation_runs_outside_the_write_transaction(repo, monkeypatch):
    """这次改造的真正目的不是「分批提交」本身,而是把每对象的 Python 计算
    (_fast_loads / seed_or_unique / alias 匹配)搬到写事务**之外**——批提交只是
    实现手段。上面 test_scratch_staging_uses_a_fresh_transaction_per_batch 只按
    连接身份判定「事务被拆开了」,测不出这一点:如果有人把 seed 计算又搬回每批
    `with self._write() as wdb:` 块内部(同一个连接照样收到 N 个不同批次,连接
    身份守卫仍然全绿),那次退化会被放过。

    直接、确定性地断言目标本身(零 sleep/计时):用一个线程本地标志位包住
    SqliteDatabase.write() 让出的整个块,再让 seed_or_unique——_seed_with_alias
    产出经 alias 匹配后的 seed,再由它做「空 seed 兜底」,是每对象计算链的收尾
    调用点——在被调用时断言该标志位为假。

    验证过该守卫本身有效:曾把 seed_or_unique 的调用暂时挪回
    `with self._write() as wdb:` 块内部,确认本测试真的变红,再恢复。
    """
    import threading
    from contextlib import contextmanager

    from app.repositories.sqlite.database import SqliteDatabase
    import app.services.kg_merge as kg_merge

    flag = threading.local()
    original_write = SqliteDatabase.write

    @contextmanager
    def traced_write(self):
        with original_write(self) as conn:
            flag.inside_write = True
            try:
                yield conn
            finally:
                flag.inside_write = False

    monkeypatch.setattr(SqliteDatabase, "write", traced_write)

    original_seed_or_unique = kg_merge.seed_or_unique
    calls = []

    def guarded_seed_or_unique(*args, **kwargs):
        calls.append(1)
        assert not getattr(flag, "inside_write", False), (
            "seed_or_unique ran INSIDE a SqliteDatabase.write() block — the "
            "per-object Python work must stay outside the write transaction")
        return original_seed_or_unique(*args, **kwargs)

    monkeypatch.setattr(kg_merge, "seed_or_unique", guarded_seed_or_unique)

    nb = _seed(repo, 2_500)
    repo.rebuild_unified_kg(nb, force=True)

    assert calls, "seed_or_unique was never called — this test would pass vacuously"
