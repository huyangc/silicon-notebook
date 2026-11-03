import pytest

from tests.collab_api_cases import CASES
from tests.postgres.test_notebook_capability_matrix_pg import client as matrix_client

client = matrix_client

pytestmark = [pytest.mark.postgres_integration, pytest.mark.xdist_group(name="postgres_collaboration_api")]


@pytest.mark.parametrize("case", sorted(CASES))
def test_collaboration_api_pg(client, monkeypatch, case):
    CASES[case](client, True, monkeypatch)
