"""全量补全一个 notebook 的 knowledge object 向量（限流友好）。

knowledge 对象向量在 store_kg 入库时嵌入，但 embed_concurrency 默认很高（50）；
批量并发上传大量文档时会瞬间打爆 embedding 服务的 QPS（dashscope 返回 429
limit_requests），导致大批对象向量缺失。本脚本以**低并发 + 多轮 + 轮间退避**
显式补齐缺失对象：每轮只补仍缺的（幂等），429 的批下一轮再试，直到嵌齐或连续
多轮无进展。

用法：
  EMBED_CONCURRENCY=4 PYTHONPATH=backend python scripts/backfill_kg_embeddings.py [notebook_id]
环境变量：
  EMBED_CONCURRENCY   每轮嵌入并发（默认 4，越低越不易 429）
  BACKFILL_SLEEP      轮间退避秒数（默认 3）
  BACKFILL_MAX_ROUNDS 最大轮数（默认 80）
"""
import os
import sys
import time

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository

NB = sys.argv[1] if len(sys.argv) > 1 else "nb-b37185f4ae"
CONC = int(os.environ.get("EMBED_CONCURRENCY", "4"))
SLEEP = float(os.environ.get("BACKFILL_SLEEP", "3"))
MAX_ROUNDS = int(os.environ.get("BACKFILL_MAX_ROUNDS", "80"))


def _counts(repo, nb):
    return repo.maintenance.node_embedding_counts(nb)


def main():
    repo = SQLiteRepository(Settings())
    if not repo.settings.embedder_configured:
        print("embedder NOT configured; abort", flush=True)
        sys.exit(1)
    try:
        repo.settings.embed_concurrency = CONC  # 降并发避 429
    except Exception:
        pass  # env EMBED_CONCURRENCY 已生效则忽略
    objs_n, before = _counts(repo, NB)
    print(f"start objects={objs_n} embedded={before} missing={objs_n - before} "
          f"conc={CONC} sleep={SLEEP}s", flush=True)
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
