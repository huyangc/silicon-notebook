"""SQLite reports-table row persistence (Task 25).

Row-level only — the notebook existence guard lives in the report application
service, engine orchestration lives in ``report_engine`` and detached execution
in ``report_execution``. Bodies are
moved verbatim from the frozen facade methods: zero-row UPDATE/DELETE stay
silent no-ops, ``get_report`` raises KeyError, list order is
``created_at DESC, id`` and export selection keeps the done-only/input-order/
IN-batched semantics; the connection-free core exporter owns filenames.
"""
from __future__ import annotations

import json
from typing import Callable, Iterator

from app.core.capability_tokens import new_capability_token
from app.domain.report_export import ReportExportSource
from app.repositories.sqlite.database import SqliteDatabase
from app.core.internal_observability import public_report_sections


class ReportStore:
    # IN(...) batching bound for export lookups — mirrors the facade's
    # `_IN_CHUNK` (well under SQLite's default variable limit).
    IN_CHUNK = 900

    def __init__(self, database: SqliteDatabase, *,
                 new_id: Callable[[str], str],
                 now: Callable[[], str],
                 current_user_id: Callable[[], str]) -> None:
        self.database = database
        self.new_id = new_id
        self.now = now
        self.current_user_id = current_user_id

    def create_report(self, notebook_id: str, question: str, depth: int = 2) -> str:
        report_id = self.new_id("rep")
        now = self.now()
        with self.database.write() as db:
            db.execute(
                "INSERT INTO reports(id, notebook_id, question, depth, created_by, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (report_id, notebook_id, question, depth,
                 self.current_user_id(), now, now))
        return report_id

    def update_report(self, notebook_id: str, report_id: str, *, status=None,
                      progress=None, error=None, outline=None, sections=None,
                      gaps=None, references=None, content_md=None,
                      section_status=None, understanding=None) -> None:
        sets, args = ["updated_at = ?"], [self.now()]
        for col, val, dump in (("status", status, False), ("progress", progress, False),
                               ("error", error, False), ("content_md", content_md, False),
                               ("outline_json", outline, True),
                               ("sections_json", sections, True), ("gaps_json", gaps, True),
                               ("references_json", references, True),
                               ("section_status_json", section_status, True),
                               ("understanding_json", understanding, True)):
            if val is None:
                continue
            if col == "understanding_json":
                # `_generation_started_at` is written by `claim_report_generation`
                # and belongs to the store, not to the caller's in-memory intent
                # contract.  A plain assignment here erases it, which is exactly
                # what happened on every terminal write: finished reports — the
                # ones whose duration matters — could never show one.
                sets.append(
                    f"{col} = CASE WHEN json_extract({col},"
                    f" '$._generation_started_at') IS NOT NULL"
                    f" THEN json_set(?, '$._generation_started_at',"
                    f" json_extract({col}, '$._generation_started_at'))"
                    f" ELSE ? END"
                )
                payload = json.dumps(val, ensure_ascii=False)
                args.extend([payload, payload])
                continue
            sets.append(f"{col} = ?")
            args.append(json.dumps(val, ensure_ascii=False) if dump else val)
        args.extend([report_id, notebook_id])
        with self.database.write() as db:
            db.execute(
                f"UPDATE reports SET {', '.join(sets)} "
                "WHERE id = ? AND notebook_id = ? "
                "AND status NOT IN ('done','failed','cancelled')",
                args,
            )

    def claim_report_intent(
        self, notebook_id: str, report_id: str, understanding: dict
    ) -> bool:
        """Atomically claim one reviewed intent for outline planning."""
        with self.database.write() as db:
            cursor = db.execute(
                "UPDATE reports SET status='planning',progress=?,"
                "understanding_json=?,updated_at=? "
                "WHERE id=? AND notebook_id=? AND status='intent_ready'",
                (
                    "按已确认问题规划中",
                    json.dumps(understanding, ensure_ascii=False),
                    self.now(),
                    report_id,
                    notebook_id,
                ),
            )
        return cursor.rowcount > 0

    def claim_report_generation(
        self,
        notebook_id: str,
        report_id: str,
        understanding: dict | None = None,
    ) -> bool:
        """Atomically claim an outline-ready or failed report for generation."""
        now = self.now()
        understanding_sql = (
            "json_set(json_remove(?, '$.credibility'),"
            "'$._generation_started_at',?)"
            if understanding is not None
            else "json_set(json_remove(understanding_json, '$.credibility'),"
            "'$._generation_started_at',?)"
        )
        understanding_args = (
            [json.dumps(understanding, ensure_ascii=False), now]
            if understanding is not None
            else [now]
        )
        with self.database.write() as db:
            cursor = db.execute(
                "UPDATE reports SET status='generating',progress=?,"
                "error='',content_md='',sections_json='[]',gaps_json='[]',"
                "references_json='[]',section_status_json='[]',"
                f"understanding_json={understanding_sql},updated_at=? "
                "WHERE id=? AND notebook_id=? "
                "AND status IN ('outline_ready','failed')",
                ("准备生成", *understanding_args, now, report_id, notebook_id),
            )
        return cursor.rowcount > 0

    def complete_report_generation(
        self,
        notebook_id: str,
        report_id: str,
        *,
        sections: list,
        content_md: str,
        gaps: list,
        references: list,
    ) -> bool:
        """Atomically publish one successful generation if it still owns it."""
        with self.database.write() as db:
            cursor = db.execute(
                "UPDATE reports SET sections_json=?,content_md=?,gaps_json=?,"
                "references_json=?,status='done',progress='完成',updated_at=? "
                "WHERE id=? AND notebook_id=? AND status='generating'",
                (
                    json.dumps(sections, ensure_ascii=False),
                    content_md,
                    json.dumps(gaps, ensure_ascii=False),
                    json.dumps(references, ensure_ascii=False),
                    self.now(),
                    report_id,
                    notebook_id,
                ),
            )
        return cursor.rowcount > 0

    def cancel_report(self, notebook_id: str, report_id: str) -> bool:
        """Durably publish the sticky terminal state before signalling a worker."""
        with self.database.write() as db:
            cursor = db.execute(
                "UPDATE reports SET status='cancelled',progress=?,updated_at=? "
                "WHERE id=? AND notebook_id=? "
                "AND status NOT IN ('done','failed','cancelled')",
                ("已取消", self.now(), report_id, notebook_id),
            )
        return cursor.rowcount > 0

    def row_to_dict(self, row, *, full: bool) -> dict:
        understanding = json.loads(row["understanding_json"] or "{}")
        generation_started_at = str(
            understanding.pop("_generation_started_at", "") or ""
        )
        d = {"id": row["id"], "notebook_id": row["notebook_id"], "question": row["question"],
             "status": row["status"], "progress": row["progress"], "error": row["error"],
             "created_by": row["created_by"], "created_at": row["created_at"],
             "generation_started_at": generation_started_at,
             "updated_at": row["updated_at"], "depth": row["depth"],
             "section_count": len(json.loads(row["outline_json"] or "[]"))}
        if full:
            d.update(outline=json.loads(row["outline_json"] or "[]"),
                     sections=public_report_sections(
                         json.loads(row["sections_json"] or "[]")
                     ),
                     gaps=json.loads(row["gaps_json"] or "[]"),
                     references=json.loads(row["references_json"] or "[]"),
                     section_status=json.loads(row["section_status_json"] or "[]"),
                     understanding=understanding,
                     shared=bool(row["share_token"]),
                     content_md=row["content_md"])
        return d

    def get_report(self, notebook_id: str, report_id: str) -> dict:
        with self.database.connect() as db:
            row = db.execute("SELECT * FROM reports WHERE id = ? AND notebook_id = ?",
                             (report_id, notebook_id)).fetchone()
        if row is None:
            raise KeyError(report_id)
        return self.row_to_dict(row, full=True)

    def list_reports(self, notebook_id: str, *, created_by: str | None) -> list:
        """Rows for one notebook, optionally narrowed to one creator.

        ``created_by`` is keyword-only and **required** (P1 group sharing):
        reports are per-creator private inside a shared notebook, and an
        optional filter defaulting to "no filter" is exactly the shape that
        lets a future call site list everyone's reports without anyone
        noticing.  Pass ``None`` only from ops/verification paths that
        deliberately want the whole notebook.  The predicate is pushed into
        SQL rather than applied to the result so paging/ordering semantics
        stay identical for both shapes.
        """
        sql = "SELECT * FROM reports WHERE notebook_id = ?"
        args: list = [notebook_id]
        if created_by is not None:
            sql += " AND created_by = ?"
            args.append(created_by)
        sql += " ORDER BY created_at DESC, id"
        with self.database.connect() as db:
            rows = db.execute(sql, args).fetchall()
        return [self.row_to_dict(r, full=False) for r in rows]

    def delete_report(self, notebook_id: str, report_id: str) -> None:
        with self.database.write() as db:
            db.execute("DELETE FROM reports WHERE id = ? AND notebook_id = ?",
                       (report_id, notebook_id))

    # --- Public share links ---------------------------------------------------
    PUBLIC_FIELDS = (
        "id", "question", "content_md", "created_at", "updated_at",
        "references_json", "understanding_json",
    )
    # Columns the token lookup reads for the SERVER-SIDE gate only — the route
    # checks that the report's creator still has read access to the notebook
    # before serving anything (P1-T3b), and `public_report_payload` is an
    # allowlist that never names them.  Deliberately a separate tuple from
    # PUBLIC_FIELDS so that name keeps its single meaning ("what may cross to an
    # anonymous reader"); `understanding_json` above is the existing precedent
    # for a selected-then-dropped column, and it is popped for the same reason.
    GATE_FIELDS = ("notebook_id", "created_by")

    def share_report(self, notebook_id: str, report_id: str) -> str:
        """Issue (or return) the public token for one report.

        Idempotent: re-sharing keeps the existing link so a URL already handed
        out never silently starts 404ing.
        """
        candidate = new_capability_token("rshr")
        with self.database.write() as db:
            row = db.execute(
                "SELECT id FROM reports WHERE id=? AND notebook_id=?",
                (report_id, notebook_id),
            ).fetchone()
            if row is None:
                raise KeyError(report_id)
            # One conditional write instead of read-then-write: COALESCE keeps
            # an already-issued token, so two concurrent shares converge on the
            # same link rather than the later one silently invalidating the
            # link the earlier caller was handed.
            issued = db.execute(
                "UPDATE reports SET share_token=COALESCE(share_token,?), "
                "shared_at=COALESCE(shared_at,?) "
                "WHERE id=? AND notebook_id=? RETURNING share_token",
                (candidate, self.now(), report_id, notebook_id),
            ).fetchone()
        return str(issued["share_token"])

    def unshare_report(self, notebook_id: str, report_id: str) -> None:
        with self.database.write() as db:
            db.execute(
                "UPDATE reports SET share_token=NULL, shared_at=NULL "
                "WHERE id=? AND notebook_id=?",
                (report_id, notebook_id),
            )

    def report_share_token(self, notebook_id: str, report_id: str) -> str:
        """The issued token, for the write-guarded read-back endpoint only.

        Never fold this into the report detail projection: that endpoint is
        reachable with read permission, and this value is an anonymous access
        grant.
        """
        with self.database.connect() as db:
            row = db.execute(
                "SELECT share_token FROM reports WHERE id=? AND notebook_id=?",
                (report_id, notebook_id),
            ).fetchone()
        return str((row["share_token"] if row else "") or "")

    def public_report_by_token(self, token: str) -> dict | None:
        """Resolve one shared report by token alone — the only session-free read.

        Deliberately selects an explicit column list rather than ``*``: this row
        leaves the authenticated surface, so a column added later must be opted
        in here rather than inherited.  Returns None for unknown/revoked tokens
        so the caller cannot distinguish "never existed" from "unshared".

        Also returns ``GATE_FIELDS`` (``notebook_id`` / ``created_by``) for the
        caller's live authorization check.  They are NOT part of the disclosure
        surface — ``public_report_payload`` names neither.
        """
        clean = str(token or "").strip()
        if not clean:
            return None
        columns = ", ".join(self.PUBLIC_FIELDS + self.GATE_FIELDS)
        with self.database.connect() as db:
            row = db.execute(
                f"SELECT {columns} FROM reports "
                "WHERE share_token = ? AND status = 'done'",
                (clean,),
            ).fetchone()
        if row is None:
            return None
        out = dict(row)
        # Decode here so the caller never has to know the dialect: PostgreSQL
        # returns jsonb as a list already, SQLite stores TEXT.
        out["references"] = json.loads(out.pop("references_json", None) or "[]")
        out.pop("understanding_json", None)
        return out

    def _in_batches(self, ids) -> Iterator[list]:
        """把 id 列表切成 ≤IN_CHUNK 的批(去重保序)——镜像 facade `_in_batches`。"""
        ids = list(dict.fromkeys(ids))
        for i in range(0, len(ids), self.IN_CHUNK):
            yield ids[i:i + self.IN_CHUNK]

    def export_reports(self, notebook_id: str, report_ids: list, *,
                       created_by: str | None) -> list:
        """读取批量导出的最小授权视图，按传入 report_ids 顺序回放。

        只取该 notebook 下 status='done'、content_md 非空且匹配 creator 的报告；
        非 done/空/跨 notebook/其他 creator 的 id 静默跳过。格式选择和文件命名在
        连接释放后的 core export service 中完成，provider 永远看不到未授权 id。

        ``created_by`` 与 ``list_reports`` 同一条契约:keyword-only 且必填,
        非 None 时作为 **SQL 谓词**下推(不做结果侧过滤),别人的报告 id 混进
        请求时与「不存在/未完成」一样静默跳过。

        只读走 connect()。report_ids 数量通常极小(用户勾选的几份报告),直接构造
        占位符即可;但仍按 _in_batches 分批以防罕见的大批量超 SQLite 变量上限
        (3.32+ 上限 32,766),批间用 dict 汇总后按原顺序回放。"""
        ids = [r for r in (report_ids or []) if r]
        if not ids:
            return []
        found: dict[str, ReportExportSource] = {}
        creator_clause = "" if created_by is None else "AND created_by = ? "
        creator_args: tuple = () if created_by is None else (created_by,)
        with self.database.connect() as db:
            for batch in self._in_batches(ids):
                placeholders = ",".join("?" for _ in batch)
                rows = db.execute(
                    f"SELECT id, question, content_md FROM reports "
                    f"WHERE notebook_id = ? AND status = 'done' "
                    f"AND content_md IS NOT NULL AND content_md != '' "
                    f"{creator_clause}"
                    f"AND id IN ({placeholders})",
                    (notebook_id, *creator_args, *batch)).fetchall()
                for row in rows:
                    found[row["id"]] = ReportExportSource(
                        row["id"], row["question"], row["content_md"]
                    )
        out: list[ReportExportSource] = []
        for rid in ids:                          # 保持传入顺序
            source = found.get(rid)
            if source is not None:
                out.append(source)
        return out


__all__ = ["ReportStore"]
