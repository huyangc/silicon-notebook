"""Fixed core authentication routes, including the pre-login OAuth callback."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse

from app.api.deps import identity_repository, user_error
from app.bootstrap import application_auth_provider
from app.core.config import get_settings
from app.domain.auth_provider import AuthProviderError
from app.models.identity import AuthResult
from app.models.sso import SsoChoice, SsoComplete, SsoLink
from app.services.auth_flow import AUTH_BROWSER_PROOF_BYTES, AuthFlowService

sso_router = APIRouter()


def auth_flow() -> AuthFlowService:
    return AuthFlowService(identity_repository().auth, application_auth_provider(), get_settings())


_MESSAGES = {
    "link_verification_failed": "用户名或密码错误",
    "link_target_invalid": "该账号不能关联统一认证，请联系管理员。",
    "link_target_linked": "该账号已关联过统一认证账号，不能再关联；如有疑问请联系管理员。",
    "username_conflict": "统一认证账号名与本站已有账号冲突，请联系管理员处理。",
    "username_case_conflict": "本站已有只差大小写的同名账号，请选择「关联老账号」并输入该账号的密码。",
    "invalid_transaction": "认证操作已过期或已使用，请重新登录。",
    "stale_transaction": "认证状态已变化，请重新登录。",
    "external_auth_expired": "统一认证登录已过期，请重新登录。",
    "account_inactive": "账号已停用，请联系管理员。",
}
# On the choice page these mean the pending choice is spent: 410 tells the
# page to lock the form and offer only "back to login".
_PENDING_GONE = frozenset({"invalid_transaction", "stale_transaction", "external_auth_expired"})


def _error(exc, *, pending=False):
    # Both stores and providers expose closed, content-free categories. Unknown
    # values use a fixed message; no exception/HTTP response text leaves here.
    code = getattr(exc, "code", str(exc))
    if isinstance(exc, AuthProviderError):
        status = 503
    else:
        status = 410 if pending and code in _PENDING_GONE else 409
    return user_error(status, _MESSAGES.get(code, "认证操作未完成，请重新尝试或联系管理员检查配置。"))


def _origin(request, flow):
    origin = request.headers.get("origin")
    allowed = {flow.settings.auth_public_base_url, flow.settings.auth_frontend_base_url}
    if origin and origin.rstrip("/") not in allowed:
        raise user_error(403, "请求来源不匹配，请从本站页面重新操作。")


def _proof(request, flow):
    value = request.cookies.get(flow.cookie_name, "")
    if not value:
        raise user_error(400, "认证浏览器凭证已失效，请重新发起登录。")
    return value


def _private(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"


@sso_router.get("/auth/capabilities")
def capabilities(response: Response):
    _private(response)
    try:
        return auth_flow().capabilities()
    except (ValueError, AuthProviderError) as exc:
        raise _error(exc) from None


@sso_router.post("/auth/sso/start")
def login_start(request: Request, response: Response):
    flow = auth_flow()
    _origin(request, flow)
    proof = request.cookies.get(flow.cookie_name) or secrets.token_urlsafe(AUTH_BROWSER_PROOF_BYTES)
    try:
        url = flow.start(proof)
    except (ValueError, AuthProviderError) as exc:
        raise _error(exc) from None
    response.set_cookie(flow.cookie_name, proof, max_age=flow.settings.auth_transaction_ttl_seconds,
                        httponly=True, secure=flow.secure_cookie, samesite="lax", path="/")
    _private(response)
    return {"authorization_url": url}


@sso_router.get("/auth/sso/callback")
def callback(request: Request):
    flow = auth_flow()
    if not flow.settings.auth_public_base_url:
        raise user_error(404, "统一认证尚未配置，请从本站登录页进入。")
    try:
        # Missing/expired browser proof is an expected callback failure. Keep
        # the browser on the same sanitized recovery path as an expired state.
        proof = request.cookies.get(flow.cookie_name, "")
        if not proof:
            raise ValueError("invalid_transaction")
        if "error" in request.query_params:
            if len(request.query_params.getlist("state")) == 1:
                flow.store.claim(request.query_params["state"], proof)
            raise ValueError("authentication_failed")
        # Reject duplicate OAuth values instead of choosing one interpretation.
        if any(len(request.query_params.getlist(key)) != 1 for key in ("state", "code")):
            raise ValueError("invalid_transaction")
        code = flow.callback(request.query_params["state"], request.query_params["code"], proof)
        url = flow.frontend_redirect(code=code)
    except (ValueError, AuthProviderError):
        url = flow.frontend_redirect(error="authentication_failed")
    response = RedirectResponse(url, status_code=303)
    _private(response)
    return response


@sso_router.post("/auth/sso/complete")
def complete(payload: SsoComplete, request: Request, response: Response):
    flow = auth_flow()
    _origin(request, flow)
    _private(response)
    try:
        return flow.complete(payload.code, _proof(request, flow))
    except (ValueError, AuthProviderError) as exc:
        raise _error(exc) from None


@sso_router.post("/auth/sso/link", response_model=AuthResult)
def link(payload: SsoLink, request: Request, response: Response):
    flow = auth_flow()
    _origin(request, flow)
    _private(response)
    try:
        user, token = flow.link(payload.pending_id, _proof(request, flow),
                                payload.login_name, payload.password)
    except (ValueError, AuthProviderError) as exc:
        raise _error(exc, pending=True) from None
    return AuthResult(user=user, token=token)


@sso_router.post("/auth/sso/create", response_model=AuthResult)
def create(payload: SsoChoice, request: Request, response: Response):
    flow = auth_flow()
    _origin(request, flow)
    _private(response)
    try:
        user, token = flow.create(payload.pending_id, _proof(request, flow))
    except (ValueError, AuthProviderError) as exc:
        raise _error(exc, pending=True) from None
    return AuthResult(user=user, token=token)


@sso_router.post("/auth/sso/cancel", status_code=204)
def cancel(payload: SsoChoice, request: Request):
    flow = auth_flow()
    _origin(request, flow)
    flow.store.cancel(payload.pending_id, _proof(request, flow))
