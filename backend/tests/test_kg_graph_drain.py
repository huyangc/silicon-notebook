"""batch-3-W1 T-5a: ``delete_notebook_kg`` 的预排水(pre-reset drain)行为钉。

T-5a 的取舍(勘误 2 的裁决,选「范围重界定」而非推翻 P0-1):终局仍是
「全部 DELETE + unified_kg_state 重置在一个 ``write()`` 里提交,提交后才做
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
    clusters: bool = False, tag: str = "",
):
    """直接落行:一个用户文档来源 + 可选一个 memory 来源,以及各自名下的
    knowledge_objects;``clusters=True`` 时每个文档对象再挂一条簇成员行。
    绕过抽取管线,行数可控。``tag`` 给第二本库的行加后缀(缺省时 id 不变)。"""
    now = _now()
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,file_path,source_type,"
            "status,parse_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"src-doc{tag}", notebook_id, "doc", "", "pdf", "ready", "parsed",
             now, now),
        )
        if memory_objects:
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,file_path,source_type,"
                "status,parse_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"src-mem{tag}", notebook_id, "mem", "", "memory", "ready",
                 "parsed", now, now),
            )
        for i in range(doc_objects):
            db.execute(
                "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,"
                "owner,payload,evidence,source_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"ko-doc{tag}-{i}", notebook_id, "concept", "approved", "", "{}",
                 "[]", f"src-doc{tag}", now, now),
            )
            if clusters:
                db.execute(
                    "INSERT INTO concept_clusters (id,notebook_id,canonical_id,"
                    "member_object_id,canonical_name,created_at) VALUES (?,?,?,?,?,?)",
                    (f"cl{tag}-{i}", notebook_id, "canon-1", f"ko-doc{tag}-{i}",
                     "Canon", now),
                )
        for i in range(memory_objects):
            db.execute(
                "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,"
                "owner,payload,evidence,source_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"ko-mem{tag}-{i}", notebook_id, "concept", "approved", "", "{}",
                 "[]", f"src-mem{tag}", now, now),
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

    def _spy(db, nb, step, limit):
        pages.append(step)
        return original(db, nb, step, limit)

    monkeypatch.setattr(store, "drain_notebook_graph_rows_page", _spy)
    counts = repo.delete_notebook_kg(notebook.id)

    assert pages == [], "小于阈值的图不得触发任何排水页"
    assert counts["knowledge_objects"] == 5


def test_drain_bounds_the_final_pass_and_counts_stay_total(repo, monkeypatch):
    """变异钉:把 `_drain_graph_rows_before_reset` 的调用从 delete_notebook_kg
    里删掉 → 排水页计数为 0,报红。同钉:排水后 counts 仍报全量(排水行并回),
    终局清空效果不变,隐藏投影(memory 来源的对象)原样保留,epoch+1;探针
    游标只前进不后退(评审 F4)。"""
    monkeypatch.setattr(
        repo._runtime.knowledge_lifecycle, "_graph_drain_budget_rows", 3
    )
    notebook = repo.create_notebook(NotebookCreate(name="big"))
    _seed_graph(repo, notebook.id, doc_objects=11, memory_objects=2)

    pages = []
    probes = []
    store = repo._runtime.knowledge
    original = store.drain_notebook_graph_rows_page
    original_backlog = store.graph_drain_backlog

    def _spy(db, nb, step, limit):
        deleted = original(db, nb, step, limit)
        pages.append((step, deleted))
        return deleted

    def _backlog_spy(db, nb, threshold, start=0, **kw):
        probes.append(start)
        return original_backlog(db, nb, threshold, start, **kw)

    monkeypatch.setattr(store, "drain_notebook_graph_rows_page", _spy)
    monkeypatch.setattr(store, "graph_drain_backlog", _backlog_spy)

    with repo._runtime.database.connect() as db:
        epoch_before = repo._runtime.unified_kg.state_row(db, notebook.id)
    counts = repo.delete_notebook_kg(notebook.id)

    assert pages, "超阈值的图必须走排水"
    assert all(
        deleted.get("knowledge_objects", 0) <= 3 for _s, deleted in pages
    ), "每页必须有界"
    assert any(p > 0 for p in probes[1:]), (
        f"游标从未离开下标 0——每批都从登记表头重扫(评审 F4):{probes}"
    )
    assert probes[-1] == 0, (
        "终局前必须有一轮从下标 0 起的干净复扫(codex #663 R2 P1:游标只是"
        f"快进,不是承诺——并发摄取可以回填已越过的表):{probes}"
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
    monkeypatch.setattr(
        repo._runtime.knowledge_lifecycle, "_graph_drain_budget_rows", 3
    )
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

    def _page_spy(db, nb, step, limit):
        deleted = original_page(db, nb, step, limit)
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
    monkeypatch.setattr(
        repo._runtime.knowledge_lifecycle, "_graph_drain_budget_rows", 3
    )
    notebook = repo.create_notebook(NotebookCreate(name="orphan"))
    _seed_graph(repo, notebook.id, doc_objects=9, clusters=True)

    orphan_snapshots = []
    store = repo._runtime.knowledge
    original = store.drain_notebook_graph_rows_page

    def _spy(db, nb, step, limit):
        # 进入本批之前,读上一批**已提交**的状态(独立连接,看不见本批未提交
        # 的写)——每个批边界都不得有孤儿簇行。
        orphan_snapshots.append(_count(
            repo,
            "SELECT COUNT(*) FROM concept_clusters c WHERE c.notebook_id=? "
            "AND NOT EXISTS (SELECT 1 FROM knowledge_objects ko "
            "WHERE ko.id=c.member_object_id)",
            (nb,),
        ))
        return original(db, nb, step, limit)

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
    monkeypatch.setattr(
        repo._runtime.knowledge_lifecycle, "_graph_drain_budget_rows", 3
    )
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

    def _page_spy(db, nb, step, limit):
        deleted = original_page(db, nb, step, limit)
        if deleted:
            pages.append(step)
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
    monkeypatch.setattr(
        repo._runtime.knowledge_lifecycle, "_graph_drain_budget_rows", 3
    )
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

    def _spy(db, nb, step, limit):
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
        return original(db, nb, step, limit)

    monkeypatch.setattr(store, "drain_notebook_graph_rows_page", _spy)
    repo.delete_notebook_kg(notebook.id)

    assert dangling_snapshots, "前置不成立:没有发生排水"
    assert all(n == 0 for n in dangling_snapshots), (
        f"排水的提交边界暴露了指向已删对象的文档源边:{dangling_snapshots}"
    )


def test_final_reset_revalidates_the_bound_inside_its_own_transaction(
    repo, monkeypatch,
):
    """变异钉(codex #663 R6 P1):把终局事务开头的 in-tx 复验拆掉 → 排水的
    只读探针给出「已排净」的**过期**结论(并发 store_kg 在探针扫到后面条目时
    往已越过的表里灌行)后,终局直接带着无界积压开跑,重排水轮数只有 1,报红。
    本用例把竞态做成确定性:包住 graph_drain_backlog,在它第一次要对 start=0
    返回 None 的那一刻,经独立连接注入 5 条新对象行、仍返回过期的 None——
    终局的 in-tx 复验必须逮住它并再排一轮。"""
    service = repo._runtime.knowledge_lifecycle
    monkeypatch.setattr(service, "_graph_drain_budget_rows", 3)
    notebook = repo.create_notebook(NotebookCreate(name="revalidate"))
    _seed_graph(repo, notebook.id, doc_objects=10)

    from app.repositories.sqlite.database import SqliteDatabase

    primary = repo._runtime.database
    independent = SqliteDatabase(primary.settings, primary.root_dir)
    store = repo._runtime.knowledge
    original_backlog = store.graph_drain_backlog
    injected = {"done": False}

    def _stale_none_backlog(db, nb, threshold, start=0, **kw):
        result = original_backlog(db, nb, threshold, start, **kw)
        if result is None and start == 0 and not injected["done"]:
            injected["done"] = True
            now = _now()
            with independent.write() as w:
                for i in range(5):
                    w.execute(
                        "INSERT INTO knowledge_objects (id,notebook_id,"
                        "object_type,status,owner,payload,evidence,source_id,"
                        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (f"ko-late-{i}", nb, "concept", "approved", "", "{}",
                         "[]", "src-doc", now, now),
                    )
            return None  # 过期结论:探针没看见刚灌进来的行
        return result

    monkeypatch.setattr(store, "graph_drain_backlog", _stale_none_backlog)

    drain_rounds = []
    original_drain = service._drain_graph_rows_before_reset

    def _drain_spy(nb):
        result = original_drain(nb)
        drain_rounds.append(dict(result))
        return result

    monkeypatch.setattr(service, "_drain_graph_rows_before_reset", _drain_spy)

    counts = repo.delete_notebook_kg(notebook.id)

    assert injected["done"], "前置不成立:没有触发注入"
    assert len(drain_rounds) >= 2, (
        "in-tx 复验必须逮住过期 None 并触发再排水:"
        f"drain_rounds={len(drain_rounds)}"
    )
    assert counts["knowledge_objects"] == 15
    assert _count(
        repo,
        "SELECT COUNT(*) FROM knowledge_objects WHERE notebook_id=?",
        (notebook.id,),
    ) == 0


def test_standalone_delete_holds_the_kg_building_fence(repo, monkeypatch):
    """变异钉(codex #663 R7 P1b):把 delete_notebook_kg 的 kg_building 全程
    登记拆掉 → 排水期集合里查无此库,报红。文档图写者(buildkg-/rebuildkg-)
    在提交口被 kg_building 单飞挡住——rebuild 路径本就先登记再进删除相位,
    独立调用方也必须自持同一把 fence,终局的 in-tx 复验才有「写者被挡」的
    前提。收尾后必须释放(不是 rebuild 持有的那份)。"""
    service = repo._runtime.knowledge_lifecycle
    monkeypatch.setattr(service, "_graph_drain_budget_rows", 3)
    notebook = repo.create_notebook(NotebookCreate(name="fence"))
    _seed_graph(repo, notebook.id, doc_objects=10)

    seen = []
    store = repo._runtime.knowledge
    original = store.drain_notebook_graph_rows_page

    def _spy(db, nb, step, limit):
        seen.append(nb in service.kg_building)
        return original(db, nb, step, limit)

    build_refusals = []

    def _probe_admission_mid_drain(db, nb, step, limit):
        # codex #663 R8:fence 必须是 build 作业的**真准入闸**——排水进行中
        # prepare_notebook_kg_job 必须拒绝(变异钉:把 prepare 里的
        # kg_building 检查删掉 → 这里能成功建出新构建作业,报红)。
        from app.repositories.ports import KgBuildAlreadyRunning

        seen.append(nb in service.kg_building)
        if len(seen) == 1:
            try:
                service.prepare_notebook_kg_job(
                    nb, "incremental", allow_without_model=True
                )
            except KgBuildAlreadyRunning:
                build_refusals.append(nb)
        return original(db, nb, step, limit)

    monkeypatch.setattr(
        store, "drain_notebook_graph_rows_page", _probe_admission_mid_drain
    )
    del _spy
    repo.delete_notebook_kg(notebook.id)

    assert seen and all(seen), "排水全程必须持有 kg_building fence"
    assert build_refusals == [notebook.id], (
        "排水期间的新构建作业准入必须被 kg_building 闸拒绝"
    )
    assert notebook.id not in service.kg_building, "收尾后必须释放 fence"


def test_small_path_still_validates_inside_the_final_transaction(
    repo, monkeypatch,
):
    """变异钉(codex #663 R7 P1a):把终局复验加回 ``drained and`` 门(小图
    路径跳过复验)→ 首扫「全表低于阈值」的过期结论直通终局,重排水轮数为
    0,报红。做法同 R6 钉:首个 start=0 的 None 瞬间注入 5 行、仍返过期
    None——无条件复验必须逮住。"""
    service = repo._runtime.knowledge_lifecycle
    monkeypatch.setattr(service, "_graph_drain_budget_rows", 3)
    notebook = repo.create_notebook(NotebookCreate(name="small-reval"))
    _seed_graph(repo, notebook.id, doc_objects=2)  # 低于阈值:不触发首轮排水

    from app.repositories.sqlite.database import SqliteDatabase

    primary = repo._runtime.database
    independent = SqliteDatabase(primary.settings, primary.root_dir)
    store = repo._runtime.knowledge
    original_backlog = store.graph_drain_backlog
    injected = {"done": False}

    def _stale_none_backlog(db, nb, threshold, start=0, **kw):
        result = original_backlog(db, nb, threshold, start, **kw)
        if result is None and start == 0 and not injected["done"]:
            injected["done"] = True
            now = _now()
            with independent.write() as w:
                for i in range(5):
                    w.execute(
                        "INSERT INTO knowledge_objects (id,notebook_id,"
                        "object_type,status,owner,payload,evidence,source_id,"
                        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (f"ko-late-{i}", nb, "concept", "approved", "", "{}",
                         "[]", "src-doc", now, now),
                    )
            return None
        return result

    monkeypatch.setattr(store, "graph_drain_backlog", _stale_none_backlog)

    drain_rounds = []
    original_drain = service._drain_graph_rows_before_reset

    def _drain_spy(nb):
        result = original_drain(nb)
        drain_rounds.append(dict(result))
        return result

    monkeypatch.setattr(service, "_drain_graph_rows_before_reset", _drain_spy)
    counts = repo.delete_notebook_kg(notebook.id)

    assert injected["done"], "前置不成立:没有触发注入"
    assert len(drain_rounds) >= 2, (
        f"小图路径的过期结论也必须被终局复验逮住:{len(drain_rounds)}"
    )
    assert counts["knowledge_objects"] == 7
    assert _count(
        repo,
        "SELECT COUNT(*) FROM knowledge_objects WHERE notebook_id=?",
        (notebook.id,),
    ) == 0


def test_high_fanout_dependents_are_predrained_in_bounded_pages(repo, monkeypatch):
    """变异钉(codex #663 R3 P1):把 ko 之前的两个非镜像预排水步从登记表删掉
    → 高扇出从属行(合并对象的 provenance 行/簇成员行)全部落进 ko 页的事务内
    级联,单页删除远超行预算,报红。预排水步存在时:从属行先按自己的行预算
    逐页排掉,ko 页的级联只剩残余。"""
    from app.repositories.sqlite.knowledge_store import _GRAPH_DRAIN_STEPS

    service = repo._runtime.knowledge_lifecycle
    monkeypatch.setattr(service, "_graph_drain_budget_rows", 3)
    notebook = repo.create_notebook(NotebookCreate(name="fanout"))
    _seed_graph(repo, notebook.id, doc_objects=4)
    now = _now()
    with repo._runtime.database.write() as db:
        # 每个对象 5 条 provenance 行(合并对象形状):20 行 >> 预算 3。
        for i in range(4):
            for j in range(5):
                db.execute(
                    "INSERT INTO knowledge_object_sources "
                    "(object_id,source_id,notebook_id) VALUES (?,?,?)",
                    (f"ko-doc-{i}", f"src-extra-{j}", notebook.id),
                )

    now2 = _now()
    with repo._runtime.database.write() as db:
        # ksfe 级联扇出(codex #663 R5 P1):4 条事实 × 5 个 element 行,
        # fk_ksfe_fact ON DELETE CASCADE——没有预排水步,一页事实(或终局)
        # 会级联删掉 20 行,行预算失效。
        for i in range(4):
            db.execute(
                "INSERT INTO knowledge_source_facts (id,notebook_id,source_id,"
                "source_generation,local_object_id,object_type,created_at,"
                "updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (f"fact-{i}", notebook.id, "src-doc", "g1", f"lo-{i}",
                 "concept", now2, now2),
            )
            for j in range(5):
                db.execute(
                    "INSERT INTO knowledge_source_fact_elements (fact_id,"
                    "notebook_id,source_id,source_generation,element_id,"
                    "created_at) VALUES (?,?,?,?,?,?)",
                    (f"fact-{i}", notebook.id, "src-doc", "g1",
                     f"el-{i}-{j}", now2),
                )

    pages = []
    store = repo._runtime.knowledge
    original = store.drain_notebook_graph_rows_page

    def _spy(db, nb, step, limit):
        deleted = original(db, nb, step, limit)
        if deleted:
            pages.append((step, deleted))
        return deleted

    from app.repositories.sqlite.database import SqliteDatabase

    primary = repo._runtime.database
    independent = SqliteDatabase(primary.settings, primary.root_dir)
    injected = {"done": False}
    ksf_index = next(
        i for i, (t, _p, _n, m) in enumerate(_GRAPH_DRAIN_STEPS)
        if t == "knowledge_source_facts" and m
    )
    original_spy = _spy

    def _inject_then_spy(db, nb, step, limit):
        # codex #663 R14 P2 注入:游标已越过 ksfe 步、事实页即将跑——此刻经
        # 独立连接灌 4 条新子行,模拟排水期放行的摄取写。变异钉:把事实页里
        # 的显式子行删除拆掉 → 这 4 条被 FK 级联无声吞掉,counts 短 4,报红。
        if step == ksf_index and not injected["done"]:
            injected["done"] = True
            now3 = _now()
            with independent.write() as w:
                for j in range(4):
                    w.execute(
                        "INSERT INTO knowledge_source_fact_elements (fact_id,"
                        "notebook_id,source_id,source_generation,element_id,"
                        "created_at) VALUES (?,?,?,?,?,?)",
                        ("fact-0", nb, "src-doc", "g1", f"el-late-{j}", now3),
                    )
        return original_spy(db, nb, step, limit)

    monkeypatch.setattr(store, "drain_notebook_graph_rows_page", _inject_then_spy)
    counts = repo.delete_notebook_kg(notebook.id)

    assert injected["done"], "前置不成立:没有触发注入"
    assert counts.get("knowledge_source_fact_elements", 0) == 24, (
        "事实页必须显式计数删除迟到子行(20 播种 + 4 注入),级联不得无声吞掉:"
        f"{counts.get('knowledge_source_fact_elements')}"
    )

    pre_kos_steps = [
        i for i, (t, _p, _n, m) in enumerate(_GRAPH_DRAIN_STEPS)
        if t == "knowledge_object_sources" and not m
    ]
    assert len(pre_kos_steps) == 1
    pre_pages = [d for s_, d in pages if s_ == pre_kos_steps[0]]
    assert pre_pages, "高扇出 provenance 行必须由预排水步先行分页排掉"
    assert all(d.get("knowledge_object_sources", 0) <= 3 for d in pre_pages), (
        "预排水页必须守自己的行预算"
    )
    ksfe_steps = [
        i for i, (t, _p, _n, _m) in enumerate(_GRAPH_DRAIN_STEPS)
        if t == "knowledge_source_fact_elements"
    ]
    assert len(ksfe_steps) == 1
    ksfe_pages = [d for s_, d in pages if s_ == ksfe_steps[0]]
    assert ksfe_pages, "级联子表 ksfe 必须由自己的排水步先行分页排掉"
    assert all(
        d.get("knowledge_source_fact_elements", 0) <= 3 for d in ksfe_pages
    ), "ksfe 排水页必须守自己的行预算"
    # 事实页/终局不再有超预算的级联余地:排水后 ksfe 残余 ≤阈值,且
    # counts 契约覆盖级联残余(codex #663 R9 P2:终局显式计数 DELETE,
    # 级联找不到东西删)。
    assert _count(
        repo,
        "SELECT COUNT(*) FROM knowledge_source_fact_elements WHERE notebook_id=?",
        (notebook.id,),
    ) == 0
    # ko 页的事务内级联只剩残余(预排水已把 20 行吃到 ≤阈值)。
    ko_pages = [
        d for s_, d in pages
        if _GRAPH_DRAIN_STEPS[s_][0] == "knowledge_objects" and _GRAPH_DRAIN_STEPS[s_][3]
    ]
    assert ko_pages, "前置不成立:没有 ko 页"
    assert all(d.get("knowledge_object_sources", 0) <= 3 for d in ko_pages), (
        f"ko 页的级联不得吞下超预算的从属行:{ko_pages}"
    )


def test_tombstone_mid_drain_aborts_like_the_other_checkpoints(repo, monkeypatch):
    """变异钉:把排水循环里的 `_notebook_deleting` 检查点删掉 → 墓碑落地后
    排水照跑到底,报红。删除作业的相位 3 拥有这些行,维护路径应当就地停手。"""
    monkeypatch.setattr(
        repo._runtime.knowledge_lifecycle, "_graph_drain_budget_rows", 3
    )
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


def test_small_graph_tombstone_still_aborts_before_the_final_reset(
    repo, monkeypatch,
):
    """变异钉(codex #663 R11 P2):把终局前的墓碑检查点删掉 → 低于阈值的图
    (排水循环从未进入,批内检查点够不着)在笔记本已 tombstone 时照样跑终局
    reset,报红。删除作业的 quiesce 正等着这个 abort。"""
    service = repo._runtime.knowledge_lifecycle
    monkeypatch.setattr(service, "_graph_drain_budget_rows", 3)
    notebook = repo.create_notebook(NotebookCreate(name="small-tomb"))
    _seed_graph(repo, notebook.id, doc_objects=2)  # 低于阈值:零排水批

    monkeypatch.setattr(service, "_notebook_deleting", lambda _nb: True)
    with pytest.raises(NotebookDeletingAbortsMaintenanceError):
        repo.delete_notebook_kg(notebook.id)
    assert _count(
        repo,
        "SELECT COUNT(*) FROM knowledge_objects WHERE notebook_id=?",
        (notebook.id,),
    ) == 2, "墓碑已落时终局 reset 不得执行"


def test_final_reset_pins_isolation_as_the_first_statement(repo, monkeypatch):
    """变异钉(codex #663 R12 P1):把终局事务里的 begin_graph_reset_isolation
    调用删掉(或挪到探针之后)→ 首语句不再是隔离钉,报红。PG 侧语句形由
    同名 static 的 fake-db 断言钉住:REPEATABLE READ 快照钉在第一条语句,
    事务内探针才原子界定其后所有 DELETE。"""
    service = repo._runtime.knowledge_lifecycle
    notebook = repo.create_notebook(NotebookCreate(name="iso"))
    _seed_graph(repo, notebook.id, doc_objects=2)

    order = []
    store = repo._runtime.knowledge
    original_iso = store.begin_graph_reset_isolation
    original_backlog = store.graph_drain_backlog

    def _iso_spy(db):
        order.append("isolation")
        return original_iso(db)

    def _backlog_spy(db, nb, threshold, start=0, **kw):
        order.append(("probe", start))
        return original_backlog(db, nb, threshold, start, **kw)

    monkeypatch.setattr(store, "begin_graph_reset_isolation", _iso_spy)
    monkeypatch.setattr(store, "graph_drain_backlog", _backlog_spy)
    repo.delete_notebook_kg(notebook.id)

    # 排水前的只读探针(connect 连接)合法地先行;终局事务内的序必须是
    # isolation 紧跟 start=0 复验探针。
    assert "isolation" in order, f"终局必须钉隔离级:{order}"
    iso_at = order.index("isolation")
    assert iso_at + 1 < len(order) and order[iso_at + 1] == ("probe", 0), (
        f"隔离钉必须先于终局事务内的复验探针:{order}"
    )

    # PG 侧的语句形(fake db,无需真库):
    from app.repositories.postgres.knowledge_store import (
        KnowledgeStore as PgStore,
    )

    executed = []

    class _FakeDb:
        def execute(self, sql, params=()):
            executed.append(" ".join(str(sql).split()))
            return None

    PgStore.begin_graph_reset_isolation(_FakeDb())
    assert executed == ["SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"]

    # sqlite 侧(codex #663 R15 P2a):跨进程原子性靠 BEGIN IMMEDIATE,
    # 不是进程内写锁。
    from app.repositories.sqlite.knowledge_store import (
        KnowledgeStore as SqStore,
    )

    executed.clear()
    SqStore.begin_graph_reset_isolation(_FakeDb())
    assert executed == ["BEGIN IMMEDIATE"]

    # PG 特例页(codex #663 R15 P2b):选页必须 FOR UPDATE,并发替换等页提交。
    import inspect

    page_src = inspect.getsource(PgStore.drain_notebook_graph_rows_page)
    # codex #663 R20 P1:ko 页刻意**不再** FOR UPDATE(与既定删除路径同序
    # 取锁,防 40P01 环);ksf 页保留(父→子与级联同序,无反转)。
    assert page_src.count("FOR UPDATE\"") == 1, (
        "只有 ksf 选页保留 FOR UPDATE;ko 页按写者同序取锁不上前置行锁"
    )
    ksf_at = page_src.index("knowledge_source_facts WHERE {predicate}")
    assert "FOR UPDATE" in page_src[ksf_at:ksf_at + 120]


def _certificate(repo, notebook_id):
    with repo._runtime.database.connect() as db:
        return repo._runtime.knowledge.source_index_backfilled(db, notebook_id)


def test_certificate_revoked_on_interrupt_and_restored_on_success(
    repo, monkeypatch,
):
    """变异钉两问(codex #663 R16 P1a):
    ① 把排水首页的 clear_source_index_backfilled 删掉 → 中断的排水留下
    「已认证却不完整」的反向索引(kos 行没了、证书还在),报红;
    ② 把终局回授改回事务内读(或删掉)→ 成功路径丢证书,报红。"""
    service = repo._runtime.knowledge_lifecycle
    monkeypatch.setattr(service, "_graph_drain_budget_rows", 3)
    notebook = repo.create_notebook(NotebookCreate(name="cert"))
    _seed_graph(repo, notebook.id, doc_objects=10)
    with repo._runtime.database.write() as db:
        repo._runtime.knowledge.mark_source_index_backfilled(db, notebook.id)
    assert _certificate(repo, notebook.id) is True

    # ① 中断:第二批起墓碑落地 → 排水停手,证书必须已吊销。
    calls = {"n": 0}

    def _deleting_after_first(_nb):
        calls["n"] += 1
        return calls["n"] > 1

    monkeypatch.setattr(service, "_notebook_deleting", _deleting_after_first)
    with pytest.raises(NotebookDeletingAbortsMaintenanceError):
        repo.delete_notebook_kg(notebook.id)
    assert _certificate(repo, notebook.id) is False, (
        "中断的排水必须留下已吊销的证书(反向索引已不完整)"
    )

    # ② 成功路径:重放证书后完整删除 → 终局按排水前快照回授。
    with repo._runtime.database.write() as db:
        repo._runtime.knowledge.mark_source_index_backfilled(db, notebook.id)
    monkeypatch.setattr(service, "_notebook_deleting", lambda _nb: False)
    repo.delete_notebook_kg(notebook.id)
    assert _certificate(repo, notebook.id) is True, (
        "成功终局必须按排水前快照回授证书"
    )


def test_post_reset_validation_catches_snapshot_invisible_commits(
    repo, monkeypatch,
):
    """变异钉(codex #663 R16 P2):把终局提交后的幸存者复验删掉 → RR 快照
    看不见的事中提交行在 delete 返回后仍留在库里、totals 短计,报红。注入:
    包住 delete_notebook_graph_rows,原始终局跑完后经独立连接插 4 行,模拟
    「快照之后、提交之前」落地的并发写。"""
    service = repo._runtime.knowledge_lifecycle
    monkeypatch.setattr(service, "_graph_drain_budget_rows", 3)
    notebook = repo.create_notebook(NotebookCreate(name="survivors"))
    _seed_graph(repo, notebook.id, doc_objects=10)

    from app.repositories.sqlite.database import SqliteDatabase

    primary = repo._runtime.database
    independent = SqliteDatabase(primary.settings, primary.root_dir)
    store = repo._runtime.knowledge
    original_backlog = store.graph_drain_backlog
    injected = {"done": False}

    def _inject_before_survivor_probe(db, nb, threshold, start=0, **kw):
        # threshold==0 只出现在终局提交后的幸存者复验——首调之前注入 4 行,
        # 模拟「终局快照之后落地、DELETE 看不见」的并发提交(此刻终局已提交,
        # 独立连接不会撞写锁)。
        if threshold == 0 and not injected["done"]:
            injected["done"] = True
            now2 = _now()
            # 只注 2 行(< 预算 3):重试轮的排水零页,轮间驱逐只能来自
            # 幸存者分支的显式驱逐——变异(删它)才真正无 evict 可补位。
            with independent.write() as w:
                for i in range(2):
                    w.execute(
                        "INSERT INTO knowledge_objects (id,notebook_id,"
                        "object_type,status,owner,payload,evidence,source_id,"
                        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (f"ko-surv-{i}", nb, "concept", "approved", "", "{}",
                         "[]", "src-doc", now2, now2),
                    )
        return original_backlog(db, nb, threshold, start, **kw)

    monkeypatch.setattr(store, "graph_drain_backlog", _inject_before_survivor_probe)

    events = []
    service2 = repo._runtime.knowledge_lifecycle
    original_evict = service2._invalidate_unified_cache
    original_final2 = store.delete_notebook_graph_rows

    def _evict_spy(nb):
        events.append("evict")
        return original_evict(nb)

    def _final_spy(db, nb, now):
        events.append("final")
        return original_final2(db, nb, now)

    monkeypatch.setattr(service2, "_invalidate_unified_cache", _evict_spy)
    monkeypatch.setattr(store, "delete_notebook_graph_rows", _final_spy)
    counts = repo.delete_notebook_kg(notebook.id)

    # codex #663 R17 P1b:两轮终局之间必须有一次驱逐(已提交的 reset 不许把
    # 温缓存留到重试排水期)。变异钉:把幸存者分支的驱逐删掉 → 两个 final
    # 之间没有 evict,报红。
    finals = [i for i, e in enumerate(events) if e == "final"]
    assert len(finals) >= 2, f"前置不成立:应有两轮终局:{events}"
    assert any(
        events[i] == "evict" for i in range(finals[0] + 1, finals[1])
    ), f"两轮终局之间必须驱逐温缓存:{events}"

    assert injected["done"], "前置不成立:没有触发注入"
    assert counts["knowledge_objects"] == 12, (
        f"totals 必须覆盖复验轮清掉的幸存者:{counts['knowledge_objects']}"
    )
    assert _count(
        repo,
        "SELECT COUNT(*) FROM knowledge_objects WHERE notebook_id=?",
        (notebook.id,),
    ) == 0, "终局后复验必须清掉快照不可见的提交行"


def test_drain_stall_raises_loudly(repo, monkeypatch):
    """变异钉:把「3 次连续零删响亮失败」改成静默继续 → 排水循环失去终止
    条件(本用例以 RuntimeError 类型断言判红;仓库没有 pytest-timeout,
    真跑死循环只会挂到 CI 任务级超时,所以辨别信号是这里的异常断言本身)。"""
    monkeypatch.setattr(
        repo._runtime.knowledge_lifecycle, "_graph_drain_budget_rows", 3
    )
    notebook = repo.create_notebook(NotebookCreate(name="stall"))
    _seed_graph(repo, notebook.id, doc_objects=10)

    store = repo._runtime.knowledge
    monkeypatch.setattr(
        store, "drain_notebook_graph_rows_page",
        lambda db, nb, step, limit: {},
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

    mirrored = [(t, p) for t, p, _n, m in steps if m]
    assert len(deletes) == len(mirrored), (
        f"终局 DELETE 语句数({len(deletes)})与登记表镜像条数({len(mirrored)})不一致"
    )
    for (table, predicate), statement in zip(mirrored, deletes):
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
    registry_tables = {t for t, _p, _n, m in _GRAPH_DRAIN_STEPS if m}
    assert registry_tables >= _GRAPH_RESET_TABLES, (
        "排水登记表漏掉了 _GRAPH_RESET_TABLES 的成员:"
        f"{sorted(_GRAPH_RESET_TABLES - registry_tables)}"
    )


# ---------------------------------------------------------------------------
# K1 (debt-fix 2026-10-12): dead-ref conflict candidates, the merge-review
# row, and the rebuild-derived tables a delete deliberately keeps (ruling 11)
# ---------------------------------------------------------------------------

_DERIVED = (
    "canonical_relations", "communities", "community_members",
    "concept_comentions", "mention_edges",
)


def _seed_derived(repo, notebook_id, n, *, generation=1, prefix="d"):
    """``n`` rows in each of the five rebuild-derived tables (direct inserts,
    keys sortable, communities/members in ``generation``)."""
    now = _now()
    with repo._runtime.database.write() as db:
        for i in range(n):
            db.execute(
                "INSERT INTO canonical_relations (notebook_id,canonical_src,"
                "edge_type,canonical_tgt,updated_at) VALUES (?,?,?,?,?)",
                (notebook_id, f"K-{prefix}{i:03d}", "supports",
                 f"K-{prefix}t{i:03d}", now),
            )
            db.execute(
                "INSERT INTO mention_edges (notebook_id,claim_object_id,"
                "concept_canonical_id) VALUES (?,?,?)",
                (notebook_id, f"ko-{prefix}c{i:03d}", f"K-{prefix}{i:03d}"),
            )
            db.execute(
                "INSERT INTO concept_comentions (notebook_id,canonical_a,"
                "canonical_b,bridge_claims) VALUES (?,?,?,3)",
                (notebook_id, f"K-{prefix}{i:03d}", f"K-{prefix}z{i:03d}"),
            )
            db.execute(
                "INSERT INTO communities (id,notebook_id,level,member_ids,size,"
                "created_at,generation) VALUES (?,?,0,'[]',3,?,?)",
                (f"cm-{prefix}{i:03d}", notebook_id, now, generation),
            )
            db.execute(
                "INSERT INTO community_members (canonical_id,notebook_id,level,"
                "community_id,canonical_name,centrality,generation) "
                "VALUES (?,?,0,?,?,0.5,?)",
                (f"K-{prefix}{i:03d}", notebook_id, f"cm-{prefix}{i:03d}",
                 f"name{i}", generation),
            )


def _conflict(repo, notebook_id, cid, left, right, status="pending", kind="node"):
    now = _now()
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO kg_conflict_candidates (id,notebook_id,kind,left_ref,"
            "right_ref,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (cid, notebook_id, kind, left, right, status, now, now),
        )


def _merge_job(repo, notebook_id, status):
    now = _now()
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO merge_review_jobs (notebook_id,status,total,done,"
            "started_at,updated_at,error) VALUES (?,?,10,4,?,?,?)",
            (notebook_id, status, now, now,
             "LLM 预审连续无进展,已中止" if status == "failed" else ""),
        )


def _derived_counts(repo, notebook_id):
    return {
        t: _count(repo, f"SELECT COUNT(*) FROM {t} WHERE notebook_id=?",
                  (notebook_id,))
        for t in _DERIVED
    }


def test_conflict_candidates_dropped_only_when_a_ref_died(repo, monkeypatch):
    """K1 变异钉两问:① 谓词改成 ``status='pending'`` → applied/rejected 的
    失效候选留下,报红;② 改成整表删除 → 两个 Memory 对象(或 Memory 关系)
    之间的候选被误删,报红。任一侧引用已不存在即删,不分状态。"""
    monkeypatch.setattr(
        repo._runtime.knowledge_lifecycle, "_graph_drain_budget_rows", 3
    )
    notebook = repo.create_notebook(NotebookCreate(name="conflicts"))
    _seed_graph(repo, notebook.id, doc_objects=8, memory_objects=2)
    now = _now()
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO knowledge_relations (id,notebook_id,source_id,"
            "source_object_id,target_object_id,edge_type,evidence,created_at) "
            "VALUES ('rel-mem-0',?,'src-mem','ko-mem-0','ko-mem-1','supports','[]',?),"
            "('rel-mem-1',?,'src-mem','ko-mem-1','ko-mem-0','contradicts','[]',?)",
            (notebook.id, now, notebook.id, now),
        )
    # 失效:6 条文档对象之间的候选,三种状态都有(超预算,走排水页)。
    for i, status in enumerate(("applied", "rejected", "pending") * 2):
        _conflict(repo, notebook.id, f"kcc-dead-{i}", f"ko-doc-{i}",
                  f"ko-doc-{i + 1}", status)
    # 失效:一侧是已删文档对象、另一侧是保留的 Memory 对象。
    _conflict(repo, notebook.id, "kcc-half", "ko-mem-0", "ko-doc-7", "applied")
    # 保留:两侧都还在(Memory 对象 / Memory 关系)。
    _conflict(repo, notebook.id, "kcc-keep-node", "ko-mem-0", "ko-mem-1", "applied")
    _conflict(repo, notebook.id, "kcc-keep-edge", "rel-mem-0", "rel-mem-1",
              "pending", kind="edge")

    counts = repo.delete_notebook_kg(notebook.id)

    with repo._runtime.database.connect() as db:
        left = {
            row["id"] for row in db.execute(
                "SELECT id FROM kg_conflict_candidates WHERE notebook_id=?",
                (notebook.id,),
            ).fetchall()
        }
    assert left == {"kcc-keep-node", "kcc-keep-edge"}, left
    assert counts["kg_conflict_candidates"] == 7


def test_running_merge_review_job_row_survives_kg_delete(repo):
    """K1 变异钉:去掉 ``status<>'running'`` → 运行中的预审作业行被删,单飞
    不变量失守,报红。非运行行(failed/done)随删除清掉,状态回到 idle——
    旧图谱的失败提示不再出现(有意的可见变化)。"""
    running = repo.create_notebook(NotebookCreate(name="mr-running"))
    failed = repo.create_notebook(NotebookCreate(name="mr-failed"))
    _seed_graph(repo, running.id, doc_objects=2)
    _seed_graph(repo, failed.id, doc_objects=2, tag="-f")
    _merge_job(repo, running.id, "running")
    _merge_job(repo, failed.id, "failed")
    governance = repo._runtime.knowledge_governance
    assert governance.merge_review_job_status(failed.id)["status"] == "failed"

    repo.delete_notebook_kg(running.id)
    repo.delete_notebook_kg(failed.id)

    assert governance.merge_review_job_status(running.id) == {
        "status": "running", "total": 10, "done": 4, "error": "",
    }
    assert governance.merge_review_job_status(failed.id) == {
        "status": "idle", "total": 0, "done": 0, "error": "",
    }


def test_drain_only_steps_stay_out_of_the_post_commit_survivor_check(
    repo, monkeypatch,
):
    """K1 变异钉(「新步骤参与复查」):冲突候选 / 合并预审两步若参与终局提交后
    的阈值 0 复查 → 提交与复查之间落地的这类行(冲突作业写回、预审作业
    running→done、Knowhow 投影器删对象)每次都多烧一轮重置,报红(终局轮数
    变 2)。它们改由复查之后的一次清扫带走;复查对文档图本身照旧(见
    ``test_post_reset_validation_catches_snapshot_invisible_commits``)。"""
    service = repo._runtime.knowledge_lifecycle
    monkeypatch.setattr(service, "_graph_drain_budget_rows", 3)
    from app.repositories.sqlite.database import SqliteDatabase

    primary = repo._runtime.database
    independent = SqliteDatabase(primary.settings, primary.root_dir)
    store = repo._runtime.knowledge
    original_backlog = store.graph_drain_backlog
    original_final = store.delete_notebook_graph_rows

    def _run(name, inject):
        notebook = repo.create_notebook(NotebookCreate(name=name))
        _seed_graph(repo, notebook.id, doc_objects=2, tag=f"-{name}")
        injected = {"done": False}
        finals = []

        def _backlog(db, nb, threshold, start=0, **kw):
            if threshold == 0 and not injected["done"]:
                injected["done"] = True
                now = _now()
                with independent.write() as w:
                    inject(w, nb, now)
            return original_backlog(db, nb, threshold, start, **kw)

        def _final(db, nb, now):
            finals.append(nb)
            return original_final(db, nb, now)

        monkeypatch.setattr(store, "graph_drain_backlog", _backlog)
        monkeypatch.setattr(store, "delete_notebook_graph_rows", _final)
        repo.delete_notebook_kg(notebook.id)
        assert injected["done"], "前置不成立:没有触发注入"
        return notebook.id, finals

    def _late_conflict_and_finished_review(w, nb, now):
        w.execute(
            "INSERT INTO kg_conflict_candidates (id,notebook_id,kind,left_ref,"
            "right_ref,status,created_at,updated_at) "
            "VALUES ('kcc-late',?,'node','ko-gone-a','ko-gone-b','pending',?,?)",
            (nb, now, now),
        )
        w.execute(
            "INSERT INTO merge_review_jobs (notebook_id,status,total,done,"
            "started_at,updated_at,error) VALUES (?,'done',3,3,?,?,'')",
            (nb, now, now),
        )

    nb_id, finals = _run("settle-exempt", _late_conflict_and_finished_review)
    assert len(finals) == 1, (
        f"冲突候选/预审行不得参与提交后复查(多烧了重置轮):{len(finals)}"
    )
    # 质量评审 P3-1:这类行由提交后的一次清扫带走(不计重试轮)。变异钉:拿掉
    # 清扫 → 失效候选与 done 行留到下一次删除,报红。
    assert _count(repo, "SELECT COUNT(*) FROM kg_conflict_candidates "
                  "WHERE notebook_id=?", (nb_id,)) == 0
    assert repo._runtime.knowledge_governance.merge_review_job_status(nb_id)[
        "status"] == "idle"


def test_derived_reads_after_delete_match_a_graph_that_never_had_them(repo):
    """K1(裁决 11):删除作业刻意**不清**派生表,这里证明留下的派生行无害——
    删除后,读者入口(``resolve_focal`` → 社区同伴 / 共提同伴)与按已发布代
    直读,在「有派生残留」的库和「从没建过派生表」的库上逐值相同:
    concept_clusters 已清,focal 解析不出;社区读按已发布代,而重置把指针拨回
    代 0。变异钉:若删除不再清 concept_clusters 或不再重置代指针,残留的
    社区 / 共提同伴重新可达,报红。"""
    from app.services.communities import CommunityQueryService

    rt = repo._runtime
    deleted = repo.create_notebook(NotebookCreate(name="had-derived"))
    clean = repo.create_notebook(NotebookCreate(name="never-derived"))
    _seed_graph(repo, deleted.id, doc_objects=3, memory_objects=1, clusters=True)
    _seed_graph(repo, clean.id, doc_objects=3, memory_objects=1, clusters=True,
                tag="-c")
    with rt.database.write() as db:
        db.execute(
            "UPDATE concept_clusters SET canonical_id='K-g1000' WHERE notebook_id=?",
            (deleted.id,),
        )
    _seed_derived(repo, deleted.id, 4, generation=1, prefix="g1")
    with rt.database.write() as db:
        db.execute(
            "INSERT INTO unified_kg_state (notebook_id,community_generation,"
            "updated_at) VALUES (?,1,?) ON CONFLICT(notebook_id) DO UPDATE SET "
            "community_generation=1", (deleted.id, _now()),
        )

    service = CommunityQueryService(
        notebooks=rt.notebook_store, unified_kg=rt.unified_kg,
        event_log=rt.event_log, sibling_min_bridge=1,
    )

    def _reads(nb):
        uk = rt.unified_kg
        return {
            "focal": [uk.resolve_focal(nb, k) for k in ("canon", "name0", "name1")],
            "community_peers": [
                service.community_peers(nb, k, "q", top_k=5, candidates=10)
                for k in ("Canon", "name0")
            ],
            "sibling_peers": [service.sibling_peers(nb, k) for k in ("Canon", "name0")],
            "top_community": [
                uk.top_community_for(nb, f"K-g1{i:03d}") for i in range(4)
            ],
            "member_peers": [
                [dict(r) for r in uk.community_member_peers(nb, f"cm-g1{i:03d}", "", 10)]
                for i in range(4)
            ],
            "comention_peers": [
                uk.comention_peers(nb, f"K-g1{i:03d}", 1, 10) for i in range(4)
            ],
        }

    # 前置:删除之前,派生行真的可达(否则下面的相等毫无意义)。
    before = _reads(deleted.id)
    assert before["focal"][0] == "K-g1000"
    assert before["top_community"][0] == "cm-g1000"
    assert before["member_peers"][0], before

    repo.delete_notebook_kg(deleted.id)
    repo.delete_notebook_kg(clean.id)
    assert _derived_counts(repo, deleted.id) == dict.fromkeys(_DERIVED, 4), (
        "前置:派生行按裁决 11 保留"
    )
    assert _reads(deleted.id) == _reads(clean.id)


def test_kg_delete_leaves_derived_tables_until_the_next_rebuild(repo, monkeypatch):
    """K1(裁决 11)变异钉:删除作业不触碰五张派生表(任何一张出现在登记表或
    终局里 → 行数 / 内容变化,报红,同时镜像钉也会红);下一次重建把它们整体
    替换掉。超阈值的图也一样(排水页里没有派生表)。"""
    monkeypatch.setattr(
        repo._runtime.knowledge_lifecycle, "_graph_drain_budget_rows", 3
    )
    notebook = repo.create_notebook(NotebookCreate(name="derived-kept"))
    _seed_graph(repo, notebook.id, doc_objects=10, clusters=True)
    _seed_derived(repo, notebook.id, 10)

    def _snapshot():
        with repo._runtime.database.connect() as db:
            return {
                t: [tuple(r) for r in db.execute(
                    f"SELECT * FROM {t} WHERE notebook_id=? ORDER BY 1, 2, 3, 4",
                    (notebook.id,),
                ).fetchall()]
                for t in _DERIVED
            }

    before = _snapshot()
    pages = []
    store = repo._runtime.knowledge
    original = store.drain_notebook_graph_rows_page

    def _spy(db, nb, step, limit):
        deleted = original(db, nb, step, limit)
        pages.append(dict(deleted))
        return deleted

    monkeypatch.setattr(store, "drain_notebook_graph_rows_page", _spy)
    counts = repo.delete_notebook_kg(notebook.id)

    assert pages, "前置不成立:超阈值的图必须走排水"
    assert not any(t in d for d in pages for t in _DERIVED)
    assert not any(t in counts for t in _DERIVED)
    assert _snapshot() == before, "删除作业不得触碰派生表"

    repo._runtime.knowledge_lifecycle.rebuild_unified_kg(notebook.id, force=True)
    assert _derived_counts(repo, notebook.id) == dict.fromkeys(_DERIVED, 0), (
        "重建必须整体替换删除留下的派生行"
    )


def test_drain_registry_keeps_derived_tables_and_exempts_two_steps():
    """K1:两端登记表——复查豁免恰好是冲突候选与合并预审两步且都在登记表里;
    派生表(裁决 11)与重建检查点、白名单、两张 scratch 表都不在。"""
    from app.repositories.postgres import knowledge_store as pg
    from app.repositories.sqlite import knowledge_store as sq

    for module in (pg, sq):
        registry = [t for t, _p, _n, m in module._GRAPH_DRAIN_STEPS if m]
        assert module._GRAPH_DRAIN_SETTLE_EXEMPT == {
            "kg_conflict_candidates", "merge_review_jobs",
        }
        assert module._GRAPH_DRAIN_SETTLE_EXEMPT <= set(registry)
        for kept in (*_DERIVED, "kg_rebuild_checkpoint", "concept_whitelist",
                     "kg_cluster_scratch", "kg_canonical_scratch"):
            assert kept not in registry, f"{kept} 不得进入删除清单"


def _reextract(repo, notebook_id, canonical_id, name, member, source_id):
    """What re-extraction + incremental fusion write after a delete: a new
    object and a generation-0 cluster row under the SAME deterministic
    canonical id (``"K-" + _norm(name)``) the deleted graph used."""
    now = _now()
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,"
            "payload,evidence,source_id,created_at,updated_at) "
            "VALUES (?,?,'concept','approved','','{}','[]',?,?,?)",
            (member, notebook_id, source_id, now, now),
        )
        db.execute(
            "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,object_type,created_at) VALUES (?,?,?,?,?,'concept',?)",
            (f"cc-{member}", notebook_id, canonical_id, member, name, now),
        )


def _derived_reads(repo, notebook_id):
    from app.services.communities import CommunityQueryService

    rt = repo._runtime
    uk = rt.unified_kg
    service = CommunityQueryService(
        notebooks=rt.notebook_store, unified_kg=uk, event_log=rt.event_log,
        sibling_min_bridge=1,
    )
    with rt.database.connect() as db:
        direct = {
            "weak_support": [dict(r) for r in uk.weak_support_relation_rows(
                db, notebook_id, ["K-g1000", "K-000"], 10, 10)],
            "relation_support": [dict(r) for r in uk.relation_support_rows(
                db, notebook_id, [("K-g1000", "supports", "K-g1t000"),
                                  ("K-000", "supports", "K-t000")])],
            "edge_support": [tuple(r) for r in uk.edge_support_rows(db, notebook_id)],
            "mention": [tuple(r) for r in uk.mention_rows(db, notebook_id)],
            "communities": uk.communities_count(db, notebook_id, 0),
            "community_members": uk.community_member_ids(db, notebook_id, 0),
        }
    return {
        **direct,
        "sibling_peers": [service.sibling_peers(notebook_id, k)
                          for k in ("foo", "legacy")],
        "community_peers": [
            service.community_peers(notebook_id, k, "q", top_k=5, candidates=10)
            for k in ("foo", "legacy")],
        "top_community": [uk.top_community_for(notebook_id, c)
                          for c in ("K-g1000", "K-000")],
        "member_peers": [
            [dict(r) for r in uk.community_member_peers(notebook_id, c, "", 10)]
            for c in ("cm-g1000", "cm-000")],
        "comention_peers": [uk.comention_peers(notebook_id, c, 1, 10)
                            for c in ("K-g1000", "K-000")],
    }


def test_derived_layers_stay_unread_after_delete_and_reextraction(repo):
    """K1 读侧闸(规格评审 P2-1 / P3-1)。删除留下派生表(裁决 11),而重抽同名
    概念会铸出同一个规范 id 并以代 0 入簇——于是共提同伴、关系支撑、提及边、
    以及迁移前遗留的代 0 社区,都会重新挂到新图上。闸:删除签名(纪元 > 0、
    该层 seq = -1、``last_rebuild_at`` 空)下,每个派生层读者都视为没有派生
    数据。断言:删除 → 重抽同名 → 每个读者的结果与「从没建过派生表」的库逐值
    相同。变异钉:拿掉任一读者的闸 / 把社区代改回原表达式 → 报红。"""
    had = repo.create_notebook(NotebookCreate(name="had-derived"))
    never = repo.create_notebook(NotebookCreate(name="never-derived"))
    _seed_graph(repo, had.id, doc_objects=2, clusters=True)
    _seed_graph(repo, never.id, doc_objects=2, clusters=True, tag="-c")
    _seed_derived(repo, had.id, 3, generation=1, prefix="g1")
    _seed_derived(repo, had.id, 3, generation=0, prefix="")  # 迁移前遗留代 0
    repo.delete_notebook_kg(had.id)
    repo.delete_notebook_kg(never.id)
    for nb, src, tag in ((had.id, "src-doc", "h"), (never.id, "src-doc-c", "n")):
        _reextract(repo, nb, "K-g1000", "foo", f"ko-re-{tag}-1", src)
        _reextract(repo, nb, "K-g1z000", "bar", f"ko-re-{tag}-2", src)
        _reextract(repo, nb, "K-000", "legacy", f"ko-re-{tag}-3", src)
        _reextract(repo, nb, "K-z000", "legacy-peer", f"ko-re-{tag}-4", src)

    assert _derived_counts(repo, had.id) == dict.fromkeys(_DERIVED, 6), (
        "前置:派生行按裁决 11 保留"
    )
    assert _derived_reads(repo, had.id) == _derived_reads(repo, never.id)


def test_derived_layer_gate_reopens_after_a_rebuild(repo):
    """K1 读侧闸只管「删除之后、重建之前」:重建写下 ``last_rebuild_at`` 与各层
    seq 后,派生层恢复可读。用重建之后再落一行派生数据来钉住(闸关着时这一行
    读不到,重建后读得到)。变异钉:闸的签名不含 ``last_rebuild_at`` / seq →
    重建后仍读不到,报红。"""
    rt = repo._runtime
    nb = repo.create_notebook(NotebookCreate(name="rebuilt"))
    _seed_graph(repo, nb.id, doc_objects=2, clusters=True)
    repo.delete_notebook_kg(nb.id)
    _seed_derived(repo, nb.id, 1, generation=0, prefix="late")
    triple = ("K-late000", "supports", "K-latet000")
    with rt.database.connect() as db:
        assert rt.unified_kg.relation_support_rows(db, nb.id, [triple]) == []
        assert rt.unified_kg.mention_rows(db, nb.id) == []
    rt.knowledge_lifecycle.rebuild_unified_kg(nb.id, force=True)
    with rt.database.connect() as db:
        state = rt.unified_kg.state_row(db, nb.id)
    assert state["last_rebuild_at"], "前置:重建写下 last_rebuild_at"
    _seed_derived(repo, nb.id, 1, generation=0, prefix="post")
    with rt.database.connect() as db:
        assert rt.unified_kg.relation_support_rows(
            db, nb.id, [("K-post000", "supports", "K-postt000")])
        assert rt.unified_kg.mention_rows(db, nb.id)
