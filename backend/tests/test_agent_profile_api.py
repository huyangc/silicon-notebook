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
# 用户名须为「单个小写字母 + 八位数字」——与 test_agent_profile_routes.py /
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
    # 总闸关掉时两份清单都空。`calls_enabled` 仍如实报调用记录**自己**那把开关的
    # 状态(这里没关),因为它回答的是另一个问题——前端据此分辨「这个部署不记调用」
    # 与「记，但还没有人调用过」,而总闸关掉时两份清单一律为空是更外层的结论。
    assert got.json() == {
        "enabled": False,
        "items": [],
        "calls_enabled": True,
        "calls": [],
    }

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


def test_agent_names_resolve_past_the_first_roster_page():
    """codex #535 R2 P2:owner 超过一页(100)个 Agent profile 时,老 profile 的
    观察归因不得落到未知兜底——名字解析按 roster 分页翻到尽头。纯函数级:直接
    喂一个 250 条的假 roster 给共享 helper。"""
    from app.services.agent_profile_block import (
        AGENT_PROFILE_NAME_PAGE,
        resolve_agent_profile_names,
    )

    class _P:
        def __init__(self, i):
            self.id = f"agent-{i:04d}"
            self.name = f"Agent {i}"

    roster = [_P(i) for i in range(2 * AGENT_PROFILE_NAME_PAGE + 50)]
    calls = []

    def list_profiles(owner_id, offset, limit):
        calls.append((offset, limit))
        return roster[offset:offset + limit]

    names = resolve_agent_profile_names(list_profiles, "user-a")
    assert len(names) == len(roster)
    assert names["agent-0249"] == "Agent 249"
    assert calls == [(0, 100), (100, 100), (200, 100)]


# ------------------------------------------------------- 调用记录(kind=call)


def _seed_call(notebook_id: str, owner_id: str, agent_profile_id: str, capability: str):
    from app.api.deps import repository

    return repository().agent_observations.append_call(
        notebook_id, owner_id, agent_profile_id, capability=capability
    )


def test_calls_are_a_separate_list_with_resolved_agent_names(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner, owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)
    agent_a, _ = _seed_observation(
        notebook_id, owner_id, agent_name="巡检助手", text="写下的一句"
    )
    _seed_call(notebook_id, owner_id, agent_a, "ask:execute")

    body = client.get(
        f"/api/notebooks/{notebook_id}/agent-observations", headers=owner
    ).json()

    # 两份清单各自独立:调用记账绝不混进 Agent 写下的短句里。
    assert [item["text"] for item in body["items"]] == ["写下的一句"]
    assert [item["capability"] for item in body["calls"]] == ["ask:execute"]
    assert body["calls"][0]["agent_name"] == "巡检助手"
    assert body["calls_enabled"] is True


def test_calls_are_owner_scoped_like_observations(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner, owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)
    reader, reader_id = _readonly_member(client, owner, notebook_id)

    owner_agent, _ = _seed_observation(
        notebook_id, owner_id, agent_name="Owner 的助手", text="owner"
    )
    reader_agent, _ = _seed_observation(
        notebook_id, reader_id, agent_name="Reader 的助手", text="reader"
    )
    _seed_call(notebook_id, owner_id, owner_agent, "ask:execute")
    _seed_call(notebook_id, reader_id, reader_agent, "knowledge:read")

    owner_calls = client.get(
        f"/api/notebooks/{notebook_id}/agent-observations", headers=owner
    ).json()["calls"]
    reader_calls = client.get(
        f"/api/notebooks/{notebook_id}/agent-observations", headers=reader
    ).json()["calls"]

    assert [row["capability"] for row in owner_calls] == ["ask:execute"]
    assert [row["capability"] for row in reader_calls] == ["knowledge:read"]


def test_call_log_switch_off_still_shows_and_clears_what_was_recorded(
    tmp_path, monkeypatch
):
    """关掉开关只是「从现在起不记」。已经记下的行必须照常看得见、删得掉——
    否则一份用户有权删除的数据会因为管理员翻了个开关变成他既看不到也删不掉的
    东西(codex #616 R1 P2)。``calls_enabled`` 负责说清「这里不再记了」。"""
    monkeypatch.setenv("AGENT_CALL_LOG_ENABLED", "false")
    client = _client(tmp_path, monkeypatch)
    owner, owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)
    agent_a, _ = _seed_observation(notebook_id, owner_id, agent_name="巡检助手")
    _seed_call(notebook_id, owner_id, agent_a, "ask:execute")

    body = client.get(
        f"/api/notebooks/{notebook_id}/agent-observations", headers=owner
    ).json()
    assert body["calls_enabled"] is False
    assert [row["capability"] for row in body["calls"]] == ["ask:execute"]
    assert body["enabled"] is True

    cleared = client.delete(
        f"/api/notebooks/{notebook_id}/agent-observations",
        params={"kind": "call"},
        headers=owner,
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json() == {"removed": 1}


def test_call_ledger_stays_readable_and_clearable_while_the_master_gate_is_off(
    tmp_path, monkeypatch
):
    """总闸关掉之后不再记新行(写侧叠在它之上),但既有的行仍然要能看、能清。
    只清调用记录那一支放行;其它形态的 DELETE 在这里照旧 409(既有契约)。"""
    client = _client(tmp_path, monkeypatch)
    owner, owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)
    agent_a, _ = _seed_observation(notebook_id, owner_id, agent_name="巡检助手")
    _seed_call(notebook_id, owner_id, agent_a, "ask:execute")

    # 造好数据之后再关总闸——模拟「先记了一批,后来才关」。设置缓存要显式失效,
    # 否则读到的还是建客户端那一刻的快照;``finally`` 里再清一次,免得这份
    # 「关掉」的快照漏给同一个 worker 后面的用例。
    from app.core.config import get_settings

    monkeypatch.setenv("AGENT_PROFILE_ENABLED", "false")
    get_settings.cache_clear()
    try:
        body = client.get(
            f"/api/notebooks/{notebook_id}/agent-observations", headers=owner
        ).json()
        assert body["enabled"] is False
        assert body["items"] == []
        assert [row["capability"] for row in body["calls"]] == ["ask:execute"]

        # 会碰到短句的清空仍然 409。
        blocked = client.delete(
            f"/api/notebooks/{notebook_id}/agent-observations", headers=owner
        )
        assert blocked.status_code == 409, blocked.text

        cleared = client.delete(
            f"/api/notebooks/{notebook_id}/agent-observations",
            params={"kind": "call"},
            headers=owner,
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json() == {"removed": 1}
    finally:
        monkeypatch.setenv("AGENT_PROFILE_ENABLED", "true")
        get_settings.cache_clear()


def test_clear_kind_narrows_and_unknown_kind_is_rejected(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner, owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)
    agent_a, _ = _seed_observation(
        notebook_id, owner_id, agent_name="巡检助手", text="留下的一句"
    )
    _seed_call(notebook_id, owner_id, agent_a, "ask:execute")

    only_calls = client.delete(
        f"/api/notebooks/{notebook_id}/agent-observations",
        params={"kind": "call"},
        headers=owner,
    )
    assert only_calls.status_code == 200, only_calls.text
    assert only_calls.json() == {"removed": 1}

    after = client.get(
        f"/api/notebooks/{notebook_id}/agent-observations", headers=owner
    ).json()
    assert after["calls"] == []
    assert [item["text"] for item in after["items"]] == ["留下的一句"]

    # 不认识的 kind 必须响亮失败:静默匹配零行会与「本来就没有」长得一模一样,
    # 用户会以为清过了。
    bad = client.delete(
        f"/api/notebooks/{notebook_id}/agent-observations",
        params={"kind": "calls"},
        headers=owner,
    )
    assert bad.status_code == 400, bad.text
    still_there = client.get(
        f"/api/notebooks/{notebook_id}/agent-observations", headers=owner
    ).json()
    assert [item["text"] for item in still_there["items"]] == ["留下的一句"]
