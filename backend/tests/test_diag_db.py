from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
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


def file_fingerprint(path: Path):
    try:
        stat = path.stat()
        return {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    except FileNotFoundError:
        return None


def database_fingerprints(path: Path):
    return {
        suffix or "database": file_fingerprint(Path(str(path) + suffix))
        for suffix in ("", "-wal", "-shm")
    }


@contextmanager
def held_database(path: Path, journal_mode: str):
    owner_code = r"""
import sqlite3
import sys

path, journal_mode = sys.argv[1:]
connection = sqlite3.connect(path)
connection.execute("PRAGMA foreign_keys = ON")
connection.execute(f"PRAGMA journal_mode = {journal_mode}")
connection.executescript('''
CREATE TABLE notebooks (id TEXT PRIMARY KEY);
CREATE TABLE live_children (
    id TEXT PRIMARY KEY,
    notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE
);
''')
connection.execute("INSERT INTO notebooks(id) VALUES ('nb-private')")
connection.commit()
if journal_mode == "DELETE":
    connection.execute("BEGIN EXCLUSIVE")
print("ready", flush=True)
sys.stdin.readline()
if connection.in_transaction:
    connection.rollback()
connection.close()
"""
    process = subprocess.Popen(
        [str(PYTHON), "-c", owner_code, str(path), journal_mode],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        yield process
    finally:
        if process.stdin is not None:
            process.stdin.write("\n")
            process.stdin.flush()
        stdout, stderr = process.communicate(timeout=3)
        assert process.returncode == 0, stdout + stderr


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
            notebook_id TEXT NOT NULL,
            vector TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX idx_knowledge_embeddings_nb
            ON knowledge_embeddings(notebook_id);
        CREATE VIRTUAL TABLE kg_objects_fts USING fts5(
            object_id UNINDEXED, notebook_id UNINDEXED, name
        );
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            chunk_id UNINDEXED, notebook_id UNINDEXED, text
        );
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
    connection.execute(
        "INSERT INTO knowledge_embeddings(id, notebook_id) VALUES (?, ?)",
        ("ko-private", "nb-private"),
    )
    connection.execute(
        "INSERT INTO kg_objects_fts(object_id, notebook_id, name) VALUES (?, ?, ?)",
        ("ko-private", "nb-private", "private knowledge"),
    )
    connection.execute(
        "INSERT INTO chunks_fts(chunk_id, notebook_id, text) VALUES (?, ?, ?)",
        ("chunk-private", "nb-private", "private chunk"),
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
        assert {row["probe"] for row in evidence["delete_plan"]} == {
            "notebook_delete",
            "knowledge_embeddings_delete",
            "kg_objects_fts_delete",
            "chunks_fts_delete",
        }
        counts = {row["table"]: row["rows"] for row in evidence["notebook_counts"]}
        assert counts["knowledge_embeddings"] == 1
        assert counts["kg_objects_fts"] == 1
        assert counts["chunks_fts"] == 1
        scan_tables = {row["table"] for row in evidence["relevant_scans"]}
        assert {"legacy_children", "kg_objects_fts", "chunks_fts"} <= scan_tables
        assert any(row["table"] == "legacy_children" for row in evidence["notebook_references"])
        assert evidence["mutations_executed"] == 0
        assert len(evidence["largest_tables"]) <= 20
        assert connection.execute("SELECT COUNT(*) FROM notebooks").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
    finally:
        connection.close()


def test_delete_journal_exclusive_lock_honors_short_deadline_without_changes(tmp_path):
    db_path = tmp_path / "locked.db"
    with held_database(db_path, "DELETE"):
        before = database_fingerprints(db_path)
        diag_db = load_diag_db()
        started = time.monotonic()
        evidence = diag_db.collect_db_evidence(
            db_path,
            notebook_id="nb-private",
            deadline_seconds=0.05,
        )
        elapsed = time.monotonic() - started

        assert elapsed < 0.25
        assert evidence["mutations_executed"] == 0
        assert any(row["category"] in {"locked", "busy"} for row in evidence["degraded"])
        assert database_fingerprints(db_path) == before
        encoded = json.dumps(evidence)
        assert "database is locked" not in encoded.lower()
        assert "nb-private" not in encoded


def test_live_wal_analysis_never_changes_source_database_or_sidecars(tmp_path):
    db_path = tmp_path / "live-wal.db"
    with held_database(db_path, "WAL"):
        before = database_fingerprints(db_path)
        assert before["-wal"] is not None
        assert before["-shm"] is not None

        diag_db = load_diag_db()
        evidence = diag_db.collect_db_evidence(db_path, notebook_id="nb-private")

        assert evidence["status"] in {"ok", "degraded"}
        assert evidence["safety"]["source_unchanged"] is True
        assert any(
            row["table"] == "live_children"
            for row in evidence["notebook_references"]
        )
        assert database_fingerprints(db_path) == before
        assert not list((tmp_path / "diagnostics").glob("diag-db-*"))


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
    assert evidence["safety"]["source_unchanged"] is True
    assert not wal_path.exists()
    assert not shm_path.exists()


def test_sidecars_appearing_during_snapshot_discards_evidence(tmp_path, monkeypatch):
    db_path = tmp_path / "sidecars-appear.db"
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("CREATE TABLE notebooks (id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()

    diag_db = load_diag_db()
    original_open = diag_db._open_read_only
    writer = None

    def race_open(*args, **kwargs):
        nonlocal writer
        writer = sqlite3.connect(db_path)
        writer.execute("CREATE TABLE raced_children(notebook_id TEXT REFERENCES notebooks(id))")
        writer.commit()
        return original_open(*args, **kwargs)

    monkeypatch.setattr(diag_db, "_open_read_only", race_open)
    try:
        evidence = diag_db.collect_db_evidence(db_path)
    finally:
        if writer is not None:
            writer.close()

    assert evidence["evidence_complete"] is False
    assert any(row["category"] == "source_changed" for row in evidence["degraded"])
    assert evidence["delete_plan"] == []
    assert evidence["missing_fk_indexes"] == []


def test_sidecars_disappearing_during_snapshot_discards_evidence(tmp_path, monkeypatch):
    db_path = tmp_path / "sidecars-disappear.db"
    writer = create_cascade_database(db_path)
    diag_db = load_diag_db()
    original_open = diag_db._open_read_only

    def race_open(*args, **kwargs):
        writer.close()
        return original_open(*args, **kwargs)

    monkeypatch.setattr(diag_db, "_open_read_only", race_open)
    evidence = diag_db.collect_db_evidence(db_path)

    assert evidence["evidence_complete"] is False
    assert any(row["category"] == "source_changed" for row in evidence["degraded"])
    assert evidence["delete_plan"] == []


def test_committed_fk_change_during_snapshot_discards_stale_evidence(tmp_path, monkeypatch):
    db_path = tmp_path / "schema-race.db"
    writer = create_cascade_database(db_path)
    diag_db = load_diag_db()
    original_open = diag_db._open_read_only

    def race_open(*args, **kwargs):
        writer.execute(
            "CREATE TABLE raced_fk(id TEXT PRIMARY KEY, "
            "notebook_id TEXT REFERENCES notebooks(id) ON DELETE CASCADE)"
        )
        writer.commit()
        return original_open(*args, **kwargs)

    monkeypatch.setattr(diag_db, "_open_read_only", race_open)
    try:
        evidence = diag_db.collect_db_evidence(db_path)
    finally:
        writer.close()

    assert evidence["evidence_complete"] is False
    assert any(row["category"] == "source_changed" for row in evidence["degraded"])
    assert evidence["missing_fk_indexes"] == []


def test_fresh_migrated_schema_compiles_all_notebook_delete_statements(
    tmp_path, monkeypatch
):
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository

    db_path = tmp_path / "fresh.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repository = SQLiteRepository(Settings(_env_file=None))
    with repository._connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] > 0
    repository.close_local()
    diag_db = load_diag_db()

    evidence = diag_db.collect_db_evidence(db_path, notebook_id="nb-private")

    assert {row["probe"] for row in evidence["delete_plan"]} == {
        "notebook_delete",
        "knowledge_embeddings_delete",
        "kg_objects_fts_delete",
        "chunks_fts_delete",
    }
    assert not [row for row in evidence["degraded"] if row["probe"].startswith("plan.")]


def test_schema_identifiers_and_evidence_are_sanitized_and_bounded(tmp_path):
    db_path = tmp_path / "hostile-schema.db"
    hostile = "private\nidentifier_" + "x" * 500
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE notebooks (id TEXT PRIMARY KEY)")
        connection.execute(
            f'CREATE TABLE "{hostile}" ('
            'id TEXT PRIMARY KEY, notebook_id TEXT REFERENCES notebooks(id))'
        )
    diag_db = load_diag_db()

    evidence = diag_db.collect_db_evidence(db_path)
    encoded = json.dumps(evidence)

    assert len(encoded.encode("utf-8")) <= 128 * 1024
    assert "private\\nidentifier" not in encoded
    assert hostile not in encoded
    assert any(row["category"] == "identifier_sanitized" for row in evidence["degraded"])


def test_snapshot_copy_helper_enforces_remaining_aggregate_budget(tmp_path):
    diag_db = load_diag_db()
    source = tmp_path / "source-wal"
    target = tmp_path / "snapshot-wal"
    source.write_bytes(b"123456")

    try:
        diag_db._copy_source_file(
            source,
            target,
            time.monotonic() + 1,
            4,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("copy must reject bytes beyond the remaining aggregate cap")

    assert target.stat().st_size <= 4


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
        "errno",
        "fcntl",
        "hashlib",
        "json",
        "os",
        "pathlib",
        "sqlite3",
        "sys",
        "tempfile",
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
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        statement = node.value.lstrip().upper()
        assert not statement.startswith(
            (
                "INSERT ",
                "UPDATE ",
                "DELETE ",
                "REPLACE ",
                "CREATE ",
                "DROP ",
                "ALTER ",
                "ATTACH ",
                "DETACH ",
            )
        )


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
