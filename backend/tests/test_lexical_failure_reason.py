"""The swallowed lexical failures leave a content-free, classified reason.

``_lexical_object_hits`` (KG lexical arm) and ``_relation_lexical_candidate_ids``
(the relation endpoint probe) fail open: the ANN arms still produce
candidates, so a failure stays an event, never a banner.  The event carries
the exception's class name and a ``reason`` from the fixed set of
``read_budget.classify_statement_failure`` -- decided by exception type,
SQLSTATE or SQLite result code, never by the message text, which can quote
user data.
"""
from __future__ import annotations

import sqlite3

import psycopg.errors
import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.ports import ChunkLexicalSearchTimeout
from app.repositories.read_budget import (
    ReadBudgetExceeded,
    classify_statement_failure,
)
from app.services.sqlite_repository import SQLiteRepository

REASONS = {"statement_timeout", "variable_limit", "other"}
SECRET = "confidential-source-title"


def _sqlite_error(setup, sql, params=()):
    db = sqlite3.connect(":memory:")
    setup(db)
    with pytest.raises(sqlite3.Error) as caught:
        db.execute(sql, params).fetchall()
    return caught.value


def _interrupted():
    def setup(db):
        db.set_progress_handler(lambda: 1, 1)
    return _sqlite_error(
        setup,
        "WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM r "
        "WHERE x<100000) SELECT count(*) FROM r",
    )


def _too_big():
    def setup(db):
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 100)
    return _sqlite_error(setup, "SELECT ?", (SECRET * 20,))


def _too_many_variables():
    def setup(db):
        db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 10)
    return _sqlite_error(
        setup, "SELECT 1 WHERE 1 IN (%s)" % ",".join("?" * 11), [1] * 11,
    )


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (lambda: ReadBudgetExceeded("read budget exhausted"), "statement_timeout"),
        (lambda: ChunkLexicalSearchTimeout("chunk probe"), "statement_timeout"),
        (lambda: psycopg.errors.QueryCanceled(SECRET), "statement_timeout"),
        (_interrupted, "statement_timeout"),
        (lambda: psycopg.errors.ProgramLimitExceeded(SECRET), "variable_limit"),
        (lambda: psycopg.errors.lookup("54023")(SECRET), "variable_limit"),
        (_too_big, "variable_limit"),
        # The bound-parameter caps carry no code on either driver: SQLite
        # reports them as a generic SQLITE_ERROR, libpq refuses client-side
        # without a SQLSTATE.  The static guard keeps them from recurring.
        (_too_many_variables, "other"),
        (lambda: psycopg.OperationalError(
            "number of parameters must be between 0 and 65535"), "other"),
        (lambda: psycopg.errors.UndefinedTable(SECRET), "other"),
        (lambda: RuntimeError("timeout: too many SQL variables"), "other"),
    ],
)
def test_classification_uses_types_and_codes_only(make, reason):
    assert classify_statement_failure(make()) == reason


def test_message_text_never_decides_the_reason():
    """The same message on a different type/code classifies differently, and
    a message that names a limit or a timeout classifies as ``other``."""
    assert classify_statement_failure(
        sqlite3.OperationalError("interrupted")
    ) == "other"
    assert classify_statement_failure(
        sqlite3.OperationalError("too many SQL variables")
    ) == "other"
    assert classify_statement_failure(
        ValueError("canceling statement due to statement timeout")
    ) == "other"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def _events(repo, monkeypatch) -> list[dict]:
    events: list[dict] = []
    original = repo.event_log.emit

    def spy(event, **kwargs):
        events.append(event)
        return original(event, **kwargs)

    monkeypatch.setattr(repo.event_log, "emit", spy)
    return events


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (lambda: psycopg.errors.QueryCanceled(SECRET), "statement_timeout"),
        (_too_big, "variable_limit"),
        (lambda: RuntimeError(SECRET), "other"),
    ],
)
def test_both_swallowed_lexical_failures_emit_a_classified_reason(
    repo, monkeypatch, make, reason
):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    candidates = repo.retrieval.candidates
    events = _events(repo, monkeypatch)

    def failing(*_args, **_kwargs):
        raise make()

    def one_hit(*_args, **_kwargs):
        return [{"object_id": "ko-1", "name": "n", "score": 1.0}]

    monkeypatch.setattr(candidates.knowledge, "fts_search", failing)
    with repo._connect() as db:
        assert candidates._lexical_object_hits(
            db, notebook.id, "wafer", 8, site="kg_lexical", corpus_langs=None,
            allowed_source_ids=["s-1"],
        ) == []
    monkeypatch.setattr(candidates.knowledge, "fts_search", one_hit)
    monkeypatch.setattr(
        candidates.knowledge, "relation_id_rows_for_objects", failing,
    )
    with repo._connect() as db:
        assert candidates._relation_lexical_candidate_ids(
            db, notebook.id, "wafer", 8, corpus_langs=None,
        ) == []

    failures = [e for e in events if e.get("kind") == "lexical_retrieval_failed"]
    assert [e["site"] for e in failures] == [
        "kg_lexical", "relation_endpoint_probe",
    ]
    for event in failures:
        assert event["reason"] == reason and event["reason"] in REASONS
        assert event["notebook_id"] == notebook.id
        # ``ts`` / ``channel`` are stamped by the event log itself.
        assert set(event) - {"ts", "channel"} == {
            "kind", "notebook_id", "site", "error_type", "reason",
        }
        assert SECRET not in repr(event), event
