import pytest

from tests.collab_api_cases import CASES
from tests.test_notebook_capability_matrix import client as matrix_client

client = matrix_client


@pytest.mark.parametrize("case", sorted(CASES))
def test_collaboration_api(client, monkeypatch, case):
    CASES[case](client, False, monkeypatch)
