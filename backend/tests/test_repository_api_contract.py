from __future__ import annotations

import json
from pathlib import Path
import threading

from fastapi.testclient import TestClient

from app import main as app_main
from app.api import deps, routes
from app.core import event_logging
from app.core.config import Settings
from app.main import app
from app.models.schemas import AskRequest
from app.services.repository import UploadedSourceFile
from app.services.sqlite_repository import SQLiteRepository


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


def _normalize_runtime(value, replacements, temporary_root, key=""):
    if key in {"created_at", "updated_at"} and isinstance(value, str):
        return "2024-01-02T03:04:05"
    if key == "created_label" and isinstance(value, str):
        return "fixture-date"
    if isinstance(value, dict):
        return {
            item_key: _normalize_runtime(
                item_value, replacements, temporary_root, item_key
            )
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [
            _normalize_runtime(item, replacements, temporary_root, key)
            for item in value
        ]
    if isinstance(value, str):
        normalized = value.replace(str(temporary_root), "<fixture-root>")
        for actual, stable in sorted(
            replacements.items(), key=lambda item: len(item[0]), reverse=True
        ):
            normalized = normalized.replace(actual, stable)
        return normalized
    return value


def test_api_contract_fixture_records_openapi_and_serialization():
    contract = _contract()

    assert contract["source_commit"] == "3334626"
    assert contract["openapi"]["paths"]
    assert contract["openapi"]["components"]["schemas"]
    assert set(contract["serialization"]) >= REQUIRED_SERIALIZATION


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


