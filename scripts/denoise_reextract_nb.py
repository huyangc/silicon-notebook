"""去噪重抽一个 notebook。RUN WITH BACKEND STOPPED（单写者）。
用法：
  cd /Users/hzf/workspace/silicon_notebook
  # 试抽 1 本（不删全部，仅替换该 source 的 KG，其余不动）：
  PYTHONPATH=backend python scripts/denoise_reextract_nb.py --notebook-id nb-... --pilot src-...
  # 只重抽指定的若干源（逗号分隔，其余源保留），抽完 rebuild：
  PYTHONPATH=backend python scripts/denoise_reextract_nb.py --notebook-id nb-... --sources src-a,src-b
  # 全量（删除该 notebook 的全部 KG，再逐源重抽 + rebuild）：
  PYTHONPATH=backend python scripts/denoise_reextract_nb.py --notebook-id nb-... --full

每个源独立 try/except：一本失败(如 LLM 抖动)不会中断其余源；print 全部 flush，
后台重定向到文件也能实时看进度。
"""
import argparse
import sys
import time

from app.core.config import Settings
from app.services.maintenance_cli import (
    MaintenanceCliError,
    open_maintenance_cli_repository,
)


_PAGE_SIZE = 500


def _run_status(repo, sid):
    r = repo.maintenance.latest_extraction_run(sid)
    return (r["status"], r["error_message"]) if r else ("?", "n/a")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Denoise and re-extract notebook KG")
    parser.add_argument("--notebook-id", required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--pilot", metavar="SOURCE_ID")
    action.add_argument("--sources", metavar="SOURCE_IDS")
    action.add_argument("--full", action="store_true")
    parser.add_argument(
        "--confirm-service-stopped",
        action="store_true",
        help="required before direct PostgreSQL maintenance",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    notebook_id = args.notebook_id
    settings = Settings()
    try:
        with open_maintenance_cli_repository(
            settings, confirm_service_stopped=args.confirm_service_stopped
        ) as repo:
            repo.get_notebook(notebook_id)
            do_rebuild = args.full or bool(args.sources)
            if args.full:
                targets = None
            elif args.sources:
                targets = [s.strip() for s in args.sources.split(",") if s.strip()]
            else:
                targets = [args.pilot]

            if targets is not None:
                invalid = [
                    sid for sid in targets
                    if not repo.maintenance.source_is_user_visible(notebook_id, sid)
                ]
                if invalid:
                    print(
                        "error: source 不存在、不属于指定 notebook，或属于 "
                        "memory/knowhow 隐藏投影: " + ",".join(invalid),
                        file=sys.stderr,
                    )
                    return 2

            # All identity/scope validation must complete before the first write.
            repo.maintenance.set_sources_doc_type(notebook_id, "textbook")
            if args.full:
                print(f"FULL: 删除 {notebook_id} 全部 KG", flush=True)
                print("deleted:", repo.delete_notebook_kg(notebook_id), flush=True)
            elif args.sources:
                print(f"SOURCES: 仅重抽 {targets}（其余源保留）", flush=True)
            else:
                print(f"PILOT: 仅重抽 {args.pilot}（其余 KG 不动）", flush=True)

            ok, failed = [], []

            def _extract(sid: str) -> None:
                t = time.perf_counter()
                try:
                    repo.maintenance.run_extraction(sid)
                    st, msg = _run_status(repo, sid)
                    print(f"[{sid}] {time.perf_counter()-t:.1f}s {st} :: {msg}", flush=True)
                    ok.append(sid)
                except Exception as exc:  # per-source 容错：一本失败不中断其余
                    print(f"[{sid}] FAILED after {time.perf_counter()-t:.1f}s: {type(exc).__name__}: {exc}", flush=True)
                    failed.append(sid)

            if targets is None:
                after_id = ""
                while rows := repo.maintenance.user_source_title_rows_page(
                    notebook_id, after_id=after_id, limit=_PAGE_SIZE
                ):
                    for row in rows:
                        print(
                            f"source: {row['id']} {str(row['title'])[:30]}",
                            flush=True,
                        )
                        _extract(str(row["id"]))
                    after_id = str(rows[-1]["id"])
            else:
                for sid in targets:
                    _extract(sid)
            print(f"done: ok={len(ok)} failed={len(failed)} {failed}", flush=True)

            if do_rebuild:
                print("rebuild clusters:", repo.rebuild_unified_kg(notebook_id), flush=True)
    except MaintenanceCliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
