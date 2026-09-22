"""守卫: 跨环境笔记本同步的操作员 CLI (app.migration.sync.cli)，对应
docs/incremental-sync-design.md §8「CLI 形态」。

这里不真跑导出/引入：``export_notebooks``/``import_package`` 全部 monkeypatch，
只盯 CLI 自己的契约——参数解析、退出码、source_env 的默认取值与覆盖、
``--json``/人读两种输出、以及 ``status`` 对一个真实迁移过的 SQLite 库的只读查询。
两端的导出/导入相位本身由 test_sync_export.py / test_sync_import.py 与
tests/postgres/test_sync_export_pg.py / test_sync_import_pg.py 覆盖；``status`` 按后端会
分叉的那一段（PostgreSQL 的 jsonb 列读回来已经是 dict）在
tests/postgres/test_sync_cli_pg.py。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.config import Settings
from app.migration.sync import cli
from app.migration.sync.export import ExportReport, SyncExportError
from app.migration.sync.import_ import (
    ImportReport,
    SyncImportError,
    TableOutcome,
    UserMappingResult,
)
from app.repositories.sqlite.database import SqliteDatabase
from app.services.sqlite_repository import SQLiteRepository


def _settings(tmp_path, monkeypatch, *, sync_env: str = "") -> Settings:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'sync_cli.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    if sync_env:
        monkeypatch.setenv("SILICON_NOTEBOOK_SYNC_ENV", sync_env)
    else:
        monkeypatch.delenv("SILICON_NOTEBOOK_SYNC_ENV", raising=False)
    return Settings()


def _empty_export_report(**overrides) -> ExportReport:
    fields = dict(
        package_dir=Path("/tmp/pkg"),
        package_id="pkg-id",
        notebooks=(),
        skipped={},
        table_counts={},
        file_count=0,
        bytes_written=0,
        mode="full",
    )
    fields.update(overrides)
    return ExportReport(**fields)


def _empty_import_report(**overrides) -> ImportReport:
    fields = dict(
        package_id="pkg-id",
        source_env="dev",
        already_applied=False,
        dry_run=False,
        notebooks=(),
        tables={},
        skipped_rows=(),
        skipped_row_total=0,
        user_mapping=UserMappingResult(matched={}, created={}, unmatched=()),
        groups_created=(),
        files_copied=0,
        warnings=(),
    )
    fields.update(overrides)
    return ImportReport(**fields)


# --------------------------------------------------------------- build_parser


def test_build_parser_parses_export():
    args = cli.build_parser().parse_args(
        [
            "export",
            "--target",
            "prod-tokyo",
            "--out",
            "/tmp/out",
            "--notebook",
            "nb-a",
            "--notebook",
            "nb-b",
            "--source-env",
            "dev",
            "--json",
        ]
    )
    assert args.command == "export"
    assert args.target == "prod-tokyo"
    assert args.out == "/tmp/out"
    assert args.notebook == ["nb-a", "nb-b"]
    assert args.source_env == "dev"
    assert args.as_json is True


def test_build_parser_export_notebook_defaults_to_none():
    args = cli.build_parser().parse_args(["export", "--target", "t", "--out", "/tmp/out"])
    assert args.notebook is None
    assert args.source_env is None
    assert args.as_json is False


def test_build_parser_parses_import():
    args = cli.build_parser().parse_args(
        [
            "import",
            "/tmp/pkg",
            "--create-missing-users",
            "--dry-run",
            "--importer-user",
            "user-1",
            "--resume",
            "--verify-files",
            "--json",
        ]
    )
    assert args.command == "import"
    assert args.package_dir == "/tmp/pkg"
    assert args.create_missing_users is True
    assert args.dry_run is True
    assert args.importer_user == "user-1"
    assert args.resume is True
    assert args.verify_files is True
    assert args.as_json is True


def test_build_parser_import_defaults():
    args = cli.build_parser().parse_args(["import", "/tmp/pkg"])
    assert args.create_missing_users is False
    assert args.dry_run is False
    assert args.importer_user is None
    assert args.resume is False
    assert args.verify_files is False
    assert args.as_json is False


def test_build_parser_parses_status():
    args = cli.build_parser().parse_args(["status", "--json"])
    assert args.command == "status"
    assert args.as_json is True


def test_build_parser_requires_a_subcommand():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


# ------------------------------------------------------------------- export


def test_export_missing_source_env_exits_2(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch, sync_env="")
    called = []
    monkeypatch.setattr(cli, "export_notebooks", lambda *a, **k: called.append((a, k)))
    exit_code = cli.main(
        ["export", "--target", "prod-tokyo", "--out", str(tmp_path / "out")]
    )
    assert exit_code == 2
    assert not called
    assert "SILICON_NOTEBOOK_SYNC_ENV" in capsys.readouterr().err


def test_export_defaults_source_env_from_settings_and_prints_json(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch, sync_env="prod-shanghai")
    report = _empty_export_report(
        package_dir=tmp_path / "out" / "pkg",
        notebooks=("nb-1",),
        table_counts={"sources": 3, "chunks": 0},
        file_count=2,
        bytes_written=1234,
    )
    captured: dict = {}

    def fake_export(settings_arg, **kwargs):
        captured["settings"] = settings_arg
        captured["kwargs"] = kwargs
        return report

    monkeypatch.setattr(cli, "export_notebooks", fake_export)
    exit_code = cli.main(
        ["export", "--target", "prod-tokyo", "--out", str(tmp_path / "out"), "--json"]
    )
    assert exit_code == 0
    assert isinstance(captured["settings"], Settings)
    assert captured["kwargs"] == {
        "target_env": "prod-tokyo",
        "out_dir": tmp_path / "out",
        "notebook_ids": None,
        "source_env": "prod-shanghai",
    }
    payload = json.loads(capsys.readouterr().out)
    assert payload["package_id"] == "pkg-id"
    assert payload["notebooks"] == ["nb-1"]
    assert payload["table_counts"] == {"sources": 3, "chunks": 0}


def test_export_human_output_includes_mode(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch, sync_env="dev")
    monkeypatch.setattr(
        cli, "export_notebooks", lambda *a, **k: _empty_export_report(mode="full")
    )
    exit_code = cli.main(["export", "--target", "prod-tokyo", "--out", str(tmp_path)])
    assert exit_code == 0
    assert "模式: full" in capsys.readouterr().out


def test_export_explicit_source_env_overrides_settings(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, sync_env="prod-shanghai")
    captured: dict = {}

    def fake_export(settings_arg, **kwargs):
        captured.update(kwargs)
        return _empty_export_report()

    monkeypatch.setattr(cli, "export_notebooks", fake_export)
    exit_code = cli.main(
        [
            "export",
            "--target",
            "prod-tokyo",
            "--out",
            str(tmp_path),
            "--source-env",
            "override-env",
        ]
    )
    assert exit_code == 0
    assert captured["source_env"] == "override-env"


def test_export_notebook_flags_become_a_tuple(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, sync_env="dev")
    captured: dict = {}

    def fake_export(settings_arg, **kwargs):
        captured.update(kwargs)
        return _empty_export_report()

    monkeypatch.setattr(cli, "export_notebooks", fake_export)
    exit_code = cli.main(
        [
            "export",
            "--target",
            "prod-tokyo",
            "--out",
            str(tmp_path),
            "--notebook",
            "nb-a",
            "--notebook",
            "nb-b",
        ]
    )
    assert exit_code == 0
    assert captured["notebook_ids"] == ("nb-a", "nb-b")


def test_export_sync_error_exits_2(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch, sync_env="dev")

    def raise_error(*a, **k):
        raise SyncExportError("boom")

    monkeypatch.setattr(cli, "export_notebooks", raise_error)
    exit_code = cli.main(["export", "--target", "prod-tokyo", "--out", str(tmp_path)])
    assert exit_code == 2
    assert "boom" in capsys.readouterr().err


# ------------------------------------------------------------------- import


def test_import_passes_arguments_and_prints_json(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch)
    report = _empty_import_report(
        notebooks=("nb-1",),
        tables={"sources": TableOutcome(inserted=2, updated=1)},
        files_copied=5,
    )
    captured: dict = {}

    def fake_import(settings_arg, package_dir, **kwargs):
        captured["settings"] = settings_arg
        captured["package_dir"] = package_dir
        captured["kwargs"] = kwargs
        return report

    monkeypatch.setattr(cli, "import_package", fake_import)
    exit_code = cli.main(
        [
            "import",
            str(tmp_path / "pkg"),
            "--create-missing-users",
            "--importer-user",
            "user-9",
            "--json",
        ]
    )
    assert exit_code == 0
    assert isinstance(captured["settings"], Settings)
    assert captured["package_dir"] == tmp_path / "pkg"
    assert captured["kwargs"] == {
        "create_missing_users": True,
        "dry_run": False,
        "importer_user_id": "user-9",
        "resume": False,
        "verify_files": False,
    }
    payload = json.loads(capsys.readouterr().out)
    assert payload["notebooks"] == ["nb-1"]
    assert payload["files_copied"] == 5


def test_import_json_uses_report_as_json(tmp_path, monkeypatch, capsys):
    """--json must match ``ImportReport.as_json()`` -- the same shape as the
    on-disk import report and ``sync_imports.report_json`` -- not a generic
    dataclass walk that would, e.g., spell out the full user_mapping dicts
    instead of ``as_json()``'s summarized counts."""
    _settings(tmp_path, monkeypatch)
    report = _empty_import_report(
        user_mapping=UserMappingResult(
            matched={"src-a": "tgt-a"}, created={}, unmatched=("carol",)
        )
    )
    monkeypatch.setattr(cli, "import_package", lambda *a, **k: report)
    exit_code = cli.main(["import", str(tmp_path / "pkg"), "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == report.as_json()
    assert payload["user_mapping"] == {"matched": 1, "created": [], "unmatched": ["carol"]}


def test_import_resume_and_verify_files_are_forwarded(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch)
    captured: dict = {}

    def fake_import(settings_arg, package_dir, **kwargs):
        captured.update(kwargs)
        return _empty_import_report()

    monkeypatch.setattr(cli, "import_package", fake_import)
    exit_code = cli.main(
        ["import", str(tmp_path / "pkg"), "--resume", "--verify-files"]
    )
    assert exit_code == 0
    assert captured["resume"] is True
    assert captured["verify_files"] is True


def test_import_real_run_human_output_includes_tables_and_files(
    tmp_path, monkeypatch, capsys
):
    _settings(tmp_path, monkeypatch)
    report = _empty_import_report(
        notebooks=("nb-1",),
        tables={"sources": TableOutcome(inserted=2, updated=1)},
        files_copied=5,
        groups_created=("acme",),
    )
    monkeypatch.setattr(cli, "import_package", lambda *a, **k: report)
    exit_code = cli.main(["import", str(tmp_path / "pkg")])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "== 预检模式" not in out
    assert "逐表结果" in out
    assert "文件数: 5" in out
    assert "新建组: acme" in out
    assert "将新建组" not in out


def test_import_dry_run_human_output_banner_first_and_skips_table_and_file_lines(
    tmp_path, monkeypatch, capsys
):
    _settings(tmp_path, monkeypatch)
    report = _empty_import_report(
        dry_run=True,
        notebooks=("nb-1",),
        groups_created=("acme",),
    )
    monkeypatch.setattr(cli, "import_package", lambda *a, **k: report)
    exit_code = cli.main(["import", str(tmp_path / "pkg"), "--dry-run"])
    assert exit_code == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0] == "== 预检模式（--dry-run）：以下是预览，未写入任何数据 =="
    assert "逐表结果" not in out
    assert "文件数" not in out
    assert "将新建组: acme" in lines
    assert "新建组: acme" not in lines


def test_import_prints_every_warning_including_sqlite_stop_service_hint(
    tmp_path, monkeypatch, capsys
):
    """cli.py never inspects the backend itself for this -- whatever the
    report's ``warnings`` carries (including a SQLite-target must-stop-service
    hint from import_.py) is printed verbatim."""
    _settings(tmp_path, monkeypatch)
    report = _empty_import_report(
        warnings=("目标是 SQLite：必须先停止应用再导入，否则并发写会互相冲突。",)
    )
    monkeypatch.setattr(cli, "import_package", lambda *a, **k: report)
    exit_code = cli.main(["import", str(tmp_path / "pkg")])
    assert exit_code == 0
    assert "必须先停止应用再导入" in capsys.readouterr().out


def test_import_already_applied_exits_0(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch)
    report = _empty_import_report(already_applied=True)
    monkeypatch.setattr(cli, "import_package", lambda *a, **k: report)
    exit_code = cli.main(["import", str(tmp_path / "pkg")])
    assert exit_code == 0
    assert "已经引入过" in capsys.readouterr().out


def test_import_dry_run_flag_is_forwarded(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch)
    captured: dict = {}

    def fake_import(settings_arg, package_dir, **kwargs):
        captured.update(kwargs)
        return _empty_import_report(dry_run=True)

    monkeypatch.setattr(cli, "import_package", fake_import)
    exit_code = cli.main(["import", str(tmp_path / "pkg"), "--dry-run"])
    assert exit_code == 0
    assert captured["dry_run"] is True


def test_import_preflight_failure_exits_2(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch)

    def raise_error(*a, **k):
        raise SyncImportError("schema pair mismatch")

    monkeypatch.setattr(cli, "import_package", raise_error)
    exit_code = cli.main(["import", str(tmp_path / "pkg")])
    assert exit_code == 2
    assert "schema pair mismatch" in capsys.readouterr().err


# ------------------------------------------------------------------- status


def test_status_lists_export_watermark_and_import_row(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()

    database = SqliteDatabase(settings, tmp_path)
    try:
        with database.write() as conn:
            conn.execute(
                "INSERT INTO sync_export_state "
                "(target_env, exported_through_seq, exported_at, package_id) "
                "VALUES (?, ?, ?, ?)",
                ("prod-tokyo", 42, "2026-01-01T00:00:00+00:00", "pkg-abc"),
            )
            conn.execute(
                "INSERT INTO sync_imports "
                "(package_id, source_env, from_seq, to_seq, status, started_at, "
                "finished_at, report_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "pkg-xyz",
                    "prod-shanghai",
                    0,
                    0,
                    "done",
                    "2026-01-02T00:00:00+00:00",
                    "2026-01-02T00:05:00+00:00",
                    json.dumps({"notebooks": ["nb-1", "nb-2"]}),
                ),
            )
    finally:
        database.close()

    exit_code = cli.main(["status", "--json"])
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
    assert len(payload["imports"]) == 1
    imported = payload["imports"][0]
    assert imported["package_id"] == "pkg-xyz"
    assert imported["source_env"] == "prod-shanghai"
    assert imported["status"] == "done"
    assert imported["notebooks"] == 2


def test_status_human_readable_output_on_empty_database(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()

    exit_code = cli.main(["status"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "导出水位" in out
    assert "已引入的包" in out
    assert "（无）" in out


def test_status_human_output_prints_dash_for_missing_finished_at(
    tmp_path, monkeypatch, capsys
):
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()

    database = SqliteDatabase(settings, tmp_path)
    try:
        with database.write() as conn:
            conn.execute(
                "INSERT INTO sync_imports "
                "(package_id, source_env, from_seq, to_seq, status, started_at, "
                "finished_at, report_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "pkg-running",
                    "prod-shanghai",
                    0,
                    0,
                    "running",
                    "2026-01-02T00:00:00+00:00",
                    None,
                    "{}",
                ),
            )
    finally:
        database.close()

    exit_code = cli.main(["status"])
    assert exit_code == 0
    assert "结束于 -" in capsys.readouterr().out


def test_status_missing_sync_tables_gives_named_message(tmp_path, monkeypatch, capsys):
    """A fresh, never-migrated SQLite file has no tables at all, including
    the sync control ones -- ``status`` must name the missing migration
    instead of surfacing sqlite3's raw "no such table" text."""
    _settings(tmp_path, monkeypatch)
    exit_code = cli.main(["status"])
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "v83/0063" in err
    assert "sync_export_state" in err
    assert "sync_imports" in err


def test_status_json_missing_sync_tables_also_exits_2(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch)
    exit_code = cli.main(["status", "--json"])
    assert exit_code == 2
    assert capsys.readouterr().out == ""


# --------------------------------------------------------------------- misc


def test_main_reports_unexpected_exception_and_exits_2(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch)
    monkeypatch.delenv("SILICON_NOTEBOOK_DEBUG", raising=False)

    def raise_unexpected(*a, **k):
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(cli, "import_package", raise_unexpected)
    exit_code = cli.main(["import", str(tmp_path / "pkg")])
    assert exit_code == 2
    assert "unexpected failure" in capsys.readouterr().err


def test_main_debug_env_reraises_instead_of_swallowing(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch)
    monkeypatch.setenv("SILICON_NOTEBOOK_DEBUG", "1")

    def raise_unexpected(*a, **k):
        raise RuntimeError("debug me")

    monkeypatch.setattr(cli, "import_package", raise_unexpected)
    with pytest.raises(RuntimeError, match="debug me"):
        cli.main(["import", str(tmp_path / "pkg")])


def test_main_debug_env_does_not_change_keyboard_interrupt_handling(
    tmp_path, monkeypatch, capsys
):
    """The escape hatch is on the catch-all ``except Exception`` only; a
    deliberate Ctrl-C still gets its own short message, not a traceback."""
    _settings(tmp_path, monkeypatch)
    monkeypatch.setenv("SILICON_NOTEBOOK_DEBUG", "1")

    def raise_interrupt(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "import_package", raise_interrupt)
    exit_code = cli.main(["import", str(tmp_path / "pkg")])
    assert exit_code == 2
    assert "interrupted" in capsys.readouterr().err


# ------------------------------------------------------------------ _jsonable


def test_jsonable_serializes_naive_datetime_as_utc():
    assert cli._jsonable(datetime(2026, 1, 1, 12, 30, 0)) == "2026-01-01T12:30:00+00:00"


def test_jsonable_serializes_aware_datetime_with_its_own_offset():
    value = datetime(2026, 1, 1, 12, 30, 0, tzinfo=timezone(timedelta(hours=8)))
    assert cli._jsonable(value) == "2026-01-01T12:30:00+08:00"


def test_jsonable_serializes_date():
    assert cli._jsonable(date(2026, 1, 1)) == "2026-01-01"


def test_jsonable_serializes_datetime_nested_in_a_dataclass():
    report = _empty_export_report()
    # ExportReport itself carries no datetime field, so exercise the branch
    # through a plain mapping -- what matters is that a nested datetime
    # anywhere in the tree round-trips through json.dumps without a
    # TypeError, which a bare `_jsonable(dataclass)` walk would hit before
    # this branch existed.
    payload = cli._jsonable({"report": report, "at": datetime(2026, 1, 1)})
    assert payload["at"] == "2026-01-01T00:00:00+00:00"
    assert payload["report"]["package_id"] == "pkg-id"


# --------------------------------------------------------- registration guard


def test_scripts_cli_registers_the_sync_command_group():
    """CLI 注册守卫: scripts/cli.py 的 COMMANDS 必须能把 `sync export/import/status`
    转发给 sync_notebooks.py 自己的子命令解析器 (见该文件的 main() 分发机制:
    两段式路径查找会把匹配到的 key 长度之后的全部 token 原样转发给目标脚本，
    所以这里只能注册单个 ("sync",) 顶层入口，不能拆成三个二段 key —— 拆开会在
    转发前吃掉 "export"/"import"/"status" 这个位置参数，sync_notebooks.py 的
    argparse 子命令就永远收不到它)。"""
    import importlib.util
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "sync_cli_registration_under_test", root / "scripts" / "cli.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    assert ("sync",) in module.COMMANDS
    command = module.COMMANDS[("sync",)]
    assert command.script == "sync_notebooks.py"
    assert (root / "scripts" / command.script).is_file()
    # All three subcommands need the deployment's real DATABASE_URL (and, for
    # `export`, SILICON_NOTEBOOK_SYNC_ENV) -- without the shared Python launch
    # environment's root-.env preload, Settings() would silently fall back to
    # its own sqlite default instead of failing loudly, and an operator could
    # export/import/status against the wrong database without any signal.
    assert command.deployment_env is True
