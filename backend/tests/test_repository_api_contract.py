from __future__ import annotations

import json
from pathlib import Path

from app.main import app


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = (
    ROOT
    / "backend"
    / "tests"
    / "fixtures"
    / "repository_contract"
    / "api_contract.json"
)

REQUIRED_SERIALIZATION = {
    "notebook_summary",
    "source_detail",
    "knowledge_page",
    "ask_job_detail",
    "conversation_detail",
    "report",
    "sharing",
    "errors",
}


def _contract() -> dict[str, object]:
    assert FIXTURE.is_file(), f"missing API contract fixture: {FIXTURE}"
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_api_contract_fixture_records_openapi_and_serialization():
    contract = _contract()

    assert contract["source_commit"] == "3334626"
    assert contract["openapi"]["paths"]
    assert contract["openapi"]["components"]["schemas"]
    assert set(contract["serialization"]) >= REQUIRED_SERIALIZATION


def test_openapi_contract_is_byte_semantically_frozen():
    assert app.openapi() == _contract()["openapi"]


def test_serialization_contract_preserves_defaults_required_fields_and_shapes():
    serialization = _contract()["serialization"]

    assert serialization["notebook_summary"]["tier"] == "personal"
    assert serialization["notebook_summary"]["access"] == "owner"
    assert serialization["source_detail"]["extraction_warning"] is None
    assert serialization["knowledge_page"]["offset"] == 0
    assert serialization["ask_job_detail"]["trace"]
    assert serialization["conversation_detail"]["turns"]
    assert serialization["report"]["outline"]
    assert set(serialization["sharing"]) >= {"share", "preview", "shared_by_me"}
    assert set(serialization["errors"]) >= {
        "http_404",
        "validation_422",
        "graph_too_large_413",
    }
