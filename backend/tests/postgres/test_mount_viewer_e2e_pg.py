"""PostgreSQL twin of ``tests/test_mount_viewer_e2e.py`` (E6-3, M3 / N-6).

The whole application (``create_app()``) runs on PostgreSQL and the same
assertions go through the same real surfaces: a private library Alice mounts
on her shared notebook contributes nothing to Bob's asks, source / asset /
MCP reads or notebook summary until he may read it himself; the graph caches
separate and share by effective participant set (fed / PPR / scale, apart
from a Global Ask override's), one snapshot per run; reports and detached asks
read by their run's actor, Global Ask reads only the selected libraries, and a
path with neither actor nor request reads only public libraries.
"""
from __future__ import annotations

import pytest

from tests.test_mount_viewer_e2e import (
    assert_a_detached_ask_reads_by_its_actor,
    assert_a_global_ask_reads_the_selected_libraries_only,
    assert_a_join_summary_is_the_joiners,
    assert_a_report_reads_the_mount_by_its_author,
    assert_every_participant_reader_follows_the_run_actor,
    assert_graph_participants_are_one_snapshot_per_run,
    assert_graph_caches_follow_the_effective_set,
    assert_no_actor_no_request_reads_only_public_libraries,
    assert_override_and_viewer_graphs_stay_apart,
    assert_scale_graph_keys_follow_the_effective_set,
    assert_the_size_guard_judges_the_graphs_snapshot,
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


def test_a_global_ask_reads_the_selected_libraries_only_on_postgres(postgres_env):
    assert_a_global_ask_reads_the_selected_libraries_only(postgres_env)


def test_a_detached_ask_reads_by_its_actor_on_postgres(postgres_env):
    assert_a_detached_ask_reads_by_its_actor(postgres_env)


def test_no_actor_and_no_request_reads_only_public_libraries_on_postgres(
    postgres_env, monkeypatch,
):
    assert_no_actor_no_request_reads_only_public_libraries(postgres_env, monkeypatch)


def test_a_join_summary_is_the_joiners_on_postgres(postgres_env):
    assert_a_join_summary_is_the_joiners(postgres_env)


def test_every_participant_reader_follows_the_run_actor_on_postgres(postgres_env):
    assert_every_participant_reader_follows_the_run_actor(postgres_env)


def test_override_and_viewer_graphs_stay_apart_on_postgres(postgres_env, monkeypatch):
    assert_override_and_viewer_graphs_stay_apart(postgres_env, monkeypatch)


def test_scale_graph_keys_follow_the_effective_set_on_postgres(postgres_env, monkeypatch):
    assert_scale_graph_keys_follow_the_effective_set(postgres_env, monkeypatch)


def test_graph_participants_are_one_snapshot_per_run_on_postgres(
    postgres_env, monkeypatch,
):
    assert_graph_participants_are_one_snapshot_per_run(postgres_env, monkeypatch)


def test_the_size_guard_judges_the_graphs_snapshot_on_postgres(postgres_env, monkeypatch):
    assert_the_size_guard_judges_the_graphs_snapshot(postgres_env, monkeypatch)
