"""在干净 _payload_text(已去 section_path)上强制重嵌一个 notebook 的知识/关系向量。
用法: PYTHONPATH=backend python -m app.scripts.reembed_kg <notebook_id>
先清空该 nb 的 knowledge_embeddings/relation_embeddings,再全量重嵌(故用 _payload_text 新文本)。"""
import json, sys
from app.core.config import get_settings
from app.services.sqlite_repository import SQLiteRepository


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: reembed_kg <notebook_id>"); return 2
    nb = sys.argv[1]
    repo = SQLiteRepository(get_settings())
    with repo._write() as db:
        db.execute("DELETE FROM knowledge_embeddings WHERE notebook_id=?", (nb,))
        db.execute("DELETE FROM relation_embeddings WHERE notebook_id=?", (nb,))
    with repo._connect() as db:
        rows = db.execute("SELECT id, payload FROM knowledge_objects WHERE notebook_id=?", (nb,)).fetchall()
    items = [{"_oid": r["id"], "payload": json.loads(r["payload"] or "{}")} for r in rows]
    repo._embed_objects_batch(nb, items)        # 干净文本重嵌对象
    repo._backfill_relation_embeddings(nb)      # 关系已清空 → 全量重嵌(名取自干净 _payload_text)
    print(f"[reembed] {nb}: re-embedded {len(items)} objects + relations"); return 0


if __name__ == "__main__":
    raise SystemExit(main())
