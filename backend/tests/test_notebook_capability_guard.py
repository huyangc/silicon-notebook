"""P0-T2 建表 / P2-T2 首次翻格:按能力命名的 notebook 写守卫工厂
——``app.api.deps.require_notebook_capability``。

P0 阶段行为零变化(每个能力名都解析到既有的 owner-only 判定)。**P2-T2 兑现了这个
接缝**:六个能力从 ``"owner"`` 档翻到 ``"admin"`` 档(owner ∪ ``role='admin'`` 的有效
授权边),73 个端点声明一个字未动。这里钉的是:

  ① 未登记的能力名当场 ``KeyError``——响亮失败,不许延迟到请求期才暴露;
  ② 能力值域被冻结在 ``{"owner", "admin"}``,并**逐格**钉住哪六个翻了、哪两个没翻
     ——批量翻格最危险的失手形态是「顺手多翻一格」(尤其 ``notebook:delete``),
     一个只看值域集合的断言对它完全无感;
  ③ 每个能力名解析到**对应档位守卫的同一个函数对象**,且 ``@lru_cache`` 让同一能力名
     恒返回同一对象(P0 预埋的依赖去重从 P2 起真的开始兑现——两档现在返回不同对象);
  ④ 结构扫描:``backend/app/api/*.py`` 里不得再残留裸 ``require_notebook_access``
     (文本扫描,连注释一起拦),也不得以任何书写形态引用 ``require_notebook_write``
     / ``require_notebook_admin`` 标识符(AST 扫描——换行的 ``Depends(\n
     require_notebook_write)``、``import ... as`` 别名、中间变量赋值统统按标识符
     节点抓)——防止新端点绕过能力工厂、悄悄挂上某一档的裸守卫;
  ⑤ 行为抽查:挑 sources:write 与 notebook:manage 两个代表性端点,owner 通过守卫、
     只读成员 404、陌生人 404;外加 P2 新增的组管理员一列(通过)与
     ``notebook:delete``(仍 404)。

⚠ 结构扫描**不检查归类是否正确**:把读端点错挂写能力(或把 kg 端点挂成
knowhow:write)在结构上不可检测——守卫只保证"必经能力工厂",归类对错靠
任务级评审兜底。
"""
import ast
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


def test_notebook_capability_allowed_rejects_unknown_capability():
    """体内自查的纯函数版本与工厂吃同一张表:未知能力名同样当场 KeyError,
    不许静默落回某个默认判定。"""
    with pytest.raises(KeyError):
        deps.notebook_capability_allowed("no:such", "nb-x", "user-x")


# --------------------------------------------------------------------------
# ② 值域冻结 + 逐格冻结
# --------------------------------------------------------------------------

#: P2-T2 之后的**逐格**期望值。改这张表 = 声明一次权限边界变更,评审必须看见它。
#:
#: 翻成 "admin" 的六格是设计文档裁决 P2-1 点名的内容管理能力;留在 "owner" 的三格
#: 各有理由(见 deps.py 的表上注释):删库爆炸半径太大且 owner 无法撤销;
#: notebook:configure(挂载配置 + 链接分享)不随内容管理权转移(P2-T2 评审 P0);
#: reports:write 目前没有任何消费点,留给它真正长出消费点的那次改动去决定档位。
#:
#: agent_profile:write(Agentic Memory P1-T6 的「共享底座」写)是内容写,与
#: knowhow:write 同格 —— 理解块是这本库被用出来的内容,库主事后照样能改掉/清空它
#: (不是授予他人的持久授权,也不是挂载配置或删库),所以它属于 P2-1 点名的那一族,
#: 取 "admin"。它不新增第三档。
EXPECTED_CAPABILITY_LEVELS = {
    "sources:write": "admin",
    "kg:write": "admin",
    "knowhow:write": "admin",
    "knowledge:write": "admin",
    "catalog:write": "admin",
    "reports:write": "owner",
    "notebook:manage": "admin",
    "notebook:configure": "owner",
    "notebook:delete": "owner",
    "agent_profile:write": "admin",
}


def test_capability_value_domain_is_frozen():
    """值域冻结在 {"owner", "admin"}。

    P0 冻结在 {"owner"};P2-T2 加了第二档。再新增档位(比如某天的 "member")时这条
    断言必须跟着改——它就是那道提醒"这里也要跟进"的信号。
    """
    assert set(deps._CAPABILITY_LEVELS.values()) == {"owner", "admin"}


def test_capability_levels_are_pinned_cell_by_cell():
    """**逐格**冻结,不只是冻结值域。

    这条才是 P2 翻格的真正闸门:批量把六格从 owner 改成 admin 时,最容易的失手是
    顺手多翻一格——而 ``notebook:delete`` 翻掉的后果是「组管理员能删掉库主的整本
    笔记本,且库主无法撤销」。只看值域集合的断言对这种失手完全无感(集合还是那两个
    值),只有逐格比对才拦得住。反方向同理:漏翻一格会让 API 与界面/文档说的不一致。
    """
    assert deps._CAPABILITY_LEVELS == EXPECTED_CAPABILITY_LEVELS


def test_never_touched_capabilities_stay_owner_only():
    """点名钉住**没翻的那三格**,并说清为什么。

    与上一条不是重复:上一条是整表比对(改表就红),这一条把「这三格必须是 owner」
    这句话本身写成可执行的断言,并且带着理由 —— 它的失败信息直接告诉后来者动的是
    一条安全边界,而不是「更新一下期望表」。
    """
    assert deps._CAPABILITY_LEVELS["notebook:delete"] == "owner", (
        "删库恒 owner:爆炸半径是整本库,且 owner 事后无法撤销组管理员的删除"
    )
    assert deps._CAPABILITY_LEVELS["notebook:configure"] == "owner", (
        "挂载配置 + 链接分享恒 owner(P2-T2 评审 P0):mountable 会枚举库主全部私有库名、"
        "PUT bases 能把私有库挂进来经代理端点读全文、share 能替库主铸对外链接——"
        "这些是 owner 对本库检索范围与对外处置的配置,不随内容管理权翻给组管理员"
    )
    assert deps._CAPABILITY_LEVELS["reports:write"] == "owner", (
        "reports:write 目前无消费点(P1-T3b 起报告走行级 created_by 判定),"
        "档位留给它真正长出消费点的那次改动显式决定"
    )


def test_every_registered_capability_resolves_to_its_level_guard():
    """每个能力名解析回**对应档位守卫的同一个函数对象**。

    不是"另一份行为等价的实现",是同一个对象——工厂只做查表,判定各只有一份实现
    (未授权 → 404,不泄露存在性)。
    """
    expected = {
        "owner": deps.require_notebook_write,
        "admin": deps.require_notebook_admin,
    }
    for capability, level in deps._CAPABILITY_LEVELS.items():
        assert deps.require_notebook_capability(capability) is expected[level], (
            capability, level,
        )
    # 两档必须是**不同**的对象,否则「翻格」根本没有发生(整张表还解析到同一个守卫)。
    assert deps.require_notebook_write is not deps.require_notebook_admin


def test_capability_factory_returns_a_stable_object_per_capability():
    """同一个能力名**恒返回同一个对象**(``@lru_cache`` 的语义)。

    P0 加 ``@lru_cache`` 时它是 no-op(所有能力返回同一个 ``require_notebook_write``);
    P2 起两档返回不同对象,这一行才开始真的兑现:FastAPI 的每请求依赖缓存按 callable
    **身份**去重,不缓存的话同一路由声明两个能力依赖就会各拿一个新函数对象、多跑一次
    判定查询。判据必须是"同一能力名两次调用同一对象",而不是"同一档同一对象"——后者
    在工厂每次都新建闭包时也照样成立。
    """
    for capability in deps._CAPABILITY_LEVELS:
        assert deps.require_notebook_capability(
            capability
        ) is deps.require_notebook_capability(capability), capability


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
    files = sorted(p for p in _API_DIR.glob("*.py") if p.is_file())
    # 空转保护:能扫 0 个文件还报绿的守卫比没有守卫更糟——api 目录改名、本测试
    # 文件挪层都会让 glob 静默落空。routes.py(聚合器)与 deps.py 必然存在。
    scanned = {p.name for p in files}
    assert {"routes.py", "deps.py"} <= scanned, (
        f"api 目录扫描落空({_API_DIR}):只见 {sorted(scanned)[:5]}…——"
        "目录被改名/挪动时必须同步更新本守卫,不许让它静默全绿"
    )
    # 豁免名单里的文件必须真实存在:文件被改名后,豁免会悄悄变成无主条目,
    # 而它当年豁免的内容可能已在新名字的文件里重新违规。
    for exempt in _BARE_ACCESS_SCAN_EXEMPT | _DIRECT_WRITE_DEPENDS_SCAN_EXEMPT:
        assert exempt in scanned, f"豁免名单引用了不存在的文件: {exempt}"
    return files


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


#: 两档的裸守卫本体。它们仍然存在(能力工厂内部各复用一个),但路由文件一个都不许
#: 直接引用。``require_notebook_admin`` 从 P2-T2 起同样进这份名单——不进的话,新端点
#: 可以直接挂 ``Depends(require_notebook_admin)`` 拿到管理档,而这个选择既不进能力表、
#: 也不会出现在任何一份「哪些能力是 admin 档」的清单里。
_BARE_LEVEL_GUARDS = ("require_notebook_write", "require_notebook_admin")


@pytest.mark.parametrize("guard_name", _BARE_LEVEL_GUARDS)
def test_bare_level_guard_identifier_absent_from_route_files(guard_name):
    """两档的守卫本体都不得被路由文件以**任何书写形态**引用——必须一律经
    ``Depends(require_notebook_capability("<能力名>"))`` 声明,才谈得上"按能力
    归类"。

    用 AST 按标识符节点判而不是子串匹配:换行的 ``Depends(\n
    require_notebook_write,\n)``、``from ... import require_notebook_write as g``
    别名、``g = require_notebook_write`` 中间变量,子串扫描全都漏,AST 全都抓
    (Name / Attribute / import alias 三种节点)。"""
    offenders = []
    for path in _api_py_files():
        if path.name in _DIRECT_WRITE_DEPENDS_SCAN_EXEMPT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
        for node in ast.walk(tree):
            hit = (
                (isinstance(node, ast.Name) and node.id == guard_name)
                or (isinstance(node, ast.Attribute) and node.attr == guard_name)
                or (isinstance(node, ast.alias) and node.name == guard_name)
            )
            if hit:
                offenders.append(path.name)
                break
    assert offenders == [], (
        f"发现绕过能力工厂、直接引用 {guard_name} 的路由文件: {offenders}"
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
    """代表性抽查 #2:``notebook:manage``(PATCH /notebooks/{id} 改名)。

    改名是设计 §4 组管理员矩阵里明确 ✓ 的能力,所以 manage 的代表端点用它——
    ⚠ **不能**再用 mounted-by-count 了:P2-T2 评审把 mounted-by-count 归到了
    notebook:configure(恒 owner),那条现在测的是 configure 不是 manage(见下一条)。
    """
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "m00000001")
    nb = client.post("/api/notebooks", json={"name": "L"}, headers=owner_h).json()["id"]
    member_h = _login(client, "m00000002")
    member_id = client.get("/api/me", headers=member_h).json()["id"]
    deps.repository().add_member(nb, member_id)
    stranger_h = _login(client, "m00000003")

    owner_resp = client.patch(
        f"/api/notebooks/{nb}", json={"name": "L2"}, headers=owner_h
    )
    assert owner_resp.status_code == 200, owner_resp.text

    member_resp = client.patch(
        f"/api/notebooks/{nb}", json={"name": "X"}, headers=member_h
    )
    assert member_resp.status_code == 404

    stranger_resp = client.patch(
        f"/api/notebooks/{nb}", json={"name": "X"}, headers=stranger_h
    )
    assert stranger_resp.status_code == 404


def test_notebook_configure_capability_matches_owner_member_stranger_matrix(
    tmp_path, monkeypatch
):
    """代表性抽查 #3:``notebook:configure``(GET .../mounted-by-count,恒 owner)。

    P2-T2 评审 P0 从 notebook:manage 拆出的新格,解析到 **owner 档**。只读成员与
    陌生人一律 404,和翻格前的 mounted-by-count 行为逐字相同——这条钉的正是「拆出来
    单独命名之后,它对非 owner 仍然是 404」。
    """
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "c00000001")
    nb = client.post("/api/notebooks", json={"name": "L"}, headers=owner_h).json()["id"]
    member_h = _login(client, "c00000002")
    member_id = client.get("/api/me", headers=member_h).json()["id"]
    deps.repository().add_member(nb, member_id)
    stranger_h = _login(client, "c00000003")

    owner_resp = client.get(f"/api/notebooks/{nb}/mounted-by-count", headers=owner_h)
    assert owner_resp.status_code == 200, owner_resp.text
    assert owner_resp.json()["count"] == 0

    member_resp = client.get(f"/api/notebooks/{nb}/mounted-by-count", headers=member_h)
    assert member_resp.status_code == 404

    stranger_resp = client.get(
        f"/api/notebooks/{nb}/mounted-by-count", headers=stranger_h
    )
    assert stranger_resp.status_code == 404


# --------------------------------------------------------------------------
# ⑤ P2-T2:组管理员一列(能力翻转的行为面)
# --------------------------------------------------------------------------
#
# 这一节与上面两条抽查互补:那两条钉的是「owner 通过 / 只读成员与陌生人 404」这条
# **没有变**的基线,这一节钉的是**这次翻了什么**。两者必须同在——只加新一列会让
# 「顺手把所有人都放行了」照样全绿。


def _group_admin_world(client: TestClient, letter: str) -> dict:
    """一本 owner 的库 + 一个组:`deputy` 是组管理员且持 admin 边,`plain` 只是组员。

    ``letter`` 是这批用户名的首字母——注册校验的用户名形态是「单个小写字母 +
    八位数字」,所以每个用例挑一个自己的字母,免得跨用例撞名。

    边发成 `principal_type='group_admins'` + `role='admin'`,即产品里「共享给群组并
    勾选组管理员可管理」那条路径。`plain` 存在的意义是证明翻的是**管理边**而不是
    「进了组就能写」——少了他,一个把整组人都放行的实现照样全绿。
    """
    owner_h = _login(client, f"{letter}00000001")
    nb = client.post("/api/notebooks", json={"name": "L"}, headers=owner_h).json()["id"]

    deputy_h = _login(client, f"{letter}00000002")
    deputy_id = client.get("/api/me", headers=deputy_h).json()["id"]
    plain_h = _login(client, f"{letter}00000003")
    plain_id = client.get("/api/me", headers=plain_h).json()["id"]
    stranger_h = _login(client, f"{letter}00000004")

    group_id = client.post(
        "/api/groups", json={"name": "项目组"}, headers=owner_h
    ).json()["id"]
    for user_id, role in ((deputy_id, "admin"), (plain_id, "member")):
        assert client.put(
            f"/api/groups/{group_id}/members/{user_id}",
            json={"role": role},
            headers=owner_h,
        ).status_code == 200
    granted = client.post(
        f"/api/notebooks/{nb}/grants",
        json={
            "principal_type": "group_admins",
            "principal_id": group_id,
            "role": "admin",
        },
        headers=owner_h,
    )
    assert granted.status_code == 200, granted.text
    return {
        "notebook": nb,
        "group": group_id,
        "owner": owner_h,
        "deputy": deputy_h,
        "deputy_id": deputy_id,
        "plain": plain_h,
        "stranger": stranger_h,
        "grant_id": granted.json()["id"],
    }



def _seed_source(notebook_id: str) -> str:
    """直接写一行已解析的 `sources`,不跑真实上传/解析。

    只为让路径上带 `source_id` 的端点能区分「守卫拒绝」与「来源不存在」——两者都是
    404,不塞这行的话那一格恒等式成立、测不出任何东西。
    """
    from app.services.sqlite_repository import _now

    source_id = f"src-{notebook_id[-8:]}"
    with deps.repository()._write() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,file_name,file_path,file_size,"
            "parse_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (source_id, notebook_id, "S", "document", "s.md", "", 0, "parsed",
             _now(), _now()),
        )
    return source_id


def test_group_admin_passes_the_flipped_content_capabilities(tmp_path, monkeypatch):
    """组管理员通过全部**六个**翻了的能力,普通组员与陌生人一律 404。

    六个都点名跑一遍(而不是抽一个代表):`_CAPABILITY_LEVELS` 是一张表,漏翻一格
    在结构上完全看不出来——只有真的敲一次那个端点才知道它到底解析到哪一档。
    """
    client = _client(tmp_path, monkeypatch)
    w = _group_admin_world(client, "a")
    nb = w["notebook"]
    src = _seed_source(nb)

    # (能力名, 请求方法, 路径, owner/组管理员的期望码)
    #
    # 期望码不都是 200:守卫放行之后是真实业务逻辑,而这些端点在一本空库上各有各的
    # 合法回答(缺 embedding 配置、没有可构建的来源……)。**判据是「不是 404」**
    # ——404 是守卫拒绝的形态,别的码都说明请求已经越过守卫进了业务体。
    probes = [
        ("sources:write", "post", f"/api/notebooks/{nb}/backfill-vectors"),
        ("kg:write", "post", f"/api/notebooks/{nb}/kg/build"),
        ("knowhow:write", "post", f"/api/notebooks/{nb}/knowhow"),
        ("knowledge:write", "post", f"/api/notebooks/{nb}/object-schemas"),
        # 命令目录那条端点的路径上带 source_id,而「来源不存在」也回 404 —— 与守卫
        # 拒绝同码。所以先塞一行真来源(直接写库,不跑一次真实解析),让 owner 那半
        # 能越过 404 这个形态,否则这一格测的其实什么都不是。
        ("catalog:write", "post", f"/api/notebooks/{nb}/sources/{src}/command-catalog"),
        # ⚠ notebook:manage 的代表端点是 PATCH 改名(设计 §4 组管理员 ✓),**不是**
        # mounted-by-count —— 后者 P2-T2 评审归到了 notebook:configure(恒 owner),
        # 组管理员对它是 404,拿它当 manage 代表会把这一格测反。
        ("notebook:manage", "patch", f"/api/notebooks/{nb}"),
    ]
    for capability, method, path in probes:
        assert deps._CAPABILITY_LEVELS[capability] == "admin", capability
        for who in ("owner", "deputy"):
            resp = getattr(client, method)(path, json={}, headers=w[who]) if method in ("post", "patch") \
                else getattr(client, method)(path, headers=w[who])
            assert resp.status_code != 404, (capability, who, resp.status_code, resp.text)
        for who in ("plain", "stranger"):
            resp = getattr(client, method)(path, json={}, headers=w[who]) if method in ("post", "patch") \
                else getattr(client, method)(path, headers=w[who])
            assert resp.status_code == 404, (capability, who, resp.status_code, resp.text)


# notebook:manage 的代表端点(组管理员可达),各处「证明 deputy 确有管理权」共用它。
def _manage_probe(client: TestClient, nb: str, headers: dict):
    return client.patch(f"/api/notebooks/{nb}", json={"name": "probe"}, headers=headers)


def test_group_admin_still_cannot_delete_the_notebook(tmp_path, monkeypatch):
    """``notebook:delete`` **没翻**:组管理员对删库仍是 404,owner 才能删。

    这是本轮唯一一格「读得到、管得了、却动不了」的能力,也是翻格时最容易顺手带走的
    一格——它的失手后果不可撤销。
    """
    client = _client(tmp_path, monkeypatch)
    w = _group_admin_world(client, "b")
    nb = w["notebook"]

    assert deps._CAPABILITY_LEVELS["notebook:delete"] == "owner"
    # 先证明这位组管理员**确实**持有管理权(否则下面那个 404 可能只是因为他什么都不是)。
    assert _manage_probe(client, nb, w["deputy"]).status_code == 200
    assert client.delete(f"/api/notebooks/{nb}", headers=w["deputy"]).status_code == 404
    assert client.get(f"/api/notebooks/{nb}", headers=w["deputy"]).status_code == 200
    # 批 3·W1 PR-3:DELETE 现在是 202(tombstone CAS 立即返回,清理异步进行)。
    assert client.delete(f"/api/notebooks/{nb}", headers=w["owner"]).status_code == 202


def test_revoking_the_admin_grant_takes_effect_immediately(tmp_path, monkeypatch):
    """撤边即失能力:管理权是每次请求实时判定,不是一次性授予。"""
    client = _client(tmp_path, monkeypatch)
    w = _group_admin_world(client, "g")
    nb = w["notebook"]

    assert _manage_probe(client, nb, w["deputy"]).status_code == 200
    client.delete(f"/api/notebooks/{nb}/grants/{w['grant_id']}", headers=w["owner"])
    assert _manage_probe(client, nb, w["deputy"]).status_code == 404


def test_demoting_the_group_admin_takes_effect_immediately(tmp_path, monkeypatch):
    """组内降级同样即刻失能力——两根轴(边的 role、组成员的 role)任一为假即为假。"""
    client = _client(tmp_path, monkeypatch)
    w = _group_admin_world(client, "h")
    nb = w["notebook"]

    assert _manage_probe(client, nb, w["deputy"]).status_code == 200
    client.put(
        f"/api/groups/{w['group']}/members/{w['deputy_id']}",
        json={"role": "member"},
        headers=w["owner"],
    )
    assert _manage_probe(client, nb, w["deputy"]).status_code == 404


def test_a_viewer_grant_does_not_confer_content_capabilities(tmp_path, monkeypatch):
    """`role='viewer'` 的群组边只给读权,一个内容管理能力都不给。

    判的是**边自己的 role**,不是「他是不是组管理员」。这条与上面那条 world 的差别
    只有 `role` 一个字段——把 `group_admins` 主体误当成管理权的实现会在这里放行。
    """
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "f00000001")
    nb = client.post("/api/notebooks", json={"name": "L"}, headers=owner_h).json()["id"]
    deputy_h = _login(client, "f00000002")
    deputy_id = client.get("/api/me", headers=deputy_h).json()["id"]
    group_id = client.post(
        "/api/groups", json={"name": "项目组"}, headers=owner_h
    ).json()["id"]
    client.put(
        f"/api/groups/{group_id}/members/{deputy_id}",
        json={"role": "admin"},
        headers=owner_h,
    )
    client.post(
        f"/api/notebooks/{nb}/grants",
        json={
            "principal_type": "group_admins",
            "principal_id": group_id,
            "role": "viewer",
        },
        headers=owner_h,
    )

    assert client.get(f"/api/notebooks/{nb}", headers=deputy_h).status_code == 200
    # viewer 边不给 manage(改名)也不给内容写(backfill)。
    assert client.patch(
        f"/api/notebooks/{nb}", json={"name": "X"}, headers=deputy_h
    ).status_code == 404
    assert client.post(
        f"/api/notebooks/{nb}/backfill-vectors", headers=deputy_h
    ).status_code == 404


def test_group_admin_uploads_count_against_the_owners_document_quota(
    tmp_path, monkeypatch
):
    """裁决 P2-4:文档上限按 **owner** 算,不按请求者(组管理员)算。

    ⚠ P2-T2 评审 P2-5:旧版只比 `owner_view == deputy_view` 是**恒真**的——
    `document_limit` 由 `notebooks.created_by` 那位用户算,同一本库谁来看都一样,
    比相等什么都证明不了。这一版给 deputy 设一个**不同**的 per-user 覆盖上限,再断言
    deputy 看这本共享库时拿到的仍是**库主**的上限,而不是 deputy 自己的——只有「按
    owner 算」为真才成立,「误改成按请求者算」会当场红。
    """
    client = _client(tmp_path, monkeypatch)
    w = _group_admin_world(client, "e")
    nb = w["notebook"]

    owner_limit = client.get(
        f"/api/notebooks/{nb}", headers=w["owner"]
    ).json()["document_limit"]
    assert owner_limit > 0
    # 给 deputy 设一个与库主明显不同的个人上限(直接写 user_profiles,绕开 admin 门
    # ——这里要的只是「deputy 的个人上限 ≠ 库主的」这个前提)。
    deputy_limit = owner_limit + 7
    with deps.repository()._write() as db:
        db.execute(
            "UPDATE user_profiles SET upload_document_limit = ? WHERE user_id = ?",
            (deputy_limit, w["deputy_id"]),
        )
    # 自证覆盖真的生效:deputy 打开**自己 owner 的**库时看到的是 deputy_limit。
    own_nb = client.post(
        "/api/notebooks", json={"name": "deputy 自己的"}, headers=w["deputy"]
    ).json()["id"]
    assert client.get(f"/api/notebooks/{own_nb}", headers=w["deputy"]).json()[
        "document_limit"
    ] == deputy_limit

    # 关键断言:deputy 看**共享进来的**库时,上限仍是**库主**的,不是他自己的。
    deputy_view = client.get(f"/api/notebooks/{nb}", headers=w["deputy"]).json()
    assert deputy_view["document_limit"] == owner_limit, (
        "组管理员看共享库时,文档上限必须按**库主**算(误改成按请求者算会在这里红)"
    )
    assert deputy_view["document_limit"] != deputy_limit


# --------------------------------------------------------------------------
# ⑥ P2-T2 评审 P0:notebook:configure(挂载配置 + 链接分享)恒 owner
# --------------------------------------------------------------------------
#
# 组管理员有内容管理权(manage/内容写),但**没有** configure。configure 端点若跟着
# 翻 admin,组管理员就能:枚举库主全部私有库名(mountable)、把库主从未共享的私有库
# 挂进共享库经代理端点读全文(PUT bases)、替库主铸对外链接(POST share)、撤链接连带
# 踢只读成员(DELETE share)。这一节把这些端点对组管理员的 404 逐条钉死。


def test_group_admin_is_denied_every_configure_endpoint(tmp_path, monkeypatch):
    """组管理员对全部 configure 端点一律 404,owner 一律放行(不是 404)。"""
    client = _client(tmp_path, monkeypatch)
    w = _group_admin_world(client, "k")
    nb = w["notebook"]

    # (方法, 路径, POST/PUT 的 body)
    configure_probes = [
        ("get", f"/api/notebooks/{nb}/bases", None),
        ("get", f"/api/notebooks/{nb}/mountable", None),
        ("put", f"/api/notebooks/{nb}/bases", {"base_notebook_ids": []}),
        ("get", f"/api/notebooks/{nb}/mounted-by-count", None),
        ("get", f"/api/notebooks/{nb}/share", None),
        ("post", f"/api/notebooks/{nb}/share", None),
        ("delete", f"/api/notebooks/{nb}/share", None),
    ]
    for method, path, body in configure_probes:
        kwargs = {"headers": w["deputy"]}
        if body is not None:
            kwargs["json"] = body
        deputy_resp = getattr(client, method)(path, **kwargs)
        assert deputy_resp.status_code == 404, (
            method, path, deputy_resp.status_code, deputy_resp.text,
        )
        owner_kwargs = {"headers": w["owner"]}
        if body is not None:
            owner_kwargs["json"] = body
        owner_resp = getattr(client, method)(path, **owner_kwargs)
        assert owner_resp.status_code != 404, (
            method, path, owner_resp.status_code, owner_resp.text,
        )

    # deputy **确实**是组管理员(有 manage 权),证明上面的 404 不是「他什么都不是」。
    assert _manage_probe(client, nb, w["deputy"]).status_code == 200


def test_group_admin_cannot_enumerate_or_mount_the_owners_private_libraries(
    tmp_path, monkeypatch
):
    """P0 复现钉成回归用例:组管理员**读不到**库主的其它私有库、**挂不进来**。

    复现路径(基线 a27c6b18 之前会成功):Alice owns 共享库 N(admin 边给组,Bob 是
    组管理员)+ 一本**从未共享的**私有库 P。mountable 候选按 N 的 owner `a.created_by`
    解析、含「同 owner」支,所以候选集里**有** P(owner 视角能看到,证明泄露面真实
    存在)。Bob 若能调 mountable 就拿到 P 的库名;若能 PUT bases 就把 P 挂进 N、经
    active-notebook 代理端点读 P 全文。configure 恒 owner 之后:Bob 两条都 404。
    """
    client = _client(tmp_path, monkeypatch)
    w = _group_admin_world(client, "l")
    shared_nb = w["notebook"]

    # Alice(owner)另建一本**从未共享**的私有库 P。
    private_p = client.post(
        "/api/notebooks", json={"name": "Alice 的私有库"}, headers=w["owner"]
    ).json()["id"]

    # owner 视角:mountable 候选**确实**列出了 P(「同 owner」支)——泄露面真实存在。
    owner_mountable = client.get(
        f"/api/notebooks/{shared_nb}/mountable", headers=w["owner"]
    )
    assert owner_mountable.status_code == 200, owner_mountable.text
    assert private_p in {n["id"] for n in owner_mountable.json()}, (
        "前提失败:mountable 候选没有列出 owner 的私有库,这条测不到 P0 的泄露面"
    )

    # 组管理员 Bob:mountable 404(读不到库名枚举)。
    assert client.get(
        f"/api/notebooks/{shared_nb}/mountable", headers=w["deputy"]
    ).status_code == 404
    # 组管理员 Bob:把 P 挂进 N 也 404(挂不进来 → 代理端点无从读 P)。
    assert client.put(
        f"/api/notebooks/{shared_nb}/bases",
        json={"base_notebook_ids": [private_p]},
        headers=w["deputy"],
    ).status_code == 404
    # 兜底:即便 Bob 直接开 P 的详情也 404(P 从未共享给他,与本 P0 正交但一并钉)。
    assert client.get(f"/api/notebooks/{private_p}", headers=w["deputy"]).status_code == 404

    # owner 仍可正常挂载自己的库(configure 对 owner 照常放行)。
    ok = client.put(
        f"/api/notebooks/{shared_nb}/bases",
        json={"base_notebook_ids": [private_p]},
        headers=w["owner"],
    )
    assert ok.status_code == 200, ok.text
    assert private_p in {edge["id"] for edge in ok.json()}
