"""Agentic Memory P3(T5)——「Agent 记录」观察管理 API 面的行为契约。

覆盖:
* 读隔离(user B 的观察永远读不到 user A 的,即使同在一个笔记本、即使笔记本是
  共享的);
* `agent_name` 从调用者自己名下的 Agent 解析;
* 按 ``agent_profile_id`` 只清一个 Agent 的记录,省略时全清;
* 总闸(``AGENT_PROFILE_ENABLED=false``)关时 GET 回 ``enabled=false`` + 空列表
  (不是 404),DELETE 一律走 ``_DISABLED_MESSAGE`` 同款 409;
* ``limit`` 查询参数的默认值/上下界
  (``AGENT_OBSERVATION_SAMPLE_MAX``/``AGENT_OBSERVATION_RING_MAX``)。

这两个端点都不走 T3 的 ``add_observation`` MCP 工具(并行任务,可能尚未落地)——
测试直接经 ``repository().agent_observations``/``repository().
create_agent_profile`` 这两个 T1/T2 已落地的仓储方法造数据,与 ``add_observation``
自身的输入校验/幂等契约无关。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.repositories.ports import AGENT_OBSERVATION_RING_MAX, AGENT_OBSERVATION_SAMPLE_MAX


_PASSWORD = "pw12345678"
# 用户名须为「单个小写字母 + 00 + 六位数字」——与 test_agent_profile_routes.py /
# test_group_routes.py 同一个套路,换一个字母前缀避免与它们并行跑时撞用户名。
_USERNAMES = iter(f"q{index:08d}" for index in range(1, 999))


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

    镜像 ``test_agent_profile_routes.py`` 的同名 helper——群组授权边是本仓库
    当前唯一能把一个陌生用户变成「共享库只读成员」的公开 API 路径。
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


def _seed_observation(
    notebook_id: str,
    owner_id: str,
    *,
    agent_name: str = "巡检助手",
    text: str = "常按型号查参数表",
    request_id: str | None = None,
) -> tuple[str, str]:
    """直接经仓储写一条观察,绕开 T3 的 MCP 工具(并行任务)。**总是新建一个
    Agent profile**——两次调用即使 ``agent_name`` 相同也是两个不同的 Agent
    (与 ``list_agent_profiles`` 不按名字去重同一条道理)。要往同一个 Agent
    名下追加第二条观察,用 ``_seed_observation_for_profile``。返回
    ``(agent_profile_id, observation_id)``。"""
    from app.api.deps import repository

    repo = repository()
    profile = repo.create_agent_profile(owner_id, agent_name)
    return _seed_observation_for_profile(
        notebook_id, owner_id, profile.id, text=text, request_id=request_id
    )


def _seed_observation_for_profile(
    notebook_id: str,
    owner_id: str,
    agent_profile_id: str,
    *,
    text: str = "常按型号查参数表",
    request_id: str | None = None,
) -> tuple[str, str]:
    """往一个**已经存在**的 Agent profile 下追加一条观察。"""
    from app.api.deps import repository

    rid = request_id or f"req-{agent_profile_id}-{text}"
    obs_id, _deduplicated = repository().agent_observations.append_observation(
        notebook_id, owner_id, agent_profile_id, text=text, client_request_id=rid
    )
    return agent_profile_id, obs_id


# --------------------------------------------------------------------- read


def test_isolation_reader_never_sees_owner_observations_in_shared_notebook(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    owner, owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)
    reader, reader_id = _readonly_member(client, owner, notebook_id)

    _seed_observation(notebook_id, owner_id, agent_name="Owner 的助手", text="owner 的记录")
    _seed_observation(notebook_id, reader_id, agent_name="Reader 的助手", text="reader 的记录")

    owner_view = client.get(
        f"/api/notebooks/{notebook_id}/agent-observations", headers=owner
    )
    assert owner_view.status_code == 200, owner_view.text
    owner_body = owner_view.json()
    assert owner_body["enabled"] is True
    assert [item["text"] for item in owner_body["items"]] == ["owner 的记录"]
    assert owner_body["items"][0]["agent_name"] == "Owner 的助手"

    reader_view = client.get(
        f"/api/notebooks/{notebook_id}/agent-observations", headers=reader
    ).json()
    assert [item["text"] for item in reader_view["items"]] == ["reader 的记录"]
    assert reader_view["items"][0]["agent_name"] == "Reader 的助手"


def test_stranger_without_read_access_gets_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner, owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)
    stranger, _ = _new_user(client)
    _seed_observation(notebook_id, owner_id)

    resp = client.get(
        f"/api/notebooks/{notebook_id}/agent-observations", headers=stranger
    )
    assert resp.status_code == 404, resp.text


# -------------------------------------------------------------------- clear


def test_clear_by_agent_profile_id_then_clear_all(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner, owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)

    agent_a, _ = _seed_observation(notebook_id, owner_id, agent_name="Agent A", text="a1")
    _seed_observation_for_profile(notebook_id, owner_id, agent_a, text="a2")
    agent_b, _ = _seed_observation(notebook_id, owner_id, agent_name="Agent B", text="b1")

    # 按 profile 只清 A——B 的记录原样保留。
    cleared_a = client.delete(
        f"/api/notebooks/{notebook_id}/agent-observations",
        params={"agent_profile_id": agent_a},
        headers=owner,
    )
    assert cleared_a.status_code == 200, cleared_a.text
    assert cleared_a.json() == {"removed": 2}

    after_a = client.get(
        f"/api/notebooks/{notebook_id}/agent-observations", headers=owner
    ).json()
    assert [item["text"] for item in after_a["items"]] == ["b1"]

    # 全清(省略 agent_profile_id)——剩下的 B 也没了。
    cleared_all = client.delete(
        f"/api/notebooks/{notebook_id}/agent-observations", headers=owner
    )
    assert cleared_all.status_code == 200, cleared_all.text
    assert cleared_all.json() == {"removed": 1}

    after_all = client.get(
        f"/api/notebooks/{notebook_id}/agent-observations", headers=owner
    ).json()
    assert after_all["items"] == []

    # id 变量仅用于构造请求,静默未使用检查(agent_b 已经在上面的全清里验证)。
    assert agent_b


# ------------------------------------------------------------------- 总闸


def test_disabled_get_returns_empty_not_404_delete_returns_409(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, agent_profile_enabled=False)
    owner, owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)

    got = client.get(f"/api/notebooks/{notebook_id}/agent-observations", headers=owner)
    assert got.status_code == 200, got.text
    assert got.json() == {"enabled": False, "items": []}

    deleted = client.delete(
        f"/api/notebooks/{notebook_id}/agent-observations", headers=owner
    )
    assert deleted.status_code == 409, deleted.text
    # ``X-User-Message`` 只是一个「detail 是人话」的布尔标记(值恒为 "1"),
    # 真正的文案在响应体的 ``detail`` 里——同 ``user_error()`` 自己的约定。
    assert deleted.headers.get("X-User-Message") == "1"
    assert deleted.json()["detail"] == "这项功能当前未开启，暂时无法编辑"


# ------------------------------------------------------------------- limit


def test_limit_default_and_bounds(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner, owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)

    for i in range(3):
        _seed_observation(
            notebook_id, owner_id, agent_name="Agent A", text=f"line-{i}",
            request_id=f"req-line-{i}",
        )

    # 默认 limit(AGENT_OBSERVATION_SAMPLE_MAX)足够装下 3 条,不截断。
    default = client.get(
        f"/api/notebooks/{notebook_id}/agent-observations", headers=owner
    ).json()
    assert len(default["items"]) == 3
    # 新到旧——最后写入的 line-2 排在最前。
    assert default["items"][0]["text"] == "line-2"

    limited = client.get(
        f"/api/notebooks/{notebook_id}/agent-observations",
        params={"limit": 1},
        headers=owner,
    ).json()
    assert [item["text"] for item in limited["items"]] == ["line-2"]

    too_small = client.get(
        f"/api/notebooks/{notebook_id}/agent-observations",
        params={"limit": 0},
        headers=owner,
    )
    assert too_small.status_code == 422, too_small.text

    too_large = client.get(
        f"/api/notebooks/{notebook_id}/agent-observations",
        params={"limit": AGENT_OBSERVATION_RING_MAX + 1},
        headers=owner,
    )
    assert too_large.status_code == 422, too_large.text

    at_ring_max = client.get(
        f"/api/notebooks/{notebook_id}/agent-observations",
        params={"limit": AGENT_OBSERVATION_RING_MAX},
        headers=owner,
    )
    assert at_ring_max.status_code == 200, at_ring_max.text
    assert AGENT_OBSERVATION_SAMPLE_MAX <= AGENT_OBSERVATION_RING_MAX
