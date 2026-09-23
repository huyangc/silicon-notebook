"""PR-4 T2 -- rebuild the scale artifacts a finished ``sync import`` invalidated.

``kg_index``/``kg_viz``/``kg_index_partitions`` are derived artifacts and do not
travel with a sync package (docs/incremental-sync-design.md §6): after an import
the target's rows moved but its published scale index did not, so a large
mirrored library keeps serving a stale artifact until somebody rebuilds it. That
used to be a documented manual step; this module is the automation §6/§8 promised.

Four properties shape everything below.

1. **The import already finished.** ``import_package`` closed the ``sync_imports``
   row as ``done`` and wrote ``report_json`` before this module runs, so nothing
   here may change the import's verdict: every per-notebook outcome is recorded,
   never raised, and ``sync import`` keeps exit code 0. Nothing here writes back
   to ``sync_imports`` either -- "done means done" stays literally true, and the
   rebuild's receipt lives only in the CLI's output/``--json``.
2. **"Does this notebook want a scale index at all" is not re-derived here.**
   That question is ``ScaleArtifactRuntime.status()["eligible"]``, which is the
   single definition the online path uses -- base tier, an artifact already on
   disk, mounted by another notebook, over the chunk-suggest threshold, or NOT
   ``copyable`` (i.e. too big to be copied whole, this codebase's spelling of
   "large"). Restating any of those thresholds here would create a second,
   drifting definition.
3. **Every eligible notebook is rebuilt in FULL.** This module deliberately
   does NOT ask ``_resolve_scale_mode(..., "auto")``, and deliberately does not
   gate on ``state``. Both are tuned for the ONLINE world, where the only thing
   that happens between builds is content being APPENDED: ``_index_delta``
   answers with the source ids that are not in the published manifest's
   ``watermark_sources``, so a fold is correct exactly while the existing
   artifact is still a subset of the notebook's content. A sync package breaks
   that premise -- it carries REPLACEMENT semantics for rows that are already
   indexed (a source's chunks and knowledge upserted in place, or a source
   deleted outright). After such an import the delta can be empty while the
   artifact is wrong: ``state`` reads ``indexed`` and a fold returns the old
   manifest untouched, so either gate would leave a stale index published --
   and a fold with new sources present would additionally stamp the current
   version onto an artifact still holding superseded content. A full rebuild is
   the only mode whose correctness does not depend on that premise, and it goes
   through the very same ``build_scale_index`` the online path calls. The cost
   (a daily full rebuild on a large mirror) is real and is the operator's to
   schedule: ``--rebuild-scale skip`` hands it back to them.
4. **It has to be synchronous.** ``maybe_auto_index`` only enqueues onto an
   in-process scheduler, which dies with the CLI. So this drives
   ``scale_build_cli.run_build`` directly, the same offline builder an operator
   would have run by hand, complete with its cross-process per-notebook claim.

PostgreSQL only, for the reason ``scale_build_cli.require_postgres`` states: a
SQLite deployment has no cross-process build claim, so an offline builder cannot
be excluded from the serving process. That is reported as a skip, not a failure.

It also runs ``verify_migration_ledger`` first, exactly where
``scale_build_cli.main`` runs it. That gate is not about this module's own
correctness -- it is the guarantee that the checkout doing the building and the
database being built from agree on what the rows MEAN. Running the default
automatic path without it would publish a quietly-wrong index over a healthy one
whenever an operator imports from a checkout that is a migration away from the
live service.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from app.core.config import Settings
from app.core.database_url import database_identity

# ``--rebuild-scale`` choices. ``auto`` is the default: it still asks the same
# gates below for every candidate, so "auto" means "decide per notebook", not
# "rebuild everything".
REBUILD_AUTO = "auto"
REBUILD_SKIP = "skip"
REBUILD_CHOICES = (REBUILD_AUTO, REBUILD_SKIP)

# The only mode this module ever builds in; see property 3 in the module
# docstring for why a fold is not sound after an import.
_FULL = "full"

SKIPPED_SQLITE = "skipped:sqlite_backend"
SKIPPED_UNKNOWN_NOTEBOOK = "skipped:unknown_notebook"
# ``status()["eligible"]`` said no: the notebook does not want a scale index at
# all. That one predicate already folds in every reason -- small enough to be
# copied whole (NOT ``copyable`` is its last clause), below the chunk-suggest
# threshold, not mounted, no artifact on disk, not base tier.
SKIPPED_NOT_ELIGIBLE = "skipped:not_eligible"
# The builder refused before doing anything: the notebook is not live as far as
# ``require_write_admission`` is concerned (mid-copy, mid-import, being deleted)
# or its indexing pipeline is unavailable. Not this pass's business to fix.
SKIPPED_REFUSED = "skipped:refused_by_builder"
# Another process holds the cross-process build claim. It may be an online
# build, but it may equally be an export or a delta fold, neither of which
# replaces already-indexed content -- so this outcome always carries a warning.
SKIPPED_BUSY = "skipped:busy"
SKIPPED_INTERRUPTED = "skipped:interrupted"

SQLITE_NOTE = (
    "scale 索引: SQLite 后端不支持离线 scale 构建，本次未重建；"
    "如需重建请在服务内触发（打开该笔记本或用页面的建索引入口）。"
)
SKIP_FLAG_NOTE = (
    "scale 索引: 按 --rebuild-scale skip 跳过；需要时手动运行 "
    "scripts/build_scale_index.py build --notebook <id>。"
)
INTERRUPTED_WARNING = (
    "scale 索引重建被中断（Ctrl-C），剩下的笔记本没有重建；导入本身已完成、"
    "未受影响。要继续请手动运行 scripts/build_scale_index.py build "
    "--notebook <id>，不要重跑这个包。"
)


@dataclass(frozen=True)
class ScaleRebuildResult:
    """What the rebuild pass did, per notebook, plus whole-run commentary.

    ``outcomes`` maps notebook id to one of ``built``/``skipped:<why>``/
    ``failed:<ExceptionClass>``. There is no ``folded``: this module only ever
    builds in full (module docstring, property 3). The failure form carries the
    exception's CLASS and not its message: a build failure's text can quote a
    storage path or a database URL, and this receipt is printed and serialized.
    The actionable half goes into ``warnings`` as a fixed sentence instead.
    """

    outcomes: Mapping[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    # One line explaining a DECISION that applied to the whole run rather than
    # to any notebook: this backend cannot build offline, or the operator asked
    # for no rebuild. Empty when every notebook was judged on its own -- a run
    # that started and went wrong reports that through ``warnings``, which is
    # where an operator already looks for things to act on.
    note: str = ""

    @property
    def counts(self) -> dict[str, int]:
        totals = {"built": 0, "skipped": 0, "failed": 0}
        for outcome in self.outcomes.values():
            key = outcome.split(":", 1)[0]
            if key in totals:
                totals[key] = totals[key] + 1
        return totals


def rebuild_candidates(
    notebooks: Sequence[str],
    deleted: Sequence[str] = (),
    copying: Sequence[str] = (),
) -> tuple[str, ...]:
    """Notebooks the package carried, minus the ones the import already excused.

    ``deleted`` is ``ImportReport.notebooks_deleted``: flipped to ``deleting``
    and queued for a delete job, so building an index for one would race that
    job for the very artifact roots it is about to remove.

    ``copying`` is ``ImportReport.notebooks_delete_skipped`` -- the notebooks
    this same run found mid-deep-copy at the target. They are not live as far
    as ``require_write_admission`` is concerned, so the builder would refuse
    each one anyway; dropping them here turns a receipt line the operator
    cannot act on into no line at all, using a fact the import ALREADY
    established rather than a second lookup of our own.
    """
    excluded = set(deleted) | set(copying)
    seen: set[str] = set()
    ordered: list[str] = []
    for notebook_id in notebooks:
        if notebook_id in excluded or notebook_id in seen:
            continue
        seen.add(notebook_id)
        ordered.append(notebook_id)
    return tuple(ordered)


def rebuild_after_import(
    settings: Settings, notebook_ids: Sequence[str]
) -> ScaleRebuildResult:
    """Build or fold each candidate's scale index, one at a time.

    Serial by construction: each build owns a notebook's whole artifact tree and
    is sized for a machine, not a core, so overlapping two of them on the box
    that just finished an import is the wrong default.
    """
    candidates = tuple(notebook_ids)
    if not candidates:
        return ScaleRebuildResult()
    # Same single condition ``scale_build_cli.require_postgres`` applies; asked
    # here as a question rather than as a refusal, because on SQLite this is a
    # reported skip and not an error (see the module docstring).
    if database_identity(settings.database_url).scheme != "postgresql":
        return ScaleRebuildResult(
            outcomes={notebook_id: SKIPPED_SQLITE for notebook_id in candidates},
            note=SQLITE_NOTE,
        )
    from app.services import scale_build_cli

    build_settings = settings.model_copy(
        update={
            # The sync CLI's settings carry the ONLINE statement timeout, sized
            # for interactive requests; a scale build on a large library runs
            # for hours. Same number and same reason as
            # ``scale_build_cli.resolve_settings``, applied before the pool is
            # constructed because the pool reads it exactly once.
            "postgres_statement_timeout_seconds": (
                scale_build_cli.DEFAULT_STATEMENT_TIMEOUT_SECONDS
            )
        }
    )
    outcomes: dict[str, str] = {}
    warnings: list[str] = []
    try:
        # Exactly what ``scale_build_cli.main`` does before composing anything,
        # and for the same reason: an off-host checkout one migration away from
        # the live database reads columns the service does not have (or misses
        # ones it does), and the index it publishes is quietly WRONG rather than
        # loudly broken -- published by an atomic rename over a healthy one.
        # This path is the automatic default, so skipping the ledger check here
        # would make "sync import" the one way to hit that without asking.
        # Read on a bare connection before the pool exists, so a refusal costs
        # one connection.
        scale_build_cli.verify_migration_ledger(build_settings.database_url)
        with scale_build_cli.open_scale_build_repository(
            build_settings
        ) as repository:
            for notebook_id in candidates:
                outcome, warning = _rebuild_one(
                    scale_build_cli, repository, notebook_id
                )
                outcomes[notebook_id] = outcome
                if warning:
                    warnings.append(warning)
    except KeyboardInterrupt:
        # Ctrl-C means "stop building", not "the import failed". It is caught
        # around the WHOLE pass, not just around a build: the compose step
        # (``prime_extension_admission``) and the close step are seconds of
        # real wall clock each, and a Ctrl-C landing there used to escape to
        # ``main`` and turn a committed import into "interrupted"/exit 2 --
        # which would send an operator re-running a package that is already
        # applied. ``run_build`` has already reported what its interrupted
        # build left on disk.
        for notebook_id in candidates:
            outcomes.setdefault(notebook_id, SKIPPED_INTERRUPTED)
        warnings.append(INTERRUPTED_WARNING)
    except Exception as exc:  # noqa: BLE001 - the import is already done
        # The ledger refused, or composing/closing the repository failed (a
        # connection, extension admission). Whatever was not judged cannot be
        # reported as skipped-on-purpose, so it inherits this failure.
        failure = f"failed:{type(exc).__name__}"
        for notebook_id in candidates:
            outcomes.setdefault(notebook_id, failure)
        warnings.append(_rebuild_aborted_warning(exc))
    return ScaleRebuildResult(outcomes=outcomes, warnings=tuple(warnings))


def _rebuild_one(
    build_cli: Any, repository: Any, notebook_id: str
) -> tuple[str, str]:
    """One notebook's verdict, as ``(outcome, warning)``; ``warning`` may be "".

    This function never raises: a notebook that cannot be judged or built is a
    receipt line, so the next candidate still gets its turn and ``sync import``
    still exits 0. A warning is attached exactly when this run leaves the
    notebook OWING a rebuild that nobody else is guaranteed to perform -- every
    failure, and ``SKIPPED_BUSY``. The other skips carry none: the notebook
    either does not want an index or is not in a state anyone could build it
    in, so there is nothing for an operator to do.
    """

    def report(message: str) -> None:
        # stderr, so a build's stage lines never contaminate ``--json``'s stdout.
        print(f"scale[{notebook_id}] {message}", file=sys.stderr, flush=True)

    try:
        status = repository.scale_index_status(notebook_id)
    except KeyError:
        # The package named it, this environment does not have it (or not as a
        # live notebook): a scoped package whose notebook was never imported
        # here, or one removed/retired since.
        return SKIPPED_UNKNOWN_NOTEBOOK, ""
    except Exception as exc:  # noqa: BLE001 - one notebook never fails another
        return f"failed:{type(exc).__name__}", _rebuild_failed_warning(notebook_id)
    if not status.get("eligible"):
        return SKIPPED_NOT_ELIGIBLE, ""
    # NOT gated on ``state``. An import replaces rows that are already indexed,
    # which ``state``'s append-only delta cannot see: a mirror whose content was
    # rewritten in place still reads ``indexed`` while its published artifact
    # describes the previous contents (module docstring, property 3). Nor is
    # ``building``/``queued`` consulted -- those read in-process sets that are
    # empty in a fresh CLI process. The cross-process claim inside ``run_build``
    # is the real answer to "is somebody else building this" (``SKIPPED_BUSY``).
    try:
        build_cli.run_build(repository, notebook_id, mode=_FULL, report=report)
    except build_cli.ScaleBuildCliBusy:
        # Another process holds this notebook's build claim. Nothing was
        # published here -- and, critically, we cannot assume the holder does
        # what this pass owed the notebook. The SAME claim is taken by
        # ``run_export`` (which only copies the artifacts out) and by an online
        # delta fold (which appends and does not replace already-indexed
        # content). Either of those overlapping this run would silently drop
        # the full rebuild the import made necessary, and re-running the
        # package will not bring it back: the second import is
        # ``already_applied`` and rebuilds nothing. So this skip is the one
        # skip that carries a warning (codex #791 R2 P2).
        return SKIPPED_BUSY, _rebuild_busy_warning(notebook_id)
    except build_cli.ScaleBuildCliError:
        # The builder refused before touching anything: the notebook is not
        # live for ``require_write_admission`` (mid-copy, mid-import, being
        # deleted) or its indexing pipeline is unavailable. A refusal is not a
        # failure, and re-running the same command by hand would refuse too --
        # so no warning.
        return SKIPPED_REFUSED, ""
    except Exception as exc:  # noqa: BLE001 - one notebook never fails another
        return f"failed:{type(exc).__name__}", _rebuild_failed_warning(notebook_id)
    return "built", ""


def _rebuild_busy_warning(notebook_id: str) -> str:
    """Actionable, because nobody else is obliged to finish this.

    Says WHAT to run rather than "retry later": re-running the package does not
    help (the second import is ``already_applied`` and rebuilds nothing), so the
    only way back to a correct index is the explicit full build below.
    """
    return (
        f"笔记本 {notebook_id} 的 scale 索引本次未重建：另一个构建者正持有该库的"
        "构建 claim。那一方不一定做的是全量重建（导出、增量 fold 用的是同一把 claim，"
        "都不会替换已经索引过的内容），而重跑这个包不会再触发重建（第二次导入是"
        "already_applied）。等它结束后手动全量重建一次："
        "PYTHONPATH=backend python scripts/build_scale_index.py build "
        f"--notebook {notebook_id} --full"
    )


def _rebuild_failed_warning(notebook_id: str) -> str:
    # ``--full`` is spelled out even though it is the default: after an import
    # a fold would be unsound (module docstring, property 3), so the command an
    # operator copies out of this warning must not be ambiguous about it.
    return (
        f"笔记本 {notebook_id} 的 scale 索引重建失败；导入本身已完成、未受影响。"
        "排查后手动重跑：PYTHONPATH=backend python scripts/build_scale_index.py "
        f"build --notebook {notebook_id} --full"
    )


def aborted_result(
    notebook_ids: Sequence[str], exc: BaseException
) -> ScaleRebuildResult:
    """The receipt for a pass that could not produce one for itself.

    ``rebuild_after_import`` answers both ``Exception`` and ``KeyboardInterrupt``
    already, so reaching this means something escaped it -- a ``BaseException``
    raised between its own handlers, or a second Ctrl-C while it was writing its
    result. The caller still has a report to print, so it gets one.
    """
    if isinstance(exc, KeyboardInterrupt):
        return ScaleRebuildResult(
            outcomes={
                notebook_id: SKIPPED_INTERRUPTED for notebook_id in notebook_ids
            },
            warnings=(INTERRUPTED_WARNING,),
        )
    return ScaleRebuildResult(
        outcomes={
            notebook_id: f"failed:{type(exc).__name__}"
            for notebook_id in notebook_ids
        },
        warnings=(_rebuild_aborted_warning(exc),),
    )


def _rebuild_aborted_warning(exc: BaseException) -> str:
    """The pass itself could not start, or could not finish cleanly.

    One sentence covers both ends on purpose: the operator's next move is the
    same either way (diagnose, then rebuild by hand), and a failure while
    closing the repository says nothing about whether the builds before it
    published -- the per-notebook outcomes above do.
    """
    return (
        f"scale 索引重建没能正常开始或收尾（{type(exc).__name__}）；导入本身已完成、"
        "未受影响。未给出结果的笔记本按失败记；排查后手动重跑 "
        "scripts/build_scale_index.py build --notebook <id>"
    )
