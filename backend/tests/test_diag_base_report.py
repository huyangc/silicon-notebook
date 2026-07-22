from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
BASE_SCRIPT = SCRIPTS / "diag_base_report.py"
DB_SCRIPT = SCRIPTS / "diag_db.py"


def _load(name: str, path: Path):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_diag_db_for_logic():
    module = _load("diag_db_base_report_test", DB_SCRIPT)
    if not hasattr(os, "O_NOATIME"):
        module._NOATIME_AVAILABLE = True
        module._NOATIME_FLAG = 0
        module._ALLOW_UNSAFE_WORKSPACE_PATH_FOR_TESTS = True
    return module


def _fingerprint(path: Path):
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOATIME", 0)
        descriptor = os.open(path, flags)
        digest = hashlib.sha256()
        try:
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    break
                digest.update(block)
        finally:
            os.close(descriptor)
        metadata = path.stat()
        return {
            "sha256": digest.hexdigest(),
            "size": metadata.st_size,
            "atime_ns": metadata.st_atime_ns,
            "mtime_ns": metadata.st_mtime_ns,
            "ctime_ns": metadata.st_ctime_ns,
        }
    except FileNotFoundError:
        return None


def _source_fingerprints(path: Path):
    return {
        suffix or "database": _fingerprint(Path(str(path) + suffix))
        for suffix in ("", "-wal", "-shm", "-journal")
    }


def _create_base_recall_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(
        """
        CREATE TABLE notebooks (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          tier TEXT NOT NULL,
          created_by TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'ready'
        );
        CREATE TABLE notebook_bases (
          notebook_id TEXT NOT NULL,
          base_notebook_id TEXT NOT NULL,
          created_at TEXT NOT NULL,
          created_by TEXT,
          PRIMARY KEY (notebook_id, base_notebook_id)
        );
        CREATE TABLE knowledge_objects (
          id TEXT PRIMARY KEY,
          notebook_id TEXT NOT NULL,
          status TEXT NOT NULL,
          payload TEXT NOT NULL
        );
        CREATE TABLE knowledge_embeddings (
          object_id TEXT PRIMARY KEY,
          notebook_id TEXT NOT NULL,
          vector BLOB,
          created_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE chunks (
          id TEXT PRIMARY KEY,
          notebook_id TEXT NOT NULL,
          text TEXT NOT NULL
        );
        CREATE TABLE knowledge_relations (
          id TEXT PRIMARY KEY,
          notebook_id TEXT NOT NULL
        );
        CREATE TABLE reports (
          id TEXT PRIMARY KEY,
          notebook_id TEXT NOT NULL,
          question TEXT NOT NULL,
          references_json TEXT NOT NULL,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """
    )
    notebooks = (
        ("nb-active-SECRET", "TITLE-SECRET-ACTIVE", "personal", "user-SECRET-A", "ready"),
        ("nb-base-SECRET", "TITLE-SECRET-BASE", "base", "user-SECRET-B", "ready"),
        ("nb-private-SECRET", "TITLE-SECRET-PRIVATE", "personal", "user-SECRET-C", "ready"),
        ("nb-copying-SECRET", "TITLE-SECRET-COPY", "base", "user-SECRET-D", "copying"),
    )
    connection.executemany(
        "INSERT INTO notebooks(id,name,tier,created_by,status) VALUES (?,?,?,?,?)",
        notebooks,
    )
    connection.executemany(
        "INSERT INTO notebook_bases(notebook_id,base_notebook_id,created_at,created_by) "
        "VALUES (?,?,?,?)",
        (
            ("nb-active-SECRET", "nb-base-SECRET", "2026-07-21", "user-SECRET-A"),
            ("nb-active-SECRET", "nb-private-SECRET", "2026-07-21", "user-SECRET-A"),
            ("nb-active-SECRET", "nb-copying-SECRET", "2026-07-21", "user-SECRET-A"),
        ),
    )
    connection.executemany(
        "INSERT INTO knowledge_objects(id,notebook_id,status,payload) VALUES (?,?,?,?)",
        (
            ("ko-base-SECRET", "nb-base-SECRET", "approved", '{"name":"CONTENT-SECRET"}'),
            ("ko-private-SECRET", "nb-private-SECRET", "approved", '{"name":"PRIVATE-CONTENT-SECRET"}'),
        ),
    )
    connection.execute(
        "INSERT INTO knowledge_embeddings(object_id,notebook_id,vector) VALUES (?,?,?)",
        ("ko-base-SECRET", "nb-base-SECRET", b"VECTOR-SECRET"),
    )
    connection.execute(
        "INSERT INTO chunks(id,notebook_id,text) VALUES (?,?,?)",
        ("chunk-base-SECRET", "nb-base-SECRET", "CHUNK-CONTENT-SECRET"),
    )
    connection.execute(
        "INSERT INTO knowledge_relations(id,notebook_id) VALUES (?,?)",
        ("rel-base-SECRET", "nb-base-SECRET"),
    )
    references = [
        {
            "notebook_id": "nb-base-SECRET",
            "tier": "base",
            "source_title": "SOURCE-TITLE-SECRET",
            "label": "LABEL-SECRET",
            "object_id": "ko-base-SECRET",
        },
        {"notebook_id": "nb-active-SECRET", "tier": "personal"},
        {"notebook_id": "nb-unmounted-SECRET", "tier": "base"},
    ]
    connection.execute(
        "INSERT INTO reports(id,notebook_id,question,references_json,status,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            "report-SECRET",
            "nb-active-SECRET",
            "QUESTION-SECRET",
            json.dumps(references),
            "done",
            "2026-07-21T00:00:00+00:00",
        ),
    )
    connection.execute("PRAGMA user_version=22")
    connection.commit()
    return connection


def test_base_recall_engine_has_no_application_or_repository_constructor_import():
    source = BASE_SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(BASE_SCRIPT))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not any(name == "app" or name.startswith("app.") for name in imported)
    assert "SQLiteRepository" not in source
    assert "Settings(" not in source


def test_snapshot_projection_returns_only_bounded_pseudonymized_metadata(tmp_path):
    db = tmp_path / "base-recall.db"
    connection = _create_base_recall_database(db)
    connection.close()
    diag_db = _load_diag_db_for_logic()

    evidence = diag_db.collect_db_evidence(
        db,
        notebook_id="nb-active-SECRET",
        deadline_seconds=2,
        projection="base-recall",
    )

    recall = evidence["base_recall"]
    assert recall["notebook_count"] == 4
    assert recall["public_base_count"] == 2
    assert recall["selected"] is True
    assert recall["selection"] == "operator"
    assert recall["notebook"] == "notebook#1"
    assert recall["mounts_total"] == 3
    assert recall["mounts_active"] == 1
    assert recall["mounts_inactive"] == 2
    assert recall["mounted_bases"] == [
        {
            "notebook": "base#1",
            "tier": "base",
            "active": True,
            "inactive_reason": "",
            "kg_objects": 1,
            "embeddings": 1,
            "chunks": 1,
            "relations": 1,
        },
        {
            "notebook": "base#2",
            "tier": "base",
            "active": False,
            "inactive_reason": "copying",
            "kg_objects": 0,
            "embeddings": 0,
            "chunks": 0,
            "relations": 0,
        },
        {
            "notebook": "base#3",
            "tier": "personal",
            "active": False,
            "inactive_reason": "not_authorized",
            "kg_objects": 1,
            "embeddings": 0,
            "chunks": 0,
            "relations": 0,
        },
    ]
    assert recall["latest_report"] == {
        "exists": True,
        "references_total": 3,
        "base_tier_references": 2,
        "personal_tier_references": 1,
        "mounted_base_references": 1,
        "malformed_references": 0,
    }
    encoded = json.dumps(evidence, ensure_ascii=False)
    for secret in (
        "nb-active-SECRET",
        "nb-base-SECRET",
        "user-SECRET",
        "TITLE-SECRET",
        "QUESTION-SECRET",
        "CONTENT-SECRET",
        "SOURCE-TITLE-SECRET",
        "LABEL-SECRET",
        "report-SECRET",
        "ko-base-SECRET",
        "chunk-base-SECRET",
        "rel-base-SECRET",
    ):
        assert secret not in encoded
    assert len(encoded.encode("utf-8")) <= 128 * 1024


def test_report_is_one_bounded_utf8_metadata_block_and_omits_all_secrets():
    module = _load("diag_base_report_render_test", BASE_SCRIPT)
    evidence = {
        "status": "ok",
        "evidence_complete": True,
        "base_recall": {
            "selected": True,
            "selection": "operator",
            "notebook": "notebook#1",
            "notebook_count": 999999,
            "public_base_count": 999999,
            "mounts_total": 5000,
            "mounts_active": 5000,
            "mounts_inactive": 0,
            "active_kg_objects": 1,
            "active_embeddings": 1,
            "mounted_bases": [
                {
                    "notebook": f"base#{index}",
                    "tier": "base",
                    "active": True,
                    "inactive_reason": "",
                    "kg_objects": index,
                    "embeddings": index,
                    "chunks": index,
                    "relations": index,
                }
                for index in range(5000)
            ],
            "latest_report": {
                "exists": True,
                "references_total": 1,
                "base_tier_references": 1,
                "personal_tier_references": 0,
                "mounted_base_references": 1,
                "malformed_references": 0,
            },
        },
        "degraded": [
            {"probe": "SECRET-PROBE", "category": "SECRET-ERROR", "exception": "SECRET-EXCEPTION"}
        ],
        "unused": {
            "question": "QUESTION-SECRET",
            "filename": "/private/FILE-SECRET.pdf",
            "authorization": "Bearer AUTH-SECRET",
        },
    }

    report = module.render_base_recall_report(evidence)

    report.encode("utf-8", "strict")
    assert report.endswith("\n")
    assert len(report.encode("utf-8")) <= 32 * 1024
    assert report.count("silicon-notebook base-recall metadata report") == 1
    assert "notebook#1" in report
    assert "QUESTION-SECRET" not in report
    assert "FILE-SECRET" not in report
    assert "AUTH-SECRET" not in report
    assert "SECRET-PROBE" not in report
    assert "SECRET-ERROR" not in report
    assert "SECRET-EXCEPTION" not in report


def test_fresh_old_schema_and_migration_sentinel_are_never_changed(tmp_path):
    db = tmp_path / "old-schema.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE migration_sentinel(value TEXT NOT NULL)")
        connection.execute("INSERT INTO migration_sentinel(value) VALUES ('DATA-SECRET')")
        connection.execute("PRAGMA user_version=1")
        connection.commit()
    with sqlite3.connect(db) as connection:
        schema_before = connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
        version_before = connection.execute("PRAGMA user_version").fetchone()[0]
        sentinel_before = connection.execute("SELECT value FROM migration_sentinel").fetchall()
    metadata = db.stat()
    os.utime(
        db,
        ns=(metadata.st_mtime_ns - 7 * 24 * 3600 * 1_000_000_000, metadata.st_mtime_ns),
    )
    before = _source_fingerprints(db)

    result = subprocess.run(
        [
            sys.executable,
            str(BASE_SCRIPT),
            "--db",
            str(db),
            "--notebook-id",
            "nb-operator-SECRET",
            "--deadline-seconds",
            "1",
        ],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert len(result.stdout.encode("utf-8")) <= 32 * 1024
    assert "DATA-SECRET" not in result.stdout + result.stderr
    after = _source_fingerprints(db)
    assert after == before
    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == version_before
        assert connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall() == schema_before
        assert connection.execute("SELECT value FROM migration_sentinel").fetchall() == sentinel_before


def test_linux_live_wal_sidecars_and_atime_are_unchanged(tmp_path):
    if not hasattr(os, "O_NOATIME") or not Path("/proc/self/fd").is_dir():
        return
    db = tmp_path / "live.db"
    connection = _create_base_recall_database(db)
    connection.execute("INSERT INTO chunks(id,notebook_id,text) VALUES (?,?,?)", (
        "chunk-live-SECRET", "nb-active-SECRET", "LIVE-CONTENT-SECRET"
    ))
    connection.commit()
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(db) + suffix)
        metadata = path.stat()
        os.utime(
            path,
            ns=(metadata.st_mtime_ns - 7 * 24 * 3600 * 1_000_000_000, metadata.st_mtime_ns),
        )
    before = _source_fingerprints(db)

    result = subprocess.run(
        [sys.executable, str(BASE_SCRIPT), "--db", str(db), "--notebook-id", "nb-active-SECRET"],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert _source_fingerprints(db) == before
    assert "SECRET" not in result.stdout + result.stderr
    connection.close()


def test_missing_noatime_and_deadline_degrade_to_bounded_category_only_output(tmp_path):
    base = _load("diag_base_report_degraded_test", BASE_SCRIPT)
    diag_db = _load("diag_db_base_report_degraded_test", DB_SCRIPT)
    missing = diag_db.collect_db_evidence(
        tmp_path / "missing.db", projection="base-recall", deadline_seconds=0.01
    )
    missing_report = base.render_base_recall_report(missing)
    assert "category=missing" in missing_report

    db = tmp_path / "noatime.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE sentinel(value TEXT)")
    diag_db._NOATIME_AVAILABLE = False
    noatime = diag_db.collect_db_evidence(
        db, projection="base-recall", deadline_seconds=1
    )
    noatime_report = base.render_base_recall_report(noatime)
    assert "category=noatime_unavailable" in noatime_report

    deadline = diag_db.collect_db_evidence(
        db, projection="base-recall", deadline_seconds=0
    )
    deadline_report = base.render_base_recall_report(deadline)
    assert "category=" in deadline_report
    for report in (missing_report, noatime_report, deadline_report):
        assert report.endswith("\n")
        assert len(report.encode("utf-8")) <= 32 * 1024
        assert str(tmp_path) not in report


def test_base_recall_cli_validation_permission_is_bounded_category_only(tmp_path):
    secret_path = tmp_path / "PATH-SECRET.db"
    code = f"""
import sys
sys.path.insert(0, {str(SCRIPTS)!r})
import diag_db

def deny_source_validation(_paths):
    raise PermissionError(13, "ERROR-SECRET locked", {str(secret_path)!r})

diag_db._validate_source_state = deny_source_validation
import diag_base_report
raise SystemExit(diag_base_report.main(["--db", {str(secret_path)!r}]))
"""

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 0
    result.stdout.encode("utf-8", "strict")
    assert result.stdout.endswith("\n")
    assert len(result.stdout.encode("utf-8")) <= 32 * 1024
    assert "category=permission" in result.stdout
    combined = result.stdout + result.stderr
    assert "Traceback" not in combined
    assert "PATH-SECRET" not in combined
    assert "ERROR-SECRET" not in combined


def test_db_cli_uses_same_safe_initial_validation_boundary(tmp_path):
    secret_path = tmp_path / "DB-PATH-SECRET.db"
    code = f"""
import sys
sys.path.insert(0, {str(SCRIPTS)!r})
import diag_db

def deny_source_validation(_paths):
    raise PermissionError(13, "DB-ERROR-SECRET locked", {str(secret_path)!r})

diag_db._validate_source_state = deny_source_validation
raise SystemExit(diag_db.main(["--db", {str(secret_path)!r}]))
"""

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 0
    result.stdout.encode("utf-8", "strict")
    assert result.stdout.endswith("\n")
    assert len(result.stdout.encode("utf-8")) <= 32 * 1024
    assert "category=permission" in result.stdout
    combined = result.stdout + result.stderr
    assert "Traceback" not in combined
    assert "DB-PATH-SECRET" not in combined
    assert "DB-ERROR-SECRET" not in combined
