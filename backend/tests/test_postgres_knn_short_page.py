"""A completed short KNN page must not pay for the similarity probe twice."""
from __future__ import annotations

import pytest

from app.repositories.postgres import search


def _row(candidate_id, rank, score):
    return {
        "candidate_id": candidate_id,
        "term_rank": rank,
        "candidate_similarity": score,
    }


class _IlikeConnection:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, statement, params):
        self.calls.append((statement, params))
        # This is a performance contract: reuse the completed similarity
        # result, while still executing the independently bounded ILIKE arm.
        assert "OPERATOR(public.%%)" not in statement
        assert " ILIKE " in statement
        assert "notebook_id=%s" in statement
        assert "status!='deprecated'" in statement
        return self

    def fetchall(self):
        return self.rows


def test_short_pages_reuse_similarity_rows_and_merge_literal_ilike(monkeypatch):
    # Rank 0 is a short page, rank 1 has no similarity matches, rank 2 is
    # full. Equal-score ids are deliberately supplied in reverse C order.
    knn = [
        _row("z-shared", 0, 0.8),
        _row("a-similarity-only", 0, 0.8),
        _row("full-b", 2, 0.9),
        _row("full-a", 2, 0.9),
        _row("full-c", 2, 0.7),
    ]
    monkeypatch.setattr(search, "_knn_candidate_rows_for_terms", lambda *a, **k: knn)
    connection = _IlikeConnection([
        _row("z-shared", 0, 0.8),
        _row("a-ilike-only", 0, 0.8),
        _row("low-ilike", 0, 0.1),
        _row("literal-only", 1, 0.05),
    ])
    stats = {}
    result = search._knowledge_rows_via_knn(
        connection, "nb-target", [(0, "thermal"), (1, "set_db%"), (2, "full")],
        3, stats,
    )

    assert result == [
        _row("a-ilike-only", 0, 0.8),
        _row("a-similarity-only", 0, 0.8),
        _row("z-shared", 0, 0.8),
        _row("literal-only", 1, 0.05),
        _row("full-a", 2, 0.9),
        _row("full-b", 2, 0.9),
        _row("full-c", 2, 0.7),
    ]
    assert len(connection.calls) == 1
    assert connection.calls[0][1] == [
        0, "thermal", "%thermal%", 1, "set_db%", "%set\\_db\\%%",
        "nb-target", 3,
    ]
    assert stats["knn_short_fallback_term_count"] == 2
    assert stats["knn_short_fallback_seconds"] >= 0


def test_full_page_does_not_query_ilike_or_change_tie_membership(monkeypatch):
    knn = [_row("full-z", 4, 0.8), _row("full-a", 4, 0.8)]
    monkeypatch.setattr(search, "_knn_candidate_rows_for_terms", lambda *a, **k: knn)
    connection = _IlikeConnection([])

    assert search._knowledge_rows_via_knn(
        connection, "nb-target", [(4, "full")], 2,
    ) == knn
    assert connection.calls == []


def test_ilike_failure_propagates_without_rerunning_similarity(monkeypatch):
    monkeypatch.setattr(search, "_knn_candidate_rows_for_terms", lambda *a, **k: [])

    class _FailedConnection(_IlikeConnection):
        def fetchall(self):
            raise RuntimeError("probe failed")

    connection = _FailedConnection([])
    stats = {}
    with pytest.raises(RuntimeError, match="probe failed"):
        search._knowledge_rows_via_knn(
            connection, "nb-target", [(2, "empty")], 3, stats,
        )
    assert len(connection.calls) == 1
    assert stats["knn_short_fallback_seconds"] >= 0
