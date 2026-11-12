"""Bounded-read cleanup preserves the failure the caller must classify."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from psycopg import OperationalError
from psycopg.errors import QueryCanceled

from app.domain.cancellation import AskCancelled
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.read_budget import ReadBudgetExceeded


@pytest.mark.parametrize("failure,discard", [
    (QueryCanceled("timeout"), True), (OperationalError("connection failed"), True),
    (ReadBudgetExceeded("before SQL"), False), (AskCancelled(), False),
    (ValueError("application error"), False),
])
def test_failed_read_discards_only_driver_errors_and_preserves_original(failure, discard):
    closed = []
    released = []

    def close():
        closed.append(True)
        raise RuntimeError("connection cleanup failed")

    @contextmanager
    def connect():
        try:
            yield SimpleNamespace(close=close)
        finally:
            released.append(True)

    database = object.__new__(PostgresDatabase)
    database.connect = connect

    with pytest.raises(type(failure)) as caught:
        with database.connect_read():
            raise failure

    assert caught.value is failure
    assert closed == ([True] if discard else [])
    assert released == [True]
