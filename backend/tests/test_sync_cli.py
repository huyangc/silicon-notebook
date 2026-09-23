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
import time
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
    _PriorImport,
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


def test_export_human_output_includes_captured_flag(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch, sync_env="dev")
    monkeypatch.setattr(
        cli, "export_notebooks", lambda *a, **k: _empty_export_report(captured=True)
    )
    exit_code = cli.main(["export", "--target", "prod-tokyo", "--out", str(tmp_path)])
    assert exit_code == 0
    assert "本次水位 captured=true" in capsys.readouterr().out


def test_export_human_output_captured_false_by_default(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch, sync_env="dev")
    monkeypatch.setattr(cli, "export_notebooks", lambda *a, **k: _empty_export_report())
    exit_code = cli.main(["export", "--target", "prod-tokyo", "--out", str(tmp_path)])
    assert exit_code == 0
    assert "本次水位 captured=false" in capsys.readouterr().out


def test_export_json_includes_captured_flag(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch, sync_env="dev")
    monkeypatch.setattr(
        cli, "export_notebooks", lambda *a, **k: _empty_export_report(captured=True)
    )
    exit_code = cli.main(
        ["export", "--target", "prod-tokyo", "--out", str(tmp_path), "--json"]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["captured"] is True


def test_export_human_output_includes_lease_run_id(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch, sync_env="dev")
    monkeypatch.setattr(
        cli, "export_notebooks", lambda *a, **k: _empty_export_report(run_id="pkg-id")
    )
    exit_code = cli.main(["export", "--target", "prod-tokyo", "--out", str(tmp_path)])
    assert exit_code == 0
    assert "租约 run_id: pkg-id" in capsys.readouterr().out


def test_export_human_output_omits_run_id_line_for_a_scoped_export(
    tmp_path, monkeypatch, capsys
):
    """A ``--notebook``-scoped export takes no lease (``run_id`` is empty) --
    the line must not appear at all rather than print an empty id."""
    _settings(tmp_path, monkeypatch, sync_env="dev")
    monkeypatch.setattr(
        cli, "export_notebooks", lambda *a, **k: _empty_export_report(run_id="", scoped=True)
    )
    exit_code = cli.main(
        ["export", "--target", "prod-tokyo", "--out", str(tmp_path), "--notebook", "nb-a"]
    )
    assert exit_code == 0
    assert "租约 run_id" not in capsys.readouterr().out


def test_export_json_includes_run_id(tmp_path, monkeypatch, capsys):
    _settings(tmp_path, monkeypatch, sync_env="dev")
    monkeypatch.setattr(
        cli, "export_notebooks", lambda *a, **k: _empty_export_report(run_id="pkg-id")
    )
    exit_code = cli.main(
        ["export", "--target", "prod-tokyo", "--out", str(tmp_path), "--json"]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_id"] == "pkg-id"


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
    instead of ``as_json()``'s summarized counts.

    The three ``scale_rebuild*`` keys are the only additions, and they are added
    by the CLI rather than by ``as_json()`` on purpose (see
    ``cli._import_report_as_json``): the rebuild runs after
    ``sync_imports.report_json`` is already written, so the stored report must
    not carry fields it could only ever store empty. This package carried no
    notebook, so the pass ran with nothing to judge."""
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
    assert payload == {
        **report.as_json(),
        "scale_rebuild": {},
        "scale_rebuild_mode": "auto",
        "scale_rebuild_note": None,
    }
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


def test_import_human_output_incremental_prints_mode_base_and_delete_counts(
    tmp_path, monkeypatch, capsys
):
    """PR-3c fields (design doc §8): a real (non-dry-run) incremental import
    prints its mode/base, the delete-replay counts, and both notebook-delete
    lists -- including the "由目标端应用的删除作业完成清理" sentence the
    coordinator asked for, since the import itself only queues the job."""
    _settings(tmp_path, monkeypatch)
    report = _empty_import_report(
        mode="incremental",
        base_package_id="pkg-base",
        notebooks=("nb-1",),
        deletes_applied=3,
        deletes_absent=1,
        deletes_orphan_skipped=2,
        deletes_folded_into_notebook_deletion=4,
        deletes_skipped_for_copying=5,
        notebooks_deleted=("nb-9",),
        notebooks_delete_skipped=("nb-8",),
    )
    monkeypatch.setattr(cli, "import_package", lambda *a, **k: report)
    exit_code = cli.main(["import", str(tmp_path / "pkg")])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "模式: incremental，base=pkg-base" in out
    assert (
        "删除重放: 应用 3、目标端已不存在 1、孤儿跳过 2、"
        "随笔记本删除作业整本清理 4、目标端正在拷贝、本次未动 5" in out
    )
    assert "已排队删除的笔记本: nb-9（由目标端应用的删除作业完成清理）" in out
    assert "跳过删除的笔记本" in out and "nb-8" in out


def test_import_human_output_incremental_empty_window_still_prints_zero_counts(
    tmp_path, monkeypatch, capsys
):
    """An empty incremental window (no rows, no deletes) is a legitimate
    shape (design doc §7 "模式判定") -- the delete-replay line must still
    appear with its zero counts, not be suppressed as if it were a full
    package."""
    _settings(tmp_path, monkeypatch)
    report = _empty_import_report(mode="incremental", base_package_id="pkg-base")
    monkeypatch.setattr(cli, "import_package", lambda *a, **k: report)
    exit_code = cli.main(["import", str(tmp_path / "pkg")])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert (
        "删除重放: 应用 0、目标端已不存在 0、孤儿跳过 0、"
        "随笔记本删除作业整本清理 0、目标端正在拷贝、本次未动 0" in out
    )
    assert "已排队删除的笔记本" not in out
    assert "跳过删除的笔记本" not in out


def test_import_human_output_full_mode_omits_delete_counts(
    tmp_path, monkeypatch, capsys
):
    """A full package's delete-related fields are always 0/empty in
    practice (design doc §8), but the human summary gates on ``mode``, not
    on the counts happening to be zero -- pin that explicitly with non-zero
    values a full package should never actually carry."""
    _settings(tmp_path, monkeypatch)
    report = _empty_import_report(mode="full", deletes_applied=5)
    monkeypatch.setattr(cli, "import_package", lambda *a, **k: report)
    exit_code = cli.main(["import", str(tmp_path / "pkg")])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "删除重放" not in out
    assert "模式: full" in out


def test_import_dry_run_shows_mode_but_omits_delete_counts(
    tmp_path, monkeypatch, capsys
):
    """dry-run never reaches the delete-replay/notebook-delete phases (it
    stops after identity mapping and preflight -- design doc §8), so those
    lines must not appear even for an incremental package; the mode/base
    line comes from classification alone and is shown regardless."""
    _settings(tmp_path, monkeypatch)
    report = _empty_import_report(
        dry_run=True, mode="incremental", base_package_id="pkg-base"
    )
    monkeypatch.setattr(cli, "import_package", lambda *a, **k: report)
    exit_code = cli.main(["import", str(tmp_path / "pkg"), "--dry-run"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "模式: incremental，base=pkg-base" in out
    assert "删除重放" not in out


def test_import_json_includes_incremental_report_fields(tmp_path, monkeypatch, capsys):
    """``--json`` is a straight ``ImportReport.as_json()`` dump (pinned
    generically by test_import_json_uses_report_as_json above); this checks
    the PR-3c field names/values explicitly so a rename shows up here too."""
    _settings(tmp_path, monkeypatch)
    report = _empty_import_report(
        mode="incremental",
        base_package_id="pkg-base",
        deletes_applied=3,
        deletes_absent=1,
        deletes_orphan_skipped=2,
        deletes_folded_into_notebook_deletion=4,
        deletes_skipped_for_copying=5,
        notebooks_deleted=("nb-9",),
        notebooks_delete_skipped=("nb-8",),
    )
    monkeypatch.setattr(cli, "import_package", lambda *a, **k: report)
    exit_code = cli.main(["import", str(tmp_path / "pkg"), "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "incremental"
    assert payload["base_package_id"] == "pkg-base"
    assert payload["deletes_applied"] == 3
    assert payload["deletes_absent"] == 1
    assert payload["deletes_orphan_skipped"] == 2
    assert payload["deletes_folded_into_notebook_deletion"] == 4
    assert payload["deletes_skipped_for_copying"] == 5
    assert payload["notebooks_deleted"] == ["nb-9"]
    assert payload["notebooks_delete_skipped"] == ["nb-8"]


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


def test_status_lists_export_runs_with_dead_flag(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()

    database = SqliteDatabase(settings, tmp_path)
    try:
        _seed_export_lease(
            database,
            target_env="prod-tokyo",
            run_id="run-live",
            package_id="pkg-live",
            floor_seq=42,
            heartbeat_moment=_old_moment(0),
        )
        dead_heartbeat = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat()
        _seed_export_lease(
            database,
            target_env="prod-osaka",
            run_id="run-dead",
            package_id="pkg-dead",
            floor_seq=7,
            heartbeat_moment=dead_heartbeat,
        )
    finally:
        database.close()

    exit_code = cli.main(["status", "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    runs_by_target = {row["target_env"]: row for row in payload["runs"]}
    assert runs_by_target["prod-tokyo"]["floor_seq"] == 42
    assert runs_by_target["prod-tokyo"]["dead"] is False
    assert runs_by_target["prod-osaka"]["floor_seq"] == 7
    assert runs_by_target["prod-osaka"]["dead"] is True
    assert payload["runs_note"] is None

    exit_code = cli.main(["status"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "在途导出" in out
    assert "prod-tokyo" in out
    assert "floor_seq=42" in out
    assert "prod-osaka" in out
    assert "已死" in out


def test_status_degrades_runs_section_when_export_runs_table_is_missing(
    tmp_path, monkeypatch, capsys
):
    """``sync_export_runs`` degrading must be independent of the
    ``sync_export_state`` column degrade -- drop only the lease table here
    and confirm the watermark section still reads its full v85 shape."""
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()

    database = SqliteDatabase(settings, tmp_path)
    try:
        with database.write() as conn:
            conn.execute("DROP TABLE sync_export_runs")

        exit_code = cli.main(["status", "--json"])
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["runs"] == []
        assert "v85/0065" in payload["runs_note"]
        assert "sync_export_runs" in payload["runs_note"]
        # The watermark section is unaffected: it has its own, independent
        # column-level check and this database still has both v85 columns.
        assert payload["exports"] == []
        assert payload["exports_note"] is None

        exit_code = cli.main(["status"])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "在途导出" in out
        assert "v85/0065" in out
    finally:
        database.close()


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


def test_status_reports_chain_head_for_a_two_link_chain(tmp_path, monkeypatch, capsys):
    """Chain head (design doc §8) is the ``done`` row nothing else is
    downstream of -- here a full baseline (pkg-A, to_seq=10) followed by one
    incremental window (pkg-B, to_seq=20) that continues from it. A third,
    unrelated ``failed`` row for the same source_env must not affect the
    result (only ``done`` rows are chain candidates)."""
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
                "finished_at, report_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
                "INSERT INTO sync_imports "
                "(package_id, source_env, from_seq, to_seq, status, started_at, "
                "finished_at, report_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "pkg-C-failed",
                    "prod-shanghai",
                    21,
                    30,
                    "failed",
                    "2026-01-03T00:00:00+00:00",
                    None,
                    json.dumps({"mode": "incremental", "base_package_id": "pkg-B"}),
                ),
            )
    finally:
        database.close()

    exit_code = cli.main(["status", "--json"])
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

    exit_code = cli.main(["status"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "导入链头" in out
    assert "pkg-B（to_seq=20，创建于 2026-01-02T00:00:00+00:00）" in out
    assert "不唯一" not in out


def test_status_reports_no_chain_head_when_nothing_is_done(
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
                    "2026-01-01T00:00:00+00:00",
                    None,
                    json.dumps({}),
                ),
            )
    finally:
        database.close()

    exit_code = cli.main(["status", "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["chain_heads"] == {"prod-shanghai": []}

    exit_code = cli.main(["status"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "尚无入链的已完成包" in out


def _insert_import_row(
    conn,
    *,
    package_id: str,
    source_env: str,
    created_at: str,
    to_seq: int = 0,
    status: str = "done",
    from_seq: int = 0,
    base_package_id: str = "",
    mode: str = "full",
    scoped: bool | None = False,
    notebooks: tuple[str, ...] | None = (),
    started_at: str | None = None,
) -> None:
    """One ``sync_imports`` row, ``report_json`` carrying exactly the keys
    ``_load_sync_status``'s chain-head grouping reads back
    (``package_created_at``/``mode``/``base_package_id``/``scoped``). Shared
    by the chain-head ambiguity/ordering/scale tests below so a 1000-row seed
    does not hand-roll the same INSERT a thousand times.

    ``scoped=None`` writes NO ``scoped`` key at all, which is what a row
    recorded by a build older than that field looks like. ``notebooks=None``
    likewise writes no ``notebooks`` key -- what the mirror survey below
    reads as "this row cannot answer which notebooks it carried".

    ``scoped=True`` forces ``from_seq``/``to_seq`` to 0 no matter what the
    caller asked for: ``export.py`` hard-codes exactly that for a subset
    package ("a subset package makes no claim about a sequence range") and
    ``import_.py`` copies the manifest's numbers into ``sync_imports``, so a
    scoped row with a non-zero range cannot exist in a real database and a
    test must not be able to seed one.

    ``started_at`` defaults to ``created_at`` (the ordinary "imported about
    when it was exported" case) and is passed explicitly only to pin that
    the lag verdict does NOT depend on import order: this column is the
    TARGET's clock, the verdict is about the SOURCE's."""
    if scoped:
        from_seq = 0
        to_seq = 0
    started_at = started_at or created_at
    conn.execute(
        "INSERT INTO sync_imports "
        "(package_id, source_env, from_seq, to_seq, status, started_at, "
        "finished_at, report_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            package_id,
            source_env,
            from_seq,
            to_seq,
            status,
            started_at,
            started_at if status == "done" else None,
            json.dumps(
                {
                    **({} if notebooks is None else {"notebooks": list(notebooks)}),
                    "mode": mode,
                    "base_package_id": base_package_id,
                    "package_created_at": created_at,
                    **({} if scoped is None else {"scoped": scoped}),
                }
            ),
        ),
    )


def test_status_prints_every_head_when_the_chain_has_more_than_one(
    tmp_path, monkeypatch, capsys
):
    """A full baseline and a LATER ``--notebook``-scoped import are BOTH
    valid chain-head candidates -- design doc §8's "已知边界". The scoped one
    took no lease and moved no watermark, so it is downstream of nothing; the
    baseline is older, so it is not downstream of the scoped one either.
    `status` must print and serialize BOTH, not silently pick one the way the
    old row-order-dependent version did (codex review round 2, P2-1).

    Two unscoped baselines a day apart are NOT this case: the later one is
    simply the head (codex #788 r2 P1 -- the rule orders by export time, so
    only a non-participating package can tie with something older)."""
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()

    database = SqliteDatabase(settings, tmp_path)
    try:
        with database.write() as conn:
            _insert_import_row(
                conn,
                package_id="pkg-full-1",
                source_env="prod-osaka",
                to_seq=100,
                created_at="2026-01-01T00:00:00+00:00",
            )
            # A scoped row's range is 0..0 in every real database:
            # ``export.py`` hard-codes it and ``import_.py`` copies the
            # manifest verbatim. That is exactly why ``downstream_of``
            # cannot compare heights here -- the older baseline's to_seq
            # is the HIGHER one.
            _insert_import_row(
                conn,
                package_id="pkg-full-2",
                source_env="prod-osaka",
                created_at="2026-01-02T00:00:00+00:00",
                scoped=True,
            )
    finally:
        database.close()

    exit_code = cli.main(["status", "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["chain_heads"]["prod-osaka"] == [
        {
            "package_id": "pkg-full-1",
            "to_seq": 100,
            "created_at": "2026-01-01T00:00:00+00:00",
        },
        {
            "package_id": "pkg-full-2",
            "to_seq": 0,
            "created_at": "2026-01-02T00:00:00+00:00",
        },
    ]

    exit_code = cli.main(["status"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "pkg-full-1" in out
    assert "pkg-full-2" in out
    # The exact sentence the operations docs quote (record order, not
    # watermark height -- codex #788 r2); a drift back to "to_seq 最大" must red.
    assert "链头不唯一；源端下一个窗口的 base 会是其中记录更晚且非限定导出的那个" in out
    assert "to_seq 最大" not in out


def test_status_chain_head_result_is_independent_of_insertion_order(
    tmp_path, monkeypatch, capsys
):
    """Same two ambiguous heads as above (a baseline and a later scoped
    import), inserted in the OPPOSITE order --
    neither ``sync_imports`` nor ``_load_sync_status``'s in-memory grouping
    carries an ORDER BY that ``_chain_heads_for`` could accidentally depend
    on, so the printed/serialized result must be identical either way (codex
    review round 2, P2-1: this is the mutation the old version's dependence
    on row order would have failed)."""
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()

    database = SqliteDatabase(settings, tmp_path)
    try:
        with database.write() as conn:
            _insert_import_row(
                conn,
                package_id="pkg-full-2",
                source_env="prod-osaka",
                created_at="2026-01-02T00:00:00+00:00",
                scoped=True,
            )
            _insert_import_row(
                conn,
                package_id="pkg-full-1",
                source_env="prod-osaka",
                to_seq=100,
                created_at="2026-01-01T00:00:00+00:00",
            )
    finally:
        database.close()

    exit_code = cli.main(["status", "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["chain_heads"]["prod-osaka"] == [
        {
            "package_id": "pkg-full-1",
            "to_seq": 100,
            "created_at": "2026-01-01T00:00:00+00:00",
        },
        {
            "package_id": "pkg-full-2",
            "to_seq": 0,
            "created_at": "2026-01-02T00:00:00+00:00",
        },
    ]


def test_status_chain_head_scales_to_a_long_done_history(tmp_path, monkeypatch, capsys):
    """1000 ``done`` rows for one ``source_env``: 990 with strictly
    increasing (and therefore never tied, never candidate) ``to_seq``, plus
    10 tied on the actual maximum -- the only rows ``_chain_heads_for`` can
    ever call ``downstream_of`` on, per its own docstring (only the maximum
    ``to_seq`` can possibly qualify as undominated).

    Wall-clock timing alone does not discriminate here: a plain, naive O(n^2)
    pairwise scan over 1000 lightweight objects still finishes in well under
    a second on ordinary hardware (measured ~15ms), so a timing budget large
    enough to be CI-safe would never actually fail if the O(n) + O(k^2)
    narrowing this is supposed to be regressed back to a full O(n^2) scan.
    The real regression guard is the ``downstream_of`` CALL COUNT: narrowed
    to the 10-row tied subset first, the pairwise comparison makes at most
    10*9 = 90 calls; an unnarrowed scan over all 1000 ``done`` rows would
    make roughly 1000*999 ~ 1,000,000 (codex review round 2, P2-2). The
    timing assertion is kept too, as a generous backstop against a much
    worse regression (e.g. a per-candidate database round trip) that a call
    count on this one method would not catch."""
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()

    database = SqliteDatabase(settings, tmp_path)
    try:
        with database.write() as conn:
            for i in range(990):
                _insert_import_row(
                    conn,
                    package_id=f"pkg-lo-{i:04d}",
                    source_env="prod-bulk",
                    to_seq=i,
                    created_at="2026-01-01T00:00:00+00:00",
                )
            # A genuine chain of 10 EMPTY windows tied on to_seq=990 (the
            # ordinary shape after `sync prune-log` dropped an empty stretch
            # of the log -- design doc §7 "导出水位"): each continues from
            # the one before it, so only the last is undominated and the
            # other 9 must each be resolved via the pairwise scan.
            for i in range(10):
                _insert_import_row(
                    conn,
                    package_id=f"pkg-hi-{i:02d}",
                    source_env="prod-bulk",
                    to_seq=990,
                    created_at=f"2026-01-02T00:00:{i:02d}+00:00",
                    base_package_id=f"pkg-hi-{i - 1:02d}" if i > 0 else "",
                    mode="incremental" if i > 0 else "full",
                )
    finally:
        database.close()

    calls = 0
    original_downstream_of = cli._PriorImport.downstream_of

    def _counting_downstream_of(self, base):
        nonlocal calls
        calls += 1
        return original_downstream_of(self, base)

    monkeypatch.setattr(cli._PriorImport, "downstream_of", _counting_downstream_of)

    started = time.monotonic()
    exit_code = cli.main(["status", "--json"])
    elapsed = time.monotonic() - started
    assert exit_code == 0
    assert elapsed < 5.0, f"sync status took {elapsed:.2f}s over 1000 done rows"
    assert calls <= 200, (
        f"_PriorImport.downstream_of was called {calls} times for 1000 done "
        "rows with only 10 tied on the maximum to_seq -- the max-to_seq "
        "narrowing appears to have regressed back to an O(n^2) full scan"
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["chain_heads"]["prod-bulk"] == [
        {
            "package_id": "pkg-hi-09",
            "to_seq": 990,
            "created_at": "2026-01-02T00:00:09+00:00",
        }
    ]


def test_status_reports_pending_notebook_deletes_grouped_by_source_env(
    tmp_path, monkeypatch, capsys
):
    """Design doc §8 "笔记本删除传播": an incremental import only flips a
    mirrored notebook to ``deleting`` and queues a delete job -- ``status``
    surfaces how many are waiting per ``source_env`` so an operator notices a
    stuck job worker rather than assuming the import failed. A locally
    ``deleting`` notebook (``sync_origin=''``) and a ``draft`` mirror must
    not be counted."""
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()

    database = SqliteDatabase(settings, tmp_path)
    rows = [
        ("nb-sh-1", "deleting", "prod-shanghai"),
        ("nb-sh-2", "deleting", "prod-shanghai"),
        ("nb-tokyo-1", "deleting", "prod-tokyo"),
        ("nb-sh-draft", "draft", "prod-shanghai"),
        ("nb-local", "deleting", ""),
    ]
    try:
        with database.write() as conn:
            for notebook_id, status, sync_origin in rows:
                conn.execute(
                    "INSERT INTO notebooks(id,name,purpose,primary_domain,status,"
                    "created_by,created_at,updated_at,sync_origin) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        notebook_id,
                        notebook_id,
                        "",
                        "",
                        status,
                        None,
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:00:00+00:00",
                        sync_origin,
                    ),
                )
    finally:
        database.close()

    exit_code = cli.main(["status", "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pending_notebook_deletes"] == {
        "prod-shanghai": 2,
        "prod-tokyo": 1,
    }

    exit_code = cli.main(["status"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "等待删除作业清理的镜像 2 个（由目标端应用的删除作业完成）" in out
    assert "等待删除作业清理的镜像 1 个（由目标端应用的删除作业完成）" in out


# --------------------------------------------- 镜像落后巡检 (_classify_mirrors)
#
# 判据见 docs/incremental-sync-design.md §9：不落 `sync_applied_through_seq` 列，
# 按 `sync_imports.report_json` 推导，并且**按记录顺序（created_at）判，不比
# 水位高度**——`export.py` 对 scoped 包硬写 `from_seq, to_seq = 0, 0`，
# `import_.py` 原样写进 `sync_imports`，所以数据库里任何 scoped 行的 to_seq 恒为
# 0，比高低只会得出「恒定落后一个链头水位」这一个答案。`_prior_import` 下面的
# 断言把这条事实钉住，免得用例再造出一个库里不可能存在的 scoped 行。
# `_classify_mirrors` 是纯函数，这一组用例不碰数据库；真 `_load_sync_status` 的
# 集成用例在下面（PostgreSQL 同款在 tests/postgres/test_sync_cli_pg.py）。


def _prior_import(package_id: str, **overrides) -> _PriorImport:
    fields = dict(
        package_id=package_id,
        status="done",
        started_at=None,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        heartbeat_at=None,
        has_progress=False,
        notebooks=("nb-1",),
        from_seq=0,
        to_seq=10,
        base_package_id="",
        scoped=False,
    )
    fields.update(overrides)
    if fields["scoped"]:
        # export.py: "A subset package makes no claim about a sequence range"
        fields["from_seq"] = 0
        fields["to_seq"] = 0
    return _PriorImport(**fields)


def _mirror_row(
    notebook_id: str = "nb-1",
    *,
    name: str = "",
    status: str = "draft",
    sync_origin: str = "prod-shanghai",
) -> dict:
    return {
        "id": notebook_id,
        "name": name or notebook_id,
        "status": status,
        "sync_origin": sync_origin,
    }


def test_classify_mirrors_chain_participant_is_in_sync():
    """判据 2：参与链的包之后的窗口覆盖整个源端 scope，所以这本跟链一样新——
    包括之后没有任何窗口再点名它（那只说明它没变过）。"""
    package = _prior_import("pkg-A")
    head = _prior_import(
        "pkg-B",
        to_seq=20,
        base_package_id="pkg-A",
        created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    mirrors = cli._classify_mirrors(
        [_mirror_row()], {"prod-shanghai": [package]}, {"prod-shanghai": [head]}
    )
    group = mirrors["prod-shanghai"]
    assert group["total"] == 1
    assert [entry["notebook_id"] for entry in group["in_sync"]] == ["nb-1"]
    assert group["in_sync"][0]["head_package_id"] == "pkg-B"
    assert group["in_sync"][0]["applied_package_id"] == "pkg-A"
    assert group["lagging"] == []
    assert group["ahead"] == []
    assert group["ambiguous"] == []


def test_classify_mirrors_chain_participant_with_two_heads_is_ambiguous():
    package = _prior_import("pkg-A")
    heads = [
        _prior_import("pkg-B", to_seq=20),
        _prior_import("pkg-C", to_seq=20),
    ]
    mirrors = cli._classify_mirrors(
        [_mirror_row()], {"prod-shanghai": [package]}, {"prod-shanghai": heads}
    )
    group = mirrors["prod-shanghai"]
    assert group["in_sync"] == []
    assert [entry["notebook_id"] for entry in group["ambiguous"]] == ["nb-1"]
    assert group["ambiguous"][0]["head_package_id"] is None


def test_classify_mirrors_chain_participant_without_a_head_is_ambiguous():
    """理论上不可能（这个包自己就是链头候选），但仍然回答而不是默认通过。"""
    package = _prior_import("pkg-A")
    mirrors = cli._classify_mirrors(
        [_mirror_row()], {"prod-shanghai": [package]}, {"prod-shanghai": []}
    )
    assert [entry["notebook_id"] for entry in mirrors["prod-shanghai"]["ambiguous"]] == [
        "nb-1"
    ]


def test_classify_mirrors_scoped_snapshot_that_overwrote_a_window_is_lagging():
    """真落后的唯一形态：某个参与链的窗口带过这本的改动（`nb-1 in
    notebooks`）、导出时间晚于这份 scoped 快照、而且**先于**它被导入——于是
    这份旧快照把窗口已经写下的新行盖了回去，后续窗口也不会再补（窗口只装
    自己范围内有变更的本）。条目里的 head 指向被盖掉的那个窗口。"""
    window = _prior_import(
        "pkg-window",
        to_seq=20,
        base_package_id="pkg-A",
        created_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
        started_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
    )
    package = _prior_import(
        "pkg-scoped",
        scoped=True,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        # 一月的快照，六月才导入——落在窗口的行上面。
        started_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    mirrors = cli._classify_mirrors(
        [_mirror_row(name="镜像本")],
        {"prod-shanghai": [window, package]},
        {"prod-shanghai": [window]},
    )
    group = mirrors["prod-shanghai"]
    assert group["in_sync"] == []
    assert group["lagging"] == [
        {
            "notebook_id": "nb-1",
            "name": "镜像本",
            "applied_package_id": "pkg-scoped",
            "applied_created_at": "2026-01-01T00:00:00+00:00",
            "head_package_id": "pkg-window",
            "head_created_at": "2026-01-05T00:00:00+00:00",
            "deleting": False,
        }
    ]


def test_classify_mirrors_older_scoped_snapshot_imported_first_is_in_sync():
    """codex #791 r2 P2 的反例，正常的日常节奏：全量 → `sync export
    --notebook A`（t1）导入 → 之后一个只带 B 改动的窗口（t2 > t1）导入。
    A 完全是最新的——它若在 (t1, t2] 有改动，窗口就会带上它——不能报落后。"""
    package = _prior_import(
        "pkg-scoped",
        scoped=True,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    window = _prior_import(
        "pkg-window",
        to_seq=20,
        base_package_id="pkg-A",
        notebooks=("nb-other",),  # 这一轮只有 B 变过
        created_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
        started_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
    )
    mirrors = cli._classify_mirrors(
        [_mirror_row()],
        {"prod-shanghai": [package, window]},
        {"prod-shanghai": [window]},
    )
    group = mirrors["prod-shanghai"]
    assert group["lagging"] == []
    assert [entry["notebook_id"] for entry in group["in_sync"]] == ["nb-1"]


def test_classify_mirrors_window_imported_after_the_scoped_snapshot_is_in_sync():
    """同一形态，但窗口**带了** A：窗口在 scoped 包之后导入，所以窗口的行压在
    上面，什么都没丢。只有「更晚导出、更早导入」才是落后。"""
    package = _prior_import(
        "pkg-scoped",
        scoped=True,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    window = _prior_import(
        "pkg-window",
        to_seq=20,
        base_package_id="pkg-A",
        created_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
        started_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
    )
    mirrors = cli._classify_mirrors(
        [_mirror_row()],
        {"prod-shanghai": [package, window]},
        {"prod-shanghai": [window]},
    )
    group = mirrors["prod-shanghai"]
    assert group["lagging"] == []
    assert [entry["notebook_id"] for entry in group["in_sync"]] == ["nb-1"]


def test_classify_mirrors_unorderable_import_times_are_unknown():
    """第三条判据要的是目标端自己的导入时钟（`started_at`）：缺了就排不出
    先后，只能报 unknown，不猜。"""
    window = _prior_import(
        "pkg-window",
        to_seq=20,
        base_package_id="pkg-A",
        created_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
        started_at=None,
    )
    package = _prior_import(
        "pkg-scoped",
        scoped=True,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        started_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    mirrors = cli._classify_mirrors(
        [_mirror_row()],
        {"prod-shanghai": [window, package]},
        {"prod-shanghai": [window]},
    )
    group = mirrors["prod-shanghai"]
    assert group["lagging"] == []
    assert group["in_sync"] == []
    assert [entry["notebook_id"] for entry in group["unknown"]] == ["nb-1"]


def test_classify_mirrors_scoped_snapshot_newer_than_the_head_is_ahead():
    package = _prior_import(
        "pkg-scoped", scoped=True, created_at=datetime(2026, 1, 9, tzinfo=timezone.utc)
    )
    head = _prior_import(
        "pkg-B", to_seq=20, created_at=datetime(2026, 1, 5, tzinfo=timezone.utc)
    )
    mirrors = cli._classify_mirrors(
        [_mirror_row()], {"prod-shanghai": [package]}, {"prod-shanghai": [head]}
    )
    group = mirrors["prod-shanghai"]
    assert group["lagging"] == []
    assert [entry["notebook_id"] for entry in group["ahead"]] == ["nb-1"]
    assert group["ahead"][0]["applied_created_at"] == "2026-01-09T00:00:00+00:00"
    assert group["ahead"][0]["head_created_at"] == "2026-01-05T00:00:00+00:00"


def test_classify_mirrors_scoped_snapshot_exactly_at_the_head_is_ahead():
    """边界：`created_at == head.created_at` 归 ahead——同一时刻的快照不可能
    漏掉链头已覆盖的东西。"""
    moment = datetime(2026, 1, 5, tzinfo=timezone.utc)
    package = _prior_import("pkg-scoped", scoped=True, created_at=moment)
    head = _prior_import("pkg-B", to_seq=20, created_at=moment)
    mirrors = cli._classify_mirrors(
        [_mirror_row()], {"prod-shanghai": [package]}, {"prod-shanghai": [head]}
    )
    group = mirrors["prod-shanghai"]
    assert group["lagging"] == []
    assert [entry["notebook_id"] for entry in group["ahead"]] == ["nb-1"]


def test_classify_mirrors_scoped_package_without_a_readable_created_at_is_unknown():
    """两个时间戳都要可读才谈得上比较：缺 `package_created_at` 的旧行只能报
    unknown，不能拿目标端的 `started_at`（另一个时钟）去跟源端时间比。"""
    package = _prior_import("pkg-scoped", scoped=True, created_at=None)
    head = _prior_import(
        "pkg-B", to_seq=20, created_at=datetime(2026, 1, 5, tzinfo=timezone.utc)
    )
    mirrors = cli._classify_mirrors(
        [_mirror_row()], {"prod-shanghai": [package]}, {"prod-shanghai": [head]}
    )
    group = mirrors["prod-shanghai"]
    assert group["lagging"] == []
    assert group["ahead"] == []
    assert [entry["notebook_id"] for entry in group["unknown"]] == ["nb-1"]
    assert group["unknown"][0]["applied_package_id"] == "pkg-scoped"
    assert group["unknown"][0]["applied_created_at"] is None


def test_classify_mirrors_scoped_package_without_any_head_is_no_chain():
    package = _prior_import("pkg-scoped", scoped=True)
    mirrors = cli._classify_mirrors(
        [_mirror_row()], {"prod-shanghai": [package]}, {"prod-shanghai": []}
    )
    group = mirrors["prod-shanghai"]
    assert [entry["notebook_id"] for entry in group["no_chain"]] == ["nb-1"]
    assert group["no_chain"][0]["applied_package_id"] == "pkg-scoped"
    assert group["no_chain"][0]["head_package_id"] is None


def test_classify_mirrors_scoped_package_with_two_heads_is_ambiguous():
    package = _prior_import("pkg-scoped", scoped=True)
    heads = [_prior_import("pkg-B", to_seq=20), _prior_import("pkg-C", to_seq=20)]
    mirrors = cli._classify_mirrors(
        [_mirror_row()], {"prod-shanghai": [package]}, {"prod-shanghai": heads}
    )
    assert [
        entry["notebook_id"] for entry in mirrors["prod-shanghai"]["ambiguous"]
    ] == ["nb-1"]


def test_classify_mirrors_a_scoped_head_is_not_a_baseline():
    """`_chain_heads_for` 会把 scoped 包当合法链头（它回答的是「下一个包的
    base 可以指向谁」），但 scoped 包不覆盖任何 scope，不能当判据基准——否则
    「只导入过 scoped 包」会被判成 `ahead`（文案说「无需动作」），而实际上
    这里没有链可续，下一个增量包会被 `_assert_chain` 拒。"""
    package = _prior_import("pkg-scoped", scoped=True)
    mirrors = cli._classify_mirrors(
        [_mirror_row()], {"prod-shanghai": [package]}, {"prod-shanghai": [package]}
    )
    group = mirrors["prod-shanghai"]
    assert group["ahead"] == []
    assert [entry["notebook_id"] for entry in group["no_chain"]] == ["nb-1"]


def test_classify_mirrors_an_extra_scoped_head_does_not_make_everything_ambiguous():
    """最后一个窗口之后又导了一个 scoped 包：链头确实变成两条，但只有一条
    参与链——没被 scoped 包碰过的镜像位置并不含糊，仍然是 in_sync。"""
    window = _prior_import("pkg-window", to_seq=30, base_package_id="pkg-full")
    late_scoped = _prior_import(
        "pkg-late",
        scoped=True,
        notebooks=("nb-other",),
        created_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
    )
    mirrors = cli._classify_mirrors(
        [_mirror_row()],
        {"prod-shanghai": [window, late_scoped]},
        {"prod-shanghai": [window, late_scoped]},
    )
    group = mirrors["prod-shanghai"]
    assert group["ambiguous"] == []
    assert [entry["notebook_id"] for entry in group["in_sync"]] == ["nb-1"]


def test_classify_mirrors_source_env_never_imported_is_unknown():
    """判据 4：目标端有 sync_origin=E 的镜像，但 E 在 sync_imports 里一行都没有。"""
    mirrors = cli._classify_mirrors([_mirror_row(sync_origin="prod-osaka")], {}, {})
    group = mirrors["prod-osaka"]
    assert group["total"] == 1
    assert [entry["notebook_id"] for entry in group["unknown"]] == ["nb-1"]
    assert group["unknown"][0]["applied_package_id"] is None
    assert group["unknown"][0]["applied_created_at"] is None


def test_classify_mirrors_package_that_does_not_name_this_notebook_is_unknown():
    package = _prior_import("pkg-A", notebooks=("nb-other",))
    mirrors = cli._classify_mirrors(
        [_mirror_row()], {"prod-shanghai": [package]}, {"prod-shanghai": [package]}
    )
    assert [entry["notebook_id"] for entry in mirrors["prod-shanghai"]["unknown"]] == [
        "nb-1"
    ]


def test_classify_mirrors_unreadable_report_json_is_unknown_not_empty_coverage():
    """``_PriorImport.notebooks is None`` 是「答不上来」，不是「什么都没带」——
    旧行/JSON 不可读只能报 unknown，不能拿来当覆盖判据。"""
    package = _prior_import("pkg-A", notebooks=None)
    mirrors = cli._classify_mirrors(
        [_mirror_row()], {"prod-shanghai": [package]}, {"prod-shanghai": [package]}
    )
    assert [entry["notebook_id"] for entry in mirrors["prod-shanghai"]["unknown"]] == [
        "nb-1"
    ]


def test_classify_mirrors_only_done_packages_count():
    """failed/running 的包碰过这本也不算数——它没有把这本落成任何一个快照。"""
    failed = _prior_import("pkg-failed", status="failed", scoped=True)
    mirrors = cli._classify_mirrors(
        [_mirror_row()], {"prod-shanghai": [failed]}, {"prod-shanghai": []}
    )
    assert [entry["notebook_id"] for entry in mirrors["prod-shanghai"]["unknown"]] == [
        "nb-1"
    ]


def test_classify_mirrors_picks_the_package_that_wrote_last_by_import_time():
    """排序口径：选「最后写过这本的包」按目标端的 `started_at`（谁最后应用，
    谁的行在上面），缺 `started_at` 的旧行才退回 `created_at`——这里选中的是
    六月才导入的 pkg-late，尽管它连导出时间都没记。选中之后它答不上导出
    时间，于是判 unknown（两个时钟不能互比）。"""
    early = _prior_import(
        "pkg-early",
        scoped=True,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    late = _prior_import(
        "pkg-late",
        scoped=True,
        created_at=None,
        started_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    head = _prior_import("pkg-head", to_seq=20)
    mirrors = cli._classify_mirrors(
        [_mirror_row()],
        {"prod-shanghai": [early, late]},
        {"prod-shanghai": [head]},
    )
    unknown = mirrors["prod-shanghai"]["unknown"]
    assert [entry["applied_package_id"] for entry in unknown] == ["pkg-late"]


def test_classify_mirrors_deleting_mirror_gets_its_own_bucket():
    """判据 5：等删除作业清理的镜像单独进 `deleting` 桶，不再进其它桶——
    这样监控可以直接拿 `len(lagging) > 0` 当报警条件。"""
    package = _prior_import(
        "pkg-scoped",
        scoped=True,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        started_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    head = _prior_import(
        "pkg-B",
        to_seq=20,
        created_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
        started_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
    )
    mirrors = cli._classify_mirrors(
        [_mirror_row(status="deleting")],
        {"prod-shanghai": [package, head]},
        {"prod-shanghai": [head]},
    )
    group = mirrors["prod-shanghai"]
    # 不做这一层短路的话这本会是 lagging（快照比链头旧）。
    assert group["lagging"] == []
    assert [entry["notebook_id"] for entry in group["deleting"]] == ["nb-1"]
    assert group["deleting"][0]["deleting"] is True
    # 条目仍然带着算出来的证据，运维看行就知道最后是哪个包碰的。
    assert group["deleting"][0]["applied_package_id"] == "pkg-scoped"


def test_classify_mirrors_groups_by_sync_origin():
    shanghai = _prior_import("pkg-sh", notebooks=("nb-1",))
    osaka = _prior_import(
        "pkg-os",
        notebooks=("nb-2",),
        scoped=True,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        started_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    osaka_head = _prior_import(
        "pkg-os-head",
        to_seq=9,
        notebooks=("nb-2",),
        created_at=datetime(2026, 1, 4, tzinfo=timezone.utc),
        started_at=datetime(2026, 1, 4, tzinfo=timezone.utc),
    )
    mirrors = cli._classify_mirrors(
        [_mirror_row("nb-1"), _mirror_row("nb-2", sync_origin="prod-osaka")],
        {"prod-shanghai": [shanghai], "prod-osaka": [osaka, osaka_head]},
        {"prod-shanghai": [shanghai], "prod-osaka": [osaka_head]},
    )
    assert sorted(mirrors) == ["prod-osaka", "prod-shanghai"]
    assert [entry["notebook_id"] for entry in mirrors["prod-shanghai"]["in_sync"]] == [
        "nb-1"
    ]
    assert [entry["notebook_id"] for entry in mirrors["prod-osaka"]["lagging"]] == [
        "nb-2"
    ]


def test_classify_mirrors_caps_detail_lines_per_bucket_in_the_human_output(capsys):
    """`status` 是巡检不是转储：每桶最多 20 行，超出打「另有 N 本」。"""
    covered = tuple(f"nb-{index}" for index in range(25))
    head = _prior_import(
        "pkg-head",
        to_seq=20,
        notebooks=covered,
        created_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
        started_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
    )
    scoped = _prior_import(
        "pkg-scoped",
        scoped=True,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        started_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        notebooks=covered,
    )
    mirrors = cli._classify_mirrors(
        [_mirror_row(f"nb-{index}") for index in range(25)],
        {"prod-shanghai": [head, scoped]},
        {"prod-shanghai": [head]},
    )
    assert len(mirrors["prod-shanghai"]["lagging"]) == 25
    cli._print_mirror_section(mirrors)
    out = capsys.readouterr().out
    assert out.count("落后 nb-") == 20
    assert "另有 5 本落后的镜像未列出" in out


def test_classify_mirrors_without_any_mirror_returns_empty():
    assert cli._classify_mirrors([], {"prod-shanghai": []}, {"prod-shanghai": []}) == {}


def _insert_mirror(conn, notebook_id, sync_origin, *, status="draft"):
    conn.execute(
        "INSERT INTO notebooks(id,name,purpose,primary_domain,status,"
        "created_by,created_at,updated_at,sync_origin) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            notebook_id,
            notebook_id,
            "",
            "",
            status,
            None,
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
            sync_origin,
        ),
    )


def test_status_surveys_mirrors_against_the_chain_head(tmp_path, monkeypatch, capsys):
    """真 ``_load_sync_status``：全量包 → 一个**更旧**的 scoped 包（导出于
    01-02，但六月才导入）→ 一个窗口（导出于 01-03，当天就导入，带了
    nb-chain/nb-scoped/nb-gone 的改动）。nb-chain 最后由窗口写下（in_sync）；
    nb-scoped 最后落下的是那份一月快照，把窗口 01-03 已经应用的改动盖了回去
    （lagging，head 指向被盖掉的窗口）；nb-gone 同形态但在等删除作业
    （deleting 桶，不在 lagging 里）；nb-orphan 的 sync_origin 指向一个从未
    导入过的环境（unknown）。"""
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()

    database = SqliteDatabase(settings, tmp_path)
    try:
        with database.write() as conn:
            _insert_import_row(
                conn,
                package_id="pkg-full",
                source_env="prod-shanghai",
                to_seq=10,
                created_at="2026-01-01T00:00:00+00:00",
                notebooks=("nb-chain", "nb-scoped", "nb-gone"),
            )
            _insert_import_row(
                conn,
                package_id="pkg-scoped",
                source_env="prod-shanghai",
                created_at="2026-01-02T00:00:00+00:00",
                # 一月的快照六月才导入：它落在窗口的行**上面**——这正是真落后
                # 的唯一形态（更晚导出、更早导入的窗口被盖回去了）。
                started_at="2026-06-01T00:00:00+00:00",
                scoped=True,
                notebooks=("nb-scoped", "nb-gone"),
            )
            _insert_import_row(
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
            _insert_mirror(conn, "nb-chain", "prod-shanghai")
            _insert_mirror(conn, "nb-scoped", "prod-shanghai")
            _insert_mirror(conn, "nb-gone", "prod-shanghai", status="deleting")
            _insert_mirror(conn, "nb-orphan", "prod-osaka")
    finally:
        database.close()

    exit_code = cli.main(["status", "--json"])
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
    # 同样被那个更旧的 scoped 包碰过，但它在等删除作业：单独进 deleting 桶，
    # 不进 lagging，这样 len(lagging) 可以直接当报警条件。
    assert [entry["notebook_id"] for entry in shanghai["deleting"]] == ["nb-gone"]
    assert shanghai["ahead"] == []
    assert shanghai["unknown"] == []
    osaka = payload["mirrors"]["prod-osaka"]
    assert osaka["total"] == 1
    assert [entry["notebook_id"] for entry in osaka["unknown"]] == ["nb-orphan"]

    exit_code = cli.main(["status"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "镜像笔记本" in out
    assert (
        "-> prod-shanghai: 共 3 本（已跟上 1，落后 1，领先 0，无链 0，不确定 0，"
        "未知 0，待删除 1）"
    ) in out
    assert (
        "落后 nb-scoped（nb-scoped）：最近带过它的是 pkg-scoped"
        "（2026-01-02T00:00:00+00:00），它盖掉了更晚导出、更早导入的 pkg-window"
        "（2026-01-03T00:00:00+00:00）；"
    ) in out
    assert "现在对它再做一次 sync export --notebook 并导入" in out
    assert "待删除 nb-gone（nb-gone）：" in out
    # 判据 4（该 source_env 从未在本环境导入过）也走 unknown，所以文案必须把
    # 三种成因都说全，不能只说「导入早于本记账」。
    assert (
        "未知 nb-orphan（nb-orphan）：没有任何 done 导入记录能说明这本的位置："
        "该源环境从未在本环境导入过、只有 failed/running 的行，"
        "或者记录早于本记账、report_json 不可读"
    ) in out


def test_status_mirror_of_a_scoped_only_source_env_is_no_chain(
    tmp_path, monkeypatch, capsys
):
    """只导入过 scoped 包的环境：`_chain_heads_for` 会把那个 scoped 包当成
    合法链头（它下游没有任何东西，也不在任何东西下游），但它不覆盖任何
    scope——拿它当基准会得出 `ahead`（「无需动作」），而实际上这里根本没有链
    可续，下一个增量包会被 `_assert_chain` 直接拒。基准必须只取参与链的链头，
    于是这里是 `no_chain`（先导一次全量）。"""
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()

    database = SqliteDatabase(settings, tmp_path)
    try:
        with database.write() as conn:
            _insert_import_row(
                conn,
                package_id="pkg-scoped-only",
                source_env="prod-osaka",
                created_at="2026-01-01T00:00:00+00:00",
                scoped=True,
                notebooks=("nb-only",),
            )
            _insert_mirror(conn, "nb-only", "prod-osaka")
    finally:
        database.close()

    exit_code = cli.main(["status", "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    osaka = payload["mirrors"]["prod-osaka"]
    assert osaka["ahead"] == []
    assert osaka["ambiguous"] == []
    assert osaka["no_chain"] == [
        {
            "notebook_id": "nb-only",
            "name": "nb-only",
            "applied_package_id": "pkg-scoped-only",
            "applied_created_at": "2026-01-01T00:00:00+00:00",
            "head_package_id": None,
            "head_created_at": None,
            "deleting": False,
        }
    ]

    exit_code = cli.main(["status"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert (
        "无链 nb-only（nb-only）：最近带过它的是 pkg-scoped-only"
        "（2026-01-01T00:00:00+00:00）；"
    ) in out
    assert "从未做过覆盖全 scope 的导入（只有 scoped 导入），先导入一次全量包" in out


def test_status_scoped_import_after_the_last_window_does_not_blur_every_mirror(
    tmp_path, monkeypatch, capsys
):
    """全量 + 窗口之后又临时 `sync export --notebook` 导了一个 scoped 包
    （`created_at` **晚于**窗口）：那个 scoped 包也是一个合法链头（没有更晚
    的、参与链的包越过它），所以 `chain_heads` 里确实有两条。但它只碰了一
    本——没被它碰过的镜像位置并不含糊，仍然是 `in_sync`；被它碰过的那本快照
    不早于链头，所以是 `ahead`（下一次窗口幂等覆盖，无需动作）。"""
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()

    database = SqliteDatabase(settings, tmp_path)
    try:
        with database.write() as conn:
            _insert_import_row(
                conn,
                package_id="pkg-full",
                source_env="prod-shanghai",
                to_seq=10,
                created_at="2026-01-01T00:00:00+00:00",
                notebooks=("nb-chain", "nb-patched"),
            )
            _insert_import_row(
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
            _insert_import_row(
                conn,
                package_id="pkg-late-scoped",
                source_env="prod-shanghai",
                created_at="2026-01-03T00:00:00+00:00",
                scoped=True,
                notebooks=("nb-patched",),
            )
            _insert_mirror(conn, "nb-chain", "prod-shanghai")
            _insert_mirror(conn, "nb-patched", "prod-shanghai")
    finally:
        database.close()

    exit_code = cli.main(["status", "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    # 链头确实有两条（窗口 + 那个更晚的 scoped 包），巡检不受影响。
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

    exit_code = cli.main(["status"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert (
        "领先 nb-patched（nb-patched）：最近带过它的是 pkg-late-scoped"
        "（2026-01-03T00:00:00+00:00），不早于链头 pkg-window"
        "（2026-01-02T00:00:00+00:00）；"
    ) in out
    assert "下一次窗口会按幂等 upsert 覆盖，无需动作" in out


def test_status_mirror_section_says_so_when_there_are_no_mirrors(
    tmp_path, monkeypatch, capsys
):
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()

    exit_code = cli.main(["status"])
    assert exit_code == 0
    assert "（本环境没有镜像笔记本）" in capsys.readouterr().out

    exit_code = cli.main(["status", "--json"])
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["mirrors"] == {}


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


def test_build_parser_prune_log_accepts_zero_keep_days():
    args = cli.build_parser().parse_args(["prune-log", "--keep-days", "0"])
    assert args.keep_days == 0


def test_build_parser_prune_log_rejects_negative_keep_days(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args(["prune-log", "--keep-days", "-1"])
    assert excinfo.value.code == 2
    assert "--keep-days" in capsys.readouterr().err


def test_build_parser_prune_log_rejects_non_integer_keep_days(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args(["prune-log", "--keep-days", "abc"])
    assert excinfo.value.code == 2
    assert "--keep-days" in capsys.readouterr().err


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


def _seed_export_lease(
    database,
    *,
    target_env: str = "prod-tokyo",
    run_id: str = "run-1",
    package_id: str = "pkg-inflight",
    floor_seq: int,
    heartbeat_moment: str,
) -> None:
    with database.write() as conn:
        conn.execute(
            "INSERT INTO sync_export_runs "
            "(target_env, run_id, package_id, started_at, heartbeat_at, floor_seq) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                target_env,
                run_id,
                package_id,
                heartbeat_moment,
                heartbeat_moment,
                floor_seq,
            ),
        )


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


def test_prune_log_active_lease_narrows_the_seq_bound(tmp_path, monkeypatch, capsys):
    """An in-flight (not-yet-published) export's lease is a lower bound on
    the watermark it will eventually publish -- prune-log must not delete
    rows above the lease's floor_seq even though a captured watermark alone
    would allow it."""
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()
    database = SqliteDatabase(settings, tmp_path)
    try:
        _seed_captured_watermark(database, exported_through_seq=100)
        _seed_export_lease(
            database, floor_seq=50, heartbeat_moment=_old_moment(0)
        )
        old = _old_moment(40)
        _insert_log_row(database, seq=40, changed_at=old)  # <= 50: eligible
        _insert_log_row(database, seq=60, changed_at=old)  # > 50, <= 100: held back by the lease

        exit_code = cli.main(["prune-log", "--json"])
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["min_seq"] == 50
        # SQLite has no txid dimension at all: sync_export_runs.floor_xmin is
        # NULL on every SQLite row (by construction) and must never make
        # prune-log refuse or otherwise change behavior there -- the txid
        # bound stays None regardless of how many active leases exist.
        assert payload["min_txid"] is None
        assert payload["deleted"] == 1
        assert payload["active_leases"] == [
            {"target_env": "prod-tokyo", "floor_seq": 50}
        ]
        assert payload["dead_leases"] == []

        with database.connect() as conn:
            remaining = {
                row["seq"]
                for row in conn.execute("SELECT seq FROM sync_change_log").fetchall()
            }
            assert remaining == {60}
            # Eviction targets DEAD leases only -- a live one must survive a
            # real (non-dry-run) prune-log run untouched.
            surviving = conn.execute(
                "SELECT target_env FROM sync_export_runs"
            ).fetchall()
            assert [row["target_env"] for row in surviving] == ["prod-tokyo"]
    finally:
        database.close()


def test_prune_log_dead_lease_does_not_narrow_the_bound_but_is_evicted(
    tmp_path, monkeypatch, capsys
):
    """A lease whose heartbeat has gone stale (>1h) no longer represents a
    run in progress and must not hold the deletion bound down. The real
    (non-dry-run) run also EVICTS it -- codex #784 r6: leaving a dead
    lease's row in place let a stalled export that later resumed and
    refreshed its own unchanged heartbeat look live again to the publish
    ownership check, passing it with a snapshot whose compensation window
    prune-log had already (correctly) pruned rows out of. Deleting the row
    the moment it is classified dead closes that window -- there is nothing
    left for the resurrection to find."""
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()
    database = SqliteDatabase(settings, tmp_path)
    try:
        _seed_captured_watermark(database, exported_through_seq=100)
        dead_heartbeat = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat()
        _seed_export_lease(
            database,
            target_env="prod-osaka",
            run_id="run-dead",
            floor_seq=10,
            heartbeat_moment=dead_heartbeat,
        )
        old = _old_moment(40)
        _insert_log_row(database, seq=90, changed_at=old)

        exit_code = cli.main(["prune-log", "--json"])
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["min_seq"] == 100  # the dead lease's floor_seq=10 did not narrow it
        assert payload["deleted"] == 1
        assert payload["active_leases"] == []
        assert payload["dead_leases"] == [
            {
                "target_env": "prod-osaka",
                "run_id": "run-dead",
                "heartbeat_at": dead_heartbeat,
            }
        ]

        # The lease row itself is gone -- eviction, not just exclusion from
        # the bound.
        with database.connect() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) AS n FROM sync_export_runs"
            ).fetchone()
            assert remaining["n"] == 0
    finally:
        database.close()


def test_prune_log_deletes_a_dead_lease_even_if_its_heartbeat_is_refreshed_between_classification_and_delete(
    tmp_path, monkeypatch, capsys
):
    """codex #784 r6: the eviction ``DELETE`` matches ``(target_env,
    run_id)`` alone and never re-checks ``heartbeat_at``. Re-deriving "is it
    dead" a second time at delete time -- instead of trusting the
    classification this same locked transaction already made -- is exactly
    the bug: a stalled export resuming and refreshing its own lease's
    heartbeat between classification and delete would then save a lease this
    call had already excluded from the bound (and possibly already pruned
    rows out from under). Simulate that landing directly, inside the same
    transaction, right where the lock is supposed to make it impossible in
    production -- this pins the DELETE's own contract regardless of the
    lock."""
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()
    database = SqliteDatabase(settings, tmp_path)
    try:
        _seed_captured_watermark(database, exported_through_seq=100)
        dead_heartbeat = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat()
        _seed_export_lease(
            database,
            target_env="prod-osaka",
            run_id="run-dead",
            floor_seq=10,
            heartbeat_moment=dead_heartbeat,
        )

        real_bounds = cli._prune_log_bounds

        def _refresh_then_return(source, conn, **kwargs):
            result = real_bounds(source, conn, **kwargs)
            # The lock (begin_immediate on SQLite, FOR UPDATE on PostgreSQL)
            # is supposed to make this impossible from another connection;
            # done here, on the SAME connection/transaction right after
            # classification, to exercise the DELETE's own contract in
            # isolation from the locking that would normally prevent it.
            conn.execute(
                "UPDATE sync_export_runs SET heartbeat_at = ? WHERE target_env = ?",
                (datetime.now(timezone.utc).isoformat(), "prod-osaka"),
            )
            return result

        monkeypatch.setattr(cli, "_prune_log_bounds", _refresh_then_return)

        exit_code = cli.main(["prune-log"])
        assert exit_code == 0

        with database.connect() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) AS n FROM sync_export_runs"
            ).fetchone()
            # Deleted despite the fresh heartbeat -- the DELETE trusted the
            # classification instead of re-deriving it.
            assert remaining["n"] == 0
    finally:
        database.close()


def test_prune_log_human_output_names_an_evicted_dead_lease(
    tmp_path, monkeypatch, capsys
):
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()
    database = SqliteDatabase(settings, tmp_path)
    try:
        _seed_captured_watermark(database, exported_through_seq=100)
        dead_heartbeat = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat()
        _seed_export_lease(
            database,
            target_env="prod-osaka",
            run_id="run-dead",
            floor_seq=10,
            heartbeat_moment=dead_heartbeat,
        )

        exit_code = cli.main(["prune-log"])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "已删除死租约" in out
        assert "prod-osaka" in out
        assert "run-dead" in out
    finally:
        database.close()


def test_prune_log_dry_run_names_a_dead_lease_as_not_yet_deleted(
    tmp_path, monkeypatch, capsys
):
    """``--dry-run`` classifies dead leases the same way a real run does, but
    must never evict them -- the row must still be there afterwards, and the
    wording must say it WOULD be deleted, not that it was."""
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()
    database = SqliteDatabase(settings, tmp_path)
    try:
        _seed_captured_watermark(database, exported_through_seq=100)
        dead_heartbeat = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat()
        _seed_export_lease(
            database,
            target_env="prod-osaka",
            run_id="run-dead",
            floor_seq=10,
            heartbeat_moment=dead_heartbeat,
        )

        exit_code = cli.main(["prune-log", "--dry-run", "--json"])
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["dead_leases"] == [
            {
                "target_env": "prod-osaka",
                "run_id": "run-dead",
                "heartbeat_at": dead_heartbeat,
            }
        ]
        with database.connect() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) AS n FROM sync_export_runs"
            ).fetchone()
            assert remaining["n"] == 1  # dry-run must not evict it

        exit_code = cli.main(["prune-log", "--dry-run"])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "死租约" in out
        assert "prod-osaka" in out
        assert "run-dead" in out
        assert "会被删除" in out
        assert "已删除死租约" not in out
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
        assert "已删除 1 行（1 批）。" in out
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


def test_prune_log_deletes_in_batches_when_more_than_the_batch_size_qualifies(
    tmp_path, monkeypatch, capsys
):
    """P2-3: the real (non-dry-run) delete is chunked at
    ``cli._PRUNE_LOG_BATCH_SIZE`` rows per transaction -- seed more than one
    batch's worth of eligible rows and assert every one of them is gone and
    more than one batch ran."""
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()
    database = SqliteDatabase(settings, tmp_path)
    try:
        total = cli._PRUNE_LOG_BATCH_SIZE + 500
        _seed_captured_watermark(database, exported_through_seq=total + 100)
        old = _old_moment(40)
        with database.write() as conn:
            conn.executemany(
                "INSERT INTO sync_change_log "
                "(seq, table_name, key_json, operation, changed_at) "
                "VALUES (?, 'notebooks', '{}', 'upsert', ?)",
                [(seq, old) for seq in range(1, total + 1)],
            )

        exit_code = cli.main(["prune-log", "--json"])
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["deleted"] == total
        assert payload["batches"] > 1

        with database.connect() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) AS n FROM sync_change_log"
            ).fetchone()
            assert remaining["n"] == 0
    finally:
        database.close()


def _drop_export_state_v85_columns(database) -> None:
    """Simulate a v83/0063 or v84/0064 database (``sync_export_state``
    exists, but not the v85/0065 ``captured``/``exported_snapshot`` columns)
    on a fully-migrated SQLite file -- SQLite 3.35+ supports
    ``ALTER TABLE ... DROP COLUMN``, so this drops just those two columns
    rather than requiring a separate never-migrated-that-far fixture."""
    with database.write() as conn:
        conn.execute("ALTER TABLE sync_export_state DROP COLUMN captured")
        conn.execute("ALTER TABLE sync_export_state DROP COLUMN exported_snapshot")


def test_status_degrades_watermark_section_when_v85_columns_are_missing(
    tmp_path, monkeypatch, capsys
):
    """P1: a v83/v84-shaped database (sync_export_state exists, captured/
    exported_snapshot columns do not) must not raise a raw "no such column"
    error out of `sync status` -- the watermark section degrades to the four
    v83 columns and names the gap; imports still read normally."""
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
                    json.dumps({"notebooks": ["nb-1"]}),
                ),
            )
        _drop_export_state_v85_columns(database)

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
        assert "captured" not in payload["exports"][0]
        assert "v85/0065" in payload["exports_note"]
        assert "captured" in payload["exports_note"]
        assert len(payload["imports"]) == 1
        assert payload["imports"][0]["package_id"] == "pkg-xyz"

        exit_code = cli.main(["status"])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "prod-tokyo" in out
        assert "v85/0065" in out
        assert "已引入的包" in out
        assert "pkg-xyz" in out
    finally:
        database.close()


def test_prune_log_reports_missing_v85_columns_and_exits_2(
    tmp_path, monkeypatch, capsys
):
    """P1: prune-log reads captured/exported_snapshot straight away (to find
    its deletion bound) and must refuse, named, rather than surface a raw
    driver error, when the database predates v85/0065."""
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()
    database = SqliteDatabase(settings, tmp_path)
    try:
        _drop_export_state_v85_columns(database)

        exit_code = cli.main(["prune-log"])
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "v85/0065" in err
        assert "captured" in err
        assert "exported_snapshot" in err
    finally:
        database.close()


def test_prune_log_reports_missing_export_runs_table_only(
    tmp_path, monkeypatch, capsys
):
    """The lease table is checked independently of the two columns -- a
    database with the columns but not ``sync_export_runs`` (should not
    happen in practice, since both land in the same migration, but the two
    facts are still verified and reported separately) names only the table,
    not a phantom column complaint."""
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()
    database = SqliteDatabase(settings, tmp_path)
    try:
        with database.write() as conn:
            conn.execute("DROP TABLE sync_export_runs")

        exit_code = cli.main(["prune-log"])
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "v85/0065" in err
        assert "缺表" in err
        assert "sync_export_runs" in err
        assert "缺列" not in err
    finally:
        database.close()


def test_prune_log_reports_missing_v85_columns_and_table_together(
    tmp_path, monkeypatch, capsys
):
    settings = _settings(tmp_path, monkeypatch)
    repository = SQLiteRepository(settings)
    repository.close()
    database = SqliteDatabase(settings, tmp_path)
    try:
        _drop_export_state_v85_columns(database)
        with database.write() as conn:
            conn.execute("DROP TABLE sync_export_runs")

        exit_code = cli.main(["prune-log"])
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "v85/0065" in err
        assert "缺列" in err
        assert "captured" in err
        assert "缺表" in err
        assert "sync_export_runs" in err
    finally:
        database.close()


def test_status_human_output_degrades_on_unparseable_snapshot(
    tmp_path, monkeypatch, capsys
):
    """P3-4: a malformed exported_snapshot (should never happen in practice
    -- _prune_log_bounds guards the write side -- but a hand-edited or
    corrupted row is exactly the case a diagnostic tool must survive) must
    not abort the rest of `status`; it degrades to a one-line hint on that
    row only."""
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
                    "not-a-snapshot",
                ),
            )

        exit_code = cli.main(["status"])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "prod-tokyo" in out
        assert "snapshot 解析失败" in out
        assert "已引入的包" in out
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
