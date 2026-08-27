"""SQLite maintenance face for CLI/batch composition roots (Task 27).

``SQLiteMaintenanceAdapter`` implements ``SQLiteMaintenancePort`` and owns the
SQL that offline tooling (``app.services.batch_ingest``, ``backend/app/scripts``
and ``scripts/`` CLI utilities) used to run inline against private facade
members.  CLI composition roots instantiate ``SQLiteRepository`` and request
``repo.maintenance``; portable application ports never include these
operations.

``ReadOnlySQLiteInspector`` is the arbitrary-path ``mode=ro`` companion for
evaluation/comparison/validation tools (MRL truncation spike, KG snapshot
diffing) — read-only by construction, never the write path.

Both classes hold no facade backreference: the adapter receives the concrete
runtime (stores/services/database) plus the lazily-wired retrieval provider;
the inspector receives only a database path.
"""
from __future__ import annotations

from contextlib import nullcontext
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Iterator, Optional, Sequence

from app.domain.image_backfill import ImageBackfillConcurrentChange
from app.repositories.chunk_elements import (
    decode_element_ids,
    reverse_rows as chunk_element_reverse_rows,
    reverse_rows_for_writes as chunk_element_reverse_rows_for_writes,
)
from app.repositories.image_backfill_rows import image_backfill_state
from app.repositories.knowhow_asset_refs import (  # 后端中性,postgres maintenance 共用
    asset_ref_tokens,
    collect_change_payload_tokens,
    keepers_among,
)
from app.repositories.ports import VectorBatchEncoder
from app.repositories.source_fact_backfill import project_historical_source_fact
from app.repositories.text_whitespace import PY_WHITESPACE  # 后端中性,postgres maintenance 共用
from app.domain.kg.source_partition import SOURCE_PARTITION_FORMAT_VERSION
from app.domain.vector_index import decode_vector

# Rows consumed per fetch while streaming the orphan-asset keeper scan. A
# protocol boundary, not a budget: every matching row is still visited, only the
# resident Python-object window is bounded (the accumulated token set is what
# survives the pass, never the notebook's cell/payload text). See
# ``_surviving_asset_refs``.
_ASSET_SCAN_FETCH_ROWS = 200

# (table, id_column) for every embeddings table maintenance tooling touches.
VECTOR_TABLES = (
    ("chunk_embeddings", "chunk_id"),
    ("knowledge_embeddings", "object_id"),
    ("element_embeddings", "element_id"),
    ("relation_embeddings", "relation_id"),
)
_VECTOR_TABLE_IDS = dict(VECTOR_TABLES)

# PY_WHITESPACE(= Python str.strip() \u7684\u5168\u96c6)\u73b0\u79fb\u5230\u540e\u7aef\u4e2d\u6027\u7684
# app.repositories.text_whitespace,\u4f9b sqlite/postgres maintenance \u5171\u7528\u540c\u4e00\u4efd TRIM charset
# (\u89c1\u8be5\u6a21\u5757 docstring)\u3002\u8fd9\u91cc\u4ece\u9876\u90e8 import \u518d\u5bfc\u51fa,test_batch_ingest \u4ecd\u53ef\u4ece\u672c\u6a21\u5757\u53d6\u3002


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _require_vector_table(table: str, id_col: str) -> None:
    if _VECTOR_TABLE_IDS.get(table) != id_col:
        raise ValueError(f"unknown embeddings table: {table}/{id_col}")


class SQLiteMaintenanceAdapter:
    """Maintenance operations over the runtime-owned stores and services."""

    def __init__(self, runtime: Any, *, retrieval: Callable[[], Any]) -> None:
        self._runtime = runtime
        self._retrieval = retrieval
        # (notebook_id, asset_id) -> monotonic stamp of the FIRST sweep that
        # found this asset unreferenced. Drives sweep_orphan_assets' grace
        # period; see its docstring for why the clock has to start here rather
        # than at upload time. Deliberately in memory, not a column: losing it
        # on restart only RE-marks (delaying a deletion by one grace period),
        # which errs toward keeping the user's data — the safe direction — and
        # costs no migration/schema bump.
        self._orphan_first_seen: dict[tuple[str, str], float] = {}
        # Sweeps for different notebooks run in independent background threads
        # (the projection scheduler submits each as its own job), so every read
        # AND write of the mark dict has to hold this. Without it, one thread
        # iterating while another inserts raises "dictionary changed size during
        # iteration" — which the caller swallows, silently burning that
        # notebook's throttle slot and delaying its cleanup.
        self._orphan_marks_lock = threading.Lock()

    def offline_maintenance_lock(self):
        """SQLite already serializes writes through its database write lock."""
        return nullcontext()

    def build_chunk_question_index(
        self, notebook_id: str, *, workers: int, force: bool = False,
        progress=None,
    ) -> dict[str, int]:
        return self._runtime.chunk_question_index.build_notebook(
            notebook_id, workers=workers, force=force, progress=progress
        )

    # -- SQLiteMaintenancePort ------------------------------------------------

    def delete_notebook_kg(self, notebook_id: str) -> dict:
        return self._runtime.knowledge_lifecycle.delete_notebook_kg(notebook_id)

    def eval_insert_source_for_test(
        self, notebook_id: str, name: str, text: str, tmpdir: str
    ) -> str:
        from pathlib import Path
        from app.repositories.sqlite.source_store import SourceElementWrite
        from app.domain.kg.parsing import parse_elements

        path = Path(tmpdir) / f"{name}.md"
        path.write_text(text, encoding="utf-8")
        source_id = self._runtime.seams.new_id("src")
        now = self._runtime.seams.now()
        elements = parse_elements(text, source_file=str(path))
        with self._runtime.database.write() as db:
            self._runtime.source_store.insert_source(
                source_id=source_id,
                notebook_id=notebook_id,
                title=name,
                source_type="markdown",
                status="extracted",
                parse_status="parsed",
                file_name=f"{name}.md",
                file_path=str(path),
                file_size=0,
                file_hash="",
                summary="",
                doc_type="textbook",
                connection=db,
            )
            self._runtime.source_store.replace_elements(
                db,
                source_id,
                [
                    SourceElementWrite(
                        id=self._runtime.seams.new_id("el"),
                        element_type=element.type,
                        location_label=f"L{element.line_start}-{element.line_end}",
                        text=element.text,
                        metadata={},
                    )
                    for element in elements
                ],
                created_at=now,
            )
        return source_id

    def backfill_kg_fts(self, notebook_id: str) -> int:
        self._runtime.catalog.get_notebook(notebook_id)  # KeyError when missing
        with self._runtime.database.write() as db:
            return self._runtime.knowledge.backfill_fts(db, notebook_id)

    def backfill_chunk_fts(self, notebook_id: str) -> int:
        with self._runtime.database.write() as db:
            count = self._runtime.chunk_store.backfill_fts(db, notebook_id)
        # Chunk set was just (re)indexed — drop the corpus-language hint so it
        # re-samples (the runtime coordinator holds the shared cache dict).
        self._runtime.kg_mutations.notebook_languages.pop(notebook_id, None)
        return count

    def build_scale_index(
        self, notebook_id: str, on_stage: Optional[Callable[[str, int], None]] = None
    ) -> dict:
        return self._runtime.scale_artifacts.build(notebook_id, on_stage=on_stage)

    def fold_scale_index_delta(
        self, notebook_id: str, _assume_locked: bool = False
    ) -> dict:
        return self._runtime.scale_artifacts.fold(
            notebook_id, assume_locked=_assume_locked
        )

    # -- identity / notebooks -------------------------------------------------

    def resolve_owner_profile(self, owner: Optional[str]):
        """Resolve a notebook owner (username, case-insensitive) or the seeded
        admin (owner=None) to a UserProfile; None when not found."""
        with self._runtime.database.connect() as db:
            if owner is not None:
                from app.domain.auth_utils import normalize_username

                user = db.execute(
                    "SELECT * FROM users WHERE username=?",
                    (normalize_username(owner),),
                ).fetchone()
            else:
                user = db.execute(
                    "SELECT * FROM users WHERE role='admin' ORDER BY created_at ASC LIMIT 1"
                ).fetchone()
            if user is None:
                return None
            profile = db.execute(
                "SELECT * FROM user_profiles WHERE user_id=?", (user["id"],)
            ).fetchone()
        return self._runtime.identity._user_profile(user, profile)

    def resolve_notebook_owner_profile(self, notebook_id: str):
        """Resolve a notebook's OWNER (``notebooks.created_by`` -> that user's
        UserProfile); None when the notebook is missing, has no owner, or its
        owner user no longer exists. Sibling of ``resolve_owner_profile``
        (which keys on a username): offline tooling that must act AS the
        notebook owner -- ``scripts/backfill_knowhow_md.py``'s ``--use-llm``,
        which resolves the per-user rewrite model client -- looks the owner up
        by notebook_id and establishes that user's request context, so the
        OWNER's per-user model configuration is honored rather than the ambient
        default (user-local) the identity plumbing falls back to when no request
        user is set."""
        with self._runtime.database.connect() as db:
            notebook = db.execute(
                "SELECT created_by FROM notebooks WHERE id=?", (notebook_id,)
            ).fetchone()
            if notebook is None or not notebook["created_by"]:
                return None
            user = db.execute(
                "SELECT * FROM users WHERE id=?", (notebook["created_by"],)
            ).fetchone()
            if user is None:
                return None
            profile = db.execute(
                "SELECT * FROM user_profiles WHERE user_id=?", (user["id"],)
            ).fetchone()
        return self._runtime.identity._user_profile(user, profile)

    def all_notebook_ids(self) -> list[str]:
        with self._runtime.database.connect() as db:
            return [
                r["id"]
                for r in db.execute("SELECT id FROM notebooks ORDER BY id").fetchall()
            ]

    def notebook_rows(self) -> list[dict]:
        with self._runtime.database.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id, name, tier, created_by FROM notebooks"
                ).fetchall()
            ]

    # -- sources / extraction -------------------------------------------------

    def source_id_by_hash(self, notebook_id: str, digest: str) -> Optional[str]:
        """Delegates to the runtime's ``SourceStore`` — the single owner of this
        query. The maintenance face exists to keep CLI composition roots off
        private facade members, not to keep a second copy of the same SQL: a
        duplicated body would drift (ordering, index usage, "" handling) from
        the one ``upload_sources`` actually dedups against."""
        return self._runtime.source_store.source_id_by_hash(notebook_id, digest)

    def source_ids(self, notebook_id: str) -> list[str]:
        with self._runtime.database.connect() as db:
            return [
                r["id"]
                for r in db.execute(
                    "SELECT id FROM sources WHERE notebook_id=? ORDER BY id",
                    (notebook_id,),
                ).fetchall()
            ]

    def source_ids_page(
        self, notebook_id: str, *, after_id: str = "", limit: int = 500
    ) -> list[str]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._runtime.database.connect() as db:
            return [
                str(row["id"])
                for row in db.execute(
                    "SELECT id FROM sources WHERE notebook_id=? AND id>? "
                    "ORDER BY id LIMIT ?",
                    (notebook_id, after_id, int(limit)),
                ).fetchall()
            ]

    def user_source_ids_page(
        self, notebook_id: str, *, after_id: str = "", limit: int = 500
    ) -> list[str]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._runtime.database.connect() as db:
            rows = db.execute(
                "SELECT id FROM sources WHERE notebook_id=? AND id>? "
                "AND source_type NOT IN ('memory','knowhow') "
                "ORDER BY id LIMIT ?",
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
                "SELECT id,title FROM sources WHERE notebook_id=? AND id>? "
                "AND source_type NOT IN ('memory','knowhow') "
                "ORDER BY id LIMIT ?",
                (notebook_id, after_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def source_has_kg(self, source_id: str) -> bool:
        with self._runtime.database.connect() as db:
            return bool(
                db.execute(
                    "SELECT EXISTS(SELECT 1 FROM knowledge_objects "
                    "WHERE source_id=? AND source_id!='') AS found",
                    (source_id,),
                ).fetchone()["found"]
            )

    def source_has_elements(self, source_id: str) -> bool:
        with self._runtime.database.connect() as db:
            return bool(
                db.execute(
                    "SELECT EXISTS(SELECT 1 FROM source_elements "
                    "WHERE source_id=?) AS found",
                    (source_id,),
                ).fetchone()["found"]
            )

    def source_is_user_visible(self, notebook_id: str, source_id: str) -> bool:
        with self._runtime.database.connect() as db:
            return bool(
                db.execute(
                    "SELECT EXISTS(SELECT 1 FROM sources WHERE id=? "
                    "AND notebook_id=? "
                    "AND source_type NOT IN ('memory','knowhow')) AS found",
                    (source_id, notebook_id),
                ).fetchone()["found"]
            )

    def source_ids_missing_elements_page(
        self, notebook_id: str, *, after_id: str = "", limit: int = 500
    ) -> list[str]:
        """User-imported source repair targets; hidden projections are excluded."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._runtime.database.connect() as db:
            rows = db.execute(
                "SELECT s.id FROM sources s WHERE s.notebook_id=? AND s.id>? "
                "AND s.source_type NOT IN ('memory','knowhow') "
                "AND NOT EXISTS (SELECT 1 FROM source_elements e "
                "WHERE e.source_id=s.id) ORDER BY s.id LIMIT ?",
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
        """Bounded eligible extraction targets in deterministic id order."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._runtime.database.connect() as db:
            rows = db.execute(
                "WITH candidates AS ("
                " SELECT s.id,EXISTS(SELECT 1 FROM knowledge_objects ko "
                " WHERE ko.notebook_id=? AND ko.source_id=s.id "
                " AND ko.source_id!='') AS has_kg,"
                " COALESCE((SELECT ("
                "  er.error_message GLOB '*windows_failed=[1-9]*/*'"
                "  OR instr(er.error_message,'retry_incomplete=1')>0"
                " ) FROM extraction_runs er WHERE er.source_id=s.id "
                " AND er.run_type='kg' ORDER BY er.created_at DESC,er.rowid DESC "
                " LIMIT 1),0)"
                " AS is_partial FROM sources s WHERE s.notebook_id=? "
                " AND s.source_type NOT IN ('memory','knowhow') AND s.id>? "
                " AND EXISTS (SELECT 1 FROM source_elements pe "
                " WHERE pe.source_id=s.id)"
                ") SELECT id AS source_id,(has_kg AND is_partial) AS is_partial "
                "FROM candidates "
                "WHERE NOT has_kg"
                + (" OR is_partial" if retry_partial else "")
                + " ORDER BY id LIMIT ?",
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
                dict(r)
                for r in db.execute(
                    "SELECT id, title FROM sources WHERE notebook_id=? ORDER BY id",
                    (notebook_id,),
                ).fetchall()
            ]

    def set_sources_doc_type(self, notebook_id: str, doc_type: str) -> None:
        with self._runtime.database.write() as db:
            db.execute(
                "UPDATE sources SET doc_type=? WHERE notebook_id=? "
                "AND source_type NOT IN ('memory','knowhow')",
                (doc_type, notebook_id),
            )

    def kg_covered_source_ids(self, notebook_id: str) -> set:
        with self._runtime.database.connect() as db:
            return {
                r["source_id"]
                for r in db.execute(
                    "SELECT DISTINCT source_id FROM knowledge_objects "
                    "WHERE notebook_id=? AND source_id!=''",
                    (notebook_id,),
                ).fetchall()
            }

    def partial_kg_source_ids(self, notebook_id: str) -> set:
        """Sources whose latest KG run reports at least one failed window.

        Restrict this repair inventory to sources that still have graph objects;
        zero-object runs are already handled by the normal incremental KG pass.
        """
        with self._runtime.database.connect() as db:
            return {
                row["source_id"]
                for row in db.execute(
                    "WITH latest AS ("
                    "  SELECT er.source_id, er.error_message, "
                    "         ROW_NUMBER() OVER ("
                    "           PARTITION BY er.source_id "
                    "           ORDER BY er.created_at DESC, er.rowid DESC"
                    "         ) AS rn "
                    "  FROM extraction_runs er "
                    "  WHERE er.notebook_id=? AND er.run_type='kg'"
                    ") "
                    "SELECT l.source_id FROM latest l "
                    "JOIN sources s ON s.id=l.source_id "
                    "WHERE l.rn=1 AND s.source_type NOT IN ('memory','knowhow') "
                    "AND ("
                    "  l.error_message GLOB '*windows_failed=[1-9]*/*' "
                    "  OR instr(l.error_message, 'retry_incomplete=1') > 0"
                    ") "
                    "AND EXISTS ("
                    "  SELECT 1 FROM knowledge_objects ko "
                    "  WHERE ko.notebook_id=? AND ko.source_id=l.source_id"
                    ")",
                    (notebook_id, notebook_id),
                ).fetchall()
            }

    def sources_with_elements(self, notebook_id: str) -> set:
        """该 notebook 下已产出 source_elements(即已成功 parse)的 source_id 集合。
        run_all 用它区分「已 parse、缺 KG → extract_source 补抽」与「无 elements →
        必须 process_source 重新 parse」:无 elements 的源若被空抽,build_records 的
        接地校验没有 element 可对照,LLM 抽出的节点会被整源丢弃、objects=0。"""
        with self._runtime.database.connect() as db:
            return {
                r["source_id"]
                for r in db.execute(
                    "SELECT DISTINCT e.source_id FROM source_elements e "
                    "JOIN sources s ON s.id = e.source_id "
                    "WHERE s.notebook_id = ?",
                    (notebook_id,),
                ).fetchall()
            }

    def count_sources_missing_kg(self, notebook_id: str) -> int:
        with self._runtime.database.connect() as db:
            return db.execute(
                "SELECT COUNT(*) c FROM sources s WHERE s.notebook_id=? "
                "AND s.source_type NOT IN ('memory','knowhow') "
                "AND EXISTS (SELECT 1 FROM source_elements pe "
                "WHERE pe.source_id=s.id) "
                "AND NOT EXISTS (SELECT 1 FROM knowledge_objects k "
                "WHERE k.source_id=s.id AND k.source_id!='' "
                "AND COALESCE(("
                "  SELECT er.status FROM extraction_runs er "
                "  WHERE er.source_id=s.id AND er.run_type='kg' "
                "  ORDER BY er.created_at DESC, er.rowid DESC LIMIT 1"
                "), 'completed')='completed')",
                (notebook_id,),
            ).fetchone()["c"]

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
                "SELECT s.id FROM sources s WHERE s.notebook_id=? AND s.id>? "
                "AND s.source_type NOT IN ('memory','knowhow') "
                "AND s.doc_type IN ('','academic_paper') "
                "AND s.parse_status IN ('parsed','extracting','extracted')"
                + existing
                + " ORDER BY s.id LIMIT ?",
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
        # Late-bound through the runtime so component-seam monkeypatches
        # (repo._runtime.source_ingestion.run_extraction) keep observing.
        if preserve_existing_until_complete:
            return self._runtime.source_ingestion.run_extraction(
                source_id,
                preserve_existing_until_complete=True,
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
            source_id, status, summary=summary, error_message=error_message
        )

    def latest_extraction_run(self, source_id: str) -> Optional[dict]:
        with self._runtime.database.connect() as db:
            row = db.execute(
                "SELECT * FROM extraction_runs WHERE source_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (source_id,),
            ).fetchone()
        return dict(row) if row else None

    def seed_parsed_source(
        self,
        notebook_id: str,
        *,
        title: str,
        doc_type: str,
        file_name: str,
        file_path: str,
        elements: Sequence[dict],
        status: str = "extracted",
    ) -> str:
        """Insert an already-parsed source plus its elements (smoke seeding)."""
        sid = f"src-{uuid.uuid4().hex[:10]}"
        now = _now()
        with self._runtime.database.write() as db:
            db.execute(
                """INSERT INTO sources
                   (id, notebook_id, title, source_type, status, parse_status,
                    file_name, file_path, file_size, file_hash, summary, doc_type,
                    created_at, updated_at)
                   VALUES (?, ?, ?, 'markdown', ?, 'parsed', ?, ?, 0, '', '', ?, ?, ?)""",
                (sid, notebook_id, title, status, file_name, file_path, doc_type, now, now),
            )
            for el in elements:
                db.execute(
                    """INSERT INTO source_elements
                       (id, source_id, element_type, location_label, text, metadata, created_at)
                       VALUES (?, ?, ?, ?, ?, '{}', ?)""",
                    (
                        f"el-{uuid.uuid4().hex[:10]}",
                        sid,
                        el["element_type"],
                        el["location_label"],
                        el["text"],
                        now,
                    ),
                )
        return sid

    def seed_rule_object(
        self,
        notebook_id: str,
        *,
        payload: dict,
        evidence: list,
        source_id: str,
    ) -> str:
        """Insert one approved rule object (smoke seeding) + cache eviction."""
        object_id = f"ko-{uuid.uuid4().hex[:10]}"
        now = _now()
        with self._runtime.database.write() as db:
            db.execute(
                """
                INSERT INTO knowledge_objects
                (id, notebook_id, object_type, status, owner, payload, evidence,
                 source_candidate_id, source_id, created_at, updated_at)
                VALUES (?, ?, 'rule', 'approved', '', ?, ?, NULL, ?, ?, ?)
                """,
                (
                    object_id,
                    notebook_id,
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(evidence, ensure_ascii=False),
                    source_id,
                    now,
                    now,
                ),
            )
            self._runtime.knowledge.replace_object_sources(
                db,
                object_id,
                notebook_id,
                json.dumps(evidence, ensure_ascii=False),
            )
        self.invalidate_unified_cache(notebook_id)
        return object_id

    def invalidate_unified_cache(self, notebook_id: str) -> None:
        self._runtime.kg_mutations.invalidate_unified_cache(notebook_id)

    def sample_knowledge_objects(self, notebook_id: str, limit: int = 5) -> list[dict]:
        with self._runtime.database.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id, object_type, payload, evidence FROM knowledge_objects "
                    "WHERE notebook_id=? LIMIT ?",
                    (notebook_id, int(limit)),
                ).fetchall()
            ]

    def element_text(self, element_id: str) -> Optional[str]:
        with self._runtime.database.connect() as db:
            row = db.execute(
                "SELECT text FROM source_elements WHERE id=?", (element_id,)
            ).fetchone()
        return row["text"] if row else None

    # -- embeddings -----------------------------------------------------------

    def missing_chunk_embedding_rows(
        self,
        notebook_id: str,
        only_source_id: "str | None" = None,
    ) -> list[dict]:
        """``only_source_id`` 只取某源的 chunk——体检 backfill **持该源的分块锁、在锁内**逐源取
        缺失行再嵌(codex P1:element/chunk id 在 reparse 时复用,锁外读到的旧代行在替换后提交会
        挂上永久陈旧向量;锁内读→嵌保证 reparse 的换血不会插进来)。CLI 盘点/补齐不传、口径不变。

        ⚠ **无界版,已无生产调用方**(审计批4):交互式 backfill 与离线 CLI 都改走
        ``missing_chunk_embedding_page`` 的 keyset 分页。保留是因为它是分页版判据的参考
        实现——测试拿它跟分页版做等价差分。新代码不要再调它。"""
        params: list = [notebook_id]
        clause = ""
        if only_source_id is not None:
            clause += " AND c.source_id = ?"
            params.append(only_source_id)
        with self._runtime.database.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT c.id, c.source_id, c.text FROM chunks c WHERE c.notebook_id=? "
                    "AND NOT EXISTS (SELECT 1 FROM chunk_embeddings e "
                    "WHERE e.chunk_id=c.id)" + clause,
                    tuple(params),
                ).fetchall()
            ]

    def missing_element_embedding_rows(
        self,
        notebook_id: str,
        only_source_id: "str | None" = None,
    ) -> list[dict]:
        """该 notebook 下缺 element 向量、且文本非空白的 source_elements 行
        ([{"id","source_id","text"}, ...])。空白文本必须排除:embed_source 会跳过
        它们(text.strip() 为空),永远不会有向量——不排除的话补齐命令每次都报「还有
        N 个缺失」、每次都试着嵌入空串,成为永不收敛的脏状态。TRIM 的字符集用
        PY_WHITESPACE(= Python str.strip() 的全集),与 embed_source 的过滤逐字符一致。
        ``only_source_id`` 只取某源——体检 backfill **持该源的分块锁、在锁内**逐源取再嵌
        (codex P1:element id 在 reparse 时复用,锁外读到的旧代文本行在替换后提交会挂上永久
        陈旧向量;锁内读→嵌保证 reparse 换血不会插进来)。CLI 不传、口径不变。

        ⚠ **无界版,已无生产调用方**(审计批4):交互式 backfill 与离线 CLI 都改走
        ``missing_element_embedding_page``。保留作分页版判据的参考实现(测试做等价差分),
        新代码不要再调它。"""
        params: list = [notebook_id, PY_WHITESPACE]
        clause = ""
        if only_source_id is not None:
            clause += " AND e.source_id = ?"
            params.append(only_source_id)
        with self._runtime.database.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT e.id, e.source_id, e.text FROM source_elements e "
                    "JOIN sources s ON s.id = e.source_id "
                    "WHERE s.notebook_id=? "
                    "AND s.source_type NOT IN ('memory', 'knowhow') "
                    "AND TRIM(e.text, ?) != '' "
                    "AND NOT EXISTS (SELECT 1 FROM element_embeddings v "
                    "WHERE v.element_id = e.id)" + clause,
                    tuple(params),
                ).fetchall()
            ]

    # 单次发现 + 按 id hydrate 的一批参数上限(SQLite 老构建 SQLITE_MAX_VARIABLE_NUMBER=999)。
    _EMBEDDING_ID_IN_CHUNK = 900

    def missing_chunk_embedding_ids(
        self, notebook_id: str, *, only_source_id: "str | None" = None
    ) -> list[str]:
        """缺 chunk 向量的 **id 列表**(只投影 id,按 id 升序)——交互式 backfill 的**单次**
        发现查询(审计批4修订)。

        为什么不是 keyset 分页:本仓库**从不**对生产库跑 ``ANALYZE``,而
        ``missing_chunk_embedding_page`` 的 ``c.id > ?`` 在无统计信息时会被 planner 选成
        ``sqlite_autoindex_chunks_1 (id>?)`` 的**整表主键区间扫**、把 ``source_id`` 降级成残余
        过滤(EXPLAIN 实测)——于是「一个 21k 行的大源」= 43 页 × 43 次整表扫。改成一次发现
        之后,扫描回到**一次**(与切页之前逐字同一条判据、同一个代价),正文驻留仍由调用方
        按页 hydrate 保持有界:id 是几十字节,21k 个 id 的内存可忽略。

        判据与 ``missing_chunk_embedding_rows`` **逐字一致**(notebook + NOT EXISTS
        chunk_embeddings + 可选 source),只把投影换成 id 并加 ``ORDER BY``:
        - ``ORDER BY c.id`` 让调用方的 hydrate 页边界确定、可复现(与分页版同序);
        - 已登记的代价:无 ``ANALYZE`` 时本条走 ``idx_chunks_nb (notebook_id=?)``、即每个源
          一次 notebook 级索引扫(ANALYZE 过的库走 ``idx_chunks_source`` 逐源 seek)。这与
          切页**之前**的 rows 版是同一个计划,不是本次引入的代价。"""
        params: list = [notebook_id]
        clause = ""
        if only_source_id is not None:
            clause = " AND c.source_id=?"
            params.append(only_source_id)
        with self._runtime.database.connect() as db:
            return [
                r["id"]
                for r in db.execute(
                    "SELECT c.id FROM chunks c WHERE c.notebook_id=? "
                    "AND NOT EXISTS (SELECT 1 FROM chunk_embeddings e "
                    "WHERE e.chunk_id=c.id)" + clause + " ORDER BY c.id",
                    tuple(params),
                ).fetchall()
            ]

    def missing_element_embedding_ids(
        self, notebook_id: str, *, only_source_id: "str | None" = None
    ) -> list[str]:
        """缺 element 向量(且文本非空白)的 **id 列表**,按 id 升序——element 侧的单次发现
        查询。判据与 ``missing_element_embedding_rows`` 逐字一致(notebook + 排除
        memory/knowhow 合成源 + ``TRIM(text, PY_WHITESPACE) != ''`` + NOT EXISTS + 可选
        source),只把投影换成 id 并加 ``ORDER BY``。理由见
        ``missing_chunk_embedding_ids``。"""
        params: list = [notebook_id, PY_WHITESPACE]
        clause = ""
        if only_source_id is not None:
            clause = " AND e.source_id=?"
            params.append(only_source_id)
        with self._runtime.database.connect() as db:
            return [
                r["id"]
                for r in db.execute(
                    "SELECT e.id FROM source_elements e "
                    "JOIN sources s ON s.id=e.source_id "
                    "WHERE s.notebook_id=? "
                    "AND s.source_type NOT IN ('memory', 'knowhow') "
                    "AND TRIM(e.text, ?) != '' "
                    "AND NOT EXISTS (SELECT 1 FROM element_embeddings v "
                    "WHERE v.element_id=e.id)" + clause + " ORDER BY e.id",
                    tuple(params),
                ).fetchall()
            ]

    def chunk_texts_by_ids(self, ids: Sequence[str]) -> list[dict]:
        """按 id 取回 chunk 的 ``{"id","source_id","text"}``——**纯主键 hydrate,不重判判据**。

        id 由同一把源分块锁内的 ``missing_chunk_embedding_ids`` 产出(notebook / source /
        NOT EXISTS 都已判过),这里只把正文取回来。**刻意不带 notebook/source 谓词**:
        EXPLAIN 实测,``notebook_id=? AND id IN (...)`` 在无 ``ANALYZE`` 的库上会被 planner
        选成 ``idx_chunks_nb`` 的整 notebook 扫,而裸 ``id IN (...)`` 在有无统计信息下都是
        ``sqlite_autoindex_chunks_1 (id=?)`` 的主键 seek。行序不做保证(``IN`` 无序),
        调用方按发现序自行重排。"""
        values = list(ids)
        if not values:
            return []
        out: list[dict] = []
        with self._runtime.database.connect() as db:
            for offset in range(0, len(values), self._EMBEDDING_ID_IN_CHUNK):
                batch = values[offset:offset + self._EMBEDDING_ID_IN_CHUNK]
                marks = ",".join("?" for _ in batch)
                out.extend(
                    dict(r) for r in db.execute(
                        f"SELECT id, source_id, text FROM chunks WHERE id IN ({marks})",
                        tuple(batch),
                    ).fetchall()
                )
        return out

    def element_texts_by_ids(self, ids: Sequence[str]) -> list[dict]:
        """按 id 取回 element 的 ``{"id","source_id","text"}``——纯主键 hydrate,判据不重判;
        见 ``chunk_texts_by_ids``(同款理由、同款主键 seek)。"""
        values = list(ids)
        if not values:
            return []
        out: list[dict] = []
        with self._runtime.database.connect() as db:
            for offset in range(0, len(values), self._EMBEDDING_ID_IN_CHUNK):
                batch = values[offset:offset + self._EMBEDDING_ID_IN_CHUNK]
                marks = ",".join("?" for _ in batch)
                out.extend(
                    dict(r) for r in db.execute(
                        f"SELECT id, source_id, text FROM source_elements "
                        f"WHERE id IN ({marks})",
                        tuple(batch),
                    ).fetchall()
                )
        return out

    def missing_chunk_embedding_page(
        self,
        notebook_id: str,
        *,
        after_id: str = "",
        limit: int = 500,
        only_source_id: "str | None" = None,
    ) -> list[dict]:
        """One stable keyset page for bounded vector repair. Judged by exactly the
        same predicate as the unbounded rows version; only the keyset window and
        the ORDER BY are added. The offline CLI (``batch_ingest``) drains a whole
        notebook through this, one page at a time.

        ⚠ ``only_source_id`` has **no production caller**: the interactive
        backfill used it for one batch and moved to
        ``missing_chunk_embedding_ids`` + ``chunk_texts_by_ids`` because
        ``c.id > ?`` costs a whole-table primary-key range scan **per page** on
        an un-ANALYZEd database (see that method). It stays as the differential
        reference the equivalence test drives; do not wire it back into a
        per-source loop."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        params: list = [notebook_id, after_id]
        clause = ""
        if only_source_id is not None:
            clause = " AND c.source_id=?"
            params.append(only_source_id)
        params.append(int(limit))
        with self._runtime.database.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT c.id, c.source_id, c.text FROM chunks c "
                    "WHERE c.notebook_id=? AND c.id>? "
                    "AND NOT EXISTS (SELECT 1 FROM chunk_embeddings e "
                    "WHERE e.chunk_id=c.id)" + clause + " ORDER BY c.id LIMIT ?",
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
        """One stable keyset page for bounded element-vector repair; drained by the
        offline CLI. ``only_source_id`` has no production caller — see
        ``missing_chunk_embedding_page`` for why the interactive backfill moved to
        single-scan discovery plus primary-key hydration."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        params: list = [notebook_id, after_id, PY_WHITESPACE]
        clause = ""
        if only_source_id is not None:
            clause = " AND e.source_id=?"
            params.append(only_source_id)
        params.append(int(limit))
        with self._runtime.database.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT e.id, e.source_id, e.text FROM source_elements e "
                    "JOIN sources s ON s.id=e.source_id "
                    "WHERE s.notebook_id=? AND e.id>? "
                    "AND s.source_type NOT IN ('memory', 'knowhow') "
                    "AND TRIM(e.text, ?) != '' "
                    "AND NOT EXISTS (SELECT 1 FROM element_embeddings v "
                    "WHERE v.element_id=e.id)" + clause + " ORDER BY e.id LIMIT ?",
                    tuple(params),
                ).fetchall()
            ]

    def missing_chunk_vector_source_ids(self, notebook_id: str) -> list[str]:
        """有缺 chunk 向量的 **DISTINCT source_id**(不取正文)——体检 backfill 的轻量源发现
        (codex 第2轮 P1:大库上别把每行全文物化进内存仅为收 id,会 GB 级/OOM)。判据与
        missing_chunk_embedding_rows 的 NOT EXISTS 逐字一致,只把投影换成 DISTINCT source_id。"""
        with self._runtime.database.connect() as db:
            return [
                r["source_id"]
                for r in db.execute(
                    "SELECT DISTINCT c.source_id FROM chunks c WHERE c.notebook_id=? "
                    "AND NOT EXISTS (SELECT 1 FROM chunk_embeddings e WHERE e.chunk_id=c.id)",
                    (notebook_id,),
                ).fetchall()
            ]

    def missing_element_vector_source_ids(self, notebook_id: str) -> list[str]:
        """有缺 element 向量(且文本非空白)的 **DISTINCT source_id**——体检 backfill 的轻量源发现。
        判据(TRIM 非空白 + NOT EXISTS)与 missing_element_embedding_rows 逐字一致,只投影 source_id,
        不物化正文(codex 第2轮 P1)。"""
        with self._runtime.database.connect() as db:
            return [
                r["source_id"]
                for r in db.execute(
                    "SELECT DISTINCT e.source_id FROM source_elements e "
                    "JOIN sources s ON s.id = e.source_id "
                    "WHERE s.notebook_id=? "
                    "AND s.source_type NOT IN ('memory', 'knowhow') "
                    "AND TRIM(e.text, ?) != '' "
                    "AND NOT EXISTS (SELECT 1 FROM element_embeddings v WHERE v.element_id = e.id)",
                    (notebook_id, PY_WHITESPACE),
                ).fetchall()
            ]

    def count_missing_element_vectors(
        self, notebook_id: str, exclude_source_ids: "set[str] | None" = None
    ) -> int:
        """missing_element_embedding_rows 的计数版(盘点用)。判据必须与它逐字一致,
        否则「盘点数」和「实际可补数」会对不上。``exclude_source_ids`` 排除指定源的 element——
        体检 H5 传活跃租约快照,免得把**正在嵌入**的源误报缺向量(codex);CLI 盘点不传、逐字一致。

        **排除 memory/knowhow 隐藏合成源**(codex 第6轮 P2,element/rows/source_ids 三处同款,两后端
        对齐):它们**设计上就没有** element 向量——knowhow 投影只嵌生成的 chunk(embed_chunk_ids,
        workload=knowhow_embedding)、memory 走独立 memory_embedding,两者的 source_elements 都不走通用
        embed_source。含进来会:①H5 在成功投影后仍报损坏;②backfill 白嵌派生格(触效率红线)。与
        H2/H3/H6 的 memory/knowhow 排除口径一致。"""
        exclude = tuple(exclude_source_ids or ())
        clause = (
            f" AND e.source_id NOT IN ({','.join('?' * len(exclude))})" if exclude else ""
        )
        with self._runtime.database.connect() as db:
            return db.execute(
                "SELECT COUNT(*) c FROM source_elements e "
                "JOIN sources s ON s.id = e.source_id "
                "WHERE s.notebook_id=? "
                "AND s.source_type NOT IN ('memory', 'knowhow') "
                "AND TRIM(e.text, ?) != '' "
                "AND NOT EXISTS (SELECT 1 FROM element_embeddings v "
                "WHERE v.element_id = e.id)" + clause,
                (notebook_id, PY_WHITESPACE, *exclude),
            ).fetchone()["c"]

    def embed_elements_batch(self, notebook_id: str, items: list[dict]) -> int:
        return self._runtime.source_embedding.embed_elements_batch(notebook_id, items)

    def embed_chunks_batch(self, notebook_id: str, items: list[dict]) -> None:
        return self._runtime.source_embedding.embed_chunks_batch(notebook_id, items)

    def embed_chunks_for_source(self, source_id: str) -> None:
        return self._runtime.source_embedding.embed_chunks_for_source(source_id)

    def chunk_and_embed_source(self, source_id: str) -> None:
        return self._runtime.source_chunking.chunk_and_embed_source(source_id)

    def embed_objects_batch(
        self,
        notebook_id: str,
        items: list[dict],
        progress=None,
        commit_every: Optional[int] = None,
    ) -> int:
        return self._runtime.source_embedding.embed_objects_batch(
            notebook_id, items, progress=progress, commit_every=commit_every
        )

    def knowledge_object_payloads(
        self, notebook_id: str, *, include_deprecated: bool = False
    ) -> list[dict]:
        """[{"id", "payload": dict}, ...] for (re-)embedding tooling."""
        where = "WHERE notebook_id=?" + (
            "" if include_deprecated else " AND status!='deprecated'"
        )
        with self._runtime.database.connect() as db:
            rows = db.execute(
                f"SELECT id, payload FROM knowledge_objects {where}", (notebook_id,)
            ).fetchall()
        return [
            {"id": r["id"], "payload": json.loads(r["payload"] or "{}")} for r in rows
        ]

    def knowledge_object_payload_page(
        self,
        notebook_id: str,
        *,
        after_id: str = "",
        limit: int = 500,
        include_deprecated: bool = False,
    ) -> list[dict]:
        """Bounded keyset view for large-library maintenance callers."""
        status = "" if include_deprecated else " AND status!='deprecated'"
        with self._runtime.database.connect() as db:
            rows = db.execute(
                "SELECT id,payload FROM knowledge_objects "
                "WHERE notebook_id=? AND id>?"
                + status
                + " ORDER BY id LIMIT ?",
                (notebook_id, after_id, max(1, int(limit))),
            ).fetchall()
        return [
            {"id": r["id"], "payload": json.loads(r["payload"] or "{}")} for r in rows
        ]

    def backfill_node_embeddings(self, notebook_id: str, progress=None) -> int:
        """Embed missing object vectors in bounded pages without holding DB."""
        with self._runtime.database.connect() as db:
            total = int(db.execute(
                "SELECT COUNT(*) c FROM knowledge_objects "
                "WHERE notebook_id=? AND status!='deprecated'",
                (notebook_id,),
            ).fetchone()["c"])
        after_id = ""
        scanned = 0
        embedded = 0
        while True:
            with self._runtime.database.connect() as db:
                rows = db.execute(
                    "SELECT o.id,o.payload,(e.object_id IS NOT NULL) AS embedded "
                    "FROM knowledge_objects o LEFT JOIN knowledge_embeddings e "
                    "ON e.object_id=o.id "
                    "WHERE o.notebook_id=? AND o.status!='deprecated' "
                    "AND o.id>? ORDER BY o.id LIMIT 500",
                    (notebook_id, after_id),
                ).fetchall()
            if not rows:
                break
            after_id = str(rows[-1]["id"])
            scanned += len(rows)
            missing = [
                {"_oid": str(row["id"]), "payload": json.loads(row["payload"] or "{}")}
                for row in rows
                if not row["embedded"]
            ]
            if missing:
                embedded += self._runtime.source_embedding.embed_objects_batch(
                    notebook_id, missing
                )
            if progress:
                progress(scanned, total)
            if len(rows) < 500:
                break
        return embedded

    def node_embedding_counts(self, notebook_id: str) -> tuple:
        with self._runtime.database.connect() as db:
            objs = db.execute(
                "SELECT COUNT(*) c FROM knowledge_objects "
                "WHERE notebook_id=? AND status!='deprecated'",
                (notebook_id,),
            ).fetchone()["c"]
            emb = db.execute(
                "SELECT COUNT(*) c FROM knowledge_embeddings WHERE notebook_id=?",
                (notebook_id,),
            ).fetchone()["c"]
        return objs, emb

    def count_missing_chunk_vectors(
        self, notebook_id: str, exclude_source_ids: "set[str] | None" = None
    ) -> int:
        """缺 chunk 向量的 chunk 数。``exclude_source_ids`` 排除指定源的 chunk——体检 H4 传
        活跃租约快照,免得把**正在嵌入**的源(chunk 已在、向量还没落)误报成损坏(codex);
        CLI 盘点不传、口径不变。"""
        exclude = tuple(exclude_source_ids or ())
        clause = (
            f" AND c.source_id NOT IN ({','.join('?' * len(exclude))})" if exclude else ""
        )
        with self._runtime.database.connect() as db:
            return db.execute(
                "SELECT COUNT(*) c FROM chunks c WHERE c.notebook_id=? AND NOT EXISTS "
                "(SELECT 1 FROM chunk_embeddings e WHERE e.chunk_id=c.id)" + clause,
                (notebook_id, *exclude),
            ).fetchone()["c"]

    def count_missing_node_vectors(self, notebook_id: str) -> int:
        with self._runtime.database.connect() as db:
            return db.execute(
                "SELECT COUNT(*) c FROM knowledge_objects o WHERE o.notebook_id=? "
                "AND o.status!='deprecated' AND NOT EXISTS "
                "(SELECT 1 FROM knowledge_embeddings e WHERE e.object_id=o.id)",
                (notebook_id,),
            ).fetchone()["c"]

    def count_chunks(self, notebook_id: str) -> int:
        with self._runtime.database.connect() as db:
            return db.execute(
                "SELECT COUNT(*) c FROM chunks WHERE notebook_id=?", (notebook_id,)
            ).fetchone()["c"]

    def count_knowledge_embeddings(self, notebook_id: str) -> int:
        with self._runtime.database.connect() as db:
            return db.execute(
                "SELECT COUNT(*) c FROM knowledge_embeddings WHERE notebook_id=?",
                (notebook_id,),
            ).fetchone()["c"]

    def purge_kg_embeddings(self, notebook_id: str) -> None:
        with self._runtime.database.write() as db:
            db.execute(
                "DELETE FROM knowledge_embeddings WHERE notebook_id=?", (notebook_id,)
            )
            db.execute(
                "DELETE FROM relation_embeddings WHERE notebook_id=?", (notebook_id,)
            )

    def backfill_relation_embeddings(self, notebook_id: str) -> None:
        """给缺向量的关系补 relation_embeddings(幂等,只补缺失)。

        系统未绑定 ``relation_embedding`` 工作负载时 no-op。
        Canonical body (Task 27) — the facade keeps a frozen-signature delegate."""
        if not self._runtime.models.configured("relation_embedding"):
            return
        relations = self._retrieval().relations_with_names(notebook_id)
        with self._runtime.database.connect() as db:
            have = self._runtime.embedding_store.embedded_relation_ids(db, notebook_id)
        missing = [
            {"_rid": r["id"], "text": r["text"]}
            for r in relations
            if r["id"] not in have
        ]
        if missing:
            self._runtime.source_embedding.embed_relations_batch(notebook_id, missing)

    def mark_unified_kg_dirty(self, notebook_id: str) -> None:
        self._runtime.kg_mutations.mark_unified_kg_dirty(notebook_id)

    # -- retrieval-adjacent reads ----------------------------------------------

    def relations_with_names(
        self, notebook_id: str, relation_ids: Optional[list] = None
    ) -> list[dict]:
        return self._retrieval().relations_with_names(notebook_id, relation_ids)

    def knowledge_context(self, notebook_id: str, hits: Sequence[Any]) -> tuple:
        """(context block, id map) over retrieval hits — diagnostics use."""
        self._retrieval()  # ensure evidence-context wiring
        return self._runtime.evidence_context.knowledge_context(notebook_id, hits)

    def load_scale_index(self, notebook_id: str, allow_stale: bool = False):
        return self._runtime.scale_artifacts.load(notebook_id, allow_stale=allow_stale)

    def has_scale_index(self, notebook_id: str) -> bool:
        return self.load_scale_index(notebook_id) is not None

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

    def gold_knowledge_object_rows(self, notebook_id: str) -> list[dict]:
        with self._runtime.database.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id, payload FROM knowledge_objects "
                    "WHERE notebook_id=? AND status IN ('approved','reviewed')",
                    (notebook_id,),
                ).fetchall()
            ]

    # -- diagnostics (read-only projections) ------------------------------------

    def kg_object_counts_by_notebook(self) -> dict:
        with self._runtime.database.connect() as db:
            return {
                r["notebook_id"]: r["c"]
                for r in db.execute(
                    "SELECT notebook_id, COUNT(*) c FROM knowledge_objects "
                    "GROUP BY notebook_id"
                ).fetchall()
            }

    def latest_done_report(self) -> Optional[dict]:
        with self._runtime.database.connect() as db:
            row = db.execute(
                "SELECT id, notebook_id, question, references_json, sections_json, "
                "outline_json FROM reports WHERE status='done' "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def sample_approved_object_payload(self, notebook_id: str) -> Optional[str]:
        with self._runtime.database.connect() as db:
            row = db.execute(
                "SELECT payload FROM knowledge_objects WHERE notebook_id=? "
                "AND status='approved' LIMIT 1",
                (notebook_id,),
            ).fetchone()
        return row["payload"] if row else None

    def chunk_notebook_map(self, chunk_ids: Sequence[str]) -> dict:
        out: dict = {}
        ids = list(chunk_ids)
        if not ids:
            return out
        with self._runtime.database.connect() as db:
            for i in range(0, len(ids), 400):  # 分批防超 SQLite 变量上限
                part = ids[i : i + 400]
                ph = ",".join("?" for _ in part)
                for r in db.execute(
                    f"SELECT id, notebook_id FROM chunks WHERE id IN ({ph})", part
                ):
                    out[r["id"]] = r["notebook_id"]
        return out

    # -- vectors-to-blob / source-index backfills --------------------------------

    def count_text_vector_rows(self, table: str, id_col: str, notebook_id: Optional[str]) -> int:
        _require_vector_table(table, id_col)
        where = "WHERE typeof(vector)='text'"
        params: tuple = ()
        if notebook_id is not None:
            where += " AND notebook_id=?"
            params = (notebook_id,)
        with self._runtime.database.connect() as db:
            return db.execute(
                f"SELECT COUNT(*) c FROM {table} {where}", params
            ).fetchone()["c"]

    def convert_text_vector_batch(
        self,
        table: str,
        id_col: str,
        notebook_id: Optional[str],
        batch_size: int,
        encode: VectorBatchEncoder,
    ) -> tuple:
        """One write transaction: select up to batch_size legacy JSON-text
        vector rows, re-encode them via ``encode(rows) -> [(blob, nb, vid)]``
        (caller-supplied, serial or process-pool) and UPDATE in place.
        Returns (rows_processed, bad_rows)."""
        _require_vector_table(table, id_col)
        where = "WHERE typeof(vector)='text'"
        params: tuple = ()
        if notebook_id is not None:
            where += " AND notebook_id=?"
            params = (notebook_id,)
        with self._runtime.database.write() as db:
            rows = db.execute(
                f"SELECT {id_col} AS vid, notebook_id, vector FROM {table} {where} "
                f"LIMIT {int(batch_size)}",
                params,
            ).fetchall()
            if not rows:
                return 0, 0
            # sqlite3.Row supports mapping-style lookup but is not a
            # collections.abc.Mapping. Convert at this dialect boundary so
            # portable maintenance callbacks receive the neutral row shape.
            updates = encode([dict(row) for row in rows])
            bad = sum(1 for blob, _nb, _vid in updates if blob == b"")
            db.executemany(
                f"UPDATE {table} SET vector=? WHERE notebook_id=? AND {id_col}=?",
                updates,
            )
        return len(rows), bad

    def begin_source_index_backfill(
        self, notebook_id: str, *, force: bool = False
    ) -> dict[str, object]:
        """Start or resume one notebook's durable reverse-index rebuild."""
        now = self._runtime.seams.now()
        with self._runtime.database.write() as db:
            state = db.execute(
                "SELECT kg_mutation_seq,source_index_backfilled "
                "FROM unified_kg_state WHERE notebook_id=?",
                (notebook_id,),
            ).fetchone()
            mutation_seq = int(state["kg_mutation_seq"]) if state else 0
            marker = bool(state and state["source_index_backfilled"])
            progress = db.execute(
                "SELECT * FROM source_index_backfills WHERE notebook_id=?",
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
                        "SELECT COUNT(*) c FROM knowledge_objects WHERE notebook_id=?",
                        (notebook_id,),
                    ).fetchone()["c"]
                )
                rows_written = int(
                    db.execute(
                        "SELECT COUNT(*) c FROM knowledge_object_sources "
                        "WHERE notebook_id=?",
                        (notebook_id,),
                    ).fetchone()["c"]
                )
                db.execute(
                    "INSERT INTO source_index_backfills "
                    "(notebook_id,kg_mutation_seq,status,after_object_id,total_objects,"
                    "objects_scanned,rows_written,failure_code,created_at,updated_at,completed_at) "
                    "VALUES (?,?,'complete','',?,?,?,?,?,?,?) "
                    "ON CONFLICT(notebook_id) DO UPDATE SET "
                    "kg_mutation_seq=excluded.kg_mutation_seq,status='complete',"
                    "after_object_id='',total_objects=excluded.total_objects,"
                    "objects_scanned=excluded.objects_scanned,rows_written=excluded.rows_written,"
                    "failure_code='',updated_at=excluded.updated_at,completed_at=excluded.completed_at",
                    (
                        notebook_id,
                        mutation_seq,
                        total,
                        total,
                        rows_written,
                        "",
                        now,
                        now,
                        now,
                    ),
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
                    "updated_at=?,completed_at='' WHERE notebook_id=?",
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
                "DELETE FROM knowledge_object_sources WHERE notebook_id=?",
                (notebook_id,),
            )
            db.execute(
                "UPDATE unified_kg_state SET source_index_backfilled=0,updated_at=? "
                "WHERE notebook_id=?",
                (now, notebook_id),
            )
            total = int(
                db.execute(
                    "SELECT COUNT(*) c FROM knowledge_objects WHERE notebook_id=?",
                    (notebook_id,),
                ).fetchone()["c"]
            )
            status = "complete" if total == 0 else "running"
            completed_at = now if total == 0 else ""
            db.execute(
                "INSERT INTO source_index_backfills "
                "(notebook_id,kg_mutation_seq,status,after_object_id,total_objects,"
                "objects_scanned,rows_written,failure_code,created_at,updated_at,completed_at) "
                "VALUES (?,?,?,'',?,0,0,'',?,?,?) "
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
        from app.repositories.sqlite.knowledge_store import KnowledgeStore

        limit = max(1, min(int(batch_size), 10_000))
        now = self._runtime.seams.now()
        with self._runtime.database.write() as db:
            progress = db.execute(
                "SELECT * FROM source_index_backfills WHERE notebook_id=?",
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
                "SELECT kg_mutation_seq FROM unified_kg_state WHERE notebook_id=?",
                (notebook_id,),
            ).fetchone()
            mutation_seq = int(state["kg_mutation_seq"]) if state else 0
            if mutation_seq != int(progress["kg_mutation_seq"]):
                db.execute(
                    "UPDATE source_index_backfills SET status='failed',"
                    "failure_code='kg_generation_changed',updated_at=? WHERE notebook_id=?",
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
                "WHERE notebook_id=? AND id>? ORDER BY id LIMIT ?",
                (notebook_id, progress["after_object_id"], limit),
            ).fetchall()
            kos_rows = [
                (row["id"], source_id, notebook_id)
                for row in batch
                for source_id in KnowledgeStore.source_ids_from_evidence(row["evidence"])
            ]
            if kos_rows:
                db.executemany(
                    "INSERT OR IGNORE INTO knowledge_object_sources "
                    "(object_id,source_id,notebook_id) VALUES (?,?,?)",
                    kos_rows,
                )
            scanned = int(progress["objects_scanned"]) + len(batch)
            written = int(progress["rows_written"]) + len(kos_rows)
            done = not batch or scanned >= int(progress["total_objects"])
            status = "complete" if done else "running"
            cursor = batch[-1]["id"] if batch else progress["after_object_id"]
            db.execute(
                "UPDATE source_index_backfills SET status=?,after_object_id=?,"
                "objects_scanned=?,rows_written=?,failure_code='',updated_at=?,completed_at=? "
                "WHERE notebook_id=?",
                (status, cursor, scanned, written, now, now if done else "", notebook_id),
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
                "batch_rows": len(kos_rows),
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
                "UPDATE source_index_backfills SET status='failed',failure_code=?,"
                "updated_at=? WHERE notebook_id=? AND status!='complete'",
                (code, self._runtime.seams.now(), notebook_id),
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
        now = self._runtime.seams.now()
        with self._runtime.database.write() as db:
            state = db.execute(
                "SELECT kg_mutation_seq,chunk_elements_indexed "
                "FROM unified_kg_state WHERE notebook_id=?",
                (notebook_id,),
            ).fetchone()
            mutation_seq = int(state["kg_mutation_seq"]) if state else 0
            marker = bool(state and state["chunk_elements_indexed"])
            progress = db.execute(
                "SELECT * FROM chunk_element_backfills WHERE notebook_id=?",
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
                        "SELECT COUNT(*) c FROM chunks WHERE notebook_id=?",
                        (notebook_id,),
                    ).fetchone()["c"]
                )
                rows_written = int(
                    db.execute(
                        "SELECT COUNT(*) c FROM chunk_elements WHERE notebook_id=?",
                        (notebook_id,),
                    ).fetchone()["c"]
                )
                db.execute(
                    "INSERT INTO chunk_element_backfills "
                    "(notebook_id,kg_mutation_seq,status,after_chunk_id,total_chunks,"
                    "chunks_scanned,rows_written,failure_code,created_at,updated_at,completed_at) "
                    "VALUES (?,?,'complete','',?,?,?,'',?,?,?) "
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
                    "updated_at=?,completed_at='' WHERE notebook_id=?",
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
                "DELETE FROM chunk_elements WHERE notebook_id=?", (notebook_id,)
            )
            db.execute(
                "UPDATE unified_kg_state SET chunk_elements_indexed=0,updated_at=? "
                "WHERE notebook_id=?",
                (now, notebook_id),
            )
            total = int(
                db.execute(
                    "SELECT COUNT(*) c FROM chunks WHERE notebook_id=?",
                    (notebook_id,),
                ).fetchone()["c"]
            )
            status = "complete" if total == 0 else "running"
            completed_at = now if total == 0 else ""
            db.execute(
                "INSERT INTO chunk_element_backfills "
                "(notebook_id,kg_mutation_seq,status,after_chunk_id,total_chunks,"
                "chunks_scanned,rows_written,failure_code,created_at,updated_at,completed_at) "
                "VALUES (?,?,?,'',?,0,0,'',?,?,?) "
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
        now = self._runtime.seams.now()
        with self._runtime.database.write() as db:
            progress = db.execute(
                "SELECT * FROM chunk_element_backfills WHERE notebook_id=?",
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
                "SELECT kg_mutation_seq FROM unified_kg_state WHERE notebook_id=?",
                (notebook_id,),
            ).fetchone()
            mutation_seq = int(state["kg_mutation_seq"]) if state else 0
            if mutation_seq != int(progress["kg_mutation_seq"]):
                db.execute(
                    "UPDATE chunk_element_backfills SET status='failed',"
                    "failure_code='kg_generation_changed',updated_at=? WHERE notebook_id=?",
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
                "WHERE notebook_id=? AND id>? ORDER BY id LIMIT ?",
                (notebook_id, progress["after_chunk_id"], limit),
            ).fetchall()
            rows = chunk_element_reverse_rows(notebook_id, batch)
            if rows:
                db.executemany(
                    "INSERT OR IGNORE INTO chunk_elements "
                    "(notebook_id,element_id,chunk_id) VALUES (?,?,?)",
                    rows,
                )
            scanned = int(progress["chunks_scanned"]) + len(batch)
            written = int(progress["rows_written"]) + len(rows)
            # Terminate ONLY on an exhausted cursor, never on a scanned/total
            # comparison. ``total_chunks`` is frozen at begin() while the write
            # path keeps inserting chunks; any new chunk whose id sorts after
            # the current cursor is still ahead of it, so a later page scans it
            # and counts it into ``scanned``. ``scanned`` therefore reaches
            # ``total_chunks`` BEFORE the cursor has walked the historical set,
            # and a `scanned >= total` gate would flip the marker — and with it
            # the read path — while genuinely old chunks were still
            # unprojected, which fails silently and never self-heals.
            # ``total_chunks`` is progress display only.
            done = not batch
            status = "complete" if done else "running"
            cursor = batch[-1]["id"] if batch else progress["after_chunk_id"]
            db.execute(
                "UPDATE chunk_element_backfills SET status=?,after_chunk_id=?,"
                "chunks_scanned=?,rows_written=?,failure_code='',updated_at=?,completed_at=? "
                "WHERE notebook_id=?",
                (status, cursor, scanned, written, now, now if done else "", notebook_id),
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
                "UPDATE chunk_element_backfills SET status='failed',failure_code=?,"
                "updated_at=? WHERE notebook_id=? AND status!='complete'",
                (code, self._runtime.seams.now(), notebook_id),
            )

    def clear_chunk_element_index(self, notebook_id: str) -> int:
        """Reset chunk_elements for one notebook; returns the chunks total the
        backfill loop must cover."""
        with self._runtime.database.write() as db:
            db.execute(
                "DELETE FROM chunk_elements WHERE notebook_id=?", (notebook_id,)
            )
            # Incomplete from here until the final marker write. Reset the
            # fast-path flag in the same transaction so an interrupted CLI can
            # never publish a partial index.
            db.execute(
                "UPDATE unified_kg_state SET chunk_elements_indexed=0, "
                "updated_at=? WHERE notebook_id=?",
                (self._runtime.seams.now(), notebook_id),
            )
            return db.execute(
                "SELECT COUNT(*) c FROM chunks WHERE notebook_id=?",
                (notebook_id,),
            ).fetchone()["c"]

    def chunk_elements_indexed(self, notebook_id: str) -> bool:
        with self._runtime.database.connect() as db:
            return self._runtime.knowledge.chunk_elements_indexed(db, notebook_id)

    # ------------------------------------------------- backfill-images (离线)
    # `batch_ingest backfill-images` 的三个读写口。刻意不进 `ports.py` 的
    # Protocol：那个文件的方法总数钉在零余量上限上（只许降），而这三个方法只有
    # 一个离线阶段消费——它按自己模块里的窄 Protocol
    # (`image_backfill_phase.ImageBackfillPort`) 结构化依赖它们，PostgreSQL 侧
    # 逐字对等。

    def image_backfill_source_page(
        self, notebook_id: str, after_id: str, limit: int
    ) -> list[dict]:
        """一页 markdown 候选来源（keyset，按 id）。

        判据是「用户可见来源 + 文件名以 .md/.markdown 结尾」，**不**筛"零元素"：
        本阶段修的正是已经解析成功、只是丢了图的来源。"""
        with self._runtime.database.connect() as db:
            rows = db.execute(
                "SELECT id, file_name, file_path FROM sources "
                "WHERE notebook_id=? AND source_type NOT IN ('memory','knowhow') "
                "AND (lower(file_name) LIKE '%.md' OR lower(file_name) LIKE '%.markdown') "
                "AND id > ? ORDER BY id LIMIT ?",
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
        """回填一个来源所需的全部既有状态，一次读完。

        元素按 ``ORDER BY id`` —— 与 `source_elements_for_chunking` 同一口径
        （id 字典序即文档序），对齐算法依赖的就是这个顺序。``element_created_at``
        是该来源既有元素批次的时间戳：新图片元素必须回填成它，否则来源详情
        按 ``(created_at,id)`` 分页会把图片全部沉底，命令目录的
        ``source_element_generation``（= MAX(created_at)）也会平白漂移，把每一份
        待审候选作废掉。"""
        with self._runtime.database.connect() as db:
            element_rows = db.execute(
                "SELECT id, element_type, text, metadata, created_at "
                "FROM source_elements WHERE source_id=? ORDER BY id",
                (source_id,),
            ).fetchall()
            chunk_rows = db.execute(
                "SELECT id, element_ids FROM chunks WHERE source_id=? ORDER BY id",
                (source_id,),
            ).fetchall()
        return image_backfill_state(element_rows, chunk_rows)

    def apply_image_backfill(
        self,
        notebook_id: str,
        source_id: str,
        elements: Sequence[dict],
        chunk_updates: Sequence[dict],
        metadata_updates: Sequence[dict] = (),
        *,
        created_at: str,
        expected_element_count: int,
    ) -> None:
        """一个来源的全部插入与就地补齐，**一个写事务**。

        `chunk_updates` 是受影响 chunk 的 ``{id, expected, element_ids}``：
        ``expected`` 是计划阶段看到的旧列表（CAS 判据），``element_ids`` 是**完整**
        的新列表。`chunks.text` 与 chunk id 逐字节不变，所以 `chunk_embeddings`
        （`ON DELETE CASCADE` 挂在 chunk 行上）一行都不会动。反查行
        `chunk_elements` 与它们描述的那次 chunk 写入同事务（v46 红线），
        `sources.updated_at` 也在同一事务里推进（元素换代的变更信号）。

        ``metadata_updates`` 是"就地补齐"那一半（见
        `image_backfill.EnrichedImage`）：只改既有 image 元素的 ``metadata``
        （补 ``asset_id``），``text``/``id``/``created_at`` 一律不动。它与元素
        插入必须同事务——它描述的资产行已经写进 `notebook_assets` 了，落在第二
        个事务里就会留下"资产在、元素还指不到它"的中间态。

        **事务里的第一件事是 CAS**，不是写。本阶段是离线工具，但 SQLite 后端没有
        PostgreSQL 那道"构造 repository 前强制 `--confirm-service-stopped`"的
        preflight，所以它可能与一个仍在跑的服务并发。计划与写入之间若发生了对同一
        来源的重新解析，快照就已作废——照它写下去会把旧 ``element_ids`` 整份盖回
        换代后的 chunk、把新图挂到已删的锚点 id 上，而且不报任何错。所以先重读
        元素代次信号 ``(COUNT, MAX(created_at))`` 与每个目标 chunk 的现值做比对，
        不一致抛 `ImageBackfillConcurrentChange` 让整个事务回滚（语义是"稍后重跑"，
        不是失败）。

        ``updated_at`` 刻意**不**由调用方传：时间戳格式是仓储自己的约定（带微秒
        与本地偏移的 ISO），而 `sources.updated_at` 是被当作字符串比较的变更信号
        令牌——让离线调用方自带一个"看起来像 ISO"的时钟，写进去的就是一个比旧值
        还小的字符串。"""
        with self._runtime.database.write() as db:
            signature = db.execute(
                "SELECT COUNT(*) AS n, MAX(created_at) AS newest "
                "FROM source_elements WHERE source_id=?",
                (source_id,),
            ).fetchone()
            if (
                int(signature["n"]) != int(expected_element_count)
                or signature["newest"] != created_at
            ):
                raise ImageBackfillConcurrentChange(
                    f"source {source_id} changed generation between plan and apply"
                )
            for update in chunk_updates:
                current = db.execute(
                    "SELECT element_ids FROM chunks WHERE id=? AND source_id=?",
                    (update["id"], source_id),
                ).fetchone()
                if current is None or decode_element_ids(
                    current["element_ids"]
                ) != list(update["expected"]):
                    raise ImageBackfillConcurrentChange(
                        f"chunk {update['id']} of source {source_id} changed "
                        "between plan and apply"
                    )
            db.executemany(
                "INSERT INTO source_elements "
                "(id, source_id, element_type, location_label, text, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        element["id"],
                        source_id,
                        element["element_type"],
                        element["location_label"],
                        element["text"],
                        json.dumps(dict(element["metadata"]), ensure_ascii=False),
                        created_at,
                    )
                    for element in elements
                ],
            )
            for update in metadata_updates:
                db.execute(
                    "UPDATE source_elements SET metadata=? "
                    "WHERE id=? AND source_id=?",
                    (
                        json.dumps(dict(update["metadata"]), ensure_ascii=False),
                        update["id"],
                        source_id,
                    ),
                )
            for update in chunk_updates:
                db.execute(
                    "UPDATE chunks SET element_ids=? WHERE id=? AND source_id=?",
                    (json.dumps(list(update["element_ids"])), update["id"], source_id),
                )
            rows = chunk_element_reverse_rows_for_writes(
                notebook_id,
                [(update["id"], update["element_ids"]) for update in chunk_updates],
            )
            if rows:
                db.executemany(
                    "INSERT OR IGNORE INTO chunk_elements "
                    "(notebook_id,element_id,chunk_id) VALUES (?,?,?)",
                    rows,
                )
            db.execute(
                "UPDATE sources SET updated_at=? WHERE id=?",
                (self._runtime.seams.now(), source_id),
            )

    def image_backfill_resolve_source_path(self, file_path: str) -> str:
        """把 ``sources.file_path`` 解析成可直接打开的路径（零 I/O、零查询）。

        刻意复用 `SourceFileStore.resolve_path`——它就是产品对来源文件路径的那**一
        条**统一约定（绝对路径原样、相对路径按仓库根解析；`read_source_text` 的
        docstring 写明"raw text reads accept both absolute stored paths and legacy
        repo-root-relative ones"）。放在仓储适配器上而不是让离线阶段自己算，是因为
        仓库根 (`root_dir`) 归数据库边界所有，服务层拿不到；自造第二份规则会让
        离线命令与在线读取对同一批历史行给出不同答案。"""
        return str(self._runtime.source_files.resolve_path(file_path))

    def image_backfill_source_asset_ids(
        self, notebook_id: str, source_id: str
    ) -> list[str]:
        """该来源名下全部 ``notebook_assets`` 行的 id（只读）。

        供离线阶段做**本趟范围内**的孤儿清扫：动手前后各取一次，只有差集里
        没人引用的那些才删。两个谓词都要——``source_id`` 是主键据，
        ``notebook_id`` 是纵深防御（资产行的 notebook 归属由 `save_source_image`
        与来源绑定，多带一个谓词让一条错挂的历史行不至于被跨库删掉）。"""
        with self._runtime.database.connect() as db:
            rows = db.execute(
                "SELECT id FROM notebook_assets "
                "WHERE notebook_id=? AND source_id=? ORDER BY id",
                (notebook_id, source_id),
            ).fetchall()
        return [row["id"] for row in rows]

    def image_backfill_discard_assets(self, asset_ids: Sequence[str]) -> list[dict]:
        """删掉这一批 ``notebook_assets`` 行并原样返回，供调用方 unlink 文件。

        只在"资产已写、元素事务失败"这条回滚路径上用。资产必须先落库（
        `AssetService.save_source_image` 有自己的事务），所以那一步与元素事务之间
        存在一个失败窗口；`sweep_orphan_assets` 明确排除 ``source_id`` 非空的行
        （来源插图只经来源级联清理），孤儿行不会被任何后台扫回收，因此这条回滚
        必须自己删干净。"""
        ids = [asset_id for asset_id in dict.fromkeys(asset_ids) if asset_id]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self._runtime.database.write() as db:
            rows = db.execute(
                f"DELETE FROM notebook_assets WHERE id IN ({placeholders}) RETURNING *",
                ids,
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_source_index(self, notebook_id: str) -> int:
        """Reset knowledge_object_sources for one notebook; returns the
        knowledge_objects total the backfill loop must cover."""
        with self._runtime.database.write() as db:
            db.execute(
                "DELETE FROM knowledge_object_sources WHERE notebook_id=?",
                (notebook_id,),
            )
            # The reverse index is incomplete from this point until the final
            # marker write.  Reset the fast-path flag in the same transaction
            # so an interrupted CLI can never publish a partial index.
            db.execute(
                "UPDATE unified_kg_state SET source_index_backfilled=0, "
                "updated_at=? WHERE notebook_id=?",
                (self._runtime.seams.now(), notebook_id),
            )
            return db.execute(
                "SELECT COUNT(*) c FROM knowledge_objects WHERE notebook_id=?",
                (notebook_id,),
            ).fetchone()["c"]

    def source_index_backfilled(self, notebook_id: str) -> bool:
        with self._runtime.database.connect() as db:
            return self._runtime.knowledge.source_index_backfilled(db, notebook_id)

    def backfill_source_index_batch(
        self, notebook_id: str, last_id: str, batch_size: int
    ) -> tuple:
        """One write transaction of the knowledge_object_sources backfill.
        Returns (batch_rows, rows_written, new_last_id)."""
        from app.repositories.sqlite.knowledge_store import KnowledgeStore

        with self._runtime.database.write() as db:
            batch = db.execute(
                "SELECT id, evidence FROM knowledge_objects "
                "WHERE notebook_id=? AND id > ? ORDER BY id LIMIT ?",
                (notebook_id, last_id, int(batch_size)),
            ).fetchall()
            if not batch:
                return 0, 0, last_id
            kos_rows = [
                (row["id"], sid, notebook_id)
                for row in batch
                for sid in KnowledgeStore.source_ids_from_evidence(row["evidence"])
            ]
            if kos_rows:
                db.executemany(
                    "INSERT INTO knowledge_object_sources (object_id, source_id, notebook_id) "
                    "VALUES (?, ?, ?)",
                    kos_rows,
                )
        return len(batch), len(kos_rows), batch[-1]["id"]

    def mark_source_index_backfilled(self, notebook_id: str) -> None:
        with self._runtime.database.write() as db:
            self._runtime.knowledge.mark_source_index_backfilled(db, notebook_id)

    def source_fact_backfill_target_page(
        self, notebook_id: str, *, after_id: str = "", limit: int = 500
    ) -> list[str]:
        with self._runtime.database.connect() as db:
            return [
                str(row["id"])
                for row in db.execute(
                    "SELECT id FROM sources WHERE notebook_id=? AND id>? "
                    "AND source_type NOT IN ('memory','knowhow') "
                    "ORDER BY id LIMIT ?",
                    (notebook_id, after_id, max(1, min(int(limit), 2000))),
                ).fetchall()
            ]

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
        now = self._runtime.seams.now()
        with self._runtime.database.write() as db:
            source = db.execute(
                "SELECT id FROM sources WHERE id=? AND notebook_id=? "
                "AND source_type NOT IN ('memory','knowhow')",
                (source_id, notebook_id),
            ).fetchone()
            if source is None:
                return {"status": "deleted", "done": True, "source_id": source_id}
            latest = db.execute(
                "SELECT id,status,error_message FROM extraction_runs "
                "WHERE notebook_id=? AND source_id=? AND run_type='kg' "
                "ORDER BY created_at DESC,id DESC LIMIT 1",
                (notebook_id, source_id),
            ).fetchone()
            if latest is None or latest["status"] == "running":
                return {
                    "status": "busy" if latest else "no_generation",
                    "done": False if latest else True,
                    "source_id": source_id,
                }
            generation = ""
            message = str(latest["error_message"] or "")
            if latest["status"] == "completed" and message.startswith("kg objects="):
                generation = str(latest["id"])
            elif latest["status"] == "completed" and "retry_incomplete=1" in message:
                prior = db.execute(
                    "SELECT id FROM extraction_runs WHERE notebook_id=? AND source_id=? "
                    "AND run_type='kg' AND status='completed' "
                    "AND error_message LIKE 'kg objects=%' "
                    "ORDER BY created_at DESC,id DESC LIMIT 1",
                    (notebook_id, source_id),
                ).fetchone()
                generation = str(prior["id"]) if prior else ""
            if not generation:
                return {"status": "no_generation", "done": True, "source_id": source_id}

            state = db.execute(
                "SELECT * FROM knowledge_source_fact_backfills WHERE source_id=?",
                (source_id,),
            ).fetchone()
            reset = bool(
                state is None or force
                or str(state["source_generation"]) != generation
                or int(state["projection_version"]) != int(projection_version)
            )
            if reset:
                # Preserve current-generation live facts published atomically by
                # ingestion.  Only stale generations and prior legacy
                # projections are rebuildable backfill output.
                db.execute(
                    "DELETE FROM knowledge_source_facts WHERE source_id=? "
                    "AND (source_generation<>? OR projection_origin='historical')",
                    (source_id, generation),
                )
                live_count = int(db.execute(
                    "SELECT COUNT(*) AS c FROM knowledge_source_facts "
                    "WHERE source_id=? AND source_generation=? "
                    "AND projection_origin='live'",
                    (source_id, generation),
                ).fetchone()["c"])
                db.execute(
                    "INSERT INTO knowledge_source_fact_backfills "
                    "(source_id,notebook_id,source_generation,projection_version,status,"
                    "after_object_id,objects_scanned,facts_written,incomplete_objects,"
                    "incomplete_reason,failure_code,created_at,updated_at) "
                    "VALUES (?,?,?,?, 'running','',0,?,0,'','',?,?) "
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
                    "failure_code='',updated_at=? WHERE source_id=?",
                    (now, source_id),
                )

            reason_codes: set[str] = set()
            if not reset and incomplete:
                reason_codes.add(prior_reason or "prior_page")

            rows = db.execute(
                "WITH owned(id) AS ("
                " SELECT id FROM knowledge_objects WHERE notebook_id=? "
                " AND source_id=? AND id>? ORDER BY id LIMIT ?"
                "), supported(id) AS ("
                " SELECT object_id FROM knowledge_object_sources "
                " WHERE notebook_id=? AND source_id=? AND object_id>? "
                " ORDER BY object_id LIMIT ?"
                "), candidate_ids(id) AS (SELECT id FROM owned UNION SELECT id FROM supported) "
                "SELECT ko.id,ko.source_id,ko.object_type,ko.payload,ko.evidence,"
                "EXISTS(SELECT 1 FROM knowledge_source_facts f WHERE f.source_id=? "
                "AND f.source_generation=? AND f.global_object_id=ko.id "
                "AND f.projection_origin='live') AS has_live_fact "
                "FROM candidate_ids c JOIN knowledge_objects ko ON ko.id=c.id "
                "AND ko.notebook_id=? ORDER BY ko.id LIMIT ?",
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
                fact, reason = project_historical_source_fact(
                    dict(raw), source_id, generation
                )
                if fact is None:
                    rejected += 1
                    reason_codes.add(reason or "unprojectable")
                else:
                    candidates.append(fact)
                    all_element_ids.extend(fact.element_ids)
            owned: set[str] = set()
            element_ids = list(dict.fromkeys(all_element_ids))
            if element_ids:
                payload = json.dumps(element_ids, ensure_ascii=False)
                owned = {
                    str(row["id"])
                    for row in db.execute(
                        "WITH requested(id) AS (SELECT CAST(value AS TEXT) FROM json_each(?)) "
                        "SELECT se.id FROM requested CROSS JOIN source_elements se "
                        "ON se.id=requested.id WHERE se.source_id=?",
                        (payload, source_id),
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
                payload = json.dumps(local_ids, ensure_ascii=False)
                collisions = {
                    str(row["local_object_id"])
                    for row in db.execute(
                        "WITH requested(id) AS ("
                        "SELECT CAST(value AS TEXT) FROM json_each(?)) "
                        "SELECT f.local_object_id FROM requested CROSS JOIN "
                        "knowledge_source_facts f ON f.local_object_id=requested.id "
                        "WHERE f.source_id=? AND f.source_generation=? "
                        "AND f.projection_origin='live'",
                        (payload, source_id, generation),
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
                "UPDATE knowledge_source_fact_backfills SET status=?,after_object_id=?,"
                "objects_scanned=?,facts_written=?,incomplete_objects=?,"
                "incomplete_reason=?,failure_code='',"
                "updated_at=? WHERE source_id=? AND source_generation=?",
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
                "failure_code=?,updated_at=? WHERE source_id=?",
                (str(code)[:64], self._runtime.seams.now(), source_id),
            )

    # -- knowhow-table assets (PR-2+3 Task 14) ---------------------------------

    def _live_cell_ref_tokens(
        self, db: sqlite3.Connection, notebook_id: str
    ) -> set:
        """``asset://`` tokens in this notebook's LIVE cell content.

        The ``LIKE`` narrows on the CONSTANT ``'%asset://%'`` (not one pattern
        per asset), so a notebook whose cells never mention an asset stays
        exactly as cheap as the retired per-asset scan — it just pays that once
        instead of once per asset. Rows are consumed in bounded fetches so only
        the token set, never the notebook's whole cell text, is resident.
        """
        tokens: set = set()
        cursor = db.execute(
            "SELECT c.content_md AS content_md FROM knowhow_cells c "
            "JOIN knowhow_rows r ON r.id = c.row_id "
            "JOIN knowhow_tables t ON t.id = r.table_id "
            "WHERE t.notebook_id = ? AND c.content_md LIKE '%asset://%'",
            (notebook_id,),
        )
        while True:
            rows = cursor.fetchmany(_ASSET_SCAN_FETCH_ROWS)
            if not rows:
                break
            for row in rows:
                tokens.update(asset_ref_tokens(row["content_md"]))
        return tokens

    def _history_ref_tokens(
        self, db: sqlite3.Connection, notebook_id: str
    ) -> set:
        """``asset://`` tokens in this notebook's ``knowhow_changes`` history.

        Coarse SQL pre-filter, exact Python match afterwards (mirrors
        ``KnowhowHistoryStore.cell_history``'s own "LIKE narrows candidates,
        Python matches exactly" pattern): a ``payload_json`` substring hit is a
        cheap SUPERSET of what actually counts — it can false-POSITIVE (matching
        inside an excluded ``code_text``) but never false-NEGATIVE, so narrowing
        candidates this way before parsing JSON is safe.
        """
        tokens: set = set()
        cursor = db.execute(
            "SELECT ch.kind AS kind, ch.payload_json AS payload_json "
            "FROM knowhow_changes ch "
            "JOIN knowhow_tables t2 ON t2.id = ch.table_id "
            "WHERE t2.notebook_id = ? AND ch.payload_json LIKE '%asset://%'",
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

    def _surviving_asset_refs(
        self, db: sqlite3.Connection, notebook_id: str, asset_ids
    ) -> set:
        """Which of ``asset_ids`` still have a surviving reference.

        This is the ONE reference-determination predicate ``sweep_orphan_assets``
        below calls from BOTH its classification read and its write-phase
        re-check (knowhow 表版本管理 Task 13 code review). Those two used to
        be independently hand-duplicated copies of the same SQL string — a
        prior implementer's own two-sided mutation test (retract EACH copy
        on its own, confirm both go red) gave false confidence, because the
        two sites are ANDed together for the purpose of "is this asset still
        safe to delete": either copy alone staying conservative (matching
        too much, never too little) is enough to keep an asset that should
        be kept, so retracting only ONE of them at a time never actually
        proved the two agree — it only proved each one, independently, errs
        safe. Calling this SAME method from both places makes "the two
        disagree" structurally impossible rather than something a future
        edit to one copy could silently reintroduce.

        A token keeps an asset alive iff it STARTS WITH that asset's id — the
        restatement of the retired ``LIKE '%asset://<id>%'``, whose exact scope
        (and two registered departures) is documented on ``keepers_among``.

        The corpus is a LIVE cell's ``content_md`` plus a HISTORY change's
        content-shaped payload fields (see ``content_strings_in_payload`` —
        never a code attachment's ``code_text``, wherever it rides along; see
        this class's own ``sweep_orphan_assets`` docstring for the full
        boundary).

        Cost (生产热点整改批 E): the old shape was ``O(assets × cells)`` — every
        asset paid its own full scan of every cell and every change payload,
        each one a leading-wildcard ``LIKE`` that no index can serve. One pass
        per corpus answers the same question for every asset at once.

        The history pass is SKIPPED when the live cells alone already account
        for every candidate — the single-pass form of the retired predicate's
        own ``if live is not None: return True`` short circuit. Without it the
        "few assets, long history" shape (the common one: history is retained
        forever by default, see ``sweep_orphan_assets``'s KNOWN COST note) would
        parse the whole change log on every sweep just to re-confirm a
        conclusion already reached. Nothing about the ANSWER depends on it —
        history tokens can only ever ADD keepers, so once every candidate is
        already kept there is nothing left for them to change.
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
    ) -> dict:
        """Delete every ``notebook_assets`` row (+ on-disk file) in this
        notebook that no knowhow cell references any more, returning
        ``{"removed": n}``.

        ``min_age_seconds`` requires an asset to have been seen unreferenced
        for at least that long, across separate sweeps, before it is deleted
        (default 0 = delete on the first sweep that finds it unreferenced).
        The clock starts when the asset is first OBSERVED UNREFERENCED, not
        when it was uploaded — those differ in the case that matters. A
        reference can exist that this scan cannot see: the cell editor keeps
        unsaved edits in the BROWSER (localStorage drafts), so an image
        pasted-but-not-yet-saved, or one being MOVED from one cell to another,
        is a live reference with no server-side trace. Keying the grace on
        upload time would protect only freshly uploaded images and would delete
        a long-lived image the instant it is cut from its old cell, before the
        destination cell is saved. Keying it on "continuously unreferenced
        since" protects both: the mark is dropped the moment a reference comes
        back, so a completed move never trips it.

        It is a mitigation, not a proof: a draft left unsaved for longer than
        the grace window is still reclaimable.

        Reference scope covers both LIVE content and HISTORY (knowhow 表
        版本管理 Task 13, spec §7.1): an asset counts as "kept" if its id
        appears as an ``asset://<id>`` substring either (a) inside a
        ``knowhow_cells.content_md`` belonging to one of THIS notebook's
        tables, OR (b) inside one of the content_md-SHAPED fields of a
        ``knowhow_changes.payload_json`` row belonging to one of this
        notebook's tables (see ``_surviving_asset_refs`` below). Before
        version management existed, only (a) was scanned — a cell edit that
        dropped an image reference made it collectable the very next sweep.
        Now that every change is remembered forever by default, that same
        cell edit also leaves the OLD value sitting in a ``knowhow_changes``
        row, and reclaiming the asset out from under it would leave a "回退"
        to that point rendering a permanently broken image — half the point
        of keeping history at all.

        The carve-out is CODE, not any particular ``kind``: a code
        attachment's ``code_text`` (``knowhow_cell_code``, migration 17) is
        source-code text, not rendered markdown, and an ``asset://``
        substring inside one — live or historical — is NOT a keeper
        reference (see
        ``test_sweep_does_not_treat_code_attachment_text_as_a_reference``).
        The history side USED TO implement this as a blanket
        ``kind NOT IN ('cell_code_put', 'cell_code_delete')`` exclusion —
        WRONG: ``row_delete``/``column_delete``/``revert`` embed a row's or
        column's remembered CODE ATTACHMENTS (a ``code`` array,
        CASCADE-doomed alongside the row/column) in the very SAME payload as
        its genuine ``content_md``, so excluding by ``kind`` alone let a
        code-only reference inside one of THOSE payloads keep an asset alive
        forever once its row/column was deleted (a real, constructed
        scenario: an image referenced ONLY inside a code attachment, then
        the row holding it is deleted — the row's ``row_delete`` payload
        embeds that code attachment's ``code_text`` right next to the row's
        genuine ``cells``, and a ``kind``-level exclusion cannot tell them
        apart). The scan is instead field-precise:
        ``content_strings_in_payload`` (``app/repositories/knowhow_asset_refs.py``
        — backend-neutral, because BOTH maintenance adapters scan
        ``knowhow_changes``; ``sqlite/knowhow_history_store.py`` only re-exports
        it for its historical import site, so changing the keeper judgement
        means editing the neutral module, not that shim)
        dispatches on ``kind`` and pulls out ONLY the content_md-shaped
        strings a change's payload carries (a deleted cell/row/column's
        remembered content, a revert's own before/after values, ...) and
        NEVER a ``code``/``code_text`` field, no matter which kind embeds it
        alongside real content. A false keeper hit here only ever
        OVER-protects (mirrors this method's existing LIKE-match asymmetry
        note above `_ASSET_REF_RE` in ``services/knowhow/api.py``:
        over-matching here only ever RETAINS an asset, never wrongly deletes
        one) — the risk this field-precision closes runs the OTHER way,
        under-protecting a still-referenced asset.

        KNOWN COST (accepted, spec §7.1): once an image has ever appeared in
        ANY cell, it is for all practical purposes never automatically
        reclaimed again — almost any later edit to that cell leaves a
        ``knowhow_changes`` row remembering the old value, and history is
        kept forever unless a human explicitly prunes it. The release path
        is "清理历史" (``KnowhowHistoryStore.prune``) followed by another
        sweep — but pruning ALONE is not sufficient (verified, not merely
        theoretical): ``prune`` always keeps HEAD (spec §7.7 — the revert
        engine's pre-flight fingerprint guard has to compare against
        something), so if an asset's LAST remaining reference happens to sit
        in the head change itself — e.g. "paste an image, delete it again,
        never touch this table again": the delete edit becomes head, and
        its own ``before`` still embeds the ``asset://`` id — pruning
        removes every OTHER change and the asset is STILL judged referenced
        afterward; a sweep run immediately after such a prune reclaims ZERO
        bytes. Reclaiming it needs one more step first: any edit UNRELATED
        to this asset moves head to a change that no longer mentions it,
        and only THEN does the next prune+sweep actually drop it. Deleting
        the whole table is unaffected by this caveat (no history survives a
        deleted table, so every asset it ever held is judged orphaned
        regardless). The prune HTTP endpoint triggers a sweep on completion
        regardless of this caveat (``prune_knowhow_history`` in
        ``knowhow_routes.py``) since pruning usually DOES free something —
        it is just not GUARANTEED to for an asset whose only surviving
        mention sits on head. This trades unbounded-until-pruned (plus this
        head caveat) disk growth for the ability to revert to (or simply
        browse) a past version that still has its images intact.

        Keeper determination is ONE streamed pass over this notebook's cell
        content and change payloads (``_surviving_asset_refs``), not the per-asset
        leading-wildcard ``LIKE`` this
        used to run — that was ``O(assets × cells)``, paid twice (classify,
        then re-check under the write lock), and the "table sizes here are
        small" justification stopped holding once history began retaining every
        past cell value forever (see the KNOWN COST note above: assets
        accumulate precisely because history keeps them alive). The scan's
        semantics are unchanged: still a loose ``asset://<id>`` substring match,
        still over-matching only ever RETAINS.

        File deletion happens BEFORE the row delete (opposite of
        ``delete_notebook``'s DB-first convention): unlike a notebook
        deletion — where the row's own data (e.g. ``sources.file_path``)
        would otherwise vanish via cascade before it could be read — an
        orphan asset's file path is always re-derivable from just its id, so
        deleting the file first and the row second makes a crash mid-sweep
        self-healing: a re-run finds the same still-orphaned row, no-ops the
        (already gone) file glob, and finishes the row delete. Missing files
        are tolerated silently either way (``glob`` simply finds nothing) —
        the row is always removed once determined orphaned, never blocked on
        disk state.
        """
        from pathlib import Path

        # ``source_id IS NULL`` below restricts this to knowhow-pasted images.
        # Source-derived images (migration 19: MinerU-extracted embedded
        # figures, ``source_id`` set) are reachable ONLY through
        # ``source_elements.metadata.asset_id`` and are served for the source
        # view — an ``asset://<id>`` substring never appears for them anywhere,
        # so the keeper scan would classify every single one as an orphan.
        # They already have their own lifecycle (source removal / reparse
        # cascade, notebook cascade), so they are simply not this sweep's
        # business. Losing one costs a full re-parse, which is exactly what the
        # image-retention stack exists to avoid — see
        # test_knowhow_asset_gc_trigger.py::
        # test_projection_run_never_touches_source_derived_images.
        with self._runtime.database.connect() as db:
            assets = db.execute(
                "SELECT id FROM notebook_assets "
                "WHERE notebook_id=? AND source_id IS NULL",
                (notebook_id,),
            ).fetchall()
            asset_ids = [row["id"] for row in assets]
            # Only scan when there is something to classify: a notebook with no
            # pasted images pays nothing, exactly as the per-asset loop did
            # (``_surviving_asset_refs`` early-returns on an empty candidate set).
            kept = self._surviving_asset_refs(db, notebook_id, asset_ids)
            unreferenced = [
                asset_id for asset_id in asset_ids if asset_id not in kept
            ]

            # Marks for notebooks that no longer exist. The adapter is
            # process-long-lived and a deleted notebook cascades its asset rows
            # away and is then never swept again, so its entries would sit in
            # the dict forever. Deciding this by AGE instead would be actively
            # harmful: sweeps are edge-triggered (a projection, or a table
            # being removed), so an old mark is NOT "stale", it usually means
            # "that notebook simply has not been edited since". Dropping those
            # would make the next sweep re-mark from scratch and start the grace
            # over — a notebook edited less often than the grace window would
            # then never reclaim anything, defeating the whole mechanism.
            with self._orphan_marks_lock:
                marked_notebooks = {key[0] for key in self._orphan_first_seen}
            marked_notebooks.discard(notebook_id)
            dead_notebooks = {
                other
                for other in marked_notebooks
                if db.execute(
                    "SELECT 1 FROM notebooks WHERE id = ? LIMIT 1", (other,)
                ).fetchone()
                is None
            }

        # Two phases. A reference that reappears (an image being moved between
        # cells: gone from the source cell, still only in the destination
        # cell's unsaved browser draft) clears the mark, so a completed move
        # never reaches deletion.
        unreferenced_set = set(unreferenced)
        live_ids = {row["id"] for row in assets}

        # Prune marks that can no longer do anything: assets this notebook no
        # longer has (deleted by any other path), plus every mark belonging to a
        # notebook that is itself gone (see above).
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

            # A reference that reappears (an image being moved between cells:
            # gone from the source cell, still only in the destination cell's
            # unsaved browser draft) clears the mark, so a completed move never
            # reaches deletion.
            for asset_id in live_ids - unreferenced_set:
                self._orphan_first_seen.pop((notebook_id, asset_id), None)

            if min_age_seconds <= 0:
                orphan_ids = list(unreferenced)
            else:
                for asset_id in unreferenced:
                    first_seen = self._orphan_first_seen.get((notebook_id, asset_id))
                    if first_seen is None:
                        self._orphan_first_seen[(notebook_id, asset_id)] = stamp  # mark
                    elif stamp - first_seen >= min_age_seconds:
                        orphan_ids.append(asset_id)

        if not unreferenced:
            return {"removed": 0}

        asset_dir = Path(self._runtime.storage_dir) / "assets" / notebook_id
        # Classification above ran on a read connection that is now closed. A
        # cell save committing in that gap would restore an ``asset://`` link to
        # a row we are about to delete — and cell references have no FK to stop
        # it, so the user would be left with a live link pointing at a deleted
        # row and a deleted file. Every writer (cell saves included) goes through
        # database.write(), so re-running the keeper check INSIDE this write
        # transaction serializes against them and closes that window. File
        # removal stays inside the same transaction and still precedes the row
        # delete, preserving the crash-self-healing order documented above.
        removed: list[str] = []
        with self._runtime.database.write() as db:
            # With ZERO knowhow tables left, the grace period is protecting
            # nothing and is skipped. The grace exists to shield an asset that
            # lives only in an unsaved browser draft, and a draft can only be
            # rescued by saving it into a cell — with no table there is no cell
            # to save into. This matters because the grace needs a LATER sweep
            # to mature the mark, and sweeps are edge-triggered by projections:
            # once the last table is gone no projection can ever fire again, so
            # marks would never mature and the files would survive until the
            # notebook itself was deleted — precisely the "GC never runs" state
            # this trigger exists to end.
            #
            # It is decided HERE, inside the write transaction, rather than by
            # the caller: another request can create a table and upload an image
            # between a caller-side check and this worker's scan, and that fresh
            # draft-only asset would then be reclaimed with no grace at all. Read
            # under the write lock, the condition is true at deletion time or not
            # at all.
            grace_waived = waive_grace_if_no_tables and (
                db.execute(
                    "SELECT 1 FROM knowhow_tables WHERE notebook_id = ? LIMIT 1",
                    (notebook_id,),
                ).fetchone()
                is None
            )
            candidates = unreferenced if grace_waived else orphan_ids
            # Same single predicate as the classification read, re-run under the
            # write lock — one pass for the whole candidate set rather than one
            # per candidate (生产热点整改批 E).
            still_kept = self._surviving_asset_refs(db, notebook_id, candidates)
            for asset_id in candidates:
                if asset_id in still_kept:
                    with self._orphan_marks_lock:
                        self._orphan_first_seen.pop((notebook_id, asset_id), None)
                    continue
                for stale_file in asset_dir.glob(f"{asset_id}.*"):
                    if stale_file.is_file():
                        # missing_ok: two sweeps can overlap if one ever outruns
                        # the caller's throttle; losing that race must not abort
                        # the remaining deletes.
                        stale_file.unlink(missing_ok=True)
                removed.append(asset_id)
            if removed:
                db.executemany(
                    "DELETE FROM notebook_assets WHERE id=?",
                    [(asset_id,) for asset_id in removed],
                )
        with self._orphan_marks_lock:
            for asset_id in removed:
                self._orphan_first_seen.pop((notebook_id, asset_id), None)
        return {"removed": len(removed)}


class ReadOnlySQLiteInspector:
    """Arbitrary-path, ``mode=ro`` SQLite reader for eval/validation tools.

    Never opens the database read-write; connections are URI ``mode=ro`` so a
    typo'd path fails loudly instead of creating an empty file."""

    def __init__(self, db_path) -> None:
        self.db_path = str(db_path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    # -- generic vector-table projections --------------------------------------

    def table_count(self, table: str, notebook_id: str) -> int:
        """COUNT(*) for one embeddings table; -1 when the table is missing."""
        _require_vector_table(table, _VECTOR_TABLE_IDS.get(table, ""))
        with self.connect() as conn:
            try:
                return conn.execute(
                    f"SELECT COUNT(*) AS c FROM {table} WHERE notebook_id=?",
                    (notebook_id,),
                ).fetchone()["c"]
            except sqlite3.Error:
                return -1  # 表不存在(旧库)

    def busiest_vector_notebook(self) -> Optional[str]:
        with self.connect() as conn:
            try:
                row = conn.execute(
                    "SELECT notebook_id, COUNT(*) AS c FROM knowledge_embeddings "
                    "GROUP BY notebook_id ORDER BY c DESC LIMIT 1"
                ).fetchone()
            except sqlite3.Error:
                return None
        return row["notebook_id"] if row else None

    def detect_vector_dim(self, table: str, id_col: str, notebook_id: str) -> Optional[int]:
        """取首个可解码向量的维度作为存储维(全表应同维;不同维行按 skip 计)。"""
        _require_vector_table(table, id_col)
        with self.connect() as conn:
            cur = conn.execute(
                f"SELECT vector FROM {table} WHERE notebook_id = ? LIMIT 20",
                (notebook_id,),
            )
            for r in cur.fetchall():
                try:
                    v = decode_vector(r["vector"])
                except Exception:  # noqa: BLE001
                    continue
                if v is not None and v.shape[0] > 0:
                    return int(v.shape[0])
        return None

    def vector_ids(self, table: str, id_col: str, notebook_id: str) -> list:
        _require_vector_table(table, id_col)
        with self.connect() as conn:
            return [
                r["vid"]
                for r in conn.execute(
                    f"SELECT {id_col} AS vid FROM {table} WHERE notebook_id=?",
                    (notebook_id,),
                )
            ]

    def vectors_for_ids(
        self, table: str, id_col: str, ids: Sequence[str], stored_dim: int
    ) -> tuple:
        """Decode the vectors for these ids (batched IN); returns
        (kept_ids, [np.ndarray]) skipping undecodable/mismatched rows."""
        _require_vector_table(table, id_col)
        kept_ids: list = []
        vectors: list = []
        with self.connect() as conn:
            for lo in range(0, len(ids), 500):
                batch = list(ids)[lo : lo + 500]
                ph = ",".join("?" for _ in batch)
                for r in conn.execute(
                    f"SELECT {id_col} AS vid, vector FROM {table} WHERE {id_col} IN ({ph})",
                    batch,
                ):
                    try:
                        v = decode_vector(r["vector"])
                    except Exception:  # noqa: BLE001
                        continue
                    if v is not None and v.shape[0] == stored_dim:
                        kept_ids.append(r["vid"])
                        vectors.append(v)
        return kept_ids, vectors

    def vector_blocks(
        self,
        table: str,
        id_col: str,
        notebook_id: str,
        stored_dim: int,
        block_rows: int,
        id_subset: Optional[list] = None,
    ) -> Iterator[tuple]:
        """流式产出 (ids, matrix[float32, 未归一], skipped) 块;坏行/维度不符行
        计数返回。id_subset 非空 → 只按这批 id 取(大库抽样评测),分批 IN 免撞
        SQLite 变量上限;否则全表游标扫。"""
        import numpy as np

        _require_vector_table(table, id_col)
        conn = self.connect()
        try:
            def _row_batches():
                if id_subset is None:
                    cur = conn.execute(
                        f"SELECT {id_col} AS vid, vector FROM {table} WHERE notebook_id = ?",
                        (notebook_id,),
                    )
                    while True:
                        rows = cur.fetchmany(block_rows)
                        if not rows:
                            return
                        yield rows
                else:
                    for lo in range(0, len(id_subset), min(block_rows, 800)):
                        batch = id_subset[lo : lo + min(block_rows, 800)]
                        ph = ",".join("?" for _ in batch)
                        yield conn.execute(
                            f"SELECT {id_col} AS vid, vector FROM {table} "
                            f"WHERE notebook_id = ? AND {id_col} IN ({ph})",
                            (notebook_id, *batch),
                        ).fetchall()

            skipped = 0
            for rows in _row_batches():
                ids, vecs = [], []
                for r in rows:
                    try:
                        v = decode_vector(r["vector"])
                    except Exception:  # noqa: BLE001 — 坏行跳过计数,不拖垮评测
                        skipped += 1
                        continue
                    if v is None or v.shape[0] != stored_dim:
                        skipped += 1
                        continue
                    ids.append(r["vid"])
                    vecs.append(v)
                if ids:
                    yield ids, np.vstack(vecs), skipped
                    skipped = 0
            if skipped:
                yield [], np.empty((0, stored_dim), dtype=np.float32), skipped
        finally:
            conn.close()

    # -- KG comparison/validation projections ----------------------------------

    def concept_whitelist_terms(self) -> set:
        with self.connect() as conn:
            return {r["term"] for r in conn.execute("SELECT term FROM concept_whitelist")}

    def concept_names(self, notebook_id: str) -> list:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM knowledge_objects "
                "WHERE notebook_id=? AND object_type='concept'",
                (notebook_id,),
            ).fetchall()
        return [json.loads(r["payload"] or "{}").get("name", "") for r in rows]

    def concept_id_names(self, notebook_id: str) -> list:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, payload FROM knowledge_objects "
                "WHERE notebook_id=? AND object_type='concept'",
                (notebook_id,),
            ).fetchall()
        return [
            (r["id"], json.loads(r["payload"] or "{}").get("name", "")) for r in rows
        ]

    def knowledge_vectors(self, notebook_id: str) -> dict:
        """{object_id: np.ndarray} for every decodable knowledge embedding."""
        out: dict = {}
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT object_id, vector FROM knowledge_embeddings WHERE notebook_id=?",
                (notebook_id,),
            ).fetchall()
        for r in rows:
            arr = decode_vector(r["vector"])
            if arr is not None:
                out[r["object_id"]] = arr
        return out
