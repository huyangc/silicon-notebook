from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import app
    return TestClient(app)


def _login(client, username, password="pw123456"):
    client.post("/api/auth/register", json={"username": username, "password": password})
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _setup(tmp_path, monkeypatch):
    """建一个 owner + notebook + 两列一行的表，返回常用句柄。"""
    client = _client(tmp_path, monkeypatch)
    owner = _login(client, "a00002080")
    nb = client.post("/api/notebooks", json={"name": "N"}, headers=owner).json()["id"]
    table = client.post(
        f"/api/notebooks/{nb}/knowhow",
        headers=owner,
        json={
            "title": "表",
            "columns": [{"name": "概念", "kind": "attribute"}, {"name": "做法", "kind": "attribute"}],
            "anchor_index": 0,
        },
    ).json()
    row = client.post(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/rows", headers=owner, json={"cells": {}}
    ).json()
    return {
        "client": client, "owner": owner, "nb": nb, "table": table,
        "row_id": row["id"], "plain": table["columns"][1]["id"],
    }


def test_history_timeline_is_readable_by_a_read_only_member(tmp_path, monkeypatch, repo):
    ctx = _setup(tmp_path, monkeypatch)
    bob = _login(ctx["client"], "b00002080")
    bob_id = ctx["client"].get("/api/me", headers=bob).json()["id"]
    repo.add_member(ctx["nb"], bob_id)

    response = ctx["client"].get(
        f"/api/notebooks/{ctx['nb']}/knowhow/{ctx['table']['id']}/history", headers=bob
    )

    assert response.status_code == 200
    seqs = [c["seq"] for c in response.json()["changes"]]
    assert seqs == sorted(seqs, reverse=True)


def test_revert_is_refused_for_a_read_only_member(tmp_path, monkeypatch, repo):
    ctx = _setup(tmp_path, monkeypatch)
    bob = _login(ctx["client"], "b00002081")
    bob_id = ctx["client"].get("/api/me", headers=bob).json()["id"]
    repo.add_member(ctx["nb"], bob_id)

    response = ctx["client"].post(
        f"/api/notebooks/{ctx['nb']}/knowhow/{ctx['table']['id']}/revert",
        json={"target_seq": 1, "expected_head_seq": 1}, headers=bob,
    )

    assert response.status_code == 404, "写守卫对非 owner 统一 404，不泄露存在性"


def _patch_cell(ctx, content, **extra):
    body = {"content_md": content, **extra}
    return ctx["client"].patch(
        f"/api/notebooks/{ctx['nb']}/knowhow/{ctx['table']['id']}"
        f"/rows/{ctx['row_id']}/cells/{ctx['plain']}",
        json=body, headers=ctx["owner"],
    )


def _head(ctx):
    return ctx["client"].get(
        f"/api/notebooks/{ctx['nb']}/knowhow/{ctx['table']['id']}/history",
        headers=ctx["owner"],
    ).json()["head_seq"]


def test_stale_head_returns_409_with_error_code(tmp_path, monkeypatch, repo):
    ctx = _setup(tmp_path, monkeypatch)
    _patch_cell(ctx, "第一版")
    good = _head(ctx)
    _patch_cell(ctx, "别人又改了")

    response = ctx["client"].post(
        f"/api/notebooks/{ctx['nb']}/knowhow/{ctx['table']['id']}/revert",
        json={"target_seq": good, "expected_head_seq": good}, headers=ctx["owner"],
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "knowhow_history_stale"
    assert "刷新" in response.json()["detail"]["message"]


def test_inconsistent_history_returns_400_with_error_code(tmp_path, monkeypatch, repo):
    ctx = _setup(tmp_path, monkeypatch)
    _patch_cell(ctx, "正常")
    good = _head(ctx)
    _patch_cell(ctx, "再改一次")
    head = _head(ctx)

    with repo._runtime.database.write() as db:  # 绕过 store：模拟漏挂钩的写路径
        db.execute(
            "UPDATE knowhow_cells SET content_md='偷偷改的' WHERE row_id=?",
            (ctx["row_id"],),
        )

    response = ctx["client"].post(
        f"/api/notebooks/{ctx['nb']}/knowhow/{ctx['table']['id']}/revert",
        json={"target_seq": good, "expected_head_seq": head}, headers=ctx["owner"],
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "knowhow_history_inconsistent"


def test_unknown_target_seq_returns_404(tmp_path, monkeypatch, repo):
    ctx = _setup(tmp_path, monkeypatch)

    response = ctx["client"].post(
        f"/api/notebooks/{ctx['nb']}/knowhow/{ctx['table']['id']}/revert",
        json={"target_seq": 9999, "expected_head_seq": _head(ctx)}, headers=ctx["owner"],
    )

    assert response.status_code == 404


def test_cell_patch_accepts_and_records_origin(tmp_path, monkeypatch, repo):
    ctx = _setup(tmp_path, monkeypatch)

    assert _patch_cell(ctx, "恢复来的内容", origin="revert").status_code == 200

    changes = ctx["client"].get(
        f"/api/notebooks/{ctx['nb']}/knowhow/{ctx['table']['id']}/history",
        headers=ctx["owner"],
    ).json()["changes"]
    assert changes[0]["origin"] == "revert"


def test_cell_patch_rejects_an_unknown_origin(tmp_path, monkeypatch, repo):
    ctx = _setup(tmp_path, monkeypatch)

    response = _patch_cell(ctx, "x", origin="伪造来源")

    assert response.status_code == 400, (
        "宽容默认会把 wire 错误降级成静默失败——anchor 特性正是这么整个失效的（PR#281→#286）"
    )
