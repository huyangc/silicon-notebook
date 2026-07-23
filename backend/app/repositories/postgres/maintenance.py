"""PostgreSQL maintenance operations required by normal product flows."""
from __future__ import annotations

from pathlib import Path
import threading
import time
from typing import Any


class PostgresMaintenanceAdapter:
    """Backend-owned asset GC with the same safety contract as SQLite."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._orphan_first_seen: dict[tuple[str, str], float] = {}
        self._orphan_marks_lock = threading.Lock()

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

        asset_dir = Path(self._runtime.storage_dir) / "assets" / notebook_id
        removed: list[str] = []
        with self._runtime.database.write() as db:
            grace_waived = waive_grace_if_no_tables and (
                db.execute(
                    "SELECT 1 FROM knowhow_tables WHERE notebook_id=%s LIMIT 1",
                    (notebook_id,),
                ).fetchone()
                is None
            )
            for asset_id in unreferenced if grace_waived else orphan_ids:
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
                for stale_file in asset_dir.glob(f"{asset_id}.*"):
                    if stale_file.is_file():
                        stale_file.unlink(missing_ok=True)
                removed.append(asset_id)
            if removed:
                with db.cursor() as cursor:
                    cursor.executemany(
                        "DELETE FROM notebook_assets WHERE id=%s",
                        [(asset_id,) for asset_id in removed],
                    )
        with self._orphan_marks_lock:
            for asset_id in removed:
                self._orphan_first_seen.pop((notebook_id, asset_id), None)
        return {"removed": len(removed)}


__all__ = ["PostgresMaintenanceAdapter"]
