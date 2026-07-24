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
from typing import Dict, Iterable, List, Optional, Sequence

from app.models.common import Evidence
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.mount_sql import (
    MOUNT_JOIN, MOUNT_VALID, MOUNTED_BASE_IDS_SUBQUERY,
)


class KnowledgeStore:
    def __init__(self, database: SqliteDatabase, seams) -> None:
        self.database = database
        self.seams = seams

    def _connect(self):
        return self.database.connect()

    # ------------------------------------------------ lifecycle projections
    @staticmethod
    def delete_notebook_graph_rows(db: sqlite3.Connection, notebook_id: str) -> dict[str, int]:
        """Wipe a notebook's KG artefacts for a full rebuild — but PRESERVE the
        deterministic knowhow-table projection (case/procedure/tool objects and
        their edges under a ``source_type='knowhow'`` hidden source). Those rows
        are KnowhowProjector's zero-LLM output, not extraction output: a KG
        rebuild must neither delete them (row-scoped reprojection, not this
        wipe, owns their lifecycle — and the knowhow table stays marked synced)
        nor free the hidden source to be re-extracted by the LLM (see
        ``source_build_rows``). ``knowledge_objects.source_id`` is NOT NULL
        DEFAULT '' so a plain ``NOT IN`` excludes knowhow while still deleting
        ordinary ('' or real-source) rows; ``knowledge_relations.source_id`` is
        nullable, so that guard also keeps NULL-source (non-knowhow) rows
        deletable. The remaining tables never hold knowhow rows (the projector
        writes no embeddings/clusters/extraction_runs and no kg_objects_fts for
        its objects), so they wipe whole."""
        counts: dict[str, int] = {}
        knowhow_sources = "SELECT id FROM sources WHERE source_type = 'knowhow'"
        cur = db.execute(
            f"DELETE FROM knowledge_objects WHERE notebook_id = ? "
            f"AND source_id NOT IN ({knowhow_sources})",
            (notebook_id,),
        )
        counts["knowledge_objects"] = cur.rowcount
        cur = db.execute(
            f"DELETE FROM knowledge_relations WHERE notebook_id = ? "
            f"AND (source_id IS NULL OR source_id NOT IN ({knowhow_sources}))",
            (notebook_id,),
        )
        counts["knowledge_relations"] = cur.rowcount
        for table in (
            "concept_clusters", "concept_merge_candidates", "knowledge_embeddings",
            "extraction_runs", "unified_kg_state",
        ):
            cur = db.execute(f"DELETE FROM {table} WHERE notebook_id = ?", (notebook_id,))
            counts[table] = cur.rowcount
        cur = db.execute("DELETE FROM kg_objects_fts WHERE notebook_id = ?", (notebook_id,))
        counts["kg_objects_fts"] = cur.rowcount
        return counts

    @staticmethod
    def notebook_tier_row(db: sqlite3.Connection, notebook_id: str):
        return db.execute("SELECT tier FROM notebooks WHERE id=?", (notebook_id,)).fetchone()

    @staticmethod
    def relink_rows(db: sqlite3.Connection, notebook_id: str):
        objects = db.execute(
            "SELECT id, object_type, source_id, payload, evidence FROM knowledge_objects "
            "WHERE notebook_id = ? AND status != 'deprecated'", (notebook_id,),
        ).fetchall()
        relations = db.execute(
            "SELECT source_object_id, target_object_id, edge_type "
            "FROM knowledge_relations WHERE notebook_id = ?", (notebook_id,),
        ).fetchall()
        valid_sources = {
            row["id"] for row in db.execute(
                "SELECT id FROM sources WHERE notebook_id = ?", (notebook_id,)
            ).fetchall()
        }
        return objects, relations, valid_sources

    @staticmethod
    def incremental_object_rows(
        db: sqlite3.Connection, notebook_id: str, source_id: str, object_type: str,
        *, exclude_source: bool = False,
    ):
        if object_type == "concept" and exclude_source:
            return db.execute(
                "SELECT id, payload FROM knowledge_objects WHERE notebook_id=? "
                "AND object_type='concept' AND status!='deprecated' AND source_id!=?",
                (notebook_id, source_id),
            ).fetchall()
        if object_type == "concept":
            return db.execute(
                "SELECT id, payload FROM knowledge_objects WHERE notebook_id=? AND source_id=? "
                "AND object_type='concept' AND status!='deprecated'",
                (notebook_id, source_id),
            ).fetchall()
        return db.execute(
            "SELECT id, payload FROM knowledge_objects WHERE notebook_id=? AND source_id=? "
            "AND object_type=? AND status!='deprecated'",
            (notebook_id, source_id, object_type),
        ).fetchall()

    @staticmethod
    def embedding_rows(db: sqlite3.Connection, notebook_id: str):
        return db.execute(
            "SELECT object_id, vector FROM knowledge_embeddings WHERE notebook_id=?",
            (notebook_id,),
        ).fetchall()

    @staticmethod
    def embedding_rows_for_objects(db: sqlite3.Connection, notebook_id: str, object_ids):
        ids = list(object_ids)
        if not ids:
            return []
        return db.execute(
            "SELECT object_id, vector FROM knowledge_embeddings WHERE notebook_id=? "
            "AND object_id IN ({})".format(",".join("?" for _ in ids)),
            (notebook_id, *ids),
        ).fetchall()

    @staticmethod
    def valid_object_ids(db: sqlite3.Connection, object_ids):
        ids = list(object_ids)
        if not ids:
            return set()
        ph = ",".join("?" for _ in ids)
        return {
            row["id"] for row in db.execute(
                f"SELECT id FROM knowledge_objects WHERE id IN ({ph}) AND status!='deprecated'",
                ids,
            ).fetchall()
        }

    @staticmethod
    def source_build_rows(db: sqlite3.Connection, notebook_id: str):
        """Sources eligible for LLM KG extraction (build/rebuild) plus the
        subset that already has KG objects. Excludes ``source_type='knowhow'``
        hidden sources: their KG objects are KnowhowProjector's deterministic
        zero-LLM output, so a knowhow hidden source (whose KOs a rebuild wipe
        deliberately preserves — see ``delete_notebook_graph_rows``) must never
        be handed to the extraction pipeline as a target, and even a knowhow
        table not yet projected (elements present, zero KOs) must not become an
        empty-KG extraction target — either would fabricate LLM-derived
        case/procedure objects, violating the feature's zero-LLM invariant."""
        source_ids = [
            row["id"] for row in db.execute(
                "SELECT id FROM sources WHERE notebook_id = ? "
                "AND source_type != 'knowhow'", (notebook_id,)
            ).fetchall()
        ]
        kg_source_ids = {
            row["source_id"] for row in db.execute(
                "SELECT DISTINCT ko.source_id FROM knowledge_objects ko "
                "WHERE ko.notebook_id = ? AND ko.source_id != '' "
                "AND COALESCE(("
                "  SELECT er.status FROM extraction_runs er "
                "  WHERE er.source_id=ko.source_id AND er.run_type='kg' "
                "  ORDER BY er.created_at DESC, er.rowid DESC LIMIT 1"
                "), 'completed')='completed'",
                (notebook_id,),
            ).fetchall()
        }
        return source_ids, kg_source_ids

    @staticmethod
    def sources_with_elements(db: sqlite3.Connection, notebook_id: str) -> set:
        """该 notebook 下已产出 source_elements(已成功 parse)的 source_id 集合。
        build_notebook_kg 用它把无 elements 的源(parse 未落地)排除出抽取 targets——
        否则接地校验(build_records)没有 element 可绑,抽出的节点被整源丢弃、objects=0。"""
        return {
            row["source_id"] for row in db.execute(
                "SELECT DISTINCT e.source_id FROM source_elements e "
                "JOIN sources s ON s.id = e.source_id WHERE s.notebook_id = ?",
                (notebook_id,),
            ).fetchall()
        }

    @staticmethod
    def active_object_count(db: sqlite3.Connection, notebook_id: str) -> int:
        return int(db.execute(
            "SELECT COUNT(*) c FROM knowledge_objects "
            "WHERE notebook_id=? AND status!='deprecated'", (notebook_id,),
        ).fetchone()["c"])

    @staticmethod
    def unified_graph_rows(db: sqlite3.Connection, notebook_id: str):
        return db.execute(
            "SELECT id, object_type, payload, status FROM knowledge_objects "
            "WHERE notebook_id=? AND status!='deprecated'", (notebook_id,),
        ).fetchall()

    @staticmethod
    def neighbor_relation_rows(db: sqlite3.Connection, notebook_id: str, object_ids):
        ids = list(object_ids)
        if not ids:
            return []
        ph = ",".join("?" for _ in ids)
        return db.execute(
            f"SELECT source_object_id, target_object_id, edge_type FROM knowledge_relations "
            f"WHERE notebook_id=? "
            f"AND (source_object_id IN ({ph}) OR target_object_id IN ({ph}))",
            (notebook_id, *ids, *ids),
        ).fetchall()

    @staticmethod
    def object_meta_rows_for_notebook(
        db: sqlite3.Connection, notebook_id: str, object_ids,
    ):
        ids = list(object_ids)
        if not ids:
            return []
        ph = ",".join("?" for _ in ids)
        return db.execute(
            f"SELECT id, object_type, payload FROM knowledge_objects "
            f"WHERE notebook_id=? AND id IN ({ph})", (notebook_id, *ids),
        ).fetchall()

    @staticmethod
    def community_context_rows(db: sqlite3.Connection, notebook_id: str, members):
        ids = list(members)
        if not ids:
            return [], []
        ph = ",".join("?" for _ in ids)
        objects = db.execute(
            f"SELECT id, object_type, payload FROM knowledge_objects WHERE id IN ({ph})", ids,
        ).fetchall()
        relations = db.execute(
            f"SELECT source_object_id, target_object_id, edge_type FROM knowledge_relations "
            f"WHERE notebook_id=? AND review_status!='rejected' "
            f"AND source_object_id IN ({ph}) AND target_object_id IN ({ph}) "
            f"ORDER BY id",
            [notebook_id, *ids, *ids],
        ).fetchall()
        return objects, relations

    def get_notebook(self, notebook_id: str) -> None:
        with self.database.connect() as db:
            if db.execute("SELECT 1 FROM notebooks WHERE id=?", (notebook_id,)).fetchone() is None:
                raise KeyError(notebook_id)

    def has_kg(self, notebook_id: str) -> bool:
        with self.database.connect() as db:
            return bool(db.execute(
                "SELECT EXISTS(SELECT 1 FROM knowledge_objects WHERE notebook_id=?)",
                (notebook_id,),
            ).fetchone()[0])

    @staticmethod
    def any_mounted_has_kg_on(db: sqlite3.Connection, notebook_id: str) -> bool:
        """本库挂载的参考库中是否有任一已建 KG —— 驱动前端严格推理门控。
        未挂载 → False(即便系统里存在有图的公共知识库)。"""
        return bool(db.execute(
            "SELECT EXISTS(SELECT 1 " + MOUNT_JOIN + MOUNT_VALID
            + " AND EXISTS(SELECT 1 FROM knowledge_objects ko WHERE ko.notebook_id = b.id))",
            (notebook_id,),
        ).fetchone()[0])

    def any_mounted_has_kg(self, notebook_id: str) -> bool:
        with self.database.connect() as db:
            return self.any_mounted_has_kg_on(db, notebook_id)

    def any_mounted_has_kg_compat(
        self, notebook_id: str, db: "sqlite3.Connection | None" = None
    ) -> bool:
        return (
            self.any_mounted_has_kg_on(db, notebook_id) if db is not None
            else self.any_mounted_has_kg(notebook_id)
        )

    def retrieval_objects_compat(
        self, db: sqlite3.Connection, notebook_id: str, object_type: str,
        statuses, id_filter,
    ) -> list[dict]:
        if id_filter is not None:
            id_filter = list(dict.fromkeys(id_filter))
        return self.retrieval_objects(
            db,
            notebook_id,
            object_type,
            statuses,
            id_filter,
            batch_size=self.seams.in_chunk_size(),
        )

    def begin_extraction(
        self, source_id: str, notebook_id: str, run_id: str, created_at: str
    ) -> None:
        with self.database.write() as db:
            self.begin_extraction_run(db, source_id, notebook_id, run_id, created_at)

    def finish_extraction(self, run_id: str, status: str, message: str) -> None:
        notebook_id = ""
        with self.database.write() as db:
            row = db.execute(
                "SELECT notebook_id FROM extraction_runs WHERE id=?",
                (run_id,),
            ).fetchone()
            self.finish_extraction_run(
                db, run_id, status, message, self.seams.now()
            )
            if row is not None:
                notebook_id = row["notebook_id"]
        if notebook_id:
            # pending_source_count depends on the latest run status, while its
            # version key is the KG mutation sequence. A status-only terminal
            # update therefore needs an explicit post-commit invalidation.
            from app.repositories.sqlite import knowledge_counts_cache
            knowledge_counts_cache.invalidate(notebook_id)

    def add_relations_current(
        self, notebook_id: str, source_id: str, relations: List[dict]
    ) -> int:
        with self.database.write() as db:
            return self.add_relations(
                db, notebook_id, source_id, relations, self.seams.now()
            )

    @staticmethod
    def object_version_row(db: sqlite3.Connection, notebook_id: str):
        return db.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(updated_at), '') AS ts "
            "FROM knowledge_objects WHERE notebook_id = ?", (notebook_id,),
        ).fetchone()

    @staticmethod
    def relation_context_rows(db: sqlite3.Connection, notebook_id: str,
                              relation_ids=None, *, batch_size: int = 900):
        base_sql = (
            "SELECT r.id AS id, r.source_object_id AS s, r.target_object_id AS t, "
            "r.edge_type AS et, r.evidence AS ev, r.review_status AS review_status, "
            "so.payload AS sp, tp.payload AS tpl "
            "FROM knowledge_relations r "
            "JOIN knowledge_objects so ON so.id = r.source_object_id "
            "JOIN knowledge_objects tp ON tp.id = r.target_object_id "
            "WHERE r.notebook_id = ?"
        )
        if relation_ids is None:
            return db.execute(base_sql, (notebook_id,)).fetchall()
        ids = list(relation_ids)
        rows = []
        for offset in range(0, len(ids), batch_size):
            batch = ids[offset:offset + batch_size]
            ph = ",".join("?" for _ in batch)
            rows.extend(db.execute(
                base_sql + f" AND r.id IN ({ph})", (notebook_id, *batch),
            ).fetchall())
        return rows

    @staticmethod
    def relation_exists(db: sqlite3.Connection, notebook_id: str) -> bool:
        return db.execute(
            "SELECT 1 FROM knowledge_relations WHERE notebook_id = ? LIMIT 1",
            (notebook_id,),
        ).fetchone() is not None

    @staticmethod
    def relation_endpoint_rows(db: sqlite3.Connection, notebook_id: str,
                               source_ids=None):
        if source_ids:
            values = list(source_ids)
            ph = ",".join("?" for _ in values)
            return db.execute(
                f"SELECT source_object_id, target_object_id FROM knowledge_relations "
                f"WHERE notebook_id=? AND source_id IN ({ph})",
                (notebook_id, *values),
            ).fetchall()
        return db.execute(
            "SELECT source_object_id, target_object_id FROM knowledge_relations "
            "WHERE notebook_id = ?", (notebook_id,),
        ).fetchall()

    @staticmethod
    def relation_connected_object_ids(
        db: sqlite3.Connection, notebook_id: str, object_ids
    ):
        """Return only candidates that have at least one incident relation.

        The correlated EXISTS probes use the notebook/endpoint covering indexes
        and short-circuit on the first edge.  Do not replace this with a query
        that returns relation endpoints: a single hub can have millions of
        incident rows while the caller only needs one boolean per candidate.
        """
        values = list(dict.fromkeys(object_ids))
        if not values:
            return []
        candidates = ",".join("(?)" for _ in values)
        return db.execute(
            f"WITH candidates(object_id) AS (VALUES {candidates}) "
            "SELECT object_id FROM candidates AS c "
            "WHERE EXISTS ("
            "SELECT 1 FROM knowledge_relations AS r "
            "WHERE r.notebook_id=? AND r.source_object_id=c.object_id LIMIT 1"
            ") OR EXISTS ("
            "SELECT 1 FROM knowledge_relations AS r "
            "WHERE r.notebook_id=? AND r.target_object_id=c.object_id LIMIT 1"
            ")",
            (*values, notebook_id, notebook_id),
        ).fetchall()

    @staticmethod
    def neighbor_ids(db: sqlite3.Connection, notebook_id: str, object_id: str,
                     *, endpoint: str, edge_type=None):
        selected = ("target_object_id" if endpoint == "source_object_id"
                    else "source_object_id")
        edge_clause = " AND edge_type=?" if edge_type else ""
        params = [notebook_id, object_id] + ([edge_type] if edge_type else [])
        return db.execute(
            f"SELECT {selected} FROM knowledge_relations "
            f"WHERE notebook_id=? AND {endpoint}=? "
            f"AND review_status!='rejected'{edge_clause}", params,
        ).fetchall()

    def usable_object_rows(self, notebook_id: str, object_ids: Sequence[str]):
        with self.database.connect() as db:
            rows = self.usable_object_rows_on(
                db, object_ids, ("reviewed", "approved", "project_specific", "conflict"),
            )
        return [dict(row) for row in rows if row["notebook_id"] == notebook_id]

    @staticmethod
    def usable_object_rows_on(db: sqlite3.Connection, object_ids, statuses):
        ids = list(object_ids)
        if not ids:
            return []
        ph = ",".join("?" for _ in ids)
        status_ph = ",".join("?" for _ in statuses)
        return db.execute(
            f"SELECT * FROM knowledge_objects WHERE id IN ({ph}) "
            f"AND status IN ({status_ph})", [*ids, *statuses],
        ).fetchall()

    @staticmethod
    def graph_version_rows(db: sqlite3.Connection, notebook_id: str):
        rel = db.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(created_at), '') AS ts, "
            "COALESCE(SUM(CASE WHEN review_status = 'rejected' THEN 1 ELSE 0 END), 0) AS n_rej, "
            "COALESCE(SUM(CASE WHEN review_status = 'verified' THEN 1 ELSE 0 END), 0) AS n_ver "
            "FROM knowledge_relations WHERE notebook_id = ?", (notebook_id,),
        ).fetchone()
        obj = db.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(updated_at), '') AS ts "
            "FROM knowledge_objects WHERE notebook_id = ?", (notebook_id,),
        ).fetchone()
        return rel, obj

    @staticmethod
    def graph_object_rows(db: sqlite3.Connection, notebook_id: str, statuses):
        ph = ",".join("?" for _ in statuses)
        return db.execute(
            "SELECT id, object_type, payload FROM knowledge_objects "
            f"WHERE notebook_id = ? AND status IN ({ph}) ORDER BY rowid, id",
            (notebook_id, *statuses),
        ).fetchall()

    @staticmethod
    def graph_relation_rows(db: sqlite3.Connection, notebook_id: str,
                            *, include_id_evidence: bool = True):
        columns = ("id, source_object_id, target_object_id, edge_type, evidence"
                   if include_id_evidence else
                   "source_object_id, target_object_id")
        return db.execute(
            f"SELECT {columns} FROM knowledge_relations "
            "WHERE notebook_id = ? AND review_status != 'rejected' ORDER BY id",
            (notebook_id,),
        ).fetchall()

    @staticmethod
    def object_evidence_rows(db: sqlite3.Connection, object_ids):
        ids = list(object_ids)
        if not ids:
            return []
        ph = ",".join("?" for _ in ids)
        return db.execute(
            f"SELECT id, evidence FROM knowledge_objects WHERE id IN ({ph})", ids,
        ).fetchall()

    @staticmethod
    def notebook_object_evidence_rows(db: sqlite3.Connection, notebook_id: str):
        return db.execute(
            "SELECT id, evidence FROM knowledge_objects WHERE notebook_id=?",
            (notebook_id,),
        ).fetchall()

    @staticmethod
    def follow_start_row(db: sqlite3.Connection, object_id: str,
                         active_notebook_id: str, statuses):
        """起点授权门:只有 active 自己的对象、或 active 挂载的参考库里的对象,
        才能作为 follow_chain 的合法起点(未挂载的 tier='base' 库不算,即便它已发布)。"""
        ph = ",".join("?" for _ in statuses)
        return db.execute(
            f"SELECT ko.*, n.tier AS notebook_tier "
            f"FROM knowledge_objects ko JOIN notebooks n ON n.id=ko.notebook_id "
            f"WHERE ko.id=? AND ko.status IN ({ph}) "
            "AND (ko.notebook_id=? OR ko.notebook_id IN ("
            + MOUNTED_BASE_IDS_SUBQUERY + "))",
            (object_id, *statuses, active_notebook_id, active_notebook_id),
        ).fetchone()

    @staticmethod
    def follow_endpoint_rows(db: sqlite3.Connection, notebook_id: str, object_id: str,
                             endpoint: str, limit: int):
        index_name = ("idx_knowledge_relations_nb_source"
                      if endpoint == "source_object_id"
                      else "idx_knowledge_relations_nb_target")
        return db.execute(
            f"SELECT r.id, r.notebook_id, r.source_id, "
            f"r.source_object_id, r.target_object_id, r.edge_type, r.review_status "
            f"FROM knowledge_relations AS r INDEXED BY {index_name} "
            f"WHERE r.notebook_id=? AND r.{endpoint}=? LIMIT ?",
            (notebook_id, object_id, limit),
        ).fetchall()

    @staticmethod
    def follow_relation_evidence_rows(db: sqlite3.Connection, relation_ids):
        ids = list(relation_ids)
        if not ids:
            return []
        ph = ",".join("?" for _ in ids)
        return db.execute(
            f"SELECT r.id, r.evidence, s.title AS source_title "
            f"FROM knowledge_relations r LEFT JOIN sources s ON s.id=r.source_id "
            f"WHERE r.id IN ({ph})", tuple(ids),
        ).fetchall()

    @staticmethod
    def follow_object_rows(db: sqlite3.Connection, notebook_id: str,
                           object_ids, statuses):
        ids = list(object_ids)
        if not ids:
            return []
        ph = ",".join("?" for _ in ids)
        status_ph = ",".join("?" for _ in statuses)
        return db.execute(
            f"SELECT * FROM knowledge_objects WHERE notebook_id=? "
            f"AND id IN ({ph}) AND status IN ({status_ph})",
            (notebook_id, *ids, *statuses),
        ).fetchall()

    @staticmethod
    def in_network_relation_rows(db: sqlite3.Connection, notebook_id: str,
                                 object_ids):
        ids = list(object_ids)
        if len(ids) < 2:
            return []
        ph = ",".join("?" for _ in ids)
        return db.execute(
            f"SELECT source_object_id, target_object_id, edge_type "
            f"FROM knowledge_relations WHERE notebook_id=? "
            f"AND review_status!='rejected' "
            f"AND source_object_id IN ({ph}) "
            f"AND target_object_id IN ({ph})",
            [notebook_id, *ids, *ids],
        ).fetchall()

    @staticmethod
    def retrieval_objects(
        db: sqlite3.Connection,
        notebook_id: str,
        object_type: str,
        statuses: Optional[Iterable[str]],
        id_filter: Optional[Iterable[str]],
        *,
        batch_size: int = 900,
    ) -> List[dict]:
        base_query = "SELECT * FROM knowledge_objects WHERE notebook_id=? AND object_type=?"
        params: List[object] = [notebook_id, object_type]
        if statuses is not None:
            values = list(statuses)
            base_query += f" AND status IN ({','.join('?' for _ in values)})"
            params.extend(values)
        if id_filter is not None:
            ids = list(id_filter)
            if not ids:
                return []
            rows = []
            batch_size = max(1, int(batch_size))
            for offset in range(0, len(ids), batch_size):
                batch = ids[offset:offset + batch_size]
                rows.extend(db.execute(
                    base_query + f" AND id IN ({','.join('?' for _ in batch)})",
                    (*params, *batch),
                ).fetchall())
            rows.sort(key=lambda row: (row["created_at"], row["id"]))
        else:
            rows = db.execute(base_query + " ORDER BY created_at ASC, id ASC", params).fetchall()
        return [{
            "id": row["id"],
            "payload": json.loads(row["payload"] or "{}"),
            "evidence": [Evidence(**item) for item in json.loads(row["evidence"] or "[]")],
            "status": row["status"],
            "owner": row["owner"],
            "last_reviewed": row["last_reviewed"] if "last_reviewed" in row.keys() else "",
        } for row in rows]

    def _element_texts(self, db, element_ids, *, with_ordinal: bool = False):
        ids = [e for e in element_ids if e]
        if not ids:
            return {}, {}
        ph = ",".join("?" for _ in ids)
        rows = db.execute(f"SELECT id, text FROM source_elements WHERE id IN ({ph})", ids).fetchall()
        texts = {r["id"]: r["text"] for r in rows}
        if not with_ordinal:
            return texts, {}
        order_rows = db.execute(
            "SELECT se.id FROM source_elements se JOIN sources s ON se.source_id=s.id "
            "WHERE s.notebook_id=(SELECT notebook_id FROM sources WHERE id=("
            "SELECT source_id FROM source_elements WHERE id=? LIMIT 1)) "
            "ORDER BY se.created_at ASC, se.id ASC",
            (ids[0],),
        ).fetchall()
        ordinal = {r["id"]: i for i, r in enumerate(order_rows)}
        return texts, ordinal
    def _enrich_evidence(self, db, evidence):
        texts, _ = self._element_texts(db, [e.get("element_id") for e in evidence])
        out = []
        for e in evidence:
            out.append({"quoted_span": e.get("quoted_span", ""),
                        "source_title": e.get("source_title", "") or e.get("source_id", ""),
                        "element_text": texts.get(e.get("element_id", ""), e.get("quoted_span", ""))})
        return out
    def node_context(self, notebook_id, object_id):
        self.get_notebook(notebook_id)
        with self._connect() as db:
            row = db.execute("SELECT id, object_type, payload, evidence FROM knowledge_objects WHERE id=? AND notebook_id=?", (object_id, notebook_id)).fetchone()
            if row is None:
                raise KeyError(object_id)
            obj_type = row["object_type"]
            payload = json.loads(row["payload"] or "{}")
            section = payload.get("section_path", "")
            occurrences = self._enrich_evidence(db, json.loads(row["evidence"] or "[]"))
            result = {"id": object_id, "object_type": obj_type, "name": payload.get("name", ""),
                      "section_path": section, "occurrences": occurrences, "definition": None, "steps": None}
            if obj_type == "concept":
                # prefer the unified cluster's fused description when present
                crow = db.execute(
                    "SELECT canonical_description FROM concept_clusters "
                    "WHERE notebook_id=? AND member_object_id=? AND canonical_description!='' LIMIT 1",
                    (notebook_id, object_id)).fetchone()
                if crow and crow["canonical_description"]:
                    result["definition"] = crow["canonical_description"]
                else:
                    drow = db.execute(
                        "SELECT ko.payload, ko.evidence FROM knowledge_relations r JOIN knowledge_objects ko ON ko.id=r.source_object_id "
                        "WHERE r.notebook_id=? AND r.target_object_id=? AND r.edge_type='defines' LIMIT 1", (notebook_id, object_id)).fetchone()
                    if drow is not None:
                        dpay = json.loads(drow["payload"] or "{}")
                        den = self._enrich_evidence(db, json.loads(drow["evidence"] or "[]"))
                        result["definition"] = (den[0]["element_text"] if den else dpay.get("name", ""))
            if obj_type == "procedure":
                steps_payload = payload.get("steps")
                if isinstance(steps_payload, list) and steps_payload:
                    # New self-contained shape: ordered steps live in the object's payload.
                    eids = [s.get("element_id") for s in steps_payload if s.get("element_id")]
                    texts, _ord = self._element_texts(db, eids) if eids else ({}, {})
                    result["steps"] = [
                        {"name": s.get("name", ""),
                         "element_text": texts.get(s.get("element_id") or "", s.get("quote", "")),
                         "section_path": section}
                        for s in steps_payload
                    ]
                else:
                    # Legacy fallback: group sibling procedure nodes by exact section_path
                    # (precedes edges are sparse). Two distinct procedures sharing a heading
                    # would merge — acceptable for inspection.
                    #
                    # P2-3: this used to scan EVERY procedure object in the notebook
                    # (regardless of section) and filter in Python — O(procedures in
                    # notebook) per call. When the target node's own section_path is
                    # known (the common case — payload.get("section_path") above),
                    # bind the query to it directly in SQL via json_extract (JSON1,
                    # already used elsewhere in this file), so SQLite only reads
                    # matching rows. section_path is free text (not a dedicated
                    # column) so this is the only way to push the filter down without
                    # a schema change. If section_path is unavailable (rare: an old
                    # or malformed payload), fall back to a bounded LIMIT — this path
                    # is a display-only legacy fallback, not a correctness-critical
                    # query, so an arbitrary-but-bounded sample is acceptable.
                    if section:
                        prows = db.execute(
                            "SELECT id, payload, evidence FROM knowledge_objects "
                            "WHERE notebook_id=? AND object_type='procedure' AND status!='deprecated' "
                            "AND json_extract(payload,'$.section_path')=?",
                            (notebook_id, section)).fetchall()
                    else:
                        prows = db.execute(
                            "SELECT id, payload, evidence FROM knowledge_objects "
                            "WHERE notebook_id=? AND object_type='procedure' AND status!='deprecated' "
                            "LIMIT 500",
                            (notebook_id,)).fetchall()
                    candidate_steps = []
                    for pr in prows:
                        ppay = json.loads(pr["payload"] or "{}")
                        if ppay.get("section_path", "") != section:
                            continue
                        ev = json.loads(pr["evidence"] or "[]")
                        first_eid = ev[0].get("element_id") if ev else ""
                        candidate_steps.append((ppay.get("name", ""), first_eid))
                    all_step_first_eids = [eid for _, eid in candidate_steps if eid]
                    if all_step_first_eids:
                        texts, ordinal = self._element_texts(db, all_step_first_eids, with_ordinal=True)
                    else:
                        texts, ordinal = {}, {}
                    steps = []
                    for step_name, first_eid in candidate_steps:
                        steps.append({"name": step_name, "element_text": texts.get(first_eid, ""),
                                      "section_path": section, "_ord": ordinal.get(first_eid, 1_000_000)})
                    steps.sort(key=lambda s: s["_ord"])
                    for s in steps:
                        s.pop("_ord", None)
                    result["steps"] = steps
            return result

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
        from app.repositories.sqlite import knowledge_counts_cache
        return knowledge_counts_cache.active_object_count(db, notebook_id)

    @staticmethod
    def type_counts(
        db: sqlite3.Connection, notebook_id: str
    ) -> "tuple[Dict[str, int], Dict[str, str]]":
        from app.repositories.sqlite import knowledge_counts_cache
        counts = knowledge_counts_cache.type_counts(db, notebook_id)  # non-deprecated
        label_rows = db.execute(
            "SELECT object_type, label FROM object_schemas"
        ).fetchall()
        labels = {r["object_type"]: (r["label"] or r["object_type"]) for r in label_rows}
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

        # Pagination total = a slice of the seq-gated type/status count memo
        # (from_row already warmed it this request), not a fresh per-page COUNT.
        from app.repositories.sqlite import knowledge_counts_cache
        total = knowledge_counts_cache.object_type_total(
            db, notebook_id, object_type, status
        )
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

    # ------------------------------------------------- knowhow projection
    # (Task 5, knowhow-tables PR-1): the deterministic projector writes
    # case/procedure/tool objects and their edges directly (bypassing
    # store_kg's fresh-id-per-call allocation — knowhow ids are STABLE
    # hashes of row_id/column_id/table_id so reprojection is idempotent, not
    # append-only), reusing insert_object_chunk/insert_relation_chunk above
    # for the actual INSERTs. These primitives cover the row/table-scoped
    # DELETEs that pattern needs and are absent from the plain store_kg path.
    @staticmethod
    def delete_objects_by_source_and_row(
        connection: sqlite3.Connection, source_id: str, row_id: str
    ) -> None:
        """Delete this row's PRIOR case+procedure objects (any column) under
        the knowhow hidden source, keyed by ``payload.row_id`` — NOT tool
        objects (table-scoped, deduped across rows, so they carry no
        ``row_id`` key and are correctly left untouched here; project_table's
        full rebuild is what sweeps orphaned tools). json_extract on payload
        is unindexed but source_id narrows the scan first (idx_knowledge_
        objects_source), acceptable at this feature's bounded scale."""
        connection.execute(
            "DELETE FROM knowledge_objects WHERE source_id = ? "
            "AND json_extract(payload, '$.row_id') = ?",
            (source_id, row_id),
        )

    @staticmethod
    def delete_objects_by_source(
        connection: sqlite3.Connection, source_id: str
    ) -> None:
        """Full wipe of every object (case+procedure+tool) a knowhow table's
        hidden source has ever produced — project_table's escape-hatch
        rebuild and delete_table_projection's cleanup both use this rather
        than the row-scoped variant above, so a stale/orphaned tool (or a
        procedure whose column has since been deleted) never survives a full
        rebuild."""
        connection.execute(
            "DELETE FROM knowledge_objects WHERE source_id = ?", (source_id,)
        )

    @staticmethod
    def delete_relations_by_source_object(
        connection: sqlite3.Connection, notebook_id: str, source_object_id: str
    ) -> None:
        """Delete every edge OUT of one case object (identified_by/
        diagnosed_by/fixed_by/requires_tool all have the case as source, per
        the knowhow projection spec) — one call cleans all of a row's prior
        edges regardless of which cell changed. Uses idx_knowledge_relations_
        nb_source (notebook_id, source_object_id)."""
        connection.execute(
            "DELETE FROM knowledge_relations WHERE notebook_id = ? "
            "AND source_object_id = ?",
            (notebook_id, source_object_id),
        )

    @staticmethod
    def delete_relations_by_source(
        connection: sqlite3.Connection, source_id: str
    ) -> None:
        """Full wipe of every relation a knowhow table's hidden source has
        ever produced (project_table / delete_table_projection). Uses
        idx_knowledge_relations_source."""
        connection.execute(
            "DELETE FROM knowledge_relations WHERE source_id = ?", (source_id,)
        )

    @staticmethod
    def insert_object_if_missing(
        connection: sqlite3.Connection, row: tuple
    ) -> None:
        """Upsert-by-absence for tool objects: a tool's id is a stable hash of
        (table_id, normalized name), so the SAME tool referenced by multiple
        rows always maps to the SAME id — the first row to mention it creates
        the row, later rows are a no-op (INSERT OR IGNORE on the id PRIMARY
        KEY) rather than a second, redundant insert attempt."""
        connection.execute(
            "INSERT OR IGNORE INTO knowledge_objects "
            "(id, notebook_id, object_type, status, owner, payload, evidence, "
            "source_candidate_id, source_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, '', ?, ?, NULL, ?, ?, ?)",
            row,
        )

    @staticmethod
    def legacy_typed_table_ids(
        connection: sqlite3.Connection, object_types: Sequence[str], id_prefix: str
    ) -> List[str]:
        """knowhow-tables PR-2+3 Task 2's one-shot startup migration bridge:
        every DISTINCT ``payload.table_id`` among objects whose
        ``object_type`` is one of ``object_types`` AND whose id starts with
        ``id_prefix`` — the detection query
        ``app.services.knowhow.projection.find_legacy_projected_table_ids``
        (its only caller) needs to find knowhow tables still carrying PR-1's
        fixed case/procedure/tool vocabulary so they can be reprojected under
        the cell-level dynamic-type model. A plain SELECT; the caller owns
        interpretation/scheduling. Kept here (not inline SQL in the service
        layer) per this codebase's SQL-ownership rule (Task 27,
        test_repository_callers_static.py): every knowledge_objects query
        lives in this store, never in a services/* file."""
        placeholders = ",".join("?" for _ in object_types)
        rows = connection.execute(
            f"SELECT DISTINCT json_extract(payload, '$.table_id') AS table_id "
            f"FROM knowledge_objects WHERE object_type IN ({placeholders}) "
            f"AND id LIKE ?",
            (*object_types, f"{id_prefix}%"),
        ).fetchall()
        return [r["table_id"] for r in rows if r["table_id"]]

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

    # ------------------------------------------------- Task 26 primitives
    # The last facade SQL bodies, moved verbatim.  All connection-taking —
    # the facade keeps its `_connect`/`_write` boundaries (and the frozen
    # patch seats on them) and passes the possibly-wrapped connection down.

    @staticmethod
    def source_has_kg(db: sqlite3.Connection, source_id: str) -> bool:
        """True iff the source has a complete KG graph.

        Direct/governance rows without extraction history remain compatible.
        When extraction history exists, the latest KG run must be completed so
        a failed legacy partial write cannot masquerade as resumable completion.
        """
        row = db.execute(
            "SELECT EXISTS("
            "  SELECT 1 FROM knowledge_objects ko "
            "  WHERE ko.source_id = ? AND ko.source_id != '' "
            "  AND COALESCE(("
            "    SELECT er.status FROM extraction_runs er "
            "    WHERE er.source_id=ko.source_id AND er.run_type='kg' "
            "    ORDER BY er.created_at DESC, er.rowid DESC LIMIT 1"
            "  ), 'completed')='completed'"
            ")",
            (source_id,),
        ).fetchone()
        return bool(row[0])

    def insert_test_object(
        self,
        db: sqlite3.Connection,
        notebook_id: str,
        object_type: str,
        payload: dict,
        source_id: str = "",
    ) -> str:
        """Test-only direct insert (facade `_test_insert_object` delegate).
        Ids/clock ride the compatibility seams — module `_new_id`/`_now`
        patches stay authoritative."""
        object_id = self.seams.new_id("ko")
        now = self.seams.now()
        db.execute(
            """INSERT INTO knowledge_objects
               (id, notebook_id, object_type, status, owner, payload, evidence,
                source_candidate_id, source_id, created_at, updated_at)
               VALUES (?, ?, ?, 'approved', '', ?, '[]', NULL, ?, ?, ?)""",
            (object_id, notebook_id, object_type,
             json.dumps(payload, ensure_ascii=False), source_id, now, now),
        )
        return object_id

    @staticmethod
    def edge_centrality_source_rows(
        db: sqlite3.Connection, notebook_id: str, max_nodes: int
    ) -> "tuple[List[str], List[dict]]":
        """Bounded (top-K by SQL degree) node ids + live relation dicts for the
        edge-betweenness loader (P0-3 semantics moved verbatim):

        1. Degree ranking via GROUP BY over non-rejected knowledge_relations —
           bounded by the distinct node count touched by an edge (isolated
           nodes cannot be edge endpoints and never rank).
        2. When bounded, only relations with BOTH endpoints in the top-K id
           set survive, loaded via json_each(?) (pure read-side join — no
           thousand-placeholder IN and no temp-table write).
        3. Under-K graphs load every live relation plus the full object id
           set — identical result to the unbounded path.
        """
        degree: Dict[str, int] = {}
        for row in db.execute(
            "SELECT source_object_id AS n, COUNT(*) AS c FROM knowledge_relations "
            "WHERE notebook_id = ? AND review_status != 'rejected' "
            "GROUP BY source_object_id", (notebook_id,),
        ).fetchall():
            degree[row["n"]] = degree.get(row["n"], 0) + row["c"]
        for row in db.execute(
            "SELECT target_object_id AS n, COUNT(*) AS c FROM knowledge_relations "
            "WHERE notebook_id = ? AND review_status != 'rejected' "
            "GROUP BY target_object_id", (notebook_id,),
        ).fetchall():
            degree[row["n"]] = degree.get(row["n"], 0) + row["c"]

        if len(degree) > max_nodes:
            # Deterministic top-K: sort by (-degree, id) so ties break on a
            # stable, reproducible key.
            top_ids = [n for n, _ in sorted(
                degree.items(), key=lambda kv: (-kv[1], kv[0])
            )[:max_nodes]]
            top_ids_json = json.dumps(top_ids)
            rel_rows = db.execute(
                "SELECT r.id, r.source_object_id, r.target_object_id, "
                "r.edge_type, r.evidence FROM knowledge_relations r "
                "JOIN json_each(?) s ON s.value = r.source_object_id "
                "JOIN json_each(?) t ON t.value = r.target_object_id "
                "WHERE r.notebook_id = ? AND r.review_status != 'rejected'",
                (top_ids_json, top_ids_json, notebook_id),
            ).fetchall()
            node_ids = top_ids
        else:
            rel_rows = db.execute(
                "SELECT id, source_object_id, target_object_id, edge_type, "
                "evidence FROM knowledge_relations "
                "WHERE notebook_id = ? AND review_status != 'rejected'",
                (notebook_id,),
            ).fetchall()
            obj_rows = db.execute(
                "SELECT id FROM knowledge_objects WHERE notebook_id = ?",
                (notebook_id,),
            ).fetchall()
            node_ids = [row["id"] for row in obj_rows]

        relations = [{
            "id": row["id"],
            "source_object_id": row["source_object_id"],
            "target_object_id": row["target_object_id"],
            "edge_type": row["edge_type"],
            "evidence": json.loads(row["evidence"] or "[]"),
        } for row in rel_rows]
        return node_ids, relations

    @staticmethod
    def concept_cluster_detail_rows(
        db: sqlite3.Connection, notebook_id: str, canonical_id: str
    ) -> "tuple[List[sqlite3.Row], str]":
        """Cluster member rows (joined onto live knowledge_objects) plus the
        canonical name for one concept cluster."""
        cluster_rows = db.execute(
            "SELECT cc.member_object_id, cc.canonical_name, ko.object_type, ko.payload, ko.evidence "
            "FROM concept_clusters cc "
            "JOIN knowledge_objects ko ON ko.id=cc.member_object_id "
            "WHERE cc.notebook_id=? AND cc.canonical_id=? AND ko.status!='deprecated'",
            (notebook_id, canonical_id),
        ).fetchall()
        name_row = db.execute(
            "SELECT canonical_name FROM concept_clusters WHERE notebook_id=? AND canonical_id=? LIMIT 1",
            (notebook_id, canonical_id),
        ).fetchone()
        return cluster_rows, (name_row["canonical_name"] if name_row else "")

    @staticmethod
    def concept_neighbor_rows(
        db: sqlite3.Connection, notebook_id: str, member_ids: "List[str]"
    ) -> "tuple[List[dict], Dict[str, dict]]":
        """Relations touching the member set plus the batch-read non-member
        endpoint objects: returns (rel_edges, objects_by_id)."""
        member_set = set(member_ids)
        placeholders = ",".join("?" for _ in member_set)
        member_list = list(member_set)
        rels_out = db.execute(
            f"SELECT source_object_id, target_object_id, edge_type "
            f"FROM knowledge_relations WHERE notebook_id=? AND source_object_id IN ({placeholders})",
            [notebook_id] + member_list,
        ).fetchall()
        rels_in = db.execute(
            f"SELECT source_object_id, target_object_id, edge_type "
            f"FROM knowledge_relations WHERE notebook_id=? AND target_object_id IN ({placeholders})",
            [notebook_id] + member_list,
        ).fetchall()

        attached_ids: set = set()
        rel_edges: List[dict] = []
        for rel in rels_out:
            other = rel["target_object_id"]
            if other not in member_set:
                attached_ids.add(other)
                rel_edges.append({"other": other, "edge_type": rel["edge_type"]})
        for rel in rels_in:
            other = rel["source_object_id"]
            if other not in member_set:
                attached_ids.add(other)
                rel_edges.append({"other": other, "edge_type": rel["edge_type"]})

        by_other: Dict[str, dict] = {}
        if attached_ids:
            attached_placeholders = ",".join("?" for _ in attached_ids)
            attached_rows = db.execute(
                f"SELECT id, object_type, payload, evidence FROM knowledge_objects "
                f"WHERE id IN ({attached_placeholders}) AND status!='deprecated'",
                list(attached_ids),
            ).fetchall()
            by_other = {
                row["id"]: {
                    "id": row["id"],
                    "object_type": row["object_type"],
                    "payload": json.loads(row["payload"] or "{}"),
                    "evidence": json.loads(row["evidence"] or "[]"),
                }
                for row in attached_rows
            }
        return rel_edges, by_other
