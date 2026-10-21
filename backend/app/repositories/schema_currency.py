"""Who may migrate the schema, and how a process that may not finds out.

Only two kinds of process own the schema: the serving process (it migrates and
seeds during startup composition) and the explicit ``database migrate`` command
(:func:`migrate_schema`; it migrates under the offline-maintenance lock and
never seeds). Every other process -- each maintenance, backfill, eval or
diagnostic CLI -- is a *schema guest*: it composes with ``migrate=False,
seed=False`` and first asks :func:`inspect_schema_currency` whether the
database is at exactly this build's schema. A guest that migrated would run DDL
a serving process never asked for; a guest that seeded would re-hash
``settings.admin_password`` with a fresh salt and silently reset the production
admin credential whenever the operator's environment lacks it.

The inspection reads on a bare connection BEFORE anything is composed: a
refusal opens no pool, creates no SQLite file and leaves no trace in the
database. :func:`schema_refusal_message` is the single wording every refusal
path prints -- it always names the reason (the two versions, or what is wrong
with the ledger) and the way out.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings
from app.core.database_url import database_identity


#: Exit status every schema-guest command returns when it refuses a database
#: whose schema is not this build's (the attribution backfill's historical 3).
EXIT_SCHEMA_NOT_CURRENT = 3

MIGRATE_COMMAND = "bash scripts/cli.sh database migrate --confirm-service-stopped"

_ROOT_DIR = Path(__file__).resolve().parents[3]


class SchemaNotMigratableError(RuntimeError):
    """``migrate_schema`` refused: the database cannot be brought to this
    build's schema by the migration command (never initialized, newer than this
    build, or migrated by different SQL). The message is the refusal wording."""


@dataclass(frozen=True)
class SchemaCurrency:
    """What a bare connection saw, compared with what this build carries.

    ``initialized`` is False for a database no serving process ever set up: a
    missing SQLite file or ``user_version`` 0, a PostgreSQL database with no
    (or an empty) migration ledger. ``current`` is the SQLite ``user_version``
    or the highest PostgreSQL ledger version (0 when there is none).
    ``recorded_versions``/``checksum_mismatch_at`` only describe PostgreSQL.
    """

    backend: str
    initialized: bool
    current: int
    required: int
    sqlite_file_missing: bool = False
    recorded_versions: tuple[int, ...] = ()
    checksum_mismatch_at: int | None = None

    @property
    def ledger_consistent(self) -> bool:
        if self.backend != "postgresql":
            return True
        return (
            self.recorded_versions == tuple(range(1, self.current + 1))
            and self.checksum_mismatch_at is None
        )

    @property
    def is_current(self) -> bool:
        return (
            self.initialized
            and self.ledger_consistent
            and self.current == self.required
        )

    @property
    def is_behind(self) -> bool:
        """Initialized, consistent and older: the one state migration fixes."""
        return (
            self.initialized
            and self.ledger_consistent
            and self.current < self.required
        )


def version_label(backend: str, version: int) -> str:
    """How the docs name a schema version: SQLite ``v98``, PostgreSQL ``0078``."""
    if backend == "postgresql":
        return f"PostgreSQL {version:04d}"
    return f"SQLite v{version}"


def schema_refusal_message(state: SchemaCurrency) -> str:
    """The one refusal wording: the reason, then the way out."""
    required = version_label(state.backend, state.required)
    if not state.initialized:
        if state.sqlite_file_missing:
            reason = "SQLite 库文件不存在"
        elif state.backend == "postgresql":
            reason = "PostgreSQL 库里没有迁移账本"
        else:
            reason = "SQLite 库的 schema 版本为 0"
        return (
            f"数据库还没有初始化（{reason}），本构建需要 {required}；本命令不创建、"
            "不迁移数据库。请先启动一次服务，由服务建库并写入内置账户，再重跑本命令。"
        )
    if not state.ledger_consistent:
        if state.checksum_mismatch_at is not None:
            reason = (
                f"版本 {state.checksum_mismatch_at:04d} 的 checksum 与本构建的迁移 SQL 不同"
            )
        else:
            missing = sorted(set(range(1, state.current + 1)) - set(state.recorded_versions))
            shown = ", ".join(f"{v:04d}" for v in missing[:5])
            reason = (
                f"记录的版本不是连续的 0001 起序列，缺少版本 {shown}{' 等' if len(missing) > 5 else ''}"
                if missing
                else "记录的版本不是连续的 0001 起序列，有重复版本"
            )
        return (
            f"PostgreSQL 迁移账本与本构建不一致（{reason}，本构建需要 {required}）："
            "数据库是由另一套迁移 SQL 迁移的。请改用与在役服务完全相同版本的代码运行本命令。"
        )
    current = version_label(state.backend, state.current)
    if state.current > state.required:
        return (
            f"数据库 schema 版本是 {current}，比本构建需要的 {required} 更新：它已被更新版本"
            "的代码迁移过，迁移是单向的。请改用与在役服务相同版本的代码运行本命令。"
        )
    return (
        f"数据库 schema 版本是 {current}，本构建需要 {required}；本命令不迁移数据库。"
        f"请先停服并运行 `{MIGRATE_COMMAND}`，或启动一次升级后的服务（启动时自动迁移），"
        "再重跑本命令；若这个库属于仍在运行旧版本的服务，请改用与该服务相同版本的代码运行本命令。"
    )


def _sqlite_path(settings: Settings) -> Path:
    path = Path(settings.sqlite_path)
    return path if path.is_absolute() else _ROOT_DIR / path


def inspect_sqlite_schema(path: Path) -> SchemaCurrency:
    """Read ``user_version`` read-only; a missing file is reported, never created."""
    import sqlite3

    from app.repositories.sqlite.migrations import SCHEMA_VERSION

    if not path.exists():
        return SchemaCurrency(
            "sqlite", False, 0, SCHEMA_VERSION, sqlite_file_missing=True
        )
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()
    return SchemaCurrency("sqlite", current > 0, current, SCHEMA_VERSION)


def packaged_postgres_migrations() -> tuple:
    """The PostgreSQL migrations this checkout carries, in version order."""
    from app.repositories.postgres import migrator as migrator_module

    return migrator_module.load_migrations(
        Path(migrator_module.__file__).with_name("migrations")
    )


def inspect_postgres_schema(database_url: str) -> SchemaCurrency:
    """Compare the live ledger with this checkout's migrations on one bare
    connection (no pool): the exact version SET ``1..N`` -- a gapped ledger has
    ``max(version) == N`` yet is one the repository's own migrator refuses --
    and every recorded checksum, which is what catches two checkouts carrying
    the same number of migrations with different SQL."""
    import psycopg

    migrations = packaged_postgres_migrations()
    required = len(migrations)
    with psycopg.connect(database_url, autocommit=True) as connection:
        present = connection.execute(
            "SELECT to_regclass('silicon_schema_migrations') IS NOT NULL"
        ).fetchone()[0]
        rows = (
            connection.execute(
                "SELECT version, checksum FROM silicon_schema_migrations "
                "ORDER BY version"
            ).fetchall()
            if present
            else []
        )
    recorded = tuple(int(row[0]) for row in rows)
    mismatch = next(
        (
            int(version)
            for version, checksum in rows
            if 1 <= int(version) <= required
            and checksum != migrations[int(version) - 1].checksum
        ),
        None,
    )
    return SchemaCurrency(
        "postgresql",
        bool(recorded),
        recorded[-1] if recorded else 0,
        required,
        recorded_versions=recorded,
        checksum_mismatch_at=mismatch,
    )


def inspect_schema_currency(settings: Settings) -> SchemaCurrency:
    if database_identity(settings.database_url).scheme == "postgresql":
        return inspect_postgres_schema(settings.database_url)
    return inspect_sqlite_schema(_sqlite_path(settings))


@dataclass(frozen=True)
class SchemaMigration:
    before: SchemaCurrency
    after: SchemaCurrency


def migrate_schema(settings: Settings) -> SchemaMigration:
    """The explicit migration command's body: migrate, never seed.

    Refuses (``SchemaNotMigratableError``) a database that is not initialized,
    is newer than this build, or carries a ledger of different SQL -- migration
    cannot fix any of those, and creating a fresh database is the serving
    process's job because only it writes the built-in accounts. An already
    current database is a no-op. On PostgreSQL the work runs under the same
    offline-maintenance advisory lock every maintenance command takes (a busy
    lock raises ``OfflineMaintenanceBusyError``); the migrator's own
    transaction-level migration lock still serializes it against a starting
    server, exactly as two starting servers are serialized.
    """
    before = inspect_schema_currency(settings)
    if before.is_current:
        return SchemaMigration(before, before)
    if not before.is_behind:
        raise SchemaNotMigratableError(schema_refusal_message(before))
    if before.backend == "postgresql":
        from app.repositories.postgres.database import PostgresDatabase
        from app.repositories.postgres.maintenance import hold_offline_maintenance_lock
        from app.repositories.postgres.migrator import PostgresMigrator

        database = PostgresDatabase(settings, _ROOT_DIR)
        try:
            with hold_offline_maintenance_lock(database):
                PostgresMigrator(database).migrate()
        finally:
            database.close()
    else:
        from app.repositories.sqlite.database import SqliteDatabase
        from app.repositories.sqlite.migrations import SqliteMigrator

        database = SqliteDatabase(settings, _ROOT_DIR)
        try:
            SqliteMigrator(database, settings).migrate()
        finally:
            database.close()
    return SchemaMigration(before, inspect_schema_currency(settings))
