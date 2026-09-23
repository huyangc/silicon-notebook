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


def test_status_reports_chain_head_and_pending_notebook_deletes_on_postgres(
    cli_settings, root_dir, capsys
):
    """PR-3c fields (design doc §8), PostgreSQL lane: ``report_json`` is
    jsonb here (psycopg hands it back already-parsed, unlike SQLite's text
    column), and ``_chain_head``/``_pending_notebook_deletes`` must read the
    same shape off it either way -- pin one chain (a full baseline plus one
    incremental window continuing from it) and one grouped delete count
    against the real PostgreSQL round trip, mirroring the SQLite-lane
    coverage in tests/test_sync_cli.py."""
    _migrate(cli_settings)
    database = PostgresDatabase(cli_settings, root_dir)
    try:
        with database.write() as conn:
            conn.execute(
                "INSERT INTO sync_imports "
                "(package_id, source_env, from_seq, to_seq, status, started_at, "
                "finished_at, report_json) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
                (
                    "pkg-A",
                    "prod-shanghai",
                    0,
                    10,
                    "done",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:05:00+00:00",
                    json.dumps(
                        {
                            "notebooks": ["nb-1"],
                            "mode": "full",
                            "base_package_id": "",
                            "package_created_at": "2026-01-01T00:00:00+00:00",
                        }
                    ),
                ),
            )
            conn.execute(
                "INSERT INTO sync_imports "
                "(package_id, source_env, from_seq, to_seq, status, started_at, "
                "finished_at, report_json) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
                (
                    "pkg-B",
                    "prod-shanghai",
                    11,
                    20,
                    "done",
                    "2026-01-02T00:00:00+00:00",
                    "2026-01-02T00:05:00+00:00",
                    json.dumps(
                        {
                            "notebooks": ["nb-1"],
                            "mode": "incremental",
                            "base_package_id": "pkg-A",
                            "package_created_at": "2026-01-02T00:00:00+00:00",
                        }
                    ),
                ),
            )
            conn.execute(
                "INSERT INTO notebooks"
                "(id, name, purpose, primary_domain, status, created_by, "
                "created_at, updated_at, sync_origin) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    "nb-sh-1",
                    "mirror",
                    "",
                    "",
                    "deleting",
                    None,
                    "2026-01-03T00:00:00+00:00",
                    "2026-01-03T00:00:00+00:00",
                    "prod-shanghai",
                ),
            )
    finally:
        database.close()

    args = cli.build_parser().parse_args(["status", "--json"])
    exit_code = cli._cmd_status(args, cli_settings)
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["chain_heads"] == {
        "prod-shanghai": [
            {
                "package_id": "pkg-B",
                "to_seq": 20,
                "created_at": "2026-01-02T00:00:00+00:00",
            }
        ]
    }
    assert payload["pending_notebook_deletes"] == {"prod-shanghai": 1}

    human_args = cli.build_parser().parse_args(["status"])
    exit_code = cli._cmd_status(human_args, cli_settings)
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "pkg-B（to_seq=20，创建于 2026-01-02T00:00:00+00:00）" in out
    assert "等待删除作业清理的镜像 1 个（由目标端应用的删除作业完成）" in out


def _insert_import_row_pg(
    conn,
    *,
    package_id: str,
    source_env: str,
    created_at: str,
    to_seq: int = 0,
    from_seq: int = 0,
    base_package_id: str = "",
    mode: str = "full",
    scoped: bool = False,
    notebooks: tuple[str, ...] = (),
    started_at: str | None = None,
) -> None:
    """一行 ``sync_imports``。``scoped=True`` 强制 ``from_seq``/``to_seq`` 归
    0——``export.py`` 对子集包硬写的就是 0/0，``import_.py`` 原样写进表里，所以
    带非零区间的 scoped 行在真库里不存在，用例也不许造出来。``started_at``
    默认等于 ``created_at``，显式传入是为了钉住判据不看目标端导入先后。"""
    if scoped:
        from_seq = 0
        to_seq = 0
    conn.execute(
        "INSERT INTO sync_imports "
        "(package_id, source_env, from_seq, to_seq, status, started_at, "
        "finished_at, report_json) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
        (
            package_id,
            source_env,
            from_seq,
            to_seq,
            "done",
            started_at or created_at,
            started_at or created_at,
            json.dumps(
                {
                    "notebooks": list(notebooks),
                    "mode": mode,
                    "base_package_id": base_package_id,
                    "scoped": scoped,
                    "package_created_at": created_at,
                }
            ),
        ),
    )


def _insert_mirror_pg(
    conn, notebook_id: str, sync_origin: str, *, status: str = "draft"
) -> None:
    conn.execute(
        "INSERT INTO notebooks"
        "(id, name, purpose, primary_domain, status, created_by, "
        "created_at, updated_at, sync_origin) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            notebook_id,
            notebook_id,
            "",
            "",
            status,
            None,
            "2026-01-03T00:00:00+00:00",
            "2026-01-03T00:00:00+00:00",
            sync_origin,
        ),
    )


def test_status_surveys_mirrors_against_the_chain_head_on_postgres(
    cli_settings, root_dir, capsys
):
    """镜像落后巡检（设计文档 §9）的 PostgreSQL 泳道：判据全部从 jsonb 的
    ``report_json`` 推导（没有 ``sync_applied_through_seq`` 列，也不比
    ``to_seq``——scoped 行的区间在库里恒为 0/0），而 ``notebooks`` 在这里是
    psycopg 已经解析好的 list、SQLite 那边是文本——两种形态必须收敛成同一份
    结论。形态是：全量 → 一份一月的 scoped 快照（六月才导入）→ 一个 01-03 的
    窗口（当天导入，带了这三本的改动）；于是那份旧快照盖掉了窗口，nb-scoped
    真落后。覆盖 lagging 与 deleting 两个桶，与 tests/test_sync_cli.py 的同款
    用例对应。"""
    _migrate(cli_settings)
    database = PostgresDatabase(cli_settings, root_dir)
    try:
        with database.write() as conn:
            _insert_import_row_pg(
                conn,
                package_id="pkg-full",
                source_env="prod-shanghai",
                to_seq=10,
                created_at="2026-01-01T00:00:00+00:00",
                notebooks=("nb-chain", "nb-scoped", "nb-gone"),
            )
            _insert_import_row_pg(
                conn,
                package_id="pkg-scoped",
                source_env="prod-shanghai",
                created_at="2026-01-02T00:00:00+00:00",
                # 一月的快照六月才导入：落在窗口的行上面，把它盖了回去。
                started_at="2026-06-01T00:00:00+00:00",
                scoped=True,
                notebooks=("nb-scoped", "nb-gone"),
            )
            _insert_import_row_pg(
                conn,
                package_id="pkg-window",
                source_env="prod-shanghai",
                from_seq=11,
                to_seq=30,
                created_at="2026-01-03T00:00:00+00:00",
                mode="incremental",
                base_package_id="pkg-full",
                # 这一轮这三本都有变更，所以窗口带上了它们。
                notebooks=("nb-chain", "nb-scoped", "nb-gone"),
            )
            _insert_mirror_pg(conn, "nb-chain", "prod-shanghai")
            _insert_mirror_pg(conn, "nb-scoped", "prod-shanghai")
            _insert_mirror_pg(conn, "nb-gone", "prod-shanghai", status="deleting")
            _insert_mirror_pg(conn, "nb-orphan", "prod-osaka")
    finally:
        database.close()

    args = cli.build_parser().parse_args(["status", "--json"])
    exit_code = cli._cmd_status(args, cli_settings)
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    shanghai = payload["mirrors"]["prod-shanghai"]
    assert shanghai["total"] == 3
    assert [entry["notebook_id"] for entry in shanghai["in_sync"]] == ["nb-chain"]
    assert shanghai["lagging"] == [
        {
            "notebook_id": "nb-scoped",
            "name": "nb-scoped",
            "applied_package_id": "pkg-scoped",
            "applied_created_at": "2026-01-02T00:00:00+00:00",
            "head_package_id": "pkg-window",
            "head_created_at": "2026-01-03T00:00:00+00:00",
            "deleting": False,
        }
    ]
    assert [entry["notebook_id"] for entry in shanghai["deleting"]] == ["nb-gone"]
    assert shanghai["ahead"] == []
    assert [
        entry["notebook_id"] for entry in payload["mirrors"]["prod-osaka"]["unknown"]
    ] == ["nb-orphan"]

    human_args = cli.build_parser().parse_args(["status"])
    exit_code = cli._cmd_status(human_args, cli_settings)
    assert exit_code == 0
    out = capsys.readouterr().out
    assert (
        "-> prod-shanghai: 共 3 本（已跟上 1，落后 1，领先 0，无链 0，不确定 0，"
        "未知 0，待删除 1）"
    ) in out
    assert (
        "落后 nb-scoped（nb-scoped）：最近带过它的是 pkg-scoped"
        "（2026-01-02T00:00:00+00:00），它盖掉了更晚导出、更早导入的 pkg-window"
        "（2026-01-03T00:00:00+00:00）；"
    ) in out
    assert "待删除 nb-gone（nb-gone）：" in out
    assert (
        "未知 nb-orphan（nb-orphan）：没有任何 done 导入记录能说明这本的位置："
        "该源环境从未在本环境导入过、只有 failed/running 的行，"
        "或者记录早于本记账、report_json 不可读"
    ) in out


def test_status_mirror_of_a_scoped_only_source_env_is_no_chain_on_postgres(
    cli_settings, root_dir, capsys
):
    """只导入过 scoped 包的环境必须报 `no_chain`（先导一次全量），不能因为
    `_chain_heads_for` 把那个 scoped 包列成链头就判成 `ahead`——它不覆盖任何
    scope，这里根本没有链可续。SQLite 泳道同款用例在 tests/test_sync_cli.py。"""
    _migrate(cli_settings)
    database = PostgresDatabase(cli_settings, root_dir)
    try:
        with database.write() as conn:
            _insert_import_row_pg(
                conn,
                package_id="pkg-scoped-only",
                source_env="prod-osaka",
                created_at="2026-01-01T00:00:00+00:00",
                scoped=True,
                notebooks=("nb-only",),
            )
            _insert_mirror_pg(conn, "nb-only", "prod-osaka")
    finally:
        database.close()

    args = cli.build_parser().parse_args(["status", "--json"])
    assert cli._cmd_status(args, cli_settings) == 0
    payload = json.loads(capsys.readouterr().out)
    osaka = payload["mirrors"]["prod-osaka"]
    assert osaka["ahead"] == []
    assert osaka["ambiguous"] == []
    assert [entry["notebook_id"] for entry in osaka["no_chain"]] == ["nb-only"]
    assert osaka["no_chain"][0]["applied_package_id"] == "pkg-scoped-only"
    assert osaka["no_chain"][0]["head_package_id"] is None


def test_status_scoped_import_after_the_last_window_does_not_blur_mirrors_on_postgres(
    cli_settings, root_dir, capsys
):
    """全量 + 窗口 + 之后一个 scoped 包（`created_at` 更晚）：链头有两条，但
    只有一条参与链——没被 scoped 包碰过的镜像仍是 `in_sync`，被碰过的那本
    快照不早于链头，所以是 `ahead`（下一次窗口幂等覆盖，无需动作）。"""
    _migrate(cli_settings)
    database = PostgresDatabase(cli_settings, root_dir)
    try:
        with database.write() as conn:
            _insert_import_row_pg(
                conn,
                package_id="pkg-full",
                source_env="prod-shanghai",
                to_seq=10,
                created_at="2026-01-01T00:00:00+00:00",
                notebooks=("nb-chain", "nb-patched"),
            )
            _insert_import_row_pg(
                conn,
                package_id="pkg-window",
                source_env="prod-shanghai",
                from_seq=11,
                to_seq=30,
                created_at="2026-01-02T00:00:00+00:00",
                mode="incremental",
                base_package_id="pkg-full",
                notebooks=("nb-chain", "nb-patched"),
            )
            _insert_import_row_pg(
                conn,
                package_id="pkg-late-scoped",
                source_env="prod-shanghai",
                created_at="2026-01-03T00:00:00+00:00",
                scoped=True,
                notebooks=("nb-patched",),
            )
            _insert_mirror_pg(conn, "nb-chain", "prod-shanghai")
            _insert_mirror_pg(conn, "nb-patched", "prod-shanghai")
    finally:
        database.close()

    args = cli.build_parser().parse_args(["status", "--json"])
    assert cli._cmd_status(args, cli_settings) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [head["package_id"] for head in payload["chain_heads"]["prod-shanghai"]] == [
        "pkg-window",
        "pkg-late-scoped",
    ]
    shanghai = payload["mirrors"]["prod-shanghai"]
    assert shanghai["total"] == 2
    assert [entry["notebook_id"] for entry in shanghai["in_sync"]] == ["nb-chain"]
    assert shanghai["ambiguous"] == []
    assert shanghai["lagging"] == []
    assert [entry["notebook_id"] for entry in shanghai["ahead"]] == ["nb-patched"]
    assert shanghai["ahead"][0]["applied_package_id"] == "pkg-late-scoped"
    assert shanghai["ahead"][0]["head_package_id"] == "pkg-window"


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
    floor_xmin: int | None = None,
    heartbeat_moment: datetime,
) -> None:
    with database.write() as conn:
        conn.execute(
            "INSERT INTO sync_export_runs "
            "(target_env, run_id, package_id, started_at, heartbeat_at, floor_seq, "
            "floor_xmin) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                target_env,
                run_id,
                package_id,
                heartbeat_moment,
                heartbeat_moment,
                floor_seq,
                floor_xmin,
            ),
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
            database,
            floor_seq=50,
            # Below the watermark's snapshot xmin (60) so this test is only
            # about the seq dimension -- the txid-dimension case (floor_xmin
            # narrowing/refusing) has its own tests below.
            floor_xmin=30,
            heartbeat_moment=datetime.now(timezone.utc),
        )
        old = datetime.now(timezone.utc) - timedelta(days=40)
        _insert_log_row(database, root_dir, cli_settings, seq=40, txid=1, changed_at=old)
        _insert_log_row(database, root_dir, cli_settings, seq=60, txid=1, changed_at=old)

        args = cli.build_parser().parse_args(["prune-log", "--json"])
        exit_code = cli._cmd_prune_log(args, cli_settings)
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["min_seq"] == 50
        assert payload["min_txid"] == 30
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


def test_prune_log_active_lease_narrows_the_txid_bound_on_postgres(
    cli_settings, root_dir, capsys
):
    """codex r4: a lease pins two dimensions, not just seq -- floor_xmin is
    taken in the lease's own claiming transaction, which starts before the
    running export's read snapshot, so it is a lower bound on the txid the
    export will eventually compensate from. A row whose txid is still >= the
    lease's floor_xmin must survive even though its seq is well under every
    other bound."""
    _migrate(cli_settings)
    database = PostgresDatabase(cli_settings, root_dir)
    try:
        with database.write() as conn:
            conn.execute(
                "INSERT INTO sync_export_state "
                "(target_env, exported_through_seq, exported_at, package_id, "
                "captured, exported_snapshot) VALUES (%s, %s, %s, %s, %s, %s)",
                ("prod-tokyo", 100, "2026-01-01T00:00:00+00:00", "pkg-abc", True, "60:70:"),
            )
        _seed_export_lease(
            database,
            floor_seq=500,
            floor_xmin=40,
            heartbeat_moment=datetime.now(timezone.utc),
        )
        old = datetime.now(timezone.utc) - timedelta(days=40)
        # seq well under both the watermark (100) and the lease (500), but
        # txid=45 >= the lease's floor_xmin=40: must survive.
        _insert_log_row(database, root_dir, cli_settings, seq=90, txid=45, changed_at=old)
        # Same shape, txid=30 < floor_xmin=40: eligible.
        _insert_log_row(database, root_dir, cli_settings, seq=91, txid=30, changed_at=old)

        args = cli.build_parser().parse_args(["prune-log", "--json"])
        exit_code = cli._cmd_prune_log(args, cli_settings)
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        # min(watermark xmin=60, lease floor_xmin=40) == 40.
        assert payload["min_txid"] == 40
        assert payload["deleted"] == 1

        with database.connect() as conn:
            remaining = {
                row["seq"]
                for row in conn.execute("SELECT seq FROM sync_change_log").fetchall()
            }
            assert remaining == {90}
    finally:
        database.close()


def test_prune_log_refuses_when_an_active_lease_has_no_floor_xmin_on_postgres(
    cli_settings, root_dir, capsys
):
    """A live PostgreSQL lease with floor_xmin=NULL should never happen (the
    claiming transaction always reads one), but prune-log must not guess a
    number for it -- it refuses outright, naming the target, for as long as
    that lease stays live."""
    _migrate(cli_settings)
    database = PostgresDatabase(cli_settings, root_dir)
    try:
        with database.write() as conn:
            conn.execute(
                "INSERT INTO sync_export_state "
                "(target_env, exported_through_seq, exported_at, package_id, "
                "captured, exported_snapshot) VALUES (%s, %s, %s, %s, %s, %s)",
                ("prod-tokyo", 100, "2026-01-01T00:00:00+00:00", "pkg-abc", True, "60:70:"),
            )
        # floor_xmin omitted -> NULL, simulating a row an older build wrote.
        _seed_export_lease(
            database,
            target_env="prod-osaka",
            floor_seq=10,
            heartbeat_moment=datetime.now(timezone.utc),
        )

        args = cli.build_parser().parse_args(["prune-log"])
        exit_code = cli._cmd_prune_log(args, cli_settings)
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "prod-osaka" in err
        assert "floor_xmin" in err
    finally:
        database.close()


def test_prune_log_evicts_a_dead_lease_on_postgres(cli_settings, root_dir, capsys):
    """codex #784 r6, PostgreSQL half: a real (non-dry-run) prune-log run
    deletes a dead lease's row, not merely excludes it from the bound --
    otherwise a stalled export that resumes and refreshes its own unchanged
    heartbeat would look live again to the publish ownership check, with a
    snapshot whose compensation window this very call already pruned rows
    out from under."""
    _migrate(cli_settings)
    database = PostgresDatabase(cli_settings, root_dir)
    try:
        with database.write() as conn:
            conn.execute(
                "INSERT INTO sync_export_state "
                "(target_env, exported_through_seq, exported_at, package_id, "
                "captured, exported_snapshot) VALUES (%s, %s, %s, %s, %s, %s)",
                ("prod-tokyo", 100, "2026-01-01T00:00:00+00:00", "pkg-abc", True, "60:70:"),
            )
        dead_heartbeat = datetime.now(timezone.utc) - timedelta(hours=2)
        _seed_export_lease(
            database,
            target_env="prod-osaka",
            run_id="run-dead",
            floor_seq=10,
            heartbeat_moment=dead_heartbeat,
        )
        old = datetime.now(timezone.utc) - timedelta(days=40)
        _insert_log_row(database, root_dir, cli_settings, seq=90, txid=1, changed_at=old)

        args = cli.build_parser().parse_args(["prune-log", "--json"])
        exit_code = cli._cmd_prune_log(args, cli_settings)
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["active_leases"] == []
        assert len(payload["dead_leases"]) == 1
        assert payload["dead_leases"][0]["target_env"] == "prod-osaka"
        assert payload["dead_leases"][0]["run_id"] == "run-dead"

        with database.connect() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) AS n FROM sync_export_runs"
            ).fetchone()
            assert remaining["n"] == 0
    finally:
        database.close()


def test_prune_log_locks_every_lease_row_before_deleting_dead_ones_on_postgres(
    cli_settings, root_dir, monkeypatch
):
    """The real run's bounds-and-eviction transaction locks every
    ``sync_export_runs`` row (``SELECT ... FOR UPDATE``, no ``WHERE`` --
    live and dead alike) BEFORE reading anything, so a concurrent lease
    claim/heartbeat refresh cannot slip in between classification and the
    ``DELETE``. Probe it directly: hook ``cli._prune_log_bounds`` to try an
    ``UPDATE`` on the dead lease's own row from a second connection, with a
    short ``lock_timeout``, while the real call's own lock is still held
    (the outer ``write()`` transaction has not committed yet) -- it must
    block.

    变异验证: 去掉 `_prune_log_bounds` 里 PostgreSQL 的 `FOR UPDATE` 语句，
    本条必须报红（探针的 UPDATE 立刻成功，没有被挡住）。
    """
    import psycopg

    _migrate(cli_settings)
    database = PostgresDatabase(cli_settings, root_dir)
    try:
        with database.write() as conn:
            conn.execute(
                "INSERT INTO sync_export_state "
                "(target_env, exported_through_seq, exported_at, package_id, "
                "captured, exported_snapshot) VALUES (%s, %s, %s, %s, %s, %s)",
                ("prod-tokyo", 100, "2026-01-01T00:00:00+00:00", "pkg-abc", True, "60:70:"),
            )
        dead_heartbeat = datetime.now(timezone.utc) - timedelta(hours=2)
        _seed_export_lease(
            database,
            target_env="prod-osaka",
            run_id="run-dead",
            floor_seq=10,
            heartbeat_moment=dead_heartbeat,
        )

        probes: list[str] = []
        real_bounds = cli._prune_log_bounds

        def hooked(source, conn, **kwargs):
            result = real_bounds(source, conn, **kwargs)
            if kwargs.get("lock"):
                # Probed AFTER the FOR UPDATE has run and while the outer
                # write() transaction is still open -- exactly the window
                # the lock exists to close.
                with psycopg.connect(cli_settings.database_url) as other:
                    other.execute("SET LOCAL lock_timeout = '400ms'")
                    try:
                        other.execute(
                            "UPDATE sync_export_runs SET heartbeat_at = now() "
                            "WHERE target_env = %s",
                            ("prod-osaka",),
                        )
                        other.commit()
                        probes.append("acquired")
                    except psycopg.errors.LockNotAvailable as exc:
                        probes.append(f"blocked: {exc}")
            return result

        monkeypatch.setattr(cli, "_prune_log_bounds", hooked)

        args = cli.build_parser().parse_args(["prune-log"])
        exit_code = cli._cmd_prune_log(args, cli_settings)
        assert exit_code == 0

        assert probes, "the locked bounds call never ran"
        assert probes[0].startswith("blocked"), probes

        with database.connect() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) AS n FROM sync_export_runs"
            ).fetchone()
            assert remaining["n"] == 0
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
            floor_xmin=37,
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
        assert runs_by_target["prod-tokyo"]["floor_xmin"] == 37
        assert runs_by_target["prod-tokyo"]["dead"] is False
        assert runs_by_target["prod-osaka"]["floor_seq"] == 7
        assert runs_by_target["prod-osaka"]["floor_xmin"] is None
        assert runs_by_target["prod-osaka"]["dead"] is True

        human_args = cli.build_parser().parse_args(["status"])
        exit_code = cli._cmd_status(human_args, cli_settings)
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "floor_xmin=37" in out
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
