"""离线建图 CLI:python -m app.scripts.build_kg <notebook_id>。
对 tier='base' 权威库一次性构建尤其适用(不占交互式服务)。"""
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
            "usage: python -m app.scripts.build_kg "
            "<notebook_id> [--confirm-service-stopped]",
            file=sys.stderr,
        )
        return 2
    settings = get_settings()
    try:
        with open_maintenance_cli_repository(
            settings, confirm_service_stopped=confirmed
        ) as repo:
            print(repo.build_notebook_kg(args[0]))
    except MaintenanceCliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
