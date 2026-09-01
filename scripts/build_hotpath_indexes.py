#!/usr/bin/env python3
"""Inspect or concurrently build the accumulated hot-path fix PostgreSQL
indexes (six query-family groups).

    python3 scripts/build_hotpath_indexes.py                 # inspect (default)
    python3 scripts/build_hotpath_indexes.py --apply          # build missing ones

Shape mirrors ``scripts/build_postgres_retrieval_indexes.py`` (argparse,
``--database-url-env`` defaulting to ``DATABASE_URL``, the URL is never
printed, ``database_identity(...)`` must resolve to ``postgresql``, inspect
vs apply, ``--lock-timeout-seconds``). See
``app/repositories/postgres/hotpath_indexes.py`` for the index
definitions and ``migrations/0039_hotpath_batch1_indexes.sql`` for the full
per-group "which query family does this serve" evidence.

Relationship to the migration: this script is how an operator builds these
indexes (batch 1: six query-family groups, eight btree/partial indexes; batch 2: the payload-search GIN + the checkup-H5 partial index; batch 3: the concept-cluster keyset-covering index; batch 4: the three source-search GIN trigram indexes — fourteen in total) on an already-populated, already-serving-traffic production
database WITHOUT taking a blocking lock (``CREATE INDEX CONCURRENTLY``, one
statement per index, no transaction). Migration 0039 in
``backend/app/repositories/postgres/migrations/`` uses plain
``CREATE INDEX IF NOT EXISTS`` (the migration runner always runs inside a
transaction, where ``CONCURRENTLY`` cannot run) and becomes a no-op ledger
entry once this script has built every index. On a fresh database with no
existing traffic, the migration alone is sufficient and this script has
nothing left to do.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.database_url import database_identity  # noqa: E402
from app.repositories.postgres.hotpath_indexes import (  # noqa: E402
    HotpathIndexError,
    inspect_hotpath_indexes,
    install_hotpath_indexes,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect or concurrently build the accumulated hot-path fix PostgreSQL "
            "indexes (six query-family groups). The database URL is read from an "
            "environment variable and never printed."
        )
    )
    parser.add_argument("--database-url-env", default="DATABASE_URL")
    parser.add_argument("--schema", default="public")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--lock-timeout-seconds", type=int, default=5)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database_url = os.environ.get(args.database_url_env, "")
    if not database_url:
        print(f"error: environment variable {args.database_url_env} is required", file=sys.stderr)
        return 2
    if database_identity(database_url).scheme != "postgresql":
        print("error: target must be PostgreSQL", file=sys.stderr)
        return 2
    try:
        if args.apply:
            state = install_hotpath_indexes(
                database_url,
                schema=args.schema,
                lock_timeout_seconds=args.lock_timeout_seconds,
                progress=lambda message: print(message, flush=True),
            )
        else:
            state = inspect_hotpath_indexes(database_url, schema=args.schema)
    except (HotpathIndexError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    # Content-free receipt: no URL, row data, query text, or identifiers beyond
    # the fixed index/table/schema names already public in this repository.
    print(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))
    if any(row["state"] != "存在" for row in state["indexes"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
