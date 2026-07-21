import json
from pathlib import Path

from app.main import app
from tests.application_boundary_snapshot import snapshot


FIXTURE = Path(__file__).parent / "fixtures" / "application_boundary_contract.json"


def test_application_contract_matches_pre_split_baseline():
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert snapshot(app) == expected
