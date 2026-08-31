"""SQLite persistence for the global wish wall."""
from __future__ import annotations

from typing import Callable

from app.repositories.sqlite.database import SqliteDatabase


_WISH_SELECT = (
    "SELECT w.id,w.kind,w.title,w.content,w.author_id,w.created_at,w.updated_at,"
    "COALESCE(NULLIF(u.display_name,''),u.username,w.author_id) AS author_name,"
    "COUNT(v.user_id) AS vote_count,"
    "MAX(CASE WHEN v.user_id=? THEN 1 ELSE 0 END) AS voted_by_me "
    "FROM wishes w JOIN users u ON u.id=w.author_id "
    "LEFT JOIN wish_votes v ON v.wish_id=w.id "
)


def _row(row) -> dict:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "title": row["title"],
        "content": row["content"],
        "author_id": row["author_id"],
        "author_name": row["author_name"],
        "vote_count": int(row["vote_count"]),
        "voted_by_me": bool(row["voted_by_me"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class WishStore:
    def __init__(
        self,
        database: SqliteDatabase,
        *,
        new_id: Callable[[str], str],
        now: Callable[[], str],
    ) -> None:
        self.database = database
        self.new_id = new_id
        self.now = now

    def create_wish(
        self, *, kind: str, title: str, content: str, actor_id: str
    ) -> dict:
        wish_id = self.new_id("wish")
        with self.database.write() as db:
            self.database.begin_immediate(db)
            if kind == "plan":
                actor = db.execute(
                    "SELECT role FROM users WHERE id=?", (actor_id,)
                ).fetchone()
                if actor is None or actor["role"] != "admin":
                    raise PermissionError("admin role required")
            now = self.now()
            db.execute(
                "INSERT INTO wishes(id,kind,title,content,author_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (wish_id, kind, title, content, actor_id, now, now),
            )
            row = db.execute(
                _WISH_SELECT
                + "WHERE w.id=? GROUP BY w.id,w.kind,w.title,w.content,w.author_id,"
                "w.created_at,w.updated_at,u.display_name,u.username",
                (actor_id, wish_id),
            ).fetchone()
        return _row(row)

    def list_wishes(
        self,
        *,
        actor_id: str,
        kind: str | None = None,
        sort: str = "priority",
        offset: int = 0,
        limit: int = 50,
    ) -> dict:
        where = " WHERE w.kind=?" if kind else ""
        filter_params = [kind] if kind else []
        order = (
            "CASE WHEN w.kind='plan' THEN 0 ELSE 1 END,"
            "CASE WHEN w.kind!='plan' THEN COUNT(v.user_id) END DESC,"
            "julianday(w.created_at) DESC,w.id DESC"
            if sort == "priority"
            else "julianday(w.created_at) DESC,w.id DESC"
        )
        with self.database.connect() as db:
            total = int(
                db.execute(
                    "SELECT COUNT(*) AS c FROM wishes w" + where, filter_params
                ).fetchone()["c"]
            )
            rows = db.execute(
                _WISH_SELECT
                + where
                + " GROUP BY w.id,w.kind,w.title,w.content,w.author_id,w.created_at,"
                "w.updated_at,u.display_name,u.username ORDER BY "
                + order
                + " LIMIT ? OFFSET ?",
                [actor_id, *filter_params, limit, offset],
            ).fetchall()
        return {"items": [_row(row) for row in rows], "total": total}

    def toggle_wish_vote(self, wish_id: str, actor_id: str) -> dict:
        with self.database.write() as db:
            self.database.begin_immediate(db)
            wish = db.execute(
                "SELECT kind FROM wishes WHERE id=?", (wish_id,)
            ).fetchone()
            if wish is None:
                raise KeyError(wish_id)
            if wish["kind"] == "plan":
                raise ValueError("plans cannot be voted")
            existing = db.execute(
                "SELECT 1 FROM wish_votes WHERE wish_id=? AND user_id=?",
                (wish_id, actor_id),
            ).fetchone()
            if existing is None:
                db.execute(
                    "INSERT INTO wish_votes(wish_id,user_id,created_at) VALUES (?,?,?)",
                    (wish_id, actor_id, self.now()),
                )
                voted = True
            else:
                db.execute(
                    "DELETE FROM wish_votes WHERE wish_id=? AND user_id=?",
                    (wish_id, actor_id),
                )
                voted = False
            count = int(
                db.execute(
                    "SELECT COUNT(*) AS c FROM wish_votes WHERE wish_id=?", (wish_id,)
                ).fetchone()["c"]
            )
        return {"wish_id": wish_id, "voted": voted, "vote_count": count}
