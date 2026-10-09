"""PostgreSQL twin of ``tests/test_default_ceiling_entrypoints.py``.

The whole application (``create_app()``) runs on PostgreSQL and the same
assertions go through the same real surfaces: HTTP ``/ask`` without a scope,
``/ask/stream``, the intent precheck and MCP ``ask`` never hand the
asker another member's Memory-derived content or a mounted library's hidden
projections; a skipped mounted library is named in the answer; a plain
notebook answers the same with and without the ceiling; a failing ceiling
read fails the ask before any model call; a Stop during the freeze cancels it;
an unscoped report retrieves only what its creator may and a plain notebook's
report is byte-identical with and without the ceiling.
"""
from __future__ import annotations

import pytest

from tests.test_default_ceiling_entrypoints import (
    assert_a_plain_notebook_is_unchanged,
    assert_a_plain_notebook_report_is_unchanged,
    assert_a_reader_failure_fails_the_ask,
    assert_a_stop_during_the_freeze_cancels_the_ask,
    assert_an_unscoped_report_retrieves_only_what_its_creator_may,
    assert_a_skipped_library_is_named_in_the_answer,
    assert_every_entry_runs_under_the_default_ceiling,
    build_app,
    seed,
)


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_default_ceiling_entrypoints"),
]


@pytest.fixture
def postgres_env(postgres_scope, tmp_path, monkeypatch):
    from app.api.deps import repository

    env = build_app(postgres_scope.url, tmp_path, monkeypatch)
    env["seeded"] = seed(env, "%s")
    try:
        yield env
    finally:
        repository().close()
        repository.cache_clear()


@pytest.mark.anyio
async def test_every_ask_entry_runs_under_the_default_ceiling_on_postgres(
    postgres_env, monkeypatch,
):
    await assert_every_entry_runs_under_the_default_ceiling(postgres_env, monkeypatch)


@pytest.mark.anyio
async def test_a_skipped_mounted_library_is_named_in_the_answer_on_postgres(
    postgres_env, monkeypatch,
):
    await assert_a_skipped_library_is_named_in_the_answer(postgres_env, monkeypatch)


@pytest.mark.anyio
async def test_a_plain_notebook_answers_the_same_with_the_ceiling_on_postgres(
    postgres_env, monkeypatch,
):
    await assert_a_plain_notebook_is_unchanged(postgres_env, monkeypatch)


@pytest.mark.anyio
async def test_a_reader_failure_fails_the_ask_on_postgres(postgres_env, monkeypatch):
    await assert_a_reader_failure_fails_the_ask(postgres_env, monkeypatch)


def test_a_stop_during_the_freeze_cancels_the_ask_on_postgres(postgres_env, monkeypatch):
    assert_a_stop_during_the_freeze_cancels_the_ask(postgres_env, monkeypatch)


def test_an_unscoped_report_retrieves_only_what_its_creator_may_on_postgres(
    postgres_env, monkeypatch,
):
    assert_an_unscoped_report_retrieves_only_what_its_creator_may(postgres_env, monkeypatch)


def test_a_plain_notebook_report_is_unchanged_on_postgres(postgres_env, monkeypatch):
    assert_a_plain_notebook_report_is_unchanged(postgres_env, monkeypatch)
