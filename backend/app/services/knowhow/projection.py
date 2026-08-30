"""Deterministic, zero-LLM projection of knowhow-table rows into the
notebook's knowledge machinery (knowhow-tables PR-1 Task 5, replaced end to
end by PR-2+3 Task 2 with the finalized **cell-level node model**, design doc
§④/§① — docs/superpowers/specs/2026-07-15-knowhow-tables-design.md).

Model (design doc §④, "格子级节点模型"): a table with a row-title (anchor)
column projects **every non-empty cell** to a knowledge object (KO) whose
``object_type`` is that cell's COLUMN NAME (a dynamic type, same standing as
Concept/Claim — domain semantics live on the column, not on a fixed
case/procedure/tool vocabulary). Same-column-same-value cells MERGE across
rows into one KO (identity = ``(table_id, column_name, value_key(text))`` —
``value_key`` normalizes casing/whitespace/punctuation, design doc "同列同值
跨行归并"), accumulating one evidence item and one ``payload.rows`` entry per
contributing row. Row structure becomes a star of ``about`` edges: every
non-row-title cell's KO points ``--about-->`` the row's row-title-cell KO
(edge id also content-derived, so the SAME edge from two rows sharing both
endpoints collapses to one row, not a duplicate insert). A table with NO
anchor column (a "记录型" record table) projects **zero KOs/edges** — it only
ever participates in chunk/FTS retrieval, never the graph — including the
transitional case of a table whose anchor was REMOVED after having been set
(the next projection pass tears every previously-projected KO/edge down and
rebuilds nothing in their place).

``KnowhowProjector`` mirrors ``kg_ingest.build_records``'s knowledge_objects/
knowledge_relations shape but writes with STABLE, content-derived ids instead
of store_kg's fresh-id-per-call allocation — reprojecting a table is an
idempotent in-place rewrite, not an append, which is what makes "edit one
cell -> only that cell's derivatives change" possible at all.

Every table's derived artifacts hang off ONE hidden synthetic source (mirrors
the Memory<->KG hidden-source pattern in ``source_ingestion.ingest_memory_
source`` — ``source_type="knowhow"``, excluded from user-facing source
listings the same way ``source_type="memory"`` already is). Composition rules
mirror ``KnowledgeLifecycleService``/``SourceChunkingService``: no facade
import, no raw SQL here — every write goes through an injected store method,
and this service owns its own ``database.write()`` transaction boundary for
the structural (non-embedding) part of a projection.

``project_table`` is the SOLE projection entry point (the PR-1/Task-1-era
per-row ``project_row`` is gone — cross-row merging inherently needs a
whole-table view, so per-cell chunk/element diffing now runs in a per-row
loop INSIDE one ``project_table`` pass while KOs/edges accumulate in memory
across that same loop and get torn down + rebuilt in ONE final atomic write
AFTER every row has been visited). Two nuances worth flagging up front:

1. ``_write_chunks`` sometimes needs to rewrite a chunk row whose TEXT never
   changed (only its ``section_path`` did — see that method's docstring).
   ``chunk_embeddings.chunk_id`` is ``ON DELETE CASCADE`` onto ``chunks(id)``
   and ChunkStore has no in-place "update one column" primitive, only
   delete/insert — so that rewrite still goes through the existing
   ``delete_by_ids``/``insert_rows`` pair (no new store method, no raw SQL),
   and the row's still-valid embedding is carried over (read via
   ``EmbeddingStore.rows_by_ids`` before the delete, re-persisted via
   ``SourceEmbeddingService.vectors.replace_chunk_vectors`` after the
   transaction commits) instead of being recomputed or silently lost. The
   same pre-delete probe doubles as a self-heal: any text-unchanged chunk
   found with NO vector at all (an earlier post-commit persist window
   interrupted) is queued for a fresh embed, so reprojecting a table repairs
   vector gaps instead of skipping the "unchanged" cell forever.

2. The KO/edge rebuild is DELIBERATELY the LAST thing ``project_table`` does,
   not the first: if any row's structural write raises (a genuine
   programming bug, not network I/O — see the embed-vs-structural failure
   split below), the exception propagates OUT of the row loop before the
   final teardown+rebuild step ever runs, so a table's derived knowledge
   graph either advances to a fully-consistent new state or is left exactly
   as it was after the last SUCCESSFUL pass — never partially torn down.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, List

from app.core.config import Settings
from app.repositories.ports import (
    ChunkWrite,
    ChunkStorePort,
    KnowledgeStorePort,
    KnowhowStorePort,
    SourceElementWrite,
    SourceStorePort,
)
from app.services import background_jobs
from app.services.knowhow import textops
from app.services.knowhow.ids import (
    _cell_ko_id,
    _chunk_row_hash,
    _h,
    _relation_id,
    cell_chunk_id,
    element_id,
)
from app.services.model_work import ModelProviderError
from app.services.source_embedding import SourceEmbeddingService
from app.services.vector_index import decode_vector

_CHUNK_CHAR_LIMIT = 4000
# `part = column.position * _COLUMN_PART_STRIDE + split_index` gives every
# cell a STABLE numeric part-range (independent of sibling cells being
# empty/filled) so a single-cell edit's old-vs-new chunk diff can be done
# PER CELL, not per row — up to 99 split-parts per single cell before this
# would collide, which a "few hundred to a few screens of text" cell (design
# doc §, "百行内...几百字到几屏富文本") never approaches.
_COLUMN_PART_STRIDE = 100

# One-shot startup migration bridge (see find_legacy_projected_table_ids /
# KnowhowProjector.reproject_legacy_tables at the bottom of this file): the
# PR-1 fixed-vocabulary object types this cell-level model replaces.
_LEGACY_OBJECT_TYPES = ("case", "procedure", "tool")


def _resolve_row_title(anchor_column, columns, cell_nets, position: int) -> str:
    """The row's display title used for section_path/evidence labels (design
    doc §① "行标题自动合成"), independent of whether this row ends up
    contributing any KO at all: the anchor cell's own ``node_name`` when the
    table has an anchor column AND this row's anchor cell has content;
    otherwise a synthesized title from up to 3 of this row's other non-empty
    cells (``textops.compose_row_title`` — covers BOTH "no anchor column"
    tables and an anchor-having table's occasional row whose anchor cell
    itself is blank); "行{position+1}" when every cell is empty (the only
    fallback ``compose_row_title`` itself does not apply, since it has no
    notion of the row's position — see that function's own docstring)."""
    if anchor_column is not None:
        anchor_text = cell_nets[anchor_column["id"]]
        if anchor_text:
            return textops.node_name(anchor_text)
    composed = textops.compose_row_title([cell_nets[c["id"]] for c in columns])
    return composed or f"行{position + 1}"


def _split_long_text(text: str, limit: int = _CHUNK_CHAR_LIMIT) -> List[str]:
    """Paragraph-bounded continuation split for an overlong cell (brief step
    3: "＞4000 字符按段落续切"). Greedily packs blank-line-separated
    paragraphs up to `limit`; a single paragraph bigger than `limit` on its
    own stays its own (oversized) part rather than being mid-word-sliced —
    this splits ONE markdown cell, not a whole document, so a simple greedy
    pack is enough (no heading-aware windowing like kg/windowing.py)."""
    if len(text) <= limit:
        return [text]
    paragraphs = text.split("\n\n")
    parts: List[str] = []
    buf = ""
    for para in paragraphs:
        candidate = f"{buf}\n\n{para}" if buf else para
        if buf and len(candidate) > limit:
            parts.append(buf)
            buf = para
        else:
            buf = candidate
    if buf:
        parts.append(buf)
    return parts or [text]


def _evidence(source_id: str, source_title: str, element_id: str, element_type: str,
             location_label: str, quote: str) -> dict:
    """Product Evidence shape (app.models.common.Evidence) — mirrors
    kg_ingest._ev's fields exactly so knowhow-derived KOs/edges are readable
    by every existing evidence consumer (citation rendering, graph
    retrieval's element->chunk bridge) without a special case."""
    return {
        "source_id": source_id, "source_title": source_title, "element_id": element_id,
        "element_type": element_type, "location_label": location_label,
        "quoted_span": (quote or "")[:400], "confidence": 1.0,
    }


def find_legacy_projected_table_ids(knowledge_store, db) -> "list[str]":
    """Pure detection query for the one-shot startup migration bridge
    (``KnowhowProjector.reproject_legacy_tables`` below, wired in by
    ``app.services.startup_warmup``): every table_id that still has at least
    one PR-1-model KO — ``object_type`` one of the fixed legacy strings
    (``case``/``procedure``/``tool``) AND an id carrying knowhow's own
    ``ko-kh-`` prefix (excludes a same-named object_type from some other
    pipeline, e.g. generic LLM extraction's own "concept"/"claim"/"procedure"
    types, which never share this prefix). A plain SELECT (delegated to
    ``KnowledgeStore.legacy_typed_table_ids`` — this codebase's SQL-ownership
    rule keeps the actual query text in the store, not here) — this function
    performs no mutation and takes no lock; the caller owns scheduling.

    Self-extinguishing by construction: once a table is reprojected under the
    cell-level model its KOs' ``object_type`` becomes that cell's COLUMN
    NAME, which only accidentally re-matches this query if a user's own
    column happens to be literally named "case"/"procedure"/"tool" — an
    accepted, harmless false positive (reprojecting an already-current table
    is an idempotent no-op, not a correctness bug)."""
    return knowledge_store.legacy_typed_table_ids(
        db, _LEGACY_OBJECT_TYPES, "ko-kh-"
    )


class KnowhowProjector:
    def __init__(
        self,
        *,
        settings: Settings,
        database: Any,
        knowhow: KnowhowStorePort,
        sources: SourceStorePort,
        chunks: ChunkStorePort,
        knowledge: KnowledgeStorePort,
        embedding: SourceEmbeddingService,
        note_model_error: Callable[..., None],
        invalidate_unified_cache: Callable[[str], None],
        mark_unified_dirty_in_tx: Callable[[Any, str], int],
        new_id: Callable[[str], str],
        now: Callable[[], str],
    ) -> None:
        self.settings = settings
        self.database = database
        self.knowhow = knowhow
        self.sources = sources
        self.chunks = chunks
        self.knowledge = knowledge
        self.embedding = embedding
        self.note_model_error = note_model_error
        self.invalidate_unified_cache = invalidate_unified_cache
        # codex #638 R5: the projector writes knowledge_objects /
        # knowledge_relations / concept_clusters directly, so it takes the
        # IN-TRANSACTION bump seat only — a post-commit ``mark_unified_kg_dirty``
        # would reopen the "rows committed, seq unmoved" window on both of its
        # graph-writing paths. There is deliberately no non-tx seat here.
        self.mark_unified_dirty_in_tx = mark_unified_dirty_in_tx
        self.new_id = new_id
        self.now = now

    # ------------------------------------------------------- hidden source
    def ensure_hidden_source(self, table_id: str) -> str:
        """Idempotent: returns the table's existing hidden source if one is
        already recorded and still resolves, else mints one. ``project_table``
        calls this itself (its own signature carries no source_id), so a
        table's very first projection transparently creates its hidden
        source — callers never need to sequence ensure_hidden_source before
        project_table by hand.

        PR review round 7 P2-A (creation+attach atomicity): minting the
        ``sources`` row and attaching it back onto ``table_id`` (writing
        ``hidden_source_id``) used to run in TWO separate ``database.
        write()`` transactions. The round-6 orphan sweep (``KnowhowTransfer
        Store.orphaned_knowhow_source_ids`` — every ``source_type='knowhow'``
        row in a notebook unreferenced by any ``knowhow_tables.hidden_
        source_id``, run at the end of every ``move_table``) cannot tell "a
        legitimately in-flight brand-new source, one statement away from
        being attached" apart from "an actual orphan a torn-down move left
        behind" — both are, at that instant, a knowhow-typed source nothing
        points at yet. A sweep landing in the gap between the two writes
        would delete a LIVE source belonging to a DIFFERENT table's
        concurrent first projection, which then fails on FK when it goes to
        write elements (self-heals next projection pass, since this method
        re-mints a replacement when the recorded id no longer resolves, but
        is still observably wrong in between).

        Both writes now share ONE ``database.write()`` transaction:
        ``SourceStore.insert_source`` already accepted an optional
        ``connection=`` to join a caller's transaction (its own docstring:
        "batch imports keep their all-or-nothing semantics"), and
        ``KnowhowStore.set_knowhow_hidden_source`` gained the identical
        parameter, mirroring that exact idiom rather than inventing a new
        one. With creation and attachment atomic, the sweep can only ever
        observe a knowhow source that is either fully absent or already
        referenced — never the intermediate state.

        PR review round 8 P1-A (atomic still isn't the same as CORRECT):
        being ONE transaction only stops the sweep from landing IN BETWEEN
        the two writes — it does nothing to stop a table that was already
        deleted by the time this method's own existence check ran (or that
        gets deleted between that check and this transaction acquiring the
        write lock — the "resume after the sweep already ran" case the
        family history above is really about) from having BOTH writes
        still go ahead: ``set_knowhow_hidden_source``'s UPDATE matches zero
        rows against a gone ``table_id``, which SQLite does not treat as an
        error, so ``insert_source``'s INSERT would commit right alongside
        it — a fresh, permanently unreferenced source, born after the one
        sweep that could have caught it already ran. ``set_knowhow_hidden_
        source`` now raises ``KeyError(table_id)`` on that zero-rowcount
        case (see its own docstring), which rolls back this WHOLE
        transaction — ``insert_source``'s INSERT included — so nothing
        commits. That ``KeyError`` then propagates straight out of this
        method (nothing here catches it) and out of ``project_table``'s own
        call to it, which sits BEFORE that method's per-row loop —
        the exact same "raise before any row is touched, so there is
        nothing to partially write" shape ``project_table`` already has at
        its very top when ``get_knowhow_table`` itself raises for a
        table_id that never existed in the first place. No new call site
        needed to wire this in; being early in the function IS the wiring."""
        table = self.knowhow.get_knowhow_table(table_id)
        existing = table.get("hidden_source_id")
        if existing:
            try:
                self.sources.get_source(existing)
                return existing
            except KeyError:
                pass  # recorded id no longer resolves — recreate below
        source_id = self.new_id("src")
        with self.database.write() as db:
            self.sources.insert_source(
                source_id=source_id,
                notebook_id=table["notebook_id"],
                title=f"Knowhow 表：{table['title']}",
                source_type="knowhow",
                status="active",
                parse_status="parsed",
                file_name="",
                file_path="",
                file_size=0,
                file_hash="",
                summary="",
                doc_type="",
                connection=db,
            )
            self.knowhow.set_knowhow_hidden_source(table_id, source_id, connection=db)
        return source_id

    # ------------------------------------------------------------- project
    def project_table(self, table_id: str, *, embed: bool = True) -> str:
        """THE projection entry point (single-flight full-table pass): for
        every row, rewrite its elements + diff/rewrite its chunks (existing
        per-cell diff machinery, untouched — only a genuinely changed cell's
        chunk gets a fresh embed) and accumulate this row's contribution to
        the table-wide KO/edge model in memory; ONLY AFTER every row has been
        visited, atomically tear down and rebuild every KO/edge this table's
        hidden source owns from the accumulated result (see the module
        docstring's point 2 for why this ordering matters on a mid-pass
        failure). A table with no anchor column (or whose anchor was cleared)
        naturally accumulates nothing, so this same rebuild step correctly
        reduces to "wipe, insert nothing" for it (design doc §① "记录型...
        不建任何图谱节点" and the anchor-removed transition case).

        ``embed=False`` skips the embedding-attempt block entirely (still
        settles every row's status) — for a caller that has already put
        correct vectors in place through some other path (e.g. a future
        deep-copy that remaps ids via raw SQL) and wants a fast structural-
        only KO/edge rebuild with a hard guarantee of zero embedder calls,
        not merely "none turned out to be necessary".

        Only the embedding call is failure-tolerant (network I/O) — every
        structural write either succeeds or raises normally (a genuine bug
        must not be silently swallowed and mis-reported as an "embedding
        failure"); see ``_write_elements``/``_write_chunks`` and the per-row
        try/except below."""
        with self.database.table_projection_lock(table_id):
            return self._project_table_locked(table_id, embed=embed)

    def _project_table_locked(self, table_id: str, *, embed: bool) -> str:
        """Run one pass while the backend's table-level projection lock is held."""
        table = self.knowhow.get_knowhow_table(table_id)
        notebook_id = table["notebook_id"]
        snapshot_mutation_seq = table["mutation_seq"]
        source_id = self.ensure_hidden_source(table_id)
        columns = table["columns"]
        anchor_column = next((c for c in columns if c["role"] == "anchor"), None)
        now = self.now()

        # Table-wide KO/edge accumulation — merging is inherently cross-row,
        # so this can only be resolved once every row has been walked (see
        # module docstring point 2 for why the actual DB rebuild waits until
        # after the loop, not just this accumulation).
        ko_acc: dict = {}
        edge_rows: List[tuple] = []
        edge_ids_seen: set = set()
        row_results: List[tuple[str, str]] = []

        for row in table["rows"]:
            row_id = row["id"]
            self.knowhow.set_knowhow_row_projection_if_table_seq(
                row_id, "syncing", snapshot_mutation_seq
            )
            cells = row["cells"]
            cell_nets = {
                column["id"]: textops.strip_images(cells.get(column["id"], "")).strip()
                for column in columns
            }
            row_title = _resolve_row_title(anchor_column, columns, cell_nets, row["position"])

            try:
                with self.database.write() as db:
                    self._write_elements(
                        db, source_id, table_id, row_id, columns, cell_nets, row_title, now
                    )
                    embed_targets, carry_over_vectors = self._write_chunks(
                        db, source_id, notebook_id, table, row_id, columns, cell_nets,
                        row_title, now,
                    )
                    if anchor_column is not None:
                        self._accumulate_row_knowledge(
                            ko_acc, edge_rows, edge_ids_seen, notebook_id, source_id,
                            table_id, table, row_id, columns, cell_nets, anchor_column,
                            row_title, now,
                        )
            except Exception:
                # Structural writes (elements/chunks/in-memory KO accumulation)
                # are a programming surface, not network I/O — unlike the
                # embed call below, a failure here is a genuine bug and must
                # fail loud (re-raise). But the row must not be left claiming
                # 'syncing' forever: no other code path ever revisits it, so a
                # silent-'syncing' row would look perpetually in-progress
                # instead of honestly failed.
                for completed_row_id, _status in row_results:
                    self.knowhow.set_knowhow_row_projection_if_table_seq(
                        completed_row_id, "failed", snapshot_mutation_seq
                    )
                self.knowhow.set_knowhow_row_projection_if_table_seq(
                    row_id, "failed", snapshot_mutation_seq
                )
                raise

            failed = False
            if embed and (embed_targets or carry_over_vectors):
                # Carry-over first: it's a plain re-persist of an already-
                # computed vector (no embedder call, must run once the
                # structural transaction above has committed — chunk_
                # embeddings' FK target must exist first). Keep this local
                # persistence failure out of provider-health telemetry.
                if carry_over_vectors:
                    try:
                        self.embedding.vectors.replace_chunk_vectors(
                            notebook_id, carry_over_vectors, created_at=now
                        )
                    except Exception as exc:  # noqa: BLE001 — best-effort vector restore
                        failed = True
                        self.note_model_error(
                            "knowhow_vector_restore",
                            "",
                            exc,
                            service="embedding",
                            provider_failure=False,
                        )

                if embed_targets and not failed:
                    try:
                        self.embedding.embed_chunk_ids(notebook_id, embed_targets)
                    except ModelProviderError as exc:
                        failed = True
                        self.note_model_error(
                            "knowhow_embed",
                            exc,
                            workload_id="knowhow_embedding",
                        )
                    except Exception as exc:  # noqa: BLE001 — local vector write is best-effort
                        failed = True
                        self.note_model_error(
                            "knowhow_vector_persist",
                            "",
                            exc,
                            service="embedding",
                            provider_failure=False,
                        )

            # ``synced`` is the public completion signal consumed by API
            # pollers. Keep every visited row at ``syncing`` until the
            # table-wide KO/edge transaction below has committed; publishing
            # the row result here creates a window where all rows look settled
            # while the graph is still empty or stale.
            row_results.append((row_id, "failed" if failed else "synced"))

        object_rows = [
            self._ko_object_row(notebook_id, source_id, ko_id, entry, now)
            for ko_id, entry in ko_acc.items()
        ]
        # Final existence guard (PR review round 1 P1-2, cross-notebook
        # move/copy): a concurrent teardown -- move_table's
        # delete_table_projection + delete_knowhow_table, or the plain DELETE
        # route's identical pair -- can land ANYWHERE during the per-row loop
        # above (production's realistic window is "parked in
        # embed_chunk_ids mid-row, several rows in"). knowledge_objects.
        # source_id carries NO FK to sources, so nothing else would stop
        # insert_object_chunk below from committing a fresh KO batch against
        # a table_id/source_id that no longer exist -- permanently
        # unreclaimable ghosts in whatever notebook just had this table torn
        # down (insert_relation_chunk, when edge_rows is non-empty, does
        # carry an FK to sources and would instead raise IntegrityError and
        # roll the whole transaction back -- a different, also-real
        # "background job crashes" symptom of the exact same missing check).
        #
        # PR review round 2 P1-1: round 1's fix re-checked here but OUTSIDE
        # this write() block (via table_exists()/source_exists(), each
        # opening its own connect()) -- itself a fresh check-then-act gap. A
        # concurrent move landing between that check returning True and this
        # write() actually acquiring the write lock would sail straight
        # through unblocked, recreating the exact orphan the guard exists to
        # prevent (see test_project_table_existence_check_and_terminal_write_
        # are_atomic). The check must run ON db, as the FIRST thing inside
        # this block, so it and the writes below share one lock-held
        # transaction: database.write() holds write_lock for the block's
        # entire duration, and any concurrent delete_knowhow_table/
        # delete_table_projection needs its own write() call, so it either
        # fully lands before this block starts (table_exists_tx/
        # source_exists_tx correctly observe "gone", we bail) or blocks until
        # this block finishes (nothing to race). Bail out here -- skipping
        # the write AND the mutation-seq bump below -- rather than
        # resurrecting anything a teardown already removed. Mirrors
        # delete_table_projection's own pre-write existence check just below.
        target_exists = True
        try:
            with self.database.write() as db:
                # 显式开写事务：write_lock 只互斥本进程写者；离线 CLI/维护进程共库时，
                # 下面两个存在性 SELECT 在首条 DML 前不开启 SQLite 事务，另一进程可在
                # 「检查通过」与「首条写入」之间提交删表/删源，让 anchor-only 投影插出
                # 无 FK 约束的孤儿 knowledge_objects（已删/已移内容仍可检索）。经
                # SqliteDatabase.begin_immediate（SQL 收在 store 层）让检查与写入对
                # 跨进程写者也原子——与 snapshot_table/delete_table_if_unchanged 同款。
                self.database.begin_guarded_write(db)
                if (
                    not self.knowhow.table_exists_tx(db, table_id)
                    or not self.sources.source_exists_tx(db, source_id)
                ):
                    target_exists = False
                else:
                    # Same transaction, BEFORE the objects go: strip the
                    # concept_clusters membership of exactly the objects this
                    # reprojection DROPS (a deleted column/row/renamed cell).
                    # KO ids are stable content hashes, so everything in
                    # `object_rows` is about to be written straight back and
                    # keeps its membership untouched — a blind wipe would
                    # de-cluster live objects, which is why the delete path's
                    # own cleanup is not reused here. With this in place the
                    # repository has no path left that leaves a dangling
                    # membership row, so `incremental_fuse_source`'s sweep is a
                    # pure per-process legacy backstop (and multi-worker safe:
                    # nothing depends on an in-process signal reaching another
                    # worker).
                    self.knowledge.prune_cluster_rows_for_source(
                        db, notebook_id, source_id,
                        keep_object_ids=[row[0] for row in object_rows],
                    )
                    self.knowledge.delete_relations_by_source(db, source_id)
                    self.knowledge.delete_objects_by_source(db, source_id)
                    if object_rows:
                        self.knowledge.insert_object_chunk(db, object_rows)
                        # Knowhow uses stable object ids and writes the graph
                        # through lower-level chunk primitives, so it must
                        # maintain the provenance reverse index explicitly in
                        # this same publication transaction.  A newly created
                        # notebook is certified-empty; leaving these rows out
                        # would make that certificate lie after the first
                        # projection and source-restricted retrieval could
                        # safely-but-incorrectly omit every Knowhow object.
                        for row in object_rows:
                            self.knowledge.replace_object_sources(
                                db, row[0], notebook_id, row[5]
                            )
                    if edge_rows:
                        self.knowledge.insert_relation_chunk(db, edge_rows)
                    # Same commit as the delete-and-reinsert above (codex #638
                    # R5). This branch always rewrites graph rows (the three
                    # deletes are unconditional), and it is the only branch that
                    # reaches the old post-commit bump — the `not target_exists`
                    # bail below returns before it — so the bump count is
                    # unchanged; only its commit boundary moved. Reaching it via
                    # the ex-post call meant a reprojection could be durable
                    # while kg_mutation_seq still described the pre-projection
                    # graph, which is exactly what the seq gate exists to
                    # prevent.
                    self.mark_unified_dirty_in_tx(db, notebook_id)

            if not target_exists:
                for row_id, _status in row_results:
                    self.knowhow.set_knowhow_row_projection_if_table_seq(
                        row_id, "failed", snapshot_mutation_seq
                    )
                return

            projected_mutation_seq = self.knowhow.bump_knowhow_mutation_seq(table_id)
            self.invalidate_unified_cache(notebook_id)
        except Exception:
            for row_id, _status in row_results:
                self.knowhow.set_knowhow_row_projection_if_table_seq(
                    row_id, "failed", snapshot_mutation_seq
                )
            raise

        # The projector's own bump is the sole allowed mutation since the
        # hydrated snapshot. Any larger sequence means a user edit landed
        # during this pass and the scheduler has a newer rerun queued. Leave
        # its pending/syncing markers intact. Even in the clean case each row
        # publication remains sequence-conditional, closing an edit race that
        # can land between this comparison and an individual status write.
        if projected_mutation_seq == snapshot_mutation_seq + 1:
            for row_id, status in row_results:
                self.knowhow.set_knowhow_row_projection_if_table_seq(
                    row_id, status, projected_mutation_seq
                )
        # Hand the owning notebook back to the caller. The scheduler needs it to
        # run the throttled orphan-asset sweep after a projection (see
        # app/services/knowhow/api.py's run_projection_and_sweep) and this pass
        # already resolved it above — returning it avoids a second table read
        # (get_knowhow_table loads every row + cell) purely to learn one id.
        return notebook_id

    # --- elements ------------------------------------------------------
    def _write_elements(self, db, source_id, table_id, row_id, columns, cell_nets, row_title, now):
        self.sources.delete_elements_by_knowhow_row(db, source_id, row_id)
        writes = []
        for column in columns:
            text = cell_nets[column["id"]]
            if not text:
                continue
            content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            writes.append(SourceElementWrite(
                id=element_id(row_id, column["id"]),
                element_type="knowhow_cell",
                location_label=f"{row_title} › {column['name']}",
                text=text,
                metadata={"knowhow": {
                    "table_id": table_id, "row_id": row_id, "column_id": column["id"],
                    "role": column["role"], "column_name": column["name"],
                    "row_title": row_title, "content_hash": content_hash,
                }},
            ))
        self.sources.insert_elements(db, source_id, writes, created_at=now)

    # --- chunks (per-cell diff) -----------------------------------------
    def _write_chunks(self, db, source_id, notebook_id, table, row_id, columns,
                      cell_nets, row_title, now) -> "tuple[List[dict], List[tuple]]":
        """Returns ``(embed_targets, carry_over_vectors)``:
        ``embed_targets`` (``[{"id","text"}, ...]``) — chunks whose TEXT
        actually changed, which genuinely need a fresh embedder call, PLUS
        any text-unchanged chunk the self-heal probe below finds vector-less.
        ``carry_over_vectors`` (``[(chunk_id, ndarray), ...]``) — chunks
        whose text did NOT change but whose row still had to be rewritten
        (section_path moved — see below), paired with their OLD vector so
        the caller can re-persist it post-commit without recomputing it."""
        row_hash = _chunk_row_hash(row_id)
        id_prefix = f"chunk-kh-{row_hash}-"
        old_rows = self.chunks.rows_by_id_prefix(db, source_id, id_prefix)
        old_by_col: dict = {}
        for r in old_rows:
            part = int(str(r["id"]).rsplit("-", 1)[-1])
            old_by_col.setdefault(part // _COLUMN_PART_STRIDE, []).append(r)
        for group in old_by_col.values():
            group.sort(key=lambda r: int(str(r["id"]).rsplit("-", 1)[-1]))

        live_col_positions: set = set()
        to_delete_ids: List[str] = []
        to_insert: List[ChunkWrite] = []
        reprint_only_ids: List[str] = []  # text unchanged, section_path moved
        # Every chunk id whose TEXT survives this pass unchanged (kept fully
        # in place OR reprint-only rewritten), mapped to that current text —
        # the probe population for the self-heal check below.
        unchanged_text_chunks: dict = {}
        embed_targets: List[dict] = []
        for column in columns:
            col_pos = column["position"]
            live_col_positions.add(col_pos)
            text = cell_nets[column["id"]]
            section_path = f"{table['title']} › {row_title} › {column['name']}"
            parts_text = _split_long_text(text) if text else []
            new_specs = [(t, section_path) for t in parts_text]
            old_group = old_by_col.get(col_pos, [])
            old_specs = [(r["text"], r["section_path"]) for r in old_group]
            if old_specs == new_specs:
                # unchanged cell — zero deletes/inserts, and zero embeds in
                # steady state; still recorded for the self-heal probe.
                for r in old_group:
                    unchanged_text_chunks[r["id"]] = r["text"]
                continue
            # Text-only equality (ignoring section_path) — e.g. this is a
            # SIBLING of the cell that actually changed: every column's
            # section_path embeds the row's title, so editing the row-title
            # cell alone (or, for an anchor-less/blank-anchor row, any cell
            # feeding compose_row_title) moves every sibling's path even
            # though their own text is untouched. The row must still be
            # rewritten (path moved), but nothing here should pay for a
            # redundant embedder call on text that hasn't changed.
            text_only_changed = [t for t, _ in old_specs] == [t for t, _ in new_specs]
            to_delete_ids.extend(r["id"] for r in old_group)
            if not text:
                continue
            eid = element_id(row_id, column["id"])
            for split_idx, part_text in enumerate(parts_text, start=1):
                cid = f"chunk-kh-{row_hash}-{col_pos * _COLUMN_PART_STRIDE + split_idx}"
                to_insert.append(ChunkWrite(
                    id=cid, text=part_text, section_path=section_path, element_ids=(eid,),
                ))
                if text_only_changed:
                    reprint_only_ids.append(cid)
                    unchanged_text_chunks[cid] = part_text
                else:
                    embed_targets.append({"id": cid, "text": part_text})

        # A column removed (or renumbered away) from the table since the
        # last projection leaves its old chunk group unvisited by the loop
        # above (it only walks CURRENT columns) — sweep every old group
        # whose position no longer belongs to a live column so project_table
        # alone stays self-healing for column deletes/reorders, the same
        # delete-all-then-reinsert reconciliation _write_elements already
        # does by row_id.
        for col_pos, group in old_by_col.items():
            if col_pos not in live_col_positions:
                to_delete_ids.extend(r["id"] for r in group)

        # Self-heal vector probe — ONE id-keyed SELECT over every chunk whose
        # text is not changing this pass (kept-unchanged + reprint-only), run
        # before delete_by_ids below (the reprint-only rows' embeddings are
        # about to be cascade-deleted; kept rows are unaffected, but one
        # probe covers both):
        #   - vector present + reprint-only -> carry it over (re-persisted
        #     post-commit by the caller, zero embedder calls);
        #   - vector present + kept         -> nothing to do (steady state);
        #   - vector ABSENT (or undecodable) -> append to embed_targets with
        #     its current text. Without this, a text-unchanged chunk that
        #     lost its vector (a previous pass's post-commit carry-over/embed
        #     window interrupted by a crash or a raise) stayed vector-less
        #     FOREVER — every later pass hit `old_specs == new_specs ->
        #     continue` and nothing else re-embeds knowhow chunks. Now a
        #     per-row reproject/retry is a real recovery path.
        carry_over_vectors: List[tuple] = []
        if unchanged_text_chunks:
            reprint_set = set(reprint_only_ids)
            have: set = set()
            for r in self.embedding.vectors.rows_by_ids(
                db, "chunk_embeddings", "chunk_id", list(unchanged_text_chunks)
            ):
                vec = decode_vector(r["vector"])
                if vec is None:
                    continue  # undecodable counts as missing -> re-embed below
                have.add(r["vid"])
                if r["vid"] in reprint_set:
                    carry_over_vectors.append((r["vid"], vec))
            embed_targets.extend(
                {"id": cid, "text": text}
                for cid, text in unchanged_text_chunks.items()
                if cid not in have
            )

        self.chunks.delete_by_ids(db, to_delete_ids)
        self.chunks.insert_rows(db, notebook_id, source_id, to_insert, created_at=now)
        return embed_targets, carry_over_vectors

    # --- KO/edge accumulation (in-memory; written once at the end) --------
    def _accumulate_row_knowledge(self, ko_acc, edge_rows, edge_ids_seen, notebook_id,
                                  source_id, table_id, table, row_id, columns, cell_nets,
                                  anchor_column, row_title, now) -> None:
        """This row's contribution to the table-wide KO/edge model (design
        doc §④): the row-title (anchor) cell's own KO, then every OTHER
        non-empty cell's KO with an ``about`` edge pointing at it. A row
        whose anchor cell itself has NO content contributes NOTHING —
        neither a row-title KO (a KO's identity is a real column value; an
        empty cell has none, matching the universal "每个非空格子→KO" rule
        applied to the anchor column too) nor, therefore, any edges for its
        other cells (there is no target to point at). Those other cells are
        NOT silently dropped from the graph forever, though: if the SAME
        value recurs in a row whose anchor IS filled, it merges into that
        occurrence's KO and picks up an edge there. Entity-kind cells split
        into one KO per list item (``textops.split_tools``); procedure-kind
        cells attach their parsed ``steps[]``; everything else is one KO for
        the whole cell's net text."""
        anchor_text = cell_nets[anchor_column["id"]]
        if not anchor_text:
            return
        row_title_ko_id = self._merge_cell(
            ko_acc, table_id, anchor_column, anchor_text, row_id, row_title, source_id, table, now,
        )
        for column in columns:
            if column["id"] == anchor_column["id"]:
                continue
            text = cell_nets[column["id"]]
            if not text:
                continue
            if column["role"] == "entity":
                for item in textops.split_tools(text):
                    item_ko_id = self._merge_cell(
                        ko_acc, table_id, column, item, row_id, row_title, source_id, table, now,
                    )
                    self._add_about_edge(
                        edge_rows, edge_ids_seen, notebook_id, source_id,
                        item_ko_id, row_title_ko_id, now,
                    )
            else:
                steps = textops.parse_steps(text) if column["role"] == "procedure" else None
                cell_ko_id = self._merge_cell(
                    ko_acc, table_id, column, text, row_id, row_title, source_id, table, now,
                    steps=steps,
                )
                self._add_about_edge(
                    edge_rows, edge_ids_seen, notebook_id, source_id,
                    cell_ko_id, row_title_ko_id, now,
                )

    @staticmethod
    def _merge_cell(ko_acc, table_id, column, text, row_id, row_title, source_id, table,
                    now, *, steps=None) -> str:
        """Merge one cell's value into the accumulator, returning its KO id.
        FIRST occurrence wins for the stored name/text/steps (mirrors
        ``textops.split_tools``'s own "keeps first-seen spelling/casing"
        convention, and the PR-1 tool-merge precedent it replaces) — a later
        occurrence only ever ADDS a row_id + an evidence item, never
        overwrites. Evidence's location_label uses THIS row's OWN row_title
        (not the merged KO's stored name) — section_path/evidence are a
        per-row display concern, independent of which literal text a
        cross-row merge happened to see first."""
        val_key = textops.value_key(text)
        ko_id = _cell_ko_id(table_id, column["name"], val_key)
        evidence_item = _evidence(
            source_id, table["title"], element_id(row_id, column["id"]),
            "knowhow_cell", f"{row_title} › {column['name']}", text,
        )
        entry = ko_acc.get(ko_id)
        if entry is None:
            ko_acc[ko_id] = {
                "object_type": column["name"],
                "name": textops.node_name(text),
                "text": text,
                "table_id": table_id,
                "column_id": column["id"],
                "rows": [row_id],
                "evidence": [evidence_item],
                "steps": steps,
            }
        else:
            if row_id not in entry["rows"]:
                entry["rows"].append(row_id)
            entry["evidence"].append(evidence_item)
        return ko_id

    @staticmethod
    def _add_about_edge(edge_rows, edge_ids_seen, notebook_id, source_id,
                        src_ko_id, dst_ko_id, now) -> None:
        """Append one ``about`` edge (cell-KO --about--> row-title-KO),
        deduped by its own content-derived id: two rows that share BOTH
        endpoints (same cell value AND same row-title value) produce the
        exact same edge id, and inserting it twice in one ``executemany``
        batch would violate ``knowledge_relations.id``'s primary key — so
        this collapses to a single row instead, exactly matching "同列同值
        跨行归并" applied to edges. The row-title cell's own KO never calls
        this for itself (the accumulation loop above only invokes it for
        OTHER cells), so no self-edge is possible."""
        edge_id = _relation_id(src_ko_id, "about", dst_ko_id)
        if edge_id in edge_ids_seen:
            return
        edge_ids_seen.add(edge_id)
        edge_rows.append(
            (edge_id, notebook_id, source_id, src_ko_id, dst_ko_id, "about", "[]", now)
        )

    @staticmethod
    def _ko_object_row(notebook_id, source_id, ko_id, entry, now) -> tuple:
        """Render one accumulated KO entry into ``insert_object_chunk``'s row
        tuple (design doc §④ payload shape: name/text/table_id/rows/
        column_id/column_name, plus ``steps`` only for procedure-kind
        cells)."""
        payload = {
            "name": entry["name"],
            "text": entry["text"],
            "table_id": entry["table_id"],
            "rows": entry["rows"],
            "column_id": entry["column_id"],
            "column_name": entry["object_type"],
        }
        if entry["steps"] is not None:
            payload["steps"] = entry["steps"]
        return (
            ko_id, notebook_id, entry["object_type"], "approved",
            json.dumps(payload, ensure_ascii=False),
            json.dumps(entry["evidence"], ensure_ascii=False),
            source_id, now, now,
        )

    # ------------------------------------------------------------- delete
    def delete_table_projection(self, hidden_source_id: "str | None") -> None:
        """Cleanup companion to KnowhowStore.delete_knowhow_table: that call
        cascades away the table/columns/rows/cells and hands back the
        (possibly None) hidden_source_id; this removes everything the
        projector ever derived from it — relations, objects (every dynamic
        per-column type this table ever produced), chunks(+FTS), elements,
        and the hidden source row itself. Idempotent no-op for a falsy or
        already-gone id (this codebase's zero-row UPDATE/DELETE convention —
        see KnowhowStore's docstring).

        Ordering note: chunks are wiped (replace_source_chunks, which cleans
        chunks_fts via a `chunks WHERE source_id=?` subquery) BEFORE the
        source row is deleted — chunks.source_id cascades on sources, so
        deleting the source row first would leave that subquery finding
        nothing and orphan the chunks_fts rows."""
        if not hidden_source_id:
            return
        try:
            source = self.sources.get_source(hidden_source_id)
        except KeyError:
            return
        notebook_id = source.notebook_id
        now = self.now()
        self.chunks.replace_source_chunks(hidden_source_id, notebook_id, [], created_at=now)
        with self.database.write() as db:
            self.database.begin_guarded_write(db)
            if self.sources.source_exists_for_update_tx(db, hidden_source_id):
                # Teardown: nothing is reinserted, so every membership row of
                # this source's objects goes with them, in this transaction.
                self.knowledge.prune_cluster_rows_for_source(
                    db, notebook_id, hidden_source_id
                )
                self.knowledge.delete_relations_by_source(db, hidden_source_id)
                self.knowledge.delete_objects_by_source(db, hidden_source_id)
                self.sources.replace_elements(db, hidden_source_id, [], created_at=now)
                self.sources.delete_source_row(db, hidden_source_id)
            # Teardown deletes graph rows, so its seq bump commits with them
            # (codex #638 R5). UNCONDITIONAL, outside the existence guard, to
            # match the post-commit call it replaces one-for-one: this moves the
            # bump's time point, not its count.
            self.mark_unified_dirty_in_tx(db, notebook_id)
        self.invalidate_unified_cache(notebook_id)

    # ------------------------------------------- legacy-model migration
    def reproject_legacy_tables(self) -> "list[str]":
        """One-shot startup catch-up (PR-2+3 Task 2's migration bridge, wired
        in by ``app.services.startup_warmup``): find every knowhow table
        still carrying PR-1's fixed-vocabulary KOs (see
        ``find_legacy_projected_table_ids``) and submit ONE background
        ``project_table`` job per table via ``app.services.background_jobs``
        (governance constraint: background work only ever goes through
        ``background_jobs.submit`` — never a bare thread), replacing them
        with the cell-level dynamic-type model. Structural rebuild only —
        any cell whose text hasn't changed keeps its existing chunk/vector
        (the same per-cell diff ``project_table`` always does), so this
        migration costs zero LLM/embedder calls per already-embedded cell.
        Self-extinguishing: once reprojected, a table's KOs no longer match
        the legacy detection query, so a later call (e.g. the next process
        restart) finds nothing left to do. Returns the table_ids it
        scheduled, for test assertions/logging — the caller decides whether
        and how to report that.

        Deliberately bypasses ProjectionScheduler's per-table single-flight
        (direct ``background_jobs.submit``): racing a concurrent user edit
        costs at most ONE duplicate ``project_table`` pass, and that is
        safe — per-row delete+insert runs in one transaction and the final
        KG swap is atomic, so whichever pass finishes last leaves the same
        deterministic full-table result, never a corrupted mix."""
        with self.database.connect() as db:
            table_ids = find_legacy_projected_table_ids(self.knowledge, db)
        for table_id in table_ids:
            background_jobs.submit(
                self.project_table, table_id,
                name=f"knowhow-legacy-reproject-{table_id}",
            )
        return table_ids


__all__ = [
    "KnowhowProjector",
    "find_legacy_projected_table_ids",
    "element_id",
    "cell_chunk_id",
]
