"""Assembly tripwires of E4-7's seams (permission remediation 2026-09-29).

E4-7 reads through three changes that land beside it: E1's Memory channel
(``source_scope.memory_access_context`` / ``memory_channel_allowed``), E4-4's
store keyword ``viewer_id`` and E4-5's isolation marker
(``MemoryIsolationStore.not_isolated``).  Each seam defaults to today's
behaviour, so forgetting to connect one at assembly would fail OPEN without
a word.  Each test below activates by itself as soon as the change it waits
for is in the tree, and goes red until the seam is connected; before that it
pins the honest default.  PostgreSQL: tests/postgres/test_kg_service_readers_pg.py.
"""
from __future__ import annotations

import importlib
import inspect

import pytest

from app.services import kg_viewer_scope, source_scope
from tests.test_kg_viewer_scope import repo  # noqa: F401  (``repo`` is a fixture)

#: Every store reader ``store_viewer_kwargs`` / the catalog's
#: ``viewer_count_kwargs`` feed, as ``(runtime seat, method)``.
SEAM_READERS = (
    ("knowledge", "list_knowledge_page", "viewer_id"),
    ("knowledge", "type_counts", "viewer_id"),
    ("knowledge", "fts_search", "viewer_id"),
    ("knowledge", "neighbor_relation_rows", "viewer_id"),
    ("queries", "knowledge_type_count_rows", "viewer_id"),
    ("queries", "notebook_has_kg", "viewer_id"),
    ("queries", "notebook_analytics", "viewer_id"),
    ("queries", "search_notebook", "viewer_id"),
    # The list page's element read asks for sources only (E4-4, P3-B).
    ("knowledge", "_enrich_evidence", "sources_only"),
)


def assert_store_seam_matches_the_stores(repo) -> None:
    """``STORE_READERS_TAKE_VIEWER_ID`` is on exactly when every reader it
    feeds takes its keyword: on while a store does not take it is a
    TypeError in production; off while they all do leaves another member's
    Memory in the list, the counts and the search legs."""
    takes = {
        f"{seat}.{name}({keyword})": keyword in inspect.signature(
            getattr(getattr(repo._runtime, seat), name)).parameters
        for seat, name, keyword in SEAM_READERS
    }
    assert len(set(takes.values())) == 1, (
        "the stores take viewer_id only in part", takes)
    assert kg_viewer_scope.STORE_READERS_TAKE_VIEWER_ID is set(takes.values()).pop(), (
        "assembly: set kg_viewer_scope.STORE_READERS_TAKE_VIEWER_ID to match", takes)


def assert_isolation_marker_is_wired(repo, backend: str) -> None:
    """Once E4-5's ``MemoryIsolationStore`` exists for ``backend``, the
    runtime's lifecycle reads its marker through ``not_isolated`` (a None
    reader serves pre-isolation artifacts as isolated); before E4-5 the
    reader is None -- every notebook is isolated, today's behaviour."""
    try:
        module = importlib.import_module(
            f"app.repositories.{backend}.memory_isolation_store")
    except ModuleNotFoundError:
        assert repo._runtime.knowledge_lifecycle._isolation_pending is None
        return
    pending = repo._runtime.knowledge_lifecycle._isolation_pending
    assert pending is not None, (
        "assembly: pass memory_isolation_pending=MemoryIsolationStore.not_isolated "
        "to KnowledgeLifecycleService")
    assert pending is module.MemoryIsolationStore.not_isolated


def test_a_closed_memory_access_context_reads_as_the_empty_identity():
    """F2 / P2-3: once E1's context manager exists, closing the channel
    through it -- not through a patched wrapper -- makes every Memory
    foreign to the viewer, their own included."""
    if not hasattr(source_scope, "memory_access_context"):
        # Before E1: no switch at all; the channel is open (today).
        assert kg_viewer_scope._e1_memory_channel_allowed is None
        assert kg_viewer_scope.memory_channel_allowed() is True
        assert kg_viewer_scope.viewer_identity("u") == "u"
        return
    assert (kg_viewer_scope._e1_memory_channel_allowed
            is source_scope.memory_channel_allowed)
    assert kg_viewer_scope.viewer_identity("u") == "u"
    with source_scope.memory_access_context(False):
        assert kg_viewer_scope.memory_channel_allowed() is False
        assert kg_viewer_scope.viewer_identity("u") == ""
    assert kg_viewer_scope.viewer_identity("u") == "u"


def test_the_viewer_identity_follows_the_e1_switch(monkeypatch):
    """The wrapper every E4-7 read consults answers what E1's switch
    answers (M7: a wrapper that ignored it would read every token as
    holding ``memory:read``)."""
    for allowed, identity in ((False, ""), (True, "u")):
        monkeypatch.setattr(kg_viewer_scope, "_e1_memory_channel_allowed",
                            lambda allowed=allowed: allowed)
        assert kg_viewer_scope.memory_channel_allowed() is allowed
        assert kg_viewer_scope.viewer_identity("u") == identity


def test_the_channel_import_is_hard_once_e1_is_in_the_tree(monkeypatch):
    """P2-3: with either E1 name present the switch is imported by name --
    a missing one fails the module import instead of reading as open."""
    monkeypatch.setattr(source_scope, "memory_access_context", object(), raising=False)
    monkeypatch.delattr(source_scope, "memory_channel_allowed", raising=False)
    with pytest.raises(ImportError):
        importlib.reload(kg_viewer_scope)
    monkeypatch.undo()
    importlib.reload(kg_viewer_scope)


def test_the_store_seam_matches_what_the_stores_take(repo):
    assert_store_seam_matches_the_stores(repo)


def test_the_isolation_marker_reader_is_wired(repo):
    assert_isolation_marker_is_wired(repo, "sqlite")
