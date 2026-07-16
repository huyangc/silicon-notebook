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

import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Callable, Iterator, Optional, Sequence

from app.services.vector_index import decode_vector

# (table, id_column) for every embeddings table maintenance tooling touches.
VECTOR_TABLES = (
    ("chunk_embeddings", "chunk_id"),
    ("knowledge_embeddings", "object_id"),
    ("element_embeddings", "element_id"),
    ("relation_embeddings", "relation_id"),
)
_VECTOR_TABLE_IDS = dict(VECTOR_TABLES)


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

    # -- SQLiteMaintenancePort ------------------------------------------------

    def delete_notebook_kg(self, notebook_id: str) -> dict:
        return self._runtime.knowledge_lifecycle.delete_notebook_kg(notebook_id)

    def eval_insert_source_for_test(
        self, notebook_id: str, name: str, text: str, tmpdir: str
    ) -> str:
        from pathlib import Path
        from app.repositories.sqlite.source_store import SourceElementWrite
        from app.services.kg.parsing import parse_elements

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
                from app.services.auth_utils import normalize_username

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
        with self._runtime.database.connect() as db:
            row = db.execute(
                "SELECT id FROM sources WHERE notebook_id=? AND file_hash=?",
                (notebook_id, digest),
            ).fetchone()
        return row["id"] if row else None

    def source_ids(self, notebook_id: str) -> list[str]:
        with self._runtime.database.connect() as db:
            return [
                r["id"]
                for r in db.execute(
                    "SELECT id FROM sources WHERE notebook_id=?", (notebook_id,)
                ).fetchall()
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
                "UPDATE sources SET doc_type=? WHERE notebook_id=?",
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
                "AND NOT EXISTS (SELECT 1 FROM knowledge_objects k "
                "WHERE k.source_id=s.id AND k.source_id!='')",
                (notebook_id,),
            ).fetchone()["c"]

    def run_extraction(self, source_id: str) -> None:
        # Late-bound through the runtime so component-seam monkeypatches
        # (repo._runtime.source_ingestion.run_extraction) keep observing.
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

    def missing_chunk_embedding_rows(self, notebook_id: str) -> list[dict]:
        with self._runtime.database.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT c.id, c.text FROM chunks c WHERE c.notebook_id=? "
                    "AND NOT EXISTS (SELECT 1 FROM chunk_embeddings e "
                    "WHERE e.chunk_id=c.id)",
                    (notebook_id,),
                ).fetchall()
            ]

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
    ) -> None:
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

    def backfill_node_embeddings(self, notebook_id: str, progress=None) -> int:
        """Embed every non-deprecated knowledge object missing a vector
        (idempotent); returns the number of objects scanned."""
        with self._runtime.database.connect() as db:
            objects = [
                {"id": r["id"], "payload": json.loads(r["payload"] or "{}")}
                for r in db.execute(
                    "SELECT id, payload FROM knowledge_objects "
                    "WHERE notebook_id=? AND status!='deprecated'",
                    (notebook_id,),
                ).fetchall()
            ]
            self._runtime.source_embedding.backfill_knowledge_embeddings(
                db, notebook_id, objects, progress=progress
            )
        return len(objects)

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

    def count_missing_chunk_vectors(self, notebook_id: str) -> int:
        with self._runtime.database.connect() as db:
            return db.execute(
                "SELECT COUNT(*) c FROM chunks c WHERE c.notebook_id=? AND NOT EXISTS "
                "(SELECT 1 FROM chunk_embeddings e WHERE e.chunk_id=c.id)",
                (notebook_id,),
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
        """给缺向量的关系补 relation_embeddings(幂等,只补缺失)。无 embedder 则 no-op。
        Canonical body (Task 27) — the facade keeps a frozen-signature delegate."""
        if not self._runtime.settings.embedder_configured:
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
        encode: Callable[[list], list],
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
            updates = encode(rows)
            bad = sum(1 for blob, _nb, _vid in updates if blob == b"")
            db.executemany(
                f"UPDATE {table} SET vector=? WHERE notebook_id=? AND {id_col}=?",
                updates,
            )
        return len(rows), bad

    def clear_source_index(self, notebook_id: str) -> int:
        """Reset knowledge_object_sources for one notebook; returns the
        knowledge_objects total the backfill loop must cover."""
        with self._runtime.database.write() as db:
            db.execute(
                "DELETE FROM knowledge_object_sources WHERE notebook_id=?",
                (notebook_id,),
            )
            return db.execute(
                "SELECT COUNT(*) c FROM knowledge_objects WHERE notebook_id=?",
                (notebook_id,),
            ).fetchone()["c"]

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

    # -- knowhow-table assets (PR-2+3 Task 14) ---------------------------------

    def sweep_orphan_assets(self, notebook_id: str) -> dict:
        """Delete every ``notebook_assets`` row (+ on-disk file) in this
        notebook that no knowhow cell references any more, returning
        ``{"removed": n}``.

        Reference scope is deliberately narrow: an asset counts as "kept"
        only if its id appears as an ``asset://<id>`` substring inside a
        ``knowhow_cells.content_md`` belonging to one of THIS notebook's
        tables. ``knowhow_cell_code`` (per-cell code attachments, migration
        17) is source-code text, not rendered markdown — an ``asset://``
        substring showing up there (e.g. in a comment) is NOT treated as a
        keeper reference. If that boundary ever proves wrong in practice
        (some real workflow renders code-attachment text as an image
        reference), widen the scan to include it then; until observed, cells
        are the only place an image actually gets embedded/displayed.

        Table sizes here are small (per-notebook assets/cells), so a plain
        per-asset ``LIKE`` scan is used rather than a single mega-query —
        matches this module's existing style for the other per-notebook
        maintenance scans above.

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

        with self._runtime.database.connect() as db:
            assets = db.execute(
                "SELECT id FROM notebook_assets WHERE notebook_id=?",
                (notebook_id,),
            ).fetchall()
            orphan_ids = [
                row["id"]
                for row in assets
                if db.execute(
                    "SELECT 1 FROM knowhow_cells c "
                    "JOIN knowhow_rows r ON r.id = c.row_id "
                    "JOIN knowhow_tables t ON t.id = r.table_id "
                    "WHERE t.notebook_id = ? AND c.content_md LIKE ? LIMIT 1",
                    (notebook_id, f"%asset://{row['id']}%"),
                ).fetchone()
                is None
            ]
        if not orphan_ids:
            return {"removed": 0}

        asset_dir = Path(self._runtime.storage_dir) / "assets" / notebook_id
        for asset_id in orphan_ids:
            for stale_file in asset_dir.glob(f"{asset_id}.*"):
                if stale_file.is_file():
                    stale_file.unlink()

        with self._runtime.database.write() as db:
            db.executemany(
                "DELETE FROM notebook_assets WHERE id=?",
                [(asset_id,) for asset_id in orphan_ids],
            )
        return {"removed": len(orphan_ids)}


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
