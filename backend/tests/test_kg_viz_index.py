"""_viz_index 大库不同步构建:后台线程 + GET 返回 building 状态,小库行为不变。"""
import os
import shutil
import time
from pathlib import Path

import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _star(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept", "payload": {"name": "gain", "section_path": ""}, "evidence": []},
        {"local_id": "c", "object_type": "concept", "payload": {"name": "bias", "section_path": ""}, "evidence": []},
    ], [
        {"source_local_id": "a", "target_local_id": "b", "edge_type": "relates", "evidence": []},
        {"source_local_id": "a", "target_local_id": "c", "edge_type": "relates", "evidence": []},
    ])
    repo.rebuild_unified_kg(nb.id)
    return nb


def _clear_viz(repo, nb_id):
    """Wipe any viz index that rebuild_unified_kg proactively built, so we can
    test the lazy/async paths as if nothing had been built yet."""
    shutil.rmtree(repo._viz_index_dir(nb_id), ignore_errors=True)
    repo._viz_idx_cache.pop(nb_id, None)


def _wait_until_not_building(repo, nb_id, cap_seconds=10):
    deadline = time.time() + cap_seconds
    while nb_id in repo._viz_building:
        if time.time() > deadline:
            raise AssertionError("background viz build did not finish within cap")
        time.sleep(0.05)


def test_unified_graph_large_nb_reports_unavailable_and_builds_nothing(repo, monkeypatch):
    """批 3·W4 T-W4-3:大库没有 viz 产物时,API 进程既不同步建也不后台建。

    此前这里会 spawn 一个后台 daemon 并回 ``viz_building: True``。那个 daemon 跑的
    是 build_viz —— 整库对象 + 全部关系 + 完整 cluster_map 物化成一份图字典,发生在
    服务请求的进程里。现在它被规模闸删掉了(上游 ``<=`` 已把未超限的库分流到同步
    建,所以走到这一支 count 构造性恒超限),因此:什么都没建、没有后台标记、返回的
    是诚实的 ``viz_unavailable``,而不是一句永远不会兑现的「构建中」。
    """
    nb = _star(repo)
    _clear_viz(repo, nb.id)
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 0)
    manifest_path = os.path.join(repo._viz_index_dir(nb.id), "manifest.json")
    assert not os.path.exists(manifest_path)
    refusals: list = []
    real_emit = repo._runtime.scale_artifacts._emit_viz_lazy_build_refused

    def _spy_emit(notebook_id, count, trigger):
        refusals.append(trigger)
        return real_emit(notebook_id, count, trigger)

    monkeypatch.setattr(
        repo._runtime.scale_artifacts,
        "_emit_viz_lazy_build_refused",
        _spy_emit,
    )
    result = repo.unified_graph(nb.id, level="object", limit=10)
    assert result["viz_building"] is False
    assert result["viz_unavailable"] is True
    # 一次请求恰好一条拒绝遥测(codex #676 R11 P2):发布竞态重探走的是
    # emit_refusal=False 的免发射变体,不许把同一次打开记成两次拒绝。
    assert refusals == ["absent"]
    assert result["nodes"] == []
    assert result["edges"] == []
    assert result["total_nodes"] == 0
    assert result["total_edges"] == 0
    assert result["truncated"] is False
    # 闸的实物证据:没有产物被写出,也没有任何后台构建在飞。
    assert not os.path.exists(manifest_path)
    assert nb.id not in repo._viz_building


def test_unified_graph_reports_viz_building_truthfully_when_one_is_running(repo, monkeypatch):
    """`viz_building` 不再写死:标记集合里真有这本库时才是 True。

    复评 P1-1 的另一半——闸上以后大库这一支通常是 False,但「有人正在建」并非不可
    达(facade 的显式 _spawn_viz_build、越过阈值前就已起飞的构建),那时候必须仍然
    如实说在建,并且**不**同时声称 unavailable(两者由后端造成互斥)。
    """
    nb = _star(repo)
    _clear_viz(repo, nb.id)
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 0)
    lifecycle = repo._runtime.knowledge_lifecycle
    monkeypatch.setattr(lifecycle.scale_artifacts, "viz_index", lambda _nb, **_kw: None)
    lifecycle.scale_artifacts.viz_building.add(nb.id)
    try:
        result = repo.unified_graph(nb.id, level="object", limit=10)
    finally:
        lifecycle.scale_artifacts.viz_building.discard(nb.id)
    assert result["viz_building"] is True
    assert result["viz_unavailable"] is False


def test_unified_graph_counts_an_inflight_scale_build_as_building(repo, monkeypatch):
    """codex #676 R4 P2:scale index build 在飞时它就是 viz 的唯一在建生产者
    ——此时报 unavailable 会让前端不开轮询,构建发布后画布停在终态文案不
    自愈。standalone viz_building 集合为空、scale build 集合有这本库,必须
    仍报 viz_building=True(且不与 unavailable 同真)。"""
    nb = _star(repo)
    _clear_viz(repo, nb.id)
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 0)
    lifecycle = repo._runtime.knowledge_lifecycle
    monkeypatch.setattr(lifecycle.scale_artifacts, "viz_index", lambda _nb, **_kw: None)
    assert nb.id not in lifecycle.scale_artifacts.viz_building
    with lifecycle.scale_artifacts.building_lock:
        lifecycle.scale_artifacts.building.add(nb.id)
    try:
        result = repo.unified_graph(nb.id, level="object", limit=10)
    finally:
        with lifecycle.scale_artifacts.building_lock:
            lifecycle.scale_artifacts.building.discard(nb.id)
    assert result["viz_building"] is True
    assert result["viz_unavailable"] is False


def test_unified_graph_sees_a_cross_process_scale_build_as_building(repo, monkeypatch):
    """codex #676 R6 P2:离线 CLI 在另一进程持有 scale-build 认领时,本进程的
    building/viz_building 集合都是空的——只有只读认领探针看得见。此时必须报
    viz_building=True(前端继续轮询,发布后自愈),不许落终态 unavailable。"""
    nb = _star(repo)
    _clear_viz(repo, nb.id)
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 0)
    lifecycle = repo._runtime.knowledge_lifecycle
    monkeypatch.setattr(lifecycle.scale_artifacts, "viz_index", lambda _nb, **_kw: None)
    monkeypatch.setattr(
        lifecycle.scale_artifacts,
        "scale_build_claim_held_anywhere",
        lambda _nb: True,
    )

    result = repo.unified_graph(nb.id, level="object", limit=10)

    assert result["viz_building"] is True
    assert result["viz_unavailable"] is False


def test_unified_graph_re_probes_the_artifact_when_no_builder_is_observed(repo, monkeypatch):
    """codex #676 R5 P2:构建可能在 viz_index 探针之后、成员检查之前发布并清
    标记——此时报终态 unavailable 会让已打开的画布卡死(前端只在
    viz_building 时轮询)。无在建者时必须重探一次产物;重探命中就按可用
    served,不返回 unavailable。"""
    nb = _star(repo)
    _clear_viz(repo, nb.id)
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 0)
    lifecycle = repo._runtime.knowledge_lifecycle

    class _PublishedIdx:
        viz_ids = ["ko-1"]

    probes = []

    def _flappy_viz_index(_nb, **_kw):
        probes.append(1)
        # 第一探 None(构建尚未发布),第二探已发布——模拟发布落在两读之间。
        return None if len(probes) == 1 else _PublishedIdx()

    monkeypatch.setattr(lifecycle.scale_artifacts, "viz_index", _flappy_viz_index)
    monkeypatch.setattr(
        lifecycle, "_unified_graph_bounded",
        lambda _nb, idx, _limit: {"served": True, "idx": idx})

    result = repo.unified_graph(nb.id, level="object", limit=10)

    assert len(probes) == 2
    assert result.get("served") is True
    assert "viz_unavailable" not in result


def test_kg_neighbors_large_nb_without_viz_never_materializes_cluster_map(repo, monkeypatch):
    """Citation focus during a large viz build must stay bounded and report that
    location is temporarily unavailable instead of loading every cluster member."""
    nb = _star(repo)
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 0)
    lifecycle = repo._runtime.knowledge_lifecycle
    monkeypatch.setattr(lifecycle.scale_artifacts, "viz_index", lambda _nb, **_kw: None)
    monkeypatch.setattr(
        lifecycle,
        "cluster_map",
        lambda _nb: (_ for _ in ()).throw(AssertionError("full cluster map loaded")),
    )

    result = repo.kg_neighbors(nb.id, "missing-raw-citation")

    assert result["locating_unavailable"] is True
    assert result["nodes"] == []
    assert result["edges"] == []


def test_unified_graph_unavailable_until_the_index_build_publishes(repo, monkeypatch):
    """大库的降级是持久态,只有指定生产者(索引构建)能把它解除。

    重复打开不会自己变好——闸掉的正是「打开就顺手建一份」。走一次 build_viz(scale
    索引构建在离线/后台做的同一件事)之后,同一次打开才回到真实图谱。
    """
    nb = _star(repo)
    _clear_viz(repo, nb.id)
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 0)
    first = repo.unified_graph(nb.id, level="object", limit=10)
    assert first["viz_unavailable"] is True
    again = repo.unified_graph(nb.id, level="object", limit=10)
    assert again["viz_unavailable"] is True      # 再打开一次仍然什么都不会发生
    assert nb.id not in repo._viz_building
    repo.build_viz_index(nb.id)                  # 指定生产者发布产物
    ready = repo.unified_graph(nb.id, level="object", limit=10)
    assert not ready.get("viz_building")
    assert not ready.get("viz_unavailable")
    assert len(ready["nodes"]) > 0


def test_unified_graph_small_nb_unchanged(repo):
    nb = _star(repo)
    _clear_viz(repo, nb.id)
    # default threshold is large — small nb builds synchronously (legacy behavior)
    result = repo.unified_graph(nb.id, level="object", limit=10)
    assert not result.get("viz_building")
    assert len(result["nodes"]) > 0
    assert os.path.exists(os.path.join(repo._viz_index_dir(nb.id), "manifest.json"))


def test_viz_stale_served_while_rebuilding(repo, monkeypatch):
    nb = _star(repo)
    # viz index already built fresh by rebuild_unified_kg above.
    assert os.path.exists(os.path.join(repo._viz_index_dir(nb.id), "manifest.json"))
    # Drift the KG version without re-running rebuild_unified_kg (so the disk
    # viz index becomes stale relative to _scale_index_version).
    repo.store_kg(nb.id, None, [
        {"local_id": "d", "object_type": "concept", "payload": {"name": "leakage", "section_path": ""}, "evidence": []},
    ], [])
    repo._viz_idx_cache.pop(nb.id, None)
    result = repo.unified_graph(nb.id, level="object", limit=10)
    # Stale data is benign to serve immediately.
    assert len(result["nodes"]) > 0
    assert not result.get("viz_building")
    # A background refresh was kicked off (may have already finished).
    _wait_until_not_building(repo, nb.id)


def _record_events(monkeypatch, scale):
    """Capture emitted events regardless of EVENT_LOG_ENABLED (emit early-returns
    when the log is off, so patching the method is the only reliable probe)."""
    seen: list[dict] = []
    monkeypatch.setattr(scale.event_log, "emit", lambda event, **_kw: seen.append(event))
    return seen


def test_stale_viz_refresh_is_refused_for_a_large_notebook(repo, monkeypatch):
    """批 3·W4 T-W4-3 的第一个调用点:stale 刷新分支同样受规模闸。

    build_viz 不是增量修补而是整图重建,所以「刷新一份 stale 产物」在大库上跟第一次
    构建一样贵,而且同样发生在服务请求的进程里。闸上之后:陈旧折叠图继续供图(那本就
    是这一支的返回值),但**不**在这里刷新;发一条只带维度的结构化事件。

    变异钉:把 ``if not self._viz_lazy_build_refused(...)`` 删掉(恢复无条件 spawn)
    或把判据反向,这条会红——viz_building 里会出现这本库。
    """
    nb = _star(repo)
    manifest_path = os.path.join(repo._viz_index_dir(nb.id), "manifest.json")
    assert os.path.exists(manifest_path)
    # 让磁盘产物相对当前版本变陈旧(与 test_viz_stale_served_while_rebuilding 同法)。
    repo.store_kg(nb.id, None, [
        {"local_id": "d", "object_type": "concept", "payload": {"name": "leakage", "section_path": ""}, "evidence": []},
    ], [])
    repo._viz_idx_cache.pop(nb.id, None)
    scale = repo._runtime.scale_artifacts
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 0)
    spawned: list[str] = []
    monkeypatch.setattr(scale, "_spawn_viz_build", lambda nbid: spawned.append(nbid))
    events = _record_events(monkeypatch, scale)

    served = scale.viz_index(nb.id)

    assert served is not None            # 陈旧折叠图照旧供图
    assert spawned == []                 # 但没有在这里刷新
    refusals = [e for e in events if e.get("kind") == "viz_lazy_build_refused"]
    assert len(refusals) == 1
    assert refusals[0]["trigger"] == "stale_refresh"
    assert refusals[0]["reason"] == "large_notebook"
    assert refusals[0]["notebook_id"] == nb.id
    assert refusals[0]["objects"] > refusals[0]["max_objects"]


def test_stale_viz_refresh_still_spawns_for_a_small_notebook(repo, monkeypatch):
    """对照:未超限的库,stale 刷新行为一个字节都没变(仍然后台刷新)。"""
    nb = _star(repo)
    repo.store_kg(nb.id, None, [
        {"local_id": "d", "object_type": "concept", "payload": {"name": "leakage", "section_path": ""}, "evidence": []},
    ], [])
    repo._viz_idx_cache.pop(nb.id, None)
    scale = repo._runtime.scale_artifacts
    # default viz_sync_build_max_objects (20000) → 4 objects is small
    spawned: list[str] = []
    monkeypatch.setattr(scale, "_spawn_viz_build", lambda nbid: spawned.append(nbid))
    events = _record_events(monkeypatch, scale)

    assert scale.viz_index(nb.id) is not None
    assert spawned == [nb.id]
    assert [e for e in events if e.get("kind") == "viz_lazy_build_refused"] == []


def _republish_viz_root_out_of_band(repo, nb_id):
    """同一代内容、新 inode —— 离线 CLI ``import`` 重放同一版本时磁盘上的样子。

    两个 DB 判据(version / cluster_seq)都不会动,所以只有磁盘签名能看见它。
    """
    live = Path(str(repo._viz_index_dir(nb_id)))
    staged = live.parent / f"{live.name}.newgen"
    shutil.copytree(live, staged)
    shutil.rmtree(live)
    staged.rename(live)


def test_a_refused_stale_viz_is_served_warm_instead_of_reloaded(repo, monkeypatch):
    """批 3·W4 T-W4-3b 必修 A:被闸掉刷新的 stale 产物必须进热缓存。

    闸掉之前,stale 是几分钟内自己收敛的过渡态(后台 daemon 正在刷新它);闸掉之后
    它是**无限期**态。而 ``_viz_manifest_fresh`` 只认新鲜产物,于是超限库每一次
    ``viz_index`` 读都要重跑一遍 ``load_viz`` —— viz_ids/types/names 三个百万级
    list 加 CSR 邻接,在服务请求的进程里。无限期 × 每请求重载才是真正的回归。

    槽位的键是**磁盘签名**,不是那两个 DB 判据:后者说「这一代落后于数据库」(正因
    如此它才在这里,而且会一直为真),签名说「这还是我上次载入并判过 stale 的那一
    代」。所以下面第二段:同代内容换个 inode(离线 import 的形状)必须自然 miss。

    变异钉:把 stale 分支的缓存写回删掉(或把命中分支改回只服务 fresh 条目),
    ``loads`` 会变成两次,第一个断言即红。
    """
    nb = _star(repo)
    repo.store_kg(nb.id, None, [
        {"local_id": "d", "object_type": "concept", "payload": {"name": "leakage", "section_path": ""}, "evidence": []},
    ], [])
    repo._viz_idx_cache.pop(nb.id, None)
    scale = repo._runtime.scale_artifacts
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 0)
    spawned: list[str] = []
    monkeypatch.setattr(scale, "_spawn_viz_build", lambda nbid: spawned.append(nbid))
    loads: list[str] = []
    real_load = scale.artifacts.load_viz
    monkeypatch.setattr(
        scale.artifacts, "load_viz",
        lambda nbid: loads.append(nbid) or real_load(nbid),
    )

    first = scale.viz_index(nb.id)
    second = scale.viz_index(nb.id)

    assert first is not None                 # 陈旧折叠图照旧供图
    assert second is first                   # ...而且是同一个对象,没有重新载入
    assert loads == [nb.id]                  # 两次读只付一次 load_viz
    assert spawned == []                     # 闸仍然生效(缓存不是绕过闸的后门)

    _republish_viz_root_out_of_band(repo, nb.id)
    third = scale.viz_index(nb.id)

    assert third is not None
    assert third is not first, "换代(新 inode)必须让签名槽位失效并重载"
    assert loads == [nb.id, nb.id]


def test_the_stale_viz_slot_is_replaced_when_the_index_build_publishes(repo, monkeypatch):
    """另一半失效路径:指定生产者发布新产物后,热槽位里不能再是那份 stale 图。"""
    nb = _star(repo)
    repo.store_kg(nb.id, None, [
        {"local_id": "d", "object_type": "concept", "payload": {"name": "leakage", "section_path": ""}, "evidence": []},
    ], [])
    repo._viz_idx_cache.pop(nb.id, None)
    scale = repo._runtime.scale_artifacts
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 0)
    monkeypatch.setattr(scale, "_spawn_viz_build", lambda nbid: None)

    stale = scale.viz_index(nb.id)
    assert stale is not None
    assert scale.viz_probe(nb.id)["viz_stale"] is True

    repo.build_viz_index(nb.id)              # 指定生产者发布新一代
    fresh = scale.viz_index(nb.id)

    assert fresh is not stale
    assert scale.viz_probe(nb.id)["viz_stale"] is False


def test_absent_viz_build_is_refused_for_a_large_notebook(repo, monkeypatch):
    """第二个调用点:没有产物 + 超限 → 什么都不建,发同一族事件。

    这一支的 count 是**构造性**超限的:上游 ``<=`` 已经把每一本未超限的库分流去同步
    建了。所以加这道判据等于把「API 进程内的大库懒 viz 生产者」整个删掉,而不是收窄
    它——注释与本用例都按这个事实写。

    变异钉:把末尾换回 ``self._spawn_viz_build(notebook_id)``,``spawned`` 非空即红。
    """
    nb = _star(repo)
    _clear_viz(repo, nb.id)
    scale = repo._runtime.scale_artifacts
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 0)
    spawned: list[str] = []
    monkeypatch.setattr(scale, "_spawn_viz_build", lambda nbid: spawned.append(nbid))
    monkeypatch.setattr(
        scale, "build_viz",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("sync build attempted")),
    )
    events = _record_events(monkeypatch, scale)

    assert scale.viz_index(nb.id) is None
    assert spawned == []
    refusals = [e for e in events if e.get("kind") == "viz_lazy_build_refused"]
    assert len(refusals) == 1
    assert refusals[0]["trigger"] == "absent"
    assert refusals[0]["objects"] > refusals[0]["max_objects"]


def test_unified_kg_status_reports_viz_building(repo, monkeypatch):
    """状态投影仍如实反映后台构建标记。

    驱动方式从「打开大库图谱顺带 spawn」改成直接调用 _spawn_viz_build:大库那条
    自动 spawn 已被规模闸删除(见上面的 refuse 用例),但标记本身与状态投影的关系
    没有变,这条守的是后者。
    """
    nb = _star(repo)
    _clear_viz(repo, nb.id)
    pending = {}
    monkeypatch.setattr(
        repo._runtime.scale_artifacts,
        "_start_daemon",
        lambda _name, target: pending.setdefault("target", target),
    )
    repo._runtime.scale_artifacts._spawn_viz_build(nb.id)
    status = repo.unified_kg_status(nb.id)
    assert status["viz_building"] is True
    pending["target"]()
    status2 = repo.unified_kg_status(nb.id)
    assert status2["viz_building"] is False


def test_unified_graph_no_limit_large_nb_never_full_derives(repo, monkeypatch):
    """No `limit` arg at all (old frontend / bare API call) must NOT fall
    through to _unified_graph_full for a large notebook — that pulls every
    knowledge_objects row into Python dicts and caches the multi-GB result in
    self._unified_cache. Proof: the (nb, level) key must never appear in
    _unified_cache, since _unified_graph_full unconditionally populates it."""
    nb = _star(repo)
    _clear_viz(repo, nb.id)
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 0)
    result = repo.unified_graph(nb.id, level="object")
    assert result["viz_unavailable"] is True
    assert (nb.id, "object") not in repo._unified_cache
    assert (nb.id, "concept") not in repo._unified_cache


def test_unified_graph_concept_level_large_nb_guarded(repo, monkeypatch):
    """level='concept' must be treated like 'object' for large notebooks (the
    folded viz graph is object-level only) — it must not fall through to
    _unified_graph_full either."""
    nb = _star(repo)
    _clear_viz(repo, nb.id)
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 0)
    result = repo.unified_graph(nb.id, level="concept")
    assert result["viz_unavailable"] is True
    assert (nb.id, "object") not in repo._unified_cache
    assert (nb.id, "concept") not in repo._unified_cache


def test_unified_graph_no_limit_small_nb_unchanged(repo):
    """Small notebooks (under the default threshold) keep EXACT legacy
    behavior: a no-limit call still returns the full graph (no viz_building
    key, total_nodes == len(nodes))."""
    nb = _star(repo)
    _clear_viz(repo, nb.id)
    result = repo.unified_graph(nb.id, level="object")
    assert "viz_building" not in result
    assert len(result["nodes"]) > 0
    assert result["total_nodes"] == len(result["nodes"])


def test_unified_graph_no_limit_large_nb_uses_default_limit_when_index_ready(repo, monkeypatch):
    """When a viz index already exists (fresh) for a large notebook, a
    no-limit call must be routed through the bounded core-graph path using
    settings.viz_default_limit as the effective limit — i.e. if the folded
    graph has more nodes than viz_default_limit, the result must report
    truncation via the same `truncated` key the bounded path already uses."""
    nb = _star(repo)
    # viz index already built fresh by rebuild_unified_kg in _star().
    assert os.path.exists(os.path.join(repo._viz_index_dir(nb.id), "manifest.json"))
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 0)
    monkeypatch.setattr(repo.settings, "viz_default_limit", 1)
    result = repo.unified_graph(nb.id, level="object")
    assert not result.get("viz_building")
    assert "truncated" in result
    assert result["total_nodes"] >= 2  # star fixture has 3 concept nodes
    assert len(result["nodes"]) <= 1
    assert result["truncated"] is True


def _seed_star_kg(repo, nb_id):
    repo.store_kg(nb_id, None, [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept", "payload": {"name": "gain", "section_path": ""}, "evidence": []},
        {"local_id": "c", "object_type": "concept", "payload": {"name": "bias", "section_path": ""}, "evidence": []},
    ], [
        {"source_local_id": "a", "target_local_id": "b", "edge_type": "relates", "evidence": []},
        {"source_local_id": "a", "target_local_id": "c", "edge_type": "relates", "evidence": []},
    ])


def test_rebuild_skips_viz_build_for_large_notebook(repo, monkeypatch):
    """OOM audit P0-2 / codex PR#356 r1+r2 P1: for a large notebook the rebuild tail
    must NOT build viz at all — neither synchronously (build_viz materialises EVERY
    object + all relations + the full cluster_map into one ~12-20GB graph, stacked on
    the rebuild's peak) nor via _spawn_viz_build (a daemon that overlaps the still-live
    rebuild frame — codex r2 P1b). Re-adding either build call for large fails here.

    批 3·W4 T-W4-3b: this docstring used to end "its viz is refreshed lazily OFF the
    rebuild thread ... the next KG-view open rebuilds it". That stopped being true in
    T-W4-3, which refuses the lazy refresh inside a request-serving process for the
    same multi-GB reason. The cluster rebuild still bumps cluster_mutation_seq so the
    persisted viz reads STALE and keeps serving; republishing it is the scale index
    build's job (queued right below this branch — see the two rebuilds test)."""
    nb = repo.create_notebook(NotebookCreate(name="big"))
    _seed_star_kg(repo, nb.id)
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 0)  # 3 objects → large
    sync_calls, async_calls = [], []
    monkeypatch.setattr(repo._runtime.scale_artifacts, "build_viz",
                        lambda nbid: sync_calls.append(nbid))
    monkeypatch.setattr(repo._runtime.scale_artifacts, "_spawn_viz_build",
                        lambda nbid: async_calls.append(nbid))
    repo.rebuild_unified_kg(nb.id)
    assert sync_calls == []    # large: no synchronous build on the rebuild thread
    assert async_calls == []   # ...and no daemon overlapping the rebuild frame either


def test_rebuild_proactively_builds_viz_sync_for_small_notebook(repo, monkeypatch):
    """Control: a small notebook (<= viz_sync_build_max_objects) still gets its
    proactive viz refresh SYNCHRONOUSLY at the rebuild tail (legacy behavior — the
    lazy KG-view open must not pay the build), never the async spawn path."""
    nb = repo.create_notebook(NotebookCreate(name="small"))
    _seed_star_kg(repo, nb.id)
    # default viz_sync_build_max_objects (20000) → 3 objects is small
    sync_calls, async_calls = [], []
    monkeypatch.setattr(repo._runtime.scale_artifacts, "build_viz",
                        lambda nbid: sync_calls.append(nbid))
    monkeypatch.setattr(repo._runtime.scale_artifacts, "_spawn_viz_build",
                        lambda nbid: async_calls.append(nbid))
    repo.rebuild_unified_kg(nb.id)
    assert sync_calls == [nb.id]    # small: sync proactive build
    assert async_calls == []


def _spy_scale_trigger(repo, monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        repo._runtime.scale_artifacts, "trigger",
        lambda nbid, when="now", mode="auto": calls.append((nbid, when, mode)),
    )
    return calls


def test_a_consumed_once_set_no_longer_silences_the_large_rebuild_tail(repo, monkeypatch):
    """批 3·W4 T-W4-3b 必修 B:大库跳过分支登记的「离峰恢复」必须在常态下也成立。

    事实链:``maybe_auto_index`` 的 ``auto_index_checked`` 是每进程每库一次的 set,
    finally 无条件 add;唯一的 re-arm 点是 kg_mutation 的 dirty choke point,而
    rebuild 刻意不走它(``finish_rebuild_state`` 必须不动 ``kg_mutation_seq``)。
    每次入库和每次走全量回退的检索都会调 ``maybe_auto_index``,所以到 rebuild 尾部
    时那个 set 通常**已经被消费掉了**——而重建恰恰是让上一次判定作废的那件事(上
    一次可能正是判的「索引是新鲜的,不用建」,重建之后它已经 stale)。于是尾部这次
    调用直接 return,而 T-W4-3 之后它是大库 viz 唯一的自动生产者:viz 停在 stale
    直到进程重启。

    整改:大库跳过分支在调 ``maybe_auto_index`` 之前 ``rearm_auto_index``。它只允许
    **重新评估**,四道前置(auto_enabled / 未在建在队 / NOT copyable / 状态闸)一道
    不减。``trigger`` 打桩成不入队的 spy,好让第二轮不被「已排队」早退掩盖。

    变异钉:删掉那一行 ``rearm_auto_index``,``calls`` 只剩一条,末尾断言即红。
    """
    monkeypatch.setattr(repo.settings, "notebook_copy_max_rows", 0)      # copyable=False → 「大」
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 0)  # viz 尺子上也「大」
    nb = repo.create_notebook(NotebookCreate(name="big"))
    _seed_star_kg(repo, nb.id)
    calls = _spy_scale_trigger(repo, monkeypatch)

    repo.maybe_auto_index(nb.id)     # 入库/检索回退先消费掉 once-set(生产上的常态)
    assert len(calls) == 1
    assert nb.id in repo._auto_index_checked
    repo.rebuild_unified_kg(nb.id)   # 重建让那次判定作废——尾部必须重新评估

    assert [c[0] for c in calls] == [nb.id, nb.id]


def test_a_forced_second_rebuild_still_reaches_the_auto_index_evaluation(repo, monkeypatch):
    """同一条恢复路径的另一种到达方式:同进程里的第二次**强制**重建。

    普通第二次重建会被 ``cluster_input_version`` 的 skip 门挡在尾部之前(输入没变),
    所以「同进程两次重建」只有在 ``force=True`` 时才真正走到尾部——那是 recluster
    脚本 / ``rebuild_only`` / ``--fresh`` 这些运维入口的形状。这条钉住:那次重建的
    尾部同样会重新评估,而不是被上一次重建自己留下的 once-set 吃掉。
    """
    monkeypatch.setattr(repo.settings, "notebook_copy_max_rows", 0)
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 0)
    nb = repo.create_notebook(NotebookCreate(name="big"))
    _seed_star_kg(repo, nb.id)
    calls = _spy_scale_trigger(repo, monkeypatch)

    repo.rebuild_unified_kg(nb.id)
    repo.rebuild_unified_kg(nb.id, force=True)

    assert [c[0] for c in calls] == [nb.id, nb.id]


def test_a_small_notebook_rebuild_does_not_rearm_the_auto_index_once_set(repo, monkeypatch):
    """对照 + 定位钉:re-arm 只挂在**大库跳过分支**上。

    小库在上一行刚同步建完 viz,没有可恢复的东西,不该每次重建都付
    ``notebook_copy_stats`` 的 5 个 COUNT + 一次 ``status()``。把 re-arm 从 else
    分支挪到无条件位置,这条会红(``calls`` 变成两条)。
    """
    monkeypatch.setattr(repo.settings, "notebook_copy_max_rows", 0)      # 仍然 copyable=False
    # viz_sync_build_max_objects 保持默认(高)→ 这本库走同步 build_viz 那一支
    nb = repo.create_notebook(NotebookCreate(name="small"))
    _seed_star_kg(repo, nb.id)
    calls = _spy_scale_trigger(repo, monkeypatch)

    repo.maybe_auto_index(nb.id)
    assert len(calls) == 1
    repo.rebuild_unified_kg(nb.id)

    assert [c[0] for c in calls] == [nb.id]


def test_rebuild_viz_refresh_fail_open_on_size_probe_error(repo, monkeypatch):
    """codex PR#356 r1 P2: the size probe that decides whether to build viz is part
    of the OPTIONAL post-rebuild viz step. If it raises (transient connection /
    repository error), it must NOT escape and turn an already-committed KG rebuild
    into a reported failure — the whole block is fail-open. (rebuild_unified_kg
    calls active_object_count ONLY at this tail, so patching it isolates the probe.)"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_star_kg(repo, nb.id)

    def boom(db, notebook_id):
        raise RuntimeError("transient size-probe failure")

    monkeypatch.setattr(repo._runtime.knowledge_lifecycle.knowledge,
                        "active_object_count", boom)
    repo.rebuild_unified_kg(nb.id)   # must NOT raise — the failing probe is caught


def test_build_viz_stamps_pre_derive_cluster_seq(repo, monkeypatch):
    """codex PR#356 r2 P1a: build_viz captures version + cluster_seq BEFORE deriving
    and stamps the artifact with them. A cluster write that commits DURING the derive
    must therefore leave the artifact stamped with the PRE-derive cseq — so it reads
    STALE, not mislabelled as current. Stamping AFTER the derive fails here."""
    nb = _star(repo)                                   # small nb: viz built at rebuild tail
    sa = repo._runtime.scale_artifacts
    pre_cseq = int(sa.projections.version_signal(nb.id)[1])
    real_derive = repo._runtime.scale_builder._derive_object_graph_lite

    def derive_and_bump(nbid):
        with repo._write() as db:                      # interleave a cluster write mid-build
            repo._runtime.knowledge_lifecycle._bump_cluster_mutation_seq(db, nbid)
        return real_derive(nbid)

    monkeypatch.setattr(repo._runtime.scale_builder, "_derive_object_graph_lite", derive_and_bump)
    _clear_viz(repo, nb.id)
    manifest = repo.build_viz_index(nb.id)
    assert manifest["cluster_seq"] == pre_cseq         # stamped with the PRE-derive cseq
    assert sa.viz_probe(nb.id)["viz_stale"] is True    # cseq has since advanced → stale


def test_viz_stale_detected_by_cluster_seq(repo):
    """codex PR#356 r1 P1: a cluster-only rewrite bumps cluster_mutation_seq but need
    not change version_facts (concept_clusters COUNT + second-granularity
    MAX(created_at)), which is all version() sees. viz freshness must consult
    cluster_seq so the persisted viz is detected STALE — not served as current
    forever. Dropping the cseq comparison keeps it 'fresh' and fails here."""
    nb = _star(repo)                                   # viz built fresh
    sa = repo._runtime.scale_artifacts
    assert sa.viz_probe(nb.id)["viz_stale"] is False   # fresh right after build
    with repo._write() as db:                          # bump cseq only (no cluster COUNT/time change)
        repo._runtime.knowledge_lifecycle._bump_cluster_mutation_seq(db, nb.id)
    assert sa.viz_probe(nb.id)["viz_stale"] is True     # cseq advanced → stale, via cluster_seq


def test_large_nb_guard_uses_cached_active_object_count(repo, monkeypatch):
    """Z3 (codex #621 同族遗漏的一行换缓存): unified_graph 与 kg_neighbors 的大库闸
    (knowledge_lifecycle.py 里 unified_graph 顶部、_kg_neighbors_unchecked 的 DB
    fallback 分支)现在都通过 ``store.count_active_objects`` 读数——那是既有的
    seq-gated ``knowledge_counts_cache.active_object_count`` memo(与
    postgres/knowledge_store.py:count_active_objects 同一条缓存路径),不再各自跑一次
    裸 ``SELECT COUNT(*) ... status!='deprecated'``。

    Spy 挂在游标方法上,按 SQL 文本区分两种形态:①缓存的冷路径(GROUP BY object_type,
    status,只在 memo miss 时才发生);②未缓存的裸计数(``AND status!='deprecated'``,
    没有 GROUP BY——即被替换前的旧查询)。断言总次数,而不只是①的次数,这样即使某个
    调用点偷偷换回②也会被抓到(只看①会漏检:②根本不含 GROUP BY,不会被①的计数器
    看见,但确实是一次不该有的冷查询)。两次大库闸检查之间 kg_mutation_seq 没变,若都
    走缓存,合计应恰好 1 次(第一次 miss 建 memo,第二次命中零查询)。

    变异自检:把 kg_neighbors 那处调用改回 ``self.knowledge.active_object_count``
    (未缓存版本)会让第二个 assert 变红——total_calls 会变成 2 而不是 1(见任务报告;
    只看①形态的计数器测不出这个变异,因为②走的是完全不同的 SQL 文本)。"""
    nb = _star(repo)
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 0)
    lifecycle = repo._runtime.knowledge_lifecycle
    # kg_neighbors 的大库闸只在「没有 viz 索引」的 DB-fallback 分支里出现;
    # _star() 对这个小图会顺带同步建好 viz,所以显式挡掉它,强制走 fallback。
    monkeypatch.setattr(lifecycle.scale_artifacts, "viz_index", lambda _nb, **_kw: None)

    from app.repositories.sqlite import knowledge_counts_cache as kcc
    from app.repositories.sqlite.database import _DiagnosticCursor

    kcc.invalidate(nb.id)  # 清掉 rebuild 过程中可能留下的 memo,保证下面第一次是冷查询
    cached_cold_calls = []
    uncached_raw_calls = []
    original_execute = _DiagnosticCursor.execute

    def spy_execute(self, sql, parameters=(), /):
        if "GROUP BY object_type, status" in sql:
            cached_cold_calls.append(sql)
        elif "knowledge_objects" in sql and "status!='deprecated'" in sql:
            uncached_raw_calls.append(sql)
        return original_execute(self, sql, parameters)

    monkeypatch.setattr(_DiagnosticCursor, "execute", spy_execute)

    result1 = repo.unified_graph(nb.id, level="object", limit=10)
    assert result1["viz_unavailable"] is True   # 确认真的走了大库闸分支
    total_calls = len(cached_cold_calls) + len(uncached_raw_calls)
    assert total_calls == 1                  # 第一次:冷查询(且必须是缓存形态①)
    assert len(cached_cold_calls) == 1
    assert len(uncached_raw_calls) == 0

    result2 = repo.kg_neighbors(nb.id, "a")
    assert result2.get("locating_unavailable") is True  # 确认走了 DB-fallback 大库闸分支
    total_calls = len(cached_cold_calls) + len(uncached_raw_calls)
    assert total_calls == 1                  # 第二次:同 seq,命中 memo,合计仍是 1
    assert len(uncached_raw_calls) == 0      # 且不能是靠走②形态凑出来的「零查询」假象
