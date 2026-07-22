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


# ===========================================================================
# knowhow 表版本管理 Task 13 code review: actor threading had ZERO test
# coverage anywhere above the store layer — the only actor assertion in the
# whole suite was test_knowhow_history_hooks.py's store-level
# test_update_cell_carries_actor_and_origin (calls the store directly with
# actor="user-1"). A mutation replacing every real `actor=user.id` in the
# HTTP routes with `actor=""` left all 4817 existing tests green. The two
# tests below drive the REAL HTTP path end to end and assert the recorded
# actor is the actually-logged-in user's own id — not empty, not "user-local"
# (the ambient ContextVar default a missing `set_request_user` would fall
# back to), not some other ambient value.
# ===========================================================================


def test_cell_patch_records_the_logged_in_users_id_as_actor(tmp_path, monkeypatch, repo):
    ctx = _setup(tmp_path, monkeypatch)
    owner_id = ctx["client"].get("/api/me", headers=ctx["owner"]).json()["id"]

    assert _patch_cell(ctx, "内容").status_code == 200

    changes = ctx["client"].get(
        f"/api/notebooks/{ctx['nb']}/knowhow/{ctx['table']['id']}/history",
        headers=ctx["owner"],
    ).json()["changes"]
    cell_update = next(c for c in changes if c["kind"] == "cell_update")
    assert cell_update["actor"] == owner_id
    assert cell_update["actor"] != ""
    assert cell_update["origin"] == "user"


def test_create_table_genesis_change_records_the_creating_users_id_as_actor(
    tmp_path, monkeypatch, repo,
):
    """create_knowhow_table's genesis ``table_create`` flow entry (seq==1,
    written by ``_setup`` itself) must carry the CREATING user's id as
    actor."""
    ctx = _setup(tmp_path, monkeypatch)
    owner_id = ctx["client"].get("/api/me", headers=ctx["owner"]).json()["id"]

    genesis = ctx["client"].get(
        f"/api/notebooks/{ctx['nb']}/knowhow/{ctx['table']['id']}/history/1",
        headers=ctx["owner"],
    ).json()
    assert genesis["kind"] == "table_create"
    assert genesis["actor"] == owner_id
    assert genesis["actor"] != ""


def test_cell_patch_rejects_an_unknown_origin(tmp_path, monkeypatch, repo):
    ctx = _setup(tmp_path, monkeypatch)

    response = _patch_cell(ctx, "x", origin="伪造来源")

    assert response.status_code == 400, (
        "宽容默认会把 wire 错误降级成静默失败——anchor 特性正是这么整个失效的（PR#281→#286）"
    )


# ===========================================================================
# PATCH .../cells（合并格批量写路径）的 origin 校验——这条路径是合并格扇写／
# LLM 规整批量确认的落库入口（见 knowhow_routes.py patch_knowhow_cells_batch
# 的文档字符串），校验逻辑本应与单格路径逐字同款，但此前没有任何测试盯着
# 它。评审实测：把这段校验整个禁用，全量 816 条测试仍然全绿——这正是 anchor
# 特性 PR#281→#286 那类"宽容默认把 wire 错误降级成静默失败"的同款隐患。
# ===========================================================================


def _add_row(ctx):
    return ctx["client"].post(
        f"/api/notebooks/{ctx['nb']}/knowhow/{ctx['table']['id']}/rows",
        headers=ctx["owner"], json={"cells": {}},
    ).json()["id"]


def _patch_cells_batch(ctx, row_ids, content, **extra):
    body = {"column_id": ctx["plain"], "row_ids": row_ids, "content_md": content, **extra}
    return ctx["client"].patch(
        f"/api/notebooks/{ctx['nb']}/knowhow/{ctx['table']['id']}/cells",
        json=body, headers=ctx["owner"],
    )


def test_cells_batch_patch_accepts_and_records_origin(tmp_path, monkeypatch, repo):
    ctx = _setup(tmp_path, monkeypatch)
    row2 = _add_row(ctx)

    response = _patch_cells_batch(
        ctx, [ctx["row_id"], row2], "批量规整后的内容", origin="llm_reformat"
    )

    assert response.status_code == 200, response.text

    changes = ctx["client"].get(
        f"/api/notebooks/{ctx['nb']}/knowhow/{ctx['table']['id']}/history",
        headers=ctx["owner"],
    ).json()["changes"]
    assert changes[0]["origin"] == "llm_reformat"


def test_cells_batch_patch_rejects_an_unknown_origin(tmp_path, monkeypatch, repo):
    ctx = _setup(tmp_path, monkeypatch)
    row2 = _add_row(ctx)

    response = _patch_cells_batch(ctx, [ctx["row_id"], row2], "x", origin="伪造来源")

    assert response.status_code == 400, (
        "批量路径必须和单格路径同款硬校验 origin 白名单——宽容默认会把 wire "
        "错误降级成静默失败，anchor 特性正是这么整个失效的（PR#281→#286）"
    )


# ===========================================================================
# 剩下 4 个从未被 HTTP 层测试碰过的端点的最小冒烟：单条变更详情／两版对比／
# 单格历史／里程碑增删／清理。
# ===========================================================================


def test_get_change_detail_returns_seq_kind_and_payload(tmp_path, monkeypatch, repo):
    ctx = _setup(tmp_path, monkeypatch)
    _patch_cell(ctx, "看这条流水")
    seq = _head(ctx)

    response = ctx["client"].get(
        f"/api/notebooks/{ctx['nb']}/knowhow/{ctx['table']['id']}/history/{seq}",
        headers=ctx["owner"],
    )

    assert response.status_code == 200
    body = response.json()
    assert body["seq"] == seq
    assert body["kind"] == "cell_update"
    assert body["payload"]["cells"][0]["after"] == "看这条流水"


def test_history_diff_reports_first_before_and_last_after_for_same_cell(
    tmp_path, monkeypatch, repo
):
    """构造一个已知的净变化：同一格子在区间内改两次，diff 必须折叠成区间
    内第一条的 before 与最后一条的 after，而不是逐条罗列或只看最新一条。"""
    ctx = _setup(tmp_path, monkeypatch)
    _patch_cell(ctx, "基线值")
    baseline_seq = _head(ctx)
    _patch_cell(ctx, "第一次改")
    _patch_cell(ctx, "第二次改")
    end_seq = _head(ctx)

    response = ctx["client"].get(
        f"/api/notebooks/{ctx['nb']}/knowhow/{ctx['table']['id']}/history/diff",
        params={"from": baseline_seq, "to": end_seq},
        headers=ctx["owner"],
    )

    assert response.status_code == 200
    cells = response.json()["cells"]
    cell = next(
        c for c in cells if c["row_id"] == ctx["row_id"] and c["column_id"] == ctx["plain"]
    )
    assert cell["before"] == "基线值"
    assert cell["after"] == "第二次改"


def test_cell_history_endpoint_entry_count_matches_edit_count(tmp_path, monkeypatch, repo):
    ctx = _setup(tmp_path, monkeypatch)
    _patch_cell(ctx, "改一")
    _patch_cell(ctx, "改二")
    _patch_cell(ctx, "改三")

    response = ctx["client"].get(
        f"/api/notebooks/{ctx['nb']}/knowhow/{ctx['table']['id']}"
        f"/rows/{ctx['row_id']}/cells/{ctx['plain']}/history",
        headers=ctx["owner"],
    )

    assert response.status_code == 200
    entries = response.json()
    assert len(entries) == 3
    assert [entry["after"] for entry in entries] == ["改三", "改二", "改一"]


def test_milestone_create_duplicate_name_and_delete_lifecycle(tmp_path, monkeypatch, repo):
    ctx = _setup(tmp_path, monkeypatch)
    seq = _head(ctx)

    create = ctx["client"].post(
        f"/api/notebooks/{ctx['nb']}/knowhow/{ctx['table']['id']}/milestones",
        headers=ctx["owner"], json={"seq": seq, "name": "v1", "note": "第一版"},
    )
    assert create.status_code == 200, create.text
    milestone_id = create.json()["id"]

    duplicate = ctx["client"].post(
        f"/api/notebooks/{ctx['nb']}/knowhow/{ctx['table']['id']}/milestones",
        headers=ctx["owner"], json={"seq": seq, "name": "v1", "note": "重名"},
    )
    assert duplicate.status_code == 400, duplicate.text

    deleted = ctx["client"].delete(
        f"/api/notebooks/{ctx['nb']}/knowhow/{ctx['table']['id']}/milestones/{milestone_id}",
        headers=ctx["owner"],
    )
    assert deleted.status_code == 204, deleted.text

    history = ctx["client"].get(
        f"/api/notebooks/{ctx['nb']}/knowhow/{ctx['table']['id']}/history",
        headers=ctx["owner"],
    ).json()
    assert milestone_id not in {m["id"] for m in history["milestones"]}


def test_prune_history_deletes_old_prefix_and_leaves_seqs_contiguous(
    tmp_path, monkeypatch, repo
):
    ctx = _setup(tmp_path, monkeypatch)
    _patch_cell(ctx, "改一")
    _patch_cell(ctx, "改二")
    _patch_cell(ctx, "改三")
    _patch_cell(ctx, "改四")
    head = _head(ctx)
    cutoff_seq = head - 2  # 保留最新两条不动，验证 prune 只删前缀不动 head 附近

    with repo._runtime.database.write() as db:
        db.execute(
            "UPDATE knowhow_changes SET created_at = ? WHERE table_id = ? AND seq <= ?",
            ("2000-01-01T00:00:00", ctx["table"]["id"], cutoff_seq),
        )

    response = ctx["client"].post(
        f"/api/notebooks/{ctx['nb']}/knowhow/{ctx['table']['id']}/history/prune",
        headers=ctx["owner"], json={"before_days": 1},
    )

    assert response.status_code == 200, response.text
    assert response.json()["removed"] == cutoff_seq

    history = ctx["client"].get(
        f"/api/notebooks/{ctx['nb']}/knowhow/{ctx['table']['id']}/history",
        headers=ctx["owner"],
    ).json()
    remaining_seqs = sorted(change["seq"] for change in history["changes"])
    assert remaining_seqs == list(range(cutoff_seq + 1, head + 1)), (
        "被清理的前缀必须真的从库里消失，且剩余 seq 连续无空洞"
    )
