"""回填关系向量 CLI:python -m app.scripts.backfill_relation_embeddings <notebook_id>。
对已存在但缺少 relation_embeddings 的笔记本尤其适用(幂等,只补缺失行)。"""
import sys
from app.core.config import get_settings
from app.services.sqlite_repository import SQLiteRepository


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m app.scripts.backfill_relation_embeddings <notebook_id>",
              file=sys.stderr)
        return 2
    repo = SQLiteRepository(get_settings())
    repo.maintenance.backfill_relation_embeddings(sys.argv[1])
    print(f"[backfill] relation embeddings done for {sys.argv[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
