#!/usr/bin/env python3
"""Measure the sync_capture_* trigger overhead (docs/incremental-sync-design.md
§7 "开销") on a bulk ``chunks`` insert, for either backend.

    PYTHONPATH=backend python3 scripts/measure_sync_capture_cost.py --backend sqlite
    PYTHONPATH=backend python3 scripts/measure_sync_capture_cost.py --backend postgres \
        --database-url postgresql://user@localhost/some_throwaway_db

Three states, ``--rows`` (default 20000) executemany-inserted into ``chunks``,
``--runs`` (default 3) times each, median reported:

  1. no triggers at all (baseline) -- the three/one ``sync_capture_chunks*``
     triggers are dropped first.
  2. triggers present, capture gate CLOSED -- the state every deployment is
     in until an operator runs ``sync capture enable``.
  3. same triggers, gate OPEN.

Destructive to whatever database it is pointed at: it creates one notebook
and one source row, then repeatedly deletes and re-inserts rows in
``chunks``/``sync_change_log``/``sync_capture_control``. Always point it at a
disposable database (a temp file for SQLite, a throwaway one-time database
for PostgreSQL -- see ``docs/development.md``'s "local PG test db" recipe),
never at a real deployment's data.

This script is NOT run by ``scripts/check.sh`` or CI; it is an operator tool
for re-measuring the cost table in the design doc when the trigger bodies,
the hardware, or the backend version change materially enough that the
existing numbers are no longer trustworthy.
"""
from __future__ import annotations

import argparse
import platform
import statistics
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]

DEFAULT_ROWS = 20_000
DEFAULT_RUNS = 3


def _median(values: list[float]) -> float:
    return statistics.median(values)


def _report(backend_label: str, version_label: str, rows: int, runs: int,
            results: dict[str, list[float]]) -> None:
    print(f"backend: {backend_label} ({version_label})")
    print(f"machine: {platform.platform()} / Python {platform.python_version()}")
    print(f"{rows} rows x {runs} runs")
    baseline = _median(results["no_triggers"])
    for label, values in results.items():
        median = _median(values)
        ratio = f"{median / baseline:.3f}x" if baseline else "n/a"
        rounded = [round(value, 4) for value in values]
        print(f"  {label}: runs={rounded} median={median:.4f}s ({ratio} of baseline)")


def _measure_sqlite(database_path: Path, rows: int, runs: int) -> None:
    import os

    os.environ["DATABASE_URL"] = f"sqlite:///{database_path}"
    os.environ["SILICON_NOTEBOOK_STORAGE_DIR"] = str(database_path.parent / "storage")
    os.environ.setdefault("EVENT_LOG_ENABLED", "false")
    os.environ.setdefault("LLM_LOG_ENABLED", "false")

    sys.path.insert(0, str(ROOT_DIR / "backend"))
    import sqlite3

    from app.core.config import Settings
    from app.migration.sync.capture import sqlite_trigger_sql
    from app.services.sqlite_repository import SQLiteRepository

    settings = Settings()
    repo = SQLiteRepository(settings)
    notebook_id, source_id = "nb-bench", "src-bench"
    now = "2026-01-01T00:00:00.000000+00:00"
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebooks (id, name, status, created_at, updated_at, "
            "created_by) VALUES (?, 'bench', 'active', ?, ?, 'user-local')",
            (notebook_id, now, now),
        )
        db.execute(
            "INSERT INTO sources (id, notebook_id, title, source_type, "
            "file_name, status, created_at, updated_at) VALUES "
            "(?, ?, 'bench', 'text', 'bench.txt', 'ready', ?, ?)",
            (source_id, notebook_id, now, now),
        )

    chunk_triggers = {
        name: sql
        for name, (table, sql) in sqlite_trigger_sql().items()
        if table == "chunks"
    }
    assert len(chunk_triggers) == 3, chunk_triggers.keys()

    def drop_triggers(db: sqlite3.Connection) -> None:
        for name in chunk_triggers:
            db.execute(f'DROP TRIGGER IF EXISTS "{name}"')

    def create_triggers(db: sqlite3.Connection) -> None:
        drop_triggers(db)
        for sql in chunk_triggers.values():
            db.execute(sql)

    def set_gate(db: sqlite3.Connection, enabled: bool) -> None:
        db.execute("DELETE FROM sync_capture_control")
        if enabled:
            db.execute(
                "INSERT INTO sync_capture_control (singleton, enabled, enabled_at) "
                "VALUES (1, 1, ?)",
                (now,),
            )

    def run_once(prefix: str) -> float:
        payload = [
            (f"{prefix}-{i}", notebook_id, source_id, f"chunk text {i}", now)
            for i in range(rows)
        ]
        with repo._write() as db:
            db.execute("DELETE FROM chunks")
            db.execute("DELETE FROM sync_change_log")
            started = time.perf_counter()
            db.executemany(
                "INSERT INTO chunks (id, notebook_id, source_id, text, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                payload,
            )
            elapsed = time.perf_counter() - started
        return elapsed

    results: dict[str, list[float]] = {
        "no_triggers": [], "gate_closed": [], "gate_open": []
    }
    with repo._write() as db:
        drop_triggers(db)
    results["no_triggers"] = [run_once(f"nt{run}") for run in range(runs)]

    with repo._write() as db:
        create_triggers(db)
        set_gate(db, False)
    results["gate_closed"] = [run_once(f"gc{run}") for run in range(runs)]

    with repo._write() as db:
        set_gate(db, True)
    results["gate_open"] = [run_once(f"go{run}") for run in range(runs)]

    repo.close()
    _report("SQLite", f"sqlite3 {sqlite3.sqlite_version}", rows, runs, results)


def _measure_postgres(database_url: str, rows: int, runs: int) -> None:
    import os

    os.environ["DATABASE_URL"] = database_url
    os.environ.setdefault("EVENT_LOG_ENABLED", "false")
    os.environ.setdefault("LLM_LOG_ENABLED", "false")

    sys.path.insert(0, str(ROOT_DIR / "backend"))
    from app.core.config import Settings
    from app.repositories.postgres.database import PostgresDatabase
    from app.repositories.postgres.repository import PostgresRepository

    settings = Settings(
        database_url=database_url,
        postgres_pool_min_size=1,
        postgres_pool_max_size=2,
        postgres_pool_acquire_timeout_seconds=5,
        postgres_statement_timeout_seconds=120,
        postgres_lock_timeout_seconds=30,
    )
    PostgresRepository(settings).close()

    database = PostgresDatabase(settings, ROOT_DIR)
    notebook_id, source_id = "nb-bench", "src-bench"
    now = "2026-01-01T00:00:00+00:00"
    with database.write() as conn:
        conn.execute(
            "INSERT INTO notebooks (id, name, status, created_at, updated_at, "
            "created_by) VALUES (%s, 'bench', 'active', %s, %s, 'user-local')",
            (notebook_id, now, now),
        )
        conn.execute(
            "INSERT INTO sources (id, notebook_id, title, source_type, "
            "file_name, status, created_at, updated_at) VALUES "
            "(%s, %s, 'bench', 'text', 'bench.txt', 'ready', %s, %s)",
            (source_id, notebook_id, now, now),
        )
        version_row = conn.execute("SELECT version()").fetchone()
        server_version = str(version_row["version"])

    def drop_trigger(conn) -> None:
        conn.execute("DROP TRIGGER IF EXISTS sync_capture_chunks ON chunks")

    def create_trigger(conn) -> None:
        drop_trigger(conn)
        conn.execute(
            "CREATE TRIGGER sync_capture_chunks AFTER INSERT OR DELETE OR UPDATE "
            "ON chunks FOR EACH ROW EXECUTE FUNCTION sync_capture_chunks()"
        )

    def set_gate(conn, enabled: bool) -> None:
        conn.execute("DELETE FROM sync_capture_control")
        if enabled:
            conn.execute(
                "INSERT INTO sync_capture_control (singleton, enabled, enabled_at) "
                "VALUES (1, %s, %s)",
                (True, now),
            )

    def run_once(prefix: str) -> float:
        payload = [
            (f"{prefix}-{i}", notebook_id, source_id, f"chunk text {i}", now)
            for i in range(rows)
        ]
        with database.write() as conn:
            conn.execute("DELETE FROM chunks")
            conn.execute("DELETE FROM sync_change_log")
            started = time.perf_counter()
            with conn.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO chunks (id, notebook_id, source_id, text, "
                    "created_at) VALUES (%s, %s, %s, %s, %s)",
                    payload,
                )
            elapsed = time.perf_counter() - started
        return elapsed

    results: dict[str, list[float]] = {
        "no_triggers": [], "gate_closed": [], "gate_open": []
    }
    with database.write() as conn:
        drop_trigger(conn)
    results["no_triggers"] = [run_once(f"nt{run}") for run in range(runs)]

    with database.write() as conn:
        create_trigger(conn)
        set_gate(conn, False)
    results["gate_closed"] = [run_once(f"gc{run}") for run in range(runs)]

    with database.write() as conn:
        set_gate(conn, True)
    results["gate_open"] = [run_once(f"go{run}") for run in range(runs)]

    database.close()
    _report("PostgreSQL", server_version, rows, runs, results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("sqlite", "postgres"), required=True)
    parser.add_argument(
        "--database-path",
        default=None,
        help="sqlite only: where to create the throwaway database file "
        "(default: a tempfile.mkdtemp() directory, removed on exit)",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="postgres only: a throwaway database's connection URL "
        "(required for --backend postgres; never point this at real data)",
    )
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    args = parser.parse_args(argv)

    if args.backend == "sqlite":
        if args.database_path:
            database_path = Path(args.database_path)
            database_path.parent.mkdir(parents=True, exist_ok=True)
            _measure_sqlite(database_path, args.rows, args.runs)
        else:
            import tempfile

            with tempfile.TemporaryDirectory(prefix="sync-capture-cost-") as tmp:
                _measure_sqlite(Path(tmp) / "bench.db", args.rows, args.runs)
        return 0

    if not args.database_url:
        print(
            "--backend postgres requires --database-url pointing at a "
            "throwaway database",
            file=sys.stderr,
        )
        return 2
    _measure_postgres(args.database_url, args.rows, args.runs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
