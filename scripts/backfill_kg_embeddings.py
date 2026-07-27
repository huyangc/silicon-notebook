"""全量补全一个 notebook 的 knowledge object 向量。

模型吞吐由 ``knowledge_object_embedding`` 所绑定服务的 ``max_concurrency``
统一控制。本脚本只负责幂等多轮补缺和轮间退避，不提供第二套并发覆盖。

用法：
  PYTHONPATH=backend python scripts/backfill_kg_embeddings.py <notebook_id>
环境变量：
  BACKFILL_SLEEP      轮间退避秒数（默认 3）
  BACKFILL_MAX_ROUNDS 最大轮数（默认 80）
"""
import os
import sys
import time

from app.core.config import Settings
from app.services.maintenance_cli import (
    MaintenanceCliError,
    open_maintenance_cli_repository,
)

SLEEP = float(os.environ.get("BACKFILL_SLEEP", "3"))
MAX_ROUNDS = int(os.environ.get("BACKFILL_MAX_ROUNDS", "80"))


def _counts(repo, nb):
    return repo.maintenance.node_embedding_counts(nb)


def main() -> int:
    args = [arg for arg in sys.argv[1:] if arg != "--confirm-service-stopped"]
    confirmed = len(args) != len(sys.argv[1:])
    if len(args) != 1:
        print(
            "usage: backfill_kg_embeddings.py "
            "<notebook_id> [--confirm-service-stopped]",
            file=sys.stderr,
        )
        return 2
    notebook_id = args[0]
    settings = Settings()
    try:
        with open_maintenance_cli_repository(
            settings, confirm_service_stopped=confirmed
        ) as repo:
            if not repo.configured("knowledge_object_embedding"):
                print("knowledge_object_embedding workload NOT configured; abort", flush=True)
                return 1
            objs_n, before = _counts(repo, notebook_id)
            missing = repo.maintenance.count_missing_node_vectors(notebook_id)
            print(
                f"start objects={objs_n} embedded={before} missing={missing} "
                f"sleep={SLEEP}s",
                flush=True,
            )
            last = before
            stale = 0
            for rnd in range(MAX_ROUNDS):
                repo.maintenance.backfill_node_embeddings(notebook_id)
                _, now = _counts(repo, notebook_id)
                added = now - last
                next_missing = repo.maintenance.count_missing_node_vectors(notebook_id)
                resolved = missing - next_missing
                print(
                    f"round={rnd} embedded={now}/{objs_n} (+{added}) "
                    f"missing={next_missing}",
                    flush=True,
                )
                if next_missing == 0:
                    print(f"DONE all {objs_n} embedded", flush=True)
                    return 0
                if resolved <= 0:
                    stale += 1
                    if stale >= 4:
                        print(f"STALLED at {now}/{objs_n} (persistent 429?) — stop", flush=True)
                        return 1
                else:
                    stale = 0
                last = now
                missing = next_missing
                time.sleep(SLEEP)
            print(f"reached MAX_ROUNDS at {last}/{objs_n}", flush=True)
            return 1
    except MaintenanceCliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
