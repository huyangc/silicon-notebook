"""Memory hard delete, the member's own exit, and the Memory export on SQLite.

The scenarios live in ``tests/memory_purge_cases.py`` and run unchanged on
PostgreSQL through ``tests/postgres/test_memory_purge_pg.py``. This file adds
the composition and service-level contracts that need no second backend.
"""
from __future__ import annotations

import inspect

import pytest

from app.core.config import Settings
from app.services import memory_service as memory_service_module
from app.services.memory_service import MemoryService
from app.services.sqlite_repository import SQLiteRepository
from tests.memory_purge_cases import (
    CASES,
    EMBED_DIM,
    MONKEYPATCH_CASES,
    build_world,
    export_text,
    plain_memory,
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'memory-purge.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", str(EMBED_DIM))
    return SQLiteRepository(Settings())


@pytest.fixture
def world(repo):
    return build_world(repo, postgres=False)


@pytest.mark.parametrize("case", sorted(CASES))
def test_memory_purge_scenario(world, case):
    CASES[case](world)


@pytest.mark.parametrize("case", sorted(MONKEYPATCH_CASES))
def test_memory_purge_fault_scenario(world, monkeypatch, case):
    MONKEYPATCH_CASES[case](world, monkeypatch)


def test_the_self_exit_is_composed_with_its_membership_dependency(repo):
    runtime = repo._runtime
    assert runtime.memory_service.membership is runtime.sharing


def test_a_memory_service_without_the_membership_dependency_cannot_be_built():
    """The self-exit and its purge are one object: composing the service
    without what the exit needs fails at construction, not at the first
    exit."""
    parameter = inspect.signature(MemoryService).parameters["membership"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty
    with pytest.raises(TypeError, match="membership"):
        MemoryService(None, None, None, None, None, None, None)


def test_removing_or_kicking_members_has_no_memory_path():
    """The sharing service has no route to Memory at all any more: the old
    purge hook is gone, so no membership removal can delete Memory."""
    from app.services import notebook_sharing

    source = inspect.getsource(notebook_sharing.NotebookSharingService)
    assert "purge" not in source.replace("no Memory exit purge", "")
    assert not hasattr(notebook_sharing.NotebookSharingService, "bind_member_memory_purge")


def test_the_export_streams_in_bounded_pages(world, monkeypatch):
    """At most one page of Memories is held at a time, however many exist."""
    monkeypatch.setattr(memory_service_module, "_EXPORT_PAGE", 2)
    for number in range(4):
        plain_memory(world, world.shared, world.alice, f"page-{number}", "confirmed")
    store = world.repo._runtime.memory_service.store
    original = store.memory_export_page
    sizes: list[int] = []

    def recording(*args, **kwargs):
        items, cursor = original(*args, **kwargs)
        sizes.append(len(items))
        return items, cursor

    monkeypatch.setattr(store, "memory_export_page", recording)
    text = export_text(world, world.alice, world.shared)
    assert sizes == [2, 2, 1]
    assert text.count("\n## ") == 5
    assert text.index("Memory alice") < text.index("Plain page-0") < text.index(
        "Plain page-3"
    )


@pytest.mark.parametrize(
    "body",
    [
        "Starts a block:\n\n```python\nprint('never closed')",
        "Tilde block:\n\n~~~~\ncode\n~~~",  # a shorter fence does not close it
        "Fence with info:\n\n````md\n```\ninner\n```",
        "An open comment <!-- never closed",
        "A raw block:\n\n<script>\nlet never = 'closed';",
        "A raw block:\n\n<pre>\nnever closed",
        "A raw block:\n\n<style>\np { color: red }",
        "Fenced raw tag stays code:\n\n```\n<pre>\n```",
        # codex r1 #3: a fence or raw tag inside a comment block is comment
        # text; the comment is what must be closed.
        "<!--\n```\nhidden",
        "Intro\n\n<!-- note\n<pre>\nstill a comment",
        "<!--\n```\n-->\n\n```python\nopen after the comment",
    ],
)
def test_an_entry_cannot_swallow_the_next_one(world, body):
    """Quality review P3 / item 7: an unclosed code fence, raw HTML block
    (``<script>`` / ``<pre>`` / ``<style>``, CommonMark type 1) or HTML comment
    in one Memory's content must not turn every following entry into code.
    Rendered with CommonMark, the entry after it is still a heading."""
    from markdown_it import MarkdownIt

    world.repo._runtime.memory_service.create_candidate(
        world.alice_home, world.alice.id, None, "req-fence-a", "Fence a",
        body, [], "reason", {}, [],
    )
    plain_memory(world, world.alice_home, world.alice, "fence-b", "candidate")
    text = export_text(world, world.alice, world.alice_home)
    tokens = MarkdownIt("commonmark").parse(text)
    headings = [
        tokens[index + 1].content
        for index, token in enumerate(tokens)
        if token.type == "heading_open" and token.tag == "h2"
    ]
    assert any(heading.endswith("Plain fence-b") for heading in headings), headings
    assert text.rstrip().endswith("条记忆。")
