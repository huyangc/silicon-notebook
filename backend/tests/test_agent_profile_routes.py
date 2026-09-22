"""Agentic Memory P1(T6)/P2(PR-2)——「AI 对这个库的理解」+「检索经验」API 面
的行为契约。

理解块(P1)矩阵覆盖:
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

检索经验(P2,文件末尾一节)矩阵覆盖:
* 全体成员可读,列表按 ``(support desc, updated_at desc, id asc)``,投影只含
  六个字段(``id``/``situation``/``provenance`` 不上屏);``updated_at`` 按真
  时刻比而不是按字符串,解析不出的排最后且不当「最近更新」;
* 取数宽度是分区上限的两倍、截断跑在排序之后——分区暂时超上限时露出来的是
  support 最高的 100 条,不是 id 序的前 100 条;
* ``can_manage`` 是纯能力位,跟 ``agent_profile:write`` 走——owner True、只读
  成员 False;镜像围栏**不**并进它(与 ``can_edit_base`` 同口径),只挡两个写端点;
* 经验总闸(``RETRIEVAL_EXPERIENCE_ENABLED=false``)关时 ``GET`` 回
  ``enabled=false`` 且**不查表**,``POST .../distill`` 409,``DELETE`` 照常能删;
* ``distill`` 的三种 409(关闸 / 单飞占用 / 冷却)文案各不相同;冷却只在该库
  没有待处理提问时生效,有新提问立刻放行;
* ``DELETE`` 只清本库那一份,全局分区与别的库一行不动;
* 写端点无 ``agent_profile:write`` → 404,陌生人三个端点一律 404;
* 路由注册顺序:``DELETE .../understanding/experiences`` 必须落进经验端点,
  不能被 ``/understanding/{label}`` 抢走(会变成一个 422)。
"""
from __future__ import annotations

from fastapi.testclient import TestClient


_PASSWORD = "pw12345678"
# 用户名须为「单个小写字母 + 八位数字」；``{index:08d}`` 直接生成八位数字部分。
_USERNAMES = iter(f"y{index:08d}" for index in range(1, 999))


def _client(
    tmp_path,
    monkeypatch,
    *,
    agent_profile_enabled: bool = True,
    retrieval_experience_enabled: bool = True,
) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv(
        "AGENT_PROFILE_ENABLED", "true" if agent_profile_enabled else "false"
    )
    # 两把总闸相互独立(见 ``agent_profile_routes`` 模块 docstring 的 P2 一节),
    # 所以两个形参也相互独立——测「理解块关了、经验还开着」这种组合正是它们
    # 分开的理由。
    monkeypatch.setenv(
        "RETRIEVAL_EXPERIENCE_ENABLED",
        "true" if retrieval_experience_enabled else "false",
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
        f"/api/notebooks/{notebook_id}/understanding/corpus_shape?scope=shared&expected_revision=0",
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
        f"/api/notebooks/{notebook_id}/understanding/corpus_shape?scope=shared&expected_revision=0",
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
    assert resp.json()["detail"] == "内容过长，最多 400 字"


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
    assert stale.json()["detail"] == "这段理解刚被更新过，请刷新后再改"

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
    assert wrong_scope.json()["detail"] == "所选范围与这项内容不匹配，无法编辑"

    # retrieval_notes 是覆盖层的 label,不许在 scope=shared 下编辑
    wrong_scope_2 = client.put(
        f"/api/notebooks/{notebook_id}/understanding/retrieval_notes",
        json={"scope": "shared", "value": "x", "expected_revision": 0},
        headers=owner,
    )
    assert wrong_scope_2.status_code == 422, wrong_scope_2.text

    # DELETE 同一条校验
    wrong_scope_delete = client.delete(
        f"/api/notebooks/{notebook_id}/understanding/corpus_shape?scope=mine&expected_revision=0",
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


def test_delete_scope_is_validated_against_the_literal_value_domain(tmp_path, monkeypatch):
    """``DELETE .../understanding/{label}?scope=...`` 走 ``UnderstandingScope``
    (``Literal["shared", "mine"]``)而不是裸 ``str`` —— 越域值在 pydantic 校验
    这一层就该 422,不该先落进 handler 再被 ``_label_allowed_for_scope`` 判
    "不匹配"。既覆盖完全未知的值,也覆盖大小写不匹配(``Literal`` 大小写敏
    感,``"Mine"`` 不是 ``"mine"``)。"""
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)

    bogus = client.delete(
        f"/api/notebooks/{notebook_id}/understanding/corpus_shape?scope=bogus&expected_revision=0",
        headers=owner,
    )
    assert bogus.status_code == 422, bogus.text

    wrong_case = client.delete(
        f"/api/notebooks/{notebook_id}/understanding/corpus_shape?scope=Mine&expected_revision=0",
        headers=owner,
    )
    assert wrong_case.status_code == 422, wrong_case.text


def test_unknown_label_422s_before_handler_code_runs(tmp_path, monkeypatch):
    """``label`` 路径参数走 ``UnderstandingLabel``(五个合法值的 ``Literal``)。
    未知 label 必须在 FastAPI 的请求校验这一层就 422 —— 不是 handler 里那句
    「scope 与 label 不匹配」的归属文案,而是路径参数本身就不存在这个值。"""
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)

    resp = client.put(
        f"/api/notebooks/{notebook_id}/understanding/bogus",
        json={"scope": "shared", "value": "x", "expected_revision": 0},
        headers=owner,
    )
    assert resp.status_code == 422, resp.text


def test_body_cannot_smuggle_an_owner_id_field(tmp_path, monkeypatch):
    """``UnderstandingBlockUpdate`` 从不读请求体里的 owner —— ``owner_id`` 永远
    由服务端从已认证调用者派生。这条用例钉的是「给 body 多塞一个 owner_id 字段
    并被采信」这种移动变异:``extra="forbid"`` 必须真的兜住它,而不是被某次
    改动悄悄换成 ``extra="ignore"`` 或在模型里加一个会被读取的 ``owner_id``
    字段。"""
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id = _new_user(client)
    _other_headers, other_id = _new_user(client)
    notebook_id = _notebook(client, owner)

    resp = client.put(
        f"/api/notebooks/{notebook_id}/understanding/retrieval_notes",
        json={
            "scope": "mine",
            "value": "x",
            "expected_revision": 0,
            "owner_id": other_id,
        },
        headers=owner,
    )
    assert resp.status_code == 422, resp.text


def test_clear_block_is_idempotent_and_preserves_the_row(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)

    # 从没写过的块:清空是幂等的,不报错
    empty = client.delete(
        f"/api/notebooks/{notebook_id}/understanding/corpus_shape?scope=shared&expected_revision=0",
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
        f"/api/notebooks/{notebook_id}/understanding/corpus_shape?scope=shared&expected_revision=1",
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
    # 披露面是一份固定键集——存储层的 ``diagnostic`` 列绝不能混进这份 wire
    # payload(store docstring 明确写它「永不上屏」)。直接断言键集,不依赖
    # api_contract 夹具(那份夹具钉的是 schema 形状,不是「这次真实响应里出现
    # 了哪些键」)。
    assert set(status["job"]["base"].keys()) == {
        "status", "pending", "updated_at", "failure_reason",
    }
    assert set(status["job"]["mine"].keys()) == {
        "status", "pending", "updated_at", "failure_reason",
    }


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
    # 总闸关闭时不再查这项能力——不可编辑已经由总闸决定,响应固定为 False
    # (即使调用者本来是 owner、本该有 agent_profile:write 能力)。
    assert body["can_edit_base"] is False

    put = client.put(
        f"/api/notebooks/{notebook_id}/understanding/corpus_shape",
        json={"scope": "shared", "value": "x", "expected_revision": 0},
        headers=owner,
    )
    assert put.status_code == 409, put.text
    assert put.headers.get("X-User-Message") == "1"
    assert put.json()["detail"] == "这项功能当前未开启，暂时无法编辑"

    delete = client.delete(
        f"/api/notebooks/{notebook_id}/understanding/corpus_shape?scope=shared&expected_revision=0",
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


def test_delete_requires_the_revision_the_browser_saw(tmp_path, monkeypatch):
    """codex R1 P2:DELETE 与 PUT 同享乐观并发。

    服务端自读当前 revision 再清空,会把「浏览器加载之后被巡固/他人更新过」的
    未见内容清掉——恰恰绕开了 PUT 的保护。DELETE 因此必须带界面看到过的
    ``expected_revision``:过期 409,缺参 422,幂等语义只对「浏览器看到的确实是
    空块」成立。
    """
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)

    # 建块(revision=1),再更新一次(revision=2)——模拟浏览器只看过 revision=1
    client.put(
        f"/api/notebooks/{notebook_id}/understanding/corpus_shape",
        json={"scope": "shared", "value": "第一版", "expected_revision": 0},
        headers=owner,
    )
    client.put(
        f"/api/notebooks/{notebook_id}/understanding/corpus_shape",
        json={"scope": "shared", "value": "第二版", "expected_revision": 1},
        headers=owner,
    )

    stale = client.delete(
        f"/api/notebooks/{notebook_id}/understanding/corpus_shape?scope=shared&expected_revision=1",
        headers=owner,
    )
    assert stale.status_code == 409, stale.text
    assert stale.headers.get("X-User-Message") == "1"
    assert stale.json()["detail"] == "这段理解刚被更新过，请刷新后再改"

    # 拿最新 revision 才能清
    ok = client.delete(
        f"/api/notebooks/{notebook_id}/understanding/corpus_shape?scope=shared&expected_revision=2",
        headers=owner,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["value"] == ""

    # 缺参是请求形状错误(FastAPI 校验),不是幂等清空
    missing = client.delete(
        f"/api/notebooks/{notebook_id}/understanding/corpus_shape?scope=shared",
        headers=owner,
    )
    assert missing.status_code == 422, missing.text

    # 行不存在但浏览器声称看过非 0 版本:它看过的那行已被整块删掉 → 409 让它重取
    other, _other_id = _new_user(client)
    fresh_nb = _notebook(client, other)
    ghost = client.delete(
        f"/api/notebooks/{fresh_nb}/understanding/corpus_shape?scope=shared&expected_revision=3",
        headers=other,
    )
    assert ghost.status_code == 409, ghost.text


def test_get_reads_job_rows_before_blocks(tmp_path, monkeypatch):
    """codex R7 P2:读序是契约——job 行先于块。

    反序时「巡固在两读之间写块并 settle」会产出 done + 旧块,前端据此解除忙碌、
    停止轮询,旧文本挂到重开面板;job 先走后同一交错最坏是 running + 新块,
    下一拍轮询自然收敛。
    """
    from app.repositories.sqlite.agent_profile_store import AgentProfileStore

    calls: list[str] = []
    real_job_row = AgentProfileStore.job_row
    real_read_blocks = AgentProfileStore.read_blocks

    def spy_job_row(self, notebook_id, owner_id):
        calls.append("job_row")
        return real_job_row(self, notebook_id, owner_id)

    def spy_read_blocks(self, notebook_id, owner_id):
        calls.append("read_blocks")
        return real_read_blocks(self, notebook_id, owner_id)

    monkeypatch.setattr(AgentProfileStore, "job_row", spy_job_row)
    monkeypatch.setattr(AgentProfileStore, "read_blocks", spy_read_blocks)

    client = _client(tmp_path, monkeypatch)
    owner, _owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)
    calls.clear()
    resp = client.get(
        f"/api/notebooks/{notebook_id}/understanding", headers=owner
    )
    assert resp.status_code == 200, resp.text
    assert calls == ["job_row", "job_row", "read_blocks"]


# ------------------------------------------------------- 检索经验(P2,PR-2)


_EXPERIENCES = "understanding/experiences"


def _experience_store():
    """应用此刻真正在用的那一份经验行存储。

    ``deps.repository`` 是 ``lru_cache``,而 conftest 每条用例都清一次缓存,
    所以这里拿到的恒是本用例自己那个 TestClient 背后的 store。
    """
    from app.api import deps

    return deps.repository().retrieval_experiences


def _experience_jobs():
    """同一份蒸馏 service(``distill_now`` 与它的冷却状态都挂在这个实例上)。"""
    from app.api import deps

    return deps.repository()._runtime.retrieval_experience_jobs


def _seed_experience(
    store,
    notebook_id: str,
    experience_id: str,
    *,
    action: str = "retrieve",
    polarity: str = "good",
    rationale: str = "这类问题先宽后窄更省步数",
    support: int = 1,
    when: str = "2026-09-22T10:00:00+08:00",
) -> None:
    """直接经 store 写一条经验行。

    刻意不经蒸馏链路:那条链路要一次真实模型调用,而这批用例测的是端点的
    读序、排序、投影与权限。``support`` 由 provenance 的条数决定——store 的
    合并语义是「新 run id 有几条就加几」,插入分支写的正是 ``len(fresh)``。
    ``when`` 覆盖 store 自己的时钟,好让 ``updated_at`` 这个次级排序键在一条
    用例里可控(SQLite 的秒级时钟本来分不开同一批写入)。
    """
    store.now = lambda: when
    store.upsert_experience(
        experience_id,
        situation={"mode": "reasoning", "result_scope": "ranked"},
        action=action,
        polarity=polarity,
        rationale=rationale,
        provenance=[f"{experience_id}-run-{index}" for index in range(support)],
        # 插入分支写的 support 是**留存后**那一段的长度,所以保留上限必须放得下
        # 想要的 support;生产里那个数是 60,这里按需放宽,不改被测的任何语义。
        provenance_max=max(60, support),
        replace_conclusion=True,
        notebook_id=notebook_id,
    )


def test_every_reader_sees_the_list_ordered_and_only_owner_can_manage(
    tmp_path, monkeypatch
):
    """§13-Q2:条目给全体成员看;管理动作按 ``agent_profile:write``。

    排序是 ``(support desc, updated_at desc, id asc)``——三级都要被这一条压住,
    所以种子里 b/c 同 support 不同时间、c/d 同 support 同时间不同 id。⚠ 那对
    同时刻的行给的是**不同的 rationale**:给成一样的话,第三级键换成任何别的
    顺序(甚至不排)这条用例都照样绿,``id asc`` 就成了一个没被钉住的契约。
    """
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)
    reader, _reader_id = _readonly_member(client, owner, notebook_id)
    store = _experience_store()

    _seed_experience(
        store, notebook_id, "b-old", rationale="这条更早",
        support=2, when="2026-09-20T09:00:00+08:00",
    )
    # 写入顺序刻意与 id 序相反:第三级键若没生效,出来的就是这个写入序。
    _seed_experience(
        store, notebook_id, "d-tie", rationale="同刻里 id 靠后的",
        support=2, when="2026-09-21T09:00:00+08:00",
    )
    _seed_experience(
        store, notebook_id, "c-tie", rationale="同刻里 id 靠前的",
        support=2, when="2026-09-21T09:00:00+08:00",
    )
    _seed_experience(
        store,
        notebook_id,
        "a-top",
        action="exact_lookup",
        polarity="bad",
        rationale="这类问题里精查基本白跑",
        support=5,
        when="2026-09-19T09:00:00+08:00",
    )

    got = client.get(f"/api/notebooks/{notebook_id}/{_EXPERIENCES}", headers=reader)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["enabled"] is True
    assert body["count"] == 4
    # 分区内最大 updated_at,不是列表第一行的那个(第一行是 support 最高的)。
    assert body["updated_at"] == "2026-09-21T09:00:00+08:00"
    # 只读成员看得到条目,但看不到按钮。
    assert body["can_manage"] is False
    assert [entry["rationale"] for entry in body["entries"]] == [
        "这类问题里精查基本白跑",   # support=5
        "同刻里 id 靠前的",         # support=2, 09-21, id "c-tie"
        "同刻里 id 靠后的",         # support=2, 09-21, id "d-tie"
        "这条更早",                 # support=2, 09-20
    ]
    assert [entry["updated_at"] for entry in body["entries"]] == [
        "2026-09-19T09:00:00+08:00",
        "2026-09-21T09:00:00+08:00",
        "2026-09-21T09:00:00+08:00",
        "2026-09-20T09:00:00+08:00",
    ]
    assert body["entries"][0]["action"] == "exact_lookup"
    assert body["entries"][0]["polarity"] == "bad"
    assert body["entries"][0]["support"] == 5
    # 披露面是一份固定键集:``id``(情境指纹的内容哈希)、``situation``(别人
    # 问题的形状)与 ``provenance``(别人的 run id)一个都不下发。
    assert set(body["entries"][0].keys()) == {
        "action", "polarity", "rationale", "support", "adopted", "updated_at",
    }

    assert client.get(
        f"/api/notebooks/{notebook_id}/{_EXPERIENCES}", headers=owner
    ).json()["can_manage"] is True


def test_empty_partition_reports_null_updated_at(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)

    body = client.get(
        f"/api/notebooks/{notebook_id}/{_EXPERIENCES}", headers=owner
    ).json()
    assert body == {
        "enabled": True,
        "count": 0,
        "updated_at": None,
        "can_manage": True,
        "entries": [],
    }


def test_updated_at_compares_real_instants_not_strings(tmp_path, monkeypatch):
    """带偏移的 ISO 文本不能按字典序比大小。

    ``08:00+00:00`` 比 ``09:00+08:00`` 晚了七小时,字典序却把后者排在前面。
    偏移会因为改时区、夏令时、或者一次跨环境同步而在同一个分区里出现两种,
    所以「最近更新」必须解析成真时刻再比。
    """
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)
    store = _experience_store()

    _seed_experience(
        store, notebook_id, "earlier", rationale="其实更早",
        when="2026-09-21T09:00:00+08:00",       # = 01:00Z
    )
    _seed_experience(
        store, notebook_id, "later", rationale="其实更晚",
        when="2026-09-21T08:00:00+00:00",       # = 08:00Z,字典序反而靠前
    )

    body = client.get(
        f"/api/notebooks/{notebook_id}/{_EXPERIENCES}", headers=owner
    ).json()
    # 顶层「最近更新」原样回那一条的原始字符串,不重新格式化。
    assert body["updated_at"] == "2026-09-21T08:00:00+00:00"
    # 同 support 时 ``updated_at desc`` 也按真时刻排。
    assert [entry["rationale"] for entry in body["entries"]] == [
        "其实更晚", "其实更早",
    ]


def test_an_unparseable_updated_at_sorts_last_and_never_becomes_the_newest(
    tmp_path, monkeypatch
):
    """解析不出时刻的行排最后,也绝不当「最近更新」。"""
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)
    store = _experience_store()

    _seed_experience(
        store, notebook_id, "ok", rationale="正常的一条",
        when="2026-09-21T09:00:00+08:00",
    )
    _seed_experience(
        store, notebook_id, "broken", rationale="时间戳坏掉的一条", when="不是时间",
    )

    body = client.get(
        f"/api/notebooks/{notebook_id}/{_EXPERIENCES}", headers=owner
    ).json()
    assert body["count"] == 2
    assert body["updated_at"] == "2026-09-21T09:00:00+08:00"
    assert [entry["rationale"] for entry in body["entries"]] == [
        "正常的一条", "时间戳坏掉的一条",
    ]


def test_read_width_is_twice_the_ceiling_so_truncation_follows_the_sort(
    tmp_path, monkeypatch
):
    """取数宽度是上限的**两倍**,截断跑在排序之后。

    淘汰在蒸馏写完之后才跑,分区因此可以短暂地多于 100 行。这时候让 store 按
    ``id``(内容哈希)序只给 100 行,截掉的是与 support 毫无关系的一批——界面
    会安安静静地丢掉这个库最被验证的几条经验。
    """
    from app.repositories.ports import RETRIEVAL_EXPERIENCE_NOTEBOOK_MAX_ENTRIES

    client = _client(tmp_path, monkeypatch)
    owner, _owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)
    store = _experience_store()
    captured: list = []
    real_read = type(store).read_partition

    def spy_read(self, partition, limit):
        captured.append((partition, limit))
        return real_read(self, partition, limit)

    monkeypatch.setattr(type(store), "read_partition", spy_read)

    got = client.get(f"/api/notebooks/{notebook_id}/{_EXPERIENCES}", headers=owner)
    assert got.status_code == 200, got.text
    assert captured == [(notebook_id, 2 * RETRIEVAL_EXPERIENCE_NOTEBOOK_MAX_ENTRIES)]


def test_an_over_ceiling_partition_shows_the_best_supported_hundred(
    tmp_path, monkeypatch
):
    """分区暂时超出上限时,露出来的是 support 最高的那 100 条。

    种子刻意让 support 与 id 序**反着走**:高 support 的那些 id 排在最后,
    先截后排的实现会把它们一条不剩地丢掉。
    """
    from app.repositories.ports import RETRIEVAL_EXPERIENCE_NOTEBOOK_MAX_ENTRIES

    cap = RETRIEVAL_EXPERIENCE_NOTEBOOK_MAX_ENTRIES
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)
    store = _experience_store()

    total = cap + 20
    for index in range(total):
        _seed_experience(
            store,
            notebook_id,
            f"e-{index:04d}",          # id 升序
            rationale=f"第 {index} 条",
            support=index + 1,         # support 也升序 —— 与 id 序正好相反的取舍
        )

    body = client.get(
        f"/api/notebooks/{notebook_id}/{_EXPERIENCES}", headers=owner
    ).json()
    assert store.count(notebook_id) == total, "淘汰还没跑,分区确实超了上限"
    assert body["count"] == cap
    assert len(body["entries"]) == cap
    # 头尾两条就足以说明截的是哪一端:support 最高的在最前,露出的最低一条是
    # 第 21 条(前 20 条被截掉),而不是 id 序的前 100 条。
    assert body["entries"][0]["support"] == total
    assert body["entries"][-1]["support"] == total - cap + 1


def test_experience_gate_closed_is_transparent_on_get_and_reads_nothing(
    tmp_path, monkeypatch
):
    """关闸时 ``GET`` 回 ``enabled=false`` 且**不查表也不查能力**。

    「不查表」不是性能修辞:关闸的语义是「完全回到接入前」,一次照查的读会
    让关掉的开关仍然按每次打开面板付一次查询。
    """
    client = _client(tmp_path, monkeypatch, retrieval_experience_enabled=False)
    owner, _owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)
    store = _experience_store()

    def refuse_read(self, partition, limit):  # pragma: no cover - 期望不被调用
        raise AssertionError("关闸时不应读分区")

    monkeypatch.setattr(type(store), "read_partition", refuse_read)

    got = client.get(f"/api/notebooks/{notebook_id}/{_EXPERIENCES}", headers=owner)
    assert got.status_code == 200, got.text
    assert got.json() == {
        "enabled": False,
        "count": 0,
        "updated_at": None,
        # owner 本来有 ``agent_profile:write``,但关闸已经决定了什么都不可做。
        "can_manage": False,
        "entries": [],
    }


def test_manual_distill_claims_the_single_flight_and_409s_while_busy(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)
    captured = _fake_submit(monkeypatch)

    started = client.post(
        f"/api/notebooks/{notebook_id}/{_EXPERIENCES}/distill", headers=owner
    )
    assert started.status_code == 200, started.text
    assert started.json() == {"started": True}
    # 真的走到了 claim→submit,并且带着**这个库**的分区 id(不是全局分区)。
    assert len(captured) == 1
    assert captured[0]["args"] == (notebook_id,)

    busy = client.post(
        f"/api/notebooks/{notebook_id}/{_EXPERIENCES}/distill", headers=owner
    )
    assert busy.status_code == 409, busy.text
    assert busy.headers.get("X-User-Message") == "1"
    # 与手动重建共用同一句——「已经有一次整理在跑」对按按钮的人是同一件事。
    assert busy.json()["detail"] == "正在整理，请稍候"


def test_manual_distill_409s_during_the_cooldown_with_its_own_wording(
    tmp_path, monkeypatch
):
    """刚整理过、又没有新提问 → 409,且是**第三句**话。

    这个按钮每按一次买一次有界模型调用;没有新输入的一批只会把同一堆 run 再
    读一遍、什么也写不出来。三种拒绝(关闸 / 忙 / 冷却)必须是三句不同的话,
    否则用户分不清「再等等」和「去提个问」。
    """
    import time

    client = _client(tmp_path, monkeypatch)
    owner, _owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)
    _fake_submit(monkeypatch)
    # 等价于「这个库刚跑完一批」,不真的跑一趟蒸馏(那要配模型)。
    _experience_jobs()._last_finished[notebook_id] = time.monotonic()

    cooling = client.post(
        f"/api/notebooks/{notebook_id}/{_EXPERIENCES}/distill", headers=owner
    )
    assert cooling.status_code == 409, cooling.text
    assert cooling.headers.get("X-User-Message") == "1"
    assert cooling.json()["detail"] == "刚整理过，暂时没有新的提问，请稍后再试"

    # 有新提问就放行——冷却挡的是「没有新输入还要付钱」,不是「刚跑过」。
    _experience_jobs().note_ask_completed(notebook_id)
    allowed = client.post(
        f"/api/notebooks/{notebook_id}/{_EXPERIENCES}/distill", headers=owner
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json() == {"started": True}


def test_manual_distill_409s_with_its_own_wording_when_the_gate_is_closed(
    tmp_path, monkeypatch
):
    """关闸与忙碌是两句话。

    ``distill_now`` 自己区分不出「路由还没判闸」这一层,所以把它们分开的是
    路由里那一句闸——没有它,一个关掉经验蒸馏的部署会告诉用户「正在整理」。
    """
    client = _client(tmp_path, monkeypatch, retrieval_experience_enabled=False)
    owner, _owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)

    refused = client.post(
        f"/api/notebooks/{notebook_id}/{_EXPERIENCES}/distill", headers=owner
    )
    assert refused.status_code == 409, refused.text
    assert refused.headers.get("X-User-Message") == "1"
    assert refused.json()["detail"] == "这项功能当前未开启，暂时无法整理"


def test_clear_removes_only_this_notebooks_slice(tmp_path, monkeypatch):
    """``DELETE`` 只碰本库分区。

    这条同时是**路由注册顺序**的回归:``/understanding/{label}`` 的路径参数
    正则照样匹配字面量 ``experiences``,若它排在前面,这个请求会落进
    ``clear_understanding_block`` 并因 label 不在词表里回 422。
    """
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)
    other_notebook = _notebook(client, owner, name="另一个库")
    store = _experience_store()

    _seed_experience(store, notebook_id, "mine-1")
    _seed_experience(store, notebook_id, "mine-2")
    _seed_experience(store, other_notebook, "theirs-1")
    _seed_experience(store, "", "global-1")

    cleared = client.delete(
        f"/api/notebooks/{notebook_id}/{_EXPERIENCES}", headers=owner
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json() == {"removed": 2}

    assert store.count(notebook_id) == 0
    assert store.count(other_notebook) == 1
    assert store.count("") == 1

    # 幂等:已经空了的分区再清一次是 0,不是错误。
    again = client.delete(
        f"/api/notebooks/{notebook_id}/{_EXPERIENCES}", headers=owner
    )
    assert again.status_code == 200, again.text
    assert again.json() == {"removed": 0}


def test_clear_still_works_while_the_gate_is_closed(tmp_path, monkeypatch):
    """关开关是「从现在起不再记」,不是「把记过的藏起来、还删不掉」。

    ``GET`` 在关闸时已经不再列出这些行,所以这条 ``DELETE`` 是用户把它们
    拿掉的唯一通道——它若也跟随总闸,这批行就成了既看不到也删不掉的东西。
    """
    client = _client(tmp_path, monkeypatch, retrieval_experience_enabled=False)
    owner, _owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)
    store = _experience_store()
    _seed_experience(store, notebook_id, "left-over")

    cleared = client.delete(
        f"/api/notebooks/{notebook_id}/{_EXPERIENCES}", headers=owner
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json() == {"removed": 1}
    assert store.count(notebook_id) == 0


def test_experience_writes_are_404_for_readers_and_strangers(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)
    reader, _reader_id = _readonly_member(client, owner, notebook_id)
    stranger, _stranger_id = _new_user(client)
    _fake_submit(monkeypatch)

    # 只读成员:能读,但两个管理动作一律 404(与共享底座写端点同形态,不泄露
    # 存在性),且**不因为按了哪个按钮**而变成 409——两个写端点都是先判权限。
    assert client.get(
        f"/api/notebooks/{notebook_id}/{_EXPERIENCES}", headers=reader
    ).status_code == 200
    denied_distill = client.post(
        f"/api/notebooks/{notebook_id}/{_EXPERIENCES}/distill", headers=reader
    )
    assert denied_distill.status_code == 404, denied_distill.text
    assert denied_distill.headers.get("X-User-Message") is None
    assert client.delete(
        f"/api/notebooks/{notebook_id}/{_EXPERIENCES}", headers=reader
    ).status_code == 404

    # 陌生人:三个端点一律 404(读守卫先拦)。
    assert client.get(
        f"/api/notebooks/{notebook_id}/{_EXPERIENCES}", headers=stranger
    ).status_code == 404
    assert client.post(
        f"/api/notebooks/{notebook_id}/{_EXPERIENCES}/distill", headers=stranger
    ).status_code == 404
    assert client.delete(
        f"/api/notebooks/{notebook_id}/{_EXPERIENCES}", headers=stranger
    ).status_code == 404


def test_experience_endpoints_do_not_follow_the_understanding_gate(
    tmp_path, monkeypatch
):
    """两把总闸相互独立:``AGENT_PROFILE_ENABLED`` 关着不影响检索经验。"""
    client = _client(
        tmp_path, monkeypatch, agent_profile_enabled=False,
        retrieval_experience_enabled=True,
    )
    owner, _owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)
    _seed_experience(_experience_store(), notebook_id, "still-here")

    body = client.get(
        f"/api/notebooks/{notebook_id}/{_EXPERIENCES}", headers=owner
    ).json()
    assert body["enabled"] is True
    assert body["count"] == 1
    assert body["can_manage"] is True
    # 理解块那一侧确实是关着的——两个断言放在同一条用例里,才说明这是「两把
    # 独立的闸」而不是「配置没生效」。
    assert client.get(
        f"/api/notebooks/{notebook_id}/understanding", headers=owner
    ).json()["enabled"] is False


def test_mirror_fence_refuses_both_writes_without_touching_can_manage(
    tmp_path, monkeypatch
):
    """镜像围栏只挡**写**,``can_manage`` 照旧是纯能力位。

    与 ``deps.py::notebook_mirror_fence`` 那条「只读投影不调」逐字一致,也与
    ``can_edit_base`` 同口径:镜像库上这个人的权限一点没少,少的是那个库此刻
    能不能被写——围栏该说的话由带 ``sync_origin`` 的 409 说,比一个消失的按钮
    准确。今天 ``_CAPABILITY_MIRROR_FENCE["agent_profile:write"]`` 是 False,
    所以这条用例把那一格翻成 True 再测:钉的是「翻过来之后两个写端点跟随、
    而读投影不跟随」,不是今天的行为。
    """
    from app.api import deps

    client = _client(tmp_path, monkeypatch)
    owner, _owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)
    monkeypatch.setitem(deps._CAPABILITY_MIRROR_FENCE, "agent_profile:write", True)
    monkeypatch.setattr(
        type(deps.notebook_access_repository()),
        "notebook_sync_origin",
        lambda self, nb: "prod-env",
    )

    body = client.get(
        f"/api/notebooks/{notebook_id}/{_EXPERIENCES}", headers=owner
    ).json()
    assert body["enabled"] is True
    assert body["can_manage"] is True

    refused_distill = client.post(
        f"/api/notebooks/{notebook_id}/{_EXPERIENCES}/distill", headers=owner
    )
    assert refused_distill.status_code == 409, refused_distill.text
    assert refused_distill.json()["detail"]["sync_origin"] == "prod-env"

    refused_clear = client.delete(
        f"/api/notebooks/{notebook_id}/{_EXPERIENCES}", headers=owner
    )
    assert refused_clear.status_code == 409, refused_clear.text
    assert refused_clear.json()["detail"]["sync_origin"] == "prod-env"
