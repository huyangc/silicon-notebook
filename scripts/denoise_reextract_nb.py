"""去噪重抽一个 notebook。RUN WITH BACKEND STOPPED（单写者）。
用法：
  cd /Users/hzf/workspace/silicon_notebook
  # 试抽 1 本（不删全部，仅替换该 source 的 KG）：
  PYTHONPATH=backend python scripts/denoise_reextract_nb.py --pilot src-8286a380ae
  # 全量（删 nb-012 全部 KG，再 5 本逐源重抽 + rebuild）：
  PYTHONPATH=backend python scripts/denoise_reextract_nb.py --full
"""
import sys
import time

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository

NB = "nb-012fb94249"


def main():
    args = sys.argv[1:]
    pilot = args[args.index("--pilot") + 1] if "--pilot" in args else None
    full = "--full" in args
    if not pilot and not full:
        print("need --pilot SRC_ID or --full")
        sys.exit(2)

    repo = SQLiteRepository(Settings())
    with repo._connect() as db:
        srcs = db.execute("SELECT id, title FROM sources WHERE notebook_id=? ORDER BY id", (NB,)).fetchall()
        db.execute("UPDATE sources SET doc_type='textbook' WHERE notebook_id=?", (NB,))
    print("sources:", [(r["id"], r["title"][:30]) for r in srcs])

    if pilot:
        targets = [pilot]
        print(f"PILOT: 仅重抽 {pilot}（其余 KG 不动）")
    else:
        print("FULL: 删除 nb-012 全部 KG")
        print("deleted:", repo.delete_notebook_kg(NB))
        targets = [r["id"] for r in srcs]

    for sid in targets:
        t = time.perf_counter()
        repo._run_extraction(sid)
        with repo._connect() as db:
            run = db.execute(
                "SELECT error_message FROM extraction_runs WHERE source_id=? ORDER BY created_at DESC LIMIT 1",
                (sid,),
            ).fetchone()
        print(f"[{sid}] {time.perf_counter()-t:.1f}s :: {run['error_message'] if run else 'n/a'}")

    if full:
        print("rebuild clusters:", repo.rebuild_unified_kg(NB))


if __name__ == "__main__":
    main()
