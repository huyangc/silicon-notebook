"""PostgreSQL twin of ``tests/test_mount_viewer_e2e.py`` (E6-3, M3 / N-6).

The whole application (``create_app()``) runs on PostgreSQL and the same
assertions go through the same real surfaces: a private library Alice mounts
on her shared notebook contributes nothing to Bob's asks, source / asset /
MCP reads or notebook summary until he may read it himself; the graph caches
separate and share by effective participant set; reports, Global Ask jobs and
detached asks read by their run's actor, and a path with neither actor nor
request reads only public libraries.
"""
from __future__ import annotations

import pytest

from tests.test_mount_viewer_e2e import (
    assert_a_detached_ask_reads_by_its_actor,
    assert_a_global_ask_reads_by_its_actor,
    assert_a_join_summary_is_the_joiners,
    assert_a_report_reads_the_mount_by_its_author,
    assert_graph_caches_follow_the_effective_set,
    assert_no_actor_no_request_reads_only_public_libraries,
    assert_the_mount_counts_only_for_its_readers,
    build_world,
)


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_mount_viewer_e2e"),
]


@pytest.fixture
def postgres_env(postgres_scope, tmp_path, monkeypatch):
    from app.api.deps import repository

    env = build_world(postgres_scope.url, tmp_path, monkeypatch, "%s")
    try:
        yield env
    finally:
        repository().close()
        repository.cache_clear()


@pytest.mark.anyio
async def test_a_private_mount_counts_only_for_its_readers_on_postgres(postgres_env):
    await assert_the_mount_counts_only_for_its_readers(postgres_env)


def test_graph_caches_follow_the_effective_set_on_postgres(postgres_env, monkeypatch):
    assert_graph_caches_follow_the_effective_set(postgres_env, monkeypatch)


def test_a_report_reads_the_mount_by_its_author_on_postgres(postgres_env, monkeypatch):
    assert_a_report_reads_the_mount_by_its_author(postgres_env, monkeypatch)


def test_a_global_ask_reads_by_its_actor_on_postgres(postgres_env):
    assert_a_global_ask_reads_by_its_actor(postgres_env)


def test_a_detached_ask_reads_by_its_actor_on_postgres(postgres_env):
    assert_a_detached_ask_reads_by_its_actor(postgres_env)


def test_no_actor_and_no_request_reads_only_public_libraries_on_postgres(
    postgres_env, monkeypatch,
):
    assert_no_actor_no_request_reads_only_public_libraries(postgres_env, monkeypatch)


def test_a_join_summary_is_the_joiners_on_postgres(postgres_env):
    assert_a_join_summary_is_the_joiners(postgres_env)
