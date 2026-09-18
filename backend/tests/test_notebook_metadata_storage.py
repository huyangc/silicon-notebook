"""Field ownership and stale-publication contracts across both store dialects."""
from contextlib import contextmanager
from itertools import count
import sqlite3

import pytest

from app.core.config import Settings
from app.models.notebooks import NotebookCreate, NotebookUpdate
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SqliteMigrator
from app.repositories.sqlite.notebook_store import NotebookStore as SQLiteNotebookStore
from app.repositories.sqlite.source_store import SourceStore as SQLiteSourceStore
from app.repositories.postgres.notebook_store import NotebookStore as PostgresNotebookStore
from app.repositories.postgres.source_store import SourceStore as PostgresSourceStore


class _PostgresSQLOnSQLite:
    """Execute the portable store statements while adapting only bind syntax."""
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=()):
        return self.connection.execute(sql.replace("%s", "?"), params)


class _Database:
    def __init__(self, connection, postgres):
        self.connection = connection
        self.postgres = postgres

    @contextmanager
    def write(self):
        with self.connection:
            yield _PostgresSQLOnSQLite(self.connection) if self.postgres else self.connection

    connect = write


@pytest.fixture(params=[False, True], ids=["sqlite", "postgres-sql"])
def store(request):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.create_collation("C", lambda a, b: (a > b) - (a < b))
    connection.executescript("""
        CREATE TABLE notebooks (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, purpose TEXT NOT NULL,
            name_auto INTEGER NOT NULL DEFAULT 0, purpose_auto INTEGER NOT NULL DEFAULT 0,
            metadata_generation INTEGER NOT NULL DEFAULT 0,
            primary_domain TEXT, status TEXT, created_by TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE unified_kg_state (
            notebook_id TEXT, dirty INTEGER, kg_mutation_seq INTEGER,
            source_index_backfilled INTEGER, updated_at TEXT
        );
        CREATE TABLE sources (
            id TEXT PRIMARY KEY, notebook_id TEXT, title TEXT, doc_type TEXT,
            summary TEXT, source_type TEXT, status TEXT, created_at TEXT
        );
    """)
    database = _Database(connection, request.param)
    ids = count()
    cls = PostgresNotebookStore if request.param else SQLiteNotebookStore
    result = cls(database, new_id=lambda prefix: f"{prefix}-{next(ids)}",
                 now=lambda: "2026-09-18T00:00:00+00:00", activity_retention_days=30)
    yield result
    connection.close()


def _publish(store, notebook_id, snapshot, name="生成标题", purpose="生成描述"):
    store.apply_meta_for_notebook(
        notebook_id, guard_name=snapshot["name"], guard_generation=snapshot["generation"],
        name=name, purpose=purpose,
    )


@pytest.mark.parametrize("name,purpose,flags", [
    ("未命名笔记本", "", (True, True)),
    ("Untitled notebook", "手写描述", (True, False)),
    ("手写标题", "", (False, True)),
    ("手写标题", "手写描述", (False, False)),
])
def test_creation_tracks_each_field_independently(store, name, purpose, flags):
    notebook_id = store.create_row(NotebookCreate(name=name, purpose=purpose), "owner")
    snapshot = store.meta_for_notebook(notebook_id)
    assert (snapshot["name_auto"], snapshot["purpose_auto"]) == flags
    _publish(store, notebook_id, snapshot)
    with store.database.connect() as db:
        row = db.execute("SELECT name,purpose FROM notebooks WHERE id=?", (notebook_id,)).fetchone()
    assert row["name"] == ("生成标题" if flags[0] else name)
    assert row["purpose"] == ("生成描述" if flags[1] else purpose)


@pytest.mark.parametrize("manual", ["name", "purpose"])
def test_explicit_placeholder_or_blank_edit_protects_only_that_field(store, manual):
    notebook_id = store.create_row(NotebookCreate(name="未命名笔记本", purpose=""), "owner")
    snapshot = store.meta_for_notebook(notebook_id)
    store.update_row(notebook_id, NotebookUpdate(**{manual: "未命名笔记本" if manual == "name" else ""}))
    _publish(store, notebook_id, snapshot)
    with store.database.connect() as db:
        row = db.execute("SELECT * FROM notebooks WHERE id=?", (notebook_id,)).fetchone()
    assert row["name"] == ("未命名笔记本" if manual == "name" else "生成标题")
    assert row["purpose"] == ("" if manual == "purpose" else "生成描述")
    assert row[f"{manual}_auto"] == 0


def test_later_request_supersedes_old_completion_and_auto_titles_keep_refreshing(store):
    notebook_id = store.create_row(NotebookCreate(name="未命名笔记本", purpose=""), "owner")
    first = store.meta_for_notebook(notebook_id)
    second = store.meta_for_notebook(notebook_id)
    assert second["generation"] == first["generation"] + 1
    _publish(store, notebook_id, second, "新标题", "新描述")
    _publish(store, notebook_id, first, "过期标题", "过期描述")
    third = store.meta_for_notebook(notebook_id)
    assert third["name"] == "新标题"
    assert third["name_auto"] is True
    _publish(store, notebook_id, third, "再次更新", "全部来源")
    with store.database.connect() as db:
        row = db.execute("SELECT name,purpose FROM notebooks WHERE id=?", (notebook_id,)).fetchone()
    assert tuple(row) == ("再次更新", "全部来源")
    assert store.meta_for_notebook("missing") is None


def test_all_visible_sources_are_read_in_stable_order_without_unready_summaries(store):
    statuses = ["uploaded", "parsing", "parsed", "extracting", "extracted", "failed"]
    connection = store.database.connection
    rows = [(f"s-{index:02d}", "nb", f"标题{index}", "论文", "摘要" * 500,
             "pdf", statuses[index % len(statuses)], "same") for index in range(30)]
    rows.extend((f"hidden-{kind}", "nb", "hidden", "", "private", kind, "extracted", "same")
                for kind in ("memory", "knowhow"))
    connection.executemany("INSERT INTO sources VALUES (?,?,?,?,?,?,?,?)", reversed(rows))
    cls = PostgresSourceStore if store.database.postgres else SQLiteSourceStore
    with store.database.connect() as db:
        result = cls.meta_source_rows(db, "nb", "s-00")
    assert len(result) == 30
    assert [row["title"] for row in result] == [f"标题{index}" for index in range(30)]
    for index, row in enumerate(result):
        ready = statuses[index % len(statuses)] in {"parsed", "extracting", "extracted"}
        assert row["summary"] == ("摘要" * 500 if ready else "")


def test_metadata_migration_preserves_ambiguous_legacy_names_and_is_idempotent(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'migration.db'}")
    database = SqliteDatabase(settings, root_dir=tmp_path)
    try:
        with database.connect() as db:
            db.execute("CREATE TABLE notebooks (id TEXT PRIMARY KEY, name TEXT)")
            db.executemany("INSERT INTO notebooks VALUES (?,?)", [
                ("blank", ""), ("cn", "未命名笔记本"), ("en", "Untitled notebook"),
                ("named", "旧标题（可能为人工或自动）"),
            ])
        migrator = SqliteMigrator(database, settings)
        migrator._migration_75()
        with database.connect() as db:
            assert {row["id"]: row["name_auto"] for row in db.execute("SELECT * FROM notebooks")} == {
                "blank": 1, "cn": 1, "en": 1, "named": 0,
            }
            db.execute("UPDATE notebooks SET name_auto=0 WHERE id='cn'")
        migrator._migration_75()
        with database.connect() as db:
            assert db.execute("SELECT name_auto,metadata_generation FROM notebooks WHERE id='cn'").fetchone()[:] == (0, 0)
    finally:
        database.close_local()
