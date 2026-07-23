"""全量补全一个 notebook 的 knowledge object 向量。

模型吞吐由 ``knowledge_object_embedding`` 所绑定服务的 ``max_concurrency``
统一控制。本脚本只负责幂等多轮补缺和轮间退避，不提供第二套并发覆盖。

用法：
  PYTHONPATH=backend python scripts/backfill_kg_embeddings.py [notebook_id]
环境变量：
  BACKFILL_SLEEP      轮间退避秒数（默认 3）
  BACKFILL_MAX_ROUNDS 最大轮数（默认 80）
"""
import os
import sys
import time

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository

NB = sys.argv[1] if len(sys.argv) > 1 else "nb-b37185f4ae"
SLEEP = float(os.environ.get("BACKFILL_SLEEP", "3"))
MAX_ROUNDS = int(os.environ.get("BACKFILL_MAX_ROUNDS", "80"))


def _counts(repo, nb):
    return repo.maintenance.node_embedding_counts(nb)


def main():
    repo = SQLiteRepository(Settings())
    if not repo.configured("knowledge_object_embedding"):
        print("knowledge_object_embedding workload NOT configured; abort", flush=True)
        sys.exit(1)
    objs_n, before = _counts(repo, NB)
    print(f"start objects={objs_n} embedded={before} missing={objs_n - before} "
          f"service_parallelism={repo._runtime.models.parallelism('knowledge_object_embedding')} "
          f"sleep={SLEEP}s", flush=True)
    last = before
    stale = 0
    for rnd in range(MAX_ROUNDS):
        repo.maintenance.backfill_node_embeddings(NB)
        _, now = _counts(repo, NB)
        added = now - last
        print(f"round={rnd} embedded={now}/{objs_n} (+{added})", flush=True)
        if now >= objs_n:
            print(f"DONE all {objs_n} embedded", flush=True)
            return
        if added <= 0:
            stale += 1
            if stale >= 4:
                print(f"STALLED at {now}/{objs_n} (persistent 429?) — stop", flush=True)
                return
        else:
            stale = 0
        last = now
        time.sleep(SLEEP)
    print(f"reached MAX_ROUNDS at {last}/{objs_n}", flush=True)


if __name__ == "__main__":
    main()
