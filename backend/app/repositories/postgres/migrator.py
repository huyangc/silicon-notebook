"""Checksummed, advisory-locked PostgreSQL schema migration runner."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.schema_manifest import POSTGRES_SCHEMA_MANIFEST


MIGRATION_ADVISORY_LOCK_NAME = "silicon-notebook:postgres-migrations"
_MIGRATION_FILENAME = re.compile(r"^(?P<version>[0-9]{4})_(?P<name>[a-z0-9_]+)\.sql$")
_LEDGER_SQL = """
CREATE TABLE IF NOT EXISTS silicon_schema_migrations (
    version integer PRIMARY KEY,
    checksum text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def migration_advisory_lock_key() -> int:
    digest = hashlib.sha256(MIGRATION_ADVISORY_LOCK_NAME.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


@dataclass(frozen=True)
class PostgresMigration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def load_migrations(directory: Path) -> tuple[PostgresMigration, ...]:
    if not directory.exists():
        return ()
    migrations: list[PostgresMigration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_FILENAME.fullmatch(path.name)
        if match is None:
            raise ValueError(f"invalid PostgreSQL migration filename: {path.name}")
        migrations.append(
            PostgresMigration(
                version=int(match.group("version")),
                name=match.group("name"),
                sql=path.read_text(encoding="utf-8"),
            )
        )
    return tuple(migrations)


def _validate_manifest(migrations: Sequence[PostgresMigration]) -> None:
    versions = [migration.version for migration in migrations]
    expected = list(range(1, len(migrations) + 1))
    if versions != expected:
        raise ValueError(
            "PostgreSQL migration versions must be unique, positive, and gap-free"
        )
    if any(
        not migration.name.strip() or not migration.sql.strip()
        for migration in migrations
    ):
        raise ValueError("PostgreSQL migration name and SQL must be non-empty")


class PostgresMigrator:
    def __init__(
        self,
        database: PostgresDatabase,
        *,
        migrations: Sequence[PostgresMigration] | None = None,
    ) -> None:
        self.database = database
        uses_packaged_migrations = migrations is None
        self.migrations = tuple(
            load_migrations(Path(__file__).with_name("migrations"))
            if migrations is None
            else migrations
        )
        if (
            uses_packaged_migrations
            and len(self.migrations) != POSTGRES_SCHEMA_MANIFEST.postgres_version
        ):
            raise ValueError(
                "packaged PostgreSQL migration count does not match schema manifest"
            )

    @staticmethod
    def _lock(conn) -> None:
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (migration_advisory_lock_key(),),
        )

    @staticmethod
    def _ledger_rows(conn) -> list[dict[str, object]]:
        return conn.execute(
            "SELECT version, checksum FROM silicon_schema_migrations ORDER BY version"
        ).fetchall()

    def _validate_ledger(self, rows: Sequence[dict[str, object]]) -> int:
        versions = [int(row["version"]) for row in rows]
        if versions and versions != list(range(1, versions[-1] + 1)):
            raise RuntimeError("PostgreSQL migration ledger has a version gap")
        if versions and versions[-1] > len(self.migrations):
            raise RuntimeError("PostgreSQL migration ledger contains a future version")
        for row in rows:
            version = int(row["version"])
            expected = self.migrations[version - 1].checksum
            if row["checksum"] != expected:
                raise RuntimeError(
                    f"PostgreSQL migration checksum mismatch at version {version}"
                )
        return versions[-1] if versions else 0

    def current_version(self) -> int:
        _validate_manifest(self.migrations)
        with self.database.connect() as conn:
            relation = conn.execute(
                "SELECT to_regclass('silicon_schema_migrations') AS relation"
            ).fetchone()["relation"]
            if relation is None:
                return 0
            return self._validate_ledger(self._ledger_rows(conn))

    def migrate(self) -> int:
        _validate_manifest(self.migrations)
        if not self.migrations:
            with self.database.write() as conn:
                self._lock(conn)
                conn.execute(_LEDGER_SQL)
                return self._validate_ledger(self._ledger_rows(conn))

        current = 0
        for migration in self.migrations:
            with self.database.write() as conn:
                self._lock(conn)
                conn.execute(_LEDGER_SQL)
                current = self._validate_ledger(self._ledger_rows(conn))
                if current >= migration.version:
                    continue
                if current != migration.version - 1:
                    raise RuntimeError("PostgreSQL migration ledger has a version gap")
                conn.execute(migration.sql, prepare=False)
                conn.execute(
                    "INSERT INTO silicon_schema_migrations(version, checksum) "
                    "VALUES (%s, %s)",
                    (migration.version, migration.checksum),
                )
                current = migration.version
        return current
