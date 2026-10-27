"""Total KG supplement deadlines and completed-phase preservation, without I/O."""
from __future__ import annotations

import pytest
from psycopg import errors

from app.repositories.postgres import knowledge_store, lexical_budget, search


class _Clock:
    now = 0.0

    def __call__(self):
        return self.now


class _Rows:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, clock, phases, timeout_ms=30000):
        self.clock = clock
        self.phases = iter(phases)
        self.timeout_ms = timeout_ms
        self.calls = []
        self.savepoints = {}
        self.usable = True

    def execute(self, statement, params=None, **_options):
        self.calls.append((statement, params))
        if statement.startswith("SAVEPOINT "):
            self.savepoints[statement.split()[-1]] = self.timeout_ms
        elif statement.startswith("ROLLBACK TO SAVEPOINT "):
            self.timeout_ms = self.savepoints[statement.split()[-1]]
            self.usable = True
        elif statement.startswith("RELEASE SAVEPOINT "):
            self.savepoints.pop(statement.split()[-1])
        elif "FROM pg_settings" in statement:
            return _Rows([{"timeout_ms": self.timeout_ms}])
        elif "set_config('statement_timeout'" in statement:
            self.timeout_ms = int(params[0].removesuffix("ms"))
        elif "CROSS JOIN LATERAL" in statement:
            duration, result = next(self.phases)
            allowed = self.timeout_ms / 1000
            self.clock.now += min(duration, allowed)
            if duration > allowed:
                self.usable = False
                raise errors.QueryCanceled("private database text")
            if isinstance(result, Exception):
                self.usable = False
                raise result
            return _Rows(result)
        elif statement == "SELECT 1":
            assert self.usable
            return _Rows([{"value": 1}])
        else:
            raise AssertionError("unexpected query after a bounded candidate read")
        return _Rows()


def _candidate(candidate_id, name="thermal", rank=0):
    return {
        "candidate_id": candidate_id,
        "candidate_name": name,
        "candidate_similarity": 0.7,
        "term_rank": rank,
    }


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(lexical_budget, "monotonic", clock)
    monkeypatch.setattr(search, "knn_name_index_available", lambda _db: True)
    monkeypatch.setattr(
        knowledge_store, "corpus_gated_recall_terms", lambda _query, _langs: ["thermal"],
    )
    return clock


def _search(connection, *, seconds=5, **kwargs):
    stats = {}
    result = knowledge_store.KnowledgeStore.fts_search(
        connection, "nb", "thermal", k=3, allow_knn=True,
        time_budget_seconds=seconds, routing_stats=stats, **kwargs,
    )
    return result, stats


def test_short_page_timeout_keeps_knn_rows_and_shares_remaining_deadline(clock):
    connection = _Connection(clock, [(4, [_candidate("ko-knn")]), (2, [])])

    result, stats = _search(connection)

    assert [row["object_id"] for row in result] == ["ko-knn"]
    assert result[0]["name"] == "thermal"
    assert stats["budget_exhausted"] == 1
    configs = [p[0] for s, p in connection.calls if "set_config(" in s]
    assert configs == ["5000ms", "1000ms"]
    assert clock.now == 5
    assert connection.timeout_ms == 30000
    assert connection.savepoints == {}
    connection.execute("SELECT 1")


def test_direct_legacy_timeout_preserves_both_completed_knn_phases(clock, monkeypatch):
    monkeypatch.setattr(
        knowledge_store, "corpus_gated_recall_terms",
        lambda _query, _langs: ["thermal", "中文"],
    )
    connection = _Connection(clock, [
        (1, [_candidate("ko-knn")]),
        (1, [_candidate("ko-ilike", "thermal appendix")]),
        (4, []),
    ])

    result, stats = _search(connection)

    assert {row["object_id"] for row in result} == {"ko-knn", "ko-ilike"}
    assert stats["budget_exhausted"] == 1
    assert [p[0] for s, p in connection.calls if "set_config(" in s] == [
        "5000ms", "4000ms", "3000ms",
    ]
    assert connection.timeout_ms == 30000


@pytest.mark.parametrize("original_timeout", [0, 30000])
def test_first_phase_timeout_has_no_partial_rows_or_extra_queries(clock, original_timeout):
    connection = _Connection(clock, [(6, [])], timeout_ms=original_timeout)

    result, stats = _search(connection)

    assert result == []
    assert stats["budget_exhausted"] == 1
    assert len([s for s, _p in connection.calls if "CROSS JOIN LATERAL" in s]) == 1
    assert connection.timeout_ms == original_timeout
    connection.execute("SELECT 1")


def test_stricter_caller_timeout_is_never_widened(clock):
    connection = _Connection(clock, [(1, [])], timeout_ms=200)

    result, stats = _search(connection)

    assert result == []
    assert stats["budget_exhausted"] == 1
    assert clock.now == 0.2
    assert connection.timeout_ms == 200
    assert [p[0] for s, p in connection.calls if "set_config(" in s] == ["200ms"]


def test_stricter_caller_timeout_caps_the_whole_call_not_each_phase(clock):
    connection = _Connection(
        clock, [(1, [_candidate("ko-knn")]), (1.5, [])], timeout_ms=2000,
    )

    result, stats = _search(connection)

    assert [row["object_id"] for row in result] == ["ko-knn"]
    assert stats["budget_exhausted"] == 1
    assert clock.now == 2
    assert [p[0] for s, p in connection.calls if "set_config(" in s] == [
        "2000ms", "1000ms",
    ]
    assert connection.timeout_ms == 2000


def test_completed_rows_need_no_hydration_query_and_timeout_is_restored(clock):
    connection = _Connection(clock, [
        (1, [_candidate("ko-knn")]),
        (1, [_candidate("ko-ilike", "thermal appendix")]),
    ])

    result, stats = _search(connection)

    assert {row["object_id"] for row in result} == {"ko-knn", "ko-ilike"}
    assert stats["budget_exhausted"] == 0
    assert connection.timeout_ms == 30000
    assert connection.savepoints == {}


def test_expired_before_first_candidate_query_does_not_start_sql(clock, monkeypatch):
    def available(_connection):
        clock.now = 5
        return True

    monkeypatch.setattr(search, "knn_name_index_available", available)
    connection = _Connection(clock, [])

    result, stats = _search(connection)

    assert result == []
    assert stats["budget_exhausted"] == 1
    assert not [s for s, _p in connection.calls if "CROSS JOIN LATERAL" in s]


def test_unrelated_database_failure_is_not_treated_as_budget_expiry(clock):
    connection = _Connection(clock, [(1, ValueError("unrelated failure"))])

    with pytest.raises(ValueError, match="unrelated failure"):
        _search(connection)

    assert connection.timeout_ms == 30000
    assert connection.savepoints == {}
    connection.execute("SELECT 1")


@pytest.mark.parametrize("seconds", [0, -1, float("nan"), float("inf")])
def test_invalid_direct_budget_is_rejected_without_sql(clock, seconds):
    connection = _Connection(clock, [])
    with pytest.raises(ValueError, match="positive and finite"):
        _search(connection, seconds=seconds)
    assert connection.calls == []
