"""``find_sources_metadata_rows`` (PostgreSQL) never lets its term legs be
auto-prepared: the statement text depends only on the term count, so a shared
prepared statement would drift to a generic plan (the measurement doc's
2-character ``wu`` case).  No server needed: a recording connection shows the
execute options."""
from __future__ import annotations

from app.repositories.postgres.source_store import SourceStore


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Recorder:
    def __init__(self, scope_size: int, visible_total: int):
        self.calls = []
        self._scope = [
            {"id": f"s-{i:03d}", "pub_year": None, "pending": False}
            for i in range(scope_size)
        ]
        self._visible = visible_total

    def execute(self, statement, params=None, **options):
        self.calls.append((statement, options))
        if "AS visible_total" in statement:
            return _Result([{"visible_total": self._visible,
                             "scope_total": self._visible, "pending_total": 0}])
        if "AS pending FROM" in statement:
            return _Result(self._scope)
        return _Result([])

    def term_leg_options(self):
        return [options for statement, options in self.calls if " UNION ALL " in statement]


def _store():
    return SourceStore.__new__(SourceStore)


def test_term_legs_run_as_a_custom_plan_without_a_bound_list():
    recorder = _Recorder(scope_size=10, visible_total=10)
    _store().find_sources_metadata_rows(recorder, "nb", terms=["wu"], kind="all")
    assert recorder.term_leg_options() == [{"prepare": False}]


def test_term_legs_run_as_a_custom_plan_with_a_bound_list():
    recorder = _Recorder(scope_size=2, visible_total=100)
    _store().find_sources_metadata_rows(recorder, "nb", terms=["wu"], kind="papers")
    assert recorder.term_leg_options() == [{"prepare": False}]
