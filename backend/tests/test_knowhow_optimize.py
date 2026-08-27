# backend/tests/test_knowhow_optimize.py
"""knowhow-tables PR-2+3 Task 8: LLM cell rewrite (design doc §③, explicit
trigger only, suggestion-only) — the feature's ONLY new LLM call.

Two layers:
  - Group A exercises ``knowhow_api.optimize_cell`` directly against a tiny
    fake ``repo._runtime.models``-shaped object (no HTTP, no DB) — this is
    where prompt construction (cell text/column name/procedure clause/asset
    preservation instruction) and the three failure paths (empty cell,
    unconfigured, LLM-call failure) are pinned down precisely, mirroring how
    test_knowhow_projection.py's
    test_embedding_failure_emits_through_model_error_channel spies on
    note_model_error with a plain recording lambda.
  - Group B drives the real HTTP endpoint (mirrors test_knowhow_editing_api.py/
    test_knowhow_template.py's TestClient + register/login + create-table-
    wizard conventions) to prove routing/permission/no-write/no-schedule
    behavior. The fake rewrite LLM client is injected onto the APP's own live
    repository singleton (``app.api.deps.repository()``) rather than a
    separately constructed ``SQLiteRepository`` — a second instance sharing
    only the on-disk DB file would never be seen by the in-process client the
    dependency-injected route handlers actually resolve, since the fake
    client lives in memory, not in the database.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.services.knowhow import api as knowhow_api
from app.services.model_work import ModelNotConfiguredError
from tests.model_testkit import bind_chat_client
from app.services.model_work import (
    ModelQueueFull, ModelQueueTimeout, ModelServiceUnavailable,
)


# ===========================================================================
# Group A: optimize_cell unit tests (no HTTP, no DB)
# ===========================================================================


class _FakeRewriteClient:
    """Fake rewrite LLM client: records every chat_json call and returns a
    canned {"suggestion_md": ...} JSON reply, mirroring
    test_followup_retrieval_grounding.py's RecordingLLM."""

    configured = True
    model = "fake-rewrite-model"

    def __init__(self, suggestion: str = "规整后的正文"):
        self.suggestion = suggestion
        self.calls: list[tuple[list[dict], str, dict]] = []

    def chat_json(self, messages, schema_hint, **kwargs):
        self.calls.append((messages, schema_hint, kwargs))
        return json.dumps({"suggestion_md": self.suggestion})


class _RaisingRewriteClient:
    """Fake rewrite LLM client whose chat_json always raises — simulates a
    network/timeout/upstream failure after the client WAS reached (as
    opposed to simply being unconfigured)."""

    configured = True
    model = "fake-rewrite-model"

    def chat_json(self, messages, schema_hint, **kwargs):
        raise RuntimeError("upstream 503")


def _fake_repo(client):
    """Minimal repo._runtime.models-shaped stand-in — optimize_cell only ever
    reaches repo._runtime.models.{chat,note_model_error}, the
    same narrow-runtime-port pattern build_projector uses (see api.py's own
    docstring). Returns (repo, error_calls) where error_calls records every
    note_model_error(stage, model, exc) invocation as (stage, model,
    type(exc).__name__) tuples — the exact idiom
    test_knowhow_projection.py::test_embedding_failure_emits_through_model_error_channel
    uses for its own projector.note_model_error spy."""
    error_calls: list[tuple[str, str, str]] = []
    def chat(workload_id):
        assert workload_id == "knowhow_optimize"
        return client

    models = SimpleNamespace(
        chat=chat,
        note_model_error=lambda stage, error, *, workload_id: error_calls.append(
            (stage, workload_id, type(error).__name__)
        ),
    )
    repo = SimpleNamespace(_runtime=SimpleNamespace(models=models))
    return repo, error_calls


def test_prompt_contains_cell_text_and_column_name():
    client = _FakeRewriteClient()
    repo, _calls = _fake_repo(client)

    knowhow_api.optimize_cell(repo, "现象：过冲振铃严重", "现象", "attribute")

    assert len(client.calls) == 1
    messages, schema_hint, _kwargs = client.calls[0]
    prompt = messages[0]["content"]
    assert "现象：过冲振铃严重" in prompt
    assert "现象" in prompt  # column name is woven into the instruction
    assert schema_hint == '{"suggestion_md": ""}'


def test_prompt_includes_procedure_clause_iff_kind_is_procedure():
    procedure_client = _FakeRewriteClient()
    repo, _ = _fake_repo(procedure_client)
    knowhow_api.optimize_cell(repo, "先做 A，再做 B", "方法", "procedure")
    procedure_prompt = procedure_client.calls[0][0][0]["content"]
    assert "方法步骤" in procedure_prompt
    assert "有序 markdown 列表" in procedure_prompt

    for other_kind in ("attribute", "entity", "anchor"):
        client = _FakeRewriteClient()
        repo, _ = _fake_repo(client)
        knowhow_api.optimize_cell(repo, "普通文本内容", "备注", other_kind)
        prompt = client.calls[0][0][0]["content"]
        assert "方法步骤" not in prompt
        assert "有序 markdown 列表" not in prompt


def test_prompt_preserves_asset_reference_and_states_the_constraint():
    client = _FakeRewriteClient()
    repo, _ = _fake_repo(client)
    content = "见下图 ![说明](asset://abc123.png) 所示，注意间距。"

    knowhow_api.optimize_cell(repo, content, "现象", "attribute")

    prompt = client.calls[0][0][0]["content"]
    # The cell's own asset:// reference is carried into the prompt verbatim...
    assert "asset://abc123.png" in prompt
    # ...and the instruction explicitly tells the model to keep it as-is.
    assert "asset://" in prompt.replace(content, "", 1)
    assert "图片引用" in prompt and "原样保留" in prompt


def test_success_returns_suggestion():
    client = _FakeRewriteClient(suggestion="规整后的正文：先 A 后 B。")
    repo, _ = _fake_repo(client)

    result = knowhow_api.optimize_cell(repo, "原文：先做A再做B", "现象", "attribute")

    assert result == "规整后的正文：先 A 后 B。"


def test_unconfigured_raises_model_not_configured_error_with_exact_message():
    client = SimpleNamespace(configured=False)
    repo, calls = _fake_repo(client)

    with pytest.raises(ModelNotConfiguredError) as exc_info:
        knowhow_api.optimize_cell(repo, "原文", "现象", "attribute")

    assert str(exc_info.value) == "尚未配置模型，无法优化表达"
    assert calls == []  # not a call failure — never reached the LLM at all


def test_empty_cell_raises_value_error_with_exact_message():
    client = _FakeRewriteClient()
    repo, _ = _fake_repo(client)

    with pytest.raises(ValueError) as exc_info:
        knowhow_api.optimize_cell(repo, "   \n  ", "现象", "attribute")

    assert str(exc_info.value) == "格子为空，无需优化"
    assert client.calls == []  # fails fast, before ever resolving the LLM call


def test_raising_client_wraps_as_unavailable_and_notes_model_error():
    repo, calls = _fake_repo(_RaisingRewriteClient())

    with pytest.raises(knowhow_api.KnowhowOptimizeUnavailable):
        knowhow_api.optimize_cell(repo, "原文", "现象", "attribute")

    assert len(calls) == 1
    stage, workload_id, exc_type = calls[0]
    assert stage == "knowhow_optimize"
    assert workload_id == "knowhow_optimize"
    assert exc_type == "RuntimeError"


@pytest.mark.parametrize(
    "error",
    [
        ModelQueueFull(support_id="mdl-kh-full"),
        ModelQueueTimeout(support_id="mdl-kh-timeout"),
        ModelServiceUnavailable(support_id="mdl-kh-unavailable"),
    ],
)
def test_scheduler_admission_failure_keeps_optimize_502_semantics(error):
    class _BusyClient:
        configured = True

        def chat_json(self, *args, **kwargs):
            raise error

    repo, calls = _fake_repo(_BusyClient())

    with pytest.raises(knowhow_api.KnowhowOptimizeUnavailable):
        knowhow_api.optimize_cell(repo, "原文", "现象", "attribute")

    assert calls == [
        ("knowhow_optimize", "knowhow_optimize", type(error).__name__)
    ]


def test_llm_empty_reply_also_wraps_as_unavailable_and_notes_model_error():
    # A well-formed-but-empty suggestion is itself treated as a failure — an
    # LLM that silently returns nothing useful is exactly as unhelpful as one
    # that raises, so it gets the same note_model_error + 502-shaped outcome
    # rather than silently handing back an empty suggestion.
    repo, calls = _fake_repo(_FakeRewriteClient(suggestion="   "))

    with pytest.raises(knowhow_api.KnowhowOptimizeUnavailable):
        knowhow_api.optimize_cell(repo, "原文", "现象", "attribute")

    assert len(calls) == 1
    assert calls[0][0] == "knowhow_optimize"


def test_model_not_configured_error_from_chat_json_itself_still_maps_to_400_shape():
    # Defensive path (design doc / task brief: "ModelNotConfiguredError same"
    # as the .configured pre-check) — if chat_json raises ModelNotConfiguredError
    # directly for any reason, it must propagate unwrapped (NOT get folded into
    # KnowhowOptimizeUnavailable/502 by the generic except Exception branch).
    class _ClientThatRaisesConfigError:
        configured = True  # passes the pre-check...

        def chat_json(self, *args, **kwargs):
            raise ModelNotConfiguredError("尚未配置模型，无法优化表达")

    repo, calls = _fake_repo(_ClientThatRaisesConfigError())

    with pytest.raises(ModelNotConfiguredError):
        knowhow_api.optimize_cell(repo, "原文", "现象", "attribute")

    assert calls == []  # not logged as a generic model_error — it's a 400, not a 502


# ===========================================================================
# Group B: HTTP-level endpoint tests
# ===========================================================================


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import app
    return TestClient(app)


def _login(client, username, password="pw123456"):
    client.post("/api/auth/register", json={"username": username, "password": password})
    tok = client.post("/api/auth/login", json={"username": username, "password": password}).json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def _mk_notebook(client, headers, name="N"):
    return client.post("/api/notebooks", json={"name": name}, headers=headers).json()["id"]


DEFAULT_COLUMNS = (("现象", "attribute"), ("方法", "procedure"), ("工具", "entity"))


def _create_table(client, headers, nb, *, title="判别表", columns=DEFAULT_COLUMNS, anchor_index=0):
    body: dict = {"title": title, "columns": [{"name": n, "kind": k} for n, k in columns]}
    if anchor_index is not None:
        body["anchor_index"] = anchor_index
    resp = client.post(f"/api/notebooks/{nb}/knowhow", headers=headers, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _add_row(client, headers, nb, table_id, cells):
    resp = client.post(
        f"/api/notebooks/{nb}/knowhow/{table_id}/rows", headers=headers, json={"cells": cells},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _optimize(client, headers, nb, table_id, row_id, column_id):
    return client.post(
        f"/api/notebooks/{nb}/knowhow/{table_id}/rows/{row_id}/cells/{column_id}/optimize",
        headers=headers,
    )


def _app_repo():
    """The SAME live repository singleton the app's route handlers resolve
    via Depends(repository) — not a separately constructed SQLiteRepository,
    which would share only the on-disk DB file, not in-memory client
    overrides. Tests set attributes on this directly (mirrors
    test_followup_retrieval_grounding.py's `repo2._rewrite_llm_client = fast`,
    just reached through the app's own singleton accessor)."""
    from app.api.deps import repository
    return repository()


def _seed(client, headers_username="a00001090"):
    """Owner + table (procedure column at index 1) + one row with content in
    that column. Returns (nb, table, row, procedure_column_id)."""
    owner_h = _login(client, headers_username)
    nb = _mk_notebook(client, owner_h)
    table = _create_table(client, owner_h, nb)
    procedure_column_id = table["columns"][1]["id"]
    row = _add_row(client, owner_h, nb, table["id"], {procedure_column_id: "先做A，再做B"})
    return owner_h, nb, table, row, procedure_column_id


def test_optimize_endpoint_success_returns_suggestion_and_writes_nothing(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, column_id = _seed(client, "a00001090")

    fake = _FakeRewriteClient(suggestion="1. 先做 A\n2. 再做 B")
    bind_chat_client(_app_repo(), "knowhow_optimize", fake)

    resp = _optimize(client, owner_h, nb, table["id"], row["id"], column_id)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"suggestion_md": "1. 先做 A\n2. 再做 B"}
    assert len(fake.calls) == 1
    prompt = fake.calls[0][0][0]["content"]
    assert "先做A，再做B" in prompt
    assert "方法步骤" in prompt  # column kind == procedure

    # optimize never writes the cell itself — content must be unchanged.
    detail = client.get(f"/api/notebooks/{nb}/knowhow/{table['id']}", headers=owner_h).json()
    updated_row = next(r for r in detail["rows"] if r["id"] == row["id"])
    assert updated_row["cells"][column_id] == "先做A，再做B"


def test_optimize_stream_uses_shared_task_envelope(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, column_id = _seed(client, "a00001097")
    bind_chat_client(
        _app_repo(),
        "knowhow_optimize",
        _FakeRewriteClient(suggestion="整理后的正文"),
    )

    response = client.post(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/rows/{row['id']}"
        f"/cells/{column_id}/optimize/stream",
        headers=owner_h,
    )

    assert response.status_code == 200
    events = [
        json.loads(line) for line in response.text.splitlines() if line.strip()
    ]
    assert events[0]["event"] == "started"
    assert events[-1] == {
        "event": "final",
        "stage": "knowhow_optimize",
        "result": {"suggestion_md": "整理后的正文"},
    }


def test_optimize_stream_preserves_preflight_http_errors(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, column_id = _seed(client, "a00001100")
    bind_chat_client(
        _app_repo(), "knowhow_optimize", SimpleNamespace(configured=False)
    )
    base = (
        f"/api/notebooks/{nb}/knowhow/{table['id']}/rows/{row['id']}"
        "/cells"
    )

    unconfigured = client.post(
        f"{base}/{column_id}/optimize/stream", headers=owner_h
    )
    bad_column = client.post(
        f"{base}/no-such-column/optimize/stream", headers=owner_h
    )
    missing_table = client.post(
        f"/api/notebooks/{nb}/knowhow/no-such-table/rows/{row['id']}"
        f"/cells/{column_id}/optimize/stream",
        headers=owner_h,
    )

    assert unconfigured.status_code == 400
    assert unconfigured.json()["detail"] == "尚未配置模型，无法优化表达"
    assert bad_column.status_code == 400
    assert bad_column.json()["detail"] == "格子定位不合法"
    assert missing_table.status_code == 404


def test_complete_stream_validates_targets_before_starting_stream(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, _column_id = _seed(client, "a00001101")

    response = client.post(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/rows/{row['id']}"
        "/complete/stream",
        headers=owner_h,
        json={"target_column_ids": ["no-such-column"]},
    )

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"] == "无法补全当前行，请检查目标列和已知内容"


def test_optimize_endpoint_unconfigured_returns_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, column_id = _seed(client, "a00001091")

    bind_chat_client(_app_repo(), "knowhow_optimize", SimpleNamespace(configured=False))

    resp = _optimize(client, owner_h, nb, table["id"], row["id"], column_id)

    assert resp.status_code == 400
    assert resp.json()["detail"] == "尚未配置模型，无法优化表达"


def test_optimize_endpoint_raising_client_returns_502(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, column_id = _seed(client, "a00001092")

    bind_chat_client(_app_repo(), "knowhow_optimize", _RaisingRewriteClient())

    resp = _optimize(client, owner_h, nb, table["id"], row["id"], column_id)

    assert resp.status_code == 502
    # cell content still untouched by the failed attempt.
    detail = client.get(f"/api/notebooks/{nb}/knowhow/{table['id']}", headers=owner_h).json()
    updated_row = next(r for r in detail["rows"] if r["id"] == row["id"])
    assert updated_row["cells"][column_id] == "先做A，再做B"


def test_optimize_endpoint_empty_cell_returns_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001093")
    nb = _mk_notebook(client, owner_h)
    table = _create_table(client, owner_h, nb)
    column_id = table["columns"][1]["id"]
    row = _add_row(client, owner_h, nb, table["id"], {})  # no cells at all

    fake = _FakeRewriteClient()
    bind_chat_client(_app_repo(), "knowhow_optimize", fake)

    resp = _optimize(client, owner_h, nb, table["id"], row["id"], column_id)

    assert resp.status_code == 400
    assert resp.json()["detail"] == "格子为空，无需优化"
    assert fake.calls == []


def test_optimize_endpoint_reader_gets_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, column_id = _seed(client, "a00001094")

    bob_h = _login(client, "b00001094")
    bob_id = client.get("/api/me", headers=bob_h).json()["id"]
    _app_repo().add_member(nb, bob_id)

    resp = _optimize(client, bob_h, nb, table["id"], row["id"], column_id)

    assert resp.status_code == 404
    # confirms it's the write-guard (require_notebook_access), not a blanket
    # notebook-access problem: the SAME reader can still read the table.
    assert client.get(f"/api/notebooks/{nb}/knowhow/{table['id']}", headers=bob_h).status_code == 200


def test_optimize_endpoint_stranger_gets_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, column_id = _seed(client, "a00001095")

    stranger_h = _login(client, "c00001095")
    resp = _optimize(client, stranger_h, nb, table["id"], row["id"], column_id)

    assert resp.status_code == 404


def test_optimize_endpoint_unknown_table_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001096")
    nb = _mk_notebook(client, owner_h)

    resp = _optimize(client, owner_h, nb, "no-such-table", "no-such-row", "no-such-col")

    assert resp.status_code == 404


def test_optimize_endpoint_bad_row_or_column_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, column_id = _seed(client, "a00001097")

    bind_chat_client(_app_repo(), "knowhow_optimize", _FakeRewriteClient())

    assert _optimize(client, owner_h, nb, table["id"], "no-such-row", column_id).status_code == 400
    assert _optimize(client, owner_h, nb, table["id"], row["id"], "no-such-col").status_code == 400


def test_optimize_endpoint_does_not_schedule_reprojection(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, column_id = _seed(client, "a00001098")

    bind_chat_client(_app_repo(), "knowhow_optimize", _FakeRewriteClient())

    scheduled: list[str] = []

    class _CountingScheduler:
        def schedule(self, table_id):
            scheduled.append(table_id)

    monkeypatch.setattr(knowhow_api, "get_scheduler", lambda repo: _CountingScheduler())

    resp = _optimize(client, owner_h, nb, table["id"], row["id"], column_id)

    assert resp.status_code == 200
    assert scheduled == []
