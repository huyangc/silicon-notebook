"""PostgreSQL twin of ``tests/test_generated_question_pushdown.py`` (#822
codex r1 P2): a late source's generated questions neither push the unbound
scan past its limit nor drop the supplement -- the scan is verified on read
and re-runs with the frozen list."""
from __future__ import annotations

import pytest

from app.extensions import default_extension_runtime
from tests.test_generated_question_pushdown import (
    assert_a_late_sources_questions_do_not_drop_the_supplement,
    build,
)

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_generated_question_pushdown"),
]


@pytest.fixture
def postgres_env(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

    repo = PostgresRepository(
        postgres_settings.model_copy(update={"embed_dim": 16}),
        retrieval_contributor_host=default_extension_runtime().retrieval_contributors,
    )
    try:
        yield build(repo, "%s")
    finally:
        repo.close()


def test_a_late_sources_questions_do_not_drop_the_supplement_on_postgres(postgres_env):
    assert_a_late_sources_questions_do_not_drop_the_supplement(postgres_env)
