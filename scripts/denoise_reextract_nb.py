"""去噪重抽一个 notebook。RUN WITH BACKEND STOPPED（单写者）。
用法：
  cd /Users/hzf/workspace/silicon_notebook
  # 试抽 1 本（不删全部，仅替换该 source 的 KG，其余不动）：
  PYTHONPATH=backend python scripts/denoise_reextract_nb.py --pilot src-8286a380ae
  # 只重抽指定的若干源（逗号分隔，其余源保留），抽完 rebuild：
  PYTHONPATH=backend python scripts/denoise_reextract_nb.py --sources src-a,src-b
  # 全量（删 nb-012 全部 KG，再 5 本逐源重抽 + rebuild）：
  PYTHONPATH=backend python scripts/denoise_reextract_nb.py --full

每个源独立 try/except：一本失败(如 LLM 抖动)不会中断其余源；print 全部 flush，
后台重定向到文件也能实时看进度。
"""
import sys
import time

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository

NB = "nb-012fb94249"


def _run_status(repo, sid):
    r = repo.maintenance.latest_extraction_run(sid)
    return (r["status"], r["error_message"]) if r else ("?", "n/a")


def main():
    args = sys.argv[1:]
    pilot = args[args.index("--pilot") + 1] if "--pilot" in args else None
    sources_arg = args[args.index("--sources") + 1] if "--sources" in args else None
    full = "--full" in args
    if not (pilot or sources_arg or full):
        print("need --pilot SID | --sources SID1,SID2 | --full")
        sys.exit(2)

    repo = SQLiteRepository(Settings())
    srcs = repo.maintenance.source_title_rows(NB)
    repo.maintenance.set_sources_doc_type(NB, "textbook")
    print("sources:", [(r["id"], r["title"][:30]) for r in srcs], flush=True)

    do_rebuild = full or bool(sources_arg)
    if full:
        print("FULL: 删除 nb-012 全部 KG", flush=True)
        print("deleted:", repo.delete_notebook_kg(NB), flush=True)
        targets = [r["id"] for r in srcs]
    elif sources_arg:
        targets = [s.strip() for s in sources_arg.split(",") if s.strip()]
        print(f"SOURCES: 仅重抽 {targets}（其余源保留）", flush=True)
    else:
        targets = [pilot]
        print(f"PILOT: 仅重抽 {pilot}（其余 KG 不动）", flush=True)

    ok, failed = [], []
    for sid in targets:
        t = time.perf_counter()
        try:
            repo.maintenance.run_extraction(sid)
            st, msg = _run_status(repo, sid)
            print(f"[{sid}] {time.perf_counter()-t:.1f}s {st} :: {msg}", flush=True)
            ok.append(sid)
        except Exception as exc:  # per-source 容错：一本失败不中断其余
            print(f"[{sid}] FAILED after {time.perf_counter()-t:.1f}s: {type(exc).__name__}: {exc}", flush=True)
            failed.append(sid)
    print(f"done: ok={len(ok)} failed={len(failed)} {failed}", flush=True)

    if do_rebuild:
        print("rebuild clusters:", repo.rebuild_unified_kg(NB), flush=True)


if __name__ == "__main__":
    main()
