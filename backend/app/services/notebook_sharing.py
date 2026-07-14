from __future__ import annotations

import json
import secrets
import shutil
from pathlib import Path
from typing import Callable, TYPE_CHECKING

from app.models.schemas import NotebookSummary
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.sharing_store import SharingStore
from app.services.notebook_catalog import NotebookCatalogService, NotebookSummaryQuery

if TYPE_CHECKING:  # runtime import would be circular (runtime constructs us)
    from app.services.repository_runtime import RepositoryCompatibilitySeams


class NotebookCopyService:
    """Deep-copy orchestration: ID remapping, chunked transactions through the
    store, filesystem copy and compensation ordering.

    Compatibility seams are read during EVERY operation — ``seams.new_id()``
    (sqlite_repository._new_id), ``seams.copy_chunk_size()``
    (sqlite_repository._COPY_CHUNK) and ``seams.remap_json_ids`` — so patches
    applied after repository construction stay authoritative, and the per-row
    insert seat (facade ``_insert_row``) is honoured inside the store.
    """

    def __init__(
        self,
        *,
        store: SharingStore,
        catalog: NotebookCatalogService,
        seams: "RepositoryCompatibilitySeams",
        storage_dir: Callable[[], Path],
    ) -> None:
        self._store = store
        self._catalog = catalog
        self._seams = seams
        self._storage_dir = storage_dir

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
        copied_files = False
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

            elements_out = []
            for data in snapshot["source_elements"]:
                data["id"] = element_map.setdefault(data["id"], remapped_id(data["id"]))
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
                data["id"] = chunk_map.setdefault(data["id"], remapped_id(data["id"]))
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
            return self._catalog.get_notebook(new_id)
        except Exception:
            self._store.compensate_copy(new_id)
            if copied_files:
                shutil.rmtree(destination_dir, ignore_errors=True)
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
