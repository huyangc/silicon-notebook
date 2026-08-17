"""深度报告的行级归属矩阵(群组知识共享 P1-T3b)。

P1 只扩读权,**唯一**的例外是这里:共享笔记本里的成员(只读成员、或经群组
授权边可读的用户)可以建**自己的**深度报告。于是报告面的授权变成两层:

  ① notebook 层 —— `require_notebook_read`(owner ∪ 只读成员 ∪ 有效授权边);
  ② 行级 —— `reports.created_by == 当前用户`。

报告按创建者隔离(设计文档 §4 决策 1/5):别人的报告一律 **404**(与"不存在"
同一形态,不泄露存在性),notebook owner 也只看得见自己建的——刻意不引入
「owner 看全部」这条新披露。要给别人看,走既有的公开分享链接。

矩阵覆盖:
* 组授权 reader 建报告成功,`created_by` 是他自己;
* 成员 A 的报告对成员 B / notebook owner 的**每一个**操作都 404;
* 列表/导出只含自己的(过滤在 SQL 里,不是结果侧);
* owner 对自己报告的行为逐字不变(parity);
* 创建者发放的分享 token 照常服务免登录公开页(公开面不受行级影响);
* 陌生人(无读权)全 404;
* 成员的报告以**创建者身份**执行——私有 Memory 的既有 per-user 谓词因此
  解析到成员自己,而不是 notebook owner。
"""
import io
import zipfile

import pytest
from fastapi.testclient import TestClient


_PASSWORD = "pw12345678"
_USERNAMES = iter(f"r{index:08d}" for index in range(1, 999))


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for key in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
                "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL",
                "REASONING_LLM_MODEL"):
        monkeypatch.setenv(key, "")
    from app.main import app

    return TestClient(app)


@pytest.fixture
def stub_report_launch(monkeypatch):
    """报告端点的规划/生成后台 job 一律不真跑。

    返回捕获到的 `_launch_plan_job` 调用参数列表,供「以谁的身份执行」那条用例
    读取。"""
    import app.api.report_routes as routes_mod
    from app.api.deps import repository

    launched: list = []
    monkeypatch.setattr(routes_mod, "_report_llm_ready", lambda repo: True)
    monkeypatch.setattr(
        routes_mod, "_launch_plan_job",
        lambda *args, **kwargs: launched.append((args, kwargs)),
    )
    monkeypatch.setattr(routes_mod, "_launch_generate_job",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(repository()._runtime.models, "configured",
                        lambda _workload: True)
    return launched


def _new_user(client: TestClient) -> tuple[dict, str]:
    username = next(_USERNAMES)
    client.post("/api/auth/register",
                json={"username": username, "password": _PASSWORD})
    token = client.post(
        "/api/auth/login", json={"username": username, "password": _PASSWORD}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    return headers, client.get("/api/me", headers=headers).json()["id"]


def _me(client: TestClient, headers: dict) -> str:
    return client.get("/api/me", headers=headers).json()["id"]


def _notebook(client: TestClient, headers: dict, name: str = "库") -> str:
    return client.post("/api/notebooks", json={"name": name},
                       headers=headers).json()["id"]


def _create_report(client: TestClient, headers: dict, notebook_id: str,
                   question: str = "为什么?") -> str:
    response = client.post(f"/api/notebooks/{notebook_id}/reports",
                           json={"question": question}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["report_id"]


def _group_granted_reader(client: TestClient, owner: dict, notebook_id: str,
                          reader: dict | None = None,
                          reader_id: str = "") -> tuple[dict, str, str]:
    """建组 → 拉人进组 → 把库共享给该组,返回 (reader headers, reader id, group id)。

    传入 `reader`/`reader_id` 可以把一位已有用户拉进新建的组(撤授权后再恢复的
    场景要复用同一个人)。"""
    group_id = client.post("/api/groups", json={"name": "项目组"},
                           headers=owner).json()["id"]
    if reader is None:
        reader, reader_id = _new_user(client)
    assert client.put(
        f"/api/groups/{group_id}/members/{reader_id}",
        json={"role": "member"}, headers=owner,
    ).status_code == 200
    assert client.post(
        f"/api/notebooks/{notebook_id}/grants",
        json={"principal_type": "group", "principal_id": group_id,
              "role": "viewer"},
        headers=owner,
    ).status_code == 200
    return reader, reader_id, group_id


def _finish(notebook_id: str, report_id: str, content: str = "# 正文") -> None:
    """把报告推到 done(分享/导出的前置条件),绕开真实生成。"""
    from app.api.deps import repository

    repository().update_report(notebook_id, report_id, status="done",
                               progress="完成", content_md=content)


# 已存在报告的全部操作面。每条 = (HTTP 方法, URL 后缀, 请求体)。
_ROW_LEVEL_OPERATIONS = (
    ("get", "", None),
    ("post", "/intent", {"resolved_question": "q", "answers": []}),
    ("patch", "/outline", {"sections": [{"title": "T", "sub_queries": ["q"]}]}),
    ("post", "/generate", {}),
    ("post", "/cancel", {}),
    ("post", "/share", {}),
    ("get", "/share", None),
    ("delete", "/share", None),
    ("delete", "", None),          # 删除放最后:它会真的删掉行
)


def _operate(client: TestClient, headers: dict, notebook_id: str,
             report_id: str, method: str, suffix: str, body):
    return client.request(
        method.upper(),
        f"/api/notebooks/{notebook_id}/reports/{report_id}{suffix}",
        headers=headers,
        json=body,
    )


# --------------------------------------------------------------- 创建与归属


def test_group_granted_reader_creates_a_report_owned_by_himself(
    client, stub_report_launch
):
    """P1 唯一放宽的写面:对库只有读权的人可以建自己的报告,且行归他。

    `created_by` 必须是**请求用户**而不是 notebook owner——它是后面每一条行级
    判定的判据,写成 owner 会让整套隔离在第一步就失效(所有人的报告都归 owner,
    于是谁也看不见自己的、owner 看得见全部)。"""
    owner, owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)
    reader, reader_id, _ = _group_granted_reader(client, owner, notebook_id)

    report_id = _create_report(client, reader, notebook_id)

    from app.api.deps import repository
    row = repository().get_report(notebook_id, report_id)
    assert row["created_by"] == reader_id
    assert row["created_by"] != owner_id
    # 建完立刻可见于**自己**的列表与详情。
    listed = client.get(f"/api/notebooks/{notebook_id}/reports", headers=reader)
    assert [item["id"] for item in listed.json()] == [report_id]
    assert client.get(
        f"/api/notebooks/{notebook_id}/reports/{report_id}", headers=reader
    ).status_code == 200


def test_plain_readonly_member_can_also_create_a_report(client, stub_report_launch):
    """读权的两种来源(只读成员行 / 群组授权边)在这里必须等价——守卫读的是
    `require_notebook_read`,不是"有没有 notebook_members 行"。"""
    owner, _ = _new_user(client)
    notebook_id = _notebook(client, owner)
    member, member_id = _new_user(client)
    from app.api.deps import repository
    repository().add_member(notebook_id, member_id)

    report_id = _create_report(client, member, notebook_id)
    assert repository().get_report(notebook_id, report_id)["created_by"] == member_id


# ------------------------------------------------------------- 行级 404 矩阵


def test_another_members_report_is_404_on_every_operation(client, stub_report_launch):
    """成员 A 的报告对成员 B **和 notebook owner** 的每一个操作都 404。

    owner 也在被拒之列是刻意的(设计决策 1):共享库里的报告按创建者隔离,
    不引入"owner 看全部"这条新披露。404 而非 403 同样是刻意的——可区分的
    403 会把这些端点变成"owner 手上有哪些报告 id"的探针。"""
    owner, _ = _new_user(client)
    notebook_id = _notebook(client, owner)
    member_a, _, _ = _group_granted_reader(client, owner, notebook_id)
    member_b, member_b_id = _new_user(client)
    from app.api.deps import repository
    repository().add_member(notebook_id, member_b_id)

    report_id = _create_report(client, member_a, notebook_id)
    _finish(notebook_id, report_id)          # 让 share/export 的前置条件成立

    for intruder, label in ((member_b, "另一位成员"), (owner, "notebook owner")):
        for method, suffix, body in _ROW_LEVEL_OPERATIONS:
            response = _operate(client, intruder, notebook_id, report_id,
                                method, suffix, body)
            assert response.status_code == 404, (label, method, suffix,
                                                 response.status_code)

    # 行仍在:上面的 DELETE 必须是被守卫拦下,而不是"拦了个寂寞"——仓库层的
    # DELETE 自带 notebook 谓词但**不带** created_by,漏了行级判定它会静默成功
    # 并同样返回 200,两种结果在响应上分不出来。
    assert repository().get_report(notebook_id, report_id)["id"] == report_id


def test_intruder_write_attempts_leave_the_report_state_untouched(
    client, stub_report_launch
):
    """404 之外还要断言**没发生任何事**——防"闸挪到状态转换之后"的砖机形态。

    `generate` / `cancel` / `intent` 都会原子改写 `status`(claim/取消)。行级判定
    若挪到 `claim_report_generation` 或 `update_report(status='cancelled')` 之后,
    响应仍是 404,而报告已经被外人推进/取消/砖掉——创建者再也回不到
    `outline_ready`。所以这里用 **outline_ready** 的报告(三条路径都会真的动它),
    而不是 done 的(done 会在更早的状态闸上被弹回,弱化整条断言)。"""
    owner, _ = _new_user(client)
    notebook_id = _notebook(client, owner)
    member_a, _, _ = _group_granted_reader(client, owner, notebook_id)
    member_b, member_b_id = _new_user(client)
    from app.api.deps import repository
    repository().add_member(notebook_id, member_b_id)

    report_id = _create_report(client, member_a, notebook_id)
    repository().update_report(
        notebook_id, report_id, status="outline_ready", progress="大纲就绪",
        outline=[{"title": "T", "sub_queries": ["q"]}],
    )
    before = repository().get_report(notebook_id, report_id)
    assert before["status"] == "outline_ready"

    destructive = (
        ("post", "/generate", {}),
        ("post", "/cancel", {}),
        ("post", "/intent", {"resolved_question": "被劫持的问题", "answers": []}),
        ("patch", "/outline",
         {"sections": [{"title": "劫持", "sub_queries": ["x"]}]}),
    )
    for intruder, label in ((member_b, "另一位成员"), (owner, "notebook owner")):
        for method, suffix, body in destructive:
            response = _operate(client, intruder, notebook_id, report_id,
                                method, suffix, body)
            assert response.status_code == 404, (label, suffix)
            after = repository().get_report(notebook_id, report_id)
            assert after["status"] == before["status"], (label, suffix)
            assert after["progress"] == before["progress"], (label, suffix)
            assert after["outline"] == before["outline"], (label, suffix)
            assert after["question"] == before["question"], (label, suffix)

    # 创建者自己仍能正常推进——证明上面拦下的是入侵者,不是这条状态路径本身。
    assert _operate(client, member_a, notebook_id, report_id,
                    "post", "/generate", {}).status_code == 200


def test_a_report_id_from_another_notebook_is_404(client, stub_report_launch):
    """URL 上的 notebook 维度不是装饰:换一个**同样有读权**的 notebook_id 去拼
    同一个 report_id,必须 404。

    行级判定读的是 `get_report(notebook_id, report_id)` 返回的行,那条 SQL 的
    `notebook_id = ?` 谓词是"报告属于这个库"的唯一证明。删掉它,守卫仍会看到
    一个 `created_by` 对得上的行、照常放行——而请求声称的库是另一本。两种形态
    都要覆盖:同一用户的两个自有库(排除"其实是读守卫在拦"的解释),以及
    自有库报告 × 共享库 id。"""
    owner, _ = _new_user(client)
    own_a = _notebook(client, owner, "A")
    own_b = _notebook(client, owner, "B")
    report_in_a = _create_report(client, owner, own_a)

    # 形态一:两个库都归他,读守卫对 B 一定放行 → 只可能是 notebook 谓词在拦。
    assert client.get(f"/api/notebooks/{own_b}/reports/{report_in_a}",
                      headers=owner).status_code == 404
    assert client.get(f"/api/notebooks/{own_a}/reports/{report_in_a}",
                      headers=owner).status_code == 200

    # 形态二:自有库的报告 × 一本他有读权的共享库。
    librarian, _ = _new_user(client)
    shared = _notebook(client, librarian, "共享库")
    _group_granted_reader(client, librarian, shared, owner, _me(client, owner))
    assert client.get(f"/api/notebooks/{shared}/reports/{report_in_a}",
                      headers=owner).status_code == 404
    # 写路径同理(它们走同一个 helper,但 URL 拼错的形态值得各钉一条)。
    assert _operate(client, owner, shared, report_in_a,
                    "delete", "", None).status_code == 404
    assert client.get(f"/api/notebooks/{own_a}/reports/{report_in_a}",
                      headers=owner).status_code == 200


def test_a_stranger_is_404_on_creation_and_on_every_operation(
    client, stub_report_launch
):
    """无读权的人连第一层都过不去(与既有 notebook 读守卫同口径)。"""
    owner, _ = _new_user(client)
    notebook_id = _notebook(client, owner)
    stranger, _ = _new_user(client)
    report_id = _create_report(client, owner, notebook_id)
    _finish(notebook_id, report_id)

    assert client.post(f"/api/notebooks/{notebook_id}/reports",
                       json={"question": "q"},
                       headers=stranger).status_code == 404
    assert client.get(f"/api/notebooks/{notebook_id}/reports",
                      headers=stranger).status_code == 404
    for method, suffix, body in _ROW_LEVEL_OPERATIONS:
        response = _operate(client, stranger, notebook_id, report_id,
                            method, suffix, body)
        assert response.status_code == 404, (method, suffix, response.status_code)


# ----------------------------------------------------------- 列表 / 导出收窄


def test_list_and_export_only_ever_return_my_own_reports(client, stub_report_launch):
    """列表与导出按 `created_by` 收窄——两个方向都要断言:自己的在、别人的不在。

    导出对别人的 id 是**静默跳过**而不是 404:导出接受一个 id 列表,逐个报
    "存在但不是你的"同样是探针。全部被跳过时落到既有的 422(没有可导出的报告)。
    """
    owner, _ = _new_user(client)
    notebook_id = _notebook(client, owner)
    member, _, _ = _group_granted_reader(client, owner, notebook_id)

    owner_report = _create_report(client, owner, notebook_id, "owner 的问题")
    member_report = _create_report(client, member, notebook_id, "成员的问题")
    _finish(notebook_id, owner_report, "# owner 正文")
    _finish(notebook_id, member_report, "# 成员正文")

    owner_list = client.get(f"/api/notebooks/{notebook_id}/reports",
                            headers=owner).json()
    member_list = client.get(f"/api/notebooks/{notebook_id}/reports",
                             headers=member).json()
    assert [item["id"] for item in owner_list] == [owner_report]
    assert [item["id"] for item in member_list] == [member_report]

    # 导出:传两个 id,只拿得回自己的那份。
    both = {"report_ids": [owner_report, member_report]}
    member_zip = client.post(f"/api/notebooks/{notebook_id}/reports/export",
                             json=both, headers=member)
    assert member_zip.status_code == 200
    with zipfile.ZipFile(io.BytesIO(member_zip.content)) as archive:
        names = archive.namelist()
        assert len(names) == 1 and member_report in names[0]
        assert archive.read(names[0]).decode() == "# 成员正文"

    owner_zip = client.post(f"/api/notebooks/{notebook_id}/reports/export",
                            json=both, headers=owner)
    with zipfile.ZipFile(io.BytesIO(owner_zip.content)) as archive:
        names = archive.namelist()
        assert len(names) == 1 and owner_report in names[0]

    # 只点名别人的报告 → 一份都导不出 → 既有的 422。
    only_others = client.post(
        f"/api/notebooks/{notebook_id}/reports/export",
        json={"report_ids": [owner_report]}, headers=member,
    )
    assert only_others.status_code == 422


# ------------------------------------------------------------------- parity


def test_owner_lifecycle_on_their_own_reports_is_unchanged(client, stub_report_launch):
    """历史数据零变化:现存报告全是 owner 建的,owner 视角必须逐字不变。"""
    owner, _ = _new_user(client)
    notebook_id = _notebook(client, owner)
    report_id = _create_report(client, owner, notebook_id)

    assert client.get(f"/api/notebooks/{notebook_id}/reports/{report_id}",
                      headers=owner).status_code == 200
    assert [item["id"] for item in client.get(
        f"/api/notebooks/{notebook_id}/reports", headers=owner).json()] == [report_id]
    # 未完成的报告不能分享(既有 409,不是被行级判定改写成 404)。
    assert _operate(client, owner, notebook_id, report_id,
                    "post", "/share", {}).status_code == 409
    _finish(notebook_id, report_id)
    token = _operate(client, owner, notebook_id, report_id,
                     "post", "/share", {}).json()["share_token"]
    assert token
    assert _operate(client, owner, notebook_id, report_id,
                    "get", "/share", None).json()["share_token"] == token
    assert _operate(client, owner, notebook_id, report_id,
                    "delete", "/share", None).status_code == 204
    assert _operate(client, owner, notebook_id, report_id,
                    "post", "/cancel", {}).status_code == 200
    assert _operate(client, owner, notebook_id, report_id,
                    "delete", "", None).status_code == 200
    assert client.get(f"/api/notebooks/{notebook_id}/reports/{report_id}",
                      headers=owner).status_code == 404


def test_a_member_runs_the_full_lifecycle_on_their_own_report(
    client, stub_report_launch
):
    """成员对**自己的**报告拥有与 owner 相同的一整套操作。"""
    owner, _ = _new_user(client)
    notebook_id = _notebook(client, owner)
    member, _, _ = _group_granted_reader(client, owner, notebook_id)
    report_id = _create_report(client, member, notebook_id)
    _finish(notebook_id, report_id)

    assert _operate(client, member, notebook_id, report_id,
                    "post", "/share", {}).status_code == 200
    assert _operate(client, member, notebook_id, report_id,
                    "get", "/share", None).status_code == 200
    assert _operate(client, member, notebook_id, report_id,
                    "delete", "/share", None).status_code == 204
    assert _operate(client, member, notebook_id, report_id,
                    "post", "/cancel", {}).status_code == 200
    assert _operate(client, member, notebook_id, report_id,
                    "delete", "", None).status_code == 200


# ------------------------------------------------------------------- 公开页


def test_the_creators_share_token_still_serves_the_anonymous_page(
    client, stub_report_launch
):
    """行级判定是**产品层**的,不落到免登录公开面上:创建者(哪怕只是成员)
    发放的 token 照常服务任何人,包括 notebook owner 与陌生人。"""
    owner, _ = _new_user(client)
    notebook_id = _notebook(client, owner)
    member, _, _ = _group_granted_reader(client, owner, notebook_id)
    report_id = _create_report(client, member, notebook_id)
    _finish(notebook_id, report_id, "# 成员写的报告")

    token = _operate(client, member, notebook_id, report_id,
                     "post", "/share", {}).json()["share_token"]
    anonymous = client.get(f"/api/public/reports/{token}")
    assert anonymous.status_code == 200
    assert anonymous.json()["content_md"] == "# 成员写的报告"
    # 撤销仍只有创建者做得到,撤销后与从未存在的 token 同为 404。
    assert _operate(client, owner, notebook_id, report_id,
                    "delete", "/share", None).status_code == 404
    assert client.get(f"/api/public/reports/{token}").status_code == 200
    assert _operate(client, member, notebook_id, report_id,
                    "delete", "/share", None).status_code == 204
    assert client.get(f"/api/public/reports/{token}").status_code == 404


def test_public_link_dies_the_moment_the_creator_loses_read_access(
    client, stub_report_launch
):
    """成员发布的公开链接只在他对该库仍有读权时有效(P1-T3b 裁决)。

    真机形态:成员建报告 → 发公开 token → 被撤授权。此后成员被读守卫挡在库外、
    owner 被行级判定挡在报告外,**谁都够不到 unshare**,而公开页会永远 200 服务
    owner 的语料。裁决不是收回成员的发布权(设计 §4 拍板「分享走既有公开链接」),
    也不是在每个撤销点做级联(撤销点有好几个,级联要处处记账),而是把「挂载
    实时判定」那套哲学延伸过来:公开页每次请求复核创建者的**当前**读权。

    三条失权路径各来一遍——它们在数据层是三件不同的事(授权边没了 / 人不在组里
    了 / 组没了),只测一条会漏掉另外两条。"""
    from app.api.deps import repository

    def _published(owner_headers, notebook_id, member_headers):
        report_id = _create_report(client, member_headers, notebook_id)
        _finish(notebook_id, report_id, "# 成员发布的正文")
        token = _operate(client, member_headers, notebook_id, report_id,
                         "post", "/share", {}).json()["share_token"]
        assert client.get(f"/api/public/reports/{token}").status_code == 200
        return report_id, token

    # ① 撤掉授权边。
    owner, _ = _new_user(client)
    nb = _notebook(client, owner)
    member, member_id, group_id = _group_granted_reader(client, owner, nb)
    _report_id, token = _published(owner, nb, member)
    grant_id = client.get(f"/api/notebooks/{nb}/grants",
                          headers=owner).json()[0]["id"]
    assert client.delete(f"/api/notebooks/{nb}/grants/{grant_id}",
                         headers=owner).status_code in (200, 204)
    assert client.get(f"/api/public/reports/{token}").status_code == 404
    # 恢复授权 → 链接复活(token 幂等语义不因这道闸被破坏)。
    assert client.post(
        f"/api/notebooks/{nb}/grants",
        json={"principal_type": "group", "principal_id": group_id,
              "role": "viewer"},
        headers=owner,
    ).status_code == 200
    assert client.get(f"/api/public/reports/{token}").status_code == 200

    # ② 把人踢出群组(授权边还在,但他不再是组成员)。
    assert client.delete(f"/api/groups/{group_id}/members/{member_id}",
                         headers=owner).status_code in (200, 204)
    assert client.get(f"/api/public/reports/{token}").status_code == 404
    assert client.put(f"/api/groups/{group_id}/members/{member_id}",
                      json={"role": "member"}, headers=owner).status_code == 200
    assert client.get(f"/api/public/reports/{token}").status_code == 200

    # ③ 删掉整个群组(授权边随之清掉)。
    assert client.delete(f"/api/groups/{group_id}",
                         headers=owner).status_code in (200, 204)
    assert client.get(f"/api/public/reports/{token}").status_code == 404
    # 失权期间与无效 token 不可区分:同样的 404,同样的 detail。
    unknown = client.get("/api/public/reports/rshr-never-issued")
    revoked = client.get(f"/api/public/reports/{token}")
    assert unknown.status_code == revoked.status_code == 404
    assert unknown.json() == revoked.json()
    # 报告行本身没有被动过——这是一道读闸,不是级联删除。
    assert repository().get_report(nb, _report_id)["status"] == "done"


def test_the_creator_gate_never_touches_owner_reports_or_the_payload(
    client, stub_report_launch
):
    """owner 自己发布的链接不受这道闸影响(创建者=owner,读权恒成立),
    且闸用的两列内部 id 绝不能跟着爬进公开响应。"""
    owner, _ = _new_user(client)
    nb = _notebook(client, owner)
    report_id = _create_report(client, owner, nb)
    _finish(nb, report_id, "# owner 的正文")
    token = _operate(client, owner, nb, report_id,
                     "post", "/share", {}).json()["share_token"]

    # 无关的群组变动(建组、共享、撤销)一次都不该影响 owner 的链接。
    _member, _member_id, group_id = _group_granted_reader(client, owner, nb)
    assert client.get(f"/api/public/reports/{token}").status_code == 200
    assert client.delete(f"/api/groups/{group_id}",
                         headers=owner).status_code in (200, 204)
    public = client.get(f"/api/public/reports/{token}")
    assert public.status_code == 200
    assert public.json()["content_md"] == "# owner 的正文"

    # 判定读的是 notebook_id/created_by,两者都必须留在服务端。
    body = public.json()
    assert "notebook_id" not in body and "created_by" not in body
    assert nb not in public.text


# --------------------------------------------------------------- 结构守卫


def test_every_report_id_route_calls_the_row_level_gate():
    """凡路径含 `{report_id}` 的认证端点,函数体内必须出现 `_own_report_or_404`。

    行级判定是**体内**的,没法像 `Depends(...)` 那样在声明处看见——P2 加一个
    "重新生成"或"导出单份"端点时,漏挂不会有任何报错,只会安静地让任何读权用户
    操作别人的报告。AST 扫描(形状仿 test_notebook_capability_guard):按装饰器
    路径字面量选端点,按 Name 节点判是否调用了守卫。
    公开分享端点挂在 `public_router` 上、路径里也没有 `{report_id}`(token 就是
    全部授权),按 router 显式豁免。"""
    import ast
    from pathlib import Path

    path = (Path(__file__).resolve().parents[1] / "app" / "api"
            / "report_routes.py")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)

    checked: list[str] = []
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if not (isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and isinstance(decorator.func.value, ast.Name)):
                continue
            router_name = decorator.func.value.id
            route = decorator.args[0] if decorator.args else None
            if not (isinstance(route, ast.Constant)
                    and isinstance(route.value, str)):
                continue
            if "{report_id}" not in route.value:
                continue
            if router_name == "public_router":      # 匿名面:token 即授权
                continue
            checked.append(node.name)
            calls_gate = any(
                isinstance(inner, ast.Name) and inner.id == "_own_report_or_404"
                for inner in ast.walk(node)
            )
            if not calls_gate:
                offenders.append(node.name)

    # 空转保护:扫到 0 个端点却报绿,比没有守卫更糟(路由文件改名/装饰器换写法)。
    assert len(checked) >= 9, f"只扫到 {len(checked)} 个 {{report_id}} 端点: {checked}"
    assert offenders == [], (
        f"以下 {{report_id}} 端点没有调用行级守卫 _own_report_or_404: {offenders}"
    )


# --------------------------------------------------------------- 执行身份


def test_a_members_report_executes_under_the_creators_identity(client, monkeypatch):
    """成员的报告以**成员自己**的身份跑检索,不是 notebook owner 的。

    这条决定私有 Memory 的归属:报告引擎把 `user_id` 一路带给
    `notebook_memory_hits(user_id, ...)`,而该查询按 `memory_items.created_by`
    收窄(见 test_memory_retrieval.py 的 bob/alice 矩阵)。身份若取成 owner,
    共享库里每位成员的报告都会读到 owner 的私有 Memory——检索侧一个字都不用
    改,错只会错在这里传进去的身份上。来源范围的隐藏参与者冻结同理
    (`_validate_source_scope` 自己取 `current_user()`)。

    刻意钉在 `report_execution.start_plan` 收到的 `user_id` 上,而不是在测试里
    重算一遍同样的表达式:那样只能证明"我和被测代码算得一样"。"""
    import app.api.report_routes as routes_mod
    from app.api.deps import repository

    captured: dict = {}

    def _capture_start_plan(*args, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(routes_mod, "_report_llm_ready", lambda repo: True)
    monkeypatch.setattr(repository()._runtime.report_execution, "start_plan",
                        _capture_start_plan)

    owner, owner_id = _new_user(client)
    notebook_id = _notebook(client, owner)
    member, member_id, _ = _group_granted_reader(client, owner, notebook_id)

    _create_report(client, member, notebook_id)

    assert captured["user_id"] == member_id
    assert captured["user_id"] != owner_id
