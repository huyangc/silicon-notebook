"""The Gate 0 tool reads the local library through a connection that cannot write (plan G0-T4)."""
from __future__ import annotations

import psycopg
import pytest

from app.eval import kg_attrs_calibration as cal


def test_prepare_connection_reads_but_never_writes(postgres_scope):
    with psycopg.connect(postgres_scope.url, autocommit=True) as setup:
        setup.execute("CREATE TABLE kg_attrs_probe (id integer)")
        setup.execute("INSERT INTO kg_attrs_probe VALUES (1)")
    conn = cal.read_only_connection(postgres_scope.url)
    try:
        assert conn.execute("SELECT count(*) AS n FROM kg_attrs_probe").fetchone()["n"] == 1
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("INSERT INTO kg_attrs_probe VALUES (2)")
        conn.execute("ROLLBACK")
        # the session default stays read-only after the explicit transaction ends
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("DELETE FROM kg_attrs_probe")
    finally:
        conn.close()
    with psycopg.connect(postgres_scope.url) as check:
        assert check.execute("SELECT count(*) FROM kg_attrs_probe").fetchone()[0] == 1
