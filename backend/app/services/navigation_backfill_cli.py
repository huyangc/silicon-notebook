"""Operator CLI of the navigation-page backfill
(``scripts/backfill_navigation_chunks.py``; the pass itself is
``app.services.navigation_backfill``).

A dry run (``--dry-run``: per-source counts, ``--show-sections`` lists the
flagged chunks' section labels), a manual re-run (pending sources only;
``--rescan`` judges every chunked source in scope) and ``--clear`` (data-level
rollback: every flag back to 0, every stamp back to NULL). The scope is
explicit: ``--notebook-id`` or ``--all-notebooks``. It is a schema guest
(never migrates, never seeds); writes use the same compare-and-set as the
service pass and need no in-process lock, and on PostgreSQL they require
``--confirm-service-stopped`` (the maintenance boundary also holds the
database-wide maintenance lock). A dry run needs neither.

Exit status: 0 done; 1 some sources were not judged (generation changed under
the pass, a source chunk lock held, or a source failed) -- re-run; 2 usage
error (including a PostgreSQL write without ``--confirm-service-stopped``, or
a write with ``NAVIGATION_DETECTION_ENABLED=false``); 3 the database is not at
this build's schema. The database URL is never printed.
"""
from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

from app.repositories.schema_currency import EXIT_SCHEMA_NOT_CURRENT
from app.services.navigation_backfill import SourceJudgement, run_backfill

EXIT_SCHEMA_NOT_READY = EXIT_SCHEMA_NOT_CURRENT  # 3, shared by every schema guest


@contextmanager
def _open_repository(settings: Any, *, write: bool, confirm_service_stopped: bool) -> Iterator[Any]:
    """Schema guest either way. A dry run composes the bare guest (reads
    only); a write goes through the maintenance boundary, which requires
    ``--confirm-service-stopped`` on PostgreSQL before composing anything and
    holds the database-wide maintenance lock."""
    from app.services.maintenance_cli import (
        create_bare_schema_guest_repository,
        open_maintenance_cli_repository,
    )

    if write:
        with open_maintenance_cli_repository(
            settings, confirm_service_stopped=confirm_service_stopped
        ) as repository:
            yield repository
        return
    repository = create_bare_schema_guest_repository(settings)
    try:
        yield repository
    finally:
        repository.close()


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    from app.core.config import Settings
    from app.services.maintenance_cli import MaintenanceCliError

    parser = argparse.ArgumentParser(
        prog="backfill_navigation_chunks",
        description=(
            "判定存量来源的导航页（目录、图表清单）并写 chunks.navigation；"
            "服务就绪后会自动在后台跑一遍，本工具用于 dry run、手动重跑与 --clear 回滚"
        ),
    )
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--notebook-id", default=None, help="只处理这一本笔记本")
    scope.add_argument("--all-notebooks", action="store_true", help="处理全部笔记本")
    parser.add_argument("--dry-run", action="store_true",
                        help="只判定、逐来源打印块数/命中数/将改变的块数，不写库")
    parser.add_argument("--show-sections", action="store_true",
                        help="dry run 时列出被判为导航页的块的章节标签")
    parser.add_argument("--rescan", action="store_true",
                        help="重判范围内全部已分块来源（默认只判未判定或已过期的来源）")
    parser.add_argument("--clear", action="store_true",
                        help="数据级回滚：范围内所有导航标记清 0、判定凭证清空")
    parser.add_argument("--confirm-service-stopped", action="store_true",
                        help="PostgreSQL 上写库（重跑或 --clear）前必须确认服务已停")
    args = parser.parse_args(argv)
    if args.show_sections and not args.dry_run:
        parser.error("--show-sections 只能与 --dry-run 一起用")
    if args.clear and (args.dry_run or args.rescan):
        parser.error("--clear 不能与 --dry-run / --rescan 同用")
    notebook_id = None if args.all_notebooks else args.notebook_id

    settings = Settings()
    if not args.dry_run and not args.clear and not settings.navigation_detection_enabled:
        print("error: NAVIGATION_DETECTION_ENABLED=false：判定已关闭，不写入导航标记"
              "（可先 --dry-run 预览）", file=sys.stderr, flush=True)
        return 2

    def report(judgement: SourceJudgement) -> None:
        if not args.dry_run and judgement.outcome == "judged" and not judgement.changed:
            return
        line = (f"source={judgement.source_id} notebook={judgement.notebook_id} "
                f"outcome={judgement.outcome} chunks={judgement.chunks} "
                f"flagged={judgement.flagged} changed={judgement.changed}")
        if judgement.skipped:
            line += f" skipped={judgement.skipped}"
        print(line, flush=True)
        for section in judgement.flagged_sections:
            print(f"  section: {section}", flush=True)

    try:
        with _open_repository(
            settings, write=not args.dry_run,
            confirm_service_stopped=args.confirm_service_stopped,
        ) as repository:
            runtime = repository._runtime
            store = runtime.chunk_store
            mark_dirty = runtime.source_chunking.mark_unified_dirty
            if args.clear:
                cleared, reset, notebooks = store.clear_navigation_flags(notebook_id=notebook_id)
                for dirty_notebook in notebooks:
                    mark_dirty(dirty_notebook)
                print("backfill-navigation-chunks clear: " + json.dumps({
                    "chunks_cleared": cleared, "sources_reset": reset,
                    "notebooks": len(notebooks),
                }), flush=True)
                return 0
            result = run_backfill(
                store, mark_dirty=mark_dirty,
                notebook_id=notebook_id, pending_only=not args.rescan,
                dry_run=args.dry_run, event_log=runtime.event_log,
                show_sections=args.show_sections, on_source=report,
            )
    except MaintenanceCliError as exc:  # usage: 2; schema refusal: 3; unreachable: 2
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return exc.exit_code
    label = "dry-run" if args.dry_run else "done"
    print(f"backfill-navigation-chunks {label}: {json.dumps(result.as_dict())}", flush=True)
    return 1 if (result.abandoned or result.failed or result.busy) else 0
