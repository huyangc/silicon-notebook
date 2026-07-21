from __future__ import annotations

import ast
import importlib.util
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "diag_db.py"
PYTHON = Path(sys.executable)


def load_diag_db():
    spec = importlib.util.spec_from_file_location("diag_db", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def create_cascade_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(
        """
        CREATE TABLE notebooks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL
        );
        CREATE TABLE sources (
            id TEXT PRIMARY KEY,
            notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
            file_path TEXT NOT NULL
        );
        CREATE INDEX ix_sources_notebook_id ON sources(notebook_id);
        CREATE TABLE legacy_children (
            id TEXT PRIMARY KEY,
            notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE
        );
        CREATE TABLE knowledge_embeddings (
            id TEXT PRIMARY KEY,
            notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE
        );
        CREATE TABLE kg_objects_fts (
            notebook_id TEXT NOT NULL,
            text TEXT NOT NULL
        );
        CREATE TABLE kg_objects_fts_data (id INTEGER PRIMARY KEY, block BLOB);
        CREATE TABLE kg_objects_fts_idx(segid INTEGER, term TEXT, pgno INTEGER);
        """
    )
    connection.execute(
        "INSERT INTO notebooks(id, title) VALUES (?, ?)",
        ("nb-private", "private title"),
    )
    connection.execute(
        "INSERT INTO sources(id, notebook_id, file_path) VALUES (?, ?, ?)",
        ("src-private", "nb-private", "/private/customer-secret.pdf"),
    )
    connection.execute(
        "INSERT INTO legacy_children(id, notebook_id) VALUES (?, ?)",
        ("legacy-private", "nb-private"),
    )
    connection.commit()
    return connection


def test_collects_read_only_cascade_index_plan_and_scale_evidence(tmp_path):
    db_path = tmp_path / "private-production.db"
    connection = create_cascade_database(db_path)
    try:
        diag_db = load_diag_db()
        evidence = diag_db.collect_db_evidence(db_path, notebook_id="nb-private")

        missing = {
            (row["table"], tuple(row["columns"]))
            for row in evidence["missing_fk_indexes"]
        }
        assert ("legacy_children", ("notebook_id",)) in missing
        assert ("sources", ("notebook_id",)) not in missing
        assert evidence["files"]["database_bytes"] > 0
        assert evidence["files"]["wal_bytes"] >= 0
        assert evidence["journal_mode"].lower() == "wal"
        assert any("SCAN" in row["detail"].upper() for row in evidence["delete_plan"])
        assert any(row["table"] == "legacy_children" for row in evidence["relevant_scans"])
        assert any(row["table"] == "legacy_children" for row in evidence["notebook_references"])
        assert evidence["mutations_executed"] == 0
        assert len(evidence["largest_tables"]) <= 20
        assert connection.execute("SELECT COUNT(*) FROM notebooks").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
    finally:
        connection.close()


def test_delete_journal_exclusive_lock_degrades_within_busy_budget(tmp_path):
    db_path = tmp_path / "locked.db"
    owner = sqlite3.connect(db_path)
    owner.execute("PRAGMA journal_mode = DELETE")
    owner.execute("CREATE TABLE notebooks (id TEXT PRIMARY KEY)")
    owner.commit()
    owner.execute("BEGIN EXCLUSIVE")
    try:
        diag_db = load_diag_db()
        started = time.monotonic()
        evidence = diag_db.collect_db_evidence(db_path, notebook_id="nb-private")
        elapsed = time.monotonic() - started

        assert elapsed < 1.5
        assert evidence["mutations_executed"] == 0
        assert any(row["category"] in {"locked", "busy"} for row in evidence["degraded"])
        encoded = json.dumps(evidence)
        assert "database is locked" not in encoded.lower()
        assert "nb-private" not in encoded
    finally:
        owner.rollback()
        owner.close()


def test_closed_wal_database_does_not_create_sidecar_files(tmp_path):
    db_path = tmp_path / "closed-wal.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("CREATE TABLE notebooks (id TEXT PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()
    wal_path = Path(str(db_path) + "-wal")
    shm_path = Path(str(db_path) + "-shm")
    assert not wal_path.exists()
    assert not shm_path.exists()

    diag_db = load_diag_db()
    evidence = diag_db.collect_db_evidence(db_path)

    assert evidence["mutations_executed"] == 0
    assert evidence["journal_mode"].lower() == "wal"
    assert evidence["safety"]["immutable_snapshot"] is True
    assert not wal_path.exists()
    assert not shm_path.exists()


def test_missing_corrupt_and_old_schema_are_copy_safe_degradations(tmp_path):
    diag_db = load_diag_db()
    missing_path = tmp_path / "customer-name.db"

    missing = diag_db.collect_db_evidence(missing_path, notebook_id="nb-secret-value")
    assert missing["status"] == "degraded"
    assert not missing_path.exists()
    missing_report = diag_db.render_db_report(missing)
    assert "customer-name" not in missing_report
    assert "nb-secret-value" not in missing_report

    corrupt_path = tmp_path / "corrupt-customer.db"
    corrupt_path.write_bytes(b"this is not sqlite")
    corrupt = diag_db.collect_db_evidence(corrupt_path)
    assert corrupt["status"] == "degraded"
    assert corrupt["degraded"]
    assert "this is not sqlite" not in json.dumps(corrupt)

    old_path = tmp_path / "old.db"
    with sqlite3.connect(old_path) as connection:
        connection.execute("CREATE TABLE legacy_only (id INTEGER PRIMARY KEY)")
    old = diag_db.collect_db_evidence(old_path, notebook_id="nb-old-secret")
    assert old["status"] in {"ok", "degraded"}
    assert old["mutations_executed"] == 0
    assert any(row["probe"].startswith("plan.") for row in old["degraded"])


def test_report_is_bounded_pseudonymized_and_excludes_private_content(tmp_path):
    db_path = tmp_path / "production-private-name.db"
    connection = create_cascade_database(db_path)
    try:
        diag_db = load_diag_db()
        evidence = diag_db.collect_db_evidence(db_path, notebook_id="nb-private")
        report = diag_db.render_db_report(evidence)

        assert len(report.encode("utf-8")) <= 32 * 1024
        assert "nb-private" not in report
        assert "private-production" not in report
        assert "private title" not in report
        assert "customer-secret.pdf" not in report
        assert "notebook=" in report
        assert "legacy_children" in report
    finally:
        connection.close()


def test_script_is_stdlib_only_and_contains_no_mutating_sql():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".", 1)[0])
    assert imports <= {
        "__future__",
        "argparse",
        "hashlib",
        "json",
        "os",
        "pathlib",
        "sqlite3",
        "sys",
        "time",
        "typing",
        "urllib",
    }
    assert "app" not in imports

    source = SCRIPT.read_text(encoding="utf-8").upper()
    for forbidden in (
        "WAL_CHECKPOINT",
        "VACUUM",
        "ANALYZE ",
        "REINDEX",
        "BEGIN IMMEDIATE",
        "BEGIN EXCLUSIVE",
    ):
        assert forbidden not in source


def test_standalone_missing_file_exits_zero_without_creating_it(tmp_path):
    db_path = tmp_path / "does-not-exist.db"
    completed = subprocess.run(
        [str(PYTHON), str(SCRIPT), "--db", str(db_path), "--notebook-id", "nb-secret"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
        env={},
    )
    assert completed.returncode == 0
    assert not db_path.exists()
    assert "does-not-exist" not in completed.stdout
    assert "nb-secret" not in completed.stdout
    assert completed.stderr == ""
