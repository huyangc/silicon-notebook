"""batch-3-W1 T-5a: ``delete_notebook_kg`` 的预排水(pre-reset drain)行为钉。

T-5a 的取舍(勘误 2 的裁决,选「范围重界定」而非推翻 P0-1):终局仍是
「13 条 DELETE + unified_kg_state 重置在一个 ``write()`` 里提交,提交后才做
缓存失效」——P0-1 单事务不变量原样保留(``test_kg_mutation_phase_matrix.py::
test_delete_delegates_in_write_then_commits_before_cache_invalidation`` 继续
钉着它);有界化来自终局之前的分页预排水:每批一个独立写事务、同事务经
``mark_unified_kg_dirty_in_tx`` 闸口 bump seq(census 纪律 + 单一闸口红线),
把终局要删的行数压到 ≤threshold 的残余。小图(全部 ≤threshold)走零排水
快路径,与 T-5a 之前逐字节同形。knowledge_objects 页按
``_delete_object_id_batch`` 同形连带同事务清 embeddings/簇成员/kos 行——
排水的任何提交边界都不暴露孤儿簇行(评审 F3)。
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


def _seed_graph(
    repo, notebook_id, *, doc_objects: int, memory_objects: int = 0,
    clusters: bool = False,
):
    """直接落行:一个用户文档来源 + 可选一个 memory 来源,以及各自名下的
    knowledge_objects;``clusters=True`` 时每个文档对象再挂一条簇成员行。
    绕过抽取管线,行数可控。"""
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
            if clusters:
                db.execute(
                    "INSERT INTO concept_clusters (id,notebook_id,canonical_id,"
                    "member_object_id,canonical_name,created_at) VALUES (?,?,?,?,?,?)",
                    (f"cl-{i}", notebook_id, "canon-1", f"ko-doc-{i}", "Canon", now),
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
    终局清空效果不变,隐藏投影(memory 来源的对象)原样保留,epoch+1;探针
    游标只前进不后退(评审 F4)。"""
    monkeypatch.setattr(kl, "_GRAPH_DRAIN_PAGE_ROWS", 3)
    monkeypatch.setattr(kl, "_GRAPH_DRAIN_THRESHOLD_ROWS", 3)
    notebook = repo.create_notebook(NotebookCreate(name="big"))
    _seed_graph(repo, notebook.id, doc_objects=11, memory_objects=2)

    pages = []
    probes = []
    store = repo._runtime.knowledge
    original = store.drain_notebook_graph_rows_page
    original_backlog = store.graph_drain_backlog

    def _spy(db, nb, table, limit):
        deleted = original(db, nb, table, limit)
        pages.append((table, deleted))
        return deleted

    def _backlog_spy(db, nb, threshold, start=0):
        probes.append(start)
        return original_backlog(db, nb, threshold, start)

    monkeypatch.setattr(store, "drain_notebook_graph_rows_page", _spy)
    monkeypatch.setattr(store, "graph_drain_backlog", _backlog_spy)

    with repo._runtime.database.connect() as db:
        epoch_before = repo._runtime.unified_kg.state_row(db, notebook.id)
    counts = repo.delete_notebook_kg(notebook.id)

    assert pages, "超阈值的图必须走排水"
    assert all(
        deleted.get("knowledge_objects", 0) <= 3 for _t, deleted in pages
    ), "每页必须有界"
    assert probes == sorted(probes), f"探针游标必须单调前进:{probes}"
    assert any(p > 0 for p in probes[1:]), (
        f"游标从未离开下标 0——每批都从登记表头重扫(评审 F4):{probes}"
    )
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


def test_each_drain_batch_bumps_seq_through_the_choke_point_in_tx(
    repo, monkeypatch,
):
    """变异钉两问(census 纪律 + 单一闸口红线):
    ① 把排水批里的 ``_mark_unified_kg_dirty_in_tx`` 调用删掉 → bump 计数为
    0,报红(没有逐批 bump,两次 mid-drain 读会把不同的半清图缓存进同一个
    (epoch, seq) 键)。
    ② 把 bump 挪出页删除事务(独立 write())→ 逐批的连接身份配对断言红——
    删除变异和移动变异都逮得住(评审 #3)。"""
    monkeypatch.setattr(kl, "_GRAPH_DRAIN_PAGE_ROWS", 3)
    monkeypatch.setattr(kl, "_GRAPH_DRAIN_THRESHOLD_ROWS", 3)
    notebook = repo.create_notebook(NotebookCreate(name="bump"))
    _seed_graph(repo, notebook.id, doc_objects=10)

    service = repo._runtime.knowledge_lifecycle
    bumps = []
    original_bump = service._mark_unified_kg_dirty_in_tx

    def _bump_spy(db, nb):
        bumps.append(id(db))
        return original_bump(db, nb)

    monkeypatch.setattr(service, "_mark_unified_kg_dirty_in_tx", _bump_spy)

    pages = []
    store = repo._runtime.knowledge
    original_page = store.drain_notebook_graph_rows_page

    def _page_spy(db, nb, table, limit):
        deleted = original_page(db, nb, table, limit)
        if deleted:
            pages.append(id(db))
        return deleted

    monkeypatch.setattr(store, "drain_notebook_graph_rows_page", _page_spy)

    repo.delete_notebook_kg(notebook.id)

    assert pages, "前置不成立:没有发生排水"
    assert bumps == pages, (
        "每个非空排水批必须在**同一个连接/事务**里恰好一次闸口 bump:"
        f"pages(db)={pages} bumps(db)={bumps}"
    )


def test_drain_commits_never_expose_orphan_cluster_rows(repo, monkeypatch):
    """变异钉(评审 F3):把 knowledge_objects 排水页里的 concept_clusters
    连带删除拆掉 → 某个已提交的批边界上出现「member_object_id 已不存在」的
    簇行,报红。孤儿簇行会被 incremental_fuse 的 canonical 折叠误吞新
    concept,而孤儿清扫每进程只跑一次——排水不许当第四个孤儿生产者。"""
    monkeypatch.setattr(kl, "_GRAPH_DRAIN_PAGE_ROWS", 3)
    monkeypatch.setattr(kl, "_GRAPH_DRAIN_THRESHOLD_ROWS", 3)
    notebook = repo.create_notebook(NotebookCreate(name="orphan"))
    _seed_graph(repo, notebook.id, doc_objects=9, clusters=True)

    orphan_snapshots = []
    store = repo._runtime.knowledge
    original = store.drain_notebook_graph_rows_page

    def _spy(db, nb, table, limit):
        # 进入本批之前,读上一批**已提交**的状态(独立连接,看不见本批未提交
        # 的写)——每个批边界都不得有孤儿簇行。
        orphan_snapshots.append(_count(
            repo,
            "SELECT COUNT(*) FROM concept_clusters c WHERE c.notebook_id=? "
            "AND NOT EXISTS (SELECT 1 FROM knowledge_objects ko "
            "WHERE ko.id=c.member_object_id)",
            (nb,),
        ))
        return original(db, nb, table, limit)

    monkeypatch.setattr(store, "drain_notebook_graph_rows_page", _spy)
    repo.delete_notebook_kg(notebook.id)

    assert orphan_snapshots, "前置不成立:没有发生排水"
    assert all(n == 0 for n in orphan_snapshots), (
        f"排水的提交边界暴露了孤儿簇行:{orphan_snapshots}"
    )
    assert _count(
        repo,
        "SELECT COUNT(*) FROM concept_clusters WHERE notebook_id=?",
        (notebook.id,),
    ) == 0


def test_each_drain_commit_evicts_the_unified_graph_cache(repo, monkeypatch):
    """变异钉(codex #663 R1 P1):把排水批提交后的 `_invalidate_unified_cache`
    删掉 → 每个非空批之后的驱逐计数缺失,报红。unified_cache 无任何版本键,
    invalidate_kg 是唯一驱逐路径——不逐批驱逐,温缓存整个排水期(中止则无限期)
    端着删前的图。"""
    monkeypatch.setattr(kl, "_GRAPH_DRAIN_PAGE_ROWS", 3)
    monkeypatch.setattr(kl, "_GRAPH_DRAIN_THRESHOLD_ROWS", 3)
    notebook = repo.create_notebook(NotebookCreate(name="evict"))
    _seed_graph(repo, notebook.id, doc_objects=10)

    service = repo._runtime.knowledge_lifecycle
    evictions = []
    original_evict = service._invalidate_unified_cache

    def _evict_spy(nb):
        evictions.append(nb)
        return original_evict(nb)

    monkeypatch.setattr(service, "_invalidate_unified_cache", _evict_spy)

    pages = []
    store = repo._runtime.knowledge
    original_page = store.drain_notebook_graph_rows_page

    def _page_spy(db, nb, table, limit):
        deleted = original_page(db, nb, table, limit)
        if deleted:
            pages.append(table)
        return deleted

    monkeypatch.setattr(store, "drain_notebook_graph_rows_page", _page_spy)
    repo.delete_notebook_kg(notebook.id)

    assert pages, "前置不成立:没有发生排水"
    assert len(evictions) == len(pages) + 1, (
        "每个非空排水批提交后 + 终局提交后各一次驱逐:"
        f"pages={len(pages)} evictions={len(evictions)}"
    )


def test_drain_commits_never_expose_doc_edges_to_missing_nodes(repo, monkeypatch):
    """变异钉(codex #663 R1 P2):把「krel 先于 ko」的登记序连同终局语句序
    一起换回旧序(点先于边)→ 某个已提交的批边界上出现「端点对象已消失」的
    文档源关系边,报红。边先于点是排水提交边界结构一致性的承载序。"""
    monkeypatch.setattr(kl, "_GRAPH_DRAIN_PAGE_ROWS", 3)
    monkeypatch.setattr(kl, "_GRAPH_DRAIN_THRESHOLD_ROWS", 3)
    notebook = repo.create_notebook(NotebookCreate(name="edges"))
    _seed_graph(repo, notebook.id, doc_objects=9)
    now = _now()
    with repo._runtime.database.write() as db:
        for i in range(8):
            db.execute(
                "INSERT INTO knowledge_relations (id,notebook_id,source_id,"
                "source_object_id,target_object_id,edge_type,evidence,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (f"rel-{i}", notebook.id, "src-doc", f"ko-doc-{i}",
                 f"ko-doc-{i + 1}", "supports", "[]", now),
            )

    dangling_snapshots = []
    store = repo._runtime.knowledge
    original = store.drain_notebook_graph_rows_page

    def _spy(db, nb, table, limit):
        dangling_snapshots.append(_count(
            repo,
            "SELECT COUNT(*) FROM knowledge_relations r WHERE r.notebook_id=? "
            "AND NOT EXISTS (SELECT 1 FROM sources s WHERE s.id=r.source_id "
            "AND s.notebook_id=? AND s.source_type IN ('memory','knowhow')) "
            "AND (NOT EXISTS (SELECT 1 FROM knowledge_objects ko "
            "WHERE ko.id=r.source_object_id) OR NOT EXISTS "
            "(SELECT 1 FROM knowledge_objects ko WHERE ko.id=r.target_object_id))",
            (nb, nb),
        ))
        return original(db, nb, table, limit)

    monkeypatch.setattr(store, "drain_notebook_graph_rows_page", _spy)
    repo.delete_notebook_kg(notebook.id)

    assert dangling_snapshots, "前置不成立:没有发生排水"
    assert all(n == 0 for n in dangling_snapshots), (
        f"排水的提交边界暴露了指向已删对象的文档源边:{dangling_snapshots}"
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
    """变异钉:把「3 次连续零删响亮失败」改成静默继续 → 排水循环失去终止
    条件(本用例以 RuntimeError 类型断言判红;仓库没有 pytest-timeout,
    真跑死循环只会挂到 CI 任务级超时,所以辨别信号是这里的异常断言本身)。"""
    monkeypatch.setattr(kl, "_GRAPH_DRAIN_PAGE_ROWS", 3)
    monkeypatch.setattr(kl, "_GRAPH_DRAIN_THRESHOLD_ROWS", 3)
    notebook = repo.create_notebook(NotebookCreate(name="stall"))
    _seed_graph(repo, notebook.id, doc_objects=10)

    store = repo._runtime.knowledge
    monkeypatch.setattr(
        store, "drain_notebook_graph_rows_page",
        lambda db, nb, table, limit: {},
    )
    with pytest.raises(RuntimeError, match="stalled"):
        repo.delete_notebook_kg(notebook.id)


class _FakeCursor:
    rowcount = 0


class _RecordingDb:
    """Trace 终局函数实际执行的 SQL(容纳 psycopg 的 Composed 对象)。"""

    def __init__(self):
        self.executed: list[str] = []

    def execute(self, statement, params=()):
        if not isinstance(statement, str):
            # psycopg sql.SQL(...).format(...) 产物;as_string(None) 在
            # psycopg>=3.1.9 上下文无关可用(仅含 Identifier/SQL 字面量)。
            statement = statement.as_string(None)
        self.executed.append(" ".join(str(statement).split()))
        return _FakeCursor()


def _mirror_check(steps, deletes):
    import re

    assert len(deletes) == len(steps), (
        f"终局 DELETE 语句数({len(deletes)})与排水登记表条数({len(steps)})不一致"
    )
    for (table, predicate, _params), statement in zip(steps, deletes):
        match = re.fullmatch(r'DELETE FROM "?([\w]+)"? WHERE (.+)', statement)
        assert match, statement
        assert match.group(1) == table, (
            f"顺序漂移:登记表期望 {table},终局执行 {match.group(1)}"
        )
        assert " ".join(predicate.split()) == match.group(2), (
            f"{table} 的谓词漂移:\n登记表: {predicate}\n终局:  {match.group(2)}"
        )


def test_drain_registry_mirrors_the_final_statements_sqlite():
    """反漂移钉(sqlite):排水登记表的 (表, 谓词) 必须与
    ``delete_notebook_graph_rows`` 实际执行的 DELETE 语句一一对应、顺序一致。
    变异钉:改登记表任何一条谓词/顺序而不同步改终局函数(或反之)→ 报红。"""
    from app.repositories.sqlite.knowledge_store import (
        _GRAPH_DRAIN_STEPS, KnowledgeStore,
    )

    fake = _RecordingDb()
    KnowledgeStore.delete_notebook_graph_rows(fake, "nb-x", "2026-01-01")
    deletes = [s for s in fake.executed if s.startswith("DELETE FROM ")]
    _mirror_check(_GRAPH_DRAIN_STEPS, deletes)


def test_drain_registry_mirrors_the_final_statements_postgres():
    """反漂移钉(PG,评审 F2):PG 登记表把 ``sorted(_GRAPH_RESET_TABLES -
    {emb, runs})`` 的结果手写展开——任何人往 ``_GRAPH_RESET_TABLES`` 加表
    (PR-2 R1 刚加过两张)而不同步登记表,那张表就在终局单事务里回到无界
    DELETE。两问:① 语句级一一对应(同 sqlite 镜像);② 登记表表集必须
    覆盖 ``_GRAPH_RESET_TABLES`` 全集(「往 set 加表」这一真实变异直接报红)。"""
    from app.repositories.postgres.knowledge_store import (
        _GRAPH_DRAIN_STEPS, _GRAPH_RESET_TABLES, KnowledgeStore,
    )

    fake = _RecordingDb()
    KnowledgeStore.delete_notebook_graph_rows(fake, "nb-x", "2026-01-01")
    deletes = [s for s in fake.executed if s.startswith("DELETE FROM ")]
    _mirror_check(_GRAPH_DRAIN_STEPS, deletes)
    registry_tables = {t for t, _p, _n in _GRAPH_DRAIN_STEPS}
    assert registry_tables >= _GRAPH_RESET_TABLES, (
        "排水登记表漏掉了 _GRAPH_RESET_TABLES 的成员:"
        f"{sorted(_GRAPH_RESET_TABLES - registry_tables)}"
    )
