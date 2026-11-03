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
    MemberExitCompletion,
    MemberExitCounts,
    MemberExitSnapshot,
    MemoryExportSnapshot,
    MemoryNotebookOption,
    MemoryRecord,
    PaginatedMemories,
    SuggestionWriteResult,
)
from app.core.json_safety import strict_json_dumps
from app.core.memory_inputs import MemoryExportTooLarge
from app.domain.memory_suggest_contracts import (
    JOB_STATUS_RUNNING,
    ORIGIN_SUGGESTED,
    REVISION_REASON_SUGGESTED,
    REVOKED_CLAIM_LOST,
    SIGNAL_CLARIFIED,
    SKIP_NO_READ_ACCESS,
    SKIP_NOTEBOOK_UNAVAILABLE,
    SKIP_USER_DISABLED,
    SUGGESTED_SIGNALS,
    SuggestionRevoked,
)
from app.domain.notebook_delete_grace import (
    WINDOW_CLOSED,
    WINDOW_OPEN,
    classify_delete_window,
    delete_window_failure_code,
    instant_text,
    utc_instant,
)
from app.repositories.identity_errors import (
    AgentTokenAccessConflictError,
    AgentTokenInactiveError,
)
from app.repositories.ports import DeleteWindowClosedError
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
    NOTEBOOK_LIVE_SQL,
    NOTEBOOK_PENDING_DELETE_STATUS,
    export_access_exists_clause,
    grant_access_expr,
    grant_probe_params,
    notebook_delete_window_open_sql,
    read_access_after_group_exit_clause,
    read_access_clause,
    live_read_access_exists_clause,
    read_access_except_grant_clause,
    read_access_exists_clause,
    read_access_params,
    read_except_grant_params,
)
from app.repositories.postgres import memory_sql
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.governance_store import GovernanceStore
from app.repositories.postgres.memory_sql import (
    memory_derived_object,
    memory_source_type_predicate,
    unadopted_suggestion,
)
from app.repositories.postgres.memory_suggest_store import (
    DECLINED_FINGERPRINT_UPSERT_SQL,
    PROPOSED_FINGERPRINT_UPSERT_SQL,
)
from app.repositories.postgres.read_authority_lock import lock_live_reader_on, lock_read_changes_on
from app.repositories.postgres.memory_suggest_read_period import lock_mode_read_period_on, record_read_loss_on
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


def _check_suggestion_writes(
    writes: Sequence[MemoryWrite], *, notebook_id: str, owner_id: str
) -> None:
    """What ``create_suggestions_with_initial_revision`` accepts (SQLite twin
    identical): open ``ask_answer`` candidates of this owner and notebook,
    each with a known ``suggested_signal``; a ``clarified`` one binds no
    answer, so it never occupies ``idx_memory_answer_once``."""
    for write in writes:
        if write.notebook_id != notebook_id or write.created_by != owner_id:
            raise ValueError("a suggestion write must belong to this owner and notebook")
        if write.origin != "ask_answer" or write.status != "candidate":
            raise ValueError("a suggestion is an ask_answer candidate")
        if write.suggested_signal not in SUGGESTED_SIGNALS:
            raise ValueError(f"unknown suggested_signal: {write.suggested_signal!r}")
        if write.suggested_signal == SIGNAL_CLARIFIED and write.source_answer_id:
            raise ValueError("a clarified suggestion binds no answer")


# 「无主的 Memory 来源」:`source_type` 是 memory,而它指回的 Memory 不再是一条已确认的
# Memory —— `memory_id` 为 NULL/空(既有的笔记本拷贝清空了它,N-5)、指向的行已不存在
# (硬删残留)、或指向的行不是 `confirmed`。`orphan_memory_source_refs`、
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
            allow_answer_links=bool(row["allow_answer_links"]),
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
        self, owner_id: str, offset: int = 0, limit: int = 100,
        *, include_deleted: bool = False,
    ) -> list[AgentProfile]:
        # ``include_deleted`` is for id→name resolution only (a deleted
        # Agent's memories/records keep its name); listings leave it False.
        live_only = "" if include_deleted else "AND deleted_at IS NULL "
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM agent_profiles WHERE owner_id=%s "
                + live_only +
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
                "WHERE id=%s AND owner_id=%s AND deleted_at IS NULL",
                (*values, profile_id, owner_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(profile_id)
            row = db.execute(
                "SELECT * FROM agent_profiles WHERE id=%s AND owner_id=%s",
                (profile_id, owner_id),
            ).fetchone()
        return self._profile(row)

    def delete_agent_profile(self, profile_id: str, owner_id: str) -> None:
        """Soft-delete one of ``owner_id``'s Profiles and revoke its live
        tokens in one write; ``KeyError`` when absent, foreign or deleted."""
        now = normalize_timestamp(self.now())
        with self.database.write() as db:
            cursor = db.execute(
                "UPDATE agent_profiles SET status='revoked',deleted_at=%s,"
                "updated_at=%s WHERE id=%s AND owner_id=%s AND deleted_at IS NULL",
                (now, now, profile_id, owner_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(profile_id)
            db.execute(
                "UPDATE agent_access_tokens SET revoked_at=%s,token_plain=NULL "
                "WHERE agent_profile_id=%s AND revoked_at IS NULL",
                (now, profile_id),
            )

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
        allow_answer_links: bool = False,
    ) -> AgentTokenSummary:
        now = self.now()
        with self.database.write() as db:
            profile = db.execute(
                # FOR SHARE: a concurrent delete_agent_profile's UPDATE queues
                # behind this issue (and then revokes the new token), or this
                # waits for the delete and re-checks status -> KeyError.
                "SELECT name FROM agent_profiles "
                "WHERE id=%s AND owner_id=%s AND status='active' FOR SHARE",
                (agent_profile_id, owner_id),
            ).fetchone()
            if profile is None:
                raise KeyError(agent_profile_id)
            db.execute(
                "INSERT INTO agent_access_tokens "
                "(id,agent_profile_id,token_hash,scopes_json,default_notebook_id,"
                "expires_at,created_at,token_plain,allow_answer_links) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    token_id,
                    agent_profile_id,
                    token_hash,
                    jsonb(list(scopes)),
                    default_notebook_id,
                    normalize_timestamp(expires_at) if expires_at else None,
                    normalize_timestamp(now),
                    token_plain,
                    1 if allow_answer_links else 0,
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
        *, allow_answer_links: bool,
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
                "expires_at=%s,allow_answer_links=%s WHERE id=%s AND revoked_at IS NULL",
                (
                    jsonb(list(scopes)),
                    default_notebook_id,
                    normalize_timestamp(expires_at) if expires_at else None,
                    1 if allow_answer_links else 0,
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
                # Explicit columns: the stored plaintext (token_plain) is never read
                # on the authentication path, which compares the hash only.
                "SELECT t.id,t.agent_profile_id,t.token_hash,t.scopes_json,"
                "t.default_notebook_id,t.expires_at,t.revoked_at,t.last_used_at,"
                "t.created_at,t.allow_answer_links,"
                "p.owner_id,p.name AS profile_name,p.status AS profile_status,"
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

    def create_agent_notebook(
        self, token_id: str,
        authorize: Callable[[Mapping[str, Any] | None], None],
        create: Callable[[Any, str], str],
    ) -> str:
        with self.database.write() as db:
            # Profile first: profile deletion locks it before revoking tokens.
            db.execute(
                "SELECT p.id FROM agent_profiles p JOIN agent_access_tokens t "
                "ON t.agent_profile_id=p.id WHERE t.id=%s FOR UPDATE OF p",
                (token_id,),
            ).fetchone()
            row = db.execute(
                "SELECT t.agent_profile_id,t.scopes_json,t.expires_at,t.revoked_at,"
                "p.owner_id,p.status AS profile_status,u.status AS owner_status "
                "FROM agent_access_tokens t JOIN agent_profiles p "
                "ON p.id=t.agent_profile_id JOIN users u ON u.id=p.owner_id "
                "WHERE t.id=%s FOR UPDATE OF t FOR SHARE OF u", (token_id,),
            ).fetchone()
            authority = dict(row) if row is not None else None
            if authority is not None:
                authority["scopes_json"] = json.dumps(json_value(authority["scopes_json"], []))
                for field in ("expires_at", "revoked_at"):
                    authority[field] = iso_timestamp(authority[field], empty="") or None
            authorize(authority)
            notebook_id = create(db, authority["owner_id"])
            db.execute(
                "INSERT INTO agent_token_notebooks (token_id,notebook_id) VALUES (%s,%s)",
                (token_id, notebook_id),
            )
        return notebook_id

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
            suggested_signal=row["suggested_signal"] if "suggested_signal" in keys else None,
        )

    @staticmethod
    def _select_columns(alias: str = "m") -> str:
        return (
            f"{alias}.id,{alias}.notebook_id,{alias}.created_by,"
            f"{alias}.agent_profile_id,{alias}.source_answer_id,{alias}.origin,"
            f"{alias}.status,{alias}.promotion_state,{alias}.title,"
            f"{alias}.content_md,{alias}.tags_json,{alias}.confirmed_by,"
            f"{alias}.confirmed_at,{alias}.embedding_status,{alias}.embedding_error,"
            f"{alias}.created_at,{alias}.updated_at,{alias}.suggested_signal,p.payload_json"
        )

    @staticmethod
    def _origin_clause(origin: str, alias: str = "m") -> tuple[str, tuple]:
        """The list filter's origin predicate: the virtual ``suggested`` is
        "has a suggested_signal"; Ask answers are only non-suggested rows."""
        if origin == ORIGIN_SUGGESTED:
            return f"{alias}.suggested_signal IS NOT NULL", ()
        if origin == "ask_answer":
            return f"{alias}.origin=%s AND {alias}.suggested_signal IS NULL", (origin,)
        return f"{alias}.origin=%s", (origin,)

    @staticmethod
    def _read_access_clause(alias: str = "m") -> str:
        """读权谓词 + 笔记本存活谓词(删除中/拷贝中/导入中的库里的 Memory 读不到,
        经这里的写语句同样落空 → 404)。定义点在 `access_sql`,这里是薄封装。"""
        return live_read_access_exists_clause(alias)

    # 测试观察点:直写 Memory 行的构造点
    def insert_memory(self, write: MemoryWrite) -> MemoryRecord:
        with self.database.write() as db:
            item, _created = self._insert_memory_on(db, write)
        return item

    def notebook_content_overview(
        self, user_id: str, notebook_id: str, limit: int = 3
    ) -> dict[str, Any]:
        bounded_limit = max(1, min(3, int(limit)))
        with self.database.connect() as db:
            # "Your memories" (design §6.3): the original count, minus the
            # unadopted suggestions, in ONE statement -- the first half is the
            # pre-suggestion statement verbatim, the subtrahend reads the
            # partial idx_memory_suggest_unadopted; GREATEST guards against a
            # negative difference.
            counts = db.execute(
                "SELECT GREATEST(0,o.total-u.unadopted) AS total,o.confirmed,"
                "GREATEST(0,o.candidate-u.unadopted_candidate) AS candidate FROM ("
                "SELECT COUNT(*) AS total,"
                "COALESCE(SUM(CASE WHEN status='confirmed' THEN 1 ELSE 0 END),0) AS confirmed,"
                "COALESCE(SUM(CASE WHEN status='candidate' THEN 1 ELSE 0 END),0) AS candidate "
                "FROM memory_items WHERE created_by=%s AND notebook_id=%s) o,("
                "SELECT COUNT(*) AS unadopted,"
                "COALESCE(SUM(CASE WHEN us.status='candidate' THEN 1 ELSE 0 END),0) "
                "AS unadopted_candidate FROM memory_items us "
                "WHERE us.created_by=%s AND us.notebook_id=%s "
                f"AND {unadopted_suggestion('us')}) u",
                (user_id, notebook_id, user_id, notebook_id),
            ).fetchone()
            rows = db.execute(
                "SELECT id,title,status,updated_at FROM memory_items m "
                "WHERE created_by=%s AND notebook_id=%s "
                "AND status IN ('confirmed','candidate') "
                f"AND NOT {unadopted_suggestion('m')} "
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
            "status,title,content_md,tags_json,confirmed_by,confirmed_at,created_at,updated_at,"
            "suggested_signal) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
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
                write.suggested_signal,
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
                    "WHERE id=%s AND owner_id=%s AND status='active' FOR SHARE",
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

    def create_suggestions_with_initial_revision(
        self,
        *,
        notebook_id: str,
        owner_id: str,
        claim_token: str,
        writes: Sequence[MemoryWrite],
        proposed_fingerprints: Sequence[str],
        now: str,
        declined_fingerprints: Sequence[str] = (),
        declined_expires_at: str | None = None,
    ) -> SuggestionWriteResult:
        """Write one run's system suggestions (design §4.2 step 8) in ONE
        write transaction, re-checking under row locks that the run may still
        write. In order:

        1. read access with locks: the notebook row ``FOR SHARE`` (and live),
           then — unless ``owner_id`` owns it — the membership row or a grant
           edge ``FOR SHARE`` (the three-segment probe; a removal or exit
           committing first makes this fail, one starting later waits);
        2. the owner's switch: ``memory_suggest_users`` ``enabled = 1``
           ``FOR SHARE``;
        3. the job row ``FOR UPDATE``: still ``running`` under
           ``claim_token`` (turning the switch off, an exit or a loss of read
           access deletes it; a stale run sees another token).
           Lock order switch row before job row, as ``note_signal`` (B11).

        Any of them failing raises ``SuggestionRevoked(reason)`` and nothing
        is written. Then 4. the ``proposed`` fingerprints and the
        ``declined`` negative cache expiring ``declined_expires_at`` (the same
        upserts as ``MemorySuggestStore.write_fingerprints``, design §5.1,
        stamped ``now``; in this transaction, after the lock checks, so a
        revoked run caches nothing -- review P3-2), and 5. each write as a ``candidate`` row with its
        provenance and one revision (reason ``suggested``, changed by the
        owner, G12). A write whose ``source_answer_id`` the owner already has
        a memory of (``idx_memory_answer_once``) is skipped and counted in
        ``deduped`` — the transaction goes on.

        Every write must be a ``candidate`` ``ask_answer`` row of this owner
        and notebook with a ``suggested_signal``; a ``clarified`` one binds no
        answer (``source_answer_id`` None). Anything else is a ``ValueError``
        before the transaction starts."""
        _check_suggestion_writes(writes, notebook_id=notebook_id, owner_id=owner_id)
        if not claim_token:
            raise ValueError("claim_token must be non-empty")
        fingerprints = list(dict.fromkeys(str(value) for value in proposed_fingerprints if value))
        declined = list(dict.fromkeys(str(value) for value in declined_fingerprints if value))
        if declined and not declined_expires_at:
            raise ValueError("declined fingerprints need an expiry")
        created: list[MemoryRecord] = []
        deduped = 0
        with self.database.write() as db:
            self._lock_suggestion_scope_on(db, notebook_id, owner_id, claim_token)
            if fingerprints:
                execute_many(
                    db,
                    PROPOSED_FINGERPRINT_UPSERT_SQL,
                    [
                        (notebook_id, owner_id, value, normalize_timestamp(now))
                        for value in fingerprints
                    ],
                )
            if declined:
                execute_many(
                    db,
                    DECLINED_FINGERPRINT_UPSERT_SQL,
                    [
                        (
                            notebook_id, owner_id, value,
                            normalize_timestamp(declined_expires_at),
                            normalize_timestamp(now),
                        )
                        for value in declined
                    ],
                )
            for write in writes:
                item, inserted = self._insert_memory_on(db, write)
                if not inserted:
                    deduped += 1
                    continue
                self._ensure_initial_revision_on(
                    db, item, True, owner_id, REVISION_REASON_SUGGESTED
                )
                created.append(item)
        return SuggestionWriteResult(created=tuple(created), deduped=deduped)

    @staticmethod
    def _lock_suggestion_scope_on(
        db: object, notebook_id: str, owner_id: str, claim_token: str
    ) -> None:
        """Steps 1–3 of ``create_suggestions_with_initial_revision``. Read
        access is locked through the same ``lock_live_reader_on`` chain as
        ``note_signal`` (live notebook row, then the membership row or the
        grant edge -- with its group membership row -- ``FOR SHARE``), so a
        concurrent removal, grant delete or group leave serialises with
        this write; then the switch row, then the job row (B11 lock order)."""
        readable = lock_live_reader_on(db, notebook_id, owner_id)
        if readable is None:
            raise SuggestionRevoked(SKIP_NOTEBOOK_UNAVAILABLE)
        if not readable:
            raise SuggestionRevoked(SKIP_NO_READ_ACCESS)
        switch = db.execute(
            "SELECT 1 FROM memory_suggest_users WHERE owner_id=%s AND enabled=1 FOR SHARE",
            (owner_id,),
        ).fetchone()
        if switch is None:
            raise SuggestionRevoked(SKIP_USER_DISABLED)
        job = db.execute(
            "SELECT status, claim_token FROM memory_suggest_jobs "
            "WHERE notebook_id=%s AND owner_id=%s FOR UPDATE",
            (notebook_id, owner_id),
        ).fetchone()
        if (
            job is None
            or job["status"] != JOB_STATUS_RUNNING
            or job["claim_token"] != claim_token
        ):
            raise SuggestionRevoked(REVOKED_CLAIM_LOST)

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
        return self._save_answer(write, changed_by, reason, replace_rejected=False)

    def replace_rejected_suggestion_with_answer(
        self, write: MemoryWrite, changed_by: str, reason: str
    ) -> MemoryRecord:
        """Design §5.3 row 2 / D16: "save to Memory" on an answer whose
        memory is the caller's DISMISSED system suggestion (``rejected``, with
        a ``suggested_signal``) replaces it. One write transaction — the same
        one ``create_answer_with_initial_revision`` runs, with the same access
        and answer-ownership checks and the same insert: the old row is
        deleted (its provenance and revisions cascade; a dismissed suggestion
        never had a vector, a derived source or a promotion) and ``write`` is
        inserted as a plain manual memory (pass it with ``suggested_signal``
        None). Fingerprints are not touched (``proposed`` is permanent).

        When the existing row is anything else by the time this transaction
        locks it (an open suggestion, an adopted one, a manual or agent
        memory — including the manual one a concurrent replace just wrote),
        this is a no-op returning that row, exactly like
        ``create_answer_with_initial_revision``; with no row at all it is a
        plain save. Errors are those of ``create_answer_with_initial_revision``
        (``KeyError`` = no access / not the answer's author → 404,
        ``ValueError`` = a concurrent access change → 409).

        Lock order (review P3-3): because it deletes a Memory row, read access
        (notebook, then membership or grant, ``FOR SHARE``) is locked BEFORE
        the existing row ``FOR UPDATE`` -- the order of ``finish_member_exit``
        -- so the two queue instead of deadlocking. A caller without access
        therefore gets ``KeyError`` even where a row exists."""
        return self._save_answer(write, changed_by, reason, replace_rejected=True)

    def _save_answer(
        self, write: MemoryWrite, changed_by: str, reason: str, *, replace_rejected: bool
    ) -> MemoryRecord:
        if not write.source_answer_id:
            raise KeyError("source_answer_id")
        try:
            with self.database.write(isolation_level="serializable") as db:
                if replace_rejected:
                    # The replace DELETEs a Memory row, so it takes the
                    # canonical order notebook → membership → memory row
                    # (finish_member_exit's, which deletes drafts under the
                    # membership lock): read access first, the row after
                    # (review P3-3). The plain save keeps its order: it only
                    # ever locks an existing row to return it at once.
                    self._lock_answer_save_access_on(db, write)
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
                    if not (
                        replace_rejected
                        and item.suggested_signal
                        and item.status == "rejected"
                    ):
                        return item
                    # Rolled back with everything else if a check below fails.
                    db.execute(
                        "DELETE FROM memory_items WHERE id=%s AND created_by=%s",
                        (item.id, write.created_by),
                    )
                if not replace_rejected:
                    self._lock_answer_save_access_on(db, write)
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
    def _lock_answer_save_access_on(db: Any, write: MemoryWrite) -> None:
        """Read access of a save from an answer, locked: the notebook row,
        then -- unless the saver owns it -- the membership row or a grant
        edge, each ``FOR SHARE``; ``KeyError`` (404) without access."""
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
                    grant_probe_params(write.notebook_id, write.created_by),
                ).fetchone()
        if notebook is None or (
            notebook["created_by"] != write.created_by and granted is None
        ):
            raise KeyError(write.source_answer_id)

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

    def memory_for_user(
        self,
        memory_id: str,
        user_id: str,
        *,
        live_only: bool = True,
        delete_window_now: Any = None,
    ) -> MemoryRecord:
        # live_only=False 只给「这行 Memory 还在不在」的存在性复读用(抽图作业
        # 收尾);读权照旧,只是不看笔记本存活——删库期间不能把它误判成已删。
        # delete_window_now:删除窗口期「转移到我的笔记本」读源,见 SQLite 那一份。
        params: tuple[Any, ...] = (memory_id, user_id, *read_access_params(user_id))
        if delete_window_now is not None:
            access = export_access_exists_clause("m")
            params += (utc_instant(delete_window_now),)
        elif live_only:
            access = self._read_access_clause()
        else:
            access = read_access_exists_clause("m")
        with self.database.connect() as db:
            row = db.execute(
                f"SELECT {self._select_columns()} FROM memory_items m "
                "LEFT JOIN memory_provenance p ON p.memory_id=m.id "
                f"WHERE m.id=%s AND m.created_by=%s AND {access}",
                params,
            ).fetchone()
        if row is None:
            raise KeyError(memory_id)
        return self._record(row)

    # ---------------------------------------------- the deletion window's exits

    @staticmethod
    def _delete_window_row_on(db: Any, notebook_id: str, user_id: str, now: Any) -> Any:
        """See the SQLite twin."""
        return db.execute(
            "SELECT nb.name AS name, nb.status AS status, "
            "nb.delete_after AS delete_after, "
            f"({read_access_clause('nb', 'nm')}) AS readable, "
            f"(nb.{NOTEBOOK_LIVE_SQL}) AS live, "
            f"({notebook_delete_window_open_sql('nb')}) AS window_open "
            "FROM notebooks nb WHERE nb.id=%s",
            (*read_access_params(user_id), now, notebook_id),
        ).fetchone()

    def delete_window_state(
        self, notebook_id: str, user_id: str, *, now: Any
    ) -> str | None:
        """See the SQLite twin."""
        with self.database.connect() as db:
            row = self._delete_window_row_on(
                db, notebook_id, user_id, utc_instant(now)
            )
        return classify_delete_window(row)

    def delete_window_failure(
        self, notebook_id: str, user_id: str, *, now: Any
    ) -> str | None:
        """See the SQLite twin."""
        with self.database.connect() as db:
            row = self._delete_window_row_on(
                db, notebook_id, user_id, utc_instant(now)
            )
        return delete_window_failure_code(row)

    @staticmethod
    def _transferable_where(after: str | None) -> str:
        return (
            "m.notebook_id=%s AND m.created_by=%s AND m.status='confirmed' "
            "AND m.promotion_state<>'proposed' AND "
            + export_access_exists_clause("m")
            + (' AND m.id COLLATE "C" > %s' if after is not None else "")
        )

    def delete_window_transferable_ids(
        self, notebook_id: str, user_id: str, *, now: Any,
        after: str | None = None, limit: int | None = None,
    ) -> list[str]:
        """See the SQLite twin (byte order, ``COLLATE "C"``, on both)."""
        params: list[Any] = [
            notebook_id, user_id, *read_access_params(user_id), utc_instant(now),
        ]
        if after is not None:
            params.append(after)
        sql = (
            "SELECT m.id FROM memory_items m WHERE "
            + self._transferable_where(after) + ' ORDER BY m.id COLLATE "C"'
        )
        if limit is not None:
            sql += " LIMIT %s"
            params.append(int(limit))
        with self.database.connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [row["id"] for row in rows]

    def delete_window_transferable_count(
        self, notebook_id: str, user_id: str, *, now: Any, after: str | None = None
    ) -> int:
        """See the SQLite twin."""
        params: list[Any] = [
            notebook_id, user_id, *read_access_params(user_id), utc_instant(now),
        ]
        if after is not None:
            params.append(after)
        with self.database.connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS c FROM memory_items m WHERE "
                + self._transferable_where(after),
                params,
            ).fetchone()
        return int(row["c"])

    def delete_window_transfer_claim(self, notebook_id: str, user_id: str):
        """See the SQLite twin. PostgreSQL's claim is a session advisory lock
        on a dedicated connection, so it holds across processes."""
        return self.database.try_claim(
            f"memory-transfer\x1f{user_id}\x1f{notebook_id}"
        )

    def memory_row_exists(self, memory_id: str) -> bool:
        """See the SQLite twin."""
        with self.database.connect() as db:
            row = db.execute(
                "SELECT 1 AS one FROM memory_items WHERE id=%s", (memory_id,)
            ).fetchone()
        return row is not None

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
                f"AND NOT {unadopted_suggestion('m')} "
                f"AND {self._read_access_clause()}",
                (notebook_id, user_id, *unique_ids, *read_access_params(user_id)),
            ).fetchall()
        return {str(row["source_answer_id"]): str(row["id"]) for row in rows}

    def answer_memory_link_rows(
        self, notebook_id: str, user_id: str, answer_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        """One read behind ``answer-memory-links``: ``links`` maps an answer
        to the user's memory saved from it, leaving unadopted suggestions out
        (exactly ``answer_memory_links``); ``states`` carries every such row,
        drafts included, as ``{memory_id, status, suggested}``."""
        unique_ids = list(
            dict.fromkeys(str(answer_id) for answer_id in answer_ids if answer_id)
        )
        if not unique_ids:
            return {"links": {}, "states": {}}
        if len(unique_ids) > 200:
            raise ValueError("answer_ids may contain at most 200 unique values")
        placeholders = ",".join("%s" for _ in unique_ids)
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT m.source_answer_id,m.id,m.status,m.suggested_signal,"
                f"{unadopted_suggestion('m')} AS unadopted FROM memory_items m "
                "WHERE m.notebook_id=%s AND m.created_by=%s "
                f"AND m.source_answer_id IN ({placeholders}) "
                f"AND {self._read_access_clause()}",
                (notebook_id, user_id, *unique_ids, *read_access_params(user_id)),
            ).fetchall()
        links: dict[str, str] = {}
        states: dict[str, dict[str, Any]] = {}
        # The answer index normally admits one row. If a projection ever
        # supplies duplicates, choose an adopted row first and break ties
        # by identity, so links and states always describe the same memory.
        for row in sorted(rows, key=lambda row: (
            str(row["status"]) not in ("confirmed", "deprecated"),
            bool(row["unadopted"]), str(row["id"]),
        )):
            answer_id = str(row["source_answer_id"])
            if answer_id in states:
                continue
            states[answer_id] = {
                "memory_id": str(row["id"]),
                "status": str(row["status"]),
                "suggested": row["suggested_signal"] is not None,
            }
            if not row["unadopted"]:
                links[answer_id] = str(row["id"])
        return {"links": links, "states": states}

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
        resync_children: bool = False,
    ) -> MemoryRecord:
        with self.database.write() as db:
            row = self._lock_memory_aggregate_on(
                db, memory_id, expected_creator=user_id, require_live=True
            )
            if row["status"] not in expected:
                destination = target or row["status"]
                raise ValueError(
                    f"invalid memory transition: {row['status']} -> {destination}"
                )
            self._write_mutation_on(
                db, row, user_id, fields=fields, expected=expected, target=target,
                changed_by=changed_by, reason=reason, resync_children=resync_children,
            )
        # The write above already checked the notebook is live under its lock;
        # a delete committing since must not turn a landed write into a 404.
        return self.memory_for_user(memory_id, user_id, live_only=False)

    def _write_mutation_on(
        self,
        db: object,
        row: Mapping[str, Any],
        user_id: str,
        *,
        fields: Mapping[str, Any],
        expected: set[str],
        target: str | None,
        changed_by: str,
        reason: str,
        resync_children: bool,
    ) -> None:
        """The UPDATE and its revision for a row the caller locked through
        ``_lock_memory_aggregate_on`` and whose status it checked against
        ``expected``. ``resync_children``: see ``_resync_suggestion_children_on``."""
        memory_id = str(row["id"])
        allowed = {"title", "content_md", "tags"}
        values = {key: value for key, value in fields.items() if key in allowed}
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
        if resync_children and row["suggested_signal"]:
            self._resync_suggestion_children_on(db, memory_id)

    @staticmethod
    def _resync_suggestion_children_on(db: object, memory_id: str) -> int:
        """D15(a) re-send at adoption (design §6.6 (a) 2): one no-op UPDATE of
        this Memory's provenance row and of every revision row, so the change
        capture trigger (``0064``: ``AFTER INSERT OR DELETE OR UPDATE``, no
        ``WHEN``) logs an upsert for each and the next incremental package
        carries the children a suggestion's first export filtered out. Skipped
        while ``sync_capture_control`` is closed (the triggers would log
        nothing; the UPDATEs would only be wasted writes). Returns how many
        rows were touched."""
        gate = db.execute(
            "SELECT 1 FROM sync_capture_control WHERE singleton = 1 AND enabled"
        ).fetchone()
        if gate is None:
            return 0
        touched = db.execute(
            "UPDATE memory_provenance SET id = id WHERE memory_id = %s", (memory_id,)
        ).rowcount
        touched += db.execute(
            "UPDATE memory_revisions SET id = id WHERE memory_id = %s", (memory_id,)
        ).rowcount
        return int(touched)

    def _lock_memory_aggregate_on(
        self,
        db: object,
        memory_id: str,
        *,
        expected_creator: str | None = None,
        expected_notebook: str | None = None,
        permission_error: bool = False,
        require_live: bool = False,
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
        # require_live: the owner's edit/confirm/reject/deprecate refuses a
        # Memory whose notebook is being deleted/copied/imported BEFORE writing
        # (KeyError -> 404), the same lifecycle rule as NOTEBOOK_WRITE_SQL and
        # the live read gate ``_read_access_clause`` that answers afterwards.
        live = f" AND {NOTEBOOK_LIVE_SQL}" if require_live else ""
        notebook = db.execute(
            f"SELECT created_by FROM notebooks WHERE id=%s{live} FOR SHARE",
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
        self,
        db: object,
        memory_id: str,
        candidate_notebook_id: str,
        *,
        require_live: bool = False,
    ) -> MemoryRecord:
        # require_live: approving a promotion materializes this Memory into a
        # base library, so a notebook being deleted/copied/imported refuses it
        # (KeyError -> 404). The FOR SHARE lock on that notebook row holds to
        # commit, so a delete cannot land between this check and the copy.
        row = self._lock_memory_aggregate_on(
            db,
            memory_id,
            expected_notebook=candidate_notebook_id,
            permission_error=True,
            require_live=require_live,
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
        resync_children: bool = False,
    ) -> MemoryRecord:
        """``resync_children=True`` (the adoption entries only): when the row
        is a system suggestion and change capture is open, also touch its
        provenance and revision rows in this transaction
        (``_resync_suggestion_children_on``)."""
        return self._mutate_with_revision(
            memory_id,
            user_id,
            fields=fields or {},
            expected=expected,
            target=target,
            changed_by=changed_by,
            reason=reason,
            resync_children=resync_children,
        )

    def adopt_suggestion_from_answer(
        self,
        notebook_id: str,
        user_id: str,
        answer_id: str,
        *,
        fields: Mapping[str, Any],
        changed_by: str,
        reason: str,
    ) -> MemoryRecord | None:
        """Design §5.3 row 1: "save to Memory" on an answer that holds the
        caller's open system suggestion adopts THAT row. One write
        transaction: lock the row like ``confirm`` does
        (``_lock_memory_aggregate_on``, notebook live), compare-and-set
        ``candidate -> confirmed`` with the submitted ``fields``
        (title / content_md / tags), append the revision and re-send the
        children (``resync_children``) — exactly the store half of
        ``transition_with_revision(..., resync_children=True)``.

        Returns the confirmed record, or ``None`` when there is nothing to
        adopt: no row of this caller binds ``answer_id``, or the row is not
        (any more) an open suggestion of ``notebook_id`` — a plain memory, an
        agent candidate, an adopted or dismissed suggestion, another
        notebook. ``None`` writes nothing; the caller re-reads
        ``memory_by_answer`` and takes the matching row of the §5.3 table.
        ``KeyError`` when the caller lost read access or the notebook is not
        live (the same 404 as ``confirm``)."""
        with self.database.write() as db:
            routing = db.execute(
                "SELECT id FROM memory_items WHERE created_by=%s AND source_answer_id=%s",
                (user_id, answer_id),
            ).fetchone()
            if routing is None:
                return None
            row = self._lock_memory_aggregate_on(
                db, routing["id"], expected_creator=user_id, require_live=True
            )
            if (
                row["status"] != "candidate"
                or not row["suggested_signal"]
                or row["notebook_id"] != notebook_id
                or row["source_answer_id"] != answer_id
            ):
                return None
            self._write_mutation_on(
                db, row, user_id, fields=fields, expected={"candidate"},
                target="confirmed", changed_by=changed_by, reason=reason,
                resync_children=True,
            )
        return self.memory_for_user(routing["id"], user_id, live_only=False)

    # 测试观察点:修订历史观察点(生产不读修订列表)
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

    # 测试观察点:直写 Memory 行的构造点
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
        # The write above already checked the notebook is live under its lock;
        # a delete committing since must not turn a landed write into a 404.
        return self.memory_for_user(memory_id, user_id, live_only=False)

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
        # The write above already checked the notebook is live under its lock;
        # a delete committing since must not turn a landed write into a 404.
        return self.memory_for_user(memory_id, user_id, live_only=False)

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

    def delete_open_suggestions(
        self, user_id: str, memory_ids: Sequence[str]
    ) -> list[str]:
        """``bulk_delete_memories`` restricted to OPEN system suggestions
        (``suggested_signal`` set, ``status='candidate'``), judged on the
        locked row: one adopted or dismissed meanwhile is left alone. Open
        suggestions are never embedded nor extracted (only adoption does
        that), so no derived row exists to remove first. Returns the ids
        deleted."""
        unique = _bulk_memory_ids(memory_ids)
        if not unique:
            return []
        with self.database.write() as db:
            return list(
                self._hard_delete_on(db, user_id, unique, open_suggestions_only=True)
            )

    def _hard_delete_on(
        self,
        db: object,
        user_id: str,
        memory_ids: Sequence[str],
        *,
        open_suggestions_only: bool = False,
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
        # ``open_suggestions_only``: the status test sits on the locked row
        # (FOR UPDATE re-checks it against the latest version).
        only_open = (
            " AND suggested_signal IS NOT NULL AND status='candidate'"
            if open_suggestions_only else ""
        )
        rows = db.execute(
            "SELECT id,notebook_id FROM memory_items "
            f"WHERE created_by=%s AND id=ANY(%s){only_open} ORDER BY id FOR UPDATE",
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
        """Read disclosure or claim only personal-memory ids; never mutate.

        Suggestions are counted separately, then deleted only by a successful
        finish that still leaves the user without read access. Adoption after
        this claim consequently keeps that memory and makes exit incomplete.
        """
        with (self.database.write() if claim else self.database.connect()) as db:
            token, keeps = self._member_exit_state_on(db, notebook_id, user_id, lock=claim)
            return self._exit_memory_snapshot_on(db, notebook_id, user_id, token, keeps, claim)

    # ------------------------------------------------ 退出协作(分享编辑 P1)
    @staticmethod
    def _exit_memory_snapshot_on(
        db: object, notebook_id: str, user_id: str, token: Any, keeps: bool, claim: bool
    ) -> MemberExitSnapshot:
        if token is None or keeps:
            return MemberExitSnapshot(token is not None, keeps, 0, membership_token=token)
        if claim:
            rows = db.execute(
                f"SELECT m.id, {unadopted_suggestion('m')} AS unadopted FROM memory_items m "
                "WHERE m.notebook_id=%s AND m.created_by=%s ORDER BY m.created_at,m.id",
                (notebook_id, user_id),
            ).fetchall()
            ids = tuple(row["id"] for row in rows if not row["unadopted"])
            suggestions = sum(1 for row in rows if row["unadopted"])
            return MemberExitSnapshot(True, False, len(ids), ids, token, suggestions)
        counts = MemoryStore._exit_memory_counts(db, notebook_id, user_id)
        return MemberExitSnapshot(True, False, counts.memory_count, suggestion_count=counts.suggestion_count)

    @staticmethod
    def _exit_memory_counts(db: object, notebook_id: str, user_id: str) -> MemberExitCounts:
        """Count personal records and unadopted drafts in one snapshot."""
        row = db.execute(
            "SELECT (SELECT COUNT(*) FROM memory_items WHERE notebook_id=%s AND created_by=%s) AS c, "
            "(SELECT COUNT(*) FROM memory_items us WHERE us.created_by=%s AND us.notebook_id=%s "
            f"AND {unadopted_suggestion('us')}) AS s",
            (notebook_id, user_id, user_id, notebook_id),
        ).fetchone()
        suggestions = int(row["s"])
        return MemberExitCounts(max(0, int(row["c"]) - suggestions), suggestions)

    def _finish_memory_exit_on(
        self, db: object, notebook_id: str, user_id: str, keeps: bool, allow_suggestion_delete: bool
    ) -> MemberExitCompletion:
        """Held access locks serialize adoption and last-edge removal."""
        if keeps:
            return MemberExitCompletion(0, True, keeps_access=True)
        counts = self._exit_memory_counts(db, notebook_id, user_id)
        if counts.memory_count or (counts.suggestion_count and not allow_suggestion_delete):
            return MemberExitCompletion(
                counts.memory_count, False, remaining_suggestion_count=counts.suggestion_count
            )
        deleted = db.execute(
            "DELETE FROM memory_items WHERE notebook_id=%s AND created_by=%s "
            f"AND {unadopted_suggestion('memory_items')}",
            (notebook_id, user_id),
        ).rowcount
        return MemberExitCompletion(0, True, deleted_suggestion_count=int(deleted))

    @staticmethod
    def _clear_exit_suggest_metadata_on(db: object, notebook_id: str, user_id: str) -> None:
        """After the last-edge fence: jobs, then fingerprints, like account cleanup."""
        db.execute(
            "DELETE FROM memory_suggest_jobs WHERE notebook_id=%s AND owner_id=%s",
            (notebook_id, user_id),
        )
        db.execute(
            "DELETE FROM memory_suggest_fingerprints WHERE notebook_id=%s AND owner_id=%s",
            (notebook_id, user_id),
        )

    @staticmethod
    def _grant_exit_state_on(
        db: object, notebook_id: str, user_id: str, *, lock: bool
    ) -> tuple[Any, bool]:
        """``(grant_id, keeps_access)`` for the user's own named grant
        (``principal_type='user'``) on this notebook; ``(None, False)`` without
        one. ``keeps_access`` is the read predicate with THAT grant row taken
        out (``read_access_except_grant_clause``, PD-13): ownership, a
        membership row or any other grant still count. Like the member exit's
        own check it ignores the lifecycle.

        ``lock`` takes the grant row ``FOR UPDATE``: every Memory write by a
        reader holding a named grant holds that row ``FOR SHARE`` for its whole
        transaction (the locked grant probe of ``access_sql``), so while it is held no
        Memory of this user can be created or changed here."""
        grant = db.execute(
            "SELECT id FROM notebook_grants WHERE notebook_id=%s "
            "AND principal_type='user' AND principal_id=%s"
            + (" FOR UPDATE" if lock else ""),
            (notebook_id, user_id),
        ).fetchone()
        if grant is None:
            return None, False
        keeps = db.execute(
            "SELECT 1 FROM notebooks nb WHERE nb.id=%s AND "
            + read_access_except_grant_clause(),
            read_except_grant_params(notebook_id, user_id, grant["id"]),
        ).fetchone() is not None
        return grant["id"], keeps

    def grant_exit_snapshot(
        self, notebook_id: str, user_id: str, *, claim: bool
    ) -> MemberExitSnapshot:
        """Read disclosure or claim only personal-memory ids; never mutate.

        Suggestions are counted separately, then deleted only by a successful
        finish that still leaves the user without read access. Adoption after
        this claim consequently keeps that memory and makes exit incomplete.
        """
        with (self.database.write() if claim else self.database.connect()) as db:
            token, keeps = self._grant_exit_state_on(db, notebook_id, user_id, lock=claim)
            return self._exit_memory_snapshot_on(db, notebook_id, user_id, token, keeps, claim)

    @staticmethod
    def _lock_exit_edges_on(db: object, notebook_id: str, user_id: str) -> None:
        """Both self-exits exclusively lock root, membership, then named grant. Locking
        only the edge being removed lets each exit observe the other one and
        both commit without the final Memory disclosure. The same root also
        serializes independent group/grant read-path revocations."""
        lock_read_changes_on(db, [notebook_id])
        db.execute(
            "SELECT added_at FROM notebook_members "
            "WHERE notebook_id=%s AND user_id=%s FOR UPDATE",
            (notebook_id, user_id),
        ).fetchone()
        db.execute(
            "SELECT id FROM notebook_grants WHERE notebook_id=%s "
            "AND principal_type='user' AND principal_id=%s FOR UPDATE",
            (notebook_id, user_id),
        ).fetchone()

    def finish_grant_exit(
        self, notebook_id: str, user_id: str, grant_token: Any, *,
        allow_suggestion_delete: bool = True,
    ) -> MemberExitCompletion:
        """End only the claimed edge, with common-lock authority revalidation.

        A surviving read path preserves every memory and its per-member state.
        Otherwise no adopted/personal memory may remain. A caller that has not
        acknowledged an exit cannot silently delete newly discovered drafts.
        """
        with self.database.write() as db:
            lock_mode_read_period_on(db)
            self._lock_exit_edges_on(db, notebook_id, user_id)
            token, keeps = self._grant_exit_state_on(db, notebook_id, user_id, lock=False)
            if token is None or token != grant_token:
                return MemberExitCompletion(0, False)
            completion = self._finish_memory_exit_on(
                db, notebook_id, user_id, keeps, allow_suggestion_delete
            )
            if completion.ended:
                db.execute(
                    "DELETE FROM notebook_grants WHERE id=%s", (grant_token,),
                )
                record_read_loss_on(db, [(notebook_id, user_id)], now=self.now())
                if not completion.keeps_access:
                    self._clear_exit_suggest_metadata_on(db, notebook_id, user_id)
        return completion

    def group_exit_memory_preview(
        self, group_id: str, user_id: str, *, now: Any = None
    ) -> list[dict[str, Any]]:
        """What leaving ``group_id`` hides of ``user_id``'s own Memory: the
        notebooks the user reads now (``read_access_clause``) but not once
        their ``group_members`` row in that group is gone
        (``read_access_after_group_exit_clause``: ownership, a membership
        row, a personal or everyone grant and grants through OTHER groups
        still count), each with how many of "your memories" it holds: every
        status except the unadopted system suggestions (design §6.3).
        Leaving deletes nothing, so this is a plain read. Notebooks without
        any of your memories are not listed; a non-member gets nothing (the
        ``gm`` row drives the query). One statement: the original per-notebook
        count (the pre-suggestion subquery verbatim, through
        ``idx_memory_owner_notebook_status``) LEFT JOIN the per-notebook
        unadopted count (through the partial ``idx_memory_suggest_unadopted``)
        and subtract; notebooks whose difference is 0 drop out before either
        access predicate runs.

        Notebooks inside their deletion window are listed too, flagged
        ``pending_delete`` with their ``delete_after`` (ruling B12; SQLite
        twin)."""
        readable_now = read_access_clause("nb", "nm", user_ref="gm.user_id")
        readable_after = read_access_after_group_exit_clause(
            "nb", "xnm", user_ref="gm.user_id", group_ref="gm.group_id",
            grant_alias="xg", group_alias="xgm", group_admin_alias="xga",
        )
        instant = utc_instant(self.now() if now is None else now)
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT nb.id AS notebook_id, nb.name AS name, "
                "nb.status AS status, nb.delete_after AS delete_after, "
                "own.memory_count AS memory_count FROM group_members gm "
                "CROSS JOIN (SELECT al.notebook_id, "
                "al.memory_count - COALESCE(dr.unadopted, 0) AS memory_count FROM ("
                "SELECT notebook_id, COUNT(*) AS memory_count "
                "FROM memory_items WHERE created_by=%s GROUP BY notebook_id) al "
                "LEFT JOIN (SELECT us.notebook_id, COUNT(*) AS unadopted "
                "FROM memory_items us WHERE us.created_by=%s "
                f"AND {unadopted_suggestion('us')} GROUP BY us.notebook_id) dr "
                "ON dr.notebook_id=al.notebook_id "
                "WHERE al.memory_count - COALESCE(dr.unadopted, 0) > 0) own "
                "JOIN notebooks nb ON nb.id=own.notebook_id "
                "WHERE gm.group_id=%s AND gm.user_id=%s "
                f"AND (nb.{NOTEBOOK_LIVE_SQL} OR "
                f"{notebook_delete_window_open_sql('nb')}) AND {readable_now} "
                f"AND NOT {readable_after}",
                (user_id, user_id, group_id, user_id, instant),
            ).fetchall()
        return [
            {
                "notebook_id": row["notebook_id"],
                "name": row["name"],
                "memory_count": int(row["memory_count"]),
                "pending_delete": row["status"] == NOTEBOOK_PENDING_DELETE_STATUS,
                "delete_after": (
                    instant_text(row["delete_after"])
                    if row["status"] == NOTEBOOK_PENDING_DELETE_STATUS
                    else None
                ),
            }
            for row in rows
        ]

    def finish_member_exit(
        self, notebook_id: str, user_id: str, membership_token: Any, *,
        allow_suggestion_delete: bool = True,
    ) -> MemberExitCompletion:
        """End only the claimed edge, with common-lock authority revalidation.

        A surviving read path preserves every memory and its per-member state.
        Otherwise no adopted/personal memory may remain. A caller that has not
        acknowledged an exit cannot silently delete newly discovered drafts.
        """
        with self.database.write() as db:
            lock_mode_read_period_on(db)
            self._lock_exit_edges_on(db, notebook_id, user_id)
            token, keeps = self._member_exit_state_on(db, notebook_id, user_id, lock=False)
            if token is None or token != membership_token:
                return MemberExitCompletion(0, False)
            completion = self._finish_memory_exit_on(
                db, notebook_id, user_id, keeps, allow_suggestion_delete
            )
            if completion.ended:
                db.execute(
                    "DELETE FROM notebook_members WHERE notebook_id=%s AND user_id=%s",
                    (notebook_id, user_id),
                )
                record_read_loss_on(db, [(notebook_id, user_id)], now=self.now())
                if not completion.keeps_access:
                    self._clear_exit_suggest_metadata_on(db, notebook_id, user_id)
        return completion

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
        bridge_canonical_ids_of: Callable[[list[dict]], dict[int, list[str]]],
    ) -> dict[str, list[str]]:
        """The Memory-specific half of a purge page, inside the teardown's
        transaction (``SourceIngestionService.remove_memory_sources`` calls it
        after locking ``sources`` — rows of ``{id, notebook_id}`` — and before
        the generic teardown). Per notebook, in batched statements:

        * (the merge half — lock the rows the teardown writes, settle the
          objects merged into the sources' objects, strip the sources'
          evidence from objects ANOTHER source owns — has already run:
          ``GovernanceStore.detach_memory_merges_on``, wired as the
          service's ``memory_evidence_detach``, so the teardown deletes only
          what the Memory itself minted and loses no other Memory's
          knowledge);
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
        for notebook_id in sorted(by_notebook):
            source_ids = sorted(by_notebook[notebook_id])
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
        self,
        notebook_id: str,
        user_id: str,
        *,
        page_size: int,
        max_bytes: int,
        now: Any = None,
    ) -> MemoryExportSnapshot:
        """This user's own Memory in one notebook -- "your memories": every
        status except the unadopted system suggestions, whose number the same
        snapshot reports as ``omitted_suggestion_count`` (design §6.3) --
        oldest first (``created_at``, then id), read in ONE read-only
        REPEATABLE READ transaction: the counts and every keyset page see the
        same snapshot,
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
        items).

        The snapshot's first statement classifies the notebook for the
        deletion window (SQLite twin has the contract): ``KeyError`` /
        ``DeleteWindowClosedError``, or the items with the title and (inside
        a window) the deadline read in this same snapshot. A promotion that
        commits after the snapshot began cannot shorten the file."""
        page = max(1, min(int(page_size), 500))
        instant = utc_instant(self.now() if now is None else now)
        with self.database.connect() as db:
            db.read_only = True
            db.isolation_level = IsolationLevel.REPEATABLE_READ
            standing = self._delete_window_row_on(db, notebook_id, user_id, instant)
            state = classify_delete_window(standing)
            if state is None:
                raise KeyError(notebook_id)
            if state == WINDOW_CLOSED:
                raise DeleteWindowClosedError(notebook_id)
            where, params = self._export_where(notebook_id, user_id, instant)
            total = int(db.execute(
                f"SELECT count(*) AS n FROM memory_items m WHERE {where}", params
            ).fetchone()["n"])
            # Drafts are not exported (design §6.3); how many were left out
            # is read in the same snapshot for the export header.
            omitted_where, omitted_params = self._export_where(
                notebook_id, user_id, instant, unadopted=True
            )
            omitted = int(db.execute(
                f"SELECT count(*) AS n FROM memory_items m WHERE {omitted_where}",
                omitted_params,
            ).fetchone()["n"])
            items: list[MemoryRecord] = []
            size = 0
            after: tuple[Any, str] | None = None
            while True:
                rows, after = self._export_page_on(
                    db, notebook_id, user_id, after, page, instant
                )
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
        return MemoryExportSnapshot(
            title=str(standing["name"] or ""),
            delete_after=(
                instant_text(standing["delete_after"]) if state == WINDOW_OPEN else None
            ),
            items=tuple(items),
            omitted_suggestion_count=omitted,
        )

    def _export_where(
        self, notebook_id: str, user_id: str, now: Any, *, unadopted: bool = False
    ) -> tuple[str, list[Any]]:
        # 读权 ∧ (存活 ∨ 删除窗口开放):窗口期内仍可导出本人记忆。未采纳的系统
        # 建议不导出;``unadopted=True`` 反过来只取它们(导出头里的「另有 N 条」)。
        clauses = [
            "m.notebook_id=%s", "m.created_by=%s", export_access_exists_clause("m"),
            unadopted_suggestion("m") if unadopted else f"NOT {unadopted_suggestion('m')}",
        ]
        return " AND ".join(clauses), [
            notebook_id, user_id, *read_access_params(user_id), now,
        ]

    def _export_page_on(
        self,
        db: Any,
        notebook_id: str,
        user_id: str,
        after: tuple[Any, str] | None,
        page: int,
        now: Any,
    ) -> tuple[list[Any], tuple[Any, str] | None]:
        """One keyset page on the caller's connection; the cursor carries the
        raw column value, so no timestamp formatting can skip or repeat a row.
        Returns the rows and the next cursor (``None`` after the last page)."""
        where, params = self._export_where(notebook_id, user_id, now)
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
            origin_sql, origin_params = self._origin_clause(origin)
            clauses.append(origin_sql)
            params.extend(origin_params)
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
