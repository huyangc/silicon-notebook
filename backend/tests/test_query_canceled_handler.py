"""T-W4-4: ``psycopg.errors.QueryCanceled`` reaching the top of the request
stack must become a structured 503 (``code=query_timeout``) plus one
``query_timeout`` event — never a bare, unobservable 500. Also pins that the
existing savepoint-bounded probe (knowledge_store.py's chunk lexical-recall
budget) is untouched: it catches its own ``QueryCanceled`` before this
handler ever sees it, so it is covered by its own existing test instead of
duplicated here (see ``tests/postgres/test_knowledge_store_conformance.py``'s
chunk-FTS-timeout coverage)."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient
from psycopg.errors import QueryCanceled


def _client(tmp_path: Path, monkeypatch, *, log_dir: Path) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'qc.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "true")
    monkeypatch.setenv("EVENT_LOG_DIR", str(log_dir))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")

    from app.api import deps
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    deps.repository.cache_clear()
    return TestClient(create_app())


def _register(client: TestClient, username: str) -> dict[str, str]:
    r = client.post("/api/auth/register", json={"username": username, "password": "pw"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _notebook(client: TestClient, headers: dict[str, str], name: str) -> str:
    r = client.post("/api/notebooks", headers=headers, json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _request_events(log_dir: Path) -> list[dict]:
    today = datetime.now().strftime("%Y-%m-%d")
    path = log_dir / f"requests-{today}.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _query_timeout_events(log_dir: Path) -> list[dict]:
    return [
        row for row in _request_events(log_dir)
        if row.get("kind") == "query_timeout"
    ]


def test_query_canceled_becomes_structured_503_with_notebook_dimension(
    tmp_path, monkeypatch
):
    log_dir = tmp_path / "logs"
    client = _client(tmp_path, monkeypatch, log_dir=log_dir)
    headers = _register(client, "a11111111")
    notebook_id = _notebook(client, headers, "qc-notebook")

    from app.api import deps

    catalog = deps.repository()._runtime.catalog

    def _raise(*_args, **_kwargs):
        raise QueryCanceled("canceling statement due to statement timeout")

    monkeypatch.setattr(catalog, "get_notebook", _raise)

    resp = client.get(f"/api/notebooks/{notebook_id}", headers=headers)

    assert resp.status_code == 503
    body = resp.json()
    assert body["code"] == "query_timeout"

    events = _query_timeout_events(log_dir)
    assert len(events) == 1, events
    assert events[0]["method"] == "GET"
    assert events[0]["path"] == f"/api/notebooks/{notebook_id}"
    assert events[0]["notebook_id"] == notebook_id

    # Execution-order pin (T4 质量评 P3-5): the handler runs INSIDE
    # log_requests, so the paired ``kind=http`` row records the handled 503
    # (not a raw error), shares the ``query_timeout`` row's request id, and
    # the response still carries X-Request-Id.
    http_rows = [
        row for row in _request_events(log_dir)
        if row.get("kind") == "http"
        and row.get("path") == f"/api/notebooks/{notebook_id}"
    ]
    assert len(http_rows) == 1, http_rows
    assert http_rows[0]["status_code"] == 503
    assert http_rows[0]["id"] == events[0]["id"]
    assert resp.headers["X-Request-Id"] == events[0]["id"]


def test_query_canceled_without_notebook_path_param_omits_dimension(
    tmp_path, monkeypatch
):
    log_dir = tmp_path / "logs"
    client = _client(tmp_path, monkeypatch, log_dir=log_dir)
    headers = _register(client, "a22222222")

    from app.api import deps

    catalog = deps.repository()._runtime.catalog

    def _raise(*_args, **_kwargs):
        raise QueryCanceled("canceling statement due to statement timeout")

    monkeypatch.setattr(catalog, "list_notebooks", _raise)

    resp = client.get("/api/notebooks", headers=headers)

    assert resp.status_code == 503
    assert resp.json()["code"] == "query_timeout"

    events = _query_timeout_events(log_dir)
    assert len(events) == 1, events
    assert events[0]["method"] == "GET"
    assert events[0]["path"] == "/api/notebooks"
    # "拿不到不硬凑": no {notebook_id} path param on this route, so the
    # dimension must be OMITTED, never a fabricated empty string.
    assert "notebook_id" not in events[0]


def test_query_canceled_mid_stream_still_emits_the_query_timeout_event(
    tmp_path, monkeypatch
):
    """Streaming leg (T4 质量评 P2-1): once the response has started, the
    exception handler is structurally unreachable (Starlette re-raises as
    RuntimeError with the original QueryCanceled as __cause__) — the
    ``query_timeout`` accounting must come from log_requests instead."""
    log_dir = tmp_path / "logs"
    client = _client(tmp_path, monkeypatch, log_dir=log_dir)
    headers = _register(client, "a33333333")

    from app.api import deps

    repo = deps.repository()

    def _raise(*_args, **_kwargs):
        raise QueryCanceled("canceling statement due to statement timeout")

    monkeypatch.setattr(repo, "pending_actions", _raise)

    import pytest

    with pytest.raises(RuntimeError) as excinfo:
        client.get("/api/me/pending-actions/stream", headers=headers)
    assert isinstance(excinfo.value.__cause__, QueryCanceled)

    events = _query_timeout_events(log_dir)
    assert len(events) == 1, events
    assert events[0]["streaming"] is True
    assert events[0]["path"] == "/api/me/pending-actions/stream"
    # The paired http row (same request id) recorded the already-sent 200
    # start frame — log_requests emits when call_next returns, BEFORE the
    # body generator fails, and the exception then bypasses its except
    # branch entirely (BaseHTTPMiddleware re-raises at body-consumption
    # time). The query_timeout row is the honest signal for this leg.
    http_rows = [
        row for row in _request_events(log_dir)
        if row.get("kind") == "http" and row.get("id") == events[0]["id"]
    ]
    assert len(http_rows) == 1, http_rows
    assert http_rows[0]["status_code"] == 200
