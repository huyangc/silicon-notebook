from __future__ import annotations

from typing import Callable, Sequence

from psycopg import errors

from app.repositories.postgres._store_utils import (
    TimestampInput,
    execute_many,
    iso_timestamp,
    json_value,
    jsonb,
    normalize_timestamp,
    normalized_clock,
)
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.embedding_store import _validated_vector
from app.repositories.postgres.source_store import VISIBLE_SOURCE_TYPES_PREDICATE
from app.domain.indexing_pipeline import IndexingPipelineStalePlanError
from app.repositories.ports import (
    INDEXING_PIPELINE_PUBLISH_DELETE_BATCH,
    KgBuildAlreadyRunning,
)


class KgBuildJobStore:
    def __init__(
        self,
        database: PostgresDatabase,
        *,
        new_id: Callable[[str], str],
        now: Callable[[], TimestampInput],
    ) -> None:
        self.database = database
        self.new_id = new_id
        self.now = normalized_clock(now)

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
            "created_at": iso_timestamp(row["created_at"]),
            "updated_at": iso_timestamp(row["updated_at"]),
            "finished_at": iso_timestamp(row["finished_at"]),
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
            with self.database.write() as connection:
                connection.execute(
                    "INSERT INTO kg_build_jobs"
                    "(id,notebook_id,created_by,mode,status,stage,total_sources,"
                    "completed_sources,failed_sources,error_code,error_message,"
                    "created_at,updated_at,finished_at) "
                    "VALUES (%s,%s,%s,%s,'running','probing',%s,0,0,'','',%s,%s,NULL)",
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
        except errors.UniqueViolation as exc:
            if exc.diag.constraint_name == "idx_kg_build_jobs_one_running":
                raise KgBuildAlreadyRunning(notebook_id) from exc
            raise
        return self.get(job_id)

    def get(self, job_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM kg_build_jobs WHERE id=%s", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._row(row)

    def latest(self, notebook_id: str) -> dict | None:
        with self.database.connect() as connection:
            return self.latest_on(connection, notebook_id)

    def latest_on(self, connection, notebook_id: str) -> dict | None:
        row = connection.execute(
            "SELECT * FROM kg_build_jobs WHERE notebook_id=%s "
            "ORDER BY created_at DESC,ordinal DESC LIMIT 1",
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
        with self.database.write() as connection:
            cursor = connection.execute(
                "UPDATE kg_build_jobs SET stage=%s,error_code=%s,error_message=%s,"
                "updated_at=%s WHERE id=%s AND status='running'",
                (stage, error_code, error_message, self.now(), job_id),
            )
        return cursor.rowcount == 1

    def record_source_result(self, job_id: str, *, succeeded: bool) -> bool:
        column = "completed_sources" if succeeded else "failed_sources"
        with self.database.write() as connection:
            cursor = connection.execute(
                f"UPDATE kg_build_jobs SET {column}={column}+1,updated_at=%s "
                "WHERE id=%s AND status='running'",
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
        with self.database.write() as connection:
            cursor = connection.execute(
                "UPDATE kg_build_jobs SET status=%s,stage='finished',error_code=%s,"
                "error_message=%s,updated_at=%s,finished_at=%s "
                "WHERE id=%s AND status='running'",
                (status, error_code, error_message, now, now, job_id),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _source_snapshot(connection, notebook_id: str, *, lock: bool) -> list[dict]:
        rows = connection.execute(
            "SELECT id,updated_at FROM sources WHERE notebook_id=%s AND "
            f"{VISIBLE_SOURCE_TYPES_PREDICATE} ORDER BY id COLLATE \"C\""
            + (" FOR UPDATE" if lock else ""),
            (notebook_id,),
        ).fetchall()
        output: list[dict] = []
        for row in rows:
            aggregate = connection.execute(
                "SELECT COUNT(*) AS element_count,MAX(created_at) AS element_updated_at "
                "FROM source_elements WHERE source_id=%s",
                (row["id"],),
            ).fetchone()
            output.append(
                {
                    "id": str(row["id"]),
                    "updated_at": iso_timestamp(row["updated_at"]),
                    "element_count": int(aggregate["element_count"]),
                    "element_updated_at": iso_timestamp(
                        aggregate["element_updated_at"]
                    ),
                }
            )
        return output

    def begin_indexing_pipeline_stage(
        self,
        job_id: str,
        notebook_id: str,
        pipeline_id: str,
        pipeline_version: str,
        pipeline_generation: str,
        source_ids: Sequence[str],
    ) -> None:
        snapshot_ids = list(source_ids)
        if len(snapshot_ids) != len(set(snapshot_ids)):
            raise ValueError("indexing stage source snapshot contains duplicates")
        now = self.now()
        with self.database.write() as connection:
            authority = connection.execute(
                "SELECT 1 FROM kg_build_jobs j JOIN notebooks n "
                "ON n.id=j.notebook_id WHERE j.id=%s AND j.notebook_id=%s "
                "AND j.mode='rebuild' AND j.status='running' "
                "AND COALESCE(n.indexing_pipeline,'')=%s "
                "AND n.indexing_pipeline_version=%s "
                "AND n.indexing_pipeline_generation=%s "
                "AND n.indexing_pipeline_job_id=%s FOR UPDATE OF j,n",
                (
                    job_id, notebook_id, pipeline_id, pipeline_version,
                    pipeline_generation, job_id,
                ),
            ).fetchone()
            snapshot = self._source_snapshot(connection, notebook_id, lock=True)
            if authority is None or [row["id"] for row in snapshot] != snapshot_ids:
                raise IndexingPipelineStalePlanError(notebook_id)
            connection.execute(
                "INSERT INTO indexing_pipeline_stages "
                "(job_id,notebook_id,pipeline_id,pipeline_version,"
                "pipeline_generation,source_snapshot,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    job_id, notebook_id, pipeline_id, pipeline_version,
                    pipeline_generation, jsonb(snapshot), now, now,
                ),
            )
            execute_many(
                connection,
                "INSERT INTO indexing_pipeline_stage_sources "
                "(job_id,source_id,status,payload,created_at,updated_at) "
                "VALUES (%s,%s,'pending',%s,%s,%s)",
                [
                    (job_id, source_id, jsonb({}), now, now)
                    for source_id in snapshot_ids
                ],
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
        with self.database.write() as connection:
            row = connection.execute(
                "SELECT s.payload FROM indexing_pipeline_stage_sources s "
                "JOIN indexing_pipeline_stages h ON h.job_id=s.job_id "
                "JOIN kg_build_jobs j ON j.id=s.job_id "
                "WHERE s.job_id=%s AND s.source_id=%s AND j.status='running' "
                "FOR UPDATE OF s,h,j", (job_id, source_id),
            ).fetchone()
            if row is None:
                return False
            merged = json_value(row["payload"], {})
            if not isinstance(merged, dict):
                raise ValueError("invalid indexing stage payload")
            merged[section] = payload
            cursor = connection.execute(
                "UPDATE indexing_pipeline_stage_sources SET payload=%s,status=%s,"
                "updated_at=%s WHERE job_id=%s AND source_id=%s",
                (
                    jsonb(merged), "completed" if completed else "pending",
                    now, job_id, source_id,
                ),
            )
            connection.execute(
                "UPDATE indexing_pipeline_stages SET updated_at=%s WHERE job_id=%s",
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
        with self.database.write() as connection:
            rows = connection.execute(
                "SELECT source_id,payload FROM indexing_pipeline_stage_sources "
                "WHERE job_id=%s ORDER BY source_id COLLATE \"C\" FOR UPDATE",
                (job_id,),
            ).fetchall()
            for row in rows:
                payload = json_value(row["payload"], {})
                if not isinstance(payload, dict) or "chunks" not in payload:
                    return False
                payload["kg"] = {"mode": "preserve"}
                connection.execute(
                    "UPDATE indexing_pipeline_stage_sources "
                    "SET payload=%s,status='completed',updated_at=%s "
                    "WHERE job_id=%s AND source_id=%s",
                    (jsonb(payload), now, job_id, row["source_id"]),
                )
            header = connection.execute(
                "UPDATE indexing_pipeline_stages SET updated_at=%s WHERE job_id=%s",
                (now, job_id),
            )
        return header.rowcount == 1

    def discard_indexing_pipeline_stage(self, job_id: str) -> None:
        with self.database.write() as connection:
            connection.execute(
                "DELETE FROM indexing_pipeline_stages WHERE job_id=%s", (job_id,)
            )

    @staticmethod
    def _delete_source_kg(connection, notebook_id: str, source_id: str) -> None:
        connection.execute(
            "DELETE FROM kg_relation_completion_state WHERE source_id=%s",
            (source_id,),
        )
        connection.execute(
            "DELETE FROM knowledge_source_fact_backfills "
            "WHERE notebook_id=%s AND source_id=%s", (notebook_id, source_id),
        )
        connection.execute(
            "DELETE FROM knowledge_source_facts "
            "WHERE notebook_id=%s AND source_id=%s", (notebook_id, source_id),
        )
        connection.execute("DELETE FROM extraction_runs WHERE source_id=%s", (source_id,))
        # PostgreSQL's legacy relation_embeddings table intentionally has no
        # FK to knowledge_relations.  Delete vectors before either relation
        # deletion shape; otherwise a successful publish can leave an orphan
        # vector or collide with a staged relation that reuses the stable id.
        connection.execute(
            "DELETE FROM relation_embeddings WHERE relation_id IN "
            "(SELECT id FROM knowledge_relations WHERE source_id=%s)",
            (source_id,),
        )
        connection.execute(
            "DELETE FROM knowledge_relations WHERE source_id=%s", (source_id,)
        )
        while True:
            rows = connection.execute(
                "SELECT DISTINCT ko.id FROM knowledge_objects ko "
                "LEFT JOIN knowledge_object_sources kos ON kos.object_id=ko.id "
                "WHERE ko.notebook_id=%s AND (ko.source_id=%s OR kos.source_id=%s "
                "OR ko.evidence @> jsonb_build_array("
                "jsonb_build_object('source_id',%s::text))) "
                "AND NOT EXISTS (SELECT 1 FROM sources hidden "
                "WHERE hidden.id=ko.source_id "
                "AND hidden.source_type IN ('memory','knowhow')) "
                "ORDER BY ko.id COLLATE \"C\" LIMIT %s",
                (
                    notebook_id, source_id, source_id, source_id,
                    INDEXING_PIPELINE_PUBLISH_DELETE_BATCH,
                ),
            ).fetchall()
            object_ids = [str(row["id"]) for row in rows]
            if not object_ids:
                break
            connection.execute(
                "DELETE FROM relation_embeddings WHERE relation_id IN "
                "(SELECT id FROM knowledge_relations WHERE "
                "source_object_id=ANY(%s) OR target_object_id=ANY(%s))",
                (object_ids, object_ids),
            )
            connection.execute(
                "DELETE FROM knowledge_relations WHERE "
                "source_object_id=ANY(%s) OR target_object_id=ANY(%s)",
                (object_ids, object_ids),
            )
            connection.execute(
                "DELETE FROM knowledge_embeddings WHERE object_id=ANY(%s)",
                (object_ids,),
            )
            connection.execute(
                "DELETE FROM concept_clusters WHERE notebook_id=%s "
                "AND member_object_id=ANY(%s)", (notebook_id, object_ids),
            )
            connection.execute(
                "DELETE FROM knowledge_object_sources WHERE object_id=ANY(%s)",
                (object_ids,),
            )
            connection.execute(
                "DELETE FROM knowledge_objects WHERE id=ANY(%s)", (object_ids,)
            )

    @staticmethod
    def _clear_notebook_derived_kg(connection, notebook_id: str) -> None:
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
            connection.execute(
                f"DELETE FROM {table} WHERE notebook_id=%s", (notebook_id,)
            )
        connection.execute(
            "DELETE FROM kg_conflict_candidates "
            "WHERE notebook_id=%s AND status='pending'", (notebook_id,),
        )
        connection.execute(
            "DELETE FROM merge_review_jobs WHERE notebook_id=%s", (notebook_id,)
        )

    @staticmethod
    def _validate_stage_payloads(connection, payloads: list[tuple[str, dict]]) -> None:
        seen_chunks: set[str] = set()
        seen_objects: set[str] = set()
        seen_relations: set[str] = set()
        seen_facts: set[str] = set()
        dimensions: dict[str, int | None] = {
            "chunk": None, "object": None, "relation": None,
        }
        for source_id, payload in payloads:
            chunks = payload["chunks"]
            chunks["_created_at"] = normalize_timestamp(
                str(chunks.get("created_at"))
            )
            chunk_ids: set[str] = set()
            element_ids: set[str] = set()
            for row in chunks["rows"]:
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
                if row["id"] in seen_chunks:
                    raise ValueError("duplicate staged chunk id")
                seen_chunks.add(row["id"])
                chunk_ids.add(row["id"])
                element_ids.update(row["element_ids"])
            vector_ids: set[str] = set()
            for vector in chunks.get("vectors") or []:
                if (
                    not isinstance(vector, dict)
                    or vector.get("id") not in chunk_ids
                    or vector.get("id") in vector_ids
                ):
                    raise ValueError("invalid staged chunk vector")
                vector_ids.add(vector["id"])
                encoded, dimensions["chunk"] = _validated_vector(
                    vector.get("vector"), dimension=dimensions["chunk"]
                )
                vector["_encoded"] = encoded
                vector["_created_at"] = normalize_timestamp(
                    str(vector.get("created_at"))
                )

            kg = payload["kg"]
            if kg["mode"] == "replace":
                extraction = kg.get("extraction")
                extraction_id = (
                    str(extraction.get("id") or "")
                    if isinstance(extraction, dict) else ""
                )
                object_ids: set[str] = set()
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
                    if row["id"] in seen_objects:
                        raise ValueError("duplicate staged KG object id")
                    seen_objects.add(row["id"])
                    object_ids.add(row["id"])
                    row["_created_at"] = normalize_timestamp(str(row.get("created_at")))
                    row["_updated_at"] = normalize_timestamp(str(row.get("updated_at")))
                    element_ids.update(
                        str(item.get("element_id") or "")
                        for item in row["evidence"]
                        if isinstance(item, dict) and item.get("element_id")
                    )
                for row in kg.get("object_sources") or []:
                    if (
                        not isinstance(row, dict)
                        or row.get("object_id") not in object_ids
                        or row.get("source_id") != source_id
                    ):
                        raise ValueError("invalid staged object-source row")
                relation_ids: set[str] = set()
                for row in kg.get("relations") or []:
                    if (
                        not isinstance(row, dict)
                        or type(row.get("id")) is not str
                        or row.get("source_object_id") not in object_ids
                        or row.get("target_object_id") not in object_ids
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
                    if row["id"] in seen_relations:
                        raise ValueError("duplicate staged KG relation id")
                    seen_relations.add(row["id"])
                    relation_ids.add(row["id"])
                    row["_created_at"] = normalize_timestamp(str(row.get("created_at")))
                    element_ids.update(
                        str(item.get("element_id") or "")
                        for item in row["evidence"]
                        if isinstance(item, dict) and item.get("element_id")
                    )
                fact_ids: set[str] = set()
                for row in kg.get("facts") or []:
                    if (
                        not isinstance(row, dict)
                        or type(row.get("id")) is not str
                        or row.get("global_object_id") not in object_ids
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
                    if row["id"] in seen_facts:
                        raise ValueError("duplicate staged source fact id")
                    seen_facts.add(row["id"])
                    fact_ids.add(row["id"])
                    row["_created_at"] = normalize_timestamp(str(row.get("created_at")))
                    row["_updated_at"] = normalize_timestamp(str(row.get("updated_at")))
                    element_ids.update(
                        str(item.get("element_id") or "")
                        for item in row["evidence"]
                        if isinstance(item, dict) and item.get("element_id")
                    )
                for row in kg.get("fact_elements") or []:
                    if (
                        not isinstance(row, dict)
                        or row.get("fact_id") not in fact_ids
                        or row.get("source_generation") != extraction_id
                        or type(row.get("element_id")) is not str
                    ):
                        raise ValueError("invalid staged source fact evidence")
                    element_ids.add(row["element_id"])
                    row["_created_at"] = normalize_timestamp(str(row.get("created_at")))
                for key, allowed, dimension_key in (
                    ("object_vectors", object_ids, "object"),
                    ("relation_vectors", relation_ids, "relation"),
                ):
                    used: set[str] = set()
                    for vector in kg.get(key) or []:
                        if (
                            not isinstance(vector, dict)
                            or vector.get("id") not in allowed
                            or vector.get("id") in used
                        ):
                            raise ValueError("invalid staged KG vector")
                        used.add(vector["id"])
                        encoded, dimensions[dimension_key] = _validated_vector(
                            vector.get("vector"), dimension=dimensions[dimension_key]
                        )
                        vector["_encoded"] = encoded
                        vector["_created_at"] = normalize_timestamp(
                            str(vector.get("created_at"))
                        )
                if extraction is not None:
                    if not extraction_id:
                        raise ValueError("invalid staged extraction run")
                    extraction["_created_at"] = normalize_timestamp(
                        str(extraction.get("created_at"))
                    )
                    extraction["_updated_at"] = normalize_timestamp(
                        str(extraction.get("updated_at"))
                    )
            expected = sorted(value for value in element_ids if value)
            if expected:
                owned = int(connection.execute(
                    "SELECT COUNT(*) AS c FROM source_elements "
                    "WHERE source_id=%s AND id=ANY(%s)",
                    (source_id, expected),
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
        with self.database.write() as connection:
            authority = connection.execute(
                "SELECT 1 FROM kg_build_jobs j JOIN notebooks n "
                "ON n.id=j.notebook_id WHERE j.id=%s AND j.notebook_id=%s "
                "AND j.mode='rebuild' AND j.status='running' "
                "AND COALESCE(n.indexing_pipeline,'')=%s "
                "AND n.indexing_pipeline_version=%s "
                "AND n.indexing_pipeline_generation=%s "
                "AND n.indexing_pipeline_job_id=%s FOR UPDATE OF j,n",
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
            header = connection.execute(
                "SELECT * FROM indexing_pipeline_stages WHERE job_id=%s "
                "AND notebook_id=%s AND pipeline_id=%s AND pipeline_version=%s "
                "AND pipeline_generation=%s FOR UPDATE",
                (
                    job_id, notebook_id, pipeline_id, pipeline_version,
                    pipeline_generation,
                ),
            ).fetchone()
            if header is None:
                return False
            snapshot_rows = json_value(header["source_snapshot"], [])
            if not isinstance(snapshot_rows, list) or any(
                not isinstance(value, dict) or type(value.get("id")) is not str
                for value in snapshot_rows
            ):
                return False
            snapshot = [str(value["id"]) for value in snapshot_rows]
            if len(snapshot) != len(set(snapshot)):
                return False
            current_snapshot = self._source_snapshot(
                connection, notebook_id, lock=True
            )
            staged = connection.execute(
                "SELECT source_id,status,payload "
                "FROM indexing_pipeline_stage_sources WHERE job_id=%s "
                "ORDER BY source_id COLLATE \"C\" FOR UPDATE",
                (job_id,),
            ).fetchall()
            if (
                current_snapshot != snapshot_rows
                or [str(row["source_id"]) for row in staged] != snapshot
            ):
                return False
            payloads: list[tuple[str, dict]] = []
            kg_modes: set[str] = set()
            for row in staged:
                payload = json_value(row["payload"], {})
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
            self._validate_stage_payloads(connection, payloads)

            if snapshot:
                connection.execute(
                    "DELETE FROM chunks WHERE source_id=ANY(%s)", (snapshot,)
                )
            chunk_rows: list[tuple] = []
            chunk_element_rows: list[tuple] = []
            chunk_vectors: list[dict] = []
            for source_id, payload in payloads:
                chunk_payload = payload["chunks"]
                created = chunk_payload["_created_at"]
                for row in chunk_payload["rows"]:
                    if not isinstance(row, dict) or str(row.get("source_id")) != source_id:
                        raise ValueError("invalid staged chunk source")
                    chunk_id = str(row["id"])
                    element_ids = list(row.get("element_ids") or [])
                    chunk_rows.append(
                        (
                            chunk_id, notebook_id, source_id, str(row["text"]),
                            str(row.get("section_path") or ""),
                            jsonb(element_ids), created,
                        )
                    )
                    chunk_element_rows.extend(
                        (notebook_id, str(element_id), chunk_id)
                        for element_id in element_ids
                    )
                chunk_vectors.extend(chunk_payload.get("vectors") or [])
                connection.execute(
                    "UPDATE sources SET chunked_at=%s WHERE id=%s AND notebook_id=%s",
                    (created, source_id, notebook_id),
                )
            execute_many(
                connection,
                "INSERT INTO chunks "
                "(id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                chunk_rows,
            )
            execute_many(
                connection,
                "INSERT INTO chunk_elements (notebook_id,element_id,chunk_id) "
                "VALUES (%s,%s,%s)",
                chunk_element_rows,
            )
            encoded_chunk_vectors: list[tuple] = []
            for row in chunk_vectors:
                encoded_chunk_vectors.append(
                    (
                        row["id"], notebook_id, row["_encoded"],
                        row["_created_at"],
                    )
                )
            execute_many(
                connection,
                "INSERT INTO chunk_embeddings "
                "(chunk_id,notebook_id,vector,created_at) VALUES (%s,%s,%s,%s)",
                encoded_chunk_vectors,
            )

            # Chunk publication invalidates every notebook-derived KG product
            # even when the selected pipeline preserves base KG rows.  Derived
            # mentions/checkpoints can otherwise retain links to deleted chunks.
            self._clear_notebook_derived_kg(connection, notebook_id)
            if kg_modes == {"replace"}:
                for source_id in snapshot:
                    self._delete_source_kg(connection, notebook_id, source_id)
                object_vectors: list[dict] = []
                relation_vectors: list[dict] = []
                for source_id, payload in payloads:
                    kg = payload["kg"]
                    execute_many(
                        connection,
                        "INSERT INTO knowledge_objects "
                        "(id,notebook_id,object_type,status,owner,payload,evidence,"
                        "source_candidate_id,source_id,created_at,updated_at) "
                        "VALUES (%s,%s,%s,%s,'',%s,%s,NULL,%s,%s,%s)",
                        [
                            (
                                row["id"], notebook_id, row["object_type"],
                                row["status"], jsonb(row["payload"]),
                                jsonb(row["evidence"]), source_id,
                                row["_created_at"], row["_updated_at"],
                            )
                            for row in kg.get("objects") or []
                        ],
                    )
                    execute_many(
                        connection,
                        "INSERT INTO knowledge_object_sources "
                        "(object_id,source_id,notebook_id) VALUES (%s,%s,%s)",
                        [
                            (row["object_id"], row["source_id"], notebook_id)
                            for row in kg.get("object_sources") or []
                        ],
                    )
                    execute_many(
                        connection,
                        "INSERT INTO knowledge_relations "
                        "(id,notebook_id,source_id,source_object_id,"
                        "target_object_id,edge_type,evidence,created_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        [
                            (
                                row["id"], notebook_id, source_id,
                                row["source_object_id"], row["target_object_id"],
                                row["edge_type"], jsonb(row["evidence"]),
                                row["_created_at"],
                            )
                            for row in kg.get("relations") or []
                        ],
                    )
                    execute_many(
                        connection,
                        "INSERT INTO knowledge_source_facts "
                        "(id,notebook_id,source_id,source_generation,local_object_id,"
                        "global_object_id,object_type,payload,evidence,projection_version,"
                        "projection_origin,created_at,updated_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1,'live',%s,%s)",
                        [
                            (
                                row["id"], notebook_id, source_id,
                                row["source_generation"], row["local_object_id"],
                                row["global_object_id"], row["object_type"],
                                jsonb(row["payload"]), jsonb(row["evidence"]),
                                row["_created_at"], row["_updated_at"],
                            )
                            for row in kg.get("facts") or []
                        ],
                    )
                    execute_many(
                        connection,
                        "INSERT INTO knowledge_source_fact_elements "
                        "(fact_id,notebook_id,source_id,source_generation,"
                        "element_id,created_at) VALUES (%s,%s,%s,%s,%s,%s)",
                        [
                            (
                                row["fact_id"], notebook_id, source_id,
                                row["source_generation"], row["element_id"],
                                row["_created_at"],
                            )
                            for row in kg.get("fact_elements") or []
                        ],
                    )
                    object_vectors.extend(kg.get("object_vectors") or [])
                    relation_vectors.extend(kg.get("relation_vectors") or [])
                    extraction = kg.get("extraction")
                    if isinstance(extraction, dict):
                        connection.execute(
                            "INSERT INTO extraction_runs "
                            "(id,notebook_id,source_id,run_type,status,error_message,"
                            "indexing_pipeline_id,indexing_pipeline_version,created_at,updated_at) "
                            "VALUES (%s,%s,%s,'kg','completed',%s,%s,%s,%s,%s)",
                            (
                                extraction["id"], notebook_id, source_id,
                                extraction.get("error_message", ""), pipeline_id,
                                pipeline_version,
                                extraction["_created_at"], extraction["_updated_at"],
                            ),
                        )
                for table, id_column, vectors in (
                    ("knowledge_embeddings", "object_id", object_vectors),
                    ("relation_embeddings", "relation_id", relation_vectors),
                ):
                    dimension = None
                    encoded_rows: list[tuple] = []
                    for row in vectors:
                        encoded_rows.append(
                            (
                                row["id"], notebook_id, row["_encoded"],
                                row["_created_at"],
                            )
                        )
                    execute_many(
                        connection,
                        f"INSERT INTO {table} "
                        f"({id_column},notebook_id,vector,created_at) "
                        "VALUES (%s,%s,%s,%s)",
                        encoded_rows,
                    )
            cleared = connection.execute(
                "UPDATE notebooks SET indexing_pipeline_job_id='',updated_at=%s "
                "WHERE id=%s AND COALESCE(indexing_pipeline,'')=%s "
                "AND indexing_pipeline_version=%s "
                "AND indexing_pipeline_generation=%s "
                "AND indexing_pipeline_job_id=%s",
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
            connection.execute(
                "INSERT INTO unified_kg_state "
                "(notebook_id,dirty,kg_mutation_seq,cluster_mutation_seq,"
                "cluster_input_version,community_seq,canonical_rel_seq,mention_seq,"
                "updated_at,indexing_pipeline_id,indexing_pipeline_version) "
                "VALUES (%s,1,1,1,'',-1,-1,-1,%s,%s,%s) "
                "ON CONFLICT(notebook_id) DO UPDATE SET "
                "dirty=1,kg_mutation_seq=unified_kg_state.kg_mutation_seq+1,"
                "cluster_mutation_seq=unified_kg_state.cluster_mutation_seq+1,"
                "cluster_input_version='',community_seq=-1,canonical_rel_seq=-1,"
                "mention_seq=-1,"
                "updated_at=EXCLUDED.updated_at,"
                "indexing_pipeline_id=EXCLUDED.indexing_pipeline_id,"
                "indexing_pipeline_version=EXCLUDED.indexing_pipeline_version",
                (notebook_id, now, pipeline_id, pipeline_version),
            )
            finished = connection.execute(
                "UPDATE kg_build_jobs SET status='succeeded',stage='finished',"
                "error_code='',error_message='',updated_at=%s,finished_at=%s "
                "WHERE id=%s AND notebook_id=%s AND mode='rebuild' "
                "AND status='running'",
                (now, now, job_id, notebook_id),
            )
            if finished.rowcount != 1:
                raise RuntimeError("indexing pipeline durable job changed")
            connection.execute(
                "DELETE FROM indexing_pipeline_stages WHERE job_id=%s", (job_id,)
            )
        return True

    def fail_submission(self, job_id: str) -> bool:
        return self.finish(
            job_id,
            "failed",
            error_code="job_submission_failed",
            error_message="知识图谱分析任务未能启动，请稍后重试。",
        )


__all__ = ["KgBuildAlreadyRunning", "KgBuildJobStore"]
