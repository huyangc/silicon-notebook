# backend/tests/test_knowhow_reformat.py
"""knowhow-md-normalize Task 3: reformat_cell orchestration (LLM reformat ->
zero-LLM content-invariance check -> rule_normalize fallback).

Fake rewrite client returns a JSON **string** (``json.dumps(...)``), not a
raw dict: the real contract (``app/core/llm.py``'s
``OpenAICompatibleClient.chat_json`` / ``JsonChatClientPort`` in
``app/repositories/ports.py``) is ``chat_json(messages, response_schema_hint,
**kwargs) -> str`` — a fence-stripped JSON string the caller itself
``json.loads``s (see ``optimize_cell``'s own ``data = json.loads(raw)``, and
this exact stub-return convention already used by
test_knowhow_optimize.py::_FakeRewriteClient). A stub returning a bare dict
would never exercise the real parsing path reformat_cell/llm_reformat has to
do.
"""
from __future__ import annotations

import json
import types
import pytest

from fastapi.testclient import TestClient

from app.services.knowhow import api as kh_api
from tests.model_testkit import bind_chat_client
from app.services.model_work import ModelQueueFull

RAW = "A. 考量\n\t• 增大 R： 变慢\n\t• 增大 C： 变化"


def _repo_with_llm(reply):
    """构造一个最小 repo stub，其 rewrite client 返回给定内容的 JSON 字符串。"""
    payload = json.dumps({"reformatted_md": reply}, ensure_ascii=False)
    client = types.SimpleNamespace(chat_json=lambda *a, **k: payload)
    models = types.SimpleNamespace(
        chat=lambda workload_id: (
            client if workload_id == "knowhow_reformat" else None
        )
    )
    runtime = types.SimpleNamespace(models=models)
    return types.SimpleNamespace(_runtime=runtime)


def _repo_no_llm():
    client = types.SimpleNamespace(configured=False)
    models = types.SimpleNamespace(
        chat=lambda workload_id: (
            client if workload_id == "knowhow_reformat" else None
        )
    )
    return types.SimpleNamespace(_runtime=types.SimpleNamespace(models=models))


def test_llm_pass_uses_llm_candidate():
    good = "**A. 考量**\n\n- 增大 R:变慢\n- 增大 C:变化"   # 只改格式 → 过校验
    out = kh_api.reformat_cell(_repo_with_llm(good), RAW, "修复方法", "procedure")
    assert out["source"] == "llm"
    assert out["candidate_md"] == good
    assert out["changed"] is True


def test_llm_changed_content_falls_back_to_rule():
    bad = "**A. 考量**\n\n- 增大 R:变快"   # 删了内容 → 校验不过
    out = kh_api.reformat_cell(_repo_with_llm(bad), RAW, "修复方法", "procedure")
    assert out["source"] == "rule/llm-failed"
    assert "•" not in out["candidate_md"] and "\t" not in out["candidate_md"]


def test_no_llm_uses_rule():
    out = kh_api.reformat_cell(_repo_no_llm(), RAW, "修复方法", "procedure")
    assert out["source"] == "rule/no-llm"
    assert "**A. 考量**" in out["candidate_md"].split("\n")


def test_empty_cell_no_change():
    out = kh_api.reformat_cell(_repo_no_llm(), "", "修复方法", "procedure")
    assert out["changed"] is False


def _repo_with_client(client):
    models = types.SimpleNamespace(
        chat=lambda workload_id: (
            client if workload_id == "knowhow_reformat" else None
        )
    )
    return types.SimpleNamespace(_runtime=types.SimpleNamespace(models=models))


def test_unconfigured_client_labeled_no_llm():
    """生产环境 rewrite_llm_client 从不是 None——未配置时 model_provider.py 的
    _llm_for_role 要么返回一个 .configured=False 的哨兵，要么回退到一个同样
    .configured=False 的系统 client（见 model_provider.py 106-137/app/core/llm.py
    的 OpenAICompatibleClient.configured）。reformat_cell 必须靠 .configured
    前置判定识别这两种未配置形状，而不是只判 client is None；未配置就不该真的
    去调一次注定失败的 chat_json。"""
    def _boom(*a, **k):
        raise AssertionError("chat_json 不应在 client 未配置时被调用")

    client = types.SimpleNamespace(configured=False, chat_json=_boom)
    out = kh_api.reformat_cell(_repo_with_client(client), RAW, "修复方法", "procedure")
    assert out["source"] == "rule/no-llm"


def test_client_raising_not_configured_labeled_no_llm():
    """哨兵 client（model_provider.py::_UnconfiguredLLMClient）的真实形状是
    configured=False 且 chat_json 抛 ModelNotConfiguredError；这里额外覆盖
    configured=True 但 chat_json 仍抛 ModelNotConfiguredError 的情形（例如未来
    某个未配置形状没有正确暴露 .configured），确认 reformat_cell 靠捕获
    ModelNotConfiguredError 兜底，仍标 rule/no-llm 而不是 rule/llm-failed。"""
    from app.services.model_work import ModelNotConfiguredError

    def _raise_not_configured(*a, **k):
        raise ModelNotConfiguredError("尚未配置模型")

    client = types.SimpleNamespace(configured=True, chat_json=_raise_not_configured)
    out = kh_api.reformat_cell(_repo_with_client(client), RAW, "修复方法", "procedure")
    assert out["source"] == "rule/no-llm"


def test_scheduler_admission_failure_falls_back_to_rules():
    client = types.SimpleNamespace(
        configured=True,
        chat_json=lambda *args, **kwargs: (_ for _ in ()).throw(
            ModelQueueFull(support_id="mdl-reformat-full")
        ),
    )

    out = kh_api.reformat_cell(
        _repo_with_client(client), RAW, "修复方法", "procedure"
    )

    assert out["source"] == "rule/llm-failed"
    assert "•" not in out["candidate_md"]


# ---------------------------------------------------------------------------
# F2 — the reformat prompt must stop requesting a transform the invariant now
# rejects. content_signature treats ordered-marker DIGITS as content (the
# anti-renumbering fix), so a model that COMPLIES with a "renumber steps into
# 1. 2. 3." instruction ALWAYS fails content_invariant -> wasted LLM call,
# misleading rule/llm-failed, silent fallback. The procedure clause must
# instead instruct the model to KEEP each line's existing marker/number
# verbatim and only tidy layout -- exactly the transforms the invariant
# accepts (bullet glyphs are format and may normalize •->-; ordered digits are
# content and must stay verbatim).
# ---------------------------------------------------------------------------


def test_reformat_procedure_prompt_keeps_markers_not_renumber():
    prompt = kh_api._reformat_cell_prompt("x", "方法", "procedure")
    # must NOT ask the model to renumber into an ordered 1. 2. 3. list (the old
    # clause did exactly this -- content_invariant rejects the renumbered digits).
    assert "整理成有序列表" not in prompt
    assert "1. 2. 3." not in prompt
    # must instruct keeping each line's existing marker/number verbatim.
    assert "原样" in prompt
    assert "不要重新编号" in prompt


def test_reformat_procedure_clause_only_for_procedure_kind():
    proc = kh_api._reformat_cell_prompt("x", "方法", "procedure")
    assert "保持每一行已有的列表标记" in proc
    for kind in ("attribute", "entity", "symptom", ""):
        other = kh_api._reformat_cell_prompt("x", "现象", kind)
        assert "保持每一行已有的列表标记" not in other


def test_llm_keeping_existing_numbers_passes_invariant():
    # A model doing EXACTLY what the new prompt asks -- •->-, bold the col-0 A.
    # section header, re-indent, and KEEP the ordered digits 2018/2019 verbatim,
    # only tidying blank lines -- must pass content_invariant end-to-end and be
    # taken as the "llm" candidate (not fall back to rule).
    raw = "A. 记录\n\t• 项一\n2018. 甲\n2019. 乙"
    good = "**A. 记录**\n\n- 项一\n\n2018. 甲\n2019. 乙"
    out = kh_api.reformat_cell(_repo_with_llm(good), raw, "方法", "procedure")
    assert out["source"] == "llm"
    assert out["candidate_md"] == good
    assert out["changed"] is True


def test_llm_renumbering_fails_invariant_regression_lock():
    # Regression lock: renumbering ordered digits (2018/2019 -> 1/2) changes
    # content per content_signature, so even if a model does it anyway, the
    # invariant must reject it and reformat_cell must fall back to rule -- never
    # mislabel a renumbered result as a clean "llm" reformat.
    raw = "2018. 甲\n2019. 乙"
    renumbered = "1. 甲\n2. 乙"
    out = kh_api.reformat_cell(_repo_with_llm(renumbered), raw, "方法", "procedure")
    assert out["source"] == "rule/llm-failed"


# ===========================================================================
# Group B: HTTP-level endpoint tests (Task 4 — POST .../reformat). Mirrors
# test_knowhow_optimize.py's own Group B: TestClient + register/login +
# create-table-wizard conventions, and the fake rewrite client is installed
# onto the APP's own live repository singleton (app.api.deps.repository())
# rather than a separately constructed SQLiteRepository, for the same reason
# documented there (a second instance sharing only the on-disk DB file would
# never be seen by the in-process client the route handlers actually
# resolve).
#
# The one deliberate divergence from optimize's endpoint tests: reformat_cell
# is graceful about an unconfigured LLM (never raises ModelNotConfiguredError
# -- see api.py's reformat_cell docstring), so there is no "unconfigured ->
# 400" test here the way test_knowhow_optimize.py has one. Instead,
# test_reformat_endpoint_unconfigured_still_returns_200_rule_no_llm below
# proves the opposite: unconfigured still 200s, just labelled rule/no-llm.
# ===========================================================================

class _FakeReformatClient:
    """Fake rewrite LLM client for the reformat endpoint: chat_json returns a
    JSON **string** shaped {"reformatted_md": ...} (reformat's own schema key
    -- distinct from optimize's {"suggestion_md": ...}), mirroring this
    file's own _repo_with_llm helper above but as a real client object so it
    can be installed onto the app's live repo singleton the same way
    test_knowhow_optimize.py's _FakeRewriteClient is."""

    configured = True
    model = "fake-reformat-model"

    def __init__(self, reformatted: str):
        self.reformatted = reformatted
        self.calls: list[tuple[list[dict], str, dict]] = []

    def chat_json(self, messages, schema_hint, **kwargs):
        self.calls.append((messages, schema_hint, kwargs))
        return json.dumps({"reformatted_md": self.reformatted}, ensure_ascii=False)


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


def _reformat(client, headers, nb, table_id, row_id, column_id):
    return client.post(
        f"/api/notebooks/{nb}/knowhow/{table_id}/rows/{row_id}/cells/{column_id}/reformat",
        headers=headers,
    )


def _app_repo():
    """The SAME live repository singleton the app's route handlers resolve
    via Depends(repository) — see test_knowhow_optimize.py's identical
    helper for why a separately constructed SQLiteRepository would not work
    here."""
    from app.api.deps import repository
    return repository()


def _seed(client, headers_username="a00002090"):
    r"""Owner + table (procedure column at index 1) + one row whose content
    carries Excel-flavored markup (tab-indented bullet) — the shape the task
    brief calls for: content containing '\t• x'. Returns (owner_headers, nb,
    table, row, procedure_column_id)."""
    owner_h = _login(client, headers_username)
    nb = _mk_notebook(client, owner_h)
    table = _create_table(client, owner_h, nb)
    procedure_column_id = table["columns"][1]["id"]
    row = _add_row(client, owner_h, nb, table["id"], {procedure_column_id: "先做A\n\t• 增大 R"})
    return owner_h, nb, table, row, procedure_column_id


def test_reformat_endpoint_returns_candidate_with_bullet_normalized(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, column_id = _seed(client, "a00002090")

    resp = _reformat(client, owner_h, nb, table["id"], row["id"], column_id)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "•" not in body["candidate_md"]
    assert body["source"] in ("llm", "rule/llm-failed", "rule/no-llm")
    assert isinstance(body["changed"], bool)


def test_reformat_endpoint_llm_success_returns_llm_candidate(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, column_id = _seed(client, "a00002091")

    # Only formatting changed (bullet -> "- ", tab-indent -> blank-line
    # separation) -- passes content_invariant's per-line signature check, so
    # reformat_cell accepts it as the "llm" candidate rather than falling
    # back to rule_normalize.
    fake = _FakeReformatClient("先做A\n\n- 增大 R")
    bind_chat_client(_app_repo(), "knowhow_reformat", fake)

    resp = _reformat(client, owner_h, nb, table["id"], row["id"], column_id)

    assert resp.status_code == 200, resp.text
    # source_md (concurrency P1 fix a) round-trips the EXACT saved content_md the
    # server read + fed to reformat_cell (the seeded cell, unchanged by this
    # suggestion-only call) so the batch client can tell a snapshot-derived
    # candidate from one derived from a cell edited under it.
    assert resp.json() == {
        "candidate_md": "先做A\n\n- 增大 R",
        "source": "llm",
        "changed": True,
        "source_md": "先做A\n\t• 增大 R",
    }
    assert len(fake.calls) == 1
    prompt = fake.calls[0][0][0]["content"]
    assert "先做A\n\t• 增大 R" in prompt
    assert "方法" in prompt  # column name is woven into the instruction

    # suggestion-only: the saved cell content is untouched.
    detail = client.get(f"/api/notebooks/{nb}/knowhow/{table['id']}", headers=owner_h).json()
    updated_row = next(r for r in detail["rows"] if r["id"] == row["id"])
    assert updated_row["cells"][column_id] == "先做A\n\t• 增大 R"


def test_reformat_endpoint_unconfigured_still_returns_200_rule_no_llm(tmp_path, monkeypatch):
    """The key divergence from /optimize: reformat_cell handles an
    unconfigured LLM internally (falls back to rule_normalize), so this
    endpoint must NOT map it to 400 the way optimize_knowhow_cell does --
    contrast with test_knowhow_optimize.py::test_optimize_endpoint_unconfigured_returns_400."""
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, column_id = _seed(client, "a00002092")

    bind_chat_client(_app_repo(), "knowhow_reformat", types.SimpleNamespace(configured=False))

    resp = _reformat(client, owner_h, nb, table["id"], row["id"], column_id)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "rule/no-llm"
    assert "•" not in body["candidate_md"]


def test_reformat_endpoint_does_not_write_or_schedule(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, column_id = _seed(client, "a00002093")

    scheduled: list[str] = []

    class _CountingScheduler:
        def schedule(self, table_id):
            scheduled.append(table_id)

    monkeypatch.setattr(kh_api, "get_scheduler", lambda repo: _CountingScheduler())

    resp = _reformat(client, owner_h, nb, table["id"], row["id"], column_id)

    assert resp.status_code == 200
    assert scheduled == []
    detail = client.get(f"/api/notebooks/{nb}/knowhow/{table['id']}", headers=owner_h).json()
    updated_row = next(r for r in detail["rows"] if r["id"] == row["id"])
    assert updated_row["cells"][column_id] == "先做A\n\t• 增大 R"


def test_reformat_endpoint_reader_gets_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, column_id = _seed(client, "a00002094")

    bob_h = _login(client, "b00002094")
    bob_id = client.get("/api/me", headers=bob_h).json()["id"]
    _app_repo().add_member(nb, bob_id)

    resp = _reformat(client, bob_h, nb, table["id"], row["id"], column_id)

    assert resp.status_code == 404
    # confirms it's the write-guard (require_notebook_access), not a blanket
    # notebook-access problem: the SAME reader can still read the table.
    assert client.get(f"/api/notebooks/{nb}/knowhow/{table['id']}", headers=bob_h).status_code == 200


def test_reformat_endpoint_unknown_table_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00002095")
    nb = _mk_notebook(client, owner_h)

    resp = _reformat(client, owner_h, nb, "no-such-table", "no-such-row", "no-such-col")

    assert resp.status_code == 404


def test_reformat_endpoint_bad_row_or_column_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, column_id = _seed(client, "a00002096")

    assert _reformat(client, owner_h, nb, table["id"], "no-such-row", column_id).status_code == 400
    assert _reformat(client, owner_h, nb, table["id"], row["id"], "no-such-col").status_code == 400
