"""Bring the configured database to this build's schema: migrate, never seed.

Usage (stop the API and every background writer first):

    bash scripts/cli.sh database migrate --confirm-service-stopped

or, where the unified entry is not shipped (the offline package), from the
install root: ``PYTHONPATH=backend .venv/bin/python -m app.scripts.migrate_database
--confirm-service-stopped``. A module under ``backend/`` so the offline package,
which ships ``backend/`` but not the repository's ``scripts/``, carries it.

This and the serving process's startup are the only two places the schema is
migrated; every other maintenance command is a schema guest and refuses a
database that is not current. It never seeds, so the admin password is never
rewritten. It never creates a database either -- a never-initialized database
is the service's to create, since only it writes the built-in accounts.

``--env-file PATH`` selects the database from that file alone (shell variables
are ignored), exactly like ``prepare_selected_source_graph.py``.

Exit status: 0 migrated or already current, 2 usage error (missing
confirmation, unreadable ``--env-file``), another maintenance command holds the
lock, or the database cannot be reached (fixed wording, no connection details), 3 the database
cannot be migrated by this build (never initialized, newer than this build, or
migrated by different SQL). The database URL is never printed.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from pydantic import ValidationError

from app.core.config import Settings
from app.repositories.schema_currency import version_label
from app.services.maintenance_cli import (
    DeploymentFileSettings,
    MaintenanceCliError,
    run_schema_migration,
)


def _settings(env_file: Path | None) -> Settings:
    """Without ``--env-file``: the ordinary deployment settings (exported
    variables first, then ``SILICON_NOTEBOOK_ENV_FILE`` or the root ``.env``).
    With it: that file alone, so shell variables cannot point the migration at
    another database -- the same contract as the selected-source preparation."""
    if env_file is None:
        return Settings()
    return DeploymentFileSettings(_env_file=env_file)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="migrate_database",
        description=(
            "把数据库迁移到本构建的 schema 版本：只迁移、不 seed（不改写 admin 密码）、"
            "不新建数据库；须先停止 API 与全部后台 writer"
        ),
    )
    parser.add_argument(
        "--confirm-service-stopped",
        action="store_true",
        help="声明 API 与全部后台 writer 均已停止（本命令不会替你停服）",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="只按这个部署 env 文件选库（忽略 shell 里导出的变量）；不给时按常规部署配置",
    )
    args = parser.parse_args(argv)
    env_file = args.env_file.resolve() if args.env_file is not None else None
    if env_file is not None and not env_file.is_file():
        print("error: --env-file 指向的文件不存在", file=sys.stderr)
        return 2
    try:
        settings = _settings(env_file)
    except (OSError, UnicodeError, ValidationError):
        print("error: 部署配置无法解析（settings_invalid）", file=sys.stderr)
        return 2
    try:
        result = run_schema_migration(
            settings, confirm_service_stopped=args.confirm_service_stopped
        )
    except MaintenanceCliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    before, after = result.before, result.after
    if before.current == after.current:
        print(
            f"database migrate: 已是 {version_label(after.backend, after.current)}，无需迁移",
            flush=True,
        )
    else:
        print(
            "database migrate: 已从 "
            f"{version_label(before.backend, before.current)} 迁移到 "
            f"{version_label(after.backend, after.current)}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
