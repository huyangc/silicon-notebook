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
#: 翻成 "admin" 的六格是设计文档裁决 P2-1 点名的内容管理能力;留在 "owner" 的四格
#: 各有理由(见 deps.py 的表上注释):删库爆炸半径太大且 owner 无法撤销;
#: notebook:configure(链接分享)与 notebook:mount(挂载配置)不随内容管理权转移
#: (P2-T2 评审 P0);reports:write 目前没有任何消费点,留给它真正长出消费点的那次
#: 改动去决定档位。
#:
#: agent_profile:write(Agentic Memory P1-T6 的「共享底座」写)是内容写,与
#: knowhow:write 同格 —— 理解块是这本库被用出来的内容,库主事后照样能改掉/清空它
#: (不是授予他人的持久授权,也不是挂载配置或删库),所以它属于 P2-1 点名的那一族,
#: 取 "admin"。它不新增第三档。
#:
#: 跨环境同步 §5 又拆出三格,**级别一个字没变**(拆前拆后同一批端点解析到同一道级别
#: 守卫):`notebook:grant`(admin)从 notebook:manage 拆出授权边端点,
#: `notebook:mount`(owner)从 notebook:configure 拆出挂载配置端点,
#: `scale_index:write`(admin)从 kg:write 拆出检索索引的重建与取消。拆的轴是下面那张
#: `EXPECTED_MIRROR_FENCE`——那三个旧能力名各自混着「改同步层内容」与「改目标端自有
#: 状态」两类端点,一格答不了两件事。
EXPECTED_CAPABILITY_LEVELS = {
    "sources:write": "admin",
    "kg:write": "admin",
    "knowhow:write": "admin",
    "knowledge:write": "admin",
    "catalog:write": "admin",
    "scale_index:write": "admin",
    "reports:write": "owner",
    "notebook:manage": "admin",
    "notebook:grant": "admin",
    "notebook:configure": "owner",
    "notebook:mount": "owner",
    "notebook:delete": "owner",
    "agent_profile:write": "admin",
}

#: 镜像写入围栏的**逐格**期望值(跨环境增量同步,docs/incremental-sync-design.md §5)。
#: True = 这个能力的端点会改写同步层内容,镜像笔记本上必须 409。
#:
#: 与 `EXPECTED_CAPABILITY_LEVELS` 同款理由逐格钉死:围栏是一张表,漏挡一格在结构上
#: 完全看不出来,而漏挡的后果是「用户在镜像上改了东西、下一次导入静默抹掉」——一种
#: 不报错的数据丢失。反方向(多挡一格)同样要拦:把 `notebook:grant` 挡掉会让目标端
#: 再也不能把镜像共享给自己的同事,把 `scale_index:write` 挡掉会让镜像的检索索引永远
#: 停在导入那一刻且无从修复,而设计 §5/§6 明确这两项都放行。
#:
#: ⚠ True 只对**非安全方法**生效:围栏表答的是「这个能力的**写**端点会不会改同步层
#: 内容」,GET/HEAD/OPTIONS 由包装依赖一律放行(`_SAFE_METHODS`)。所以这张表里的 True
#: 读作「这一格里的写端点要挡」,不是「这一格里的一切请求都挡」——下面
#: `test_safe_methods_under_a_fenced_capability_are_never_mirrored` 按真实路由表钉住
#: 这一点。
EXPECTED_MIRROR_FENCE = {
    "sources:write": True,
    "kg:write": True,
    "knowhow:write": True,
    "knowledge:write": True,
    "catalog:write": True,
    "scale_index:write": False,
    "reports:write": False,
    "notebook:manage": True,
    "notebook:grant": False,
    "notebook:configure": False,
    "notebook:mount": True,
    "notebook:delete": True,
    "agent_profile:write": False,
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
        "链接分享恒 owner(P2-T2 评审 P0):share 能替库主铸对外链接、撤链接连带踢掉"
        "全部只读成员——这是 owner 对本库对外处置的配置,不随内容管理权翻给组管理员"
    )
    assert deps._CAPABILITY_LEVELS["notebook:mount"] == "owner", (
        "挂载配置恒 owner(P2-T2 评审 P0,拆格后原样继承):mountable 会枚举库主全部"
        "私有库名、PUT bases 能把私有库挂进来经代理端点读全文——它是 owner 对本库"
        "检索范围的配置,不随内容管理权翻给组管理员"
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
        if deps._CAPABILITY_MIRROR_FENCE[capability]:
            # 带围栏的能力返回的是**包装依赖**而不是裸档位守卫(它要在档位守卫之后
            # 再判一次镜像状态)。包装是加法:档位判定仍然只有档位守卫那一份,由下面
            # 的行为矩阵用例证明它逐字沿用(未授权仍 404)。
            continue
        assert deps.require_notebook_capability(capability) is expected[level], (
            capability, level,
        )
    # 两档必须是**不同**的对象,否则「翻格」根本没有发生(整张表还解析到同一个守卫)。
    assert deps.require_notebook_write is not deps.require_notebook_admin


# --------------------------------------------------------------------------
# ②b 镜像写入围栏:键集合 + 逐格冻结(跨环境同步 §5)
# --------------------------------------------------------------------------
def test_mirror_fence_covers_exactly_the_capability_table():
    """两张表的**键集合必须相等**。

    这是围栏最容易的失手形态:加了一个新能力名却忘了在围栏表里登记它。工厂会当场
    KeyError(所以漏登记不会静默放行),但那是模块 import 时才炸;这条断言在 CI 上先
    一步说清楚缺的是哪一格,而不是让人从一条 KeyError 回溯去猜。
    反方向同样拦:围栏表里留着一个能力表已经删掉的名字,就是一条无主登记。
    """
    assert set(deps._CAPABILITY_MIRROR_FENCE) == set(deps._CAPABILITY_LEVELS)


def test_mirror_fence_is_pinned_cell_by_cell():
    """**逐格**冻结围栏,不只是冻结键集合。

    漏挡一格的后果是**不报错的数据丢失**(用户在镜像上改了东西,下一次导入静默抹掉);
    多挡一格的后果是把设计 §5 明确放行的目标端自有操作(授权、链接分享、报告、理解
    底座)也一起堵死。两个方向都只有整表比对拦得住。
    """
    assert deps._CAPABILITY_MIRROR_FENCE == EXPECTED_MIRROR_FENCE


def test_mirror_fence_values_are_booleans():
    """值域是 bool,不是「真值」。

    写成 `"yes"` / 非空字符串这类真值同样能跑,但它会让这张表从「一个二值归属」滑成
    「随便放点什么」——而这张表的全部意义就是让归属一眼可数。
    """
    assert all(
        isinstance(value, bool) for value in deps._CAPABILITY_MIRROR_FENCE.values()
    )


def test_fenced_capability_factory_returns_a_stable_object():
    """带围栏的能力同样**恒返回同一个对象**。

    FastAPI 按 callable 身份做每请求依赖去重。包装依赖是工厂现场造出来的闭包,
    没有 `@lru_cache` 的话每次调用都是一个新对象——同一路由声明两个能力依赖就会各跑
    一遍级别查询**和**一遍 sync_origin 查询。这条与上面那条同名断言互补:那条覆盖裸
    守卫,这条覆盖新的包装路径(裸守卫本来就是模块级单例,包装不是)。
    """
    fenced = [c for c, on in deps._CAPABILITY_MIRROR_FENCE.items() if on]
    assert fenced, "围栏表全空,这条用例就什么都没测到"
    for capability in fenced:
        guard = deps.require_notebook_capability(capability)
        assert guard is deps.require_notebook_capability(capability), capability
        # 不同能力名之间必须是**不同**对象:共用一个闭包等于能力名没有进包装,
        # 将来某一格单独改判据时会连带改掉别的格。
        assert guard is not deps.require_notebook_write, capability
        assert guard is not deps.require_notebook_admin, capability
    assert len({id(deps.require_notebook_capability(c)) for c in fenced}) == len(fenced)


def test_mirror_fence_helper_rejects_unknown_capability():
    """`notebook_mirror_fence` 与另外两条同口径:未知能力名当场 KeyError,
    不许静默落回「不挡」。"""
    with pytest.raises(KeyError):
        deps.notebook_mirror_fence("no:such", "nb-x")


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
    ⚠ **不能**再用 mounted-by-count 了:P2-T2 评审把它归到了恒 owner 的那一族,
    跨环境同步 §5 又把那一族拆成了 notebook:mount,两次都不是 manage(见下一条)。
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
    """代表性抽查 #3:``notebook:mount``(GET .../mounted-by-count,恒 owner)。

    P2-T2 评审 P0 先从 notebook:manage 拆出 notebook:configure,跨环境同步 §5 又把
    挂载配置从它里面拆成 notebook:mount;**两次拆分都没动级别**,这条端点自始至终
    解析到 **owner 档**。只读成员与陌生人一律 404,和两次拆分之前逐字相同——这条钉的
    正是「反复拆出来单独命名之后,它对非 owner 仍然是 404」。
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
        # mounted-by-count —— 后者归在恒 owner 的 notebook:mount 下,组管理员对它是
        # 404,拿它当 manage 代表会把这一格测反。
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
# ⑥ P2-T2 评审 P0:挂载配置(notebook:mount)与链接分享(notebook:configure)恒 owner
# --------------------------------------------------------------------------
#
# 组管理员有内容管理权(manage/内容写),但这两格一格都没有。它们若跟着翻 admin,
# 组管理员就能:枚举库主全部私有库名(mountable)、把库主从未共享的私有库挂进共享库
# 经代理端点读全文(PUT bases)、替库主铸对外链接(POST share)、撤链接连带踢只读成员
# (DELETE share)。这一节把这些端点对组管理员的 404 逐条钉死——**跨环境同步 §5 把它们
# 拆成两个能力名之后,这批 404 一个字都没变**,正是这一节还在原样绿着所证明的。


def test_group_admin_is_denied_every_configure_endpoint(tmp_path, monkeypatch):
    """组管理员对全部挂载配置 / 链接分享端点一律 404,owner 一律放行(不是 404)。

    这批端点现在分属 `notebook:mount`(挂载)与 `notebook:configure`(链接分享)两格,
    但**级别同为 owner**,所以断言矩阵与拆分之前逐字相同——刻意不按能力名拆成两条
    用例:这一节要证的是「这七条端点对组管理员全是 404」这件事本身,按能力名切开会让
    「拆格时漏掉一条」变成两条用例各自全绿而端点却掉出了名单。
    """
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


# --------------------------------------------------------------------------
# ⑦ 跨环境同步 §5:目标端写入围栏(镜像笔记本)
# --------------------------------------------------------------------------
#
# 围栏叠在能力守卫**之上**,是与权限正交的一轴:镜像上 owner 的权限一点没少,少的是
# 「这本库的同步来的内容还允不允许被改」。所以这一节要同时钉三件事:
#   ① 挡的能力在镜像上 409,且 detail 是那个契约形状(前端要读 sync_origin);
#   ② 放行的能力在镜像上照常工作(漏挡的反面是过度挡死,一样是回归);
#   ③ 未授权的人对镜像**仍然 404** —— 围栏绝不能变成一条「这本库存在、而且是从
#      <源环境> 同步来的」的泄露通道。


def _mark_mirror(notebook_id: str, origin: str = "prod-shanghai") -> str:
    """把这本库标成镜像,并读回来自证写入生效。

    直接走 store(`set_notebook_sync_origin`)而不是某个 API:产品里根本没有用户面
    的写入口——这一列只有跨环境导入器会写,而导入器刻意走 repository 层绕过围栏。

    写走 store、读走 service,是为了把**两条真实路径**都压到:导入器写的是 store,
    而围栏读的是 service 那一跳委托(`deps.notebook_access_repository()` 返回的正是
    这个 service)。两边都走 service 的话,委托断了也测不出来。
    """
    runtime = deps.repository()._runtime
    runtime.sharing_store.set_notebook_sync_origin(notebook_id, origin)
    assert runtime.sharing.notebook_sync_origin(notebook_id) == origin
    return origin


def _mirror_world(client: TestClient, letter: str) -> dict:
    """一本 owner 的**镜像**库 + 一个只读成员 + 一个陌生人。

    owner 那一列是围栏的正面(权限齐全却仍被挡),另外两列是它的不泄露面。
    """
    owner_h = _login(client, f"{letter}00000001")
    nb = client.post("/api/notebooks", json={"name": "M"}, headers=owner_h).json()["id"]
    src = _seed_source(nb)
    member_h = _login(client, f"{letter}00000002")
    member_id = client.get("/api/me", headers=member_h).json()["id"]
    deps.repository().add_member(nb, member_id)
    stranger_h = _login(client, f"{letter}00000003")
    origin = _mark_mirror(nb)
    return {
        "notebook": nb, "source": src, "origin": origin,
        "owner": owner_h, "member": member_h, "stranger": stranger_h,
    }


def _fenced_probes(nb: str, src: str) -> list[tuple[str, str, str, dict | None]]:
    """(能力名, 方法, 路径, body)—— 每个**挡**的能力一个真实端点。

    逐个敲而不是抽代表:围栏是一张表,漏挡一格在结构上看不出来,只有真的打一次那个
    端点才知道它到底有没有串上包装依赖。`notebook:delete` 尤其要单独敲——它根本不经
    能力工厂(挂的是 `require_notebook_delete`),围栏在那道守卫体内是**手抄的**第二处,
    表上写着 True 不代表代码里真的应用了。
    """
    return [
        ("sources:write", "post", f"/api/notebooks/{nb}/backfill-vectors", None),
        ("kg:write", "post", f"/api/notebooks/{nb}/kg/build", {}),
        ("knowhow:write", "post", f"/api/notebooks/{nb}/knowhow", {}),
        ("knowledge:write", "post", f"/api/notebooks/{nb}/object-schemas", {}),
        (
            "catalog:write", "post",
            f"/api/notebooks/{nb}/sources/{src}/command-catalog", {},
        ),
        ("notebook:manage", "patch", f"/api/notebooks/{nb}", {"name": "X"}),
        (
            "notebook:mount", "put", f"/api/notebooks/{nb}/bases",
            {"base_notebook_ids": []},
        ),
        ("notebook:delete", "delete", f"/api/notebooks/{nb}", None),
    ]


def _call(client: TestClient, method: str, path: str, body, headers):
    kwargs = {"headers": headers}
    if body is not None:
        kwargs["json"] = body
    return getattr(client, method)(path, **kwargs)


def _expected_mirror_detail(origin: str) -> dict:
    """The 409 body's exact shape — built here, independently of `deps`.

    Deliberately NOT `deps._mirror_detail(origin)`: reusing production's own
    builder would make every assertion below a tautology, and the point of
    pinning a response contract is that a change to it has to be typed out
    twice. The wording is the one thing that may legitimately be reworded, so
    it is asserted through `deps._mirror_message` — what is frozen here is that
    the key set is exactly these three and that `sync_origin` is the raw value.
    """
    return {
        "code": "notebook_mirrored",
        "message": deps._mirror_message(origin),
        "sync_origin": origin,
    }


def test_the_mirror_detail_message_is_user_facing_and_names_the_origin():
    """`message` 必须是能直接给终端用户看的中文文案,并点名源环境。

    这条单独存在是因为 `_expected_mirror_detail` 把文案本身委托给了
    `deps._mirror_message`——那让「改文案」不必改一堆用例,但也意味着没有任何断言在
    看文案的**内容**。少了这一条,把它改成空串或一句英文异常文本都还是全绿。
    """
    message = deps._mirror_message("prod-shanghai")
    assert "prod-shanghai" in message
    assert "镜像" in message and "源环境" in message
    # 不含异常类名 / 堆栈 / 字段名——与 `user_error()` 对文案的要求同一条口径。
    assert "Error" not in message and "None" not in message


def test_the_mirror_detail_carries_no_user_message_header(tmp_path, monkeypatch):
    """围栏的 409 **不带** `X-User-Message`。

    那个头是 `user_error()` 的出处标记,而 `user_error()` 的 detail 是**裸字符串**
    (见 deps.py 末尾「用户可见文案的出处标记」)。围栏的 detail 是结构化对象,前端按
    `code` 分支、拿 `message` 显示,本来就不会去原样打印整个 detail——带上那个头等于
    声明「这个 detail 可以原样展示」,而原样展示一个 JSON 对象正是那条约定要防的。
    与 `knowhow_history_stale` 逐字同款。
    """
    client = _client(tmp_path, monkeypatch)
    w = _mirror_world(client, "v")
    resp = client.patch(
        f"/api/notebooks/{w['notebook']}", json={"name": "X"}, headers=w["owner"]
    )
    assert resp.status_code == 409
    assert deps.USER_MESSAGE_HEADER not in resp.headers


def test_every_fenced_capability_is_refused_on_a_mirrored_notebook(
    tmp_path, monkeypatch
):
    """挡的能力在镜像上一律 409,detail 形状逐字符合契约(设计文档 §5)。

    409 而不是 403:请求者的权限没问题,是**目标资源此刻的状态**不接受这次写入。
    detail 三个键都要在,各有各的消费者(形状与 `knowhow_history_stale` 那一族对齐):
    `code` 给前端分支,`message` 是可直接展示的中文文案,`sync_origin` 让「镜像自
    <源环境>」不必再发一次请求去问——而那次请求与本次之间又是一个窗口。
    """
    client = _client(tmp_path, monkeypatch)
    w = _mirror_world(client, "n")
    probes = _fenced_probes(w["notebook"], w["source"])
    # 自证覆盖完整:敲到的能力名必须**恰好**是围栏表里所有 True 的格。
    assert {capability for capability, *_ in probes} == {
        capability
        for capability, fenced in deps._CAPABILITY_MIRROR_FENCE.items()
        if fenced
    }
    for capability, method, path, body in probes:
        resp = _call(client, method, path, body, w["owner"])
        assert resp.status_code == 409, (capability, resp.status_code, resp.text)
        assert resp.json()["detail"] == _expected_mirror_detail(w["origin"]), (
            capability, resp.text,
        )


def test_the_mirror_fence_does_not_leak_existence(tmp_path, monkeypatch):
    """未授权的人对镜像**仍然 404**,一个字都不泄露。

    顺序判据:围栏必须跑在级别守卫**之后**。写反了(先查 sync_origin)会让陌生人拿到
    409 —— 那条响应同时坐实了「这本库存在」和「它是从某个环境同步来的」,而基线是
    一句「Notebook not found」。
    """
    client = _client(tmp_path, monkeypatch)
    w = _mirror_world(client, "o")
    for capability, method, path, body in _fenced_probes(w["notebook"], w["source"]):
        for who in ("member", "stranger"):
            resp = _call(client, method, path, body, w[who])
            assert resp.status_code == 404, (
                capability, who, resp.status_code, resp.text,
            )


def test_allowed_capabilities_still_work_on_a_mirror(tmp_path, monkeypatch):
    """放行的能力在镜像上照常工作——围栏过度挡死同样是回归。

    点名跑的是**这次拆格新分出来的三格**:
    * `notebook:grant` —— 目标端自己的可见性由目标端管理(§5 明文放行);把它挡掉
      会让目标端再也不能把镜像共享给自己的同事;
    * `notebook:configure` —— `share_token` 是目标端自有列,不随同步走;
    * `scale_index:write` —— 检索索引是目标端自有的派生产物(设计 §6);把它挡掉,
      一本镜像库的索引会永远停在导入那一刻,而重建恰恰是目标端唯一的修复手段。
    这三格若跟着旧的 `notebook:manage` / `notebook:configure` / `kg:write` 一起被挡,
    共享流程与索引修复在镜像上就断了,而那正是拆格要避免的事。
    """
    client = _client(tmp_path, monkeypatch)
    w = _mirror_world(client, "p")
    nb = w["notebook"]

    grants = client.get(f"/api/notebooks/{nb}/grants", headers=w["owner"])
    assert grants.status_code == 200, grants.text
    share = client.post(f"/api/notebooks/{nb}/share", headers=w["owner"])
    assert share.status_code == 200, share.text
    assert share.json()["share_token"]
    assert client.get(
        f"/api/notebooks/{nb}/share", headers=w["owner"]
    ).status_code == 200
    # `scale_index:write`:重建与取消都必须走得通。判据是「不是 409」而不是「200」——
    # 一本空的 personal 库本来就不够格建索引,那条 409 是业务的(detail 是一句原因串),
    # 与围栏的 409 形状完全不同,所以比的是 detail 里有没有围栏那个 code。
    for path in ("rebuild", "cancel"):
        resp = client.post(
            f"/api/notebooks/{nb}/scale-index/{path}", json={}, headers=w["owner"]
        )
        assert "notebook_mirrored" not in resp.text, (path, resp.status_code, resp.text)

    # 只读投影同样不受围栏影响:镜像照样打得开、列得出来。
    assert client.get(f"/api/notebooks/{nb}", headers=w["owner"]).status_code == 200
    assert client.get(f"/api/notebooks/{nb}/bases", headers=w["owner"]).status_code == 200


def test_the_same_endpoints_are_untouched_on_a_local_notebook(tmp_path, monkeypatch):
    """同一批端点在**本地**库上一个都没变——围栏是条件分支,不是新的全局收紧。

    没有这一条,一个「把所有人都挡住」的实现会在上面两条里照样全绿。
    """
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "q00000001")
    nb = client.post("/api/notebooks", json={"name": "L"}, headers=owner_h).json()["id"]
    src = _seed_source(nb)
    assert deps.repository()._runtime.sharing_store.notebook_sync_origin(nb) == ""
    for capability, method, path, body in _fenced_probes(nb, src):
        if capability == "notebook:delete":
            continue  # 留到最后单独敲:删了后面的探针就没库可打了
        resp = _call(client, method, path, body, owner_h)
        assert resp.status_code != 404, (capability, resp.status_code, resp.text)
        # 判据不能只是「不是 409」:这些端点在一本空库上各有各的合法 409
        # (kg/build 在未配置模型时就回 409「LLM not configured」)。要判的是**这一条
        # 409 不是围栏发的**,所以比 detail 形状而不是比状态码。
        assert "notebook_mirrored" not in resp.text, (
            capability, resp.status_code, resp.text,
        )
    # 批 3·W1 PR-3:本地库的 DELETE 仍然是 202(tombstone CAS 立即返回)。
    assert client.delete(f"/api/notebooks/{nb}", headers=owner_h).status_code == 202


def test_source_body_level_self_checks_are_fenced_too(tmp_path, monkeypatch):
    """`source_routes` 的 parse/delete 体内自查也接了围栏。

    这两条端点的 URL 上**没有** notebook_id,守卫挂不到静态 `Depends` 上,所以它们走
    `notebook_capability_allowed` 在函数体内自查——围栏在那里同样是手抄的第二处。
    漏接的话,「重解析」与「删来源」就是两条绕过整道围栏的门。
    """
    client = _client(tmp_path, monkeypatch)
    w = _mirror_world(client, "r")
    src = w["source"]

    parsed = client.post(f"/api/sources/{src}/parse", headers=w["owner"])
    assert parsed.status_code == 409, parsed.text
    assert parsed.json()["detail"] == _expected_mirror_detail(w["origin"])
    deleted = client.delete(f"/api/sources/{src}", headers=w["owner"])
    assert deleted.status_code == 409, deleted.text
    assert deleted.json()["detail"]["code"] == "notebook_mirrored"
    # 不泄露面同款:陌生人仍然 404(而且是 "Source not found",不是围栏那条)。
    assert client.post(
        f"/api/sources/{src}/parse", headers=w["stranger"]
    ).status_code == 404
    assert client.delete(
        f"/api/sources/{src}", headers=w["stranger"]
    ).status_code == 404


def test_copying_a_mirror_produces_a_local_notebook(tmp_path, monkeypatch):
    """复制镜像的产物是**本地**库:`sync_origin` 必须写成 ''(设计文档 §5)。

    照抄源库那一列会让副本也被围栏挡住,而它根本不在任何同步关系里——下一次导入不会
    碰它,新 owner 却再也改不动它。这条用例同时证明副本**能被写**(不是只看那一列)。
    """
    client = _client(tmp_path, monkeypatch)
    w = _mirror_world(client, "t")
    nb = w["notebook"]
    token = client.post(f"/api/notebooks/{nb}/share", headers=w["owner"]).json()[
        "share_token"
    ]
    copier_h = _login(client, "t00000004")
    copied = client.post(f"/api/shared/{token}/copy", headers=copier_h)
    assert copied.status_code == 200, copied.text
    new_id = copied.json()["id"]

    assert deps.repository()._runtime.sharing_store.notebook_sync_origin(new_id) == ""
    # 投影同样如实:副本是本地库,镜像原库带着来源标识。
    assert client.get(f"/api/notebooks/{new_id}", headers=copier_h).json()[
        "sync_origin"
    ] == ""
    assert client.get(f"/api/notebooks/{nb}", headers=w["owner"]).json()[
        "sync_origin"
    ] == w["origin"]
    # 副本真的可写(围栏没跟着复制过去)。
    renamed = client.patch(
        f"/api/notebooks/{new_id}", json={"name": "副本改名"}, headers=copier_h
    )
    assert renamed.status_code == 200, renamed.text


def test_sync_origin_reaches_both_the_list_and_the_detail_projection(
    tmp_path, monkeypatch
):
    """`NotebookSummary.sync_origin` 在**列表与详情两条路径**上都有值。

    两条取行 SQL 都是 `SELECT notebooks.*`,所以这一列随行到达、零新增查询——但
    「构造性成立」不等于「真的成立」:`from_row` 里漏写一行赋值,两条路径就都恒空,
    而前端据它决定要不要把写入口画出来。少了列表那一半,镜像在卡片上与本地库长得
    一模一样,用户点进去才发现动不了。
    """
    client = _client(tmp_path, monkeypatch)
    w = _mirror_world(client, "u")
    nb = w["notebook"]
    local = client.post(
        "/api/notebooks", json={"name": "本地"}, headers=w["owner"]
    ).json()["id"]

    listed = client.get("/api/notebooks", headers=w["owner"])
    assert listed.status_code == 200, listed.text
    by_id = {row["id"]: row["sync_origin"] for row in listed.json()}
    assert by_id[nb] == w["origin"]
    assert by_id[local] == ""

    assert client.get(f"/api/notebooks/{nb}", headers=w["owner"]).json()[
        "sync_origin"
    ] == w["origin"]
    assert client.get(f"/api/notebooks/{local}", headers=w["owner"]).json()[
        "sync_origin"
    ] == ""


# --------------------------------------------------------------------------
# ⑧ 安全方法豁免:按**真实路由表**扫,不靠手写清单
# --------------------------------------------------------------------------


def _fenced_get_routes(app) -> list[tuple[str, str]]:
    """(能力名, 路径) —— 挂着「挡」能力、且方法是 GET 的每一条真实路由。

    从 `app.routes` 的依赖树里认能力名,而不是手写一份清单:手写清单答不了「明天
    有人在 kg:write 底下加了第七条 GET」这个问题,而那正是这条豁免要盖住的形状。
    认法是 callable 身份——包装依赖是工厂按能力名缓存的**唯一对象**,所以反查是
    确定的、没有字符串匹配的模糊性。
    """
    by_guard = {
        deps.require_notebook_capability(capability): capability
        for capability, fenced in deps._CAPABILITY_MIRROR_FENCE.items()
        if fenced
    }
    found: list[tuple[str, str]] = []
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        if "GET" not in methods:
            continue
        for dependency in getattr(route, "dependencies", []):
            capability = by_guard.get(getattr(dependency, "dependency", None))
            if capability is not None:
                found.append((capability, route.path))
    return found


def test_safe_methods_under_a_fenced_capability_are_never_mirrored(
    tmp_path, monkeypatch
):
    """镜像上,挂着「挡」能力的**每一条 GET** 都不许回 409。

    这是安全方法豁免的正向判据,而它按真实路由表扫是刻意的:归属按**写**来定,于是
    每个「挡」能力底下都可能挂着只为那个写服务的读端点(`GET .../scale-index/status`、
    `GET .../unified-kg/merges/review-job`、`GET .../mountable`、
    `GET .../mounted-by-count`)。少了豁免,它们会在镜像上整批 409——一本镜像库的面板
    连状态都读不出来。手写一份端点清单挡不住这个形状:下一条挂在同一个能力上的 GET
    不会出现在清单里,而它会**立刻**变成一个新的 409。

    判据只钉「不是 409」,不钉 200:这些端点在一本空库上各有各的合法回答(404、
    业务 409……),而且其中一条真的会回业务 409(索引不合格)。所以比的是 detail 里
    有没有围栏那个 `code`,不是状态码。
    """
    client = _client(tmp_path, monkeypatch)
    w = _mirror_world(client, "w")
    routes = _fenced_get_routes(client.app)
    # 空转保护:扫到 0 条就报绿等于什么都没测。今天至少有 mountable /
    # mounted-by-count / scale-index status / merges review-job 四条。
    assert len(routes) >= 4, f"路由扫描落空,只找到 {routes}"

    for capability, template in routes:
        path = template.replace("{notebook_id}", w["notebook"])
        assert "{" not in path, (
            f"{template} 还有未填的路径参数,这条用例请不动它——"
            "给它一个真实值,或在这里显式登记为不可探测"
        )
        resp = client.get(path, headers=w["owner"])
        assert "notebook_mirrored" not in resp.text, (
            capability, path, resp.status_code, resp.text,
        )


def test_the_same_get_routes_behave_identically_on_a_local_notebook(
    tmp_path, monkeypatch
):
    """同一批 GET 在本地库上状态码逐条相同——豁免没有顺手改掉它们的行为。

    与上一条互补:上一条只证明「镜像上不是围栏的 409」,一个把这些 GET 全都改成 500
    的实现照样能过。这一条把镜像与本地库的状态码逐条对齐,所以豁免只能是「跳过那次
    sync_origin 查询」,不能是别的任何东西。
    """
    client = _client(tmp_path, monkeypatch)
    w = _mirror_world(client, "x")
    local = client.post(
        "/api/notebooks", json={"name": "本地"}, headers=w["owner"]
    ).json()["id"]

    for capability, template in _fenced_get_routes(client.app):
        mirrored = client.get(
            template.replace("{notebook_id}", w["notebook"]), headers=w["owner"]
        )
        plain = client.get(
            template.replace("{notebook_id}", local), headers=w["owner"]
        )
        assert mirrored.status_code == plain.status_code, (
            capability, template, mirrored.status_code, plain.status_code,
        )
