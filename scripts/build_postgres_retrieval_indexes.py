#!/usr/bin/env python3
"""Inspect or concurrently build notebook-aware PostgreSQL trigram indexes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.database_url import database_identity  # noqa: E402
from app.repositories.postgres.retrieval_indexes import (  # noqa: E402
    RetrievalIndexError,
    inspect_retrieval_indexes,
    install_retrieval_indexes,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect or concurrently build notebook-aware PostgreSQL trigram indexes. "
            "The database URL is read from an environment variable and never printed."
        )
    )
    parser.add_argument("--database-url-env", default="DATABASE_URL")
    parser.add_argument("--schema", default="public")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--drop-legacy",
        action="store_true",
        help="after both new indexes verify, concurrently drop the old global GIN indexes",
    )
    parser.add_argument("--lock-timeout-seconds", type=int, default=5)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.drop_legacy and not args.apply:
        print("error: --drop-legacy requires --apply", file=sys.stderr)
        return 2
    database_url = os.environ.get(args.database_url_env, "")
    if not database_url:
        print(f"error: environment variable {args.database_url_env} is required", file=sys.stderr)
        return 2
    if database_identity(database_url).scheme != "postgresql":
        print("error: target must be PostgreSQL", file=sys.stderr)
        return 2
    try:
        if args.apply:
            state = install_retrieval_indexes(
                database_url,
                schema=args.schema,
                lock_timeout_seconds=args.lock_timeout_seconds,
                drop_legacy=args.drop_legacy,
                progress=lambda message: print(message, flush=True),
            )
        else:
            state = inspect_retrieval_indexes(database_url, schema=args.schema)
    except (RetrievalIndexError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # Content-free catalog receipt: no URL, row data, query text, or identifiers
    # beyond fixed index/extension/schema names.
    print(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))
    if not args.apply and any(row["state"] != "ready" for row in state["indexes"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
