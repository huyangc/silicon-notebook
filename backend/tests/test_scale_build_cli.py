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
import shutil
import signal
from pathlib import Path

import numpy as np
import pytest

from app.repositories.filesystem.scale_artifact_store import ScaleArtifactStore
from app.repositories.scale_build_lock import ScaleBuildLockLost
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
    def __init__(self, identity) -> None:
        self._identity = identity

    def pipeline_identity(self, _notebook_id: str):
        return self._identity


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
        assert not Path(str(root) + ".tmp").exists()
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
        assert not Path(str(root) + ".tmp").exists(), "half a copy must not linger"


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
    staged = Path(str(store.source_partition_dir("nb-1")) + ".tmp")
    assert staged.exists(), "the staged copy is left for the operator"
    assert lost.released is True


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
    assert not Path(str(store.scale_dir("nb-1")) + ".tmp").exists()


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
    assert not Path(str(store.scale_dir("nb-1")) + ".tmp").exists()


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


def test_packaged_migration_count_matches_the_schema_manifest():
    """The ledger preflight is only meaningful if this number is the truth."""
    from app.repositories.postgres.schema_manifest import POSTGRES_SCHEMA_MANIFEST

    assert cli.packaged_migration_count() == (
        POSTGRES_SCHEMA_MANIFEST.postgres_version
    )
