"""A3 (perf-audit P1-4 follow-on): notebook_copy_stats per-version memo.

notebook_copy_stats runs 5 aggregate queries (bytes/sources/chunks/nodes/
edges) and is now on ask-path guards (_scale_index_eligible,
maybe_auto_index) plus share/copy paths (share_notebook, shared_preview,
shared_by_me). Memoize it the same way _scale_index_version/edge_centrality/
clustermap already do: VectorCache keyed on _scale_index_version(nb).

Three guarantees under test:
1. Memo hit: two calls with no intervening mutation run the loader exactly
   once (spy on db.execute's aggregate queries).
2. Refresh after mutation: a chunk write (which bumps kg_mutation_seq, part
   of _scale_index_version) must invalidate the memo — the next call re-runs
   the aggregates and reflects the new count.
3. Output equality: the memoized dict is identical in shape/values to the
   unmemoized computation (no behavior change, only caching).

R2-2(热路径修复批 2 / 审计 ASK-4)把这份 memo 从共享 VectorCache 搬进
``notebook_scale`` 自己的有界 per-notebook 存储:版本键与判据**逐字不变**,变的
只有存放位置,所以上面三条保证原样成立。新增的两条:等价 oracle(旧的
VectorCache 路径与新 memo 在同一数据上逐字段相同)与「不被别的键族挤兑」——后者
是这次改动真正要买的东西,也是唯一能钉住它的断言(值等价对回退变异天然是绿的)。
"""
import json
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository, _now
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _add_concept(repo, nb_id, local_id, name):
    repo.store_kg(nb_id, None, [{
        "local_id": local_id, "object_type": "concept",
        "payload": {"name": name, "section_path": ""}, "evidence": [],
    }], [])


def _add_chunk(repo, nb_id, cid, text):
    """Mirror _build_chunks_for_source's production write + dirty-mark."""
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (f"src-{cid}", nb_id, "t", "md", "ready", now, now),
        )
        db.execute(
            "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (cid, nb_id, f"src-{cid}", text, "", "[]", now),
        )
    repo._mark_unified_kg_dirty(nb_id)


def _count_cold_loads(repo, monkeypatch) -> dict:
    """数「五条整表聚合」真正跑了几次。

    ⚠ 这里刻意 spy ``facts_repo.load_notebook_scale_facts``,而不是像本文件
    早先那样包一层 ``repo._connect`` 数 SQL:facts 读走的是 QueryStore 自己的
    连接,根本不经过 ``repo._connect``——那个连接级 spy 因此**永远**数到 0,
    对「缓存整个失效」这类变异也是绿的(R2-2 的变异自检里实测确认)。
    """
    loads = {"n": 0}
    facts_repo = repo._runtime.scale_artifacts.facts_repo
    real_load = facts_repo.load_notebook_scale_facts
    monkeypatch.setattr(
        facts_repo, "load_notebook_scale_facts",
        lambda notebook_id: (loads.__setitem__("n", loads["n"] + 1),
                             real_load(notebook_id))[1],
    )
    return loads


def test_copy_stats_memo_hit_runs_loader_once(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="b"))
    _add_concept(repo, nb.id, "a", "MOSFET")

    first = repo.notebook_copy_stats(nb.id)

    loads = _count_cold_loads(repo, monkeypatch)

    second = repo.notebook_copy_stats(nb.id)
    assert second == first
    assert loads["n"] == 0, (
        "second call with no intervening mutation must be a memo hit "
        f"(0 cold five-aggregate loads); ran {loads['n']}"
    )


def test_copy_stats_memo_refreshes_after_chunk_write(repo):
    nb = repo.create_notebook(NotebookCreate(name="b"))
    _add_concept(repo, nb.id, "a", "MOSFET")

    before = repo.notebook_copy_stats(nb.id)
    assert before["size"]["chunks"] == 0

    _add_chunk(repo, nb.id, "c1", "chunk text")

    after = repo.notebook_copy_stats(nb.id)
    assert after["size"]["chunks"] == 1, (
        "a chunk write (which bumps kg_mutation_seq / _scale_index_version) "
        "must invalidate the memo so the next call reflects the new count"
    )


def test_copy_stats_output_matches_unmemoized_shape(repo):
    nb = repo.create_notebook(NotebookCreate(name="b"))
    _add_concept(repo, nb.id, "a", "MOSFET")
    stats = repo.notebook_copy_stats(nb.id)
    assert set(stats) == {"copyable", "size"}
    assert set(stats["size"]) == {"bytes", "sources", "chunks", "nodes", "edges"}
    assert stats["size"]["nodes"] == 1


def test_copy_stats_invalidated_by_invalidate_unified_cache(repo):
    """Sibling invalidation convention: _invalidate_unified_cache must also
    evict the copystats memo (defense against same-second in-place edits
    with an unchanged version tuple).

    R2-2 moved the memo out of the shared VectorCache into
    ``notebook_scale``'s own bounded per-notebook store; the invalidation
    contract — same family, same trigger — is what this pins, so it now reads
    that module instead of a vector-cache key."""
    from app.services import notebook_scale

    nb = repo.create_notebook(NotebookCreate(name="b"))
    _add_concept(repo, nb.id, "a", "MOSFET")
    repo.notebook_copy_stats(nb.id)  # warm
    assert notebook_scale.copy_stats_cached_version(nb.id) is not None
    repo._invalidate_unified_cache(nb.id)
    assert notebook_scale.copy_stats_cached_version(nb.id) is None


# ────────────────────── R2-2:等价 oracle 与「不被挤兑」的方向钉 ──────────────


def _legacy_copy_stats(repo, notebook_id, cache):
    """``copy_stats`` 改造**前**的正文(VectorCache 版),原样抄成 oracle。

    版本键、阈值、copyable 判据、``size`` 字典全部与现实现相同——差别只有存储:
    这里 ``cache.get(f"{nb}:copystats", version, load)``,新实现走
    ``notebook_scale`` 的 per-notebook memo。
    """
    settings = repo._runtime.settings
    runtime = repo._runtime.scale_artifacts
    version = (
        tuple(runtime.version(notebook_id)),
        settings.notebook_copy_max_bytes,
        settings.notebook_copy_max_rows,
    )

    def load():
        f = runtime.facts_repo.load_notebook_scale_facts(notebook_id)
        return {
            "copyable": (
                f.bytes <= settings.notebook_copy_max_bytes
                and f.chunks + f.nodes <= settings.notebook_copy_max_rows
            ),
            "size": f.as_size_dict(),
        }

    return cache.get(f"{notebook_id}:copystats", version, load)


def test_copy_stats_matches_the_vector_cache_oracle(repo):
    """R2-2 等价 oracle:新 memo 与旧 VectorCache 路径在同一数据上逐字段相等。

    三个数据状态各比一次(空库 / 有 KO / 又加了 chunk+source),免得只在一种
    形状上碰巧相同。
    """
    from app.services.vector_cache import VectorCache

    oracle_cache = VectorCache()
    nb = repo.create_notebook(NotebookCreate(name="b"))

    assert repo.notebook_copy_stats(nb.id) == _legacy_copy_stats(
        repo, nb.id, oracle_cache)

    _add_concept(repo, nb.id, "a", "MOSFET")
    assert repo.notebook_copy_stats(nb.id) == _legacy_copy_stats(
        repo, nb.id, oracle_cache)

    _add_chunk(repo, nb.id, "c1", "chunk text")
    new = repo.notebook_copy_stats(nb.id)
    assert new == _legacy_copy_stats(repo, nb.id, oracle_cache)
    # 夹具真的走过一次重算(否则两侧都在返回同一份冷值,等式没有信息量)。
    assert new["size"] == {"bytes": 0, "sources": 1, "chunks": 1, "nodes": 1, "edges": 0}


def test_copy_stats_memo_holds_every_active_notebook(repo, monkeypatch):
    """R2-2 的方向钉:专池能同时装下**所有活跃 notebook** 的 copy-stats,而共享
    VectorCache 的 ``:copystats`` 键族只装得下 ``per_family_entries`` 个。

    现场(审计 ASK-4):copy-stats 的冷载是五条整表聚合,而一次提问要问它 5–10
    次(``_federated_graph_is_large`` 每参与库一次、``_lexical_knn_allowed``、
    chunk 暴力守卫、``requires_index`` …)。它此前存在全进程共用的 VectorCache
    里,被别的键族(和后来的族上限)挤兑;专池的容量是 512 个 notebook,每条只有
    一个 ``{"copyable", "size"}`` 小 dict。

    ⚠ 这条钉子的**第一版是空的**(评审 P1-3 实测):它当时用 40 个
    ``other-nb-i:matrix:knowledge_embeddings`` 灌 VectorCache,而 R2-4 的分池
    把这 40 条全归进 ``matrix`` 族、按族上限砍掉,全局上限根本不触发 —— 于是
    「改回 VectorCache」的变异照样绿。现在改成按 notebook 数量做对照,那才是
    专池真正买到的东西。

    **变异锚点**:把 ``NotebookScaleProfile.copy_stats`` 改回
    ``self.cache.get(f"{nb}:copystats", version, load)`` → 只有最近 8 个
    notebook 还暖,其余 12 个各付一次五条整表聚合,这条报红(已实测)。
    """
    from app.services.vector_cache import VectorCache

    notebooks = [repo.create_notebook(NotebookCreate(name=f"b{i}")) for i in range(20)]
    for nb in notebooks:
        _add_concept(repo, nb.id, "a", "MOSFET")
        repo.notebook_copy_stats(nb.id)          # warm

    loads = _count_cold_loads(repo, monkeypatch)
    for nb in notebooks:
        repo.notebook_copy_stats(nb.id)
    assert loads["n"] == 0, (
        "20 个活跃 notebook 的 copy-stats 必须全部仍在专池里;冷载了 "
        f"{loads['n']} 次")

    # 对照:同一批 key 走共享 VectorCache 的 ``:copystats`` 键族(族上限 8 个
    # notebook,每库一条)—— 只有最近 8 个留得住,前 12 个必须重付五条聚合。
    shared = VectorCache(max_entries=128, per_family_entries=8, max_bytes=0)
    for nb in notebooks:
        shared.get(f"{nb.id}:copystats", version=1, loader=lambda: {})
    resident = [nb.id for nb in notebooks if shared.peek(f"{nb.id}:copystats", version=1)]
    assert resident == [nb.id for nb in notebooks[-8:]], (
        "对照组必须真的复现「共享缓存装不下」,否则上面那条断言没有对照意义")


def test_copy_stats_recomputes_after_a_source_upload_bumps_the_version(repo):
    """上传(bump 之后)必须重算 —— memo 不是永久钉住的。

    ``_add_chunk`` 走的是生产写入 + ``_mark_unified_kg_dirty`` 的同一形状(插
    sources 行 + chunks 行),``sources`` 与 ``chunks`` 两个计数都必须跟着变。
    """
    nb = repo.create_notebook(NotebookCreate(name="b"))
    _add_concept(repo, nb.id, "a", "MOSFET")
    before = repo.notebook_copy_stats(nb.id)
    assert before["size"]["sources"] == 0 and before["size"]["chunks"] == 0

    _add_chunk(repo, nb.id, "c1", "chunk text")

    after = repo.notebook_copy_stats(nb.id)
    assert after["size"]["sources"] == 1 and after["size"]["chunks"] == 1


def test_ingestion_invalidation_clears_the_copy_stats_memo(repo):
    """P2-1:摄取改了语料规模时,copy-stats memo 必须跟着失效。

    版本键里没有 ``sources`` 表的信号(见 ``notebook_scale`` 模块 docstring),
    而 R2-2 把这份 memo 搬进专池之后驻留时长变长了 —— 原先有一部分新鲜度是被
    32 条共享 LRU 的挤兑白捡的。所以摄取路径上补了显式失效。

    **变异锚点**:把 ``_invalidate_corpus_scale_memos`` 里的
    ``invalidate_copy_stats(notebook_id)`` 删掉 → 这条报红。
    """
    from app.services import notebook_scale
    from app.services.source_ingestion import SourceIngestionService

    nb = repo.create_notebook(NotebookCreate(name="b"))
    _add_concept(repo, nb.id, "a", "MOSFET")
    repo.notebook_copy_stats(nb.id)
    assert notebook_scale.copy_stats_cached_version(nb.id) is not None

    counts_calls: list = []
    service = SourceIngestionService.__new__(SourceIngestionService)
    service.invalidate_knowledge_counts = counts_calls.append
    service._invalidate_corpus_scale_memos(nb.id)

    # 既有的开路计数失效没有被替换掉,copy-stats 是**新增**的一半。
    assert counts_calls == [nb.id]
    assert notebook_scale.copy_stats_cached_version(nb.id) is None


# ─────────── P2(codex PR#634 R1):冷载的 single-flight —— R2-2 搬家时丢掉的 ──
def test_concurrent_cold_loads_run_the_five_aggregates_once():
    """同一个 ``(notebook_id, version)`` 的并发冷 miss 只跑一次五条整表聚合。

    旧的 ``VectorCache.get`` 有 per-key single-flight;R2-2 照抄
    ``knowledge_counts_cache`` 的形态时把它丢了(那边的冷查询是单条 GROUP BY,
    重复跑一次不致命,copy-stats 是**五条**整表聚合,而且一次提问要被问 5–10 次,
    在 reasoning/report 的并发扇出里是真并发)。

    **变异锚点**:删掉 ``_memoized_copy_stats`` 的 ``_PENDING`` leader/follower
    分支(退回「每个 miss 各自 compute」)→ ``calls`` 变成 2、follower 自己的
    compute 也跑了,这条报红。
    """
    import threading
    import time

    from app.services import notebook_scale

    notebook_scale.invalidate_copy_stats()
    version = ("v", 1)
    calls = {"n": 0}
    follower_entered = threading.Event()
    follower_computed = threading.Event()
    release = threading.Event()
    results: dict = {}

    def leader_compute():
        calls["n"] += 1
        # 等 follower 真的进到 memo 里再返回,保证两者重叠。
        follower_entered.wait(5)
        release.wait(5)
        return {"copyable": True, "size": {"bytes": 1}}

    def follower_compute():
        calls["n"] += 1
        follower_computed.set()
        return {"copyable": False, "size": {"bytes": 2}}

    def leader():
        results["leader"] = notebook_scale._memoized_copy_stats(
            "nb-sf", version, leader_compute)

    def follower():
        follower_entered.set()
        results["follower"] = notebook_scale._memoized_copy_stats(
            "nb-sf", version, follower_compute)

    t_leader = threading.Thread(target=leader)
    t_leader.start()
    # leader 已经在 compute 里(它在等 follower_entered)。
    t_follower = threading.Thread(target=follower)
    t_follower.start()
    assert follower_entered.wait(5)
    time.sleep(0.05)          # 让 follower 走到等待点
    release.set()
    t_leader.join(10)
    t_follower.join(10)
    assert not t_leader.is_alive() and not t_follower.is_alive()

    assert calls["n"] == 1, f"并发冷 miss 只该跑一次聚合,跑了 {calls['n']} 次"
    assert not follower_computed.is_set(), "等待者不得自己跑聚合"
    assert results["follower"] is results["leader"], "等待者必须复用同一个结果"
    # 有界性:完成即清理,不留在途条目。
    assert notebook_scale._PENDING == {}


def test_a_failed_cold_load_wakes_waiters_and_caches_no_poison():
    """异常路径:leader 失败必须唤醒等待者(不能把它们挂死),而且不缓存毒值 ——
    等待者各自重试,下一次成功的结果才进 memo。

    **变异锚点**:去掉异常分支里的 ``pending.ready.set()`` → 等待者永远等不到,
    这条测试超时/报红;去掉 ``_PENDING.pop`` → 有界性断言报红。
    """
    import threading

    from app.services import notebook_scale

    notebook_scale.invalidate_copy_stats()
    version = ("v", 1)
    follower_entered = threading.Event()
    attempts = {"n": 0}
    results: dict = {}
    errors: list = []

    def failing_compute():
        attempts["n"] += 1
        follower_entered.wait(5)
        raise RuntimeError("aggregate blew up")

    def retry_compute():
        attempts["n"] += 1
        return {"copyable": True, "size": {"bytes": 7}}

    def leader():
        try:
            notebook_scale._memoized_copy_stats("nb-err", version, failing_compute)
        except RuntimeError as exc:
            errors.append(exc)

    def follower():
        follower_entered.set()
        results["follower"] = notebook_scale._memoized_copy_stats(
            "nb-err", version, retry_compute)

    t_leader = threading.Thread(target=leader)
    t_leader.start()
    t_follower = threading.Thread(target=follower)
    t_follower.start()
    t_leader.join(10)
    t_follower.join(10)
    assert not t_leader.is_alive() and not t_follower.is_alive(), (
        "leader 失败后必须唤醒等待者,不能把它们挂死")

    assert len(errors) == 1, "leader 自己仍要把异常抛给它的调用方"
    assert results["follower"] == {"copyable": True, "size": {"bytes": 7}}
    assert attempts["n"] == 2, "失败不缓存毒值:等待者必须自己重试一次"
    assert notebook_scale.copy_stats_cached_version("nb-err") == version
    assert notebook_scale._PENDING == {}
