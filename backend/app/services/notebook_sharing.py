from __future__ import annotations

import json
import re
import secrets
import shutil
from pathlib import Path
from typing import Callable, TYPE_CHECKING

from app.models.schemas import NotebookSummary
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.sharing_store import SharingStore
from app.services.knowhow.assets import ALLOWED_MIME_EXTENSIONS
from app.services.knowhow.projection import cell_chunk_id, element_id
from app.services.notebook_catalog import NotebookCatalogService, NotebookSummaryQuery

if TYPE_CHECKING:  # runtime import would be circular (runtime constructs us)
    from app.services.repository_runtime import RepositoryCompatibilitySeams


# PR-2+3 Task 13: a knowhow cell's content_md embeds pasted images as
# ``![alt](asset://<asset_id>)`` (design doc §①) — a deep copy mints a fresh
# asset id per copied ``notebook_assets`` row (see copy_notebook), so every
# cell that references one must have its markdown rewritten to point at the
# NEW id instead of the source's. Asset ids are ``new_id("asset")`` = a
# lowercase prefix + a dash + a 32-hex uuid4 (sqlite_repository._new_id) —
# ``[\w-]+`` safely captures the whole thing up to the closing ``)`` the
# markdown image syntax always supplies, with no dependency on that exact
# format (a wider match just means "not in asset_map -> left unchanged",
# never a wrong rewrite).
_ASSET_REF_RE = re.compile(r"asset://([\w-]+)")


def _rewrite_asset_refs(text: str, asset_map: dict) -> str:
    """Rewrite every ``asset://<old_id>`` in ``text`` to ``asset://<new_id>``
    via ``asset_map`` (old id -> new id); an id with no entry (should not
    happen — every asset referenced by a copied cell was itself copied, see
    copy_notebook's ordering) is left exactly as-is rather than dropped."""
    if "asset://" not in text:
        return text
    return _ASSET_REF_RE.sub(
        lambda m: f"asset://{asset_map.get(m.group(1), m.group(1))}", text
    )


class NotebookCopyService:
    """Deep-copy orchestration: ID remapping, chunked transactions through the
    store, filesystem copy and compensation ordering.

    Compatibility seams are read during EVERY operation — ``seams.new_id()``
    (sqlite_repository._new_id), ``seams.copy_chunk_size()``
    (sqlite_repository._COPY_CHUNK) and ``seams.remap_json_ids`` — so patches
    applied after repository construction stay authoritative, and the per-row
    insert seat (facade ``_insert_row``) is honoured inside the store.

    ``schedule_projection`` (PR-2+3 Task 13) is the facade's
    ``knowhow_api.get_scheduler(repo).schedule`` callable — copy_notebook
    calls it once per copied knowhow table, right after publish, so the
    structural KO/edge graph gets rebuilt (dynamic column-name types) without
    the caller ever blocking on it (same debounced background scheduler every
    editing endpoint already goes through — see ``ProjectionScheduler``).
    """

    def __init__(
        self,
        *,
        store: SharingStore,
        catalog: NotebookCatalogService,
        seams: "RepositoryCompatibilitySeams",
        storage_dir: Callable[[], Path],
        schedule_projection: Callable[[str], None],
    ) -> None:
        self._store = store
        self._catalog = catalog
        self._seams = seams
        self._storage_dir = storage_dir
        self._schedule_projection = schedule_projection

    def sweep_stuck_copies(self, created_by: "str | None" = None) -> int:
        return self._store.sweep_stale_copies(created_by=created_by)

    def copy_notebook(
        self,
        source_notebook_id: str,
        *,
        new_owner_id: str,
        new_name: "str | None" = None,
    ) -> NotebookSummary:
        """Deep-copy a notebook with remapped IDs and a hidden copy sentinel.

        Phase contract (frozen): sweep only stale own copies → copy source
        directory → insert copying sentinel → copy each table in configured
        chunks → validate counts and references → publish original status.
        Failure compensates ONLY the destination rows/files.
        """
        self._store.sweep_stale_copies(created_by=new_owner_id)
        source_notebook = self._catalog.get_notebook(source_notebook_id)
        new_id = self._seams.new_id("nb")
        now = self._seams.now()
        name = new_name or f"{source_notebook.name} (副本)"
        chunk_size = self._seams.copy_chunk_size()

        def remapped_id(old: str) -> str:
            prefix = old.split("-", 1)[0] if old else "id"
            return self._seams.new_id(prefix)

        source_dir = self._storage_dir() / "notebooks" / source_notebook_id
        destination_dir = self._storage_dir() / "notebooks" / new_id
        # PR-2+3 Task 13: notebook_assets' on-disk files live in a SEPARATE
        # tree from source_dir (storage_dir/assets/<nb>/, not
        # storage_dir/notebooks/<nb>/ — see app.services.knowhow.assets.
        # AssetService.path_for), so they need their own copied-flag +
        # destination path for the same "compensate only what we actually
        # wrote" discipline as destination_dir below.
        assets_dest_dir = self._storage_dir() / "assets" / new_id
        copied_files = False
        assets_copied = False
        try:
            if source_dir.exists():
                shutil.copytree(source_dir, destination_dir)
                copied_files = True

            snapshot = self._store.snapshot_copy_rows(source_notebook_id)

            notebook_row = snapshot["notebooks"][0]
            notebook_row.update(
                id=new_id,
                name=name,
                created_by=new_owner_id,
                tier="personal",
                is_shared=0,
                share_token=None,
                status="copying",
                created_at=now,
                updated_at=now,
            )
            self._store.insert_copy_rows(
                "notebooks", [notebook_row], chunk_size=chunk_size
            )

            source_map: dict = {}
            element_map: dict = {}
            chunk_map: dict = {}
            object_map: dict = {}
            relation_map: dict = {}
            # PR-2+3 Task 13: knowhow business-table + hidden-source-content
            # remap maps. ``knowhow_source_ids_old`` is captured from the
            # RAW (not-yet-mutated) snapshot, before anything below rewrites
            # snapshot["knowhow_tables"]' own dicts in place — it is how the
            # elements/chunks loops further down tell a knowhow row apart
            # from an ordinary document row (both share the same snapshot
            # queries now that neither is excluded).
            khtbl_map: dict = {}
            khcol_map: dict = {}
            khrow_map: dict = {}
            khcel_map: dict = {}
            khcode_map: dict = {}
            asset_map: dict = {}
            knowhow_source_ids_old = {
                t["hidden_source_id"]
                for t in snapshot["knowhow_tables"]
                if t.get("hidden_source_id")
            }
            # old element id -> NEW row id, populated by the elements_out
            # loop below; the chunks_out loop reads it to recompute each
            # knowhow chunk's id from ITS row (a chunk has no row_id column
            # of its own — only its element_ids[0] indirectly names one).
            element_row_new: dict = {}

            sources_out = []
            for data in snapshot["sources"]:
                data["id"] = source_map.setdefault(data["id"], remapped_id(data["id"]))
                data["notebook_id"] = new_id
                if data.get("file_path"):
                    data["file_path"] = str(destination_dir / Path(data["file_path"]).name)
                # Task 6: memory_id is NOT an id that gets remapped — it points
                # at a Memory row that is owner-private and never travels with
                # a copy. Force it empty (insert_source's own "no link"
                # default) so the copy doesn't dangle-reference a Memory the
                # recipient can't see, and doesn't collide with the global
                # partial unique index idx_sources_memory_id (which the
                # source row's own unchanged memory_id still occupies).
                data["memory_id"] = ""
                sources_out.append(data)
            self._store.insert_copy_rows("sources", sources_out, chunk_size=chunk_size)

            paper_meta_out = []
            for data in snapshot["source_paper_meta"]:
                data["source_id"] = source_map[data["source_id"]]
                data["notebook_id"] = new_id
                paper_meta_out.append(data)
            self._store.insert_copy_rows(
                "source_paper_meta", paper_meta_out, chunk_size=chunk_size
            )

            authors_out = []
            for data in snapshot["source_authors"]:
                new_source_id = source_map[data["source_id"]]
                # Deterministic id scheme mirrors SourceStore.upsert_paper_meta's
                # write-time convention exactly, so a copy's author rows are
                # indistinguishable from freshly-extracted ones.
                data["id"] = f"{new_source_id}:auth:{int(data['position']):03d}"
                data["source_id"] = new_source_id
                data["notebook_id"] = new_id
                authors_out.append(data)
            self._store.insert_copy_rows(
                "source_authors", authors_out, chunk_size=chunk_size
            )

            # --- PR-2+3 Task 13: knowhow business tables ------------------
            # Order is FK-safe (each leg's remap map is fully populated
            # before the next leg that depends on it runs): tables (needs
            # source_map for hidden_source_id) -> columns/rows (need
            # khtbl_map) -> assets (independent; builds asset_map) -> cells
            # (needs khrow_map + khcol_map + asset_map, for the content_md
            # asset:// rewrite) -> cell_code (needs khrow_map + khcol_map).
            tables_out = []
            for data in snapshot["knowhow_tables"]:
                data["id"] = khtbl_map.setdefault(data["id"], remapped_id(data["id"]))
                data["notebook_id"] = new_id
                old_hidden = data.get("hidden_source_id")
                data["hidden_source_id"] = (
                    source_map.get(old_hidden, old_hidden) if old_hidden else old_hidden
                )
                tables_out.append(data)
            self._store.insert_copy_rows("knowhow_tables", tables_out, chunk_size=chunk_size)

            columns_out = []
            for data in snapshot["knowhow_columns"]:
                data["id"] = khcol_map.setdefault(data["id"], remapped_id(data["id"]))
                data["table_id"] = khtbl_map[data["table_id"]]
                columns_out.append(data)
            self._store.insert_copy_rows("knowhow_columns", columns_out, chunk_size=chunk_size)

            rows_out = []
            for data in snapshot["knowhow_rows"]:
                data["id"] = khrow_map.setdefault(data["id"], remapped_id(data["id"]))
                data["table_id"] = khtbl_map[data["table_id"]]
                # The copy has not been (re)projected yet regardless of what
                # the SOURCE row's own status was — 'pending' is the schema
                # default for exactly this "not yet projected" state (see
                # migrations.py _migration_16), and the scheduling loop at
                # the end of this method is what will settle it.
                data["projection_status"] = "pending"
                rows_out.append(data)
            self._store.insert_copy_rows("knowhow_rows", rows_out, chunk_size=chunk_size)

            assets_out = []
            asset_files: list[tuple[str, str, str]] = []  # (old_id, new_id, mime)
            for data in snapshot["notebook_assets"]:
                old_asset_id = data["id"]
                new_asset_id = asset_map.setdefault(old_asset_id, remapped_id(old_asset_id))
                data["id"] = new_asset_id
                data["notebook_id"] = new_id
                asset_files.append((old_asset_id, new_asset_id, data["mime"]))
                assets_out.append(data)
            self._store.insert_copy_rows("notebook_assets", assets_out, chunk_size=chunk_size)

            if asset_files:
                assets_src_dir = self._storage_dir() / "assets" / source_notebook_id
                assets_dest_dir.mkdir(parents=True, exist_ok=True)
                assets_copied = True
                for old_asset_id, new_asset_id, mime in asset_files:
                    ext = ALLOWED_MIME_EXTENSIONS.get(mime, "bin")
                    src_path = assets_src_dir / f"{old_asset_id}.{ext}"
                    if src_path.is_file():
                        shutil.copy2(src_path, assets_dest_dir / f"{new_asset_id}.{ext}")

            cells_out = []
            for data in snapshot["knowhow_cells"]:
                data["id"] = khcel_map.setdefault(data["id"], remapped_id(data["id"]))
                data["row_id"] = khrow_map[data["row_id"]]
                data["column_id"] = khcol_map[data["column_id"]]
                data["content_md"] = _rewrite_asset_refs(
                    data.get("content_md") or "", asset_map
                )
                cells_out.append(data)
            self._store.insert_copy_rows("knowhow_cells", cells_out, chunk_size=chunk_size)

            cell_code_out = []
            for data in snapshot["knowhow_cell_code"]:
                data["id"] = khcode_map.setdefault(data["id"], remapped_id(data["id"]))
                data["row_id"] = khrow_map[data["row_id"]]
                data["column_id"] = khcol_map[data["column_id"]]
                cell_code_out.append(data)
            self._store.insert_copy_rows(
                "knowhow_cell_code", cell_code_out, chunk_size=chunk_size
            )
            # --- end knowhow business tables -------------------------------

            elements_out = []
            for data in snapshot["source_elements"]:
                old_element_id = data["id"]
                if data["source_id"] in knowhow_source_ids_old:
                    # A knowhow cell's element: recompute its id via the
                    # SAME stable formula project_table itself uses
                    # (app.services.knowhow.projection.element_id), keyed on
                    # the ALREADY-remapped row/column ids, instead of an
                    # arbitrary fresh id — this is what lets the post-copy
                    # reprojection find this exact row already in place.
                    metadata = json.loads(data.get("metadata") or "{}")
                    kh_meta = dict(metadata.get("knowhow") or {})
                    new_row_id = khrow_map[kh_meta["row_id"]]
                    new_column_id = khcol_map[kh_meta["column_id"]]
                    new_element_id = element_id(new_row_id, new_column_id)
                    element_map[old_element_id] = new_element_id
                    element_row_new[old_element_id] = new_row_id
                    kh_meta["table_id"] = khtbl_map.get(
                        kh_meta.get("table_id"), kh_meta.get("table_id")
                    )
                    kh_meta["row_id"] = new_row_id
                    kh_meta["column_id"] = new_column_id
                    metadata["knowhow"] = kh_meta
                    data["metadata"] = json.dumps(metadata, ensure_ascii=False)
                    data["id"] = new_element_id
                else:
                    data["id"] = element_map.setdefault(
                        old_element_id, remapped_id(old_element_id)
                    )
                data["source_id"] = source_map[data["source_id"]]
                elements_out.append(data)
            self._store.insert_copy_rows(
                "source_elements", elements_out, chunk_size=chunk_size
            )

            json_maps = {
                "element_id": element_map,
                "element_ids": element_map,
                "source_id": source_map,
                "object_id": object_map,
            }

            chunks_out = []
            for data in snapshot["chunks"]:
                old_chunk_id = data["id"]
                if data["source_id"] in knowhow_source_ids_old:
                    # Recompute the SAME stable chunk id project_table would
                    # (app.services.knowhow.projection.cell_chunk_id): the
                    # trailing "part" number (column-position-derived) is
                    # unchanged by a copy (column position is copied as-is),
                    # so it is read straight off the OLD id; only the
                    # row-hash segment (a pure function of row_id) needs
                    # recomputing for the remapped row. This is the crux of
                    # the zero-re-embed contract — landing on this exact id
                    # with unchanged text+section_path is what makes the
                    # post-copy project_table pass see "nothing changed".
                    old_element_ids = json.loads(data.get("element_ids") or "[]")
                    new_row_id = element_row_new[old_element_ids[0]]
                    part = int(old_chunk_id.rsplit("-", 1)[-1])
                    new_chunk_id = cell_chunk_id(new_row_id, part)
                    chunk_map[old_chunk_id] = new_chunk_id
                    data["id"] = new_chunk_id
                else:
                    data["id"] = chunk_map.setdefault(
                        old_chunk_id, remapped_id(old_chunk_id)
                    )
                data["notebook_id"] = new_id
                data["source_id"] = source_map[data["source_id"]]
                element_ids = {"element_ids": json.loads(data.get("element_ids") or "[]")}
                data["element_ids"] = json.dumps(
                    self._seams.remap_json_ids(element_ids, json_maps)["element_ids"]
                )
                chunks_out.append(data)
            self._store.insert_copy_rows("chunks", chunks_out, chunk_size=chunk_size)

            objects_out = []
            for data in snapshot["knowledge_objects"]:
                old_source_id = data["source_id"]
                old_candidate_id = data.get("source_candidate_id")
                data["id"] = object_map.setdefault(data["id"], remapped_id(data["id"]))
                data["notebook_id"] = new_id
                data["source_id"] = source_map.get(old_source_id, old_source_id)
                if old_candidate_id:
                    data["source_candidate_id"] = source_map.get(
                        old_candidate_id, old_candidate_id
                    )
                data["payload"] = json.dumps(
                    self._seams.remap_json_ids(json.loads(data.get("payload") or "{}"), json_maps)
                )
                data["evidence"] = json.dumps(
                    self._seams.remap_json_ids(json.loads(data.get("evidence") or "[]"), json_maps)
                )
                objects_out.append(data)
            self._store.insert_copy_rows(
                "knowledge_objects", objects_out, chunk_size=chunk_size
            )

            relations_out = []
            for data in snapshot["knowledge_relations"]:
                old_source_id = data["source_id"]
                data["id"] = relation_map.setdefault(data["id"], remapped_id(data["id"]))
                data["notebook_id"] = new_id
                data["source_id"] = source_map.get(old_source_id, old_source_id)
                data["source_object_id"] = object_map[data["source_object_id"]]
                data["target_object_id"] = object_map[data["target_object_id"]]
                data["evidence"] = json.dumps(
                    self._seams.remap_json_ids(json.loads(data.get("evidence") or "[]"), json_maps)
                )
                relations_out.append(data)
            self._store.insert_copy_rows(
                "knowledge_relations", relations_out, chunk_size=chunk_size
            )

            chunk_embeddings_out = []
            for data in snapshot["chunk_embeddings"]:
                data["chunk_id"] = chunk_map[data["chunk_id"]]
                data["notebook_id"] = new_id
                chunk_embeddings_out.append(data)
            self._store.insert_copy_rows(
                "chunk_embeddings", chunk_embeddings_out, chunk_size=chunk_size
            )

            element_embeddings_out = []
            for data in snapshot["element_embeddings"]:
                data["element_id"] = element_map[data["element_id"]]
                data["source_id"] = source_map[data["source_id"]]
                data["notebook_id"] = new_id
                element_embeddings_out.append(data)
            self._store.insert_copy_rows(
                "element_embeddings", element_embeddings_out, chunk_size=chunk_size
            )

            knowledge_embeddings_out = []
            for data in snapshot["knowledge_embeddings"]:
                data["object_id"] = object_map[data["object_id"]]
                data["notebook_id"] = new_id
                knowledge_embeddings_out.append(data)
            self._store.insert_copy_rows(
                "knowledge_embeddings", knowledge_embeddings_out, chunk_size=chunk_size
            )

            relation_embeddings_out = []
            for data in snapshot["relation_embeddings"]:
                data["relation_id"] = relation_map[data["relation_id"]]
                data["notebook_id"] = new_id
                relation_embeddings_out.append(data)
            self._store.insert_copy_rows(
                "relation_embeddings", relation_embeddings_out, chunk_size=chunk_size
            )

            clusters_out = []
            for data in snapshot["concept_clusters"]:
                data["id"] = remapped_id(data["id"])
                data["notebook_id"] = new_id
                data["canonical_id"] = object_map.get(data["canonical_id"], data["canonical_id"])
                data["member_object_id"] = object_map.get(
                    data["member_object_id"], data["member_object_id"]
                )
                clusters_out.append(data)
            self._store.insert_copy_rows(
                "concept_clusters", clusters_out, chunk_size=chunk_size
            )

            kg_fts_rows = [
                (
                    data["id"],
                    new_id,
                    (json.loads(data["payload"]).get("name") or "").strip(),
                )
                for data in objects_out
                if data.get("status") != "deprecated"
            ]
            kg_fts_rows = [row for row in kg_fts_rows if row[2]]
            self._store.insert_fts_rows(
                "INSERT INTO kg_objects_fts(object_id, notebook_id, name) VALUES (?, ?, ?)",
                kg_fts_rows,
                chunk_size=chunk_size,
            )

            chunk_fts_rows = [
                (data["id"], new_id, data.get("text") or "") for data in chunks_out
            ]
            self._store.insert_fts_rows(
                "INSERT INTO chunks_fts(chunk_id, notebook_id, text) VALUES (?, ?, ?)",
                chunk_fts_rows,
                chunk_size=chunk_size,
            )

            self._store.validate_copy(source_notebook_id, new_id)
            self._store.publish_copy(new_id, source_notebook.status)
            # PR-2+3 Task 13: schedule ONE structural reprojection per copied
            # knowhow table (khtbl_map.values() are all NEW table ids; empty
            # for a notebook with no knowhow tables, so this is a zero-cost
            # no-op for the overwhelmingly common case). The chunks/vectors
            # already sitting in the copy are byte-identical to what
            # project_table will independently recompute, so this rebuilds
            # the KO/edge graph (dynamic column-name types) without a single
            # additional embedder call — see element_id/cell_chunk_id above.
            for new_table_id in khtbl_map.values():
                self._schedule_projection(new_table_id)
            return self._catalog.get_notebook(new_id)
        except Exception:
            self._store.compensate_copy(new_id)
            if copied_files:
                shutil.rmtree(destination_dir, ignore_errors=True)
            if assets_copied:
                shutil.rmtree(assets_dest_dir, ignore_errors=True)
            raise


class NotebookSharingService:
    """Sharing, membership and read-access orchestration over SharingStore.

    ``copy_stats`` is a late-bound callback to the facade's
    ``notebook_copy_stats`` (the cross-domain scale-profile memo) so instance
    patches and live Settings mutations keep being observed.  ``database`` +
    ``summaries`` hydrate join_shared's NotebookSummary over one connection,
    exactly like the former mixin's ``_notebook_from_row`` path.
    """

    def __init__(
        self,
        *,
        store: SharingStore,
        copies: NotebookCopyService,
        catalog: NotebookCatalogService,
        summaries: NotebookSummaryQuery,
        database: SqliteDatabase,
        copy_stats: Callable[[str], dict],
    ) -> None:
        self._store = store
        self._copies = copies
        self._catalog = catalog
        self._summaries = summaries
        self._database = database
        self._copy_stats = copy_stats

    # ---------------------------------------------------------------- share
    def share_notebook(self, notebook_id: str) -> dict:
        self._catalog.get_notebook(notebook_id)  # raises KeyError if missing
        token = self._store.set_share_token(
            notebook_id, f"shr-{secrets.token_urlsafe(16)}"
        )
        stats = self.notebook_copy_stats(notebook_id)
        return {
            "share_token": token,
            "copyable": stats["copyable"],
            "size": stats["size"],
        }

    def unshare_notebook(self, notebook_id: str) -> None:
        self._catalog.get_notebook(notebook_id)  # raises KeyError if missing
        self._store.clear_share(notebook_id)

    def find_notebook_by_share_token(self, token: str) -> "str | None":
        if not token:
            return None
        return self._store.find_by_token(token)

    def notebook_copy_stats(self, notebook_id: str) -> dict:
        return self._copy_stats(notebook_id)

    def shared_preview(self, notebook_id: str) -> dict:
        notebook = self._catalog.get_notebook(notebook_id)
        stats = self.notebook_copy_stats(notebook_id)
        owner_display, titles = self._store.shared_preview_rows(notebook_id)
        return {
            "name": notebook.name,
            "owner_display": owner_display,
            "source_count": stats["size"]["sources"],
            "node_count": stats["size"]["nodes"],
            "edge_count": stats["size"]["edges"],
            "source_titles": titles,
            "mode": "copy" if stats["copyable"] else "readonly",
            "size": stats["size"],
        }

    def shared_by_me(self, user_id: str) -> list:
        out = []
        for row in self._store.list_shared_by_owner(user_id):
            stats = self.notebook_copy_stats(row["id"])
            readonly = not stats["copyable"]
            out.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "share_token": row["share_token"] or "",
                    "mode": "readonly" if readonly else "copy",
                    "size": stats["size"],
                    "members": self.list_members(row["id"]) if readonly else [],
                }
            )
        return out

    # ----------------------------------------------------------------- copy
    def copy_notebook(
        self,
        source_notebook_id: str,
        *,
        new_owner_id: str,
        new_name: "str | None" = None,
    ) -> NotebookSummary:
        return self._copies.copy_notebook(
            source_notebook_id, new_owner_id=new_owner_id, new_name=new_name
        )

    def sweep_stuck_copies(self, created_by: "str | None" = None) -> int:
        return self._copies.sweep_stuck_copies(created_by)

    # -------------------------------------------------------- access guards
    def user_can_access_notebook(self, notebook_id: str, user_id: str) -> bool:
        """写权:仅 owner(安全边界,勿放宽)。"""
        return self._store.user_can_access_notebook(notebook_id, user_id)

    def is_member(self, notebook_id: str, user_id: str) -> bool:
        return self._store.is_member(notebook_id, user_id)

    def user_can_read_notebook(self, notebook_id: str, user_id: str) -> bool:
        """读权:owner ∪ 只读成员。"""
        return self.user_can_access_notebook(notebook_id, user_id) or self.is_member(
            notebook_id, user_id
        )

    def user_can_read_source(self, source_id: str, user_id: str) -> bool:
        notebook_id = self._store.source_notebook_id(source_id)
        return bool(notebook_id) and self.user_can_read_notebook(notebook_id, user_id)

    def user_can_read_answer(self, answer_id: str, user_id: str) -> bool:
        notebook_id = self._store.answer_notebook_id(answer_id)
        return bool(notebook_id) and self.user_can_read_notebook(notebook_id, user_id)

    # ------------------------------------------------------------ membership
    def add_member(self, notebook_id: str, user_id: str) -> None:
        return self._store.add_member(notebook_id, user_id)

    def remove_member(self, notebook_id: str, user_id: str) -> None:
        return self._store.remove_member(notebook_id, user_id)

    def kick_all_members(self, notebook_id: str) -> None:
        return self._store.kick_all_members(notebook_id)

    def list_members(self, notebook_id: str) -> list:
        return self._store.list_members(notebook_id)

    def join_shared(self, notebook_id: str, user_id: str) -> NotebookSummary:
        self.add_member(notebook_id, user_id)
        with self._database.connect() as db:
            row = self._store.notebook_row_on(db, notebook_id)
            notebook = self._summaries.from_row(db, row)
        notebook.access = "reader"
        return notebook

    def leave_notebook(self, notebook_id: str, user_id: str) -> None:
        self.remove_member(notebook_id, user_id)

    # -------------------------------------------------------------- ownership
    def source_owner(self, source_id: str) -> "str | None":
        return self._store.source_owner(source_id)

    def conversation_owner(self, conversation_id: str) -> "str | None":
        return self._store.conversation_owner(conversation_id)

    def answer_owner(self, answer_id: str) -> "str | None":
        return self._store.answer_owner(answer_id)
