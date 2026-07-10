"""Knowledge-object persistence store (Task 13).

Owns the knowledge type/list/graph/schema/provenance/FTS SQL plus the
``add_relations`` / ``relations_for_notebook`` compatibility primitives and
the connection-taking chunk writes ``store_kg`` rides.

Composition rules (Gate 5): primitives take the CALLER's connection wherever
the facade owns a transaction/connection boundary today — commit boundaries,
``_write`` trace patches and ``_connect`` spies keep observing every query
because the (possibly wrapped) connection object flows through unchanged.
Only ``get_object_row`` opens its own read connection (plan-frozen signature).
SQL text is moved verbatim — statement-matching failure-injection wrappers in
the frozen suites keep binding.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Dict, List, Optional, Sequence

from app.models.schemas import Evidence
from app.repositories.sqlite.database import SqliteDatabase


class KnowledgeStore:
    def __init__(self, database: SqliteDatabase, seams) -> None:
        self.database = database
        self.seams = seams

    # ------------------------------------------------------------- counts
    @staticmethod
    def count_knowledge(
        db: sqlite3.Connection, notebook_id: str, object_type: str, statuses
    ) -> int:
        placeholders = ",".join("?" for _ in statuses)
        row = db.execute(
            f"SELECT COUNT(*) AS count FROM knowledge_objects "
            f"WHERE notebook_id = ? AND object_type = ? AND status IN ({placeholders})",
            (notebook_id, object_type, *statuses),
        ).fetchone()
        return int(row["count"])

    @staticmethod
    def count_active_objects(db: sqlite3.Connection, notebook_id: str) -> int:
        return int(db.execute(
            "SELECT COUNT(*) c FROM knowledge_objects "
            "WHERE notebook_id=? AND status!='deprecated'", (notebook_id,)
        ).fetchone()["c"])

    @staticmethod
    def type_counts(
        db: sqlite3.Connection, notebook_id: str
    ) -> "tuple[Dict[str, int], Dict[str, str]]":
        rows = db.execute(
            "SELECT object_type, COUNT(*) AS c FROM knowledge_objects "
            "WHERE notebook_id = ? AND status != 'deprecated' "
            "GROUP BY object_type",
            (notebook_id,),
        ).fetchall()
        label_rows = db.execute(
            "SELECT object_type, label FROM object_schemas"
        ).fetchall()
        labels = {r["object_type"]: (r["label"] or r["object_type"]) for r in label_rows}
        counts = {row["object_type"]: int(row["c"]) for row in rows}
        return counts, labels

    # --------------------------------------------------------------- list
    @staticmethod
    def list_knowledge_page(
        db: sqlite3.Connection,
        notebook_id: str,
        object_type: str,
        status: Optional[str],
        offset: int,
        limit: int,
    ) -> "tuple[int, List[dict]]":
        base_query = (
            "FROM knowledge_objects "
            "WHERE notebook_id = ? AND object_type = ?"
        )
        params: List[object] = [notebook_id, object_type]
        if status:
            base_query += " AND status = ?"
            params.append(status)

        total = db.execute(
            f"SELECT COUNT(*) c {base_query}", params
        ).fetchone()["c"]
        rows = db.execute(
            f"SELECT * {base_query} ORDER BY created_at ASC, id ASC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()

        objects: List[dict] = []
        for row in rows:
            keys = row.keys()
            objects.append(
                {
                    "id": row["id"],
                    "payload": json.loads(row["payload"] or "{}"),
                    "evidence": [
                        Evidence(**item)
                        for item in json.loads(row["evidence"] or "[]")
                    ],
                    "status": row["status"],
                    "owner": row["owner"],
                    "last_reviewed": row["last_reviewed"] if "last_reviewed" in keys else "",
                }
            )
        return int(total), objects

    # -------------------------------------------------------------- graph
    @staticmethod
    def graph_node_rows(db: sqlite3.Connection, notebook_id: str) -> List[sqlite3.Row]:
        return db.execute(
            "SELECT id, object_type, status, payload FROM knowledge_objects "
            "WHERE notebook_id = ? AND status != 'deprecated'", (notebook_id,)
        ).fetchall()

    @staticmethod
    def relations_for_notebook(db: sqlite3.Connection, notebook_id: str) -> List[dict]:
        rows = db.execute(
            "SELECT * FROM knowledge_relations WHERE notebook_id = ?",
            (notebook_id,),
        ).fetchall()
        return [
            {
                "id": r["id"], "source_id": r["source_id"],
                "source_object_id": r["source_object_id"],
                "target_object_id": r["target_object_id"], "edge_type": r["edge_type"],
                "evidence": json.loads(r["evidence"] or "[]"),
            }
            for r in rows
        ]

    def add_relations(
        self,
        db: sqlite3.Connection,
        notebook_id: str,
        source_id: str,
        relations: List[dict],
        now: str,
    ) -> int:
        for rel in relations:
            db.execute(
                """
                INSERT INTO knowledge_relations
                (id, notebook_id, source_id, source_object_id, target_object_id,
                 edge_type, evidence, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.seams.new_id("rel"), notebook_id, source_id,
                    rel["source_object_id"], rel["target_object_id"],
                    rel["edge_type"],
                    json.dumps(rel.get("evidence", []), ensure_ascii=False),
                    now,
                ),
            )
        return len(relations)

    # ------------------------------------------------- store_kg chunk writes
    @staticmethod
    def insert_object_chunk(
        connection: sqlite3.Connection, rows: Sequence[tuple]
    ) -> None:
        connection.executemany(
            "INSERT INTO knowledge_objects "
            "(id, notebook_id, object_type, status, owner, payload, evidence, "
            "source_candidate_id, source_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, '', ?, ?, NULL, ?, ?, ?)",
            rows,
        )

    @staticmethod
    def insert_relation_chunk(
        connection: sqlite3.Connection, rows: Sequence[tuple]
    ) -> None:
        connection.executemany(
            "INSERT INTO knowledge_relations "
            "(id, notebook_id, source_id, source_object_id, target_object_id, "
            "edge_type, evidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

    @staticmethod
    def insert_kg_fts_rows(
        connection: sqlite3.Connection, rows: Sequence[tuple]
    ) -> None:
        connection.executemany(
            "INSERT INTO kg_objects_fts(object_id, notebook_id, name) "
            "VALUES (?, ?, ?)",
            rows,
        )

    @staticmethod
    def insert_object_source_rows(
        connection: sqlite3.Connection, rows: Sequence[tuple]
    ) -> None:
        """Forward maintenance (P0-4 reverse index) for FRESH inserts — rows
        never had prior entries, so a plain batched INSERT suffices (no
        DELETE-first)."""
        connection.executemany(
            "INSERT INTO knowledge_object_sources (object_id, source_id, notebook_id) "
            "VALUES (?, ?, ?)",
            rows,
        )

    def get_object_row(
        self, notebook_id: str, object_id: str
    ) -> "sqlite3.Row | None":
        with self.database.connect() as db:
            return db.execute(
                "SELECT * FROM knowledge_objects WHERE id=? AND notebook_id=?",
                (object_id, notebook_id),
            ).fetchone()

    # --------------------------------------------------------- provenance
    @staticmethod
    def source_ids_from_evidence(evidence_json: Optional[str]) -> set:
        """PURE: parse an evidence JSON TEXT column value into the set of distinct
        source_ids it references (Evidence.source_id is present on every item —
        confirmed in app/models/schemas.py; a merged object's evidence can span
        multiple sources, which is exactly why a per-object single source_id
        column is insufficient and this reverse table exists)."""
        try:
            items = json.loads(evidence_json or "[]")
        except json.JSONDecodeError:
            items = []
        return {
            item.get("source_id")
            for item in items
            if isinstance(item, dict) and item.get("source_id")
        }

    @classmethod
    def replace_object_sources(
        cls,
        connection: sqlite3.Connection,
        object_id: str,
        notebook_id: str,
        evidence_json: Optional[str],
    ) -> None:
        """Forward maintenance: replace object_id's rows in the reverse index with
        the source_ids its CURRENT evidence references. Called by every write path
        that creates/updates a knowledge_objects row with evidence (store_kg,
        confirm_promotion insert/merge, merge_knowledge). Delete-then-insert keeps
        this correct even when evidence shrinks (not currently possible, but cheap
        to keep safe)."""
        connection.execute(
            "DELETE FROM knowledge_object_sources WHERE object_id = ?", (object_id,)
        )
        source_ids = cls.source_ids_from_evidence(evidence_json)
        if source_ids:
            connection.executemany(
                "INSERT INTO knowledge_object_sources (object_id, source_id, notebook_id) "
                "VALUES (?, ?, ?)",
                [(object_id, sid, notebook_id) for sid in source_ids],
            )

    @staticmethod
    def delete_object_sources(
        connection: sqlite3.Connection, object_ids: List[str]
    ) -> None:
        """Deletion coherence: drop reverse-index rows for objects that are
        actually removed from knowledge_objects (source delete/reparse path).
        merge_knowledge does NOT call this — it deprecates the losing object
        in place rather than deleting it, so that object's evidence (now folded
        into the target too, but still physically present on its own row) must
        stay indexed until it is truly deleted."""
        if not object_ids:
            return
        placeholders = ",".join("?" for _ in object_ids)
        connection.execute(
            f"DELETE FROM knowledge_object_sources WHERE object_id IN ({placeholders})",
            object_ids,
        )

    @staticmethod
    def source_index_backfilled(db: sqlite3.Connection, notebook_id: str) -> bool:
        row = db.execute(
            "SELECT source_index_backfilled FROM unified_kg_state WHERE notebook_id=?",
            (notebook_id,),
        ).fetchone()
        return bool(row and row["source_index_backfilled"])

    def mark_source_index_backfilled(
        self, db: sqlite3.Connection, notebook_id: str
    ) -> None:
        now = self.seams.now()
        db.execute(
            """
            INSERT INTO unified_kg_state (notebook_id, dirty, kg_mutation_seq, source_index_backfilled, updated_at)
            VALUES (?, 0, 0, 1, ?)
            ON CONFLICT(notebook_id) DO UPDATE SET
              source_index_backfilled=1,
              updated_at=excluded.updated_at
            """,
            (notebook_id, now),
        )

    def stale_object_ids_for_source(
        self, db: sqlite3.Connection, source_id: str, notebook_id: str
    ) -> List[str]:
        """Return knowledge_objects.id values whose evidence references source_id.

        Fast path (backfilled notebooks): a single indexed SQL lookup against
        knowledge_object_sources — O(matches), not O(notebook size).

        Legacy path (not yet backfilled): the original full-evidence-JSON scan
        of every object in the notebook — but the scan the caller was about to
        pay anyway is reused to populate knowledge_object_sources for every
        object encountered, and the notebook is marked backfilled, so it is
        provably the LAST time this notebook pays the O(N) cost (backfill-on-
        first-use)."""
        if self.source_index_backfilled(db, notebook_id):
            rows = db.execute(
                "SELECT DISTINCT object_id FROM knowledge_object_sources "
                "WHERE source_id = ? AND notebook_id = ?",
                (source_id, notebook_id),
            ).fetchall()
            return [r["object_id"] for r in rows]

        stale_knowledge_ids: List[str] = []
        knowledge_rows = db.execute(
            "SELECT id, evidence FROM knowledge_objects WHERE notebook_id = ?",
            (notebook_id,),
        ).fetchall()
        for row in knowledge_rows:
            source_ids = self.source_ids_from_evidence(row["evidence"])
            self.replace_object_sources(db, row["id"], notebook_id, row["evidence"])
            if source_id in source_ids:
                stale_knowledge_ids.append(row["id"])
        self.mark_source_index_backfilled(db, notebook_id)
        return stale_knowledge_ids

    def clear_source_extraction_state(
        self,
        db: sqlite3.Connection,
        source_id: str,
        notebook_id: str,
        *,
        clear_embeddings: bool,
    ) -> None:
        # KG writes directly to knowledge_objects; find stale objects by evidence source_id
        # (see stale_object_ids_for_source for the reverse-index/legacy-scan split).
        stale_knowledge_ids = self.stale_object_ids_for_source(db, source_id, notebook_id)

        if stale_knowledge_ids:
            placeholders = ",".join("?" for _ in stale_knowledge_ids)
            db.execute(
                f"DELETE FROM knowledge_embeddings WHERE object_id IN ({placeholders})",
                stale_knowledge_ids,
            )
            db.execute(
                f"DELETE FROM knowledge_objects WHERE id IN ({placeholders})",
                stale_knowledge_ids,
            )
            self.delete_object_sources(db, stale_knowledge_ids)
        db.execute("DELETE FROM extraction_runs WHERE source_id = ?", (source_id,))
        if clear_embeddings:
            db.execute("DELETE FROM element_embeddings WHERE source_id = ?", (source_id,))

    @staticmethod
    def delete_relations_for_source(db: sqlite3.Connection, source_id: str) -> None:
        db.execute("DELETE FROM knowledge_relations WHERE source_id = ?", (source_id,))

    def begin_extraction_run(
        self,
        db: sqlite3.Connection,
        source_id: str,
        notebook_id: str,
        run_id: str,
        created_at: str,
    ) -> None:
        """Reset one source's prior KG artefacts and open its extraction_runs
        row — the caller owns the ONE write transaction this rides (the exact
        commit boundary the inline _run_extraction body always had)."""
        self.clear_source_extraction_state(db, source_id, notebook_id, clear_embeddings=False)
        self.delete_relations_for_source(db, source_id)
        db.execute(
            "DELETE FROM knowledge_embeddings WHERE object_id IN "
            "(SELECT id FROM knowledge_objects WHERE source_id = ?)",
            (source_id,),
        )
        direct_ids = [
            r["id"] for r in db.execute(
                "SELECT id FROM knowledge_objects WHERE source_id = ?", (source_id,)
            ).fetchall()
        ]
        db.execute("DELETE FROM knowledge_objects WHERE source_id = ?", (source_id,))
        self.delete_object_sources(db, direct_ids)
        db.execute(
            """INSERT INTO extraction_runs
               (id, notebook_id, source_id, run_type, status, error_message, created_at, updated_at)
               VALUES (?, ?, ?, 'kg', 'running', '', ?, ?)""",
            (run_id, notebook_id, source_id, created_at, created_at))

    @staticmethod
    def finish_extraction_run(
        db: sqlite3.Connection, run_id: str, status: str, message: str, now: str
    ) -> None:
        db.execute(
            "UPDATE extraction_runs SET status=?, error_message=?, updated_at=? WHERE id=?",
            (status, message, now, run_id),
        )

    # ------------------------------------------------------------------ FTS
    @staticmethod
    def fts_search(db, notebook_id: str, q: str, k: int = 30) -> List[Dict]:
        """FTS5 MATCH(kg_objects_fts, trigram)。notebook 维度过滤。返回
        [{object_id, name, score, match:'lexical'}]。q 空 → []。"""
        needle = (q or "").strip()
        if not needle:
            return []
        rows = db.execute(
            "SELECT object_id, name, bm25(kg_objects_fts) AS rank "
            "FROM kg_objects_fts WHERE notebook_id=? AND kg_objects_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            (notebook_id, '"' + needle.replace('"', '""') + '"', k)).fetchall()
        return [{"object_id": r["object_id"], "name": r["name"],
                 "score": -float(r["rank"]), "match": "lexical"} for r in rows]

    @staticmethod
    def chunk_fts_search(db, notebook_id: str, q: str, k: int = 30) -> List[Dict]:
        """FTS5 MATCH(chunks_fts, trigram)。notebook 维度过滤。返回
        [{chunk_id, score, match:'lexical'}]。q 空 → []。"""
        needle = (q or "").strip()
        if not needle:
            return []
        rows = db.execute(
            "SELECT chunk_id, bm25(chunks_fts) AS rank FROM chunks_fts "
            "WHERE notebook_id=? AND chunks_fts MATCH ? ORDER BY rank LIMIT ?",
            (notebook_id, '"' + needle.replace('"', '""') + '"', k)).fetchall()
        return [{"chunk_id": r["chunk_id"], "score": -float(r["rank"]),
                 "match": "lexical"} for r in rows]

    @staticmethod
    def backfill_fts(db: sqlite3.Connection, notebook_id: str) -> int:
        """Re-populate kg_objects_fts from knowledge_objects for this notebook.
        Idempotent: deletes existing FTS rows first, then re-inserts from
        knowledge_objects (non-deprecated, non-empty name). Returns the number
        of rows inserted."""
        db.execute("DELETE FROM kg_objects_fts WHERE notebook_id=?", (notebook_id,))
        rows = db.execute(
            "SELECT id, payload FROM knowledge_objects "
            "WHERE notebook_id=? AND status != 'deprecated'",
            (notebook_id,),
        ).fetchall()
        fts_rows = []
        for r in rows:
            try:
                payload = json.loads(r["payload"] or "{}")
            except Exception:
                payload = {}
            name = (payload.get("name") or "").strip()
            if name:
                fts_rows.append((r["id"], notebook_id, name))
        if fts_rows:
            db.executemany(
                "INSERT INTO kg_objects_fts(object_id, notebook_id, name) VALUES (?,?,?)",
                fts_rows,
            )
        return len(fts_rows) if fts_rows else 0

    @staticmethod
    def object_meta_rows(db: sqlite3.Connection, ids: List[str]) -> List[sqlite3.Row]:
        placeholders = ",".join("?" for _ in ids)
        return db.execute(
            f"SELECT id, object_type, status, payload FROM knowledge_objects "
            f"WHERE id IN ({placeholders})",
            ids,
        ).fetchall()

    # ------------------------------------------------------------- schemas
    @staticmethod
    def schema_rows(db: sqlite3.Connection) -> List[sqlite3.Row]:
        return db.execute("SELECT * FROM object_schemas").fetchall()

    @staticmethod
    def active_schema_rows(db: sqlite3.Connection) -> List[sqlite3.Row]:
        return db.execute(
            "SELECT * FROM object_schemas WHERE status = 'active'"
        ).fetchall()

    @staticmethod
    def schema_row(db: sqlite3.Connection, object_type: str) -> "sqlite3.Row | None":
        return db.execute(
            "SELECT * FROM object_schemas WHERE object_type = ?", (object_type,)
        ).fetchone()

    @staticmethod
    def schema_exists(db: sqlite3.Connection, object_type: str) -> bool:
        return db.execute(
            "SELECT 1 FROM object_schemas WHERE object_type = ?", (object_type,)
        ).fetchone() is not None

    @staticmethod
    def existing_schema_types(db: sqlite3.Connection) -> set:
        return {
            r["object_type"]
            for r in db.execute("SELECT object_type FROM object_schemas").fetchall()
        }

    @staticmethod
    def insert_custom_schema(
        db: sqlite3.Connection,
        object_type: str,
        plural: str,
        fields_json: str,
        primary: str,
        description: str,
        label: str,
        list_fields_json: str,
        now: str,
    ) -> None:
        db.execute(
            """
            INSERT INTO object_schemas
            (object_type, plural, fields, primary_field, description, label,
             list_fields, source, status, rationale, notebook_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'custom', 'active', '', '', ?, ?)
            """,
            (object_type, plural, fields_json, primary, description, label,
             list_fields_json, now, now),
        )

    @staticmethod
    def insert_induced_schema(
        db: sqlite3.Connection,
        object_type: str,
        plural: str,
        fields_json: str,
        primary: str,
        description: str,
        label: str,
        rationale: str,
        notebook_id: str,
        now: str,
    ) -> None:
        db.execute(
            """
            INSERT INTO object_schemas
            (object_type, plural, fields, primary_field, description, label,
             list_fields, source, status, rationale, notebook_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, '[]', 'induced', 'proposed', ?, ?, ?, ?)
            """,
            (object_type, plural, fields_json, primary, description, label,
             rationale, notebook_id, now, now),
        )

    @staticmethod
    def update_schema_columns(
        db: sqlite3.Connection,
        object_type: str,
        updates: List[str],
        values: List[object],
    ) -> None:
        db.execute(
            f"UPDATE object_schemas SET {', '.join(updates)} WHERE object_type = ?",
            values,
        )

    @staticmethod
    def delete_schema_row(db: sqlite3.Connection, object_type: str) -> None:
        db.execute(
            "DELETE FROM object_schemas WHERE object_type = ?", (object_type,)
        )
