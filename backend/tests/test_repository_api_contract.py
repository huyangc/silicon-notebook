from __future__ import annotations

import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import main as app_main
from app.api import ask_routes, deps
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
    if key in {"answered_at", "created_at", "updated_at"} and isinstance(value, str):
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


def _runtime_serialization(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("AUTH_OPTIONAL", "true")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "")
    settings = Settings(
        _env_file=None,
        auth_optional=True,
        event_log_enabled=False,
        llm_log_enabled=False,
        viz_sync_build_max_objects=1,
    )
    repo = SQLiteRepository(settings)

    def fixture_repository():
        return repo

    fixture_repository.cache_info = lambda: SimpleNamespace(currsize=1)
    fixture_repository.cache_clear = lambda: None

    monkeypatch.setattr(app_main, "get_settings", lambda: settings)
    monkeypatch.setattr(app_main, "repository", fixture_repository)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "repository", fixture_repository)
    monkeypatch.setattr(ask_routes, "repository", fixture_repository)
    monkeypatch.setattr(event_logging._archive_pool, "submit", lambda *_a, **_k: None)
    test_app = app_main.create_app()

    with TestClient(test_app) as client:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if client.get("/api/ready").json()["ready"]:
                break
            time.sleep(0.01)
        assert client.get("/api/ready").json()["ready"] is True
        created = client.post(
            "/api/notebooks",
            json={
                "name": "API fixture",
                "purpose": "Freeze serialization",
                "primary_domain": "Analog IC",
            },
        )
        assert created.status_code == 200, created.text
        notebook_id = created.json()["id"]

        source = repo.upload_sources(
            notebook_id,
            [
                UploadedSourceFile(
                    file_name="fixture.md",
                    content_type="text/markdown",
                    content=b"# Gain\n\nSource degeneration stabilizes fixture gain.",
                )
            ],
            scheduler=lambda _source_id: None,
        )[0]
        repo.process_source(source.id)
        element = repo.source_elements(source.id)[0]
        evidence = [
            {
                "source_id": source.id,
                "source_title": source.title,
                "element_id": element.id,
                "element_type": element.element_type,
                "location_label": element.location_label,
                "quoted_span": element.text,
                "confidence": 0.98,
            }
        ]
        repo.store_kg(
            notebook_id,
            source.id,
            [
                {
                    "local_id": "concept",
                    "object_type": "concept",
                    "payload": {
                        "name": "source degeneration",
                        "definition": "Local feedback",
                    },
                    "evidence": evidence,
                },
                {
                    "local_id": "claim",
                    "object_type": "claim",
                    "payload": {
                        "name": "Source degeneration stabilizes gain",
                        "statement": "Source degeneration stabilizes gain",
                    },
                    "evidence": evidence,
                },
            ],
            [
                {
                    "source_local_id": "claim",
                    "target_local_id": "concept",
                    "edge_type": "about",
                    "evidence": evidence,
                }
            ],
        )

        ask_response = client.post(
            f"/api/notebooks/{notebook_id}/ask",
            json={"question": "fixture gain", "mode": "chunk"},
        )
        assert ask_response.status_code == 200, ask_response.text
        ask_payload = ask_response.json()
        job_payload = AskRequest(
            question="trace fixture gain",
            mode="reasoning",
            conversation_id=ask_payload["conversation_id"],
        )
        job_id, conversation_id = repo.begin_ask_job(
            notebook_id, job_payload, "reasoning", threading.Event()
        )
        repo.append_ask_trace(
            job_id,
            {
                "step_type": "retrieve",
                "summary": "Retrieved one item",
                "detail": {"found": 1},
                "duration_ms": 2,
            },
        )
        repo.finish_ask_job(job_id, "done", answer_id=ask_payload["answer_id"])

        report_id = repo.create_report(notebook_id, "Explain fixture gain", depth=2)
        repo.update_report(
            notebook_id,
            report_id,
            status="outline_ready",
            progress="outline ready",
            outline=[{"title": "Evidence", "goal": "Explain feedback"}],
            references=[{"source_id": source.id, "title": source.title}],
            section_status=[{"title": "Evidence", "phase": "排队", "step": 0}],
        )

        share_response = client.post(f"/api/notebooks/{notebook_id}/share")
        assert share_response.status_code == 200, share_response.text
        share = share_response.json()

        def json_response(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            assert response.status_code == 200, response.text
            return response.json()

        serialization = {
            "notebook_summary": json_response(
                "GET", f"/api/notebooks/{notebook_id}"
            ),
            "source_detail": json_response("GET", f"/api/sources/{source.id}"),
            "knowledge_page": json_response(
                "GET", f"/api/notebooks/{notebook_id}/knowledge?type=claim"
            ),
            "ask_response": ask_payload,
            "ask_job_detail": json_response(
                "GET", f"/api/notebooks/{notebook_id}/ask/jobs/{job_id}"
            ),
            "conversation_detail": json_response(
                "GET", f"/api/conversations/{conversation_id}"
            ),
            "report": json_response(
                "GET", f"/api/notebooks/{notebook_id}/reports/{report_id}"
            ),
            "sharing": {
                "share": share,
                "preview": json_response("GET", f"/api/shared/{share['share_token']}"),
                "shared_by_me": next(
                    item
                    for item in json_response("GET", "/api/notebooks/shared-by-me")
                    if item["id"] == notebook_id
                ),
            },
        }
        error_responses = {
            "http_404": client.get("/api/notebooks/nb-does-not-exist"),
            "validation_422": client.post(
                f"/api/notebooks/{notebook_id}/ask", json={}
            ),
            "graph_too_large_413": client.get(
                f"/api/notebooks/{notebook_id}/graph"
            ),
        }
        serialization["errors"] = {
            name: {"status_code": response.status_code, "body": response.json()}
            for name, response in error_responses.items()
        }

    with repo._connect() as db:
        object_ids = [
            row["id"]
            for row in db.execute(
                "SELECT id FROM knowledge_objects WHERE notebook_id=? ORDER BY object_type,id",
                (notebook_id,),
            ).fetchall()
        ]
    replacements = {
        notebook_id: "nb-api",
        source.id: "src-api",
        element.id: "el-api",
        ask_payload["answer_id"]: "ans-api",
        conversation_id: "conv-api",
        job_id: "askjob-api",
        report_id: "rep-api",
        share["share_token"]: "shr-api",
        **{object_id: f"ko-api-{index}" for index, object_id in enumerate(object_ids, 1)},
    }
    return _normalize_runtime(serialization, replacements, tmp_path)


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


def test_serialization_contract_replays_real_repository_and_endpoints(
    tmp_path, monkeypatch
):
    assert _runtime_serialization(tmp_path, monkeypatch) == _contract()["serialization"]
