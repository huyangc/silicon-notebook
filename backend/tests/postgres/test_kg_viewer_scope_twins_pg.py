"""PostgreSQL twin of tests/test_kg_viewer_scope_twins.py (E4-7, plan §2 D2):
the Python viewer rule and the PostgreSQL ``memory_sql`` fragments agree on
the shared fixture set (tests/kg_viewer_twin_cases.py)."""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.repositories.postgres import memory_sql
from tests.kg_viewer_twin_cases import (
    as_user,
    assert_twins_agree,
    expected_hidden,
    seed_twin_world,
)
from tests.postgres.test_kg_viewer_scope_pg import (  # noqa: F401  (``repo`` is a fixture)
    T0,
    _memory,
    _source,
    repo,
)


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_kg_viewer_scope"),
]


def _world(repo):
    return seed_twin_world(
        repo, source=_source, memory=_memory, ph="%s", cast="::jsonb",
        now=lambda index: T0 + timedelta(seconds=index))


@pytest.mark.parametrize("channel_open", [True, False])
def test_pg_python_rule_and_sql_fragments_agree(repo, monkeypatch, channel_open):
    world = _world(repo)
    hidden = assert_twins_agree(repo, world, memory_sql, "%s",
                                channel_open=channel_open, monkeypatch=monkeypatch)
    for name in ("a", "b"):
        assert hidden[name] == expected_hidden(name, channel_open=channel_open), name


def test_pg_mixed_evidence_object_stays_and_loses_only_the_foreign_item(repo):
    world = _world(repo)
    ctx = as_user(world["a"], repo.node_context, world["nb"], "ko-mix-plain-mb")
    assert ctx["id"] == "ko-mix-plain-mb"
    assert "src-mb" not in repr(ctx)
    both = as_user(world["a"], repo.node_context, world["nb"], "ko-mix-plain-both")
    assert [o["source_id"] for o in both["occurrences"]] == ["src-plain"]
    with pytest.raises(KeyError):
        as_user(world["a"], repo.node_context, world["nb"], "ko-mix-mb-plain")
