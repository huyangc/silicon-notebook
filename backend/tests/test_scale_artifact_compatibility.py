"""Task 18 — on-disk artifact compatibility through ScaleArtifactStore.

The frozen v9 storage fixtures (kg_index / kg_viz manifest.json + npy/npz)
must keep loading byte-compatibly through the filesystem store — an artifact
written by an earlier deploy is served as-is, no format change. The
temporary/old/live sequence stays atomic for BOTH fold and full rebuild:
prepare never touches the live artifact; only swap replaces it (a save or fold
failure before swap keeps the old index intact).

The load side cross-checks manifest counts against the arrays it just read and
degrades to "no index" (None + a warning) instead of serving a mismatched one —
while a manifest from an older deploy, which simply lacks those count keys,
must keep loading (older-index-stays-valid).
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import numpy as np
import pytest
import scipy.sparse as sp

from app.repositories.filesystem import scale_artifact_store as store_module
from app.repositories.filesystem.scale_artifact_store import ScaleArtifactStore

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "repository_v9" / "storage"


class _StorageSettings:
    """Minimal storage-dir carrier — the store reads only settings.storage_dir."""

    def __init__(self, storage_dir: str) -> None:
        self.storage_dir = storage_dir


@pytest.fixture
def store(tmp_path):
    return ScaleArtifactStore(_StorageSettings(str(tmp_path / "storage")))


def _copy_fixture(store, kind: str, notebook_id: str) -> Path:
    src = FIXTURES / kind / "nb-fixture"
    dst = (store.scale_dir(notebook_id) if kind == "kg_index"
           else store.viz_dir(notebook_id))
    shutil.copytree(src, dst)
    return Path(dst)


def _write_manifest(directory: Path, payload: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(json.dumps(payload))


def _artifacts(node_ids, manifest, **extra) -> dict:
    """The smallest complete save_full payload over `node_ids`."""
    size = len(node_ids)
    payload = dict(
        node_ids=list(node_ids),
        transition=sp.csr_matrix((size, size), dtype=np.float64),
        idf=[1.0] * size,
        chunk_index=[],
        ann_vectors=np.eye(size, 4, dtype=np.float32),
        ann_labels=list(node_ids),
        manifest=manifest,
    )
    payload.update(extra)
    return payload


def _snapshot(directory: Path) -> dict:
    return {
        path.name: path.read_bytes()
        for path in sorted(Path(directory).iterdir())
        if path.is_file()
    }


def test_v9_scale_artifact_loads_and_probes(store):
    dst = _copy_fixture(store, "kg_index", "nb-old")
    expected = json.loads((dst / "manifest.json").read_text())
    assert store.read_manifest(dst) == expected
    assert store.read_manifest_version(dst) == expected["version"]
    idx = store.load_scale("nb-old")
    assert idx is not None
    assert idx.manifest == expected
    assert list(idx.node_ids)
    assert idx.transition.shape[0] == len(idx.node_ids)


def test_indexed_notebook_ids_lists_only_published_live_directories(store):
    _write_manifest(store.scale_dir("nb-b"), {"version": ["v", 1]})
    _write_manifest(store.scale_dir("nb-a"), {"version": ["v", 1]})
    _write_manifest(Path(str(store.scale_dir("nb-a")) + ".tmp"), {"version": []})
    _write_manifest(Path(str(store.scale_dir("nb-b")) + ".old"), {"version": []})
    # P1, codex PR#643 R1: the claim-unique staging shape must be excluded
    # too, not just the legacy fixed ``.tmp`` — a scratch dir can carry a
    # manifest.json right up until the swap.
    _write_manifest(
        Path(str(store.scale_dir("nb-c")) + ".tmp-abc123"), {"version": []}
    )
    (store.scale_dir("no-manifest") / "graph.npz").parent.mkdir(parents=True)

    assert store.indexed_notebook_ids() == ["nb-a", "nb-b"]


def test_v9_viz_artifact_loads(store):
    dst = _copy_fixture(store, "kg_viz", "nb-old")
    expected = json.loads((dst / "manifest.json").read_text())
    viz = store.load_viz("nb-old")
    assert viz is not None
    assert viz.manifest == expected
    assert len(viz.viz_ids) == viz.viz_adj.shape[0]


def test_manifest_probes_fail_soft_and_preserve_raises(store, tmp_path):
    missing = tmp_path / "nowhere"
    assert store.read_manifest(missing) is None
    assert store.read_manifest_version(missing) is None
    corrupt = tmp_path / "corrupt"
    corrupt.mkdir()
    (corrupt / "manifest.json").write_text("{not json")
    assert store.read_manifest_version(corrupt) is None   # cheap probe fail-soft
    with pytest.raises(ValueError):
        store.read_manifest(corrupt)                       # full read keeps raising
    (corrupt / "manifest.json").write_text(json.dumps({"n_nodes": 5}))
    assert store.read_manifest_version(corrupt) is None    # no version field
    assert store.read_manifest(corrupt) == {"n_nodes": 5}


def test_save_full_and_load_scale_roundtrip(store):
    transition = sp.csr_matrix(np.array([[0.0, 1.0], [1.0, 0.0]]))
    manifest = store.save_full("nb-new", dict(
        node_ids=["a", "b"],
        transition=transition,
        idf=[1.0, 0.5],
        chunk_index=[1],
        ann_vectors=np.eye(2, 4, dtype=np.float32),
        ann_labels=["a", "b"],
        manifest={"version": ["v", 1], "dim": 4},
    ))
    assert manifest["version"] == ["v", 1]
    live = store.scale_dir("nb-new")
    for artifact in ("graph.npz", "node_ids.npy", "idf.npy", "chunk_index.npy",
                     "ann.bin", "ann_labels.npy", "manifest.json"):
        assert (live / artifact).exists(), artifact
    idx = store.load_scale("nb-new")
    assert list(idx.node_ids) == ["a", "b"]
    assert idx.manifest["dim"] == 4
    assert store.read_manifest_version(live) == ["v", 1]


def test_save_viz_and_load_viz_roundtrip(store):
    adjacency = sp.csr_matrix(np.array([[0, 1], [1, 0]], dtype=np.int8))
    manifest = store.save_viz("nb-viz", dict(
        viz_ids=["a", "b"],
        viz_adj=adjacency,
        viz_deg=np.asarray([1, 1], dtype=np.int32),
        viz_types=["concept", "claim"],
        viz_names=["A", "B"],
        viz_payload={"edges": [["a", "b", "relates"]]},
        manifest={"version": ["v", 2], "n_viz_nodes": 2, "n_viz_edges": 1},
    ))
    assert manifest["n_viz_nodes"] == 2
    viz = store.load_viz("nb-viz")
    assert list(viz.viz_ids) == ["a", "b"]
    assert list(viz.viz_edges.rows(viz.viz_ids)) == [["a", "b", "relates"]]
    assert viz.manifest["version"] == ["v", 2]


def test_prepare_fold_directory_resets_leftovers(store):
    """P1, codex PR#643 R1: staging is now ``{scale_dir}.tmp-<claim_token>``,
    not a fixed name — ``prepare`` only ever resets ITS OWN token's residue
    (a retry under the SAME token, e.g. after a transient failure). The one
    exception is a pre-existing NO-SUFFIX ``.tmp``, discarded once as a
    one-time compatibility cleanup for staging left over from before this
    change."""
    live = store.scale_dir("nb1")
    _write_manifest(live, {"version": ["v", 1]})
    legacy_leftover = Path(str(live) + ".tmp")
    legacy_leftover.mkdir(parents=True)
    (legacy_leftover / "junk.bin").write_text("stale")

    temporary = store.prepare_fold_directory("nb1", "retry-token")
    assert Path(temporary) == Path(str(live) + ".tmp-retry-token")
    assert Path(temporary).is_dir()
    assert not legacy_leftover.exists()   # legacy no-suffix leftover cleared
    assert store.read_manifest_version(live) == ["v", 1]   # live untouched

    # A retry under the SAME token clears its OWN prior residue too.
    (Path(temporary) / "junk.bin").write_text("stale")
    temporary_again = store.prepare_fold_directory("nb1", "retry-token")
    assert Path(temporary_again) == Path(temporary)
    assert not (Path(temporary_again) / "junk.bin").exists()


def test_swap_fold_directory_replaces_live_and_cleans_up(store):
    live = store.scale_dir("nb2")
    _write_manifest(live, {"version": ["v", 1]})
    (live / "ann.bin").write_text("old")
    temporary = store.prepare_fold_directory("nb2")
    _write_manifest(Path(temporary), {"version": ["v", 2]})
    (Path(temporary) / "ann.bin").write_text("new")
    store.swap_fold_directory("nb2", temporary)
    assert store.read_manifest_version(live) == ["v", 2]
    assert (live / "ann.bin").read_text() == "new"
    assert not Path(temporary).exists()
    assert not Path(str(live) + ".old").exists()


def test_unswapped_temporary_never_touches_live(store):
    live = store.scale_dir("nb3")
    _write_manifest(live, {"version": ["v", 1]})
    before = (live / "manifest.json").stat().st_mtime_ns
    temporary = store.prepare_fold_directory("nb3")
    _write_manifest(Path(temporary), {"version": ["v", 9]})
    # no swap — a fold failure before the swap leaves the old artifact intact
    assert store.read_manifest_version(live) == ["v", 1]
    assert (live / "manifest.json").stat().st_mtime_ns == before


# ── P1, codex PR#643 R1: claim-unique staging closes the zombie/fresh-claim
# corruption race — a fixed shared `{live}.tmp` let a lock-session-lost
# builder and the process that took over its claim reset and write the same
# directory. ──


def test_two_claim_tokens_stage_independent_trees(store):
    """Mutation anchor: revert ``prepare_staging_directory`` to the fixed
    ``{live}.tmp`` path (ignore ``claim_token``) and the second prepare below
    ``rmtree``s the first claim's still-in-flight tree out from under it —
    this goes red."""
    live = store.scale_dir("nb-race")
    _write_manifest(live, {"version": ["v", 1]})

    zombie_temp = store.prepare_fold_directory("nb-race", "zombie-token")
    _write_manifest(zombie_temp, {"version": ["v", "zombie"]})

    fresh_temp = store.prepare_fold_directory("nb-race", "fresh-token")
    (fresh_temp / "marker.txt").write_text("fresh", encoding="utf-8")

    assert zombie_temp != fresh_temp
    # The zombie's in-flight tree must be exactly what it wrote — a shared
    # fixed path would have had it rmtree'd by the fresh prepare above.
    assert store.read_manifest(zombie_temp) == {"version": ["v", "zombie"]}
    assert (fresh_temp / "marker.txt").read_text(encoding="utf-8") == "fresh"

    # Re-preparing the SAME token only clears ITS OWN residue.
    zombie_temp_again = store.prepare_fold_directory("nb-race", "zombie-token")
    assert zombie_temp_again == zombie_temp
    assert store.read_manifest(zombie_temp) is None      # its own leftover cleared
    assert (fresh_temp / "marker.txt").read_text(encoding="utf-8") == "fresh"  # untouched


def test_a_zombie_swap_is_rejected_without_touching_the_new_claimants_tree(
    store,
):
    """The zombie's belated swap — refused by re-verification — must not step
    on the tree the process that took over its claim is building. Mutation
    anchor: same as above (fixed staging path) makes this scenario
    impossible to even set up distinctly, which is itself the bug."""
    live = store.scale_dir("nb-zombie")
    _write_manifest(live, {"version": ["v", 1]})

    zombie_temp = store.prepare_fold_directory("nb-zombie", "zombie-token")
    _write_manifest(zombie_temp, {"version": ["v", "zombie"]})
    fresh_temp = store.prepare_fold_directory("nb-zombie", "fresh-token")
    (fresh_temp / "marker.txt").write_text("fresh", encoding="utf-8")

    with pytest.raises(store_module.ScaleBuildLockLost):
        store.swap_fold_directory(
            "nb-zombie", zombie_temp, verify_held=lambda: False
        )

    assert store.read_manifest_version(live) == ["v", 1]   # nothing published
    assert zombie_temp.is_dir()                              # left for inspection
    assert (fresh_temp / "marker.txt").read_text(encoding="utf-8") == "fresh"


# ── P2, codex PR#643 R1: the source-partition companion re-verifies the
# claim exactly like the main root's swap does — it runs strictly AFTER the
# main root is already published, so a claim lost by then must not let a
# stale companion overwrite whoever else is publishing there. ──


def test_save_source_partitions_honors_verify_held(store):
    """Mutation anchor: drop ``verify_held=verify_held`` from
    ``save_source_partitions``'s swap call and this goes green while a
    lock-lost companion rebuild silently overwrites the live companion."""
    live = store.source_partition_dir("nb-companion")
    _write_manifest(live, {"parent_version": ["v", 1], "published_sources": 1})
    before = (live / "manifest.json").read_bytes()

    with pytest.raises(store_module.ScaleBuildLockLost):
        store.save_source_partitions(
            "nb-companion",
            parent_version=["v", 2],
            source_ids=[],
            load_rows=lambda _source_id: None,
            verify_held=lambda: False,
        )

    assert (live / "manifest.json").read_bytes() == before  # companion untouched


def test_save_source_partitions_publishes_when_the_claim_holds(store):
    live = store.source_partition_dir("nb-companion-ok")
    manifest = store.save_source_partitions(
        "nb-companion-ok",
        parent_version=["v", 2],
        source_ids=[],
        load_rows=lambda _source_id: None,
        verify_held=lambda: True,
    )
    assert manifest["parent_version"] == ["v", 2]
    assert (
        json.loads((live / "manifest.json").read_text())["parent_version"]
        == ["v", 2]
    )


# ── full rebuild is atomic: staging + swap, never an in-place overwrite ──


def test_save_full_failure_leaves_previous_artifact_byte_identical(
    store, monkeypatch
):
    """A rebuild that dies mid-save must not touch the live index.

    This is the whole point of staging: writing straight into the live
    directory used to leave the *previous* manifest.json (written last, so it
    survived) sitting next to half-overwritten arrays — an index that still
    loaded but described data that was no longer there.
    """
    store.save_full("nb-atomic", _artifacts(
        ["a", "b"], {"version": ["v", 1], "dim": 4, "n_nodes": 2, "n_ann": 2}))
    live = store.scale_dir("nb-atomic")
    before = _snapshot(live)
    assert before                                      # v1 really is on disk

    def half_write_then_fail(out_dir, **artifacts):
        target = Path(out_dir)
        target.mkdir(parents=True, exist_ok=True)
        (target / "graph.npz").write_bytes(b"half-written")
        (target / "node_ids.npy").write_bytes(b"half-written")
        raise RuntimeError("injected save failure")

    monkeypatch.setattr(
        store_module.scale_index_module, "save_scale_index", half_write_then_fail)
    # A literal claim_token, standing in for "this operator's retry keeps the
    # same claim" (P1, codex PR#643 R1) — the property this test demonstrates
    # is per-token, not "any subsequent save clears any prior leftover".
    with pytest.raises(RuntimeError, match="injected save failure"):
        store.save_full("nb-atomic", _artifacts(
            ["a", "b", "c"],
            {"version": ["v", 2], "dim": 4, "n_nodes": 3, "n_ann": 3}),
            claim_token="retry-token")

    assert _snapshot(live) == before                   # not one byte moved
    assert store.read_manifest_version(live) == ["v", 1]
    # the abandoned staging directory is inert — loading ignores it entirely
    staged = Path(str(live) + ".tmp-retry-token")
    assert staged.is_dir()
    stale = store.load_scale("nb-atomic")
    assert stale is not None and list(stale.node_ids) == ["a", "b"]
    assert stale.manifest["version"] == ["v", 1]
    # ...and a retry under the SAME claim_token clears its own leftover and
    # publishes normally. A DIFFERENT token would leave it as an orphan for
    # `inspect` to report instead (see test_scale_build_cli.py's leftover
    # coverage) — no token here means "the same build attempt retrying".
    monkeypatch.undo()
    store.save_full("nb-atomic", _artifacts(
        ["a", "b", "c"],
        {"version": ["v", 2], "dim": 4, "n_nodes": 3, "n_ann": 3}),
        claim_token="retry-token")
    assert store.read_manifest_version(live) == ["v", 2]
    assert list(store.load_scale("nb-atomic").node_ids) == ["a", "b", "c"]
    assert not staged.exists()


def test_save_full_first_build_publishes_without_a_live_directory(store):
    """First-ever full rebuild: there is no live directory to set aside, so the
    swap's live → .old step has to be skipped rather than raise. Fold never hits
    this branch (it only runs on an already-indexed notebook)."""
    live = store.scale_dir("nb-first")
    assert not live.exists()
    manifest = store.save_full("nb-first", _artifacts(
        ["a", "b"], {"version": ["v", 1], "dim": 4, "n_nodes": 2, "n_ann": 2}))
    assert manifest["n_nodes"] == 2
    assert live.is_dir()
    for artifact in ("graph.npz", "node_ids.npy", "idf.npy", "chunk_index.npy",
                     "ann.bin", "ann_labels.npy", "manifest.json"):
        assert (live / artifact).exists(), artifact
    assert list(live.parent.glob(f"{live.name}.tmp*")) == []
    assert not Path(str(live) + ".old").exists()
    idx = store.load_scale("nb-first")
    assert list(idx.node_ids) == ["a", "b"]
    assert idx.manifest["version"] == ["v", 1]


# ── load-side integrity: mismatched artifacts degrade to "no index" ──


def test_load_scale_rejects_manifest_node_count_mismatch(store, caplog):
    """manifest says 100 nodes, node_ids.npy has 2 → the artifact is not
    serving anything; degrade to None (a rebuild) and say why in the log."""
    store.save_full("nb-mismatch", _artifacts(
        ["a", "b"], {"version": ["v", 1], "dim": 4, "n_nodes": 2, "n_ann": 2}))
    live = store.scale_dir("nb-mismatch")
    _write_manifest(live, {"version": ["v", 1], "dim": 4,
                           "n_nodes": 100, "n_ann": 2})
    with caplog.at_level(logging.WARNING, logger="app.services.kg.scale_index"):
        assert store.load_scale("nb-mismatch") is None
    message = "\n".join(record.getMessage() for record in caplog.records)
    assert "n_nodes" in message and "100" in message and "2" in message
    assert str(live) in message


def test_load_scale_rejects_cross_array_shape_mismatch(store, caplog):
    """The counts the manifest never carried are still cross-checkable between
    arrays: both write paths build the transition as a len(node_ids)-square
    matrix and idf one entry per node."""
    manifest = {"version": ["v", 1], "dim": 4}   # deliberately no n_nodes/n_ann
    store.save_full("nb-idf", _artifacts(["a", "b"], dict(manifest)))
    np.save(store.scale_dir("nb-idf") / "idf.npy",
            np.asarray([1.0, 1.0, 1.0], dtype=np.float32))
    with caplog.at_level(logging.WARNING, logger="app.services.kg.scale_index"):
        assert store.load_scale("nb-idf") is None
    assert "idf.npy" in "\n".join(r.getMessage() for r in caplog.records)

    store.save_full("nb-graph", _artifacts(["a", "b"], dict(manifest)))
    sp.save_npz(store.scale_dir("nb-graph") / "graph.npz",
                sp.csr_matrix((3, 3), dtype=np.float64))
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.services.kg.scale_index"):
        assert store.load_scale("nb-graph") is None
    assert "graph.npz" in "\n".join(r.getMessage() for r in caplog.records)


def test_load_scale_missing_core_artifact_returns_none(store, caplog):
    """A manifest with no graph.npz next to it must not raise into the
    retrieval path — None means "no index", which triggers a rebuild."""
    store.save_full("nb-gone", _artifacts(
        ["a", "b"], {"version": ["v", 1], "dim": 4, "n_nodes": 2, "n_ann": 2}))
    (store.scale_dir("nb-gone") / "graph.npz").unlink()
    with caplog.at_level(logging.WARNING, logger="app.services.kg.scale_index"):
        assert store.load_scale("nb-gone") is None
    assert caplog.records                                # and it said so


def test_load_scale_older_manifest_without_counts_still_loads(store, caplog):
    """older-index-stays-valid: a manifest from a deploy that never wrote
    n_ann / has_viz / n_chunk_ann must load exactly as before. Missing key →
    that check is skipped, never "corrupt"."""
    frozen = _copy_fixture(store, "kg_index", "nb-old")
    manifest = json.loads((frozen / "manifest.json").read_text())
    assert "n_ann" not in manifest and "has_viz" not in manifest
    with caplog.at_level(logging.WARNING, logger="app.services.kg.scale_index"):
        idx = store.load_scale("nb-old")
    assert idx is not None and list(idx.node_ids)
    assert idx.manifest == manifest

    # same for a manifest carrying no counts at all
    store.save_full("nb-bare", _artifacts(["a", "b"], {"version": ["v", 1],
                                                       "dim": 4}))
    bare = store.load_scale("nb-bare")
    assert bare is not None and list(bare.node_ids) == ["a", "b"]
    assert [r.getMessage() for r in caplog.records] == []


def test_load_scale_optional_artifact_counts_are_checked_when_present(store):
    """The optional ANN counts come from the same write as the main index, so a
    mismatch proves that write was incomplete → corrupt. A *missing* optional
    file keeps its long-standing meaning instead ("serve without that ANN"),
    which is what older/degraded indexes rely on."""
    store.save_full("nb-chunk", _artifacts(
        ["a", "b"], {"version": ["v", 1], "dim": 4, "n_nodes": 2, "n_ann": 2},
        chunk_ann_vectors=np.eye(2, 4, dtype=np.float32),
        chunk_ann_labels=["c1", "c2"]))
    live = store.scale_dir("nb-chunk")
    assert store.read_manifest(live)["n_chunk_ann"] == 2
    assert store.load_scale("nb-chunk").chunk_ann_labels == ["c1", "c2"]

    manifest = store.read_manifest(live)
    _write_manifest(live, {**manifest, "n_chunk_ann": 99})
    assert store.load_scale("nb-chunk") is None

    _write_manifest(live, manifest)
    (live / "chunk_ann_labels.npy").unlink()
    degraded = store.load_scale("nb-chunk")
    assert degraded is not None                  # not escalated to "corrupt"
    assert degraded.chunk_ann_labels is None


def test_chunk_ann_source_sidecar_is_optional_but_strict_when_declared(store):
    store.save_full("nb-scoped", _artifacts(
        ["a", "b"],
        {"version": ["v", 1], "dim": 4, "n_nodes": 2, "n_ann": 2},
        chunk_ann_vectors=np.eye(2, 4, dtype=np.float32),
        chunk_ann_labels=["c1", "c2"],
        chunk_ann_source_names=["s1", "s2"],
        chunk_ann_source_codes=np.asarray([0, 1], dtype=np.int32),
        chunk_ann_source_counts=np.asarray([1, 1], dtype=np.int64),
    ))
    live = store.scale_dir("nb-scoped")
    loaded = store.load_scale("nb-scoped")
    assert loaded.chunk_ann_source_names == ["s1", "s2"]
    assert loaded.chunk_ann_source_codes.tolist() == [0, 1]

    np.save(
        live / "chunk_ann_source_counts.npy",
        np.asarray([2, 0], dtype=np.int64),
    )
    assert store.load_scale("nb-scoped") is None


@pytest.mark.parametrize(
    ("filename", "bad_value"),
    [
        (
            "chunk_ann_source_names.npy",
            np.asarray([["s1", "s2"]], dtype=object),
        ),
        (
            "chunk_ann_source_names.npy",
            np.asarray(["s1", "s1"], dtype=object),
        ),
        (
            "chunk_ann_source_codes.npy",
            np.asarray([0.0, 1.0], dtype=np.float64),
        ),
        (
            "chunk_ann_source_codes.npy",
            np.asarray([[0, 1]], dtype=np.int32),
        ),
        (
            "chunk_ann_source_counts.npy",
            np.asarray([1.0, 1.0], dtype=np.float64),
        ),
    ],
)
def test_chunk_ann_source_sidecar_rejects_bad_dtype_or_shape(
    store, filename, bad_value
):
    store.save_full("nb-malformed", _artifacts(
        ["a", "b"],
        {"version": ["v", 1], "dim": 4, "n_nodes": 2, "n_ann": 2},
        chunk_ann_vectors=np.eye(2, 4, dtype=np.float32),
        chunk_ann_labels=["c1", "c2"],
        chunk_ann_source_names=["s1", "s2"],
        chunk_ann_source_codes=np.asarray([0, 1], dtype=np.int32),
        chunk_ann_source_counts=np.asarray([1, 1], dtype=np.int64),
    ))
    live = store.scale_dir("nb-malformed")
    np.save(live / filename, bad_value)

    assert store.load_scale("nb-malformed") is None
