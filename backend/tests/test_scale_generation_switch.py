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


# ──────────────────────────────────────────── 3. 一次 load 只 stat 一次 ──
@pytest.mark.parametrize("allow_stale", [False, True])
@pytest.mark.parametrize("warm", [False, True])
def test_one_load_stats_the_manifest_exactly_once(tmp_path, allow_stale, warm):
    """新增的这次 stat 必须与 ``_manifest_identity`` memo 共用同一次调用。

    冷路径尤其容易退化:``_stale_manifest_admissible`` 在单飞锁前后各调一次,
    再加上换代判据自己那次,不共用就是一次 load 三次 stat。
    """
    version = {"value": ["v-stable"]}
    catalog, store, _cache = _catalog(tmp_path, version)
    _publish(catalog, "nb", {"version": ["v-stable"]})
    if warm:
        catalog.load("nb", allow_stale=allow_stale)

    store.stat_calls = 0
    assert catalog.load("nb", allow_stale=allow_stale) is not None
    assert store.stat_calls == 1, (
        f"one load must stat once, stat'd {store.stat_calls} times")


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
