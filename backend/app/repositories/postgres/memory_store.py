"""Owner-scoped persistence for manually confirmed and agent-proposed Memory."""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, Callable, Mapping, Sequence

from psycopg import IsolationLevel
from psycopg.errors import DeadlockDetected, SerializationFailure

from app.models.memory import MemoryRevision, MemoryWrite
from app.models.identity import AgentProfile, AgentTokenAccess, AgentTokenSummary
from app.models.memory import (
    MemberExitSnapshot,
    MemoryNotebookOption,
    MemoryRecord,
    PaginatedMemories,
)
from app.core.json_safety import strict_json_dumps
from app.core.memory_inputs import MemoryExportTooLarge
from app.repositories.identity_errors import (
    AgentTokenAccessConflictError,
    AgentTokenInactiveError,
)
from app.repositories.postgres._store_utils import (
    execute_many,
    iso_timestamp,
    json_value,
    jsonb,
    normalize_timestamp,
)
from app.repositories.postgres.access_sql import (
    GRANT_PROBE_FOR_SHARE_SQL,
    MEMBER_PROBE_FOR_SHARE_SQL,
    grant_access_expr,
    grant_probe_params,
    read_access_clause,
    read_access_exists_clause,
    read_access_params,
)
from app.repositories.postgres import memory_sql
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.governance_store import GovernanceStore
from app.repositories.postgres.memory_sql import (
    memory_derived_object,
    memory_source_type_predicate,
)
from app.repositories.postgres.search import (
    MemoryCandidateScope,
    memory_candidate_ids,
    memory_match_count,
    memory_page_candidate_ids,
)
from app.domain.vector_index import encode_vector


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _json_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw]
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


# ``promotion_candidates.reason`` for a proposal withdrawn because its Memory
# was hard-deleted (single delete, bulk delete, or a member's exit purge).
MEMORY_DELETED_PROMOTION_REASON = "withdrawn_memory_deleted"


def _bulk_memory_ids(memory_ids: Sequence[str]) -> list[str]:
    """De-duplicated, bounded id list shared by bulk delete and its pre-read."""
    unique = list(dict.fromkeys(str(m) for m in memory_ids if m))
    if len(unique) > 200:
        raise ValueError("memory_ids may contain at most 200 unique values")
    return unique


def _strict_json_value(value: Any, *, field: str) -> Any:
    """Validate application-owned JSON and return a Jsonb-ready value."""
    return json.loads(strict_json_dumps(value, field=field))


# 「无主的 Memory 来源」:`source_type` 是 memory,而它指回的 Memory 不再是一条已确认的
# Memory —— `memory_id` 为 NULL/空(既有的笔记本拷贝清空了它,N-5)、指向的行已不存在
# (硬删残留)、或指向的行不是 `confirmed`。`orphan_memory_source_ids`、
# `has_orphan_memory_sources` 与 `orphan_memory_source_count_on` 共用这一段文本,清扫与
# 体检不可能各说各话。
#
# 形状是**可去相关**的单个 `NOT EXISTS`,没有 `OR`:NULL 的 `memory_id` 让 `m.id = s.memory_id`
# 恒不真,所以 NOT EXISTS 自然成立;空串由子查询里的 `m.id <> ''` 保证(即便存在 id 为空串的
# Memory 行,空串链接也仍是孤儿)。带 `OR memory_id IS NULL OR memory_id = ''` 的旧写法会
# 让规划器放弃反连接,在 id 随机的百万级 `sources` 上沿主键整表走一遍并逐个 Memory 来源
# 做探针(冷缓存 8–26 秒);现在的形状走 Hash Anti Join,见
# `test_memory_orphan_sweep_explain_pins`。
_ORPHAN_MEMORY_SOURCE_WHERE = (
    memory_sql.memory_source_type_predicate("s.source_type")
    + " AND NOT EXISTS (SELECT 1 FROM memory_items m WHERE m.id = s.memory_id "
    "AND m.status = 'confirmed' AND m.id <> '')"
)


class MemoryStore:
    def __init__(self, database: PostgresDatabase, *, new_id, now) -> None:
        self.database = database
        self.new_id = new_id
        self.now = now

    @staticmethod
    def _profile(row: Mapping[str, Any]) -> AgentProfile:
        return AgentProfile(
            id=row["id"],
            owner_id=row["owner_id"],
            name=row["name"],
            description=row["description"],
            status=row["status"],
            created_at=iso_timestamp(row["created_at"]),
            updated_at=iso_timestamp(row["updated_at"]),
        )

    @staticmethod
    def _token(row: Mapping[str, Any], notebook_ids: Sequence[str]) -> AgentTokenSummary:
        return AgentTokenSummary(
            id=row["id"],
            agent_profile_id=row["agent_profile_id"],
            profile_name=row["profile_name"],
            scopes=_json_list(row["scopes_json"]),
            default_notebook_id=row["default_notebook_id"],
            notebook_ids=list(notebook_ids),
            expires_at=iso_timestamp(row["expires_at"], empty="") or None,
            revoked_at=iso_timestamp(row["revoked_at"], empty="") or None,
            last_used_at=iso_timestamp(row["last_used_at"], empty="") or None,
            created_at=iso_timestamp(row["created_at"]),
            copyable=bool(row["token_plain"]) and row["revoked_at"] is None,
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
                "VALUES (%s,%s,%s,%s,'active',%s,%s)",
                (
                    profile_id,
                    owner_id,
                    name,
                    description,
                    normalize_timestamp(now),
                    normalize_timestamp(now),
                ),
            )
            row = db.execute(
                "SELECT * FROM agent_profiles WHERE id=%s", (profile_id,)
            ).fetchone()
        return self._profile(row)

    def list_agent_profiles(
        self, owner_id: str, offset: int = 0, limit: int = 100
    ) -> list[AgentProfile]:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM agent_profiles WHERE owner_id=%s "
                "ORDER BY updated_at DESC,id COLLATE \"C\" DESC LIMIT %s OFFSET %s",
                (owner_id, limit, offset),
            ).fetchall()
        return [self._profile(row) for row in rows]

    def update_agent_profile(
        self, profile_id: str, owner_id: str, fields: Mapping[str, Any]
    ) -> AgentProfile:
        if not set(fields) <= {"name", "description", "status"}:
            raise ValueError("unsupported agent profile field")
        assignments = [f"{key}=%s" for key in fields]
        values = list(fields.values())
        assignments.append("updated_at=%s")
        values.append(normalize_timestamp(self.now()))
        with self.database.write() as db:
            cursor = db.execute(
                f"UPDATE agent_profiles SET {','.join(assignments)} "
                "WHERE id=%s AND owner_id=%s",
                (*values, profile_id, owner_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(profile_id)
            row = db.execute(
                "SELECT * FROM agent_profiles WHERE id=%s AND owner_id=%s",
                (profile_id, owner_id),
            ).fetchone()
        return self._profile(row)

    @staticmethod
    def _token_notebooks_on(db: object, token_id: str) -> list[str]:
        return [
            str(row["notebook_id"])
            for row in db.execute(
                "SELECT notebook_id FROM agent_token_notebooks "
                "WHERE token_id=%s ORDER BY notebook_id COLLATE \"C\"",
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
        token_plain: str | None = None,
    ) -> AgentTokenSummary:
        now = self.now()
        with self.database.write() as db:
            profile = db.execute(
                "SELECT name FROM agent_profiles "
                "WHERE id=%s AND owner_id=%s AND status='active'",
                (agent_profile_id, owner_id),
            ).fetchone()
            if profile is None:
                raise KeyError(agent_profile_id)
            db.execute(
                "INSERT INTO agent_access_tokens "
                "(id,agent_profile_id,token_hash,scopes_json,default_notebook_id,"
                "expires_at,created_at,token_plain) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    token_id,
                    agent_profile_id,
                    token_hash,
                    jsonb(list(scopes)),
                    default_notebook_id,
                    normalize_timestamp(expires_at) if expires_at else None,
                    normalize_timestamp(now),
                    token_plain,
                ),
            )
            execute_many(
                db,
                "INSERT INTO agent_token_notebooks (token_id,notebook_id) VALUES (%s,%s)",
                [(token_id, notebook_id) for notebook_id in notebook_ids],
            )
            row = db.execute(
                "SELECT t.*,p.name AS profile_name FROM agent_access_tokens t "
                "JOIN agent_profiles p ON p.id=t.agent_profile_id WHERE t.id=%s",
                (token_id,),
            ).fetchone()
        return self._token(row, notebook_ids)

    def list_agent_tokens(
        self, owner_id: str, offset: int = 0, limit: int = 100
    ) -> list[AgentTokenSummary]:
        # 白名单与 token 行同一条语句读出:编辑器拿这份列表当 expected 前置条件,
        # 分开读可能拼出一份从未存在过的配置(见 agent_token_auth_row)。
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT t.*,p.name AS profile_name,"
                "COALESCE((SELECT array_agg(n.notebook_id ORDER BY n.notebook_id COLLATE \"C\") "
                "FROM agent_token_notebooks n WHERE n.token_id=t.id),ARRAY[]::text[]) "
                "AS list_notebook_ids "
                "FROM agent_access_tokens t "
                "JOIN agent_profiles p ON p.id=t.agent_profile_id "
                "WHERE p.owner_id=%s ORDER BY t.created_at DESC,t.id COLLATE \"C\" DESC "
                "LIMIT %s OFFSET %s",
                (owner_id, limit, offset),
            ).fetchall()
        return [
            self._token(row, [str(notebook_id) for notebook_id in row["list_notebook_ids"]])
            for row in rows
        ]

    def revoke_agent_token(
        self, token_id: str, owner_id: str
    ) -> AgentTokenSummary:
        now = self.now()
        with self.database.write() as db:
            cursor = db.execute(
                "UPDATE agent_access_tokens SET revoked_at=COALESCE(revoked_at,%s),"
                "token_plain=NULL "
                "WHERE id=%s AND EXISTS (SELECT 1 FROM agent_profiles p "
                "WHERE p.id=agent_access_tokens.agent_profile_id AND p.owner_id=%s)",
                (normalize_timestamp(now), token_id, owner_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(token_id)
            row = db.execute(
                "SELECT t.*,p.name AS profile_name FROM agent_access_tokens t "
                "JOIN agent_profiles p ON p.id=t.agent_profile_id WHERE t.id=%s",
                (token_id,),
            ).fetchone()
            notebooks = self._token_notebooks_on(db, token_id)
        return self._token(row, notebooks)

    def agent_token_secret(
        self, token_id: str, owner_id: str
    ) -> tuple[bool, str | None] | None:
        """``(revoked, token_plain)`` of one of ``owner_id``'s own tokens, or
        ``None`` when the token does not exist or belongs to someone else."""
        with self.database.connect() as db:
            row = db.execute(
                "SELECT t.revoked_at,t.token_plain FROM agent_access_tokens t "
                "JOIN agent_profiles p ON p.id=t.agent_profile_id "
                "WHERE t.id=%s AND p.owner_id=%s",
                (token_id, owner_id),
            ).fetchone()
        if row is None:
            return None
        return row["revoked_at"] is not None, row["token_plain"]

    def update_agent_token_access(
        self,
        token_id: str,
        owner_id: str,
        scopes: Sequence[str],
        default_notebook_id: str,
        notebook_ids: Sequence[str],
        expires_at: str | None,
        expected: AgentTokenAccess | None = None,
    ) -> AgentTokenSummary:
        # 一个写事务:``FOR UPDATE OF t`` 锁住 token 行,使下面的「读撤销/停用
        # 状态与 expected 前置条件」与「写新配置」之间不会被另一次并发撤销或
        # 修改插入(并发修改在行锁上排队,后到者读到的是先到者提交后的配置)。
        with self.database.write() as db:
            row = db.execute(
                "SELECT t.*,p.name AS profile_name,p.status AS profile_status "
                "FROM agent_access_tokens t "
                "JOIN agent_profiles p ON p.id=t.agent_profile_id "
                "WHERE t.id=%s AND p.owner_id=%s FOR UPDATE OF t",
                (token_id, owner_id),
            ).fetchone()
            if row is None:
                raise KeyError(token_id)
            if row["revoked_at"] is not None:
                raise AgentTokenInactiveError("revoked")
            if row["profile_status"] != "active":
                raise AgentTokenInactiveError("profile_disabled")
            if expected is not None and not expected.matches(
                self._token(row, self._token_notebooks_on(db, token_id))
            ):
                raise AgentTokenAccessConflictError(token_id)
            cursor = db.execute(
                "UPDATE agent_access_tokens SET scopes_json=%s,default_notebook_id=%s,"
                "expires_at=%s WHERE id=%s AND revoked_at IS NULL",
                (
                    jsonb(list(scopes)),
                    default_notebook_id,
                    normalize_timestamp(expires_at) if expires_at else None,
                    token_id,
                ),
            )
            if cursor.rowcount != 1:
                raise AgentTokenInactiveError("revoked")
            db.execute(
                "DELETE FROM agent_token_notebooks WHERE token_id=%s", (token_id,)
            )
            execute_many(
                db,
                "INSERT INTO agent_token_notebooks (token_id,notebook_id) VALUES (%s,%s)",
                [(token_id, notebook_id) for notebook_id in notebook_ids],
            )
            row = db.execute(
                "SELECT t.*,p.name AS profile_name FROM agent_access_tokens t "
                "JOIN agent_profiles p ON p.id=t.agent_profile_id WHERE t.id=%s",
                (token_id,),
            ).fetchone()
            notebooks = self._token_notebooks_on(db, token_id)
        return self._token(row, notebooks)

    def agent_token_auth_row(self, token_id: str) -> dict[str, Any] | None:
        # 一条语句读完整份访问配置:token 配置可原地修改(update_agent_token_access),
        # READ COMMITTED 下每条语句各取快照,分两次读会让鉴权拿到「旧 scopes + 新
        # 白名单」这种两份配置都没授权过的组合。
        with self.database.connect() as db:
            row = db.execute(
                "SELECT t.*,p.owner_id,p.name AS profile_name,p.status AS profile_status,"
                "COALESCE((SELECT array_agg(n.notebook_id ORDER BY n.notebook_id COLLATE \"C\") "
                "FROM agent_token_notebooks n WHERE n.token_id=t.id),ARRAY[]::text[]) "
                "AS auth_notebook_ids "
                "FROM agent_access_tokens t JOIN agent_profiles p "
                "ON p.id=t.agent_profile_id WHERE t.id=%s",
                (token_id,),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            # The plaintext never travels with the auth row (authentication
            # compares the hash only).
            result.pop("token_plain", None)
            result["scopes_json"] = json.dumps(
                json_value(result.get("scopes_json"), []),
                ensure_ascii=False,
                allow_nan=False,
            )
            for column in ("expires_at", "revoked_at", "last_used_at", "created_at"):
                value = result.get(column)
                result[column] = iso_timestamp(value, empty="") or None
            result["notebook_ids"] = [
                str(notebook_id) for notebook_id in result.pop("auth_notebook_ids")
            ]
        return result

    def touch_agent_token(
        self, token_id: str, used_at: str, touch_before: str
    ) -> None:
        with self.database.write() as db:
            db.execute(
                "UPDATE agent_access_tokens SET last_used_at=%s WHERE id=%s "
                "AND revoked_at IS NULL AND "
                "(last_used_at IS NULL OR last_used_at<=%s)",
                (normalize_timestamp(used_at), token_id, normalize_timestamp(touch_before)),
            )

    @staticmethod
    def _record(row: Mapping[str, Any]) -> MemoryRecord:
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
            confirmed_at=iso_timestamp(row["confirmed_at"], empty="") or None,
            embedding_status=row["embedding_status"],
            embedding_error=row["embedding_error"],
            created_at=iso_timestamp(row["created_at"]),
            updated_at=iso_timestamp(row["updated_at"]),
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
                "FROM memory_items WHERE created_by=%s AND notebook_id=%s",
                (user_id, notebook_id),
            ).fetchone()
            rows = db.execute(
                "SELECT id,title,status,updated_at FROM memory_items "
                "WHERE created_by=%s AND notebook_id=%s "
                "AND status IN ('confirmed','candidate') "
                "ORDER BY updated_at DESC,id COLLATE \"C\" DESC LIMIT %s",
                (user_id, notebook_id, bounded_limit),
            ).fetchall()
        return {
            "total": int(counts["total"]),
            "confirmed": int(counts["confirmed"]),
            "candidate": int(counts["candidate"]),
            "recent": [
                {**dict(row), "updated_at": iso_timestamp(row["updated_at"])}
                for row in rows
            ],
        }

    def _insert_memory_on(
        self, db: object, write: MemoryWrite
    ) -> tuple[MemoryRecord, bool]:
        client_request_id = (write.provenance or {}).get("client_request_id")
        idempotency_key = None
        if write.source_answer_id:
            idempotency_key = (
                f"silicon-notebook:memory-answer:{write.created_by}:"
                f"{write.source_answer_id}"
            )
        elif write.origin == "external_agent" and client_request_id:
            idempotency_key = (
                f"silicon-notebook:memory-agent:{write.created_by}:"
                f"{write.notebook_id}:{write.agent_profile_id or ''}:{client_request_id}"
            )
        if idempotency_key:
            db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (idempotency_key,),
            )
        if write.origin == "external_agent" and client_request_id:
            existing = db.execute(
                f"SELECT {self._select_columns()} FROM memory_items m "
                "JOIN memory_provenance p ON p.memory_id=m.id "
                "WHERE m.created_by=%s AND m.notebook_id=%s "
                "AND m.origin='external_agent' "
                "AND m.agent_profile_id IS NOT DISTINCT FROM %s "
                "AND p.payload_json->>'client_request_id'=%s "
                "ORDER BY m.created_at, m.id COLLATE \"C\" LIMIT 1",
                (
                    write.created_by,
                    write.notebook_id,
                    write.agent_profile_id,
                    client_request_id,
                ),
            ).fetchone()
            if existing is not None:
                return self._record(existing), False
        statement = (
            "INSERT INTO memory_items "
            "(id,notebook_id,created_by,agent_profile_id,source_answer_id,origin,"
            "status,title,content_md,tags_json,confirmed_by,confirmed_at,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        )
        if write.source_answer_id is not None:
            statement += (
                " ON CONFLICT (created_by,source_answer_id) "
                "WHERE source_answer_id IS NOT NULL DO NOTHING RETURNING id"
            )
        cursor = db.execute(
            statement,
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
                jsonb(_strict_json_value(list(write.tags), field="tags")),
                write.confirmed_by,
                normalize_timestamp(write.confirmed_at) if write.confirmed_at else None,
                normalize_timestamp(write.created_at),
                normalize_timestamp(write.updated_at),
            ),
        )
        if write.source_answer_id is not None and cursor.fetchone() is None:
            existing = db.execute(
                f"SELECT {self._select_columns()} FROM memory_items m "
                "LEFT JOIN memory_provenance p ON p.memory_id=m.id "
                "WHERE m.created_by=%s AND m.source_answer_id=%s",
                (write.created_by, write.source_answer_id),
            ).fetchone()
            if existing is None:
                raise RuntimeError("Memory answer idempotency conflict")
            return self._record(existing), False
        db.execute(
            "INSERT INTO memory_provenance "
            "(id,memory_id,origin,payload_json,created_at) VALUES (%s,%s,%s,%s,%s)",
            (
                self.new_id("memprov"),
                write.id,
                write.origin,
                jsonb(_strict_json_value(
                    dict(write.provenance or {}), field="memory provenance"
                )),
                normalize_timestamp(write.created_at),
            ),
        )
        row = db.execute(
            f"SELECT {self._select_columns()} FROM memory_items m "
            "LEFT JOIN memory_provenance p ON p.memory_id=m.id "
            "WHERE m.id=%s AND m.created_by=%s",
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
        db: object,
        item: MemoryRecord,
        created: bool,
        changed_by: str,
        reason: str,
    ) -> None:
        has_revision = db.execute(
            "SELECT 1 FROM memory_revisions WHERE memory_id=%s LIMIT 1",
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
            # ⚠ 三段式(owner / 成员 / 授权边),刻意不合并成 access_sql.NOTEBOOK_READ_SQL
            # 单条 EXISTS:每一步各自挂 FOR SHARE 锁住 notebooks / notebook_members /
            # notebook_grants 的行,EXISTS 子查询里的行拿不到这把锁。后两半复用唯一
            # 定义点的探测常量;读权谓词扩展时这里不会自动跟随,必须像本次这样手改
            # (已登记在 access_sql 的消费者清单)。
            notebook = db.execute(
                "SELECT created_by FROM notebooks WHERE id=%s FOR SHARE",
                (write.notebook_id,),
            ).fetchone()
            granted = None
            if notebook is not None and notebook["created_by"] != write.created_by:
                granted = db.execute(
                    MEMBER_PROBE_FOR_SHARE_SQL,
                    (write.notebook_id, write.created_by),
                ).fetchone()
                if granted is None:
                    granted = db.execute(
                        GRANT_PROBE_FOR_SHARE_SQL,
                        grant_probe_params(write.notebook_id, write.created_by),
                    ).fetchone()
            if notebook is None or (
                notebook["created_by"] != write.created_by and granted is None
            ):
                raise PermissionError(write.notebook_id)
            agent_profile = None
            if write.agent_profile_id:
                profile = db.execute(
                    "SELECT id,name FROM agent_profiles "
                    "WHERE id=%s AND owner_id=%s AND status='active'",
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
        db: object,
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
                "WHERE s.id=%s AND e.id=%s AND s.notebook_id=%s",
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
                "SELECT 1 FROM sources WHERE id=%s AND notebook_id=%s",
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
                "SELECT 1 FROM knowledge_objects WHERE id=%s AND notebook_id=%s "
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
                "SELECT 1 FROM memory_items WHERE id=%s AND notebook_id=%s "
                "AND created_by=%s AND status='confirmed'",
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
        try:
            with self.database.write(isolation_level="serializable") as db:
                existing = db.execute(
                    f"SELECT {self._select_columns()} FROM memory_items m "
                    "LEFT JOIN memory_provenance p ON p.memory_id=m.id "
                    "WHERE m.created_by=%s AND m.source_answer_id=%s FOR UPDATE OF m",
                    (write.created_by, write.source_answer_id),
                ).fetchone()
                if existing is not None:
                    item = self._record(existing)
                    if item.notebook_id != write.notebook_id:
                        raise KeyError(write.source_answer_id)
                    return item
                # ⚠ 三段式带 FOR SHARE 行锁,刻意不合并——理由同
                # create_candidate_with_initial_revision 处的注释。
                notebook = db.execute(
                    "SELECT created_by FROM notebooks WHERE id=%s FOR SHARE",
                    (write.notebook_id,),
                ).fetchone()
                granted = None
                if notebook is not None and notebook["created_by"] != write.created_by:
                    granted = db.execute(
                        MEMBER_PROBE_FOR_SHARE_SQL,
                        (write.notebook_id, write.created_by),
                    ).fetchone()
                    if granted is None:
                        granted = db.execute(
                            GRANT_PROBE_FOR_SHARE_SQL,
                            grant_probe_params(
                                write.notebook_id, write.created_by
                            ),
                        ).fetchone()
                if notebook is None or (
                    notebook["created_by"] != write.created_by and granted is None
                ):
                    raise KeyError(write.source_answer_id)
                row = db.execute(
                    "SELECT a.question,a.payload,a.conversation_id FROM answers a "
                    # the author, re-checked in this transaction like read access
                    "JOIN conversations c ON c.id=a.conversation_id "
                    "WHERE a.id=%s AND a.notebook_id=%s AND c.created_by=%s "
                    "FOR SHARE OF a",
                    (
                        write.source_answer_id,
                        write.notebook_id,
                        write.created_by,
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
        except (SerializationFailure, DeadlockDetected):
            # Recheck after PostgreSQL aborts the serializable transaction:
            # a revoked scope is the existing 404 domain; a still-live scope
            # is an honest concurrent-state conflict (ValueError -> API 409).
            if self._answer_save_scope_exists(write):
                raise ValueError("concurrent notebook access change") from None
            raise KeyError(write.source_answer_id) from None

    @staticmethod
    def _answer_save_scope_locked_on(db: object, write: MemoryWrite, row: object):
        """Testable seam reached only after notebook/member/answer locks."""
        del db, write
        return row

    def _answer_save_scope_exists(self, write: MemoryWrite) -> bool:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT 1 FROM answers a JOIN notebooks n ON n.id=a.notebook_id "
                "JOIN conversations c ON c.id=a.conversation_id "
                "WHERE a.id=%s AND a.notebook_id=%s AND c.created_by=%s AND "
                + read_access_clause("n", "nm"),
                (
                    write.source_answer_id,
                    write.notebook_id,
                    write.created_by,
                    *read_access_params(write.created_by),
                ),
            ).fetchone()
        return row is not None

    def memory_for_user(self, memory_id: str, user_id: str) -> MemoryRecord:
        with self.database.connect() as db:
            row = db.execute(
                f"SELECT {self._select_columns()} FROM memory_items m "
                "LEFT JOIN memory_provenance p ON p.memory_id=m.id "
                f"WHERE m.id=%s AND m.created_by=%s AND {self._read_access_clause()}",
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
                "WHERE m.created_by=%s AND m.source_answer_id=%s",
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
        placeholders = ",".join("%s" for _ in unique_ids)
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT m.source_answer_id,m.id FROM memory_items m "
                "WHERE m.notebook_id=%s AND m.created_by=%s "
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
                "WHERE m.created_by=%s AND m.notebook_id=%s "
                "AND m.origin='external_agent' "
                "AND m.agent_profile_id IS NOT DISTINCT FROM %s "
                "AND p.payload_json->>'client_request_id'=%s "
                "ORDER BY m.created_at, m.id COLLATE \"C\" LIMIT 1",
                (user_id, notebook_id, agent_profile_id, client_request_id),
            ).fetchone()
        return self._record(row) if row is not None else None

    def agent_profile_belongs_to(self, agent_profile_id: str, user_id: str) -> bool:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT 1 FROM agent_profiles WHERE id=%s AND owner_id=%s AND status='active'",
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
        db: object,
        memory_id: str,
        snapshot: Mapping[str, Any],
        changed_by: str,
        reason: str,
    ) -> None:
        db.execute(
            "SELECT id FROM memory_items WHERE id=%s FOR UPDATE", (memory_id,)
        )
        row = db.execute(
            "SELECT COALESCE(MAX(revision),0)+1 AS revision "
            "FROM memory_revisions WHERE memory_id=%s",
            (memory_id,),
        ).fetchone()
        db.execute(
            "INSERT INTO memory_revisions "
            "(id,memory_id,revision,title,content_md,tags_json,status,promotion_state,"
            "changed_by,change_reason,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                self.new_id("memrev"),
                memory_id,
                int(row["revision"]),
                snapshot["title"],
                snapshot["content_md"],
                jsonb(_strict_json_value(snapshot.get("tags", []), field="tags")),
                snapshot["status"],
                snapshot.get("promotion_state", "none"),
                changed_by,
                reason,
                normalize_timestamp(self.now()),
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
                "title=%s",
                "content_md=%s",
                "tags_json=%s",
                "status=%s",
                "promotion_state=%s",
                "updated_at=%s",
            ]
            params: list[Any] = [
                title,
                content_md,
                jsonb(_strict_json_value(tags, field="tags")),
                status,
                promotion_state,
                normalize_timestamp(now),
            ]
            if values or target == "confirmed":
                assignments.extend(
                    ["embedding_status='pending'", "embedding_error=''"]
                )
            if target == "confirmed":
                assignments.extend(["confirmed_by=%s", "confirmed_at=%s"])
                params.extend([user_id, normalize_timestamp(now)])
            placeholders = ",".join("%s" for _ in expected)
            params.extend([memory_id, user_id, *sorted(expected)])
            cursor = db.execute(
                f"UPDATE memory_items SET {','.join(assignments)} "
                f"WHERE id=%s AND created_by=%s AND status IN ({placeholders})",
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
        db: object,
        memory_id: str,
        *,
        expected_creator: str | None = None,
        expected_notebook: str | None = None,
        permission_error: bool = False,
    ):
        """Canonical PG lock order: notebook -> member -> Memory -> proposal.

        The first read only obtains immutable routing fields. Every mutable
        fact is revalidated after the corresponding row lock is acquired.
        """
        routing = db.execute(
            "SELECT notebook_id,created_by FROM memory_items WHERE id=%s",
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
        # ⚠ 三段式带 FOR SHARE 行锁,刻意不合并成单条 EXISTS:除了锁,owner 那一半还要
        # 单独区分「notebook 不存在」(恒 KeyError)与「无读权」(按调用方选
        # PermissionError/KeyError)。成员与授权边两半复用唯一定义点的探测常量;读权
        # 谓词扩展时这里必须手改(已登记在 access_sql 的消费者清单)。
        notebook = db.execute(
            "SELECT created_by FROM notebooks WHERE id=%s FOR SHARE",
            (notebook_id,),
        ).fetchone()
        if notebook is None:
            raise KeyError(memory_id)
        if notebook["created_by"] != creator_id:
            member = db.execute(
                MEMBER_PROBE_FOR_SHARE_SQL,
                (notebook_id, creator_id),
            ).fetchone()
            if member is None:
                grant = db.execute(
                    GRANT_PROBE_FOR_SHARE_SQL,
                    grant_probe_params(notebook_id, creator_id),
                ).fetchone()
                if grant is None:
                    if permission_error:
                        raise PermissionError(memory_id)
                    raise KeyError(memory_id)
        row = db.execute(
            f"SELECT {self._select_columns()} FROM memory_items m "
            "LEFT JOIN memory_provenance p ON p.memory_id=m.id "
            "WHERE m.id=%s AND m.notebook_id=%s AND m.created_by=%s FOR UPDATE OF m",
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
        db: object, proposal_id: str, memory_id: str
    ):
        return db.execute(
            "SELECT id FROM promotion_candidates WHERE id=%s AND object_id=%s "
            "FOR UPDATE",
            (proposal_id, memory_id),
        ).fetchone()

    def lock_promotion_memory_on(
        self, db: object, memory_id: str, candidate_notebook_id: str
    ) -> MemoryRecord:
        row = self._lock_memory_aggregate_on(
            db,
            memory_id,
            expected_notebook=candidate_notebook_id,
            permission_error=True,
        )
        return self._record(row)

    @staticmethod
    def _supersede_active_promotion_on(
        db: object,
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
            "UPDATE promotion_candidates SET status='rejected',reason=%s,"
            "reviewed_by=%s,updated_at=%s WHERE id=%s AND object_id=%s "
            "AND status IN ('proposed','under_review')",
            (reason, changed_by, normalize_timestamp(now), proposal_id, memory_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("active Memory promotion could not be superseded")
        provenance["kg_promotion"] = {
            "state": "superseded",
            "superseded_at": now,
            "superseded_reason": reason,
        }
        db.execute(
            "UPDATE memory_provenance SET payload_json=%s WHERE memory_id=%s",
            (
                jsonb(_strict_json_value(provenance, field="memory provenance")),
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
                "WHERE memory_id=%s ORDER BY revision",
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
                created_at=iso_timestamp(row["created_at"]),
            )
            for row in rows
        ]

    def propose_promotion_on(
        self,
        db: object,
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
            f"WHERE m.id=%s AND m.created_by=%s AND {self._read_access_clause()} "
            "FOR UPDATE OF m",
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
            "WHERE memory_id=%s",
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
            "UPDATE memory_provenance SET payload_json=%s WHERE memory_id=%s",
            (
                jsonb(_strict_json_value(provenance, field="memory provenance")),
                memory_id,
            ),
        )
        db.execute(
            "UPDATE memory_items SET promotion_state='proposed',updated_at=%s "
            "WHERE id=%s AND created_by=%s AND status='confirmed'",
            (normalize_timestamp(now), memory_id, user_id),
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
        db: object,
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
            "FROM memory_revisions WHERE memory_id=%s AND revision=%s",
            (item.id, source_revision),
        ).fetchone()
        latest = db.execute(
            "SELECT COALESCE(MAX(revision),0) AS revision FROM memory_revisions "
            "WHERE memory_id=%s",
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
        self, db: object, memory_ids: Sequence[str]
    ) -> dict[str, MemoryRecord]:
        if not memory_ids:
            return {}
        # The whole promotion queue's memories (grows with the queue): one
        # array parameter, each id a primary-key probe (id_binding class 3).
        rows = db.execute(
            f"SELECT {self._select_columns()} FROM memory_items m "
            "LEFT JOIN memory_provenance p ON p.memory_id=m.id "
            "WHERE m.id=ANY(%s)",
            (list(memory_ids),),
        ).fetchall()
        return {str(row["id"]): self._record(row) for row in rows}

    def promotion_data_on(
        self, db: object, memory_id: str
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
        db: object,
        memory_id: str,
        candidate_notebook_id: str,
    ) -> None:
        """Revalidate Memory scope and creator access inside approval's write txn.

        读权判定整条走唯一定义点的**列引用**形式,不消费参数——理由与 SQLite 侧同款
        (见那一份的 docstring)。
        """
        row = db.execute(
            "SELECT m.notebook_id,"
            + read_access_clause("n", user_ref="m.created_by")
            + " AS has_access FROM memory_items m "
            "JOIN notebooks n ON n.id=m.notebook_id WHERE m.id=%s",
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
        db: object,
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
            "UPDATE memory_provenance SET payload_json=%s WHERE memory_id=%s",
            (
                jsonb(_strict_json_value(provenance, field="memory provenance")),
                memory_id,
            ),
        )
        db.execute(
            "UPDATE memory_items SET promotion_state=%s,updated_at=%s "
            "WHERE id=%s AND status='confirmed'",
            (state, normalize_timestamp(now), memory_id),
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
            assignments.append(f"{column}=%s")
            params.append(
                jsonb(_strict_json_value(list(value), field="tags"))
                if key == "tags"
                else value
            )
        assignments.extend(["embedding_status='pending'", "embedding_error=''", "updated_at=%s"])
        params.extend([
            normalize_timestamp(self.now()),
            memory_id,
            user_id,
            *read_access_params(user_id),
        ])
        with self.database.write() as db:
            cursor = db.execute(
                f"UPDATE memory_items SET {','.join(assignments)} "
                "WHERE id=%s AND created_by=%s AND "
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
        placeholders = ",".join("%s" for _ in expected)
        confirmation = (
            ",confirmed_by=%s,confirmed_at=%s,embedding_status='pending',embedding_error=''"
            if target == "confirmed"
            else ""
        )
        params: list[Any] = [target, normalize_timestamp(now)]
        if target == "confirmed":
            params.extend([user_id, normalize_timestamp(now)])
        params.extend([memory_id, user_id, *read_access_params(user_id), *sorted(expected)])
        with self.database.write() as db:
            cursor = db.execute(
                f"UPDATE memory_items SET status=%s,updated_at=%s{confirmation} "
                "WHERE id=%s AND created_by=%s AND "
                f"{self._read_access_clause('memory_items')} "
                f"AND status IN ({placeholders})",
                params,
            )
            if cursor.rowcount != 1:
                exists = db.execute(
                    "SELECT status FROM memory_items m "
                    "WHERE id=%s AND created_by=%s AND "
                    f"{self._read_access_clause()}",
                    (memory_id, user_id, *read_access_params(user_id)),
                ).fetchone()
                if exists is None:
                    raise KeyError(memory_id)
                raise ValueError(f"invalid memory transition: {exists['status']} -> {target}")
        return self.memory_for_user(memory_id, user_id)

    def delete_memory(self, memory_id: str, user_id: str) -> None:
        with self.database.write() as db:
            deleted = self._hard_delete_on(db, user_id, [memory_id])
        if len(deleted) != 1:
            raise KeyError(memory_id)

    def delete_memory_if_unchanged(
        self, memory_id: str, user_id: str, expected_revision: "int | None"
    ) -> bool:
        """Atomic conditional delete for ``MemoryService.transfer``'s move.

        The parent Memory row is locked before revision, status, promotion,
        ownership, and deletion are evaluated in one write transaction. This
        ordering makes a revision committed by an editor waiting on the same
        row visible before this method decides whether deletion is still safe.

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
        ``promotion_state`` in the same locked transaction closes that gap
        regardless of exactly when, relative to the revision capture, the
        proposal lands. A failed promotion-state check flows through the same
        ``False`` return / retain-and-report-``copied_source_not_removed``
        path as every other "changed since expected_revision" cause above
        — not a new result shape."""
        with self.database.write() as db:
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
                "DELETE FROM memory_items WHERE id=%s AND created_by=%s",
                (memory_id, user_id),
            )
        return cursor.rowcount == 1

    def bulk_delete_memories(self, user_id: str, memory_ids: Sequence[str]) -> int:
        unique = _bulk_memory_ids(memory_ids)
        if not unique:
            return 0
        with self.database.write() as db:
            return len(self._hard_delete_on(db, user_id, unique))

    def _hard_delete_on(
        self, db: object, user_id: str, memory_ids: Sequence[str]
    ) -> list[str]:
        """Delete the caller's own Memory rows and withdraw their live proposals.

        One transaction: lock the rows in id order, reject every still-active
        promotion proposal pointing at them, then delete them (revisions,
        provenance and embeddings go with the row through their cascading
        foreign keys). Locking the Memory rows first gives the same
        Memory-then-candidate order ``approve_promotion`` uses, and it makes a
        proposal racing this delete either visible to the withdrawal or fail
        on the vanished row. Every statement is scoped by owner, notebook and
        id — the creator condition is enforced here too, not only by the
        callers that pre-filter. The derived source and KG rows are NOT
        touched here — ``MemoryService`` removes them first, before this row
        disappears."""
        rows = db.execute(
            "SELECT id,notebook_id FROM memory_items "
            "WHERE created_by=%s AND id=ANY(%s) ORDER BY id FOR UPDATE",
            (user_id, list(memory_ids)),
        ).fetchall()
        by_notebook: dict[str, list[str]] = {}
        for row in rows:
            by_notebook.setdefault(row["notebook_id"], []).append(row["id"])
        now = self.now()
        for notebook_id, ids in by_notebook.items():
            GovernanceStore.withdraw_memory_promotions_on(
                db, notebook_id, ids, MEMORY_DELETED_PROMOTION_REASON, now
            )
            db.execute(
                "DELETE FROM memory_items "
                "WHERE notebook_id=%s AND created_by=%s AND id=ANY(%s)",
                (notebook_id, user_id, ids),
            )
        return [row["id"] for row in rows]

    def owned_memory_refs(
        self, user_id: str, memory_ids: Sequence[str]
    ) -> list[tuple[str, str]]:
        """``(memory_id, notebook_id)`` of the subset of ``memory_ids`` this
        user created, whatever their current read access — exactly the rows
        ``bulk_delete_memories`` would delete, so the service can remove
        their derived rows first. Same de-duplication and 200-id bound."""
        unique = _bulk_memory_ids(memory_ids)
        if not unique:
            return []
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT id,notebook_id FROM memory_items "
                "WHERE created_by=%s AND id=ANY(%s) ORDER BY id",
                (user_id, unique),
            ).fetchall()
        return [(row["id"], row["notebook_id"]) for row in rows]

    @staticmethod
    def _member_exit_state_on(
        db: object, notebook_id: str, user_id: str, *, lock: bool
    ) -> tuple[Any, bool]:
        """``(is_member, keeps_access)`` for one user and notebook.

        ``lock`` takes the membership row ``FOR UPDATE``: every Memory write
        by a member holds that row ``FOR SHARE`` for its whole transaction
        (the locked member probe of ``access_sql``), so while it is held no Memory of
        this member can be created or changed here. ``keeps_access`` is the
        user's read path WITHOUT the membership row: ownership or any grant
        (the same ``grant_access_expr`` the read predicate uses)."""
        member = db.execute(
            "SELECT added_at FROM notebook_members WHERE notebook_id=%s AND user_id=%s"
            + (" FOR UPDATE" if lock else ""),
            (notebook_id, user_id),
        ).fetchone()
        grant = grant_access_expr("nb.id", "%s", "xg", "xgm", "xga")
        access = db.execute(
            "SELECT COALESCE(nb.created_by=%s, false) OR "
            f"{grant} AS keeps_access FROM notebooks nb WHERE nb.id=%s",
            (user_id, *(user_id,) * grant.count("%s"), notebook_id),
        ).fetchone()
        return (
            member["added_at"] if member is not None else None,
            bool(access and access["keeps_access"]),
        )

    def member_exit_snapshot(
        self, notebook_id: str, user_id: str, *, claim: bool
    ) -> MemberExitSnapshot:
        """What leaving ``notebook_id`` would delete of ``user_id``'s Memory.

        ``claim=False`` is the disclosure read: counts only. ``claim=True`` is
        the exit's first step: under the membership row lock (see
        ``_member_exit_state_on``) it returns the ids to delete, so the count
        the leaver acknowledged is checked against exactly the rows that will
        be deleted — a Memory saved after the disclosure changes the count.
        Every status counts: the exit deletes candidates, rejected and
        deprecated rows too."""
        if claim:
            with self.database.write() as db:
                token, keeps = self._member_exit_state_on(
                    db, notebook_id, user_id, lock=True
                )
                ids: tuple[str, ...] = ()
                if token is not None and not keeps:
                    ids = tuple(
                        row["id"]
                        for row in db.execute(
                            "SELECT id FROM memory_items "
                            "WHERE notebook_id=%s AND created_by=%s "
                            "ORDER BY created_at,id",
                            (notebook_id, user_id),
                        ).fetchall()
                    )
            return MemberExitSnapshot(
                token is not None, keeps, len(ids), ids, membership_token=token
            )
        with self.database.connect() as db:
            token, keeps = self._member_exit_state_on(
                db, notebook_id, user_id, lock=False
            )
            count = 0
            if token is not None and not keeps:
                count = int(db.execute(
                    "SELECT COUNT(*) AS c FROM memory_items "
                    "WHERE notebook_id=%s AND created_by=%s",
                    (notebook_id, user_id),
                ).fetchone()["c"])
        return MemberExitSnapshot(token is not None, keeps, count)

    def finish_member_exit(
        self, notebook_id: str, user_id: str, membership_token: Any
    ) -> tuple[int, bool]:
        """End the membership this exit claimed — atomically with the check
        that none of the leaver's Memory is left.

        One transaction under the membership row lock. Returns
        ``(remaining, ended)``:

        * the row is gone, or is not the claimed one (its ``added_at`` differs
          from ``membership_token``: someone removed the member and added
          them back while the purge ran) → ``(0, False)``: the claimed
          membership has already ended, and a membership created after it
          is never touched — nor counted;
        * the claimed row, and Memory of the leaver exists here (saved while
          the purge ran) → ``(count, False)``: the membership stays, nothing
          unacknowledged is ever deleted;
        * otherwise the row is deleted → ``(0, True)``.

        The row delete lives here, not in the sharing store, because it must
        commit together with that count."""
        with self.database.write() as db:
            member = db.execute(
                "SELECT added_at FROM notebook_members "
                "WHERE notebook_id=%s AND user_id=%s FOR UPDATE",
                (notebook_id, user_id),
            ).fetchone()
            if member is None or member["added_at"] != membership_token:
                return 0, False
            remaining = int(db.execute(
                "SELECT COUNT(*) AS c FROM memory_items "
                "WHERE notebook_id=%s AND created_by=%s",
                (notebook_id, user_id),
            ).fetchone()["c"])
            if remaining:
                return remaining, False
            db.execute(
                "DELETE FROM notebook_members WHERE notebook_id=%s AND user_id=%s",
                (notebook_id, user_id),
            )
        return 0, True

    def derived_memory_sources(
        self, refs: Sequence[tuple[str, str]]
    ) -> list[tuple[str, str, str]]:
        """``(memory_id, source_id, notebook_id)`` of the hidden sources
        projected from these ``(memory_id, notebook_id)`` refs — one read for
        a whole page (at most ``_PURGE_PAGE`` ids). Keyed on the ref pairs
        rather than a join with ``memory_items``, so it also finds a source
        whose Memory row is already gone (the post-delete sweep for an ingest
        that finished in between). A Memory without a derived source simply
        has no row.

        The predicate repeats ``idx_sources_memory_id``'s partial-index
        condition (``memory_id IS NOT NULL AND memory_id <> ''``): PostgreSQL
        cannot infer ``memory_id <> ''`` from ``memory_id = ANY($1)``, so
        without it both the custom and the generic plan scan the whole
        ``sources`` table (measured 36 ms vs 0.46 ms at 300k rows)."""
        if not refs:
            return []
        wanted = {(memory_id, notebook_id) for memory_id, notebook_id in refs}
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT id,notebook_id,memory_id FROM sources "
                "WHERE memory_id = ANY(%s) AND memory_id IS NOT NULL "
                f"AND memory_id <> '' AND {memory_source_type_predicate()} "
                "ORDER BY id",
                (sorted({memory_id for memory_id, _ in refs}),),
            ).fetchall()
        return [
            (row["memory_id"], row["id"], row["notebook_id"])
            for row in rows
            if (row["memory_id"], row["notebook_id"]) in wanted
        ]

    def detach_memory_projection_on(
        self,
        db: object,
        sources: Sequence[Mapping[str, Any]],
        *,
        bridge_canonical_ids_of: Callable[[list[dict]], list[str]],
    ) -> dict[str, list[str]]:
        """The Memory-specific half of a purge page, inside the teardown's
        transaction (``SourceIngestionService.remove_memory_sources`` calls it
        after locking ``sources`` — rows of ``{id, notebook_id}`` — and before
        the generic teardown). Per notebook, in batched statements:

        * strip the sources' evidence from objects ANOTHER source owns
          (``strip_sources_evidence_on``: one id-ordered lock statement), so
          the teardown deletes only what the Memory itself minted;
        * read the sources' own objects (``memory_derived_object``);
        * remove the whole clusters of those objects and the merge and
          conflict candidates naming them
          (``purge_memory_review_rows_on``).

        Returns the own object ids per notebook, for the lexical-index
        cleanup after the teardown. ``bridge_canonical_ids_of`` maps the
        objects ``[{object_id, object_type, name}]`` to their bridge canonical
        ids (``kg_merge.purge_bridge_canonical_ids``); the ids minted from
        them are derived in SQL."""
        owned: dict[str, list[str]] = {}
        by_notebook: dict[str, list[str]] = {}
        for row in sources:
            by_notebook.setdefault(row["notebook_id"], []).append(row["id"])
        now = self.now()
        for notebook_id in sorted(by_notebook):
            source_ids = sorted(by_notebook[notebook_id])
            GovernanceStore.strip_sources_evidence_on(db, notebook_id, source_ids, now)
            objects = db.execute(
                "SELECT ko.id,ko.object_type,ko.payload->>'name' AS name "
                "FROM knowledge_objects ko "
                "WHERE ko.notebook_id=%s AND ko.source_id = ANY(%s) "
                f"AND {memory_derived_object('ko')} ORDER BY ko.id",
                (notebook_id, source_ids),
            ).fetchall()
            object_ids = [row["id"] for row in objects]
            bridge = bridge_canonical_ids_of([
                {"object_id": row["id"], "object_type": row["object_type"],
                 "name": row["name"] or ""}
                for row in objects
            ])
            GovernanceStore.purge_memory_review_rows_on(
                db,
                notebook_id,
                source_ids,
                bridge_canonical_ids=bridge,
            )
            owned[notebook_id] = object_ids
        return owned

    @staticmethod
    def drop_memory_lexical_rows_on(
        db: object, owned_objects: Mapping[str, Sequence[str]] | None
    ) -> int:
        """PostgreSQL searches ``knowledge_objects`` itself; there is no
        separate lexical-index table to clean (see the SQLite twin)."""
        return 0

    def memory_export_snapshot(
        self, notebook_id: str, user_id: str, *, page_size: int, max_bytes: int
    ) -> list[MemoryRecord]:
        """This user's own Memory in one notebook, every status, oldest first
        (``created_at``, then id), read in ONE read-only REPEATABLE READ
        transaction: the count and every keyset page see the same snapshot,
        so a concurrent purge (the member's exit, a hard or bulk delete, a
        transfer's move) can never cut the result short. The transaction lasts
        as long as the server's reads, never the client's download: the
        caller renders and streams the returned list afterwards.

        Read-gated like every Memory read, owner-scoped like every Memory row.
        Statements stay bounded by ``page_size`` rows; the list holds every
        item; once their content passes ``max_bytes`` (UTF-8) the read stops
        and ``MemoryExportTooLarge`` is raised. Raises ``RuntimeError`` when
        the pages disagree with the snapshot's count (they cannot, inside one
        snapshot; the check refuses to hand out a file that silently lacks
        items)."""
        page = max(1, min(int(page_size), 500))
        with self.database.connect() as db:
            db.read_only = True
            db.isolation_level = IsolationLevel.REPEATABLE_READ
            where, params = self._export_where(notebook_id, user_id)
            total = int(db.execute(
                f"SELECT count(*) AS n FROM memory_items m WHERE {where}", params
            ).fetchone()["n"])
            items: list[MemoryRecord] = []
            size = 0
            after: tuple[Any, str] | None = None
            while True:
                rows, after = self._export_page_on(db, notebook_id, user_id, after, page)
                for row in rows:
                    record = self._record(row)
                    size += len(record.content_md.encode("utf-8"))
                    if size > max_bytes:
                        raise MemoryExportTooLarge(total, max_bytes)
                    items.append(record)
                if after is None:
                    break
        if len(items) != total:
            raise RuntimeError(
                f"memory export read {len(items)} of {total} items in one snapshot"
            )
        return items

    def _export_where(self, notebook_id: str, user_id: str) -> tuple[str, list[Any]]:
        clauses = ["m.notebook_id=%s", "m.created_by=%s", self._read_access_clause()]
        return " AND ".join(clauses), [notebook_id, user_id, *read_access_params(user_id)]

    def _export_page_on(
        self,
        db: Any,
        notebook_id: str,
        user_id: str,
        after: tuple[Any, str] | None,
        page: int,
    ) -> tuple[list[Any], tuple[Any, str] | None]:
        """One keyset page on the caller's connection; the cursor carries the
        raw column value, so no timestamp formatting can skip or repeat a row.
        Returns the rows and the next cursor (``None`` after the last page)."""
        where, params = self._export_where(notebook_id, user_id)
        if after is not None:
            where += " AND (m.created_at,m.id)>(%s,%s)"
            params.extend(after)
        rows = db.execute(
            f"SELECT {self._select_columns()},m.created_at AS cursor_created_at "
            "FROM memory_items m LEFT JOIN memory_provenance p ON p.memory_id=m.id "
            f"WHERE {where} ORDER BY m.created_at,m.id LIMIT %s",
            (*params, page + 1),
        ).fetchall()
        more = len(rows) > page
        rows = rows[:page]
        cursor = (
            (rows[-1]["cursor_created_at"], rows[-1]["id"]) if more and rows else None
        )
        return rows, cursor

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
        clauses = ["m.created_by=%s", self._read_access_clause()]
        params: list[Any] = [user_id, *read_access_params(user_id)]
        clean_query = (query or "").strip()
        if notebook_id:
            clauses.append("m.notebook_id=%s")
            params.append(notebook_id)
        if status:
            clauses.append("m.status=%s")
            params.append(status)
        if origin:
            clauses.append("m.origin=%s")
            params.append(origin)
        where = " AND ".join(clauses)
        with self.database.connect() as db:
            filtered_total: int | None = None
            row_offset = offset
            if clean_query:
                scope = MemoryCandidateScope(
                    owner_id=user_id,
                    viewer_id=user_id,
                    notebook_id=notebook_id,
                    statuses=(status,) if status else (),
                    origin=origin,
                )
                filtered_total = memory_match_count(
                    db,
                    clean_query,
                    scope=scope,
                )
                candidate_ids = memory_page_candidate_ids(
                    db, clean_query, limit, offset, scope=scope
                )
                clauses.append("m.id=ANY(%s)")
                params.append(candidate_ids)
                where = " AND ".join(clauses)
                row_offset = 0
            aggregate = db.execute(
                "SELECT COUNT(*) AS total_count,"
                "COALESCE(SUM(CASE WHEN m.status='candidate' THEN 1 ELSE 0 END),0) "
                "AS pending_count FROM memory_items m WHERE m.created_by=%s AND "
                f"{self._read_access_clause()}",
                (user_id, *read_access_params(user_id)),
            ).fetchone()
            option_rows = db.execute(
                "SELECT m.notebook_id,nb.name,COUNT(*) AS memory_count,"
                "COALESCE(SUM(CASE WHEN m.status='candidate' THEN 1 ELSE 0 END),0) "
                "AS pending_count FROM memory_items m "
                "JOIN notebooks nb ON nb.id=m.notebook_id "
                "WHERE m.created_by=%s AND "
                f"{self._read_access_clause()} "
                "GROUP BY m.notebook_id,nb.name "
                "ORDER BY nb.name COLLATE \"C\",m.notebook_id COLLATE \"C\" LIMIT 200",
                (user_id, *read_access_params(user_id)),
            ).fetchall()
            total = (
                filtered_total
                if filtered_total is not None
                else db.execute(
                    f"SELECT COUNT(*) AS c FROM memory_items m {joins} WHERE {where}",
                    params,
                ).fetchone()["c"]
            )
            rows = db.execute(
                f"SELECT {self._select_columns()} FROM memory_items m {joins} "
                f"WHERE {where} ORDER BY m.updated_at DESC,m.id COLLATE \"C\" "
                "LIMIT %s OFFSET %s",
                (*params, limit, row_offset),
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
                "WHERE m.id=%s AND m.title=%s AND m.content_md=%s AND m.tags_json=%s "
                "AND m.status=%s",
                (
                    memory_id,
                    item.title,
                    item.content_md,
                    jsonb(_strict_json_value(list(item.tags), field="tags")),
                    item.status,
                ),
            ).fetchone()
        return int(row["revision"]) if row is not None else None

    def replace_embedding(
        self, memory_id: str, expected_revision: int, model: str,
        vector: Sequence[float],
    ) -> bool:
        with self.database.write() as db:
            locked = self._lock_memory_revision_on(db, memory_id)
            if locked is None:
                return False
            _, revision = locked
            if revision != int(expected_revision):
                return False
            db.execute(
                "INSERT INTO memory_embeddings "
                "(memory_id,model,dimension,vector,updated_at) VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT(memory_id) DO UPDATE SET model=EXCLUDED.model,"
                "dimension=EXCLUDED.dimension,vector=EXCLUDED.vector,"
                "updated_at=EXCLUDED.updated_at",
                (
                    memory_id,
                    model,
                    len(vector),
                    encode_vector(vector),
                    normalize_timestamp(self.now()),
                ),
            )
            db.execute(
                "UPDATE memory_items SET embedding_status='ready',embedding_error='' "
                "WHERE id=%s",
                (memory_id,),
            )
        return True

    @staticmethod
    def _lock_memory_revision_on(db: object, memory_id: str):
        item = db.execute(
            "SELECT id,created_by,status,promotion_state FROM memory_items "
            "WHERE id=%s FOR UPDATE",
            (memory_id,),
        ).fetchone()
        if item is None:
            return None
        current = db.execute(
            "SELECT COALESCE(MAX(revision),0) AS revision "
            "FROM memory_revisions WHERE memory_id=%s",
            (memory_id,),
        ).fetchone()
        return item, int(current["revision"])

    def mark_embedding_failed(
        self, memory_id: str, expected_revision: int, error: str
    ) -> bool:
        with self.database.write() as db:
            locked = self._lock_memory_revision_on(db, memory_id)
            if locked is None:
                return False
            _, revision = locked
            if revision != int(expected_revision):
                return False
            cursor = db.execute(
                "UPDATE memory_items SET embedding_status='failed',embedding_error=%s "
                "WHERE id=%s",
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
        placeholders = ",".join("%s" for _ in allowed)
        common_params = (user_id, notebook_id, *allowed, *read_access_params(user_id))
        select = self._select_columns()
        with self.database.connect() as db:
            candidate_ids = memory_candidate_ids(
                db,
                clean_query,
                lexical_limit,
                scope=MemoryCandidateScope(
                    owner_id=user_id,
                    viewer_id=user_id,
                    notebook_id=notebook_id,
                    statuses=allowed,
                ),
                phrase_queries=phrase_queries,
            )
            lexical_rows = db.execute(
                f"SELECT {select},me.vector AS retrieval_vector "
                "FROM memory_items m "
                "LEFT JOIN memory_provenance p ON p.memory_id=m.id "
                "LEFT JOIN memory_embeddings me ON me.memory_id=m.id "
                "WHERE m.created_by=%s AND m.notebook_id=%s "
                f"AND m.status IN ({placeholders}) "
                f"AND {self._read_access_clause()} "
                "AND m.id=ANY(%s) "
                "ORDER BY m.updated_at DESC,m.id COLLATE \"C\" LIMIT %s",
                (*common_params, candidate_ids, lexical_limit),
            ).fetchall()
            vector_rows = db.execute(
                f"SELECT {select},me.vector AS retrieval_vector "
                "FROM memory_items m "
                "LEFT JOIN memory_provenance p ON p.memory_id=m.id "
                "JOIN memory_embeddings me ON me.memory_id=m.id "
                "WHERE m.created_by=%s AND m.notebook_id=%s "
                f"AND m.status IN ({placeholders}) "
                f"AND {self._read_access_clause()} "
                "ORDER BY m.updated_at DESC,m.id COLLATE \"C\" LIMIT %s",
                (*common_params, vector_limit),
            ).fetchall()
        rows: dict[str, dict[str, Any]] = {}
        for row in [*lexical_rows, *vector_rows]:
            rows.setdefault(
                str(row["id"]),
                {
                    "record": self._record(row),
                    "vector": (
                        bytes(row["retrieval_vector"])
                        if row["retrieval_vector"] is not None
                        else None
                    ),
                },
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
            source_guard = self._lock_copy_source_on(db, source_memory_id)
            item, created = self._insert_memory_on(db, write)
            if item.id != write.id:
                return item  # 幂等命中已有行（正常不会发生：copy 用全新 id）
            self._ensure_initial_revision_on(db, item, created, changed_by, reason)
            source_state = db.execute(
                "SELECT embedding_status,(SELECT COALESCE(MAX(revision),0) "
                "FROM memory_revisions WHERE memory_id=%s) AS revision "
                "FROM memory_items WHERE id=%s",
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
                    "SELECT model, dimension, vector FROM memory_embeddings WHERE memory_id=%s",
                    (source_memory_id,),
                ).fetchone()
                if vec is not None:
                    db.execute(
                        "INSERT INTO memory_embeddings "
                        "(memory_id,model,dimension,vector,updated_at) VALUES (%s,%s,%s,%s,%s) "
                        "ON CONFLICT(memory_id) DO UPDATE SET model=EXCLUDED.model,"
                        "dimension=EXCLUDED.dimension,vector=EXCLUDED.vector,"
                        "updated_at=EXCLUDED.updated_at",
                        (
                            write.id,
                            vec["model"],
                            vec["dimension"],
                            vec["vector"],
                            normalize_timestamp(self.now()),
                        ),
                    )
                    db.execute(
                        "UPDATE memory_items SET embedding_status='ready',embedding_error='' "
                        "WHERE id=%s",
                        (write.id,),
                    )
                    item = self._record(
                        db.execute(
                            f"SELECT {self._select_columns()} FROM memory_items m "
                            "LEFT JOIN memory_provenance p ON p.memory_id=m.id "
                            "WHERE m.id=%s AND m.created_by=%s",
                            (write.id, write.created_by),
                        ).fetchone()
                    )
        return item

    @staticmethod
    def _lock_copy_source_on(db: object, source_memory_id: str):
        return db.execute(
            "SELECT embedding_status FROM memory_items WHERE id=%s FOR SHARE",
            (source_memory_id,),
        ).fetchone()

    def has_orphan_memory_sources(self) -> bool:
        """是否存在至少一个无主 Memory 来源(启动探测;`EXISTS`,不排序,首个命中即停)。"""
        with self.database.connect() as db:
            row = db.execute(
                f"SELECT EXISTS (SELECT 1 FROM sources s WHERE {_ORPHAN_MEMORY_SOURCE_WHERE}) AS found"
            ).fetchone()
        return bool(row["found"])

    def orphan_memory_source_ids(self, limit: int, after_id: str = "") -> list[str]:
        """`orphan_memory_source_refs` 的 id 一列(同一条语句)。"""
        return [source_id for source_id, _ in self.orphan_memory_source_refs(limit, after_id)]

    def orphan_memory_source_refs(
        self, limit: int, after_id: str = ""
    ) -> list[tuple[str, str]]:
        """至多 `limit` 个无主 Memory 来源的 `(id, notebook_id)`,按 id 升序,只取 `after_id`
        之后的(键集分页)。清扫按 `notebook_id` 把一页切成同库的批,一次移除只涉及一个笔记本。

        全库读(清扫在启动后一次跑完,不属于任何笔记本)。反连接放进 MATERIALIZED CTE、
        再对结果(只有孤儿行,数量以 Memory 来源数为界)排序取前 `limit` 个:CTE 阻止
        规划器为了 `ORDER BY s.id LIMIT` 去沿主键整表走一遍。`sources` 上没有单独的
        `source_type` 索引,所以反连接对 `sources` 是整表一趟;清扫每页至多再来一趟,页数
        由 `limit` 决定但不改变结果。实测数字与计划见 `test_memory_orphan_sweep_explain_pins`
        和 docs/operations.md。
        """
        with self.database.connect() as db:
            rows = db.execute(
                "WITH o AS MATERIALIZED (SELECT s.id, s.notebook_id FROM sources s "
                f"WHERE {_ORPHAN_MEMORY_SOURCE_WHERE} AND s.id > %s) "
                "SELECT id, notebook_id FROM o ORDER BY id LIMIT %s",
                (after_id, max(1, int(limit))),
            ).fetchall()
        return [(str(row["id"]), str(row["notebook_id"])) for row in rows]

    @staticmethod
    def orphan_memory_source_count_on(db: object, notebook_id: str) -> int:
        """本笔记本里仍在的无主 Memory 来源数(体检只读项;搭调用方的读快照)。

        先按 `(notebook_id, source_type)` 索引限到本库的 Memory 来源(数量以本库
        已确认 Memory 为界),再逐行判无主。
        """
        row = db.execute(
            "SELECT count(*) AS n FROM sources s "
            f"WHERE s.notebook_id = %s AND {_ORPHAN_MEMORY_SOURCE_WHERE}",
            (notebook_id,),
        ).fetchone()
        return int(row["n"])

    @staticmethod
    def memory_sources_for_source_ids_sql(*, lock: bool = False) -> str:
        """The statement behind the Memory-source lookups (also EXPLAIN-pinned).

        Two scalar parameters, in order: the id list as ONE JSON array text, and
        the owner.  The id list is a citation list (a report's references), so it
        can run to hundreds; it is unpacked in SQL instead of being bound as one
        placeholder per id or as ``= ANY(%s)`` with a Python list.  The row
        estimate of ``jsonb_array_elements_text`` does not depend on the bound
        value, so the custom and generic plans are the same and a prepared
        statement cannot flip to a worse plan.

        ``lock=True`` (the report share transaction) adds ``FOR SHARE OF s`` on
        the matching source rows, taken in ``s.id`` order; the share takes the
        matching Memory rows first, in Memory-id order, with
        ``memory_rows_lock_sql`` (see there and ``memory_sources_on``).
        Readability is decided by ``memory_source_readable`` alone.
        """
        from app.repositories.postgres import memory_sql

        return (
            "SELECT s.id AS source_id, s.memory_id AS memory_id "
            "FROM jsonb_array_elements_text(%s::jsonb) AS wanted(id) "
            "JOIN sources s ON s.id = wanted.id "
            f"WHERE {memory_sql.memory_source_type_predicate('s.source_type')} "
            f"AND {memory_sql.memory_source_readable('s')}"
            + (" ORDER BY s.id FOR SHARE OF s" if lock else "")
        )

    @staticmethod
    def memory_rows_lock_sql() -> str:
        """The share transaction's first lock: ``FOR SHARE`` on the Memory rows
        behind the cited sources that are the owner's Memory sources, in
        Memory-id order (also EXPLAIN-pinned).  Same two scalar parameters as
        ``memory_sources_for_source_ids_sql``.

        The count and the token are one snapshot: each lock guards one input of
        the count — this one the Memory row (its hard delete or a change of its
        owner), the source lock the source row (its removal, or a change of its
        type / ``memory_id``); ``sources.memory_id`` carries no foreign key, so
        neither implies the other.  A concurrent write to exactly those rows
        waits for the share transaction, or — when it committed first — is
        seen (READ COMMITTED re-checks a row it waited for).

        Order.  Memory rows are locked in Memory-id order, the order the Memory
        purge locks them in (``_hard_delete_on``: ``SELECT … ORDER BY id
        FOR UPDATE``); that shared order is what keeps a share and a purge of
        the same author out of a cycle.  Taking the Memory rows
        before the source rows is not what prevents it — the purge removes the
        derived source rows and the Memory rows in separate transactions, so
        no purge transaction holds both.  The synchronous notebook-delete path
        (sources ``FOR UPDATE``, then ``memory_items`` through its cascade) is
        used only by evaluation and tests; production deletes a notebook as a
        job after its tombstone, and a tombstoned notebook no longer admits a
        share.  Only rows of cited sources without a stored record are locked;
        other users' Memory and the author's uncited Memory are untouched.
        """
        from app.repositories.postgres import memory_sql

        return (
            "SELECT lm.id FROM memory_items lm WHERE lm.id IN ("
            "SELECT s.memory_id "
            "FROM jsonb_array_elements_text(%s::jsonb) AS wanted(id) "
            "JOIN sources s ON s.id = wanted.id "
            f"WHERE {memory_sql.memory_source_type_predicate('s.source_type')} "
            f"AND {memory_sql.memory_source_readable('s')}"
            ") ORDER BY lm.id FOR SHARE OF lm"
        )

    @staticmethod
    def memory_sources_on(
        db: object, source_ids: Sequence[str], owner_id: str, *, lock: bool = False
    ) -> dict[str, str]:
        """``{source_id: memory_id}`` for the given ids that are ``owner_id``'s
        Memory sources, read on the caller's connection (see the SQL builder).
        ``lock=True``: the Memory rows first (Memory-id order), then the source
        rows (source-id order); see ``memory_rows_lock_sql``."""
        wanted = list(dict.fromkeys(str(item) for item in source_ids if item))
        owner = str(owner_id or "")
        if not wanted or not owner:
            return {}
        if lock:
            db.execute(
                MemoryStore.memory_rows_lock_sql(), (json.dumps(wanted), owner)
            ).fetchall()
        rows = db.execute(
            MemoryStore.memory_sources_for_source_ids_sql(lock=lock),
            (json.dumps(wanted), owner),
        ).fetchall()
        return {str(row["source_id"]): str(row["memory_id"]) for row in rows}

    def memory_sources_for_source_ids(
        self, source_ids: Sequence[str], owner_id: str
    ) -> dict[str, str]:
        """``{source_id: memory_id}`` for the ids that are ``owner_id``'s Memory
        sources.

        A source maps only when it is a Memory source (``source_type =
        'memory'``) readable by ``owner_id`` under ``memory_sql``'s single
        definition — its ``memory_items`` row was created by that owner.
        Another member's Memory source, an orphaned Memory source, Knowhow, an
        ordinary source and an unknown id map to nothing, so a caller can never
        learn about anyone else's Memory.  Empty ids or owner: ``{}``.
        """
        with self.database.connect() as db:
            return self.memory_sources_on(db, source_ids, owner_id)

    def memory_ids_for_source_ids(
        self, source_ids: Sequence[str], owner_id: str
    ) -> list[str]:
        """Sorted distinct Memory ids behind ``memory_sources_for_source_ids``."""
        return sorted(set(self.memory_sources_for_source_ids(source_ids, owner_id).values()))

    @staticmethod
    def foreign_memory_sources_for_source_ids_sql() -> str:
        """The statement behind ``foreign_memory_sources_for_source_ids`` (also
        EXPLAIN-pinned).  Same two scalar parameters and the same id-list
        unpacking as ``memory_sources_for_source_ids_sql``.

        A cited source is another member's Memory source when it is a Memory
        source that ``memory_source_readable`` refuses to the given member AND
        its ``memory_items`` row exists (so its owner is known).  An orphaned
        Memory source (its Memory row gone) has no known owner and maps to
        nothing here, exactly as it maps to nothing in the author's read.
        """
        from app.repositories.postgres import memory_sql

        return (
            "SELECT s.id AS source_id, s.memory_id AS memory_id, "
            "fo.created_by AS owner_id "
            "FROM jsonb_array_elements_text(%s::jsonb) AS wanted(id) "
            "JOIN sources s ON s.id = wanted.id "
            "JOIN memory_items fo ON fo.id = s.memory_id "
            f"WHERE {memory_sql.memory_source_type_predicate('s.source_type')} "
            f"AND NOT {memory_sql.memory_source_readable('s')}"
        )

    def foreign_memory_sources_for_source_ids(
        self, source_ids: Sequence[str], member_id: str
    ) -> dict[str, tuple[str, str]]:
        """``{source_id: (memory_id, owner_id)}`` for the given ids that are
        Memory sources of someone other than ``member_id``.

        Used where a stored artifact of ``member_id`` (a report) is about to
        leave the notebook: citing another member's Memory must be refused, and
        the refusal must be able to say so.  Empty ids or member: ``{}``.
        """
        wanted = list(dict.fromkeys(str(item) for item in source_ids if item))
        member = str(member_id or "")
        if not wanted or not member:
            return {}
        with self.database.connect() as db:
            rows = db.execute(
                self.foreign_memory_sources_for_source_ids_sql(),
                (json.dumps(wanted), member),
            ).fetchall()
        return {
            str(row["source_id"]): (str(row["memory_id"]), str(row["owner_id"]))
            for row in rows
        }
