"""PostgreSQL reports-table row persistence."""
from __future__ import annotations

from typing import Callable, Iterator

from app.repositories.postgres._store_utils import (
    json_value,
    jsonb,
    iso_timestamp,
    normalize_timestamp,
)
from app.core.capability_tokens import new_capability_token
from app.domain.report_export import ReportExportSource
from app.repositories.postgres.database import PostgresDatabase
from app.core.internal_observability import public_report_sections


class ReportStore:
    # Bound memory and parameter use for large export selections.
    IN_CHUNK = 900

    def __init__(self, database: PostgresDatabase, *,
                 new_id: Callable[[str], str],
                 now: Callable[[], str],
                 current_user_id: Callable[[], str]) -> None:
        self.database = database
        self.new_id = new_id
        self.now = now
        self.current_user_id = current_user_id

    def create_report(self, notebook_id: str, question: str, depth: int = 2) -> str:
        report_id = self.new_id("rep")
        now = normalize_timestamp(self.now())
        with self.database.write() as db:
            db.execute(
                "INSERT INTO reports(id, notebook_id, question, depth, created_by, created_at, updated_at)"
                " VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (report_id, notebook_id, question, depth,
                 self.current_user_id(), now, now))
        return report_id

    def update_report(self, notebook_id: str, report_id: str, *, status=None,
                      progress=None, error=None, outline=None, sections=None,
                      gaps=None, references=None, content_md=None,
                      section_status=None, understanding=None) -> None:
        sets, args = ["updated_at = %s"], [normalize_timestamp(self.now())]
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
                    f"{col} = %s::jsonb || (CASE"
                    f" WHEN {col} ? '_generation_started_at'"
                    f" THEN jsonb_build_object('_generation_started_at',"
                    f" {col} -> '_generation_started_at')"
                    f" ELSE '{{}}'::jsonb END)"
                )
            else:
                sets.append(f"{col} = %s")
            args.append(jsonb(val) if dump else val)
        args.extend([report_id, notebook_id])
        with self.database.write() as db:
            db.execute(
                f"UPDATE reports SET {', '.join(sets)} "
                "WHERE id = %s AND notebook_id = %s "
                "AND status NOT IN ('done','failed','cancelled')",
                args,
            )

    def claim_report_intent(
        self, notebook_id: str, report_id: str, understanding: dict
    ) -> bool:
        """Atomically claim one reviewed intent for outline planning."""
        with self.database.write() as db:
            row = db.execute(
                "UPDATE reports SET status='planning',progress=%s,"
                "understanding_json=%s,updated_at=%s "
                "WHERE id=%s AND notebook_id=%s AND status='intent_ready' "
                "RETURNING id",
                (
                    "按已确认问题规划中",
                    jsonb(understanding),
                    normalize_timestamp(self.now()),
                    report_id,
                    notebook_id,
                ),
            ).fetchone()
        return row is not None

    def claim_report_generation(
        self,
        notebook_id: str,
        report_id: str,
        understanding: dict | None = None,
    ) -> bool:
        """Atomically claim an outline-ready or failed report for generation.

        A failed report retains its confirmed intent and outline, so retry can
        safely rerun phase 2 without another planning/model interpretation.
        Prior generated artifacts are cleared in the same CAS transaction.
        """
        now = normalize_timestamp(self.now())
        understanding_sql = (
            "jsonb_set(%s::jsonb - 'credibility',"
            "'{_generation_started_at}',%s,true)"
            if understanding is not None
            else "jsonb_set(understanding_json - 'credibility',"
            "'{_generation_started_at}',%s,true)"
        )
        understanding_args = (
            [jsonb(understanding), jsonb(now.isoformat())]
            if understanding is not None
            else [jsonb(now.isoformat())]
        )
        with self.database.write() as db:
            row = db.execute(
                "UPDATE reports SET status='generating',progress=%s,"
                "error='',content_md='',sections_json='[]'::jsonb,"
                "gaps_json='[]'::jsonb,references_json='[]'::jsonb,"
                "section_status_json='[]'::jsonb,"
                f"understanding_json={understanding_sql},updated_at=%s "
                "WHERE id=%s AND notebook_id=%s "
                "AND status IN ('outline_ready','failed') "
                "RETURNING id",
                (
                    "准备生成",
                    *understanding_args,
                    now,
                    report_id,
                    notebook_id,
                ),
            ).fetchone()
        return row is not None

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
            row = db.execute(
                "UPDATE reports SET sections_json=%s,content_md=%s,gaps_json=%s,"
                "references_json=%s,status='done',progress='完成',updated_at=%s "
                "WHERE id=%s AND notebook_id=%s AND status='generating' "
                "RETURNING id",
                (
                    jsonb(sections),
                    content_md,
                    jsonb(gaps),
                    jsonb(references),
                    normalize_timestamp(self.now()),
                    report_id,
                    notebook_id,
                ),
            ).fetchone()
        return row is not None

    def cancel_report(self, notebook_id: str, report_id: str) -> bool:
        """Durably publish the sticky terminal state before signalling a worker."""
        with self.database.write() as db:
            row = db.execute(
                "UPDATE reports SET status='cancelled',progress=%s,updated_at=%s "
                "WHERE id=%s AND notebook_id=%s "
                "AND status NOT IN ('done','failed','cancelled') RETURNING id",
                (
                    "已取消",
                    normalize_timestamp(self.now()),
                    report_id,
                    notebook_id,
                ),
            ).fetchone()
        return row is not None

    def row_to_dict(self, row, *, full: bool) -> dict:
        understanding = dict(json_value(row["understanding_json"], {}))
        generation_started_at = str(
            understanding.pop("_generation_started_at", "") or ""
        )
        d = {"id": row["id"], "notebook_id": row["notebook_id"], "question": row["question"],
             "status": row["status"], "progress": row["progress"], "error": row["error"],
             "created_by": row["created_by"],
             "created_at": iso_timestamp(row["created_at"]),
             "generation_started_at": generation_started_at,
             "updated_at": iso_timestamp(row["updated_at"]), "depth": row["depth"],
             "section_count": len(json_value(row["outline_json"], []))}
        if full:
            d.update(outline=json_value(row["outline_json"], []),
                     sections=public_report_sections(
                         json_value(row["sections_json"], [])
                     ),
                     gaps=json_value(row["gaps_json"], []),
                     references=json_value(row["references_json"], []),
                     section_status=json_value(row["section_status_json"], []),
                     understanding=understanding,
                     shared=bool(row["share_token"]),
                     content_md=row["content_md"])
        return d

    def get_report(self, notebook_id: str, report_id: str) -> dict:
        with self.database.connect() as db:
            row = db.execute("SELECT * FROM reports WHERE id = %s AND notebook_id = %s",
                             (report_id, notebook_id)).fetchone()
        if row is None:
            raise KeyError(report_id)
        return self.row_to_dict(row, full=True)

    def list_reports(self, notebook_id: str, *, created_by: str | None) -> list:
        """Mirror of the SQLite store — see its docstring for why ``created_by``
        is keyword-only and required, and why the predicate is pushed into SQL
        instead of being applied to the returned rows."""
        sql = "SELECT * FROM reports WHERE notebook_id = %s"
        args: list = [notebook_id]
        if created_by is not None:
            sql += " AND created_by = %s"
            args.append(created_by)
        sql += " ORDER BY created_at DESC, id COLLATE \"C\""
        with self.database.connect() as db:
            rows = db.execute(sql, args).fetchall()
        return [self.row_to_dict(r, full=False) for r in rows]

    def delete_report(self, notebook_id: str, report_id: str) -> None:
        with self.database.write() as db:
            db.execute("DELETE FROM reports WHERE id = %s AND notebook_id = %s",
                       (report_id, notebook_id))

    # --- Public share links ---------------------------------------------------
    PUBLIC_FIELDS = (
        "id", "question", "content_md", "created_at", "updated_at",
        "references_json", "understanding_json",
    )
    # Server-side gate columns — see the SQLite adapter's docstring for why they
    # are a separate tuple from PUBLIC_FIELDS and never reach the payload.
    GATE_FIELDS = ("notebook_id", "created_by")

    def share_report(self, notebook_id: str, report_id: str) -> str:
        """Issue (or return) the public token for one report.

        Idempotent: re-sharing keeps the existing link so a URL already handed
        out never silently starts 404ing.
        """
        candidate = new_capability_token("rshr")
        with self.database.write() as db:
            row = db.execute(
                "SELECT id FROM reports WHERE id=%s AND notebook_id=%s",
                (report_id, notebook_id),
            ).fetchone()
            if row is None:
                raise KeyError(report_id)
            # One conditional write instead of read-then-write.  Under
            # READ COMMITTED two concurrent shares both observe NULL and would
            # each overwrite unconditionally, so the later token wins and the
            # first caller's link 404s despite the idempotency contract.  The
            # second UPDATE blocks on the row, re-reads it, and COALESCE keeps
            # the winner; RETURNING hands back what is actually persisted.
            issued = db.execute(
                "UPDATE reports SET share_token=COALESCE(share_token,%s), "
                "shared_at=COALESCE(shared_at,%s) "
                "WHERE id=%s AND notebook_id=%s RETURNING share_token",
                (candidate, normalize_timestamp(self.now()), report_id, notebook_id),
            ).fetchone()
        return str(issued["share_token"])

    def unshare_report(self, notebook_id: str, report_id: str) -> None:
        with self.database.write() as db:
            db.execute(
                "UPDATE reports SET share_token=NULL, shared_at=NULL "
                "WHERE id=%s AND notebook_id=%s",
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
                "SELECT share_token FROM reports WHERE id=%s AND notebook_id=%s",
                (report_id, notebook_id),
            ).fetchone()
        return str((row["share_token"] if row else "") or "")

    def public_report_by_token(self, token: str) -> dict | None:
        """Resolve one shared report by token alone — the only session-free read.

        Deliberately selects an explicit column list rather than ``*``: this row
        leaves the authenticated surface, so a column added later must be opted
        in here rather than inherited.  Returns None for unknown/revoked tokens
        so the caller cannot distinguish "never existed" from "unshared".

        Also returns ``GATE_FIELDS`` for the caller's live authorization check;
        they are not part of the disclosure surface.
        """
        clean = str(token or "").strip()
        if not clean:
            return None
        columns = ", ".join(self.PUBLIC_FIELDS + self.GATE_FIELDS)
        with self.database.connect() as db:
            row = db.execute(
                f"SELECT {columns} FROM reports "
                "WHERE share_token = %s AND status = 'done'",
                (clean,),
            ).fetchone()
        if row is None:
            return None
        out = dict(row)
        # Same shape as the SQLite adapter: the caller never sees the dialect.
        out["references"] = json_value(out.pop("references_json", None), [])
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

        SQL 在 provider 之前完成 notebook/done/nonempty/creator 收窄，provider 只在
        本连接上下文退出后运行。

        ``created_by`` 与 ``list_reports`` 同一条契约:keyword-only 且必填,
        非 None 时作为 **SQL 谓词**下推(不做结果侧过滤)。

        只读走 connect()。report_ids 数量通常极小；仍按 _in_batches 分批以限制
        单条语句的参数与内存占用，批间用 dict 汇总后按原顺序回放。"""
        ids = [r for r in (report_ids or []) if r]
        if not ids:
            return []
        found: dict[str, ReportExportSource] = {}
        creator_clause = "" if created_by is None else "AND created_by = %s "
        creator_args: tuple = () if created_by is None else (created_by,)
        with self.database.connect() as db:
            for batch in self._in_batches(ids):
                placeholders = ",".join("%s" for _ in batch)
                rows = db.execute(
                    f"SELECT id, question, content_md FROM reports "
                    f"WHERE notebook_id = %s AND status = 'done' "
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
