"""重建一个 notebook 的 canonical 簇(concept_clusters),让检索折叠覆盖全部当前对象。
用法: PYTHONPATH=backend python -m app.scripts.recluster_kg <notebook_id>"""
import sys
from app.core.config import get_settings
from app.services.sqlite_repository import SQLiteRepository


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: recluster_kg <notebook_id>"); return 2
    nb = sys.argv[1]
    repo = SQLiteRepository(get_settings())
    n = repo.rebuild_unified_kg(nb)
    print(f"[recluster] {nb}: rebuilt unified KG (clusters={n})"); return 0


if __name__ == "__main__":
    raise SystemExit(main())
