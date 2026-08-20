"""SQLite v56 group-owner migration contract."""

import sqlite3

from app.repositories.sqlite.migrations import SqliteMigrator


def _run_v56(db: sqlite3.Connection) -> None:
    migrator = object.__new__(SqliteMigrator)
    migrator._connect = lambda: db
    migrator._migration_56()


def _schema(*, owner_column: bool = False) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    owner_sql = ", owner_id TEXT NOT NULL DEFAULT ''" if owner_column else ""
    db.execute(
        f"""
        CREATE TABLE groups (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          kind TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          created_by TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
          {owner_sql}
        )
        """
    )
    db.execute(
        """
        CREATE TABLE group_members (
          group_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          role TEXT NOT NULL,
          added_at TEXT NOT NULL,
          added_by TEXT NOT NULL,
          PRIMARY KEY (group_id, user_id)
        )
        """
    )
    db.execute(
        "INSERT INTO groups (id,name,kind,description,created_by,created_at,updated_at) "
        "VALUES ('grp-old','历史项目','project','','user-creator','t0','t0')"
    )
    return db


def test_v56_prefers_creator_when_creator_is_still_an_admin():
    db = _schema()
    db.executemany(
        "INSERT INTO group_members VALUES ('grp-old',?,?,?,?)",
        [
            ("user-other", "admin", "t1", "user-creator"),
            ("user-creator", "admin", "t2", "user-creator"),
        ],
    )

    _run_v56(db)

    assert db.execute(
        "SELECT owner_id FROM groups WHERE id='grp-old'"
    ).fetchone()[0] == "user-creator"


def test_v56_uses_current_admin_when_creator_was_demoted():
    db = _schema()
    db.executemany(
        "INSERT INTO group_members VALUES ('grp-old',?,?,?,?)",
        [
            ("user-creator", "member", "t0", "user-creator"),
            ("user-admin", "admin", "t1", "user-creator"),
        ],
    )

    _run_v56(db)

    assert db.execute(
        "SELECT owner_id FROM groups WHERE id='grp-old'"
    ).fetchone()[0] == "user-admin"
    assert db.execute(
        "SELECT role FROM group_members "
        "WHERE group_id='grp-old' AND user_id='user-creator'"
    ).fetchone()[0] == "member"


def test_v56_uses_current_admin_when_creator_already_left():
    db = _schema()
    db.execute(
        "INSERT INTO group_members VALUES "
        "('grp-old','user-admin','admin','t1','user-creator')"
    )

    _run_v56(db)

    assert db.execute(
        "SELECT owner_id FROM groups WHERE id='grp-old'"
    ).fetchone()[0] == "user-admin"
    assert db.execute(
        "SELECT COUNT(*) FROM group_members WHERE user_id='user-creator'"
    ).fetchone()[0] == 0


def test_v56_repairs_a_corrupt_group_with_no_admin_and_is_idempotent():
    db = _schema(owner_column=True)
    db.execute(
        "INSERT INTO group_members VALUES "
        "('grp-old','user-creator','member','t0','user-creator')"
    )

    _run_v56(db)
    _run_v56(db)

    columns = [row[1] for row in db.execute("PRAGMA table_info(groups)")]
    assert columns.count("owner_id") == 1
    assert db.execute(
        "SELECT owner_id FROM groups WHERE id='grp-old'"
    ).fetchone()[0] == "user-creator"
    assert db.execute(
        "SELECT role FROM group_members "
        "WHERE group_id='grp-old' AND user_id='user-creator'"
    ).fetchone()[0] == "admin"
