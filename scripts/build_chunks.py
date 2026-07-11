"""为现有 notebook 回填 chunk + chunk_embedding(不重抽 KG)。
用法: PYTHONPATH=backend python scripts/build_chunks.py <notebook_id>"""
import sys
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


def main():
    nb = sys.argv[1] if len(sys.argv) > 1 else None
    if not nb:
        print("usage: build_chunks.py <notebook_id>"); sys.exit(2)
    repo = SQLiteRepository(Settings())
    mnt = repo.maintenance
    sids = mnt.source_ids(nb)
    print(f"sources: {len(sids)}", flush=True)
    for i, sid in enumerate(sids, 1):
        try:
            mnt.chunk_and_embed_source(sid)
            print(f"[{i}/{len(sids)}] {sid} ok", flush=True)
        except Exception as exc:
            print(f"[{i}/{len(sids)}] {sid} FAILED: {exc}", flush=True)
    print(f"total chunks: {mnt.count_chunks(nb)}", flush=True)


if __name__ == "__main__":
    main()
