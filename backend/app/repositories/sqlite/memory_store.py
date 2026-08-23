"""Owner-scoped persistence for manually confirmed and agent-proposed Memory."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from typing import Any, Mapping, Sequence

from app.models.memory import MemoryRevision, MemoryWrite
from app.models.identity import AgentProfile, AgentTokenSummary
from app.models.memory import (
    MemoryNotebookOption,
    MemoryRecord,
    PaginatedMemories,
)
from app.core.json_safety import strict_json_dumps
from app.repositories.sqlite.access_sql import (
    GRANT_PROBE_SQL,
    MEMBER_PROBE_SQL,
    NOTEBOOK_READ_SQL,
    grant_probe_params,
    read_access_clause,
    read_access_exists_clause,
    read_access_params,
)
from app.repositories.sqlite.database import SqliteDatabase
from app.domain.vector_index import encode_vector


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
    def _profile(row: sqlite3.Row) -> AgentProfile:
        return AgentProfile(
            id=row["id"],
            owner_id=row["owner_id"],
            name=row["name"],
            description=row["description"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _token(row: sqlite3.Row, notebook_ids: Sequence[str]) -> AgentTokenSummary:
        return AgentTokenSummary(
            id=row["id"],
            agent_profile_id=row["agent_profile_id"],
            profile_name=row["profile_name"],
            scopes=_json_list(row["scopes_json"]),
            default_notebook_id=row["default_notebook_id"],
            notebook_ids=list(notebook_ids),
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
            last_used_at=row["last_used_at"],
            created_at=row["created_at"],
        )

    def create_agent_profile(
        self, owner_id: str, name: str, description: str
    ) -> AgentProfile:
        now = self.now()
        profile_id = self.new_id("agent")
        with self.database.write() as db:
            db.execute(
                "INSERT INTO agent_profiles "
                "(id,owner_id,name,description,status,created_at,updated_at) "
                "VALUES (?,?,?,?,'active',?,?)",
                (profile_id, owner_id, name, description, now, now),
            )
            row = db.execute(
                "SELECT * FROM agent_profiles WHERE id=?", (profile_id,)
            ).fetchone()
        return self._profile(row)

    def list_agent_profiles(
        self, owner_id: str, offset: int = 0, limit: int = 100
    ) -> list[AgentProfile]:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM agent_profiles WHERE owner_id=? "
                "ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?",
                (owner_id, limit, offset),
            ).fetchall()
        return [self._profile(row) for row in rows]

    def update_agent_profile(
        self, profile_id: str, owner_id: str, fields: Mapping[str, Any]
    ) -> AgentProfile:
        assignments = [f"{key}=?" for key in fields]
        values = list(fields.values())
        assignments.append("updated_at=?")
        values.append(self.now())
        with self.database.write() as db:
            cursor = db.execute(
                f"UPDATE agent_profiles SET {','.join(assignments)} "
                "WHERE id=? AND owner_id=?",
                (*values, profile_id, owner_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(profile_id)
            row = db.execute(
                "SELECT * FROM agent_profiles WHERE id=? AND owner_id=?",
                (profile_id, owner_id),
            ).fetchone()
        return self._profile(row)

    @staticmethod
    def _token_notebooks_on(db: sqlite3.Connection, token_id: str) -> list[str]:
        return [
            str(row["notebook_id"])
            for row in db.execute(
                "SELECT notebook_id FROM agent_token_notebooks "
                "WHERE token_id=? ORDER BY notebook_id",
                (token_id,),
            ).fetchall()
        ]

    def create_agent_token(
        self,
        token_id: str,
        owner_id: str,
        agent_profile_id: str,
        token_hash: str,
        scopes: Sequence[str],
        default_notebook_id: str,
        notebook_ids: Sequence[str],
        expires_at: str | None,
    ) -> AgentTokenSummary:
        now = self.now()
        with self.database.write() as db:
            profile = db.execute(
                "SELECT name FROM agent_profiles "
                "WHERE id=? AND owner_id=? AND status='active'",
                (agent_profile_id, owner_id),
            ).fetchone()
            if profile is None:
                raise KeyError(agent_profile_id)
            db.execute(
                "INSERT INTO agent_access_tokens "
                "(id,agent_profile_id,token_hash,scopes_json,default_notebook_id,"
                "expires_at,created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    token_id,
                    agent_profile_id,
                    token_hash,
                    json.dumps(list(scopes), ensure_ascii=False),
                    default_notebook_id,
                    expires_at,
                    now,
                ),
            )
            db.executemany(
                "INSERT INTO agent_token_notebooks (token_id,notebook_id) VALUES (?,?)",
                [(token_id, notebook_id) for notebook_id in notebook_ids],
            )
            row = db.execute(
                "SELECT t.*,p.name AS profile_name FROM agent_access_tokens t "
                "JOIN agent_profiles p ON p.id=t.agent_profile_id WHERE t.id=?",
                (token_id,),
            ).fetchone()
        return self._token(row, notebook_ids)

    def list_agent_tokens(
        self, owner_id: str, offset: int = 0, limit: int = 100
    ) -> list[AgentTokenSummary]:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT t.*,p.name AS profile_name FROM agent_access_tokens t "
                "JOIN agent_profiles p ON p.id=t.agent_profile_id "
                "WHERE p.owner_id=? ORDER BY t.created_at DESC,t.id DESC "
                "LIMIT ? OFFSET ?",
                (owner_id, limit, offset),
            ).fetchall()
            return [
                self._token(row, self._token_notebooks_on(db, row["id"]))
                for row in rows
            ]

    def revoke_agent_token(
        self, token_id: str, owner_id: str
    ) -> AgentTokenSummary:
        now = self.now()
        with self.database.write() as db:
            cursor = db.execute(
                "UPDATE agent_access_tokens SET revoked_at=COALESCE(revoked_at,?) "
                "WHERE id=? AND EXISTS (SELECT 1 FROM agent_profiles p "
                "WHERE p.id=agent_access_tokens.agent_profile_id AND p.owner_id=?)",
                (now, token_id, owner_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(token_id)
            row = db.execute(
                "SELECT t.*,p.name AS profile_name FROM agent_access_tokens t "
                "JOIN agent_profiles p ON p.id=t.agent_profile_id WHERE t.id=?",
                (token_id,),
            ).fetchone()
            notebooks = self._token_notebooks_on(db, token_id)
        return self._token(row, notebooks)

    def agent_token_auth_row(self, token_id: str) -> dict[str, Any] | None:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT t.*,p.owner_id,p.name AS profile_name,p.status AS profile_status "
                "FROM agent_access_tokens t JOIN agent_profiles p "
                "ON p.id=t.agent_profile_id WHERE t.id=?",
                (token_id,),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["notebook_ids"] = self._token_notebooks_on(db, token_id)
        return result

    def touch_agent_token(
        self, token_id: str, used_at: str, touch_before: str
    ) -> None:
        with self.database.write() as db:
            db.execute(
                "UPDATE agent_access_tokens SET last_used_at=? WHERE id=? "
                "AND revoked_at IS NULL AND "
                "(last_used_at IS NULL OR last_used_at<=?)",
                (used_at, token_id, touch_before),
            )

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
        """读权谓词。定义点在 `access_sql`,这里只是保留既有调用形状的薄封装。"""
        return read_access_exists_clause(alias)

    def insert_memory(self, write: MemoryWrite) -> MemoryRecord:
        with self.database.write() as db:
            item, _created = self._insert_memory_on(db, write)
        return item

    def notebook_content_overview(
        self, user_id: str, notebook_id: str, limit: int = 3
    ) -> dict[str, Any]:
        bounded_limit = max(1, min(3, int(limit)))
        with self.database.connect() as db:
            counts = db.execute(
                "SELECT COUNT(*) AS total,"
                "COALESCE(SUM(CASE WHEN status='confirmed' THEN 1 ELSE 0 END),0) AS confirmed,"
                "COALESCE(SUM(CASE WHEN status='candidate' THEN 1 ELSE 0 END),0) AS candidate "
                "FROM memory_items WHERE created_by=? AND notebook_id=?",
                (user_id, notebook_id),
            ).fetchone()
            rows = db.execute(
                "SELECT id,title,status,updated_at FROM memory_items "
                "WHERE created_by=? AND notebook_id=? "
                "AND status IN ('confirmed','candidate') "
                "ORDER BY updated_at DESC,id DESC LIMIT ?",
                (user_id, notebook_id, bounded_limit),
            ).fetchall()
        return {
            "total": int(counts["total"]),
            "confirmed": int(counts["confirmed"]),
            "candidate": int(counts["candidate"]),
            "recent": [dict(row) for row in rows],
        }

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
                    strict_json_dumps(list(write.tags), field="tags"),
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
                strict_json_dumps(
                    dict(write.provenance or {}), field="memory provenance"
                ),
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
        with self.database.write() as db:
            db.execute("BEGIN IMMEDIATE")
            access = db.execute(
                NOTEBOOK_READ_SQL,
                (write.notebook_id, *read_access_params(write.created_by)),
            ).fetchone()
            if access is None:
                raise PermissionError(write.notebook_id)
            agent_profile = None
            if write.agent_profile_id:
                profile = db.execute(
                    "SELECT id,name FROM agent_profiles "
                    "WHERE id=? AND owner_id=? AND status='active'",
                    (write.agent_profile_id, write.created_by),
                ).fetchone()
                if profile is None:
                    raise PermissionError(write.agent_profile_id)
                agent_profile = {"id": profile["id"], "name": profile["name"]}
            provenance = dict(write.provenance or {})
            submitted_refs = provenance.get("evidence_refs")
            provenance["agent_profile"] = agent_profile
            provenance["evidence_refs"] = [
                self._validate_evidence_ref_on(
                    db,
                    reference,
                    index=index,
                    notebook_id=write.notebook_id,
                    user_id=write.created_by,
                )
                for index, reference in enumerate(
                    submitted_refs if isinstance(submitted_refs, list) else []
                )
            ]
            item, created = self._insert_memory_on(
                db, replace(write, provenance=provenance)
            )
            self._ensure_initial_revision_on(db, item, created, changed_by, reason)
        return item

    @staticmethod
    def _validation(
        normalized: dict[str, Any], *, trusted: bool, reason: str
    ) -> dict[str, Any]:
        return {
            **normalized,
            "trusted": trusted,
            "validation": {
                "status": "validated" if trusted else "invalid",
                "reason": reason,
            },
        }

    def _validate_evidence_ref_on(
        self,
        db: sqlite3.Connection,
        reference: Mapping[str, Any],
        *,
        index: int,
        notebook_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        source_id = str(reference.get("source_id") or "").strip()
        element_id = str(reference.get("element_id") or "").strip()
        knowledge_id = str(
            reference.get("knowledge_id") or reference.get("object_id") or ""
        ).strip()
        memory_id = str(reference.get("memory_id") or "").strip()
        base: dict[str, Any] = {"index": index}
        if source_id and element_id:
            normalized = {
                **base,
                "type": "source_element",
                "source_id": source_id,
                "element_id": element_id,
            }
            row = db.execute(
                "SELECT 1 FROM sources s JOIN source_elements e ON e.source_id=s.id "
                "WHERE s.id=? AND e.id=? AND s.notebook_id=?",
                (source_id, element_id, notebook_id),
            ).fetchone()
            return self._validation(
                normalized,
                trusted=row is not None,
                reason="live_source_element" if row else "missing_or_cross_notebook",
            )
        if source_id:
            normalized = {**base, "type": "source", "source_id": source_id}
            row = db.execute(
                "SELECT 1 FROM sources WHERE id=? AND notebook_id=?",
                (source_id, notebook_id),
            ).fetchone()
            return self._validation(
                normalized,
                trusted=row is not None,
                reason="live_source" if row else "missing_or_cross_notebook",
            )
        if knowledge_id:
            normalized = {
                **base,
                "type": "knowledge",
                "knowledge_id": knowledge_id,
            }
            row = db.execute(
                "SELECT 1 FROM knowledge_objects WHERE id=? AND notebook_id=? "
                "AND status!='deprecated'",
                (knowledge_id, notebook_id),
            ).fetchone()
            return self._validation(
                normalized,
                trusted=row is not None,
                reason="live_knowledge_object" if row else "missing_or_cross_notebook",
            )
        if memory_id:
            normalized = {**base, "type": "memory", "memory_id": memory_id}
            row = db.execute(
                "SELECT 1 FROM memory_items WHERE id=? AND notebook_id=? "
                "AND created_by=? AND status='confirmed'",
                (memory_id, notebook_id, user_id),
            ).fetchone()
            return self._validation(
                normalized,
                trusted=row is not None,
                reason="live_owner_memory" if row else "missing_or_cross_owner",
            )
        submitted_type = str(
            reference.get("type") or reference.get("kind") or "unsupported"
        ).strip()[:80]
        return self._validation(
            {**base, "type": submitted_type or "unsupported"},
            trusted=False,
            reason="unsupported_reference",
        )

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
                "SELECT a.question,a.payload,a.conversation_id FROM answers a "
                "JOIN notebooks nb ON nb.id=a.notebook_id "
                "WHERE a.id=? AND a.notebook_id=? AND " + read_access_clause(),
                (
                    write.source_answer_id,
                    write.notebook_id,
                    *read_access_params(write.created_by),
                ),
            ).fetchone()
            if row is None:
                raise KeyError(write.source_answer_id)
            row = self._answer_save_scope_locked_on(db, write, row)
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

    @staticmethod
    def _answer_save_scope_locked_on(
        db: sqlite3.Connection, write: MemoryWrite, row: object
    ):
        """Parity seam reached after SQLite's guarded access snapshot."""
        del db, write
        return row

    def memory_for_user(self, memory_id: str, user_id: str) -> MemoryRecord:
        with self.database.connect() as db:
            row = db.execute(
                f"SELECT {self._select_columns()} FROM memory_items m "
                "LEFT JOIN memory_provenance p ON p.memory_id=m.id "
                f"WHERE m.id=? AND m.created_by=? AND {self._read_access_clause()}",
                (memory_id, user_id, *read_access_params(user_id)),
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
                (notebook_id, user_id, *unique_ids, *read_access_params(user_id)),
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
            self.database.begin_guarded_write(db)
            row = self._lock_memory_aggregate_on(
                db, memory_id, expected_creator=user_id
            )
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
            promotion_state = str(row["promotion_state"])
            invalidates_proposal = bool(values) or target in {
                "rejected",
                "deprecated",
            }
            if invalidates_proposal and promotion_state == "proposed":
                supersede_reason = (
                    "superseded_by_memory_edit"
                    if values
                    else "superseded_by_memory_terminal_status"
                )
                self._supersede_active_promotion_on(
                    db,
                    memory_id,
                    changed_by,
                    now,
                    _json_object(row["payload_json"]),
                    reason=supersede_reason,
                )
                promotion_state = "none"
            assignments = [
                "title=?",
                "content_md=?",
                "tags_json=?",
                "status=?",
                "promotion_state=?",
                "updated_at=?",
            ]
            params: list[Any] = [
                title,
                content_md,
                json.dumps(tags, ensure_ascii=False),
                status,
                promotion_state,
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
                    "promotion_state": promotion_state,
                },
                changed_by,
                reason,
            )
        return self.memory_for_user(memory_id, user_id)

    def _lock_memory_aggregate_on(
        self,
        db: sqlite3.Connection,
        memory_id: str,
        *,
        expected_creator: str | None = None,
        expected_notebook: str | None = None,
        permission_error: bool = False,
    ):
        """SQLite parity for the canonical promotion aggregate guard.

        The caller owns ``BEGIN IMMEDIATE``, which serializes the same
        notebook/member/Memory/candidate decision across processes.
        """
        routing = db.execute(
            "SELECT notebook_id,created_by FROM memory_items WHERE id=?",
            (memory_id,),
        ).fetchone()
        if routing is None or (
            expected_creator is not None
            and routing["created_by"] != expected_creator
        ):
            raise KeyError(memory_id)
        notebook_id = str(routing["notebook_id"])
        creator_id = str(routing["created_by"])
        if expected_notebook is not None and notebook_id != expected_notebook:
            raise ValueError("promotion candidate notebook does not match Memory notebook")
        # ⚠ 三段式,刻意不合并成 access_sql.NOTEBOOK_READ_SQL 单条 EXISTS:owner 那一半
        # 要单独区分「notebook 不存在」(恒 KeyError)与「无读权」(按调用方选
        # PermissionError/KeyError),合并后两者都只剩「无行」这一个信号。PG 侧对应
        # 实现还在这几步上各挂 FOR SHARE 行锁,合并会一并丢掉。成员与授权边两半各自
        # 复用唯一定义点的探测常量(读权扩展时这里不会自动跟随,必须像本次这样手改
        # ——已登记在 access_sql 的消费者清单)。
        notebook = db.execute(
            "SELECT created_by FROM notebooks WHERE id=?", (notebook_id,)
        ).fetchone()
        if notebook is None:
            raise KeyError(memory_id)
        if notebook["created_by"] != creator_id:
            member = db.execute(
                MEMBER_PROBE_SQL, (notebook_id, creator_id)
            ).fetchone()
            if member is None:
                grant = db.execute(
                    GRANT_PROBE_SQL, grant_probe_params(notebook_id, creator_id)
                ).fetchone()
                if grant is None:
                    if permission_error:
                        raise PermissionError(memory_id)
                    raise KeyError(memory_id)
        row = db.execute(
            f"SELECT {self._select_columns()} FROM memory_items m "
            "LEFT JOIN memory_provenance p ON p.memory_id=m.id "
            "WHERE m.id=? AND m.notebook_id=? AND m.created_by=?",
            (memory_id, notebook_id, creator_id),
        ).fetchone()
        if row is None:
            raise KeyError(memory_id)
        if row["promotion_state"] == "proposed":
            provenance = _json_object(row["payload_json"])
            promotion = provenance.get("kg_promotion")
            proposal_id = (
                str(promotion.get("proposal_id") or "")
                if isinstance(promotion, dict)
                else ""
            )
            if proposal_id:
                self._lock_active_promotion_candidate_on(
                    db, proposal_id, memory_id
                )
        return row

    @staticmethod
    def _lock_active_promotion_candidate_on(
        db: sqlite3.Connection, proposal_id: str, memory_id: str
    ):
        return db.execute(
            "SELECT id FROM promotion_candidates WHERE id=? AND object_id=?",
            (proposal_id, memory_id),
        ).fetchone()

    def lock_promotion_memory_on(
        self,
        db: sqlite3.Connection,
        memory_id: str,
        candidate_notebook_id: str,
    ) -> MemoryRecord:
        self.database.begin_guarded_write(db)
        row = self._lock_memory_aggregate_on(
            db,
            memory_id,
            expected_notebook=candidate_notebook_id,
            permission_error=True,
        )
        return self._record(row)

    @staticmethod
    def _supersede_active_promotion_on(
        db: sqlite3.Connection,
        memory_id: str,
        changed_by: str,
        now: str,
        provenance: dict[str, Any],
        *,
        reason: str,
    ) -> None:
        """Reject one active pinned proposal inside the caller's Memory write."""
        promotion = provenance.get("kg_promotion")
        proposal_id = (
            str(promotion.get("proposal_id") or "")
            if isinstance(promotion, dict)
            else ""
        )
        if not proposal_id:
            raise ValueError("proposed Memory is missing its promotion snapshot")
        cursor = db.execute(
            "UPDATE promotion_candidates SET status='rejected',reason=?,"
            "reviewed_by=?,updated_at=? WHERE id=? AND object_id=? "
            "AND status IN ('proposed','under_review')",
            (reason, changed_by, now, proposal_id, memory_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("active Memory promotion could not be superseded")
        provenance["kg_promotion"] = {
            "state": "superseded",
            "superseded_at": now,
            "superseded_reason": reason,
        }
        db.execute(
            "UPDATE memory_provenance SET payload_json=? WHERE memory_id=?",
            (
                strict_json_dumps(provenance, field="memory provenance"),
                memory_id,
            ),
        )

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

    def propose_promotion_on(
        self,
        db: sqlite3.Connection,
        memory_id: str,
        user_id: str,
        proposal_id: str,
        candidates: Sequence[Mapping[str, Any]],
        evidence: Sequence[Mapping[str, Any]],
        expected_item: MemoryRecord,
        now: str,
    ) -> MemoryRecord:
        row = db.execute(
            f"SELECT {self._select_columns()} FROM memory_items m "
            "LEFT JOIN memory_provenance p ON p.memory_id=m.id "
            f"WHERE m.id=? AND m.created_by=? AND {self._read_access_clause()}",
            (memory_id, user_id, *read_access_params(user_id)),
        ).fetchone()
        if row is None:
            raise KeyError(memory_id)
        item = self._record(row)
        if item.status != "confirmed":
            raise ValueError("only confirmed Memory can be promoted")
        if item.promotion_state not in {"none", "proposed"}:
            raise ValueError(f"Memory promotion is already {item.promotion_state}")
        if (
            item.title != expected_item.title
            or item.content_md != expected_item.content_md
            or list(item.tags) != list(expected_item.tags)
            or item.status != expected_item.status
        ):
            raise ValueError("Memory changed before its promotion snapshot was pinned")

        revision_row = db.execute(
            "SELECT COALESCE(MAX(revision),0) AS revision FROM memory_revisions "
            "WHERE memory_id=?",
            (memory_id,),
        ).fetchone()
        source_revision = int(revision_row["revision"])
        if source_revision <= 0:
            raise ValueError("Memory source revision is missing")

        provenance = dict(item.provenance)
        snapshots = provenance.get("kg_promotion_snapshots")
        snapshot_history = dict(snapshots) if isinstance(snapshots, dict) else {}
        pinned_snapshot = {
            "source_revision": source_revision,
            "proposal_revision": source_revision + 1,
            "title": item.title,
            "content_md": item.content_md,
            "tags": list(item.tags),
            "status": item.status,
            "candidates": [dict(candidate) for candidate in candidates],
            "evidence": [dict(card) for card in evidence],
        }
        snapshot_history[proposal_id] = pinned_snapshot
        provenance["kg_promotion_snapshots"] = snapshot_history
        provenance["kg_promotion"] = {
            "proposal_id": proposal_id,
            "state": "proposed",
            "source_revision": source_revision,
            "base_object_ids": [],
        }
        db.execute(
            "UPDATE memory_provenance SET payload_json=? WHERE memory_id=?",
            (
                strict_json_dumps(provenance, field="memory provenance"),
                memory_id,
            ),
        )
        db.execute(
            "UPDATE memory_items SET promotion_state='proposed',updated_at=? "
            "WHERE id=? AND created_by=? AND status='confirmed'",
            (now, memory_id, user_id),
        )
        self._append_revision_on(
            db,
            memory_id,
            {
                "title": item.title,
                "content_md": item.content_md,
                "tags": item.tags,
                "status": item.status,
                "promotion_state": "proposed",
            },
            user_id,
            "kg_promotion_proposed",
        )
        return item.model_copy(
            update={
                "promotion_state": "proposed",
                "updated_at": now,
                "provenance": provenance,
            }
        )

    @staticmethod
    def pinned_promotion_snapshot(
        item: MemoryRecord, proposal_id: str, *, required: bool = True
    ) -> dict[str, Any]:
        snapshots = item.provenance.get("kg_promotion_snapshots")
        snapshot = snapshots.get(proposal_id) if isinstance(snapshots, dict) else None
        if not isinstance(snapshot, dict):
            if required:
                raise ValueError("Memory promotion snapshot is missing")
            return {}
        return dict(snapshot)

    def validate_pinned_promotion_on(
        self,
        db: sqlite3.Connection,
        item: MemoryRecord,
        proposal_id: str,
        snapshot: Mapping[str, Any],
    ) -> None:
        promotion = item.provenance.get("kg_promotion")
        if (
            item.status != "confirmed"
            or item.promotion_state != "proposed"
            or not isinstance(promotion, dict)
            or promotion.get("proposal_id") != proposal_id
            or promotion.get("state") != "proposed"
        ):
            raise ValueError("Memory promotion is no longer active")
        source_revision = int(snapshot.get("source_revision") or 0)
        proposal_revision = int(snapshot.get("proposal_revision") or 0)
        revision = db.execute(
            "SELECT revision,title,content_md,tags_json,status,promotion_state "
            "FROM memory_revisions WHERE memory_id=? AND revision=?",
            (item.id, source_revision),
        ).fetchone()
        latest = db.execute(
            "SELECT COALESCE(MAX(revision),0) AS revision FROM memory_revisions "
            "WHERE memory_id=?",
            (item.id,),
        ).fetchone()
        if (
            revision is None
            or proposal_revision != source_revision + 1
            or int(latest["revision"]) != proposal_revision
            or revision["title"] != snapshot.get("title")
            or revision["content_md"] != snapshot.get("content_md")
            or _json_list(revision["tags_json"]) != list(snapshot.get("tags") or [])
            or revision["status"] != "confirmed"
            or item.title != snapshot.get("title")
            or item.content_md != snapshot.get("content_md")
            or list(item.tags) != list(snapshot.get("tags") or [])
        ):
            raise ValueError("Memory revision no longer matches the pinned snapshot")

    def promotion_rows_on(
        self, db: sqlite3.Connection, memory_ids: Sequence[str]
    ) -> dict[str, MemoryRecord]:
        if not memory_ids:
            return {}
        placeholders = ",".join("?" for _ in memory_ids)
        rows = db.execute(
            f"SELECT {self._select_columns()} FROM memory_items m "
            "LEFT JOIN memory_provenance p ON p.memory_id=m.id "
            f"WHERE m.id IN ({placeholders})",
            list(memory_ids),
        ).fetchall()
        return {str(row["id"]): self._record(row) for row in rows}

    def promotion_data_on(
        self, db: sqlite3.Connection, memory_id: str
    ) -> tuple[MemoryRecord, list[dict[str, Any]], list[str]]:
        records = self.promotion_rows_on(db, [memory_id])
        item = records.get(memory_id)
        if item is None:
            raise KeyError(memory_id)
        promotion = item.provenance.get("kg_promotion")
        if not isinstance(promotion, dict):
            raise ValueError("Memory promotion payload is missing")
        raw_candidates = promotion.get("candidates")
        candidates = [
            dict(candidate)
            for candidate in raw_candidates
            if isinstance(candidate, dict)
        ] if isinstance(raw_candidates, list) else []
        raw_ids = promotion.get("base_object_ids")
        base_ids = [str(value) for value in raw_ids] if isinstance(raw_ids, list) else []
        return item, candidates, base_ids

    @staticmethod
    def validate_promotion_approval_access_on(
        db: sqlite3.Connection,
        memory_id: str,
        candidate_notebook_id: str,
    ) -> None:
        """Revalidate Memory scope and creator access inside approval's write txn.

        读权判定整条走唯一定义点的**列引用**形式(notebook 是已 join 的 `n`,用户是
        `m.created_by`),因而一个参数都不消费。此前这里只算 `is_member` 再在 Python
        里补一次 owner 比较——那是「读权」的一份手写复刻,群组授权扩展时它不会自动
        跟随,会让被授权者的晋升审批在写事务里被拒。
        """
        row = db.execute(
            "SELECT m.notebook_id,"
            + read_access_clause("n", user_ref="m.created_by")
            + " AS has_access FROM memory_items m "
            "JOIN notebooks n ON n.id=m.notebook_id WHERE m.id=?",
            (memory_id,),
        ).fetchone()
        if row is None:
            raise KeyError(memory_id)
        if row["notebook_id"] != candidate_notebook_id:
            raise ValueError("promotion candidate notebook does not match Memory notebook")
        if not bool(row["has_access"]):
            raise PermissionError(memory_id)

    def record_promotion_decision_on(
        self,
        db: sqlite3.Connection,
        memory_id: str,
        state: str,
        changed_by: str,
        now: str,
        *,
        base_object_ids: Sequence[str] = (),
        reason: str = "",
    ) -> MemoryRecord:
        item, candidates, _existing_ids = self.promotion_data_on(db, memory_id)
        provenance = dict(item.provenance)
        current = provenance.get("kg_promotion")
        promotion = dict(current) if isinstance(current, dict) else {}
        promotion.update(
            {
                "state": state,
                "candidates": candidates,
                "base_object_ids": list(base_object_ids),
            }
        )
        if reason:
            promotion["reason"] = reason
        else:
            promotion.pop("reason", None)
        provenance["kg_promotion"] = promotion
        db.execute(
            "UPDATE memory_provenance SET payload_json=? WHERE memory_id=?",
            (
                strict_json_dumps(provenance, field="memory provenance"),
                memory_id,
            ),
        )
        db.execute(
            "UPDATE memory_items SET promotion_state=?,updated_at=? "
            "WHERE id=? AND status='confirmed'",
            (state, now, memory_id),
        )
        self._append_revision_on(
            db,
            memory_id,
            {
                "title": item.title,
                "content_md": item.content_md,
                "tags": item.tags,
                "status": item.status,
                "promotion_state": state,
            },
            changed_by,
            f"kg_promotion_{state}",
        )
        return item.model_copy(
            update={
                "promotion_state": state,
                "updated_at": now,
                "provenance": provenance,
            }
        )

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
        params.extend([self.now(), memory_id, user_id, *read_access_params(user_id)])
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
        params.extend([memory_id, user_id, *read_access_params(user_id), *sorted(expected)])
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
                    (memory_id, user_id, *read_access_params(user_id)),
                ).fetchone()
                if exists is None:
                    raise KeyError(memory_id)
                raise ValueError(f"invalid memory transition: {exists['status']} -> {target}")
        return self.memory_for_user(memory_id, user_id)

    def delete_memory(self, memory_id: str, user_id: str) -> None:
        with self.database.write() as db:
            cursor = db.execute(
                "DELETE FROM memory_items WHERE id=? AND created_by=?",
                (memory_id, user_id),
            )
        if cursor.rowcount != 1:
            raise KeyError(memory_id)

    def delete_memory_if_unchanged(
        self, memory_id: str, user_id: str, expected_revision: "int | None"
    ) -> bool:
        """Atomic conditional delete for ``MemoryService.transfer``'s move.

        ``BEGIN IMMEDIATE`` is acquired before revision, status, promotion,
        ownership, and deletion are evaluated in one write transaction. This
        serializes the decision with writers in other SQLite processes.

        Keyed on the ``memory_revisions`` MAX(revision) for this memory, NOT
        ``updated_at``: ``updated_at`` was the first design tried here, but
        ``app.services.sqlite_repository._now`` truncates to whole seconds
        (``datetime.now().replace(microsecond=0)``) — two mutations inside
        the same wall-clock second (an entirely realistic gap between "copy
        committed" and "a concurrent edit lands") produce the byte-identical
        string, which would make this check silently treat an edited row as
        unchanged and delete it anyway (verified failing via this repo's own
        test suite before switching to revision). ``revision`` has no such
        collision: ``_mutate_with_revision``'s ``_append_revision_on`` inserts
        exactly one new, strictly-incrementing row every time
        ``update_with_revision``/``transition_with_revision`` runs (both
        content edits and status transitions — ``deprecate`` goes through
        ``transition_with_revision`` too), so any mutation since
        ``expected_revision`` was captured makes MAX(revision) strictly
        greater, unconditionally. ``status='confirmed'`` is redundant with
        that (any transition
        away from 'confirmed' also advances revision) but kept as an
        explicit, cheap belt-and-suspenders check — a move should never
        delete a row that isn't (still) confirmed, regardless of how that
        came to be true. ``expected_revision=None`` (the source's revision
        couldn't be captured — see caller) can never equal a real revision
        number, so the method safely returns ``False``. Returns whether the
        row was actually deleted (``False`` means the memory was edited or
        transitioned away from 'confirmed', or (round 8, see below) had a
        promotion proposed, since ``expected_revision`` was captured — the
        row is left fully intact; caller should surface this as
        ``copied_source_not_removed``, not retry the delete).

        PR review round 8 P1-B: also requires ``promotion_state NOT IN
        ('proposed')``. ``MemoryService.transfer`` already rejects a move
        outright when the LOOP-TOP snapshot's ``promotion_state`` is
        already ``'proposed'`` (its own pre-check, cheap and gives the
        clearer per-item message for that case — kept as-is, this method
        doesn't replace it). But a proposal landing AFTER that snapshot is
        read and BEFORE this DELETE runs used to slip through anyway, and
        NOT because the revision guard above was somehow blind to it in
        the way ``status`` alone would be — ``memory_store.
        propose_promotion_on`` calls ``_append_revision_on`` too, a
        genuine new ``memory_revisions`` row. The actual gap: the caller
        captures ``expected_revision`` via ``embedding_revision`` a few
        lines AFTER its own promotion-state pre-check — if the proposal
        lands in exactly that narrow window, ``embedding_revision``'s read
        observes the ALREADY-BUMPED revision, and THIS delete's revision
        check a moment later compares against that same, unmoved-since
        number: both sides agree, precisely because the very mutation this
        method needed to catch is what moved the number they now agree on.
        A revision-only check cannot distinguish "unchanged since
        expected_revision was captured" from "changed, but AFTER capture
        and by exactly the mutation this delete needs to reject". Rechecking
        ``promotion_state`` in the same guarded transaction closes that gap
        regardless of exactly when, relative to the revision capture, the
        proposal lands. A failed promotion-state check flows through the same
        ``False`` return / retain-and-report-``copied_source_not_removed``
        path as every other "changed since expected_revision" cause above
        — not a new result shape."""
        with self.database.write() as db:
            self.database.begin_guarded_write(db)
            locked = self._lock_memory_revision_on(db, memory_id)
            if locked is None or expected_revision is None:
                return False
            item, revision = locked
            if (
                item["created_by"] != user_id
                or item["status"] != "confirmed"
                or item["promotion_state"] == "proposed"
                or revision != int(expected_revision)
            ):
                return False
            cursor = db.execute(
                "DELETE FROM memory_items WHERE id=? AND created_by=?",
                (memory_id, user_id),
            )
        return cursor.rowcount == 1

    def bulk_delete_memories(self, user_id: str, memory_ids: Sequence[str]) -> int:
        unique = list(dict.fromkeys(str(m) for m in memory_ids if m))
        if not unique:
            return 0
        if len(unique) > 200:
            raise ValueError("memory_ids may contain at most 200 unique values")
        placeholders = ",".join("?" for _ in unique)
        with self.database.write() as db:
            rows = db.execute(
                f"SELECT id FROM memory_items WHERE created_by=? AND id IN ({placeholders})",
                (user_id, *unique),
            ).fetchall()
            ids = [r["id"] for r in rows]
            db.executemany(
                "DELETE FROM memory_items WHERE id=? AND created_by=?",
                [(i, user_id) for i in ids],
            )
        return len(ids)

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
        params: list[Any] = [user_id, *read_access_params(user_id)]
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
            aggregate = db.execute(
                "SELECT COUNT(*) AS total_count,"
                "COALESCE(SUM(CASE WHEN m.status='candidate' THEN 1 ELSE 0 END),0) "
                "AS pending_count FROM memory_items m WHERE m.created_by=? AND "
                f"{self._read_access_clause()}",
                (user_id, *read_access_params(user_id)),
            ).fetchone()
            option_rows = db.execute(
                "SELECT m.notebook_id,nb.name,COUNT(*) AS memory_count,"
                "COALESCE(SUM(CASE WHEN m.status='candidate' THEN 1 ELSE 0 END),0) "
                "AS pending_count FROM memory_items m "
                "JOIN notebooks nb ON nb.id=m.notebook_id "
                "WHERE m.created_by=? AND "
                f"{self._read_access_clause()} "
                "GROUP BY m.notebook_id,nb.name ORDER BY nb.name,m.notebook_id LIMIT 200",
                (user_id, *read_access_params(user_id)),
            ).fetchall()
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
            owner_total_count=int(aggregate["total_count"]),
            owner_pending_count=int(aggregate["pending_count"]),
            notebook_options=[
                MemoryNotebookOption(
                    notebook_id=row["notebook_id"],
                    name=row["name"],
                    memory_count=int(row["memory_count"]),
                    pending_count=int(row["pending_count"]),
                )
                for row in option_rows
            ],
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
            self.database.begin_guarded_write(db)
            locked = self._lock_memory_revision_on(db, memory_id)
            if locked is None:
                return False
            _, revision = locked
            if revision != int(expected_revision):
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

    @staticmethod
    def _lock_memory_revision_on(db: sqlite3.Connection, memory_id: str):
        item = db.execute(
            "SELECT id,created_by,status,promotion_state FROM memory_items WHERE id=?",
            (memory_id,),
        ).fetchone()
        if item is None:
            return None
        current = db.execute(
            "SELECT COALESCE(MAX(revision),0) AS revision "
            "FROM memory_revisions WHERE memory_id=?",
            (memory_id,),
        ).fetchone()
        return item, int(current["revision"])

    def mark_embedding_failed(
        self, memory_id: str, expected_revision: int, error: str
    ) -> bool:
        with self.database.write() as db:
            self.database.begin_guarded_write(db)
            locked = self._lock_memory_revision_on(db, memory_id)
            if locked is None:
                return False
            _, revision = locked
            if revision != int(expected_revision):
                return False
            cursor = db.execute(
                "UPDATE memory_items SET embedding_status='failed',embedding_error=? "
                "WHERE id=?",
                (error[:500], memory_id),
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
        phrase_queries: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        """Return a bounded lexical union embedding pool for one owner/notebook.

        The embedding side is deliberately capped and index-ordered.  It never
        turns an Ask into a whole-Memory scan or an embedding backfill.

        `phrase_queries` are additional INDEPENDENT probes OR-ed into the same
        MATCH expression — extra terms on one already-bounded query, not extra
        queries. The whole query is otherwise one mandatory FTS phrase, so a
        memory holding a user-quoted phrase but not the surrounding sentence
        would never become a candidate (codex #410 round-6 P2). The trigram
        tokenizer makes each quoted term a literal substring match, exactly as
        on the chunk path.
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
        probes = [clean_query]
        for phrase in phrase_queries:
            value = str(phrase).strip()
            if value and value not in probes:
                probes.append(value)
        match_expression = " OR ".join(
            '"' + probe.replace('"', '""') + '"' for probe in probes
        )
        placeholders = ",".join("?" for _ in allowed)
        common_params = (user_id, notebook_id, *allowed, *read_access_params(user_id))
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
                (*common_params, match_expression, lexical_limit),
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

    def create_copy_with_initial_revision(
        self,
        write: "MemoryWrite",
        source_memory_id: str,
        changed_by: str,
        reason: str,
        expected_source_revision: "int | None",
    ) -> MemoryRecord:
        """把一条已有 memory 复制成 write（新 id/notebook）：单事务建 4 表 + 拷向量。

        source_answer_id 必须为 None（避免 idx_memory_answer_once 撞键）；向量随拷
        零重嵌入，源无向量则新 item 保持 embedding_status='pending'（服务侧补嵌）。

        PR review round 7 P1-A（拷贝陈旧向量却标 ready）：源当前的
        ``memory_embeddings`` 行可能相对源当前的 ``memory_items`` 内容是陈旧的
        ——编辑一条 confirmed memory 的正文会把 ``embedding_status`` 翻回
        ``'pending'``（``_mutate_with_revision``，只要 ``values`` 非空），但从不
        触碰/删除旧的 ``memory_embeddings`` 行，那要等到后续的重嵌入任务调用
        ``replace_embedding`` 才会发生。若一次 transfer 恰好落在「编辑已提交、
        重嵌入还没跑完」这个窗口，无条件拷贝「此刻存在的那行向量」并标
        ready，拷的就是旧文本的向量、却配着新文本、且从此永久标着 ready——
        ``MemoryService.transfer`` 只在 ``copied.embedding_status != "ready"``
        时才补调度 ``_schedule_embed``，一旦被误标 ready，不会再有任何东西替
        它重新嵌入。

        因此只有源在**这同一个事务内**被重新确认「此刻确实是就绪状态」才把
        向量拷走并标 ready：``memory_items.embedding_status == 'ready'`` **且**
        源当前的 ``memory_revisions`` 最新 revision 等于调用方传入的
        ``expected_source_revision``（调用方——即 ``MemoryService.transfer``
        ——早已为收尾的原子删源捕获过这同一个 revision 号，此处原样复用同一
        判据，不发明第二套；``None`` 与任何真实 revision 都不相等，天然安
        全失败，与 ``delete_memory_if_unchanged`` 对 ``expected_revision=None``
        的既有约定一致）。仅有 ``embedding_status=='ready'`` 不够：若源在调用
        方捕获 revision 之后、这个事务真正运行之前，先后经历「并发编辑
        （ready→pending）」又「并发重嵌入完成（pending→ready，但对应的是编辑
        后的新内容）」，此刻的 ready 对应的是比 ``write.content_md``（调用方
        更早捕获的旧快照）更新的一次修订——revision 号比对能截住这第二类错
        配，仅查 embedding_status 截不住。两个条件有一个不满足，副本就不拷
        任何向量、留在 schema 默认的 ``'pending'``——与「源从未有过向量」那
        条既有分支（``test_create_copy_without_source_vector_stays_pending``）
        退化成完全相同的形状，服务侧的 ``_schedule_embed`` 兜底照常接管。
        """
        with self.database.write() as db:
            self.database.begin_guarded_write(db)
            source_guard = self._lock_copy_source_on(db, source_memory_id)
            item, created = self._insert_memory_on(db, write)
            if item.id != write.id:
                return item  # 幂等命中已有行（正常不会发生：copy 用全新 id）
            self._ensure_initial_revision_on(db, item, created, changed_by, reason)
            source_state = db.execute(
                "SELECT embedding_status,(SELECT COALESCE(MAX(revision),0) "
                "FROM memory_revisions WHERE memory_id=?) AS revision "
                "FROM memory_items WHERE id=?",
                (source_memory_id, source_memory_id),
            ).fetchone()
            source_is_current = (
                source_guard is not None
                and source_state is not None
                and source_state["embedding_status"] == "ready"
                and expected_source_revision is not None
                and int(source_state["revision"]) == int(expected_source_revision)
            )
            if source_is_current:
                vec = db.execute(
                    "SELECT model, dimension, vector FROM memory_embeddings WHERE memory_id=?",
                    (source_memory_id,),
                ).fetchone()
                if vec is not None:
                    db.execute(
                        "INSERT OR REPLACE INTO memory_embeddings "
                        "(memory_id,model,dimension,vector,updated_at) VALUES (?,?,?,?,?)",
                        (write.id, vec["model"], vec["dimension"], vec["vector"], self.now()),
                    )
                    db.execute(
                        "UPDATE memory_items SET embedding_status='ready',embedding_error='' "
                        "WHERE id=?",
                        (write.id,),
                    )
                    item = self._record(
                        db.execute(
                            f"SELECT {self._select_columns()} FROM memory_items m "
                            "LEFT JOIN memory_provenance p ON p.memory_id=m.id "
                            "WHERE m.id=? AND m.created_by=?",
                            (write.id, write.created_by),
                        ).fetchone()
                    )
        return item

    @staticmethod
    def _lock_copy_source_on(db: sqlite3.Connection, source_memory_id: str):
        return db.execute(
            "SELECT embedding_status FROM memory_items WHERE id=?",
            (source_memory_id,),
        ).fetchone()
