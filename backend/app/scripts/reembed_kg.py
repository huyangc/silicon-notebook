"""在干净 _payload_text(已去 section_path)上强制重嵌一个 notebook 的知识/关系向量。
用法: PYTHONPATH=backend python -m app.scripts.reembed_kg <notebook_id>
先清空该 nb 的 knowledge_embeddings/relation_embeddings,再全量重嵌(故用 _payload_text 新文本)。
完成后建议再跑 `python -m app.scripts.recluster_kg <nb>`,在干净向量上刷新 canonical 簇。"""
import sys
from app.core.config import get_settings
from app.services.maintenance_cli import (
    MaintenanceCliError,
    open_maintenance_cli_repository,
)


_PAGE_SIZE = 500


def main() -> int:
    args = [arg for arg in sys.argv[1:] if arg != "--confirm-service-stopped"]
    confirmed = len(args) != len(sys.argv[1:])
    if len(args) != 1:
        print("usage: reembed_kg <notebook_id> [--confirm-service-stopped]")
        return 2
    nb = args[0]
    settings = get_settings()
    try:
        with open_maintenance_cli_repository(
            settings, confirm_service_stopped=confirmed
        ) as repo:
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
            after_id = ""
            object_count = 0
            while True:
                rows = mnt.knowledge_object_payload_page(
                    nb,
                    after_id=after_id,
                    limit=_PAGE_SIZE,
                    include_deprecated=True,
                )
                if not rows:
                    break
                items = [{"_oid": r["id"], "payload": r["payload"]} for r in rows]
                mnt.embed_objects_batch(nb, items)      # 干净文本重嵌对象
                object_count += len(items)
                after_id = str(rows[-1]["id"])
                if len(rows) < _PAGE_SIZE:
                    break
            mnt.backfill_relation_embeddings(nb)        # 关系已清空 → 全量重嵌(名取自干净 _payload_text)
            # 重嵌改变了对象向量内容(COUNT 不变但内容变)→ 标脏使 kg_mutation_seq 前进,
            # 这样即便随后跑 force=False 的 rebuild 也不会因「计数未变」而跳过聚类。
            mnt.mark_unified_kg_dirty(nb)
            print(f"[reembed] {nb}: re-embedded {object_count} objects + relations")
            return 0
    except MaintenanceCliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
