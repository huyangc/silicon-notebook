"""在干净 _payload_text(已去 section_path)上强制重嵌一个 notebook 的知识/关系向量。
用法: PYTHONPATH=backend python -m app.scripts.reembed_kg <notebook_id>
先清空该 nb 的 knowledge_embeddings/relation_embeddings,再全量重嵌(故用 _payload_text 新文本)。
完成后建议再跑 `python -m app.scripts.recluster_kg <nb>`,在干净向量上刷新 canonical 簇。"""
import sys
from app.core.config import get_settings
from app.services.sqlite_repository import SQLiteRepository


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: reembed_kg <notebook_id>"); return 2
    nb = sys.argv[1]
    repo = SQLiteRepository(get_settings())
    required = ("knowledge_object_embedding", "relation_embedding")
    missing = [workload for workload in required if not repo.configured(workload)]
    if missing:
        print(
            "[reembed] ABORT: missing system model workload(s): "
            f"{', '.join(missing)} — vectors were not purged."
        )
        return 2
    mnt = repo.maintenance
    mnt.purge_kg_embeddings(nb)
    rows = mnt.knowledge_object_payloads(nb, include_deprecated=True)
    items = [{"_oid": r["id"], "payload": r["payload"]} for r in rows]
    mnt.embed_objects_batch(nb, items)          # 干净文本重嵌对象
    mnt.backfill_relation_embeddings(nb)        # 关系已清空 → 全量重嵌(名取自干净 _payload_text)
    # 重嵌改变了对象向量内容(COUNT 不变但内容变)→ 标脏使 kg_mutation_seq 前进,
    # 这样即便随后跑 force=False 的 rebuild 也不会因「计数未变」而跳过聚类。
    mnt.mark_unified_kg_dirty(nb)
    print(f"[reembed] {nb}: re-embedded {len(items)} objects + relations"); return 0


if __name__ == "__main__":
    raise SystemExit(main())
