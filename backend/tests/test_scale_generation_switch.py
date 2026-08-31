"""W-CLI T-W3:「数据不变、产物变化」的换代盲区,以及 manifest 的库版本字段。

换代读取本来是逐请求探测的(version_signal + manifest 磁盘签名),外部
``rename`` 换上来的新 inode 天然会被看见。剩下的唯一盲区是:离线 CLI / 异机
``import`` 原子换上了新工件,而 DB 侧 ``version`` 值一模一样 —— 那时
``ScaleArtifactCatalog.load`` 的两处 ``version`` 值判等都会命中进程缓存里那个
几 GB 的旧 ScaleIndex(连同已打开的 hnswlib handle),新产物永远不被服务。

本文件钉三件事:
1. 同 version 换产物 → 下一次 load 必须给出新实例(**变异锚点**:把
   ``_signature_superseded`` 改成恒 False,或去掉三处 ``_still_current`` 里的
   签名比对,这些用例报红);
2. 签名没变就不许重载(计数器)——修盲区不能把「静态大库零重载」的收益吃掉;
3. 一次 ``load`` 只 ``stat`` 一次(计数器)——新增的这次 stat 落在两条路径最热
   的共用分支上,必须与 ``_manifest_identity`` memo 共用同一次调用。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.filesystem.scale_artifact_store import ScaleArtifactStore
from app.services.embedding import FakeEmbedder
from app.services.kg import scale_index as scale_index_module
from app.services.scale_artifact_catalog import ScaleArtifactCatalog
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients


# ────────────────────────────────────────────────────── counting test rig ──
class _CountingStore(ScaleArtifactStore):
    """真实的 store(真 stat、真 manifest 解析),只把多 GB 的索引体换成一个
    轻量替身,并数住两件事:``manifest_stat_signature`` 调了几次、
    ``load_scale`` 重载了几次。"""

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self.stat_calls = 0
        self.load_calls = 0

    def manifest_stat_signature(self, directory):
        self.stat_calls += 1
        return super().manifest_stat_signature(directory)

    def load_scale(self, notebook_id: str):
        self.load_calls += 1
        manifest = self.read_manifest(self.scale_dir(notebook_id))
        if manifest is None:
            return None
        return SimpleNamespace(manifest=manifest)


def _catalog(tmp_path, version):
    settings = Settings(storage_dir=str(tmp_path))
    store = _CountingStore(settings)
    cache: dict = {}
    catalog = ScaleArtifactCatalog(
        artifacts=store,
        settings=settings,
        version=lambda _notebook_id: version["value"],
        scale_cache=lambda: cache,
        load_lock=threading.Lock,
        load_locks=lambda: {},
        note_model_error=lambda *a, **k: None,
    )
    return catalog, store, cache


def _publish(catalog, notebook_id, manifest):
    """照生产的原子发布写一份 manifest:先写 ``.tmp``,再 rename 换掉。

    换上来的是**另一个 inode**,所以即使 version/大小/mtime 全撞上,磁盘签名
    也必然不同 —— 这正是盲区用例要复现的东西。
    """
    directory = str(catalog.artifacts.scale_dir(notebook_id))
    os.makedirs(directory, exist_ok=True)
    staged = os.path.join(directory, "manifest.json.tmp")
    with open(staged, "w") as handle:
        json.dump(manifest, handle)
    os.replace(staged, os.path.join(directory, "manifest.json"))


# ────────────────────────────────────────── 1. 换代盲区(变异锚点在这三条) ──
@pytest.mark.parametrize("allow_stale", [False, True])
def test_a_new_artifact_with_the_same_version_is_reloaded(tmp_path, allow_stale):
    """同一个 ``version`` 值下换了产物 → 下一次 load 必须给出新实例。

    **变异锚点**:去掉签名比对(``_signature_superseded`` 恒 False)→ 第二次
    load 仍然返回缓存里的旧对象,``marker`` 还是 "gen-1",本条报红。
    """
    version = {"value": ["v-stable"]}
    catalog, store, _cache = _catalog(tmp_path, version)
    _publish(catalog, "nb", {"version": ["v-stable"], "marker": "gen-1"})

    first = catalog.load("nb", allow_stale=allow_stale)
    assert first is not None and first.manifest["marker"] == "gen-1"
    assert store.load_calls == 1

    # 离线 CLI / 异机 import:产物换代,DB 侧 version 一个字节都没变。
    _publish(catalog, "nb", {"version": ["v-stable"], "marker": "gen-2"})

    second = catalog.load("nb", allow_stale=allow_stale)
    assert second is not first
    assert second.manifest["marker"] == "gen-2"
    assert store.load_calls == 2


def test_exact_reloads_after_a_generation_swap_are_single_flight(tmp_path):
    """codex PR#643 R21 P1:同版本 import 换代那一刻,签名失效让所有并发
    exact(allow_stale=False)读同时脱缓存;exact 路径原来没有 stale 冷路径那把
    per-nb 单飞锁,N 个并发 graph/status 请求就各自 load 一份多 GB 的索引——
    恰好在发布完成后打爆内存。exact 重载必须走同一把锁并在锁内 double-check。

    **变异锚点**:去掉 exact 分支的 ``_notebook_load_lock``/double-check →
    多个线程各自 load,``load_calls`` > 1,本条报红。
    """
    version = {"value": ["v-stable"]}
    settings = Settings(storage_dir=str(tmp_path))
    store = _CountingStore(settings)
    original_load = _CountingStore.load_scale

    def slow_load(self, notebook_id):
        time.sleep(0.05)  # 给并发线程留出叠上来的窗口
        return original_load(self, notebook_id)

    store.load_scale = slow_load.__get__(store)
    cache: dict = {}
    locks: dict = {}
    catalog = ScaleArtifactCatalog(
        artifacts=store,
        settings=settings,
        version=lambda _notebook_id: version["value"],
        scale_cache=lambda: cache,
        load_lock=threading.Lock,
        load_locks=lambda: locks,
        note_model_error=lambda *a, **k: None,
    )
    _publish(catalog, "nb", {"version": ["v-stable"], "marker": "gen-1"})
    assert catalog.load("nb", allow_stale=False).manifest["marker"] == "gen-1"

    # 同版本换代:每个并发读都会看到签名失效。
    _publish(catalog, "nb", {"version": ["v-stable"], "marker": "gen-2"})
    store.load_calls = 0
    barrier = threading.Barrier(6)
    results = []

    def read() -> None:
        barrier.wait()
        results.append(catalog.load("nb", allow_stale=False))

    threads = [threading.Thread(target=read) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert store.load_calls == 1, (
        "one generation swap must trigger exactly one exact reload, "
        f"not {store.load_calls}"
    )
    assert all(r is not None and r.manifest["marker"] == "gen-2" for r in results)


def test_a_stale_pre_lock_signature_does_not_evict_a_current_entry(tmp_path):
    """codex PR#643 R29 P1: a request can capture the manifest signature just
    before a same-version publication, then wait on the per-notebook lock
    while another request loads and caches the NEW generation. The in-lock
    double-check must re-probe the signature — comparing against the stale
    pre-lock capture would evict the already-current entry, reload the same
    multi-GB artifact and tag it with the old signature (another reload on
    the next request, two copies transiently resident).

    Scripted single-threaded: the first ``_manifest_signature`` call (the
    pre-lock capture) answers the OLD generation's signature; every later
    call answers the real one.

    Mutation anchor: drop the in-lock ``_manifest_signature`` re-probe and
    this goes red — ``load_calls`` rises and the cache entry is re-adopted.
    """
    version = {"value": ["v-stable"]}
    catalog, store, _cache = _catalog(tmp_path, version)
    _publish(catalog, "nb", {"version": ["v-stable"], "marker": "gen-1"})
    first = catalog.load("nb", allow_stale=False)
    stale_signature = store.manifest_stat_signature(
        catalog.artifacts.scale_dir("nb")
    )
    _publish(catalog, "nb", {"version": ["v-stable"], "marker": "gen-2"})
    current = catalog.load("nb", allow_stale=False)
    assert current is not first and current.manifest["marker"] == "gen-2"
    loads_before = store.load_calls

    real = catalog._manifest_signature
    answers = iter([stale_signature])

    def scripted(notebook_id):
        try:
            return next(answers)
        except StopIteration:
            return real(notebook_id)

    catalog._manifest_signature = scripted  # type: ignore[method-assign]
    again = catalog.load("nb", allow_stale=False)

    assert again is current, (
        "the in-lock re-probe must accept the entry another request already "
        "loaded for the current generation"
    )
    assert store.load_calls == loads_before, "no redundant reload"


def test_a_new_artifact_is_reloaded_on_the_drifted_stale_path(tmp_path):
    """allow_stale 的第二处判等(cur 已漂移、按**磁盘** version 复用)同样是盲区:
    ``disk_ver`` 来自新解析的 manifest,值一样就会把旧对象交出去。"""
    version = {"value": ["v-drifted"]}   # 与 manifest 的 version 恒不相等
    catalog, store, _cache = _catalog(tmp_path, version)
    _publish(catalog, "nb", {"version": ["v-disk"], "marker": "gen-1"})

    first = catalog.load("nb", allow_stale=True)
    assert first.manifest["marker"] == "gen-1"

    _publish(catalog, "nb", {"version": ["v-disk"], "marker": "gen-2"})

    second = catalog.load("nb", allow_stale=True)
    assert second is not first and second.manifest["marker"] == "gen-2"
    assert store.load_calls == 2


def test_end_to_end_republished_index_is_picked_up_without_a_restart(
    tmp_path, monkeypatch,
):
    """真实工件上的同一件事:build → 拷成新一代 → 原子换目录 → 同进程再取。

    复现的是离线 CLI 的 ``import``(三根各自 ``.tmp`` + rename 落位),version
    一个字节没改。改动前这里会一直服务 build 时缓存的那个 ScaleIndex。
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    repo = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(repo, FakeEmbedder(dim=16))
    notebook = repo.create_notebook(NotebookCreate(name="republish"))
    repo.store_kg(
        notebook.id,
        None,
        [{
            "local_id": "a",
            "object_type": "concept",
            "payload": {"name": "MOSFET", "section_path": ""},
            "evidence": [],
        }],
        [],
    )
    repo.build_scale_index(notebook.id)

    before = repo._scale_index(notebook.id, allow_stale=True)
    assert before is not None
    # 构建侧确实写了库版本字段(异机比对的依据)。
    assert before.manifest["library_versions"]["hnswlib"]

    live = repo._runtime.scale_artifact_store.scale_dir(notebook.id)
    staged = live.with_name(live.name + ".imported")
    shutil.copytree(live, staged)
    manifest_path = staged / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["version"] == before.manifest["version"]
    manifest["imported_marker"] = True
    manifest_path.write_text(json.dumps(manifest))
    retired = live.with_name(live.name + ".old")
    os.rename(live, retired)
    os.rename(staged, live)
    shutil.rmtree(retired)

    after = repo._scale_index(notebook.id, allow_stale=True)
    assert after is not before
    assert after.manifest.get("imported_marker") is True


# ─────────────────────────────────── 2. 没换代就不许重载(收益不能被吃掉) ──
@pytest.mark.parametrize("allow_stale", [False, True])
def test_an_unchanged_artifact_is_never_reloaded(tmp_path, allow_stale):
    """签名没变 → 一直复用同一个实例。静态大库每次提问 5–10 次 load,任何一次
    多余的重载都是几 GB 的 ANN handle 重开。"""
    version = {"value": ["v-stable"]}
    catalog, store, _cache = _catalog(tmp_path, version)
    _publish(catalog, "nb", {"version": ["v-stable"]})

    loaded = [catalog.load("nb", allow_stale=allow_stale) for _ in range(8)]

    assert all(item is loaded[0] for item in loaded)
    assert store.load_calls == 1, (
        f"one artifact generation must load once, loaded {store.load_calls}x")


def test_a_transiently_unreadable_manifest_keeps_serving_the_cached_index(
    tmp_path,
):
    """签名读不到(swap 的两次 rename 之间那一瞬)不是「换代了」,是「暂时看不见」:
    继续服务手上的实例,绝不因此丢一个多 GB 的 handle。"""
    version = {"value": ["v-stable"]}
    catalog, store, _cache = _catalog(tmp_path, version)
    _publish(catalog, "nb", {"version": ["v-stable"]})
    first = catalog.load("nb")

    os.remove(os.path.join(str(catalog.artifacts.scale_dir("nb")), "manifest.json"))

    assert catalog.load("nb") is first
    assert store.load_calls == 1


def test_an_entry_adopted_with_an_unknown_signature_is_still_supersedable(
    tmp_path,
):
    """codex #643 R5 P2: ``load()``'s OWN top-of-call stat — not just an
    external caller's — can land in the live→``.old``→live rename gap and
    read ``None``, while the ``load_scale`` a few lines later (now a moment
    AFTER the rename finished) opens a genuinely new, valid generation. That
    legitimate instance gets ``_adopt``-ed with an unknown recorded signature.

    The old guard (``recorded is not None and recorded != signature``) treated
    "recorded unknown" the same as "current unknown" — fail-soft forever. But
    unlike the current-side gap (which resolves itself on the very next call,
    since the file is stable again), an unknown RECORDED signature never heals
    on its own: it stays ``None`` on this cached instance permanently, so a
    same-version republish after this point could never be picked up again
    until process restart.

    **Mutation anchor**: reverting to ``recorded is not None and recorded !=
    signature`` makes this red — the second ``load()`` keeps returning the
    gen-1 instance.
    """
    version = {"value": ["v-stable"]}
    catalog, store, _cache = _catalog(tmp_path, version)
    _publish(catalog, "nb", {"version": ["v-stable"], "marker": "gen-1"})

    # Simulate the gap on load()'s FIRST stat only: unreadable right now, even
    # though the manifest is actually there and load_scale succeeds moments
    # later (the exact race the guard has to survive).
    real_stat = store.manifest_stat_signature
    calls = {"n": 0}

    def gapped_once(directory):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_stat(directory)

    store.manifest_stat_signature = gapped_once

    first = catalog.load("nb")
    assert first is not None and first.manifest["marker"] == "gen-1"
    assert store.load_calls == 1

    store.manifest_stat_signature = real_stat  # the gap has passed

    # Same version, republished again (offline CLI / import), same as the
    # blind-spot tests above.
    _publish(catalog, "nb", {"version": ["v-stable"], "marker": "gen-2"})

    second = catalog.load("nb")
    assert second is not first, (
        "stale generation served: an entry recorded with an unknown "
        "signature must still be superseded once a real signature is "
        "available, not cached forever"
    )
    assert second.manifest["marker"] == "gen-2"
    assert store.load_calls == 2


# ──────────────────────────────────────────── 3. 一次 load 只 stat 一次 ──
@pytest.mark.parametrize("allow_stale", [False, True])
@pytest.mark.parametrize("warm", [False, True])
def test_one_load_stats_the_manifest_exactly_once(tmp_path, allow_stale, warm):
    """新增的这次 stat 必须与 ``_manifest_identity`` memo 共用同一次调用。

    冷路径尤其容易退化:``_stale_manifest_admissible`` 在单飞锁前后各调一次,
    再加上换代判据自己那次,不共用就是一次 load 三次 stat。

    诚实预算(codex PR#643 R29 P1 之后):暖命中仍是恰好 1 次;冷路径是
    2 次——锁前那次快路径判等,加锁内那次**重探**(等锁期间盘面可能又换
    了代,拿陈旧签名 double-check 会误逐+旧签名入账)。
    """
    version = {"value": ["v-stable"]}
    catalog, store, _cache = _catalog(tmp_path, version)
    _publish(catalog, "nb", {"version": ["v-stable"]})
    if warm:
        catalog.load("nb", allow_stale=allow_stale)

    store.stat_calls = 0
    assert catalog.load("nb", allow_stale=allow_stale) is not None
    budget = 1 if warm else 2
    assert store.stat_calls == budget, (
        f"this load's stat budget is {budget}, stat'd {store.stat_calls} times")


def test_the_added_stat_is_orders_of_magnitude_below_a_manifest_read(tmp_path):
    """成本如实登记(规格评审 9)的 characterization:新增的 stat 与它所在分支
    上任何一件既有工作相比都可以忽略。

    这里比的是同一份**生产尺寸** manifest(``watermark_sources`` 数万条)的一次
    完整解析 —— 那正是 R2-5 的 memo 已经在替这条路径省掉的东西。本机实测
    (APFS、暖 dentry、1.9MB manifest):单次 ``manifest_stat_signature``
    ≈1.4µs,单次 ``read_manifest`` ≈1.9ms,相差约三个数量级;倍率断言只留了
    100 倍的余量,用来抓「stat 退化成了真读盘」这类灾难性回归,不做精确计时。
    """
    settings = Settings(storage_dir=str(tmp_path))
    store = ScaleArtifactStore(settings)
    directory = store.scale_dir("nb")
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(str(directory), "manifest.json"), "w") as handle:
        json.dump(
            # 48k 条、id 取生产那样的 uuid 长度 → ≈1.9MB,与 docstring 的实测同规模。
            {"version": ["v"], "dim": 64,
             "watermark_sources": [
                 f"src-{i:08d}-0000-0000-0000-000000000000"
                 for i in range(48000)
             ]},
            handle,
        )

    store.manifest_stat_signature(directory)   # 预热 dentry
    started = time.perf_counter()
    for _ in range(100):
        store.manifest_stat_signature(directory)
    stat_seconds = (time.perf_counter() - started) / 100

    started = time.perf_counter()
    store.read_manifest(directory)
    read_seconds = time.perf_counter() - started

    assert stat_seconds * 100 < read_seconds, (
        f"stat {stat_seconds * 1e6:.1f}µs vs manifest read "
        f"{read_seconds * 1e3:.1f}ms — the added probe must stay negligible")


# ───────────────────────────────────────────────── 4. manifest 库版本字段 ──
def test_runtime_library_versions_reports_the_three_libraries():
    versions = scale_index_module.runtime_library_versions()
    assert set(versions) == {"hnswlib", "numpy", "scipy"}
    assert all(isinstance(value, str) and value for value in versions.values())


def test_load_warns_once_when_the_artifact_hnswlib_differs(tmp_path, caplog):
    """异机构建的工件带着别的 hnswlib 版本时,读侧要响亮地说出来。

    只警告不拒绝:工件已经在这台机器上了,拒绝服务它等于把检索打成零召回,比
    带警告服务更糟;硬拒属于离线 CLI 的 ``import`` 校验(T-W2)。
    """
    version = {"value": ["v-stable"]}
    catalog, _store, _cache = _catalog(tmp_path, version)
    _publish(catalog, "nb", {
        "version": ["v-stable"],
        "library_versions": {"hnswlib": "0.0.1-from-another-machine"},
    })

    with caplog.at_level(logging.WARNING, logger="app.services.scale_artifact_catalog"):
        assert catalog.load("nb", allow_stale=True) is not None

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "hnswlib" in message and "0.0.1-from-another-machine" in message
    assert "nb" in message


@pytest.mark.parametrize(
    "library_versions",
    [None, {}, {"numpy": "1.0"}],
    ids=["legacy-artifact", "empty", "no-hnswlib-entry"],
)
def test_an_artifact_without_a_recorded_hnswlib_stays_silent(
    tmp_path, caplog, library_versions,
):
    """未知不是失配:本特性之前构建的工件没有这个键,不许因此刷 warning
    (older-index-stays-valid)。"""
    version = {"value": ["v-stable"]}
    catalog, _store, _cache = _catalog(tmp_path, version)
    manifest = {"version": ["v-stable"]}
    if library_versions is not None:
        manifest["library_versions"] = library_versions
    _publish(catalog, "nb", manifest)

    with caplog.at_level(logging.WARNING, logger="app.services.scale_artifact_catalog"):
        assert catalog.load("nb", allow_stale=True) is not None

    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_a_matching_hnswlib_stays_silent(tmp_path, caplog):
    version = {"value": ["v-stable"]}
    catalog, _store, _cache = _catalog(tmp_path, version)
    running = scale_index_module.runtime_library_versions()["hnswlib"]
    _publish(catalog, "nb", {
        "version": ["v-stable"],
        "library_versions": {"hnswlib": running},
    })

    with caplog.at_level(logging.WARNING, logger="app.services.scale_artifact_catalog"):
        assert catalog.load("nb", allow_stale=True) is not None

    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []
