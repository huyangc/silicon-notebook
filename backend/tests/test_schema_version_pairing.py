"""Constant-only guard: the shadow migration module's pinned schema pair must
track the live repository schema-version pair.

It opens no database and starts no process; it only compares two
module-level constants. The wider dual-backend parity checks (the SQLite
migrator's ``SCHEMA_VERSION`` against the same pair, gap-free migrations on
both sides, port and store-surface parity) live in
``test_dual_backend_parity.py``; this file stays the narrow, import-time-error
guard for the shadow module described below.

Why this exists: `backend/app/migration/shadow/manifest.py` hardcodes
`RUNNING_SCHEMA_PAIR` as a literal `(sqlite_version, postgres_version, epoch)`
tuple instead of deriving it from `POSTGRES_SCHEMA_MANIFEST`. Every schema
bump must update both by hand, and `validate_manifest()` runs at import time
(`backend/app/migration/shadow/manifest.py` module scope), so a missed update
turns into an import-time `ValueError: shadow and repository schema pairs
disagree` the first time anything imports the shadow module — including the
`scripts/shadow_sqlite_to_postgres.py` CLI's `--help`. This guard catches a
missed update at collection time instead of at first shadow-CLI invocation.
"""
from __future__ import annotations


def test_shadow_schema_pair_matches_repository_schema_manifest():
    from app.migration.shadow.manifest import RUNNING_SCHEMA_PAIR
    from app.repositories.postgres.schema_manifest import POSTGRES_SCHEMA_MANIFEST

    assert RUNNING_SCHEMA_PAIR.sqlite_version == POSTGRES_SCHEMA_MANIFEST.sqlite_version
    assert (
        RUNNING_SCHEMA_PAIR.postgres_version
        == POSTGRES_SCHEMA_MANIFEST.postgres_version
    )
