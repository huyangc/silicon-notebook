"""How quickly a budgeted SQLite read notices a Stop.

``_READ_BUDGET_VM_STEPS`` sets how often the progress handler checks the read
budget (deadline and cancel token).  It trades interruption latency against
the young-generation GC collections each handler call lets the interpreter run
(see the comment on the constant).  The latency side is what callers rely on:
a Stop must interrupt a running statement promptly.  Pinned here end to end,
with a real statement that would otherwise run for minutes.
"""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from app.core.config import Settings
from app.repositories.read_budget import read_budget
from app.repositories.sqlite.database import SqliteDatabase

# Minutes of pure VM work: nothing but the progress handler can end it early.
_ENDLESS = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c "
    "WHERE x < 2000000000) SELECT count(*) FROM c"
)
# The stated bound: ~0.5 ms of VM work per check on the development machine;
# 250 ms leaves room for a loaded CI runner and thread scheduling, and still
# fails a regression to an interval of ~50M steps or more.
NOTICED_WITHIN_SECONDS = 0.25


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_a_running_statement_notices_the_budget_within_the_bound(tmp_path, stop):
    database = SqliteDatabase(
        Settings(database_url=f"sqlite:///{tmp_path / 'budget.db'}"), tmp_path,
    )
    cancel = threading.Event()
    fired: dict[str, float] = {}
    started = time.monotonic()
    if stop == "cancel":
        deadline = started + 60.0

        def fire():
            time.sleep(0.3)
            fired["at"] = time.monotonic()
            cancel.set()

        threading.Thread(target=fire, daemon=True).start()
    else:
        deadline = started + 0.3
        fired["at"] = deadline
    with pytest.raises(sqlite3.OperationalError) as caught:
        with read_budget(deadline, cancel):
            with database.connect() as db:
                db.execute(_ENDLESS).fetchone()
    noticed = time.monotonic()
    assert "interrupted" in str(caught.value).lower()
    assert "at" in fired, "interrupted before the stop was even requested"
    assert noticed - fired["at"] < NOTICED_WITHIN_SECONDS, noticed - fired["at"]
