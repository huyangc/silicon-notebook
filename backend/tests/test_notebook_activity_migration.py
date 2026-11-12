import ast
from pathlib import Path

import pytest

from app.core.config import Settings
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SqliteMigrator
from tests.sqlite_migration_testkit import rollback_v107


def _shape(db):
    return {
        "table": [tuple(row) for row in db.execute("PRAGMA table_info(notebook_activity)")],
        "fk": [tuple(row) for row in db.execute("PRAGMA foreign_key_list(notebook_activity)")],
        "indexes": [tuple(row) for row in db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name='notebook_activity' ORDER BY name")],
    }


@pytest.mark.real_sqlite_migration
def test_notebook_activity_migration_fresh_equals_upgraded(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'migration.db'}")
    database = SqliteDatabase(settings, root_dir=tmp_path)
    migrator = SqliteMigrator(database, settings)
    try:
        migrator.migrate()
        with database.write() as db:
            fresh = _shape(db)
            rollback_v107(db)
            db.execute("PRAGMA user_version=106")
        assert migrator.migrate() == [107, 108, 109]
        assert migrator.migrate() == []
        migrator._migration_107()
        with database.connect() as db:
            assert _shape(db) == fresh
            assert fresh["fk"][0][2] == "notebooks"
            assert fresh["fk"][0][6] == "CASCADE"
            assert all(row[2] != "users" for row in fresh["fk"])
    finally:
        database.close_local()


def test_owner_probe_callers_are_allowlisted():
    root = Path(__file__).resolve().parents[1] / "app"
    allowed = {
        ("services/notebook_sharing.py", "owner_if_can_edit"),
        ("services/notebook_sharing.py", "owner_if_can_admin"),
        ("api/deps.py", "require_notebook_capability"),
        ("api/deps.py", "notebook_capability_owner"),
        ("api/deps.py", "mark_pulse"),
    }
    found = set()
    for path in root.rglob("*.py"):
        class Visitor(ast.NodeVisitor):
            def __init__(self):
                self.scopes = []

            def visit_FunctionDef(self, node):
                self.scopes.append(node.name)
                self.generic_visit(node)
                self.scopes.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, node):
                if isinstance(node.func, ast.Attribute) and node.func.attr in {"owner_if_can_edit", "owner_if_can_admin"}:
                    relative = path.relative_to(root).as_posix()
                    owner = next((scope for scope in self.scopes if (relative, scope) in allowed), None)
                    assert owner is not None, (relative, tuple(self.scopes), node.func.attr)
                    found.add((relative, owner))
                self.generic_visit(node)

        Visitor().visit(ast.parse(path.read_text()))
    assert {("services/notebook_sharing.py", "owner_if_can_edit"),
            ("services/notebook_sharing.py", "owner_if_can_admin")} <= found
