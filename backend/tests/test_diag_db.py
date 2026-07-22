from __future__ import annotations

import ast
import errno
import fcntl
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "diag_db.py"
PYTHON = Path(sys.executable)

# 下面两个预算表达的是**活性**（「没有卡死」），不是性能。
#
# 被诊断的对象里有 FIFO、被独占锁住的库、两千个待清理条目这类会让天真实现
# **无限期**阻塞的东西；正确实现要么根本不打开它们（`lstat` 判非普通文件即
# 拒绝），要么带 `O_NONBLOCK` 打开，要么被 deadline 提前掐断。所以「有限时间内
# 返回」就足以证成断言——具体是 0.25 秒还是 5 秒，对结论没有任何影响。
#
# 数字给得宽，是因为紧的数字只会买到抖动：这些用例和整个后端套件并发跑在共享的
# 4 核 runner 上，其中一条还要把一次 Python 解释器冷启动装进预算里。曾经的
# 0.25 秒余量几近于零，在 PR #322 的 full-gate 上随机报红过一次（同一提交重跑
# 即绿）。
#
# 真正承载语义的是各用例里的断言本身——`degraded` 里的类别、`mutations_executed`、
# 文件指纹不变——那些与机器快慢无关。**若要把这两个数字改小，先确认你要防的
# 回归确实能被新数字区分出来**：`diag_db` 内部对单次等待已有 1 秒硬顶
# （`MAX_BUSY_TIMEOUT_MS`、`timeout = min(1.0, remaining)`），所以这里防的是
# 病理性挂死，不是毫秒级性能。
IN_PROCESS_LIVENESS_SECONDS = 5.0
SUBPROCESS_LIVENESS_SECONDS = 10.0


def load_diag_db(*, allow_unsafe_noatime_for_logic_tests=True):
    spec = importlib.util.spec_from_file_location("diag_db", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if allow_unsafe_noatime_for_logic_tests and not hasattr(os, "O_NOATIME"):
        # macOS has no O_NOATIME. Logic tests inject an ordinary descriptor
        # opener; dedicated atime tests exercise the real fail-closed path.
        module._NOATIME_AVAILABLE = True
        module._NOATIME_FLAG = 0
        module._ALLOW_UNSAFE_WORKSPACE_PATH_FOR_TESTS = True
    return module


def file_fingerprint(path: Path):
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        stat = path.stat()
        return {
            "size": stat.st_size,
            "atime_ns": stat.st_atime_ns,
            "mtime_ns": stat.st_mtime_ns,
            "ctime_ns": stat.st_ctime_ns,
            "sha256": digest,
        }
    except FileNotFoundError:
        return None


def database_fingerprints(path: Path):
    return {
        suffix or "database": file_fingerprint(Path(str(path) + suffix))
        for suffix in ("", "-wal", "-shm")
    }


def set_detectable_old_atime(path: Path):
    metadata = path.stat()
    os.utime(
        path,
        ns=(metadata.st_mtime_ns - 7 * 24 * 3600 * 1_000_000_000, metadata.st_mtime_ns),
    )


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
        assert evidence["source_file_plan"]
        assert {
            row["probe"] for row in evidence["source_file_plan"]
        } == {"source_file_select"}
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
    """独占 DELETE 日志锁下，取证要么让开要么被 deadline 掐断，不能卡在锁上。

    时间断言是**活性**断言：证明它没有一直等锁。真正的语义由下面几条承担——
    没有写入、`degraded` 里记了 locked/busy、文件指纹不变、不泄漏库名与原始
    错误串。deadline 传的是 0.05 秒，而预算给到 `IN_PROCESS_LIVENESS_SECONDS`，
    两者差两个数量级是有意为之，理由见文件头。
    """
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

        assert elapsed < IN_PROCESS_LIVENESS_SECONDS
        assert evidence["mutations_executed"] == 0
        assert any(row["category"] in {"locked", "busy"} for row in evidence["degraded"])
        assert database_fingerprints(db_path) == before
        encoded = json.dumps(evidence)
        assert "database is locked" not in encoded.lower()
        assert "nb-private" not in encoded


def test_fifo_wal_is_rejected_without_blocking(tmp_path):
    """WAL 边车是 FIFO 时必须被拒，而不是打开它。

    这是本文件里最纯粹的活性断言：对 FIFO 做阻塞式 `open(O_RDONLY)` 会**永久**
    挂起（没有写端就一直等），所以只要进程有限时间内退出，就证明实现没走阻塞
    读——`lstat` 判非普通文件即拒，加上真要开也带 `O_NONBLOCK`。

    预算用 `SUBPROCESS_LIVENESS_SECONDS` 而不是毫秒级：这条要起真进程，一次
    Python 解释器冷启动就吃掉大部分紧预算，而它对结论毫无贡献。
    """
    db_path = tmp_path / "fifo.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("CREATE TABLE notebooks (id TEXT PRIMARY KEY)")
    wal_path = Path(str(db_path) + "-wal")
    if wal_path.exists():
        wal_path.unlink()
    os.mkfifo(wal_path)

    started = time.monotonic()
    process = subprocess.Popen(
        [str(PYTHON), str(SCRIPT), "--db", str(db_path), "--deadline-seconds", "0.05"],
        cwd=REPO,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={},
    )
    try:
        stdout, stderr = process.communicate(timeout=SUBPROCESS_LIVENESS_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise AssertionError("FIFO sidecar caused a blocking read")

    # 上面的 timeout 已经是这条活性断言的本体；这里再兜一次，覆盖 Popen 自身
    # 耗时（communicate 的计时从 Popen 返回之后才起算）。
    assert time.monotonic() - started < SUBPROCESS_LIVENESS_SECONDS
    assert process.returncode == 0
    assert "unsafe_file" in stdout
    assert stderr == ""


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_symlink_database_or_sidecar_is_rejected(tmp_path, suffix):
    real_db = tmp_path / "real.db"
    with sqlite3.connect(real_db) as connection:
        connection.execute("CREATE TABLE notebooks (id TEXT PRIMARY KEY)")
    db_path = tmp_path / "probe.db"
    if suffix:
        db_path.write_bytes(real_db.read_bytes())
        target = tmp_path / f"target{suffix}"
        target.write_bytes(b"not a source sidecar")
        Path(str(db_path) + suffix).symlink_to(target)
    else:
        db_path.symlink_to(real_db)

    diag_db = load_diag_db(allow_unsafe_noatime_for_logic_tests=False)
    evidence = diag_db.collect_db_evidence(db_path, deadline_seconds=0.05)

    assert evidence["evidence_complete"] is False
    assert any(row["category"] == "unsafe_file" for row in evidence["degraded"])
    assert evidence["delete_plan"] == []


def test_source_atime_mtime_and_hash_are_unchanged(tmp_path):
    db_path = tmp_path / "atime.db"
    with held_database(db_path, "WAL"):
        paths = [db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")]
        before_hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        for path in paths:
            set_detectable_old_atime(path)
        before_stats = {
            path: (path.stat().st_atime_ns, path.stat().st_mtime_ns) for path in paths
        }

        diag_db = load_diag_db(allow_unsafe_noatime_for_logic_tests=False)
        evidence = diag_db.collect_db_evidence(db_path)

        after_stats = {
            path: (path.stat().st_atime_ns, path.stat().st_mtime_ns) for path in paths
        }
        after_hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        assert after_stats == before_stats
        assert after_hashes == before_hashes
        if not hasattr(os, "O_NOATIME"):
            assert any(
                row["category"] == "noatime_unavailable"
                for row in evidence["degraded"]
            )
            assert evidence["delete_plan"] == []


@pytest.mark.parametrize(
    ("failure", "expected_category"),
    (
        (PermissionError(13, "PERMISSION-ERROR-SECRET locked", "PATH-SECRET"), "permission"),
        (OSError("OS-ERROR-SECRET"), "unavailable"),
        (ValueError("VALUE-ERROR-SECRET"), "unavailable"),
    ),
)
def test_initial_source_validation_errors_are_fixed_category_only(
    tmp_path, monkeypatch, failure, expected_category
):
    diag_db = load_diag_db(allow_unsafe_noatime_for_logic_tests=False)

    def fail_validation(_paths):
        raise failure

    monkeypatch.setattr(diag_db, "_validate_source_state", fail_validation)
    evidence = diag_db.collect_db_evidence(tmp_path / "DB-PATH-SECRET.db")

    assert evidence["evidence_complete"] is False
    assert [row["category"] for row in evidence["degraded"]] == [expected_category]
    encoded = json.dumps(evidence, ensure_ascii=False)
    assert "PATH-SECRET" not in encoded
    assert "ERROR-SECRET" not in encoded


def test_rollback_journal_metadata_and_hash_are_unchanged(tmp_path):
    db_path = tmp_path / "journal-atime.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE notebooks (id TEXT PRIMARY KEY)")
    journal = Path(str(db_path) + "-journal")
    journal.write_bytes(b"inert journal sentinel")
    before_hash = hashlib.sha256(journal.read_bytes()).hexdigest()
    set_detectable_old_atime(journal)
    before = (journal.stat().st_atime_ns, journal.stat().st_mtime_ns)

    diag_db = load_diag_db(allow_unsafe_noatime_for_logic_tests=False)
    evidence = diag_db.collect_db_evidence(db_path)

    after = (journal.stat().st_atime_ns, journal.stat().st_mtime_ns)
    after_hash = hashlib.sha256(journal.read_bytes()).hexdigest()
    assert after == before
    assert after_hash == before_hash
    assert evidence["evidence_complete"] is False


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


def test_replace_copy_restore_wal_is_detected_by_pinned_identity(tmp_path, monkeypatch):
    db_path = tmp_path / "wal-swap.db"
    writer = sqlite3.connect(db_path)
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute("CREATE TABLE notebooks(id TEXT PRIMARY KEY)")
    writer.commit()
    wal_path = Path(str(db_path) + "-wal")
    older_wal = wal_path.read_bytes()
    writer.execute(
        "CREATE TABLE newest_children(notebook_id TEXT REFERENCES notebooks(id))"
    )
    writer.commit()

    diag_db = load_diag_db()
    original_copy = diag_db._copy_pinned_file
    swapped = False

    def race_copy(source, *args, **kwargs):
        nonlocal swapped
        if source.name == "wal" and not swapped:
            swapped = True
            saved = tmp_path / "newest.wal"
            os.replace(wal_path, saved)
            wal_path.write_bytes(older_wal)
            try:
                return original_copy(source, *args, **kwargs)
            finally:
                wal_path.unlink()
                os.replace(saved, wal_path)
        return original_copy(source, *args, **kwargs)

    monkeypatch.setattr(diag_db, "_copy_pinned_file", race_copy)
    try:
        evidence = diag_db.collect_db_evidence(db_path)
    finally:
        writer.close()

    assert swapped is True
    assert evidence["evidence_complete"] is False
    assert any(row["category"] == "source_changed" for row in evidence["degraded"])
    assert evidence["delete_plan"] == []


def test_dual_notebook_foreign_keys_count_unique_rows(tmp_path):
    db_path = tmp_path / "dual-fk.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE notebooks(id TEXT PRIMARY KEY);
            CREATE TABLE notebook_bases(
                notebook_id TEXT REFERENCES notebooks(id),
                base_notebook_id TEXT REFERENCES notebooks(id)
            );
            INSERT INTO notebooks VALUES ('nb-private');
            INSERT INTO notebooks VALUES ('nb-other');
            INSERT INTO notebook_bases VALUES ('nb-private', 'nb-other');
            INSERT INTO notebook_bases VALUES ('nb-other', 'nb-private');
            INSERT INTO notebook_bases VALUES ('nb-private', 'nb-private');
            """
        )
    diag_db = load_diag_db()

    evidence = diag_db.collect_db_evidence(db_path, notebook_id="nb-private")
    counts = {row["table"]: row["rows"] for row in evidence["notebook_counts"]}

    assert counts["notebook_bases"] == 3


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


def test_unsafe_diagnostics_symlink_is_never_followed(tmp_path):
    db_path = tmp_path / "diagnostics-link.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE notebooks(id TEXT PRIMARY KEY)")
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "diagnostics").symlink_to(outside, target_is_directory=True)
    diag_db = load_diag_db()

    evidence = diag_db.collect_db_evidence(db_path)

    assert evidence["evidence_complete"] is False
    assert any(row["category"] == "unsafe_diagnostics" for row in evidence["degraded"])
    assert list(outside.iterdir()) == []


def test_db_collector_creates_runtime_compatible_private_diagnostics_parent(tmp_path):
    from app.core.diagnostics_runtime import DiagnosticsRuntime

    db_path = tmp_path / "runtime-compatible.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE notebooks(id TEXT PRIMARY KEY)")
    diag_db = load_diag_db()

    evidence = diag_db.collect_db_evidence(db_path)

    diagnostics = tmp_path / "diagnostics"
    assert evidence["safety"]["snapshot_used"] is True
    assert stat.S_IMODE(diagnostics.stat().st_mode) == 0o700

    runtime = DiagnosticsRuntime(
        diagnostics,
        readiness_provider=lambda: {},
        concurrency_provider=lambda: {},
        interval_seconds=0.01,
        enable_signal=False,
    )
    runtime.start()
    try:
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not (diagnostics / "runtime.json").exists():
            time.sleep(0.01)
        assert (diagnostics / "runtime.json").exists()
    finally:
        runtime.stop()


def test_runtime_created_0755_diagnostics_parent_uses_private_snapshot_root(tmp_path):
    from app.core.diagnostics_runtime import DiagnosticsRuntime

    local = tmp_path / ".local"
    local.mkdir(mode=0o755)
    diagnostics = local / "diagnostics"
    runtime = DiagnosticsRuntime(
        diagnostics,
        readiness_provider=lambda: {"ready": True, "phase": "ready"},
        concurrency_provider=lambda: {},
        interval_seconds=0.01,
        enable_signal=False,
    )
    runtime.start()
    runtime.stop()
    os.chmod(diagnostics, 0o755)
    assert diagnostics.stat().st_mode & 0o777 == 0o755
    db_path = local / "runtime.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE notebooks(id TEXT PRIMARY KEY)")
    diag_db = load_diag_db()

    evidence = diag_db.collect_db_evidence(db_path)

    assert not [
        row for row in evidence["degraded"] if row["category"] == "unsafe_diagnostics"
    ]
    assert evidence["safety"]["snapshot_used"] is True
    assert evidence["safety"]["source_unchanged"] is True
    snapshot_root = diagnostics / "db-snapshots"
    assert snapshot_root.stat().st_mode & 0o777 == 0o700


def test_stale_cleanup_removes_only_owned_known_snapshot_artifacts(tmp_path):
    db_path = tmp_path / "stale-cleanup.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE notebooks(id TEXT PRIMARY KEY)")
    diagnostics = tmp_path / "diagnostics"
    diagnostics.mkdir(mode=0o755)
    snapshot_root = diagnostics / "db-snapshots"
    snapshot_root.mkdir(mode=0o700)
    safe = snapshot_root / "diag-db-safe"
    safe.mkdir(mode=0o700)
    (safe / "snapshot.db").write_bytes(b"old snapshot")
    (safe / ".active.lock").write_bytes(b"")
    hostile = snapshot_root / "diag-db-hostile"
    hostile.mkdir(mode=0o700)
    (hostile / ".active.lock").write_bytes(b"")
    outside = tmp_path / "outside-sentinel"
    outside.write_bytes(b"keep")
    (hostile / "snapshot.db").symlink_to(outside)
    old = time.time_ns() - 2 * 24 * 60 * 60 * 1_000_000_000
    os.utime(safe, ns=(old, old))
    os.utime(hostile, ns=(old, old))
    diag_db = load_diag_db()

    diag_db.collect_db_evidence(db_path)

    assert not safe.exists()
    assert hostile.exists()
    assert (hostile / "snapshot.db").is_symlink()
    assert outside.read_bytes() == b"keep"


def test_stale_cleanup_is_bounded_by_total_entries_before_prefix_filter(tmp_path):
    """陈旧快照清理要在**前缀过滤之前**就按总条目数封顶。

    承载语义的是下面那条 `cleanup_entry_limit` —— 它证明封顶真的生效了，且与
    机器快慢无关。时间断言只是活性兜底：防「两千个条目被逐个走一遍」退化成
    长时间空转。故用宽预算，见文件头。
    """
    db_path = tmp_path / "bounded-cleanup.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE notebooks(id TEXT PRIMARY KEY)")
    snapshot_root = tmp_path / "diagnostics" / "db-snapshots"
    snapshot_root.mkdir(parents=True, mode=0o700)
    os.chmod(tmp_path / "diagnostics", 0o755)
    for index in range(2000):
        (snapshot_root / f"unrelated-{index:04d}").write_bytes(b"")
    diag_db = load_diag_db()
    started = time.monotonic()

    evidence = diag_db.collect_db_evidence(db_path, deadline_seconds=0.05)

    assert time.monotonic() - started < IN_PROCESS_LIVENESS_SECONDS
    assert any(
        row["category"] == "cleanup_entry_limit" for row in evidence["degraded"]
    )


def test_stale_cleanup_skips_locked_active_workspace(tmp_path):
    db_path = tmp_path / "active-cleanup.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE notebooks(id TEXT PRIMARY KEY)")
    workspace = tmp_path / "diagnostics" / "db-snapshots" / "diag-db-active"
    workspace.mkdir(parents=True, mode=0o700)
    os.chmod(tmp_path / "diagnostics", 0o755)
    os.chmod(tmp_path / "diagnostics" / "db-snapshots", 0o700)
    marker = workspace / ".active.lock"
    marker.write_bytes(b"")
    (workspace / "snapshot.db").write_bytes(b"active")
    old = time.time_ns() - 2 * 24 * 60 * 60 * 1_000_000_000
    os.utime(workspace, ns=(old, old))
    descriptor = os.open(marker, os.O_RDWR)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    diag_db = load_diag_db()
    try:
        diag_db.collect_db_evidence(db_path)
        assert workspace.exists()
        assert (workspace / "snapshot.db").read_bytes() == b"active"
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def test_stale_cleanup_skips_hardlinked_artifact_and_reports_degradation(tmp_path):
    db_path = tmp_path / "hardlink-cleanup.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE notebooks(id TEXT PRIMARY KEY)")
    workspace = tmp_path / "diagnostics" / "db-snapshots" / "diag-db-hardlink"
    workspace.mkdir(parents=True, mode=0o700)
    os.chmod(tmp_path / "diagnostics", 0o755)
    os.chmod(tmp_path / "diagnostics" / "db-snapshots", 0o700)
    (workspace / ".active.lock").write_bytes(b"")
    outside = tmp_path / "outside-hardlink"
    outside.write_bytes(b"keep")
    os.link(outside, workspace / "snapshot.db")
    old = time.time_ns() - 2 * 24 * 60 * 60 * 1_000_000_000
    os.utime(workspace, ns=(old, old))
    diag_db = load_diag_db()

    evidence = diag_db.collect_db_evidence(db_path)

    assert workspace.exists()
    assert outside.read_bytes() == b"keep"
    assert any(
        row["category"] == "unsafe_cleanup_entry" for row in evidence["degraded"]
    )


def test_diagnostics_root_replacement_cannot_redirect_snapshot_open(tmp_path, monkeypatch):
    if not Path("/proc/self/fd").is_dir():
        return
    db_path = tmp_path / "root-race.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE notebooks(id TEXT PRIMARY KEY)")
    diagnostics = tmp_path / "diagnostics"
    diagnostics.mkdir(mode=0o755)
    diag_db = load_diag_db()
    original_open = diag_db._open_read_only
    saved = tmp_path / "diagnostics-saved"
    attacked = False

    def replace_root(path, *args, **kwargs):
        nonlocal attacked
        if not attacked:
            attacked = True
            os.replace(diagnostics, saved)
            diagnostics.mkdir(mode=0o755)
            redirected = diagnostics / "redirected.db"
            with sqlite3.connect(redirected) as connection:
                connection.execute("CREATE TABLE redirected_secret(value TEXT)")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(diag_db, "_open_read_only", replace_root)
    try:
        evidence = diag_db.collect_db_evidence(db_path)
    finally:
        shutil.rmtree(diagnostics, ignore_errors=True)
        if saved.exists():
            os.replace(saved, diagnostics)

    assert attacked is True
    assert evidence["evidence_complete"] is False
    assert any(
        row["category"] == "unsafe_diagnostics" for row in evidence["degraded"]
    )
    assert not any(
        row.get("name") == "redirected_secret" for row in evidence["largest_tables"]
    )


def test_source_lock_is_released_before_snapshot_analysis(tmp_path, monkeypatch):
    db_path = tmp_path / "early-unlock.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE notebooks(id TEXT PRIMARY KEY)")
    diag_db = load_diag_db()
    original_open = diag_db._open_read_only
    writer_result = None

    def probe_open(*args, **kwargs):
        nonlocal writer_result
        code = (
            "import sqlite3,sys;"
            "c=sqlite3.connect(sys.argv[1],timeout=.1);"
            "c.execute('BEGIN EXCLUSIVE');"
            "c.rollback();c.close();print('acquired')"
        )
        writer_result = subprocess.run(
            [str(PYTHON), "-c", code, str(db_path)],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
        return original_open(*args, **kwargs)

    monkeypatch.setattr(diag_db, "_open_read_only", probe_open)
    evidence = diag_db.collect_db_evidence(db_path)

    assert writer_result is not None
    assert writer_result.returncode == 0, writer_result.stderr
    assert writer_result.stdout.strip() == "acquired"
    assert evidence["mutations_executed"] == 0


def test_source_fstat_failure_closes_new_descriptor(tmp_path, monkeypatch):
    db_path = tmp_path / "fstat-failure.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE notebooks(id TEXT PRIMARY KEY)")
    diag_db = load_diag_db()
    original_open = diag_db.os.open
    original_fstat = diag_db.os.fstat
    opened = None

    def capture_open(path, *args, **kwargs):
        nonlocal opened
        descriptor = original_open(path, *args, **kwargs)
        if os.fspath(path) == os.fspath(db_path):
            opened = descriptor
        return descriptor

    def fail_fstat(descriptor):
        if descriptor == opened:
            raise OSError(errno.EIO, "injected")
        return original_fstat(descriptor)

    monkeypatch.setattr(diag_db.os, "open", capture_open)
    monkeypatch.setattr(diag_db.os, "fstat", fail_fstat)
    evidence = diag_db.collect_db_evidence(db_path)
    monkeypatch.setattr(diag_db.os, "fstat", original_fstat)

    assert evidence["evidence_complete"] is False
    with pytest.raises(OSError) as exc_info:
        original_fstat(opened)
    assert exc_info.value.errno == errno.EBADF


def test_final_unlock_and_close_failures_are_structured(tmp_path, monkeypatch):
    db_path = tmp_path / "cleanup-failure.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE notebooks(id TEXT PRIMARY KEY)")
    diag_db = load_diag_db()
    original_lockf = diag_db.fcntl.lockf
    original_close = diag_db.os.close
    database_descriptor = None
    original_pin = diag_db._pin_source_files

    def capture_pin(*args, **kwargs):
        nonlocal database_descriptor
        pinned = original_pin(*args, **kwargs)
        database_descriptor = pinned["database"].descriptor
        return pinned

    def fail_unlock(descriptor, operation, *args, **kwargs):
        if descriptor == database_descriptor and operation == fcntl.LOCK_UN:
            raise OSError(errno.EIO, "injected unlock")
        return original_lockf(descriptor, operation, *args, **kwargs)

    close_failed = False

    def fail_close(descriptor):
        nonlocal close_failed
        if descriptor == database_descriptor and not close_failed:
            close_failed = True
            raise OSError(errno.EIO, "injected close")
        return original_close(descriptor)

    monkeypatch.setattr(diag_db, "_pin_source_files", capture_pin)
    monkeypatch.setattr(diag_db.fcntl, "lockf", fail_unlock)
    monkeypatch.setattr(diag_db.os, "close", fail_close)
    evidence = diag_db.collect_db_evidence(db_path)
    if database_descriptor is not None:
        try:
            original_lockf(
                database_descriptor,
                fcntl.LOCK_UN,
                1,
                diag_db._SHARED_FIRST,
                os.SEEK_SET,
            )
        finally:
            original_close(database_descriptor)

    assert evidence["evidence_complete"] is False
    assert any(row["category"] == "cleanup_error" for row in evidence["degraded"])


@pytest.mark.parametrize(
    "unsupported_errno",
    sorted({errno.EINVAL, getattr(errno, "EOPNOTSUPP", errno.EINVAL)}),
)
def test_unsupported_noatime_open_degrades_before_read(tmp_path, monkeypatch, unsupported_errno):
    db_path = tmp_path / "noatime-unsupported.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE notebooks(id TEXT PRIMARY KEY)")
    diag_db = load_diag_db()
    diag_db._NOATIME_AVAILABLE = True
    diag_db._NOATIME_FLAG = 0x40000000
    original_open = diag_db.os.open

    def reject_noatime(path, flags, *args, **kwargs):
        if os.fspath(path) == os.fspath(db_path):
            raise OSError(unsupported_errno, "unsupported")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(diag_db.os, "open", reject_noatime)
    evidence = diag_db.collect_db_evidence(db_path)

    assert any(
        row["category"] == "noatime_unavailable" for row in evidence["degraded"]
    )
    assert evidence["safety"]["snapshot_used"] is False


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


def test_missing_index_and_reference_caps_report_truncation(tmp_path, monkeypatch):
    db_path = tmp_path / "many-fks.db"
    diag_db = load_diag_db()
    monkeypatch.setattr(diag_db, "MAX_MISSING_INDEXES", 2)
    monkeypatch.setattr(diag_db, "MAX_NOTEBOOK_REFERENCES", 2)
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE notebooks(id TEXT PRIMARY KEY)")
        for index in range(4):
            connection.execute(
                f"CREATE TABLE child_{index}(notebook_id TEXT REFERENCES notebooks(id))"
            )

    evidence = diag_db.collect_db_evidence(db_path)
    categories = {row["category"] for row in evidence["degraded"]}

    assert "missing_index_limit" in categories
    assert "notebook_reference_limit" in categories


def test_snapshot_copy_helper_enforces_remaining_aggregate_budget(tmp_path):
    diag_db = load_diag_db()
    source = tmp_path / "source-wal"
    target = tmp_path / "snapshot-wal"
    source.write_bytes(b"123456")

    descriptor = os.open(source, os.O_RDONLY)
    try:
        pinned = diag_db._PinnedSource(
            "wal", source, descriptor, diag_db._descriptor_identity(descriptor)
        )
        with target.open("xb") as writer:
            with pytest.raises(ValueError):
                diag_db._copy_pinned_file(
                    pinned,
                    writer,
                    time.monotonic() + 1,
                    4,
                )
    finally:
        os.close(descriptor)

    assert target.stat().st_size <= 4


def test_open_read_only_closes_connection_when_post_connect_initialization_raises(
    tmp_path, monkeypatch
):
    diag_db = load_diag_db()

    class InitializationFailure(BaseException):
        pass

    class FakeConnection:
        row_factory = None
        closed = False

        def setlimit(self, *_args):
            raise InitializationFailure("post-connect failure")

        def close(self):
            self.closed = True

    connection = FakeConnection()
    monkeypatch.setattr(diag_db.sqlite3, "connect", lambda *_args, **_kwargs: connection)

    with pytest.raises(InitializationFailure, match="post-connect failure"):
        diag_db._open_read_only(
            tmp_path / "snapshot.db", time.monotonic() + 1
        )

    assert connection.closed is True


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


def test_script_uses_only_stdlib_and_the_stdlib_only_shared_boundary_and_no_mutating_sql():
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
        "diag_common",
        "errno",
        "fcntl",
        "hashlib",
        "json",
        "os",
        "pathlib",
        "sqlite3",
        "stat",
        "sys",
        "tempfile",
        "time",
        "typing",
        "urllib",
    }
    assert "app" not in imports

    common_tree = ast.parse((SCRIPT.parent / "diag_common.py").read_text(encoding="utf-8"))
    assert all(
        not (
            isinstance(node, ast.Import)
            and any(alias.name == "app" or alias.name.startswith("app.") for alias in node.names)
        )
        and not (
            isinstance(node, ast.ImportFrom)
            and (node.module == "app" or (node.module or "").startswith("app."))
        )
        for node in ast.walk(common_tree)
    )

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
