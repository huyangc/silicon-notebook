"""The SQLite shared concept-rank contract against PostgreSQL."""
import pytest

from tests import concept_rank_store_cases as cases
from tests.postgres.test_kg_findings_store_pg import postgres_repository as postgres_repository

pytestmark = pytest.mark.postgres_integration


@pytest.mark.parametrize("check", [
    cases.check_counts, cases.check_names_citations, cases.check_publication, cases.check_deletion,
])
def test_concept_rank_contract(postgres_repository, check):
    check(postgres_repository, postgres=True)
