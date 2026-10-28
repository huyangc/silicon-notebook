"""PostgreSQL twin of ``tests/test_source_finder.py``: the same cases over a
``PostgresRepository`` (the canonical store SQL, the PostgreSQL chunk lanes and
the same permission cases)."""
from __future__ import annotations

import pytest

from app.core.request_context import reset_request_user, set_request_user
from app.models.notebooks import NotebookCreate
from app.services.embedding import FakeEmbedder
from tests import test_source_finder as cases
from tests.model_testkit import bind_all_embedding_clients

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_source_finder"),
]


@pytest.fixture
def repo(postgres_settings, monkeypatch):
    from app.services.postgres_repository import PostgresRepository

    postgres_settings.embed_dim = 16
    instance = PostgresRepository(postgres_settings)
    bind_all_embedding_clients(instance, FakeEmbedder(dim=16))
    monkeypatch.setattr(instance.settings, "find_sources_strong_relevance", cases.STRONG)
    monkeypatch.setattr(instance.settings, "find_sources_related_relevance", cases.RELATED)
    alice = instance._runtime.identity.create_user("a00123456", "password-12")
    bob = instance._runtime.identity.create_user("b00654321", "password-12")
    token = set_request_user(alice)
    try:
        nb = instance.create_notebook(NotebookCreate(name="papers")).id
        instance._runtime.sharing.add_member(nb, bob.id)
        yield instance, nb, alice.id, bob.id
    finally:
        reset_request_user(token)
        instance.close()


test_metadata_tiers_follow_the_field_and_word_boundary_rule = (
    cases.test_metadata_tiers_follow_the_field_and_word_boundary_rule)
test_semantic_tiers_use_the_configured_lines = (
    cases.test_semantic_tiers_use_the_configured_lines)
test_the_lexical_only_lane_needs_two_passages_and_ignores_the_lines = (
    cases.test_the_lexical_only_lane_needs_two_passages_and_ignores_the_lines)
test_the_order_is_total_and_deterministic = cases.test_the_order_is_total_and_deterministic
test_a_pure_filter_lists_every_source_strong_by_year = (
    cases.test_a_pure_filter_lists_every_source_strong_by_year)
test_the_cursor_pages_and_dies_with_any_parameter_change = (
    cases.test_the_cursor_pages_and_dies_with_any_parameter_change)
test_counts_and_coverage_flags = cases.test_counts_and_coverage_flags
test_the_filters_decide_the_semantic_allow_list_first = (
    cases.test_the_filters_decide_the_semantic_allow_list_first)
test_card_status_is_read_in_one_batch_per_library = (
    cases.test_card_status_is_read_in_one_batch_per_library)
test_the_run_ceiling_is_the_source_set = cases.test_the_run_ceiling_is_the_source_set
test_a_pushed_down_read_is_verified_and_cut_to_the_plan = (
    cases.test_a_pushed_down_read_is_verified_and_cut_to_the_plan)
test_without_a_run_scope_another_members_memory_never_appears = (
    cases.test_without_a_run_scope_another_members_memory_never_appears)
test_every_real_lane_groups_by_source = cases.test_every_real_lane_groups_by_source
