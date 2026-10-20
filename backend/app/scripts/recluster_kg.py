"""重建一个 notebook 的 canonical 簇(concept_clusters),让检索折叠覆盖全部当前对象。
用法: PYTHONPATH=backend python -m app.scripts.recluster_kg <notebook_id>
      [--cold-partition] [--confirm-service-stopped]

``--cold-partition``:主题板块划分从冷启动开始,不以上一代划分热启动(丢掉划分的历史;
board_key 仍按成员续用)。默认是热启动——`force` 不等于冷启动,见
`KnowledgeLifecycleService._partition_boards_for_rebuild`。"""
import sys
from app.core.config import get_settings
from app.services.maintenance_cli import (
    MaintenanceCliError,
    open_maintenance_cli_repository,
)

_FLAGS = ("--confirm-service-stopped", "--cold-partition")


def main() -> int:
    args = [arg for arg in sys.argv[1:] if arg not in _FLAGS]
    confirmed = "--confirm-service-stopped" in sys.argv[1:]
    cold_partition = "--cold-partition" in sys.argv[1:]
    if len(args) != 1:
        print("usage: recluster_kg <notebook_id> [--cold-partition] [--confirm-service-stopped]")
        return 2
    nb = args[0]
    settings = get_settings()
    try:
        with open_maintenance_cli_repository(
            settings, confirm_service_stopped=confirmed
        ) as repo:
            if cold_partition:
                # 持久请求:这次重建没跑完、或被在飞的重建挡住,下一次照样冷启动。
                repo.request_cold_community_partition(nb)
                print(f"[recluster] {nb}: cold board partition requested")
            # Explicit recluster entrypoint → force a full recompute (bypass the
            # skip-when-unchanged gate; the whole point of this script is to re-cluster).
            n = repo.rebuild_unified_kg(nb, force=True)
            print(f"[recluster] {nb}: rebuilt unified KG (clusters={n})")
            if cold_partition:
                _report_cold_partition(nb, repo.community_partition_state(nb))
    except MaintenanceCliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    return 0


def _report_cold_partition(nb: str, state: dict) -> None:
    """Say what happened to the cold request, not what was hoped for. Version 0
    after the rebuild means the request is still pending: this run did not
    publish a partition of its own (another rebuild held the notebook and
    published over it, or the community layer is off) -- or it partitioned
    without igraph, whose networkx result is stamped 0 on purpose."""
    if int(state["version"]) != 0:
        print(f"[recluster] {nb}: boards re-partitioned ({state['mode']})")
    elif state["mode"] == "cold_fallback":
        print(f"[recluster] {nb}: boards re-partitioned without igraph (networkx fallback); "
              "the next rebuild with igraph starts cold")
    else:
        print(f"[recluster] {nb}: this run did not re-partition the boards; the request is "
              "recorded and the next rebuild that re-partitions the board graph starts cold")


if __name__ == "__main__":
    raise SystemExit(main())
