from __future__ import annotations

import json
import sqlite3
from typing import Callable, List, Literal, Sequence

from app.core.activity_time import activity_retention_window
from app.domain.source_display import source_display_title
from app.models.notebooks import NotebookCreate, NotebookUpdate
from app.repositories.sqlite.access_sql import NOTEBOOK_LIVE_SQL
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.notebook_delete_job_store import NotebookDeleteJobStore
from app.repositories.sqlite.mount_sql import (
    MOUNT_GATE_CLOSED_EXPR, MOUNT_JOIN, MOUNT_ORDER, MOUNT_ORIGIN_COLUMN,
    MOUNT_VALID, MOUNT_VALID_EXPR,
)

# Knowledge-object statuses that count as "usable" for retrieval and the
# NotebookSummary type counts.  Task 13 moved the canonical definition to
# app.domain.knowledge_contracts (sunk from app.services in B3); this
# re-export keeps the Task-8 import sites (facade / notebook_catalog)
# pointing at the SAME tuple.
from app.domain.knowledge_contracts import USABLE_STATUSES  # noqa: F401
from app.repositories.sqlite.source_store import VISIBLE_SOURCE_TYPES_PREDICATE


class NotebookStore:
    """SQLite notebooks-table row persistence: CRUD, tier transitions, 参考库挂载边
    (notebook_bases) 与检索参与集解析, and row deletion (including the orphan
    knowledge-embedding cleanup that the schema's missing FK makes necessary).
    Row-level only — summary projection and orchestration live in
    app.services.notebook_catalog."""

    def __init__(
        self,
        database: SqliteDatabase,
        *,
        new_id: Callable[[str], str],
        now: Callable[[], str],
        activity_retention_days: int,
    ) -> None:
        self.database = database
        self.new_id = new_id
        self.now = now
        self.activity_retention_days = int(activity_retention_days)

    def tier_map(self, notebook_ids: Sequence[str]) -> dict[str, str]:
        ids = list(dict.fromkeys(notebook_id for notebook_id in notebook_ids if notebook_id))
        if not ids:
            return {}
        out: dict[str, str] = {}
        with self.database.connect() as db:
            for offset in range(0, len(ids), 900):
                batch = ids[offset:offset + 900]
                placeholders = ",".join("?" for _ in batch)
                for row in db.execute(
                    f"SELECT id, tier FROM notebooks WHERE id IN ({placeholders})",
                    batch,
                ):
                    out[row["id"]] = row["tier"] or "personal"
        return out

    # ---------------------------------------------------------------- 参考库挂载
    # 参与集 = [本库] + 本库「有效」挂载的库(notebook_bases)。基准库不再全局唯一,
    # 也不再隐式参与 —— 必须显式挂载。有效性的定义见 mount_sql 模块。

    @staticmethod
    def resolve_participants(
        db: sqlite3.Connection, active_notebook_id: str
    ) -> list[tuple[str, str]]:
        """[(notebook_id, tier)] —— 首项恒为 active 本身。唯一的参与集定义点。"""
        active = db.execute(
            "SELECT tier FROM notebooks WHERE id=?", (active_notebook_id,)
        ).fetchone()
        out = [(
            active_notebook_id,
            (active["tier"] if active is not None else "personal") or "personal",
        )]
        rows = db.execute(
            "SELECT b.id AS id, b.tier AS tier "
            + MOUNT_JOIN + MOUNT_VALID + MOUNT_ORDER,
            (active_notebook_id,),
        ).fetchall()
        out.extend((row["id"], row["tier"] or "personal") for row in rows)
        return out

    def participant_notebook_ids(self, active_notebook_id: str) -> list[str]:
        with self.database.connect() as db:
            return self.participant_ids(db, active_notebook_id)

    @staticmethod
    def participant_ids(db: sqlite3.Connection, active_notebook_id: str) -> list[str]:
        return [nb_id for nb_id, _ in NotebookStore.resolve_participants(db, active_notebook_id)]

    @staticmethod
    def participant_rows(db: sqlite3.Connection, active_notebook_id: str):
        """(active_row, base_rows) —— 形状与全局唯一 base 时代一致,消费方无需改动。"""
        base_rows = db.execute(
            "SELECT b.id AS id, b.tier AS tier "
            + MOUNT_JOIN + MOUNT_VALID + MOUNT_ORDER,
            (active_notebook_id,),
        ).fetchall()
        active_row = db.execute(
            "SELECT id, tier FROM notebooks WHERE id=?", (active_notebook_id,),
        ).fetchone()
        return active_row, base_rows

    @staticmethod
    def participant_tiers(db: sqlite3.Connection, active_notebook_id: str):
        pairs = NotebookStore.resolve_participants(db, active_notebook_id)
        return [nb_id for nb_id, _ in pairs], dict(pairs)

    @staticmethod
    def list_mount_edges(db: sqlite3.Connection, notebook_id: str) -> list[dict]:
        """全部挂载边(含失效的)。失效边保留展示 + 置灰,不能假装它还在工作。

        失效边如果不属于本笔记本(notebook_id)的 owner,名字一律遮蔽:挂载时
        合法看到过对方当时的名字,不代表被挂库易主、或公共库被降级之后对方
        (新 owner / 原 owner)改的新名字也该继续流向这个已经无权访问的挂载方
        ——那是一条持续的信息泄露通道,不是一次性的。仅当失效边就是本笔记本
        owner 自己的库(比如自挂,后来因别的原因失效)时才照常显示真实名字,
        因为库主本就看得到自己的库,没有泄露可言。"""
        rows = db.execute(
            "SELECT b.id AS id, b.name AS name, b.tier AS tier, "
            + MOUNT_VALID_EXPR + " AS ok, "
            + MOUNT_GATE_CLOSED_EXPR + " AS gate_closed, "
            "(b.created_by = a.created_by) AS same_owner "
            + MOUNT_JOIN + MOUNT_ORDER,
            (notebook_id,),
        ).fetchall()
        out = []
        for row in rows:
            active = bool(row["ok"])
            gate_closed = bool(row["gate_closed"])
            # 被未共享门关上的借入边:挂载方 owner 对被挂库仍有合法读权,名字
            # 照常显示(无泄露可言),文案给出恢复出口而不是错误的「不属于你」。
            name_visible = active or bool(row["same_owner"]) or gate_closed
            if active:
                reason = ""
            elif gate_closed:
                reason = "本笔记本已共享，借来的参考库暂停参与检索；取消本笔记本的共享即可恢复"
            else:
                reason = "该库已不是公共知识库，且不属于你"
            out.append({
                "id": row["id"],
                "name": row["name"] if name_visible else "已不可用的知识库",
                "tier": row["tier"] or "personal",
                "active": active,
                "inactive_reason": reason,
            })
        return out

    def list_mount_edges_for_notebook(self, notebook_id: str) -> list[dict]:
        """self-connecting 版 list_mount_edges —— facade 一跳委托用(不外传 db)。"""
        with self.database.connect() as db:
            return self.list_mount_edges(db, notebook_id)

    @staticmethod
    def mounted_by_count(db: sqlite3.Connection, notebook_id: str) -> int:
        """有多少笔记本正在把 notebook_id 挂为参考库——删除确认弹窗专用(spec §6)。
        故意不按 MOUNT_VALID_EXPR 过滤生效性:即便边当前失效,删除这个 notebook
        仍会经 ON DELETE CASCADE 把这些边彻底清空,连"对方重新发布后自动恢复"的
        可能性都没了——这个影响面也该让操作者在删前看到。PRIMARY KEY(notebook_id,
        base_notebook_id) 保证同一挂载方不会被计两次。"""
        row = db.execute(
            "SELECT COUNT(*) AS c FROM notebook_bases WHERE base_notebook_id=?",
            (notebook_id,),
        ).fetchone()
        return int(row["c"]) if row is not None else 0

    def mounted_by_count_for_notebook(self, notebook_id: str) -> int:
        """self-connecting 版 mounted_by_count —— facade 一跳委托用(不外传 db)。"""
        with self.database.connect() as db:
            return self.mounted_by_count(db, notebook_id)

    @staticmethod
    def mountable_notebooks(db: sqlite3.Connection, notebook_id: str) -> list[dict]:
        """可挂候选 = 公共知识库 ∪ 同 owner 的库 ∪ everyone 授权的库 ∪
        (本库 owner 有受限读权、且**本库自身未被共享**的库),排除本库自己。

        公共知识库对普通用户的常规列表是隐藏的,故此处专门放行 id/name/tier 三个
        字段——这是用户发现领域库的唯一入口。受限读权那一支(只读分享进来的、
        经 user/group/group_admins 授权边可读的)带「未共享门」:本库一旦被共享,
        这批候选就从列表消失(已挂的边同步失效、保留置灰)——门堵的是把借来的
        参考库转手再分享;完整理由(实时判定吸收撤销、未共享门吸收转手)写在
        mount_sql.py 的模块 docstring。

        有效性谓词与排序复用 mount_sql 的 MOUNT_VALID_EXPR/MOUNT_ORDER(唯一定义
        点)——但 FROM 子句不能复用 MOUNT_JOIN:这里枚举的是候选笔记本本身,不是
        已有的挂载边。"""
        rows = db.execute(
            "SELECT b.id AS id, b.name AS name, b.tier AS tier, "
            + MOUNT_ORIGIN_COLUMN
            + " FROM notebooks b JOIN notebooks a ON a.id = ? "
            "WHERE b.id != a.id AND " + MOUNT_VALID_EXPR
            + MOUNT_ORDER,
            (notebook_id,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "tier": r["tier"] or "personal",
                # 「凭什么能挂」——挂载选择器据此分组,见 MOUNT_ORIGIN_COLUMN。
                "origin": r["origin"],
            }
            for r in rows
        ]

    def mountable_for_notebook(self, notebook_id: str) -> list[dict]:
        """self-connecting 版 mountable_notebooks —— facade 一跳委托用(不外传 db)。"""
        with self.database.connect() as db:
            return self.mountable_notebooks(db, notebook_id)

    def replace_mounts(
        self, notebook_id: str, base_notebook_ids: Sequence[str], created_by: str
    ) -> None:
        """全量替换本库的挂载集合(幂等)。自挂与重复项在写入前剔除,与 CHECK/PK 双保险。"""
        wanted = [
            nb_id for nb_id in dict.fromkeys(base_notebook_ids)
            if nb_id and nb_id != notebook_id
        ]
        now = self.now()
        with self.database.write() as db:
            db.execute("DELETE FROM notebook_bases WHERE notebook_id=?", (notebook_id,))
            for base_id in wanted:
                db.execute(
                    "INSERT INTO notebook_bases"
                    "(notebook_id, base_notebook_id, created_at, created_by)"
                    " VALUES (?,?,?,?)",
                    (notebook_id, base_id, now, created_by),
                )

    def create_row(self, payload: NotebookCreate, created_by: str) -> str:
        """Minimal creation: only name + description (purpose). When the user
        leaves the description blank it is flagged auto (purpose_auto=1) and
        later derived from the first batch of uploaded sources."""
        notebook_id = self.new_id("nb")
        now = self.now()
        purpose = (payload.purpose or "").strip()
        purpose_auto = 0 if purpose else 1
        with self.database.write() as db:
            db.execute(
                """
                INSERT INTO notebooks
                (id, name, purpose, primary_domain, status, created_by, created_at, updated_at,
                 purpose_auto)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    notebook_id,
                    payload.name,
                    purpose,
                    "Semiconductor",
                    "draft",
                    created_by,
                    now,
                    now,
                    purpose_auto,
                ),
            )
            # A brand-new notebook has an empty, therefore complete, KG
            # provenance reverse index.  Every online KG writer maintains
            # knowledge_object_sources transactionally from this point on.
            # Historical databases and deep copies do not pass through this
            # creation seam, so their marker remains false/unknown and readers
            # use the authoritative compatibility path.
            db.execute(
                "INSERT INTO unified_kg_state "
                "(notebook_id,dirty,kg_mutation_seq,source_index_backfilled,updated_at) "
                "VALUES (?,0,0,1,?)",
                (notebook_id, now),
            )
        return notebook_id

    def get_row(self, notebook_id: str) -> sqlite3.Row:
        """Fetch one notebooks row; raises KeyError when absent.  Rows hidden by
        ``NOTEBOOK_LIVE_SQL`` (``status='copying'`` — copy_notebook's in-progress
        sentinel, P1-4 — or the future ``deleting`` tombstone) are treated as
        not-yet-existing; nothing in the tree still needs to see them (the
        previous ``include_copying`` escape hatch had zero call sites)."""
        sql = f"SELECT * FROM notebooks WHERE id = ? AND {NOTEBOOK_LIVE_SQL}"
        with self.database.connect() as db:
            row = db.execute(sql, (notebook_id,)).fetchone()
        if row is None:
            raise KeyError(notebook_id)
        return row

    def update_row(self, notebook_id: str, payload: NotebookUpdate) -> None:
        updates: List[str] = []
        values: List[str] = []
        if payload.name is not None:
            updates.append("name = ?")
            values.append(payload.name.strip() or "Untitled notebook")
        if payload.purpose is not None:
            updates.append("purpose = ?")
            values.append(payload.purpose.strip())
            # A user-edited description is manual; stop auto-regenerating it.
            updates.append("purpose_auto = ?")
            values.append(0)
        if payload.primary_domain is not None:
            updates.append("primary_domain = ?")
            values.append(payload.primary_domain.strip() or "Semiconductor")
        if payload.target_users is not None:
            updates.append("target_users = ?")
            values.append(payload.target_users.strip())
        if payload.access_scope is not None:
            updates.append("access_scope = ?")
            values.append(payload.access_scope.strip())
        for field in ("expected_questions", "source_types", "taxonomy"):
            value = getattr(payload, field)
            if value is not None:
                updates.append(f"{field} = ?")
                values.append(json.dumps(value, ensure_ascii=False))
        if updates:
            updates.append("updated_at = ?")
            values.append(self.now())
            values.append(notebook_id)
            with self.database.write() as db:
                db.execute(
                    f"UPDATE notebooks SET {', '.join(updates)} WHERE id = ?",
                    values,
                )

    def set_tier(
        self, notebook_id: str, tier: Literal["base", "personal"]
    ) -> None:
        """tier='base': 发布为公共知识库(admin 动作)。**不再全局唯一** —— 每个领域
        可以有自己的公共知识库,谁参与某次检索由 notebook_bases 挂载边决定。
        tier='personal': 撤回发布。两者幂等。

        降级为 personal 时不清理指向它的挂载边:边保留但解析时跳过(见
        resolve_participants),重新发布即自动恢复。"""
        now = self.now()
        with self.database.write() as db:
            db.execute(
                "UPDATE notebooks SET tier=?, updated_at=? WHERE id=?",
                (tier, now, notebook_id),
            )

    def indexing_pipeline_state(self, notebook_id: str) -> dict[str, str]:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT COALESCE(n.indexing_pipeline,'') AS pipeline_id,"
                "COALESCE(n.indexing_pipeline_version,'builtin.chunk.v1') "
                "AS pipeline_version,"
                "COALESCE(n.indexing_pipeline_generation,'') "
                "AS pipeline_generation,"
                "COALESCE(n.indexing_pipeline_job_id,'') AS pipeline_job_id,"
                "COALESCE(j.status,'') AS pipeline_job_status,"
                "COALESCE(u.indexing_pipeline_id,'') AS published_pipeline_id,"
                "COALESCE(u.indexing_pipeline_version,'builtin.chunk.v1') "
                "AS published_pipeline_version "
                "FROM notebooks n LEFT JOIN unified_kg_state u "
                "ON u.notebook_id=n.id LEFT JOIN kg_build_jobs j "
                "ON j.id=n.indexing_pipeline_job_id "
                f"WHERE n.id=? AND n.{NOTEBOOK_LIVE_SQL}",
                (notebook_id,),
            ).fetchone()
        if row is None:
            raise KeyError(notebook_id)
        return {
            "pipeline_id": str(row["pipeline_id"] or ""),
            "pipeline_version": str(
                row["pipeline_version"] or "builtin.chunk.v1"
            ),
            "pipeline_generation": str(row["pipeline_generation"] or ""),
            "pipeline_job_id": str(row["pipeline_job_id"] or ""),
            "pipeline_job_status": str(row["pipeline_job_status"] or ""),
            "published_pipeline_id": str(row["published_pipeline_id"] or ""),
            "published_pipeline_version": str(
                row["published_pipeline_version"] or "builtin.chunk.v1"
            ),
        }

    def set_indexing_pipeline_desired(
        self, notebook_id: str, pipeline_id: str, pipeline_version: str
    ) -> str:
        """Persist pending intent before any proposal work and return its CAS token."""
        generation = self.new_id("ipg")
        with self.database.write() as db:
            changed = db.execute(
                "UPDATE notebooks SET indexing_pipeline=?,"
                "indexing_pipeline_version=?,indexing_pipeline_generation=?,"
                "indexing_pipeline_job_id=?,"
                f"updated_at=? WHERE id=? AND {NOTEBOOK_LIVE_SQL}",
                (
                    pipeline_id or None,
                    pipeline_version,
                    generation,
                    f"pending:{generation}",
                    self.now(),
                    notebook_id,
                ),
            )
            if changed.rowcount != 1:
                raise KeyError(notebook_id)
        return generation

    def attach_indexing_pipeline_job(
        self, notebook_id: str, generation: str, job_id: str
    ) -> bool:
        with self.database.write() as db:
            changed = db.execute(
                "UPDATE notebooks SET indexing_pipeline_job_id=? "
                "WHERE id=? AND indexing_pipeline_generation=? "
                "AND indexing_pipeline_job_id=?",
                (job_id, notebook_id, generation, f"pending:{generation}"),
            )
        return changed.rowcount == 1

    def status_of(self, notebook_id: str) -> str | None:
        """Raw ``notebooks.status`` read -- NO ``NOTEBOOK_LIVE_SQL`` filter.
        PostgreSQL twin's docstring has the full rationale (batch 3·W1 PR-3
        §4.2's three in-flight-rebuild checkpoints)."""
        with self.database.connect() as db:
            row = db.execute(
                "SELECT status FROM notebooks WHERE id=?", (notebook_id,)
            ).fetchone()
        return row["status"] if row is not None else None

    def delete_row_and_orphan_embeddings(
        self,
        notebook_id: str,
        *,
        job_id: str | None = None,
        lease_token: str | None = None,
    ) -> list[str]:
        """Delete the notebooks row in ONE committed transaction and return the
        source file paths for the caller to remove AFTER the commit (DB first,
        files second — never the other way around).

        ``job_id`` (batch 3·W1 PR-3 Phase A): see the PostgreSQL twin's
        docstring for the full byte-identity rationale — ``None`` (every
        pre-existing caller) keeps this method's behavior exactly as it was
        before this parameter existed; SQLite has no per-transaction
        statement_timeout knob to tighten (D-4 is PostgreSQL-only), so the
        only added work when ``job_id`` is given is the two extra DELETEs at
        the end, run in both the early-return branch and the normal path.

        ``lease_token`` (codex #659 R14 P2): forwarded verbatim to
        ``cleanup_job_on`` — see that method's docstring for the
        transaction-level fence it enforces. Meaningless without ``job_id``
        and simply passed through as ``None`` in that case."""
        with self.database.write(operation="notebook.delete") as db:
            # The process-local write lock does not coordinate a second
            # SqliteDatabase instance. Acquire SQLite's cross-instance writer
            # lease before the existence check so two duplicate deletes cannot
            # both pass it and let the loser replace the winner's archive with
            # an empty snapshot after the notebook row has gone.
            self.database.begin_immediate(db)
            if db.execute(
                "SELECT 1 FROM notebooks WHERE id=?", (notebook_id,)
            ).fetchone() is None:
                # A concurrent/duplicate request may have passed its service
                # precheck before the first delete acquired the write lock.
                # Preserve the archive committed by that winner.
                if job_id is not None:
                    NotebookDeleteJobStore.cleanup_job_on(db, job_id, lease_token)
                return []
            source_rows = db.execute(
                "SELECT file_path FROM sources WHERE notebook_id = ?",
                (notebook_id,),
            ).fetchall()
            self._retain_user_activity_before_delete(db, notebook_id)
            # knowledge_embeddings has no FK to notebooks (see DDL), so
            # deleting the notebooks row does NOT cascade to it. Delete it here so
            # every public delete caller leaves zero orphan embedding rows.
            # (element_embeddings DOES cascade transitively via
            # source_elements -> sources -> notebooks, so it needs no explicit delete.)
            db.execute(
                "DELETE FROM knowledge_embeddings WHERE notebook_id = ?",
                (notebook_id,),
            )
            # conversations likewise has no FK (closure-external, phase 3's
            # DirectTable list) — this finalize-time delete is defense in
            # depth against codex #659 R6 P2's race: phase 3 sweeps
            # conversations ONCE; ensure_conversation's INSERT is now
            # lifecycle-guarded (won't insert a row for a non-live notebook,
            # see ask_state_store.py), but a turn that started its write
            # transaction a moment before that guard would have rejected it
            # could still land a row between phase 3's sweep and this
            # transaction. Also closes a PRE-EXISTING gap in the legacy
            # synchronous path (job_id is None), which never ran phase 3 at
            # all and had no other conversations cleanup anywhere.
            # conversations likewise has no FK (closure-external, phase 3's
            # DirectTable list) — this finalize-time delete is defense in
            # depth against codex #659 R6 P2's race: phase 3 sweeps
            # conversations ONCE; ensure_conversation's INSERT is now
            # lifecycle-guarded (won't insert a row for a non-live notebook,
            # see ask_state_store.py), but a turn that started its write
            # transaction a moment before that guard would have rejected it
            # could still land a row between phase 3's sweep and this
            # transaction. Also closes a PRE-EXISTING gap in the legacy
            # synchronous path (job_id is None), which never ran phase 3 at
            # all and had no other conversations cleanup anywhere.
            db.execute(
                "DELETE FROM conversations WHERE notebook_id = ?", (notebook_id,),
            )
            if job_id is None:
                # §4.4/P2-g: the JOBIZED path (job_id given) already cleared
                # both FTS5 shadows in phase 3, alongside knowledge_objects/
                # chunks' own batched delete (see NotebookDeleteJobStore.
                # delete_fts_shadow) -- redoing it here would just be a
                # harmless no-op WHERE match, but skipping it keeps this
                # tail doing exactly what phase 3 already did, not a
                # parallel implementation of the same fact. The LEGACY
                # synchronous path (job_id is None -- every test/eval direct
                # caller) never runs phase 3 at all, so this is its ONLY
                # chance to clear them; deleting it here unconditionally
                # would violate the G1 byte-identity contract this
                # parameter's whole docstring is built on.
                db.execute(
                    "DELETE FROM kg_objects_fts WHERE notebook_id = ?", (notebook_id,)
                )
                db.execute(
                    "DELETE FROM chunks_fts WHERE notebook_id = ?", (notebook_id,)
                )
            db.execute("DELETE FROM notebooks WHERE id = ?", (notebook_id,))
            if job_id is not None:
                NotebookDeleteJobStore.cleanup_job_on(db, job_id, lease_token)
        return [row["file_path"] for row in source_rows]

    def _retain_user_activity_before_delete(
        self, db: sqlite3.Connection, notebook_id: str
    ) -> None:
        """Snapshot analysis metadata while the notebook row still exists.

        This runs inside the same write transaction as ``DELETE notebooks``:
        either the three projections and the delete commit together, or none
        do. The table intentionally carries no notebook FK. It is not a backup:
        answer bodies, traces, citations, source content and report sections
        never cross this boundary.
        """
        deleted_at, expires_at = activity_retention_window(
            self.now(), retention_days=self.activity_retention_days
        )
        deleted_text = deleted_at.isoformat()
        expires_text = expires_at.isoformat()

        # Ring-style physical cleanup on every new archive write. Reads also
        # gate on expires_at, so an expired row is invisible even before the
        # next deletion/startup sweep reaches it.
        db.execute(
            "DELETE FROM retained_user_activity "
            "WHERE julianday(expires_at) <= julianday(?)",
            (deleted_text,),
        )
        # An offline merge can place an older archive beside a live copy of
        # the same notebook. Replace that archive as a set: rows removed from
        # the live aggregate must not survive merely because no new row exists
        # to hit their primary key during the upsert below.
        db.execute(
            "DELETE FROM retained_user_activity WHERE notebook_id=?",
            (notebook_id,),
        )
        common_columns = (
            "activity_type,record_id,actor_id,notebook_id,notebook_owner_id,"
            "notebook_name,created_at,updated_at,asked_at,conversation_id,"
            "question,mode,status,display_title,file_name,source_type,"
            "parse_status,parse_failed,depth,generation_started_at,deleted_at,"
            "expires_at"
        )
        refresh_on_conflict = (
            " ON CONFLICT(activity_type,record_id) DO UPDATE SET "
            + ",".join(
                f"{column}=excluded.{column}"
                for column in common_columns.split(",")[2:]
            )
        )
        db.execute(
            f"INSERT INTO retained_user_activity ({common_columns}) "
            "SELECT 'ask',j.id,j.created_by,j.notebook_id,n.created_by,n.name,"
            "j.created_at,j.updated_at,j.asked_at,j.conversation_id,"
            "COALESCE(NULLIF(a.question,''),j.question),"
            "j.mode,j.status,'','','','',0,0,'',?,? "
            "FROM ask_jobs j JOIN notebooks n ON n.id=j.notebook_id "
            "LEFT JOIN answers a ON a.id=j.answer_id "
            "WHERE j.notebook_id=?" + refresh_on_conflict,
            (deleted_text, expires_text, notebook_id),
        )
        source_rows = db.execute(
            "SELECT s.id,s.notebook_id,s.uploaded_by,"
            "n.created_by AS notebook_owner_id,"
            "n.name AS notebook_name,s.created_at,s.updated_at,s.status,"
            "s.title,s.file_name,s.source_type,s.parse_status,"
            "pm.is_paper,pm.paper_title "
            "FROM sources s JOIN notebooks n ON n.id=s.notebook_id "
            "LEFT JOIN source_paper_meta pm ON pm.source_id=s.id "
            f"WHERE s.notebook_id=? AND {VISIBLE_SOURCE_TYPES_PREDICATE}",
            (notebook_id,),
        ).fetchall()
        values_sql = ",".join("?" for _ in common_columns.split(","))
        db.executemany(
            f"INSERT INTO retained_user_activity ({common_columns}) "
            f"VALUES ({values_sql}){refresh_on_conflict}",
            [
                (
                    "source", row["id"], row["uploaded_by"] or "",
                    row["notebook_id"], row["notebook_owner_id"],
                    row["notebook_name"], row["created_at"], row["updated_at"],
                    "", "", "", "", row["status"], source_display_title(row),
                    row["file_name"], row["source_type"], row["parse_status"],
                    int(row["parse_status"] == "failed"), 0, "", deleted_text,
                    expires_text,
                )
                for row in source_rows
            ],
        )
        db.execute(
            f"INSERT INTO retained_user_activity ({common_columns}) "
            "SELECT 'report',r.id,r.created_by,r.notebook_id,n.created_by,n.name,"
            "r.created_at,r.updated_at,'','',r.question,'',r.status,'','','','',"
            "0,r.depth,COALESCE(json_extract(CASE "
            "WHEN json_valid(r.understanding_json) THEN r.understanding_json "
            "ELSE '{}' END,'$._generation_started_at'),''),?,? FROM reports r "
            "JOIN notebooks n ON n.id=r.notebook_id WHERE r.notebook_id=?"
            + refresh_on_conflict,
            (deleted_text, expires_text, notebook_id),
        )

    # ------------------------------------------------- Task 26 primitives
    @staticmethod
    def meta_row(db: sqlite3.Connection, notebook_id: str) -> "dict | None":
        """Name + purpose_auto flag for the metadata-augmentation guard
        (moved verbatim from the facade's `_notebook_meta_row`)."""
        row = db.execute(
            "SELECT name, purpose_auto FROM notebooks WHERE id = ?", (notebook_id,)
        ).fetchone()
        if row is None:
            return None
        return {
            "name": row["name"],
            "purpose_auto": ("purpose_auto" in row.keys() and row["purpose_auto"] == 1),
        }

    def apply_meta(
        self, db: sqlite3.Connection, notebook_id: str, *,
        guard_name: str, name: str, purpose: str,
    ) -> None:
        """Optimistically apply auto-derived notebook metadata: the name only
        overwrites the placeholder we read (no clobber of a concurrent
        rename); the purpose only lands while purpose_auto=1. The caller owns
        the ONE write transaction; the clock rides the compatibility seam."""
        if name:
            db.execute(
                "UPDATE notebooks SET name = ?, updated_at = ? WHERE id = ? AND name = ?",
                (name, self.now(), notebook_id, guard_name),
            )
        if purpose:
            db.execute(
                "UPDATE notebooks SET purpose = ?, updated_at = ? "
                "WHERE id = ? AND purpose_auto = 1",
                (purpose, self.now(), notebook_id),
            )

    @staticmethod
    def tier_on(db: sqlite3.Connection, notebook_id: str) -> str:
        row = db.execute(
            "SELECT tier FROM notebooks WHERE id=?", (notebook_id,)
        ).fetchone()
        return str(row["tier"]) if row is not None and row["tier"] else ""

    def meta_for_notebook(self, notebook_id: str) -> "dict | None":
        with self.database.connect() as db:
            return self.meta_row(db, notebook_id)

    def apply_meta_for_notebook(
        self, notebook_id: str, *, guard_name: str, name: str, purpose: str
    ) -> None:
        with self.database.write() as db:
            self.apply_meta(
                db, notebook_id, guard_name=guard_name, name=name, purpose=purpose
            )

    def tier(self, notebook_id: str) -> str:
        with self.database.connect() as db:
            return self.tier_on(db, notebook_id)
