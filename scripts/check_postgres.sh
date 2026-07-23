#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# Preserve repo-relative interpreter paths after the script changes into backend/.
if [[ "$PYTHON_BIN" != /* && -x "$ROOT_DIR/$PYTHON_BIN" ]]; then
  PYTHON_BIN="$ROOT_DIR/$PYTHON_BIN"
fi

: "${TEST_POSTGRES_URL:?TEST_POSTGRES_URL is required}"

if [[ "${POSTGRES_CI_AUXILIARY_TARGETS_REQUIRED:-0}" == "1" ]]; then
  : "${TEST_POSTGRES_NON_C_URL:?TEST_POSTGRES_NON_C_URL is required in the authoritative CI lane}"
  : "${TEST_POSTGRES_NON_UTF_URL:?TEST_POSTGRES_NON_UTF_URL is required in the authoritative CI lane}"
fi

# The PostgreSQL lane is still network-offline with respect to model providers.
# It talks only to the explicitly supplied disposable database targets.
export SILICON_NOTEBOOK_ENV_FILE=""
export OPENAI_COMPAT_BASE_URL="" OPENAI_COMPAT_API_KEY="" OPENAI_COMPAT_MODEL=""
export REASONING_LLM_BASE_URL="" REASONING_LLM_API_KEY="" REASONING_LLM_MODEL=""
export REWRITE_LLM_BASE_URL="" REWRITE_LLM_API_KEY="" REWRITE_LLM_MODEL=""
export KG_LLM_BASE_URL="" KG_LLM_API_KEY="" KG_LLM_MODEL=""
export EMBED_PROVIDER="" EMBED_BASE_URL="" EMBED_API_KEY="" EMBED_MODEL=""
export RERANK_MODEL="" RERANK_API_KEY=""
export MINERU_MODE="off" MINERU_API_TOKEN=""

PYTHONPATH="$ROOT_DIR/backend" "$PYTHON_BIN" - <<'PY'
import os
import re
import sys

import psycopg
from psycopg.rows import dict_row

from app.core.database_url import database_identity, database_status, redact_database_url


DEDICATED = re.compile(r"^silicon_notebook_(?:test(?:_[a-z0-9_]+)?|[a-z0-9_]+_test)$")


def fail(label: str, url: str, reason: str) -> None:
    # Both helpers intentionally discard userinfo and query options. Never print
    # a raw conninfo or the original driver exception.
    safe = database_status(url)
    _ = redact_database_url(url)
    print(f"PostgreSQL {label} preflight failed: {safe} ({reason})", file=sys.stderr)
    raise SystemExit(2)


def inspect(label: str, env_name: str, expected: str) -> None:
    url = os.environ.get(env_name)
    if not url:
        return
    try:
        identity = database_identity(url)
        if identity.scheme != "postgresql" or not DEDICATED.fullmatch(identity.database):
            fail(label, url, "target must be a dedicated silicon_notebook_*_test database")
        with psycopg.connect(url, row_factory=dict_row, connect_timeout=5) as connection:
            row = connection.execute(
                "SELECT current_database() AS database, "
                "current_setting('server_encoding') AS encoding, "
                "datcollate,datctype FROM pg_database WHERE datname=current_database()"
            ).fetchone()
        if str(row["database"]) != identity.database:
            fail(label, url, "database identity mismatch")
        encoding = str(row["encoding"])
        if expected == "utf8" and encoding != "UTF8":
            fail(label, url, "server_encoding must be UTF8")
        if expected == "non-c":
            if encoding != "UTF8":
                fail(label, url, "non-C target must be UTF8")
            if str(row["datcollate"]) in {"C", "POSIX"} or str(row["datctype"]) in {"C", "POSIX"}:
                fail(label, url, "database default collation must be non-C")
        if expected == "non-utf" and encoding == "UTF8":
            fail(label, url, "negative target must not be UTF8")
    except SystemExit:
        raise
    except BaseException:
        fail(label, url, "connection or identity check failed")
    print(f"PostgreSQL {label} preflight ok: {database_status(url)}")


inspect("primary", "TEST_POSTGRES_URL", "utf8")
inspect("non-C UTF8", "TEST_POSTGRES_NON_C_URL", "non-c")
inspect("non-UTF negative", "TEST_POSTGRES_NON_UTF_URL", "non-utf")
PY

cd "$ROOT_DIR/backend"
PYTHONPATH="$ROOT_DIR/backend" "$PYTHON_BIN" -m pytest -p no:cacheprovider \
  -n 0 \
  -m postgres_integration tests/postgres
