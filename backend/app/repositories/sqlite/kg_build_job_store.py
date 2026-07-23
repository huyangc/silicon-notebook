from __future__ import annotations

import sqlite3
from typing import Callable

from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.ports import KgBuildAlreadyRunning


class KgBuildJobStore:
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
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "finished_at": row["finished_at"],
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
            with self.database.write() as db:
                db.execute(
                    """
                    INSERT INTO kg_build_jobs
                    (id, notebook_id, created_by, mode, status, stage,
                     total_sources, completed_sources, failed_sources,
                     error_code, error_message, created_at, updated_at,
                     finished_at)
                    VALUES (?, ?, ?, ?, 'running', 'probing', ?, 0, 0,
                            '', '', ?, ?, '')
                    """,
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
        except sqlite3.IntegrityError as exc:
            if "kg_build_jobs.notebook_id" in str(exc):
                raise KgBuildAlreadyRunning(notebook_id) from exc
            raise
        return self.get(job_id)

    def get(self, job_id: str) -> dict:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM kg_build_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._row(row)

    def latest(self, notebook_id: str) -> dict | None:
        with self.database.connect() as db:
            return self.latest_on(db, notebook_id)

    def latest_on(
        self,
        db: sqlite3.Connection,
        notebook_id: str,
    ) -> dict | None:
        row = db.execute(
            "SELECT * FROM kg_build_jobs WHERE notebook_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
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
        with self.database.write() as db:
            cursor = db.execute(
                "UPDATE kg_build_jobs SET stage=?, error_code=?, "
                "error_message=?, updated_at=? "
                "WHERE id=? AND status='running'",
                (
                    stage,
                    error_code,
                    error_message,
                    self.now(),
                    job_id,
                ),
            )
        return cursor.rowcount == 1

    def record_source_result(
        self,
        job_id: str,
        *,
        succeeded: bool,
    ) -> bool:
        column = "completed_sources" if succeeded else "failed_sources"
        with self.database.write() as db:
            cursor = db.execute(
                f"UPDATE kg_build_jobs SET {column}={column}+1, updated_at=? "
                "WHERE id=? AND status='running'",
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
        with self.database.write() as db:
            cursor = db.execute(
                "UPDATE kg_build_jobs SET status=?, stage='finished', "
                "error_code=?, error_message=?, updated_at=?, finished_at=? "
                "WHERE id=? AND status='running'",
                (
                    status,
                    error_code,
                    error_message,
                    now,
                    now,
                    job_id,
                ),
            )
        return cursor.rowcount == 1

    def fail_submission(self, job_id: str) -> bool:
        return self.finish(
            job_id,
            "failed",
            error_code="job_submission_failed",
            error_message="知识图谱分析任务未能启动，请稍后重试。",
        )


__all__ = [
    "KgBuildAlreadyRunning",
    "KgBuildJobStore",
]
