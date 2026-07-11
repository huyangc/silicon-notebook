"""SQLite reports-table row persistence (Task 25).

Row-level only — the notebook existence guard stays in the facade delegate
(``create_report`` raises KeyError there), engine orchestration lives in
``report_engine`` and detached execution in ``report_execution``.  Bodies are
moved verbatim from the frozen facade methods: zero-row UPDATE/DELETE stay
silent no-ops, ``get_report`` raises KeyError, list order is
``created_at DESC, id`` and export keeps the done-only/input-order/
suffix-dedup/IN-batched semantics.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Iterator

from app.repositories.sqlite.database import SqliteDatabase


class ReportStore:
    # IN(...) batching bound for export lookups — mirrors the facade's
    # `_IN_CHUNK` (well under SQLite's default variable limit).
    IN_CHUNK = 900

    def __init__(self, database: SqliteDatabase, *,
                 new_id: Callable[[str], str],
                 now: Callable[[], str],
                 current_user_id: Callable[[], str],
                 get_notebook: Callable[[str], object] | None = None) -> None:
        self.database = database
        self.new_id = new_id
        self.now = now
        self.current_user_id = current_user_id
        self.get_notebook = get_notebook

    def create_report_guarded(
        self, notebook_id: str, question: str, depth: int = 2
    ) -> str:
        if self.get_notebook is not None:
            self.get_notebook(notebook_id)
        return self.create_report(notebook_id, question, depth)

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
                      section_status=None) -> None:
        sets, args = ["updated_at = ?"], [self.now()]
        for col, val, dump in (("status", status, False), ("progress", progress, False),
                               ("error", error, False), ("content_md", content_md, False),
                               ("outline_json", outline, True),
                               ("sections_json", sections, True), ("gaps_json", gaps, True),
                               ("references_json", references, True),
                               ("section_status_json", section_status, True)):
            if val is not None:
                sets.append(f"{col} = ?")
                args.append(json.dumps(val, ensure_ascii=False) if dump else val)
        args.extend([report_id, notebook_id])
        with self.database.write() as db:
            db.execute(f"UPDATE reports SET {', '.join(sets)} WHERE id = ? AND notebook_id = ?", args)

    def row_to_dict(self, row, *, full: bool) -> dict:
        d = {"id": row["id"], "notebook_id": row["notebook_id"], "question": row["question"],
             "status": row["status"], "progress": row["progress"], "error": row["error"],
             "created_by": row["created_by"], "created_at": row["created_at"],
             "updated_at": row["updated_at"], "depth": row["depth"],
             "section_count": len(json.loads(row["outline_json"] or "[]"))}
        if full:
            d.update(outline=json.loads(row["outline_json"] or "[]"),
                     sections=json.loads(row["sections_json"] or "[]"),
                     gaps=json.loads(row["gaps_json"] or "[]"),
                     references=json.loads(row["references_json"] or "[]"),
                     section_status=json.loads(row["section_status_json"] or "[]"),
                     content_md=row["content_md"])
        return d

    def get_report(self, notebook_id: str, report_id: str) -> dict:
        with self.database.connect() as db:
            row = db.execute("SELECT * FROM reports WHERE id = ? AND notebook_id = ?",
                             (report_id, notebook_id)).fetchone()
        if row is None:
            raise KeyError(report_id)
        return self.row_to_dict(row, full=True)

    def list_reports(self, notebook_id: str) -> list:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM reports WHERE notebook_id = ? ORDER BY created_at DESC, id",
                (notebook_id,)).fetchall()
        return [self.row_to_dict(r, full=False) for r in rows]

    def delete_report(self, notebook_id: str, report_id: str) -> None:
        with self.database.write() as db:
            db.execute("DELETE FROM reports WHERE id = ? AND notebook_id = ?",
                       (report_id, notebook_id))

    def _in_batches(self, ids) -> Iterator[list]:
        """把 id 列表切成 ≤IN_CHUNK 的批(去重保序)——镜像 facade `_in_batches`。"""
        ids = list(dict.fromkeys(ids))
        for i in range(0, len(ids), self.IN_CHUNK):
            yield ids[i:i + self.IN_CHUNK]

    def export_reports(self, notebook_id: str, report_ids: list) -> list:
        """批量导出:返回 [(filename, content_md)],按传入 report_ids 顺序,只取该
        notebook 下 status='done' 且 content_md 非空的报告(非 done/空/跨 notebook 的
        id 静默跳过)。文件名 = f"{_safe(question)[:40]}-{rid}.md"。

        只读走 connect()。report_ids 数量通常极小(用户勾选的几份报告),直接构造
        占位符即可;但仍按 _in_batches 分批以防罕见的大批量超 SQLite 变量上限
        (3.32+ 上限 32,766),批间用 dict 汇总后按原顺序回放。"""
        def _safe(name: str) -> str:
            s = re.sub(r'[/\\:*?"<>|\r\n]', "_", name or "").strip()
            return s or ""

        ids = [r for r in (report_ids or []) if r]
        if not ids:
            return []
        found: dict = {}                         # rid -> (question, content_md)
        with self.database.connect() as db:
            for batch in self._in_batches(ids):
                placeholders = ",".join("?" for _ in batch)
                rows = db.execute(
                    f"SELECT id, question, content_md FROM reports "
                    f"WHERE notebook_id = ? AND status = 'done' "
                    f"AND content_md IS NOT NULL AND content_md != '' "
                    f"AND id IN ({placeholders})",
                    (notebook_id, *batch)).fetchall()
                for row in rows:
                    found[row["id"]] = (row["question"], row["content_md"])
        out: list = []
        seen: dict = {}                          # 文件名去重(极端同名 → 加 -N 后缀)
        for rid in ids:                          # 保持传入顺序
            if rid not in found:
                continue
            question, content_md = found[rid]
            stem = _safe(question)[:40] or rid
            fname = f"{stem}-{rid}.md"
            if fname in seen:
                seen[fname] += 1
                fname = f"{stem}-{rid}-{seen[fname]}.md"
            else:
                seen[fname] = 0
            out.append((fname, content_md))
        return out


__all__ = ["ReportStore"]
