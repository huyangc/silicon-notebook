from __future__ import annotations

import sqlite3
import base64
import hashlib
import hmac
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.migration.shadow.control import (
    BackupEvidence,
    DiskEvidence,
    preflight,
)
from app.migration.shadow.identity import (
    ConfirmationBinding,
    DatabaseFingerprint,
    fingerprint_postgres,
    fingerprint_sqlite,
    issue_confirmation_token,
    reject_same_database,
    verify_confirmation_token,
)
from app.migration.shadow.manifest import MANIFEST
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SqliteMigrator


REPO_ROOT = Path(__file__).resolve().parents[3]
SECRET = b"shadow-confirmation-test-secret-32-bytes-long"


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _ReadOnlyTargetConnection:
    def __init__(
        self,
        *,
        encoding: str = "UTF8",
        objects=(),
        schema: str = "shadow_target",
        pg_trgm_installed: bool = False,
        public_usage: bool = True,
        public_create: bool = True,
    ):
        self.encoding = encoding
        self.objects = list(objects)
        self.schema = schema
        self.pg_trgm_installed = pg_trgm_installed
        self.public_usage = public_usage
        self.public_create = public_create
        self.statements: list[str] = []

    def execute(self, statement: str, parameters=()):
        del parameters
        normalized = " ".join(statement.split())
        self.statements.append(normalized)
        assert not normalized.upper().startswith(
            ("CREATE ", "ALTER ", "DROP ", "INSERT ", "UPDATE ", "DELETE ")
        )
        if "server_encoding" in normalized:
            return _Rows([{"server_encoding": self.encoding}])
        if "current_database() AS database" in normalized:
            return _Rows(
                [
                    {
                        "database": "silicon_notebook_test",
                        "schema": self.schema,
                        "database_oid": "42",
                    }
                ]
            )
        if "nspname='silicon_shadow'" in normalized:
            return _Rows([])
        if "pg_available_extensions" in normalized:
            return _Rows(
                [
                    {
                        "installed_version": (
                            "1.6" if self.pg_trgm_installed else None
                        ),
                        "default_version": "1.6",
                        "extension_schema": (
                            "public" if self.pg_trgm_installed else None
                        ),
                        "public_exists": True,
                        "public_usage": self.public_usage,
                        "public_create": self.public_create,
                    }
                ]
            )
        if "has_schema_privilege" in normalized:
            return _Rows(
                [
                    {
                        "owner": "tester",
                        "current_user": "tester",
                        "owner_authorized": True,
                        "can_use": True,
                        "can_create": True,
                    }
                ]
            )
        if "has_database_privilege" in normalized:
            return _Rows([{"can_connect": True, "can_create": True, "can_temp": True}])
        if "SELECT object_kind,object_name" in normalized:
            return _Rows(self.objects)
        if "max_connections" in normalized:
            return _Rows(
                [
                    {
                        "max_connections": 100,
                        "reserved_connections": 3,
                        "current_connections": 4,
                    }
                ]
            )
        if "pg_database_size" in normalized:
            return _Rows([{"database_bytes": 8192}])
        raise AssertionError(f"unexpected preflight SQL: {normalized}")


class _ReadOnlyTarget:
    def __init__(self, conn: _ReadOnlyTargetConnection):
        self.conn = conn
        self.settings = SimpleNamespace(
            database_url=(
                "postgresql://migration-user:super-secret@db.example:5432/"
                "silicon_notebook_test"
            ),
            postgres_pool_max_size=4,
        )

    @contextmanager
    def connect(self):
        yield self.conn


@pytest.fixture
def migrated_source(tmp_path: Path):
    database_path = tmp_path / "private-source.db"
    settings = Settings(database_url=f"sqlite:///{database_path}", _env_file=None)
    database = SqliteDatabase(settings, REPO_ROOT)
    SqliteMigrator(database, settings).migrate()
    storage = tmp_path / "storage"
    storage.mkdir()
    try:
        with database.connect() as conn:
            yield settings.database_url, conn, storage, database_path
    finally:
        database.close_local()


def test_sqlite_fingerprint_binds_connection_and_redacts_parent_path(tmp_path: Path):
    path = tmp_path / "private-tenant" / "source.db"
    path.parent.mkdir()
    with sqlite3.connect(path) as conn:
        fingerprint = fingerprint_sqlite(f"sqlite:///{path}", conn)
        assert fingerprint.backend == "sqlite"
        assert fingerprint.redacted_identity == "sqlite:///<redacted>"
        assert str(path.parent) not in fingerprint.redacted_identity
        with pytest.raises(ValueError, match="different files"):
            other = tmp_path / "other.db"
            sqlite3.connect(other).close()
            fingerprint_sqlite(f"sqlite:///{other}", conn)


def test_identity_failures_do_not_disclose_paths_or_credentials(tmp_path: Path):
    missing = tmp_path / "private-tenant-secret" / "missing.db"
    with sqlite3.connect(":memory:") as conn:
        with pytest.raises(ValueError) as sqlite_error:
            fingerprint_sqlite(f"sqlite:///{missing}", conn)
    assert str(missing) not in str(sqlite_error.value)
    postgres_url = "postgresql://operator:credential-secret@db.example/private"
    with pytest.raises(ValueError) as postgres_error:
        fingerprint_postgres(
            postgres_url,
            object(),
            identity_row={
                "database": "other",
                "schema": "public",
                "database_oid": "42",
            },
        )
    assert "credential-secret" not in str(postgres_error.value)
    assert postgres_url not in str(postgres_error.value)


def test_identity_bound_confirmation_token_rejects_tamper_and_expiry():
    binding = ConfirmationBinding("run-1", "a" * 64, "b" * 64, 32, 10, 1)
    token = issue_confirmation_token(binding, secret=SECRET, now=1_000, ttl_seconds=60)
    verify_confirmation_token(token, binding, secret=SECRET, now=1_030)
    with pytest.raises(ValueError, match="bound"):
        verify_confirmation_token(
            token,
            ConfirmationBinding("run-2", "a" * 64, "b" * 64, 32, 10, 1),
            secret=SECRET,
            now=1_030,
        )
    with pytest.raises(ValueError, match="expired"):
        verify_confirmation_token(token, binding, secret=SECRET, now=1_061)
    payload_part, signature_part = token.split(".")
    replacement = "A" if signature_part[0] != "A" else "B"
    tampered = f"{payload_part}.{replacement}{signature_part[1:]}"
    with pytest.raises(ValueError, match="invalid"):
        verify_confirmation_token(tampered, binding, secret=SECRET, now=1_030)
    with pytest.raises(ValueError, match="invalid"):
        verify_confirmation_token(
            payload_part + "=" + "." + signature_part, binding, secret=SECRET, now=1_030
        )


@pytest.mark.parametrize(
    "bad_time", [None, True, "1000", 1.5, float("nan"), float("inf"), 2**80]
)
def test_confirmation_token_malformed_times_fail_generically(bad_time):
    binding = ConfirmationBinding("run-1", "a" * 64, "b" * 64, 32, 10, 1)
    payload = binding.payload(issued_at=1_000, expires_at=1_060, nonce="c" * 32)
    payload["iat"] = bad_time
    raw = json.dumps(payload, separators=(",", ":"), allow_nan=True).encode()
    signature = hmac.new(SECRET, raw, hashlib.sha256).digest()
    token = ".".join(
        base64.urlsafe_b64encode(part).rstrip(b"=").decode()
        for part in (raw, signature)
    )
    with pytest.raises(ValueError, match="invalid confirmation token"):
        verify_confirmation_token(token, binding, secret=SECRET, now=1_030)


def test_same_database_identity_is_rejected():
    identity = DatabaseFingerprint("sqlite", "sqlite:///<redacted>/db", "f" * 64)
    with pytest.raises(ValueError, match="different databases"):
        reject_same_database(identity, identity)


def test_preflight_is_read_only_redacted_and_encoding_is_first_target_check(
    migrated_source,
):
    source_url, source_conn, storage, database_path = migrated_source
    target_conn = _ReadOnlyTargetConnection()
    target = _ReadOnlyTarget(target_conn)
    report = preflight(
        run_id="forward-1",
        source_url=source_url,
        source_conn=source_conn,
        storage_root=storage,
        target=target,
        disk_evidence=DiskEvidence("disk-check-1", 1_000_000_000, True),
        backup_evidence=BackupEvidence("backup-check-1", True, True),
        confirmation_secret=SECRET,
        manifest=MANIFEST,
        now=1_000,
    )
    assert "server_encoding" in target_conn.statements[0]
    assert all(
        statement.startswith(("SELECT ", "WITH "))
        for statement in target_conn.statements
    )
    assert report.target_initial_schema_version == 0
    assert report.schema_pair.sqlite_version == 32
    assert report.schema_pair.postgres_version == 10
    assert report.source_database_bytes >= database_path.stat().st_size
    assert "super-secret" not in repr(report)
    assert str(database_path.parent) not in repr(report)
    verify_confirmation_token(
        report.confirmation_token,
        report.binding,
        secret=SECRET,
        now=1_001,
    )


def test_preflight_non_utf_target_fails_on_first_query_without_later_reads(
    migrated_source,
):
    source_url, source_conn, storage, _ = migrated_source
    target_conn = _ReadOnlyTargetConnection(encoding="LATIN1")
    with pytest.raises(ValueError, match="server_encoding"):
        preflight(
            run_id="forward-1",
            source_url=source_url,
            source_conn=source_conn,
            storage_root=storage,
            target=_ReadOnlyTarget(target_conn),
            disk_evidence=DiskEvidence("disk", 1_000_000_000, True),
            backup_evidence=BackupEvidence("backup", True, True),
            confirmation_secret=SECRET,
        )
    assert len(target_conn.statements) == 1


def test_preflight_public_pg_trgm_exception_and_privilege_split(migrated_source):
    source_url, source_conn, storage, _ = migrated_source
    installed = _ReadOnlyTargetConnection(
        schema="public",
        pg_trgm_installed=True,
        public_usage=True,
        public_create=False,
    )
    report = preflight(
        run_id="forward-public-installed",
        source_url=source_url,
        source_conn=source_conn,
        storage_root=storage,
        target=_ReadOnlyTarget(installed),
        disk_evidence=DiskEvidence("disk", 1_000_000_000, True),
        backup_evidence=BackupEvidence("backup", True, True),
        confirmation_secret=SECRET,
    )
    assert report.pg_trgm_installed is True
    catalog_sql = next(
        statement for statement in installed.statements if "WITH objects(" in statement
    )
    assert "d.deptype='e'" in catalog_sql
    assert "e.extname='pg_trgm'" in catalog_sql
    assert "current_schema()='public'" in catalog_sql

    unavailable_create = _ReadOnlyTargetConnection(
        schema="public",
        pg_trgm_installed=False,
        public_usage=True,
        public_create=False,
    )
    with pytest.raises(ValueError, match="privileges"):
        preflight(
            run_id="forward-public-uninstalled",
            source_url=source_url,
            source_conn=source_conn,
            storage_root=storage,
            target=_ReadOnlyTarget(unavailable_create),
            disk_evidence=DiskEvidence("disk", 1_000_000_000, True),
            backup_evidence=BackupEvidence("backup", True, True),
            confirmation_secret=SECRET,
        )

    other_extension = _ReadOnlyTargetConnection(
        schema="public",
        pg_trgm_installed=True,
        public_usage=True,
        public_create=False,
        objects=[{"object_kind": "extension", "object_name": "hstore"}],
    )
    with pytest.raises(ValueError, match="must be empty"):
        preflight(
            run_id="forward-public-other-extension",
            source_url=source_url,
            source_conn=source_conn,
            storage_root=storage,
            target=_ReadOnlyTarget(other_extension),
            disk_evidence=DiskEvidence("disk", 1_000_000_000, True),
            backup_evidence=BackupEvidence("backup", True, True),
            confirmation_secret=SECRET,
        )


def test_preflight_rejects_nonempty_target_and_missing_evidence(migrated_source):
    source_url, source_conn, storage, _ = migrated_source
    with pytest.raises(ValueError, match="disk evidence"):
        preflight(
            run_id="forward-1",
            source_url=source_url,
            source_conn=source_conn,
            storage_root=storage,
            target=_ReadOnlyTarget(_ReadOnlyTargetConnection()),
            disk_evidence=DiskEvidence("disk", 1, True),
            backup_evidence=BackupEvidence("backup", True, True),
            confirmation_secret=SECRET,
        )
    with pytest.raises(ValueError, match="must be empty"):
        preflight(
            run_id="forward-1",
            source_url=source_url,
            source_conn=source_conn,
            storage_root=storage,
            target=_ReadOnlyTarget(
                _ReadOnlyTargetConnection(
                    objects=[{"relname": "unexpected", "relkind": "r"}]
                )
            ),
            disk_evidence=DiskEvidence("disk", 1_000_000_000, True),
            backup_evidence=BackupEvidence("backup", True, True),
            confirmation_secret=SECRET,
        )
