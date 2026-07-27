"""为现有 notebook 回填 chunk + chunk_embedding(不重抽 KG)。
用法: PYTHONPATH=backend python scripts/build_chunks.py <notebook_id>"""
import sys
from app.core.config import Settings
from app.services.maintenance_cli import (
    MaintenanceCliError,
    open_maintenance_cli_repository,
)


_PAGE_SIZE = 500


def main() -> int:
    args = [arg for arg in sys.argv[1:] if arg != "--confirm-service-stopped"]
    confirmed = len(args) != len(sys.argv[1:])
    if len(args) != 1:
        print("usage: build_chunks.py <notebook_id> [--confirm-service-stopped]")
        return 2
    notebook_id = args[0]
    settings = Settings()
    try:
        with open_maintenance_cli_repository(
            settings, confirm_service_stopped=confirmed
        ) as repo:
            mnt = repo.maintenance
            processed = 0
            after_id = ""
            while sids := mnt.user_source_ids_page(
                notebook_id, after_id=after_id, limit=_PAGE_SIZE
            ):
                for sid in sids:
                    processed += 1
                    try:
                        mnt.chunk_and_embed_source(sid)
                        print(f"[{processed}] {sid} ok", flush=True)
                    except Exception as exc:
                        print(f"[{processed}] {sid} FAILED: {exc}", flush=True)
                after_id = sids[-1]
            print(f"sources: {processed}", flush=True)
            print(f"total chunks: {mnt.count_chunks(notebook_id)}", flush=True)
    except MaintenanceCliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
