"""E4-7 (plan §2 D2): the Python viewer rule and the ``memory_sql`` fragments
decide the same thing on one fixture set -- own Memory, another member's
Memory, orphan and lost Memory, Knowhow, a plain source and mixed evidence.
PostgreSQL twin: tests/postgres/test_kg_viewer_scope_twins_pg.py; the cases
live in tests/kg_viewer_twin_cases.py."""
from __future__ import annotations

import pytest

from app.repositories.sqlite import memory_sql
from tests.kg_viewer_twin_cases import (
    as_user,
    assert_twins_agree,
    expected_hidden,
    seed_twin_world,
)
from tests.test_kg_viewer_scope import _memory, _now, _source, repo  # noqa: F401


def _world(repo):
    return seed_twin_world(repo, source=_source, memory=_memory, ph="?", cast="",
                           now=_now)


@pytest.mark.parametrize("channel_open", [True, False])
def test_python_rule_and_sql_fragments_agree(repo, monkeypatch, channel_open):
    world = _world(repo)
    hidden = assert_twins_agree(repo, world, memory_sql, "?",
                                channel_open=channel_open, monkeypatch=monkeypatch)
    for name in ("a", "b"):
        assert hidden[name] == expected_hidden(name, channel_open=channel_open), name


def test_mixed_evidence_object_stays_and_loses_only_the_foreign_item(repo):
    """The object owned by the plain source that cites only B's Memory: A
    reads it (the retired evidence rule 404'd it) and never sees B's item;
    the one citing both keeps only the plain item."""
    world = _world(repo)
    ctx = as_user(world["a"], repo.node_context, world["nb"], "ko-mix-plain-mb")
    assert ctx["id"] == "ko-mix-plain-mb"
    assert "src-mb" not in repr(ctx)
    both = as_user(world["a"], repo.node_context, world["nb"], "ko-mix-plain-both")
    assert [o["source_id"] for o in both["occurrences"]] == ["src-plain"]
    with pytest.raises(KeyError):
        as_user(world["a"], repo.node_context, world["nb"], "ko-mix-mb-plain")
    own = as_user(world["b"], repo.node_context, world["nb"], "ko-mix-mb-plain")
    assert own["id"] == "ko-mix-mb-plain"
