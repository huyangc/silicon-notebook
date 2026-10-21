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

Exit status: 0 migrated or already current, 2 usage error (missing
confirmation) or another maintenance command holds the lock, 3 the database
cannot be migrated by this build (never initialized, newer than this build, or
migrated by different SQL). The database URL is never printed.
"""
from __future__ import annotations

import argparse
import sys
from typing import Sequence

from app.core.config import Settings
from app.repositories.schema_currency import version_label
from app.services.maintenance_cli import MaintenanceCliError, run_schema_migration


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
    args = parser.parse_args(argv)
    try:
        result = run_schema_migration(
            Settings(), confirm_service_stopped=args.confirm_service_stopped
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
