"""W-CLI T-W2 — the offline scale-build CLI's refusals, atomicity and guards.

Everything here runs without a database: the package validation is a pure
function over a directory, and the publish path is exercised against the real
``ScaleArtifactStore`` with a stand-in repository. The live-database properties
(the migration ledger, the ``migrate=False``/``seed=False`` seam and the
end-to-end build→export→import→inspect smoke) are in
``tests/postgres/test_scale_build_cli.py`` — they cannot be faked.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
from pathlib import Path

import numpy as np
import pytest

from app.repositories.filesystem.scale_artifact_store import (
    ScaleArtifactStore,
    SwapInterruptGuard,
)
from app.repositories.scale_build_lock import (
    SCALE_BUILD_LOCK_UNAVAILABLE,
    ScaleBuildLockLost,
)
from app.services import scale_build_cli as cli

FIXTURES = (
    Path(__file__).resolve().parent / "fixtures" / "repository_v9" / "storage"
)
PIPELINE = ["", "builtin-1"]
LIBRARIES = {"hnswlib": "0.8.0", "numpy": "1.26.4", "scipy": "1.13.0"}


# ─────────────────────────────────────────────────────────────── fixtures ──

class _Settings:
    def __init__(self, storage_dir: str, *, embed_dim: int = 4) -> None:
        self.storage_dir = storage_dir
        self.embed_dim = embed_dim
        self.embed_runtime_dim = 0


class _Lock:
    supported = True

    def __init__(self, *, held: bool = True) -> None:
        self._held = held
        self.released = False
        self.claim_token = secrets.token_hex(8)

    def verify_held(self) -> bool:
        return self._held

    def release(self) -> None:
        self.released = True


class _Database:
    def __init__(self, handle) -> None:
        self.handle = handle
        self.claims: list[str] = []

    def try_scale_build_lock(self, notebook_id: str):
        self.claims.append(notebook_id)
        return self.handle


class _Projections:
    def __init__(self, identity, *, sources=("s-1", "s-2"), tier="base") -> None:
        self._identity = identity
        self._sources = list(sources)
        self._tier = tier
        self.source_id_reads = 0

    def pipeline_identity(self, _notebook_id: str):
        return self._identity

    def notebook_tier(self, _notebook_id: str):
        return self._tier

    def source_ids(self, _notebook_id: str):
        self.source_id_reads += 1
        return list(self._sources)


class _Runtime:
    def __init__(self, store, database, projections) -> None:
        self.scale_artifact_store = store
        self.database = database
        self.index_projections = projections


class _Repository:
    """The narrow surface the CLI actually reaches through."""

    def __init__(self, settings, store, database, projections) -> None:
        self.settings = settings
        self._runtime = _Runtime(store, database, projections)


@pytest.fixture
def storage(tmp_path) -> _Settings:
    return _Settings(str(tmp_path / "storage"))


@pytest.fixture
def store(storage) -> ScaleArtifactStore:
    return ScaleArtifactStore(storage)


@pytest.fixture
def lock() -> _Lock:
    return _Lock()


@pytest.fixture
def repository(storage, store, lock) -> _Repository:
    return _Repository(storage, store, _Database(lock), _Projections(PIPELINE))


def _write_manifest(directory: Path, payload: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _main_manifest(**overrides) -> dict:
    manifest = {
        "version": ["nb-1", 7],
        "notebook_id": "nb-1",
        "watermark_sources": ["s-1"],
        "pipeline_identity": list(PIPELINE),
        "dim": 4,
        "n_nodes": 2,
        "n_chunks": 2,
        "built_at": "2024-01-02T03:04:05",
        cli.MANIFEST_LIBRARY_KEY: dict(LIBRARIES),
    }
    manifest.update(overrides)
    return manifest


def _package(tmp_path, *, name: str = "package", companion=None, **overrides) -> Path:
    """A three-root export package built from the frozen v9 artifact."""
    package = tmp_path / name
    main = package / cli.MAIN_ROOT
    shutil.copytree(FIXTURES / "kg_index" / "nb-fixture", main)
    _write_manifest(main, _main_manifest(**overrides))
    if companion is not None:
        _write_manifest(package / cli.COMPANION_ROOT, companion)
    return package


def _validate(package: Path, **kwargs):
    arguments = {
        "expected_notebook_id": "nb-1",
        "known_source_ids": lambda: ["s-1", "s-2"],
        "expected_pipeline_identity": PIPELINE,
        "runtime_dim": 4,
        "runtime_libraries": dict(LIBRARIES),
    }
    arguments.update(kwargs)
    return cli.validate_import_package(package, **arguments)


# ──────────────────────────────────────────────────── package validation ──

def test_a_matching_package_validates_with_no_warnings(tmp_path):
    manifest, warnings = _validate(_package(tmp_path))
    assert manifest["dim"] == 4
    assert warnings == []


def test_a_foreign_pipeline_identity_is_refused(tmp_path):
    """Mutation anchor: the retrieval side discards a scale core built by a
    different pipeline WITHOUT any error, so this must refuse, not warn."""
    package = _package(tmp_path, pipeline_identity=["acme-pipeline", "3"])
    with pytest.raises(cli.ScaleBuildCliError, match="pipeline identity"):
        _validate(package)


def test_a_legacy_package_without_pipeline_identity_reads_as_builtin(tmp_path):
    package = _package(tmp_path)
    manifest = _main_manifest()
    manifest.pop("pipeline_identity")
    _write_manifest(package / cli.MAIN_ROOT, manifest)
    from app.domain.indexing_pipeline import BUILTIN_INDEXING_PIPELINE_VERSION

    _validate(
        package,
        expected_pipeline_identity=["", BUILTIN_INDEXING_PIPELINE_VERSION],
    )


def test_a_different_embedding_dimension_is_refused(tmp_path):
    """A dim mismatch makes open_ann fail open — zero recall, no error."""
    with pytest.raises(cli.ScaleBuildCliError, match="dimension mismatch"):
        _validate(_package(tmp_path, dim=1024))


def test_a_package_without_a_dim_is_refused(tmp_path):
    package = _package(tmp_path)
    manifest = _main_manifest()
    manifest.pop("dim")
    _write_manifest(package / cli.MAIN_ROOT, manifest)
    with pytest.raises(cli.ScaleBuildCliError, match="no usable dim"):
        _validate(package)


def test_an_hnswlib_mismatch_is_refused_by_default_and_overridable(tmp_path):
    package = _package(tmp_path)
    other = dict(LIBRARIES, hnswlib="0.7.0")
    with pytest.raises(cli.ScaleBuildCliError, match="hnswlib version mismatch"):
        _validate(package, runtime_libraries=other)

    _manifest, warnings = _validate(
        package, runtime_libraries=other, allow_library_mismatch=True
    )
    assert any("hnswlib" in warning for warning in warnings)


def test_an_unrecorded_hnswlib_version_counts_as_a_mismatch(tmp_path):
    """``ann.bin`` has no format version header: "unknown" cannot be proven
    equal, and the failure mode is silent zero recall."""
    package = _package(tmp_path)
    manifest = _main_manifest()
    manifest.pop(cli.MANIFEST_LIBRARY_KEY)
    _write_manifest(package / cli.MAIN_ROOT, manifest)
    with pytest.raises(cli.ScaleBuildCliError, match="hnswlib version mismatch"):
        _validate(package)


def test_numpy_and_scipy_differences_only_warn(tmp_path):
    package = _package(tmp_path)
    _manifest, warnings = _validate(
        package, runtime_libraries=dict(LIBRARIES, numpy="2.0.0", scipy="1.14.0")
    )
    assert len(warnings) == 2
    assert all("hnswlib" not in warning for warning in warnings)


def test_a_missing_core_file_is_refused(tmp_path):
    package = _package(tmp_path)
    (package / cli.MAIN_ROOT / "idf.npy").unlink()
    with pytest.raises(cli.ScaleBuildCliError, match="idf.npy"):
        _validate(package)


def test_a_directory_masquerading_as_a_required_artifact_is_refused(tmp_path):
    """A corrupt package with a directory named ``idf.npy`` passes ``.exists()``
    but the later header probe can only ever return ``None`` for it — publish
    must not accept that as "present" (codex PR#643 R2 P2)."""
    package = _package(tmp_path)
    idf = package / cli.MAIN_ROOT / "idf.npy"
    idf.unlink()
    idf.mkdir()
    with pytest.raises(cli.ScaleBuildCliError, match="idf.npy"):
        _validate(package)


def test_a_manifest_count_that_disagrees_with_the_arrays_is_refused(tmp_path):
    package = _package(tmp_path, n_nodes=99)
    with pytest.raises(cli.ScaleBuildCliError, match="n_nodes=99"):
        _validate(package)


def test_a_flagged_optional_artifact_must_be_present(tmp_path):
    package = _package(tmp_path, has_relation_ann=True, n_relation_ann=3)
    with pytest.raises(cli.ScaleBuildCliError, match="relation_ann"):
        _validate(package)


def test_a_companion_from_another_generation_is_refused(tmp_path):
    package = _package(
        tmp_path, companion={"parent_version": ["nb-1", 6], "published_sources": 1}
    )
    with pytest.raises(cli.ScaleBuildCliError, match="different generation"):
        _validate(package)


def test_a_companion_matching_the_main_version_passes(tmp_path):
    package = _package(
        tmp_path, companion={"parent_version": ["nb-1", 7], "published_sources": 1}
    )
    _validate(package)


def test_an_absent_companion_is_not_a_refusal(tmp_path):
    """The switch that produces companions is off in many deployments; "one
    side missing" is the normal shape, not an error."""
    package = _package(tmp_path)
    assert not (package / cli.COMPANION_ROOT).exists()
    _validate(package)


def test_a_companion_root_with_no_manifest_is_refused(tmp_path):
    """codex PR#643 R4 P2: a PRESENT ``kg_index_partitions`` directory with no
    ``manifest.json`` is not "no companion" (that is an absent root, still
    unconditionally allowed above) — it is an incomplete package. Skipping the
    generation check here would publish it straight over a healthy live
    companion and leave the companion unreadable until the next rebuild."""
    package = _package(tmp_path)
    (package / cli.COMPANION_ROOT).mkdir()
    assert not (package / cli.COMPANION_ROOT / "manifest.json").exists()
    with pytest.raises(cli.ScaleBuildCliError, match="no manifest.json"):
        _validate(package)


def test_a_directory_that_is_not_an_export_is_refused(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(cli.ScaleBuildCliError, match="does not look like an export"):
        _validate(empty)


def test_npy_row_count_reads_the_header_without_unpickling(tmp_path):
    """Object arrays would need ``allow_pickle`` to materialize — an arbitrary
    code execution surface the validation must not touch."""
    path = tmp_path / "labels.npy"
    np.save(path, np.asarray(["a", "b", "c"], dtype=object))
    assert cli.npy_row_count(path) == 3
    with pytest.raises(ValueError):
        np.load(path, allow_pickle=False)


# ────────────────────────────────────────────────────────── import publish ──

def _staging_glob(root: Path) -> list[Path]:
    """Every staging sibling ``prepare_staging_directory`` could have left
    for this root — the legacy fixed ``{root}.tmp`` and any claim-unique
    ``{root}.tmp-<token>`` (P1, codex PR#643 R1). Staging is no longer a
    single guessable name, so "leaves no staging" assertions glob instead of
    checking one literal path."""
    matches = sorted(root.parent.glob(f"{root.name}.tmp-*"))
    legacy = root.parent / f"{root.name}.tmp"
    if legacy.exists():
        matches.insert(0, legacy)
    return matches


def _published_version(store, notebook_id: str):
    manifest = json.loads(
        (store.scale_dir(notebook_id) / "manifest.json").read_text(encoding="utf-8")
    )
    return manifest.get("version")


def _seed_live(store, notebook_id: str) -> None:
    """A previous generation on disk, in all three roots."""
    shutil.copytree(
        FIXTURES / "kg_index" / "nb-fixture", store.scale_dir(notebook_id)
    )
    _write_manifest(
        Path(store.scale_dir(notebook_id)), _main_manifest(version=["nb-1", 1])
    )
    _write_manifest(Path(store.viz_dir(notebook_id)), {"generation": "old"})
    _write_manifest(
        Path(store.source_partition_dir(notebook_id)),
        {"parent_version": ["nb-1", 1], "published_sources": 1},
    )


def _full_package(tmp_path) -> Path:
    package = _package(
        tmp_path, companion={"parent_version": ["nb-1", 7], "published_sources": 2}
    )
    _write_manifest(package / "kg_viz", {"generation": "new"})
    return package


def test_import_publishes_every_root_and_leaves_no_staging(
    repository, store, tmp_path
):
    _seed_live(store, "nb-1")
    receipt = cli.run_import(
        repository,
        "nb-1",
        _full_package(tmp_path),
        allow_library_mismatch=False,
        report=lambda _message: None,
    )

    assert receipt["roots"] == list(cli.PUBLISH_ORDER)
    assert _published_version(store, "nb-1") == ["nb-1", 7]
    assert json.loads(
        (Path(store.viz_dir("nb-1")) / "manifest.json").read_text()
    ) == {"generation": "new"}
    for root in cli.artifact_roots(store, "nb-1").values():
        assert _staging_glob(root) == []
        assert not Path(str(root) + ".old").exists()


def test_import_publishes_the_main_root_last(repository, store, tmp_path):
    """Publication order is load-bearing: an interruption between renames must
    leave the *live* index on its previous generation, and the companion's
    parent-version gate makes the intermediate state unreadable, not wrong."""
    _seed_live(store, "nb-1")
    order: list[str] = []
    original = ScaleArtifactStore.swap_staging_directory

    def spy(live, temporary, **kwargs):
        order.append(Path(str(live)).parent.name)
        return original(live, temporary, **kwargs)

    store.swap_staging_directory = spy  # type: ignore[method-assign]
    cli.run_import(
        repository,
        "nb-1",
        _full_package(tmp_path),
        allow_library_mismatch=False,
        report=lambda _message: None,
    )
    assert order == list(cli.PUBLISH_ORDER)
    assert order[-1] == cli.MAIN_ROOT


def test_a_staging_failure_never_touches_the_live_tree(
    repository, store, tmp_path, monkeypatch
):
    """Copying is the long, failure-prone half. It happens entirely in .tmp."""
    _seed_live(store, "nb-1")
    real_copytree = shutil.copytree
    calls: list[int] = []

    def flaky(source, destination, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise OSError("no space left on device")
        return real_copytree(source, destination, **kwargs)

    monkeypatch.setattr(cli.shutil, "copytree", flaky)
    with pytest.raises(OSError):
        cli.run_import(
            repository,
            "nb-1",
            _full_package(tmp_path),
            allow_library_mismatch=False,
            report=lambda _message: None,
        )

    assert _published_version(store, "nb-1") == ["nb-1", 1]
    for root in cli.artifact_roots(store, "nb-1").values():
        assert _staging_glob(root) == [], "half a copy must not linger"


def test_a_failed_swap_restores_the_previous_artifact(
    repository, store, tmp_path, monkeypatch
):
    _seed_live(store, "nb-1")
    real_rename = os.rename
    renames: list[int] = []

    def flaky(source, destination):
        renames.append(1)
        # The first rename of the first root sets the live directory aside; the
        # second one publishes. Failing there is what the rollback exists for.
        if len(renames) == 2:
            raise OSError("publish failed")
        return real_rename(source, destination)

    monkeypatch.setattr(
        "app.repositories.filesystem.scale_artifact_store.os.rename", flaky
    )
    with pytest.raises(OSError):
        cli.run_import(
            repository,
            "nb-1",
            _full_package(tmp_path),
            allow_library_mismatch=False,
            report=lambda _message: None,
        )

    companion = Path(store.source_partition_dir("nb-1"))
    assert json.loads((companion / "manifest.json").read_text())[
        "parent_version"
    ] == ["nb-1", 1]
    assert _published_version(store, "nb-1") == ["nb-1", 1]


def test_import_refuses_to_publish_when_the_claim_was_lost(
    storage, store, tmp_path
):
    """Mutation anchor: drop ``verify_held=`` from the swap call and a build
    whose lock session died republishes over whoever owns the directory now."""
    _seed_live(store, "nb-1")
    lost = _Lock(held=False)
    repository = _Repository(
        storage, store, _Database(lost), _Projections(PIPELINE)
    )
    with pytest.raises(cli.ScaleBuildCliFailure, match="lock was lost"):
        cli.run_import(
            repository,
            "nb-1",
            _full_package(tmp_path),
            allow_library_mismatch=False,
            report=lambda _message: None,
        )

    assert _published_version(store, "nb-1") == ["nb-1", 1]
    staged = _staging_glob(store.source_partition_dir("nb-1"))
    assert len(staged) == 1, "the staged copy is left for the operator"
    assert lost.released is True


def test_import_refuses_to_publish_when_pipeline_identity_drifts_during_staging(
    storage, store, tmp_path
):
    """codex PR#643 R5 P1: the import claim (T-W1's advisory lock) does not
    block a pipeline switch (``execute_indexing_pipeline_rebuild``) from
    publishing a NEW live identity while this package's multi-GB roots are
    being staged — a plugin activation is a different mechanism entirely.
    A package validated against the identity that was live a MOMENT AGO must
    not be published once the identity has moved on: the retrieval side's own
    pipeline gate (``scale_artifact_catalog``) would silently discard it.

    Mutation anchor: removing the re-check between "staging done" and "first
    rename" makes this pass with the drifted package published anyway.
    """
    _seed_live(store, "nb-1")
    calls = {"n": 0}
    projections = _Projections(PIPELINE)
    read_at_validation_time = projections.pipeline_identity

    def drifting(notebook_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return read_at_validation_time(notebook_id)
        # A pipeline switch completed while staging (the slow part) ran.
        return ["acme-pipeline", "3"]

    projections.pipeline_identity = drifting  # type: ignore[method-assign]
    repository = _Repository(storage, store, _Database(_Lock()), projections)

    with pytest.raises(cli.ScaleBuildCliFailure, match="pipeline identity"):
        cli.run_import(
            repository,
            "nb-1",
            _full_package(tmp_path),
            allow_library_mismatch=False,
            report=lambda _message: None,
        )

    assert calls["n"] >= 2, "the identity must be re-read after staging"
    assert _published_version(store, "nb-1") == ["nb-1", 1], (
        "the drifted package must never reach the live tree"
    )
    staged = _staging_glob(store.source_partition_dir("nb-1"))
    assert len(staged) == 1, "the staged copy is left for the operator"


def test_a_held_claim_makes_every_command_refuse(storage, store, tmp_path):
    repository = _Repository(
        storage, store, _Database(None), _Projections(PIPELINE)
    )
    with pytest.raises(cli.ScaleBuildCliFailure, match="held by another process"):
        cli.run_import(
            repository,
            "nb-1",
            _full_package(tmp_path),
            allow_library_mismatch=False,
            report=lambda _message: None,
        )
    assert _staging_glob(store.scale_dir("nb-1")) == []


def test_a_refused_package_never_reaches_the_disk(repository, store, tmp_path):
    _seed_live(store, "nb-1")
    package = _package(tmp_path, dim=1024)
    with pytest.raises(cli.ScaleBuildCliError):
        cli.run_import(
            repository,
            "nb-1",
            package,
            allow_library_mismatch=False,
            report=lambda _message: None,
        )
    assert _published_version(store, "nb-1") == ["nb-1", 1]
    assert _staging_glob(store.scale_dir("nb-1")) == []


def test_a_deferred_interrupt_that_published_everything_is_not_a_failure(
    repository, store, tmp_path
):
    """codex W-CLI R1 P2-6. Ctrl-C during the renames is deferred; if every
    root then lands, nothing was abandoned. Re-raising there printed
    "interrupted" and returned 130 for a publish that fully succeeded."""
    _seed_live(store, "nb-1")
    original = ScaleArtifactStore.swap_staging_directory
    delivered: list[int] = []

    def swap_and_signal(live, temporary, **kwargs):
        original(live, temporary, **kwargs)
        if not delivered:
            delivered.append(1)
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)

    store.swap_staging_directory = swap_and_signal  # type: ignore[method-assign]
    messages: list[str] = []
    receipt = cli.run_import(
        repository,
        "nb-1",
        _full_package(tmp_path),
        allow_library_mismatch=False,
        report=messages.append,
    )

    assert receipt["roots"] == list(cli.PUBLISH_ORDER)
    assert _published_version(store, "nb-1") == ["nb-1", 7]
    assert any("deferred interrupt" in message for message in messages)


# ───────────────────────────────────────────────── which library is this ──

def test_a_package_built_for_another_notebook_is_refused(tmp_path):
    """codex W-CLI R1 P1-2. Pipeline identity, dim and hnswlib are
    deployment-wide facts — identical for every library on the host — so a
    mistyped ``--notebook`` walked past all of them and published library A's
    index into library B. Mutation anchor: drop the notebook binding and this
    goes green while the wrong library starts serving."""
    package = _package(tmp_path, notebook_id="nb-other")
    with pytest.raises(cli.ScaleBuildCliError, match="belongs to notebook"):
        _validate(package)


def test_a_legacy_package_is_bound_by_its_watermark_sources(tmp_path):
    """Artifacts built before the manifest carried ``notebook_id`` are still
    bound: the sources they were built over must be sources this notebook
    has."""
    manifest = _main_manifest()
    manifest.pop("notebook_id")
    package = _package(tmp_path)
    _write_manifest(package / cli.MAIN_ROOT, manifest)
    _validate(package)  # {"s-1"} ⊆ {"s-1", "s-2"}

    manifest["watermark_sources"] = ["s-1", "s-from-another-library"]
    _write_manifest(package / cli.MAIN_ROOT, manifest)
    with pytest.raises(cli.ScaleBuildCliError, match="does not have"):
        _validate(package)


def test_a_package_with_no_binding_at_all_is_refused(tmp_path):
    """Fail closed: every artifact this codebase writes carries a watermark,
    so "neither key" is an artifact this tool cannot vouch for."""
    manifest = _main_manifest()
    manifest.pop("notebook_id")
    manifest.pop("watermark_sources")
    package = _package(tmp_path)
    _write_manifest(package / cli.MAIN_ROOT, manifest)
    with pytest.raises(cli.ScaleBuildCliError, match="neither notebook_id"):
        _validate(package)


def test_the_watermark_is_only_read_when_the_manifest_has_no_notebook_id(
    repository, store, tmp_path
):
    """A 48k-source library makes that a full column read; the strong binding
    must not pay for it."""
    _seed_live(store, "nb-1")
    projections = repository._runtime.index_projections
    cli.run_import(
        repository,
        "nb-1",
        _full_package(tmp_path),
        allow_library_mismatch=False,
        report=lambda _message: None,
    )
    assert projections.source_id_reads == 0


def test_an_unknown_notebook_is_refused_before_anything_is_staged(
    storage, store, tmp_path, lock
):
    """``pipeline_identity`` answers with the builtin default for a notebook
    with no state row instead of raising, so the identity gate cannot catch a
    typo; the tier read can."""
    repository = _Repository(
        storage, store, _Database(lock), _Projections(PIPELINE, tier=None)
    )
    with pytest.raises(cli.ScaleBuildCliError, match="unknown notebook"):
        cli.run_import(
            repository,
            "nb-1",
            _full_package(tmp_path),
            allow_library_mismatch=False,
            report=lambda _message: None,
        )
    assert _staging_glob(store.scale_dir("nb-1")) == []


@pytest.mark.parametrize(
    "notebook_id", ["../../etc", "nb/../..", "", ".", "a/b"]
)
def test_a_notebook_id_that_escapes_the_storage_root_is_refused(
    store, notebook_id
):
    """codex W-CLI R1 P2-5: the id becomes the last path segment of three
    directories this CLI creates, renames and ``rmtree``s."""
    with pytest.raises(cli.ScaleBuildCliError):
        cli.artifact_roots(store, notebook_id)


def test_a_traversing_notebook_id_never_reaches_the_staging_rmtree(
    repository, store, tmp_path
):
    # {storage}/kg_index/../../victim.tmp is what prepare_staging_directory
    # would rmtree for --notebook ../../victim.
    victim = Path(str(store.settings.storage_dir)).parent / "victim.tmp"
    victim.mkdir(parents=True)
    (victim / "keep").write_text("data", encoding="utf-8")

    with pytest.raises(cli.ScaleBuildCliError):
        cli.run_import(
            repository,
            "../../victim",
            _full_package(tmp_path),
            allow_library_mismatch=False,
            report=lambda _message: None,
        )
    assert (victim / "keep").exists()


# ─────────────────────────────────────────────────── build interruption ──

class _BuildRepository(_Repository):
    def __init__(self, settings, store, database, projections, build) -> None:
        super().__init__(settings, store, database, projections)
        self._build = build

    def build_scale_index(self, notebook_id, on_stage=None):
        return self._build(notebook_id)

    def fold_scale_index_delta(self, notebook_id):
        return self._build(notebook_id)


def test_build_of_an_unknown_notebook_is_a_clean_refusal(storage, store, lock):
    """P2, codex PR#643 R1: ``require_write_admission`` (reached deep inside
    ``build_scale_index``/``fold_scale_index_delta`` for an unknown OR
    currently-copying notebook) raises a bare ``KeyError(notebook_id)``.
    Uncaught, this used to surface a Python traceback instead of the
    documented exit-code-2 refusal ``inspect``/``import`` already give.
    Mutation anchor: drop the ``except KeyError`` clause in ``run_build`` and
    this goes green while a raw traceback reaches the operator."""

    def build(notebook_id):
        raise KeyError(notebook_id)

    repository = _BuildRepository(
        storage, store, _Database(lock), _Projections(PIPELINE), build
    )
    with pytest.raises(cli.ScaleBuildCliError, match="unknown notebook"):
        cli.run_build(repository, "nb-ghost", mode="full", report=lambda _m: None)
    with pytest.raises(cli.ScaleBuildCliError, match="unknown notebook"):
        cli.run_build(repository, "nb-ghost", mode="fold", report=lambda _m: None)


def test_an_interrupt_before_the_publish_leaves_staging_for_inspect(
    storage, store, lock, tmp_path
):
    """P1, codex PR#643 R1: ``run_build`` never holds the claim itself (the
    runtime takes and releases one internally), so by the time it can react
    to Ctrl-C the ``claim_token`` its own staging directory was suffixed
    with is already gone from view. Unlike the old fixed-``.tmp`` behavior,
    nothing is deleted here — ``inspect`` reports the leftover instead."""
    _seed_live(store, "nb-1")
    live = Path(store.scale_dir("nb-1"))

    def build(_notebook_id):
        store.prepare_staging_directory(live, "build-token")
        raise KeyboardInterrupt

    repository = _BuildRepository(
        storage, store, _Database(lock), _Projections(PIPELINE), build
    )
    messages: list[str] = []
    with pytest.raises(KeyboardInterrupt):
        cli.run_build(repository, "nb-1", mode="full", report=messages.append)

    assert Path(str(live) + ".tmp-build-token").is_dir()
    assert (live / "manifest.json").is_file()
    assert any("inspect" in message for message in messages)


def test_an_interrupt_inside_the_publish_keeps_both_copies(
    storage, store, lock, tmp_path
):
    """codex W-CLI R1 B1, the loss the reviewer found: the staged directory
    between the two renames is not abandoned staging, it is one of the two
    copies of the index that exist — and the previous generation is sitting
    in ``.old``. Deleting it there discards hours of work. Mutation anchor:
    report unconditionally as "nothing staged" here and the recovery message
    disappears."""
    _seed_live(store, "nb-1")
    live = Path(store.scale_dir("nb-1"))

    def build(_notebook_id):
        staged = store.prepare_staging_directory(live, "build-token")
        (staged / "manifest.json").write_text("{}", encoding="utf-8")
        os.rename(live, str(live) + ".old")  # the first of the two renames
        raise KeyboardInterrupt

    repository = _BuildRepository(
        storage, store, _Database(lock), _Projections(PIPELINE), build
    )
    messages: list[str] = []
    with pytest.raises(KeyboardInterrupt):
        cli.run_build(repository, "nb-1", mode="full", report=messages.append)

    assert Path(str(live) + ".tmp-build-token").is_dir(), (
        "the new generation must survive"
    )
    assert Path(str(live) + ".old").is_dir(), "so must the previous one"
    assert any(f"mv {live}.old {live}" in message for message in messages)


# ──────────────────────────────────────────────────────────────── export ──

def test_export_copies_every_present_root_under_the_claim(
    repository, store, tmp_path, lock
):
    _seed_live(store, "nb-1")
    receipt = cli.run_export(
        repository, "nb-1", tmp_path / "out", report=lambda _message: None
    )
    assert sorted(receipt["roots"]) == sorted(cli.PUBLISH_ORDER)
    assert (tmp_path / "out" / cli.MAIN_ROOT / "manifest.json").is_file()
    assert lock.released is True


def test_export_refuses_a_companion_from_another_generation(
    repository, store, tmp_path
):
    """The companion is rebuilt AFTER the main swap, so a "new main, old
    companion" window exists by construction; exporting it would ship a package
    that mixes two generations."""
    _seed_live(store, "nb-1")
    _write_manifest(
        Path(store.source_partition_dir("nb-1")),
        {"parent_version": ["nb-1", 0]},
    )
    with pytest.raises(cli.ScaleBuildCliError, match="different generation"):
        cli.run_export(
            repository, "nb-1", tmp_path / "out", report=lambda _message: None
        )


def test_export_refuses_a_non_empty_destination(repository, store, tmp_path):
    _seed_live(store, "nb-1")
    destination = tmp_path / "out"
    (destination / "kg_index").mkdir(parents=True)
    with pytest.raises(cli.ScaleBuildCliError, match="not empty"):
        cli.run_export(
            repository, "nb-1", destination, report=lambda _message: None
        )


def test_export_refuses_a_notebook_with_no_published_index(repository, tmp_path):
    with pytest.raises(cli.ScaleBuildCliError, match="no published scale index"):
        cli.run_export(
            repository, "nb-1", tmp_path / "out", report=lambda _message: None
        )


def test_export_refuses_a_destination_inside_a_source_root(repository, store, tmp_path):
    """``destination.mkdir()`` runs before ``copytree`` scans the source, so a
    destination under a live root gets walked as part of its own source and
    recurses without bound — and also writes into the read-only live index
    (codex PR#643 R2 P2)."""
    _seed_live(store, "nb-1")
    destination = Path(store.scale_dir("nb-1")) / "out"
    with pytest.raises(cli.ScaleBuildCliError, match="inside the"):
        cli.run_export(repository, "nb-1", destination, report=lambda _m: None)
    assert not destination.exists(), "nothing should have been created at all"


def test_export_refuses_a_destination_equal_to_a_source_root(repository, store, tmp_path):
    _seed_live(store, "nb-1")
    destination = Path(store.scale_dir("nb-1"))
    with pytest.raises(cli.ScaleBuildCliError, match="inside the"):
        cli.run_export(repository, "nb-1", destination, report=lambda _m: None)


def test_export_receipt_describes_the_copied_package_not_a_later_live_publish(
    repository, store, tmp_path
):
    """The claim is released when the ``with`` block in ``run_export`` exits;
    another builder can publish a newer generation the instant that happens.
    The receipt must describe what was actually copied into ``--to``, not
    whatever happens to be live when the manifest gets read (codex PR#643 R2
    P2)."""
    _seed_live(store, "nb-1")

    def report(message: str) -> None:
        if message == f"exported {cli.MAIN_ROOT}":
            # Simulate a concurrent publish landing on the live root right
            # after this export's copy of it finished.
            _write_manifest(
                Path(store.scale_dir("nb-1")), _main_manifest(version=["nb-1", 99])
            )

    receipt = cli.run_export(repository, "nb-1", tmp_path / "out", report=report)
    assert receipt["version"] == ["nb-1", 1]


def test_swap_reports_the_actual_tokenized_staging_path_on_lock_loss(tmp_path):
    """Recovery must be pointed at the directory that actually holds the
    staged build — the claim-unique ``{live}.tmp-<token>`` handed in as
    ``temporary`` — not the legacy no-suffix ``{live}.tmp`` this scheme
    replaced and which no longer exists (codex PR#643 R2 P2;
    docs/operations.md tokenized-path contract)."""
    live = tmp_path / "kg_index"
    temporary = tmp_path / "kg_index.tmp-abc123token"
    temporary.mkdir()
    with pytest.raises(ScaleBuildLockLost) as excinfo:
        ScaleArtifactStore.swap_staging_directory(
            live, temporary, verify_held=lambda: False
        )
    assert str(temporary) in str(excinfo.value)
    # The staged build must genuinely still be there for the message to be
    # actionable — the swap must not have touched it before refusing.
    assert temporary.is_dir()


# ──────────────────────────────────────────────────────── interrupt guard ──

def test_the_swap_guard_defers_sigint_and_restores_the_handler():
    """Mutation anchor: remove the guard and a Ctrl-C between the
    ``live → .old`` and ``tmp → live`` renames leaves the notebook with no live
    index at all."""
    previous = signal.getsignal(signal.SIGINT)
    messages: list[str] = []
    guard = cli.SwapInterruptGuard(messages.append)

    with pytest.raises(KeyboardInterrupt):
        with guard:
            installed = signal.getsignal(signal.SIGINT)
            assert installed is not previous
            installed(signal.SIGINT, None)  # what the OS would deliver
            assert guard.interrupted is True

    assert signal.getsignal(signal.SIGINT) is previous
    assert messages and "swap" in messages[0]


def test_the_swap_guard_does_not_replace_a_real_failure():
    previous = signal.getsignal(signal.SIGINT)
    guard = cli.SwapInterruptGuard(lambda _message: None)
    with pytest.raises(ScaleBuildLockLost):
        with guard:
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            raise ScaleBuildLockLost("claim gone")
    assert signal.getsignal(signal.SIGINT) is previous


def test_the_store_defers_sigint_across_its_own_rename_sequence(store, tmp_path):
    """codex W-CLI R1 B1. A build's renames happen deep inside
    ``build_scale_index``, hours after the CLI frame started, so the guard has
    to live at the primitive. Mutation anchor: drop the ``with
    SwapInterruptGuard()`` from ``swap_staging_directory`` and the interrupt
    escapes between the two renames — the live directory is left in ``.old``
    and this run's build is what the CLI's cleanup would then delete."""
    live = tmp_path / "root"
    live.mkdir()
    (live / "marker").write_text("old", encoding="utf-8")
    staged = store.prepare_staging_directory(live, "guard-token")
    (staged / "marker").write_text("new", encoding="utf-8")

    real_rename = os.rename
    renames: list[int] = []
    guarded: list[bool] = []

    def rename_and_signal(source, destination):
        real_rename(source, destination)
        renames.append(1)
        if len(renames) == 1:  # between live → .old and tmp → live
            handler = signal.getsignal(signal.SIGINT)
            guarded.append(
                isinstance(getattr(handler, "__self__", None), SwapInterruptGuard)
            )
            handler(signal.SIGINT, None)  # what the OS would deliver

    previous = signal.getsignal(signal.SIGINT)
    original = os.rename
    os.rename = rename_and_signal  # type: ignore[assignment]
    try:
        with pytest.raises(KeyboardInterrupt):
            ScaleArtifactStore.swap_staging_directory(live, staged)
    finally:
        os.rename = original  # type: ignore[assignment]

    assert guarded == [True]
    # The sequence finished before the interrupt was honoured.
    assert (live / "marker").read_text(encoding="utf-8") == "new"
    assert not Path(str(live) + ".old").exists()
    assert signal.getsignal(signal.SIGINT) is previous


def test_a_nested_guard_leaves_the_outer_deferral_window_intact():
    """The CLI wraps a whole multi-root publish in one guard, and each root's
    swap opens its own. If the inner one took over, the interrupt would land
    between two roots instead of after all of them."""
    outer = cli.SwapInterruptGuard(lambda _message: None, reraise=False)
    with outer:
        inner = cli.SwapInterruptGuard(lambda _message: None)
        with inner:
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        # No KeyboardInterrupt from the inner exit: the outer still owns it.
        assert inner.interrupted is False
        assert outer.interrupted is True
    assert outer.completed is True


def test_the_guard_reports_a_completed_block_without_raising():
    """codex W-CLI R1 P2-6: an interrupt that arrived while everything was
    published did not stop anything."""
    guard = cli.SwapInterruptGuard(lambda _message: None, reraise=False)
    with guard:
        signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
    assert (guard.interrupted, guard.completed) == (True, True)


def test_publish_started_recognizes_a_half_finished_swap(tmp_path):
    live = tmp_path / "kg_index"
    live.mkdir()
    assert cli.publish_started(live) is False
    (tmp_path / "kg_index.tmp").mkdir()
    assert cli.publish_started(live) is False, "staging alone is not publishing"
    (tmp_path / "kg_index.old").mkdir()
    assert cli.publish_started(live) is True
    shutil.rmtree(tmp_path / "kg_index.old")
    shutil.rmtree(live)
    assert cli.publish_started(live) is True, "tmp without live is mid-rename"


def test_publish_started_recognizes_the_claim_unique_staging_shape(tmp_path):
    """P1, codex PR#643 R1: staging can also be ``{live}.tmp-<token>``, not
    only the legacy fixed ``{live}.tmp`` — both must be recognized."""
    live = tmp_path / "kg_index"
    live.mkdir()
    (tmp_path / "kg_index.tmp-abc123").mkdir()
    assert cli.publish_started(live) is False, "staging alone is not publishing"
    shutil.rmtree(live)
    assert cli.publish_started(live) is True, "tmp-<token> without live is mid-rename"


def test_discard_staging_removes_only_what_it_is_given(tmp_path):
    staged = tmp_path / "kg_index.tmp"
    staged.mkdir()
    keep = tmp_path / "kg_index"
    keep.mkdir()
    messages: list[str] = []
    cli.discard_staging([staged, tmp_path / "absent.tmp"], messages.append)
    assert not staged.exists()
    assert keep.exists()
    assert len(messages) == 1


# ───────────────────────────────────────────────────────────────── argv ──

def test_sqlite_is_refused_before_anything_is_opened(monkeypatch, capsys):
    """A single-process deployment has no cross-process claim to take, so an
    offline builder there would race the serving process on one directory."""
    opened: list[str] = []
    monkeypatch.setattr(
        cli, "open_scale_build_repository", lambda _s: opened.append("opened")
    )
    assert cli.main(["inspect", "--notebook", "nb-1"]) == 2
    error = capsys.readouterr().err
    assert "requires PostgreSQL" in error
    assert opened == []


def test_a_ledger_mismatch_refuses_before_composing(monkeypatch, capsys):
    from app.core.config import Settings

    settings = Settings(database_url="postgresql://example/silicon_notebook")
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    opened: list[str] = []
    monkeypatch.setattr(
        cli, "open_scale_build_repository", lambda _s: opened.append("opened")
    )

    def refuse(_url):
        raise cli.ScaleBuildCliError("migration ledger mismatch: 41 vs 42")

    monkeypatch.setattr(cli, "verify_migration_ledger", refuse)
    assert cli.main(["build", "--notebook", "nb-1"]) == 2
    assert "migration ledger mismatch" in capsys.readouterr().err
    assert opened == []


def test_the_offline_statement_timeout_is_applied_before_composition(monkeypatch):
    """The pool's configure/reset callbacks issue RESET ALL, so a SET on a
    borrowed connection is wiped; the number has to be in ``Settings`` before
    ``PostgresDatabase`` reads it."""
    from app.core.config import Settings

    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: Settings(database_url="postgresql://example/silicon_notebook"),
    )
    assert cli.resolve_settings().postgres_statement_timeout_seconds == 86_400
    assert (
        cli.resolve_settings(3_600).postgres_statement_timeout_seconds == 3_600
    )
    with pytest.raises(cli.ScaleBuildCliError):
        cli.resolve_settings(0)


def test_the_composition_root_disowns_the_schema_at_the_call_site(monkeypatch):
    """codex W-CLI R1 P2-4. The PostgreSQL lane proves the SEAM works; this
    proves the CLI still uses it. Deleting both kwargs from
    ``open_scale_build_repository`` left every lane but that one green — and the
    damage is a composition that migrates the live database and rewrites the
    production admin credential with a fresh salt."""
    from app import bootstrap
    from app.core.config import Settings
    from app.repositories.postgres import repository as repository_module

    captured: dict = {}

    class _Composed:
        def __init__(self, settings, **kwargs) -> None:
            captured.update(kwargs)
            self.settings = settings
            self.closed = False

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(repository_module, "PostgresRepository", _Composed)
    monkeypatch.setattr(bootstrap, "prime_extension_admission", lambda _repo: None)

    settings = Settings(database_url="postgresql://example/silicon_notebook")
    with cli.open_scale_build_repository(settings) as repository:
        assert isinstance(repository, _Composed)
    assert captured["migrate"] is False
    assert captured["seed"] is False
    assert repository.closed is True


def test_packaged_migration_count_matches_the_schema_manifest():
    """The ledger preflight is only meaningful if this number is the truth."""
    from app.repositories.postgres.schema_manifest import POSTGRES_SCHEMA_MANIFEST

    assert cli.packaged_migration_count() == (
        POSTGRES_SCHEMA_MANIFEST.postgres_version
    )
