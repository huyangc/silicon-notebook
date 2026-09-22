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
        package_created_at="2026-01-01T00:00:00+00:00",
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
            "--full",
            "--json",
        ]
    )
    assert args.command == "export"
    assert args.target == "prod-tokyo"
    assert args.out == "/tmp/out"
    assert args.notebook == ["nb-a", "nb-b"]
    assert args.source_env == "dev"
    assert args.full is True
    assert args.as_json is True


def test_build_parser_export_notebook_defaults_to_none():
    args = cli.build_parser().parse_args(["export", "--target", "t", "--out", "/tmp/out"])
    assert args.notebook is None
    assert args.source_env is None
    assert args.full is False
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
            "--take-over",
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
    assert args.take_over is True
    assert args.as_json is True


def test_build_parser_import_defaults():
    args = cli.build_parser().parse_args(["import", "/tmp/pkg"])
    assert args.create_missing_users is False
    assert args.dry_run is False
    assert args.importer_user is None
    assert args.resume is False
    assert args.verify_files is False
    assert args.take_over is False
    assert args.as_json is False


def test_build_parser_parses_status():
    args = cli.build_parser().parse_args(["status", "--json"])
    assert args.command == "status"
    assert args.as_json is True
    assert args.exact_count is False


def test_build_parser_parses_status_exact_count():
    args = cli.build_parser().parse_args(["status", "--exact-count"])
    assert args.exact_count is True


def test_build_parser_parses_capture_enable():
    args = cli.build_parser().parse_args(["capture", "enable", "--json"])
    assert args.command == "capture"
    assert args.capture_command == "enable"
    assert args.as_json is True


def test_build_parser_parses_capture_disable():
    args = cli.build_parser().parse_args(["capture", "disable"])
    assert args.command == "capture"
    assert args.capture_command == "disable"
    assert args.as_json is False


def test_build_parser_parses_capture_status():
    args = cli.build_parser().parse_args(["capture", "status", "--json"])
    assert args.command == "capture"
    assert args.capture_command == "status"
    assert args.as_json is True
    assert args.exact_count is False


def test_build_parser_parses_capture_status_exact_count():
    args = cli.build_parser().parse_args(["capture", "status", "--exact-count"])
    assert args.exact_count is True


def test_build_parser_capture_requires_a_subcommand():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["capture"])


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
        "full": False,
    }
    payload = json.loads(capsys.readouterr().out)
    assert payload["package_id"] == "pkg-id"
    assert payload["notebooks"] == ["nb-1"]
    assert payload["table_counts"] == {"sources": 3, "chunks": 0}


def test_export_full_flag_is_forwarded(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, sync_env="dev")
    captured: dict = {}

    def fake_export(settings_arg, **kwargs):
        captured.update(kwargs)
        return _empty_export_report()

    monkeypatch.setattr(cli, "export_notebooks", fake_export)
    exit_code = cli.main(
        ["export", "--target", "prod-tokyo", "--out", str(tmp_path), "--full"]
    )
    assert exit_code == 0
    assert captured["full"] is True


def test_export_full_flag_defaults_to_false(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, sync_env="dev")
    captured: dict = {}

    def fake_export(settings_arg, **kwargs):
        captured.update(kwargs)
        return _empty_export_report()

    monkeypatch.setattr(cli, "export_notebooks", fake_export)
    exit_code = cli.main(["export", "--target", "prod-tokyo", "--out", str(tmp_path)])
    assert exit_code == 0
    assert captured["full"] is False


def test_export_human_output_includes_mode(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch, sync_env="dev")
    monkeypatch.setattr(
        cli, "export_notebooks", lambda *a, **k: _empty_export_report(mode="full")
    )
    exit_code = cli.main(["export", "--target", "prod-tokyo", "--out", str(tmp_path)])
    assert exit_code == 0
    assert "模式: full" in capsys.readouterr().out


def test_export_human_output_includes_captured_through_seq(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch, sync_env="dev")
    monkeypatch.setattr(
        cli,
        "export_notebooks",
        lambda *a, **k: _empty_export_report(captured_through_seq=42),
    )
    exit_code = cli.main(["export", "--target", "prod-tokyo", "--out", str(tmp_path)])
    assert exit_code == 0
    assert "seq=42" in capsys.readouterr().out


def test_export_json_includes_captured_through_seq(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch, sync_env="dev")
    monkeypatch.setattr(
        cli,
        "export_notebooks",
        lambda *a, **k: _empty_export_report(captured_through_seq=7),
    )
    exit_code = cli.main(
        ["export", "--target", "prod-tokyo", "--out", str(tmp_path), "--json"]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["captured_through_seq"] == 7


def test_export_human_output_includes_incremental_fields(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch, sync_env="dev")
    report = _empty_export_report(
        mode="incremental",
        from_seq=8,
        to_seq=15,
        base_package_id="pkg-old",
        deletes=3,
        deleted_notebooks=("nb-gone",),
        skipped_mirror_changes=2,
    )
    monkeypatch.setattr(cli, "export_notebooks", lambda *a, **k: report)
    exit_code = cli.main(["export", "--target", "prod-tokyo", "--out", str(tmp_path)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "序号区间: from_seq=8, to_seq=15" in out
    assert "续接自包: pkg-old" in out
    assert "已删除的笔记本: 1 个" in out
    assert "nb-gone" in out
    assert "删除行数: 3" in out
    assert "跳过的镜像笔记本变更: 2 条" in out


def test_export_human_output_reports_empty_incremental_window(
    tmp_path, monkeypatch, capsys
):
    _settings(tmp_path, monkeypatch, sync_env="dev")
    report = _empty_export_report(mode="incremental", from_seq=9, to_seq=8, empty=True)
    monkeypatch.setattr(cli, "export_notebooks", lambda *a, **k: report)
    exit_code = cli.main(["export", "--target", "prod-tokyo", "--out", str(tmp_path)])
    assert exit_code == 0
    assert "窗口为空，仍产出空增量包；水位已记录。" in capsys.readouterr().out


def test_export_human_output_reports_scoped_export_does_not_advance_watermark(
    tmp_path, monkeypatch, capsys
):
    _settings(tmp_path, monkeypatch, sync_env="dev")
    report = _empty_export_report(scoped=True, watermark_advanced=False)
    monkeypatch.setattr(cli, "export_notebooks", lambda *a, **k: report)
    exit_code = cli.main(
        [
            "export",
            "--target",
            "prod-tokyo",
            "--out",
            str(tmp_path),
            "--notebook",
            "nb-a",
        ]
    )
    assert exit_code == 0
    assert "本次未推进水位" in capsys.readouterr().out


def test_export_json_includes_incremental_and_scoped_fields(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch, sync_env="dev")
    report = _empty_export_report(
        mode="incremental",
        from_seq=8,
        to_seq=15,
        base_package_id="pkg-old",
        deletes=3,
        deleted_notebooks=("nb-gone",),
        skipped_mirror_changes=2,
        scoped=False,
        watermark_advanced=True,
        empty=False,
    )
    monkeypatch.setattr(cli, "export_notebooks", lambda *a, **k: report)
    exit_code = cli.main(
        ["export", "--target", "prod-tokyo", "--out", str(tmp_path), "--json"]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "incremental"
    assert payload["from_seq"] == 8
    assert payload["to_seq"] == 15
    assert payload["base_package_id"] == "pkg-old"
    assert payload["deletes"] == 3
    assert payload["deleted_notebooks"] == ["nb-gone"]
    assert payload["skipped_mirror_changes"] == 2
    assert payload["scoped"] is False
    assert payload["watermark_advanced"] is True
    assert payload["empty"] is False


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
        "take_over": False,
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
    assert captured["take_over"] is False


def test_import_take_over_is_forwarded(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch)
    captured: dict = {}

    def fake_import(settings_arg, package_dir, **kwargs):
        captured.update(kwargs)
        return _empty_import_report()

    monkeypatch.setattr(cli, "import_package", fake_import)
    exit_code = cli.main(["import", str(tmp_path / "pkg"), "--take-over"])
    assert exit_code == 0
    assert captured["take_over"] is True


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
            "captured": False,
            "exported_snapshot": None,
        }
    ]
    assert len(payload["imports"]) == 1
    imported = payload["imports"][0]
    assert imported["package_id"] == "pkg-xyz"
    assert imported["source_env"] == "prod-shanghai"
    assert imported["status"] == "done"
    assert imported["notebooks"] == 2


def test_status_reports_captured_watermark_and_snapshot_xmin(
    tmp_path, monkeypatch, capsys
):
    """SQLite never has a real PostgreSQL snapshot, but ``captured``/
    ``exported_snapshot`` are ordinary columns _load_sync_status now reads
    regardless of backend -- pin the shape here with a hand-crafted snapshot
    text (the same grammar ``_Source.snapshot_xmin`` parses), and leave the
    PostgreSQL-native round trip to tests/postgres/test_sync_cli_pg.py."""
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()

    database = SqliteDatabase(settings, tmp_path)
    try:
        with database.write() as conn:
            conn.execute(
                "INSERT INTO sync_export_state "
                "(target_env, exported_through_seq, exported_at, package_id, "
                "captured, exported_snapshot) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "prod-tokyo",
                    42,
                    "2026-01-01T00:00:00+00:00",
                    "pkg-abc",
                    1,
                    "50:60:55",
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
            "captured": True,
            "exported_snapshot": "50:60:55",
        }
    ]

    exit_code = cli.main(["status"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "captured=true" in out
    assert "snapshot xmin=50" in out


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


def test_status_human_output_flags_superseded_row_with_replacement_id(
    tmp_path, monkeypatch, capsys
):
    """A `failed` import that a newer, already-applied package from the same
    source_env replaced is reported as `superseded`, carrying the replacing
    package_id in report_json.superseded_by -- the human summary must surface
    that id, not just the bare status word."""
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
                    "pkg-old",
                    "prod-shanghai",
                    0,
                    0,
                    "superseded",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:05:00+00:00",
                    json.dumps({"notebooks": [], "superseded_by": "pkg-new"}),
                ),
            )
    finally:
        database.close()

    exit_code = cli.main(["status"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "pkg-old" in out
    assert "superseded" in out
    assert "被 pkg-new 取代" in out


def test_status_human_output_shows_heartbeat_for_running_row(
    tmp_path, monkeypatch, capsys
):
    """A `running` row's heartbeat (refreshed on every table commit, per
    import_.py) lives in report_json.heartbeat_at -- not a dedicated column --
    same place `superseded_by` lives. The human summary must surface it so an
    operator can tell a live import from a dead one before reaching for
    --take-over."""
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
                    "pkg-live",
                    "prod-shanghai",
                    0,
                    0,
                    "running",
                    "2026-01-02T00:00:00+00:00",
                    None,
                    json.dumps(
                        {
                            "package_created_at": "2026-01-02T00:00:00+00:00",
                            "heartbeat_at": "2026-01-02T00:03:00+00:00",
                        }
                    ),
                ),
            )
    finally:
        database.close()

    exit_code = cli.main(["status"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "pkg-live" in out
    assert "心跳: 2026-01-02T00:03:00+00:00" in out


def test_status_human_output_shows_dash_when_running_row_has_no_heartbeat_yet(
    tmp_path, monkeypatch, capsys
):
    """A just-claimed running row (before the first table commit refreshes
    heartbeat_at) must not crash the summary -- it prints "-", same fallback
    as a missing finished_at."""
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
                    "pkg-fresh",
                    "prod-shanghai",
                    0,
                    0,
                    "running",
                    "2026-01-02T00:00:00+00:00",
                    None,
                    json.dumps({"package_created_at": "2026-01-02T00:00:00+00:00"}),
                ),
            )
    finally:
        database.close()

    exit_code = cli.main(["status"])
    assert exit_code == 0
    assert "心跳: -" in capsys.readouterr().out


def test_status_json_carries_heartbeat_unchanged(tmp_path, monkeypatch, capsys):
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
                    "pkg-live",
                    "prod-shanghai",
                    0,
                    0,
                    "running",
                    "2026-01-02T00:00:00+00:00",
                    None,
                    json.dumps({"heartbeat_at": "2026-01-02T00:03:00+00:00"}),
                ),
            )
    finally:
        database.close()

    exit_code = cli.main(["status", "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["imports"][0]["report_json"]["heartbeat_at"] == "2026-01-02T00:03:00+00:00"


def test_status_json_carries_status_unchanged_including_superseded(
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
                    "pkg-old",
                    "prod-shanghai",
                    0,
                    0,
                    "superseded",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:05:00+00:00",
                    json.dumps({"notebooks": [], "superseded_by": "pkg-new"}),
                ),
            )
    finally:
        database.close()

    exit_code = cli.main(["status", "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["imports"]) == 1
    row = payload["imports"][0]
    assert row["status"] == "superseded"
    assert row["report_json"]["superseded_by"] == "pkg-new"


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


def test_status_human_output_includes_capture_section(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()

    exit_code = cli.main(["status"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "变更捕获：" in out
    assert "从未开启过" in out


def test_status_json_includes_capture_section(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()

    exit_code = cli.main(["status", "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["capture"] == {
        "enabled": False,
        "enabled_at": None,
        "disabled_at": None,
        "log_rows": 0,
        "log_rows_exact": True,
        "min_seq": None,
        "max_seq": None,
    }


def test_status_degrades_capture_section_when_v84_tables_are_missing(
    tmp_path, monkeypatch, capsys
):
    """A database migrated only as far as v83/0063 (capture tables not yet
    applied) must still answer `sync status` for its watermark and import
    sections -- the capture section degrades to a diagnostic object instead
    of turning the whole command into a hard failure."""
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
                ("prod-tokyo", 0, "2026-01-01T00:00:00+00:00", "pkg-old"),
            )
        with database.write() as conn:
            # Simulate a database that predates v84/0064: drop the capture
            # tables the triggers reference, exactly as the coordinator's
            # scenario describes -- everything else about the schema is
            # v84-shaped (migrated, current SCHEMA_VERSION), only these two
            # tables are gone.
            conn.execute("DROP TABLE sync_capture_control")
            conn.execute("DROP TABLE sync_change_log")

        exit_code = cli.main(["status", "--json"])
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["exports"] == [
            {
                "target_env": "prod-tokyo",
                "exported_through_seq": 0,
                "exported_at": "2026-01-01T00:00:00+00:00",
                "package_id": "pkg-old",
                "captured": False,
                "exported_snapshot": None,
            }
        ]
        assert payload["imports"] == []
        assert payload["capture"]["missing_tables"] == [
            "sync_capture_control",
            "sync_change_log",
        ]
        assert "v84/0064" in payload["capture"]["detail"]

        exit_code = cli.main(["status"])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "prod-tokyo" in out
        assert "变更捕获：本环境尚未迁移到 v84/0064" in out
    finally:
        database.close()


def test_capture_status_still_exits_2_when_v84_tables_are_missing(
    tmp_path, monkeypatch, capsys
):
    """Unlike `sync status`, `sync capture status` asks specifically about
    capture -- it must keep failing loudly rather than degrade."""
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()
    database = SqliteDatabase(settings, tmp_path)
    try:
        with database.write() as conn:
            conn.execute("DROP TABLE sync_capture_control")
            conn.execute("DROP TABLE sync_change_log")

        exit_code = cli.main(["capture", "status", "--json"])
        assert exit_code == 2
        assert capsys.readouterr().out == ""
    finally:
        database.close()


# ------------------------------------------------------------------ capture


def _seed_watermark(database, target_env: str = "prod-tokyo") -> None:
    with database.write() as conn:
        conn.execute(
            "INSERT INTO sync_export_state "
            "(target_env, exported_through_seq, exported_at, package_id) "
            "VALUES (?, ?, ?, ?)",
            (target_env, 0, "2026-01-01T00:00:00+00:00", "pkg-old"),
        )


def test_capture_enable_turns_on_the_gate_and_clears_export_watermarks(
    tmp_path, monkeypatch, capsys
):
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()
    database = SqliteDatabase(settings, tmp_path)
    try:
        _seed_watermark(database)
        _seed_watermark(database, target_env="prod-osaka")

        exit_code = cli.main(["capture", "enable", "--json"])
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["already_enabled"] is False
        assert payload["cleared_export_watermarks"] == 2
        assert payload["enabled_at"] is not None
        assert payload["log_rows"] == 0

        with database.connect() as conn:
            control = conn.execute(
                "SELECT enabled, enabled_at FROM sync_capture_control WHERE singleton=1"
            ).fetchone()
            assert control["enabled"] == 1
            assert control["enabled_at"] == payload["enabled_at"]
            remaining = conn.execute("SELECT COUNT(*) AS n FROM sync_export_state").fetchone()
            assert remaining["n"] == 0
    finally:
        database.close()


def test_capture_enable_is_idempotent_and_does_not_reclear_watermarks(
    tmp_path, monkeypatch, capsys
):
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()
    database = SqliteDatabase(settings, tmp_path)
    try:
        exit_code = cli.main(["capture", "enable", "--json"])
        assert exit_code == 0
        first = json.loads(capsys.readouterr().out)

        # A watermark written AFTER capture is already on: a second `enable`
        # must leave it alone, or the idempotency guarantee (no observable
        # effect on an already-enabled gate) would be broken by exactly the
        # side effect this test exists to catch.
        _seed_watermark(database)

        exit_code = cli.main(["capture", "enable", "--json"])
        assert exit_code == 0
        second = json.loads(capsys.readouterr().out)
        assert second["already_enabled"] is True
        assert second["enabled_at"] == first["enabled_at"]
        assert second["cleared_export_watermarks"] == 0

        with database.connect() as conn:
            remaining = conn.execute("SELECT COUNT(*) AS n FROM sync_export_state").fetchone()
            assert remaining["n"] == 1
    finally:
        database.close()


def test_capture_enable_human_output_reports_cleared_watermarks(
    tmp_path, monkeypatch, capsys
):
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()
    database = SqliteDatabase(settings, tmp_path)
    try:
        _seed_watermark(database)
        exit_code = cli.main(["capture", "enable"])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "变更捕获已开启" in out
        assert "已清空导出水位: 1 条" in out
    finally:
        database.close()


def test_capture_disable_clears_watermarks_and_log(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()
    database = SqliteDatabase(settings, tmp_path)
    try:
        cli.main(["capture", "enable"])
        capsys.readouterr()
        _seed_watermark(database)
        with database.write() as conn:
            conn.execute(
                "INSERT INTO sync_change_log "
                "(table_name, key_json, operation, changed_at) "
                "VALUES ('notebooks', '{}', 'upsert', '2026-01-01T00:00:00+00:00')"
            )

        exit_code = cli.main(["capture", "disable", "--json"])
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["already_disabled"] is False
        assert payload["cleared_export_watermarks"] == 1
        assert payload["cleared_log_rows"] == 1
        assert payload["log_rows"] == 0

        with database.connect() as conn:
            control = conn.execute(
                "SELECT enabled, disabled_at FROM sync_capture_control WHERE singleton=1"
            ).fetchone()
            assert control["enabled"] == 0
            assert control["disabled_at"] == payload["disabled_at"]
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM sync_export_state"
            ).fetchone()["n"] == 0
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM sync_change_log"
            ).fetchone()["n"] == 0
    finally:
        database.close()


def test_capture_disable_is_idempotent_when_never_enabled(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()

    exit_code = cli.main(["capture", "disable", "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["already_disabled"] is True
    assert payload["disabled_at"] is None
    assert payload["cleared_export_watermarks"] == 0
    assert payload["cleared_log_rows"] == 0


def test_capture_disable_is_idempotent_once_already_disabled(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()

    cli.main(["capture", "enable"])
    capsys.readouterr()
    cli.main(["capture", "disable"])
    first = capsys.readouterr().out

    exit_code = cli.main(["capture", "disable", "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["already_disabled"] is True
    assert payload["disabled_at"] is not None
    assert "已关闭" in first


def test_capture_disable_human_output_when_never_enabled(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()

    exit_code = cli.main(["capture", "disable"])
    assert exit_code == 0
    assert "本来就是关闭的" in capsys.readouterr().out


def test_capture_status_reports_enabled_state_and_log_range(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()
    database = SqliteDatabase(settings, tmp_path)
    try:
        cli.main(["capture", "enable"])
        capsys.readouterr()
        with database.write() as conn:
            for _ in range(3):
                conn.execute(
                    "INSERT INTO sync_change_log "
                    "(table_name, key_json, operation, changed_at) "
                    "VALUES ('notebooks', '{}', 'upsert', '2026-01-01T00:00:00+00:00')"
                )

        exit_code = cli.main(["capture", "status", "--json"])
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["enabled"] is True
        assert payload["log_rows"] == 3
        assert payload["log_rows_exact"] is False
        assert payload["min_seq"] == 1
        assert payload["max_seq"] == 3
    finally:
        database.close()


def test_capture_status_approximate_count_over_counts_across_a_gap_and_exact_count_does_not(
    tmp_path, monkeypatch, capsys
):
    """SQLite's approximate count (``MAX(seq) - MIN(seq) + 1``) is an upper
    bound, not always the true row count: deleting a row out of the middle
    of the log's seq range (something PR-3a's own code never does, but the
    approximation is documented as an upper bound rather than exact for
    exactly this reason) creates a gap the approximation cannot see.
    ``--exact-count`` must still report the true row count."""
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()
    database = SqliteDatabase(settings, tmp_path)
    try:
        cli.main(["capture", "enable"])
        capsys.readouterr()
        with database.write() as conn:
            for _ in range(5):
                conn.execute(
                    "INSERT INTO sync_change_log "
                    "(table_name, key_json, operation, changed_at) "
                    "VALUES ('notebooks', '{}', 'upsert', '2026-01-01T00:00:00+00:00')"
                )
            # Punch a hole in the middle of the seq range (seq 3 of 1..5):
            # min=1, max=5, true row count=4, approximate=5-1+1=5.
            conn.execute("DELETE FROM sync_change_log WHERE seq = 3")

        exit_code = cli.main(["capture", "status", "--json"])
        assert exit_code == 0
        approx = json.loads(capsys.readouterr().out)
        assert approx["log_rows"] == 5
        assert approx["log_rows_exact"] is False
        assert approx["min_seq"] == 1
        assert approx["max_seq"] == 5

        exit_code = cli.main(["capture", "status", "--json", "--exact-count"])
        assert exit_code == 0
        exact = json.loads(capsys.readouterr().out)
        assert exact["log_rows"] == 4
        assert exact["log_rows_exact"] is True
        assert exact["min_seq"] == 1
        assert exact["max_seq"] == 5
    finally:
        database.close()


def test_capture_status_human_output_marks_approximate_counts(
    tmp_path, monkeypatch, capsys
):
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()
    database = SqliteDatabase(settings, tmp_path)
    try:
        cli.main(["capture", "enable"])
        capsys.readouterr()
        with database.write() as conn:
            conn.execute(
                "INSERT INTO sync_change_log "
                "(table_name, key_json, operation, changed_at) "
                "VALUES ('notebooks', '{}', 'upsert', '2026-01-01T00:00:00+00:00')"
            )

        exit_code = cli.main(["capture", "status"])
        assert exit_code == 0
        approx_out = capsys.readouterr().out
        assert "约 1 行" in approx_out

        exit_code = cli.main(["capture", "status", "--exact-count"])
        assert exit_code == 0
        exact_out = capsys.readouterr().out
        assert "约" not in exact_out
        assert "1 行" in exact_out
    finally:
        database.close()


def test_capture_status_human_output_on_a_disabled_never_enabled_gate(
    tmp_path, monkeypatch, capsys
):
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()

    exit_code = cli.main(["capture", "status"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "关闭" in out
    assert "从未开启过" in out
    assert "变更日志: 0 行" in out


def test_capture_enable_missing_capture_tables_gives_named_message(
    tmp_path, monkeypatch, capsys
):
    """A database that predates v84/0064 (here: never migrated at all) must
    name the missing capture tables instead of surfacing a raw sqlite3 "no
    such table" driver error."""
    _settings(tmp_path, monkeypatch)
    exit_code = cli.main(["capture", "enable"])
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "v84/0064" in err
    assert "sync_capture_control" in err


# ---------------------------------------------------------------- prune-log


def test_build_parser_parses_prune_log_defaults():
    args = cli.build_parser().parse_args(["prune-log"])
    assert args.command == "prune-log"
    assert args.keep_days == 30
    assert args.dry_run is False
    assert args.as_json is False


def test_build_parser_parses_prune_log_explicit_flags():
    args = cli.build_parser().parse_args(
        ["prune-log", "--keep-days", "7", "--dry-run", "--json"]
    )
    assert args.keep_days == 7
    assert args.dry_run is True
    assert args.as_json is True


def _seed_captured_watermark(
    database,
    *,
    target_env: str = "prod-tokyo",
    exported_through_seq: int,
    captured: int = 1,
) -> None:
    with database.write() as conn:
        conn.execute(
            "INSERT INTO sync_export_state "
            "(target_env, exported_through_seq, exported_at, package_id, captured) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                target_env,
                exported_through_seq,
                "2026-01-01T00:00:00+00:00",
                f"pkg-{target_env}",
                captured,
            ),
        )


def _insert_log_row(database, *, seq: int, changed_at: str) -> None:
    with database.write() as conn:
        conn.execute(
            "INSERT INTO sync_change_log "
            "(seq, table_name, key_json, operation, changed_at) "
            "VALUES (?, 'notebooks', '{}', 'upsert', ?)",
            (seq, changed_at),
        )


def _old_moment(days: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def test_prune_log_rejects_when_no_captured_watermark_exists(
    tmp_path, monkeypatch, capsys
):
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()
    database = SqliteDatabase(settings, tmp_path)
    try:
        # A watermark row exists, but with captured=0 -- it does not count.
        _seed_captured_watermark(
            database, exported_through_seq=10, captured=0
        )
        exit_code = cli.main(["prune-log"])
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "captured=1" in err
    finally:
        database.close()


def test_prune_log_missing_capture_tables_gives_named_message(
    tmp_path, monkeypatch, capsys
):
    """A database that predates v84/0064 (here: never migrated at all) must
    name the missing capture tables, same diagnosis every other capture-
    touching command gives -- checked BEFORE the captured-watermark rule, so
    an un-migrated database never gets the (misleading) "no captured
    watermark" message instead."""
    _settings(tmp_path, monkeypatch)
    exit_code = cli.main(["prune-log"])
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "v84/0064" in err
    assert "sync_change_log" in err


def test_prune_log_dry_run_reports_count_without_deleting(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()
    database = SqliteDatabase(settings, tmp_path)
    try:
        _seed_captured_watermark(database, exported_through_seq=100)
        old = _old_moment(40)
        _insert_log_row(database, seq=1, changed_at=old)
        _insert_log_row(database, seq=2, changed_at=old)

        exit_code = cli.main(["prune-log", "--dry-run", "--json"])
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["dry_run"] is True
        assert payload["would_delete"] == 2
        assert payload["deleted"] == 0

        with database.connect() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) AS n FROM sync_change_log"
            ).fetchone()
            assert remaining["n"] == 2  # dry-run deleted nothing
    finally:
        database.close()


def test_prune_log_real_run_deletes_eligible_rows_only(tmp_path, monkeypatch, capsys):
    """Three rows: one eligible on every condition (deleted), one whose seq
    is past the watermark (kept), one recent enough by changed_at to be kept
    even though its seq qualifies -- proves the AND of seq/changed_at, not
    just seq alone, decides eligibility."""
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()
    database = SqliteDatabase(settings, tmp_path)
    try:
        _seed_captured_watermark(database, exported_through_seq=100)
        old = _old_moment(40)
        recent = _old_moment(1)
        _insert_log_row(database, seq=1, changed_at=old)  # eligible
        _insert_log_row(database, seq=200, changed_at=old)  # seq beyond watermark
        _insert_log_row(database, seq=2, changed_at=recent)  # too recent

        exit_code = cli.main(["prune-log", "--json"])
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["dry_run"] is False
        assert payload["deleted"] == 1
        assert payload["min_seq"] == 100
        assert payload["min_txid"] is None

        with database.connect() as conn:
            remaining = {
                row["seq"]
                for row in conn.execute("SELECT seq FROM sync_change_log").fetchall()
            }
            assert remaining == {200, 2}
    finally:
        database.close()


def test_prune_log_default_keep_days_is_30(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()
    database = SqliteDatabase(settings, tmp_path)
    try:
        _seed_captured_watermark(database, exported_through_seq=100)
        _insert_log_row(database, seq=1, changed_at=_old_moment(31))  # deleted
        _insert_log_row(database, seq=2, changed_at=_old_moment(29))  # kept

        exit_code = cli.main(["prune-log", "--json"])
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["deleted"] == 1
        assert payload["keep_days"] == 30

        with database.connect() as conn:
            remaining = {
                row["seq"]
                for row in conn.execute("SELECT seq FROM sync_change_log").fetchall()
            }
            assert remaining == {2}
    finally:
        database.close()


def test_prune_log_only_captured_watermarks_vote_on_the_minimum(
    tmp_path, monkeypatch, capsys
):
    """A target with NO captured watermark (here: never exported, so no row
    at all) must not drag the bound down -- only the captured=1 target's
    seq=100 sets the ceiling. This is the guard
    ``_prune_log_bounds``'s ``WHERE captured = ?`` filter exists for: dropping
    that filter would let an uncaptured/low watermark block pruning for
    everyone."""
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()
    database = SqliteDatabase(settings, tmp_path)
    try:
        _seed_captured_watermark(
            database, target_env="prod-tokyo", exported_through_seq=100, captured=1
        )
        _seed_captured_watermark(
            database, target_env="prod-osaka", exported_through_seq=5, captured=0
        )
        old = _old_moment(40)
        _insert_log_row(database, seq=50, changed_at=old)

        exit_code = cli.main(["prune-log", "--json"])
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["min_seq"] == 100
        assert payload["deleted"] == 1

        with database.connect() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) AS n FROM sync_change_log"
            ).fetchone()
            assert remaining["n"] == 0
    finally:
        database.close()


def test_prune_log_human_output(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()
    database = SqliteDatabase(settings, tmp_path)
    try:
        _seed_captured_watermark(database, exported_through_seq=100)
        _insert_log_row(database, seq=1, changed_at=_old_moment(40))

        exit_code = cli.main(["prune-log"])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "已删除 1 行。" in out
        assert "保留天数: 30 天" in out
        assert "seq<=100" in out
    finally:
        database.close()


def test_prune_log_dry_run_human_output(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()
    database = SqliteDatabase(settings, tmp_path)
    try:
        _seed_captured_watermark(database, exported_through_seq=100)
        _insert_log_row(database, seq=1, changed_at=_old_moment(40))

        exit_code = cli.main(["prune-log", "--dry-run"])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "会删除 1 行，不会真的删除。" in out
    finally:
        database.close()


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


def test_capture_log_range_query_is_two_index_probes_on_sqlite(tmp_path, monkeypatch):
    """codex #783 r1: ``SELECT MIN(seq), MAX(seq)`` in ONE aggregate defeats
    SQLite's min/max index optimization and plans as a full
    ``SCAN sync_change_log`` -- on an unbounded log, from a status command.
    The shipped statement uses two scalar subqueries, and this pins the plan:
    no step may be a table scan, each endpoint is a primary-key probe. The
    combined form is asserted to scan so the pin cannot be vacuous."""
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()
    database = SqliteDatabase(settings, tmp_path)
    try:
        with database.write() as conn:
            shipped = [
                str(row[3])
                for row in conn.execute(
                    f"EXPLAIN QUERY PLAN {cli._CAPTURE_LOG_RANGE_SQL}"
                )
            ]
            combined = [
                str(row[3])
                for row in conn.execute(
                    "EXPLAIN QUERY PLAN SELECT MIN(seq) AS min_seq, "
                    "MAX(seq) AS max_seq FROM sync_change_log"
                )
            ]
    finally:
        database.close()
    # "SCAN CONSTANT ROW" is the FROM-less outer row, not a table read: the
    # only touches of the log must be two SEARCHes (one per endpoint).
    assert [step for step in shipped if "sync_change_log" in step] == [
        "SEARCH sync_change_log",
        "SEARCH sync_change_log",
    ], shipped
    assert any(step.startswith("SCAN sync_change_log") for step in combined), combined
