from __future__ import annotations

import json
import math
import sqlite3
from typing import Callable, Sequence

from app.domain.vector_index import encode_vector
from app.domain.indexing_pipeline import IndexingPipelineStalePlanError
from app.models.sources import INDEXING_CHUNK_FALLBACK_WARNING_PREFIX
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.source_store import VISIBLE_SOURCE_TYPES_PREDICATE
from app.repositories.ports import (
    INDEXING_PIPELINE_PUBLISH_DELETE_BATCH,
    KgBuildAlreadyRunning,
)

# 协议边界:staged 回退警告码的最大长度(具名常量,不是可调预算)。
_STAGE_FALLBACK_WARNING_MAX_CHARS = 200


class KgBuildJobStore:
    def __init__(
        self,
        database: SqliteDatabase,
        *,
        new_id: Callable[[str], str],
        now: Callable[[], str],
    ) -> None:
        self.database = database
        self.new_id = new_id
        self.now = now

    @staticmethod
    def _row(row) -> dict:
        return {
            "id": row["id"],
            "notebook_id": row["notebook_id"],
            "created_by": row["created_by"],
            "mode": row["mode"],
            "status": row["status"],
            "stage": row["stage"],
            "total_sources": int(row["total_sources"]),
            "completed_sources": int(row["completed_sources"]),
            "failed_sources": int(row["failed_sources"]),
            "error_code": row["error_code"],
            "error_message": row["error_message"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "finished_at": row["finished_at"],
        }

    def create_job(
        self,
        notebook_id: str,
        created_by: str,
        mode: str,
        total_sources: int,
    ) -> dict:
        if mode not in {"incremental", "rebuild"}:
            raise ValueError("unsupported KG build mode")
        job_id = self.new_id("kgj")
        now = self.now()
        try:
            with self.database.write() as db:
                db.execute(
                    """
                    INSERT INTO kg_build_jobs
                    (id, notebook_id, created_by, mode, status, stage,
                     total_sources, completed_sources, failed_sources,
                     error_code, error_message, created_at, updated_at,
                     finished_at)
                    VALUES (?, ?, ?, ?, 'running', 'probing', ?, 0, 0,
                            '', '', ?, ?, '')
                    """,
                    (
                        job_id,
                        notebook_id,
                        created_by,
                        mode,
                        max(0, int(total_sources)),
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if "kg_build_jobs.notebook_id" in str(exc):
                raise KgBuildAlreadyRunning(notebook_id) from exc
            raise
        return self.get(job_id)

    def get(self, job_id: str) -> dict:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM kg_build_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._row(row)

    def has_running(self, notebook_id: str) -> bool:
        """Quiesce leg A (batch 3·W1 PR-3 §T-3.3). PostgreSQL twin's
        docstring has the full index rationale."""
        with self.database.connect() as db:
            row = db.execute(
                "SELECT 1 FROM kg_build_jobs WHERE notebook_id=? "
                "AND status='running'",
                (notebook_id,),
            ).fetchone()
        return row is not None

    def latest(self, notebook_id: str) -> dict | None:
        with self.database.connect() as db:
            return self.latest_on(db, notebook_id)

    def latest_on(
        self,
        db: sqlite3.Connection,
        notebook_id: str,
    ) -> dict | None:
        row = db.execute(
            "SELECT * FROM kg_build_jobs WHERE notebook_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (notebook_id,),
        ).fetchone()
        return self._row(row) if row is not None else None

    def set_stage(
        self,
        job_id: str,
        stage: str,
        *,
        error_code: str = "",
        error_message: str = "",
    ) -> bool:
        with self.database.write() as db:
            cursor = db.execute(
                "UPDATE kg_build_jobs SET stage=?, error_code=?, "
                "error_message=?, updated_at=? "
                "WHERE id=? AND status='running'",
                (
                    stage,
                    error_code,
                    error_message,
                    self.now(),
                    job_id,
                ),
            )
        return cursor.rowcount == 1

    def record_source_result(
        self,
        job_id: str,
        *,
        succeeded: bool,
    ) -> bool:
        column = "completed_sources" if succeeded else "failed_sources"
        with self.database.write() as db:
            cursor = db.execute(
                f"UPDATE kg_build_jobs SET {column}={column}+1, updated_at=? "
                "WHERE id=? AND status='running'",
                (self.now(), job_id),
            )
        return cursor.rowcount == 1

    def finish(
        self,
        job_id: str,
        status: str,
        *,
        error_code: str = "",
        error_message: str = "",
    ) -> bool:
        if status not in {"succeeded", "failed"}:
            raise ValueError("KG build terminal status must be succeeded or failed")
        now = self.now()
        with self.database.write() as db:
            cursor = db.execute(
                "UPDATE kg_build_jobs SET status=?, stage='finished', "
                "error_code=?, error_message=?, updated_at=?, finished_at=? "
                "WHERE id=? AND status='running'",
                (
                    status,
                    error_code,
                    error_message,
                    now,
                    now,
                    job_id,
                ),
            )
        return cursor.rowcount == 1

    def begin_indexing_pipeline_stage(
        self,
        job_id: str,
        notebook_id: str,
        pipeline_id: str,
        pipeline_version: str,
        pipeline_generation: str,
        source_ids: Sequence[str],
    ) -> None:
        """Freeze the only source set this durable worker may publish."""
        snapshot = list(source_ids)
        if len(snapshot) != len(set(snapshot)):
            raise ValueError("indexing stage source snapshot contains duplicates")
        now = self.now()
        with self.database.write() as db:
            self.database.begin_guarded_write(db)
            authority = db.execute(
                "SELECT 1 FROM kg_build_jobs j JOIN notebooks n "
                "ON n.id=j.notebook_id WHERE j.id=? AND j.notebook_id=? "
                "AND j.mode='rebuild' AND j.status='running' "
                "AND COALESCE(n.indexing_pipeline,'')=? "
                "AND n.indexing_pipeline_version=? "
                "AND n.indexing_pipeline_generation=? "
                "AND n.indexing_pipeline_job_id=?",
                (
                    job_id, notebook_id, pipeline_id, pipeline_version,
                    pipeline_generation, job_id,
                ),
            ).fetchone()
            current_rows = db.execute(
                "SELECT s.id,s.updated_at,COUNT(se.id) AS element_count,"
                "COALESCE(MAX(se.created_at),'') AS element_updated_at "
                "FROM sources s LEFT JOIN source_elements se ON se.source_id=s.id "
                "WHERE s.notebook_id=? AND "
                f"{VISIBLE_SOURCE_TYPES_PREDICATE} GROUP BY s.id,s.updated_at "
                "ORDER BY s.id",
                (notebook_id,),
            ).fetchall()
            current = [str(row["id"]) for row in current_rows]
            source_snapshot = [
                {
                    "id": str(row["id"]),
                    "updated_at": str(row["updated_at"]),
                    "element_count": int(row["element_count"]),
                    "element_updated_at": str(row["element_updated_at"]),
                }
                for row in current_rows
            ]
            if authority is None or current != snapshot:
                raise IndexingPipelineStalePlanError(notebook_id)
            db.execute(
                "INSERT INTO indexing_pipeline_stages "
                "(job_id,notebook_id,pipeline_id,pipeline_version,"
                "pipeline_generation,source_snapshot,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    job_id, notebook_id, pipeline_id, pipeline_version,
                    pipeline_generation, json.dumps(source_snapshot), now, now,
                ),
            )
            db.executemany(
                "INSERT INTO indexing_pipeline_stage_sources "
                "(job_id,source_id,status,payload,created_at,updated_at) "
                "VALUES (?,?,'pending','{}',?,?)",
                [(job_id, source_id, now, now) for source_id in snapshot],
            )

    def _merge_stage_payload(
        self,
        job_id: str,
        source_id: str,
        section: str,
        payload: dict,
        *,
        completed: bool,
    ) -> bool:
        now = self.now()
        with self.database.write() as db:
            row = db.execute(
                "SELECT s.payload FROM indexing_pipeline_stage_sources s "
                "JOIN indexing_pipeline_stages h ON h.job_id=s.job_id "
                "JOIN kg_build_jobs j ON j.id=s.job_id "
                "WHERE s.job_id=? AND s.source_id=? AND j.status='running'",
                (job_id, source_id),
            ).fetchone()
            if row is None:
                return False
            merged = json.loads(row["payload"] or "{}")
            if not isinstance(merged, dict):
                raise ValueError("invalid indexing stage payload")
            merged[section] = payload
            cursor = db.execute(
                "UPDATE indexing_pipeline_stage_sources SET payload=?,status=?,"
                "updated_at=? WHERE job_id=? AND source_id=?",
                (
                    json.dumps(merged, ensure_ascii=False),
                    "completed" if completed else "pending",
                    now,
                    job_id,
                    source_id,
                ),
            )
            db.execute(
                "UPDATE indexing_pipeline_stages SET updated_at=? WHERE job_id=?",
                (now, job_id),
            )
        return cursor.rowcount == 1

    def stage_indexing_pipeline_chunks(
        self, job_id: str, source_id: str, payload: dict
    ) -> bool:
        return self._merge_stage_payload(
            job_id, source_id, "chunks", payload, completed=False
        )

    def stage_indexing_pipeline_kg(
        self, job_id: str, source_id: str, payload: dict
    ) -> bool:
        return self._merge_stage_payload(
            job_id, source_id, "kg", payload, completed=True
        )

    def complete_indexing_pipeline_stage_without_kg(self, job_id: str) -> bool:
        now = self.now()
        with self.database.write() as db:
            rows = db.execute(
                "SELECT source_id,payload FROM indexing_pipeline_stage_sources "
                "WHERE job_id=? ORDER BY source_id", (job_id,),
            ).fetchall()
            for row in rows:
                payload = json.loads(row["payload"] or "{}")
                if not isinstance(payload, dict) or "chunks" not in payload:
                    return False
                payload["kg"] = {"mode": "preserve"}
                db.execute(
                    "UPDATE indexing_pipeline_stage_sources "
                    "SET payload=?,status='completed',updated_at=? "
                    "WHERE job_id=? AND source_id=?",
                    (
                        json.dumps(payload, ensure_ascii=False), now,
                        job_id, row["source_id"],
                    ),
                )
            header = db.execute(
                "UPDATE indexing_pipeline_stages SET updated_at=? WHERE job_id=?",
                (now, job_id),
            )
        return header.rowcount == 1

    def discard_indexing_pipeline_stage(self, job_id: str) -> None:
        with self.database.write() as db:
            db.execute(
                "DELETE FROM indexing_pipeline_stages WHERE job_id=?", (job_id,)
            )

    @staticmethod
    def _delete_source_kg(
        db: sqlite3.Connection, notebook_id: str, source_id: str
    ) -> None:
        db.execute(
            "DELETE FROM kg_relation_completion_state WHERE source_id=?",
            (source_id,),
        )
        db.execute(
            "DELETE FROM knowledge_source_fact_backfills "
            "WHERE notebook_id=? AND source_id=?", (notebook_id, source_id),
        )
        db.execute(
            "DELETE FROM knowledge_source_facts "
            "WHERE notebook_id=? AND source_id=?", (notebook_id, source_id),
        )
        db.execute("DELETE FROM extraction_runs WHERE source_id=?", (source_id,))
        db.execute("DELETE FROM knowledge_relations WHERE source_id=?", (source_id,))
        # Fresh extraction rows carry source_id directly.  The reverse/evidence
        # clauses retain compatibility with older fused rows, while the hidden
        # source exclusion keeps Memory/Knowhow products core-owned.
        while True:
            rows = db.execute(
                "SELECT DISTINCT ko.id FROM knowledge_objects ko "
                "LEFT JOIN knowledge_object_sources kos ON kos.object_id=ko.id "
                "WHERE ko.notebook_id=? AND (ko.source_id=? OR kos.source_id=? "
                "OR EXISTS (SELECT 1 FROM json_each(CASE "
                "WHEN json_valid(ko.evidence) AND json_type(ko.evidence)='array' "
                "THEN ko.evidence ELSE '[]' END) e "
                "WHERE json_extract(e.value,'$.source_id')=?)) "
                "AND NOT EXISTS (SELECT 1 FROM sources hidden "
                "WHERE hidden.id=ko.source_id "
                "AND hidden.source_type IN ('memory','knowhow')) "
                "ORDER BY ko.id LIMIT ?",
                (
                    notebook_id, source_id, source_id, source_id,
                    INDEXING_PIPELINE_PUBLISH_DELETE_BATCH,
                ),
            ).fetchall()
            object_ids = [str(row["id"]) for row in rows]
            if not object_ids:
                break
            placeholders = ",".join("?" for _ in object_ids)
            db.execute(
                f"DELETE FROM relation_embeddings WHERE relation_id IN "
                f"(SELECT id FROM knowledge_relations WHERE "
                f"source_object_id IN ({placeholders}) OR "
                f"target_object_id IN ({placeholders}))",
                (*object_ids, *object_ids),
            )
            db.execute(
                f"DELETE FROM knowledge_relations WHERE "
                f"source_object_id IN ({placeholders}) OR "
                f"target_object_id IN ({placeholders})",
                (*object_ids, *object_ids),
            )
            db.execute(
                f"DELETE FROM kg_objects_fts WHERE object_id IN ({placeholders})",
                object_ids,
            )
            db.execute(
                f"DELETE FROM knowledge_embeddings WHERE object_id IN ({placeholders})",
                object_ids,
            )
            db.execute(
                f"DELETE FROM concept_clusters WHERE notebook_id=? "
                f"AND member_object_id IN ({placeholders})",
                (notebook_id, *object_ids),
            )
            db.execute(
                f"DELETE FROM knowledge_object_sources "
                f"WHERE object_id IN ({placeholders})", object_ids,
            )
            db.execute(
                f"DELETE FROM knowledge_objects WHERE id IN ({placeholders})",
                object_ids,
            )

    @staticmethod
    def _clear_notebook_derived_kg(
        db: sqlite3.Connection, notebook_id: str
    ) -> None:
        for table in (
            "concept_clusters",
            "concept_merge_candidates",
            "canonical_relations",
            "mention_edges",
            "concept_comentions",
            "communities",
            "community_members",
            "kg_community_edges",
            "kg_source_profiles",
            "kg_analysis_artifacts",
            "kg_rebuild_checkpoint",
            "kg_cluster_scratch",
            "kg_canonical_scratch",
            "kg_relation_completion_state",
        ):
            db.execute(f"DELETE FROM {table} WHERE notebook_id=?", (notebook_id,))
        db.execute(
            "DELETE FROM kg_conflict_candidates "
            "WHERE notebook_id=? AND status='pending'", (notebook_id,),
        )
        db.execute("DELETE FROM merge_review_jobs WHERE notebook_id=?", (notebook_id,))

    @staticmethod
    def _validated_vector(value: object, dimension: int | None) -> tuple[bytes, int]:
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError("staged embedding must be a non-empty vector")
        numbers = [float(item) for item in value]
        if not all(math.isfinite(item) for item in numbers):
            raise ValueError("staged embedding contains a non-finite value")
        current = len(numbers)
        if dimension is not None and current != dimension:
            raise ValueError("staged embedding dimensions differ")
        return encode_vector(numbers), current

    @classmethod
    def _validate_stage_payloads(
        cls,
        db: sqlite3.Connection,
        payloads: list[tuple[str, dict]],
    ) -> None:
        seen_chunks: set[str] = set()
        seen_objects: set[str] = set()
        seen_relations: set[str] = set()
        seen_facts: set[str] = set()
        dimensions: dict[str, int | None] = {
            "chunk": None,
            "object": None,
            "relation": None,
        }
        for source_id, payload in payloads:
            chunk_payload = payload["chunks"]
            fallback_warning = chunk_payload.get("chunk_fallback_warning", "")
            if (
                type(fallback_warning) is not str
                or len(fallback_warning) > _STAGE_FALLBACK_WARNING_MAX_CHARS
            ):
                raise ValueError("invalid staged chunk fallback warning")
            source_chunk_ids: set[str] = set()
            evidence_elements: set[str] = set()
            for row in chunk_payload["rows"]:
                if (
                    not isinstance(row, dict)
                    or str(row.get("source_id") or "") != source_id
                    or type(row.get("id")) is not str
                    or not isinstance(row.get("text"), str)
                    or not isinstance(row.get("section_path", ""), str)
                    or not isinstance(row.get("element_ids"), list)
                    or any(type(value) is not str for value in row["element_ids"])
                    or len(row["element_ids"]) != len(set(row["element_ids"]))
                ):
                    raise ValueError("invalid staged chunk row")
                chunk_id = row["id"]
                if chunk_id in seen_chunks:
                    raise ValueError("duplicate staged chunk id")
                seen_chunks.add(chunk_id)
                source_chunk_ids.add(chunk_id)
                evidence_elements.update(row["element_ids"])
            chunk_vector_ids: set[str] = set()
            for vector in chunk_payload.get("vectors") or []:
                if (
                    not isinstance(vector, dict)
                    or vector.get("id") not in source_chunk_ids
                    or vector.get("id") in chunk_vector_ids
                    or "created_at" not in vector
                ):
                    raise ValueError("invalid staged chunk vector")
                chunk_vector_ids.add(vector["id"])
                encoded, dimensions["chunk"] = cls._validated_vector(
                    vector.get("vector"), dimensions["chunk"]
                )
                vector["_encoded"] = encoded

            kg = payload["kg"]
            if kg["mode"] == "replace":
                extraction = kg.get("extraction")
                extraction_id = (
                    str(extraction.get("id") or "")
                    if isinstance(extraction, dict) else ""
                )
                source_object_ids: set[str] = set()
                for row in kg.get("objects") or []:
                    if (
                        not isinstance(row, dict)
                        or type(row.get("id")) is not str
                        or not str(row.get("object_type") or "")
                        or row.get("status") not in {"approved", "reviewed"}
                        or not isinstance(row.get("payload"), dict)
                        or not isinstance(row.get("evidence"), list)
                    ):
                        raise ValueError("invalid staged KG object")
                    if any(
                        isinstance(item, dict)
                        and item.get("source_id")
                        and item.get("source_id") != source_id
                        for item in row["evidence"]
                    ):
                        raise ValueError("staged object evidence crosses source")
                    object_id = row["id"]
                    if object_id in seen_objects:
                        raise ValueError("duplicate staged KG object id")
                    seen_objects.add(object_id)
                    source_object_ids.add(object_id)
                    evidence_elements.update(
                        str(item.get("element_id") or "")
                        for item in row["evidence"]
                        if isinstance(item, dict) and item.get("element_id")
                    )
                for row in kg.get("object_sources") or []:
                    if (
                        not isinstance(row, dict)
                        or row.get("object_id") not in source_object_ids
                        or row.get("source_id") != source_id
                    ):
                        raise ValueError("invalid staged object-source row")
                source_relation_ids: set[str] = set()
                for row in kg.get("relations") or []:
                    if (
                        not isinstance(row, dict)
                        or type(row.get("id")) is not str
                        or row.get("source_object_id") not in source_object_ids
                        or row.get("target_object_id") not in source_object_ids
                        or not str(row.get("edge_type") or "")
                        or not isinstance(row.get("evidence"), list)
                    ):
                        raise ValueError("invalid staged KG relation")
                    if any(
                        isinstance(item, dict)
                        and item.get("source_id")
                        and item.get("source_id") != source_id
                        for item in row["evidence"]
                    ):
                        raise ValueError("staged relation evidence crosses source")
                    relation_id = row["id"]
                    if relation_id in seen_relations:
                        raise ValueError("duplicate staged KG relation id")
                    seen_relations.add(relation_id)
                    source_relation_ids.add(relation_id)
                    evidence_elements.update(
                        str(item.get("element_id") or "")
                        for item in row["evidence"]
                        if isinstance(item, dict) and item.get("element_id")
                    )
                source_fact_ids: set[str] = set()
                for row in kg.get("facts") or []:
                    if (
                        not isinstance(row, dict)
                        or type(row.get("id")) is not str
                        or row.get("global_object_id") not in source_object_ids
                        or not extraction_id
                        or row.get("source_generation") != extraction_id
                        or not isinstance(row.get("payload"), dict)
                        or not isinstance(row.get("evidence"), list)
                    ):
                        raise ValueError("invalid staged source fact")
                    if any(
                        isinstance(item, dict)
                        and item.get("source_id")
                        and item.get("source_id") != source_id
                        for item in row["evidence"]
                    ):
                        raise ValueError("staged fact evidence crosses source")
                    fact_id = row["id"]
                    if fact_id in seen_facts:
                        raise ValueError("duplicate staged source fact id")
                    seen_facts.add(fact_id)
                    source_fact_ids.add(fact_id)
                    evidence_elements.update(
                        str(item.get("element_id") or "")
                        for item in row["evidence"]
                        if isinstance(item, dict) and item.get("element_id")
                    )
                for row in kg.get("fact_elements") or []:
                    if (
                        not isinstance(row, dict)
                        or row.get("fact_id") not in source_fact_ids
                        or row.get("source_generation") != extraction_id
                        or type(row.get("element_id")) is not str
                    ):
                        raise ValueError("invalid staged source fact evidence")
                    evidence_elements.add(row["element_id"])
                for key, allowed_ids, dimension_key in (
                    ("object_vectors", source_object_ids, "object"),
                    ("relation_vectors", source_relation_ids, "relation"),
                ):
                    vector_ids: set[str] = set()
                    for vector in kg.get(key) or []:
                        if (
                            not isinstance(vector, dict)
                            or vector.get("id") not in allowed_ids
                            or vector.get("id") in vector_ids
                            or "created_at" not in vector
                        ):
                            raise ValueError("invalid staged KG vector")
                        vector_ids.add(vector["id"])
                        encoded, dimensions[dimension_key] = cls._validated_vector(
                            vector.get("vector"), dimensions[dimension_key]
                        )
                        vector["_encoded"] = encoded
                if extraction is not None and (
                    not isinstance(extraction, dict)
                    or not extraction_id
                    or "created_at" not in extraction
                    or "updated_at" not in extraction
                ):
                    raise ValueError("invalid staged extraction run")
            expected = sorted(value for value in evidence_elements if value)
            if expected:
                owned = int(db.execute(
                    "WITH requested(id) AS "
                    "(SELECT CAST(value AS TEXT) FROM json_each(?)) "
                    "SELECT COUNT(*) AS c FROM requested JOIN source_elements se "
                    "ON se.id=requested.id WHERE se.source_id=?",
                    (json.dumps(expected), source_id),
                ).fetchone()["c"])
                if owned != len(expected):
                    raise ValueError("staged element reference is stale")

    def publish_indexing_pipeline_success(
        self,
        job_id: str,
        notebook_id: str,
        pipeline_id: str,
        pipeline_version: str,
        pipeline_generation: str,
    ) -> bool:
        """CAS and publish the complete staged notebook generation."""
        now = self.now()
        with self.database.write() as db:
            self.database.begin_guarded_write(db)
            authority = db.execute(
                "SELECT 1 FROM kg_build_jobs j JOIN notebooks n "
                "ON n.id=j.notebook_id WHERE j.id=? AND j.notebook_id=? "
                "AND j.mode='rebuild' AND j.status='running' "
                "AND COALESCE(n.indexing_pipeline,'')=? "
                "AND n.indexing_pipeline_version=? "
                "AND n.indexing_pipeline_generation=? "
                "AND n.indexing_pipeline_job_id=?",
                (
                    job_id,
                    notebook_id,
                    pipeline_id,
                    pipeline_version,
                    pipeline_generation,
                    job_id,
                ),
            ).fetchone()
            if authority is None:
                return False
            header = db.execute(
                "SELECT * FROM indexing_pipeline_stages WHERE job_id=? "
                "AND notebook_id=? AND pipeline_id=? AND pipeline_version=? "
                "AND pipeline_generation=?",
                (
                    job_id, notebook_id, pipeline_id, pipeline_version,
                    pipeline_generation,
                ),
            ).fetchone()
            if header is None:
                return False
            try:
                snapshot_rows = json.loads(header["source_snapshot"])
            except (TypeError, ValueError):
                return False
            if not isinstance(snapshot_rows, list) or any(
                not isinstance(value, dict) or type(value.get("id")) is not str
                for value in snapshot_rows
            ):
                return False
            snapshot = [str(value["id"]) for value in snapshot_rows]
            if len(snapshot) != len(set(snapshot)):
                return False
            current_rows = db.execute(
                "SELECT s.id,s.updated_at,COUNT(se.id) AS element_count,"
                "COALESCE(MAX(se.created_at),'') AS element_updated_at "
                "FROM sources s LEFT JOIN source_elements se ON se.source_id=s.id "
                "WHERE s.notebook_id=? AND "
                f"{VISIBLE_SOURCE_TYPES_PREDICATE} GROUP BY s.id,s.updated_at "
                "ORDER BY s.id", (notebook_id,),
            ).fetchall()
            current_snapshot = [
                {
                    "id": str(row["id"]),
                    "updated_at": str(row["updated_at"]),
                    "element_count": int(row["element_count"]),
                    "element_updated_at": str(row["element_updated_at"]),
                }
                for row in current_rows
            ]
            # 已登记内存边界(评审 P1,后续独立一件事):发布事务把整本库的 staged
            # chunk+向量 JSON 一次读进内存并在写锁内解析——部署上限拉满
            # (`indexing_pipeline_rebuild_max_*`)的最坏情形是 GB 级瞬时驻留。
            # 正解是 staging 向量改存 encode_vector 的 bytes 并按 source keyset
            # 分页读取/校验;当前默认上限下的典型库远小于最坏值,先如实登记。
            staged = db.execute(
                "SELECT source_id,status,payload "
                "FROM indexing_pipeline_stage_sources WHERE job_id=? "
                "ORDER BY source_id", (job_id,),
            ).fetchall()
            if current_snapshot != snapshot_rows or [row["source_id"] for row in staged] != snapshot:
                return False
            payloads: list[tuple[str, dict]] = []
            kg_modes: set[str] = set()
            for row in staged:
                try:
                    payload = json.loads(row["payload"])
                except (TypeError, ValueError):
                    return False
                chunks = payload.get("chunks") if isinstance(payload, dict) else None
                kg = payload.get("kg") if isinstance(payload, dict) else None
                mode = kg.get("mode") if isinstance(kg, dict) else None
                if (
                    row["status"] != "completed"
                    or not isinstance(chunks, dict)
                    or not isinstance(chunks.get("rows"), list)
                    or mode not in {"replace", "preserve"}
                ):
                    return False
                kg_modes.add(mode)
                payloads.append((str(row["source_id"]), payload))
            if len(kg_modes) > 1:
                return False

            self._validate_stage_payloads(db, payloads)

            # All validation precedes the first live mutation.
            for source_id in snapshot:
                db.execute(
                    "DELETE FROM chunks_fts WHERE chunk_id IN "
                    "(SELECT id FROM chunks WHERE source_id=?)", (source_id,),
                )
                db.execute("DELETE FROM chunks WHERE source_id=?", (source_id,))
            for source_id, payload in payloads:
                chunk_payload = payload["chunks"]
                rows = chunk_payload["rows"]
                created_at = str(chunk_payload.get("created_at") or now)
                for row in rows:
                    if not isinstance(row, dict) or str(row.get("source_id")) != source_id:
                        raise ValueError("invalid staged chunk source")
                    chunk_id = str(row["id"])
                    text = str(row["text"])
                    element_ids = row.get("element_ids") or []
                    db.execute(
                        "INSERT INTO chunks "
                        "(id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (
                            chunk_id, notebook_id, source_id, text,
                            str(row.get("section_path") or ""),
                            json.dumps(element_ids), created_at,
                        ),
                    )
                    db.execute(
                        "INSERT INTO chunks_fts(chunk_id,notebook_id,text) "
                        "VALUES (?,?,?)", (chunk_id, notebook_id, text),
                    )
                    db.executemany(
                        "INSERT INTO chunk_elements "
                        "(notebook_id,element_id,chunk_id) VALUES (?,?,?)",
                        [
                            (notebook_id, str(element_id), chunk_id)
                            for element_id in element_ids
                        ],
                    )
                for vector in chunk_payload.get("vectors") or []:
                    db.execute(
                        "INSERT INTO chunk_embeddings "
                        "(chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                        (
                            str(vector["id"]), notebook_id,
                            vector["_encoded"],
                            str(vector.get("created_at") or created_at),
                        ),
                    )
                db.execute(
                    "UPDATE sources SET chunked_at=? WHERE id=? AND notebook_id=?",
                    (created_at, source_id, notebook_id),
                )
                # 每源回退徽标随本事务置/清(codex #602 R4 P2):非空写稳定前缀
                # 诊断(不覆盖既有 MinerU 诊断),空则只清本前缀自己的旧诊断——
                # 干净重建后不残留过期「降级整理」。
                warning_code = str(
                    chunk_payload.get("chunk_fallback_warning") or ""
                )
                prefix = INDEXING_CHUNK_FALLBACK_WARNING_PREFIX
                if warning_code:
                    db.execute(
                        "UPDATE sources SET error_message=? WHERE id=? "
                        "AND notebook_id=? AND (error_message IS NULL "
                        "OR error_message='' OR error_message LIKE ?)",
                        (
                            f"{prefix} {warning_code}", source_id,
                            notebook_id, f"{prefix}%",
                        ),
                    )
                else:
                    db.execute(
                        "UPDATE sources SET error_message='' WHERE id=? "
                        "AND notebook_id=? AND error_message LIKE ?",
                        (source_id, notebook_id, f"{prefix}%"),
                    )

            # Chunk publication invalidates every notebook-derived KG product
            # even when the selected pipeline preserves base KG rows.  Derived
            # mentions/checkpoints can otherwise retain links to deleted chunks.
            self._clear_notebook_derived_kg(db, notebook_id)
            if kg_modes == {"replace"}:
                for source_id in snapshot:
                    self._delete_source_kg(db, notebook_id, source_id)
                for source_id, payload in payloads:
                    kg = payload["kg"]
                    for row in kg.get("objects") or []:
                        db.execute(
                            "INSERT INTO knowledge_objects "
                            "(id,notebook_id,object_type,status,owner,payload,evidence,"
                            "source_candidate_id,source_id,created_at,updated_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                row["id"], notebook_id, row["object_type"],
                                row["status"], "",
                                json.dumps(row["payload"], ensure_ascii=False),
                                json.dumps(row["evidence"], ensure_ascii=False),
                                None, source_id, row["created_at"], row["updated_at"],
                            ),
                        )
                        name = str((row.get("payload") or {}).get("name") or "")
                        if name.strip():
                            db.execute(
                                "INSERT INTO kg_objects_fts(object_id,notebook_id,name) "
                                "VALUES (?,?,?)", (row["id"], notebook_id, name),
                            )
                    db.executemany(
                        "INSERT INTO knowledge_object_sources "
                        "(object_id,source_id,notebook_id) VALUES (?,?,?)",
                        [
                            (row["object_id"], row["source_id"], notebook_id)
                            for row in kg.get("object_sources") or []
                        ],
                    )
                    for row in kg.get("relations") or []:
                        db.execute(
                            "INSERT INTO knowledge_relations "
                            "(id,notebook_id,source_id,source_object_id,"
                            "target_object_id,edge_type,evidence,created_at) "
                            "VALUES (?,?,?,?,?,?,?,?)",
                            (
                                row["id"], notebook_id, source_id,
                                row["source_object_id"], row["target_object_id"],
                                row["edge_type"],
                                json.dumps(row["evidence"], ensure_ascii=False),
                                row["created_at"],
                            ),
                        )
                    for row in kg.get("facts") or []:
                        db.execute(
                            "INSERT INTO knowledge_source_facts "
                            "(id,notebook_id,source_id,source_generation,local_object_id,"
                            "global_object_id,object_type,payload,evidence,projection_version,"
                            "projection_origin,created_at,updated_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?,1,'live',?,?)",
                            (
                                row["id"], notebook_id, source_id,
                                row["source_generation"], row["local_object_id"],
                                row["global_object_id"], row["object_type"],
                                json.dumps(row["payload"], ensure_ascii=False),
                                json.dumps(row["evidence"], ensure_ascii=False),
                                row["created_at"], row["updated_at"],
                            ),
                        )
                    db.executemany(
                        "INSERT INTO knowledge_source_fact_elements "
                        "(fact_id,notebook_id,source_id,source_generation,"
                        "element_id,created_at) VALUES (?,?,?,?,?,?)",
                        [
                            (
                                row["fact_id"], notebook_id, source_id,
                                row["source_generation"], row["element_id"],
                                row["created_at"],
                            )
                            for row in kg.get("fact_elements") or []
                        ],
                    )
                    for table, id_key in (
                        ("knowledge_embeddings", "object_id"),
                        ("relation_embeddings", "relation_id"),
                    ):
                        vector_key = (
                            "object_vectors" if table == "knowledge_embeddings"
                            else "relation_vectors"
                        )
                        db.executemany(
                            f"INSERT INTO {table} "
                            f"({id_key},notebook_id,vector,created_at) VALUES (?,?,?,?)",
                            [
                                (
                                    row["id"], notebook_id,
                                    row["_encoded"], row["created_at"],
                                )
                                for row in kg.get(vector_key) or []
                            ],
                        )
                    extraction = kg.get("extraction")
                    if isinstance(extraction, dict):
                        db.execute(
                            "INSERT INTO extraction_runs "
                            "(id,notebook_id,source_id,run_type,status,error_message,"
                            "indexing_pipeline_id,indexing_pipeline_version,created_at,updated_at) "
                            "VALUES (?,?,?,'kg','completed',?,?,?,?,?)",
                            (
                                extraction["id"], notebook_id, source_id,
                                extraction.get("error_message", ""), pipeline_id,
                                pipeline_version, extraction["created_at"],
                                extraction["updated_at"],
                            ),
                        )
            cleared = db.execute(
                "UPDATE notebooks SET indexing_pipeline_job_id='',updated_at=? "
                "WHERE id=? AND COALESCE(indexing_pipeline,'')=? "
                "AND indexing_pipeline_version=? "
                "AND indexing_pipeline_generation=? "
                "AND indexing_pipeline_job_id=?",
                (
                    now,
                    notebook_id,
                    pipeline_id,
                    pipeline_version,
                    pipeline_generation,
                    job_id,
                ),
            )
            if cleared.rowcount != 1:
                raise RuntimeError("indexing pipeline publication authority changed")
            db.execute(
                "INSERT INTO unified_kg_state "
                "(notebook_id,dirty,kg_mutation_seq,cluster_mutation_seq,"
                "cluster_input_version,community_seq,canonical_rel_seq,mention_seq,"
                "updated_at,indexing_pipeline_id,indexing_pipeline_version) "
                "VALUES (?,1,1,1,'',-1,-1,-1,?,?,?) "
                "ON CONFLICT(notebook_id) DO UPDATE SET "
                "dirty=1,kg_mutation_seq=unified_kg_state.kg_mutation_seq+1,"
                "cluster_mutation_seq=unified_kg_state.cluster_mutation_seq+1,"
                "cluster_input_version='',community_seq=-1,canonical_rel_seq=-1,"
                "mention_seq=-1,"
                "updated_at=excluded.updated_at,"
                "indexing_pipeline_id=excluded.indexing_pipeline_id,"
                "indexing_pipeline_version=excluded.indexing_pipeline_version",
                (notebook_id, now, pipeline_id, pipeline_version),
            )
            finished = db.execute(
                "UPDATE kg_build_jobs SET status='succeeded',stage='finished',"
                "error_code='',error_message='',updated_at=?,finished_at=? "
                "WHERE id=? AND notebook_id=? AND mode='rebuild' "
                "AND status='running'",
                (now, now, job_id, notebook_id),
            )
            if finished.rowcount != 1:
                raise RuntimeError("indexing pipeline durable job changed")
            db.execute(
                "DELETE FROM indexing_pipeline_stages WHERE job_id=?", (job_id,)
            )
        return True

    def fail_submission(self, job_id: str) -> bool:
        return self.finish(
            job_id,
            "failed",
            error_code="job_submission_failed",
            error_message="知识图谱分析任务未能启动，请稍后重试。",
        )


__all__ = [
    "KgBuildAlreadyRunning",
    "KgBuildJobStore",
]
