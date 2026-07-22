from __future__ import annotations

from typing import Callable, Literal, Sequence

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
from app.services.knowledge_contracts import USABLE_STATUSES  # noqa: F401


_MOUNT_JOIN = (
    "FROM notebook_bases e "
    "JOIN notebooks b ON b.id=e.base_notebook_id "
    "JOIN notebooks a ON a.id=e.notebook_id "
    "WHERE e.notebook_id=%s AND b.id<>e.notebook_id"
)
_MOUNT_VALID_EXPR = (
    "(b.status<>'copying' AND (b.tier='base' OR b.created_by=a.created_by))"
)
_MOUNT_VALID = " AND " + _MOUNT_VALID_EXPR
_MOUNT_ORDER = " ORDER BY CASE WHEN b.tier='base' THEN 0 ELSE 1 END,b.name COLLATE \"C\""


class NotebookStore:
    """PostgreSQL notebook rows, tier transitions, and mounted-base edges."""

    def __init__(
        self,
        database: PostgresDatabase,
        *,
        new_id: Callable[[str], str],
        now: Callable[[], TimestampInput],
    ) -> None:
        self.database = database
        self.new_id = new_id
        self.now = normalized_clock(now)

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
            + " AS ok,(b.created_by=a.created_by) AS same_owner "
            + _MOUNT_JOIN
            + _MOUNT_ORDER,
            (notebook_id,),
        ).fetchall()
        result = []
        for row in rows:
            active = bool(row["ok"])
            name_visible = active or bool(row["same_owner"])
            result.append(
                {
                    "id": row["id"],
                    "name": row["name"] if name_visible else "已不可用的知识库",
                    "tier": row["tier"] or "personal",
                    "active": active,
                    "inactive_reason": "" if active else "该库已不是公共知识库，且不属于你",
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
            "SELECT b.id AS id,b.name AS name,b.tier AS tier "
            "FROM notebooks b JOIN notebooks a ON a.id=%s "
            "WHERE b.id<>a.id AND "
            + _MOUNT_VALID_EXPR
            + _MOUNT_ORDER,
            (notebook_id,),
        ).fetchall()
        return [
            {"id": row["id"], "name": row["name"], "tier": row["tier"] or "personal"}
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

    def delete_row_and_orphan_embeddings(self, notebook_id: str) -> list[str]:
        with self.database.write() as connection:
            rows = connection.execute(
                "SELECT file_path FROM sources WHERE notebook_id=%s", (notebook_id,)
            ).fetchall()
            connection.execute(
                "DELETE FROM knowledge_embeddings WHERE notebook_id=%s", (notebook_id,)
            )
            connection.execute("DELETE FROM notebooks WHERE id=%s", (notebook_id,))
        return [row["file_path"] for row in rows]

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
