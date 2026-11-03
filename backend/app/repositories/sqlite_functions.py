"""Pure SQLite schema functions for runtime and offline database connections.

Expression indexes require the same deterministic callbacks during writes,
integrity checks and VACUUM as when the schema was created. Registering these
callbacks performs no SQL and never changes data or connection policy.
"""
from __future__ import annotations

import sqlite3

from app.domain.instant_order import utc_microsecond_key


def register_sqlite_functions(connection: sqlite3.Connection) -> None:
    connection.create_function("sn_utc_us", 1, utc_microsecond_key, deterministic=True)
