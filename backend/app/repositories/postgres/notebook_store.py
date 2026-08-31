from __future__ import annotations

from typing import Callable, Literal, Sequence

from app.core.activity_time import activity_retention_window
from app.models.notebooks import NotebookCreate, NotebookUpdate
from app.repositories.postgres._store_utils import (
    TimestampInput,
    execute_many,
    jsonb,
    normalized_clock,
    placeholders,
    sqlite_compatible_notebook_row,
)
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.mount_sql import (
    MOUNT_GATE_CLOSED_EXPR as _MOUNT_GATE_CLOSED_EXPR,
    MOUNT_JOIN as _MOUNT_JOIN,
    MOUNT_ORDER as _MOUNT_ORDER,
    MOUNT_ORIGIN_COLUMN as _MOUNT_ORIGIN_COLUMN,
    MOUNT_VALID as _MOUNT_VALID,
    MOUNT_VALID_EXPR as _MOUNT_VALID_EXPR,
)
from app.domain.knowledge_contracts import USABLE_STATUSES  # noqa: F401
from app.repositories.postgres.source_store import VISIBLE_SOURCE_TYPES_PREDICATE


class NotebookStore:
    """PostgreSQL notebook rows, tier transitions, and mounted-base edges."""

    def __init__(
        self,
        database: PostgresDatabase,
        *,
        new_id: Callable[[str], str],
        now: Callable[[], TimestampInput],
        activity_retention_days: int,
    ) -> None:
        self.database = database
        self.new_id = new_id
        self.now = normalized_clock(now)
        self.activity_retention_days = int(activity_retention_days)

    def tier_map(self, notebook_ids: Sequence[str]) -> dict[str, str]:
        ids = list(dict.fromkeys(value for value in notebook_ids if value))
        if not ids:
            return {}
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT id,tier FROM notebooks WHERE id IN ({placeholders(ids)})",
                ids,
            ).fetchall()
        return {row["id"]: row["tier"] or "personal" for row in rows}

    @staticmethod
    def resolve_participants(connection, active_notebook_id: str) -> list[tuple[str, str]]:
        active = connection.execute(
            "SELECT tier FROM notebooks WHERE id=%s", (active_notebook_id,)
        ).fetchone()
        result = [
            (
                active_notebook_id,
                (active["tier"] if active is not None else "personal") or "personal",
            )
        ]
        rows = connection.execute(
            "SELECT b.id AS id,b.tier AS tier "
            + _MOUNT_JOIN
            + _MOUNT_VALID
            + _MOUNT_ORDER,
            (active_notebook_id,),
        ).fetchall()
        result.extend((row["id"], row["tier"] or "personal") for row in rows)
        return result

    def participant_notebook_ids(self, active_notebook_id: str) -> list[str]:
        with self.database.connect() as connection:
            return self.participant_ids(connection, active_notebook_id)

    @staticmethod
    def participant_ids(connection, active_notebook_id: str) -> list[str]:
        return [
            notebook_id
            for notebook_id, _tier in NotebookStore.resolve_participants(
                connection, active_notebook_id
            )
        ]

    @staticmethod
    def participant_rows(connection, active_notebook_id: str):
        bases = connection.execute(
            "SELECT b.id AS id,b.tier AS tier "
            + _MOUNT_JOIN
            + _MOUNT_VALID
            + _MOUNT_ORDER,
            (active_notebook_id,),
        ).fetchall()
        active = connection.execute(
            "SELECT id,tier FROM notebooks WHERE id=%s", (active_notebook_id,)
        ).fetchone()
        return active, bases

    @staticmethod
    def participant_tiers(connection, active_notebook_id: str):
        pairs = NotebookStore.resolve_participants(connection, active_notebook_id)
        return [notebook_id for notebook_id, _tier in pairs], dict(pairs)

    @staticmethod
    def list_mount_edges(connection, notebook_id: str) -> list[dict]:
        rows = connection.execute(
            "SELECT b.id AS id,b.name AS name,b.tier AS tier,"
            + _MOUNT_VALID_EXPR
            + " AS ok,"
            + _MOUNT_GATE_CLOSED_EXPR
            + " AS gate_closed,(b.created_by=a.created_by) AS same_owner "
            + _MOUNT_JOIN
            + _MOUNT_ORDER,
            (notebook_id,),
        ).fetchall()
        result = []
        for row in rows:
            active = bool(row["ok"])
            gate_closed = bool(row["gate_closed"])
            # 被未共享门关上的借入边:挂载方 owner 对被挂库仍有合法读权,名字
            # 照常显示,文案给出恢复出口(与 SQLite 侧逐字一致)。
            name_visible = active or bool(row["same_owner"]) or gate_closed
            if active:
                reason = ""
            elif gate_closed:
                reason = "本笔记本已共享，借来的参考库暂停参与检索；取消本笔记本的共享即可恢复"
            else:
                reason = "该库已不是公共知识库，且不属于你"
            result.append(
                {
                    "id": row["id"],
                    "name": row["name"] if name_visible else "已不可用的知识库",
                    "tier": row["tier"] or "personal",
                    "active": active,
                    "inactive_reason": reason,
                }
            )
        return result

    def list_mount_edges_for_notebook(self, notebook_id: str) -> list[dict]:
        with self.database.connect() as connection:
            return self.list_mount_edges(connection, notebook_id)

    @staticmethod
    def mounted_by_count(connection, notebook_id: str) -> int:
        row = connection.execute(
            "SELECT COUNT(*) AS c FROM notebook_bases WHERE base_notebook_id=%s",
            (notebook_id,),
        ).fetchone()
        return int(row["c"]) if row else 0

    def mounted_by_count_for_notebook(self, notebook_id: str) -> int:
        with self.database.connect() as connection:
            return self.mounted_by_count(connection, notebook_id)

    @staticmethod
    def mountable_notebooks(connection, notebook_id: str) -> list[dict]:
        rows = connection.execute(
            "SELECT b.id AS id,b.name AS name,b.tier AS tier,"
            + _MOUNT_ORIGIN_COLUMN
            + " FROM notebooks b JOIN notebooks a ON a.id=%s "
            "WHERE b.id<>a.id AND "
            + _MOUNT_VALID_EXPR
            + _MOUNT_ORDER,
            (notebook_id,),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "tier": row["tier"] or "personal",
                "origin": row["origin"],
            }
            for row in rows
        ]

    def mountable_for_notebook(self, notebook_id: str) -> list[dict]:
        with self.database.connect() as connection:
            return self.mountable_notebooks(connection, notebook_id)

    def replace_mounts(
        self, notebook_id: str, base_notebook_ids: Sequence[str], created_by: str
    ) -> None:
        wanted = [
            value
            for value in dict.fromkeys(base_notebook_ids)
            if value and value != notebook_id
        ]
        now = self.now()
        with self.database.write() as connection:
            connection.execute(
                "DELETE FROM notebook_bases WHERE notebook_id=%s", (notebook_id,)
            )
            execute_many(
                connection,
                "INSERT INTO notebook_bases"
                "(notebook_id,base_notebook_id,created_at,created_by) VALUES (%s,%s,%s,%s)",
                [
                    (notebook_id, base_id, now, created_by)
                    for base_id in wanted
                ],
            )

    def create_row(self, payload: NotebookCreate, created_by: str) -> str:
        notebook_id = self.new_id("nb")
        purpose = (payload.purpose or "").strip()
        now = self.now()
        with self.database.write() as connection:
            connection.execute(
                "INSERT INTO notebooks"
                "(id,name,purpose,primary_domain,status,created_by,created_at,updated_at,"
                "purpose_auto) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    notebook_id,
                    payload.name,
                    purpose,
                    "Semiconductor",
                    "draft",
                    created_by,
                    now,
                    now,
                    0 if purpose else 1,
                ),
            )
            # Empty is a complete provenance index.  Online KG mutations keep
            # knowledge_object_sources coherent; legacy imports/copies bypass
            # this seam and intentionally retain an unknown (false) marker.
            # `last_rebuild_at` stays NULL: every reader goes through this
            # store's `state_row`, whose `iso_timestamp` normalizes NULL to ""
            # (pinned by test_new_notebook_status_is_typed_serializable), and
            # the KG-analysis view maps this zero-history row to its row-absent
            # shape (`_state_view`: present means "has KG history").
            connection.execute(
                "INSERT INTO unified_kg_state"
                "(notebook_id,dirty,kg_mutation_seq,source_index_backfilled,updated_at) "
                "VALUES (%s,0,0,1,%s)",
                (notebook_id, now),
            )
        return notebook_id

    def get_row(self, notebook_id: str, *, include_copying: bool = False) -> dict:
        statement = "SELECT * FROM notebooks WHERE id=%s"
        if not include_copying:
            statement += " AND status<>'copying'"
        with self.database.connect() as connection:
            row = connection.execute(statement, (notebook_id,)).fetchone()
        if row is None:
            raise KeyError(notebook_id)
        return sqlite_compatible_notebook_row(row)

    def update_row(self, notebook_id: str, payload: NotebookUpdate) -> None:
        updates: list[str] = []
        values: list[object] = []
        if payload.name is not None:
            updates.append("name=%s")
            values.append(payload.name.strip() or "Untitled notebook")
        if payload.purpose is not None:
            updates.extend(("purpose=%s", "purpose_auto=%s"))
            values.extend((payload.purpose.strip(), 0))
        if payload.primary_domain is not None:
            updates.append("primary_domain=%s")
            values.append(payload.primary_domain.strip() or "Semiconductor")
        if payload.target_users is not None:
            updates.append("target_users=%s")
            values.append(payload.target_users.strip())
        if payload.access_scope is not None:
            updates.append("access_scope=%s")
            values.append(payload.access_scope.strip())
        for field in ("expected_questions", "source_types", "taxonomy"):
            value = getattr(payload, field)
            if value is not None:
                updates.append(f"{field}=%s")
                values.append(jsonb(value))
        if not updates:
            return
        updates.append("updated_at=%s")
        values.extend((self.now(), notebook_id))
        with self.database.write() as connection:
            connection.execute(
                f"UPDATE notebooks SET {','.join(updates)} WHERE id=%s", values
            )

    def set_tier(self, notebook_id: str, tier: Literal["base", "personal"]) -> None:
        with self.database.write() as connection:
            connection.execute(
                "UPDATE notebooks SET tier=%s,updated_at=%s WHERE id=%s",
                (tier, self.now(), notebook_id),
            )

    def indexing_pipeline_state(self, notebook_id: str) -> dict[str, str]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(n.indexing_pipeline,'') AS pipeline_id,"
                "COALESCE(n.indexing_pipeline_version,'builtin.chunk.v1') "
                "AS pipeline_version,"
                "COALESCE(n.indexing_pipeline_generation,'') "
                "AS pipeline_generation,"
                "COALESCE(n.indexing_pipeline_job_id,'') AS pipeline_job_id,"
                "COALESCE(j.status,'') AS pipeline_job_status,"
                "COALESCE(u.indexing_pipeline_id,'') AS published_pipeline_id,"
                "COALESCE(u.indexing_pipeline_version,'builtin.chunk.v1') "
                "AS published_pipeline_version "
                "FROM notebooks n LEFT JOIN unified_kg_state u "
                "ON u.notebook_id=n.id LEFT JOIN kg_build_jobs j "
                "ON j.id=n.indexing_pipeline_job_id "
                "WHERE n.id=%s AND n.status<>'copying'",
                (notebook_id,),
            ).fetchone()
        if row is None:
            raise KeyError(notebook_id)
        return {
            "pipeline_id": str(row["pipeline_id"] or ""),
            "pipeline_version": str(
                row["pipeline_version"] or "builtin.chunk.v1"
            ),
            "pipeline_generation": str(row["pipeline_generation"] or ""),
            "pipeline_job_id": str(row["pipeline_job_id"] or ""),
            "pipeline_job_status": str(row["pipeline_job_status"] or ""),
            "published_pipeline_id": str(row["published_pipeline_id"] or ""),
            "published_pipeline_version": str(
                row["published_pipeline_version"] or "builtin.chunk.v1"
            ),
        }

    def set_indexing_pipeline_desired(
        self, notebook_id: str, pipeline_id: str, pipeline_version: str
    ) -> str:
        generation = self.new_id("ipg")
        with self.database.write() as connection:
            changed = connection.execute(
                "UPDATE notebooks SET indexing_pipeline=%s,"
                "indexing_pipeline_version=%s,indexing_pipeline_generation=%s,"
                "indexing_pipeline_job_id=%s,"
                "updated_at=%s WHERE id=%s AND status<>'copying'",
                (
                    pipeline_id or None,
                    pipeline_version,
                    generation,
                    f"pending:{generation}",
                    self.now(),
                    notebook_id,
                ),
            )
            if changed.rowcount != 1:
                raise KeyError(notebook_id)
        return generation

    def attach_indexing_pipeline_job(
        self, notebook_id: str, generation: str, job_id: str
    ) -> bool:
        with self.database.write() as connection:
            changed = connection.execute(
                "UPDATE notebooks SET indexing_pipeline_job_id=%s "
                "WHERE id=%s AND indexing_pipeline_generation=%s "
                "AND indexing_pipeline_job_id=%s",
                (job_id, notebook_id, generation, f"pending:{generation}"),
            )
        return changed.rowcount == 1

    def delete_row_and_orphan_embeddings(self, notebook_id: str) -> list[str]:
        with self.database.write() as connection:
            # Lock the aggregate root before reading any child rows. PostgreSQL
            # FK inserts take FOR KEY SHARE on this row, which conflicts with
            # FOR UPDATE: after this point a concurrent ask/source/report can
            # neither commit between the snapshot and cascade nor be silently
            # deleted without entering the snapshot.
            connection.execute(
                "SELECT id FROM notebooks WHERE id=%s FOR UPDATE",
                (notebook_id,),
            ).fetchone()
            rows = connection.execute(
                "SELECT file_path FROM sources WHERE notebook_id=%s", (notebook_id,)
            ).fetchall()
            self._retain_user_activity_before_delete(connection, notebook_id)
            connection.execute(
                "DELETE FROM knowledge_embeddings WHERE notebook_id=%s", (notebook_id,)
            )
            connection.execute("DELETE FROM notebooks WHERE id=%s", (notebook_id,))
        return [row["file_path"] for row in rows]

    def _retain_user_activity_before_delete(
        self, connection, notebook_id: str
    ) -> None:
        """PostgreSQL twin of SQLite's atomic, content-minimal projection."""
        deleted_at, expires_at = activity_retention_window(
            self.now(), retention_days=self.activity_retention_days
        )
        connection.execute(
            "DELETE FROM retained_user_activity WHERE expires_at<=%s",
            (deleted_at,),
        )
        common_columns = (
            "activity_type,record_id,actor_id,notebook_id,notebook_owner_id,"
            "notebook_name,created_at,updated_at,asked_at,conversation_id,"
            "question,mode,status,display_title,file_name,source_type,"
            "parse_status,parse_failed,depth,generation_started_at,deleted_at,"
            "expires_at"
        )
        connection.execute(
            f"INSERT INTO retained_user_activity ({common_columns}) "
            "SELECT 'ask',j.id,j.created_by,j.notebook_id,n.created_by,n.name,"
            "j.created_at,j.updated_at,j.asked_at,j.conversation_id,j.question,"
            "j.mode,j.status,'','','','',false,0,'',%s,%s "
            "FROM ask_jobs j JOIN notebooks n ON n.id=j.notebook_id "
            "WHERE j.notebook_id=%s ON CONFLICT DO NOTHING",
            (deleted_at, expires_at, notebook_id),
        )
        connection.execute(
            f"INSERT INTO retained_user_activity ({common_columns}) "
            "SELECT 'source',s.id,n.created_by,s.notebook_id,n.created_by,n.name,"
            "s.created_at,s.updated_at,'','','','',s.status,"
            "CASE WHEN COALESCE(pm.is_paper,0)=1 "
            "AND btrim(COALESCE(pm.paper_title,''))<>'' "
            "THEN btrim(pm.paper_title) ELSE btrim(CASE "
            "WHEN COALESCE(s.title,'')<>'' THEN s.title "
            "ELSE COALESCE(s.file_name,'') END) END,"
            "s.file_name,s.source_type,s.parse_status,"
            "CASE WHEN s.parse_status='failed' THEN true ELSE false END,0,'',%s,%s "
            "FROM sources s JOIN notebooks n ON n.id=s.notebook_id "
            "LEFT JOIN source_paper_meta pm ON pm.source_id=s.id "
            f"WHERE s.notebook_id=%s AND {VISIBLE_SOURCE_TYPES_PREDICATE} "
            "ON CONFLICT DO NOTHING",
            (deleted_at, expires_at, notebook_id),
        )
        connection.execute(
            f"INSERT INTO retained_user_activity ({common_columns}) "
            "SELECT 'report',r.id,r.created_by,r.notebook_id,n.created_by,n.name,"
            "r.created_at,r.updated_at,'','',r.question,'',r.status,'','','','',"
            "false,r.depth,COALESCE(r.understanding_json->>"
            "'_generation_started_at',''),%s,%s FROM reports r "
            "JOIN notebooks n ON n.id=r.notebook_id WHERE r.notebook_id=%s "
            "ON CONFLICT DO NOTHING",
            (deleted_at, expires_at, notebook_id),
        )

    @staticmethod
    def meta_row(connection, notebook_id: str) -> dict | None:
        row = connection.execute(
            "SELECT name,purpose_auto FROM notebooks WHERE id=%s", (notebook_id,)
        ).fetchone()
        if row is None:
            return None
        return {"name": row["name"], "purpose_auto": row["purpose_auto"] == 1}

    def apply_meta(
        self,
        connection,
        notebook_id: str,
        *,
        guard_name: str,
        name: str,
        purpose: str,
    ) -> None:
        if name:
            connection.execute(
                "UPDATE notebooks SET name=%s,updated_at=%s WHERE id=%s AND name=%s",
                (name, self.now(), notebook_id, guard_name),
            )
        if purpose:
            connection.execute(
                "UPDATE notebooks SET purpose=%s,updated_at=%s "
                "WHERE id=%s AND purpose_auto=1",
                (purpose, self.now(), notebook_id),
            )

    @staticmethod
    def tier_on(connection, notebook_id: str) -> str:
        row = connection.execute(
            "SELECT tier FROM notebooks WHERE id=%s", (notebook_id,)
        ).fetchone()
        return str(row["tier"]) if row and row["tier"] else ""

    def meta_for_notebook(self, notebook_id: str) -> dict | None:
        with self.database.connect() as connection:
            return self.meta_row(connection, notebook_id)

    def apply_meta_for_notebook(
        self, notebook_id: str, *, guard_name: str, name: str, purpose: str
    ) -> None:
        with self.database.write() as connection:
            self.apply_meta(
                connection,
                notebook_id,
                guard_name=guard_name,
                name=name,
                purpose=purpose,
            )

    def tier(self, notebook_id: str) -> str:
        with self.database.connect() as connection:
            return self.tier_on(connection, notebook_id)
