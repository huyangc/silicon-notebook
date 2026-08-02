"""错误层的「出处标记」契约（PR #295 第三轮评审阻塞 1）。

前端 `frontend/app/errors.ts` 是 deny-by-default 的：**只有**带
`X-User-Message: 1` 的 4xx，它才会把 detail 原样显示给用户；其余一律按状态码
给通用中文文案。所以这一侧必须保证：

1. `user_error()` 真的把这个头写进了 HTTP 响应（不只是构造对象时带着）；
2. 裸 `HTTPException` 绝不带这个头——否则 `detail=str(exc)` 会重新变成可展示的；
3. 这个头登记进了 CORS `expose_headers`——**跨源部署的唯一失败点**，同源开发
   和前端单测都发现不了它；
4. 不再新增「4xx + 中文字面量 detail」的裸 HTTPException（会静默变成用户看不
   懂的通用文案，等于悄悄退化）。
"""
import ast
import pathlib
import re

import pytest
from fastapi.testclient import TestClient

from app.api.deps import USER_MESSAGE_HEADER, user_error

APP_ROOT = pathlib.Path(__file__).resolve().parents[1] / "app"
CJK_RE = re.compile(r"[一-鿿]")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    from app.core.config import get_settings
    get_settings.cache_clear()
    from app.api import deps
    deps.repository.cache_clear()
    from app.main import create_app
    return TestClient(create_app())


# ---------------------------------------------------------------------------
# helper 本身
# ---------------------------------------------------------------------------

def test_user_error_carries_the_marker_and_keeps_detail_a_plain_string():
    exc = user_error(403, "仅管理员可设为公共知识库")
    assert exc.status_code == 403
    # detail 的类型是 MCP agent / 日志 / 排查的契约，标记只能挂在头上。
    assert exc.detail == "仅管理员可设为公共知识库"
    assert isinstance(exc.detail, str)
    assert exc.headers == {USER_MESSAGE_HEADER: "1"}


# ---------------------------------------------------------------------------
# 真实响应：头必须一路走到 wire 上
# ---------------------------------------------------------------------------

def test_marked_4xx_reaches_the_wire_with_the_header(client):
    r = client.post("/api/auth/register", json={"username": "bad", "password": "pw"})
    assert r.status_code == 400
    assert r.headers.get(USER_MESSAGE_HEADER) == "1"
    # detail 仍是原来那个字符串字段，形状没变。
    assert r.json()["detail"].startswith("用户名须为")


def test_marked_401_login_failure_carries_the_header(client):
    client.post("/api/auth/register", json={"username": "z00123456", "password": "pw"})
    r = client.post("/api/auth/login", json={"username": "z00123456", "password": "wrong"})
    assert r.status_code == 401
    assert r.headers.get(USER_MESSAGE_HEADER) == "1"
    assert r.json()["detail"] == "用户名或密码错误"


def test_duplicate_username_marked_even_though_detail_is_a_variable(client):
    """auth_routes 那处 detail 是变量（三元表达式），AST 扫不出来，靠人工登记。"""
    client.post("/api/auth/register", json={"username": "z00123456", "password": "pw"})
    r = client.post("/api/auth/register", json={"username": "z00123456", "password": "x"})
    assert r.status_code == 400
    assert r.headers.get(USER_MESSAGE_HEADER) == "1"
    assert r.json()["detail"] == "用户名已被占用"


def test_unmarked_4xx_has_no_header(client):
    """未认证请求走的是裸 HTTPException：detail 是给日志/MCP 的，不带标记。"""
    r = client.get("/api/notebooks")
    assert r.status_code == 401
    assert USER_MESSAGE_HEADER not in r.headers


def test_validation_error_has_no_header(client):
    """FastAPI 自己生成的 422（`[{loc,msg,type}]`）永远不是用户文案。"""
    r = client.post("/api/auth/register", json={"username": "z00123456"})
    assert r.status_code == 422
    assert USER_MESSAGE_HEADER not in r.headers


# ---------------------------------------------------------------------------
# 跨源部署的失败点
# ---------------------------------------------------------------------------

def test_header_is_exposed_through_cors():
    """没有 expose_headers，跨源部署时浏览器读不到这个头，于是**所有**后端中文
    文案都会被前端判成「没标记」而压平成通用文案——而同源开发、后端单测、前端
    单测（mock Response 不受 CORS 约束）三者都是绿的。这条断言是那个坑的守卫。
    """
    from app.main import create_app
    from fastapi.middleware.cors import CORSMiddleware

    app = create_app()
    cors = [m for m in app.user_middleware if m.cls is CORSMiddleware]
    assert cors, "CORS 中间件不在了？"
    exposed = cors[0].kwargs.get("expose_headers", [])
    assert USER_MESSAGE_HEADER in exposed, (
        f"{USER_MESSAGE_HEADER} 必须登记进 CORS expose_headers，否则跨源部署时"
        "前端读不到它"
    )
    assert "X-Request-Id" in exposed, "别把已有的诊断头挤掉了"


# ---------------------------------------------------------------------------
# 防漂移：不许再出现「4xx + 中文字面量 detail」的裸 HTTPException
# ---------------------------------------------------------------------------

def _bare_chinese_4xx_sites() -> list[str]:
    offenders = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "HTTPException":
                continue
            kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            args = list(node.args)
            status = kwargs.get("status_code", args[0] if args else None)
            detail = kwargs.get("detail", args[1] if len(args) > 1 else None)
            if "headers" in kwargs:
                continue  # 自己挂了头的（含 user_error 内部实现），放行
            if not (isinstance(status, ast.Constant) and isinstance(status.value, int)):
                continue
            if not 400 <= status.value < 500:
                continue
            if not (isinstance(detail, ast.Constant) and isinstance(detail.value, str)):
                continue
            if CJK_RE.search(detail.value):
                offenders.append(
                    f"{path.relative_to(APP_ROOT.parent)}:{node.lineno}  {detail.value!r}"
                )
    return offenders


@pytest.mark.architecture_contract
def test_no_bare_chinese_4xx_http_exception():
    """中文字面量 detail 说明它是写给用户的 —— 那就必须用 user_error() 标出来，
    否则前端只会显示「操作失败，请重试」，可操作信息静默丢失。
    """
    offenders = _bare_chinese_4xx_sites()
    assert offenders == [], (
        "这些 4xx 的 detail 是中文用户文案，但没走 user_error()，前端会把它们"
        "压平成通用文案：\n" + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# 反方向：user_error() 里的文案必须真的是中文（第四轮评审阻塞 1）
# ---------------------------------------------------------------------------
#
# 上面那条查的是**反方向**（中文 detail 必须走 user_error），它挡不住
# `user_error(400, "Quota exceeded")` —— 那句会合法通过全部门禁，然后英文原样
# 上屏。而 AGENTS.md 写的是「用户可见错误一律中文」。
#
# 前端 errors.ts 的闸2 已经会把非中文文案退成通用文案（用户不会看到英文），
# 但那是**兜底**：真出现了就意味着这条错误的可操作信息白丢了。这条测试是把
# 缺陷挡在写码时。


# 动态实参（变量 / f-string / 拼接）AST 看不出内容，**不静默跳过**：逐个登记，
# 写清楚为什么它仍然满足「中文用户文案」这个契约、以及谁在覆盖它。
# 键用「文件::函数名」而不是行号——行号会被无关改动推移。
ALLOWED_DYNAMIC_USER_ERROR = {
    "app/api/auth_routes.py::register": (
        "detail 是同函数内两个中文字面量的三元选择（「用户名已被占用」/"
        "「用户名不合法」），异常原文只用于分类、不外泄。真实响应由 "
        "test_duplicate_username_marked_even_though_detail_is_a_variable 覆盖。"
    ),
    "app/api/source_routes.py::_enforce_document_capacity": (
        "detail 是纯中文文档数量上限模板 f-string，只插入 limit/current/adding 三个"
        "整数（「该笔记本最多可添加 N 篇文档，当前已有 X 篇，无法再添加 K 篇。」）——"
        "无内部黑话，只说「文档」。真实响应由 tests/test_document_limit.py 的"
        " import/upload/url 三端点 409 用例覆盖(断言 X-User-Message 头 + 「文档」文案)。"
    ),
    "app/api/source_routes.py::_source_upload_too_large": (
        "detail 是纯中文的单文件大小上限模板，只插入部署配置派生的两个数值："
        "_format_upload_size_limit(max_bytes) 与 max_bytes 本身。max_bytes 一路来自 "
        "Settings.source_upload_max_bytes（source_upload_max_mb × 1024 × 1024，字段约束"
        " 0 < mb ≤ 1024 的整数），故格式化只会走「N MB」那一支——既无路径也无异常原文。"
        "真实响应由 tests/test_source_upload_size_limit.py::"
        "test_upload_over_configured_single_file_limit_is_authoritatively_rejected 覆盖"
        "(断言 413 + X-User-Message 头 + detail 同时含「1 MB」与字节数)。"
    ),
    "app/api/source_routes.py::_parse_source_upload": (
        "该函数里唯一的动态实参是「单次最多上传 N 个来源文件。请减少本批文件数量后重试。」，"
        "N 是模块常量 SOURCE_UPLOAD_MAX_FILES_PER_BATCH(整数)，同函数其余 user_error 文案都是"
        "中文字面量。multipart 解析器的异常原文(exc.message)刻意留在同函数下方的**裸** "
        "HTTPException 上作诊断，不进 user_error()。真实响应由 "
        "tests/test_source_upload_size_limit.py::"
        "test_upload_batch_above_fixed_file_count_guard_is_rejected_before_orchestration 覆盖"
        "(断言 413 + X-User-Message 头 + detail 含上限数字)。"
    ),
    "app/api/knowhow_routes.py::_knowhow_import_user_error": (
        "detail 只能来自 GridParseError 或 KnowhowImportValidationError 的 user_message；"
        "两个异常类型只接受导入层维护的固定中文可操作文案，其他 ValueError 仍走未标记的"
        "诊断通道。真实响应由 test_knowhow_api.py 的解析失败、属性按行提示与列数不一致"
        "用例覆盖，均断言 X-User-Message。"
    ),
    "app/api/catalog_routes.py::start_command_catalog": (
        "三处 detail：本文件顶部维护的中文常量 `_ALREADY_RUNNING_MESSAGE`"
        "（该来源已有识别任务在跑）、`catalog_job.MODEL_UNAVAILABLE_MESSAGE`"
        "（模型未配置），以及 `catalog_job.pending_candidates_message(exc.pending)`"
        "（R5 P2：上一轮成功但还有待审阅候选，`.../job` 只返回最近一次任务、"
        "新一轮会让旧候选永远够不着——文案是同模块维护的纯中文模板 f-string，只插入"
        "一个整数计数，无内部黑话）。真实响应由 test_command_catalog_job.py 的"
        "test_api_duplicate_start_is_a_user_readable_409、"
        "test_api_start_without_a_configured_model_is_a_user_readable_409 与"
        "test_api_second_start_with_unreviewed_candidates_is_a_user_readable_409 覆盖，均"
        "断言 X-User-Message 头与逐字 detail。"
    ),
    "app/api/catalog_routes.py::_not_parsed_error": (
        "R8：detail 是 `catalog_job` 维护的两个中文常量之一——"
        "`SOURCE_PARSE_FAILED_MESSAGE`（来源解析失败，要重新解析或重新上传）与"
        "`SOURCE_NOT_PARSED_MESSAGE`（来源尚未完成解析，要等待）。二选一只看"
        "`CatalogSourceNotParsed.parse_status` 这个**字段**，不看异常文本，"
        "异常原文既不参与判断也不外泄；helper 只是把 preview/start 两个端点共用的"
        "这段选择收拢到一处，避免同一份文案抄两遍走样。真实响应由"
        "test_command_catalog_job.py 的"
        "test_api_start_and_preview_refuse_a_source_that_is_not_parsed_yet 与"
        "test_api_start_and_preview_refuse_a_source_whose_parse_failed 覆盖，"
        "均断言 X-User-Message 头与逐字 detail。"
    ),
    "app/api/catalog_routes.py::apply_command_catalog": (
        "五处 detail 都是本文件/`catalog_job` 维护的中文常量："
        "`_APPLY_EMPTY_MESSAGE`（未选择候选）、`_APPLY_TOO_MANY_MESSAGE`"
        "（显式选择超过 MAX_APPLY_CANDIDATES）、`catalog_job."
        "APPLY_TABLE_SHAPE_MESSAGE`（目标表丢失命令列，走策展常量而非"
        "`str(exc)` —— 与同文件 CatalogModelUnavailable 的先例一致）、R8 的"
        "`catalog_job.SOURCE_STALE_MESSAGE`（来源已重新解析，候选同时被过期），"
        "以及 R10 的 `catalog_job.SOURCE_BUSY_MESSAGE`（来源正在重新解析，确认"
        "无法安全落库）。后两条刻意分开：stale 那条连带过期了候选，busy 这条"
        "什么都没过期，所以不能叫用户去「重新识别」。真实响应由"
        "test_command_catalog_job.py 的"
        "test_api_apply_without_a_selection_is_a_user_readable_400、"
        "test_api_apply_with_too_many_explicit_candidates_is_a_user_readable_422、"
        "test_api_apply_refuses_a_broken_table_with_a_user_readable_400、"
        "test_api_apply_after_a_reparse_is_a_user_readable_409 与"
        "test_api_apply_during_an_in_flight_reparse_is_a_user_readable_409 覆盖，"
        "均断言 X-User-Message 头与逐字 detail。"
    ),
    "app/api/catalog_routes.py::dismiss_command_catalog": (
        "R7：四处 detail 都是本文件顶部/`catalog_job` 维护的中文常量，逐条镜像同文件"
        "apply 的先例——`_DISMISS_EMPTY_MESSAGE`（未选择候选）、"
        "`_DISMISS_TOO_MANY_MESSAGE`（显式选择超过 MAX_APPLY_CANDIDATES）、R8 的"
        "`catalog_job.SOURCE_STALE_MESSAGE`（来源已重新解析）与 R10 的"
        "`catalog_job.SOURCE_BUSY_MESSAGE`（来源正在重新解析）。真实响应由"
        "test_command_catalog_job.py 的"
        "test_api_dismiss_without_a_selection_is_a_user_readable_400、"
        "test_api_dismiss_with_too_many_explicit_candidates_is_a_user_readable_422 与"
        "test_api_dismiss_during_an_in_flight_reparse_is_a_user_readable_409 覆盖，"
        "均断言 X-User-Message 头与逐字 detail。"
    ),
}


def _user_error_sites() -> tuple[list[str], list[str]]:
    """扫全部 user_error() 调用点。

    返回 (静态字面量里非中文的违规点, 动态实参的调用点标识)。
    """
    static_offenders: list[str] = []
    dynamic_sites: list[str] = []

    def visit(node: ast.AST, rel: str, func: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, rel, child.name)
                continue
            if isinstance(child, ast.Call):
                name = getattr(child.func, "id", None) or getattr(child.func, "attr", None)
                if name == "user_error":
                    kwargs = {kw.arg: kw.value for kw in child.keywords if kw.arg}
                    args = list(child.args)
                    message = kwargs.get("message", args[1] if len(args) > 1 else None)
                    if isinstance(message, ast.Constant) and isinstance(message.value, str):
                        if not CJK_RE.search(message.value):
                            static_offenders.append(f"{rel}:{child.lineno}  {message.value!r}")
                    else:
                        dynamic_sites.append(f"{rel}::{func}")
            visit(child, rel, func)

    for path in sorted(APP_ROOT.rglob("*.py")):
        rel = str(path.relative_to(APP_ROOT.parent))
        if rel == "app/api/deps.py":
            continue  # helper 自己的定义（message 是形参，不是文案）
        visit(ast.parse(path.read_text(encoding="utf-8")), rel, None)
    return static_offenders, dynamic_sites


@pytest.mark.architecture_contract
def test_static_user_error_messages_are_chinese():
    """user_error() 的文案是**给终端用户看的**，按产品约定一律中文。

    英文文案会被前端闸2 退成状态码通用文案：用户不会看到英文，但这条错误原本
    要传达的可操作信息（该改什么、该找谁）就白丢了。
    """
    static_offenders, _ = _user_error_sites()
    assert static_offenders == [], (
        "这些 user_error() 的文案不是中文。用户可见错误一律中文（AGENTS.md）；"
        "前端 errors.ts 的闸2 会把它们退成通用文案，可操作信息静默丢失：\n"
        + "\n".join(static_offenders)
    )


@pytest.mark.architecture_contract
def test_dynamic_user_error_sites_are_explicitly_registered():
    """动态实参 AST 查不了内容 —— 那就**显式登记**，不许静默放过。

    双向断言：新增的动态站点会红（有人绕过了静态检查），登记表里过期的条目也
    会红（防止 allowlist 变成只增不减的坟场）。
    """
    _, dynamic_sites = _user_error_sites()
    found = set(dynamic_sites)
    registered = set(ALLOWED_DYNAMIC_USER_ERROR)

    unregistered = sorted(found - registered)
    assert unregistered == [], (
        "这些 user_error() 的文案是动态实参，AST 看不出是不是中文。要么改成中文"
        "字面量，要么登记进 ALLOWED_DYNAMIC_USER_ERROR 并写清理由：\n"
        + "\n".join(unregistered)
    )

    stale = sorted(registered - found)
    assert stale == [], (
        "登记表里这些条目已经没有对应的调用点了，删掉它们：\n" + "\n".join(stale)
    )
