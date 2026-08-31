"""W-CLI T-W2 — the offline scale-index build CLI (``scripts/build_scale_index.py``).

An independent process that builds, exports and imports a notebook's scale
index **beside a live service** against the same database and artifact tree.
The serving process picks a newly published artifact up on its own (per-request
probing, see ``scale_artifact_catalog.load``); no restart, no notification
channel.

Three properties make that safe, and each is enforced here:

1. **Mutual exclusion** — every destructive subcommand takes the per-notebook
   cross-process build claim (T-W1). SQLite has no such claim, so this CLI
   refuses SQLite outright rather than pretending; a single-process deployment
   has no scenario for it anyway.
2. **Schema ownership** — this process does NOT own the schema. It composes the
   repository with ``migrate=False, seed=False`` and verifies, on a bare
   connection before composing anything, that the ledger in the live database
   matches the migrations in this checkout. Migrating would apply DDL the
   running service never asked for; seeding would silently reset the production
   admin credential (``bundle._initialize``'s unconditional ``UPDATE users``).
3. **Atomic publication** — every root is staged in ``{live}.tmp`` and renamed
   into place, and the claim is re-verified in the instant before the first
   rename. ``SIGINT`` is masked across the rename sequence.

Receipts are content-free: notebook ids the operator typed, fixed artifact file
names, sizes, counts and versions. The database URL is never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional

from app.core.config import Settings
from app.core.database_url import database_identity
from app.repositories.filesystem.scale_artifact_store import SwapInterruptGuard
from app.repositories.scale_build_lock import (
    SCALE_BUILD_LOCK_UNAVAILABLE,
    ScaleBuildBusy,
    ScaleBuildLockLost,
)
from app.services.kg.scale_index import (
    MANIFEST_LIBRARY_KEY,
    runtime_library_versions,
)


# A build on a 9M-object library runs for hours; the online default (30s, sized
# for interactive requests) would kill it. Applied to ``Settings`` BEFORE the
# repository is composed: the pool's connection configure/reset callbacks issue
# ``RESET ALL``, so a ``SET`` on a borrowed connection is wiped before it can
# take effect. The pool reads this number once, at construction.
DEFAULT_STATEMENT_TIMEOUT_SECONDS = 86_400

# Publication order for an import. The companion goes first and the main index
# last, so any interruption between renames leaves the *live* index on its
# previous generation. Both partial states are fail-soft rather than wrong: the
# companion's ``parent_version`` gate refuses a companion that does not match
# the live main index, so a mismatched pair degrades to "no companion", never
# to "companion describing a different generation".
PUBLISH_ORDER = ("kg_index_partitions", "kg_viz", "kg_index")

MAIN_ROOT = "kg_index"
COMPANION_ROOT = "kg_index_partitions"

# Artifacts the main root always carries, plus the ones its manifest flags
# promise. Derived from ``kg.scale_index.save_scale_index`` / ``load_scale_index``.
_CORE_FILES = (
    "manifest.json",
    "graph.npz",
    "node_ids.npy",
    "idf.npy",
    "chunk_index.npy",
    "ann_labels.npy",
)
_FLAGGED_FILES = {
    "has_viz": ("viz.npz", "viz_adj.npz"),
    "has_chunk_ann": ("chunk_ann.bin", "chunk_ann_labels.npy"),
    "has_chunk_ann_sources": (
        "chunk_ann_source_names.npy",
        "chunk_ann_source_codes.npy",
        "chunk_ann_source_counts.npy",
    ),
    "has_relation_ann": ("relation_ann.bin", "relation_ann_labels.npy"),
}
# manifest count key → the .npy whose row count must equal it.
_COUNTED_ARRAYS = {
    "n_nodes": "node_ids.npy",
    "n_ann": "ann_labels.npy",
    "n_chunk_ann": "chunk_ann_labels.npy",
    "n_relation_ann": "relation_ann_labels.npy",
}


class ScaleBuildCliError(RuntimeError):
    """Refused before any work was attempted. Operator-facing; exit code 2."""


class ScaleBuildCliFailure(RuntimeError):
    """The command was attempted and failed. Operator-facing; exit code 1."""


class _ImportPipelineIdentityDrifted(RuntimeError):
    """Internal to ``run_import`` (codex PR#643 R5 P1): the live pipeline
    identity changed while this package's roots were being staged. Caught
    separately from the generic staging-failure handler so the staged copies
    are left on disk for inspection instead of being discarded — the same
    "nothing published, staging preserved" contract ``ScaleBuildLockLost``
    already gets below."""


# ─────────────────────────────────────────────────────── migration ledger ──

def packaged_migrations() -> tuple:
    """The migrations this checkout carries, in version order."""
    from app.repositories.postgres import migrator as migrator_module

    return migrator_module.load_migrations(
        Path(migrator_module.__file__).with_name("migrations")
    )


def packaged_migration_count() -> int:
    """How many migrations this checkout carries."""
    return len(packaged_migrations())


def verify_migration_ledger(database_url: str) -> tuple[int, int]:
    """Refuse to run against a database whose schema is not this code's schema.

    The whole premise of an off-host build is that the builder and the serving
    process agree on what the data means. The ledger is the cheapest hard
    evidence of that: an off-host checkout one migration behind (or ahead) reads
    columns the live service does not have, or misses ones it does, and the
    artifact it produces is quietly wrong rather than loudly broken.

    The count alone is not that evidence (codex W-CLI R1 P2-8). Two checkouts
    can carry the same NUMBER of migrations and different SQL — a rebased
    branch, a cherry-pick, an edited file — and the ledger already stores the
    per-migration checksum the service's own migrator validates on startup. So
    this compares every recorded checksum, not just ``max(version)``; one extra
    column on a query that was already being issued.

    Neither is ``max(version)`` evidence that every migration in between was
    actually applied (codex PR#643 R1 P2): a ledger recording ``1, 3, ...,
    expected`` — version 2 missing — has ``max(version) == expected`` and
    every recorded checksum matches, so the old count-plus-checksums check
    passed it, even though the repository's own migrator
    (``app.repositories.postgres.migrator``) treats a gapped ledger as
    invalid and refuses to run against it. This compares the exact SET of
    recorded versions against ``1..expected`` — no gaps, no duplicates, no
    stragglers above ``expected`` — before trusting any checksum in it.

    Read on a bare connection, before the repository (and its pool) exists, so a
    refusal costs one connection and leaves no trace in the live database.
    """
    import psycopg

    migrations = packaged_migrations()
    expected = len(migrations)
    # Registered, not fixed (codex W-CLI R1 N4): this is the one connection in
    # the command that is NOT wrapped by the pool's credential-safe error
    # rendering, so a psycopg connection failure here can surface a message
    # naming the host/user (never the password — libpq redacts it, and this
    # process is the operator's own shell). Routing it through
    # ``PostgresDatabase`` would mean composing the pool before the very check
    # that decides whether composing is safe.
    with psycopg.connect(database_url, autocommit=True) as connection:
        present = connection.execute(
            "SELECT to_regclass('silicon_schema_migrations') IS NOT NULL"
        ).fetchone()[0]
        if not present:
            raise ScaleBuildCliError(
                "the target database has no silicon_schema_migrations ledger; "
                "this CLI never migrates — start the service (or a maintenance "
                "CLI) against it once before building here"
            )
        rows = connection.execute(
            "SELECT version, checksum FROM silicon_schema_migrations "
            "ORDER BY version"
        ).fetchall()
    recorded_versions = sorted(int(row[0]) for row in rows)
    applied = recorded_versions[-1] if recorded_versions else 0
    if recorded_versions != list(range(1, expected + 1)):
        raise ScaleBuildCliError(
            f"migration ledger mismatch: the database records versions "
            f"{recorded_versions}, this checkout expects exactly "
            f"1..{expected} with no gaps or duplicates. Check out the exact "
            "revision the service is running (this CLI deliberately does not "
            "migrate)."
        )
    for version, checksum in ((int(row[0]), row[1]) for row in rows):
        if checksum != migrations[version - 1].checksum:
            raise ScaleBuildCliError(
                f"migration checksum mismatch at version {version}: the "
                "database was migrated by different SQL than this checkout "
                "carries under the same version number. Check out the exact "
                "revision the service is running."
            )
    return applied, expected


# ────────────────────────────────────────────────────────── composition ──

def resolve_settings(
    statement_timeout_seconds: int = DEFAULT_STATEMENT_TIMEOUT_SECONDS,
) -> Settings:
    """Production settings with the offline statement timeout applied.

    ``Settings()`` is read from the operator's environment/``.env`` — the
    documentation requires running this with the *production* env file, because
    the storage root, embedding dimension and pipeline configuration all have to
    match the service whose artifacts are being replaced.
    """
    if statement_timeout_seconds <= 0:
        raise ScaleBuildCliError("--statement-timeout-seconds must be positive")
    settings = Settings()
    return settings.model_copy(
        update={"postgres_statement_timeout_seconds": int(statement_timeout_seconds)}
    )


def require_postgres(settings: Settings) -> None:
    if database_identity(settings.database_url).scheme != "postgresql":
        raise ScaleBuildCliError(
            "this CLI requires PostgreSQL. A SQLite deployment is single-process "
            "by construction: it has no cross-process build claim, so an offline "
            "builder could not be excluded from the serving process and both "
            "would race on the same artifact directory. Build in-process instead."
        )


@contextmanager
def open_scale_build_repository(settings: Settings) -> Iterator[object]:
    """Compose the live-service repository WITHOUT owning its schema.

    The seats come from the same ``bootstrap`` helper the server uses, so the
    indexing pipeline this build runs under is the one the service publishes.
    The PostgreSQL adapter is named directly (rather than going through the
    backend-neutral ``create_repository`` selector) because the schema-ownership
    seam is PostgreSQL-only and ``require_postgres`` has already run.
    """
    from app.bootstrap import (
        application_extension_runtime,
        application_repository_hosts,
        prime_extension_admission,
    )
    from app.repositories.postgres.repository import PostgresRepository

    runtime = application_extension_runtime()
    repository = PostgresRepository(
        settings,
        migrate=False,
        seed=False,
        **application_repository_hosts(runtime),  # type: ignore[arg-type]
    )
    # Closes the repository itself if priming fails; do not double-close.
    prime_extension_admission(repository)
    try:
        yield repository
    finally:
        repository.close()


def require_safe_notebook_id(store, notebook_id: str) -> None:
    """Refuse an id that would aim this command's ``rmtree``s somewhere else.

    The notebook id is an operator-typed string that becomes the last path
    segment of three directories this CLI creates, renames and — via
    ``prepare_staging_directory`` — ``rmtree``s. A separator or a ``..`` in it
    escapes the storage tree entirely: ``--notebook ../../etc`` would have
    ``{storage}/kg_index/../../etc.tmp`` removed recursively (codex W-CLI R1
    P2-5). Two independent checks, because either alone can be argued around:
    the syntactic one is what an operator can read off the error message, and
    the containment one holds even if the layout, or a symlinked storage root,
    ever changes.
    """
    separators = [os.sep, "/", "\0"]
    if os.altsep:
        separators.append(os.altsep)
    if (
        not notebook_id
        or notebook_id in (".", "..")
        or any(marker in notebook_id for marker in separators)
    ):
        raise ScaleBuildCliError(
            f"{notebook_id!r} is not a usable notebook id: it must be a single "
            "path segment (no separators, not '.' or '..')"
        )
    root = Path(str(store.settings.storage_dir)).resolve()
    for live in (
        store.scale_dir(notebook_id),
        store.viz_dir(notebook_id),
        store.source_partition_dir(notebook_id),
    ):
        if not Path(str(live)).resolve().is_relative_to(root):
            raise ScaleBuildCliError(
                f"the artifact directory for {notebook_id!r} resolves outside "
                f"the storage root {root}; refusing to touch it"
            )


def artifact_roots(store, notebook_id: str) -> dict[str, Path]:
    """The three independent on-disk roots one notebook's index spans.

    Every subcommand starts here, which is why the id guard lives here: it is
    the one function all four paths must call before naming a directory.
    """
    require_safe_notebook_id(store, notebook_id)
    return {
        MAIN_ROOT: Path(str(store.scale_dir(notebook_id))),
        "kg_viz": Path(str(store.viz_dir(notebook_id))),
        COMPANION_ROOT: Path(str(store.source_partition_dir(notebook_id))),
    }


@contextmanager
def claim_notebook(repository, notebook_id: str) -> Iterator[object]:
    """Hold this notebook's cross-process build claim for the whole command.

    ``build`` does NOT use this — the runtime's own ``build``/``fold`` take the
    claim and hand it to the swap's re-verification, and a second claim from
    this process's own separate lock session would simply refuse itself.
    """
    database = repository._runtime.database  # noqa: SLF001 — CLI composition root
    handle = database.try_scale_build_lock(notebook_id)
    if handle is SCALE_BUILD_LOCK_UNAVAILABLE:
        raise ScaleBuildCliFailure(
            f"the scale-build claim for {notebook_id} could not be evaluated: "
            "this process has no dedicated lock session left. Nothing was "
            "changed; retry once the other builds on this host finish."
        )
    if handle is None:
        raise ScaleBuildCliFailure(
            f"the scale-build claim for {notebook_id} is held by another process "
            "(a service worker, a maintenance CLI, or another run of this tool). "
            "Nothing was changed; retry once it finishes."
        )
    try:
        yield handle
    finally:
        handle.release()


# ─────────────────────────────────────────────────── interrupt handling ──

# ``SwapInterruptGuard`` (imported above) is defined beside the publish
# primitive it protects: a build's publish step is reached only through the
# store, so masking SIGINT from here would have covered ``import`` and nothing
# else. This module still wraps the multi-root import publish in one guard —
# nesting is a no-op, so the outer one owns the whole sequence.


def publish_started(live) -> bool:
    """Filesystem evidence that this root's rename sequence had BEGUN.

    The ONLY trustworthy evidence is ``{live}.old``: it exists strictly
    between the two renames of a replacing publish (and is removed as the
    last step), so its presence means the staged directory may already be
    the only copy of a generation — never delete it as "abandoned staging"
    (codex W-CLI R1 B1). The earlier heuristic "a staging sibling present
    while ``live`` is absent" was WRONG (P2, codex PR#643 R7): that shape is
    the normal state of a FIRST-TIME import for this root's entire staging
    copy — ``live`` never existed, the ``.tmp-<token>`` tree is being
    filled — and a first-ever publish is a single atomic rename with no
    half state at all (either ``live`` appeared, or the tree is still plain
    staging that this run may discard). Inferring publication from it made
    every first-publish interrupt skip cleanup and print a recovery
    instruction pointing at a nonexistent ``.old``.
    """
    return os.path.exists(str(live) + ".old")


def discard_staging(paths, report: Callable[[str], None]) -> None:
    """Remove the staging directories this run created, and say which."""
    for path in paths:
        try:
            if os.path.exists(path):
                shutil.rmtree(path, ignore_errors=True)
                report(f"removed staged build directory {path}")
        except OSError as error:  # noqa: PERF203 - one report per root
            report(f"could not remove staged build directory {path}: {error!r}")


def discard_staging_unless_publishing(
    roots, report: Callable[[str], None]
) -> None:
    """Interrupt cleanup that can tell staging from a half-finished publish.

    ``roots`` maps each live directory to the EXACT staging directory this
    run created for it — the ``Path`` ``prepare_staging_directory`` returned,
    a claim-unique ``{live}.tmp-<claim_token>`` (P1, codex PR#643 R1) — never
    a guessed name. If ANY root shows publish evidence the whole cleanup is
    skipped and the recovery is printed instead: a publish sequence that was
    cut in half has the previous generation in ``{live}.old`` and the new one
    in the staged directory, and deleting either is the data loss.

    Only a caller that held its OWN claim throughout staging AND publish can
    supply an accurate ``roots`` mapping here — currently only ``import``
    (see ``run_build``, which cannot).
    """
    interrupted_publish = [live for live in roots if publish_started(live)]
    if not interrupted_publish:
        discard_staging(list(roots.values()), report)
        return
    for live in interrupted_publish:
        report(
            f"the publish of {live} was interrupted between renames; nothing "
            f"was deleted. The previous generation is at {live}.old (restore "
            f"with `mv {live}.old {live}`) and this run's build is at "
            f"{roots[live]}"
        )
    report(
        "staged directories are kept for every root of this notebook; run "
        "`inspect` once the tree is restored"
    )


def report_interrupted_build(
    roots, notebook_id: str, mode: str, report: Callable[[str], None]
) -> None:
    """``build``'s interrupt diagnostics — never deletes anything.

    Unlike ``import`` (``discard_staging_unless_publishing`` above), this
    command holds no claim of its own: ``build``/``fold`` acquire and release
    the runtime's claim internally (T-W1's ``_claim_scale_build``), entirely
    before this frame regains control on ``KeyboardInterrupt``. So it never
    learns the ``claim_token`` its own staging directory — if it staged one
    at all — was suffixed with, and cannot safely name, let alone delete, a
    ``{live}.tmp-<claim_token>`` this attempt may have left behind (P1, codex
    PR#643 R1). Only the claim-INDEPENDENT ``.old`` signal is trustworthy
    here, so a mid-publish interrupt is still reported precisely; anything
    else defers entirely to ``inspect`` and an operator's judgment.
    """
    interrupted_publish = [live for live in roots if publish_started(live)]
    if interrupted_publish:
        for live in interrupted_publish:
            report(
                f"the publish of {live} was interrupted between renames; "
                f"nothing was deleted. The previous generation is at "
                f"{live}.old (restore with `mv {live}.old {live}`) and this "
                f"run's staged build is the `{live}.tmp-*` directory beside it"
            )
        return
    report(
        f"{mode} for {notebook_id} was interrupted; this command holds no "
        "claim of its own here, so it cannot identify its own staged "
        "`{live}.tmp-<claim_token>` directory. Run `inspect` to see any "
        "leftover under `leftovers` and remove it once you have confirmed no "
        "other builder still owns it."
    )


# ──────────────────────────────────────────────────────────── validation ──

def _read_manifest(directory: Path) -> Optional[dict]:
    path = directory / "manifest.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ScaleBuildCliError(
            f"{path} is not readable JSON ({error!r})"
        ) from None
    if not isinstance(data, dict):
        raise ScaleBuildCliError(f"{path} is not a JSON object")
    return data


def npy_row_count(path: Path) -> Optional[int]:
    """Row count from a ``.npy`` HEADER — no array data, no unpickling.

    Object arrays (``node_ids``/``ann_labels``) would need ``allow_pickle`` to
    materialize, which is an arbitrary-code-execution surface; the header is
    plain and tells us everything the count check needs.
    """
    from numpy.lib import format as npy_format

    try:
        with open(path, "rb") as handle:
            version = npy_format.read_magic(handle)
            if version == (1, 0):
                shape, _fortran, _dtype = npy_format.read_array_header_1_0(handle)
            elif version == (2, 0):
                shape, _fortran, _dtype = npy_format.read_array_header_2_0(handle)
            else:
                return None
    except (OSError, ValueError):
        return None
    return int(shape[0]) if shape else 0


def _graph_shape(path: Path) -> Optional[tuple[int, int]]:
    """``graph.npz``'s declared matrix shape, read without the data members."""
    import numpy as np

    try:
        with np.load(path) as archive:
            if "shape" not in archive.files:
                return None
            shape = tuple(int(value) for value in archive["shape"])
    except (OSError, ValueError, KeyError):
        return None
    return shape if len(shape) == 2 else None


def artifact_inventory_error(directory: Path, manifest: dict) -> Optional[str]:
    """Missing files or manifest counts that disagree with the arrays on disk.

    Header-only: the check is O(number of files), not O(index size), so it runs
    in milliseconds on a multi-GB package.
    """
    required = list(_CORE_FILES)
    if int(manifest.get("n_ann") or 0) > 0:
        required.append("ann.bin")
    for flag, files in _FLAGGED_FILES.items():
        if manifest.get(flag):
            required.extend(files)
    missing = [name for name in required if not (directory / name).is_file()]
    if missing:
        return f"the package is missing {', '.join(sorted(missing))}"

    for key, filename in _COUNTED_ARRAYS.items():
        expected = manifest.get(key)
        if not isinstance(expected, int) or isinstance(expected, bool):
            continue
        path = directory / filename
        if not path.exists():
            continue
        actual = npy_row_count(path)
        if actual is not None and actual != expected:
            return (
                f"manifest.{key}={expected} but {filename} holds {actual} rows"
            )
    nodes = manifest.get("n_nodes")
    shape = _graph_shape(directory / "graph.npz")
    if isinstance(nodes, int) and not isinstance(nodes, bool) and shape is not None:
        if shape != (nodes, nodes):
            return (
                f"manifest.n_nodes={nodes} but graph.npz is {shape[0]}×{shape[1]}"
            )
    return None


def _require_package_belongs_to(
    manifest: dict, main: Path, notebook_id: str, known_source_ids
) -> None:
    """Refuse a package that is not provably this notebook's index.

    Nothing else in the validation notices a typo in ``--notebook``: pipeline
    identity, dim and hnswlib are all DEPLOYMENT-wide facts, identical for every
    library on the host. So ``import --notebook nb-B --from <nb-A's pack>``
    passed every gate, published library A's artifacts into library B's
    directories, and B started serving them — the retrieval side reads the
    manifest that arrived and never compares it against the database
    (codex W-CLI R1 P1-2).

    Two bindings, in order of strength:

    1. ``manifest["notebook_id"]``, written by every build from this revision on
       — exact match required;
    2. for artifacts built before that key existed: ``watermark_sources`` (the
       source ids the index was built over) must be a subset of this notebook's
       source ids. A foreign package fails it on the first id; a legitimate
       older package can only have sources this notebook still has, or fewer.

    A package that offers neither is refused. That is a deliberate fail-closed:
    every artifact this codebase has ever written carries ``watermark_sources``
    (the delta scan needs it), so "neither key" means an artifact this tool
    cannot vouch for at all.
    """
    recorded = manifest.get("notebook_id")
    if isinstance(recorded, str) and recorded:
        if recorded != notebook_id:
            raise ScaleBuildCliError(
                f"this package belongs to notebook {recorded!r}, not "
                f"{notebook_id!r}. Importing it would publish another "
                "library's index here and start serving it."
            )
        return
    watermark = manifest.get("watermark_sources")
    if not isinstance(watermark, list) or not watermark:
        raise ScaleBuildCliError(
            f"{main}/manifest.json carries neither notebook_id nor "
            "watermark_sources, so it cannot be shown to belong to "
            f"{notebook_id!r}. Rebuild the package with a current checkout."
        )
    foreign = sorted(set(map(str, watermark)) - set(known_source_ids()))
    if foreign:
        raise ScaleBuildCliError(
            f"this package was built over sources {notebook_id!r} does not "
            f"have ({', '.join(foreign[:3])}"
            f"{'…' if len(foreign) > 3 else ''}); it is another library's "
            "index. (This package predates the manifest's notebook_id, so the "
            "watermark is the only binding available.)"
        )


def validate_import_package(
    package: Path,
    *,
    expected_notebook_id: str,
    known_source_ids,
    expected_pipeline_identity,
    runtime_dim: int,
    runtime_libraries: dict,
    allow_library_mismatch: bool = False,
) -> tuple[dict, list[str]]:
    """Everything that must be true before a foreign package touches live disk.

    Returns ``(main manifest, warnings)``; every refusal raises.
    ``known_source_ids`` is a callable, not a list: on a 48k-source library it
    is a full column read, and only the legacy binding path below needs it.

    The first four checks refuse rather than warn because each failure mode is
    *silent* at serve time. The standing redline for this whole batch is
    "answer quality does not change", and an index that quietly answers nothing
    — or answers from another library — is the worst possible violation of it:

    0. ``notebook_id`` / ``watermark_sources`` — which LIBRARY this package
       describes (see ``_require_package_belongs_to``). Everything else here is
       a deployment-wide fact, so this is the only check a typo in
       ``--notebook`` cannot walk past.
    1. ``pipeline_identity`` — a mismatched identity makes the retrieval side
       discard the scale core wholesale (``scale_artifact_catalog``'s pipeline
       gate) and fall back to live retrieval, with no error anywhere.
    2. ``dim`` — a mismatched embedding width makes ``open_ann`` fail open, i.e.
       zero recall with a healthy-looking index. A package with no ``dim`` at
       all is refused for the same reason: it cannot be shown to match, and this
       is the one path where the artifact came from another machine.
    3. ``hnswlib`` — the ``.bin`` carries no format version header, so a
       version mismatch can load "successfully" and return garbage, which the
       fail-open again swallows into silent zero recall. Strict equality, and
       an unknown version on either side counts as a mismatch.
       ``--allow-library-mismatch`` downgrades this one to a warning; there is
       deliberately no override for the first two.

    ``numpy``/``scipy`` mismatches only warn: ``.npy``/``.npz`` carry a format
    version and fail loudly, so they cannot degrade silently.
    """
    main = package / MAIN_ROOT
    if not main.is_dir():
        raise ScaleBuildCliError(
            f"{package} does not look like an export: no {MAIN_ROOT}/ directory"
        )
    manifest = _read_manifest(main)
    if manifest is None:
        raise ScaleBuildCliError(f"{main} has no manifest.json")
    if manifest.get("version") is None:
        raise ScaleBuildCliError(f"{main}/manifest.json has no version")
    _require_package_belongs_to(
        manifest, main, expected_notebook_id, known_source_ids
    )

    warnings: list[str] = []

    from app.domain.indexing_pipeline import BUILTIN_INDEXING_PIPELINE_VERSION

    # Legacy artifacts predate the plugin pipeline and carry no identity; the
    # retrieval side reads them as the builtin identity, so this does too.
    package_identity = list(
        manifest.get("pipeline_identity") or ["", BUILTIN_INDEXING_PIPELINE_VERSION]
    )
    if package_identity != list(expected_pipeline_identity):
        raise ScaleBuildCliError(
            f"pipeline identity mismatch: the package was built by "
            f"{package_identity}, this notebook publishes "
            f"{list(expected_pipeline_identity)}. Importing it would make the "
            "retrieval side discard the scale core silently."
        )

    package_dim = manifest.get("dim")
    if not isinstance(package_dim, int) or isinstance(package_dim, bool):
        raise ScaleBuildCliError(
            f"{main}/manifest.json has no usable dim; the embedding width "
            "cannot be verified and a mismatch degrades to silent zero recall"
        )
    if int(package_dim) != int(runtime_dim):
        raise ScaleBuildCliError(
            f"embedding dimension mismatch: the package was built at "
            f"dim={package_dim}, this deployment runs at dim={runtime_dim}"
        )

    recorded = manifest.get(MANIFEST_LIBRARY_KEY)
    recorded = recorded if isinstance(recorded, dict) else {}
    package_hnswlib = str(recorded.get("hnswlib") or "")
    running_hnswlib = str(runtime_libraries.get("hnswlib") or "")
    if not package_hnswlib or not running_hnswlib or package_hnswlib != running_hnswlib:
        detail = (
            f"the package was built with hnswlib "
            f"{package_hnswlib or '(unrecorded)'}, this machine runs "
            f"{running_hnswlib or '(unknown)'}"
        )
        if not allow_library_mismatch:
            raise ScaleBuildCliError(
                f"hnswlib version mismatch: {detail}. ann.bin has no format "
                "version header, so a mismatch can degrade to silent zero "
                "recall. Pin both machines to the same version, or pass "
                "--allow-library-mismatch if you have verified it is safe."
            )
        warnings.append(f"hnswlib version mismatch accepted on request: {detail}")
    for library in ("numpy", "scipy"):
        package_version = str(recorded.get(library) or "")
        running_version = str(runtime_libraries.get(library) or "")
        if package_version and running_version and package_version != running_version:
            warnings.append(
                f"{library} version differs (package {package_version}, "
                f"machine {running_version}); npy/npz carry a format version "
                "and fail loudly, so this is informational"
            )

    inventory = artifact_inventory_error(main, manifest)
    if inventory is not None:
        raise ScaleBuildCliError(f"the package is incomplete: {inventory}")

    companion = package / COMPANION_ROOT
    if companion.is_dir():
        # codex PR#643 R4 P2: a MISSING root (the switch that produces
        # companions is off) is the normal "no companion" shape and passes
        # below unconditionally — that conditional stays exactly as it was.
        # A PRESENT root with no readable manifest.json is a different
        # thing: not "no companion" but an incomplete package, and
        # ``_read_manifest`` returning ``None`` for a missing file would
        # otherwise skip the parent_version check entirely and let this
        # publish over a healthy live companion, leaving it unreadable
        # until the next rebuild.
        companion_manifest = _read_manifest(companion)
        if companion_manifest is None:
            raise ScaleBuildCliError(
                f"{companion} exists but has no manifest.json; the package "
                "is incomplete. Rebuild the package with a current checkout "
                "or remove the empty companion root before importing."
            )
        parent = companion_manifest.get("parent_version")
        if parent != manifest.get("version"):
            raise ScaleBuildCliError(
                "the source-partition companion in this package belongs to a "
                f"different generation (parent_version={parent!r}, main index "
                f"version={manifest.get('version')!r})"
            )
    return manifest, warnings


def companion_generation_error(roots: dict[str, Path]) -> Optional[str]:
    """The same parent-version gate over LIVE roots, for export."""
    companion_manifest = (
        _read_manifest(roots[COMPANION_ROOT])
        if roots[COMPANION_ROOT].is_dir()
        else None
    )
    if companion_manifest is None:
        return None
    main_manifest = _read_manifest(roots[MAIN_ROOT])
    main_version = None if main_manifest is None else main_manifest.get("version")
    parent = companion_manifest.get("parent_version")
    if parent != main_version:
        return (
            "the live source-partition companion belongs to a different "
            f"generation (parent_version={parent!r}, main index version="
            f"{main_version!r}); rebuild the index before exporting"
        )
    return None


# ───────────────────────────────────────────────────────────── reporting ──

def directory_report(path: Path) -> dict:
    """File count and total bytes for one root — never file *contents*."""
    if not path.exists():
        return {"present": False}
    files = 0
    total = 0
    for current, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(current, name))
            except OSError:
                continue
            files += 1
    return {"present": True, "files": files, "bytes": total}


def _runtime_dim(settings: Settings) -> int:
    from app.services.vector_index import resolve_runtime_dim

    return int(resolve_runtime_dim(settings) or settings.embed_dim)


# ────────────────────────────────────────────────────────── subcommands ──

def leftover_staging_directories(roots: dict[str, Path]) -> dict[str, dict]:
    """Every ``.old`` and staging sibling next to each live root, by report key.

    Staging can be the legacy fixed ``{live}.tmp`` (a leftover from before P1,
    codex PR#643 R1, or the one-time compatibility form ``prepare_staging_
    directory`` still self-heals) or a claim-unique ``{live}.tmp-<token>`` —
    reported here as ``{root}.tmp-<token>``. Nothing here is a signal to
    delete automatically: ``inspect`` only reports what it can SEE, never
    what it can prove is abandoned — see the module docs for why (an
    unaffiliated process cannot tell a still-writing zombie from dead work by
    filesystem shape alone).
    """
    leftovers: dict[str, dict] = {}
    for name, live in roots.items():
        old_candidate = Path(str(live) + ".old")
        if old_candidate.exists():
            leftovers[name + ".old"] = directory_report(old_candidate)
        legacy_tmp = Path(str(live) + ".tmp")
        if legacy_tmp.exists():
            leftovers[name + ".tmp"] = directory_report(legacy_tmp)
        prefix = live.name + ".tmp-"
        parent = live.parent
        if not parent.is_dir():
            continue
        for entry in sorted(parent.iterdir()):
            if entry.is_dir() and entry.name.startswith(prefix):
                suffix = entry.name[len(prefix) :]
                leftovers[f"{name}.tmp-{suffix}"] = directory_report(entry)
    return leftovers


def run_inspect(repository, notebook_id: str, report: Callable[[str], None]) -> dict:
    """Read-only: what is on disk, what the database thinks, who holds the claim.

    ``scale_index_status`` is the same call the service's own status endpoint
    makes, including its full delta scan — a few seconds on a very large
    notebook. That is the point: "is this artifact stale, and by how much" is
    the question an operator is actually asking before deciding build vs fold.
    """
    runtime = repository._runtime  # noqa: SLF001 — CLI composition root
    store = runtime.scale_artifact_store
    roots = artifact_roots(store, notebook_id)
    try:
        status = repository.scale_index_status(notebook_id)
    except KeyError:
        raise ScaleBuildCliError(f"unknown notebook: {notebook_id}") from None

    manifest = _read_manifest(roots[MAIN_ROOT])
    database_version = runtime.scale_artifacts.version(notebook_id)
    # Probe-and-release: the only correct way to answer "is anybody building
    # this right now" without a lock table of our own. Holding it any longer
    # would make an inspection block a real build.
    database = runtime.database
    probe = database.try_scale_build_lock(notebook_id)
    if probe is SCALE_BUILD_LOCK_UNAVAILABLE:
        # Says nothing about the notebook — only that this process could not ask
        # (no lock session left). Reporting it as "held_elsewhere" would send an
        # operator looking for a builder that may not exist.
        lock_state = "unknown"
    elif probe is None:
        lock_state = "held_elsewhere"
    else:
        probe.release()
        lock_state = "free"

    leftovers = leftover_staging_directories(roots)

    receipt = {
        "notebook_id": notebook_id,
        "state": status.get("state"),
        "exists": bool(status.get("exists")),
        "building": bool(status.get("building")),
        "delta_chunks": status.get("delta_chunks"),
        "total_chunks": status.get("total_chunks"),
        "build_claim": lock_state,
        "manifest": None
        if manifest is None
        else {
            "version": manifest.get("version"),
            "pipeline_identity": manifest.get("pipeline_identity"),
            "dim": manifest.get("dim"),
            "n_nodes": manifest.get("n_nodes"),
            "n_ann": manifest.get("n_ann"),
            "n_chunks": manifest.get("n_chunks"),
            "built_at": manifest.get("built_at"),
            "total_build_ms": manifest.get("total_build_ms"),
            "build_ms": manifest.get("build_ms"),
            "library_versions": manifest.get(MANIFEST_LIBRARY_KEY),
        },
        "version_matches_database": (
            manifest is not None
            and manifest.get("version") == database_version
        ),
        "roots": {name: directory_report(live) for name, live in roots.items()},
        "leftovers": leftovers,
        "runtime_dim": _runtime_dim(repository.settings),
        "runtime_library_versions": _runtime_libraries(),
    }
    if leftovers:
        report(
            "leftover staging/rollback directories are present; a `.old` "
            "directory can be restored with `mv {dir}.old {dir}`. A "
            "`.tmp`/`.tmp-<token>` directory is NOT auto-removed by anything "
            "— it may belong to a build still running elsewhere. Cross-check "
            "`build_claim` above (and the process list on both machines if "
            "still unsure) before deleting one by hand."
        )
    return receipt


def _runtime_libraries() -> dict:
    return runtime_library_versions()


def run_build(
    repository,
    notebook_id: str,
    *,
    mode: str,
    report: Callable[[str], None],
) -> dict:
    """Full rebuild or delta fold through the ordinary facade entry points.

    Those already take the per-notebook claim (T-W1's ``_claim_scale_build``),
    already hand its ``verify_held`` to the swap, and already publish the
    companion when the deployment's switch is on — so the offline path and the
    online path are literally the same code, which is the only way "the offline
    builder produces what the service would have produced" stays true.

    The publish step is masked against ``SIGINT`` inside the store's swap
    primitive, which is the only seam this path has: a build's renames happen
    deep inside ``build_scale_index``, hours after this frame started, and
    masking from here would mean masking Ctrl-C for the whole build.

    Unlike ``import`` below, this command never holds the claim itself, so an
    interrupt here cannot be resolved to a specific staging directory — see
    ``report_interrupted_build`` (P1, codex PR#643 R1).
    """
    store = repository._runtime.scale_artifact_store  # noqa: SLF001
    roots = list(artifact_roots(store, notebook_id).values())
    started = time.perf_counter()
    try:
        if mode == "fold":
            result = repository.fold_scale_index_delta(notebook_id)
        else:
            def on_stage(stage: str, elapsed_ms: int) -> None:
                report(f"stage {stage}: {elapsed_ms} ms")

            result = repository.build_scale_index(notebook_id, on_stage)
    except KeyboardInterrupt:
        report_interrupted_build(roots, notebook_id, mode, report)
        raise
    except KeyError:
        # An unknown (or currently-copying) notebook: the write-admission
        # check inside build/fold — ``require_write_admission`` reading
        # ``indexing_pipeline_state`` — raises a bare ``KeyError(notebook_id)``
        # for both, same as the row lookups ``inspect``/``import`` already
        # translate. Uncaught here it would surface a Python traceback instead
        # of the documented exit-code-2 refusal (codex PR#643 R1 P2).
        raise ScaleBuildCliError(f"unknown notebook: {notebook_id}") from None
    except ScaleBuildBusy as error:
        raise ScaleBuildCliFailure(
            f"{error}. Nothing was published; retry once the other builder "
            "finishes."
        ) from None
    except ScaleBuildLockLost as error:
        raise ScaleBuildCliFailure(
            f"{error} Investigate why the lock session died (an idle reaper, a "
            "failover, a terminated backend) before retrying."
        ) from None
    report(f"{mode} finished in {round(time.perf_counter() - started, 1)}s")
    return {"notebook_id": notebook_id, "mode": mode, "result": result}


def run_export(
    repository, notebook_id: str, destination: Path, report: Callable[[str], None]
) -> dict:
    """Copy the live roots out under the claim.

    The claim matters even though export writes nothing to the artifact tree: a
    ``copytree`` racing a publish would walk one root before its swap and
    another after it, producing a package that mixes two generations — and the
    companion is rebuilt *after* the main swap, so that window exists by design.

    ``destination`` is rejected up front if it is (or sits inside) any of the
    three live artifact roots: ``destination.mkdir()`` below makes an empty
    destination visible before ``copytree`` scans its source, so a destination
    under a source root gets walked as part of that source's own tree —
    ``--to <kg_index>/out`` grows ``<kg_index>/out/kg_index/out/kg_index/...``
    without bound, and every one of those nested copies also writes into the
    supposedly read-only live index (codex PR#643 R2 P2).
    """
    store = repository._runtime.scale_artifact_store  # noqa: SLF001
    roots = artifact_roots(store, notebook_id)
    if not (roots[MAIN_ROOT] / "manifest.json").is_file():
        raise ScaleBuildCliError(
            f"{notebook_id} has no published scale index to export"
        )
    destination_resolved = destination.resolve()
    for name, root in roots.items():
        root_resolved = root.resolve()
        if (
            destination_resolved == root_resolved
            or destination_resolved.is_relative_to(root_resolved)
        ):
            raise ScaleBuildCliError(
                f"--to {destination} is inside the {name} artifact root "
                f"{root}; copying would recurse into its own destination "
                "and could modify the live index"
            )
    if destination.exists():
        if not destination.is_dir():
            raise ScaleBuildCliError(f"--to {destination} is not a directory")
        if any(destination.iterdir()):
            raise ScaleBuildCliError(f"--to {destination} exists and is not empty")

    with claim_notebook(repository, notebook_id):
        problem = companion_generation_error(roots)
        if problem is not None:
            raise ScaleBuildCliError(problem)
        destination.mkdir(parents=True, exist_ok=True)
        exported = []
        for name in PUBLISH_ORDER:
            live = roots[name]
            if not live.is_dir():
                continue
            shutil.copytree(live, destination / name)
            exported.append(name)
            report(f"exported {name}")
        # Read the manifest that was just copied into `destination`, not the
        # live one: the claim releases when this block exits, and another
        # builder can publish a new generation the instant it does. Reading
        # `roots[MAIN_ROOT]` after release would describe whatever version
        # happens to be live at read time, not the package actually written
        # to disk (codex PR#643 R2 P2).
        manifest = _read_manifest(destination / MAIN_ROOT) or {}
    return {
        "notebook_id": notebook_id,
        "to": str(destination),
        "roots": exported,
        "version": manifest.get("version"),
        "library_versions": manifest.get(MANIFEST_LIBRARY_KEY),
    }


def run_import(
    repository,
    notebook_id: str,
    package: Path,
    *,
    allow_library_mismatch: bool,
    report: Callable[[str], None],
) -> dict:
    """Validate a package, stage all of its roots, then publish them atomically.

    Staging is the expensive, failure-prone half (a full copy of a multi-GB
    tree); it happens entirely in ``.tmp`` directories with the live tree
    untouched. Only when every root is staged does the claim get re-verified and
    the renames run, under ``SIGINT`` masking.

    codex PR#643 R5 P1: the import claim (T-W1's per-notebook advisory lock)
    does not block ``execute_indexing_pipeline_rebuild`` from completing a
    pipeline switch and publishing a NEW live identity during that staging
    window — a plugin activation is a different mechanism entirely, with no
    reason to wait on a scale-build claim. A package validated against the
    identity that was live a moment ago, and then staged for as long as a
    multi-GB copy takes, can therefore describe a pipeline the retrieval side
    has already stopped trusting: ``scale_artifact_catalog``'s own pipeline
    gate discards it silently, same as any other identity-mismatched core.
    So the identity is re-read — same method, ``projections.pipeline_identity``
    — right before the destructive renames begin, and a drift refuses the
    publish outright rather than let it land unusable.

    Registered, not fixed (codex W-CLI R1 N3): each root's rename is atomic, the
    SET of three is not. A hard kill (SIGKILL, power loss) between two of them
    leaves companion/viz from the new generation beside a main index from the
    old one. That state is fail-soft by construction — the companion's
    ``parent_version`` gate makes it unreadable rather than wrong, and viz is
    advisory — which is exactly why the publish order is companion → viz → main.
    A cross-root journal is the only thing that would close it, and it would buy
    nothing the ordering does not already give.
    """
    runtime = repository._runtime  # noqa: SLF001
    store = runtime.scale_artifact_store
    roots = artifact_roots(store, notebook_id)
    settings = repository.settings
    projections = runtime.index_projections
    # An unknown notebook must be refused HERE, not left to the validation
    # below: ``pipeline_identity`` answers with the builtin default for a
    # notebook that has no ``unified_kg_state`` row rather than raising, so a
    # mistyped id would sail past the identity gate and publish a directory tree
    # for a library that does not exist (codex W-CLI R1 P1-2). This is the same
    # cheap tier read ``status()`` uses to keep its missing-notebook contract.
    if projections.notebook_tier(notebook_id) is None:
        raise ScaleBuildCliError(f"unknown notebook: {notebook_id}")

    with claim_notebook(repository, notebook_id) as handle:
        expected_pipeline_identity = projections.pipeline_identity(notebook_id)
        manifest, warnings = validate_import_package(
            package,
            expected_notebook_id=notebook_id,
            known_source_ids=lambda: projections.source_ids(notebook_id),
            expected_pipeline_identity=expected_pipeline_identity,
            runtime_dim=_runtime_dim(settings),
            runtime_libraries=_runtime_libraries(),
            allow_library_mismatch=allow_library_mismatch,
        )
        for warning in warnings:
            report(f"warning: {warning}")

        staged: dict[str, Path] = {}
        published: list[str] = []
        # One guard around ALL the renames, not one per root: an interrupt
        # between two roots would leave the pair mismatched (fail-soft, but
        # avoidable), and the whole sequence is milliseconds. ``reraise`` is off
        # because this frame can tell the two interrupt cases apart — see below.
        guard = SwapInterruptGuard(report, reraise=False)
        try:
            for name in PUBLISH_ORDER:
                source = package / name
                if not source.is_dir():
                    continue
                target = store.prepare_staging_directory(
                    roots[name], handle.claim_token
                )
                staged[name] = target
                shutil.copytree(source, target, dirs_exist_ok=True)
                report(f"staged {name}")

            # codex PR#643 R5 P1: re-verify the identity the package was
            # validated against, right before the first rename — the staging
            # copy above is the slow, interruptible part, and a claim/identity
            # proven fresh before it says nothing about what is live now.
            current_pipeline_identity = projections.pipeline_identity(notebook_id)
            if list(current_pipeline_identity) != list(expected_pipeline_identity):
                raise _ImportPipelineIdentityDrifted(
                    f"the live pipeline identity for {notebook_id} changed "
                    f"from {list(expected_pipeline_identity)} to "
                    f"{list(current_pipeline_identity)} while this package "
                    "was being staged"
                )

            with guard:
                for name in PUBLISH_ORDER:
                    if name not in staged:
                        continue
                    store.swap_staging_directory(
                        roots[name], staged[name], verify_held=handle.verify_held
                    )
                    published.append(name)
                    staged.pop(name)
        except _ImportPipelineIdentityDrifted as error:
            # Same contract as the ScaleBuildLockLost branch below: nothing
            # was renamed, so the staged copies are left on disk rather than
            # discarded — re-exporting is real work an operator should not
            # have to repeat while diagnosing this.
            report(f"staged before the pipeline identity changed: {list(staged)}")
            raise ScaleBuildCliFailure(
                f"{error}. Nothing was published; the staged copies are left "
                "on disk for inspection. Re-export the package with a current "
                "checkout against the new pipeline and re-run import."
            ) from None
        except ScaleBuildLockLost as error:
            # The claim is the reason a second writer cannot be here; losing it
            # mid-publish means abandoning the operation with the live tree as
            # it stands, NOT deleting the staged copies an operator may need.
            report(f"published before the claim was lost: {published or 'nothing'}")
            raise ScaleBuildCliFailure(
                f"{error} Nothing further was published; the staged copies are "
                "left on disk for inspection."
            ) from None
        except BaseException:
            # Covers a staging failure (nothing renamed yet, live tree pristine)
            # and Ctrl-C. ``published`` is the EXPLICIT phase marker (P2, codex
            # PR#643 R7): once any root has been renamed this run keeps every
            # remaining staged copy — the three roots are one generation and
            # discarding the unpublished half would leave a torn set with no
            # way to complete it. Before the first rename, only this run's own
            # ``.tmp-<token>`` directories are removed, with the ``.old``
            # evidence check as the belt for an interrupt landing inside a
            # single root's two-rename window (see publish_started).
            if published:
                report(
                    f"already published before the failure: {published}; the "
                    "remaining staged copies are kept so the generation can "
                    "be completed by re-running import"
                )
            else:
                discard_staging_unless_publishing(
                    {roots[name]: path for name, path in staged.items()}, report
                )
            raise
        if guard.interrupted:
            # The interrupt arrived while the renames were being deferred and
            # they all completed. Nothing was abandoned, so this exits 0 with
            # the ordinary receipt: raising here would print "interrupted" and
            # return 130 for a publish that fully succeeded, sending an operator
            # to check a tree that is exactly as they asked for it
            # (codex W-CLI R1 P2-6).
            report(
                "the deferred interrupt is being honoured now: every root was "
                "published before it could take effect, so this run completed"
            )

    return {
        "notebook_id": notebook_id,
        "from": str(package),
        "roots": published,
        "version": manifest.get("version"),
        "warnings": warnings,
    }


# ──────────────────────────────────────────────────────────────── argv ──

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build_scale_index.py",
        description=(
            "Build, export and import a notebook's scale index from a process "
            "that runs beside the live service. PostgreSQL only; the database "
            "URL comes from the environment and is never printed."
        ),
    )
    parser.add_argument(
        "--statement-timeout-seconds",
        type=int,
        default=DEFAULT_STATEMENT_TIMEOUT_SECONDS,
        help=(
            "per-statement timeout for this process only (default: 86400). The "
            "online default is sized for interactive requests and would kill a "
            "multi-hour build."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect = subparsers.add_parser(
        "inspect", help="read-only report on one notebook's artifacts and claim"
    )
    inspect.add_argument("--notebook", required=True)

    build = subparsers.add_parser("build", help="rebuild or fold the index")
    build.add_argument("--notebook", required=True)
    mode = build.add_mutually_exclusive_group()
    mode.add_argument(
        "--full",
        dest="mode",
        action="store_const",
        const="full",
        help="full rebuild (default)",
    )
    mode.add_argument(
        "--fold",
        dest="mode",
        action="store_const",
        const="fold",
        help="fold the delta into the published artifact",
    )
    build.set_defaults(mode="full")

    export = subparsers.add_parser("export", help="copy the artifacts to a directory")
    export.add_argument("--notebook", required=True)
    export.add_argument("--to", required=True)

    importer = subparsers.add_parser(
        "import", help="publish artifacts built elsewhere"
    )
    importer.add_argument("--notebook", required=True)
    importer.add_argument("--from", dest="source", required=True)
    importer.add_argument(
        "--allow-library-mismatch",
        action="store_true",
        help=(
            "accept an hnswlib version difference between the building machine "
            "and this one. Only for a difference you have verified; the .bin "
            "format carries no version header."
        ),
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    def report(message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    try:
        settings = resolve_settings(args.statement_timeout_seconds)
        require_postgres(settings)
        verify_migration_ledger(settings.database_url)
        with open_scale_build_repository(settings) as repository:
            if args.command == "inspect":
                receipt = run_inspect(repository, args.notebook, report)
            elif args.command == "build":
                receipt = run_build(
                    repository, args.notebook, mode=args.mode, report=report
                )
            elif args.command == "export":
                receipt = run_export(
                    repository, args.notebook, Path(args.to), report
                )
            else:
                receipt = run_import(
                    repository,
                    args.notebook,
                    Path(args.source),
                    allow_library_mismatch=args.allow_library_mismatch,
                    report=report,
                )
    except ScaleBuildCliError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except ScaleBuildCliFailure as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # The messages above say exactly what was staged, published or removed;
        # claiming "nothing happened" here would be a guess.
        print("interrupted", file=sys.stderr)
        return 130
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0
