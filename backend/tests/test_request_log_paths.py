"""Request paths in logs carry no content (AGENTS.md: no source content or credentials
in logs). A few routes put content or a credential in a path segment -- the KG
analysis finding dismissal (F4's subject key is a source-derived concept name, F2's a
pair of source ids), invite / share tokens, custom object type names. Every place
that records a request path (uvicorn's access log, the ``requests`` event channel)
keeps the route shape and replaces those segments; the request itself is untouched.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.core.auth_logging import AuthenticationAccessFilter
from app.core.log_paths import loggable_path, loggable_target
from tests.clock_testkit import FROZEN_DAY, FrozenDatetime

CONCEPT = "tcp/ip 协议栈"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        # F4: a concept name, decoded (contains "/") and percent-encoded alike.
        (
            f"/api/notebooks/nb-1/kg-analysis/findings/F4/{CONCEPT}/dismissal",
            "/api/notebooks/nb-1/kg-analysis/findings/F4/{subject_key}/dismissal",
        ),
        (
            f"/api/notebooks/nb-1/kg-analysis/findings/F4/{quote(CONCEPT, safe='')}/dismissal",
            "/api/notebooks/nb-1/kg-analysis/findings/F4/{subject_key}/dismissal",
        ),
        # F2: two source ids joined by "|".
        (
            "/api/notebooks/nb-1/kg-analysis/findings/F2/src-b|src-c/dismissal",
            "/api/notebooks/nb-1/kg-analysis/findings/F2/{subject_key}/dismissal",
        ),
        # Behind a root path.
        (
            f"/silicon/api/notebooks/nb-1/kg-analysis/findings/F4/{CONCEPT}/dismissal",
            "/silicon/api/notebooks/nb-1/kg-analysis/findings/F4/{subject_key}/dismissal",
        ),
        ("/api/group-invites/inv-secret/join", "/api/group-invites/{token}/join"),
        ("/api/public/reports/rpt-secret", "/api/public/reports/{token}"),
        ("/api/public/conversations/cnv-secret", "/api/public/conversations/{token}"),
        (
            "/api/public/conversations/cnv-secret/assets/img-1",
            "/api/public/conversations/{token}/assets/{alias}",
        ),
        ("/api/object-schemas/芯片型号", "/api/object-schemas/{object_type}"),
        ("/api/notebooks/nb-1/object-schemas/芯片型号", "/api/notebooks/nb-1/object-schemas/{object_type}"),
        # Content-free routes stay as they are.
        ("/api/notebooks/nb-1/kg-analysis/findings", "/api/notebooks/nb-1/kg-analysis/findings"),
        ("/api/notebooks/nb-1/object-schemas", "/api/notebooks/nb-1/object-schemas"),
        ("/api/notebooks/nb-1/sources/src-1", "/api/notebooks/nb-1/sources/src-1"),
    ],
)
def test_loggable_path_templates_content_segments(path, expected):
    assert loggable_path(path) == expected


def test_loggable_target_keeps_the_query_and_templates_the_path():
    assert loggable_target("/api/group-invites/inv-secret/join?x=1") == "/api/group-invites/{token}/join?x=1"
    assert loggable_target("/api/notebooks?limit=20") == "/api/notebooks?limit=20"


def _access_record(target: str) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 1, '%s - "%s %s HTTP/%s" %d',
        ("client", "PUT", target, "1.1", 204), None,
    )


def test_access_log_omits_the_dismissal_subject_key():
    # uvicorn logs the percent-encoded target: the concept name must not appear in
    # either spelling.
    record = _access_record(
        f"/api/notebooks/nb-1/kg-analysis/findings/F4/{quote(CONCEPT)}/dismissal"
    )
    assert AuthenticationAccessFilter().filter(record)
    message = record.getMessage()
    assert "/api/notebooks/nb-1/kg-analysis/findings/F4/{subject_key}/dismissal" in message
    for form in (CONCEPT, quote(CONCEPT), quote(CONCEPT, safe=""), "tcp", "协议栈"):
        assert form not in message


def test_access_log_omits_invite_and_share_tokens():
    for target, secret in (
        ("/api/group-invites/inv-secret/join", "inv-secret"),
        ("/api/public/reports/rpt-secret", "rpt-secret"),
        ("/api/public/conversations/cnv-secret", "cnv-secret"),
    ):
        record = _access_record(target)
        assert AuthenticationAccessFilter().filter(record)
        assert secret not in record.getMessage()


def _client(tmp_path: Path, monkeypatch, *, log_dir: Path) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'paths.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "true")
    monkeypatch.setenv("EVENT_LOG_DIR", str(log_dir))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    import app.core.event_logging as event_logging_module

    monkeypatch.setattr(event_logging_module, "datetime", FrozenDatetime)

    from app.api import deps
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    deps.repository.cache_clear()
    return TestClient(create_app())


def test_request_event_log_omits_the_dismissal_subject_key(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    client = _client(tmp_path, monkeypatch, log_dir=log_dir)
    registered = client.post("/api/auth/register", json={"username": "a22222222", "password": "pw"})
    assert registered.status_code == 200, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['token']}"}
    created = client.post("/api/notebooks", headers=headers, json={"name": "paths"})
    assert created.status_code == 200, created.text
    notebook_id = created.json()["id"]

    for method in ("put", "delete"):
        response = getattr(client, method)(
            f"/api/notebooks/{notebook_id}/kg-analysis/findings/F4/{quote(CONCEPT, safe='')}/dismissal",
            headers=headers,
        )
        # The route was reached (no such finding -> 404, undismiss is idempotent -> 204):
        # the event is about this exact request.
        assert response.status_code in {204, 404}, response.text

    log_file = log_dir / f"requests-{FROZEN_DAY}.jsonl"
    text = log_file.read_text(encoding="utf-8")
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    dismissal_rows = [
        row for row in rows
        if row.get("kind") == "http" and str(row.get("path", "")).endswith("/dismissal")
    ]
    assert [row["method"] for row in dismissal_rows] == ["PUT", "DELETE"]
    assert {row["path"] for row in dismissal_rows} == {
        f"/api/notebooks/{notebook_id}/kg-analysis/findings/F4/{{subject_key}}/dismissal"
    }
    for form in (CONCEPT, quote(CONCEPT), quote(CONCEPT, safe=""), "协议栈"):
        assert form not in text
