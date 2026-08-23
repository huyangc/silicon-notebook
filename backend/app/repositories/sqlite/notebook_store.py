from __future__ import annotations

import json
import sqlite3
from typing import Callable, List, Literal, Sequence

from app.models.notebooks import NotebookCreate, NotebookUpdate
from app.repositories.sqlite.database import SqliteDatabase
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
    ) -> None:
        self.database = database
        self.new_id = new_id
        self.now = now

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
        return notebook_id

    def get_row(
        self, notebook_id: str, *, include_copying: bool = False
    ) -> sqlite3.Row:
        """Fetch one notebooks row; raises KeyError when absent.  By default
        status='copying' rows (copy_notebook's in-progress sentinel, P1-4) are
        treated as not-yet-existing; pass include_copying=True for the
        copy/sharing paths that must see them."""
        sql = "SELECT * FROM notebooks WHERE id = ?"
        if not include_copying:
            sql += " AND status != 'copying'"
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

    def delete_row_and_orphan_embeddings(self, notebook_id: str) -> list[str]:
        """Delete the notebooks row in ONE committed transaction and return the
        source file paths for the caller to remove AFTER the commit (DB first,
        files second — never the other way around)."""
        with self.database.write(operation="notebook.delete") as db:
            source_rows = db.execute(
                "SELECT file_path FROM sources WHERE notebook_id = ?",
                (notebook_id,),
            ).fetchall()
            # knowledge_embeddings has no FK to notebooks (see DDL), so
            # deleting the notebooks row does NOT cascade to it. Delete it here so
            # every public delete caller leaves zero orphan embedding rows.
            # (element_embeddings DOES cascade transitively via
            # source_elements -> sources -> notebooks, so it needs no explicit delete.)
            db.execute(
                "DELETE FROM knowledge_embeddings WHERE notebook_id = ?",
                (notebook_id,),
            )
            db.execute("DELETE FROM kg_objects_fts WHERE notebook_id = ?", (notebook_id,))
            db.execute("DELETE FROM chunks_fts WHERE notebook_id = ?", (notebook_id,))
            db.execute("DELETE FROM notebooks WHERE id = ?", (notebook_id,))
        return [row["file_path"] for row in source_rows]

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
