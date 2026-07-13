"""Owner-scoped persistence for manually confirmed and agent-proposed Memory."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from typing import Any, Mapping, Sequence

from app.models.memory import MemoryRevision, MemoryWrite
from app.models.schemas import MemoryRecord, PaginatedMemories
from app.repositories.sqlite.database import SqliteDatabase
from app.services.vector_index import encode_vector


def _json_object(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _json_list(raw: Any) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


class MemoryStore:
    def __init__(self, database: SqliteDatabase, *, new_id, now) -> None:
        self.database = database
        self.new_id = new_id
        self.now = now

    @staticmethod
    def _record(row: sqlite3.Row) -> MemoryRecord:
        keys = row.keys()
        return MemoryRecord(
            id=row["id"],
            notebook_id=row["notebook_id"],
            created_by=row["created_by"],
            agent_profile_id=row["agent_profile_id"],
            source_answer_id=row["source_answer_id"],
            origin=row["origin"],
            status=row["status"],
            promotion_state=row["promotion_state"],
            title=row["title"],
            content_md=row["content_md"],
            tags=_json_list(row["tags_json"]),
            confirmed_by=row["confirmed_by"],
            confirmed_at=row["confirmed_at"],
            embedding_status=row["embedding_status"],
            embedding_error=row["embedding_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            provenance=_json_object(row["payload_json"] if "payload_json" in keys else "{}"),
        )

    @staticmethod
    def _select_columns(alias: str = "m") -> str:
        return (
            f"{alias}.id,{alias}.notebook_id,{alias}.created_by,"
            f"{alias}.agent_profile_id,{alias}.source_answer_id,{alias}.origin,"
            f"{alias}.status,{alias}.promotion_state,{alias}.title,"
            f"{alias}.content_md,{alias}.tags_json,{alias}.confirmed_by,"
            f"{alias}.confirmed_at,{alias}.embedding_status,{alias}.embedding_error,"
            f"{alias}.created_at,{alias}.updated_at,p.payload_json"
        )

    @staticmethod
    def _read_access_clause(alias: str = "m") -> str:
        return (
            "EXISTS (SELECT 1 FROM notebooks access_nb "
            f"WHERE access_nb.id={alias}.notebook_id AND "
            "(access_nb.created_by=? OR EXISTS (SELECT 1 FROM notebook_members access_nm "
            "WHERE access_nm.notebook_id=access_nb.id AND access_nm.user_id=?)))"
        )

    def insert_memory(self, write: MemoryWrite) -> MemoryRecord:
        with self.database.write() as db:
            item, _created = self._insert_memory_on(db, write)
        return item

    def _insert_memory_on(
        self, db: sqlite3.Connection, write: MemoryWrite
    ) -> tuple[MemoryRecord, bool]:
        client_request_id = (write.provenance or {}).get("client_request_id")
        if write.origin == "external_agent" and client_request_id:
            existing = db.execute(
                f"SELECT {self._select_columns()} FROM memory_items m "
                "JOIN memory_provenance p ON p.memory_id=m.id "
                "WHERE m.created_by=? AND m.notebook_id=? "
                "AND m.origin='external_agent' "
                "AND m.agent_profile_id IS ? "
                "AND json_extract(p.payload_json,'$.client_request_id')=? "
                "ORDER BY m.created_at LIMIT 1",
                (
                    write.created_by,
                    write.notebook_id,
                    write.agent_profile_id,
                    client_request_id,
                ),
            ).fetchone()
            if existing is not None:
                return self._record(existing), False
        try:
            db.execute(
                "INSERT INTO memory_items "
                "(id,notebook_id,created_by,agent_profile_id,source_answer_id,origin,"
                "status,title,content_md,tags_json,confirmed_by,confirmed_at,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    write.id,
                    write.notebook_id,
                    write.created_by,
                    write.agent_profile_id,
                    write.source_answer_id,
                    write.origin,
                    write.status,
                    write.title,
                    write.content_md,
                    json.dumps(list(write.tags), ensure_ascii=False),
                    write.confirmed_by,
                    write.confirmed_at,
                    write.created_at,
                    write.updated_at,
                ),
            )
        except sqlite3.IntegrityError:
            if write.source_answer_id is None:
                raise
            existing = db.execute(
                f"SELECT {self._select_columns()} FROM memory_items m "
                "LEFT JOIN memory_provenance p ON p.memory_id=m.id "
                "WHERE m.created_by=? AND m.source_answer_id=?",
                (write.created_by, write.source_answer_id),
            ).fetchone()
            if existing is None:
                raise
            return self._record(existing), False
        db.execute(
            "INSERT INTO memory_provenance "
            "(id,memory_id,origin,payload_json,created_at) VALUES (?,?,?,?,?)",
            (
                self.new_id("memprov"),
                write.id,
                write.origin,
                json.dumps(dict(write.provenance or {}), ensure_ascii=False),
                write.created_at,
            ),
        )
        row = db.execute(
            f"SELECT {self._select_columns()} FROM memory_items m "
            "LEFT JOIN memory_provenance p ON p.memory_id=m.id "
            "WHERE m.id=? AND m.created_by=?",
            (write.id, write.created_by),
        ).fetchone()
        return self._record(row), True

    def _create_with_initial_revision(
        self, write: MemoryWrite, changed_by: str, reason: str
    ) -> MemoryRecord:
        with self.database.write() as db:
            item, created = self._insert_memory_on(db, write)
            self._ensure_initial_revision_on(db, item, created, changed_by, reason)
        return item

    def _ensure_initial_revision_on(
        self,
        db: sqlite3.Connection,
        item: MemoryRecord,
        created: bool,
        changed_by: str,
        reason: str,
    ) -> None:
        has_revision = db.execute(
            "SELECT 1 FROM memory_revisions WHERE memory_id=? LIMIT 1",
            (item.id,),
        ).fetchone()
        if created or has_revision is None:
            self._append_revision_on(
                db,
                item.id,
                {
                    "title": item.title,
                    "content_md": item.content_md,
                    "tags": item.tags,
                    "status": item.status,
                    "promotion_state": item.promotion_state,
                },
                changed_by,
                reason,
            )

    def create_candidate_with_initial_revision(
        self, write: MemoryWrite, changed_by: str, reason: str
    ) -> MemoryRecord:
        return self._create_with_initial_revision(write, changed_by, reason)

    def create_answer_with_initial_revision(
        self, write: MemoryWrite, changed_by: str, reason: str
    ) -> MemoryRecord:
        if not write.source_answer_id:
            raise KeyError("source_answer_id")
        with self.database.write() as db:
            # Lock writers before reading the answer so deletion cannot commit
            # between the trusted snapshot and the Memory/provenance insert.
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                f"SELECT {self._select_columns()} FROM memory_items m "
                "LEFT JOIN memory_provenance p ON p.memory_id=m.id "
                "WHERE m.created_by=? AND m.source_answer_id=?",
                (write.created_by, write.source_answer_id),
            ).fetchone()
            if existing is not None:
                item = self._record(existing)
                if item.notebook_id != write.notebook_id:
                    raise KeyError(write.source_answer_id)
                return item
            row = db.execute(
                "SELECT question,payload,conversation_id FROM answers WHERE id=?",
                (write.source_answer_id,),
            ).fetchone()
            if row is None:
                raise KeyError(write.source_answer_id)
            payload = _json_object(row["payload"])
            provenance = {
                "answer_id": write.source_answer_id,
                "question": row["question"] or "",
                "answer": str(payload.get("answer") or payload.get("conclusion") or ""),
                "conversation_id": row["conversation_id"],
                "mode": str(payload.get("mode") or ""),
                "model": str(payload.get("llm_mode") or ""),
                "evidence_level": str(payload.get("evidence_level") or "inferred"),
                "anchors": payload.get("anchors") if isinstance(payload.get("anchors"), list) else [],
                "citations": payload.get("citations") if isinstance(payload.get("citations"), list) else [],
            }
            item, created = self._insert_memory_on(
                db, replace(write, provenance=provenance)
            )
            self._ensure_initial_revision_on(db, item, created, changed_by, reason)
        return item

    def memory_for_user(self, memory_id: str, user_id: str) -> MemoryRecord:
        with self.database.connect() as db:
            row = db.execute(
                f"SELECT {self._select_columns()} FROM memory_items m "
                "LEFT JOIN memory_provenance p ON p.memory_id=m.id "
                f"WHERE m.id=? AND m.created_by=? AND {self._read_access_clause()}",
                (memory_id, user_id, user_id, user_id),
            ).fetchone()
        if row is None:
            raise KeyError(memory_id)
        return self._record(row)

    def memory_by_answer(self, user_id: str, answer_id: str) -> MemoryRecord | None:
        with self.database.connect() as db:
            row = db.execute(
                f"SELECT {self._select_columns()} FROM memory_items m "
                "LEFT JOIN memory_provenance p ON p.memory_id=m.id "
                "WHERE m.created_by=? AND m.source_answer_id=?",
                (user_id, answer_id),
            ).fetchone()
        return self._record(row) if row is not None else None

    def answer_memory_links(
        self, notebook_id: str, user_id: str, answer_ids: Sequence[str]
    ) -> dict[str, str]:
        unique_ids = list(
            dict.fromkeys(str(answer_id) for answer_id in answer_ids if answer_id)
        )
        if not unique_ids:
            return {}
        if len(unique_ids) > 200:
            raise ValueError("answer_ids may contain at most 200 unique values")
        placeholders = ",".join("?" for _ in unique_ids)
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT m.source_answer_id,m.id FROM memory_items m "
                "WHERE m.notebook_id=? AND m.created_by=? "
                f"AND m.source_answer_id IN ({placeholders}) "
                f"AND {self._read_access_clause()}",
                (notebook_id, user_id, *unique_ids, user_id, user_id),
            ).fetchall()
        return {str(row["source_answer_id"]): str(row["id"]) for row in rows}

    def memory_by_agent_request(
        self,
        user_id: str,
        notebook_id: str,
        agent_profile_id: str | None,
        client_request_id: str,
    ) -> MemoryRecord | None:
        with self.database.connect() as db:
            row = db.execute(
                f"SELECT {self._select_columns()} FROM memory_items m "
                "JOIN memory_provenance p ON p.memory_id=m.id "
                "WHERE m.created_by=? AND m.notebook_id=? "
                "AND m.origin='external_agent' "
                "AND m.agent_profile_id IS ? "
                "AND json_extract(p.payload_json,'$.client_request_id')=? "
                "ORDER BY m.created_at LIMIT 1",
                (user_id, notebook_id, agent_profile_id, client_request_id),
            ).fetchone()
        return self._record(row) if row is not None else None

    def agent_profile_belongs_to(self, agent_profile_id: str, user_id: str) -> bool:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT 1 FROM agent_profiles WHERE id=? AND owner_id=? AND status='active'",
                (agent_profile_id, user_id),
            ).fetchone()
        return row is not None

    def append_revision(
        self, memory_id: str, snapshot: dict, changed_by: str, reason: str
    ) -> None:
        with self.database.write() as db:
            self._append_revision_on(db, memory_id, snapshot, changed_by, reason)

    def _append_revision_on(
        self,
        db: sqlite3.Connection,
        memory_id: str,
        snapshot: Mapping[str, Any],
        changed_by: str,
        reason: str,
    ) -> None:
        row = db.execute(
            "SELECT COALESCE(MAX(revision),0)+1 AS revision "
            "FROM memory_revisions WHERE memory_id=?",
            (memory_id,),
        ).fetchone()
        db.execute(
            "INSERT INTO memory_revisions "
            "(id,memory_id,revision,title,content_md,tags_json,status,promotion_state,"
            "changed_by,change_reason,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.new_id("memrev"),
                memory_id,
                int(row["revision"]),
                snapshot["title"],
                snapshot["content_md"],
                json.dumps(snapshot.get("tags", []), ensure_ascii=False),
                snapshot["status"],
                snapshot.get("promotion_state", "none"),
                changed_by,
                reason,
                self.now(),
            ),
        )

    def _mutate_with_revision(
        self,
        memory_id: str,
        user_id: str,
        *,
        fields: Mapping[str, Any],
        expected: set[str],
        target: str | None,
        changed_by: str,
        reason: str,
    ) -> MemoryRecord:
        allowed = {"title", "content_md", "tags"}
        values = {key: value for key, value in fields.items() if key in allowed}
        with self.database.write() as db:
            row = db.execute(
                "SELECT title,content_md,tags_json,status,promotion_state "
                "FROM memory_items m WHERE id=? AND created_by=? AND "
                f"{self._read_access_clause()}",
                (memory_id, user_id, user_id, user_id),
            ).fetchone()
            if row is None:
                raise KeyError(memory_id)
            if row["status"] not in expected:
                destination = target or row["status"]
                raise ValueError(
                    f"invalid memory transition: {row['status']} -> {destination}"
                )

            title = values.get("title", row["title"])
            content_md = values.get("content_md", row["content_md"])
            tags = (
                [str(item) for item in values["tags"]]
                if "tags" in values
                else _json_list(row["tags_json"])
            )
            status = target or row["status"]
            now = self.now()
            assignments = [
                "title=?",
                "content_md=?",
                "tags_json=?",
                "status=?",
                "updated_at=?",
            ]
            params: list[Any] = [
                title,
                content_md,
                json.dumps(tags, ensure_ascii=False),
                status,
                now,
            ]
            if values or target == "confirmed":
                assignments.extend(
                    ["embedding_status='pending'", "embedding_error=''"]
                )
            if target == "confirmed":
                assignments.extend(["confirmed_by=?", "confirmed_at=?"])
                params.extend([user_id, now])
            placeholders = ",".join("?" for _ in expected)
            params.extend([memory_id, user_id, *sorted(expected)])
            cursor = db.execute(
                f"UPDATE memory_items SET {','.join(assignments)} "
                f"WHERE id=? AND created_by=? AND status IN ({placeholders})",
                params,
            )
            if cursor.rowcount != 1:  # pragma: no cover - shared write lock guard
                raise ValueError(f"concurrent memory transition for {memory_id}")
            self._append_revision_on(
                db,
                memory_id,
                {
                    "title": title,
                    "content_md": content_md,
                    "tags": tags,
                    "status": status,
                    "promotion_state": row["promotion_state"],
                },
                changed_by,
                reason,
            )
        return self.memory_for_user(memory_id, user_id)

    def update_with_revision(
        self,
        memory_id: str,
        user_id: str,
        fields: Mapping[str, Any],
        *,
        expected: set[str],
        changed_by: str,
        reason: str,
    ) -> MemoryRecord:
        return self._mutate_with_revision(
            memory_id,
            user_id,
            fields=fields,
            expected=expected,
            target=None,
            changed_by=changed_by,
            reason=reason,
        )

    def transition_with_revision(
        self,
        memory_id: str,
        user_id: str,
        expected: set[str],
        target: str,
        *,
        fields: Mapping[str, Any] | None,
        changed_by: str,
        reason: str,
    ) -> MemoryRecord:
        return self._mutate_with_revision(
            memory_id,
            user_id,
            fields=fields or {},
            expected=expected,
            target=target,
            changed_by=changed_by,
            reason=reason,
        )

    def revisions_for_user(self, memory_id: str, user_id: str) -> list[MemoryRevision]:
        self.memory_for_user(memory_id, user_id)
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT revision,title,content_md,tags_json,status,promotion_state,"
                "changed_by,change_reason,created_at FROM memory_revisions "
                "WHERE memory_id=? ORDER BY revision",
                (memory_id,),
            ).fetchall()
        return [
            MemoryRevision(
                revision=int(row["revision"]),
                title=row["title"],
                content_md=row["content_md"],
                tags=_json_list(row["tags_json"]),
                status=row["status"],
                promotion_state=row["promotion_state"],
                changed_by=row["changed_by"],
                change_reason=row["change_reason"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def update_fields(
        self, memory_id: str, user_id: str, fields: Mapping[str, Any]
    ) -> MemoryRecord:
        allowed = {"title", "content_md", "tags"}
        values = {key: value for key, value in fields.items() if key in allowed}
        if not values:
            return self.memory_for_user(memory_id, user_id)
        assignments: list[str] = []
        params: list[Any] = []
        for key, value in values.items():
            column = "tags_json" if key == "tags" else key
            assignments.append(f"{column}=?")
            params.append(json.dumps(list(value), ensure_ascii=False) if key == "tags" else value)
        assignments.extend(["embedding_status='pending'", "embedding_error=''", "updated_at=?"])
        params.extend([self.now(), memory_id, user_id, user_id, user_id])
        with self.database.write() as db:
            cursor = db.execute(
                f"UPDATE memory_items SET {','.join(assignments)} "
                "WHERE id=? AND created_by=? AND "
                f"{self._read_access_clause('memory_items')}",
                params,
            )
            if cursor.rowcount != 1:
                raise KeyError(memory_id)
        return self.memory_for_user(memory_id, user_id)

    def transition(
        self,
        memory_id: str,
        user_id: str,
        expected: set[str],
        target: str,
    ) -> MemoryRecord:
        now = self.now()
        placeholders = ",".join("?" for _ in expected)
        confirmation = (
            ",confirmed_by=?,confirmed_at=?,embedding_status='pending',embedding_error=''"
            if target == "confirmed"
            else ""
        )
        params: list[Any] = [target, now]
        if target == "confirmed":
            params.extend([user_id, now])
        params.extend([memory_id, user_id, user_id, user_id, *sorted(expected)])
        with self.database.write() as db:
            cursor = db.execute(
                f"UPDATE memory_items SET status=?,updated_at=?{confirmation} "
                "WHERE id=? AND created_by=? AND "
                f"{self._read_access_clause('memory_items')} "
                f"AND status IN ({placeholders})",
                params,
            )
            if cursor.rowcount != 1:
                exists = db.execute(
                    "SELECT status FROM memory_items m "
                    "WHERE id=? AND created_by=? AND "
                    f"{self._read_access_clause()}",
                    (memory_id, user_id, user_id, user_id),
                ).fetchone()
                if exists is None:
                    raise KeyError(memory_id)
                raise ValueError(f"invalid memory transition: {exists['status']} -> {target}")
        return self.memory_for_user(memory_id, user_id)

    def list_memories(
        self,
        user_id: str,
        *,
        notebook_id: str | None,
        status: str | None,
        origin: str | None,
        query: str,
        offset: int,
        limit: int,
    ) -> PaginatedMemories:
        offset = max(0, int(offset))
        limit = max(1, min(200, int(limit)))
        joins = "LEFT JOIN memory_provenance p ON p.memory_id=m.id"
        clauses = ["m.created_by=?", self._read_access_clause()]
        params: list[Any] = [user_id, user_id, user_id]
        clean_query = (query or "").strip()
        if clean_query:
            joins += " JOIN memory_items_fts f ON f.rowid=m.rowid"
            clauses.append("memory_items_fts MATCH ?")
            params.append('"' + clean_query.replace('"', '""') + '"')
        if notebook_id:
            clauses.append("m.notebook_id=?")
            params.append(notebook_id)
        if status:
            clauses.append("m.status=?")
            params.append(status)
        if origin:
            clauses.append("m.origin=?")
            params.append(origin)
        where = " AND ".join(clauses)
        with self.database.connect() as db:
            total = db.execute(
                f"SELECT COUNT(*) AS c FROM memory_items m {joins} WHERE {where}",
                params,
            ).fetchone()["c"]
            rows = db.execute(
                f"SELECT {self._select_columns()} FROM memory_items m {joins} "
                f"WHERE {where} ORDER BY m.updated_at DESC,m.id LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return PaginatedMemories(
            items=[self._record(row) for row in rows],
            total_count=int(total),
            offset=offset,
            limit=limit,
        )

    def embedding_revision(
        self, memory_id: str, item: MemoryRecord
    ) -> int | None:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT (SELECT COALESCE(MAX(revision),0) FROM memory_revisions "
                "WHERE memory_id=m.id) AS revision FROM memory_items m "
                "WHERE m.id=? AND m.title=? AND m.content_md=? AND m.tags_json=? "
                "AND m.status=?",
                (
                    memory_id,
                    item.title,
                    item.content_md,
                    json.dumps(list(item.tags), ensure_ascii=False),
                    item.status,
                ),
            ).fetchone()
        return int(row["revision"]) if row is not None else None

    def replace_embedding(
        self, memory_id: str, expected_revision: int, model: str,
        vector: Sequence[float],
    ) -> bool:
        with self.database.write() as db:
            current = db.execute(
                "SELECT COALESCE(MAX(revision),0) AS revision "
                "FROM memory_revisions WHERE memory_id=?",
                (memory_id,),
            ).fetchone()
            if int(current["revision"]) != int(expected_revision):
                return False
            db.execute(
                "INSERT OR REPLACE INTO memory_embeddings "
                "(memory_id,model,dimension,vector,updated_at) VALUES (?,?,?,?,?)",
                (memory_id, model, len(vector), encode_vector(vector), self.now()),
            )
            db.execute(
                "UPDATE memory_items SET embedding_status='ready',embedding_error='' "
                "WHERE id=?",
                (memory_id,),
            )
        return True

    def mark_embedding_failed(
        self, memory_id: str, expected_revision: int, error: str
    ) -> bool:
        with self.database.write() as db:
            cursor = db.execute(
                "UPDATE memory_items SET embedding_status='failed',embedding_error=? "
                "WHERE id=? AND (SELECT COALESCE(MAX(revision),0) "
                "FROM memory_revisions WHERE memory_id=?)=?",
                (error[:500], memory_id, memory_id, int(expected_revision)),
            )
        return cursor.rowcount == 1

    def memory_retrieval_rows(
        self,
        user_id: str,
        notebook_id: str,
        statuses: Sequence[str],
        query: str,
        *,
        lexical_limit: int,
        vector_limit: int,
    ) -> list[dict[str, Any]]:
        """Return a bounded lexical union embedding pool for one owner/notebook.

        The embedding side is deliberately capped and index-ordered.  It never
        turns an Ask into a whole-Memory scan or an embedding backfill.
        """
        allowed = tuple(
            status for status in dict.fromkeys(str(item) for item in statuses)
            if status in {"candidate", "confirmed"}
        )
        clean_query = (query or "").strip()
        if not allowed or not clean_query:
            return []
        lexical_limit = max(1, min(int(lexical_limit), 200))
        vector_limit = max(1, min(int(vector_limit), 500))
        placeholders = ",".join("?" for _ in allowed)
        common_params = (user_id, notebook_id, *allowed, user_id, user_id)
        select = self._select_columns()
        with self.database.connect() as db:
            lexical_rows = db.execute(
                f"SELECT {select},me.vector AS retrieval_vector "
                "FROM memory_items m "
                "JOIN memory_items_fts f ON f.rowid=m.rowid "
                "LEFT JOIN memory_provenance p ON p.memory_id=m.id "
                "LEFT JOIN memory_embeddings me ON me.memory_id=m.id "
                "WHERE m.created_by=? AND m.notebook_id=? "
                f"AND m.status IN ({placeholders}) "
                f"AND {self._read_access_clause()} "
                "AND memory_items_fts MATCH ? "
                "ORDER BY bm25(memory_items_fts),m.updated_at DESC LIMIT ?",
                (*common_params, '"' + clean_query.replace('"', '""') + '"', lexical_limit),
            ).fetchall()
            vector_rows = db.execute(
                f"SELECT {select},me.vector AS retrieval_vector "
                "FROM memory_items m "
                "LEFT JOIN memory_provenance p ON p.memory_id=m.id "
                "JOIN memory_embeddings me ON me.memory_id=m.id "
                "WHERE m.created_by=? AND m.notebook_id=? "
                f"AND m.status IN ({placeholders}) "
                f"AND {self._read_access_clause()} "
                "ORDER BY m.updated_at DESC,m.id LIMIT ?",
                (*common_params, vector_limit),
            ).fetchall()
        rows: dict[str, dict[str, Any]] = {}
        for row in [*lexical_rows, *vector_rows]:
            rows.setdefault(
                str(row["id"]),
                {"record": self._record(row), "vector": row["retrieval_vector"]},
            )
        return list(rows.values())
