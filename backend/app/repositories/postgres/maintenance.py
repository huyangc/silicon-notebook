"""PostgreSQL maintenance operations required by normal product flows."""
from __future__ import annotations

from contextlib import contextmanager
import json
import logging
from pathlib import Path
import threading
import time
from typing import Any, Callable, Iterator, Optional, Sequence

from app.repositories.postgres._store_utils import (
    execute_many,
    json_value,
    jsonb,
    normalize_timestamp,
    sqlite_compatible_row,
)
from app.repositories.chunk_elements import (
    reverse_rows as chunk_element_reverse_rows,
    reverse_rows_for_writes as chunk_element_reverse_rows_for_writes,
)
from app.repositories.image_backfill_rows import image_backfill_state
from app.repositories.knowhow_asset_refs import (  # 后端中性,与 sqlite maintenance 共用
    asset_ref_tokens,
    collect_change_payload_tokens,
    keepers_among,
)
from app.repositories.ports import OfflineMaintenanceBusyError
from app.repositories.source_fact_backfill import project_historical_source_fact
from app.repositories.text_whitespace import PY_WHITESPACE  # 后端中性,与 sqlite maintenance 共用
from app.domain.kg.source_partition import SOURCE_PARTITION_FORMAT_VERSION


logger = logging.getLogger("silicon_notebook.postgres.maintenance")


# Fixed, product-owned key for session-level PostgreSQL advisory locking.  It
# contains no database/user identity and deliberately remains stable across
# processes so every offline maintenance composition root contends on the same
# lock. The dedicated session remains open for the lock's whole lifetime.
_OFFLINE_MAINTENANCE_LOCK_KEY = 0x53494C49434F4E
_EMBEDDING_PAGE_SIZE = 500
# Rows consumed per fetch while streaming the orphan-asset keeper scan. Twin of
# the SQLite adapter's constant; see ``_surviving_asset_refs``.
_ASSET_SCAN_FETCH_ROWS = 200


class PostgresMaintenanceAdapter:
    """PostgreSQL batch/repair operations plus backend-owned asset GC."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._orphan_first_seen: dict[tuple[str, str], float] = {}
        self._orphan_marks_lock = threading.Lock()

    @contextmanager
    def offline_maintenance_lock(self) -> Iterator[None]:
        """Serialize direct PostgreSQL maintenance across backend processes.

        PostgreSQL advisory locks are session scoped, so the same dedicated,
        non-pooled connection is held from acquisition through release. A failed
        try never waits behind an unknown operator process: the CLI receives an
        actionable error and exits before any mutation.
        """
        with self._runtime.database.offline_maintenance_session() as db:
            acquired = bool(
                db.execute(
                    "SELECT pg_try_advisory_lock(%s) AS acquired",
                    (_OFFLINE_MAINTENANCE_LOCK_KEY,),
                ).fetchone()["acquired"]
            )
            db.commit()
            if not acquired:
                raise OfflineMaintenanceBusyError(
                    "another PostgreSQL offline maintenance command is already running; "
                    "wait for it to finish and retry"
                )
            try:
                yield
            finally:
                try:
                    db.execute(
                        "SELECT pg_advisory_unlock(%s)",
                        (_OFFLINE_MAINTENANCE_LOCK_KEY,),
                    )
                    db.commit()
                except Exception:  # pragma: no cover - broken sessions self-release
                    logger.warning("offline PostgreSQL maintenance unlock failed")

    def build_chunk_question_index(
        self, notebook_id: str, *, workers: int, force: bool = False,
        progress=None,
    ) -> dict[str, int]:
        return self._runtime.chunk_question_index.build_notebook(
            notebook_id, workers=workers, force=force, progress=progress
        )

    # -- backend-neutral batch maintenance ----------------------------------

    def resolve_owner_profile(self, owner: Optional[str]):
        from app.domain.auth_utils import normalize_username

        with self._runtime.database.connect() as db:
            if owner is None:
                user = db.execute(
                    "SELECT * FROM users WHERE role='admin' "
                    "ORDER BY created_at,id COLLATE \"C\" LIMIT 1"
                ).fetchone()
            else:
                user = db.execute(
                    "SELECT * FROM users WHERE username=%s",
                    (normalize_username(owner),),
                ).fetchone()
            if user is None:
                return None
            profile = db.execute(
                "SELECT * FROM user_profiles WHERE user_id=%s", (user["id"],)
            ).fetchone()
        return self._runtime.identity._user_profile(user, profile)

    def resolve_notebook_owner_profile(self, notebook_id: str):
        with self._runtime.database.connect() as db:
            user = db.execute(
                "SELECT u.* FROM notebooks n JOIN users u ON u.id=n.created_by "
                "WHERE n.id=%s",
                (notebook_id,),
            ).fetchone()
            if user is None:
                return None
            profile = db.execute(
                "SELECT * FROM user_profiles WHERE user_id=%s", (user["id"],)
            ).fetchone()
        return self._runtime.identity._user_profile(user, profile)

    def all_notebook_ids(self) -> list[str]:
        with self._runtime.database.connect() as db:
            return [
                str(row["id"])
                for row in db.execute(
                    "SELECT id FROM notebooks ORDER BY id COLLATE \"C\""
                ).fetchall()
            ]

    def source_id_by_hash(self, notebook_id: str, digest: str) -> Optional[str]:
        return self._runtime.source_store.source_id_by_hash(notebook_id, digest)

    def source_ids(self, notebook_id: str) -> list[str]:
        with self._runtime.database.connect() as db:
            return [
                str(row["id"])
                for row in db.execute(
                    "SELECT id FROM sources WHERE notebook_id=%s "
                    "ORDER BY id COLLATE \"C\"",
                    (notebook_id,),
                ).fetchall()
            ]

    def source_ids_page(
        self, notebook_id: str, *, after_id: str = "", limit: int = 500
    ) -> list[str]:
        """Bounded keyset form for callers that drive very large notebooks."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._runtime.database.connect() as db:
            return [
                str(row["id"])
                for row in db.execute(
                    "SELECT id FROM sources WHERE notebook_id=%s "
                    "AND id COLLATE \"C\">%s "
                    "ORDER BY id COLLATE \"C\" LIMIT %s",
                    (notebook_id, after_id, max(1, int(limit))),
                ).fetchall()
            ]

    def user_source_ids_page(
        self, notebook_id: str, *, after_id: str = "", limit: int = 500
    ) -> list[str]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._runtime.database.connect() as db:
            rows = db.execute(
                "SELECT id FROM sources WHERE notebook_id=%s "
                "AND id COLLATE \"C\">%s "
                "AND source_type NOT IN ('memory','knowhow') "
                "ORDER BY id COLLATE \"C\" LIMIT %s",
                (notebook_id, after_id, int(limit)),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def user_source_title_rows_page(
        self, notebook_id: str, *, after_id: str = "", limit: int = 500
    ) -> list[dict]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._runtime.database.connect() as db:
            rows = db.execute(
                "SELECT id,title FROM sources WHERE notebook_id=%s "
                "AND id COLLATE \"C\">%s "
                "AND source_type NOT IN ('memory','knowhow') "
                "ORDER BY id COLLATE \"C\" LIMIT %s",
                (notebook_id, after_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def source_has_kg(self, source_id: str) -> bool:
        with self._runtime.database.connect() as db:
            return bool(
                db.execute(
                    "SELECT EXISTS(SELECT 1 FROM knowledge_objects "
                    "WHERE source_id=%s AND source_id<>'') AS found",
                    (source_id,),
                ).fetchone()["found"]
            )

    def source_has_elements(self, source_id: str) -> bool:
        with self._runtime.database.connect() as db:
            return bool(
                db.execute(
                    "SELECT EXISTS(SELECT 1 FROM source_elements "
                    "WHERE source_id=%s) AS found",
                    (source_id,),
                ).fetchone()["found"]
            )

    def source_is_user_visible(self, notebook_id: str, source_id: str) -> bool:
        with self._runtime.database.connect() as db:
            return bool(
                db.execute(
                    "SELECT EXISTS(SELECT 1 FROM sources WHERE id=%s "
                    "AND notebook_id=%s "
                    "AND source_type NOT IN ('memory','knowhow')) AS found",
                    (source_id, notebook_id),
                ).fetchone()["found"]
            )

    def source_ids_missing_elements_page(
        self, notebook_id: str, *, after_id: str = "", limit: int = 500
    ) -> list[str]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._runtime.database.connect() as db:
            rows = db.execute(
                "SELECT s.id FROM sources s WHERE s.notebook_id=%s "
                "AND s.id COLLATE \"C\">%s "
                "AND s.source_type NOT IN ('memory','knowhow') "
                "AND NOT EXISTS (SELECT 1 FROM source_elements e "
                "WHERE e.source_id=s.id) "
                "ORDER BY s.id COLLATE \"C\" LIMIT %s",
                (notebook_id, after_id, int(limit)),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def kg_target_source_rows_page(
        self,
        notebook_id: str,
        *,
        after_id: str = "",
        limit: int = 500,
        retry_partial: bool = False,
    ) -> list[dict]:
        """Return a bounded extraction target page with partial-retry state."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        partial_projection = (
            "(has_kg AND partial_error)" if retry_partial else "FALSE"
        )
        with self._runtime.database.connect() as db:
            rows = db.execute(
                "WITH candidates AS ("
                " SELECT s.id,EXISTS(SELECT 1 FROM knowledge_objects ko "
                "  WHERE ko.notebook_id=%s AND ko.source_id=s.id "
                "  AND ko.source_id<>'') AS has_kg,"
                " COALESCE((latest.error_message "
                "   ~ 'windows_failed=[1-9][0-9]*/[0-9]+'"
                "   OR strpos(latest.error_message,'retry_incomplete=1')>0),"
                " FALSE) AS partial_error "
                " FROM sources s LEFT JOIN LATERAL ("
                "  SELECT er.error_message FROM extraction_runs er "
                "  WHERE er.source_id=s.id AND er.run_type='kg' "
                "  ORDER BY er.created_at DESC,er.ordinal DESC LIMIT 1"
                " ) latest ON TRUE WHERE s.notebook_id=%s "
                " AND s.id COLLATE \"C\">%s "
                " AND s.source_type NOT IN ('memory','knowhow') "
                " AND EXISTS (SELECT 1 FROM source_elements pe "
                " WHERE pe.source_id=s.id) "
                + ") SELECT id AS source_id,"
                + partial_projection
                + " AS is_partial FROM candidates "
                "WHERE NOT has_kg"
                + (" OR (has_kg AND partial_error)" if retry_partial else "")
                + " ORDER BY id COLLATE \"C\" LIMIT %s",
                (notebook_id, notebook_id, after_id, int(limit)),
            ).fetchall()
        return [
            {
                "source_id": str(row["source_id"]),
                "is_partial": bool(row["is_partial"]),
            }
            for row in rows
        ]

    def source_title_rows(self, notebook_id: str) -> list[dict]:
        with self._runtime.database.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,title FROM sources WHERE notebook_id=%s "
                    "ORDER BY id COLLATE \"C\"",
                    (notebook_id,),
                ).fetchall()
            ]

    def set_sources_doc_type(self, notebook_id: str, doc_type: str) -> None:
        with self._runtime.database.write() as db:
            db.execute(
                "UPDATE sources SET doc_type=%s WHERE notebook_id=%s "
                "AND source_type NOT IN ('memory','knowhow')",
                (doc_type, notebook_id),
            )

    def kg_covered_source_ids(self, notebook_id: str) -> set[str]:
        with self._runtime.database.connect() as db:
            return {
                str(row["source_id"])
                for row in db.execute(
                    "SELECT DISTINCT source_id FROM knowledge_objects "
                    "WHERE notebook_id=%s AND source_id<>''",
                    (notebook_id,),
                ).fetchall()
            }

    def partial_kg_source_ids(self, notebook_id: str) -> set[str]:
        """Sources whose latest KG attempt is partial and old graph survives."""
        with self._runtime.database.connect() as db:
            rows = db.execute(
                "WITH latest AS ("
                " SELECT er.source_id,er.error_message,"
                " ROW_NUMBER() OVER (PARTITION BY er.source_id "
                " ORDER BY er.created_at DESC,er.ordinal DESC) AS rn"
                " FROM extraction_runs er"
                " WHERE er.notebook_id=%s AND er.run_type='kg'"
                ") "
                "SELECT l.source_id FROM latest l "
                "JOIN sources s ON s.id=l.source_id "
                "WHERE l.rn=1 AND s.source_type NOT IN ('memory','knowhow') "
                "AND (l.error_message ~ 'windows_failed=[1-9][0-9]*/[0-9]+' "
                " OR strpos(l.error_message,'retry_incomplete=1')>0) "
                "AND EXISTS (SELECT 1 FROM knowledge_objects ko "
                " WHERE ko.notebook_id=%s AND ko.source_id=l.source_id)",
                (notebook_id, notebook_id),
            ).fetchall()
        return {str(row["source_id"]) for row in rows}

    def sources_with_elements(self, notebook_id: str) -> set[str]:
        with self._runtime.database.connect() as db:
            rows = db.execute(
                "SELECT DISTINCT e.source_id FROM source_elements e "
                "JOIN sources s ON s.id=e.source_id WHERE s.notebook_id=%s",
                (notebook_id,),
            ).fetchall()
        return {str(row["source_id"]) for row in rows}

    def count_sources_missing_kg(self, notebook_id: str) -> int:
        with self._runtime.database.connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS c FROM sources s WHERE s.notebook_id=%s "
                "AND s.source_type NOT IN ('memory','knowhow') "
                "AND EXISTS (SELECT 1 FROM source_elements pe "
                "WHERE pe.source_id=s.id) "
                "AND NOT EXISTS (SELECT 1 FROM knowledge_objects k "
                "WHERE k.source_id=s.id AND k.source_id<>'' "
                "AND COALESCE((SELECT er.status FROM extraction_runs er "
                "WHERE er.source_id=s.id AND er.run_type='kg' "
                "ORDER BY er.created_at DESC,er.ordinal DESC LIMIT 1),"
                "'completed')='completed')",
                (notebook_id,),
            ).fetchone()
        return int(row["c"])

    def paper_metadata_source_ids_page(
        self,
        notebook_id: str,
        *,
        after_id: str = "",
        limit: int = 500,
        include_existing: bool = False,
    ) -> list[str]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        existing = (
            "" if include_existing else
            " AND NOT EXISTS (SELECT 1 FROM source_paper_meta m WHERE m.source_id=s.id)"
        )
        with self._runtime.database.connect() as db:
            rows = db.execute(
                "SELECT s.id FROM sources s WHERE s.notebook_id=%s "
                "AND s.id COLLATE \"C\">%s "
                "AND s.source_type NOT IN ('memory','knowhow') "
                "AND s.doc_type IN ('','academic_paper') "
                "AND s.parse_status IN ('parsed','extracting','extracted')"
                + existing
                + " ORDER BY s.id COLLATE \"C\" LIMIT %s",
                (notebook_id, after_id, int(limit)),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def ensure_paper_metadata_source(
        self, source_id: str, *, force: bool = False
    ) -> str:
        source = self._runtime.source_store.get_source(source_id)
        return self._runtime.source_ingestion.ensure_paper_metadata(
            source, force=force
        )

    def run_extraction(
        self,
        source_id: str,
        *,
        preserve_existing_until_complete: bool = False,
    ) -> None:
        if preserve_existing_until_complete:
            return self._runtime.source_ingestion.run_extraction(
                source_id, preserve_existing_until_complete=True
            )
        return self._runtime.source_ingestion.run_extraction(source_id)

    def set_source_status(
        self,
        source_id: str,
        status: str,
        *,
        summary: Optional[str] = None,
        error_message: str = "",
    ) -> None:
        self._runtime.source_ingestion.set_source_status(
            source_id,
            status,
            summary=summary,
            error_message=error_message,
        )

    def latest_extraction_run(self, source_id: str) -> Optional[dict]:
        with self._runtime.database.connect() as db:
            row = db.execute(
                "SELECT * FROM extraction_runs WHERE source_id=%s "
                "ORDER BY created_at DESC,ordinal DESC LIMIT 1",
                (source_id,),
            ).fetchone()
        return sqlite_compatible_row(
            row,
            timestamp_columns=("created_at", "updated_at"),
        )

    def delete_notebook_kg(self, notebook_id: str) -> dict:
        return self._runtime.knowledge_lifecycle.delete_notebook_kg(notebook_id)

    def backfill_kg_fts(self, notebook_id: str) -> int:
        self._runtime.catalog.get_notebook(notebook_id)
        with self._runtime.database.write() as db:
            return int(self._runtime.knowledge.backfill_fts(db, notebook_id))

    def backfill_chunk_fts(self, notebook_id: str) -> int:
        with self._runtime.database.write() as db:
            count = int(self._runtime.chunk_store.backfill_fts(db, notebook_id))
        self._runtime.kg_mutations.notebook_languages.pop(notebook_id, None)
        return count

    def build_scale_index(
        self,
        notebook_id: str,
        on_stage: Optional[Callable[[str, int], None]] = None,
    ) -> dict:
        return self._runtime.scale_artifacts.build(notebook_id, on_stage=on_stage)

    def fold_scale_index_delta(
        self, notebook_id: str, _assume_locked: bool = False
    ) -> dict:
        return self._runtime.scale_artifacts.fold(
            notebook_id, assume_locked=_assume_locked
        )

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
            db.execute("DELETE FROM indexing_pipeline_stages")
            # Mirrors the SQLite sweep: the command-catalog single-flight
            # predicate covers queued AND running, so a row stranded before its
            # worker thread started would otherwise hold that source's guard
            # forever.
            # Wording kept in sync with app/services/catalog_job.py's
            # INTERRUPTED_MESSAGE: the user-facing verb is "识别" ("recognize"),
            # not the internal "抽取" ("extract") — this SQL literal bypasses
            # user_error()/the frontend vocabulary gate and lands on screen
            # verbatim, so it is on its own to keep the interface vocabulary right.
            # R6 P1: dropped the unconditional "可重新发起识别" promise — retained
            # is not the same as reachable, `.../job` only ever returns the
            # latest run, and a restart from here now orphans those candidates
            # exactly like a restart from `succeeded` (see catalog_job.py's
            # `_reject_if_pending_candidates`, which now blocks this status too).
            db.execute(
                "UPDATE catalog_jobs SET status='failed',"
                "failure_reason='服务重启导致命令目录识别中断；已生成的候选已保留，"
                "请先在审阅面板确认或跳过，再重新发起识别。',"
                "diagnostic='worker_interrupted',"
                "updated_at=%s,finished_at=%s WHERE status IN ('queued','running')",
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

    def _live_cell_ref_tokens(self, db, notebook_id: str) -> set:
        """``asset://`` tokens in this notebook's LIVE cell content.

        Backend twin of ``SQLiteMaintenanceAdapter._live_cell_ref_tokens``. The
        ``LIKE`` narrows on the CONSTANT ``'%asset://%'`` instead of one pattern
        per asset. Rows are consumed in bounded fetches; psycopg keeps the result
        set client-side, so this bounds the resident PYTHON objects rather than
        the libpq buffer — the accumulated token set is all that survives.
        """
        tokens: set = set()
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT c.content_md AS content_md FROM knowhow_cells c "
                "JOIN knowhow_rows r ON r.id=c.row_id "
                "JOIN knowhow_tables t ON t.id=r.table_id "
                "WHERE t.notebook_id=%s AND c.content_md LIKE '%%asset://%%'",
                (notebook_id,),
            )
            while True:
                rows = cursor.fetchmany(_ASSET_SCAN_FETCH_ROWS)
                if not rows:
                    break
                for row in rows:
                    tokens.update(asset_ref_tokens(row["content_md"]))
        return tokens

    def _history_ref_tokens(self, db, notebook_id: str) -> set:
        """``asset://`` tokens in this notebook's ``knowhow_changes`` history.

        **Scanning this at all is a correctness fix, not an optimisation**: this
        adapter only ever scanned LIVE cells, so an asset whose last surviving
        reference sat in HISTORY was deleted here and kept on SQLite. Reverting
        to that version then rendered a permanently broken image — half the
        point of keeping history. The two backends now share one predicate and
        one corpus. The registered cost is that PostgreSQL now retains assets it
        used to reclaim (the same "once referenced, effectively never reclaimed
        until 清理历史" behaviour SQLite has always had).

        Coarse pre-filter, exact Python match afterwards — a ``payload_json``
        substring hit is a cheap SUPERSET of what counts (it can false-POSITIVE
        inside an excluded ``code_text``, never false-NEGATIVE).
        """
        tokens: set = set()
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT ch.kind AS kind, ch.payload_json AS payload_json "
                "FROM knowhow_changes ch "
                "JOIN knowhow_tables t2 ON t2.id=ch.table_id "
                "WHERE t2.notebook_id=%s AND ch.payload_json LIKE '%%asset://%%'",
                (notebook_id,),
            )
            while True:
                rows = cursor.fetchmany(_ASSET_SCAN_FETCH_ROWS)
                if not rows:
                    break
                for row in rows:
                    collect_change_payload_tokens(
                        row["kind"], row["payload_json"], tokens
                    )
        return tokens

    def _surviving_asset_refs(self, db, notebook_id: str, asset_ids) -> set:
        """Which of ``asset_ids`` still have a surviving reference.

        Backend twin of ``SQLiteMaintenanceAdapter._surviving_asset_refs``; see
        that docstring for the single-predicate argument, the keeper boundary,
        and ``keepers_among``'s prefix restatement of the retired per-asset
        ``LIKE``.

        The history pass is SKIPPED when the live cells alone already account
        for every candidate — the single-pass form of the retired predicate's
        own early return. History tokens can only ever ADD keepers, so once
        every candidate is already kept there is nothing left for them to
        change, and the "few assets, long history" shape stops re-parsing the
        whole change log on every sweep.
        """
        candidates = set(asset_ids)
        if not candidates:
            return set()
        tokens = self._live_cell_ref_tokens(db, notebook_id)
        kept = keepers_among(candidates, tokens)
        if kept == candidates:
            return kept
        tokens |= self._history_ref_tokens(db, notebook_id)
        return keepers_among(candidates, tokens)

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

        Keeper determination rides ``_surviving_asset_refs`` — one streamed
        pass covering LIVE cell content AND ``knowhow_changes`` history, the
        same corpus SQLite has always used (see that method for the registered
        behaviour change).
        """
        with self._runtime.database.connect() as db:
            assets = db.execute(
                "SELECT id FROM notebook_assets "
                "WHERE notebook_id=%s AND source_id IS NULL",
                (notebook_id,),
            ).fetchall()
            asset_ids = [row["id"] for row in assets]
            kept = self._surviving_asset_refs(db, notebook_id, asset_ids)
            unreferenced = [
                asset_id for asset_id in asset_ids if asset_id not in kept
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
            # Same single predicate as the classification read, re-run inside
            # the write transaction AFTER every candidate lock is held — one
            # pass for the whole candidate set rather than one per candidate.
            still_kept = self._surviving_asset_refs(
                db, notebook_id, locked_candidates
            )
            for asset_id in locked_candidates:
                if asset_id in still_kept:
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
        """⚠ **无界版,已无生产调用方**(审计批4):交互式 backfill 与离线 CLI 都改走
        ``missing_chunk_embedding_page``。保留作分页版判据的参考实现(测试做等价差分),
        新代码不要再调它。判据与分页版逐字一致,只是没有 keyset 窗口。"""
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
                    "WHERE e.chunk_id=c.id)" + clause + " ORDER BY c.id COLLATE \"C\"",
                    tuple(params),
                ).fetchall()
            ]

    def missing_chunk_embedding_ids(
        self, notebook_id: str, *, only_source_id: "str | None" = None
    ) -> list[str]:
        """Ids of chunks missing a vector, ascending — the interactive backfill's
        **single** discovery query (mirrors the SQLite adapter; see its docstring
        for the un-ANALYZEd plan that made per-page keyset discovery 43x more
        expensive than one scan). Predicate identical to
        ``missing_chunk_embedding_rows``; only the projection changes."""
        params: list = [notebook_id]
        clause = ""
        if only_source_id is not None:
            clause = " AND c.source_id=%s"
            params.append(only_source_id)
        with self._runtime.database.connect() as db:
            return [
                row["id"]
                for row in db.execute(
                    "SELECT c.id FROM chunks c WHERE c.notebook_id=%s "
                    "AND NOT EXISTS (SELECT 1 FROM chunk_embeddings e "
                    "WHERE e.chunk_id=c.id)" + clause
                    + " ORDER BY c.id COLLATE \"C\"",
                    tuple(params),
                ).fetchall()
            ]

    def missing_element_embedding_ids(
        self, notebook_id: str, *, only_source_id: "str | None" = None
    ) -> list[str]:
        """Ids of eligible elements missing a vector, ascending. Predicate identical
        to ``missing_element_embedding_rows`` (notebook, memory/knowhow exclusion,
        ``btrim`` non-empty, NOT EXISTS, optional source); only the projection
        changes. See ``missing_chunk_embedding_ids``."""
        params: list = [notebook_id, PY_WHITESPACE]
        clause = ""
        if only_source_id is not None:
            clause = " AND e.source_id=%s"
            params.append(only_source_id)
        with self._runtime.database.connect() as db:
            return [
                row["id"]
                for row in db.execute(
                    "SELECT e.id FROM source_elements e "
                    "JOIN sources s ON s.id=e.source_id "
                    "WHERE s.notebook_id=%s "
                    "AND s.source_type NOT IN ('memory', 'knowhow') "
                    "AND btrim(e.text, %s) != '' "
                    "AND NOT EXISTS (SELECT 1 FROM element_embeddings v "
                    "WHERE v.element_id=e.id)" + clause
                    + " ORDER BY e.id COLLATE \"C\"",
                    tuple(params),
                ).fetchall()
            ]

    def chunk_texts_by_ids(self, ids: "Sequence[str]") -> list[dict]:
        """``{"id","source_id","text"}`` by primary key — pure hydration, the
        predicate is NOT re-evaluated (the ids come from
        ``missing_chunk_embedding_ids`` inside the same source lock). Deliberately
        carries no notebook/source clause so the plan stays a primary-key lookup;
        ``=ANY(%s)`` takes the whole page in one bound parameter. Row order is
        unspecified — the caller re-orders by the discovery order."""
        values = list(ids)
        if not values:
            return []
        with self._runtime.database.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id, source_id, text FROM chunks WHERE id=ANY(%s)",
                    (values,),
                ).fetchall()
            ]

    def element_texts_by_ids(self, ids: "Sequence[str]") -> list[dict]:
        """``{"id","source_id","text"}`` by primary key; see ``chunk_texts_by_ids``
        (same contract, same primary-key lookup)."""
        values = list(ids)
        if not values:
            return []
        with self._runtime.database.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id, source_id, text FROM source_elements WHERE id=ANY(%s)",
                    (values,),
                ).fetchall()
            ]

    def missing_chunk_embedding_page(
        self,
        notebook_id: str,
        *,
        after_id: str = "",
        limit: int = 500,
        only_source_id: "str | None" = None,
    ) -> list[dict]:
        """Return one stable keyset page without retaining a model-call lock. The
        eligibility predicate is unchanged from the rows version; only the keyset
        window is added. Drained page by page by the offline CLI.

        ⚠ ``only_source_id`` has **no production caller** — the interactive
        backfill moved to ``missing_chunk_embedding_ids`` + ``chunk_texts_by_ids``
        (one discovery scan instead of one per page). Kept as the differential
        reference the equivalence tests drive; do not wire it back."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        params: list = [notebook_id, after_id]
        clause = ""
        if only_source_id is not None:
            clause = " AND c.source_id=%s"
            params.append(only_source_id)
        params.append(max(1, int(limit)))
        with self._runtime.database.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT c.id,c.source_id,c.text FROM chunks c "
                    "WHERE c.notebook_id=%s AND c.id COLLATE \"C\">%s "
                    "AND NOT EXISTS (SELECT 1 FROM chunk_embeddings e "
                    "WHERE e.chunk_id=c.id)" + clause + " "
                    "ORDER BY c.id COLLATE \"C\" LIMIT %s",
                    tuple(params),
                ).fetchall()
            ]

    def missing_element_embedding_rows(
        self, notebook_id: str, only_source_id: "str | None" = None
    ) -> list[dict]:
        """⚠ **无界版,已无生产调用方**(审计批4):交互式 backfill 与离线 CLI 都改走
        ``missing_element_embedding_page``。保留作分页版判据的参考实现(测试做等价差分),
        新代码不要再调它。判据与分页版逐字一致,只是没有 keyset 窗口。"""
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
                    "WHERE v.element_id = e.id)" + clause + " ORDER BY e.id COLLATE \"C\"",
                    tuple(params),
                ).fetchall()
            ]

    def missing_element_embedding_page(
        self,
        notebook_id: str,
        *,
        after_id: str = "",
        limit: int = 500,
        only_source_id: "str | None" = None,
    ) -> list[dict]:
        """Return one bounded page of eligible, missing element vectors; drained by
        the offline CLI. ``only_source_id`` has no production caller — see
        ``missing_chunk_embedding_page``."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        params: list = [notebook_id, PY_WHITESPACE, after_id]
        clause = ""
        if only_source_id is not None:
            clause = " AND e.source_id=%s"
            params.append(only_source_id)
        params.append(max(1, int(limit)))
        with self._runtime.database.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT e.id,e.source_id,e.text FROM source_elements e "
                    "JOIN sources s ON s.id=e.source_id "
                    "WHERE s.notebook_id=%s "
                    "AND s.source_type NOT IN ('memory','knowhow') "
                    "AND btrim(e.text,%s)<>'' AND e.id COLLATE \"C\">%s "
                    "AND NOT EXISTS (SELECT 1 FROM element_embeddings v "
                    "WHERE v.element_id=e.id)" + clause + " "
                    "ORDER BY e.id COLLATE \"C\" LIMIT %s",
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

    def embed_chunks_for_source(self, source_id: str) -> None:
        return self._runtime.source_embedding.embed_chunks_for_source(source_id)

    def chunk_and_embed_source(self, source_id: str) -> None:
        return self._runtime.source_chunking.chunk_and_embed_source(source_id)

    def embed_objects_batch(
        self,
        notebook_id: str,
        items: list[dict],
        progress: Optional[Callable[[int, int], None]] = None,
        commit_every: Optional[int] = None,
    ) -> int:
        return self._runtime.source_embedding.embed_objects_batch(
            notebook_id,
            items,
            progress=progress,
            commit_every=commit_every,
        )

    def knowledge_object_payload_page(
        self,
        notebook_id: str,
        *,
        after_id: str = "",
        limit: int = 500,
        include_deprecated: bool = False,
    ) -> list[dict]:
        status = "" if include_deprecated else " AND status<>'deprecated'"
        with self._runtime.database.connect() as db:
            rows = db.execute(
                "SELECT id,payload FROM knowledge_objects "
                "WHERE notebook_id=%s AND id COLLATE \"C\">%s"
                + status
                + " ORDER BY id COLLATE \"C\" LIMIT %s",
                (notebook_id, after_id, max(1, int(limit))),
            ).fetchall()
        return [
            {"id": str(row["id"]), "payload": json_value(row["payload"], {})}
            for row in rows
        ]

    def knowledge_object_payloads(
        self, notebook_id: str, *, include_deprecated: bool = False
    ) -> list[dict]:
        """Compatibility projection; large-library callers should use pages."""
        output: list[dict] = []
        after_id = ""
        while True:
            page = self.knowledge_object_payload_page(
                notebook_id,
                after_id=after_id,
                limit=_EMBEDDING_PAGE_SIZE,
                include_deprecated=include_deprecated,
            )
            if not page:
                break
            output.extend(page)
            after_id = str(page[-1]["id"])
            if len(page) < _EMBEDDING_PAGE_SIZE:
                break
        return output

    def backfill_node_embeddings(
        self, notebook_id: str, progress: Optional[Callable[[int, int], None]] = None
    ) -> int:
        """Backfill knowledge vectors in bounded keyset pages.

        Rows and their JSON payloads are read while a short connection is held;
        embedding/model waits happen only after that connection has returned to
        the pool.  Advancing by object id also prevents a failed best-effort
        model batch from causing an infinite retry loop.
        """
        with self._runtime.database.connect() as db:
            total = int(
                db.execute(
                    "SELECT COUNT(*) AS c FROM knowledge_objects "
                    "WHERE notebook_id=%s AND status<>'deprecated'",
                    (notebook_id,),
                ).fetchone()["c"]
            )
        after_id = ""
        scanned = 0
        embedded = 0
        while True:
            with self._runtime.database.connect() as db:
                rows = db.execute(
                    "SELECT o.id,o.payload,"
                    "(e.object_id IS NOT NULL) AS embedded "
                    "FROM knowledge_objects o LEFT JOIN knowledge_embeddings e "
                    "ON e.object_id=o.id "
                    "WHERE o.notebook_id=%s AND o.status<>'deprecated' "
                    "AND o.id COLLATE \"C\">%s "
                    "ORDER BY o.id COLLATE \"C\" LIMIT %s",
                    (notebook_id, after_id, _EMBEDDING_PAGE_SIZE),
                ).fetchall()
            if not rows:
                break
            after_id = str(rows[-1]["id"])
            scanned += len(rows)
            missing = [
                {"_oid": str(row["id"]), "payload": json_value(row["payload"], {})}
                for row in rows
                if not row["embedded"]
            ]
            if missing:
                embedded += self._runtime.source_embedding.embed_objects_batch(
                    notebook_id, missing
                )
            if progress:
                progress(scanned, total)
            if len(rows) < _EMBEDDING_PAGE_SIZE:
                break
        if progress and scanned == 0:
            progress(0, 0)
        return embedded

    def count_missing_node_vectors(self, notebook_id: str) -> int:
        with self._runtime.database.connect() as db:
            return int(
                db.execute(
                    "SELECT COUNT(*) AS c FROM knowledge_objects o "
                    "WHERE o.notebook_id=%s AND o.status<>'deprecated' "
                    "AND NOT EXISTS (SELECT 1 FROM knowledge_embeddings e "
                    "WHERE e.object_id=o.id)",
                    (notebook_id,),
                ).fetchone()["c"]
            )

    def node_embedding_counts(self, notebook_id: str) -> tuple[int, int]:
        with self._runtime.database.connect() as db:
            objects = int(
                db.execute(
                    "SELECT COUNT(*) AS c FROM knowledge_objects "
                    "WHERE notebook_id=%s AND status<>'deprecated'",
                    (notebook_id,),
                ).fetchone()["c"]
            )
            embeddings = int(
                db.execute(
                    "SELECT COUNT(*) AS c FROM knowledge_embeddings "
                    "WHERE notebook_id=%s",
                    (notebook_id,),
                ).fetchone()["c"]
            )
        return objects, embeddings

    def count_chunks(self, notebook_id: str) -> int:
        with self._runtime.database.connect() as db:
            return int(
                db.execute(
                    "SELECT COUNT(*) AS c FROM chunks WHERE notebook_id=%s",
                    (notebook_id,),
                ).fetchone()["c"]
            )

    def purge_kg_embeddings(self, notebook_id: str) -> None:
        with self._runtime.database.write() as db:
            db.execute(
                "DELETE FROM knowledge_embeddings WHERE notebook_id=%s",
                (notebook_id,),
            )
            db.execute(
                "DELETE FROM relation_embeddings WHERE notebook_id=%s",
                (notebook_id,),
            )

    def backfill_relation_embeddings(self, notebook_id: str) -> None:
        """Fill relation vectors in bounded id pages, idempotently."""
        if not self._runtime.models.configured("relation_embedding"):
            return
        after_id = ""
        while True:
            with self._runtime.database.connect() as db:
                rows = db.execute(
                    "SELECT r.id FROM knowledge_relations r "
                    "WHERE r.notebook_id=%s AND r.id COLLATE \"C\">%s "
                    "AND NOT EXISTS (SELECT 1 FROM relation_embeddings e "
                    "WHERE e.relation_id=r.id) "
                    "ORDER BY r.id COLLATE \"C\" LIMIT %s",
                    (notebook_id, after_id, _EMBEDDING_PAGE_SIZE),
                ).fetchall()
            if not rows:
                break
            relation_ids = [str(row["id"]) for row in rows]
            after_id = relation_ids[-1]
            # Hydration and model work run after releasing the discovery read.
            relations = self._runtime.retrieval.relations_with_names(
                notebook_id, relation_ids
            )
            items = [
                {"_rid": str(relation["id"]), "text": relation["text"]}
                for relation in relations
            ]
            if items:
                self._runtime.source_embedding.embed_relations_batch(
                    notebook_id, items
                )
            if len(rows) < _EMBEDDING_PAGE_SIZE:
                break

    def mark_unified_kg_dirty(self, notebook_id: str) -> None:
        self._runtime.kg_mutations.mark_unified_kg_dirty(notebook_id)

    def has_scale_index(self, notebook_id: str) -> bool:
        return self._runtime.scale_artifacts.load(notebook_id) is not None

    def selected_source_graph_artifact_status(
        self, notebook_id: str
    ) -> dict[str, object]:
        """Cheap version/count probe; never opens ANN or partition payloads."""
        runtime = self._runtime.scale_artifacts
        store = runtime.artifacts
        try:
            current_version = runtime.version(notebook_id)
            main = store.read_manifest(store.scale_dir(notebook_id)) or {}
            partition = (
                store.read_manifest(store.source_partition_dir(notebook_id)) or {}
            )
            main_version = main.get("version")
            parent_version = partition.get("parent_version")
            ready = (
                main_version == current_version
                and parent_version == main_version
                and partition.get("format_version")
                == SOURCE_PARTITION_FORMAT_VERSION
            )
            return {
                "ready": ready,
                "n_nodes": int(main.get("n_nodes", 0)),
                "published_sources": int(partition.get("published_sources", 0)),
                "unavailable_sources": int(partition.get("unavailable_sources", 0)),
            }
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return {
                "ready": False,
                "n_nodes": 0,
                "published_sources": 0,
                "unavailable_sources": 0,
            }

    def begin_source_index_backfill(
        self, notebook_id: str, *, force: bool = False
    ) -> dict[str, object]:
        """Start or resume one notebook's durable reverse-index rebuild."""
        now = normalize_timestamp(self._runtime.seams.now())
        with self._runtime.database.write() as db:
            state = db.execute(
                "SELECT kg_mutation_seq,source_index_backfilled "
                "FROM unified_kg_state WHERE notebook_id=%s FOR UPDATE",
                (notebook_id,),
            ).fetchone()
            mutation_seq = int(state["kg_mutation_seq"]) if state else 0
            marker = bool(state and state["source_index_backfilled"])
            progress = db.execute(
                "SELECT * FROM source_index_backfills WHERE notebook_id=%s FOR UPDATE",
                (notebook_id,),
            ).fetchone()

            if marker and not force:
                if (
                    progress is not None
                    and progress["status"] == "complete"
                    and int(progress["kg_mutation_seq"]) == mutation_seq
                ):
                    return {
                        "notebook_id": notebook_id,
                        "status": "complete",
                        "total_objects": int(progress["total_objects"]),
                        "objects_scanned": int(progress["objects_scanned"]),
                        "rows_written": int(progress["rows_written"]),
                        "resumed": False,
                        "already_complete": True,
                    }
                total = int(
                    db.execute(
                        "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=%s",
                        (notebook_id,),
                    ).fetchone()["c"]
                )
                rows_written = int(
                    db.execute(
                        "SELECT COUNT(*) AS c FROM knowledge_object_sources "
                        "WHERE notebook_id=%s",
                        (notebook_id,),
                    ).fetchone()["c"]
                )
                db.execute(
                    "INSERT INTO source_index_backfills "
                    "(notebook_id,kg_mutation_seq,status,after_object_id,total_objects,"
                    "objects_scanned,rows_written,failure_code,created_at,updated_at,completed_at) "
                    "VALUES (%s,%s,'complete','',%s,%s,%s,'',%s,%s,%s) "
                    "ON CONFLICT(notebook_id) DO UPDATE SET "
                    "kg_mutation_seq=excluded.kg_mutation_seq,status='complete',"
                    "after_object_id='',total_objects=excluded.total_objects,"
                    "objects_scanned=excluded.objects_scanned,rows_written=excluded.rows_written,"
                    "failure_code='',updated_at=excluded.updated_at,completed_at=excluded.completed_at",
                    (notebook_id, mutation_seq, total, total, rows_written, now, now, now),
                )
                return {
                    "notebook_id": notebook_id,
                    "status": "complete",
                    "total_objects": total,
                    "objects_scanned": total,
                    "rows_written": rows_written,
                    "resumed": False,
                    "already_complete": True,
                }

            if (
                progress is not None
                and int(progress["kg_mutation_seq"]) == mutation_seq
                and progress["status"] in {"running", "failed"}
                and not force
            ):
                db.execute(
                    "UPDATE source_index_backfills SET status='running',failure_code='',"
                    "updated_at=%s,completed_at=NULL WHERE notebook_id=%s",
                    (now, notebook_id),
                )
                return {
                    "notebook_id": notebook_id,
                    "status": "running",
                    "total_objects": int(progress["total_objects"]),
                    "objects_scanned": int(progress["objects_scanned"]),
                    "rows_written": int(progress["rows_written"]),
                    "resumed": bool(progress["objects_scanned"]),
                    "already_complete": False,
                }

            db.execute(
                "DELETE FROM knowledge_object_sources WHERE notebook_id=%s",
                (notebook_id,),
            )
            db.execute(
                "UPDATE unified_kg_state SET source_index_backfilled=0,updated_at=%s "
                "WHERE notebook_id=%s",
                (now, notebook_id),
            )
            total = int(
                db.execute(
                    "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=%s",
                    (notebook_id,),
                ).fetchone()["c"]
            )
            status = "complete" if total == 0 else "running"
            completed_at = now if total == 0 else None
            db.execute(
                "INSERT INTO source_index_backfills "
                "(notebook_id,kg_mutation_seq,status,after_object_id,total_objects,"
                "objects_scanned,rows_written,failure_code,created_at,updated_at,completed_at) "
                "VALUES (%s,%s,%s,'',%s,0,0,'',%s,%s,%s) "
                "ON CONFLICT(notebook_id) DO UPDATE SET "
                "kg_mutation_seq=excluded.kg_mutation_seq,status=excluded.status,"
                "after_object_id='',total_objects=excluded.total_objects,objects_scanned=0,"
                "rows_written=0,failure_code='',created_at=excluded.created_at,"
                "updated_at=excluded.updated_at,completed_at=excluded.completed_at",
                (notebook_id, mutation_seq, status, total, now, now, completed_at),
            )
            if total == 0:
                self._runtime.knowledge.mark_source_index_backfilled(db, notebook_id)
            return {
                "notebook_id": notebook_id,
                "status": status,
                "total_objects": total,
                "objects_scanned": 0,
                "rows_written": 0,
                "resumed": False,
                "already_complete": total == 0,
            }

    def resume_source_index_backfill_batch(
        self, notebook_id: str, *, batch_size: int = 2000
    ) -> dict[str, object]:
        """Commit one cursor page and its reverse-index rows atomically."""
        limit = max(1, min(int(batch_size), 10_000))
        now = normalize_timestamp(self._runtime.seams.now())
        with self._runtime.database.write() as db:
            progress = db.execute(
                "SELECT * FROM source_index_backfills WHERE notebook_id=%s FOR UPDATE",
                (notebook_id,),
            ).fetchone()
            if progress is None:
                raise RuntimeError("source index backfill has not been initialized")
            if progress["status"] == "complete":
                return {
                    "notebook_id": notebook_id,
                    "status": "complete",
                    "total_objects": int(progress["total_objects"]),
                    "objects_scanned": int(progress["objects_scanned"]),
                    "rows_written": int(progress["rows_written"]),
                    "batch_objects": 0,
                    "batch_rows": 0,
                }
            state = db.execute(
                "SELECT kg_mutation_seq FROM unified_kg_state "
                "WHERE notebook_id=%s FOR UPDATE",
                (notebook_id,),
            ).fetchone()
            mutation_seq = int(state["kg_mutation_seq"]) if state else 0
            if mutation_seq != int(progress["kg_mutation_seq"]):
                db.execute(
                    "UPDATE source_index_backfills SET status='failed',"
                    "failure_code='kg_generation_changed',updated_at=%s "
                    "WHERE notebook_id=%s",
                    (now, notebook_id),
                )
                return {
                    "notebook_id": notebook_id,
                    "status": "failed",
                    "failure_code": "kg_generation_changed",
                    "total_objects": int(progress["total_objects"]),
                    "objects_scanned": int(progress["objects_scanned"]),
                    "rows_written": int(progress["rows_written"]),
                    "batch_objects": 0,
                    "batch_rows": 0,
                }
            batch = db.execute(
                "SELECT id,evidence FROM knowledge_objects "
                "WHERE notebook_id=%s AND id COLLATE \"C\">%s "
                "ORDER BY id COLLATE \"C\" LIMIT %s",
                (notebook_id, progress["after_object_id"], limit),
            ).fetchall()
            rows = [
                (str(row["id"]), str(source_id), notebook_id)
                for row in batch
                for source_id in self._runtime.knowledge.source_ids_from_evidence(
                    row["evidence"]
                )
            ]
            if rows:
                execute_many(
                    db,
                    "INSERT INTO knowledge_object_sources "
                    "(object_id,source_id,notebook_id) VALUES (%s,%s,%s) "
                    "ON CONFLICT DO NOTHING",
                    rows,
                )
            scanned = int(progress["objects_scanned"]) + len(batch)
            written = int(progress["rows_written"]) + len(rows)
            done = not batch or scanned >= int(progress["total_objects"])
            status = "complete" if done else "running"
            cursor = str(batch[-1]["id"]) if batch else progress["after_object_id"]
            db.execute(
                "UPDATE source_index_backfills SET status=%s,after_object_id=%s,"
                "objects_scanned=%s,rows_written=%s,failure_code='',updated_at=%s,"
                "completed_at=%s WHERE notebook_id=%s",
                (status, cursor, scanned, written, now, now if done else None, notebook_id),
            )
            if done:
                self._runtime.knowledge.mark_source_index_backfilled(db, notebook_id)
            return {
                "notebook_id": notebook_id,
                "status": status,
                "total_objects": int(progress["total_objects"]),
                "objects_scanned": scanned,
                "rows_written": written,
                "batch_objects": len(batch),
                "batch_rows": len(rows),
            }

    def mark_source_index_backfill_failed(
        self, notebook_id: str, failure_code: str
    ) -> None:
        code = failure_code if failure_code in {
            "kg_generation_changed",
            "source_index_backfill_failed",
        } else "source_index_backfill_failed"
        with self._runtime.database.write() as db:
            db.execute(
                "UPDATE source_index_backfills SET status='failed',failure_code=%s,"
                "updated_at=%s WHERE notebook_id=%s AND status!='complete'",
                (code, normalize_timestamp(self._runtime.seams.now()), notebook_id),
            )

    # ------------------------------------------------ chunk_elements backfill
    # Explicit, offline-only historical projection of chunks.element_ids into
    # the chunk_elements reverse index.  NEVER triggered from an interactive
    # request: it is a whole-notebook rewrite, so only the operator-run
    # `batch_ingest backfill-chunk-elements` phase may drive it.  Same shape as
    # the source-index backfill above: bounded keyset page + per-page
    # transaction + kg_mutation_seq generation check that fails closed.

    def begin_chunk_element_backfill(
        self, notebook_id: str, *, force: bool = False
    ) -> dict[str, object]:
        """Start or resume one notebook's element -> chunk reverse-index build."""
        now = normalize_timestamp(self._runtime.seams.now())
        with self._runtime.database.write() as db:
            state = db.execute(
                "SELECT kg_mutation_seq,chunk_elements_indexed "
                "FROM unified_kg_state WHERE notebook_id=%s FOR UPDATE",
                (notebook_id,),
            ).fetchone()
            mutation_seq = int(state["kg_mutation_seq"]) if state else 0
            marker = bool(state and state["chunk_elements_indexed"])
            progress = db.execute(
                "SELECT * FROM chunk_element_backfills WHERE notebook_id=%s FOR UPDATE",
                (notebook_id,),
            ).fetchone()

            if marker and not force:
                if (
                    progress is not None
                    and progress["status"] == "complete"
                    and int(progress["kg_mutation_seq"]) == mutation_seq
                ):
                    return {
                        "notebook_id": notebook_id,
                        "status": "complete",
                        "total_chunks": int(progress["total_chunks"]),
                        "chunks_scanned": int(progress["chunks_scanned"]),
                        "rows_written": int(progress["rows_written"]),
                        "resumed": False,
                        "already_complete": True,
                    }
                total = int(
                    db.execute(
                        "SELECT COUNT(*) AS c FROM chunks WHERE notebook_id=%s",
                        (notebook_id,),
                    ).fetchone()["c"]
                )
                rows_written = int(
                    db.execute(
                        "SELECT COUNT(*) AS c FROM chunk_elements WHERE notebook_id=%s",
                        (notebook_id,),
                    ).fetchone()["c"]
                )
                db.execute(
                    "INSERT INTO chunk_element_backfills "
                    "(notebook_id,kg_mutation_seq,status,after_chunk_id,total_chunks,"
                    "chunks_scanned,rows_written,failure_code,created_at,updated_at,completed_at) "
                    "VALUES (%s,%s,'complete','',%s,%s,%s,'',%s,%s,%s) "
                    "ON CONFLICT(notebook_id) DO UPDATE SET "
                    "kg_mutation_seq=excluded.kg_mutation_seq,status='complete',"
                    "after_chunk_id='',total_chunks=excluded.total_chunks,"
                    "chunks_scanned=excluded.chunks_scanned,rows_written=excluded.rows_written,"
                    "failure_code='',updated_at=excluded.updated_at,completed_at=excluded.completed_at",
                    (notebook_id, mutation_seq, total, total, rows_written, now, now, now),
                )
                return {
                    "notebook_id": notebook_id,
                    "status": "complete",
                    "total_chunks": total,
                    "chunks_scanned": total,
                    "rows_written": rows_written,
                    "resumed": False,
                    "already_complete": True,
                }

            if (
                progress is not None
                and int(progress["kg_mutation_seq"]) == mutation_seq
                and progress["status"] in {"running", "failed"}
                and not force
            ):
                db.execute(
                    "UPDATE chunk_element_backfills SET status='running',failure_code='',"
                    "updated_at=%s,completed_at=NULL WHERE notebook_id=%s",
                    (now, notebook_id),
                )
                return {
                    "notebook_id": notebook_id,
                    "status": "running",
                    "total_chunks": int(progress["total_chunks"]),
                    "chunks_scanned": int(progress["chunks_scanned"]),
                    "rows_written": int(progress["rows_written"]),
                    "resumed": bool(progress["chunks_scanned"]),
                    "already_complete": False,
                }

            db.execute(
                "DELETE FROM chunk_elements WHERE notebook_id=%s", (notebook_id,)
            )
            db.execute(
                "UPDATE unified_kg_state SET chunk_elements_indexed=0,updated_at=%s "
                "WHERE notebook_id=%s",
                (now, notebook_id),
            )
            total = int(
                db.execute(
                    "SELECT COUNT(*) AS c FROM chunks WHERE notebook_id=%s",
                    (notebook_id,),
                ).fetchone()["c"]
            )
            status = "complete" if total == 0 else "running"
            completed_at = now if total == 0 else None
            db.execute(
                "INSERT INTO chunk_element_backfills "
                "(notebook_id,kg_mutation_seq,status,after_chunk_id,total_chunks,"
                "chunks_scanned,rows_written,failure_code,created_at,updated_at,completed_at) "
                "VALUES (%s,%s,%s,'',%s,0,0,'',%s,%s,%s) "
                "ON CONFLICT(notebook_id) DO UPDATE SET "
                "kg_mutation_seq=excluded.kg_mutation_seq,status=excluded.status,"
                "after_chunk_id='',total_chunks=excluded.total_chunks,chunks_scanned=0,"
                "rows_written=0,failure_code='',created_at=excluded.created_at,"
                "updated_at=excluded.updated_at,completed_at=excluded.completed_at",
                (notebook_id, mutation_seq, status, total, now, now, completed_at),
            )
            if total == 0:
                self._runtime.knowledge.mark_chunk_elements_indexed(db, notebook_id)
            return {
                "notebook_id": notebook_id,
                "status": status,
                "total_chunks": total,
                "chunks_scanned": 0,
                "rows_written": 0,
                "resumed": False,
                "already_complete": total == 0,
            }

    def resume_chunk_element_backfill_batch(
        self, notebook_id: str, *, batch_size: int = 2000
    ) -> dict[str, object]:
        """Commit one cursor page and its reverse-index rows atomically."""
        limit = max(1, min(int(batch_size), 10_000))
        now = normalize_timestamp(self._runtime.seams.now())
        with self._runtime.database.write() as db:
            progress = db.execute(
                "SELECT * FROM chunk_element_backfills WHERE notebook_id=%s FOR UPDATE",
                (notebook_id,),
            ).fetchone()
            if progress is None:
                raise RuntimeError("chunk element backfill has not been initialized")
            if progress["status"] == "complete":
                return {
                    "notebook_id": notebook_id,
                    "status": "complete",
                    "total_chunks": int(progress["total_chunks"]),
                    "chunks_scanned": int(progress["chunks_scanned"]),
                    "rows_written": int(progress["rows_written"]),
                    "batch_chunks": 0,
                    "batch_rows": 0,
                }
            state = db.execute(
                "SELECT kg_mutation_seq FROM unified_kg_state "
                "WHERE notebook_id=%s FOR UPDATE",
                (notebook_id,),
            ).fetchone()
            mutation_seq = int(state["kg_mutation_seq"]) if state else 0
            if mutation_seq != int(progress["kg_mutation_seq"]):
                db.execute(
                    "UPDATE chunk_element_backfills SET status='failed',"
                    "failure_code='kg_generation_changed',updated_at=%s "
                    "WHERE notebook_id=%s",
                    (now, notebook_id),
                )
                return {
                    "notebook_id": notebook_id,
                    "status": "failed",
                    "failure_code": "kg_generation_changed",
                    "total_chunks": int(progress["total_chunks"]),
                    "chunks_scanned": int(progress["chunks_scanned"]),
                    "rows_written": int(progress["rows_written"]),
                    "batch_chunks": 0,
                    "batch_rows": 0,
                }
            batch = db.execute(
                "SELECT id,element_ids FROM chunks "
                "WHERE notebook_id=%s AND id COLLATE \"C\">%s "
                "ORDER BY id COLLATE \"C\" LIMIT %s",
                (notebook_id, progress["after_chunk_id"], limit),
            ).fetchall()
            rows = chunk_element_reverse_rows(notebook_id, batch)
            if rows:
                execute_many(
                    db,
                    "INSERT INTO chunk_elements (notebook_id,element_id,chunk_id) "
                    "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    rows,
                )
            scanned = int(progress["chunks_scanned"]) + len(batch)
            written = int(progress["rows_written"]) + len(rows)
            # Terminate ONLY on an exhausted cursor — see the SQLite adapter's
            # note: ``total_chunks`` is frozen at begin() while the write path
            # keeps inserting chunks whose ids can sort after the current
            # cursor and are therefore scanned (and counted) by a later page,
            # so a `scanned >= total` gate would flip the marker while
            # genuinely old chunks were still unprojected. Display only.
            done = not batch
            status = "complete" if done else "running"
            cursor = str(batch[-1]["id"]) if batch else progress["after_chunk_id"]
            db.execute(
                "UPDATE chunk_element_backfills SET status=%s,after_chunk_id=%s,"
                "chunks_scanned=%s,rows_written=%s,failure_code='',updated_at=%s,"
                "completed_at=%s WHERE notebook_id=%s",
                (status, cursor, scanned, written, now, now if done else None, notebook_id),
            )
            if done:
                self._runtime.knowledge.mark_chunk_elements_indexed(db, notebook_id)
            return {
                "notebook_id": notebook_id,
                "status": status,
                "total_chunks": int(progress["total_chunks"]),
                "chunks_scanned": scanned,
                "rows_written": written,
                "batch_chunks": len(batch),
                "batch_rows": len(rows),
            }

    def mark_chunk_element_backfill_failed(
        self, notebook_id: str, failure_code: str
    ) -> None:
        code = failure_code if failure_code in {
            "kg_generation_changed",
            "chunk_element_backfill_failed",
        } else "chunk_element_backfill_failed"
        with self._runtime.database.write() as db:
            db.execute(
                "UPDATE chunk_element_backfills SET status='failed',failure_code=%s,"
                "updated_at=%s WHERE notebook_id=%s AND status!='complete'",
                (code, normalize_timestamp(self._runtime.seams.now()), notebook_id),
            )

    def clear_chunk_element_index(self, notebook_id: str) -> int:
        """Reset chunk_elements for one notebook; returns the chunks total the
        backfill loop must cover."""
        with self._runtime.database.write() as db:
            db.execute(
                "DELETE FROM chunk_elements WHERE notebook_id=%s", (notebook_id,)
            )
            db.execute(
                "UPDATE unified_kg_state SET chunk_elements_indexed=0,"
                "updated_at=%s WHERE notebook_id=%s",
                (normalize_timestamp(self._runtime.seams.now()), notebook_id),
            )
            row = db.execute(
                "SELECT COUNT(*) AS c FROM chunks WHERE notebook_id=%s",
                (notebook_id,),
            ).fetchone()
        return int(row["c"])

    def chunk_elements_indexed(self, notebook_id: str) -> bool:
        with self._runtime.database.connect() as db:
            return self._runtime.knowledge.chunk_elements_indexed(db, notebook_id)

    # ------------------------------------------------- backfill-images (离线)
    # SQLite 侧 `SQLiteMaintenanceAdapter` 的逐字对等半（双后端同修红线）。
    # 同样刻意不进 `ports.py`：那里的 Protocol 方法总数是只许降的零余量上限，
    # 而这三个方法只有 `image_backfill_phase` 一个消费方，它按自己模块里的窄
    # Protocol 结构化依赖它们。

    def image_backfill_source_page(
        self, notebook_id: str, after_id: str, limit: int
    ) -> list[dict]:
        with self._runtime.database.connect() as db:
            rows = db.execute(
                "SELECT id, file_name, file_path FROM sources "
                "WHERE notebook_id=%s AND source_type NOT IN ('memory','knowhow') "
                "AND (lower(file_name) LIKE '%%.md' OR lower(file_name) LIKE '%%.markdown') "
                # 比较键与排序键写同一个 collation，与本文件全部兄弟分页器同
                # 口径。**今天这是纵深防御而不是修 bug**：`0001_initial.sql` 把
                # 每个 id 列都声明成 `text COLLATE "C"`（全库 117 处），所以裸
                # `id > %s` 眼下已经在 C 序上比较、行为逐字相同——这也是为什么没
                # 有任何行为用例能钉住它（真 PG 上做过变异验证：去掉那半
                # collation，7 条 PG 用例全绿）。写全的理由是：一旦哪天某个 id 列
                # 的列级 collation 变了，裸比较键与 `ORDER BY ... COLLATE "C"` 会
                # 给出两种顺序，keyset 分页开始**漏源**——而漏源不报错，只表现为
                # "这批图没补上"。形状由
                # tests/test_image_backfill_transaction_guard.py 的源码守卫钉住。
                "AND id COLLATE \"C\" > %s ORDER BY id COLLATE \"C\" LIMIT %s",
                (notebook_id, after_id, int(limit)),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "file_name": row["file_name"] or "",
                "file_path": row["file_path"] or "",
            }
            for row in rows
        ]

    def image_backfill_source_state(self, source_id: str) -> dict:
        """SQLite 侧同名方法的对等半；元素顺序用 ``id COLLATE "C"``——与
        `source_elements_for_chunking` 同一口径，也就是对齐算法依赖的文档序。"""
        with self._runtime.database.connect() as db:
            element_rows = db.execute(
                "SELECT id, element_type, text, metadata, created_at "
                "FROM source_elements WHERE source_id=%s ORDER BY id COLLATE \"C\"",
                (source_id,),
            ).fetchall()
            chunk_rows = db.execute(
                "SELECT id, element_ids FROM chunks WHERE source_id=%s "
                "ORDER BY id COLLATE \"C\"",
                (source_id,),
            ).fetchall()
        return image_backfill_state(element_rows, chunk_rows)

    def apply_image_backfill(
        self,
        notebook_id: str,
        source_id: str,
        elements: Sequence[dict],
        chunk_element_ids: dict[str, list[str]],
        metadata_updates: Sequence[dict] = (),
        *,
        created_at: Any,
    ) -> None:
        """一个来源的全部插入与就地补齐，一个写事务（SQLite 侧的对等半）。

        `chunks.text` 与 chunk id 不变，所以 `chunk_embeddings` 一行不动；反查行
        与 `sources.updated_at` 同事务，后者的时间戳同样由仓储自己的时钟给。
        ``metadata_updates`` 只补既有 image 元素的 ``metadata.asset_id``
        （见 SQLite 侧同名方法的 docstring 与 `image_backfill.EnrichedImage`）。"""
        stamp = normalize_timestamp(created_at)
        with self._runtime.database.write() as db:
            execute_many(
                db,
                "INSERT INTO source_elements "
                "(id, source_id, element_type, location_label, text, metadata, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                [
                    (
                        element["id"],
                        source_id,
                        element["element_type"],
                        element["location_label"],
                        element["text"],
                        jsonb(dict(element["metadata"])),
                        stamp,
                    )
                    for element in elements
                ],
            )
            for update in metadata_updates:
                db.execute(
                    "UPDATE source_elements SET metadata=%s "
                    "WHERE id=%s AND source_id=%s",
                    (jsonb(dict(update["metadata"])), update["id"], source_id),
                )
            for chunk_id, element_ids in chunk_element_ids.items():
                db.execute(
                    "UPDATE chunks SET element_ids=%s WHERE id=%s AND source_id=%s",
                    (jsonb(list(element_ids)), chunk_id, source_id),
                )
            rows = chunk_element_reverse_rows_for_writes(
                notebook_id, list(chunk_element_ids.items())
            )
            if rows:
                execute_many(
                    db,
                    "INSERT INTO chunk_elements (notebook_id,element_id,chunk_id) "
                    "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    rows,
                )
            db.execute(
                "UPDATE sources SET updated_at=%s WHERE id=%s",
                (normalize_timestamp(self._runtime.seams.now()), source_id),
            )

    def image_backfill_resolve_source_path(self, file_path: str) -> str:
        """SQLite 侧同名方法的对等半：复用 `SourceFileStore.resolve_path` 这条产品
        统一的来源文件路径约定（零 I/O、零查询）。"""
        return str(self._runtime.source_files.resolve_path(file_path))

    def image_backfill_source_asset_ids(
        self, notebook_id: str, source_id: str
    ) -> list[str]:
        """SQLite 侧同名方法的对等半（只读；本趟范围孤儿清扫的前后快照）。"""
        with self._runtime.database.connect() as db:
            rows = db.execute(
                "SELECT id FROM notebook_assets "
                "WHERE notebook_id=%s AND source_id=%s ORDER BY id COLLATE \"C\"",
                (notebook_id, source_id),
            ).fetchall()
        return [row["id"] for row in rows]

    def image_backfill_discard_assets(self, asset_ids: Sequence[str]) -> list[dict]:
        """SQLite 侧同名方法的对等半：删这一批资产行并原样返回供 unlink。"""
        ids = [asset_id for asset_id in dict.fromkeys(asset_ids) if asset_id]
        if not ids:
            return []
        with self._runtime.database.write() as db:
            rows = db.execute(
                "DELETE FROM notebook_assets WHERE id = ANY(%s) RETURNING *",
                (ids,),
            ).fetchall()
        return [
            sqlite_compatible_row(row, timestamp_columns=("created_at",)) or {}
            for row in rows
        ]

    def clear_source_index(self, notebook_id: str) -> int:
        with self._runtime.database.write() as db:
            db.execute(
                "DELETE FROM knowledge_object_sources WHERE notebook_id=%s",
                (notebook_id,),
            )
            db.execute(
                "UPDATE unified_kg_state SET source_index_backfilled=0,"
                "updated_at=%s WHERE notebook_id=%s",
                (
                    normalize_timestamp(self._runtime.seams.now()),
                    notebook_id,
                ),
            )
            row = db.execute(
                "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=%s",
                (notebook_id,),
            ).fetchone()
        return int(row["c"])

    def source_index_backfilled(self, notebook_id: str) -> bool:
        with self._runtime.database.connect() as db:
            return self._runtime.knowledge.source_index_backfilled(db, notebook_id)

    def backfill_source_index_batch(
        self, notebook_id: str, last_id: str, batch_size: int
    ) -> tuple[int, int, str]:
        """Rebuild one evidence reverse-index keyset page transactionally."""
        with self._runtime.database.write() as db:
            batch = db.execute(
                "SELECT id,evidence FROM knowledge_objects "
                "WHERE notebook_id=%s AND id COLLATE \"C\">%s "
                "ORDER BY id COLLATE \"C\" LIMIT %s",
                (notebook_id, last_id, max(1, int(batch_size))),
            ).fetchall()
            if not batch:
                return 0, 0, last_id
            rows = [
                (str(row["id"]), str(source_id), notebook_id)
                for row in batch
                for source_id in self._runtime.knowledge.source_ids_from_evidence(
                    row["evidence"]
                )
            ]
            execute_many(
                db,
                "INSERT INTO knowledge_object_sources "
                "(object_id,source_id,notebook_id) VALUES (%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                rows,
            )
        return len(batch), len(rows), str(batch[-1]["id"])

    def mark_source_index_backfilled(self, notebook_id: str) -> None:
        with self._runtime.database.write() as db:
            self._runtime.knowledge.mark_source_index_backfilled(db, notebook_id)

    def source_fact_backfill_target_page(
        self, notebook_id: str, *, after_id: str = "", limit: int = 500
    ) -> list[str]:
        with self._runtime.database.connect() as db:
            rows = db.execute(
                "SELECT id FROM sources WHERE notebook_id=%s AND id COLLATE \"C\">%s "
                "AND source_type NOT IN ('memory','knowhow') "
                "ORDER BY id COLLATE \"C\" LIMIT %s",
                (notebook_id, after_id, max(1, min(int(limit), 2000))),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def backfill_source_fact_batch(
        self,
        notebook_id: str,
        source_id: str,
        *,
        batch_size: int = 500,
        projection_version: int = 1,
        force: bool = False,
    ) -> dict[str, object]:
        limit = max(1, min(int(batch_size), 2000))
        now = normalize_timestamp(self._runtime.seams.now())
        with self._runtime.database.write() as db:
            source = db.execute(
                "SELECT id FROM sources WHERE id=%s AND notebook_id=%s "
                "AND source_type NOT IN ('memory','knowhow') FOR UPDATE",
                (source_id, notebook_id),
            ).fetchone()
            if source is None:
                return {"status": "deleted", "done": True, "source_id": source_id}
            latest = db.execute(
                "SELECT id,status,error_message FROM extraction_runs "
                "WHERE notebook_id=%s AND source_id=%s AND run_type='kg' "
                "ORDER BY created_at DESC,id COLLATE \"C\" DESC LIMIT 1",
                (notebook_id, source_id),
            ).fetchone()
            if latest is None or latest["status"] == "running":
                return {"status": "busy" if latest else "no_generation",
                        "done": False if latest else True, "source_id": source_id}
            generation = ""
            message = str(latest["error_message"] or "")
            if latest["status"] == "completed" and message.startswith("kg objects="):
                generation = str(latest["id"])
            elif latest["status"] == "completed" and "retry_incomplete=1" in message:
                prior = db.execute(
                    "SELECT id FROM extraction_runs WHERE notebook_id=%s AND source_id=%s "
                    "AND run_type='kg' AND status='completed' "
                    "AND error_message LIKE 'kg objects=%%' "
                    "ORDER BY created_at DESC,id COLLATE \"C\" DESC LIMIT 1",
                    (notebook_id, source_id),
                ).fetchone()
                generation = str(prior["id"]) if prior else ""
            if not generation:
                return {"status": "no_generation", "done": True, "source_id": source_id}

            state = db.execute(
                "SELECT * FROM knowledge_source_fact_backfills WHERE source_id=%s",
                (source_id,),
            ).fetchone()
            reset = bool(state is None or force
                         or str(state["source_generation"]) != generation
                         or int(state["projection_version"]) != int(projection_version))
            if reset:
                db.execute(
                    "DELETE FROM knowledge_source_facts WHERE source_id=%s "
                    "AND (source_generation<>%s OR projection_origin='historical')",
                    (source_id, generation),
                )
                live_count = int(db.execute(
                    "SELECT COUNT(*) AS c FROM knowledge_source_facts "
                    "WHERE source_id=%s AND source_generation=%s "
                    "AND projection_origin='live'",
                    (source_id, generation),
                ).fetchone()["c"])
                db.execute(
                    "INSERT INTO knowledge_source_fact_backfills "
                    "(source_id,notebook_id,source_generation,projection_version,status,"
                    "after_object_id,objects_scanned,facts_written,incomplete_objects,"
                    "incomplete_reason,failure_code,created_at,updated_at) VALUES "
                    "(%s,%s,%s,%s,'running','',0,%s,0,'','',%s,%s) "
                    "ON CONFLICT(source_id) DO UPDATE SET notebook_id=excluded.notebook_id,"
                    "source_generation=excluded.source_generation,projection_version=excluded.projection_version,"
                    "status='running',after_object_id='',objects_scanned=0,"
                    "facts_written=excluded.facts_written,incomplete_objects=0,"
                    "incomplete_reason='',failure_code='',created_at=excluded.created_at,"
                    "updated_at=excluded.updated_at",
                    (
                        source_id, notebook_id, generation, projection_version,
                        live_count, now, now,
                    ),
                )
                after_id = ""
                scanned = incomplete = 0
                written = live_count
            else:
                if state["status"] in ("complete", "incomplete"):
                    return {**dict(state), "done": True}
                after_id = str(state["after_object_id"] or "")
                scanned = int(state["objects_scanned"])
                written = int(state["facts_written"])
                incomplete = int(state["incomplete_objects"])
                prior_reason = str(state["incomplete_reason"] or "")
                db.execute(
                    "UPDATE knowledge_source_fact_backfills SET status='running',"
                    "failure_code='',updated_at=%s WHERE source_id=%s",
                    (now, source_id),
                )

            reason_codes: set[str] = set()
            if not reset and incomplete:
                reason_codes.add(prior_reason or "prior_page")

            rows = db.execute(
                "WITH owned(id) AS ("
                " SELECT id FROM knowledge_objects WHERE notebook_id=%s "
                " AND source_id=%s AND id COLLATE \"C\">%s "
                " ORDER BY id COLLATE \"C\" LIMIT %s"
                "), supported(id) AS ("
                " SELECT object_id FROM knowledge_object_sources "
                " WHERE notebook_id=%s AND source_id=%s "
                " AND object_id COLLATE \"C\">%s "
                " ORDER BY object_id COLLATE \"C\" LIMIT %s"
                "), candidate_ids(id) AS (SELECT id FROM owned UNION SELECT id FROM supported) "
                "SELECT ko.id,ko.source_id,ko.object_type,ko.payload,ko.evidence,"
                "EXISTS(SELECT 1 FROM knowledge_source_facts f WHERE f.source_id=%s "
                "AND f.source_generation=%s AND f.global_object_id=ko.id "
                "AND f.projection_origin='live') AS has_live_fact "
                "FROM candidate_ids c JOIN knowledge_objects ko ON ko.id=c.id "
                "AND ko.notebook_id=%s ORDER BY ko.id COLLATE \"C\" LIMIT %s",
                (notebook_id, source_id, after_id, limit,
                 notebook_id, source_id, after_id, limit,
                 source_id, generation, notebook_id, limit),
            ).fetchall()
            rejected = 0
            candidates = []
            all_element_ids: list[str] = []
            for raw in rows:
                if raw["has_live_fact"]:
                    continue
                fact, reason = project_historical_source_fact(dict(raw), source_id, generation)
                if fact is None:
                    rejected += 1
                    reason_codes.add(reason or "unprojectable")
                else:
                    candidates.append(fact)
                    all_element_ids.extend(fact.element_ids)
            element_ids = list(dict.fromkeys(all_element_ids))
            owned: set[str] = set()
            if element_ids:
                owned = {
                    str(row["id"])
                    for row in db.execute(
                        "SELECT id FROM source_elements WHERE source_id=%s AND id=ANY(%s)",
                        (source_id, element_ids),
                    ).fetchall()
                }
            valid = []
            for fact in candidates:
                if not set(fact.element_ids).issubset(owned):
                    rejected += 1
                    reason_codes.add("missing_element_ownership")
                else:
                    valid.append(fact)
            if valid:
                local_ids = [fact.local_object_id for fact in valid]
                collisions = {
                    str(row["local_object_id"])
                    for row in db.execute(
                        "SELECT local_object_id FROM knowledge_source_facts "
                        "WHERE source_id=%s AND source_generation=%s "
                        "AND projection_origin='live' AND local_object_id=ANY(%s)",
                        (source_id, generation, local_ids),
                    ).fetchall()
                }
                if collisions:
                    rejected += sum(
                        fact.local_object_id in collisions for fact in valid
                    )
                    reason_codes.add("local_id_collision")
                    valid = [
                        fact for fact in valid
                        if fact.local_object_id not in collisions
                    ]
            fact_rows = [
                (f.fact_id, notebook_id, source_id, generation, f.local_object_id,
                 f.global_object_id, f.object_type, f.payload_json, f.evidence_json,
                 projection_version, now, now)
                for f in valid
            ]
            element_rows = [
                (f.fact_id, notebook_id, source_id, generation, element_id, now)
                for f in valid for element_id in f.element_ids
            ]
            self._runtime.knowledge.insert_source_fact_rows(
                db,
                fact_rows,
                element_rows,
                projection_origin="historical",
            )
            scanned += len(rows)
            written += len(valid)
            incomplete += rejected
            new_after = str(rows[-1]["id"]) if rows else after_id
            done = len(rows) < limit
            status = ("incomplete" if incomplete else "complete") if done else "running"
            incomplete_reason = sorted(reason_codes)[0] if incomplete else ""
            db.execute(
                "UPDATE knowledge_source_fact_backfills SET status=%s,after_object_id=%s,"
                "objects_scanned=%s,facts_written=%s,incomplete_objects=%s,"
                "incomplete_reason=%s,failure_code='',"
                "updated_at=%s WHERE source_id=%s AND source_generation=%s",
                (status, new_after, scanned, written, incomplete, incomplete_reason,
                 now, source_id, generation),
            )
            return {"source_id": source_id, "source_generation": generation,
                    "status": status, "done": done, "objects_scanned": scanned,
                    "facts_written": written, "incomplete_objects": incomplete,
                    "after_object_id": new_after}

    def mark_source_fact_backfill_failed(self, source_id: str, code: str) -> None:
        with self._runtime.database.write() as db:
            db.execute(
                "UPDATE knowledge_source_fact_backfills SET status='failed',"
                "failure_code=%s,updated_at=%s WHERE source_id=%s",
                (str(code)[:64], normalize_timestamp(self._runtime.seams.now()), source_id),
            )


__all__ = ["PostgresMaintenanceAdapter"]
