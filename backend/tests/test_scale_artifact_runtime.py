"""Task 20: scale/viz cache, version and scheduling state has one owner."""
from __future__ import annotations

import datetime
import gc
import shutil
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.filesystem.scale_artifact_store import MANIFEST_ABSENT
from app.services.embedding import FakeEmbedder
from app.services.scale_artifact_runtime import offpeak_window_state
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    return _make_repo(tmp_path, monkeypatch)


def _make_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for key, value in {"EMBED_DIM": "16"}.items():
        monkeypatch.setenv(key, value)
    repository = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    return repository


def _seed(repo):
    notebook = repo.create_notebook(NotebookCreate(name="scale-runtime"))
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
    return notebook


def test_facade_scale_runtime_properties_share_identity(repo):
    scale = repo._runtime.scale_artifacts
    assert scale.catalog is repo._runtime.scale_catalog
    assert scale.builder is repo._runtime.scale_builder
    assert repo._scale_idx_cache is scale.scale_cache
    assert repo._viz_idx_cache is scale.viz_cache
    assert repo._scale_ver_cache is scale.version_memo
    assert repo._scale_ver_lock is scale.version_lock
    assert repo._scale_ver_locks is scale.version_locks
    assert repo._scale_idx_load_lock is scale.load_lock
    assert repo._scale_idx_load_locks is scale.load_locks
    assert repo._scale_building is scale.building
    assert repo._scale_building_lock is scale.building_lock
    assert repo._scale_idle_queue is scale.idle_queue
    assert repo._auto_index_checked is scale.auto_index_checked
    assert repo._viz_building is scale.viz_building
    assert repo._viz_building_lock is scale.viz_building_lock


def test_scale_runtime_callbacks_do_not_retain_repository_facade(repo):
    import functools
    import inspect

    def retains(value, target, seen=None):
        if value is target:
            return True
        if value is None or isinstance(value, (str, bytes, int, float, bool)):
            return False
        seen = seen or set()
        marker = id(value)
        if marker in seen:
            return False
        seen.add(marker)
        if inspect.ismethod(value):
            return retains(value.__self__, target, seen) or retains(
                value.__func__, target, seen
            )
        if isinstance(value, functools.partial):
            return any(
                retains(part, target, seen)
                for part in (value.func, value.args, value.keywords)
            )
        if inspect.isfunction(value):
            stored = [value.__defaults__, value.__kwdefaults__]
            stored.extend(
                cell.cell_contents for cell in (value.__closure__ or ())
            )
            return any(retains(part, target, seen) for part in stored)
        if isinstance(value, dict):
            return any(
                retains(part, target, seen)
                for pair in value.items()
                for part in pair
            )
        if isinstance(value, (list, tuple, set, frozenset)):
            return any(retains(part, target, seen) for part in value)
        return False

    scale = repo._runtime.scale_artifacts
    callbacks = {
        name: getattr(scale, name)
        for name in {
            "get_notebook",
            "notebook_copy_stats",
            "eligible",
            "notify_index_done",
            "unified_status",
        }
    }
    assert [name for name, callback in callbacks.items() if retains(callback, repo)] == []


def test_retained_scale_runtime_does_not_transitively_retain_repository(
    tmp_path, monkeypatch
):
    repository = _make_repo(tmp_path, monkeypatch)
    notebook = repository.create_notebook(NotebookCreate(name="retention"))
    notebook_id = notebook.id
    scale = repository._runtime.scale_artifacts

    assert scale.unified_status(notebook_id)["dirty"] is False

    repository_ref = weakref.ref(repository)
    del notebook
    del repository
    gc.collect()

    assert repository_ref() is None
    with pytest.raises(RuntimeError, match="knowledge lifecycle is not wired"):
        scale.unified_status(notebook_id)


# ── batch-3-W1 PR-2 (design doc Sec 3.4): conditional kg_reset_epoch in
# version() — "上线模拟" hard gate ─────────────────────────────────────────


def test_epoch_zero_notebooks_version_list_never_carries_kg_reset_epoch(repo):
    """A notebook that has never been through ``delete_notebook_kg`` sits at
    ``kg_reset_epoch == 0`` forever. Sec 3.4's "on-rollout simulation" gate:
    such a notebook's ``version()`` list must be exactly what it always was
    (byte-identical, no unexpected trailing element) — appending
    unconditionally would make every existing on-disk manifest compare
    unequal the instant this PR ships (red line one, fleet-wide rebuild
    storm). Two independently-seeded notebooks are checked so this is not a
    single-instance fluke.

    变异锚点:把 ``version()`` 里 ``if epoch:`` 的条件追加改成无条件追加,本条
    必须报红(mutation-verified — post-review P3-6: 原先陪跑的
    ``test_unconditional_kg_reset_epoch_append_is_the_mutation_this_gate_
    exists_to_catch`` 是自证的假守卫,monkeypatch 出一份手写的"无条件追加"实现
    再断言它符合自己的描述,从不调用真实 ``version()``,删真实实现里的
    ``if epoch:`` 也不会让它报红。已删除,改由本条 + 下面两条在真实代码上双向
    覆盖:本条钉 epoch=0 分支,``test_delete_notebook_kg_appends_kg_reset_
    epoch_to_the_version_list`` 钉 epoch>0 分支)。
    """
    scale = repo._runtime.scale_artifacts
    for name in ("epoch-zero-a", "epoch-zero-b"):
        notebook = repo.create_notebook(NotebookCreate(name=name))
        repo.store_kg(
            notebook.id, None,
            [{"local_id": "a", "object_type": "concept",
              "payload": {"name": "x", "section_path": ""}, "evidence": []}],
            [],
        )
        version = scale.version(notebook.id)
        assert "kg_reset_epoch" not in version, (
            f"{name}: an epoch-0 notebook's version list must not carry "
            "kg_reset_epoch at all"
        )
        with repo._connect() as db:
            row = db.execute(
                "SELECT kg_reset_epoch FROM unified_kg_state WHERE notebook_id=?",
                (notebook.id,),
            ).fetchone()
        assert int(row["kg_reset_epoch"]) == 0


def test_delete_notebook_kg_appends_kg_reset_epoch_to_the_version_list(repo):
    """The ONE legitimate case the conditional append exists to serve: a
    notebook that HAS been through delete_notebook_kg gets a version() list
    with ``["kg_reset_epoch", N]`` as its trailing two elements — a real,
    warranted staleness against any manifest written before the delete (the
    KG genuinely was reset, so a rebuild is the correct outcome)."""
    scale = repo._runtime.scale_artifacts
    notebook = _seed(repo)
    before = scale.version(notebook.id)
    assert "kg_reset_epoch" not in before

    repo.delete_notebook_kg(notebook.id)

    after = scale.version(notebook.id)
    assert after[-2:] == ["kg_reset_epoch", 1], (
        "kg_reset_epoch must be appended as the LAST two elements once "
        "epoch > 0 -- inserting it anywhere else would silently shift the "
        "three positional [1]-index consumers (scale_artifact_runtime.py, "
        "scale_index_builder.py)"
    )
    assert after != before, (
        "a real KG reset must produce a genuinely different version list "
        "(a warranted rebuild), not merely append-then-still-compare-equal"
    )

    # process-internal memo must not serve the pre-delete list for a
    # post-delete read: calling version() again returns the SAME (already
    # reflecting-the-reset) list, not the stale pre-delete one.
    assert scale.version(notebook.id) == after


def test_kg_reset_epoch_only_affects_the_deleted_notebook_not_its_siblings(repo):
    """design doc Sec 3.4's behaviour matrix, per-notebook isolation half:
    ``kg_reset_epoch`` is a per-notebook column (design doc Sec 3.3 point 2),
    so deleting ONE notebook's KG must never touch a sibling's version()
    list — a sibling that has never been reset stays byte-identical
    (epoch=0, no trailing kg_reset_epoch element), exactly as if the deleted
    notebook did not exist. This is the scenario the fleet-wide-rebuild-storm
    hazard (Sec 3.4's red line one) is really about: an epoch bump must be
    scoped to the one notebook whose KG genuinely was reset, never leak into
    every OTHER notebook's manifest comparison."""
    scale = repo._runtime.scale_artifacts
    reset_notebook = _seed(repo)
    sibling = repo.create_notebook(NotebookCreate(name="untouched sibling"))
    repo.store_kg(
        sibling.id, None,
        [{"local_id": "s", "object_type": "concept",
          "payload": {"name": "sibling", "section_path": ""}, "evidence": []}],
        [],
    )
    sibling_before = scale.version(sibling.id)
    assert "kg_reset_epoch" not in sibling_before

    repo.delete_notebook_kg(reset_notebook.id)

    # The reset notebook's own list now carries the trailing element...
    reset_after = scale.version(reset_notebook.id)
    assert reset_after[-2:] == ["kg_reset_epoch", 1]
    # ...but the untouched sibling's list, and its epoch, are unaffected.
    sibling_after = scale.version(sibling.id)
    assert "kg_reset_epoch" not in sibling_after
    assert sibling_after == sibling_before
    with repo._connect() as db:
        sibling_row = db.execute(
            "SELECT kg_reset_epoch FROM unified_kg_state WHERE notebook_id=?",
            (sibling.id,),
        ).fetchone()
    assert int(sibling_row["kg_reset_epoch"]) == 0


def test_version_cold_failure_releases_singleflight_and_does_not_poison_memo(
    repo, monkeypatch
):
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    projections = repo._runtime.index_projections
    real = projections.version_facts
    calls = 0

    def fail_once(notebook_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("cold version failed")
        return real(notebook_id)

    monkeypatch.setattr(projections, "version_facts", fail_once)
    with pytest.raises(RuntimeError, match="cold version failed"):
        scale.version(notebook.id)
    version = scale.version(notebook.id)
    assert isinstance(version, list)
    assert calls == 2
    assert notebook.id in scale.version_memo


def test_six_concurrent_stale_loads_hit_disk_once(repo, monkeypatch):
    notebook = _seed(repo)
    repo.build_scale_index(notebook.id)
    repo.store_kg(
        notebook.id,
        None,
        [{
            "local_id": "b",
            "object_type": "concept",
            "payload": {"name": "gm", "section_path": ""},
            "evidence": [],
        }],
        [],
    )
    scale = repo._runtime.scale_artifacts
    scale.scale_cache.pop(notebook.id, None)
    store = repo._runtime.scale_artifact_store
    real = store.load_scale
    calls = 0
    calls_lock = threading.Lock()

    def slow_load(notebook_id):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.03)
        return real(notebook_id)

    monkeypatch.setattr(store, "load_scale", slow_load)
    with ThreadPoolExecutor(max_workers=6) as pool:
        loaded = list(pool.map(lambda _: scale.load(notebook.id, allow_stale=True), range(6)))
    assert calls == 1
    assert loaded[0] is not None
    assert all(item is loaded[0] for item in loaded)


def test_exact_and_stale_loads_preserve_disk_identity(repo):
    notebook = _seed(repo)
    repo.build_scale_index(notebook.id)
    scale = repo._runtime.scale_artifacts
    exact = scale.load(notebook.id)
    assert exact is not None
    repo.store_kg(
        notebook.id,
        None,
        [{
            "local_id": "b",
            "object_type": "concept",
            "payload": {"name": "ro", "section_path": ""},
            "evidence": [],
        }],
        [],
    )
    assert scale.load(notebook.id) is None
    stale_a = scale.load(notebook.id, allow_stale=True)
    stale_b = scale.load(notebook.id, allow_stale=True)
    assert stale_a is exact
    assert stale_b is exact


def test_startup_preload_loads_ann_and_safe_ppr_core_before_progress(repo):
    notebook = _seed(repo)
    repo.build_scale_index(notebook.id)
    scale = repo._runtime.scale_artifacts
    scale.scale_cache.pop(notebook.id, None)
    # R2-2 之后 ScaleArtifactRuntime 不再持有 snapshots(唯一读者是搬走的
    # copy-stats memo);共享 VectorCache 仍由 facade 的写穿句柄暴露,是同一
    # 个对象。
    repo._vector_cache.invalidate(f"{notebook.id}:scale_combined")
    progress = []

    result = repo._preload_scale_retrieval_artifacts(
        progress=lambda done, total: progress.append((done, total))
    )

    loaded = scale.scale_cache.get(notebook.id)
    assert loaded is not None
    assert loaded.ann_handle is not None
    assert loaded._ppr_transition.dtype.name == "float32"
    assert loaded._ppr_chunk_ids == {
        loaded.node_ids[int(position)] for position in loaded.chunk_index
    }
    # Combined graphs share the general VectorCache and are intentionally not
    # claimed as startup-resident: mounted multi-index composition can require
    # multi-GB copies.  The reusable self-only core lives on ScaleIndex instead.
    assert f"{notebook.id}:scale_combined" not in repo._vector_cache.keys()
    assert result == {"indexes": 1, "ann_handles": 1, "ppr_cores": 1}
    assert progress == [(0, 1), (1, 1)]


def test_startup_preload_rejects_declared_enabled_ann_with_missing_labels(
    repo, monkeypatch
):
    notebook = _seed(repo)
    repo.build_scale_index(notebook.id)
    scale = repo._runtime.scale_artifacts
    index = scale.load(notebook.id, allow_stale=True)
    index.manifest["has_chunk_ann"] = True
    index.chunk_ann_labels = None
    monkeypatch.setattr(scale.settings, "chunk_ann_enabled", True)
    monkeypatch.setattr(scale, "load", lambda *_args, **_kwargs: index)

    with pytest.raises(RuntimeError, match="declared scale ANN"):
        scale._preload_one_retrieval_index(notebook.id)


def test_startup_preload_refuses_to_evict_earlier_indexes(repo, monkeypatch):
    scale = repo._runtime.scale_artifacts
    monkeypatch.setattr(scale.artifacts, "indexed_notebook_ids", lambda: ["a", "b"])
    monkeypatch.setattr(scale.projections, "notebook_tier", lambda _id: "personal")
    monkeypatch.setattr(scale.settings, "scale_idx_cache_max", 1)
    monkeypatch.setattr(
        scale,
        "load",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("capacity must fail before loading")
        ),
    )

    with pytest.raises(RuntimeError, match="SCALE_IDX_CACHE_MAX"):
        scale.preload_retrieval_artifacts()


def test_startup_preload_refuses_more_large_indexes_than_large_cache_cap(
    repo, monkeypatch
):
    scale = repo._runtime.scale_artifacts
    monkeypatch.setattr(scale.artifacts, "indexed_notebook_ids", lambda: ["a", "b"])
    monkeypatch.setattr(scale.projections, "notebook_tier", lambda _id: "personal")
    monkeypatch.setattr(scale.settings, "scale_idx_cache_max", 8)
    monkeypatch.setattr(scale.settings, "scale_idx_cache_max_large", 1)
    monkeypatch.setattr(scale.settings, "scale_idx_large_bytes", 1)
    monkeypatch.setattr(
        scale.artifacts,
        "read_manifest",
        lambda _directory: {"n_ann": 1},
    )
    monkeypatch.setattr(
        scale,
        "_preload_one_retrieval_index",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("large-capacity check must precede loading")
        ),
    )

    with pytest.raises(RuntimeError, match="SCALE_IDX_CACHE_MAX_LARGE"):
        scale.preload_retrieval_artifacts()


def test_startup_preload_accepts_large_indexes_when_large_cache_cap_is_sufficient(
    repo, monkeypatch
):
    scale = repo._runtime.scale_artifacts
    monkeypatch.setattr(scale.artifacts, "indexed_notebook_ids", lambda: ["a", "b"])
    monkeypatch.setattr(scale.projections, "notebook_tier", lambda _id: "personal")
    monkeypatch.setattr(scale.settings, "scale_idx_cache_max", 8)
    monkeypatch.setattr(scale.settings, "scale_idx_cache_max_large", 2)
    monkeypatch.setattr(scale.settings, "scale_idx_large_bytes", 1)
    monkeypatch.setattr(
        scale.artifacts,
        "read_manifest",
        lambda _directory: {"n_ann": 1},
    )
    loaded = []
    monkeypatch.setattr(
        scale,
        "_preload_one_retrieval_index",
        lambda notebook_id: (loaded.append(notebook_id) or {
            "ann_handles": 1,
            "ppr_cores": 0,
        }),
    )

    result = scale.preload_retrieval_artifacts()

    assert loaded == ["a", "b"]
    assert result == {"indexes": 2, "ann_handles": 2, "ppr_cores": 0}


def test_startup_preload_keeps_small_indexes_on_total_capacity_rail(
    repo, monkeypatch
):
    scale = repo._runtime.scale_artifacts
    monkeypatch.setattr(scale.artifacts, "indexed_notebook_ids", lambda: ["a", "b"])
    monkeypatch.setattr(scale.projections, "notebook_tier", lambda _id: "personal")
    monkeypatch.setattr(scale.settings, "scale_idx_cache_max", 2)
    monkeypatch.setattr(scale.settings, "scale_idx_cache_max_large", 1)
    monkeypatch.setattr(scale.settings, "scale_idx_large_bytes", 1_000_000)
    monkeypatch.setattr(
        scale.artifacts,
        "read_manifest",
        lambda _directory: {"n_ann": 1},
    )
    loaded = []
    monkeypatch.setattr(
        scale,
        "_preload_one_retrieval_index",
        lambda notebook_id: (loaded.append(notebook_id) or {
            "ann_handles": 0,
            "ppr_cores": 0,
        }),
    )

    scale.preload_retrieval_artifacts()

    assert loaded == ["a", "b"]


def test_viz_probe_is_read_only_and_never_invokes_builder(repo, monkeypatch):
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    monkeypatch.setattr(
        scale.builder,
        "build_viz",
        lambda *_: (_ for _ in ()).throw(AssertionError("probe attempted build")),
    )
    assert scale.viz_probe(notebook.id) == {
        "viz_indexed": False,
        "viz_nodes": 0,
        "viz_edges": 0,
        "viz_stale": False,
    }


def test_auto_index_once_set_short_circuits_before_scale_fact_aggregates(
    repo, monkeypatch
):
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    scale.auto_index_checked.add(notebook.id)
    monkeypatch.setattr(
        repo._runtime.index_projections,
        "version_facts",
        lambda *_: (_ for _ in ()).throw(AssertionError("scale facts queried")),
    )
    monkeypatch.setattr(
        scale,
        "notebook_copy_stats",
        lambda *_: (_ for _ in ()).throw(AssertionError("copy aggregates queried")),
    )
    scale.maybe_auto_index(notebook.id)


class _VizClaim:
    """A granted cross-process claim for the viz-rebuild tests."""

    supported = True

    def __init__(self, *, held: bool = True) -> None:
        self.claim_token = "viz-token"
        self._held = held
        self.released = False

    def verify_held(self) -> bool:
        return self._held

    def release(self) -> None:
        self.released = True


def test_a_background_viz_rebuild_gives_way_when_the_claim_is_held(
    repo, monkeypatch
):
    """P2, codex PR#643 R12: the standalone viz rebuild used to write the live
    ``kg_viz`` root with only a process-local marker for company — so the
    offline CLI's ``export`` could copy a half-written root, and its
    ``import`` could rename or retire that root out from under the writer. The
    rebuild now takes the SAME per-notebook cross-process claim; when somebody
    else holds it (a CLI import, another replica) this run gives way entirely.

    Giving way must leave no residue: ``viz_building`` is the marker that
    suppresses duplicate spawns, and a rebuild that never ran must clear it or
    no later trigger can ever start one.

    Mutation anchor: call ``self.builder.build_viz`` without taking the claim
    and this goes red — the builder runs while the CLI owns the notebook.
    """
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    monkeypatch.setattr(scale, "_start_daemon", lambda _name, target: target())
    # "Provably held by somebody else" — the CLI's import is mid-publish.
    monkeypatch.setattr(scale, "_scale_build_lock", lambda _nb: None)
    # Recorded, not raised: the background worker is fail-open by design and
    # would swallow an AssertionError, hiding exactly the regression this
    # pins.
    attempts: list[str] = []
    monkeypatch.setattr(
        scale.builder, "build_viz", lambda nb: attempts.append(nb)
    )

    scale._spawn_viz_build(notebook.id)

    assert attempts == [], (
        "the viz rebuild ran while another process held the notebook's claim"
    )
    assert notebook.id not in scale.viz_building
    assert notebook.id not in scale._scale_build_lock_handles


def test_the_synchronous_viz_path_does_not_block_on_a_held_claim(
    repo, monkeypatch
):
    """The small-notebook branch of ``viz_index`` builds synchronously on the
    request thread. A held claim there must SKIP the build and answer with
    whatever is already available — never wait for the other builder, and
    never raise into an interactive graph-view read."""
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    monkeypatch.setattr(scale, "_scale_build_lock", lambda _nb: None)
    monkeypatch.setattr(
        scale.builder,
        "build_viz",
        lambda *_: (_ for _ in ()).throw(AssertionError("sync build attempted")),
    )
    monkeypatch.setattr(scale.settings, "viz_sync_build_max_objects", 10**6)

    assert scale.viz_index(notebook.id) is None


def test_an_unsupported_lock_backend_keeps_the_in_process_viz_behaviour(
    repo, monkeypatch
):
    """SQLite has no cross-process claim at all and the offline CLI refuses
    that deployment outright, so ``UNSUPPORTED`` is not a failure (the
    three-state contract) — the viz rebuild runs exactly as it always has, and
    nothing is registered as a claim."""
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    built: list[str] = []
    monkeypatch.setattr(
        scale.builder, "build_viz", lambda nb: built.append(nb) or {"n": 1}
    )

    assert scale.build_viz(notebook.id) == {"n": 1}
    assert built == [notebook.id]
    assert notebook.id not in scale._scale_build_lock_handles


def test_a_viz_rebuild_registers_its_claim_for_the_swap_to_reverify(
    repo, monkeypatch
):
    """The claim is held across build AND publish, and it is REGISTERED while
    held: that is what makes the builder's existing
    ``scale_build_claim_token``/``verify_scale_build_lock`` hooks resolve to
    it, carrying the token into the staging path and the re-verification into
    the swap. A claim lost mid-build is refused by that swap and comes back as
    "nothing built" rather than escaping into a graph-view read."""
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    lost = _VizClaim(held=False)
    monkeypatch.setattr(scale, "_scale_build_lock", lambda _nb: lost)
    observed: dict[str, object] = {}

    def observe(notebook_id):
        observed["token"] = scale.scale_build_claim_token(notebook_id)
        observed["held"] = scale.verify_scale_build_lock(notebook_id)
        return scale.builder.__class__.build_viz(scale.builder, notebook_id)

    monkeypatch.setattr(scale.builder, "build_viz", observe)

    assert scale.build_viz(notebook.id) is None, (
        "a claim lost before the swap must read as 'nothing built', not raise"
    )
    assert observed == {"token": "viz-token", "held": False}
    assert lost.released is True
    assert notebook.id not in scale._scale_build_lock_handles
    live = repo._runtime.scale_artifact_store.viz_dir(notebook.id)
    assert not live.exists(), "the refused swap must not publish anything"
    assert Path(f"{live}.tmp-viz-token").is_dir(), (
        "the build DID run and staged under this claim's own token — without "
        "this the assertion above would pass vacuously on an empty graph"
    )


# ─────────────────────────── standalone viz cache · disk-generation probing ──

def _warm_standalone_viz(repo):
    """A published standalone ``kg_viz`` plus the warm cache entry a serving
    process would hold for it, and the notebook it belongs to."""
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    assert scale.build_viz(notebook.id) is not None
    index = scale.viz_index(notebook.id)
    assert index is not None
    return notebook, scale, index


def _replace_viz_root_out_of_band(repo, notebook_id):
    """A cross-process republish of the SAME generation: identical manifest
    (same ``version``, same ``cluster_seq``), new inode, and no call into this
    process's invalidation. Exactly what the offline CLI's ``import`` leaves
    behind when the package repeats the live version."""
    live = Path(str(repo._runtime.scale_artifact_store.viz_dir(notebook_id)))
    staged = live.parent / f"{live.name}.newgen"
    shutil.copytree(live, staged)
    shutil.rmtree(live)
    staged.rename(live)


def test_a_same_version_viz_republish_is_picked_up_without_a_restart(repo):
    """P2, codex PR#643 R18. ``_viz_manifest_fresh``'s two gates
    (``version``/``cluster_seq``) are database-derived, so a cross-process
    ``import`` that replaces ``kg_viz`` under the same version moves neither —
    the warm entry would be served for the life of the process. One stat of the
    viz root's manifest gives the on-disk generation a comparable identity, the
    same way the source-partition companion cache does.

    Mutation anchor: drop the ``_viz_signature_superseded`` check on the hit
    path and the stale object is returned (``second is first``).
    """
    notebook, scale, first = _warm_standalone_viz(repo)
    _replace_viz_root_out_of_band(repo, notebook.id)

    second = scale.viz_index(notebook.id)

    assert second is not None
    assert second is not first, "the warm cache served the superseded generation"
    assert second.manifest == first.manifest, (
        "same generation content — only the bytes on disk were replaced, which "
        "is precisely what the version/cluster_seq gates cannot see"
    )
    assert scale.viz_index(notebook.id) is second, (
        "the reload must RECORD the new signature; otherwise every later read "
        "reloads too"
    )


def test_one_viz_read_takes_at_most_one_viz_stat_probe(repo, monkeypatch):
    """The "one load, one stat" discipline the companion cache follows: a cache
    HIT pays a single probe of the VIZ root, shared with the entry that read may
    write. (``load()``'s own probe of the SCALE root is the catalog's, counted
    separately and unchanged by this.)"""
    notebook, scale, _first = _warm_standalone_viz(repo)
    viz_dir = repo._runtime.scale_artifact_store.viz_dir(notebook.id)
    probe = scale.artifacts.manifest_stat_signature
    calls: list[object] = []
    monkeypatch.setattr(
        scale.artifacts,
        "manifest_stat_signature",
        lambda directory: calls.append(directory) or probe(directory),
    )

    assert scale.viz_index(notebook.id) is not None
    assert [str(call) for call in calls].count(str(viz_dir)) == 1, calls


def test_a_retired_viz_root_is_evicted_instead_of_served_forever(repo, monkeypatch):
    """A same-version ``import`` whose package omits ``kg_viz`` RETIRES the live
    root. Nothing about that moves the database-derived gates, and the probe
    keeps answering "absent", so a fail-soft reading of that answer would serve
    the removed generation until the process restarted — the R12 finding, in the
    viz cache instead of the companion cache.

    ``build_viz`` is stubbed out so the eviction is observable on its own; in
    production this call goes on to the existing "no standalone viz" path and
    spawns or runs a rebuild exactly as a cold read would.

    Mutation anchors: (a) drop the ``MANIFEST_ABSENT`` eviction *and* the
    superseded check, or (b) collapse ``_viz_signature`` back to a two-state
    probe that answers ``None`` for an absent root — either way the retired
    index is handed back.
    """
    notebook, scale, _first = _warm_standalone_viz(repo)
    monkeypatch.setattr(scale.builder, "build_viz", lambda *_: None)
    shutil.rmtree(Path(str(repo._runtime.scale_artifact_store.viz_dir(notebook.id))))

    assert scale.viz_index(notebook.id) is None
    assert notebook.id not in scale.viz_cache


def test_the_viz_mid_swap_window_keeps_serving_the_warm_cache(repo, monkeypatch):
    """codex PR#643 R22 P2, viz mirror: ``save_viz`` publishes through the
    same two-rename sequence, so a stat landing between ``live → .old`` and
    ``tmp → live`` sees the root transiently invisible on an ordinary
    republish. With the previous generation's manifest still at ``.old`` the
    probe must answer "could not tell" and the warm entry keeps serving —
    not the durable-retirement eviction.

    Mutation anchor: drop the ``.old`` confirmation in ``_viz_signature``
    and this goes red — the warm index is evicted mid-window.
    """
    notebook, scale, first = _warm_standalone_viz(repo)
    monkeypatch.setattr(scale.builder, "build_viz", lambda *_: None)
    live = Path(str(repo._runtime.scale_artifact_store.viz_dir(notebook.id)))
    live.rename(str(live) + ".old")

    assert scale.viz_index(notebook.id) is first, (
        "a transiently invisible root (.old still on disk) must stay fail-soft"
    )
    assert notebook.id in scale.viz_cache


def test_a_viz_swap_completed_between_probes_is_not_read_as_retirement(
    repo, monkeypatch
):
    """codex PR#643 R23 P2, viz mirror: between the live probe (ENOENT,
    mid-swap) and the ``.old`` probe, the publisher can finish ``tmp → live``
    and delete ``.old`` — both probes then miss a generation that is now
    live. The probe rechecks the live path before declaring durable absence.

    Mutation anchor: drop the live recheck in ``_viz_signature`` and this
    goes red — the warm index is evicted.
    """
    notebook, scale, first = _warm_standalone_viz(repo)
    monkeypatch.setattr(scale.builder, "build_viz", lambda *_: None)
    real = scale.artifacts.manifest_stat_signature
    answers = iter([MANIFEST_ABSENT, MANIFEST_ABSENT])

    def racing(directory):
        try:
            return next(answers)
        except StopIteration:
            return real(directory)

    monkeypatch.setattr(scale.artifacts, "manifest_stat_signature", racing)
    assert scale.viz_index(notebook.id) is first, (
        "a swap that completed between the two probes must not evict the "
        "warm index"
    )
    assert notebook.id in scale.viz_cache


def test_a_second_viz_publication_racing_the_recheck_stays_fail_soft(
    repo, monkeypatch
):
    """codex PR#643 R25 P2, viz mirror: three consecutive misses can all be
    explained by two back-to-back publications; the final ``.old`` look
    catches the second publisher mid-swap and stays fail-soft.

    Mutation anchor: drop the final ``.old`` look in ``_viz_signature`` and
    this goes red — the warm index is evicted mid-publication.
    """
    notebook, scale, first = _warm_standalone_viz(repo)
    monkeypatch.setattr(scale.builder, "build_viz", lambda *_: None)
    live = Path(str(repo._runtime.scale_artifact_store.viz_dir(notebook.id)))
    live.rename(str(live) + ".old")
    real = scale.artifacts.manifest_stat_signature
    answers = iter([MANIFEST_ABSENT, MANIFEST_ABSENT, MANIFEST_ABSENT])

    def racing(directory):
        try:
            return next(answers)
        except StopIteration:
            return real(directory)

    monkeypatch.setattr(scale.artifacts, "manifest_stat_signature", racing)
    assert scale.viz_index(notebook.id) is first, (
        "a second publication racing the recheck must stay fail-soft"
    )
    assert notebook.id in scale.viz_cache


def test_an_adapter_without_a_stat_probe_keeps_its_warm_viz(repo, monkeypatch):
    """Negative anchor: "no probe on this adapter" is not "changed". Old test
    doubles and any artifacts adapter without ``manifest_stat_signature`` keep
    the pre-existing behaviour exactly — fail-soft, keep serving."""
    notebook, scale, first = _warm_standalone_viz(repo)
    monkeypatch.setattr(scale.artifacts, "manifest_stat_signature", None)
    _replace_viz_root_out_of_band(repo, notebook.id)

    assert scale.viz_index(notebook.id) is first


def test_a_probe_that_cannot_answer_keeps_the_warm_viz(repo, monkeypatch):
    """Negative anchor: a transient ``None`` (a permission error, an I/O blip on
    a network mount) is "could not tell", not "the root changed" — serving the
    warm entry is the safe reading. Only a CONFIRMED absence evicts."""
    notebook, scale, first = _warm_standalone_viz(repo)
    monkeypatch.setattr(scale.artifacts, "manifest_stat_signature", lambda _d: None)
    _replace_viz_root_out_of_band(repo, notebook.id)

    assert scale.viz_index(notebook.id) is first


def test_daemon_and_viz_failures_clear_build_markers(repo, monkeypatch):
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    monkeypatch.setattr(scale, "_start_daemon", lambda _name, target: target())
    monkeypatch.setattr(
        scale.builder,
        "build",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("build failed")),
    )
    monkeypatch.setattr(scale, "_resolve_mode", lambda *_: "full")
    scale._run_scale_op(notebook.id, "full")
    assert notebook.id not in scale.building

    monkeypatch.setattr(
        scale.builder,
        "build_viz",
        lambda *_: (_ for _ in ()).throw(RuntimeError("viz failed")),
    )
    scale._spawn_viz_build(notebook.id)
    assert notebook.id not in scale.viz_building


def test_runtime_never_holds_state_locks_around_loader_builder_or_notification(
    repo, monkeypatch
):
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    observations = []
    monkeypatch.setattr(scale, "_start_daemon", lambda _name, target: target())
    monkeypatch.setattr(scale, "_resolve_mode", lambda *_: "full")
    monkeypatch.setattr(
        scale.builder,
        "build",
        lambda *_args, **_kwargs: observations.append(
            ("builder", scale.building_lock.locked())
        ) or {"version": []},
    )
    monkeypatch.setattr(
        scale,
        "notify_index_done",
        lambda *_: observations.append(
            ("notification", scale.building_lock.locked())
        ),
    )
    scale._run_scale_op(notebook.id, "full")
    assert observations == [("builder", False), ("notification", False)]


def test_daemon_start_failures_rearm_all_runtime_markers(repo, monkeypatch):
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    monkeypatch.setattr(
        scale,
        "_start_daemon",
        lambda *_: (_ for _ in ()).throw(RuntimeError("thread start failed")),
    )

    with pytest.raises(RuntimeError, match="thread start failed"):
        scale._run_scale_op(notebook.id, "full")
    assert notebook.id not in scale.building

    with pytest.raises(RuntimeError, match="thread start failed"):
        scale._spawn_viz_build(notebook.id)
    assert notebook.id not in scale.viz_building

    with pytest.raises(RuntimeError, match="thread start failed"):
        scale._ensure_scheduler()
    assert scale.scheduler_started is False


def test_manual_now_supersedes_existing_idle_queue_atomically(repo, monkeypatch):
    """A queued auto-build must not reappear after a manual immediate build."""
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    with repo._write() as db:
        db.execute("UPDATE notebooks SET tier='base' WHERE id=?", (notebook.id,))

    launched = []
    monkeypatch.setattr(
        scale,
        "_start_daemon",
        lambda name, target: launched.append((name, target)),
    )

    assert scale.trigger(notebook.id, when="idle", mode="auto")["status"] == "queued"
    assert scale.idle_queue[notebook.id][0] == "auto"
    assert scale.idle_queue[notebook.id][1]  # queued_at stamp is non-empty

    result = scale.trigger(notebook.id, when="now", mode="full")

    assert result["status"] == "building"
    assert notebook.id in scale.building
    assert notebook.id not in scale.idle_queue
    assert scale.status(notebook.id)["state"] == "building"
    assert [name for name, _target in launched].count(f"scaleidx-{notebook.id}") == 1

    # A genuinely newer follow-up remains queued behind the claimed build.
    assert scale.trigger(notebook.id, when="idle", mode="fold")["status"] == "queued"
    assert scale.idle_queue[notebook.id][0] == "fold"
    assert scale.status(notebook.id)["state"] == "building"


def test_post_publication_rebuild_coalesces_busy_full_followup(repo, monkeypatch):
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    with repo._write() as db:
        db.execute("UPDATE notebooks SET tier='base' WHERE id=?", (notebook.id,))

    launched = []
    builds = []
    monkeypatch.setattr(
        scale,
        "_start_daemon",
        lambda name, target: launched.append((name, target)),
    )
    monkeypatch.setattr(scale, "_resolve_mode", lambda *_: "full")
    monkeypatch.setattr(
        scale.builder,
        "build",
        lambda notebook_id, **_kwargs: builds.append(notebook_id) or {},
    )
    monkeypatch.setattr(scale, "notify_index_done", lambda *_: None)

    assert scale.rebuild_after_publication(notebook.id)["status"] == "building"
    assert scale.rebuild_after_publication(notebook.id)["status"] == "queued_followup"
    assert scale.idle_queue[notebook.id][0] == "full"

    scale_targets = [
        target for name, target in launched if name == f"scaleidx-{notebook.id}"
    ]
    assert len(scale_targets) == 1
    scale_targets[0]()

    scale_targets = [
        target for name, target in launched if name == f"scaleidx-{notebook.id}"
    ]
    assert len(scale_targets) == 2
    assert notebook.id not in scale.idle_queue
    scale_targets[1]()
    assert builds == [notebook.id, notebook.id]
    assert notebook.id not in scale.building


def test_requeue_updates_mode_but_keeps_first_queued_at(repo, monkeypatch):
    """重复排队(连续加来源触发 maybe_enqueue_fold 的常态)只更新 mode,保留首次
    入队时刻:dict 对既有 key 赋值不改插入序,位次锚定首次入队,时间戳必须与它
    同锚点,否则「入队序位次 + 刷新的时间戳」自相矛盾(codex R3 P2)。"""
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    with repo._write() as db:
        db.execute("UPDATE notebooks SET tier='base' WHERE id=?", (notebook.id,))
    monkeypatch.setattr(scale, "_ensure_scheduler", lambda: None)

    assert scale.trigger(notebook.id, when="idle", mode="auto")["status"] == "queued"
    first_stamp = scale.idle_queue[notebook.id][1]
    assert first_stamp

    assert scale.trigger(notebook.id, when="idle", mode="fold")["status"] == "queued"
    assert scale.idle_queue[notebook.id] == ("fold", first_stamp)  # mode 新、时刻不变


def test_enqueue_and_cancel_publish_pending_snapshot(repo, monkeypatch):
    """入列/出列都要推待办快照:已连接的铃铛靠 SSE 增量,不推的话「已排队」的
    出现与消失都要等重连或无关快照(codex R5 P2)。not_queued 的取消不推。"""
    import app.services.pending_bus as pending_bus

    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    with repo._write() as db:
        db.execute("UPDATE notebooks SET tier='base' WHERE id=?", (notebook.id,))
    monkeypatch.setattr(scale, "_ensure_scheduler", lambda: None)
    published = []
    monkeypatch.setattr(
        pending_bus, "publish_snapshot", lambda owner: published.append(owner)
    )

    assert scale.trigger(notebook.id, when="idle", mode="auto")["status"] == "queued"
    assert len(published) == 1

    assert scale.cancel(notebook.id)["cancelled"] is True
    assert len(published) == 2

    assert scale.cancel(notebook.id)["cancelled"] is False  # not_queued
    assert len(published) == 2  # 无状态变化不推


def test_queue_position_survives_restore_reorder(repo):
    """worker 启动失败的恢复路径会 pop 后 setdefault 回队列——dict 插入序因此把
    该项挪到末尾,但位次按首次入队时刻排序推导,不随 dict 序漂移(codex R4 P2)。"""
    first = _seed(repo)
    second = _seed(repo)
    scale = repo._runtime.scale_artifacts
    scale.idle_queue[first.id] = ("fold", "2026-01-01T00:00:00.000000+00:00")
    scale.idle_queue[second.id] = ("full", "2026-01-01T00:00:01.000000+00:00")

    # 模拟恢复:first 被 pop 后重插,dict 插入序变成 [second, first]。
    entry = scale.idle_queue.pop(first.id)
    scale.idle_queue.setdefault(first.id, entry)
    assert list(scale.idle_queue) == [second.id, first.id]

    position, length, queued_at = scale._queue_snapshot(first.id)
    assert (position, length) == (1, 2)  # 仍按首次入队时刻排第 1
    assert queued_at == "2026-01-01T00:00:00.000000+00:00"
    assert scale._queue_snapshot(second.id)[0] == 2


def test_manual_now_restores_displaced_idle_request_if_worker_cannot_start(
    repo, monkeypatch
):
    """A thread-launch failure must not silently lose the older safe fallback."""
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    scale.idle_queue[notebook.id] = ("fold", "2026-01-01T00:00:00.000000+00:00")
    monkeypatch.setattr(
        scale,
        "_start_daemon",
        lambda *_: (_ for _ in ()).throw(RuntimeError("thread start failed")),
    )

    with pytest.raises(RuntimeError, match="thread start failed"):
        scale._run_scale_op(notebook.id, "full", supersede_idle=True)

    assert notebook.id not in scale.building
    assert scale.idle_queue[notebook.id] == ("fold", "2026-01-01T00:00:00.000000+00:00")


def test_idle_tick_keeps_follow_up_for_notebook_that_is_still_building(
    repo, monkeypatch
):
    """A scheduler tick must not consume work queued behind an active build."""
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    scale.building.add(notebook.id)
    scale.idle_queue[notebook.id] = ("fold", "2026-01-01T00:00:00.000000+00:00")
    launched = []
    monkeypatch.setattr(
        scale,
        "_start_daemon",
        lambda name, target: launched.append((name, target)),
    )

    scale._process_idle_queue(force=True)

    assert scale.idle_queue[notebook.id] == ("fold", "2026-01-01T00:00:00.000000+00:00")
    assert launched == []


def test_idle_tick_restores_failed_item_and_continues_with_remaining_queue(
    repo, monkeypatch
):
    """One launch failure must neither drop itself nor starve later entries."""
    first = _seed(repo)
    second = _seed(repo)
    scale = repo._runtime.scale_artifacts
    scale.idle_queue[first.id] = ("fold", "2026-01-01T00:00:00.000000+00:00")
    scale.idle_queue[second.id] = ("full", "2026-01-01T00:00:01.000000+00:00")
    launched = []

    def start(name, target):
        if name == f"scaleidx-{first.id}":
            raise RuntimeError("thread start failed")
        launched.append((name, target))

    monkeypatch.setattr(scale, "_start_daemon", start)

    scale._process_idle_queue(force=True)

    assert scale.idle_queue[first.id] == ("fold", "2026-01-01T00:00:00.000000+00:00")
    assert first.id not in scale.building
    assert second.id not in scale.idle_queue
    assert second.id in scale.building
    assert [name for name, _target in launched] == [f"scaleidx-{second.id}"]


# ---------------------------------------------------------------------------
# Z5: process-wide build/fold concurrency ceiling + per-notebook failure backoff.
# ---------------------------------------------------------------------------


def _live_workers() -> int:
    return sum(
        1 for thread in threading.enumerate()
        if thread.name.startswith("scaleidx-") and thread.name != "scaleidx-scheduler"
    )


def test_scale_build_worker_threads_are_bounded_by_the_ceiling(repo, monkeypatch):
    """并发 2、队列 10:**线程数**封顶在 2,拿不到 slot 的 8 个库停在数据里
    (_scale_pending),一条线程都不占。

    此前是「先 spawn 再阻塞等票」:闸只限住了同时**执行**的数量,10 个库照样
    10 条阻塞 daemon —— 低峰一次 drain 大队列,或反复取消+重触发,进程线程/
    内存无界(codex PR#627 R1 P1)。"""
    notebooks = [_seed(repo) for _ in range(10)]
    scale = repo._runtime.scale_artifacts
    for notebook in notebooks:
        _base_tier(repo, notebook)
    scale._scale_build_semaphore = threading.BoundedSemaphore(2)
    monkeypatch.setattr(scale, "_resolve_mode", lambda *_: "full")
    monkeypatch.setattr(scale, "notify_index_done", lambda *_: None)

    spawned: list[str] = []
    real_start = scale._start_daemon

    def counting_start(name, target):
        if name != "scaleidx-scheduler":  # 调度器是常驻兜底,不是构建线程
            spawned.append(name)
        real_start(name, target)

    monkeypatch.setattr(scale, "_start_daemon", counting_start)

    state_lock = threading.Lock()
    built: list[str] = []
    concurrent = 0
    peak_concurrent = 0
    peak_live = 0
    release = threading.Event()

    def fake_build(notebook_id, **_kwargs):
        nonlocal concurrent, peak_concurrent, peak_live
        with state_lock:
            concurrent += 1
            peak_concurrent = max(peak_concurrent, concurrent)
            peak_live = max(peak_live, _live_workers())
        assert release.wait(timeout=10)
        with state_lock:
            concurrent -= 1
            built.append(notebook_id)
        return {}

    monkeypatch.setattr(scale.builder, "build", fake_build)

    started = [scale._run_scale_op(notebook.id, "full") for notebook in notebooks]

    assert started == [True, True] + [False] * 8
    assert len(spawned) == 2                     # 8 个库连线程都没起
    assert len(scale._scale_pending) == 8        # 它们等在数据里
    assert _live_workers() <= 2
    assert set(scale._scale_pending) == {nb.id for nb in notebooks[2:]}

    # 放行后靠 worker 收尾的 handoff 自动接续,全程线程数仍然有界。
    release.set()
    deadline = time.monotonic() + 20
    while len(built) < 10 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(built) == 10
    assert scale._scale_pending == {}
    assert peak_concurrent <= 2
    # 线程数不随**队列长度**增长:上限 2 + 收尾中(已放票、线程尚未退出)的少量
    # 重叠。这里给的是宽松常数而非精确值 —— 收尾重叠取决于机器快慢,而真正锐利
    # 的判据在上面那段(spawned == 2 / pending == 8)。回归形态是 10。
    assert peak_live <= 6
    assert len(spawned) == 10  # 每个库最终恰好起一次

    deadline = time.monotonic() + 5
    while any(nb.id in scale.building for nb in notebooks) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert all(nb.id not in scale.building for nb in notebooks)


def test_automatic_retry_refused_during_backoff_manual_bypasses_it(repo, monkeypatch):
    """Z5②:同一 notebook 构建失败后,自动重跑在退避窗口内被拒绝(admission 直接
    返回 False,不占并发 slot);手动(manual=True,如用户点『立即重建』)不受限,
    始终立即受理。"""
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    with repo._write() as db:
        db.execute("UPDATE notebooks SET tier='base' WHERE id=?", (notebook.id,))
    monkeypatch.setattr(scale, "_start_daemon", lambda _name, target: target())
    monkeypatch.setattr(scale, "_resolve_mode", lambda *_: "full")
    monkeypatch.setattr(
        scale.builder,
        "build",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    # First (automatic) attempt is admitted, runs, and fails — recording backoff.
    assert scale._run_scale_op(notebook.id, "full") is True
    assert notebook.id not in scale.building

    # A second automatic attempt inside the backoff window is refused outright:
    # it never reaches `building`/`_start_daemon` — no wasted concurrency slot.
    assert scale._run_scale_op(notebook.id, "full") is False
    assert notebook.id not in scale.building

    # An explicit manual trigger is exempt from backoff and admitted immediately.
    assert scale._run_scale_op(notebook.id, "full", manual=True) is True


def test_failure_backoff_is_cleared_by_a_subsequent_success(repo, monkeypatch):
    """一次成功清空该 notebook 的失败退避状态,后续自动重跑不再被拒绝。"""
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    with repo._write() as db:
        db.execute("UPDATE notebooks SET tier='base' WHERE id=?", (notebook.id,))
    monkeypatch.setattr(scale, "_start_daemon", lambda _name, target: target())
    monkeypatch.setattr(scale, "_resolve_mode", lambda *_: "full")
    monkeypatch.setattr(scale, "notify_index_done", lambda *_: None)

    monkeypatch.setattr(
        scale.builder,
        "build",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert scale._run_scale_op(notebook.id, "full") is True
    assert scale._scale_backoff_active(notebook.id) is True

    monkeypatch.setattr(scale.builder, "build", lambda *_a, **_k: {})
    assert scale._run_scale_op(notebook.id, "full", manual=True) is True
    assert scale._scale_backoff_active(notebook.id) is False

    # With the failure state cleared, a plain automatic retry is admitted again.
    assert scale._run_scale_op(notebook.id, "full") is True


def test_failure_backoff_delay_doubles_and_is_capped(repo, monkeypatch):
    """指数退避:60s 起步,每次失败翻倍,封顶 scale_build_failure_backoff_max_seconds。"""
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    monkeypatch.setattr(scale.settings, "scale_build_failure_backoff_seconds", 60, raising=False)
    monkeypatch.setattr(
        scale.settings, "scale_build_failure_backoff_max_seconds", 180, raising=False
    )
    before = time.monotonic()

    scale._scale_record_failure(notebook.id)
    _streak, retry_at = scale._scale_failure_state[notebook.id]
    assert 55 <= retry_at - before <= 65  # ~60s

    scale._scale_record_failure(notebook.id)
    before = time.monotonic()
    _streak, retry_at = scale._scale_failure_state[notebook.id]
    assert 115 <= retry_at - before <= 125  # ~120s (doubled)

    scale._scale_record_failure(notebook.id)
    before = time.monotonic()
    _streak, retry_at = scale._scale_failure_state[notebook.id]
    assert 175 <= retry_at - before <= 185  # capped at 180s, not 240s


def _base_tier(repo, notebook):
    with repo._write() as db:
        db.execute("UPDATE notebooks SET tier='base' WHERE id=?", (notebook.id,))


def _fake_clock(scale, monkeypatch, start=1_000.0):
    """Drive the backoff clock without patching the process-wide time module
    (other threads in this file legitimately read the real monotonic clock)."""
    state = {"now": start}
    monkeypatch.setattr(scale, "_monotonic", lambda: state["now"])
    return state


def test_publication_during_backoff_still_queues_and_rebuilds_after_expiry(
    repo, monkeypatch
):
    """P1-1:退避挡的是**执行**,不挡**排队**。

    某库构建失败进入退避窗口(最长 30 分钟),期间用户改分块/管线并发布 ——
    发布尾段的 rebuild_after_publication 是这次新代次**唯一**的持久登记。若在
    退避处直接拒绝,idle 队列里什么都没有,调度器永远不会重建新代次,而 HTTP
    却回了 queued_followup(谎报)。退避过期后调度器必须自然把它接走。
    """
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    _base_tier(repo, notebook)
    clock = _fake_clock(scale, monkeypatch)
    monkeypatch.setattr(scale.settings, "scale_build_failure_backoff_seconds", 60, raising=False)
    monkeypatch.setattr(
        scale.settings, "scale_build_failure_backoff_max_seconds", 1800, raising=False
    )
    monkeypatch.setattr(scale, "_start_daemon", lambda _name, target: target())
    monkeypatch.setattr(scale, "_resolve_mode", lambda *_: "full")
    monkeypatch.setattr(scale, "notify_index_done", lambda *_: None)
    monkeypatch.setattr(scale, "_ensure_scheduler", lambda: None)

    builds: list[str] = []

    def failing_build(notebook_id, **_kwargs):
        builds.append(notebook_id)
        raise RuntimeError("boom")

    monkeypatch.setattr(scale.builder, "build", failing_build)

    # An automatic build fails → the notebook enters its backoff window.
    assert scale._run_scale_op(notebook.id, "full") is True
    assert scale._scale_backoff_active(notebook.id) is True
    assert len(builds) == 1

    # A publication lands inside that window: nothing may start, but the
    # follow-up MUST be registered — and the reported status must match.
    result = scale.rebuild_after_publication(notebook.id)
    assert result["status"] == "queued_followup"
    assert scale.idle_queue[notebook.id][0] == "full"
    assert notebook.id not in scale.building

    # A scheduler tick inside the window must not consume the entry either.
    scale._process_idle_queue(force=True)
    assert scale.idle_queue[notebook.id][0] == "full"
    assert len(builds) == 1

    # Once the window expires the very same queued entry is claimed and built.
    clock["now"] += 61
    monkeypatch.setattr(scale.builder, "build", lambda nbid, **_k: builds.append(nbid) or {})
    scale._process_idle_queue(force=True)
    assert notebook.id not in scale.idle_queue
    assert len(builds) == 2
    assert notebook.id not in scale.building


def test_rebuild_after_publication_status_matches_what_was_registered(
    repo, monkeypatch
):
    """P1-1:三条返回分支逐一与真实登记状态对齐 —— building ⇔ 认领了构建;
    queued_followup ⇔ idle 队列里真有这一条。"""
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    _base_tier(repo, notebook)
    _fake_clock(scale, monkeypatch)  # 步骤 3 的退避窗口必须确定性地「仍然活着」
    monkeypatch.setattr(scale, "_start_daemon", lambda _name, _target: None)
    monkeypatch.setattr(scale, "_ensure_scheduler", lambda: None)

    # 1) Nothing in flight → started, and the claim is real.
    assert scale.rebuild_after_publication(notebook.id) == {
        "status": "building",
        "notebook_id": notebook.id,
    }
    assert notebook.id in scale.building
    assert notebook.id not in scale.idle_queue

    # 2) Busy → queued_followup, and the entry is really there.
    assert scale.rebuild_after_publication(notebook.id)["status"] == "queued_followup"
    assert scale.idle_queue[notebook.id][0] == "full"

    # 3) Backed off → still queued_followup, still a real entry.
    scale.building.discard(notebook.id)
    scale._scale_pending.pop(notebook.id, None)
    scale.idle_queue.pop(notebook.id, None)
    scale._scale_record_failure(notebook.id)
    assert scale._scale_backoff_active(notebook.id) is True
    assert scale.rebuild_after_publication(notebook.id)["status"] == "queued_followup"
    assert scale.idle_queue[notebook.id][0] == "full"

    # 4) Not applicable is unchanged and registers nothing.
    other = repo.create_notebook(NotebookCreate(name="tiny"))
    assert scale.rebuild_after_publication(other.id)["status"] == "not_applicable"
    assert other.id not in scale.idle_queue


def test_expired_failure_state_is_reclaimed_and_bounded(repo, monkeypatch):
    """P2-2:_scale_failure_state 既不能无限长,也不能永不回收。

    过期的一刻**不**丢条目 —— streak 是指数退避的全部依据,一到点就忘会把
    60→120→240 退化成恒定 60s;只有再空过一个封顶窗口(远长于调度器轮询)
    才判定这条记录已经没有意义并回收。"""
    import app.services.scale_artifact_runtime as runtime_module

    first = _seed(repo)
    second = _seed(repo)
    scale = repo._runtime.scale_artifacts
    clock = _fake_clock(scale, monkeypatch)
    monkeypatch.setattr(scale.settings, "scale_build_failure_backoff_seconds", 60, raising=False)
    monkeypatch.setattr(
        scale.settings, "scale_build_failure_backoff_max_seconds", 600, raising=False
    )

    scale._scale_record_failure(first.id)
    assert scale._scale_backoff_active(first.id) is True

    # 刚过期:窗口开了,但记录仍在 —— 下一次失败要在 streak 上继续翻倍。
    clock["now"] += 61
    assert scale._scale_backoff_active(first.id) is False
    assert first.id in scale._scale_failure_state
    scale._scale_record_failure(first.id)
    _streak, retry_at = scale._scale_failure_state[first.id]
    assert retry_at - clock["now"] == 120  # 翻倍,而不是从 60 重来

    # 过期 + 再空过一个封顶窗口 → 回收,内存不随笔记本数单调增长。
    clock["now"] += 120 + 600 + 1
    assert scale._scale_backoff_active(first.id) is False
    assert first.id not in scale._scale_failure_state

    # 有界:超过上限时按「最久未失败」淘汰,被挤掉的条目等价于退避提前结束。
    monkeypatch.setattr(runtime_module, "_SCALE_FAILURE_STATE_MAX", 2)
    scale._scale_failure_state.clear()
    for index in range(4):
        scale._scale_record_failure(f"nb-{index}")
    assert len(scale._scale_failure_state) == 2
    assert set(scale._scale_failure_state) == {"nb-2", "nb-3"}
    assert second.id not in scale._scale_failure_state


def _occupy_all_slots(scale, monkeypatch, repo, capacity=1):
    """Fill the concurrency ceiling with one blocking build and return the
    handle that lets it finish (plus that notebook)."""
    holder = _seed(repo)
    _base_tier(repo, holder)
    scale._scale_build_semaphore = threading.BoundedSemaphore(capacity)
    monkeypatch.setattr(scale, "_resolve_mode", lambda *_: "full")
    monkeypatch.setattr(scale, "notify_index_done", lambda *_: None)
    inside = threading.Event()
    finish = threading.Event()
    built: list[str] = []

    def build(notebook_id, **_kwargs):
        if notebook_id == holder.id:
            inside.set()
            assert finish.wait(timeout=10)
        built.append(notebook_id)
        return {}

    monkeypatch.setattr(scale.builder, "build", build)
    assert scale._run_scale_op(holder.id, "full") is True
    assert inside.wait(timeout=5)
    return holder, finish, built


def test_slot_parked_notebook_reports_queued_and_cancels_without_a_thread(
    repo, monkeypatch
):
    """P2-3:拿不到 slot 的库报「已排队」而不是「构建中」,而且 cancel 就是
    删一条记录 —— 它背后压根没有线程要唤醒/中断。"""
    scale = repo._runtime.scale_artifacts
    holder, finish, built = _occupy_all_slots(scale, monkeypatch, repo)
    notebook = _seed(repo)
    _base_tier(repo, notebook)

    assert scale._run_scale_op(notebook.id, "full") is False
    assert notebook.id in scale._scale_pending
    assert notebook.id not in scale.building          # 未认领:没在写产物
    assert not any(
        thread.name == f"scaleidx-{notebook.id}" for thread in threading.enumerate()
    )

    status = scale.status(notebook.id)
    assert status["state"] == "queued"
    assert status["building"] is False                # 没开跑就不能说「构建中」
    assert (status["queue_position"], status["queue_length"]) == (1, 1)
    assert status["queued_at"]
    # 等的是执行 slot,不是低峰窗口 —— 不许下发「预计今天 02:00 后开始」。
    assert "offpeak_next_start_at" not in status

    cancelled = scale.cancel(notebook.id)
    assert cancelled == {
        "cancelled": True,
        "state": scale.status(notebook.id)["state"],
        "reason": "",
    }
    assert notebook.id not in scale._scale_pending
    assert not any(
        thread.name == f"scaleidx-{notebook.id}" for thread in threading.enumerate()
    )

    # 取消之后重新触发照常受理(仍无 slot → 重新入停车位,依旧零线程)。
    assert scale._run_scale_op(notebook.id, "full") is False
    assert notebook.id in scale._scale_pending
    assert _live_workers() == 1  # 只有占位的那一条

    finish.set()
    deadline = time.monotonic() + 10
    while notebook.id in scale._scale_pending and time.monotonic() < deadline:
        time.sleep(0.01)
    assert built == [holder.id, notebook.id]


def test_completed_build_hands_its_slot_to_the_parked_queue(repo, monkeypatch):
    """P2-3/handoff:admission 不再阻塞线程等票,所以空出来的 slot 必须由收尾的
    worker 主动交棒 —— 否则停车位上的活要等下一次调度器 tick(窗口外则遥遥无期)。"""
    scale = repo._runtime.scale_artifacts
    holder, finish, built = _occupy_all_slots(scale, monkeypatch, repo)
    parked = [_seed(repo) for _ in range(2)]
    for notebook in parked:
        _base_tier(repo, notebook)
        assert scale._run_scale_op(notebook.id, "full") is False
    assert len(scale._scale_pending) == 2

    # 没有任何 tick / 外部请求:只让占位的那次构建结束。
    finish.set()
    deadline = time.monotonic() + 10
    while scale._scale_pending and time.monotonic() < deadline:
        time.sleep(0.01)

    assert scale._scale_pending == {}
    assert sorted(built) == sorted([holder.id] + [nb.id for nb in parked])
    deadline = time.monotonic() + 5
    while _live_workers() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert _live_workers() == 0


# ---------------------------------------------------------------------------
# Task 4 delegation tests: ScaleArtifactRuntime holds ZERO notebook-metadata
# SQL — `self.projections` (== repo._runtime.index_projections) owns the three
# reads it used to run inline (owner / name / unified last-rebuild).  These
# spies pin each fail-open notification / mode-resolution path to its store
# method so the SQL can't be re-inlined.  Primary assertions tagged `# MUT`.
# ---------------------------------------------------------------------------


def _mute_pending_bus(monkeypatch):
    # notify_index_done is fail-open: a prior test can leave the asyncio loop
    # closed so pending_bus.mark_dirty raises and aborts the notify before the
    # projection reads run.  Stub the bus so the delegation path is reached
    # deterministically regardless of suite ordering.
    from app.services.pending_bus import pending_bus

    monkeypatch.setattr(pending_bus, "mark_dirty", lambda *a, **k: None)
    monkeypatch.setattr(pending_bus, "emit", lambda *a, **k: None)


def test_t4deleg_notebook_owner_delegate(repo, monkeypatch):
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    _mute_pending_bus(monkeypatch)
    calls = []

    def spy(notebook_id):
        calls.append(notebook_id)
        return "user-sentinel"

    monkeypatch.setattr(repo._runtime.index_projections, "notebook_owner", spy)
    # notify_index_done -> _resolve_index_owner (no request user in tests) ->
    # projections.notebook_owner.
    scale.notify_index_done(notebook.id)
    assert calls and calls[0] == notebook.id  # MUT


def test_t4deleg_notebook_name_delegate(repo, monkeypatch):
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    _mute_pending_bus(monkeypatch)
    calls = []

    def spy(notebook_id):
        calls.append(notebook_id)
        return "SENTINEL-NAME"

    monkeypatch.setattr(repo._runtime.index_projections, "notebook_name", spy)
    # Real notebook_owner returns a truthy owner, so notify_index_done proceeds
    # to _notebook_name -> projections.notebook_name for the emit payload.
    scale.notify_index_done(notebook.id)
    assert calls and calls[0] == notebook.id  # MUT


def test_t4deleg_unified_last_rebuild_at_delegate(repo, monkeypatch):
    notebook = _seed(repo)
    repo.build_scale_index(notebook.id)
    scale = repo._runtime.scale_artifacts
    calls = []

    def spy(notebook_id):
        calls.append(notebook_id)
        # A last-rebuild strictly newer than the fresh index's built_at forces
        # a full rebuild — proving the delegated value is consumed by the mode.
        return "9999-12-31T23:59:59"

    monkeypatch.setattr(
        repo._runtime.index_projections, "unified_last_rebuild_at", spy
    )
    mode = scale._resolve_mode(notebook.id, "fold")
    assert calls and calls[0] == notebook.id and mode == "full"  # MUT


# ---------------------------------------------------------------------------
# Indexing-queue transparency: offpeak_window_state (pure function) and the
# status() queue_position/queue_length/queued_at/last_build_ms fields it feeds.
# ---------------------------------------------------------------------------


def test_offpeak_window_state_normal_range_in_and_out():
    tz = datetime.timezone(datetime.timedelta(hours=8))
    in_window, next_start = offpeak_window_state(
        datetime.datetime(2026, 8, 11, 3, 30, tzinfo=tz), 2, 6
    )
    assert in_window is True
    assert next_start is None

    in_window, next_start = offpeak_window_state(
        datetime.datetime(2026, 8, 11, 10, 0, tzinfo=tz), 2, 6
    )
    assert in_window is False
    assert next_start == datetime.datetime(2026, 8, 12, 2, 0, tzinfo=tz)


def test_offpeak_window_state_start_hour_inclusive_end_hour_exclusive():
    tz = datetime.timezone.utc
    in_window, next_start = offpeak_window_state(
        datetime.datetime(2026, 8, 11, 2, 0, tzinfo=tz), 2, 6
    )
    assert in_window is True
    assert next_start is None

    # end_hour is exclusive: exactly at the boundary is already out of window,
    # and the next window is tomorrow's start_hour (today's has passed).
    in_window, next_start = offpeak_window_state(
        datetime.datetime(2026, 8, 11, 6, 0, tzinfo=tz), 2, 6
    )
    assert in_window is False
    assert next_start == datetime.datetime(2026, 8, 12, 2, 0, tzinfo=tz)


def test_offpeak_window_state_wraps_midnight():
    tz = datetime.timezone.utc
    in_window, _next_start = offpeak_window_state(
        datetime.datetime(2026, 8, 11, 23, 0, tzinfo=tz), 22, 6
    )
    assert in_window is True
    in_window, _next_start = offpeak_window_state(
        datetime.datetime(2026, 8, 11, 3, 0, tzinfo=tz), 22, 6
    )
    assert in_window is True
    in_window, next_start = offpeak_window_state(
        datetime.datetime(2026, 8, 11, 12, 0, tzinfo=tz), 22, 6
    )
    assert in_window is False
    assert next_start == datetime.datetime(2026, 8, 11, 22, 0, tzinfo=tz)


def test_offpeak_window_state_equal_hours_is_always_out_of_window():
    # start_hour == end_hour is a window that never opens: fail-open to
    # (False, None) rather than promising a next start time that will never
    # actually arrive (there is no meaningful "today's start_hour").
    tz = datetime.timezone.utc
    for hour in (0, 2, 12, 23):
        in_window, next_start = offpeak_window_state(
            datetime.datetime(2026, 8, 11, hour, 0, tzinfo=tz), 4, 4
        )
        assert in_window is False
        assert next_start is None


def test_offpeak_window_state_out_of_range_hours_fail_open():
    # A misconfigured deployment env var must not turn a /scale-index/status
    # poll into a 500 — out-of-range hours fail open just like start==end.
    tz = datetime.timezone.utc
    now = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=tz)
    for start_hour, end_hour in ((24, 6), (2, 24), (-1, 6), (2, -1), (24, -1)):
        in_window, next_start = offpeak_window_state(now, start_hour, end_hour)
        assert in_window is False
        assert next_start is None


def test_status_reports_queue_position_length_and_queued_at(repo, monkeypatch):
    first = _seed(repo)
    second = _seed(repo)
    with repo._write() as db:
        db.execute("UPDATE notebooks SET tier='base' WHERE id IN (?, ?)", (first.id, second.id))
    scale = repo._runtime.scale_artifacts
    monkeypatch.setattr(scale, "_ensure_scheduler", lambda: None)
    monkeypatch.setattr(scale, "_start_daemon", lambda name, target: None)

    scale.trigger(first.id, when="idle", mode="auto")
    scale.trigger(second.id, when="idle", mode="fold")

    first_status = scale.status(first.id)
    assert first_status["state"] == "queued"
    assert first_status["queue_position"] == 1
    assert first_status["queue_length"] == 2
    assert first_status["queued_at"]
    assert isinstance(first_status["offpeak_in_window"], bool)

    second_status = scale.status(second.id)
    assert second_status["state"] == "queued"
    assert second_status["queue_position"] == 2
    assert second_status["queue_length"] == 2

    # when=now 抢占 first(supersede_idle pops it off the queue) — second's
    # position must move up to 1 / queue length shrinks to 1.
    scale.trigger(first.id, when="now", mode="full")
    assert first.id not in scale.idle_queue
    second_status_after = scale.status(second.id)
    assert second_status_after["queue_position"] == 1
    assert second_status_after["queue_length"] == 1


def test_status_queued_offpeak_next_start_at_is_tz_aware_utc(repo, monkeypatch):
    # The frontend renders offpeak_next_start_at in the browser's local
    # timezone: the wire value must be UTC and carry tzinfo, never a naive
    # local timestamp, or every deployment off UTC would render the wrong
    # clock time.
    notebook = _seed(repo)
    with repo._write() as db:
        db.execute("UPDATE notebooks SET tier='base' WHERE id=?", (notebook.id,))
    scale = repo._runtime.scale_artifacts
    monkeypatch.setattr(scale, "_ensure_scheduler", lambda: None)
    monkeypatch.setattr(scale, "_start_daemon", lambda name, target: None)
    # Deterministically out-of-window regardless of wall-clock time at test
    # run: a 1-hour window starting 2 hours from "now" can never contain the
    # current hour.
    now_hour = datetime.datetime.now().astimezone().hour
    start_hour = (now_hour + 2) % 24
    end_hour = (start_hour + 1) % 24
    monkeypatch.setattr(scale.settings, "scale_index_offpeak_start_hour", start_hour)
    monkeypatch.setattr(scale.settings, "scale_index_offpeak_end_hour", end_hour)

    scale.trigger(notebook.id, when="idle", mode="auto")
    status = scale.status(notebook.id)
    assert status["state"] == "queued"
    assert status["offpeak_in_window"] is False

    next_start_at = status["offpeak_next_start_at"]
    assert next_start_at
    parsed = datetime.datetime.fromisoformat(next_start_at)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == datetime.timedelta(0)


def test_status_queued_surfaces_last_build_ms_from_disk_manifest(repo, monkeypatch):
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    manifest = repo.build_scale_index(notebook.id)
    assert manifest["total_build_ms"] >= 0

    monkeypatch.setattr(scale, "_ensure_scheduler", lambda: None)
    scale.idle_queue[notebook.id] = ("fold", "2026-01-01T00:00:00.000000+00:00")

    status = scale.status(notebook.id)
    assert status["state"] == "queued"
    assert status["last_build_ms"] == manifest["total_build_ms"]
    assert status["last_built_at"] == manifest["built_at"]


def test_status_queued_legacy_manifest_without_total_build_ms_defaults_zero(
    repo, monkeypatch
):
    notebook = _seed(repo)
    scale = repo._runtime.scale_artifacts
    repo.build_scale_index(notebook.id)

    # A manifest predating this feature has no total_build_ms key at all.
    monkeypatch.setattr(
        scale.artifacts,
        "read_manifest",
        lambda out_dir: {"built_at": "2020-01-01T00:00:00", "version": []},
    )
    scale.idle_queue[notebook.id] = ("fold", "2026-01-01T00:00:00.000000+00:00")

    status = scale.status(notebook.id)
    assert status["state"] == "queued"
    assert status["last_build_ms"] == 0
    assert status["last_built_at"] == "2020-01-01T00:00:00"


def test_allow_stale_rejects_artifacts_from_another_pipeline_generation():
    """管线切换发布后,旧代 scale 工件不得再经 allow_stale 服务(codex #602 R8 P1):
    chunk id 已重铸,旧 ANN 命中指向已删行——按「无工件」回落 live 检索;legacy
    工件缺 pipeline_identity 键按内建身份放行。"""
    import threading
    from types import SimpleNamespace

    from app.services.scale_artifact_catalog import ScaleArtifactCatalog

    def catalog(manifest, current_identity, load_calls):
        artifacts = SimpleNamespace(
            scale_dir=lambda _nb: "/nonexistent",
            read_manifest=lambda _d: manifest,
            load_scale=lambda _nb: (
                load_calls.append(True) or SimpleNamespace(manifest=manifest)
            ),
        )
        return ScaleArtifactCatalog(
            artifacts=artifacts,
            settings=None,
            version=lambda _nb: ["v-current"],
            scale_cache=lambda: {},
            load_lock=threading.Lock,
            load_locks=lambda: {},
            note_model_error=lambda *a, **k: None,
            pipeline_identity=lambda _nb: current_identity,
        )

    # 不同代:拒绝且连 load_scale 都不发生(闸在冷加载之前)。
    calls: list = []
    stale = catalog(
        {"version": ["v-old"], "pipeline_identity": ["p.x", "v2"]},
        ("", "builtin.chunk.v1"),
        calls,
    )
    assert stale.load("nb", allow_stale=True) is None
    assert calls == []

    # legacy 工件缺键 + 当前内建:照常服务。
    calls = []
    legacy = catalog({"version": ["v-old"]}, ("", "builtin.chunk.v1"), calls)
    assert legacy.load("nb", allow_stale=True) is not None
    assert calls == [True]

    # 同代插件身份:照常服务。
    calls = []
    matched = catalog(
        {"version": ["v-old"], "pipeline_identity": ["p.x", "v2"]},
        ("p.x", "v2"),
        calls,
    )
    assert matched.load("nb", allow_stale=True) is not None


def test_backoff_ceiling_below_base_is_rejected_at_settings_construction() -> None:
    """封顶 < 起步的矛盾配置必须在启动期响亮拒绝(codex #627 R3 P2)。
    两字段各自 ge=1 之外的交叉校验:运行侧 `_scale_record_failure` 用
    `min(..., max(base, cap))` 兜底,矛盾组合会把实际封顶静默抬到 base
    (base=600、max=60 实得恒 600s),与文档宣称的封顶语义相悖。"""
    import pydantic

    from app.core.config import Settings

    with pytest.raises(pydantic.ValidationError, match="SCALE_BUILD_FAILURE_BACKOFF_MAX_SECONDS"):
        Settings(
            database_url="sqlite:///backoff-validator-test.db",
            scale_build_failure_backoff_seconds=600,
            scale_build_failure_backoff_max_seconds=60,
        )
    # 合法组合(含相等)照常通过。
    ok = Settings(
        database_url="sqlite:///backoff-validator-test.db",
        scale_build_failure_backoff_seconds=60,
        scale_build_failure_backoff_max_seconds=60,
    )
    assert ok.scale_build_failure_backoff_max_seconds == 60
