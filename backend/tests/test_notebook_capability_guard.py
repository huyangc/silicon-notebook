"""P0-T2:按能力命名的 notebook 写守卫工厂——``app.api.deps.require_notebook_capability``。

P0 阶段行为零变化(每个能力名都解析到既有的 owner-only 判定,与旧的
``require_notebook_access``/``require_notebook_write`` 逐字等价)。这里钉的是:

  ① 未登记的能力名当场 ``KeyError``——响亮失败,不许延迟到请求期才暴露;
  ② 能力值域被冻结在 ``{"owner"}``(P1/P2 群组授权落地时这条断言要跟着改,
     它就是那道"改这里"的提醒);
  ③ 结构扫描:``backend/app/api/*.py`` 里不得再残留裸 ``require_notebook_access``
     或直接 ``Depends(require_notebook_write)``——防止新端点绕过能力工厂、
     悄悄回退到裸守卫;
  ④ 行为等价抽查:挑 sources:write 与 notebook:manage 两个代表性端点,
     owner 通过守卫、只读成员 404、陌生人 404——与 T1 既有矩阵同口径。
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import deps


# --------------------------------------------------------------------------
# ① 未登记能力名 → 当场 KeyError
# --------------------------------------------------------------------------
def test_unknown_capability_raises_keyerror():
    """路由文件里 ``Depends(require_notebook_capability("拼错的名字"))`` 这类
    调用点在**模块 import 时**就会炸——不会被漏迁移的新端点悄悄接住、落到某个
    宽松默认值上。这里直接调用工厂本体验证同一件事,不依赖某个具体路由。"""
    with pytest.raises(KeyError):
        deps.require_notebook_capability("no:such")


# --------------------------------------------------------------------------
# ② 值域冻结
# --------------------------------------------------------------------------
def test_capability_value_domain_is_frozen_to_owner():
    """P0 冻结:``_CAPABILITY_LEVELS`` 的值域只有 "owner" 一档。P1/P2 群组
    授权扩展值域(比如新增 "group_admin")落地时,这条断言必须跟着改——它就是
    那道提醒"这里也要跟进"的信号,不是要求值域永远只有一种。"""
    assert set(deps._CAPABILITY_LEVELS.values()) == {"owner"}


def test_every_registered_capability_resolves_to_require_notebook_write():
    """P0 阶段:每个已登记的能力名都解析回**同一个** ``require_notebook_write``
    函数对象——不是"另一份行为等价的实现",是同一个对象,行为逐字相同
    (非 owner → 404,不泄露存在性)。"""
    for capability in deps._CAPABILITY_LEVELS:
        assert (
            deps.require_notebook_capability(capability) is deps.require_notebook_write
        ), capability


# --------------------------------------------------------------------------
# ③ 结构扫描:防止新端点绕过能力工厂回退到裸守卫
# --------------------------------------------------------------------------
_API_DIR = Path(__file__).resolve().parents[1] / "app" / "api"

# mcp_server.py 豁免于「裸 require_notebook_access」扫描:它现存的两处出现都是
# 解释性 docstring/注释("...dependency resolves to"、"rather than
# `require_notebook_access` (owner-only)"),对比的是 MCP 工具面另一套鉴权
# 机制(user_or_agent_scope / require_user_or_agent),不是本任务改动的对象——
# 任务红线明确写了 mcp_server.py 一律不碰。deps.py 自身豁免于两条扫描(它是
# require_notebook_write/require_notebook_capability 的定义处)。
_BARE_ACCESS_SCAN_EXEMPT = {"deps.py", "mcp_server.py"}
_DIRECT_WRITE_DEPENDS_SCAN_EXEMPT = {"deps.py"}


def _api_py_files() -> list[Path]:
    return sorted(p for p in _API_DIR.glob("*.py") if p.is_file())


def test_no_bare_require_notebook_access_outside_deps_and_mcp():
    """``require_notebook_access`` 别名已从 deps.py 删除——任何 api/*.py 文件
    (mcp_server.py 除外,见上面的豁免说明)里都不该再出现这个名字,包括
    import、``Depends(...)``、乃至只是文档字符串里的一句提及,否则要么是漏
    迁移的路由,要么是引用一个已不存在的名字的过期注释。"""
    offenders = []
    for path in _api_py_files():
        if path.name in _BARE_ACCESS_SCAN_EXEMPT:
            continue
        if "require_notebook_access" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert offenders == [], (
        f"require_notebook_access 已删除，以下文件仍引用它: {offenders}"
    )


def test_no_direct_depends_require_notebook_write_outside_deps():
    """``require_notebook_write`` 函数本体仍然存在(被能力工厂内部复用作
    "owner" 级的实现),但路由文件不得再直接写
    ``Depends(require_notebook_write)`` ——必须一律经
    ``Depends(require_notebook_capability("<能力名>"))`` 声明,才谈得上"按能力
    归类"。"""
    offenders = []
    for path in _api_py_files():
        if path.name in _DIRECT_WRITE_DEPENDS_SCAN_EXEMPT:
            continue
        if "Depends(require_notebook_write)" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert offenders == [], (
        f"发现绕过能力工厂、直接 Depends(require_notebook_write) 的路由: {offenders}"
    )


# --------------------------------------------------------------------------
# ④ 行为等价抽查(镜像 test_notebook_share_readonly.py 的 T1 矩阵写法)
# --------------------------------------------------------------------------
def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import app

    return TestClient(app)


def _login(client: TestClient, username: str, password: str = "pw123456") -> dict:
    client.post("/api/auth/register", json={"username": username, "password": password})
    tok = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def test_sources_write_capability_matches_owner_member_stranger_matrix(
    tmp_path, monkeypatch
):
    """代表性抽查 #1:``sources:write``(POST .../backfill-vectors)。

    owner 必须通过守卫(不是 404——具体业务状态取决于 embedding 是否配置,这里
    只断言"守卫放行"这一件事);只读成员与陌生人一律 404,不泄露存在性。"""
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "s00000001")
    nb = client.post("/api/notebooks", json={"name": "L"}, headers=owner_h).json()["id"]
    member_h = _login(client, "s00000002")
    member_id = client.get("/api/me", headers=member_h).json()["id"]
    deps.repository().add_member(nb, member_id)
    stranger_h = _login(client, "s00000003")

    owner_resp = client.post(f"/api/notebooks/{nb}/backfill-vectors", headers=owner_h)
    assert owner_resp.status_code == 200, owner_resp.text

    member_resp = client.post(f"/api/notebooks/{nb}/backfill-vectors", headers=member_h)
    assert member_resp.status_code == 404

    stranger_resp = client.post(
        f"/api/notebooks/{nb}/backfill-vectors", headers=stranger_h
    )
    assert stranger_resp.status_code == 404


def test_notebook_manage_capability_matches_owner_member_stranger_matrix(
    tmp_path, monkeypatch
):
    """代表性抽查 #2:``notebook:manage``(GET .../mounted-by-count)。"""
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "m00000001")
    nb = client.post("/api/notebooks", json={"name": "L"}, headers=owner_h).json()["id"]
    member_h = _login(client, "m00000002")
    member_id = client.get("/api/me", headers=member_h).json()["id"]
    deps.repository().add_member(nb, member_id)
    stranger_h = _login(client, "m00000003")

    owner_resp = client.get(f"/api/notebooks/{nb}/mounted-by-count", headers=owner_h)
    assert owner_resp.status_code == 200, owner_resp.text
    assert owner_resp.json()["count"] == 0

    member_resp = client.get(f"/api/notebooks/{nb}/mounted-by-count", headers=member_h)
    assert member_resp.status_code == 404

    stranger_resp = client.get(
        f"/api/notebooks/{nb}/mounted-by-count", headers=stranger_h
    )
    assert stranger_resp.status_code == 404
