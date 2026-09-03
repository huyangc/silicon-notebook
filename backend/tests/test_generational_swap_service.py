"""批 3·W2 PR-2 §5:代际切换在 service 层的行为契约。

覆盖设计验收里 store 引物 pin(tests/postgres/test_derived_generation_
primitives.py)兜不住的那半:数据级单飞拒绝、读者半态不可见、链 a(翻转
锚点窗口的并发 append 经催收闭合)、翻转后崩溃的欠账恢复、翻转驱动分区
签名失配(验收 8b),以及「翻转微事务里不许出现行级搬运」的语句形状守卫。
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.ports import KgMaintenanceAlreadyRunning
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _seed(repo, name="nb"):
    nb = repo.create_notebook(NotebookCreate(name=name))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept",
         "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept",
         "payload": {"name": "FinFET", "section_path": ""}, "evidence": []},
    ], [])
    return nb


def _state(repo, notebook_id):
    with repo._connect() as db:
        return dict(db.execute(
            "SELECT * FROM unified_kg_state WHERE notebook_id=?",
            (notebook_id,)).fetchone())


def test_rebuild_is_gated_by_the_data_level_claim(repo):
    """取号是数据级跨进程单飞:别处在飞(哪怕是离线 CLI 直连留下的认领),
    rebuild 立即被 KgMaintenanceAlreadyRunning('rebuild') 拒绝——不烧号、
    不动 checkpoint、不写任何东西。"""
    nb = _seed(repo)
    store = repo._runtime.unified_kg
    with repo._write() as db:
        claim = store.claim_derived_generation(db, nb.id, ttl_seconds=3600)
        assert claim is not None
    counter_before = _state(repo, nb.id)["derived_generation_counter"]
    with pytest.raises(KgMaintenanceAlreadyRunning) as exc:
        repo.rebuild_unified_kg(nb.id, force=True)
    assert exc.value.holder == "rebuild"
    after = _state(repo, nb.id)
    assert after["derived_generation_counter"] == counter_before, "被拒不烧号"
    assert after["derived_building_generation"] == claim["generation"], (
        "被拒不得动别人的在飞认领")
    with repo._write() as db:
        store.release_derived_claim(db, nb.id, claim["generation"])


def test_readers_never_see_an_unpublished_generation(repo):
    """读侧永不见半态:未发布代的行(哪怕已经落库)对 cluster_map/计数读者
    整体不可见;翻转(此处经完整 rebuild)之后才可见。这是「写新代不持锁、
    想写多久写多久」的全部前提。"""
    nb = _seed(repo)
    repo.rebuild_unified_kg(nb.id, force=True)
    published = repo.cluster_map(nb.id)
    assert published, "前置:published 代非空"
    gen = _state(repo, nb.id)["cluster_generation"]
    with repo._write() as db:
        db.execute(
            "INSERT INTO concept_clusters (id,notebook_id,canonical_id,"
            "member_object_id,canonical_name,object_type,created_at,generation) "
            "VALUES ('cc-halfstate',?,?,?,?,?,?,?)",
            (nb.id, "K-ghost", "ko-ghost", "Ghost", "concept",
             "2026-01-01T00:00:00", gen + 41),
        )
    repo._runtime.knowledge_lifecycle._invalidate_unified_cache(nb.id)
    assert repo.cluster_map(nb.id) == published, (
        "未发布代的行泄漏进了 published 读者——半态可见")


def test_chain_a_append_during_the_build_window_survives_the_flip(repo, monkeypatch):
    """链 a 闭合(设计 §1.5):写段期间落进退休代的融合 append,翻转后由
    催收单遍搬运进新 published 代——删掉催收段,这个成员就随退休代一起
    消失。注入点选在写段(翻转锚点之后、翻转之前),行落当时的 published 代
    (指针在锁后读,见 insert_clusters 的锁序红线)。"""
    nb = _seed(repo)
    repo.rebuild_unified_kg(nb.id, force=True)
    service = repo._runtime.knowledge_lifecycle
    original_write_map = service._write_cluster_map_streamed
    fired = []
    late_ref: list[str] = []

    def inject_append_then_write(notebook_id, object_type, *args, **kwargs):
        if not fired:
            fired.append(True)
            # 并发融合写者,注入在 concept 的 scratch 流**已提交之后**、写代段
            # 之前:新对象因此不在本轮 rebuild 的输入快照里(不会被写代段顺手
            # 带进新代——否则本测试空洞),它进新 published 代的唯一通道就是
            # 催收。对象要真实入库:催收 join knowledge_objects 取 payload。
            repo.store_kg(notebook_id, None, [
                {"local_id": "late", "object_type": "concept",
                 "payload": {"name": "GAAFET", "section_path": ""},
                 "evidence": []},
            ], [])
            with repo._connect() as db:
                late_ref.append(db.execute(
                    "SELECT id FROM knowledge_objects WHERE notebook_id=? "
                    "AND json_extract(payload,'$.name')='GAAFET'",
                    (notebook_id,)).fetchone()["id"])
            # append 走锁后读指针的 insert_clusters → 落在此刻仍是 published
            # 的退休代。
            service.append_clusters(notebook_id, [{
                "canonical_id": "K-gaafet", "member_object_id": late_ref[0],
                "canonical_name": "GAAFET",
            }], object_type="concept")
        return original_write_map(notebook_id, object_type, *args, **kwargs)

    monkeypatch.setattr(service, "_write_cluster_map_streamed",
                        inject_append_then_write)
    retired_gen = _state(repo, nb.id)["cluster_generation"]
    repo.rebuild_unified_kg(nb.id, force=True)
    state = _state(repo, nb.id)
    assert fired and state["cluster_generation"] > retired_gen
    assert state["derived_catchup_from"] is None, "催收完成必须清锚点标记"
    with repo._connect() as db:
        rows = db.execute(
            "SELECT generation FROM concept_clusters WHERE notebook_id=? "
            "AND member_object_id=?", (nb.id, late_ref[0])).fetchall()
    gens = {int(r["generation"]) for r in rows}
    assert state["cluster_generation"] in gens, (
        f"窗口 append 的成员没被催收进新 published 代:{gens}")
    # 非空洞证明:退休代里也有它(append 真的落在了退休代,而不是被写代段
    # 顺手带进新代)。
    assert retired_gen in gens, gens
    # codex #671 R1 P2:催收新增了本轮聚类没见过的 canonical,持久化的
    # cluster_count 必须重算——按 published 代的 DISTINCT concept canonical
    # 口径与库里实际值逐字相等,不许低报。
    with repo._connect() as db:
        live = db.execute(
            "SELECT COUNT(DISTINCT canonical_id) AS c FROM concept_clusters "
            "WHERE notebook_id=? AND object_type='concept' AND generation=?",
            (nb.id, state["cluster_generation"])).fetchone()["c"]
    assert int(state["cluster_count"]) == int(live), (
        f"催收后 cluster_count 没重算:{state['cluster_count']} != {live}")


def test_crash_after_flip_settles_the_debt_on_the_next_round(repo, monkeypatch):
    """翻转后崩溃(锚点标记已落库、催收没跑)→ 下一轮取号先补欠账再干自己
    的活:欠账催收以「崩溃时的 published 代」为排除基准,清掉标记。标记不清,
    退休代永远不被回收(回收前置检查),库就只涨不缩。"""
    nb = _seed(repo)
    service = repo._runtime.knowledge_lifecycle
    original_settle = service._settle_generation_catchup
    calls: list[dict] = []

    def crashing_settle(notebook_id, **kwargs):
        calls.append(dict(kwargs))
        if len(calls) == 1:
            raise RuntimeError("simulated crash after flip")
        return original_settle(notebook_id, **kwargs)

    monkeypatch.setattr(service, "_settle_generation_catchup", crashing_settle)
    with pytest.raises(RuntimeError, match="simulated crash"):
        repo.rebuild_unified_kg(nb.id, force=True)
    crashed = _state(repo, nb.id)
    assert crashed["derived_catchup_from"] is not None, "锚点标记必须已落库"
    assert crashed["derived_building_generation"] == 0, "finally 释放必须已生效"
    flipped_to = crashed["cluster_generation"]

    repo.rebuild_unified_kg(nb.id, force=True)
    settled = _state(repo, nb.id)
    assert settled["derived_catchup_from"] is None
    assert settled["cluster_generation"] > flipped_to
    # 第 2 轮的两次催收:先补欠账(published 基准 = 崩溃轮翻到的代),
    # 再做自己翻转后的锚点窗口。
    assert len(calls) >= 3
    assert calls[1]["published_generation"] == flipped_to
    assert calls[1]["since_ts"] == crashed["derived_catchup_from"]
    assert calls[2]["published_generation"] == settled["cluster_generation"]


def test_the_skip_gate_settles_stranded_catchup_debt(repo, monkeypatch):
    """codex #671 R3 P1:force 重建翻转后崩溃、输入又没变时,后续 force=False
    刷新走 skip 短路——欠账若不在短路前补掉,启动恢复与预回收都按标记跳过,
    退休代永不回收、窗口成员永不发布。skip 分支必须先取号补欠账再返回。"""
    nb = _seed(repo)
    repo.rebuild_unified_kg(nb.id, force=True)          # 完整一轮,存下 _ver
    service = repo._runtime.knowledge_lifecycle
    calls: list[int] = []
    original_settle = service._settle_generation_catchup

    def crashing_settle(notebook_id, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("simulated crash after flip")
        return original_settle(notebook_id, **kwargs)

    monkeypatch.setattr(service, "_settle_generation_catchup", crashing_settle)
    with pytest.raises(RuntimeError, match="simulated crash"):
        repo.rebuild_unified_kg(nb.id, force=True)      # 输入未变的 force 重建
    assert _state(repo, nb.id)["derived_catchup_from"] is not None

    cached = repo.rebuild_unified_kg(nb.id)             # force=False → skip 短路
    assert cached >= 1
    state = _state(repo, nb.id)
    assert state["derived_catchup_from"] is None, (
        "skip 短路吞掉了欠账——退休代从此永不回收")
    assert state["derived_building_generation"] == 0, "补欠账的认领必须已释放"


def test_a_preempted_community_writer_aborts_before_writing(repo, monkeypatch):
    """codex #671 R3 P2:Louvain/预计算超 TTL 被抢占(或 standalone delete
    重置认领)后,communities 写代段必须先复读认领再写——不复读就把整代
    不可见行留给下一轮回收(生产量级百万行)。"""
    from app.repositories.ports import KgDerivedGenerationPreempted

    nb = _seed(repo)
    repo.rebuild_unified_kg(nb.id, force=True)
    service = repo._runtime.knowledge_lifecycle
    original_reap = service._reap_stale_community_generations

    def usurp_after_reap(notebook_id, **kwargs):
        result = original_reap(notebook_id, **kwargs)
        with repo._write() as db:
            db.execute(
                "UPDATE unified_kg_state SET "
                "derived_building_generation=derived_building_generation+7 "
                "WHERE notebook_id=?", (notebook_id,))
        return result

    monkeypatch.setattr(service, "_reap_stale_community_generations",
                        usurp_after_reap)
    events: list[dict] = []
    monkeypatch.setattr(repo._runtime.event_log, "emit",
                        lambda e: events.append(e))
    with repo._connect() as db:
        rows_before = db.execute(
            "SELECT COUNT(*) AS c FROM communities WHERE notebook_id=?",
            (nb.id,)).fetchone()["c"]
    with pytest.raises(KgDerivedGenerationPreempted):
        repo.rebuild_communities(nb.id, force=True)
    stages = [e["stage"] for e in events
              if e.get("kind") == "kg_generation_preempted"]
    assert stages == ["community_write"], stages
    with repo._connect() as db:
        rows_after = db.execute(
            "SELECT COUNT(*) AS c FROM communities WHERE notebook_id=?",
            (nb.id,)).fetchone()["c"]
    assert rows_after == rows_before, "被抢占后一行新代行都不许落"


def test_finish_rebuild_state_is_a_noop_once_the_pointer_moved_on(repo):
    """codex #671 R3 P2(收尾无主写回):翻转清认领后催收期间,另一进程可
    发布更新的代并收尾——旧 worker 的 finish 必须按「指针还是自己那代」
    条件化,失配即 no-op,不许拿旧代 metadata 盖掉新发布者的收尾。"""
    nb = _seed(repo)
    repo.rebuild_unified_kg(nb.id, force=True)
    before = _state(repo, nb.id)
    store = repo._runtime.unified_kg
    with repo._write() as db:
        store.finish_rebuild_state(
            db, nb.id, "stale-version", 999, "2026-02-02T00:00:00",
            published_generation=int(before["cluster_generation"]) + 5)
    unchanged = _state(repo, nb.id)
    assert unchanged["cluster_input_version"] == before["cluster_input_version"]
    assert unchanged["cluster_count"] == before["cluster_count"]
    with repo._write() as db:
        store.finish_rebuild_state(
            db, nb.id, "fresh-version", 7, "2026-02-02T00:00:00",
            published_generation=int(before["cluster_generation"]))
    applied = _state(repo, nb.id)
    assert applied["cluster_input_version"] == "fresh-version"
    assert applied["cluster_count"] == 7


def test_mention_seed_rows_read_only_the_published_generation(repo):
    """codex #671 R1 P1:翻转后退休代行留一轮宽限,mention_seed_rows(共提
    桥接的 canonical 名录)不配 published 谓词就会把新旧两代混在一起,发布
    指向已退休 canonical 的 mention 边。"""
    nb = _seed(repo)
    repo.rebuild_unified_kg(nb.id, force=True)
    published = _state(repo, nb.id)["cluster_generation"]
    with repo._connect() as db:
        member = db.execute(
            "SELECT member_object_id FROM concept_clusters WHERE notebook_id=? "
            "AND generation=? LIMIT 1", (nb.id, published)).fetchone()[0]
    with repo._write() as db:
        db.execute(
            "INSERT INTO concept_clusters (id,notebook_id,canonical_id,"
            "member_object_id,canonical_name,object_type,created_at,generation) "
            "VALUES ('cc-retired-seed',?,?,?,?,?,?,?)",
            (nb.id, "K-retired", member, "Retired", "concept",
             "2026-01-01T00:00:00", published + 9))
    with repo._connect() as db:
        clusters, _claims = repo._runtime.unified_kg.mention_seed_rows(db, nb.id)
    cids = {r["cid"] for r in clusters}
    assert "K-retired" not in cids, cids
    assert cids, "published 代的种子必须还在"


def test_a_crash_before_the_flip_releases_the_claim_via_finally(repo, monkeypatch):
    """释放通道 b 的专属 pin(复评 P0-2:缺一即整库 409 数小时)。崩溃注入
    在**翻转之前**——翻转成功自己会清认领(通道 a),把注入放在翻转之后的
    用例证明不了 finally 的存在;这里删掉 rebuild 的 finally 释放块必红。"""
    nb = _seed(repo)
    service = repo._runtime.knowledge_lifecycle

    def exploding_write(*args, **kwargs):
        raise RuntimeError("simulated crash before flip")

    monkeypatch.setattr(service, "_write_cluster_map_streamed", exploding_write)
    with pytest.raises(RuntimeError, match="before flip"):
        repo.rebuild_unified_kg(nb.id, force=True)
    state = _state(repo, nb.id)
    assert state["derived_building_generation"] == 0, (
        "finally CAS 释放没生效——认领要按 TTL(数小时)才解锁")
    assert state["derived_building_claimed_at"] is None
    assert state["derived_catchup_from"] is None, "没翻转就不该有欠账标记"
    # 认领立即可再取(而不是被拒到 TTL)。
    store = repo._runtime.unified_kg
    with repo._write() as db:
        again = store.claim_derived_generation(db, nb.id, ttl_seconds=3600)
        assert again is not None
        store.release_derived_claim(db, nb.id, again["generation"])


def test_a_preempted_writer_stops_at_the_next_type_boundary(repo, monkeypatch):
    """写段前复读的 service 级 pin(复评 P2-4):认领被抢走(TTL 抢占/
    delete 重置的化身)后,下一个 type 的写段必须当场作废早停——把
    `_write_cluster_map_streamed` 里的复读删掉,本轮会一路写到翻转才失败,
    这里断言的 `stage` 前缀当场对不上。"""
    nb = _seed(repo)
    service = repo._runtime.knowledge_lifecycle
    original_write_map = service._write_cluster_map_streamed
    events: list[dict] = []
    monkeypatch.setattr(repo._runtime.event_log, "emit",
                        lambda e: events.append(e))
    fired = []

    def usurp_then_write(notebook_id, object_type, *args, **kwargs):
        if not fired:
            fired.append(True)
            with repo._write() as db:
                # 模拟抢占者:把在飞认领改成别人的号。
                db.execute(
                    "UPDATE unified_kg_state SET "
                    "derived_building_generation=derived_building_generation+7 "
                    "WHERE notebook_id=?", (notebook_id,))
        return original_write_map(notebook_id, object_type, *args, **kwargs)

    monkeypatch.setattr(service, "_write_cluster_map_streamed", usurp_then_write)
    from app.repositories.ports import KgDerivedGenerationPreempted
    with pytest.raises(KgDerivedGenerationPreempted):
        repo.rebuild_unified_kg(nb.id, force=True)
    preempts = [e for e in events if e.get("kind") == "kg_generation_preempted"]
    assert preempts and preempts[0]["stage"].startswith("write:"), preempts


def test_a_flip_moves_the_source_partition_signature(repo):
    """验收 8b:SourceSubgraphSnapshot 的 ``cluster_generation``(实为
    cluster_mutation_seq 的同名异义投影,见 source_subgraph_projection 的
    交叉引用注释)必须被翻转推进——离线分区清单按签名逐字节比对,翻转后
    旧分区必须判 identity_mismatch,而不是把旧代簇继续端给读者。"""
    from app.repositories.source_subgraph_projection import (
        source_subgraph_signature_on,
    )
    nb = _seed(repo)
    with repo._connect() as db:
        src_ids = [r["id"] for r in db.execute(
            "SELECT DISTINCT source_id AS id FROM knowledge_objects "
            "WHERE notebook_id=? AND source_id != ''", (nb.id,)).fetchall()]
    with repo._connect() as db:
        before = source_subgraph_signature_on(
            db, nb.id, src_ids or ["src-x"], placeholder="?", postgres=False)
    repo.rebuild_unified_kg(nb.id, force=True)
    with repo._connect() as db:
        after = source_subgraph_signature_on(
            db, nb.id, src_ids or ["src-x"], placeholder="?", postgres=False)
    assert after[1] > before[1], (
        "翻转没有推进分区签名的 cluster 分量——旧离线分区会被当成新鲜的")


def test_flip_transactions_carry_no_row_migration_statements():
    """语句形状守卫(设计 §1.2):flip 原语的源码里不许出现 INSERT...SELECT /
    DELETE 这类行级搬运——cluster 侧翻转微事务持全 4 类 advisory lock,窗口
    必须毫秒级;community 侧虽不持 advisory lock、且骑在更大的发布事务里
    (D-W2-6 原子性优先),指针语句本身同样必须纯 CAS。登记的盲区:把搬运
    挪进被 flip 调用的 helper 能绕过本守卫(移动变异),那一侧由发布形态
    pin(test_kg_analysis_precompute 的事务分工断言)兜。"""
    import inspect

    from app.repositories.postgres.unified_kg_store import (
        UnifiedKgStore as PgStore,
    )
    from app.repositories.sqlite.unified_kg_store import (
        UnifiedKgStore as LiteStore,
    )
    for fn in (PgStore.flip_cluster_generation, PgStore.flip_community_generation,
               LiteStore.flip_cluster_generation,
               LiteStore.flip_community_generation):
        src = inspect.getsource(fn)
        assert "INSERT" not in src and "DELETE" not in src, (
            f"{fn.__qualname__} 的锁窗口里混进了行级搬运语句")
