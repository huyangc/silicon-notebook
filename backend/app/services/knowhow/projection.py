"""Deterministic, zero-LLM projection of knowhow-table rows into the
notebook's knowledge machinery (Task 5, knowhow-tables PR-1).

``KnowhowProjector`` mirrors ``kg_ingest.build_records``'s knowledge_objects/
knowledge_relations shape (design doc §④) but writes with STABLE, content-
derived ids instead of store_kg's fresh-id-per-call allocation — reprojecting
a row is an idempotent in-place rewrite, not an append, which is what makes
"edit one cell -> only that cell's derivatives change" possible at all.

Every row's derived artifacts hang off ONE hidden synthetic source (mirrors
the Memory<->KG hidden-source pattern in ``source_ingestion.ingest_memory_
source`` — ``source_type="knowhow"``, excluded from user-facing source
listings the same way ``source_type="memory"`` already is). Composition rules
mirror ``KnowledgeLifecycleService``/``SourceChunkingService``: no facade
import, no raw SQL here — every write goes through an injected store method,
and this service owns its own ``database.write()`` transaction boundary for
the structural (non-embedding) part of a projection.

One nuance worth flagging up front: ``_write_chunks`` sometimes needs to
rewrite a chunk row whose TEXT never changed (only its ``section_path``
did — see that method's docstring). ``chunk_embeddings.chunk_id`` is ``ON
DELETE CASCADE`` onto ``chunks(id)`` and ChunkStore has no in-place "update
one column" primitive, only delete/insert — so that rewrite still goes
through the existing ``delete_by_ids``/``insert_rows`` pair (no new store
method, no raw SQL), and the row's still-valid embedding is carried over
(read via ``EmbeddingStore.rows_by_ids`` before the delete, re-persisted via
``SourceEmbeddingService.vectors.replace_chunk_vectors`` after the
transaction commits) instead of being recomputed or silently lost. The same
pre-delete probe doubles as a self-heal: any text-unchanged chunk found with
NO vector at all (an earlier post-commit persist window interrupted) is
queued for a fresh embed, so reprojecting a row repairs vector gaps instead
of skipping the "unchanged" cell forever.
"""
from __future__ import annotations

import hashlib
import json
from typing import Callable, List

from app.core.config import Settings
from app.repositories.sqlite.chunk_store import ChunkStore, ChunkWrite
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.embedding_store import EmbeddingStore
from app.repositories.sqlite.knowhow_store import KnowhowStore
from app.repositories.sqlite.knowledge_store import KnowledgeStore
from app.repositories.sqlite.source_store import SourceElementWrite, SourceStore
from app.services.knowhow import textops
from app.services.source_embedding import SourceEmbeddingService
from app.services.vector_index import decode_vector

# Column roles that become a `procedure` object per non-empty cell, and their
# case->procedure edge_type (design doc §④ / task brief step 4).
PROCEDURE_ROLES = ("identify", "root_cause", "fix")
_EDGE_BY_ROLE = {
    "identify": "identified_by",
    "root_cause": "diagnosed_by",
    "fix": "fixed_by",
}

_CHUNK_CHAR_LIMIT = 4000
# `part = column.position * _COLUMN_PART_STRIDE + split_index` gives every
# cell a STABLE numeric part-range (independent of sibling cells being
# empty/filled) so a single-cell edit's old-vs-new chunk diff can be done
# PER CELL, not per row — up to 99 split-parts per single cell before this
# would collide, which a "few hundred to a few screens of text" cell (design
# doc §, "百行内...几百字到几屏富文本") never approaches.
_COLUMN_PART_STRIDE = 100


def _h(*parts: str) -> str:
    """Stable content hash — matches the brief's derived-id contract verbatim
    (``_h=lambda *p: hashlib.sha1("|".join(p).encode()).hexdigest()``) so ids
    stay reproducible across processes/restarts, the entire point of
    "deterministic": Task 4/6/10 and later PRs depend on the exact prefixes
    below (``ko-kh-``/``el-kh-``/``chunk-kh-``/``kr-kh-``)."""
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def _case_id(row_id: str) -> str:
    return f"ko-kh-{_h('case', row_id)[:32]}"


def _procedure_id(row_id: str, column_id: str) -> str:
    return f"ko-kh-{_h('proc', row_id, column_id)[:32]}"


def _tool_id(table_id: str, norm_name: str) -> str:
    return f"ko-kh-{_h('tool', table_id, norm_name)[:32]}"


def _element_id(row_id: str, column_id: str) -> str:
    return f"el-kh-{_h(row_id, column_id)[:32]}"


def _chunk_row_hash(row_id: str) -> str:
    return _h(row_id)[:16]


def _relation_id(source_object_id: str, edge_type: str, target_object_id: str) -> str:
    return f"kr-kh-{_h(source_object_id, edge_type, target_object_id)[:32]}"


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
    """Product Evidence shape (app.models.schemas.Evidence) — mirrors
    kg_ingest._ev's fields exactly so knowhow-derived KOs/edges are readable
    by every existing evidence consumer (citation rendering, graph
    retrieval's element->chunk bridge) without a special case."""
    return {
        "source_id": source_id, "source_title": source_title, "element_id": element_id,
        "element_type": element_type, "location_label": location_label,
        "quoted_span": (quote or "")[:400], "confidence": 1.0,
    }


class KnowhowProjector:
    def __init__(
        self,
        *,
        settings: Settings,
        database: SqliteDatabase,
        knowhow: KnowhowStore,
        sources: SourceStore,
        chunks: ChunkStore,
        knowledge: KnowledgeStore,
        embedding: SourceEmbeddingService,
        note_model_error: Callable[[str, str, Exception], None],
        invalidate_unified_cache: Callable[[str], None],
        mark_unified_dirty: Callable[[str], None],
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
        self.mark_unified_dirty = mark_unified_dirty
        self.new_id = new_id
        self.now = now

    # ------------------------------------------------------- hidden source
    def ensure_hidden_source(self, table_id: str) -> str:
        """Idempotent: returns the table's existing hidden source if one is
        already recorded and still resolves, else mints one. ``project_row``
        calls this itself (its own signature carries no source_id), so a
        table's very first cell edit transparently creates its hidden
        source — callers never need to sequence ensure_hidden_source before
        project_row by hand."""
        table = self.knowhow.get_knowhow_table(table_id)
        existing = table.get("hidden_source_id")
        if existing:
            try:
                self.sources.get_source(existing)
                return existing
            except KeyError:
                pass  # recorded id no longer resolves — recreate below
        source_id = self.new_id("src")
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
        )
        self.knowhow.set_knowhow_hidden_source(table_id, source_id)
        return source_id

    # ------------------------------------------------------------- project
    def project_row(self, table_id: str, row_id: str) -> None:
        """Idempotent, per-row projection (task brief step ①-⑤). Only the
        embedding call is failure-tolerant (network I/O) — everything else
        either succeeds or raises normally (a genuine bug shouldn't be
        silently swallowed and mis-reported as an "embedding failure")."""
        table = self.knowhow.get_knowhow_table(table_id)
        row = next((r for r in table["rows"] if r["id"] == row_id), None)
        if row is None:
            return  # deleted mid-flight — nothing left to project

        self.knowhow.set_knowhow_row_projection(row_id, "syncing")
        notebook_id = table["notebook_id"]
        source_id = self.ensure_hidden_source(table_id)
        columns = table["columns"]
        cells = row["cells"]
        position = row["position"]

        # ① concept = concept cell's net-text FIRST LINE, else "行{position+1}"
        concept_column = next((c for c in columns if c["role"] == "concept"), None)
        concept_raw = (
            textops.strip_images(cells.get(concept_column["id"], "")).strip()
            if concept_column is not None else ""
        )
        concept = (concept_raw.splitlines()[0].strip() if concept_raw else "") or (
            f"行{position + 1}"
        )
        cell_nets = {
            column["id"]: textops.strip_images(cells.get(column["id"], "")).strip()
            for column in columns
        }

        now = self.now()

        try:
            with self.database.write() as db:
                self._write_elements(db, source_id, table_id, row_id, columns, cell_nets, concept, now)
                embed_targets, carry_over_vectors = self._write_chunks(
                    db, source_id, notebook_id, table, row_id, columns, cell_nets, concept, now
                )
                self._write_knowledge(
                    db, source_id, notebook_id, table, table_id, row_id, columns, cell_nets, concept, now
                )
        except Exception:
            # Structural writes (elements/chunks/KOs) are a programming
            # surface, not network I/O — unlike the embed call below, a
            # failure here is a genuine bug and must fail loud (re-raise).
            # But the row must not be left claiming 'syncing' forever: no
            # other code path ever revisits it, so a silent-'syncing' row
            # would look perpetually in-progress instead of honestly failed.
            self.knowhow.set_knowhow_row_projection(row_id, "failed")
            raise

        failed = False
        if embed_targets or carry_over_vectors:
            try:
                # Carry-over first: it's a plain re-persist of an already-
                # computed vector (no embedder call, must run once the
                # structural transaction above has committed — chunk_
                # embeddings' FK target must exist first), independent of
                # whatever embed_chunk_ids below does.
                if carry_over_vectors:
                    self.embedding.vectors.replace_chunk_vectors(
                        notebook_id, carry_over_vectors, created_at=now
                    )
                if embed_targets:
                    self.embedding.embed_chunk_ids(notebook_id, embed_targets)
            except Exception as exc:  # noqa: BLE001 — surfaced via model_error, never raised
                failed = True
                self.note_model_error("knowhow_embed", self.settings.embed_model, exc)

        self.knowhow.set_knowhow_row_projection(row_id, "failed" if failed else "synced")
        self.knowhow.bump_knowhow_mutation_seq(table_id)
        self.invalidate_unified_cache(notebook_id)
        self.mark_unified_dirty(notebook_id)

    # --- step 2: elements --------------------------------------------------
    def _write_elements(self, db, source_id, table_id, row_id, columns, cell_nets, concept, now):
        self.sources.delete_elements_by_knowhow_row(db, source_id, row_id)
        writes = []
        for column in columns:
            text = cell_nets[column["id"]]
            if not text:
                continue
            content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            writes.append(SourceElementWrite(
                id=_element_id(row_id, column["id"]),
                element_type="knowhow_cell",
                location_label=f"{concept} › {column['name']}",
                text=text,
                metadata={"knowhow": {
                    "table_id": table_id, "row_id": row_id, "column_id": column["id"],
                    "role": column["role"], "column_name": column["name"],
                    "concept": concept, "content_hash": content_hash,
                }},
            ))
        self.sources.insert_elements(db, source_id, writes, created_at=now)

    # --- step 3: chunks (per-cell diff) ------------------------------------
    def _write_chunks(self, db, source_id, notebook_id, table, row_id, columns,
                      cell_nets, concept, now) -> "tuple[List[dict], List[tuple]]":
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
            section_path = f"{table['title']} › {concept} › {column['name']}"
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
            # section_path embeds the row's concept, so editing the concept
            # cell alone moves every sibling's path even though their own
            # text is untouched. The row must still be rewritten (path
            # moved), but nothing here should pay for a redundant embedder
            # call on text that hasn't changed.
            text_only_changed = [t for t, _ in old_specs] == [t for t, _ in new_specs]
            to_delete_ids.extend(r["id"] for r in old_group)
            if not text:
                continue
            eid = _element_id(row_id, column["id"])
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
        # whose position no longer belongs to a live column so project_row
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
            for r in EmbeddingStore.rows_by_ids(
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

    # --- step 4: KO / edges -------------------------------------------------
    def _write_knowledge(self, db, source_id, notebook_id, table, table_id, row_id,
                         columns, cell_nets, concept, now) -> None:
        row_case_id = _case_id(row_id)
        self.knowledge.delete_relations_by_source_object(db, notebook_id, row_case_id)
        self.knowledge.delete_objects_by_source_and_row(db, source_id, row_id)

        # A row with no content in ANY cell projects NO case KO (belt-and-braces
        # with grid_parser dropping all-empty rows at import — this covers the
        # edit path, e.g. a row whose every cell was cleared). The delete calls
        # above already ran, so an emptied row correctly leaves zero KOs behind
        # rather than a phantom empty-fields case.
        if not any(cell_nets.values()):
            return

        object_rows: List[tuple] = []
        relation_rows: List[tuple] = []

        fields: dict = {}
        case_evidence = []
        for column in columns:
            text = cell_nets[column["id"]]
            if not text:
                continue
            fields[column["role"]] = text
            case_evidence.append(_evidence(
                source_id, table["title"], _element_id(row_id, column["id"]),
                "knowhow_cell", f"{concept} › {column['name']}", text,
            ))
        case_payload = {"title": concept, "table_id": table_id, "row_id": row_id, "fields": fields}
        object_rows.append((
            row_case_id, notebook_id, "case", "approved",
            json.dumps(case_payload, ensure_ascii=False),
            json.dumps(case_evidence, ensure_ascii=False),
            source_id, now, now,
        ))

        for column in columns:
            if column["role"] not in PROCEDURE_ROLES:
                continue
            text = cell_nets[column["id"]]
            if not text:
                continue
            proc_id = _procedure_id(row_id, column["id"])
            proc_payload = {
                "method_kind": column["role"],
                "name": f"{concept}·{column['name']}",
                "table_id": table_id, "row_id": row_id, "column_id": column["id"],
                "steps": textops.parse_steps(text),
                "text": text,
            }
            proc_evidence = [_evidence(
                source_id, table["title"], _element_id(row_id, column["id"]),
                "knowhow_cell", f"{concept} › {column['name']}", text,
            )]
            object_rows.append((
                proc_id, notebook_id, "procedure", "approved",
                json.dumps(proc_payload, ensure_ascii=False),
                json.dumps(proc_evidence, ensure_ascii=False),
                source_id, now, now,
            ))
            edge_type = _EDGE_BY_ROLE[column["role"]]
            relation_rows.append((
                _relation_id(row_case_id, edge_type, proc_id), notebook_id, source_id,
                row_case_id, proc_id, edge_type, "[]", now,
            ))

        for column in columns:
            if column["role"] != "tool":
                continue
            text = cell_nets[column["id"]]
            if not text:
                continue
            for name in textops.split_tools(text):
                norm_name = name.strip().casefold()
                t_id = _tool_id(table_id, norm_name)
                tool_payload = {"name": name, "table_id": table_id}
                tool_evidence = [_evidence(
                    source_id, table["title"], _element_id(row_id, column["id"]),
                    "knowhow_cell", f"{concept} › {column['name']}", name,
                )]
                self.knowledge.insert_object_if_missing(db, (
                    t_id, notebook_id, "tool", "approved",
                    json.dumps(tool_payload, ensure_ascii=False),
                    json.dumps(tool_evidence, ensure_ascii=False),
                    source_id, now, now,
                ))
                relation_rows.append((
                    _relation_id(row_case_id, "requires_tool", t_id), notebook_id, source_id,
                    row_case_id, t_id, "requires_tool", "[]", now,
                ))

        self.knowledge.insert_object_chunk(db, object_rows)
        self.knowledge.insert_relation_chunk(db, relation_rows)

    # -------------------------------------------------- full-table rebuild
    def project_table(self, table_id: str) -> None:
        """Full-rebuild escape hatch (task brief: "全量重建=逃生口，顺带清孤儿
        tool KO"): wipe every object/relation/chunk/element this table's
        hidden source has ever produced — including tool objects no row
        references anymore — then reproject every current row from scratch.
        Deliberately more expensive than project_row's incremental per-cell
        diff (every chunk gets rebuilt+re-embedded); this is a user-triggered
        "when in doubt, reset" button (design doc: role/column changes are
        rare, and the table is capped at ~100 rows, so brute-force is cheap
        at this scale), not a per-edit hot path."""
        source_id = self.ensure_hidden_source(table_id)
        table = self.knowhow.get_knowhow_table(table_id)
        notebook_id = table["notebook_id"]
        now = self.now()
        with self.database.write() as db:
            self.knowledge.delete_relations_by_source(db, source_id)
            self.knowledge.delete_objects_by_source(db, source_id)
            self.sources.replace_elements(db, source_id, [], created_at=now)
        self.chunks.replace_source_chunks(source_id, notebook_id, [], created_at=now)
        for row in table["rows"]:
            self.project_row(table_id, row["id"])
        self.invalidate_unified_cache(notebook_id)
        self.mark_unified_dirty(notebook_id)

    # ------------------------------------------------------------- delete
    def delete_table_projection(self, hidden_source_id: "str | None") -> None:
        """Cleanup companion to KnowhowStore.delete_knowhow_table: that call
        cascades away the table/columns/rows/cells and hands back the
        (possibly None) hidden_source_id; this removes everything the
        projector ever derived from it — relations, objects (case/procedure/
        tool), chunks(+FTS), elements, and the hidden source row itself.
        Idempotent no-op for a falsy or already-gone id (this codebase's
        zero-row UPDATE/DELETE convention — see KnowhowStore's docstring).

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
            self.knowledge.delete_relations_by_source(db, hidden_source_id)
            self.knowledge.delete_objects_by_source(db, hidden_source_id)
            self.sources.replace_elements(db, hidden_source_id, [], created_at=now)
            self.sources.delete_source_row(db, hidden_source_id)
        self.invalidate_unified_cache(notebook_id)
        self.mark_unified_dirty(notebook_id)


__all__ = ["KnowhowProjector"]
