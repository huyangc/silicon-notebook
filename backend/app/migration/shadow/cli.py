"""Explicit operator CLI for SQLite-to-PostgreSQL forward shadowing."""

from __future__ import annotations

import argparse
import dataclasses
from datetime import date, datetime
import hashlib
import json
import os
import re
import secrets
import shlex
import sqlite3
import sys
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.core.config import Settings
from app.core.database_url import database_identity
from app.migration.shadow.bulk_copy import copy_snapshot
from app.migration.shadow.capture import install_sqlite_capture
from app.migration.shadow.control import (
    BackupEvidence,
    DiskEvidence,
    PreflightReport,
    ShadowPhase,
    commit_guard_report,
    committed_sqlite_guard_report,
    create_or_reuse_run,
    enable_sqlite_capture_after_guard_reports,
    get_run,
    install_postgres_control_schema,
    preflight,
    prepare_postgres_replication_key_guards,
    update_forward_state,
)
from app.migration.shadow.identity import (
    ConfirmationBinding,
    DatabaseFingerprint,
    fingerprint_postgres,
    fingerprint_sqlite,
    issue_confirmation_token,
    verify_confirmation_token,
)
from app.migration.shadow.manifest import MANIFEST
from app.migration.shadow.replicator import Direction
from app.migration.shadow.snapshot import (
    SnapshotInfo,
    _validate_snapshot_file,
    create_snapshot,
)
from app.migration.shadow.types import SchemaPair
from app.migration.shadow.verifier import ShadowVerifier, VerificationLevel
from app.migration.shadow.worker import ForwardWorker, termination_event
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.sqlite.database import SqliteDatabase


REPO_ROOT = Path(__file__).resolve().parents[4]
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_URL = re.compile(r"(?:postgres(?:ql)?|sqlite)://[^\s'\"]+", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(password|secret|token|credential|api[_-]?key)\s*[=:]\s*[^\s,;]+"
)
_REPORT_FILE = "preflight.json"
_SECRET_FILE = "confirmation.secret"
_SNAPSHOT_FILE = "baseline.sqlite3"
_SNAPSHOT_META = "baseline.json"
_WORKER_TOKEN_FILE = "worker.confirmation"


class OperatorError(RuntimeError):
    """Expected, safely reportable operator failure."""


def _validate_run_id(value: str) -> str:
    if _RUN_ID.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("run id must be 1..128 path-safe characters")
    return value


def _safe_error(exc: BaseException) -> str:
    text = _URL.sub("<redacted-database-url>", str(exc))
    text = _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    return text[:1_000] or type(exc).__name__


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {field.name: _jsonable(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _emit(payload: Mapping[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(_jsonable(payload), ensure_ascii=False, sort_keys=True))
        return
    command = payload.get("command", "shadow")
    status = payload.get("status", "ok")
    print(f"shadow {command}: {status}")
    for key, value in payload.items():
        if key in {"command", "status", "confirmation_token"}:
            continue
        if isinstance(value, (dict, list, tuple)):
            value = json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True)
        print(f"  {key}: {value}")
    if "confirmation_token" in payload:
        print("  confirmation_token: <redacted; rerun with --json to retrieve>")


def _work_dir(args: argparse.Namespace) -> Path:
    path = (
        Path(args.work_dir)
        if getattr(args, "work_dir", None)
        else REPO_ROOT / ".local" / "shadow" / args.run_id
    ).absolute()
    if path.name != args.run_id and getattr(args, "work_dir", None) is None:
        raise OperatorError("shadow work directory binding is invalid")
    if path.exists():
        metadata = path.lstat()
        if path.is_symlink() or not path.is_dir():
            raise OperatorError("shadow work directory must be a real directory")
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
            raise OperatorError("shadow work directory must be owner-only")
    else:
        path.mkdir(parents=True, mode=0o700)
        os.chmod(path, 0o700)
    return path


def _atomic_private_write(path: Path, data: bytes) -> None:
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise OperatorError("shadow artifact directory is invalid")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _read_private(path: Path) -> bytes:
    metadata = path.lstat()
    if path.is_symlink() or not path.is_file():
        raise OperatorError("shadow artifact must be a real file")
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise OperatorError("shadow artifact must be owner-only")
    return path.read_bytes()


def _load_secret(work_dir: Path, *, create: bool) -> bytes:
    path = work_dir / _SECRET_FILE
    if not path.exists():
        if not create:
            raise OperatorError("shadow confirmation secret is unavailable")
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            secret = secrets.token_bytes(32)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(secret)
                stream.flush()
                os.fsync(stream.fileno())
    secret = _read_private(path)
    if len(secret) != 32:
        raise OperatorError("shadow confirmation secret is invalid")
    return secret


def _report_payload(report: PreflightReport) -> dict[str, Any]:
    return {
        "run_id": report.run_id,
        "source": _jsonable(report.source),
        "target": _jsonable(report.target),
        "schema_pair": _jsonable(report.schema_pair),
        "target_initial_schema_version": report.target_initial_schema_version,
        "source_database_bytes": report.source_database_bytes,
        "source_storage_bytes": report.source_storage_bytes,
        "source_storage_references": report.source_storage_references,
        "target_database_bytes": report.target_database_bytes,
        "required_target_bytes": report.required_target_bytes,
        "available_target_bytes": report.available_target_bytes,
        "target_pool_max_connections": report.target_pool_max_connections,
        "target_pool_current_connections": report.target_pool_current_connections,
        "pg_trgm_installed": report.pg_trgm_installed,
        "disk_evidence_id": report.disk_evidence_id,
        "backup_evidence_id": report.backup_evidence_id,
    }


def _fingerprint(payload: Any) -> DatabaseFingerprint:
    if not isinstance(payload, Mapping):
        raise OperatorError("shadow preflight fingerprint is invalid")
    return DatabaseFingerprint(
        backend=str(payload["backend"]),
        redacted_identity=str(payload["redacted_identity"]),
        identity_hash=str(payload["identity_hash"]),
    )


def _load_report(work_dir: Path, secret: bytes, *, issue_token: bool = False) -> PreflightReport:
    try:
        payload = json.loads(_read_private(work_dir / _REPORT_FILE))
        pair = payload["schema_pair"]
        binding = ConfirmationBinding(
            run_id=str(payload["run_id"]),
            source_identity_hash=str(payload["source"]["identity_hash"]),
            target_identity_hash=str(payload["target"]["identity_hash"]),
            sqlite_version=int(pair["sqlite_version"]),
            postgres_version=int(pair["postgres_version"]),
            schema_epoch=int(pair["epoch"]),
        )
        token = issue_confirmation_token(binding, secret=secret) if issue_token else ""
        return PreflightReport(
            run_id=binding.run_id,
            source=_fingerprint(payload["source"]),
            target=_fingerprint(payload["target"]),
            schema_pair=SchemaPair(
                binding.sqlite_version, binding.postgres_version, binding.schema_epoch
            ),
            target_initial_schema_version=int(payload["target_initial_schema_version"]),
            source_database_bytes=int(payload["source_database_bytes"]),
            source_storage_bytes=int(payload["source_storage_bytes"]),
            source_storage_references=int(payload["source_storage_references"]),
            target_database_bytes=int(payload["target_database_bytes"]),
            required_target_bytes=int(payload["required_target_bytes"]),
            available_target_bytes=int(payload["available_target_bytes"]),
            target_pool_max_connections=int(payload["target_pool_max_connections"]),
            target_pool_current_connections=int(payload["target_pool_current_connections"]),
            pg_trgm_installed=bool(payload["pg_trgm_installed"]),
            disk_evidence_id=str(payload["disk_evidence_id"]),
            backup_evidence_id=str(payload["backup_evidence_id"]),
            confirmation_token=token,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise OperatorError("shadow preflight artifact is invalid") from None


def _databases(settings: Settings) -> tuple[SqliteDatabase, PostgresDatabase]:
    if database_identity(settings.database_url).scheme != "sqlite":
        raise OperatorError("DATABASE_URL must identify the active SQLite source")
    if not settings.shadow_database_url:
        raise OperatorError("SHADOW_DATABASE_URL is required")
    if database_identity(settings.shadow_database_url).scheme != "postgresql":
        raise OperatorError("SHADOW_DATABASE_URL must identify PostgreSQL")
    source = SqliteDatabase(settings, REPO_ROOT)
    target_settings = settings.model_copy(
        update={"database_url": settings.shadow_database_url, "shadow_database_url": None}
    )
    return source, PostgresDatabase(target_settings, REPO_ROOT)


def _readonly_source(source: SqliteDatabase) -> sqlite3.Connection:
    path = source.db_path.resolve(strict=True)
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _validate_live_bindings(
    source: SqliteDatabase, target: PostgresDatabase, report: PreflightReport
) -> None:
    conn = _readonly_source(source)
    try:
        live_source = fingerprint_sqlite(source.settings.database_url, conn)
    finally:
        conn.close()
    with target.connect() as pg:
        live_target = fingerprint_postgres(target.settings.database_url, pg)
    if (
        live_source.identity_hash != report.source.identity_hash
        or live_target.identity_hash != report.target.identity_hash
        or report.schema_pair != MANIFEST.schema_pair
    ):
        raise OperatorError("shadow preflight binding no longer matches live databases")


def _preflight(args: argparse.Namespace, settings: Settings) -> Mapping[str, Any]:
    work_dir = _work_dir(args)
    secret = _load_secret(work_dir, create=True)
    source, target = _databases(settings)
    try:
        artifact = work_dir / _REPORT_FILE
        if artifact.exists():
            report = _load_report(work_dir, secret, issue_token=True)
            if report.run_id != args.run_id:
                raise OperatorError("shadow preflight run binding is invalid")
            _validate_live_bindings(source, target, report)
            reissued = True
        else:
            if args.available_target_bytes is None:
                raise OperatorError("--available-target-bytes evidence is required")
            if not args.disk_evidence_id or not args.backup_evidence_id:
                raise OperatorError("disk and backup evidence ids are required")
            conn = _readonly_source(source)
            try:
                report = preflight(
                    run_id=args.run_id,
                    source_url=settings.database_url,
                    source_conn=conn,
                    storage_root=settings.storage_dir,
                    target=target,
                    disk_evidence=DiskEvidence(
                        args.disk_evidence_id,
                        args.available_target_bytes,
                        True,
                    ),
                    backup_evidence=BackupEvidence(
                        args.backup_evidence_id,
                        args.confirm_source_backup,
                        args.confirm_target_restore,
                    ),
                    confirmation_secret=secret,
                )
            finally:
                conn.close()
            _atomic_private_write(
                artifact,
                json.dumps(_report_payload(report), sort_keys=True).encode("utf-8"),
            )
            reissued = False
        return {
            "command": "preflight",
            "status": "confirmed",
            "run_id": args.run_id,
            "reissued": reissued,
            "source": report.source.redacted_identity,
            "target": report.target.redacted_identity,
            "schema_pair": _jsonable(report.schema_pair),
            "required_target_bytes": report.required_target_bytes,
            "confirmation_token": report.confirmation_token,
        }
    finally:
        source.close()
        target.close()


def _snapshot_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_info(work_dir: Path, source: SqliteDatabase, report: PreflightReport) -> SnapshotInfo:
    snapshot_path = work_dir / _SNAPSHOT_FILE
    metadata_path = work_dir / _SNAPSHOT_META
    if not snapshot_path.exists():
        snapshot = create_snapshot(source, snapshot_path, run_id=report.run_id)
        _atomic_private_write(
            metadata_path,
            json.dumps(_jsonable(snapshot), sort_keys=True).encode("utf-8"),
        )
        return snapshot
    metadata = snapshot_path.lstat()
    if snapshot_path.is_symlink() or not snapshot_path.is_file() or metadata.st_mode & 0o077:
        raise OperatorError("shadow snapshot artifact is not owner-only")
    if metadata_path.exists():
        try:
            raw = json.loads(_read_private(metadata_path))
            return SnapshotInfo(
                path=snapshot_path,
                run_id=str(raw["run_id"]),
                source_identity_hash=str(raw["source_identity_hash"]),
                schema_epoch=int(raw["schema_epoch"]),
                baseline_seq=int(raw["baseline_seq"]),
                size_bytes=int(raw["size_bytes"]),
                sha256=str(raw["sha256"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise OperatorError("shadow snapshot metadata is invalid") from None
    baseline = _validate_snapshot_file(snapshot_path, run_id=report.run_id, manifest=MANIFEST)
    recovered = SnapshotInfo(
        snapshot_path,
        report.run_id,
        report.source.identity_hash,
        report.schema_pair.epoch,
        baseline,
        metadata.st_size,
        _snapshot_digest(snapshot_path),
    )
    _atomic_private_write(
        metadata_path,
        json.dumps(_jsonable(recovered), sort_keys=True).encode("utf-8"),
    )
    return recovered


def _baseline_is_published(target: PostgresDatabase, snapshot: SnapshotInfo) -> bool:
    """Recognize a completed H0 even after the worker advanced beyond it."""

    state = get_run(target, snapshot.run_id)
    if (
        state.phase is not ShadowPhase.SQLITE_TO_POSTGRES
        or state.progress.get("snapshot_sha256") != snapshot.sha256
        or state.progress.get("baseline_seq") != snapshot.baseline_seq
        or not isinstance(state.progress.get("applied_seq"), int)
        or state.progress["applied_seq"] < snapshot.baseline_seq
    ):
        return False
    with target.connect() as conn:
        checkpoint = conn.execute(
            "SELECT last_seq FROM silicon_shadow.checkpoints "
            "WHERE run_id=%s AND direction='sqlite_to_postgres'",
            (snapshot.run_id,),
        ).fetchone()
        progress = conn.execute(
            "SELECT COUNT(*) AS count,COUNT(*) FILTER (WHERE complete) AS complete "
            "FROM silicon_shadow.copy_progress WHERE run_id=%s",
            (snapshot.run_id,),
        ).fetchone()
    return bool(
        checkpoint is not None
        and int(checkpoint["last_seq"]) >= snapshot.baseline_seq
        and int(progress["count"]) == len(MANIFEST.replicated)
        and int(progress["complete"]) == len(MANIFEST.replicated)
    )


def _verify_confirmation(args: argparse.Namespace, work_dir: Path) -> tuple[bytes, PreflightReport]:
    secret = _load_secret(work_dir, create=False)
    report = _load_report(work_dir, secret)
    if report.run_id != args.run_id:
        raise OperatorError("shadow confirmation run binding is invalid")
    token = getattr(args, "confirmation_token", None)
    token_file = getattr(args, "confirmation_token_file", None)
    if token_file:
        token = _read_private(Path(token_file).absolute()).decode("ascii").strip()
    if not token:
        raise OperatorError("an explicit confirmation token is required")
    verify_confirmation_token(token, report.binding, secret=secret)
    return secret, report


def _start_forward(args: argparse.Namespace, settings: Settings) -> Mapping[str, Any]:
    work_dir = _work_dir(args)
    secret, report = _verify_confirmation(args, work_dir)
    source, target = _databases(settings)
    try:
        _validate_live_bindings(source, target, report)
        PostgresMigrator(target).migrate(
            target_version=MANIFEST.schema_pair.postgres_version,
            statement_timeout_seconds=3_600,
        )
        install_postgres_control_schema(target)
        state = create_or_reuse_run(
            target,
            report,
            confirmation_token=args.confirmation_token,
            confirmation_secret=secret,
        )
        source_conn = source.connect()
        if state.phase is ShadowPhase.OFF:
            install_sqlite_capture(
                source_conn,
                run_id=args.run_id,
                schema_epoch=MANIFEST.schema_pair.epoch,
                manifest=MANIFEST,
            )
            sqlite_report = committed_sqlite_guard_report(
                source_conn,
                source_url=settings.database_url,
                manifest=MANIFEST,
                run_id=args.run_id,
            )
            postgres_report = prepare_postgres_replication_key_guards(
                target, manifest=MANIFEST, run_id=args.run_id
            )
            state = get_run(target, args.run_id)
            state = commit_guard_report(
                target, report=sqlite_report, expected_revision=state.revision
            )
            state = commit_guard_report(
                target, report=postgres_report, expected_revision=state.revision
            )
            enable_sqlite_capture_after_guard_reports(
                source_conn,
                target,
                source_url=settings.database_url,
                sqlite_report=sqlite_report,
                postgres_report=postgres_report,
                manifest=MANIFEST,
            )
            state = get_run(target, args.run_id)
            state = update_forward_state(
                target,
                run_id=args.run_id,
                expected_phase=ShadowPhase.OFF,
                expected_revision=state.revision,
                new_phase=ShadowPhase.SQLITE_TO_POSTGRES,
                progress={"stage": "capture-enabled"},
                source_url=settings.database_url,
                source_conn=source_conn,
            )
        if state.phase is not ShadowPhase.SQLITE_TO_POSTGRES:
            raise OperatorError("shadow run is not in the forward phase")
        snapshot = _snapshot_info(work_dir, source, report)
        if _baseline_is_published(target, snapshot):
            tables_copied = len(MANIFEST.replicated)
        else:
            copied = copy_snapshot(snapshot, source, target, MANIFEST)
            tables_copied = len(copied.tables)
        worker_token = issue_confirmation_token(report.binding, secret=secret, ttl_seconds=3_600)
        token_path = work_dir / _WORKER_TOKEN_FILE
        _atomic_private_write(token_path, worker_token.encode("ascii"))
        command = "PYTHONPATH=backend " + shlex.join(
            [
                "python",
                "scripts/migrate_sqlite_to_postgres.py",
                "worker",
                "--run-id",
                args.run_id,
                "--direction",
                "forward",
                "--work-dir",
                str(work_dir),
                "--confirmation-token-file",
                str(token_path),
            ]
        )
        return {
            "command": "start-forward",
            "status": "ready",
            "run_id": args.run_id,
            "phase": state.phase.value,
            "baseline_seq": snapshot.baseline_seq,
            "tables_copied": tables_copied,
            "worker_command": command,
        }
    finally:
        source.close()
        target.close()


def _status(args: argparse.Namespace, settings: Settings) -> Mapping[str, Any]:
    source, target = _databases(settings)
    try:
        state = get_run(target, args.run_id)
        with target.connect() as conn:
            checkpoint = conn.execute(
                "SELECT last_seq,updated_at FROM silicon_shadow.checkpoints "
                "WHERE run_id=%s AND direction='sqlite_to_postgres'",
                (args.run_id,),
            ).fetchone()
            poison = conn.execute(
                "SELECT COUNT(*) AS count,MIN(source_seq) AS first_seq "
                "FROM silicon_shadow.poison_events WHERE run_id=%s "
                "AND direction='sqlite_to_postgres'",
                (args.run_id,),
            ).fetchone()
            worker = conn.execute(
                "SELECT lease_expires_at>clock_timestamp() AS live,heartbeat_at "
                "FROM silicon_shadow.workers WHERE run_id=%s "
                "AND direction='sqlite_to_postgres'",
                (args.run_id,),
            ).fetchone()
            verification = conn.execute(
                "SELECT level,status,target_checkpoint,completed_at "
                "FROM silicon_shadow.verification_runs WHERE run_id=%s "
                "ORDER BY started_at DESC,verification_id DESC LIMIT 1",
                (args.run_id,),
            ).fetchone()
        conn = source.connect()
        high = conn.execute(
            "SELECT COALESCE(MAX(seq),0),COUNT(*) FROM shadow_change_log"
        ).fetchone()
        return {
            "command": "status",
            "status": "ok",
            "run_id": args.run_id,
            "phase": state.phase.value,
            "active_backend": state.active_backend,
            "revision": state.revision,
            "capture_enabled": state.capture_enabled,
            "progress": state.progress,
            "checkpoint": int(checkpoint["last_seq"]) if checkpoint else None,
            "source_high_water": int(high[0]),
            "change_log_rows": int(high[1]),
            "poison_count": int(poison["count"]),
            "first_poison_seq": poison["first_seq"],
            "worker_live": bool(worker["live"]) if worker else False,
            "worker_heartbeat": str(worker["heartbeat_at"]) if worker else None,
            "latest_verification": dict(verification) if verification else None,
        }
    finally:
        source.close()
        target.close()


def _verify(args: argparse.Namespace, settings: Settings) -> Mapping[str, Any]:
    work_dir = _work_dir(args)
    _verify_confirmation(args, work_dir)
    source, target = _databases(settings)
    try:
        report = ShadowVerifier(source, target).verify(
            run_id=args.run_id, level=VerificationLevel(args.level)
        )
        return {
            "command": "verify",
            "status": report.status.value,
            "run_id": args.run_id,
            "report": _jsonable(report),
        }
    finally:
        source.close()
        target.close()


def _worker(args: argparse.Namespace, settings: Settings) -> Mapping[str, Any]:
    work_dir = _work_dir(args)
    _verify_confirmation(args, work_dir)
    if args.direction != "forward":
        raise OperatorError("only the forward worker direction is implemented")
    source, target = _databases(settings)
    try:
        worker = ForwardWorker(source, target, run_id=args.run_id)
        with termination_event() as stop:
            worker.run(stop)
        return {
            "command": "worker",
            "status": "stopped",
            "run_id": args.run_id,
            "direction": Direction.SQLITE_TO_POSTGRES.value,
        }
    finally:
        source.close()
        target.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate_sqlite_to_postgres.py",
        description="Operate explicit SQLite-to-PostgreSQL forward shadow sync.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common(name: str, help_text: str) -> argparse.ArgumentParser:
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--run-id", required=True, type=_validate_run_id)
        command.add_argument("--work-dir")
        command.add_argument("--json", action="store_true", dest="as_json")
        return command

    status = common("status", "show redacted run status")
    status.set_defaults(handler=_status)

    check = common("preflight", "run the read-only compatibility preflight")
    check.add_argument("--disk-evidence-id")
    check.add_argument("--available-target-bytes", type=int)
    check.add_argument("--backup-evidence-id")
    check.add_argument("--confirm-source-backup", action="store_true")
    check.add_argument("--confirm-target-restore", action="store_true")
    check.set_defaults(handler=_preflight)

    start = common("start-forward", "prepare baseline copy and forward capture")
    start.add_argument("--confirmation-token", required=True)
    start.set_defaults(handler=_start_forward)

    verify_parser = common("verify", "verify the shadow at a safe barrier")
    verify_parser.add_argument("--level", required=True, choices=("structural", "full"))
    verify_confirmation = verify_parser.add_mutually_exclusive_group(required=True)
    verify_confirmation.add_argument("--confirmation-token")
    verify_confirmation.add_argument("--confirmation-token-file")
    verify_parser.set_defaults(handler=_verify)

    worker = common("worker", "run one foreground replication worker")
    worker.add_argument("--direction", required=True, choices=("forward",))
    worker_confirmation = worker.add_mutually_exclusive_group(required=True)
    worker_confirmation.add_argument("--confirmation-token")
    worker_confirmation.add_argument("--confirmation-token-file")
    worker.set_defaults(handler=_worker)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = args.handler(args, Settings())
        _emit(payload, as_json=args.as_json)
        return 0
    except KeyboardInterrupt:
        error = "interrupted"
    except BaseException as exc:
        error = _safe_error(exc)
    if args.as_json:
        print(
            json.dumps(
                {"command": args.command, "status": "error", "error": error},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
    else:
        print(f"shadow {args.command}: error: {error}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
