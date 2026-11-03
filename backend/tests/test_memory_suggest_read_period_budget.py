"""Finite work budget for enabled-reader capture and set-based fence writes."""
from __future__ import annotations

from app.repositories.sqlite.memory_suggest_read_period import readers_before_loss_on, record_read_loss_on
from tests import memory_suggest_access_loss_cases as cases
from tests.test_memory_suggest_access_loss import env as env


class _CountedConnection:
    def __init__(self, db):
        self.db = db
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return self.db.execute(sql, params)


def test_read_loss_work_is_set_based_and_ignores_manual_rows(env):
    env.seed()
    env.grant("everyone", "everyone", "")
    # Unrelated manual switch rows must not enter the affected reader set.
    with env.rt.database.write() as db:
        db.executemany(
            "INSERT INTO memory_suggest_users(owner_id,enabled,updated_at) VALUES (?,0,?)",
            [(f"manual-{index}", cases.NOW) for index in range(2048)],
        )
    work = []
    for count in (128, 512):
        with env.rt.database.write() as db:
            db.execute("DELETE FROM memory_suggest_read_fences")
            db.execute("DELETE FROM memory_suggest_users WHERE enabled=1")
            db.executemany(
                "INSERT INTO memory_suggest_users(owner_id,enabled,enabled_at,updated_at) VALUES (?,1,?,?)",
                [(f"enabled-{index}", cases.NOW, cases.NOW) for index in range(count)],
            )
            db.execute("INSERT OR IGNORE INTO notebook_grants(id,notebook_id,principal_type,principal_id,role,created_at) VALUES ('everyone',?,'everyone','','viewer',?)",
                       (cases.NB, cases.NOW))
            measured = _CountedConnection(db)
            steps = 0

            def tick():
                nonlocal steps
                steps += 1
                return 0

            db.set_progress_handler(tick, 1)
            try:
                readers = readers_before_loss_on(measured, [cases.NB])
                db.execute("DELETE FROM notebook_grants WHERE id='everyone'")
                record_read_loss_on(measured, readers, now=cases.at(5))
            finally:
                db.set_progress_handler(None, 0)
            assert len(readers) == count
            assert len(measured.calls) == 2
            plan = db.execute("EXPLAIN QUERY PLAN " + measured.calls[0][0], measured.calls[0][1]).fetchall()
            assert any("idx_memory_suggest_users_enabled" in str(tuple(row)) for row in plan)
            assert db.execute("SELECT COUNT(*) FROM memory_suggest_read_fences").fetchone()[0] == count
            # An older clock sample must never move the epoch backwards.
            record_read_loss_on(db, readers, now=cases.at(4))
            assert db.execute("SELECT DISTINCT lost_at FROM memory_suggest_read_fences").fetchall()[0][0] == cases.at(5)
            work.append(steps)
    print(f"read-period VM steps: {work[0]} -> {work[1]}")
    assert work[1] < work[0] * 7

