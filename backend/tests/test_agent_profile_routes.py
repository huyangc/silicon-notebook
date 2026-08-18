"""Agentic Memory P1(T6)——「AI 对这个库的理解」API 面的行为契约。

矩阵覆盖:
* 只读成员能读(共享底座 + 自己的覆盖层)、能改自己的覆盖层、不能改共享底座;
* owner 能改共享底座;
* 陌生人(无读权)对全部四个端点一律 404;
* 编辑超长 422;revision 冲突 409;
* scope 与 label 的组归属校验(共享底座只认 ``BASE_LABELS``,个人覆盖层只认
  ``OVERLAY_LABELS``)——不匹配 422;
* 手动重建走 ``start_base``/``start_overlay`` 同一条 claim→submit 路径(假
  ``background_jobs.submit`` 收集,不真的起后台线程),忙碌时二次调用 409;
* 总闸(``AGENT_PROFILE_ENABLED=false``)关时 ``GET`` 回 ``enabled=false`` + 空
  列表(不是 404),``PUT``/``DELETE``/``rebuild`` 一律 409。
"""
from __future__ import annotations

from fastapi.testclient import TestClient


_PASSWORD = "pw12345678"
# 用户名须为「单个小写字母 + 00 + 六位数字」——``{index:08d}`` 在 index < 1_000_000
# 时前两位天然是 "00",与 test_group_routes.py / test_report_ownership.py 同一个套路。
_USERNAMES = iter(f"y{index:08d}" for index in range(1, 999))


def _client(tmp_path, monkeypatch, *, agent_profile_enabled: bool = True) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv(
        "AGENT_PROFILE_ENABLED", "true" if agent_profile_enabled else "false"
    )
    from app.main import app

    return TestClient(app)


def _new_user(client: TestClient) -> tuple[dict, str]:
    username = next(_USERNAMES)
    client.post(
        "/api/auth/register", json={"username": username, "password": _PASSWORD}
    )
    token = client.post(
        "/api/auth/login", json={"username": username, "password": _PASSWORD}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    return headers, client.get("/api/me", headers=headers).json()["id"]


def _notebook(client: TestClient, headers: dict, name: str = "库") -> str:
    return client.post("/api/notebooks", json={"name": name}, headers=headers).json()["id"]


def _readonly_member(
    client: TestClient, owner_headers: dict, notebook_id: str
) -> tuple[dict, str]:
    """建组 → 拉一个新用户进组(member 档)→ 把库共享给该组(viewer 档)。

    返回该只读成员的 (headers, user_id)。镜像 ``test_report_ownership.py`` 的
    ``_group_granted_reader``——群组授权边是本仓库当前唯一能把一个陌生用户
    变成「共享库只读成员」的公开 API 路径(``notebook_grants`` 的
    ``GRANTABLE_PRINCIPAL_TYPES`` 只认 group/group_admins,不接受直接的
    ``principal_type="user"``)。
    """
    reader_headers, reader_id = _new_user(client)
    group_id = client.post(
        "/api/groups", json={"name": "项目组"}, headers=owner_headers
    ).json()["id"]
    assert client.put(
        f"/api/groups/{group_id}/members/{reader_id}",
        json={"role": "member"},
        headers=owner_headers,
    ).status_code == 200
    assert client.post(
        f"/api/notebooks/{notebook_id}/grants",
        json={"principal_type": "group", "principal_id": group_id, "role": "viewer"},
        headers=owner_headers,
    ).status_code == 200
    return reader_headers, reader_id


def _fake_submit(monkeypatch):
    """把 ``agent_profile_job.py`` 用的 ``background_jobs.submit`` 换成只记录
    调用、不真的起后台线程的假实现——手动重建因此真的走到 claim,但不会跑真的
    巡固(不需要配置模型)。"""
    from app.services import background_jobs

    captured: list = []

    def _submit(fn, *args, **kwargs):
        captured.append({"fn": fn, "args": args, "kwargs": kwargs})
        return None

    monkeypatch.setattr(background_jobs, "submit", _submit)
    return captured


# --------------------------------------------------------------------- read


def test_readonly_member_reads_base_and_own_overlay_but_cannot_write_base(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)
    reader, reader_id = _readonly_member(client, owner, notebook_id)

    # owner 写一条共享底座
    put = client.put(
        f"/api/notebooks/{notebook_id}/understanding/corpus_shape",
        json={"scope": "shared", "value": "这是一批数据手册", "expected_revision": 0},
        headers=owner,
    )
    assert put.status_code == 200, put.text
    assert put.json()["value"] == "这是一批数据手册"
    assert put.json()["updated_origin"] == "user"

    # 只读成员能读到那条底座
    got = client.get(f"/api/notebooks/{notebook_id}/understanding", headers=reader)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["enabled"] is True
    assert body["can_edit_base"] is False
    assert [b["label"] for b in body["base"]] == ["corpus_shape"]
    assert body["base"][0]["value"] == "这是一批数据手册"
    assert body["mine"] == []

    # 只读成员能写自己的覆盖层
    mine = client.put(
        f"/api/notebooks/{notebook_id}/understanding/retrieval_notes",
        json={"scope": "mine", "value": "我常按型号查", "expected_revision": 0},
        headers=reader,
    )
    assert mine.status_code == 200, mine.text
    assert mine.json()["value"] == "我常按型号查"

    # owner 看不到只读成员的覆盖层(owner 自己没有覆盖层行)
    owner_view = client.get(
        f"/api/notebooks/{notebook_id}/understanding", headers=owner
    ).json()
    assert owner_view["mine"] == []
    assert owner_view["can_edit_base"] is True

    # 只读成员不能写共享底座 —— 404(不泄露存在性,与 owner-only 写守卫同形态)
    denied = client.put(
        f"/api/notebooks/{notebook_id}/understanding/corpus_shape",
        json={"scope": "shared", "value": "改一下", "expected_revision": 1},
        headers=reader,
    )
    assert denied.status_code == 404, denied.text
    assert denied.headers.get("X-User-Message") is None

    # 同理不能清空共享底座
    denied_clear = client.delete(
        f"/api/notebooks/{notebook_id}/understanding/corpus_shape?scope=shared",
        headers=reader,
    )
    assert denied_clear.status_code == 404, denied_clear.text

    # 同理不能手动重建共享底座
    denied_rebuild = client.post(
        f"/api/notebooks/{notebook_id}/understanding/rebuild",
        json={"scope": "shared"},
        headers=reader,
    )
    assert denied_rebuild.status_code == 404, denied_rebuild.text


def test_stranger_gets_404_on_every_endpoint(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)
    stranger, _stranger_id = _new_user(client)

    get_resp = client.get(
        f"/api/notebooks/{notebook_id}/understanding", headers=stranger
    )
    assert get_resp.status_code == 404, get_resp.text

    put_resp = client.put(
        f"/api/notebooks/{notebook_id}/understanding/corpus_shape",
        json={"scope": "shared", "value": "x", "expected_revision": 0},
        headers=stranger,
    )
    assert put_resp.status_code == 404, put_resp.text

    delete_resp = client.delete(
        f"/api/notebooks/{notebook_id}/understanding/corpus_shape?scope=shared",
        headers=stranger,
    )
    assert delete_resp.status_code == 404, delete_resp.text

    rebuild_resp = client.post(
        f"/api/notebooks/{notebook_id}/understanding/rebuild",
        json={"scope": "shared"},
        headers=stranger,
    )
    assert rebuild_resp.status_code == 404, rebuild_resp.text


# ------------------------------------------------------------------- write


def test_value_too_long_is_rejected_with_422(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)

    too_long = "字" * 401
    resp = client.put(
        f"/api/notebooks/{notebook_id}/understanding/corpus_shape",
        json={"scope": "shared", "value": too_long, "expected_revision": 0},
        headers=owner,
    )
    assert resp.status_code == 422, resp.text
    assert resp.headers.get("X-User-Message") == "1"


def test_revision_conflict_is_409(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)

    first = client.put(
        f"/api/notebooks/{notebook_id}/understanding/corpus_shape",
        json={"scope": "shared", "value": "第一版", "expected_revision": 0},
        headers=owner,
    )
    assert first.status_code == 200, first.text
    assert first.json()["revision"] == 1

    # 用旧的 expected_revision(0)再写一次 —— 服务端此刻已经是 1
    stale = client.put(
        f"/api/notebooks/{notebook_id}/understanding/corpus_shape",
        json={"scope": "shared", "value": "第二版", "expected_revision": 0},
        headers=owner,
    )
    assert stale.status_code == 409, stale.text
    assert stale.headers.get("X-User-Message") == "1"

    # 对一条从未写过的块,非 0 的 expected_revision 同样是冲突(没有行可比对)
    never_written = client.put(
        f"/api/notebooks/{notebook_id}/understanding/key_entities",
        json={"scope": "shared", "value": "x", "expected_revision": 3},
        headers=owner,
    )
    assert never_written.status_code == 409, never_written.text


def test_scope_label_mismatch_is_422(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)

    # corpus_shape 是共享底座的 label,不许在 scope=mine 下编辑
    wrong_scope = client.put(
        f"/api/notebooks/{notebook_id}/understanding/corpus_shape",
        json={"scope": "mine", "value": "x", "expected_revision": 0},
        headers=owner,
    )
    assert wrong_scope.status_code == 422, wrong_scope.text
    assert wrong_scope.headers.get("X-User-Message") == "1"

    # retrieval_notes 是覆盖层的 label,不许在 scope=shared 下编辑
    wrong_scope_2 = client.put(
        f"/api/notebooks/{notebook_id}/understanding/retrieval_notes",
        json={"scope": "shared", "value": "x", "expected_revision": 0},
        headers=owner,
    )
    assert wrong_scope_2.status_code == 422, wrong_scope_2.text

    # DELETE 同一条校验
    wrong_scope_delete = client.delete(
        f"/api/notebooks/{notebook_id}/understanding/corpus_shape?scope=mine",
        headers=owner,
    )
    assert wrong_scope_delete.status_code == 422, wrong_scope_delete.text

    # rebuild 不按 label 校验(它没有 label),但 scope 值域仍由 pydantic 校验
    bad_scope = client.post(
        f"/api/notebooks/{notebook_id}/understanding/rebuild",
        json={"scope": "everyone"},
        headers=owner,
    )
    assert bad_scope.status_code == 422, bad_scope.text


def test_clear_block_is_idempotent_and_preserves_the_row(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)

    # 从没写过的块:清空是幂等的,不报错
    empty = client.delete(
        f"/api/notebooks/{notebook_id}/understanding/corpus_shape?scope=shared",
        headers=owner,
    )
    assert empty.status_code == 200, empty.text
    assert empty.json()["value"] == ""
    assert empty.json()["revision"] == 0

    client.put(
        f"/api/notebooks/{notebook_id}/understanding/corpus_shape",
        json={"scope": "shared", "value": "有内容", "expected_revision": 0},
        headers=owner,
    )
    cleared = client.delete(
        f"/api/notebooks/{notebook_id}/understanding/corpus_shape?scope=shared",
        headers=owner,
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["value"] == ""
    # 清空保留行与历史(``clear_block`` 的既有契约),revision 继续前进而不是清零
    assert cleared.json()["revision"] == 2

    # 清空之后的块仍然存在于 GET 的 base 列表里(只是值为空)——不是被整行删掉
    listed = client.get(
        f"/api/notebooks/{notebook_id}/understanding", headers=owner
    ).json()
    assert [b["label"] for b in listed["base"]] == ["corpus_shape"]
    assert listed["base"][0]["value"] == ""


# ----------------------------------------------------------------- rebuild


def test_manual_rebuild_claims_via_start_base_and_start_overlay(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner, owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)
    captured = _fake_submit(monkeypatch)

    shared = client.post(
        f"/api/notebooks/{notebook_id}/understanding/rebuild",
        json={"scope": "shared"},
        headers=owner,
    )
    assert shared.status_code == 200, shared.text
    assert shared.json() == {"started": True}

    mine = client.post(
        f"/api/notebooks/{notebook_id}/understanding/rebuild",
        json={"scope": "mine"},
        headers=owner,
    )
    assert mine.status_code == 200, mine.text
    assert mine.json() == {"started": True}

    # 两条链路各排了一次(假 submit 真的被调用了两次)——一次是 start_base 的
    # run_base,一次是 start_overlay 的 run_overlay。
    assert len(captured) == 2
    fn_names = {call["fn"].__name__ for call in captured}
    assert fn_names == {"run_base", "run_overlay"}

    # 两条链路的 job 状态都已经从 idle 翻到 running(claim 成功、submit 被拦下
    # 没有真的跑,所以状态停在 running 而不是被 settle 推进)。
    status = client.get(
        f"/api/notebooks/{notebook_id}/understanding", headers=owner
    ).json()
    assert status["job"]["base"]["status"] == "running"
    assert status["job"]["mine"]["status"] == "running"


def test_manual_rebuild_is_409_when_already_running(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)
    _fake_submit(monkeypatch)

    first = client.post(
        f"/api/notebooks/{notebook_id}/understanding/rebuild",
        json={"scope": "shared"},
        headers=owner,
    )
    assert first.status_code == 200, first.text

    second = client.post(
        f"/api/notebooks/{notebook_id}/understanding/rebuild",
        json={"scope": "shared"},
        headers=owner,
    )
    assert second.status_code == 409, second.text
    assert second.headers.get("X-User-Message") == "1"
    assert second.json()["detail"] == "正在整理，请稍候"


# ------------------------------------------------------------------- gate


def test_disabled_gate_is_transparent_on_get_and_409_on_writes(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, agent_profile_enabled=False)
    owner, _owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)

    got = client.get(f"/api/notebooks/{notebook_id}/understanding", headers=owner)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["enabled"] is False
    assert body["base"] == []
    assert body["mine"] == []
    assert body["job"] == {"base": None, "mine": None}
    # can_edit_base 是一条独立于总闸的纯权限判定,关闸时仍如实返回
    assert body["can_edit_base"] is True

    put = client.put(
        f"/api/notebooks/{notebook_id}/understanding/corpus_shape",
        json={"scope": "shared", "value": "x", "expected_revision": 0},
        headers=owner,
    )
    assert put.status_code == 409, put.text
    assert put.headers.get("X-User-Message") == "1"

    delete = client.delete(
        f"/api/notebooks/{notebook_id}/understanding/corpus_shape?scope=shared",
        headers=owner,
    )
    assert delete.status_code == 409, delete.text

    rebuild = client.post(
        f"/api/notebooks/{notebook_id}/understanding/rebuild",
        json={"scope": "shared"},
        headers=owner,
    )
    assert rebuild.status_code == 409, rebuild.text
    assert rebuild.headers.get("X-User-Message") == "1"
