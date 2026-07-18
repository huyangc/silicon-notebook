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
    exc = user_error(403, "仅管理员可设置基准库")
    assert exc.status_code == 403
    # detail 的类型是 MCP agent / 日志 / 排查的契约，标记只能挂在头上。
    assert exc.detail == "仅管理员可设置基准库"
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


def test_no_bare_chinese_4xx_http_exception():
    """中文字面量 detail 说明它是写给用户的 —— 那就必须用 user_error() 标出来，
    否则前端只会显示「操作失败，请重试」，可操作信息静默丢失。
    """
    offenders = _bare_chinese_4xx_sites()
    assert offenders == [], (
        "这些 4xx 的 detail 是中文用户文案，但没走 user_error()，前端会把它们"
        "压平成通用文案：\n" + "\n".join(offenders)
    )
