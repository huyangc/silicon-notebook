#!/usr/bin/env python3
"""Thin CLI for the centralized SQLite to PostgreSQL importer."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.migration.sqlite_to_postgres import (  # noqa: E402
    DEFAULT_BATCH_ROWS,
    SqliteToPostgresMigrationError,
    migrate,
    preflight,
    target_url_from_environment,
)
from app.migration.database_activation import (  # noqa: E402
    activate_postgres_from_receipt,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate one consistent silicon-notebook SQLite snapshot to an "
            "empty PostgreSQL target. Dry-run preflight is the default."
        )
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=REPO_ROOT / ".local" / "silicon_notebook.db",
        help="SQLite source file (default: the main workspace .local database)",
    )
    parser.add_argument(
        "--target-env",
        default="POSTGRES_MIGRATION_URL",
        help="environment variable containing the target URL; URL is never printed",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=REPO_ROOT / ".local" / "postgres-migration",
        help="private directory for the consistent snapshot and receipt",
    )
    parser.add_argument(
        "--batch-rows",
        type=int,
        default=DEFAULT_BATCH_ROWS,
        help=f"bounded SQLite/COPY batch size (default: {DEFAULT_BATCH_ROWS})",
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        help=(
            "reuse an importer-owned sealed snapshot after a failed target attempt; "
            "hash, quick_check, sidecars, name, and work directory are revalidated"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="create the snapshot and write the empty PostgreSQL target",
    )
    parser.add_argument(
        "--activate-env",
        type=Path,
        help=(
            "after full receipt verification, atomically switch DATABASE_URL in "
            "this stopped local deployment env file and preserve SQLite as shadow"
        ),
    )
    parser.add_argument(
        "--activation-receipt",
        type=Path,
        help=(
            "activate an already completed migration from this receipt instead of "
            "writing a new empty target"
        ),
    )
    parser.add_argument(
        "--confirm-service-stopped",
        action="store_true",
        help="required for activation: confirm all API/background writers are stopped",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        target_url = target_url_from_environment(args.target_env)
        if args.activation_receipt is not None:
            if args.apply or args.snapshot is not None:
                raise SqliteToPostgresMigrationError(
                    "--activation-receipt cannot be combined with --apply or --snapshot"
                )
            if args.activate_env is None or not args.confirm_service_stopped:
                raise SqliteToPostgresMigrationError(
                    "receipt activation requires --activate-env and "
                    "--confirm-service-stopped"
                )
            activation = activate_postgres_from_receipt(
                source_path=args.source,
                target_url=target_url,
                receipt_path=args.activation_receipt,
                work_dir=args.work_dir,
                env_path=args.activate_env,
                root_dir=REPO_ROOT,
                batch_rows=args.batch_rows,
            )
            action = "already active" if not activation.changed else "activated"
            print(
                "ACTIVATION OK: "
                f"PostgreSQL {action}; {activation.verification.total_rows} verified "
                f"rows across {len(activation.verification.tables)} tables; "
                f"env={activation.env_path}; backup={activation.backup_path or 'unchanged'}"
            )
            print("Restart the backend and require /api/ready before resuming writes.")
            return 0

        if args.activate_env is not None and (
            not args.apply or not args.confirm_service_stopped
        ):
            raise SqliteToPostgresMigrationError(
                "--activate-env requires --apply and --confirm-service-stopped"
            )
        if not args.apply:
            source, target = preflight(
                source_path=args.source,
                target_url=target_url,
                root_dir=REPO_ROOT,
            )
            print(
                "PRECHECK OK: "
                f"SQLite v{source.schema_version}, {source.size_bytes} bytes, "
                f"journal={source.journal_mode}; PostgreSQL schema={target.schema}, "
                f"prepared=v{target.prepared_version}, UTF8, empty"
            )
            print("No data was written. Re-run with --apply to migrate.")
            return 0

        result = migrate(
            source_path=args.source,
            target_url=target_url,
            work_dir=args.work_dir,
            root_dir=REPO_ROOT,
            batch_rows=args.batch_rows,
            existing_snapshot=args.snapshot,
        )
        print(
            "MIGRATION OK: "
            f"{result.total_rows} rows across {len(result.tables)} tables; "
            f"SQLite v{result.upgraded_from}->v{result.upgraded_to}; "
            f"snapshot={result.snapshot_path}; receipt={result.receipt_path}"
        )
        if args.activate_env is None:
            print(
                "DATABASE_URL was not changed. Keep SQLite authoritative until the "
                "documented stop/write-freeze/final-migration cutover."
            )
            return 0

        activation = activate_postgres_from_receipt(
            source_path=args.source,
            target_url=target_url,
            receipt_path=Path(result.receipt_path),
            work_dir=args.work_dir,
            env_path=args.activate_env,
            root_dir=REPO_ROOT,
            batch_rows=args.batch_rows,
        )
        print(
            "ACTIVATION OK: PostgreSQL activated after final checksum verification; "
            f"env={activation.env_path}; backup={activation.backup_path}"
        )
        print("Restart the backend and require /api/ready before resuming writes.")
        return 0
    except SqliteToPostgresMigrationError as exc:
        print(f"MIGRATION FAILED: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("MIGRATION CANCELLED", file=sys.stderr)
        return 130
    except Exception:
        print(
            "MIGRATION FAILED: unexpected internal error (credentials and row data hidden)",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
