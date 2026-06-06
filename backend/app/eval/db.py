"""只读评测取数:不修改任何产品数据。"""
from __future__ import annotations
import json, sqlite3
from typing import Any, Dict, List, Optional


def source_of(evidence_json: Optional[str]) -> Optional[str]:
    """按书归属 = evidence 数组首元素的 source_id。"""
    try:
        ev = json.loads(evidence_json or "[]")
    except (ValueError, TypeError):
        return None
    if isinstance(ev, list) and ev and isinstance(ev[0], dict):
        return ev[0].get("source_id")
    return None


class EvalDB:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        return db

    def objects(self, notebook_id: str, object_type: str) -> List[Dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, payload, evidence FROM knowledge_objects "
                "WHERE notebook_id=? AND object_type=?",
                (notebook_id, object_type)).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            try:
                payload = json.loads(r["payload"] or "{}")
            except (ValueError, TypeError):
                payload = {}
            evidence = json.loads(r["evidence"] or "[]")
            out.append({
                "id": r["id"],
                "name": payload.get("name", ""),
                "payload": payload,
                "evidence": evidence,
                "evidence_count": len(evidence),
                "source_id": source_of(r["evidence"]),
            })
        return out

    def relation_degree(self, notebook_id: str) -> Dict[str, int]:
        deg: Dict[str, int] = {}
        with self._connect() as db:
            rows = db.execute(
                "SELECT source_object_id, target_object_id FROM knowledge_relations "
                "WHERE notebook_id=?", (notebook_id,)).fetchall()
        for r in rows:
            for k in (r["source_object_id"], r["target_object_id"]):
                if k:
                    deg[k] = deg.get(k, 0) + 1
        return deg

    def source_titles(self, notebook_id: str) -> Dict[str, str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, file_name FROM sources WHERE notebook_id=?",
                (notebook_id,)).fetchall()
        return {r["id"]: r["file_name"] for r in rows}
