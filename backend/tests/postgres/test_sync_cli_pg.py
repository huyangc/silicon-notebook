"""守卫: 跨环境笔记本同步 CLI (app.migration.sync.cli) 的 PostgreSQL 泳道。

SQLite 泳道 (tests/test_sync_cli.py) 已经钉住参数解析、退出码与 --json/人读
两种输出。这里只覆盖 ``status`` 按后端会分叉的那一段——PostgreSQL 的
``report_json`` 是 jsonb 列，psycopg 在取回时就已经把它反序列化成 dict（不像
SQLite 那样是存文本、读回来还要 ``json.loads``），``_load_sync_status`` 对两种
形态都要收敛成同一个 JSON 输出。
"""

from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.migration.sync import cli
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
