"""Asking about a deep report over HTTP, on SQLite (plan 2026-10-21 T9).

The route scenarios live in ``ask_report_followup_api_cases.py`` and run here
and, unchanged, on PostgreSQL in ``postgres/test_ask_report_followup_api_pg.py``.
This file adds what has no database in it: the follow-up scope's compact form
for a selection longer than a request may carry, and the receipt reader the
conversation share disclosure counts Memory with.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.services.share_disclosure import linked_report_memory_ids
from tests.ask_report_followup_api_cases import CASES
from tests.report_share_disclosure_cases import build_world


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.api import deps
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    deps.repository.cache_clear()
    return TestClient(create_app())


@pytest.fixture
def world(client, monkeypatch):
    return build_world(client, monkeypatch)


@pytest.mark.parametrize("case", sorted(CASES))
def test_ask_report_followup_api_scenario(world, case):
    CASES[case](world)


# --- the follow-up scope's request-sized form ---------------------------------


class _Roster:
    """The two reads ``_followup_source_scope`` makes of the repository."""

    def __init__(self, universe: list[str]) -> None:
        self.universe = universe
        self.reads = 0

    def all_visible_source_ids(self, notebook_id: str) -> list[str]:
        self.reads += 1
        return list(self.universe)


def test_followup_scope_cap_matches_the_request_model():
    """The cap the follow-up scope fits its ids under is the one the browser's
    next request is validated against."""
    from app.api.report_routes import _SCOPE_IDS_MAX
    from app.models.source_scope import SourceScope

    (cap,) = [
        meta.max_length for meta in SourceScope.model_fields["source_ids"].metadata
        if hasattr(meta, "max_length")
    ]
    assert _SCOPE_IDS_MAX == cap


def test_followup_scope_keeps_the_include_list_while_it_fits(monkeypatch):
    import app.api.report_routes as routes

    monkeypatch.setattr(routes, "_SCOPE_IDS_MAX", 3)
    roster = _Roster(["a", "b", "c", "d", "e"])
    scope = routes._followup_source_scope(roster, "nb", ["a", "b", "c"])
    assert (scope.mode, scope.source_ids) == ("include", ["a", "b", "c"])
    assert roster.reads == 0                  # the universe is not read for it


def test_followup_scope_flips_to_the_complement_when_the_selection_is_longer(monkeypatch):
    """A big notebook narrowed by unticking a few sources freezes into an
    include list far longer than a request may carry; the browser's own shape
    for "all but a few" is the exclude list."""
    import app.api.report_routes as routes

    monkeypatch.setattr(routes, "_SCOPE_IDS_MAX", 3)
    roster = _Roster(["a", "b", "c", "d", "e"])
    scope = routes._followup_source_scope(roster, "nb", ["a", "b", "c", "d"])
    assert (scope.mode, scope.source_ids) == ("exclude", ["e"])


def test_followup_scope_with_no_submittable_form_has_none(monkeypatch):
    import app.api.report_routes as routes

    monkeypatch.setattr(routes, "_SCOPE_IDS_MAX", 2)
    roster = _Roster(["a", "b", "c", "d", "e", "f"])
    assert routes._followup_source_scope(roster, "nb", ["a", "b", "c"]) is None


class _Notebook:
    counts = {"sources": 6}
    local_evidence_available = True
    base_notebooks = [SimpleNamespace(id="lib-a")]


class _Repo(_Roster):
    """The reads ``_report_followup_scope`` makes, over a notebook of six
    sources of which the frozen selection still sees five."""

    def __init__(self) -> None:
        super().__init__(["a", "b", "c", "d", "e", "f"])

    def get_notebook(self, notebook_id: str) -> _Notebook:
        return _Notebook()

    def visible_source_scope_snapshot(self, notebook_id: str, ids: list[str]):
        return [i for i in ids if i in self.universe], len(self.universe)


def test_a_selection_no_request_can_carry_falls_back_to_everything(monkeypatch):
    """The browser could not submit a selection that long in either form
    (include or exclude), so the follow-up scope hands back everything -- both
    dimensions, the narrowed library one too -- and says so; what dropped out
    is still counted."""
    import app.api.report_routes as routes

    monkeypatch.setattr(routes, "_SCOPE_IDS_MAX", 2)
    row = {"question": "题", "understanding": {
        "source_scope": {"mode": "include", "narrowed": True,
                         "source_ids": ["a", "b", "c", "x"]},
        "base_scope": {"mode": "include", "narrowed": True, "notebook_ids": ["lib-a"]},
    }}
    scope = routes._report_followup_scope(_Repo(), "nb", "rep-1", row)
    assert scope.fell_back_to_all is True
    assert (scope.source_scope.mode, scope.source_scope.source_ids) == ("exclude", [])
    assert (scope.base_scope.mode, scope.base_scope.notebook_ids) == ("exclude", [])
    assert (scope.dropped_sources, scope.dropped_libraries) == (1, 0)
    # The same selection, once a request can carry it, is restored as it was.
    monkeypatch.setattr(routes, "_SCOPE_IDS_MAX", 10)
    scope = routes._report_followup_scope(_Repo(), "nb", "rep-1", row)
    assert scope.fell_back_to_all is False
    assert scope.source_scope.source_ids == ["a", "b", "c"]
    assert scope.base_scope.notebook_ids == ["lib-a"]


# --- the receipt reader of the conversation share count ----------------------


def test_only_a_read_receipt_counts_its_memory_toward_a_share():
    read = {"linked_report": {"status": "read", "memory_used": ["m1", "m2", "m1"]}}
    assert linked_report_memory_ids(read) == {"m1", "m2"}
    # A turn that did not read the report put none of it into the prompt.
    skipped = {"linked_report": {"status": "skipped", "memory_used": ["m1"]}}
    assert linked_report_memory_ids(skipped) == frozenset()


@pytest.mark.parametrize("payload", [
    None, "text", [], {}, {"linked_report": None}, {"linked_report": "read"},
    {"linked_report": {"status": "read"}},
    {"linked_report": {"status": "read", "memory_used": "m1"}},
    {"linked_report": {"status": "read", "memory_used": [None, 3, "", {"id": "m1"}]}},
])
def test_a_stored_receipt_that_is_not_in_shape_counts_nothing(payload):
    assert linked_report_memory_ids(payload) == frozenset()
