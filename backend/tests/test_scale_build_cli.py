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
    ScaleArtifactSwapRefused,
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


class _ScriptedLock(_Lock):
    """A claim whose ``verify_held`` follows a script, so a test can place the
    loss at ONE specific destructive step (P1, codex PR#643 R12).

    ``answers`` is consumed front to back, one entry per ``verify_held`` call;
    once exhausted it keeps answering ``False`` — a lock session that died
    stays dead, and every later step must see the same verdict.
    """

    def __init__(self, answers) -> None:
        super().__init__()
        self.answers = list(answers)
        self.checks = 0

    def verify_held(self) -> bool:
        self.checks += 1
        return self.answers.pop(0) if self.answers else False


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


def _write_transfer_manifest(package: Path) -> None:
    """(Re)generate ``transfer_manifest.json`` via the PRODUCTION generator
    (codex PR#643 R24 P1), never a parallel test-side implementation. Call
    this again any time a test adds/removes/mutates package content AFTER a
    ``_package``/``_full_package`` call and still expects ``run_import`` to
    publish successfully — a stale manifest would make ``verify_staged_
    transfer`` refuse a perfectly good package."""
    cli.write_transfer_manifest(package, cli.PUBLISH_ORDER, lambda _message: None)


def _package(tmp_path, *, name: str = "package", companion=None, **overrides) -> Path:
    """A three-root export package built from the frozen v9 artifact."""
    package = tmp_path / name
    main = package / cli.MAIN_ROOT
    shutil.copytree(FIXTURES / "kg_index" / "nb-fixture", main)
    _write_manifest(main, _main_manifest(**overrides))
    if companion is not None:
        _write_manifest(package / cli.COMPANION_ROOT, companion)
    _write_transfer_manifest(package)
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


def test_a_present_but_unreadable_viz_root_is_refused(tmp_path):
    """codex PR#643 R15 P2: a present ``kg_viz`` that the serving-side
    ``load_viz_index`` reads as ``None`` (here: manifest.json but no npz
    files) must refuse the package — importing it would atomically replace a
    healthy live viz root with an unreadable tree and still report success.

    Mutation anchor: drop the ``load_viz_index`` probe in
    ``validate_import_package`` and both these shapes validate cleanly.
    """
    package = _package(tmp_path)
    _write_manifest(package / "kg_viz", {"generation": "new"})
    with pytest.raises(cli.ScaleBuildCliError, match="kg_viz"):
        _validate(package)


def test_an_empty_viz_directory_is_refused(tmp_path):
    package = _package(tmp_path)
    (package / "kg_viz").mkdir()
    with pytest.raises(cli.ScaleBuildCliError, match="kg_viz"):
        _validate(package)


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


def test_a_truncated_required_array_is_refused(tmp_path):
    """P2, codex PR#643 R18: a present-but-unreadable ``.npy`` header used to be
    skipped as "unchecked" — the package validated, ``import`` published it over
    a healthy live index, and only then did ``load_scale_index`` reject it,
    leaving the notebook with no scale core at all.

    Mutation anchor: restore the ``actual is not None`` skip in
    ``artifact_inventory_error`` and this package validates cleanly.
    """
    package = _package(tmp_path)
    (package / cli.MAIN_ROOT / "node_ids.npy").write_bytes(b"\x93NUMPY")
    with pytest.raises(
        cli.ScaleBuildCliError, match="node_ids.npy.*truncated or malformed"
    ):
        _validate(package)


def test_a_truncated_graph_matrix_is_refused(tmp_path):
    """The same for ``graph.npz``'s shape. Second mutation anchor: a truncated
    npz raises ``zipfile.BadZipFile``, which is neither ``OSError`` nor
    ``ValueError`` — narrow the ``_graph_shape`` catch back to those and this
    escapes ``validate_import_package`` as an unhandled traceback instead of a
    refusal, so ``pytest.raises(ScaleBuildCliError)`` fails either way."""
    package = _package(tmp_path)
    graph = package / cli.MAIN_ROOT / "graph.npz"
    graph.write_bytes(graph.read_bytes()[:24])
    with pytest.raises(
        cli.ScaleBuildCliError, match="graph.npz.*truncated or malformed"
    ):
        _validate(package)


def test_a_manifest_that_declares_no_count_still_validates(tmp_path):
    """Negative anchor for the refusal above (``older-index-stays-valid``): the
    two "nothing to check" shapes must keep passing, because they are decided at
    the CALL SITE, not by the probes —

    * the manifest carries no expected value (here: no ``n_nodes``, so neither
      ``node_ids.npy``'s row count nor ``graph.npz``'s shape has anything to be
      compared against);
    * the manifest carries a count for an array this package does not include
      (here: ``n_chunk_ann`` with no ``has_chunk_ann`` flag and no
      ``chunk_ann_labels.npy``).

    A fix that made the probes themselves fail-closed would refuse both.
    """
    package = _package(tmp_path)
    manifest = _main_manifest(n_chunk_ann=5)
    manifest.pop("n_nodes")
    _write_manifest(package / cli.MAIN_ROOT, manifest)
    assert not (package / cli.MAIN_ROOT / "chunk_ann_labels.npy").exists()

    _manifest, warnings = _validate(package)
    assert warnings == []


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


def test_a_same_version_companion_from_another_build_is_refused(tmp_path):
    """P1, codex PR#643 R26: the two roots agree on ``version`` — a
    same-version republish is a supported scenario — but were produced by two
    different builds, which is exactly the half-published pair an interrupted
    publish leaves behind. The build id is the only thing that separates them.

    Mutation anchor: drop the ``build_generation_mismatch`` check from
    ``validate_import_package``'s companion block and this package validates
    cleanly, publishing a mixed generation.
    """
    package = _package(
        tmp_path,
        build_id="a" * 32,
        companion={
            "parent_version": ["nb-1", 7],
            "parent_build_id": "b" * 32,
            "published_sources": 1,
        },
    )
    with pytest.raises(cli.ScaleBuildCliError, match="build id mismatch"):
        _validate(package)


def test_a_companion_from_the_same_build_passes(tmp_path):
    package = _package(
        tmp_path,
        build_id="a" * 32,
        companion={
            "parent_version": ["nb-1", 7],
            "parent_build_id": "a" * 32,
            "published_sources": 1,
        },
    )
    _validate(package)


@pytest.mark.parametrize(
    "main_extra, companion_extra",
    [
        ({}, {}),
        ({"build_id": "a" * 32}, {}),
        ({}, {"parent_build_id": "b" * 32}),
    ],
    ids=["neither-side", "main-only", "companion-only"],
)
def test_a_package_missing_a_build_id_on_either_side_still_pairs_on_version(
    tmp_path, main_extra, companion_extra
):
    """Negative anchor for the refusal above (older-index-stays-valid). A
    package built before ``build_id`` existed carries it on NEITHER root; a
    package that mixes one old root with one new one carries it on exactly
    one. All three keep pairing on ``parent_version`` alone — the residual
    blind spot documented on ``build_generation_mismatch``, deliberately
    accepted so an existing package does not become unimportable. A fix that
    made the gate fail-closed on a missing id would refuse all three."""
    package = _package(
        tmp_path,
        companion={
            "parent_version": ["nb-1", 7],
            "published_sources": 1,
            **companion_extra,
        },
        **main_extra,
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


def test_a_regular_file_named_kg_index_is_refused_like_a_missing_export(tmp_path):
    """codex PR#643 R13 P2-b anchor: the MAIN root is never read as
    "omitted" the way an optional root can be — a package where ``kg_index``
    is a REGULAR FILE (not a directory) is already caught by the same
    ``main.is_dir()`` check that refuses a missing ``kg_index`` altogether,
    so this needs no new guard alongside the one added for the optional
    roots. Pinned here so a future refactor that loosens that check to
    "exists" cannot silently reopen this hole for the main root too."""
    package = tmp_path / "package"
    package.mkdir()
    (package / cli.MAIN_ROOT).write_text("not a directory", encoding="utf-8")
    with pytest.raises(cli.ScaleBuildCliError, match="does not look like an export"):
        _validate(package)


def test_npy_row_count_reads_the_header_without_unpickling(tmp_path):
    """Object arrays would need ``allow_pickle`` to materialize — an arbitrary
    code execution surface the validation must not touch."""
    path = tmp_path / "labels.npy"
    np.save(path, np.asarray(["a", "b", "c"], dtype=object))
    assert cli.npy_row_count(path) == 3
    with pytest.raises(ValueError):
        np.load(path, allow_pickle=False)


# ──────────────────────────────────────────────────────── transfer manifest ──

def test_a_package_with_no_transfer_manifest_is_refused_by_validate(tmp_path):
    """codex PR#643 R24 P1: this CLI has not shipped, so there is no
    compatibility obligation to a package built before the transfer manifest
    existed — a missing manifest is refused outright, not treated as an
    older-but-valid package.

    Mutation anchor: drop the ``_read_transfer_manifest`` call from
    ``validate_import_package`` and this goes green on a package with no
    manifest at all.
    """
    package = _full_package(tmp_path)
    (package / cli.TRANSFER_MANIFEST_FILENAME).unlink()
    with pytest.raises(cli.ScaleBuildCliError, match="transfer_manifest.json"):
        _validate(package)


def test_a_malformed_transfer_manifest_is_refused_by_validate(tmp_path):
    package = _full_package(tmp_path)
    (package / cli.TRANSFER_MANIFEST_FILENAME).write_text(
        json.dumps({"files": {"kg_index/manifest.json": {"bytes": "not-an-int"}}}),
        encoding="utf-8",
    )
    with pytest.raises(cli.ScaleBuildCliError, match="not a usable"):
        _validate(package)


def test_export_writes_a_transfer_manifest_covering_every_copied_file(
    repository, store, tmp_path
):
    """codex PR#643 R24 P1: the manifest ``run_export`` writes must list
    EVERY file it actually copied — not a subset — or ``import``'s check
    against it is worthless. Independently walks ``destination`` (not via
    ``cli.build_transfer_manifest``, which would just check the function
    against itself) and compares the path set.

    Mutation anchor: have ``write_transfer_manifest`` skip a root (or a file
    within one) and this goes red.
    """
    _seed_live(store, "nb-1")
    destination = tmp_path / "out"
    cli.run_export(repository, "nb-1", destination, report=lambda _m: None)

    manifest = json.loads(
        (destination / cli.TRANSFER_MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    listed = set(manifest["files"])

    actual: set[str] = set()
    for root_name in cli.PUBLISH_ORDER:
        root_dir = destination / root_name
        if not root_dir.is_dir():
            continue
        for current, _dirs, names in os.walk(root_dir):
            for filename in names:
                relative = (
                    Path(current) / filename
                ).relative_to(root_dir).as_posix()
                actual.add(f"{root_name}/{relative}")

    assert listed == actual
    assert actual, "the fixture must actually copy files for this to prove anything"
    # The manifest itself must not be nested inside a live root's own copy
    # (it never is — it is written at ``destination``, not inside a root).
    assert not any(name == cli.TRANSFER_MANIFEST_FILENAME for name in listed)


def test_export_then_import_round_trips_with_a_real_transfer_manifest(
    repository, store, tmp_path
):
    """The true end-to-end path: a package ``run_export`` actually wrote,
    re-imported by ``run_import`` — not the ``_package``/``_full_package``
    hand-assembled fixtures used everywhere else in this file. Proves
    ``write_transfer_manifest`` and ``verify_staged_transfer`` agree with
    each other, not just with themselves.
    """
    _seed_live(store, "nb-1")
    destination = tmp_path / "out"
    cli.run_export(repository, "nb-1", destination, report=lambda _m: None)

    receipt = cli.run_import(
        repository,
        "nb-1",
        destination,
        allow_library_mismatch=False,
        report=lambda _message: None,
    )
    assert receipt["roots"] == list(cli.PUBLISH_ORDER)
    assert _published_version(store, "nb-1") == ["nb-1", 1]
    for root in cli.artifact_roots(store, "nb-1").values():
        assert _staging_glob(root) == []
        assert not Path(str(root) + ".old").exists()


def test_export_then_import_carries_the_build_generation_across_both_roots(
    repository, store, tmp_path
):
    """P1, codex PR#643 R26, end to end on the offline path: the generation id
    is ordinary manifest content, so ``export``/``import`` must move it
    verbatim on BOTH roots. If either side dropped or rewrote it, the imported
    pair would stop matching and the notebook would silently lose the
    companion capability on arrival — a same-version import being the exact
    case this whole gate exists for.
    """
    _seed_live(store, "nb-1")
    _write_manifest(
        Path(store.scale_dir("nb-1")),
        _main_manifest(version=["nb-1", 1], build_id="a" * 32),
    )
    _write_manifest(
        Path(store.source_partition_dir("nb-1")),
        {
            "parent_version": ["nb-1", 1],
            "parent_build_id": "a" * 32,
            "published_sources": 1,
        },
    )
    destination = tmp_path / "out"
    cli.run_export(repository, "nb-1", destination, report=lambda _m: None)

    cli.run_import(
        repository,
        "nb-1",
        destination,
        allow_library_mismatch=False,
        report=lambda _message: None,
    )

    main = json.loads((Path(store.scale_dir("nb-1")) / "manifest.json").read_text())
    companion = json.loads(
        (Path(store.source_partition_dir("nb-1")) / "manifest.json").read_text()
    )
    assert main["build_id"] == "a" * 32
    assert companion["parent_build_id"] == "a" * 32


def _truncate_after_header(path: Path, keep_extra_bytes: int = 8) -> None:
    """Truncate a ``.npy`` file to its (intact) header plus a few payload
    bytes — the shape ``artifact_inventory_error``'s header-only check
    cannot see (codex PR#643 R24 P1)."""
    from numpy.lib import format as npy_format

    with open(path, "rb") as handle:
        version = npy_format.read_magic(handle)
        if version == (1, 0):
            npy_format.read_array_header_1_0(handle)
        else:
            npy_format.read_array_header_2_0(handle)
        header_end = handle.tell()
    with open(path, "rb") as handle:
        truncated = handle.read(header_end + keep_extra_bytes)
    path.write_bytes(truncated)


def test_import_refuses_a_transfer_truncated_npy_after_a_valid_header(
    repository, store, tmp_path
):
    """codex PR#643 R24 P1, the exact defect from the review finding: a
    transfer that truncates a ``.npy``'s PAYLOAD after an intact header is
    invisible to ``artifact_inventory_error`` (header-only) — the row count
    it reports still matches the manifest, since the shape lives in the
    header. Only the transfer manifest's byte count catches it.

    Mutation anchor: drop the ``verify_staged_transfer`` call in
    ``run_import`` and this goes green on a corrupted staged copy.
    """
    _seed_live(store, "nb-1")
    package = _full_package(tmp_path)
    target = package / cli.MAIN_ROOT / "node_ids.npy"
    original_size = target.stat().st_size
    _truncate_after_header(target)
    assert target.stat().st_size < original_size, "the file must actually shrink"
    # The header-only check alone must still be satisfied — this is the
    # whole point of the defect: it is NOT what catches this corruption.
    assert cli.npy_row_count(target) is not None

    with pytest.raises(cli.ScaleBuildCliFailure, match="bytes"):
        cli.run_import(
            repository,
            "nb-1",
            package,
            allow_library_mismatch=False,
            report=lambda _message: None,
        )

    assert _published_version(store, "nb-1") == ["nb-1", 1], (
        "the live tree must stay untouched"
    )
    for root in cli.artifact_roots(store, "nb-1").values():
        assert _staging_glob(root) == [], "the corrupted staging must be discarded"


def test_import_refuses_a_transfer_that_flips_one_byte_in_ann_bin(
    repository, store, tmp_path
):
    """``ann.bin`` has no header check at all — only existence is verified
    elsewhere — so a same-size, single-byte corruption is invisible to every
    check except the transfer manifest's SHA-256.

    Mutation anchor: have ``verify_staged_transfer`` compare only ``bytes``
    (not ``sha256``) and this goes green on a same-size corruption.
    """
    _seed_live(store, "nb-1")
    package = _full_package(tmp_path)
    ann_path = package / cli.MAIN_ROOT / "ann.bin"
    data = bytearray(ann_path.read_bytes())
    assert data, "the fixture's ann.bin must be non-empty for this to prove anything"
    data[0] ^= 0xFF
    ann_path.write_bytes(bytes(data))

    with pytest.raises(cli.ScaleBuildCliFailure, match="sha256"):
        cli.run_import(
            repository,
            "nb-1",
            package,
            allow_library_mismatch=False,
            report=lambda _message: None,
        )

    assert _published_version(store, "nb-1") == ["nb-1", 1]
    for root in cli.artifact_roots(store, "nb-1").values():
        assert _staging_glob(root) == []


def test_import_refuses_a_staged_file_the_transfer_manifest_does_not_list(
    repository, store, tmp_path
):
    """A file the package gained AFTER its manifest was written (a corrupted
    or tampered transfer) must be refused, not silently published."""
    _seed_live(store, "nb-1")
    package = _full_package(tmp_path)
    (package / cli.MAIN_ROOT / "unexpected.bin").write_bytes(b"surprise")

    with pytest.raises(cli.ScaleBuildCliFailure, match="not listed"):
        cli.run_import(
            repository,
            "nb-1",
            package,
            allow_library_mismatch=False,
            report=lambda _message: None,
        )

    assert _published_version(store, "nb-1") == ["nb-1", 1]


def test_import_refuses_when_the_transfer_manifest_lists_a_file_the_package_lacks(
    repository, store, tmp_path
):
    """``ann.bin`` is present in the fixture but not formally "required" by
    ``artifact_inventory_error`` (the manifest here carries no ``n_ann``), so
    removing it is invisible to every EXISTING pre-staging check — only the
    transfer manifest, which recorded its presence at export/package-build
    time, catches the loss."""
    _seed_live(store, "nb-1")
    package = _full_package(tmp_path)
    (package / cli.MAIN_ROOT / "ann.bin").unlink()

    with pytest.raises(cli.ScaleBuildCliFailure, match="missing"):
        cli.run_import(
            repository,
            "nb-1",
            package,
            allow_library_mismatch=False,
            report=lambda _message: None,
        )

    assert _published_version(store, "nb-1") == ["nb-1", 1]


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
    _write_viz_root(Path(store.viz_dir(notebook_id)), {"generation": "old"})
    _write_manifest(
        Path(store.source_partition_dir(notebook_id)),
        {"parent_version": ["nb-1", 1], "published_sources": 1},
    )


def _write_viz_root(out_dir: Path, manifest: dict) -> None:
    """A REAL minimal viz root (codex PR#643 R15 P2): ``validate_import_package``
    probes a present ``kg_viz`` with the serving-side ``load_viz_index``, so a
    manifest-only fake no longer passes as a package root."""
    import numpy as _np  # noqa: F401 - save_viz_index needs the array stack
    import scipy.sparse as _sp

    from app.services.kg import viz_index as _viz_index

    _viz_index.save_viz_index(
        str(out_dir),
        viz_ids=["a"],
        viz_adj=_sp.csr_matrix((1, 1)),
        viz_deg=[0],
        viz_types=["concept"],
        viz_names=["a"],
        viz_payload={},
        manifest=manifest,
    )


def _full_package(tmp_path) -> Path:
    package = _package(
        tmp_path, companion={"parent_version": ["nb-1", 7], "published_sources": 2}
    )
    _write_viz_root(package / "kg_viz", {"generation": "new"})
    # ``_package`` above already wrote a transfer manifest, but only over the
    # roots it built (kg_index + kg_index_partitions) — kg_viz was added
    # after. Regenerate so the manifest covers all three roots.
    _write_transfer_manifest(package)
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


def test_import_refuses_a_package_nested_inside_the_kg_index_root(
    repository, store, tmp_path
):
    """P2, codex PR#643 R10: staging only READS from ``package``, so one
    nested inside a live artifact root would survive staging unnoticed — but
    the swap below renames that very root to ``.old`` and ``finalize_swap``
    deletes it, silently deleting the operator's own input package. Reject
    before any copying happens, analogous to ``export --to``'s nesting guard.

    Mutation anchor: drop the nesting guard and this goes red — with the
    fake projections here reporting no identity drift, ``run_import`` runs to
    completion and the nested package is deleted by ``finalize_swap`` along
    with the ``.old`` it now lives inside. That deletion — not just the
    missing exception — is the real, user-visible harm this guard exists to
    prevent.
    """
    _seed_live(store, "nb-1")
    main_root = Path(store.scale_dir("nb-1"))
    package = _full_package(main_root)

    with pytest.raises(cli.ScaleBuildCliFailure, match=cli.MAIN_ROOT):
        cli.run_import(
            repository,
            "nb-1",
            package,
            allow_library_mismatch=False,
            report=lambda _message: None,
        )

    # Nothing on disk changed: the live tree is on its previous generation,
    # no staging was left behind, and — the actual harm this guard prevents —
    # the input package itself is untouched.
    assert _published_version(store, "nb-1") == ["nb-1", 1]
    assert (package / cli.MAIN_ROOT / "manifest.json").is_file()
    for root in cli.artifact_roots(store, "nb-1").values():
        assert _staging_glob(root) == []


def test_import_refuses_a_package_nested_inside_a_stale_old(
    repository, store, tmp_path
):
    """P2, codex PR#643 R10: the swap's own pre-clean ``rmtree``s a stale
    ``.old`` before its renames — a package staged there is destroyed just as
    surely as one nested inside the live root itself, so ``{root}.old`` must
    be rejected the same way ``{root}`` is."""
    _seed_live(store, "nb-1")
    old_root = Path(f"{store.scale_dir('nb-1')}.old")
    package = _full_package(old_root)

    with pytest.raises(cli.ScaleBuildCliFailure, match=cli.MAIN_ROOT):
        cli.run_import(
            repository,
            "nb-1",
            package,
            allow_library_mismatch=False,
            report=lambda _message: None,
        )

    assert _published_version(store, "nb-1") == ["nb-1", 1]
    assert (package / cli.MAIN_ROOT / "manifest.json").is_file()
    for root in cli.artifact_roots(store, "nb-1").values():
        assert _staging_glob(root) == []


def test_import_refuses_a_package_nested_inside_the_legacy_tmp_staging(
    repository, store, tmp_path
):
    """codex PR#643 R11 P1: ``prepare_staging_directory`` unconditionally
    ``rmtree``s the legacy no-suffix ``{root}.tmp`` before copying that root's
    staged tree in — a package nested there is destroyed one step earlier
    than the R10 nesting guard (which only checks ``{root}``/``{root}.old``)
    ever sees it.

    Mutation anchor: drop the ``staging_tmp_family`` branch of the
    containment check and this goes red — with the fake projections here
    reporting no identity drift, ``run_import`` runs to completion and
    ``prepare_staging_directory`` deletes the nested package via its own
    legacy-``.tmp`` pre-clean before anything is even staged.
    """
    _seed_live(store, "nb-1")
    legacy_tmp = Path(f"{store.scale_dir('nb-1')}.tmp")
    package = _full_package(legacy_tmp)

    with pytest.raises(cli.ScaleBuildCliFailure, match=cli.MAIN_ROOT):
        cli.run_import(
            repository,
            "nb-1",
            package,
            allow_library_mismatch=False,
            report=lambda _message: None,
        )

    # Nothing on disk changed: the live tree is on its previous generation,
    # and — the actual harm this guard prevents — the input package itself
    # (still living under the legacy ``.tmp`` name) is untouched. The package
    # itself IS a ``.tmp``-shaped sibling of ``kg_index``, so it legitimately
    # shows up in that root's own glob; the other two roots (never touched by
    # this refusal) must show no staging leftovers at all, and no ADDITIONAL
    # entry (a real staging copy) must have appeared beside the package.
    assert _published_version(store, "nb-1") == ["nb-1", 1]
    assert (package / cli.MAIN_ROOT / "manifest.json").is_file()
    roots = cli.artifact_roots(store, "nb-1")
    assert _staging_glob(roots[cli.COMPANION_ROOT]) == []
    assert _staging_glob(roots["kg_viz"]) == []
    assert _staging_glob(roots[cli.MAIN_ROOT]) == [legacy_tmp]


def test_import_refuses_a_package_nested_inside_another_claims_tokened_tmp(
    repository, store, tmp_path
):
    """The same danger for a claim-unique ``{root}.tmp-<token>`` actually on
    disk — not this run's own token, but some other build's (live or a
    zombie's). ``prepare_staging_directory`` only spares tokens that are NOT
    its own, but the containment check has to name every on-disk ``.tmp-*``
    sibling, not just the legacy no-suffix name."""
    _seed_live(store, "nb-1")
    tokened_tmp = Path(f"{store.scale_dir('nb-1')}.tmp-sometoken")
    package = _full_package(tokened_tmp)

    with pytest.raises(cli.ScaleBuildCliFailure, match=cli.MAIN_ROOT):
        cli.run_import(
            repository,
            "nb-1",
            package,
            allow_library_mismatch=False,
            report=lambda _message: None,
        )

    assert _published_version(store, "nb-1") == ["nb-1", 1]
    assert (package / cli.MAIN_ROOT / "manifest.json").is_file()
    roots = cli.artifact_roots(store, "nb-1")
    assert _staging_glob(roots[cli.COMPANION_ROOT]) == []
    assert _staging_glob(roots["kg_viz"]) == []
    assert _staging_glob(roots[cli.MAIN_ROOT]) == [tokened_tmp]


def test_import_refuses_a_package_nested_inside_this_runs_own_future_tmp(
    repository, store, tmp_path, lock
):
    """The claim-token directory THIS run's own ``prepare_staging_directory``
    is about to create does not exist on disk yet at containment-check time —
    it has to be named from ``handle.claim_token`` rather than discovered by
    globbing, or a package staged exactly there would still slip through."""
    _seed_live(store, "nb-1")
    own_future_tmp = Path(f"{store.scale_dir('nb-1')}.tmp-{lock.claim_token}")
    assert not own_future_tmp.exists(), "must be checked by name, not existence"
    package = _full_package(own_future_tmp)

    with pytest.raises(cli.ScaleBuildCliFailure, match=cli.MAIN_ROOT):
        cli.run_import(
            repository,
            "nb-1",
            package,
            allow_library_mismatch=False,
            report=lambda _message: None,
        )

    assert _published_version(store, "nb-1") == ["nb-1", 1]
    assert (package / cli.MAIN_ROOT / "manifest.json").is_file()
    roots = cli.artifact_roots(store, "nb-1")
    assert _staging_glob(roots[cli.COMPANION_ROOT]) == []
    assert _staging_glob(roots["kg_viz"]) == []
    assert _staging_glob(roots[cli.MAIN_ROOT]) == [own_future_tmp]


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


def test_import_translates_a_failed_pre_publish_identity_read_into_a_cli_failure(
    storage, store, tmp_path
):
    """codex PR#643 R19 P2-b: the R5 pre-rename re-read above does not only
    ever find a MISMATCH (the drift test above) — the read itself can fail to
    complete at all (a dropped connection, a statement timeout) after a
    multi-GB package has already been staged. That failure used to fall
    through to the generic ``except BaseException`` staging-discard handler,
    which both deleted the expensive staged copies AND let the raw database
    exception escape uncaught past ``main()`` as a traceback instead of the
    documented ``ScaleBuildCliFailure``/exit-1 contract. It must instead
    surface as a clean ``ScaleBuildCliFailure`` with the staged copies KEPT
    for a cheap retry, exactly like a detected drift — nothing was renamed,
    so there is nothing to roll back.

    Mutation anchors: (1) drop the translating ``try/except`` around the
    pre-publish read and the bare ``_FakeOperationalError`` escapes instead
    of ``ScaleBuildCliFailure`` (RED on ``pytest.raises``); (2) route the
    translated failure back through the generic ``except BaseException``
    branch instead of a dedicated one and the staged copies are deleted
    instead of kept (RED on the staging-glob assertions below).
    """
    _seed_live(store, "nb-1")
    calls = {"n": 0}
    projections = _Projections(PIPELINE)
    base = projections.pipeline_identity

    def failing(notebook_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return base(notebook_id)
        # The pre-rename (R5) re-read itself fails to complete — not a
        # mismatch it finds, but the read never returning an answer.
        raise _FakeOperationalError("server closed the connection unexpectedly")

    projections.pipeline_identity = failing  # type: ignore[method-assign]
    repository = _Repository(storage, store, _Database(_Lock()), projections)

    with pytest.raises(
        cli.ScaleBuildCliFailure, match="could not be re-verified"
    ):
        cli.run_import(
            repository,
            "nb-1",
            _full_package(tmp_path),
            allow_library_mismatch=False,
            report=lambda _message: None,
        )

    assert calls["n"] == 2, "the identity must be re-read exactly once before publishing"
    assert _published_version(store, "nb-1") == ["nb-1", 1], (
        "nothing may be published when the pre-publish identity check itself fails"
    )
    for root in cli.artifact_roots(store, "nb-1").values():
        assert not Path(str(root) + ".old").exists(), (
            "no rename ever happened, so there is no previous generation to "
            "roll back — .old must not appear"
        )
        assert len(_staging_glob(root)) == 1, (
            "the staged copies must be KEPT for a cheap retry, not discarded"
        )


def test_import_pre_publish_identity_interrupt_discards_staging_like_any_other(
    storage, store, tmp_path
):
    """codex PR#643 R19 P2-b anchor: ``KeyboardInterrupt`` on the pre-publish
    (R5) identity re-read is deliberately NOT translated — it must keep
    taking the ordinary pre-rename interrupt path (discard this run's own
    staging, propagate ``KeyboardInterrupt`` unchanged), the exact contract
    ``test_a_staging_failure_never_touches_the_live_tree`` above already pins
    for a plain ``OSError`` at the same point in the sequence. This confirms
    the P2-b translation added just above did not also swallow or change the
    interrupt case.

    Mutation anchor: route this ``KeyboardInterrupt`` through the same
    translation the other exceptions get and this goes red — either the
    exception type changes to ``ScaleBuildCliFailure`` (RED on
    ``pytest.raises``) or staging survives instead of being discarded
    (mismatching every other pre-rename interrupt in this file).
    """
    _seed_live(store, "nb-1")
    calls = {"n": 0}
    projections = _Projections(PIPELINE)
    base = projections.pipeline_identity

    def interrupting(notebook_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return base(notebook_id)
        # The pre-rename (R5) re-read itself is interrupted, not just slow.
        raise KeyboardInterrupt

    projections.pipeline_identity = interrupting  # type: ignore[method-assign]
    repository = _Repository(storage, store, _Database(_Lock()), projections)

    with pytest.raises(KeyboardInterrupt):
        cli.run_import(
            repository,
            "nb-1",
            _full_package(tmp_path),
            allow_library_mismatch=False,
            report=lambda _message: None,
        )

    assert calls["n"] == 2, "the identity must be re-read exactly once before publishing"
    assert _published_version(store, "nb-1") == ["nb-1", 1]
    for root in cli.artifact_roots(store, "nb-1").values():
        assert _staging_glob(root) == [], (
            "a pre-rename interrupt discards this run's own staging, exactly "
            "like the OSError case above"
        )


def test_import_rolls_back_when_pipeline_identity_drifts_during_the_swap(
    storage, store, tmp_path
):
    """codex PR#643 R8 P1: the R5 re-check above only catches a pipeline
    switch that lands BEFORE the renames start. A switch landing DURING them
    — a rebuild does not wait on this claim either — sails past it and gets
    published. The identity is read a THIRD time once the main root (last in
    PUBLISH_ORDER) is live; a drift there must undo the publish: every root
    rolled back to exactly its previous state, the rejected build left
    staged for inspection, exit code 1 (``ScaleBuildCliFailure``).

    Mutation anchors: (1) removing the post-swap re-check makes this pass
    with the drifted package published anyway (RED without a raise here);
    (2) keeping the raise but skipping ``rollback_swap`` makes the exception
    still fire but leaves the live tree on the drifted generation (RED on
    the version/``.old`` assertions below).
    """
    _seed_live(store, "nb-1")
    calls = {"n": 0}
    projections = _Projections(PIPELINE)
    base = projections.pipeline_identity

    def drifting(notebook_id):
        calls["n"] += 1
        if calls["n"] <= 2:
            return base(notebook_id)
        # A pipeline switch completed while the renames themselves were
        # running — after the pre-rename (R5) check already passed.
        return ["acme-pipeline", "3"]

    projections.pipeline_identity = drifting  # type: ignore[method-assign]
    repository = _Repository(storage, store, _Database(_Lock()), projections)

    with pytest.raises(cli.ScaleBuildCliFailure, match="changed"):
        cli.run_import(
            repository,
            "nb-1",
            _full_package(tmp_path),
            allow_library_mismatch=False,
            report=lambda _message: None,
        )

    assert calls["n"] >= 3, "the identity must be re-read a third time after the swap"
    assert _published_version(store, "nb-1") == ["nb-1", 1], (
        "the live main index must be rolled back to the previous generation"
    )
    assert json.loads(
        (Path(store.viz_dir("nb-1")) / "manifest.json").read_text()
    ) == {"generation": "old"}, "the live viz root must be rolled back too"
    assert json.loads(
        (Path(store.source_partition_dir("nb-1")) / "manifest.json").read_text()
    )["parent_version"] == ["nb-1", 1], "the live companion must be rolled back too"
    for root in cli.artifact_roots(store, "nb-1").values():
        assert not Path(str(root) + ".old").exists(), (
            "rollback must restore the previous .old back onto live, leaving "
            "no .old behind"
        )
        assert len(_staging_glob(root)) == 1, (
            "the rejected (drifted) build must remain staged for inspection, "
            "not be discarded"
        )


def test_a_filesystem_failure_during_rollback_stops_and_reports_observed_state(
    storage, store, tmp_path
):
    """codex PR#643 R14 P2: only ``ScaleBuildLockLost`` used to stop the
    rollback walk — an ``OSError`` from ``rollback_swap`` (first rename
    landed, ``.old`` restore did not) escaped as a raw traceback, leaving a
    partially reverted tree with none of the per-root recovery this helper
    promises. It must stop the walk exactly like a lost claim, report the
    failed root's OBSERVED path states (its shape can no longer be assumed),
    the published shape of every root behind it, and surface as
    ``ScaleBuildCliFailure``.

    Mutation anchor: drop the ``except OSError`` branch and this goes red —
    the raw ``OSError`` escapes instead of the ``ScaleBuildCliFailure``.
    """
    _seed_live(store, "nb-1")
    calls = {"n": 0}
    projections = _Projections(PIPELINE)
    base = projections.pipeline_identity

    def drifting(notebook_id):
        calls["n"] += 1
        if calls["n"] <= 2:
            return base(notebook_id)
        return ["acme-pipeline", "3"]

    projections.pipeline_identity = drifting  # type: ignore[method-assign]
    repository = _Repository(storage, store, _Database(_Lock()), projections)

    real_rollback = store.rollback_swap

    def failing_rollback(live, temporary, preserved, **kwargs):
        if f"{os.sep}kg_viz{os.sep}" in str(live):
            raise OSError("disk gone read-only under the second rename")
        return real_rollback(live, temporary, preserved, **kwargs)

    store.rollback_swap = failing_rollback  # type: ignore[method-assign]
    messages: list[str] = []

    with pytest.raises(cli.ScaleBuildCliFailure, match="mid-rename"):
        cli.run_import(
            repository,
            "nb-1",
            _full_package(tmp_path),
            allow_library_mismatch=False,
            report=messages.append,
        )

    # Walk order is newest-first: the main root rolled back before the
    # failure, the viz root failed, the companion behind it was never tried.
    assert _published_version(store, "nb-1") == ["nb-1", 1], (
        "the root reverted before the failure stays reverted"
    )
    observed = [m for m in messages if m.startswith("kg_viz: rollback stopped")]
    assert len(observed) == 1, "the failed root gets an observed-state report"
    assert "present" in observed[0] and "Manual recovery" in observed[0]
    assert any(
        m.startswith("kg_index_partitions: still live") for m in messages
    ), "roots behind the failure keep the published-shape recovery line"


def test_import_rolls_back_when_post_swap_verification_is_interrupted(
    storage, store, tmp_path
):
    """P2, codex PR#643 R9: a Ctrl-C landing on the post-swap identity read
    ITSELF — not a mismatch it finds, but the read failing to complete at
    all — used to bypass rollback entirely, propagating straight out of
    ``run_import`` and leaving an UNVERIFIED publish live with every root's
    ``.old`` still on disk. It must roll back exactly like a detected drift,
    then re-raise ``KeyboardInterrupt`` unchanged so the ordinary
    interrupted-run path (``main``'s ``except KeyboardInterrupt`` — prints
    "interrupted", exit 130) still applies.

    Mutation anchor: drop the ``except KeyboardInterrupt`` branch (or its
    rollback call) around this read and this goes red — the live tree stays
    on the unverified new generation and every root keeps its ``.old``.
    """
    _seed_live(store, "nb-1")
    calls = {"n": 0}
    projections = _Projections(PIPELINE)
    base = projections.pipeline_identity

    def interrupting(notebook_id):
        calls["n"] += 1
        if calls["n"] <= 2:
            return base(notebook_id)
        # The post-swap read itself is interrupted, not just slow.
        raise KeyboardInterrupt

    projections.pipeline_identity = interrupting  # type: ignore[method-assign]
    repository = _Repository(storage, store, _Database(_Lock()), projections)

    with pytest.raises(KeyboardInterrupt):
        cli.run_import(
            repository,
            "nb-1",
            _full_package(tmp_path),
            allow_library_mismatch=False,
            report=lambda _message: None,
        )

    assert calls["n"] >= 3, "the identity must be re-read a third time after the swap"
    assert _published_version(store, "nb-1") == ["nb-1", 1], (
        "the live main index must be rolled back to the previous generation"
    )
    assert json.loads(
        (Path(store.viz_dir("nb-1")) / "manifest.json").read_text()
    ) == {"generation": "old"}, "the live viz root must be rolled back too"
    assert json.loads(
        (Path(store.source_partition_dir("nb-1")) / "manifest.json").read_text()
    )["parent_version"] == ["nb-1", 1], "the live companion must be rolled back too"
    for root in cli.artifact_roots(store, "nb-1").values():
        assert not Path(str(root) + ".old").exists(), (
            "rollback must restore the previous .old back onto live, leaving "
            "no .old behind"
        )
        assert len(_staging_glob(root)) == 1, (
            "the unverified build must remain staged for inspection, not be "
            "discarded"
        )


class _FakeOperationalError(Exception):
    """Stands in for a transient database error (e.g. sqlalchemy's
    ``OperationalError``) on the post-swap identity read, without requiring a
    real database connection to provoke one."""


def test_import_rolls_back_when_post_swap_verification_raises(
    storage, store, tmp_path
):
    """P2, codex PR#643 R9: a transient database error on the post-swap
    identity read — not a drift it detects, but the read itself failing —
    must roll back exactly like a detected drift and surface a clean
    ``ScaleBuildCliFailure`` (exit code 1) instead of letting the bare
    database exception escape with an unverified publish left live.

    Mutation anchor: drop the ``except BaseException`` branch (or its
    rollback call) around this read and this goes red — the live tree stays
    on the unverified new generation, every root keeps its ``.old``, and the
    bare ``_FakeOperationalError`` escapes uncaught instead of a
    ``ScaleBuildCliFailure``.
    """
    _seed_live(store, "nb-1")
    calls = {"n": 0}
    projections = _Projections(PIPELINE)
    base = projections.pipeline_identity

    def failing(notebook_id):
        calls["n"] += 1
        if calls["n"] <= 2:
            return base(notebook_id)
        raise _FakeOperationalError("server closed the connection unexpectedly")

    projections.pipeline_identity = failing  # type: ignore[method-assign]
    repository = _Repository(storage, store, _Database(_Lock()), projections)

    with pytest.raises(cli.ScaleBuildCliFailure, match="could not complete"):
        cli.run_import(
            repository,
            "nb-1",
            _full_package(tmp_path),
            allow_library_mismatch=False,
            report=lambda _message: None,
        )

    assert calls["n"] >= 3, "the identity must be re-read a third time after the swap"
    assert _published_version(store, "nb-1") == ["nb-1", 1], (
        "the live main index must be rolled back to the previous generation"
    )
    assert json.loads(
        (Path(store.viz_dir("nb-1")) / "manifest.json").read_text()
    ) == {"generation": "old"}, "the live viz root must be rolled back too"
    assert json.loads(
        (Path(store.source_partition_dir("nb-1")) / "manifest.json").read_text()
    )["parent_version"] == ["nb-1", 1], "the live companion must be rolled back too"
    for root in cli.artifact_roots(store, "nb-1").values():
        assert not Path(str(root) + ".old").exists(), (
            "rollback must restore the previous .old back onto live, leaving "
            "no .old behind"
        )
        assert len(_staging_glob(root)) == 1, (
            "the unverified build must remain staged for inspection, not be "
            "discarded"
        )


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


class _FailingLockProbeDatabase:
    """``try_scale_build_lock`` that raises rather than returning a
    ``ScaleBuildLockAttempt`` — stands in for the dedicated lock connection
    or ``pg_try_advisory_lock`` itself failing (codex PR#643 R11 P2-b)."""

    def try_scale_build_lock(self, notebook_id: str):
        from app.repositories.postgres.database import PostgresDatabaseError

        raise PostgresDatabaseError(
            "PostgreSQL scale build lock acquisition failed for <redacted>"
        )


def test_claim_notebook_translates_a_lock_probe_failure_into_a_clean_refusal(
    storage, store
):
    """codex PR#643 R11 P2-b: the online runtime
    (``scale_artifact_runtime``'s ``_acquire_scale_build_lock``) already
    translates a lock-probe failure into ``SCALE_BUILD_LOCK_UNAVAILABLE`` —
    but the CLI's ``claim_notebook`` calls ``try_scale_build_lock`` directly,
    bypassing that wrapper, so the bare ``PostgresDatabaseError`` used to
    escape as an unhandled traceback instead of the documented "nothing
    changed, retry later" refusal every other claim failure gets.

    Mutation anchor: drop the ``except PostgresDatabaseError`` translation in
    ``claim_notebook`` and this goes red with the raw
    ``PostgresDatabaseError`` escaping instead of ``ScaleBuildCliFailure``.
    """
    repository = _Repository(
        storage, store, _FailingLockProbeDatabase(), _Projections(PIPELINE)
    )

    with pytest.raises(cli.ScaleBuildCliFailure, match="lock backend"):
        with cli.claim_notebook(repository, "nb-1"):
            pytest.fail("must not yield a claim on a probe failure")


def test_run_inspect_reports_an_unknown_claim_on_a_lock_probe_failure(
    storage, store
):
    """The same probe failure, reached through ``run_inspect`` this time:
    a lock-backend error is not a statement about the notebook any more than
    an exhausted lock-session budget is, so it must fold into the SAME
    documented ``unknown`` claim state — not crash the read-only inspect an
    operator is very likely running to diagnose exactly this trouble.

    Mutation anchor: drop the ``except PostgresDatabaseError`` around the
    probe in ``run_inspect`` and this goes red with the raw exception
    escaping instead of a clean ``build_claim: unknown`` receipt.
    """
    _seed_live(store, "nb-1")

    class _ScaleArtifacts:
        @staticmethod
        def version(_notebook_id: str):
            return ["nb-1", 1]

    class _InspectRuntime(_Runtime):
        def __init__(self, store, database, projections) -> None:
            super().__init__(store, database, projections)
            self.scale_artifacts = _ScaleArtifacts()

    class _InspectRepository(_Repository):
        def __init__(self, settings, store, database, projections) -> None:
            self.settings = settings
            self._runtime = _InspectRuntime(store, database, projections)

        @staticmethod
        def scale_index_status(_notebook_id: str):
            return {
                "state": "ready",
                "exists": True,
                "building": False,
                "delta_chunks": 0,
                "total_chunks": 2,
            }

    repository = _InspectRepository(
        storage, store, _FailingLockProbeDatabase(), _Projections(PIPELINE)
    )

    receipt = cli.run_inspect(repository, "nb-1", report=lambda _message: None)

    assert receipt["build_claim"] == "unknown"


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
        preserved = original(live, temporary, **kwargs)
        if not delivered:
            delivered.append(1)
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        return preserved

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
    for root in cli.artifact_roots(store, "nb-1").values():
        assert not Path(str(root) + ".old").exists(), (
            "a fully published run must still finalize .old cleanup for "
            "every root (codex PR#643 R8 P1/P2 keep_old bookkeeping)"
        )


def test_import_retires_a_stale_companion_when_the_package_omits_it(
    repository, store, tmp_path
):
    """codex PR#643 R11 P2-a: a same-version republish whose package has no
    companion used to leave a STALE live companion in place — its
    ``parent_version`` still matches (same version number), its stat
    signature never changes, so a reader keeps pairing it with the new main
    root even though this package never vouched for it. Absent from the
    package AND live on disk must retire the stale root, not skip it.

    Mutation anchor: drop the ``retiring``/``retire_live_directory`` branch
    in ``run_import`` and this goes red — the old companion (and its
    ``parent_version``) survives the import untouched.
    """
    _seed_live(store, "nb-1")  # companion parent_version=["nb-1", 1]
    package = _package(tmp_path, version=["nb-1", 1])  # same version, no companion
    assert not (package / cli.COMPANION_ROOT).exists()
    _write_viz_root(package / "kg_viz", {"generation": "kept"})
    _write_transfer_manifest(package)  # regenerate: now covers kg_viz too

    receipt = cli.run_import(
        repository,
        "nb-1",
        package,
        allow_library_mismatch=False,
        report=lambda _message: None,
    )

    assert receipt["retired"] == [cli.COMPANION_ROOT]
    assert cli.COMPANION_ROOT in receipt["roots"]
    companion_dir = Path(store.source_partition_dir("nb-1"))
    assert not companion_dir.exists(), "the stale companion must be gone"
    assert not Path(f"{companion_dir}.old").exists(), (
        "a clean retire must finalize its own .old, not leave it behind"
    )
    # The root this package DID include is untouched by the retire logic.
    assert (
        json.loads((Path(store.viz_dir("nb-1")) / "manifest.json").read_text())
        == {"generation": "kept"}
    )


def test_import_retires_a_stale_viz_root_when_the_package_omits_it(
    repository, store, tmp_path
):
    """The viz root is retired the same way the companion is — a package
    that omits it while a live viz tree still exists from an earlier
    generation must not leave that stale tree in place."""
    _seed_live(store, "nb-1")
    package = _package(
        tmp_path,
        version=["nb-1", 7],
        companion={"parent_version": ["nb-1", 7], "published_sources": 2},
    )
    assert not (package / "kg_viz").exists()

    receipt = cli.run_import(
        repository,
        "nb-1",
        package,
        allow_library_mismatch=False,
        report=lambda _message: None,
    )

    assert receipt["retired"] == ["kg_viz"]
    viz_dir = Path(store.viz_dir("nb-1"))
    assert not viz_dir.exists()
    assert not Path(f"{viz_dir}.old").exists()


def test_import_skips_an_optional_root_absent_from_both_package_and_disk(
    repository, store, tmp_path
):
    """No live root to retire, and none in the package: unchanged "skip"
    behaviour — nothing is created, nothing is reported as retired."""
    package = _package(tmp_path)  # no companion, no live seed at all
    assert not (package / cli.COMPANION_ROOT).exists()

    receipt = cli.run_import(
        repository,
        "nb-1",
        package,
        allow_library_mismatch=False,
        report=lambda _message: None,
    )

    assert receipt["retired"] == []
    assert cli.COMPANION_ROOT not in receipt["roots"]
    assert not Path(store.source_partition_dir("nb-1")).exists()


def test_import_refuses_a_package_where_an_optional_root_is_a_regular_file(
    repository, store, tmp_path
):
    """codex PR#643 R13 P2-b: a damaged transfer that leaves a REGULAR FILE
    named ``kg_viz`` inside the package looks exactly like an omitted root
    to the retiring judgment — ``source.is_dir()`` is False either way — so
    without a guard the healthy LIVE viz root would be silently retired and
    the rest of the package published in its place.

    Mutation anchor: drop the non-directory guard added ahead of the
    staging loop in ``run_import`` and this goes red — the live viz root is
    retired even though the package never validly omitted it.
    """
    _seed_live(store, "nb-1")
    package = _package(
        tmp_path, companion={"parent_version": ["nb-1", 7], "published_sources": 2}
    )
    (package / "kg_viz").write_text("not a directory", encoding="utf-8")

    with pytest.raises(cli.ScaleBuildCliFailure, match="kg_viz"):
        cli.run_import(
            repository,
            "nb-1",
            package,
            allow_library_mismatch=False,
            report=lambda _message: None,
        )

    viz_dir = Path(store.viz_dir("nb-1"))
    assert json.loads((viz_dir / "manifest.json").read_text()) == {
        "generation": "old"
    }, "the healthy live viz root must not be retired"
    assert not Path(f"{viz_dir}.old").exists()
    # Refused before any staging began: no root's .tmp-<token> is left behind.
    storage_root = Path(store.settings.storage_dir)
    assert not list(storage_root.rglob("*.tmp-*")), (
        "the refusal must land before any staging copy starts"
    )


def test_import_refuses_a_transfer_that_dropped_a_whole_listed_root(
    repository, store, tmp_path
):
    """codex PR#643 R25 P1: an off-host transfer can lose an ENTIRE optional
    directory while ``transfer_manifest.json`` still lists its files. The
    per-root file comparison never visits a root that is not in ``staged``,
    so without a root-set check ``run_import`` reads the loss as an
    intentional omission, RETIRES the healthy live root and reports success.

    Mutation anchor: drop the manifest-roots vs staged-roots comparison in
    ``verify_staged_transfer`` and this goes red — the import succeeds and
    the live viz root is retired.
    """
    _seed_live(store, "nb-1")
    package = _full_package(tmp_path)
    shutil.rmtree(package / "kg_viz")  # the manifest still lists kg_viz/*

    with pytest.raises(cli.ScaleBuildCliFailure, match="whole root"):
        cli.run_import(
            repository,
            "nb-1",
            package,
            allow_library_mismatch=False,
            report=lambda _message: None,
        )

    viz_dir = Path(store.viz_dir("nb-1"))
    assert (viz_dir / "manifest.json").is_file(), (
        "the healthy live viz root must not be retired"
    )
    assert not Path(f"{viz_dir}.old").exists()


def test_import_refuses_a_package_where_an_optional_root_is_a_dangling_symlink(
    repository, store, tmp_path
):
    """codex PR#643 R13 follow-up P2: a corrupted transfer can also leave the
    optional root as a DANGLING symlink — ``entry.exists()`` follows the link
    and reports False, so the regular-file guard alone reads it as an omitted
    root and retires the healthy live artifact for it.

    Mutation anchor: drop the ``is_symlink()`` half of the guard and this
    goes red the same way the regular-file test does.
    """
    _seed_live(store, "nb-1")
    package = _package(
        tmp_path, companion={"parent_version": ["nb-1", 7], "published_sources": 2}
    )
    (package / "kg_viz").symlink_to(package / "no-such-target")

    with pytest.raises(cli.ScaleBuildCliFailure, match="kg_viz"):
        cli.run_import(
            repository,
            "nb-1",
            package,
            allow_library_mismatch=False,
            report=lambda _message: None,
        )

    viz_dir = Path(store.viz_dir("nb-1"))
    assert json.loads((viz_dir / "manifest.json").read_text()) == {
        "generation": "old"
    }, "the healthy live viz root must not be retired"
    assert not Path(f"{viz_dir}.old").exists()
    storage_root = Path(store.settings.storage_dir)
    assert not list(storage_root.rglob("*.tmp-*")), (
        "the refusal must land before any staging copy starts"
    )


def test_import_rolls_back_a_retired_companion_when_identity_drifts_during_the_swap(
    storage, store, tmp_path
):
    """codex PR#643 R11 P2-a + R8 P1: a retired root is published inside the
    same guarded loop as a real swap, so it must be rolled back exactly like
    one when the post-swap pipeline-identity re-check finds a drift — the
    companion goes back to being live, not stranded in ``.old``.

    Mutation anchor: keep the retire but skip it in
    ``ScaleArtifactStore.rollback_swap``'s ``temporary=None`` branch and this
    goes red — the drift is still refused, but the companion never comes back
    to its live path.
    """
    _seed_live(store, "nb-1")
    package = _package(tmp_path, version=["nb-1", 1])  # no companion
    _write_viz_root(package / "kg_viz", {"generation": "kept"})
    _write_transfer_manifest(package)  # regenerate: now covers kg_viz too
    calls = {"n": 0}
    projections = _Projections(PIPELINE)
    base = projections.pipeline_identity

    def drifting(notebook_id):
        calls["n"] += 1
        if calls["n"] <= 2:
            return base(notebook_id)
        return ["acme-pipeline", "3"]

    projections.pipeline_identity = drifting  # type: ignore[method-assign]
    repository = _Repository(storage, store, _Database(_Lock()), projections)

    with pytest.raises(cli.ScaleBuildCliFailure, match="changed"):
        cli.run_import(
            repository,
            "nb-1",
            package,
            allow_library_mismatch=False,
            report=lambda _message: None,
        )

    assert calls["n"] >= 3
    assert _published_version(store, "nb-1") == ["nb-1", 1]
    companion_dir = Path(store.source_partition_dir("nb-1"))
    assert companion_dir.exists(), "the retired companion must come back live"
    assert json.loads(
        (companion_dir / "manifest.json").read_text()
    )["parent_version"] == ["nb-1", 1]
    assert not Path(f"{companion_dir}.old").exists(), (
        "rollback must restore .old back onto live, leaving no .old behind"
    )


class _ExclusiveDatabase:
    """A claim backend that grants ONE holder at a time — the single property
    a PostgreSQL session advisory lock provides. Both the CLI's
    ``claim_notebook`` and the serving process's viz rebuild
    (``ScaleArtifactRuntime._acquire_scale_build_lock``) reach the claim
    through this one method, so this is enough to state the exclusion between
    them without a database."""

    def __init__(self) -> None:
        self.held: dict[str, _Lock] = {}

    def try_scale_build_lock(self, notebook_id: str):
        if notebook_id in self.held:
            return None  # "provably held by somebody else"
        handle = _Lock()
        granted_release = handle.release

        def release() -> None:
            self.held.pop(notebook_id, None)
            granted_release()

        handle.release = release  # type: ignore[method-assign]
        self.held[notebook_id] = handle
        return handle


def test_the_import_claim_excludes_an_online_viz_rebuild(
    storage, store, tmp_path
):
    """P2, codex PR#643 R12: the serving process's standalone viz rebuild now
    takes the same per-notebook claim this command holds for its whole run.
    Probed from inside the publish window — the exact moment ``import`` is
    renaming roots — that claim must come back "held by somebody else", which
    is the branch that makes the viz rebuild give way instead of writing into
    a ``kg_viz`` directory this run may be renaming or retiring.

    Mutation anchor: have the viz rebuild skip the claim (see
    ``ScaleArtifactRuntime.build_viz``) and this exclusion buys nothing — the
    online writer is back inside the window, which is the state the finding
    describes.
    """
    _seed_live(store, "nb-1")
    database = _ExclusiveDatabase()
    repository = _Repository(storage, store, database, _Projections(PIPELINE))
    probes: list[object] = []
    original = ScaleArtifactStore.swap_staging_directory

    def spy(live, temporary, **kwargs):
        probes.append(database.try_scale_build_lock("nb-1"))
        return original(live, temporary, **kwargs)

    store.swap_staging_directory = spy  # type: ignore[method-assign]
    cli.run_import(
        repository,
        "nb-1",
        _full_package(tmp_path),
        allow_library_mismatch=False,
        report=lambda _message: None,
    )

    assert probes and all(probe is None for probe in probes), (
        "a viz rebuild probing the claim mid-import must be refused"
    )
    # And the claim is free again the moment the command releases it.
    assert database.try_scale_build_lock("nb-1") is not None


def test_import_refuses_to_retire_a_root_when_the_claim_was_lost(
    storage, store, tmp_path
):
    """P1, codex PR#643 R12: retiring a root is a ``live -> .old`` rename —
    just as destructive as a swap — and it used to run with NO claim
    re-verification, on the theory that the main root's later swap would catch
    the loss. It would, but far too late: by then this rename has already
    happened to whatever is live NOW, which after a lost claim may be a second
    builder's freshly published root, and no handler puts it back.

    The claim here survives the companion's swap and dies immediately before
    the viz retirement. Nothing may be renamed for viz, and the companion this
    run DID publish must be reported precisely — its rollback re-verifies the
    same (now dead) claim, so it stops without renaming either.

    Mutation anchor: drop ``verify_held=`` from the ``retire_live_directory``
    call and this goes red on the viz assertion below — the stale-claim run
    retires the live viz root anyway.
    """
    _seed_live(store, "nb-1")
    package = _package(
        tmp_path,
        version=["nb-1", 7],
        companion={"parent_version": ["nb-1", 7], "published_sources": 2},
    )  # no kg_viz: the live viz root is a retire candidate
    lock = _ScriptedLock([True])  # companion swap ok, then the session dies
    repository = _Repository(
        storage, store, _Database(lock), _Projections(PIPELINE)
    )
    messages: list[str] = []

    with pytest.raises(cli.ScaleBuildCliFailure, match="lock was lost"):
        cli.run_import(
            repository,
            "nb-1",
            package,
            allow_library_mismatch=False,
            report=messages.append,
        )

    viz_dir = Path(store.viz_dir("nb-1"))
    assert json.loads((viz_dir / "manifest.json").read_text()) == {
        "generation": "old"
    }, "a retirement without the claim must not rename anything"
    assert not Path(f"{viz_dir}.old").exists()
    # The companion swap that DID happen stays exactly as published: its
    # rollback re-verifies the same dead claim and refuses too.
    companion = Path(store.source_partition_dir("nb-1"))
    assert json.loads((companion / "manifest.json").read_text())[
        "parent_version"
    ] == ["nb-1", 7]
    assert Path(f"{companion}.old").exists()
    assert _published_version(store, "nb-1") == ["nb-1", 1]
    assert any(
        f"mv {companion}.old" in message and "still live" in message
        for message in messages
    ), "the un-reverted root must be reported with its concrete recovery"


def test_import_stops_a_rollback_that_loses_the_claim_midway(
    storage, store, tmp_path
):
    """P1, codex PR#643 R12: the post-swap rollback is two renames per root
    and used to run unverified. Undoing a publish without the claim is exactly
    as destructive as making one — ``live`` may already belong to a second
    builder — so each root re-verifies first, and a refusal stops the walk
    where it is rather than plowing on.

    The script here lets all three roots publish, lets the FIRST rollback (the
    main root, since rollback walks in reverse) succeed, and kills the claim
    before the second. The run must report which roots were reverted, which
    were not, and the exact ``mv`` for each of the latter.

    Mutation anchor: drop ``verify_held=`` from the ``rollback_swap`` call and
    this goes red — all three roots roll back and the viz/companion
    assertions below (still on this run's generation) fail.
    """
    _seed_live(store, "nb-1")
    projections = _Projections(PIPELINE)
    base = projections.pipeline_identity
    calls = {"n": 0}

    def drifting(notebook_id):
        calls["n"] += 1
        if calls["n"] <= 2:
            return base(notebook_id)
        return ["acme-pipeline", "3"]  # a switch landed during the renames

    projections.pipeline_identity = drifting  # type: ignore[method-assign]
    # 3 swaps, then the main root's rollback, then the session dies.
    lock = _ScriptedLock([True, True, True, True])
    repository = _Repository(storage, store, _Database(lock), projections)
    messages: list[str] = []

    with pytest.raises(cli.ScaleBuildCliFailure, match="rollback that was under way"):
        cli.run_import(
            repository,
            "nb-1",
            _full_package(tmp_path),
            allow_library_mismatch=False,
            report=messages.append,
        )

    assert _published_version(store, "nb-1") == ["nb-1", 1], (
        "the main root's rollback ran while the claim was still held"
    )
    viz_dir = Path(store.viz_dir("nb-1"))
    assert json.loads((viz_dir / "manifest.json").read_text()) == {
        "generation": "new"
    }, "the viz rollback must NOT run without the claim"
    companion = Path(store.source_partition_dir("nb-1"))
    assert json.loads((companion / "manifest.json").read_text())[
        "parent_version"
    ] == ["nb-1", 7], "the companion rollback must not run either"
    for root in (viz_dir, companion):
        assert Path(f"{root}.old").exists(), (
            "the previous generation stays in .old for the manual recovery"
        )
    reported = "\n".join(messages)
    assert f"mv {viz_dir} " in reported and f"mv {viz_dir}.old {viz_dir}" in reported
    assert f"mv {companion}.old {companion}" in reported


def test_import_stops_the_old_cleanup_when_the_claim_is_lost(
    repository, storage, store, tmp_path
):
    """P1, codex PR#643 R12: ``finalize_swap`` deletes each root's ``.old``.
    After a lost claim that directory may be a SECOND builder's rollback
    generation — its only way back — so every delete re-verifies first and a
    refusal stops the rest.

    Unlike the two cases above nothing here is half-published: the whole
    generation is live and identity-verified, so the only residue is a
    leftover ``.old``, exactly the shape the leftovers documentation covers.
    The run still fails loudly and names the roots that kept one.

    Mutation anchor: drop ``verify_held=`` from the ``finalize_swap`` call and
    this goes red — every ``.old`` is deleted and the run reports success.
    """
    _seed_live(store, "nb-1")
    # 3 swaps + the companion's finalize succeed; the session dies before the
    # viz finalize.
    lock = _ScriptedLock([True, True, True, True])
    repository = _Repository(
        storage, store, _Database(lock), _Projections(PIPELINE)
    )

    with pytest.raises(cli.ScaleBuildCliFailure, match="SUCCEEDED"):
        cli.run_import(
            repository,
            "nb-1",
            _full_package(tmp_path),
            allow_library_mismatch=False,
            report=lambda _message: None,
        )

    # Everything is live on the new generation — the publish itself stood.
    assert _published_version(store, "nb-1") == ["nb-1", 7]
    assert json.loads(
        (Path(store.viz_dir("nb-1")) / "manifest.json").read_text()
    ) == {"generation": "new"}
    companion = Path(store.source_partition_dir("nb-1"))
    assert not Path(f"{companion}.old").exists(), (
        "the finalize that ran under a held claim did its job"
    )
    for name in ("kg_viz", cli.MAIN_ROOT):
        root = cli.artifact_roots(store, "nb-1")[name]
        assert Path(f"{root}.old").exists(), (
            "a .old must be LEFT, not deleted, once the claim is gone"
        )


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


def test_export_refuses_a_same_version_companion_from_another_build(
    repository, store, tmp_path
):
    """P1, codex PR#643 R26: the live pair agrees on ``version`` and still
    belongs to two different builds — the shape a publish interrupted between
    its two roots leaves behind on a same-version republish. Exporting it
    would package a pair the serving side already refuses to open.

    Mutation anchor: drop the ``build_generation_mismatch`` check from
    ``companion_generation_error`` and this export succeeds.
    """
    _seed_live(store, "nb-1")
    _write_manifest(
        Path(store.scale_dir("nb-1")),
        _main_manifest(version=["nb-1", 1], build_id="a" * 32),
    )
    _write_manifest(
        Path(store.source_partition_dir("nb-1")),
        {
            "parent_version": ["nb-1", 1],
            "parent_build_id": "b" * 32,
            "published_sources": 1,
        },
    )
    with pytest.raises(cli.ScaleBuildCliError, match="build id mismatch"):
        cli.run_export(
            repository, "nb-1", tmp_path / "out", report=lambda _message: None
        )


def test_export_accepts_a_live_pair_from_the_same_build(repository, store, tmp_path):
    """Negative anchor: the identical shape with ONE build id on both roots is
    the ordinary healthy pair and must export."""
    _seed_live(store, "nb-1")
    _write_manifest(
        Path(store.scale_dir("nb-1")),
        _main_manifest(version=["nb-1", 1], build_id="a" * 32),
    )
    _write_manifest(
        Path(store.source_partition_dir("nb-1")),
        {
            "parent_version": ["nb-1", 1],
            "parent_build_id": "a" * 32,
            "published_sources": 1,
        },
    )
    receipt = cli.run_export(
        repository, "nb-1", tmp_path / "out", report=lambda _message: None
    )
    assert sorted(receipt["roots"]) == sorted(cli.PUBLISH_ORDER)


def test_export_aborts_and_removes_the_package_when_the_claim_is_lost_mid_copy(
    storage, store, tmp_path
):
    """codex PR#643 R20 P1: a multi-GB ``copytree`` can outlive the
    advisory-lock session. Once the claim is gone another builder can legally
    swap a root mid-walk, so the package being written may mix two
    generations — for a same-version rebuild, undetectably. The claim must be
    re-verified after every copied root; a loss removes what this run wrote
    and fails loudly instead of reporting the mixed package as success.

    Mutation anchor: drop the per-copy ``verify_held`` re-check in
    ``run_export`` and this goes red — the export succeeds.
    """
    _seed_live(store, "nb-1")
    lock = _ScriptedLock([False])
    repository = _Repository(storage, store, _Database(lock), _Projections(PIPELINE))
    destination = tmp_path / "out"

    with pytest.raises(cli.ScaleBuildCliFailure, match="was lost while copying"):
        cli.run_export(repository, "nb-1", destination, report=lambda _m: None)

    assert not destination.exists(), (
        "an untrustworthy partial package must not be left on disk"
    )
    assert lock.checks >= 1
    assert (Path(store.scale_dir("nb-1")) / "manifest.json").is_file(), (
        "the live tree stays untouched"
    )


def test_a_manifest_write_failure_removes_the_partial_package(
    repository, store, tmp_path, monkeypatch
):
    """P2, codex PR#643 R31: hashing/writing ``transfer_manifest.json`` can
    fail (I/O error, disk exhaustion, Ctrl-C) after every root was copied;
    without cleanup the destination stays non-empty and the documented
    re-run is refused until an operator hand-deletes it.

    Mutation anchor: drop the cleanup around the post-claim manifest write
    and this goes red — the copied roots survive.
    """
    _seed_live(store, "nb-1")
    destination = tmp_path / "out"

    def failing(_destination, _exported, _report):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cli, "write_transfer_manifest", failing)
    with pytest.raises(OSError, match="No space left"):
        cli.run_export(
            repository, "nb-1", destination, report=lambda _m: None
        )

    assert not destination.exists(), (
        "a failed manifest write must not leave a partial package behind"
    )


def test_the_transfer_manifest_is_hashed_after_the_claim_releases(
    repository, store, tmp_path, lock, monkeypatch
):
    """P2, codex PR#643 R31: the manifest pass re-reads every copied byte;
    holding the per-notebook claim through it would block online builds,
    folds and imports for the whole multi-GB hashing run, even though the
    package bytes are already independent of the live tree.

    Mutation anchor: move ``write_transfer_manifest`` back inside
    ``claim_notebook`` and this goes red — the claim is still held when the
    hashing pass starts.
    """
    _seed_live(store, "nb-1")
    real = cli.write_transfer_manifest
    observed: list[bool] = []

    def observing(destination, exported, report):
        observed.append(lock.released)
        return real(destination, exported, report)

    monkeypatch.setattr(cli, "write_transfer_manifest", observing)
    receipt = cli.run_export(
        repository, "nb-1", tmp_path / "out", report=lambda _m: None
    )

    assert receipt["roots"]
    assert observed == [True], (
        "the claim must already be released when the hashing pass runs"
    )


def test_export_refuses_a_companion_with_no_readable_manifest(
    repository, store, tmp_path
):
    """codex PR#643 R16 P2: a PRESENT live companion whose manifest.json is
    missing used to read as "no companion" here — export copied the broken
    directory verbatim and reported success, producing a package this same
    CLI's import validation necessarily rejects.

    Mutation anchor: make ``companion_generation_error`` treat an unreadable
    manifest as ``None`` again and this goes red — the export succeeds and
    ``validate_import_package`` refuses its output.
    """
    _seed_live(store, "nb-1")
    (Path(store.source_partition_dir("nb-1")) / "manifest.json").unlink()
    with pytest.raises(cli.ScaleBuildCliError, match="manifest.json"):
        cli.run_export(
            repository, "nb-1", tmp_path / "out", report=lambda _message: None
        )
    assert not (tmp_path / "out").exists(), "refused before anything is copied"


def test_export_refuses_a_live_viz_root_the_serving_loader_rejects(
    repository, store, tmp_path
):
    """Same defect one root over (R15 closed the import side): a live
    ``kg_viz`` the serving-side ``load_viz_index`` reads as ``None`` would be
    copied verbatim into a package import now refuses.

    Mutation anchor: drop the viz probe in ``run_export`` and this goes red.
    """
    _seed_live(store, "nb-1")
    viz_dir = Path(store.viz_dir("nb-1"))
    (viz_dir / "viz.npz").unlink()
    with pytest.raises(cli.ScaleBuildCliError, match="viz"):
        cli.run_export(
            repository, "nb-1", tmp_path / "out", report=lambda _message: None
        )
    assert not (tmp_path / "out").exists(), "refused before anything is copied"


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


def test_swap_refuses_to_delete_the_sole_surviving_old_when_live_is_absent(
    tmp_path,
):
    """P1, codex PR#643 R9: a previous swap that was interrupted BETWEEN its
    two renames leaves ``live`` absent and ``.old`` as the ONLY surviving
    generation. The old pre-clean unconditionally deleted ``.old`` before
    even attempting the ``temporary -> live`` rename that would replace it —
    if that rename then failed, both generations were gone. The swap must
    refuse this recovery state outright instead of guessing.

    Mutation anchor: drop the ``if not os.path.exists(out_dir): raise ...``
    branch (restoring the old unconditional pre-clean) and this goes red —
    ``.old`` disappears instead of the refusal firing.
    """
    live = tmp_path / "kg_index"
    old = Path(str(live) + ".old")
    old.mkdir()
    (old / "marker").write_text("recovered", encoding="utf-8")
    temporary = tmp_path / "kg_index.tmp-token"
    temporary.mkdir()
    (temporary / "marker").write_text("new", encoding="utf-8")

    with pytest.raises(ScaleArtifactSwapRefused) as excinfo:
        ScaleArtifactStore.swap_staging_directory(live, temporary)

    message = str(excinfo.value)
    assert "mv" in message
    assert str(old) in message
    assert str(live) in message
    assert str(temporary) in message
    # Nothing was renamed or deleted: .old is still the only surviving
    # generation, and the staged build is untouched.
    assert not live.exists()
    assert old.is_dir()
    assert (old / "marker").read_text(encoding="utf-8") == "recovered"
    assert temporary.is_dir()
    assert (temporary / "marker").read_text(encoding="utf-8") == "new"


def test_swap_with_keep_old_leaves_old_on_disk_and_returns_preserved(tmp_path):
    """P1, codex PR#643 R8: ``keep_old=True`` is what lets a caller decide,
    AFTER the fact, whether a publish should stand — that only works if the
    previous generation is actually still there to roll back to."""
    live = tmp_path / "kg_index"
    live.mkdir()
    (live / "marker").write_text("old", encoding="utf-8")
    temporary = tmp_path / "kg_index.tmp-token"
    temporary.mkdir()
    (temporary / "marker").write_text("new", encoding="utf-8")

    preserved = ScaleArtifactStore.swap_staging_directory(
        live, temporary, keep_old=True
    )

    assert preserved is True
    assert (live / "marker").read_text(encoding="utf-8") == "new"
    assert Path(str(live) + ".old").is_dir(), (
        "keep_old=True must leave .old on disk instead of deleting it"
    )


def test_rollback_swap_restores_a_replacing_publish(tmp_path):
    """P1, codex PR#643 R8: rolling back a publish that replaced an existing
    generation must restore that exact previous live directory and put the
    rejected build back at its own staging name — the shape a swap leaves
    when ``preserved`` is True."""
    live = tmp_path / "kg_index"
    live.mkdir()
    (live / "marker").write_text("old", encoding="utf-8")
    temporary = tmp_path / "kg_index.tmp-token"
    temporary.mkdir()
    (temporary / "marker").write_text("new", encoding="utf-8")

    preserved = ScaleArtifactStore.swap_staging_directory(
        live, temporary, keep_old=True
    )
    assert preserved is True

    ScaleArtifactStore.rollback_swap(live, temporary, preserved)

    assert (live / "marker").read_text(encoding="utf-8") == "old", (
        "the previous generation must be exactly restored"
    )
    assert not Path(str(live) + ".old").exists()
    assert (temporary / "marker").read_text(encoding="utf-8") == "new", (
        "the rejected build must reappear at its own staging name"
    )


def test_rollback_swap_restores_a_first_time_publish(tmp_path):
    """P1, codex PR#643 R8: a first-ever publish has no ``.old`` — rollback
    must leave the notebook with no live directory at all, exactly as before
    that publish, the shape a swap leaves when ``preserved`` is False."""
    live = tmp_path / "kg_index"
    temporary = tmp_path / "kg_index.tmp-token"
    temporary.mkdir()
    (temporary / "marker").write_text("new", encoding="utf-8")

    preserved = ScaleArtifactStore.swap_staging_directory(
        live, temporary, keep_old=True
    )
    assert preserved is False
    assert not Path(str(live) + ".old").exists()

    ScaleArtifactStore.rollback_swap(live, temporary, preserved)

    assert not live.exists(), "no live directory existed before this publish"
    assert (temporary / "marker").read_text(encoding="utf-8") == "new"


def test_finalize_swap_deletes_old_only_when_preserved(tmp_path):
    live = tmp_path / "kg_index"
    live.mkdir()
    old = Path(str(live) + ".old")
    old.mkdir()

    ScaleArtifactStore.finalize_swap(live, False)
    assert old.is_dir(), "preserved=False must not touch an unrelated .old"

    ScaleArtifactStore.finalize_swap(live, True)
    assert not old.exists()


def test_finalize_swap_skips_the_claim_check_on_a_first_ever_publish(tmp_path):
    """codex PR#643 R13 follow-up P2: ``preserved`` False means a first-ever
    publish left no ``.old``, so there is no destructive cleanup for
    ``verify_held`` to protect. A lock session that died AFTER the roots were
    fully published and identity-verified must not turn that no-op into a
    ``ScaleBuildLockLost`` — the CLI would exit 1 and falsely report leftover
    ``.old`` directories that never existed.

    Mutation anchor: move the ``preserved`` early-return back below the
    ``verify_held`` check and this goes red.
    """
    live = tmp_path / "kg_index"
    live.mkdir()
    calls = []

    def dead_claim() -> bool:
        calls.append(True)
        return False

    ScaleArtifactStore.finalize_swap(live, False, verify_held=dead_claim)
    assert calls == [], "no .old to delete means no claim check at all"

    with pytest.raises(ScaleBuildLockLost):
        ScaleArtifactStore.finalize_swap(live, True, verify_held=dead_claim)
    assert calls == [True], "a real cleanup must still verify the claim"


def test_retire_clears_a_stale_old_before_renaming_live(tmp_path):
    """P2-a, codex PR#643 R13: an earlier interrupted cleanup can leave both
    a populated ``live`` and a leftover ``.old`` on disk — the same safe
    "live present, .old present" shape ``swap_staging_directory`` already
    self-heals. Retirement's target IS that ``.old`` path, so plain
    ``os.rename`` would otherwise fail every time with "destination not
    empty" until an operator cleaned it up by hand. This must clear the
    stale ``.old`` first, then retire ``live`` into a fresh one.

    Mutation anchor: drop the pre-clean ``shutil.rmtree(old_dir)`` this test
    exercises and this goes red — ``os.rename`` raises ``OSError`` (the
    destination directory is not empty).
    """
    live = tmp_path / "kg_viz"
    live.mkdir()
    (live / "marker").write_text("live", encoding="utf-8")
    old = Path(str(live) + ".old")
    old.mkdir()
    (old / "marker").write_text("stale", encoding="utf-8")

    preserved = ScaleArtifactStore.retire_live_directory(live)

    assert preserved is True
    assert not live.exists()
    assert old.is_dir()
    assert (old / "marker").read_text(encoding="utf-8") == "live", (
        "the new .old must be the just-retired live directory, not the "
        "stale one that was pre-cleaned"
    )


def test_retire_reverifies_the_claim_after_the_stale_old_preclean(tmp_path):
    """P2-a, codex PR#643 R13: mirrors
    ``test_swap_reverifies_the_claim_after_the_stale_old_preclean`` — the
    pre-clean ``rmtree`` of a large stale ``.old`` can itself take tens of
    seconds, long enough for the claim's lock session to die and a second
    builder to legitimately take over. Retirement must re-check
    ``verify_held`` right after the pre-clean, before its own rename, or it
    would go on to rename over the new owner's generation.

    Mutation anchor: drop the second ``verify_held`` re-check that follows
    the pre-clean and this goes red — retirement proceeds despite the claim
    already being gone.
    """
    live = tmp_path / "kg_viz"
    live.mkdir()
    (live / "marker").write_text("live", encoding="utf-8")
    old = Path(str(live) + ".old")
    old.mkdir()
    (old / "marker").write_text("stale", encoding="utf-8")

    results = iter([True, False])
    seen: list[bool] = []

    def verify_held() -> bool:
        value = next(results)
        seen.append(value)
        return value

    with pytest.raises(ScaleBuildLockLost) as excinfo:
        ScaleArtifactStore.retire_live_directory(live, verify_held=verify_held)

    assert "nothing was renamed" in str(excinfo.value)
    assert seen == [True, False], (
        "verify_held must be called once at entry and once more after the "
        "stale .old pre-clean"
    )
    # Nothing was RENAMED: live is exactly as it was before this call.
    assert live.is_dir()
    assert (live / "marker").read_text(encoding="utf-8") == "live"


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


def test_the_interrupt_handler_only_records_and_reports_after_the_window():
    """P1, codex PR#643 R30: a signal handler runs at an arbitrary bytecode
    boundary — calling ``report`` from inside it can hit Python's
    reentrant-I/O protection when the interrupt lands while the guarded code
    is itself writing to the same stream, and an exception escaping the
    handler aborts the very rename sequence the guard protects. The handler
    must only record; the notice is emitted in ``__exit__``, where a stream
    hiccup can no longer abort anything.

    Mutation anchor: move the ``report`` call back into ``_handle`` and this
    goes red — the reentrant stream blows up inside the handler.
    """
    messages: list[str] = []

    def reentrant_stream(message: str) -> None:
        messages.append(message)
        raise RuntimeError("reentrant call inside buffered writer")

    guard = SwapInterruptGuard(reentrant_stream, reraise=False)
    with guard:
        # The interrupt lands mid-window; the handler must not touch the
        # (currently reentrant) stream at all.
        guard._handle(None, None)
        assert guard.interrupted is True
        assert messages == [], "the handler itself must not report"
    # The deferred notice was attempted after the window — and its failure
    # was swallowed rather than replacing the block's own outcome.
    assert len(messages) == 1
    assert guard.completed is True


def test_a_copy_failure_mid_export_removes_the_partial_package(
    repository, store, tmp_path, monkeypatch
):
    """P2, codex PR#643 R30: an I/O error (or Ctrl-C) inside ``copytree``
    used to leave the partially written root on disk; the documented re-run
    then refused on "destination is not empty" until an operator hand-deleted
    the fragment. Any pre-success exception now gets the same cleanup as the
    lost-claim path, and the original failure propagates untouched.

    Mutation anchor: drop the ``except BaseException`` cleanup around the
    copy loop and this goes red — the destination survives with a fragment.
    """
    _seed_live(store, "nb-1")
    destination = tmp_path / "out"
    real_copytree = shutil.copytree
    calls = {"n": 0}

    def failing(src, dst, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "No space left on device")
        return real_copytree(src, dst, **kwargs)

    monkeypatch.setattr(shutil, "copytree", failing)
    with pytest.raises(OSError, match="No space left"):
        cli.run_export(
            repository, "nb-1", destination, report=lambda _m: None
        )

    assert not destination.exists(), (
        "everything this invocation wrote must be removed so the documented "
        "re-run is not refused on a non-empty destination"
    )


def test_old_directory_deletion_runs_outside_the_sigint_mask(
    store, tmp_path, monkeypatch
):
    """P2, codex PR#643 R8: only the two renames — the steps that can leave a
    notebook with no live index — run inside ``SwapInterruptGuard``. Deleting
    the (potentially multi-GB) previous generation must not extend that
    masked window. Structural check: by the time ``.old`` is removed, the
    installed SIGINT handler must no longer be the guard's.

    Mutation anchor: move the ``rmtree(old_dir, ...)`` call back inside the
    ``with SwapInterruptGuard():`` block and this goes red."""
    live = tmp_path / "root"
    live.mkdir()
    (live / "marker").write_text("old", encoding="utf-8")
    staged = store.prepare_staging_directory(live, "guard-token")
    (staged / "marker").write_text("new", encoding="utf-8")

    previous = signal.getsignal(signal.SIGINT)
    real_rmtree = shutil.rmtree
    observed: list[bool] = []

    def observing_rmtree(path, *args, **kwargs):
        if str(path) == str(live) + ".old":
            handler = signal.getsignal(signal.SIGINT)
            observed.append(
                isinstance(getattr(handler, "__self__", None), SwapInterruptGuard)
            )
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(
        "app.repositories.filesystem.scale_artifact_store.shutil.rmtree",
        observing_rmtree,
    )
    ScaleArtifactStore.swap_staging_directory(live, staged)

    assert observed == [False], (
        "the .old rmtree must run after the guard's handler has already been "
        "uninstalled, not while SIGINT is still masked"
    )
    assert signal.getsignal(signal.SIGINT) is previous


def test_stale_old_pre_clean_runs_outside_the_sigint_mask(
    store, tmp_path, monkeypatch
):
    """P2, codex PR#643 R9: a leftover ``.old`` from an EARLIER swap's own
    cleanup (that swap finished both renames but never got to, or lost the
    race to, delete its own ``.old``) is pure cruft once ``live`` is present
    — deleting it here cannot leave the notebook without a live index, so it
    must be removed BEFORE ``SwapInterruptGuard`` is even entered, not inside
    it. Structural check, same technique as
    ``test_old_directory_deletion_runs_outside_the_sigint_mask``: at the
    moment this PRE-clean rmtree runs, the installed SIGINT handler must not
    be the guard's (the guard does not exist yet at that point).

    Mutation anchor: move the pre-clean ``shutil.rmtree(old_dir)`` call back
    inside the ``with guard:`` block and this goes red."""
    live = tmp_path / "root"
    live.mkdir()
    (live / "marker").write_text("live", encoding="utf-8")
    old = Path(str(live) + ".old")
    old.mkdir()
    (old / "marker").write_text("stale", encoding="utf-8")
    staged = store.prepare_staging_directory(live, "guard-token")
    (staged / "marker").write_text("new", encoding="utf-8")

    previous = signal.getsignal(signal.SIGINT)
    real_rmtree = shutil.rmtree
    observed: list[bool] = []

    def observing_rmtree(path, *args, **kwargs):
        if str(path) == str(old):
            handler = signal.getsignal(signal.SIGINT)
            observed.append(
                isinstance(getattr(handler, "__self__", None), SwapInterruptGuard)
            )
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(
        "app.repositories.filesystem.scale_artifact_store.shutil.rmtree",
        observing_rmtree,
    )
    ScaleArtifactStore.swap_staging_directory(live, staged)

    # The FIRST rmtree of ``.old`` is the pre-clean of the stale cruft found
    # before the swap even started; it must run unmasked. (A second call may
    # follow for the post-swap cleanup of the generation this swap itself
    # just replaced — R8's already-covered window — also unmasked.)
    assert observed, "the stale .old must actually be deleted"
    assert observed[0] is False, (
        "the pre-clean rmtree must run before the guard installs its "
        "handler, not while SIGINT is masked"
    )
    assert signal.getsignal(signal.SIGINT) is previous
    assert (live / "marker").read_text(encoding="utf-8") == "new"


def test_swap_reverifies_the_claim_after_the_stale_old_preclean(tmp_path):
    """P1, codex PR#643 R10: the pre-clean ``rmtree`` of a large stale
    ``.old`` can itself take tens of seconds — long enough for the claim's
    PostgreSQL lock session to die mid-delete. A second builder could then
    legitimately acquire the now-free claim and start publishing; this swap
    must re-check ``verify_held`` right after the pre-clean, before its own
    first rename, or it would go on to rename over the new owner's
    generation.

    Mutation anchor: drop the second ``verify_held`` re-check that follows
    the pre-clean ``shutil.rmtree(old_dir)`` and this goes red — the swap
    proceeds to rename despite the claim already being gone.
    """
    live = tmp_path / "kg_index"
    live.mkdir()
    (live / "marker").write_text("live", encoding="utf-8")
    old = Path(str(live) + ".old")
    old.mkdir()
    (old / "marker").write_text("stale", encoding="utf-8")
    temporary = tmp_path / "kg_index.tmp-token"
    temporary.mkdir()
    (temporary / "marker").write_text("new", encoding="utf-8")

    results = iter([True, False])
    seen: list[bool] = []

    def verify_held() -> bool:
        value = next(results)
        seen.append(value)
        return value

    with pytest.raises(ScaleBuildLockLost) as excinfo:
        ScaleArtifactStore.swap_staging_directory(
            live, temporary, verify_held=verify_held
        )

    message = str(excinfo.value)
    assert "nothing was published" in message
    assert str(temporary) in message
    assert seen == [True, False], (
        "verify_held must be called once at entry and once more after the "
        "stale .old pre-clean"
    )
    # Nothing was renamed: live is exactly as it was before this call, and
    # the staged build is untouched.
    assert (live / "marker").read_text(encoding="utf-8") == "live"
    assert temporary.is_dir()
    assert (temporary / "marker").read_text(encoding="utf-8") == "new"


def test_swap_does_not_reverify_the_claim_without_a_stale_old_to_clean(
    tmp_path,
):
    """P1, codex PR#643 R10 (negative anchor): when there is no stale
    ``.old``, nothing slow runs between the entry check and the renames, so
    nothing can invalidate the claim in between — ``verify_held`` must be
    called exactly once, not a needless second time.

    Mutation anchor: call ``verify_held`` unconditionally a second time
    (instead of only on the branch that actually ran the pre-clean rmtree)
    and this goes red."""
    live = tmp_path / "kg_index"
    live.mkdir()
    (live / "marker").write_text("live", encoding="utf-8")
    temporary = tmp_path / "kg_index.tmp-token"
    temporary.mkdir()
    (temporary / "marker").write_text("new", encoding="utf-8")

    calls: list[bool] = []

    def verify_held() -> bool:
        calls.append(True)
        return True

    ScaleArtifactStore.swap_staging_directory(
        live, temporary, verify_held=verify_held
    )

    assert calls == [True]


def test_a_real_interrupt_during_old_cleanup_is_honoured_immediately(
    store, tmp_path, monkeypatch
):
    """P2, codex PR#643 R8: Ctrl-C landing while the previous generation is
    being deleted must interrupt right away, not be deferred — the new
    generation is already live and correct by then, so there is nothing left
    for the deferral to protect."""
    live = tmp_path / "root"
    live.mkdir()
    (live / "marker").write_text("old", encoding="utf-8")
    staged = store.prepare_staging_directory(live, "guard-token")
    (staged / "marker").write_text("new", encoding="utf-8")

    real_rmtree = shutil.rmtree

    def interrupting_rmtree(path, *args, **kwargs):
        if str(path) == str(live) + ".old":
            raise KeyboardInterrupt
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(
        "app.repositories.filesystem.scale_artifact_store.shutil.rmtree",
        interrupting_rmtree,
    )

    with pytest.raises(KeyboardInterrupt):
        ScaleArtifactStore.swap_staging_directory(live, staged)

    # The new generation is already live; only the (harmless, inspect-
    # reported) leftover .old is a consequence of the interrupted cleanup.
    assert (live / "marker").read_text(encoding="utf-8") == "new"


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
    shutil.rmtree(live)
    assert cli.publish_started(live) is True, ".old without live is mid-rename"


def test_publish_started_does_not_mistake_first_time_staging(tmp_path):
    """P2, codex PR#643 R7: a root that never had a live directory carries
    the exact shape ``.tmp-<token> present, live absent`` for its ENTIRE
    staging copy. That is normal first-publish staging — not a half-finished
    swap — and inferring publication from it made every first-import
    interrupt skip cleanup and print a recovery pointing at a nonexistent
    ``.old``. Only ``.old`` itself is publish evidence.

    Mutation anchor: restoring the old "tmp present while live absent"
    inference turns this red."""
    live = tmp_path / "kg_index"
    (tmp_path / "kg_index.tmp-abc123").mkdir()
    assert cli.publish_started(live) is False, (
        "first-time staging (no live, no .old) must not read as publishing"
    )
    (tmp_path / "kg_index.tmp").mkdir()
    assert cli.publish_started(live) is False, "legacy shape: same rule"
    (tmp_path / "kg_index.old").mkdir()
    assert cli.publish_started(live) is True, ".old is the only evidence"


def test_first_time_import_interrupt_cleans_its_own_staging(tmp_path):
    """P2, codex PR#643 R7 end-to-end shape: an interrupt during a
    FIRST-TIME import's staging copy must discard this run's own staged
    directories (nothing was renamed; re-import recreates them) instead of
    keeping them behind a misleading ``.old`` recovery message."""
    live = tmp_path / "kg_index"
    staged = tmp_path / "kg_index.tmp-abc123"
    staged.mkdir()
    messages: list[str] = []
    cli.discard_staging_unless_publishing({live: staged}, messages.append)
    assert not staged.exists(), "first-time staging must be cleaned up"
    assert not any(".old" in message for message in messages)


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
