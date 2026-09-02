"""batch-3-W1 T-5a: ``delete_notebook_kg`` 的预排水(pre-reset drain)行为钉。

T-5a 的取舍(勘误 2 的裁决,选「范围重界定」而非推翻 P0-1):终局仍是
「13 条 DELETE + unified_kg_state 重置在一个 ``write()`` 里提交,提交后才做
缓存失效」——P0-1 单事务不变量原样保留(``test_kg_mutation_phase_matrix.py::
test_delete_delegates_in_write_then_commits_before_cache_invalidation`` 继续
钉着它);有界化来自终局之前的分页预排水:每批一个独立写事务、同事务
``mark_dirty`` bump seq(census 纪律),把终局要删的行数压到 ≤threshold 的
残余。小图(全部 ≤threshold)走零排水快路径,与 T-5a 之前逐字节同形。
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.ports import NotebookDeletingAbortsMaintenanceError
from app.services import knowledge_lifecycle as kl
from app.services.sqlite_repository import SQLiteRepository, _now


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'drain.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    return SQLiteRepository(Settings(_env_file=None))


def _seed_graph(repo, notebook_id, *, doc_objects: int, memory_objects: int = 0):
    """直接落行:一个用户文档来源 + 可选一个 memory 来源,以及各自名下的
    knowledge_objects。绕过抽取管线,行数可控。"""
    now = _now()
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,file_path,source_type,"
            "status,parse_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("src-doc", notebook_id, "doc", "", "pdf", "ready", "parsed", now, now),
        )
        if memory_objects:
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,file_path,source_type,"
                "status,parse_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                ("src-mem", notebook_id, "mem", "", "memory", "ready", "parsed",
                 now, now),
            )
        for i in range(doc_objects):
            db.execute(
                "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,"
                "owner,payload,evidence,source_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"ko-doc-{i}", notebook_id, "concept", "approved", "", "{}",
                 "[]", "src-doc", now, now),
            )
        for i in range(memory_objects):
            db.execute(
                "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,"
                "owner,payload,evidence,source_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"ko-mem-{i}", notebook_id, "concept", "approved", "", "{}",
                 "[]", "src-mem", now, now),
            )


def _count(repo, sql, params=()):
    with repo._runtime.database.connect() as db:
        return db.execute(sql, params).fetchone()[0]


def test_small_graph_path_performs_zero_drain_writes(repo, monkeypatch):
    """变异钉:把「先只读探针、小图直接跳过排水」的快路径拆掉(比如无条件
    先开一个排水写事务)→ 本用例的排水页计数不再是 0,报红。这是 P0-1 钉
    测试之外的显式一问:小图路径必须零排水写事务,不只是事件序列碰巧没变。"""
    notebook = repo.create_notebook(NotebookCreate(name="small"))
    _seed_graph(repo, notebook.id, doc_objects=5)

    pages = []
    store = repo._runtime.knowledge
    original = store.drain_notebook_graph_rows_page

    def _spy(db, nb, table, limit):
        pages.append(table)
        return original(db, nb, table, limit)

    monkeypatch.setattr(store, "drain_notebook_graph_rows_page", _spy)
    counts = repo.delete_notebook_kg(notebook.id)

    assert pages == [], "小于阈值的图不得触发任何排水页"
    assert counts["knowledge_objects"] == 5


def test_drain_bounds_the_final_pass_and_counts_stay_total(repo, monkeypatch):
    """变异钉:把 `_drain_graph_rows_before_reset` 的调用从 delete_notebook_kg
    里删掉 → 排水页计数为 0,报红。同钉:排水后 counts 仍报全量(排水行并回),
    终局清空效果不变,隐藏投影(memory 来源的对象)原样保留,epoch+1。"""
    monkeypatch.setattr(kl, "_GRAPH_DRAIN_PAGE_ROWS", 3)
    monkeypatch.setattr(kl, "_GRAPH_DRAIN_THRESHOLD_ROWS", 3)
    notebook = repo.create_notebook(NotebookCreate(name="big"))
    _seed_graph(repo, notebook.id, doc_objects=11, memory_objects=2)

    pages = []
    store = repo._runtime.knowledge
    original = store.drain_notebook_graph_rows_page

    def _spy(db, nb, table, limit):
        deleted = original(db, nb, table, limit)
        pages.append((table, deleted))
        return deleted

    monkeypatch.setattr(store, "drain_notebook_graph_rows_page", _spy)

    with repo._runtime.database.connect() as db:
        epoch_before = repo._runtime.unified_kg.state_row(db, notebook.id)
    counts = repo.delete_notebook_kg(notebook.id)

    assert pages, "超阈值的图必须走排水"
    assert all(deleted <= 3 for _t, deleted in pages), "每页必须有界"
    # counts 并回排水行:11 条用户文档对象一条不少(memory 的 2 条不算——
    # 它们本来就不删)。
    assert counts["knowledge_objects"] == 11
    assert _count(
        repo,
        "SELECT COUNT(*) FROM knowledge_objects WHERE notebook_id=? "
        "AND source_id='src-doc'",
        (notebook.id,),
    ) == 0
    assert _count(
        repo,
        "SELECT COUNT(*) FROM knowledge_objects WHERE notebook_id=? "
        "AND source_id='src-mem'",
        (notebook.id,),
    ) == 2, "memory 来源的隐藏投影必须原样保留"
    with repo._runtime.database.connect() as db:
        state = repo._runtime.unified_kg.state_row(db, notebook.id)
    assert state["kg_mutation_seq"] == 0
    assert state["kg_reset_epoch"] == (
        (epoch_before["kg_reset_epoch"] if epoch_before else 0) + 1
    )


def test_each_drain_batch_bumps_seq_in_the_same_transaction(repo, monkeypatch):
    """变异钉(census 纪律):把排水批里的 ``mark_dirty`` 调用删掉 → bump 计数
    为 0,报红。没有逐批 bump,两次 mid-drain 读会把不同的半清图缓存进同一个
    (epoch, seq) 键。"""
    monkeypatch.setattr(kl, "_GRAPH_DRAIN_PAGE_ROWS", 3)
    monkeypatch.setattr(kl, "_GRAPH_DRAIN_THRESHOLD_ROWS", 3)
    notebook = repo.create_notebook(NotebookCreate(name="bump"))
    _seed_graph(repo, notebook.id, doc_objects=10)

    bumps = []
    unified = repo._runtime.unified_kg
    original_mark = unified.mark_dirty

    def _spy(db, nb, now):
        bumps.append(nb)
        return original_mark(db, nb, now)

    monkeypatch.setattr(unified, "mark_dirty", _spy)
    pages = []
    store = repo._runtime.knowledge
    original_page = store.drain_notebook_graph_rows_page

    def _page_spy(db, nb, table, limit):
        deleted = original_page(db, nb, table, limit)
        if deleted:
            pages.append(deleted)
        return deleted

    monkeypatch.setattr(store, "drain_notebook_graph_rows_page", _page_spy)

    repo.delete_notebook_kg(notebook.id)

    assert pages, "前置不成立:没有发生排水"
    assert len(bumps) == len(pages), (
        f"每个非空排水批必须恰好一次 seq bump:pages={len(pages)} "
        f"bumps={len(bumps)}"
    )


def test_tombstone_mid_drain_aborts_like_the_other_checkpoints(repo, monkeypatch):
    """变异钉:把排水循环里的 `_notebook_deleting` 检查点删掉 → 墓碑落地后
    排水照跑到底,报红。删除作业的相位 3 拥有这些行,维护路径应当就地停手。"""
    monkeypatch.setattr(kl, "_GRAPH_DRAIN_PAGE_ROWS", 3)
    monkeypatch.setattr(kl, "_GRAPH_DRAIN_THRESHOLD_ROWS", 3)
    notebook = repo.create_notebook(NotebookCreate(name="abort"))
    _seed_graph(repo, notebook.id, doc_objects=10)

    service = repo._runtime.knowledge_lifecycle
    calls = {"n": 0}

    def _deleting_after_first(_nb):
        calls["n"] += 1
        return calls["n"] > 1  # 第一批放行,第二批起墓碑已落

    monkeypatch.setattr(service, "_notebook_deleting", _deleting_after_first)
    with pytest.raises(NotebookDeletingAbortsMaintenanceError):
        repo.delete_notebook_kg(notebook.id)
    # 排水就地停手:还有超过一页的行留在原地(没有跑到终局清空)。
    assert _count(
        repo,
        "SELECT COUNT(*) FROM knowledge_objects WHERE notebook_id=?",
        (notebook.id,),
    ) > 3


def test_drain_stall_raises_loudly(repo, monkeypatch):
    """变异钉:把 3 次连续零删的响亮失败改成静默继续 → 本用例死循环被
    pytest 超时打断而不是 RuntimeError,报红(以异常类型断言)。"""
    monkeypatch.setattr(kl, "_GRAPH_DRAIN_PAGE_ROWS", 3)
    monkeypatch.setattr(kl, "_GRAPH_DRAIN_THRESHOLD_ROWS", 3)
    notebook = repo.create_notebook(NotebookCreate(name="stall"))
    _seed_graph(repo, notebook.id, doc_objects=10)

    store = repo._runtime.knowledge
    monkeypatch.setattr(
        store, "drain_notebook_graph_rows_page",
        lambda db, nb, table, limit: 0,
    )
    with pytest.raises(RuntimeError, match="stalled"):
        repo.delete_notebook_kg(notebook.id)


def test_drain_registry_mirrors_the_final_statements(repo):
    """反漂移钉(sqlite):排水登记表的 (表, 谓词) 必须与
    ``delete_notebook_graph_rows`` 实际执行的 DELETE 语句一一对应、顺序一致。
    变异钉:改登记表任何一条谓词/顺序而不同步改终局函数(或反之)→ 报红。"""
    import re

    from app.repositories.sqlite.knowledge_store import (
        _GRAPH_DRAIN_STEPS, KnowledgeStore,
    )

    executed: list[str] = []

    class _FakeCursor:
        rowcount = 0

    class _FakeDb:
        def execute(self, sql, params=()):
            executed.append(" ".join(str(sql).split()))
            return _FakeCursor()

    KnowledgeStore.delete_notebook_graph_rows(_FakeDb(), "nb-x", "2026-01-01")

    deletes = [s for s in executed if s.startswith("DELETE FROM ")]
    assert len(deletes) == len(_GRAPH_DRAIN_STEPS), (
        f"终局 DELETE 语句数({len(deletes)})与排水登记表条数"
        f"({len(_GRAPH_DRAIN_STEPS)})不一致"
    )
    for (table, predicate, _params), statement in zip(_GRAPH_DRAIN_STEPS, deletes):
        match = re.fullmatch(r"DELETE FROM (\S+) WHERE (.+)", statement)
        assert match, statement
        assert match.group(1) == table, (
            f"顺序漂移:登记表期望 {table},终局执行 {match.group(1)}"
        )
        assert " ".join(predicate.split()) == match.group(2), (
            f"{table} 的谓词漂移:\n登记表: {predicate}\n终局:  {match.group(2)}"
        )
