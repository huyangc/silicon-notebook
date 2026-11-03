"""viz 索引状态探针:只读不构建 + 三态(未建/已就绪/待刷新)。"""
import os
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


def _seed(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept", "payload": {"name": "gain", "section_path": ""}, "evidence": []},
    ], [
        {"source_local_id": "a", "target_local_id": "b", "edge_type": "relates", "evidence": []},
    ])
    return nb


def test_probe_never_builds(repo):
    nb = _seed(repo)
    repo.rebuild_unified_kg(nb.id)
    # 但 rebuild 会主动建;为测"未建"态,删掉 kg_viz 再探
    import shutil
    shutil.rmtree(str(repo._runtime.scale_artifact_store.viz_dir(nb.id)), ignore_errors=True)
    repo._runtime.scale_artifacts.viz_cache.pop(nb.id, None)
    probe = repo._runtime.scale_artifacts.viz_probe(nb.id)
    assert probe["viz_indexed"] is False
    assert probe["viz_stale"] is False
    # 探针没有偷偷构建
    assert not os.path.exists(os.path.join(str(repo._runtime.scale_artifact_store.viz_dir(nb.id)), "manifest.json"))


def test_rebuild_refreshes_viz_index(repo):
    nb = _seed(repo)
    repo.rebuild_unified_kg(nb.id)
    probe = repo._runtime.scale_artifacts.viz_probe(nb.id)
    assert probe["viz_indexed"] is True
    assert probe["viz_nodes"] == 2
    assert probe["viz_stale"] is False


def test_stale_after_mutation(repo):
    nb = _seed(repo)
    repo.rebuild_unified_kg(nb.id)         # 建了新鲜索引
    repo._runtime.scale_artifacts.viz_cache.pop(nb.id, None)
    # 变更 KG(加对象)→ version 变 → 磁盘旧索引变 stale
    repo.store_kg(nb.id, None, [
        {"local_id": "c", "object_type": "concept", "payload": {"name": "bias", "section_path": ""}, "evidence": []},
    ], [])
    probe = repo._runtime.scale_artifacts.viz_probe(nb.id)
    assert probe["viz_indexed"] is False
    assert probe["viz_stale"] is True


def test_unified_kg_status_carries_viz_fields(repo):
    nb = _seed(repo)
    repo.rebuild_unified_kg(nb.id)
    st = repo.unified_kg_status(nb.id)
    assert st["viz_indexed"] is True
    assert st["viz_nodes"] == 2
    assert "viz_edges" in st and "viz_stale" in st


@pytest.mark.parametrize("embedded", [False, True])
def test_cold_viz_status_never_loads_graph_or_ann_payloads(repo, monkeypatch, embedded):
    nb = _seed(repo)
    repo.rebuild_unified_kg(nb.id)
    if embedded:
        repo.build_scale_index(nb.id)
    runtime = repo._runtime.scale_artifacts
    runtime.scale_cache.pop(nb.id, None)
    runtime.viz_cache.pop(nb.id, None)

    def reject_payload(*_args, **_kwargs):
        pytest.fail("a status probe attempted a full index load")

    monkeypatch.setattr(runtime, "load", reject_payload)
    monkeypatch.setattr(runtime.artifacts, "load_viz", reject_payload)
    for _ in range(2):
        assert runtime.viz_probe(nb.id) == {
            "viz_indexed": True,
            "viz_nodes": 2,
            "viz_edges": 1,
            "viz_stale": False,
        }


def test_viz_status_notices_payload_header_damage_after_a_healthy_probe(repo):
    nb = _seed(repo)
    repo.rebuild_unified_kg(nb.id)
    runtime = repo._runtime.scale_artifacts
    assert runtime.viz_probe(nb.id)["viz_indexed"] is True
    path = runtime.artifacts.viz_dir(nb.id) / "viz.npz"
    path.write_bytes(b"broken zip")
    assert runtime.viz_probe(nb.id) == {
        "viz_indexed": False,
        "viz_nodes": 0,
        "viz_edges": 0,
        "viz_stale": False,
    }


def test_viz_status_notices_same_version_republication(repo):
    import json

    nb = _seed(repo)
    repo.rebuild_unified_kg(nb.id)
    runtime = repo._runtime.scale_artifacts
    assert runtime.viz_probe(nb.id)["viz_indexed"] is True
    manifest_path = runtime.artifacts.viz_dir(nb.id) / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    # An externally republished, incomplete generation can have the same DB
    # version. Its count mismatch must not reuse the previous healthy result.
    manifest["n_viz_nodes"] += 1
    temporary = manifest_path.with_suffix(".replacement")
    temporary.write_text(json.dumps(manifest))
    temporary.replace(manifest_path)
    assert runtime.viz_probe(nb.id)["viz_indexed"] is False


@pytest.mark.parametrize("load_succeeds", [False, True])
def test_concurrent_cold_viz_reads_share_one_load(repo, monkeypatch, load_succeeds):
    import threading
    from concurrent.futures import Future, ThreadPoolExecutor

    import app.services.scale_artifact_runtime as runtime_mod

    nb = _seed(repo)
    repo.rebuild_unified_kg(nb.id)
    runtime = repo._runtime.scale_artifacts
    runtime.viz_cache.pop(nb.id, None)
    monkeypatch.setattr(runtime, "load", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 0)
    real_load = runtime.artifacts.load_viz
    entered, followed, release = threading.Event(), threading.Event(), threading.Event()
    calls = []

    class ObservedFuture(Future):
        def result(self, *args, **kwargs):
            followed.set()
            return super().result(*args, **kwargs)

    def load(notebook_id):
        calls.append(notebook_id)
        entered.set()
        assert release.wait(5)
        return real_load(notebook_id) if load_succeeds else None

    monkeypatch.setattr(runtime_mod, "Future", ObservedFuture, raising=False)
    monkeypatch.setattr(runtime.artifacts, "load_viz", load)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(runtime.viz_index, nb.id)
        try:
            assert entered.wait(5)
            second = pool.submit(runtime.viz_index, nb.id)
            assert followed.wait(5)
        finally:
            release.set()
        a, b = first.result(5), second.result(5)
    assert a is b
    assert (a is not None) is load_succeeds
    assert calls == [nb.id]


def test_cold_viz_read_retries_a_generation_replaced_during_load(repo, monkeypatch):
    import weakref
    from types import SimpleNamespace

    nb = _seed(repo)
    runtime = repo._runtime.scale_artifacts
    monkeypatch.setattr(runtime, "load", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_viz_head_predates_isolation", lambda *_args: False)
    cur = runtime.version(nb.id)
    cseq = int(runtime.projections.version_signal(nb.id)[1])
    manifest = {"version": cur, "cluster_seq": cseq, "memory_isolation": 1}
    replacement = SimpleNamespace(manifest=manifest)
    discarded = []

    class LoadedIndex:
        def __init__(self):
            self.manifest = dict(manifest)
    signature = ["first"]
    monkeypatch.setattr(runtime, "_viz_signature", lambda _nb: signature[0])
    calls = []

    def load(_notebook_id):
        calls.append(signature[0])
        if len(calls) == 1:
            first = LoadedIndex()
            discarded.append(weakref.ref(first))
            signature[0] = "replacement"
            return first
        assert discarded[0]() is None, "release the old graph before materializing its replacement"
        return replacement

    monkeypatch.setattr(runtime.artifacts, "load_viz", load)
    assert runtime.viz_index(nb.id) is replacement
    assert runtime._viz_cache_entry(runtime.viz_cache.get(nb.id)) == (replacement, "replacement")
    assert calls == ["first", "replacement"]


@pytest.mark.parametrize("mutation", ["version", "cluster_seq"])
def test_waiting_viz_read_rechecks_database_freshness(repo, monkeypatch, mutation):
    import threading
    from concurrent.futures import Future, ThreadPoolExecutor

    import app.services.scale_artifact_runtime as runtime_mod

    nb = _seed(repo)
    repo.rebuild_unified_kg(nb.id)
    runtime = repo._runtime.scale_artifacts
    runtime.viz_cache.pop(nb.id, None)
    monkeypatch.setattr(runtime, "load", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_viz_lazy_build_refused", lambda *_args: False)
    monkeypatch.setattr(runtime, "_spawn_viz_build", lambda _nb: None)
    entered, followed, release = threading.Event(), threading.Event(), threading.Event()
    loads, callbacks = [], []
    real_load = runtime.artifacts.load_viz

    class ObservedFuture(Future):
        def result(self, *args, **kwargs):
            followed.set()
            return super().result(*args, **kwargs)

    def load(notebook_id):
        loads.append(notebook_id)
        entered.set()
        assert release.wait(5)
        return real_load(notebook_id)

    monkeypatch.setattr(runtime_mod, "Future", ObservedFuture)
    monkeypatch.setattr(runtime.artifacts, "load_viz", load)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(runtime.viz_index, nb.id, on_refresh=lambda: callbacks.append("first"))
        try:
            assert entered.wait(5)
            if mutation == "version":
                repo.store_kg(nb.id, None, [{
                    "local_id": "new", "object_type": "concept",
                    "payload": {"name": "new"}, "evidence": [],
                }], [])
            else:
                with repo._write() as db:
                    repo._runtime.knowledge_lifecycle._bump_cluster_mutation_seq(db, nb.id)
            second = pool.submit(runtime.viz_index, nb.id, on_refresh=lambda: callbacks.append("second"))
            assert followed.wait(5)
        finally:
            release.set()
        assert first.result(5) is second.result(5)
    assert loads == [nb.id]
    assert sorted(callbacks) == ["first", "second"]
