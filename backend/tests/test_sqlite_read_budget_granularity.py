"""How quickly a budgeted SQLite read notices a Stop.

``_READ_BUDGET_VM_STEPS`` sets how often the progress handler checks the read
budget (deadline and cancel token -- one ``budget.check()`` covers both).  It
trades interruption latency against the young-generation GC collections each
handler call lets the interpreter run (see the comment on the constant).  The
latency side is what callers rely on: a Stop must interrupt a RUNNING
statement promptly.  Pinned here end to end, with a real statement that would
otherwise run for minutes, and the Stop fired only once the statement is
executing (a SQL function marks its first row), so the measured delay is the
handler interval and not the entry check.
"""
from __future__ import annotations

import sqlite3
import threading
import time

from app.core.config import Settings
from app.repositories.read_budget import read_budget
from app.repositories.sqlite.database import SqliteDatabase

# Minutes of pure VM work: nothing but the progress handler can end it early.
_ENDLESS = (
    "WITH RECURSIVE c(x) AS (SELECT test_budget_mark(1) UNION ALL "
    "SELECT x + 1 FROM c WHERE x < 2000000000) SELECT count(*) FROM c"
)
# The stated bound.  On the development machine a check runs every ~0.5 ms of
# this statement (0.29 ms median from Stop to interrupt at 100 000 steps);
# 250 ms leaves room for a loaded CI runner and thread scheduling, and still
# fails an interval of 100M steps (measured: ~0.4 s from Stop to interrupt).
NOTICED_WITHIN_SECONDS = 0.25


def test_a_running_statement_notices_a_stop_within_the_bound(tmp_path):
    database = SqliteDatabase(
        Settings(database_url=f"sqlite:///{tmp_path / 'budget.db'}"), tmp_path,
    )
    cancel, running = threading.Event(), threading.Event()
    fired: dict[str, float] = {}

    def mark(value):
        running.set()
        return value

    def stop_once_running():
        running.wait(30)
        time.sleep(0.02)
        fired["at"] = time.monotonic()
        cancel.set()

    stopper = threading.Thread(target=stop_once_running, daemon=True)
    stopper.start()
    try:
        with read_budget(time.monotonic() + 60.0, cancel):
            with database.connect() as db:
                db.create_function("test_budget_mark", 1, mark)
                db.execute(_ENDLESS).fetchone()
    except sqlite3.OperationalError as exc:
        noticed = time.monotonic()
        assert "interrupted" in str(exc).lower()
    else:
        raise AssertionError("the statement was never interrupted")
    finally:
        running.set()
        stopper.join(5)
    assert "at" in fired, "interrupted before the Stop was requested"
    assert noticed - fired["at"] < NOTICED_WITHIN_SECONDS, noticed - fired["at"]
