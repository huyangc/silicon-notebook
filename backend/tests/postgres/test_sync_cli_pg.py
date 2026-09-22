"""守卫: 跨环境笔记本同步 CLI (app.migration.sync.cli) 的 PostgreSQL 泳道。

SQLite 泳道 (tests/test_sync_cli.py) 已经钉住参数解析、退出码与 --json/人读
两种输出。这里只覆盖 ``status`` 按后端会分叉的那一段——PostgreSQL 的
``report_json`` 是 jsonb 列，psycopg 在取回时就已经把它反序列化成 dict（不像
SQLite 那样是存文本、读回来还要 ``json.loads``），``_load_sync_status`` 对两种
形态都要收敛成同一个 JSON 输出。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.config import Settings
from app.migration.sync import cli
from app.migration.sync.export import _Source
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.repository import PostgresRepository


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_sync_cli"),
]


@pytest.fixture
def cli_settings(postgres_scope) -> Settings:
    return Settings(
        database_url=postgres_scope.url,
        postgres_pool_min_size=1,
        postgres_pool_max_size=2,
        postgres_pool_acquire_timeout_seconds=2,
        postgres_statement_timeout_seconds=5,
        postgres_lock_timeout_seconds=2,
    )


@pytest.fixture
def root_dir(tmp_path):
    return tmp_path


def _migrate(settings: Settings) -> None:
    """Bring the isolated test schema up to the current PostgreSQL schema
    (including v81's ``sync_export_state``/``sync_imports``) the same way the
    application does at startup, without seeding any application data."""
    repository = PostgresRepository(settings)
    repository.close()


def test_status_reads_watermark_and_import_row_from_postgres(
    cli_settings, root_dir, capsys
):
    _migrate(cli_settings)
    database = PostgresDatabase(cli_settings, root_dir)
    try:
        with database.write() as conn:
            conn.execute(
                "INSERT INTO sync_export_state "
                "(target_env, exported_through_seq, exported_at, package_id) "
                "VALUES (%s, %s, %s, %s)",
                ("prod-tokyo", 7, "2026-01-01T00:00:00+00:00", "pkg-abc"),
            )
            conn.execute(
                "INSERT INTO sync_imports "
                "(package_id, source_env, from_seq, to_seq, status, started_at, "
                "finished_at, report_json) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
                (
                    "pkg-xyz",
                    "prod-shanghai",
                    0,
                    0,
                    "done",
                    "2026-01-02T00:00:00+00:00",
                    "2026-01-02T00:05:00+00:00",
                    json.dumps({"notebooks": ["nb-1", "nb-2", "nb-3"]}),
                ),
            )
    finally:
        database.close()

    args = cli.build_parser().parse_args(["status", "--json"])
    exit_code = cli._cmd_status(args, cli_settings)
    assert exit_code == 0

    out = capsys.readouterr().out
    payload = json.loads(out)  # must parse cleanly: proves --json is one JSON object

    assert payload["exports"] == [
        {
            "target_env": "prod-tokyo",
            "exported_through_seq": 7,
            "exported_at": "2026-01-01T00:00:00+00:00",
            "package_id": "pkg-abc",
            "captured": False,
            "exported_snapshot": None,
        }
    ]
    assert isinstance(payload["exports"][0]["exported_at"], str)

    assert len(payload["imports"]) == 1
    imported = payload["imports"][0]
    assert imported["package_id"] == "pkg-xyz"
    assert imported["source_env"] == "prod-shanghai"
    assert imported["status"] == "done"
    assert isinstance(imported["started_at"], str)
    assert isinstance(imported["finished_at"], str)
    # psycopg hands jsonb back already-parsed; _load_sync_status must not
    # choke on a dict where the SQLite lane would have seen a text column.
    assert isinstance(imported["report_json"], dict)
    assert imported["report_json"] == {"notebooks": ["nb-1", "nb-2", "nb-3"]}
    assert imported["notebooks"] == 3


def test_status_missing_sync_tables_gives_named_message_on_postgres(
    postgres_scope, capsys
):
    """A schema that was created (by ``postgres_scope``) but never migrated
    has none of the application's tables, including the sync control ones --
    the PostgreSQL analogue of the SQLite "fresh empty file" case."""
    settings = Settings(
        database_url=postgres_scope.url,
        postgres_pool_min_size=1,
        postgres_pool_max_size=2,
        postgres_pool_acquire_timeout_seconds=2,
        postgres_statement_timeout_seconds=5,
        postgres_lock_timeout_seconds=2,
    )
    with pytest.raises(cli.SyncStatusError, match="v83/0063"):
        cli._load_sync_status(settings)


def test_capture_enable_then_disable_round_trips_on_postgres(cli_settings, capsys):
    """SQLite lane (tests/test_sync_cli.py) already covers enable/disable's
    behavior (idempotency, watermark/log clearing) in full; this only pins
    that the same code path works against PostgreSQL's real ``boolean``
    ``enabled`` column and ``timestamptz`` moments through ``_Source``."""
    _migrate(cli_settings)

    args = cli.build_parser().parse_args(["capture", "enable", "--json"])
    exit_code = cli._cmd_capture_enable(args, cli_settings)
    assert exit_code == 0
    enabled = json.loads(capsys.readouterr().out)
    assert enabled["already_enabled"] is False
    assert enabled["enabled_at"] is not None

    status_args = cli.build_parser().parse_args(["capture", "status", "--json"])
    exit_code = cli._cmd_capture_status(status_args, cli_settings)
    assert exit_code == 0
    status = json.loads(capsys.readouterr().out)
    assert status["enabled"] is True

    disable_args = cli.build_parser().parse_args(["capture", "disable", "--json"])
    exit_code = cli._cmd_capture_disable(disable_args, cli_settings)
    assert exit_code == 0
    disabled = json.loads(capsys.readouterr().out)
    assert disabled["already_disabled"] is False
    assert disabled["disabled_at"] is not None

    # And disable is idempotent, same as the SQLite lane pins in full.
    exit_code = cli._cmd_capture_disable(disable_args, cli_settings)
    assert exit_code == 0
    again = json.loads(capsys.readouterr().out)
    assert again["already_disabled"] is True


def test_concurrent_enable_reports_exactly_one_first_time_enable(cli_settings):
    """Two genuinely concurrent ``sync capture enable`` calls, racing on a
    PostgreSQL schema where the control row does not exist yet (the normal
    starting state -- the migration never seeds it): exactly one must
    observe ``already_enabled: False`` (it wrote the row) and the other
    ``already_enabled: True`` (it saw the first one's row already there).

    This is exactly the race a naive "SELECT, then branch, then INSERT ...
    ON CONFLICT" implementation gets wrong when the row does not exist yet:
    ``SELECT ... FOR UPDATE`` has nothing to lock before any row exists, so
    both racers would read "not enabled" and both would report a first-time
    enable, with whichever commits last silently overwriting the other's
    ``enabled_at``. ``_capture_enable`` instead makes the enabled-vs-
    idempotent decision from the single ``INSERT ... ON CONFLICT DO UPDATE
    ... RETURNING enabled_at`` statement itself (see its docstring), which
    PostgreSQL's own row lock on the conflicting key serializes correctly
    regardless of whether the row existed beforehand.
    """
    _migrate(cli_settings)
    root_dir = Path(__file__).resolve().parents[3]

    def _enable() -> dict:
        source = _Source(cli_settings, root_dir)
        try:
            return cli._capture_enable(source)
        finally:
            source.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = [future.result() for future in [
            pool.submit(_enable), pool.submit(_enable)
        ]]

    already_enabled_flags = sorted(
        [first["already_enabled"], second["already_enabled"]]
    )
    assert already_enabled_flags == [False, True], (
        f"exactly one of the two concurrent enables must report a "
        f"first-time transition, got {first!r} and {second!r}"
    )
    # Both must agree on the SAME enabled_at -- the winner's -- proving
    # neither silently clobbered the other's timestamp.
    assert first["enabled_at"] == second["enabled_at"]


# ------------------------------------------------------------- status: captured


def test_status_reports_captured_watermark_and_snapshot_on_postgres(
    cli_settings, root_dir, capsys
):
    """SQLite's lane (tests/test_sync_cli.py) already pins the shape with a
    hand-crafted snapshot string; this is the real thing -- a genuine
    PostgreSQL ``boolean`` column and a snapshot text this deployment's own
    ``pg_current_snapshot()`` grammar produced, round-tripped through
    ``_load_sync_status``."""
    _migrate(cli_settings)
    database = PostgresDatabase(cli_settings, root_dir)
    try:
        with database.write() as conn:
            snapshot = conn.execute(
                "SELECT pg_current_snapshot()::text AS snapshot"
            ).fetchone()["snapshot"]
            conn.execute(
                "INSERT INTO sync_export_state "
                "(target_env, exported_through_seq, exported_at, package_id, "
                "captured, exported_snapshot) VALUES (%s, %s, %s, %s, %s, %s)",
                ("prod-tokyo", 42, "2026-01-01T00:00:00+00:00", "pkg-abc", True, snapshot),
            )
    finally:
        database.close()

    args = cli.build_parser().parse_args(["status", "--json"])
    exit_code = cli._cmd_status(args, cli_settings)
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["exports"] == [
        {
            "target_env": "prod-tokyo",
            "exported_through_seq": 42,
            "exported_at": "2026-01-01T00:00:00+00:00",
            "package_id": "pkg-abc",
            "captured": True,
            "exported_snapshot": snapshot,
        }
    ]

    human_args = cli.build_parser().parse_args(["status"])
    exit_code = cli._cmd_status(human_args, cli_settings)
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "captured=true" in out
    assert f"snapshot xmin={_Source.snapshot_xmin(snapshot)}" in out


# ---------------------------------------------------------------- prune-log


def _insert_log_row(
    database, root_dir, cli_settings, *, seq: int, txid: int, changed_at: datetime
) -> None:
    with database.write() as conn:
        conn.execute(
            "INSERT INTO sync_change_log "
            "(seq, table_name, key_json, operation, txid, changed_at) "
            "VALUES (%s, 'notebooks', '{}'::jsonb, 'upsert', %s, %s)",
            (seq, txid, changed_at),
        )


def test_prune_log_deletes_only_rows_below_both_the_seq_and_txid_bounds(
    cli_settings, root_dir
):
    """A ``captured=1`` watermark with ``exported_snapshot`` xmin=50 and
    ``exported_through_seq=100``: a row with seq<=100 AND txid<50 old enough
    by ``changed_at`` is eligible; a row with seq<=100 but txid>=50 must
    survive -- that is the row that proves the txid condition, not just the
    seq one, is actually applied (docs/incremental-sync-design.md §7 "保留
    策略"; the compensation-window reasoning behind requiring both is §7
    "导出水位"/txid). A row past the seq bound survives regardless."""
    _migrate(cli_settings)
    database = PostgresDatabase(cli_settings, root_dir)
    try:
        with database.write() as conn:
            conn.execute(
                "INSERT INTO sync_export_state "
                "(target_env, exported_through_seq, exported_at, package_id, "
                "captured, exported_snapshot) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    "prod-tokyo",
                    100,
                    "2026-01-01T00:00:00+00:00",
                    "pkg-abc",
                    True,
                    "50:60:",
                ),
            )
        old = datetime.now(timezone.utc) - timedelta(days=40)
        # seq<=100, txid<50, old enough: the only row a correct run deletes.
        _insert_log_row(database, root_dir, cli_settings, seq=10, txid=20, changed_at=old)
        # seq<=100 but txid>=50 (still "in flight" as far as the stored
        # snapshot is concerned): must survive. If the txid condition were
        # dropped this row would be deleted too -- see the mutation check
        # this test's docstring/report references.
        _insert_log_row(database, root_dir, cli_settings, seq=90, txid=55, changed_at=old)
        # seq beyond the watermark: must survive regardless of txid/age.
        _insert_log_row(database, root_dir, cli_settings, seq=200, txid=10, changed_at=old)

        source = _Source(cli_settings, root_dir)
        try:
            result = cli._prune_log(source, keep_days=30, dry_run=False)
        finally:
            source.close()
        assert result["deleted"] == 1
        assert result["min_seq"] == 100
        assert result["min_txid"] == 50

        with database.connect() as conn:
            remaining = {
                row["seq"]
                for row in conn.execute(
                    "SELECT seq FROM sync_change_log"
                ).fetchall()
            }
        assert remaining == {90, 200}
    finally:
        database.close()


def test_prune_log_dry_run_does_not_delete_on_postgres(cli_settings, root_dir):
    _migrate(cli_settings)
    database = PostgresDatabase(cli_settings, root_dir)
    try:
        with database.write() as conn:
            conn.execute(
                "INSERT INTO sync_export_state "
                "(target_env, exported_through_seq, exported_at, package_id, "
                "captured, exported_snapshot) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    "prod-tokyo",
                    100,
                    "2026-01-01T00:00:00+00:00",
                    "pkg-abc",
                    True,
                    "50:60:",
                ),
            )
        old = datetime.now(timezone.utc) - timedelta(days=40)
        _insert_log_row(database, root_dir, cli_settings, seq=10, txid=20, changed_at=old)

        source = _Source(cli_settings, root_dir)
        try:
            result = cli._prune_log(source, keep_days=30, dry_run=True)
        finally:
            source.close()
        assert result["dry_run"] is True
        assert result["would_delete"] == 1
        assert result["deleted"] == 0

        with database.connect() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) AS n FROM sync_change_log"
            ).fetchone()
            assert remaining["n"] == 1
    finally:
        database.close()


# ------------------------------------------------------- v85/0065 column guard


def _drop_export_state_v85_columns(database) -> None:
    """PostgreSQL analogue of the SQLite lane's fixture: simulate a
    v83/0063 or v84/0064 database on a fully-migrated schema by dropping just
    the two v85/0065 columns off ``sync_export_state``."""
    with database.write() as conn:
        conn.execute("ALTER TABLE sync_export_state DROP COLUMN captured")
        conn.execute("ALTER TABLE sync_export_state DROP COLUMN exported_snapshot")


def test_status_degrades_watermark_section_when_v85_columns_are_missing_on_postgres(
    cli_settings, root_dir, capsys
):
    """P1 on PostgreSQL: the column guard reads ``information_schema.columns``
    (the SQLite lane exercises ``PRAGMA table_info`` instead) -- pin that the
    real thing works, not just the SQLite branch."""
    _migrate(cli_settings)
    database = PostgresDatabase(cli_settings, root_dir)
    try:
        with database.write() as conn:
            conn.execute(
                "INSERT INTO sync_export_state "
                "(target_env, exported_through_seq, exported_at, package_id) "
                "VALUES (%s, %s, %s, %s)",
                ("prod-tokyo", 42, "2026-01-01T00:00:00+00:00", "pkg-abc"),
            )
        _drop_export_state_v85_columns(database)

        args = cli.build_parser().parse_args(["status", "--json"])
        exit_code = cli._cmd_status(args, cli_settings)
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["exports"] == [
            {
                "target_env": "prod-tokyo",
                "exported_through_seq": 42,
                "exported_at": "2026-01-01T00:00:00+00:00",
                "package_id": "pkg-abc",
            }
        ]
        assert "captured" not in payload["exports"][0]
        assert "v85/0065" in payload["exports_note"]
    finally:
        database.close()


def test_prune_log_reports_missing_v85_columns_on_postgres(
    cli_settings, root_dir, capsys
):
    _migrate(cli_settings)
    database = PostgresDatabase(cli_settings, root_dir)
    try:
        _drop_export_state_v85_columns(database)

        args = cli.build_parser().parse_args(["prune-log"])
        exit_code = cli._cmd_prune_log(args, cli_settings)
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "v85/0065" in err
        assert "captured" in err
        assert "exported_snapshot" in err
    finally:
        database.close()


# ------------------------------------------------------------- batched delete


def test_prune_log_deletes_in_batches_on_postgres(
    cli_settings, root_dir, monkeypatch, capsys
):
    """P2-3 on PostgreSQL: each batch is its own transaction there too. The
    batch size is monkeypatched down to keep this a fast integration test --
    the SQLite lane already proves the real 5000-row default end to end."""
    monkeypatch.setattr(cli, "_PRUNE_LOG_BATCH_SIZE", 3)
    _migrate(cli_settings)
    database = PostgresDatabase(cli_settings, root_dir)
    try:
        with database.write() as conn:
            conn.execute(
                "INSERT INTO sync_export_state "
                "(target_env, exported_through_seq, exported_at, package_id, "
                "captured, exported_snapshot) VALUES (%s, %s, %s, %s, %s, %s)",
                ("prod-tokyo", 100, "2026-01-01T00:00:00+00:00", "pkg-abc", True, "50:60:"),
            )
        old = datetime.now(timezone.utc) - timedelta(days=40)
        total = 10
        with database.write() as conn:
            for seq in range(1, total + 1):
                conn.execute(
                    "INSERT INTO sync_change_log "
                    "(seq, table_name, key_json, operation, txid, changed_at) "
                    "VALUES (%s, 'notebooks', '{}'::jsonb, 'upsert', %s, %s)",
                    # txid must be BELOW the seeded watermark's xmin (50) --
                    # NULL (the column's default) would fail "txid < 50" as
                    # UNKNOWN and delete nothing, the same trap the txid-bound
                    # test above avoids with an explicit value.
                    (seq, 1, old),
                )

        args = cli.build_parser().parse_args(["prune-log", "--json"])
        exit_code = cli._cmd_prune_log(args, cli_settings)
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["deleted"] == total
        # 10 rows at 3 per batch: 3, 3, 3, 1 -- four batches.
        assert payload["batches"] == 4

        with database.connect() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) AS n FROM sync_change_log"
            ).fetchone()
            assert remaining["n"] == 0
    finally:
        database.close()


# --------------------------------------------------------- export lease (runs)


def _seed_export_lease(
    database,
    *,
    target_env: str = "prod-tokyo",
    run_id: str = "run-1",
    package_id: str = "pkg-inflight",
    floor_seq: int,
    heartbeat_moment: datetime,
) -> None:
    with database.write() as conn:
        conn.execute(
            "INSERT INTO sync_export_runs "
            "(target_env, run_id, package_id, started_at, heartbeat_at, floor_seq) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (target_env, run_id, package_id, heartbeat_moment, heartbeat_moment, floor_seq),
        )


def test_prune_log_active_lease_narrows_the_seq_bound_on_postgres(
    cli_settings, root_dir, capsys
):
    _migrate(cli_settings)
    database = PostgresDatabase(cli_settings, root_dir)
    try:
        with database.write() as conn:
            conn.execute(
                "INSERT INTO sync_export_state "
                "(target_env, exported_through_seq, exported_at, package_id, "
                "captured, exported_snapshot) VALUES (%s, %s, %s, %s, %s, %s)",
                ("prod-tokyo", 100, "2026-01-01T00:00:00+00:00", "pkg-abc", True, "50:60:"),
            )
        _seed_export_lease(
            database, floor_seq=50, heartbeat_moment=datetime.now(timezone.utc)
        )
        old = datetime.now(timezone.utc) - timedelta(days=40)
        _insert_log_row(database, root_dir, cli_settings, seq=40, txid=1, changed_at=old)
        _insert_log_row(database, root_dir, cli_settings, seq=60, txid=1, changed_at=old)

        args = cli.build_parser().parse_args(["prune-log", "--json"])
        exit_code = cli._cmd_prune_log(args, cli_settings)
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["min_seq"] == 50
        assert payload["deleted"] == 1
        assert payload["active_leases"] == [
            {"target_env": "prod-tokyo", "floor_seq": 50}
        ]

        with database.connect() as conn:
            remaining = {
                row["seq"]
                for row in conn.execute("SELECT seq FROM sync_change_log").fetchall()
            }
            assert remaining == {60}
    finally:
        database.close()


def test_status_lists_export_runs_on_postgres(cli_settings, root_dir, capsys):
    _migrate(cli_settings)
    database = PostgresDatabase(cli_settings, root_dir)
    try:
        _seed_export_lease(
            database,
            target_env="prod-tokyo",
            floor_seq=42,
            heartbeat_moment=datetime.now(timezone.utc),
        )
        _seed_export_lease(
            database,
            target_env="prod-osaka",
            run_id="run-dead",
            package_id="pkg-dead",
            floor_seq=7,
            heartbeat_moment=datetime.now(timezone.utc) - timedelta(hours=2),
        )

        args = cli.build_parser().parse_args(["status", "--json"])
        exit_code = cli._cmd_status(args, cli_settings)
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        runs_by_target = {row["target_env"]: row for row in payload["runs"]}
        assert runs_by_target["prod-tokyo"]["floor_seq"] == 42
        assert runs_by_target["prod-tokyo"]["dead"] is False
        assert runs_by_target["prod-osaka"]["floor_seq"] == 7
        assert runs_by_target["prod-osaka"]["dead"] is True
    finally:
        database.close()


def test_prune_log_reports_missing_v85_columns_and_runs_table_together_on_postgres(
    cli_settings, root_dir, capsys
):
    _migrate(cli_settings)
    database = PostgresDatabase(cli_settings, root_dir)
    try:
        with database.write() as conn:
            conn.execute("ALTER TABLE sync_export_state DROP COLUMN captured")
            conn.execute("ALTER TABLE sync_export_state DROP COLUMN exported_snapshot")
            conn.execute("DROP TABLE sync_export_runs")

        args = cli.build_parser().parse_args(["prune-log"])
        exit_code = cli._cmd_prune_log(args, cli_settings)
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "v85/0065" in err
        assert "缺列" in err
        assert "captured" in err
        assert "缺表" in err
        assert "sync_export_runs" in err
    finally:
        database.close()
