"""PostgreSQL maintenance operations required by normal product flows."""
from __future__ import annotations

import logging
from pathlib import Path
import threading
import time
from typing import Any

from app.repositories.postgres._store_utils import normalize_timestamp
from app.repositories.text_whitespace import PY_WHITESPACE  # 后端中性,与 sqlite maintenance 共用


logger = logging.getLogger("silicon_notebook.postgres.maintenance")


class PostgresMaintenanceAdapter:
    """Backend-owned asset GC with PostgreSQL cross-process row locking."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._orphan_first_seen: dict[tuple[str, str], float] = {}
        self._orphan_marks_lock = threading.Lock()

    def recover_interrupted_jobs(self) -> None:
        """Settle process-owned work abandoned by a previous backend process."""
        now = normalize_timestamp(self._runtime.seams.now())
        with self._runtime.database.write() as db:
            db.execute(
                "UPDATE merge_review_jobs SET status='failed', "
                "error='中断:服务重启' WHERE status='running'"
            )
            db.execute(
                "UPDATE ask_jobs SET status='interrupted', "
                "error='中断:服务重启' WHERE status='running'"
            )
            db.execute(
                "UPDATE knowhow_rows SET projection_status='failed' "
                "WHERE projection_status IN ('syncing','pending')"
            )
            db.execute(
                "UPDATE sources SET status='parsed',parse_status='parsed',"
                "error_message='',updated_at=%s WHERE parse_status='extracting'",
                (now,),
            )
            db.execute(
                "UPDATE sources SET status='failed',parse_status='failed',"
                "error_message='服务重启导致文档解析中断；文件已保留，可重新解析。',"
                "updated_at=%s WHERE parse_status IN ('queued','parsing')",
                (now,),
            )
            db.execute(
                "UPDATE extraction_runs SET status='failed',"
                "error_message='worker_interrupted: 服务重启导致知识图谱分析中断',"
                "updated_at=%s WHERE run_type='kg' AND status='running'",
                (now,),
            )
            db.execute(
                "UPDATE kg_build_jobs SET status='failed',stage='finished',"
                "error_code='worker_interrupted',"
                "error_message='服务重启导致本次分析中断；已完成内容已保留，可继续分析未完成内容。',"
                "updated_at=%s,finished_at=%s WHERE status='running'",
                (now, now),
            )
            db.execute("DELETE FROM kg_cluster_scratch")
            db.execute("DELETE FROM kg_canonical_scratch")

    @staticmethod
    def _lock_candidate_assets(
        db, notebook_id: str, asset_ids: list[str]
    ) -> tuple[str, ...]:
        """Take every surviving GC candidate row in canonical order.

        Cell writers hold their table first and then take these same sorted
        rows ``FOR KEY SHARE``. GC deliberately takes asset locks only (never a
        table lock), all ``FOR UPDATE``, so the global order is:

        writer: table -> sorted assets -> cell
        GC:                 sorted assets -> recheck/delete

        This avoids table/asset inversion and asset/asset deadlocks while
        making the final reference check authoritative across processes.
        """
        locked: list[str] = []
        for asset_id in sorted(set(asset_ids)):
            row = db.execute(
                "SELECT id FROM notebook_assets "
                "WHERE id=%s AND notebook_id=%s AND source_id IS NULL FOR UPDATE",
                (asset_id, notebook_id),
            ).fetchone()
            if row is not None:
                locked.append(str(row["id"]))
        return tuple(locked)

    def sweep_orphan_assets(
        self,
        notebook_id: str,
        *,
        min_age_seconds: float = 0.0,
        waive_grace_if_no_tables: bool = False,
    ) -> dict[str, object]:
        """Remove unreferenced pasted-image rows and their bounded asset files.

        Source-derived assets are deliberately excluded: they are owned by the
        source/reparse lifecycle, not knowhow-cell Markdown. Candidate files are
        derived only beneath ``storage/assets/<notebook>/<asset>.*``.
        """
        with self._runtime.database.connect() as db:
            assets = db.execute(
                "SELECT id FROM notebook_assets "
                "WHERE notebook_id=%s AND source_id IS NULL",
                (notebook_id,),
            ).fetchall()
            unreferenced = [
                row["id"]
                for row in assets
                if db.execute(
                    "SELECT 1 FROM knowhow_cells c "
                    "JOIN knowhow_rows r ON r.id=c.row_id "
                    "JOIN knowhow_tables t ON t.id=r.table_id "
                    "WHERE t.notebook_id=%s AND c.content_md LIKE %s LIMIT 1",
                    (notebook_id, f"%asset://{row['id']}%"),
                ).fetchone()
                is None
            ]
            with self._orphan_marks_lock:
                marked_notebooks = {key[0] for key in self._orphan_first_seen}
            marked_notebooks.discard(notebook_id)
            dead_notebooks = {
                other
                for other in marked_notebooks
                if db.execute(
                    "SELECT 1 FROM notebooks WHERE id=%s LIMIT 1", (other,)
                ).fetchone()
                is None
            }

        unreferenced_set = set(unreferenced)
        live_ids = {row["id"] for row in assets}
        stamp = time.monotonic()
        orphan_ids: list[str] = []
        with self._orphan_marks_lock:
            for key in [
                key
                for key in self._orphan_first_seen
                if (key[0] == notebook_id and key[1] not in live_ids)
                or key[0] in dead_notebooks
            ]:
                self._orphan_first_seen.pop(key, None)
            for asset_id in live_ids - unreferenced_set:
                self._orphan_first_seen.pop((notebook_id, asset_id), None)
            if min_age_seconds <= 0:
                orphan_ids = list(unreferenced)
            else:
                for asset_id in unreferenced:
                    first_seen = self._orphan_first_seen.get(
                        (notebook_id, asset_id)
                    )
                    if first_seen is None:
                        self._orphan_first_seen[(notebook_id, asset_id)] = stamp
                    elif stamp - first_seen >= min_age_seconds:
                        orphan_ids.append(asset_id)

        if not unreferenced:
            return {"removed": 0}

        removed: list[str] = []
        with self._runtime.database.write() as db:
            grace_waived = waive_grace_if_no_tables and (
                db.execute(
                    "SELECT 1 FROM knowhow_tables WHERE notebook_id=%s LIMIT 1",
                    (notebook_id,),
                ).fetchone()
                is None
            )
            candidates = unreferenced if grace_waived else orphan_ids
            # Acquire ALL candidate locks before rechecking even one reference.
            # A writer that won first holds KEY SHARE, so we wait and then see
            # its committed cell. If GC won first, writers wait here until the
            # row deletion commits, then fail full-set validation and roll back.
            locked_candidates = self._lock_candidate_assets(
                db, notebook_id, candidates
            )
            for asset_id in locked_candidates:
                still_unreferenced = (
                    db.execute(
                        "SELECT 1 FROM knowhow_cells c "
                        "JOIN knowhow_rows r ON r.id=c.row_id "
                        "JOIN knowhow_tables t ON t.id=r.table_id "
                        "WHERE t.notebook_id=%s AND c.content_md LIKE %s LIMIT 1",
                        (notebook_id, f"%asset://{asset_id}%"),
                    ).fetchone()
                    is None
                )
                if not still_unreferenced:
                    with self._orphan_marks_lock:
                        self._orphan_first_seen.pop((notebook_id, asset_id), None)
                    continue
                removed.append(asset_id)
            if removed:
                with db.cursor() as cursor:
                    cursor.executemany(
                        "DELETE FROM notebook_assets WHERE id=%s",
                        [(asset_id,) for asset_id in removed],
                    )

        # Filesystem deletion is intentionally AFTER the row-delete transaction
        # commits. Deleting a file inside a transaction and then rolling the DB
        # back would resurrect an asset row whose file is gone; a waiting writer
        # could subsequently save a live broken reference. DB-first means a
        # commit/rollback failure touches no file. A later unlink failure can
        # only leak an unreachable orphan file because no writer can validate
        # the now-absent asset row. Missing files remain harmless/idempotent.
        asset_dir = Path(self._runtime.storage_dir) / "assets" / notebook_id
        for asset_id in removed:
            for stale_file in asset_dir.glob(f"{asset_id}.*"):
                if not stale_file.is_file():
                    continue
                try:
                    stale_file.unlink(missing_ok=True)
                except OSError:
                    logger.warning("orphan asset file cleanup failed")
        with self._orphan_marks_lock:
            for asset_id in removed:
                self._orphan_first_seen.pop((notebook_id, asset_id), None)
        return {"removed": len(removed)}

    # -- 体检 / backfill(H4/H5)所需的向量盘点 + 补齐 seam ---------------------
    # 判据与 sqlite maintenance 的同名方法**逐字一致**,只是 postgres SQL(``%s`` 占位符、
    # ``btrim(text, %s)`` 对应 sqlite 的 ``TRIM(text, ?)``)。element 侧的 TRIM charset 用共享的
    # PY_WHITESPACE(= Python str.strip() 全集),与嵌入资格过滤(embed_source 跳过 strip 空)一致。
    # checkup/backfill 后端中性,两后端跑同一套聚合/补齐逻辑,只是落到各自 maintenance。

    def count_missing_chunk_vectors(
        self, notebook_id: str, exclude_source_ids: "set[str] | None" = None
    ) -> int:
        exclude = tuple(exclude_source_ids or ())
        clause = (
            " AND c.source_id NOT IN (" + ",".join(["%s"] * len(exclude)) + ")"
            if exclude else ""
        )
        with self._runtime.database.connect() as db:
            return int(db.execute(
                "SELECT COUNT(*) c FROM chunks c WHERE c.notebook_id=%s AND NOT EXISTS "
                "(SELECT 1 FROM chunk_embeddings e WHERE e.chunk_id=c.id)" + clause,
                (notebook_id, *exclude),
            ).fetchone()["c"])

    def count_missing_element_vectors(
        self, notebook_id: str, exclude_source_ids: "set[str] | None" = None
    ) -> int:
        exclude = tuple(exclude_source_ids or ())
        clause = (
            " AND e.source_id NOT IN (" + ",".join(["%s"] * len(exclude)) + ")"
            if exclude else ""
        )
        with self._runtime.database.connect() as db:
            return int(db.execute(
                "SELECT COUNT(*) c FROM source_elements e "
                "JOIN sources s ON s.id = e.source_id "
                "WHERE s.notebook_id=%s "
                "AND s.source_type NOT IN ('memory', 'knowhow') "
                "AND btrim(e.text, %s) != '' "
                "AND NOT EXISTS (SELECT 1 FROM element_embeddings v "
                "WHERE v.element_id = e.id)" + clause,
                (notebook_id, PY_WHITESPACE, *exclude),
            ).fetchone()["c"])

    def missing_chunk_embedding_rows(
        self, notebook_id: str, only_source_id: "str | None" = None
    ) -> list[dict]:
        params: list = [notebook_id]
        clause = ""
        if only_source_id is not None:
            clause += " AND c.source_id = %s"
            params.append(only_source_id)
        with self._runtime.database.connect() as db:
            return [
                dict(r) for r in db.execute(
                    "SELECT c.id, c.source_id, c.text FROM chunks c WHERE c.notebook_id=%s "
                    "AND NOT EXISTS (SELECT 1 FROM chunk_embeddings e "
                    "WHERE e.chunk_id=c.id)" + clause,
                    tuple(params),
                ).fetchall()
            ]

    def missing_element_embedding_rows(
        self, notebook_id: str, only_source_id: "str | None" = None
    ) -> list[dict]:
        params: list = [notebook_id, PY_WHITESPACE]
        clause = ""
        if only_source_id is not None:
            clause += " AND e.source_id = %s"
            params.append(only_source_id)
        with self._runtime.database.connect() as db:
            return [
                dict(r) for r in db.execute(
                    "SELECT e.id, e.source_id, e.text FROM source_elements e "
                    "JOIN sources s ON s.id = e.source_id "
                    "WHERE s.notebook_id=%s "
                    "AND s.source_type NOT IN ('memory', 'knowhow') "
                    "AND btrim(e.text, %s) != '' "
                    "AND NOT EXISTS (SELECT 1 FROM element_embeddings v "
                    "WHERE v.element_id = e.id)" + clause,
                    tuple(params),
                ).fetchall()
            ]

    def missing_chunk_vector_source_ids(self, notebook_id: str) -> list[str]:
        with self._runtime.database.connect() as db:
            return [
                r["source_id"] for r in db.execute(
                    "SELECT DISTINCT c.source_id FROM chunks c WHERE c.notebook_id=%s "
                    "AND NOT EXISTS (SELECT 1 FROM chunk_embeddings e WHERE e.chunk_id=c.id)",
                    (notebook_id,),
                ).fetchall()
            ]

    def missing_element_vector_source_ids(self, notebook_id: str) -> list[str]:
        with self._runtime.database.connect() as db:
            return [
                r["source_id"] for r in db.execute(
                    "SELECT DISTINCT e.source_id FROM source_elements e "
                    "JOIN sources s ON s.id = e.source_id "
                    "WHERE s.notebook_id=%s "
                    "AND s.source_type NOT IN ('memory', 'knowhow') "
                    "AND btrim(e.text, %s) != '' "
                    "AND NOT EXISTS (SELECT 1 FROM element_embeddings v "
                    "WHERE v.element_id = e.id)",
                    (notebook_id, PY_WHITESPACE),
                ).fetchall()
            ]

    def embed_chunks_batch(self, notebook_id: str, items: list[dict]) -> None:
        # 嵌入服务是后端中性的共享 runtime 组件——两后端薄委托同一实现(与 sqlite maintenance 一致)。
        return self._runtime.source_embedding.embed_chunks_batch(notebook_id, items)

    def embed_elements_batch(self, notebook_id: str, items: list[dict]) -> int:
        return self._runtime.source_embedding.embed_elements_batch(notebook_id, items)


__all__ = ["PostgresMaintenanceAdapter"]
