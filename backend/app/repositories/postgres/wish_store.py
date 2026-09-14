"""PostgreSQL persistence for the global wish wall."""
from __future__ import annotations

from typing import Callable

from app.models.wishes import WISH_PAGE_DEFAULT
from app.repositories.postgres._store_utils import (
    TimestampInput,
    iso_timestamp,
    normalized_clock,
)
from app.repositories.postgres.database import PostgresDatabase


_WISH_SELECT = (
    "SELECT w.id,w.kind,w.title,w.content,w.author_id,w.status,w.created_at,"
    "w.updated_at,"
    "COALESCE(NULLIF(u.display_name,''),u.username,w.author_id) AS author_name,"
    "COUNT(v.user_id) AS vote_count,"
    "BOOL_OR(v.user_id=%s) AS voted_by_me "
    "FROM wishes w JOIN users u ON u.id=w.author_id "
    "LEFT JOIN wish_votes v ON v.wish_id=w.id "
)
_WISH_GROUP_BY = (
    " GROUP BY w.id,w.kind,w.title,w.content,w.author_id,w.status,w.created_at,"
    "w.updated_at,u.display_name,u.username"
)
# Priority order: plans first, then still-open work above closed work, then
# the vote count (plans carry none), then newest first. ``latest`` ignores
# status on purpose — it is the audit view of what was published when.
_CLOSED_STATUSES = ("done", "declined")


def _row(row) -> dict:
    return {
        "id": str(row["id"]),
        "kind": str(row["kind"]),
        "title": str(row["title"]),
        "content": str(row["content"]),
        "author_id": str(row["author_id"]),
        "author_name": str(row["author_name"]),
        "status": str(row["status"]),
        "vote_count": int(row["vote_count"]),
        "voted_by_me": bool(row["voted_by_me"]),
        "created_at": iso_timestamp(row["created_at"]),
        "updated_at": iso_timestamp(row["updated_at"]),
    }


class WishStore:
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

    def _fetch(self, db, wish_id: str, actor_id: str) -> dict:
        row = db.execute(
            _WISH_SELECT + "WHERE w.id=%s" + _WISH_GROUP_BY, (actor_id, wish_id)
        ).fetchone()
        if row is None:
            raise KeyError(wish_id)
        return _row(row)

    @staticmethod
    def _actor_role(db, actor_id: str) -> str:
        actor = db.execute(
            "SELECT role FROM users WHERE id=%s FOR UPDATE", (actor_id,)
        ).fetchone()
        return "" if actor is None else str(actor["role"])

    @staticmethod
    def _lock_wish(db, wish_id: str):
        wish = db.execute(
            "SELECT kind,author_id FROM wishes WHERE id=%s FOR UPDATE", (wish_id,)
        ).fetchone()
        if wish is None:
            raise KeyError(wish_id)
        return wish

    def create_wish(
        self, *, kind: str, title: str, content: str, actor_id: str
    ) -> dict:
        wish_id = self.new_id("wish")
        with self.database.write() as db:
            if kind == "plan" and self._actor_role(db, actor_id) != "admin":
                raise PermissionError("admin role required")
            now = self.now()
            db.execute(
                "INSERT INTO wishes(id,kind,title,content,author_id,status,created_at,"
                "updated_at) VALUES (%s,%s,%s,%s,%s,'open',%s,%s)",
                (wish_id, kind, title, content, actor_id, now, now),
            )
            return self._fetch(db, wish_id, actor_id)

    def list_wishes(
        self,
        *,
        actor_id: str,
        kind: str | None = None,
        status: str | None = None,
        sort: str = "priority",
        offset: int = 0,
        limit: int = WISH_PAGE_DEFAULT,
    ) -> dict:
        clauses: list[str] = []
        filter_params: list[str] = []
        if kind:
            clauses.append("w.kind=%s")
            filter_params.append(kind)
        if status:
            clauses.append("w.status=%s")
            filter_params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        closed = ",".join(f"'{value}'" for value in _CLOSED_STATUSES)
        order = (
            "CASE WHEN w.kind='plan' THEN 0 ELSE 1 END,"
            f"CASE WHEN w.status IN ({closed}) THEN 1 ELSE 0 END,"
            "CASE WHEN w.kind!='plan' THEN COUNT(v.user_id) END DESC,"
            "w.created_at DESC,w.id COLLATE \"C\" DESC"
            if sort == "priority"
            else "w.created_at DESC,w.id COLLATE \"C\" DESC"
        )
        with self.database.connect() as db:
            total = int(
                db.execute(
                    "SELECT COUNT(*) AS c FROM wishes w" + where, filter_params
                ).fetchone()["c"]
            )
            rows = db.execute(
                _WISH_SELECT + where + _WISH_GROUP_BY + " ORDER BY " + order
                + " LIMIT %s OFFSET %s",
                [actor_id, *filter_params, limit, offset],
            ).fetchall()
        return {"items": [_row(row) for row in rows], "total": total}

    def update_wish(
        self,
        wish_id: str,
        *,
        actor_id: str,
        kind: str | None = None,
        title: str | None = None,
        content: str | None = None,
    ) -> dict:
        with self.database.write() as db:
            wish = self._lock_wish(db, wish_id)
            role = self._actor_role(db, actor_id)
            if wish["author_id"] != actor_id and role != "admin":
                raise PermissionError("author or admin role required")
            next_kind = kind or wish["kind"]
            # Same rule as create: only an administrator may publish a plan, and
            # turning a bug/feature into a plan is publishing one. Editing the
            # title/content of a plan an admin already promoted stays with the
            # author -- the restriction is on the kind change, not the kind.
            if next_kind == "plan" and wish["kind"] != "plan" and role != "admin":
                raise PermissionError("admin role required")
            assignments = ["kind=%s", "updated_at=%s"]
            params: list[object] = [next_kind, self.now()]
            if title is not None:
                assignments.append("title=%s")
                params.append(title)
            if content is not None:
                assignments.append("content=%s")
                params.append(content)
            params.append(wish_id)
            db.execute(
                f"UPDATE wishes SET {','.join(assignments)} WHERE id=%s", params
            )
            return self._fetch(db, wish_id, actor_id)

    def delete_wish(self, wish_id: str, *, actor_id: str) -> None:
        with self.database.write() as db:
            wish = self._lock_wish(db, wish_id)
            if wish["author_id"] != actor_id and self._actor_role(db, actor_id) != "admin":
                raise PermissionError("author or admin role required")
            # Votes go with the wish. The FK cascade would do this too, but an
            # explicit delete keeps the write self-describing and mirrors the
            # SQLite store line for line.
            db.execute("DELETE FROM wish_votes WHERE wish_id=%s", (wish_id,))
            db.execute("DELETE FROM wishes WHERE id=%s", (wish_id,))

    def set_wish_status(
        self, wish_id: str, *, status: str, actor_id: str
    ) -> dict:
        with self.database.write() as db:
            self._lock_wish(db, wish_id)
            if self._actor_role(db, actor_id) != "admin":
                raise PermissionError("admin role required")
            db.execute(
                "UPDATE wishes SET status=%s,updated_at=%s WHERE id=%s",
                (status, self.now(), wish_id),
            )
            return self._fetch(db, wish_id, actor_id)

    def toggle_wish_vote(self, wish_id: str, actor_id: str) -> dict:
        with self.database.write() as db:
            wish = self._lock_wish(db, wish_id)
            if wish["kind"] == "plan":
                raise ValueError("plans cannot be voted")
            existing = db.execute(
                "SELECT 1 FROM wish_votes WHERE wish_id=%s AND user_id=%s",
                (wish_id, actor_id),
            ).fetchone()
            if existing is None:
                db.execute(
                    "INSERT INTO wish_votes(wish_id,user_id,created_at) VALUES (%s,%s,%s)",
                    (wish_id, actor_id, self.now()),
                )
                voted = True
            else:
                db.execute(
                    "DELETE FROM wish_votes WHERE wish_id=%s AND user_id=%s",
                    (wish_id, actor_id),
                )
                voted = False
            count = int(
                db.execute(
                    "SELECT COUNT(*) AS c FROM wish_votes WHERE wish_id=%s", (wish_id,)
                ).fetchone()["c"]
            )
        return {"wish_id": wish_id, "voted": voted, "vote_count": count}
