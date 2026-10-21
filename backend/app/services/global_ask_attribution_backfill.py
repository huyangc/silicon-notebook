"""Backfill of the global Ask attribution rows (SQLite v98 / PostgreSQL 0078).

A ``done`` global Ask job records the libraries it is attributed to in
``global_ask_job_notebooks`` when it finishes (``GlobalAskStore.save``). Jobs
that finished before the migration carry ``attributed = 0`` and no rows; the
notebook analytics count reads them through the payload predicate
(``global_ask_attribution.attribution_sql``) until this pass has given them
rows. The migration deliberately does not do it: it would parse every global
payload inside the upgrade.

Each job is read back as the ``GlobalAskJob`` it was written from and handed
to ``job_attribution`` -- the very call the completion write and the
completion hook make -- so a backfilled row set is what the job would have
written had the column existed. A payload that does not read back as a job is
reported (``payload_unreadable``) and left as it is: it keeps being counted
through the payload predicate, exactly as before this pass.

Two drivers share ``run_backfill``:

* **after readiness, in the service** -- ``schedule_attribution_backfill``
  (called by ``startup_warmup`` strictly after ``mark_ready()``, like the
  memory orphan sweep) logs how many done jobs are still at ``attributed = 0``
  and, when there are any, submits ONE background pass. It never delays
  readiness and never raises into startup. Until it has caught up the panel
  keeps counting those jobs through the slower payload predicate, with the
  same result;
* **by hand** -- ``scripts/backfill_global_ask_attribution.py`` (``main``) for
  a re-run or a dry run. It does NOT own the schema: like every maintenance
  CLI it is a schema guest -- the bare one,
  ``maintenance_cli.create_bare_schema_guest_repository`` (``migrate=False,
  seed=False``, no extension seats, no admission prime, no auth wiring: the
  composition it always had, for a tool that runs beside a serving process)
  -- and refuses, with the shared wording, a database that is not at this
  build's schema (so never one short of SQLite v98 / PostgreSQL 0078).

Resumable and idempotent: pages are read by job id (keyset) from the partial
index of still-unattributed done jobs, and each page is written in one
transaction that never waits on a row lock (``GlobalAskStore.
write_backfilled_attributions``: ``FOR UPDATE SKIP LOCKED`` on PostgreSQL)
and re-checks ``attributed = 0``. A row another transaction holds is skipped
and stays pending (``pending_after``) for the next pass; interrupting and
re-running continues with what is left; ``--after-id`` skips an already
examined prefix (for example past a reported unreadable job). Safe beside a
serving process -- the completion path only ever writes rows it moves out of
``running``, never one this pass selects.

CLI exit status: 0 done (nothing unreadable), 1 some payloads were unreadable,
2 usage error, 3 the database is not migrated to the attribution schema. The
database URL is never printed.
"""
from __future__ import annotations

import json
import logging
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol, Sequence

from pydantic import ValidationError

from app.domain.global_ask_attribution import job_attribution
from app.models.global_ask import GlobalAskJob
from app.repositories.schema_currency import EXIT_SCHEMA_NOT_CURRENT

DEFAULT_BATCH_SIZE = 500
PAYLOAD_UNREADABLE = "payload_unreadable"
#: Diagnostic name of the one post-readiness background thread.
JOB_NAME = "global-ask-attribution-backfill"
EXIT_SCHEMA_NOT_READY = EXIT_SCHEMA_NOT_CURRENT  # 3, shared by every schema guest

logger = logging.getLogger("silicon_notebook.global_ask_attribution")


class AttributionBackfillPort(Protocol):
    """The maintenance adapter's methods this pass depends on."""

    def global_ask_attribution_schema_ready(self) -> bool: ...
    def global_ask_unattributed_count(self) -> int: ...
    def global_ask_unattributed_page(self, after_id: str, limit: int) -> list[tuple[str, str]]: ...
    def write_global_ask_attributions(self, attributions: list[tuple[str, list[str]]]) -> int: ...


@dataclass
class BackfillResult:
    pending_before: int = 0
    scanned: int = 0
    written: int = 0
    # Not attributed by this pass although readable: attributed or purged by
    # someone else between read and write, or (PostgreSQL) a row another
    # transaction held -- skipped rather than waited for, so still pending.
    skipped: int = 0
    unreadable: int = 0
    last_id: str = ""
    pending_after: int = 0

    def as_dict(self) -> dict[str, object]:
        return dict(self.__dict__)


def _attribution(payload_json: str) -> list[str] | None:
    try:
        return job_attribution(GlobalAskJob.model_validate_json(payload_json))
    except (ValidationError, ValueError, TypeError):
        return None


def run_backfill(
    port: AttributionBackfillPort,
    *,
    after_id: str = "",
    limit: int | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    on_unreadable: Callable[[str], None] = lambda _job_id: None,
    progress: Callable[[BackfillResult], None] = lambda _result: None,
) -> BackfillResult:
    """Attribute the done jobs still at ``attributed = 0``, id order, from
    ``after_id`` on; at most ``limit`` jobs when given. ``dry_run`` reads and
    computes but writes nothing (``written`` then counts what would be)."""
    result = BackfillResult(pending_before=port.global_ask_unattributed_count(), last_id=after_id)
    cursor = after_id
    while limit is None or result.scanned < limit:
        size = batch_size if limit is None else min(batch_size, limit - result.scanned)
        page = port.global_ask_unattributed_page(cursor, size)
        if not page:
            break
        batch: list[tuple[str, list[str]]] = []
        for job_id, payload_json in page:
            notebook_ids = _attribution(payload_json)
            if notebook_ids is None:
                result.unreadable += 1
                on_unreadable(job_id)
            else:
                batch.append((job_id, notebook_ids))
        if dry_run:
            result.written += len(batch)
        elif batch:
            written = port.write_global_ask_attributions(batch)
            result.written += written
            result.skipped += len(batch) - written
        result.scanned += len(page)
        cursor = result.last_id = page[-1][0]
        progress(result)
    result.pending_after = port.global_ask_unattributed_count()
    return result


def _report_writer(path: Path | None) -> Callable[[str], None]:
    if path is None:
        return lambda _job_id: None
    path.parent.mkdir(parents=True, exist_ok=True)

    def write(job_id: str) -> None:
        # Ids and a stable reason code only: never a payload or a question.
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"job_id": job_id, "reason": PAYLOAD_UNREADABLE}) + "\n")

    return write


def schedule_attribution_backfill(
    repo: Any,
    *,
    is_ready: Callable[[], bool] | None = None,
    submit: Callable[..., Any] | None = None,
) -> Any:
    """Post-readiness step: log the pending count and submit ONE background
    pass when there is anything to attribute. Returns the job handle, or None
    when nothing was submitted. Never raises (startup must not be affected)."""
    if is_ready is None:
        from app.core import readiness

        is_ready = readiness.is_ready
    if submit is None:
        from app.services import background_jobs

        submit = background_jobs.submit
    try:
        port = repo.maintenance
        if not port.global_ask_attribution_schema_ready():
            logger.warning("global ask attribution: schema not migrated; backfill not scheduled")
            return None
        pending = int(port.global_ask_unattributed_count())
        logger.info("global ask attribution: %d done job(s) with attributed=0", pending)
        if not pending or not is_ready():
            return None
        return submit(run_background_pass, port, is_ready, name=JOB_NAME)
    except Exception as exc:  # noqa: BLE001 - best-effort, must never affect readiness
        logger.warning("global ask attribution: scheduling failed (%s)", type(exc).__name__)
        return None


def run_background_pass(port: AttributionBackfillPort, is_ready: Callable[[], bool]) -> BackfillResult | None:
    """The background pass itself: one ``run_backfill`` over everything
    pending. Refuses while the process is not ready; logs counts and an
    exception CLASS only."""
    if not is_ready():
        logger.info("global ask attribution: service not ready; pass refused")
        return None
    try:
        result = run_backfill(port)
    except Exception as exc:  # noqa: BLE001 - what is left waits for the next start
        logger.warning("global ask attribution backfill failed (%s)", type(exc).__name__)
        return None
    logger.info(
        "global ask attribution backfill: written=%d skipped=%d unreadable=%d pending_after=%d",
        result.written, result.skipped, result.unreadable, result.pending_after,
    )
    return result


@contextmanager
def open_attribution_repository(settings: Any) -> Iterator[Any]:
    """Compose the repository WITHOUT owning its schema: no migration (DDL a
    running service never asked for), no seed (whose unconditional admin
    password rewrite would reset the production credential whenever the
    operator's environment lacks it). The shared schema-guest gate raises
    ``MaintenanceCliSchemaError`` before anything is composed. Bare (no
    extension seats): the pass only reads and writes attribution rows."""
    from app.services.maintenance_cli import create_bare_schema_guest_repository

    repository = create_bare_schema_guest_repository(settings)
    try:
        yield repository
    finally:
        repository.close()


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    from app.core.config import Settings

    parser = argparse.ArgumentParser(
        prog="backfill_global_ask_attribution",
        description="为升级前已完成的全局问答补写归因行（global_ask_job_notebooks）；可续跑、可在服务运行时执行",
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help=f"每页作业数（每页一个事务；默认 {DEFAULT_BATCH_SIZE}）")
    parser.add_argument("--limit", type=int, default=None, help="本次最多处理多少个作业")
    parser.add_argument("--after-id", default="",
                        help="keyset 续跑起点：只处理 id 严格大于它的作业（原样重跑也安全，只是会重读一遍已报告的坏行）")
    parser.add_argument("--report", type=Path, default=None,
                        help="读不回的作业逐行写入 JSONL（只写作业 id 与原因码）")
    parser.add_argument("--dry-run", action="store_true", help="只读、只计数，不写库")
    args = parser.parse_args(argv)
    if args.batch_size < 1 or (args.limit is not None and args.limit < 1):
        parser.error("--batch-size / --limit 必须为正")

    def progress(result: BackfillResult) -> None:
        print(f"scanned={result.scanned} written={result.written} skipped={result.skipped} "
              f"unreadable={result.unreadable} last_id={result.last_id}", flush=True)

    from app.services.maintenance_cli import MaintenanceCliError

    try:
        with open_attribution_repository(Settings()) as repository:
            result = run_backfill(
                repository.maintenance,
                after_id=args.after_id,
                limit=args.limit,
                batch_size=args.batch_size,
                dry_run=args.dry_run,
                on_unreadable=_report_writer(args.report),
                progress=progress,
            )
    except MaintenanceCliError as exc:  # schema refusal: 3; unreachable database: 2
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return exc.exit_code
    label = "dry-run" if args.dry_run else "done"
    print(f"backfill-global-ask-attribution {label}: {json.dumps(result.as_dict())}", flush=True)
    return 1 if result.unreadable else 0
