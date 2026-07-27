"""重建一个 notebook 的 canonical 簇(concept_clusters),让检索折叠覆盖全部当前对象。
用法: PYTHONPATH=backend python -m app.scripts.recluster_kg <notebook_id>"""
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
        print("usage: recluster_kg <notebook_id> [--confirm-service-stopped]")
        return 2
    nb = args[0]
    settings = get_settings()
    try:
        with open_maintenance_cli_repository(
            settings, confirm_service_stopped=confirmed
        ) as repo:
            # Explicit recluster entrypoint → force a full recompute (bypass the
            # skip-when-unchanged gate; the whole point of this script is to re-cluster).
            n = repo.rebuild_unified_kg(nb, force=True)
            print(f"[recluster] {nb}: rebuilt unified KG (clusters={n})")
    except MaintenanceCliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
