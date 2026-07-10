from __future__ import annotations

import json
import secrets
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

from app.models.schemas import NotebookSummary


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _repository_new_id(prefix: str) -> str:
    # Keep one ID policy and preserve sqlite_repository._new_id monkeypatches.
    from app.services import sqlite_repository

    return sqlite_repository._new_id(prefix)


def _copy_chunk_size() -> int:
    # _COPY_CHUNK has long been a test/perf tuning seam on sqlite_repository.
    # Resolve it at call time so the facade's backwards-compatible export
    # remains authoritative after this domain extraction.
    from app.services import sqlite_repository

    return int(sqlite_repository._COPY_CHUNK)


def _remap_json_ids(value, maps: dict):
    """Recursively rewrite copied source/element/object references in JSON."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in ("element_id", "source_id", "object_id") and isinstance(item, str):
                out[key] = maps.get(key, {}).get(item, item)
            elif key == "element_ids" and isinstance(item, list):
                mapping = maps.get("element_ids", {})
                out[key] = [
                    mapping.get(child, child)
                    if isinstance(child, str)
                    else _remap_json_ids(child, maps)
                    for child in item
                ]
            else:
                out[key] = _remap_json_ids(item, maps)
        return out
    if isinstance(value, list):
        return [_remap_json_ids(item, maps) for item in value]
    return value


class SQLiteNotebookSharingMixin:
    """Sharing, deep-copy, membership, and read-access SQLite domain.

    The host remains the public ``SQLiteRepository`` facade and supplies its
    connection/write boundaries, notebook hydration, storage path, settings,
    and versioned vector cache.  No endpoint or schema contract changes.
    """

    def share_notebook(self, notebook_id: str) -> dict:
        self.get_notebook(notebook_id)
        with self._write() as db:
            row = db.execute(
                "SELECT is_shared, share_token FROM notebooks WHERE id = ?", (notebook_id,)
            ).fetchone()
            token = (
                row["share_token"]
                if row["is_shared"] and row["share_token"]
                else f"shr-{secrets.token_urlsafe(16)}"
            )
            db.execute(
                "UPDATE notebooks SET is_shared = 1, share_token = ?, updated_at = ? WHERE id = ?",
                (token, _now(), notebook_id),
            )
        stats = self.notebook_copy_stats(notebook_id)
        return {"share_token": token, "copyable": stats["copyable"], "size": stats["size"]}

    def unshare_notebook(self, notebook_id: str) -> None:
        self.get_notebook(notebook_id)
        with self._write() as db:
            db.execute(
                "UPDATE notebooks SET is_shared = 0, share_token = NULL, updated_at = ? WHERE id = ?",
                (_now(), notebook_id),
            )
            db.execute("DELETE FROM notebook_members WHERE notebook_id = ?", (notebook_id,))

    def find_notebook_by_share_token(self, token: str) -> "str | None":
        if not token:
            return None
        with self._connect() as db:
            row = db.execute(
                "SELECT id FROM notebooks WHERE share_token = ? AND is_shared = 1", (token,)
            ).fetchone()
        return row["id"] if row else None

    def notebook_copy_stats(self, notebook_id: str) -> dict:
                return __import__("app.services.notebook_scale", fromlist=["NotebookScaleProfile"]).NotebookScaleProfile(self.settings, self, lambda nb: tuple(self._scale_index_version(nb)), self._vector_cache).copy_stats(notebook_id)

    def shared_preview(self, notebook_id: str) -> dict:
        notebook = self.get_notebook(notebook_id)
        stats = self.notebook_copy_stats(notebook_id)
        with self._connect() as db:
            owner = db.execute(
                "SELECT u.username FROM notebooks nb LEFT JOIN users u ON u.id = nb.created_by "
                "WHERE nb.id = ?",
                (notebook_id,),
            ).fetchone()
            titles = [
                row["title"]
                for row in db.execute(
                    "SELECT title FROM sources WHERE notebook_id = ? ORDER BY created_at LIMIT 50",
                    (notebook_id,),
                ).fetchall()
            ]
        return {
            "name": notebook.name,
            "owner_display": owner["username"] if owner and owner["username"] else "",
            "source_count": stats["size"]["sources"],
            "node_count": stats["size"]["nodes"],
            "edge_count": stats["size"]["edges"],
            "source_titles": titles,
            "mode": "copy" if stats["copyable"] else "readonly",
            "size": stats["size"],
        }

    def shared_by_me(self, user_id: str) -> list:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, name, share_token FROM notebooks "
                "WHERE created_by = ? AND is_shared = 1 ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()
        out = []
        for row in rows:
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

    @staticmethod
    def _insert_row(db, table: str, data: dict) -> None:
        columns = list(data.keys())
        db.execute(
            f"INSERT INTO {table} ({','.join(columns)}) "
            f"VALUES ({','.join('?' * len(columns))})",
            [data[column] for column in columns],
        )

    def _sweep_stuck_copies(self, created_by: "str | None" = None) -> int:
        """Delete expired copy sentinels without touching concurrent copies."""
        cutoff = (
            datetime.now()
            - timedelta(seconds=max(1, self.settings.notebook_copy_stale_seconds))
        ).replace(microsecond=0).isoformat()
        with self._write() as db:
            if created_by is not None:
                rows = db.execute(
                    "SELECT id FROM notebooks WHERE status = 'copying' AND created_by = ? "
                    "AND created_at < ?",
                    (created_by, cutoff),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT id FROM notebooks WHERE status = 'copying' AND created_at < ?",
                    (cutoff,),
                ).fetchall()
            stuck_ids = [row["id"] for row in rows]
            if not stuck_ids:
                return 0
            placeholders = ",".join("?" for _ in stuck_ids)
            db.execute(
                f"DELETE FROM kg_objects_fts WHERE notebook_id IN ({placeholders})", stuck_ids
            )
            db.execute(
                f"DELETE FROM chunks_fts WHERE notebook_id IN ({placeholders})", stuck_ids
            )
            db.execute(
                f"DELETE FROM knowledge_embeddings WHERE notebook_id IN ({placeholders})",
                stuck_ids,
            )
            db.execute(f"DELETE FROM notebooks WHERE id IN ({placeholders})", stuck_ids)
        return len(stuck_ids)

    def copy_notebook(
        self,
        source_notebook_id: str,
        *,
        new_owner_id: str,
        new_name: "str | None" = None,
    ) -> NotebookSummary:
        """Deep-copy a notebook with remapped IDs and a hidden copy sentinel."""
        self._sweep_stuck_copies(created_by=new_owner_id)
        source_notebook = self.get_notebook(source_notebook_id)
        new_id = _repository_new_id("nb")
        now = _now()
        name = new_name or f"{source_notebook.name} (副本)"
        chunk_size = _copy_chunk_size()

        def remapped_id(old: str) -> str:
            prefix = old.split("-", 1)[0] if old else "id"
            return _repository_new_id(prefix)

        def chunked_insert(table: str, rows: List[dict]) -> None:
            for index in range(0, len(rows), chunk_size):
                with self._write() as db:
                    for data in rows[index:index + chunk_size]:
                        self._insert_row(db, table, data)

        source_dir = self.storage_dir / "notebooks" / source_notebook_id
        destination_dir = self.storage_dir / "notebooks" / new_id
        copied_files = False
        try:
            if source_dir.exists():
                shutil.copytree(source_dir, destination_dir)
                copied_files = True

            with self._connect() as db:
                notebook_row = dict(
                    db.execute(
                        "SELECT * FROM notebooks WHERE id = ?", (source_notebook_id,)
                    ).fetchone()
                )
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
            with self._write() as db:
                self._insert_row(db, "notebooks", notebook_row)

            source_map: dict = {}
            element_map: dict = {}
            chunk_map: dict = {}
            object_map: dict = {}
            relation_map: dict = {}

            with self._connect() as db:
                source_rows = db.execute(
                    "SELECT * FROM sources WHERE notebook_id = ?", (source_notebook_id,)
                ).fetchall()
            sources_out = []
            for row in source_rows:
                data = dict(row)
                data["id"] = source_map.setdefault(row["id"], remapped_id(row["id"]))
                data["notebook_id"] = new_id
                if data.get("file_path"):
                    data["file_path"] = str(destination_dir / Path(data["file_path"]).name)
                sources_out.append(data)
            chunked_insert("sources", sources_out)

            with self._connect() as db:
                element_rows = db.execute(
                    "SELECT se.* FROM source_elements se JOIN sources s ON s.id = se.source_id "
                    "WHERE s.notebook_id = ?",
                    (source_notebook_id,),
                ).fetchall()
            elements_out = []
            for row in element_rows:
                data = dict(row)
                data["id"] = element_map.setdefault(row["id"], remapped_id(row["id"]))
                data["source_id"] = source_map[row["source_id"]]
                elements_out.append(data)
            chunked_insert("source_elements", elements_out)

            json_maps = {
                "element_id": element_map,
                "element_ids": element_map,
                "source_id": source_map,
                "object_id": object_map,
            }

            with self._connect() as db:
                chunk_rows = db.execute(
                    "SELECT * FROM chunks WHERE notebook_id = ?", (source_notebook_id,)
                ).fetchall()
            chunks_out = []
            for row in chunk_rows:
                data = dict(row)
                data["id"] = chunk_map.setdefault(row["id"], remapped_id(row["id"]))
                data["notebook_id"] = new_id
                data["source_id"] = source_map[row["source_id"]]
                element_ids = {"element_ids": json.loads(data.get("element_ids") or "[]")}
                data["element_ids"] = json.dumps(
                    _remap_json_ids(element_ids, json_maps)["element_ids"]
                )
                chunks_out.append(data)
            chunked_insert("chunks", chunks_out)

            with self._connect() as db:
                object_rows = db.execute(
                    "SELECT * FROM knowledge_objects WHERE notebook_id = ?",
                    (source_notebook_id,),
                ).fetchall()
            objects_out = []
            for row in object_rows:
                data = dict(row)
                data["id"] = object_map.setdefault(row["id"], remapped_id(row["id"]))
                data["notebook_id"] = new_id
                data["source_id"] = source_map.get(row["source_id"], row["source_id"])
                if data.get("source_candidate_id"):
                    data["source_candidate_id"] = source_map.get(
                        row["source_candidate_id"], row["source_candidate_id"]
                    )
                data["payload"] = json.dumps(
                    _remap_json_ids(json.loads(data.get("payload") or "{}"), json_maps)
                )
                data["evidence"] = json.dumps(
                    _remap_json_ids(json.loads(data.get("evidence") or "[]"), json_maps)
                )
                objects_out.append(data)
            chunked_insert("knowledge_objects", objects_out)

            with self._connect() as db:
                relation_rows = db.execute(
                    "SELECT * FROM knowledge_relations WHERE notebook_id = ?",
                    (source_notebook_id,),
                ).fetchall()
            relations_out = []
            for row in relation_rows:
                data = dict(row)
                data["id"] = relation_map.setdefault(row["id"], remapped_id(row["id"]))
                data["notebook_id"] = new_id
                data["source_id"] = source_map.get(row["source_id"], row["source_id"])
                data["source_object_id"] = object_map[row["source_object_id"]]
                data["target_object_id"] = object_map[row["target_object_id"]]
                data["evidence"] = json.dumps(
                    _remap_json_ids(json.loads(data.get("evidence") or "[]"), json_maps)
                )
                relations_out.append(data)
            chunked_insert("knowledge_relations", relations_out)

            with self._connect() as db:
                chunk_embedding_rows = db.execute(
                    "SELECT * FROM chunk_embeddings WHERE notebook_id = ?",
                    (source_notebook_id,),
                ).fetchall()
            chunk_embeddings_out = []
            for row in chunk_embedding_rows:
                data = dict(row)
                data["chunk_id"] = chunk_map[row["chunk_id"]]
                data["notebook_id"] = new_id
                chunk_embeddings_out.append(data)
            chunked_insert("chunk_embeddings", chunk_embeddings_out)

            with self._connect() as db:
                element_embedding_rows = db.execute(
                    "SELECT ee.* FROM element_embeddings ee JOIN sources s ON s.id = ee.source_id "
                    "WHERE s.notebook_id = ?",
                    (source_notebook_id,),
                ).fetchall()
            element_embeddings_out = []
            for row in element_embedding_rows:
                data = dict(row)
                data["element_id"] = element_map[row["element_id"]]
                data["source_id"] = source_map[row["source_id"]]
                data["notebook_id"] = new_id
                element_embeddings_out.append(data)
            chunked_insert("element_embeddings", element_embeddings_out)

            with self._connect() as db:
                knowledge_embedding_rows = db.execute(
                    "SELECT * FROM knowledge_embeddings WHERE notebook_id = ?",
                    (source_notebook_id,),
                ).fetchall()
            knowledge_embeddings_out = []
            for row in knowledge_embedding_rows:
                data = dict(row)
                data["object_id"] = object_map[row["object_id"]]
                data["notebook_id"] = new_id
                knowledge_embeddings_out.append(data)
            chunked_insert("knowledge_embeddings", knowledge_embeddings_out)

            with self._connect() as db:
                relation_embedding_rows = db.execute(
                    "SELECT * FROM relation_embeddings WHERE notebook_id = ?",
                    (source_notebook_id,),
                ).fetchall()
            relation_embeddings_out = []
            for row in relation_embedding_rows:
                data = dict(row)
                data["relation_id"] = relation_map[row["relation_id"]]
                data["notebook_id"] = new_id
                relation_embeddings_out.append(data)
            chunked_insert("relation_embeddings", relation_embeddings_out)

            with self._connect() as db:
                cluster_rows = db.execute(
                    "SELECT * FROM concept_clusters WHERE notebook_id = ?",
                    (source_notebook_id,),
                ).fetchall()
            clusters_out = []
            for row in cluster_rows:
                data = dict(row)
                data["id"] = remapped_id(row["id"])
                data["notebook_id"] = new_id
                data["canonical_id"] = object_map.get(row["canonical_id"], row["canonical_id"])
                data["member_object_id"] = object_map.get(
                    row["member_object_id"], row["member_object_id"]
                )
                clusters_out.append(data)
            chunked_insert("concept_clusters", clusters_out)

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
            for index in range(0, len(kg_fts_rows), chunk_size):
                with self._write() as db:
                    db.executemany(
                        "INSERT INTO kg_objects_fts(object_id, notebook_id, name) VALUES (?, ?, ?)",
                        kg_fts_rows[index:index + chunk_size],
                    )

            chunk_fts_rows = [
                (data["id"], new_id, data.get("text") or "") for data in chunks_out
            ]
            for index in range(0, len(chunk_fts_rows), chunk_size):
                with self._write() as db:
                    db.executemany(
                        "INSERT INTO chunks_fts(chunk_id, notebook_id, text) VALUES (?, ?, ?)",
                        chunk_fts_rows[index:index + chunk_size],
                    )

            with self._connect() as db:
                for table in (
                    "sources",
                    "chunks",
                    "knowledge_objects",
                    "knowledge_relations",
                    "concept_clusters",
                ):
                    copied_count = db.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE notebook_id = ?", (new_id,)
                    ).fetchone()[0]
                    source_count = db.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE notebook_id = ?",
                        (source_notebook_id,),
                    ).fetchone()[0]
                    if copied_count != source_count:
                        raise RuntimeError(
                            f"copy_notebook: {table} 行数不一致 {copied_count}!={source_count}"
                        )
                dangling = db.execute(
                    "SELECT COUNT(*) FROM knowledge_relations r WHERE r.notebook_id = ? AND ("
                    "r.source_object_id NOT IN "
                    "(SELECT id FROM knowledge_objects WHERE notebook_id = ?) OR "
                    "r.target_object_id NOT IN "
                    "(SELECT id FROM knowledge_objects WHERE notebook_id = ?))",
                    (new_id, new_id, new_id),
                ).fetchone()[0]
                if dangling:
                    raise RuntimeError("copy_notebook: 关系存在悬空引用")

            with self._write() as db:
                db.execute(
                    "UPDATE notebooks SET status = ?, updated_at = ? WHERE id = ?",
                    (source_notebook.status, _now(), new_id),
                )
            return self.get_notebook(new_id)
        except Exception:
            with self._write() as db:
                db.execute("DELETE FROM kg_objects_fts WHERE notebook_id = ?", (new_id,))
                db.execute("DELETE FROM chunks_fts WHERE notebook_id = ?", (new_id,))
                db.execute(
                    "DELETE FROM knowledge_embeddings WHERE notebook_id = ?", (new_id,)
                )
                db.execute(
                    "DELETE FROM notebooks WHERE id = ? AND status = 'copying'", (new_id,)
                )
            if copied_files:
                shutil.rmtree(destination_dir, ignore_errors=True)
            raise

    def user_can_access_notebook(self, notebook_id: str, user_id: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT created_by FROM notebooks WHERE id = ?", (notebook_id,)
            ).fetchone()
        return bool(row) and row["created_by"] == user_id

    def is_member(self, notebook_id: str, user_id: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM notebook_members WHERE notebook_id = ? AND user_id = ?",
                (notebook_id, user_id),
            ).fetchone()
        return row is not None

    def user_can_read_notebook(self, notebook_id: str, user_id: str) -> bool:
        return self.user_can_access_notebook(notebook_id, user_id) or self.is_member(
            notebook_id, user_id
        )

    def user_can_read_source(self, source_id: str, user_id: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT notebook_id FROM sources WHERE id = ?", (source_id,)
            ).fetchone()
        return bool(row) and self.user_can_read_notebook(row["notebook_id"], user_id)

    def add_member(self, notebook_id: str, user_id: str) -> None:
        with self._write() as db:
            db.execute(
                "INSERT OR IGNORE INTO notebook_members (notebook_id, user_id, role, added_at) "
                "VALUES (?, ?, 'reader', ?)",
                (notebook_id, user_id, _now()),
            )

    def remove_member(self, notebook_id: str, user_id: str) -> None:
        with self._write() as db:
            db.execute(
                "DELETE FROM notebook_members WHERE notebook_id = ? AND user_id = ?",
                (notebook_id, user_id),
            )

    def kick_all_members(self, notebook_id: str) -> None:
        with self._write() as db:
            db.execute("DELETE FROM notebook_members WHERE notebook_id = ?", (notebook_id,))

    def list_members(self, notebook_id: str) -> list:
        with self._connect() as db:
            rows = db.execute(
                "SELECT u.username AS username, m.added_at AS added_at "
                "FROM notebook_members m JOIN users u ON u.id = m.user_id "
                "WHERE m.notebook_id = ? ORDER BY m.added_at ASC",
                (notebook_id,),
            ).fetchall()
        return [
            {"username": row["username"], "added_at": row["added_at"]} for row in rows
        ]

    def join_shared(self, notebook_id: str, user_id: str) -> NotebookSummary:
        self.add_member(notebook_id, user_id)
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM notebooks WHERE id = ?", (notebook_id,)
            ).fetchone()
            notebook = self._notebook_from_row(db, row)
        notebook.access = "reader"
        return notebook

    def leave_notebook(self, notebook_id: str, user_id: str) -> None:
        self.remove_member(notebook_id, user_id)

    def source_owner(self, source_id: str) -> "str | None":
        with self._connect() as db:
            row = db.execute(
                "SELECT nb.created_by AS owner FROM sources s "
                "JOIN notebooks nb ON nb.id = s.notebook_id WHERE s.id = ?",
                (source_id,),
            ).fetchone()
        return row["owner"] if row else None

    def conversation_owner(self, conversation_id: str) -> "str | None":
        with self._connect() as db:
            row = db.execute(
                "SELECT created_by AS owner FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
        return row["owner"] if row else None

    def answer_owner(self, answer_id: str) -> "str | None":
        with self._connect() as db:
            row = db.execute(
                "SELECT nb.created_by AS owner FROM answers a "
                "JOIN notebooks nb ON nb.id = a.notebook_id WHERE a.id = ?",
                (answer_id,),
            ).fetchone()
        return row["owner"] if row else None

    def user_can_read_answer(self, answer_id: str, user_id: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT notebook_id FROM answers WHERE id = ?", (answer_id,)
            ).fetchone()
        return bool(row) and self.user_can_read_notebook(row["notebook_id"], user_id)
