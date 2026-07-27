"""回填关系向量 CLI:python -m app.scripts.backfill_relation_embeddings <notebook_id>。
对已存在但缺少 relation_embeddings 的笔记本尤其适用(幂等,只补缺失行)。"""
import sys
from app.core.config import get_settings
from app.services.maintenance_cli import (
    MaintenanceCliError,
    open_maintenance_cli_repository,
)


def main() -> int:
    args = [arg for arg in sys.argv[1:] if arg != "--confirm-service-stopped"]
    confirmed = len(args) != len(sys.argv[1:])
    if len(args) != 1:
        print(
            "usage: python -m app.scripts.backfill_relation_embeddings "
            "<notebook_id> [--confirm-service-stopped]",
            file=sys.stderr,
        )
        return 2
    settings = get_settings()
    try:
        with open_maintenance_cli_repository(
            settings, confirm_service_stopped=confirmed
        ) as repo:
            repo.maintenance.backfill_relation_embeddings(args[0])
            print(f"[backfill] relation embeddings done for {args[0]}")
    except MaintenanceCliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
