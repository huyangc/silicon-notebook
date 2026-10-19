"""Global Ask turn retention after a participant library is deleted -- SQLite.

Runs the SAME case list as the PostgreSQL twin
(``tests/postgres/test_global_ask_retention_pg.py``); the scenarios live in
``tests/global_ask_retention_cases.py``. The rest of this file covers what is
not a storage contract: the ``scope="global"`` event marker and the domain
helpers' SQL spelling.
"""
from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.core.event_logging import EventLogger, current_event_scope
from app.domain.global_ask_retention import (
    live_turn_sql,
    participant_bind_count,
    participant_sql,
)
from app.services.sqlite_repository import SQLiteRepository
from tests.global_ask_retention_cases import CASE_IDS, CASES


@pytest.fixture
def repo(tmp_path):
    settings = Settings(
        _env_file=None, database_url=f"sqlite:///{tmp_path / 'retention.db'}",
        storage_dir=str(tmp_path / "storage"), event_log_enabled=False,
    )
    repository = SQLiteRepository(settings)
    yield repository
    repository.close()


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_global_ask_retention_contract(repo, case):
    case(repo)


def _events(tmp_path):
    lines = []
    for path in sorted(tmp_path.glob("events-*.jsonl")):
        lines.extend(json.loads(line) for line in path.read_text().splitlines() if line)
    return lines


def test_every_event_of_a_global_run_is_marked_global(tmp_path):
    """``global_ask_run`` installs the marker for the run's whole extent, so the
    stage events and the retrieval run's own totals (emitted on the way OUT of
    the engine, still inside the run) carry ``scope="global"`` beside the
    ``notebook_id`` they already named; outside a run nothing is added."""
    from app.services.global_run import global_ask_run
    from app.services.retrieval_run import retrieval_run
    from tests.test_global_run import _ceilings, _override, _plan, _turn

    logger = EventLogger(
        Settings(_env_file=None, event_log_dir=str(tmp_path)), channel="events",
    )
    logger.emit({"kind": "ask_stage", "notebook_id": "nb-1", "stage": "total"})
    override = _override()
    with global_ask_run(
        override, _ceilings(), _turn(), _plan(), nominal_active=override.nominal_active_id,
    ):
        assert current_event_scope() == "global"
        with retrieval_run(run_kind="ask_chunk", event_log=logger, correlation_id="gask-1"):
            logger.emit({"kind": "ask_stage", "notebook_id": override.nominal_active_id,
                         "stage": "total"})
    assert current_event_scope() is None
    logger.emit({"kind": "ask_stage", "notebook_id": "nb-1", "stage": "total"})

    events = _events(tmp_path)
    assert [event["kind"] for event in events] == [
        "ask_stage", "ask_stage", "retrieval_run_stats", "ask_stage",
    ]
    assert "scope" not in events[0] and "scope" not in events[3]
    assert events[1]["scope"] == "global" and events[2]["scope"] == "global"
    # The anchor id stays where existing consumers read it.
    assert events[1]["notebook_id"] == override.nominal_active_id


def test_retention_fragments_are_dialect_exact():
    assert live_turn_sql(dialect="sqlite", alias="g") == (
        "(g.retained_until IS NULL OR julianday(g.retained_until) > julianday('now'))"
    )
    assert live_turn_sql(dialect="postgres") == (
        "(retained_until IS NULL OR retained_until::timestamptz > CURRENT_TIMESTAMP)"
    )
    assert participant_sql(dialect="sqlite", marker="?").count("?") == participant_bind_count(
        dialect="sqlite")
    assert participant_sql(dialect="postgres", marker="%s").count("%s") == participant_bind_count(
        dialect="postgres")
    with pytest.raises(ValueError):
        live_turn_sql(dialect="mysql")
