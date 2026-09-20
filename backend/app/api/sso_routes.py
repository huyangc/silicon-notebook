"""Fixed core authentication routes, including the pre-login OAuth callback."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import RedirectResponse

from app.api.deps import _bearer_token, identity_repository, user_error
from app.bootstrap import application_auth_provider
from app.core.config import get_settings
from app.domain.auth_provider import AuthProviderError
from app.domain.auth_policy import AUTH_INVENTORY_PAGE_SIZE, AUTH_INVENTORY_PAGE_MAX
from app.models.identity import AuthResult
from app.models.sso import (
    AuthenticationAccountUpdate, AuthenticationPolicyUpdate,
    AuthenticationGrantCreate, AuthenticationGrantStart,
    AuthenticationConfigurationUpdate,
    BindingConfirm, BindingStart, SsoComplete,
)
from app.services.auth_flow import AUTH_BROWSER_PROOF_BYTES, AuthFlowService

sso_router = APIRouter()


def auth_flow() -> AuthFlowService:
    return AuthFlowService(identity_repository().auth, application_auth_provider(), get_settings())


def _error(exc):
    # Both stores and providers expose closed, content-free categories. Unknown
    # values use a fixed message; no exception/HTTP response text leaves here.
    code = getattr(exc, "code", str(exc))
    messages = {
        "identity_not_linked": "统一账号尚未关联，请先登录原本站账号进行关联；迁移结束后请联系管理员。",
        "identity_conflict": "统一账号已被关联，请联系管理员核实，原有数据未改变。",
        "username already exists": "统一账号名与已有账号冲突，请联系管理员处理。",
        "local_verification_failed": "本站密码验证失败，请重新输入当前密码。",
        "invalid_transaction": "认证操作已过期或已使用，请重新发起。",
        "stale_transaction": "认证状态已变化，请重新发起。",
        "stale_local_proof": "原本站登录状态已失效，请重新登录后关联。",
        "account_inactive": "账号已停用，请联系管理员。",
        "admin_required": "仅管理员可执行此操作。",
        "irreversible_policy": "本地认证已退役，不能恢复密码登录。",
        "migration_incomplete": "迁移条件尚未满足，请检查用户和管理员迁移清单。",
        "stale_policy": "迁移状态已更新，请刷新后重试。",
        "invalid_transition": "不支持此阶段切换，请按迁移顺序操作。",
    }
    return user_error(409 if not isinstance(exc, AuthProviderError) else 503,
                      messages.get(code, "认证操作未完成，请重新尝试或联系管理员检查配置。"))


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


def _actor(request, flow, *, admin=False):
    user = flow.store.resolve_session(_bearer_token(request), migration_only=not admin)
    if user is None:
        raise user_error(401, "登录已失效，请重新登录。")
    if admin and user.role != "admin":
        raise user_error(403, "仅管理员可执行此操作。")
    return user


@sso_router.get("/auth/capabilities")
def capabilities(response: Response):
    _private(response)
    try:
        return auth_flow().capabilities()
    except (ValueError, AuthProviderError) as exc:
        raise _error(exc) from None


def _start(request, response, purpose, password="", grant_token=""):
    flow = auth_flow()
    _origin(request, flow)
    proof = request.cookies.get(flow.cookie_name) or secrets.token_urlsafe(AUTH_BROWSER_PROOF_BYTES)
    try:
        url = flow.start(purpose, proof, session_token=_bearer_token(request), password=password,
                         grant_token=grant_token)
    except (ValueError, AuthProviderError) as exc:
        raise _error(exc) from None
    response.set_cookie(flow.cookie_name, proof, max_age=flow.settings.auth_transaction_ttl_seconds,
                        httponly=True, secure=flow.secure_cookie, samesite="lax", path="/")
    _private(response)
    return {"authorization_url": url}


@sso_router.post("/auth/sso/start")
def login_start(request: Request, response: Response):
    return _start(request, response, "login")


@sso_router.post("/me/identity-binding/start")
def binding_start(payload: BindingStart, request: Request, response: Response):
    return _start(request, response, "bind", payload.current_password)


@sso_router.post("/auth/sso/grant/start")
def grant_start(payload: AuthenticationGrantStart, request: Request, response: Response):
    return _start(request, response, payload.purpose, grant_token=payload.grant_token)


@sso_router.get("/auth/sso/callback")
def callback(request: Request):
    flow = auth_flow()
    if not flow.settings.auth_public_base_url:
        raise user_error(404, "统一认证尚未配置，请从本站登录页进入。")
    try:
        if "error" in request.query_params:
            if len(request.query_params.getlist("state")) == 1:
                flow.store.claim(request.query_params["state"], _proof(request, flow))
            raise ValueError("authentication_failed")
        # Reject duplicate OAuth values instead of choosing one interpretation.
        if any(len(request.query_params.getlist(key)) != 1 for key in ("state", "code")):
            raise ValueError("invalid_transaction")
        code = flow.callback(request.query_params["state"], request.query_params["code"], _proof(request, flow))
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


@sso_router.post("/me/identity-binding/confirm", response_model=AuthResult)
def confirm(payload: BindingConfirm, request: Request, response: Response):
    flow = auth_flow()
    _origin(request, flow)
    _private(response)
    try:
        user, token = flow.confirm(payload.pending_id, _proof(request, flow), _bearer_token(request))
    except (ValueError, AuthProviderError) as exc:
        raise _error(exc) from None
    return AuthResult(user=user, token=token)


@sso_router.post("/me/identity-binding/cancel", status_code=204)
def cancel(payload: BindingConfirm, request: Request):
    flow = auth_flow()
    _origin(request, flow)
    try:
        flow.store.cancel(payload.pending_id, _proof(request, flow), session_token=_bearer_token(request))
    except ValueError as exc:
        raise _error(exc) from None


@sso_router.get("/me/identities")
def identities(request: Request, response: Response):
    flow = auth_flow()
    user = _actor(request, flow)
    _private(response)
    return flow.store.identities(user.id)


@sso_router.get("/admin/auth/policy")
def policy(request: Request, response: Response):
    flow = auth_flow()
    _actor(request, flow, admin=True)
    _private(response)
    return flow.store.get_policy()


@sso_router.get("/admin/auth/migration")
def migration(request: Request, response: Response):
    flow = auth_flow()
    _actor(request, flow, admin=True)
    _private(response)
    return flow.store.preflight()


@sso_router.get("/admin/auth/accounts")
def accounts(request: Request, response: Response, offset: int = Query(default=0, ge=0),
             limit: int = Query(default=AUTH_INVENTORY_PAGE_SIZE, ge=1, le=AUTH_INVENTORY_PAGE_MAX)):
    flow = auth_flow()
    _actor(request, flow, admin=True)
    _private(response)
    return flow.store.inventory_page(offset=offset, limit=limit)


@sso_router.get("/admin/auth/audit")
def audit(request: Request, response: Response, offset: int = Query(default=0, ge=0),
          limit: int = Query(default=AUTH_INVENTORY_PAGE_SIZE, ge=1, le=AUTH_INVENTORY_PAGE_MAX)):
    flow = auth_flow()
    _actor(request, flow, admin=True)
    _private(response)
    return flow.store.audit_page(offset=offset, limit=limit)


@sso_router.patch("/admin/auth/provider-configuration")
def prepare_configuration(payload: AuthenticationConfigurationUpdate, request: Request):
    flow = auth_flow()
    actor = _actor(request, flow, admin=True)
    policy = flow.store.get_policy()
    descriptor = flow.host.describe()
    if descriptor is None or (
        descriptor.plugin_id != policy["plugin_id"]
        or descriptor.provider_id != policy["provider_id"]
        or descriptor.provider_namespace != policy["provider_namespace"]
    ):
        raise user_error(409, "当前认证身份源不匹配，不能准备配置升级。")
    try:
        return flow.store.prepare_provider_configuration(expected_revision=payload.expected_revision,
            actor_id=actor.id, plugin_id=descriptor.plugin_id, provider_id=descriptor.provider_id,
            provider_namespace=descriptor.provider_namespace,
            config_generation=payload.configuration_generation)
    except ValueError as exc:
        raise _error(exc) from None


@sso_router.patch("/admin/auth/policy")
def update_policy(payload: AuthenticationPolicyUpdate, request: Request):
    flow = auth_flow()
    actor = _actor(request, flow, admin=True)
    descriptor = flow.host.describe()
    if descriptor is None or not flow.settings.auth_public_base_url or flow.settings.auth_optional:
        raise user_error(409, "请先配置可用的认证插件、回调地址，并关闭匿名访问。")
    try:
        flow.validate_configuration({"mode": payload.mode, "provider_id": descriptor.provider_id,
            "plugin_id": descriptor.plugin_id,
            "provider_namespace": descriptor.provider_namespace,
            "config_generation": descriptor.configuration_generation})
        return flow.store.set_policy(payload.mode, actor_id=actor.id,
            expected_revision=payload.expected_revision, provider_id=descriptor.provider_id,
            plugin_id=descriptor.plugin_id,
            provider_namespace=descriptor.provider_namespace,
            config_generation=descriptor.configuration_generation,
            allow_rollback=payload.allow_rollback)
    except (ValueError, AuthProviderError) as exc:
        raise _error(exc) from None


@sso_router.post("/admin/auth/grants")
def create_grant(payload: AuthenticationGrantCreate, request: Request, response: Response):
    flow = auth_flow()
    actor = _actor(request, flow, admin=True)
    _private(response)
    try:
        flow.validate_configuration()
        token = flow.store.issue_grant(payload.purpose, payload.subject, actor_id=actor.id,
            ttl_seconds=flow.settings.auth_transaction_ttl_seconds,
            target_user_id=payload.target_user_id)
        return {"grant_token": token, "purpose": payload.purpose,
                "expires_in": flow.settings.auth_transaction_ttl_seconds}
    except (ValueError, AuthProviderError) as exc:
        raise _error(exc) from None


@sso_router.patch("/admin/auth/accounts/{user_id}")
def update_account(user_id: str, payload: AuthenticationAccountUpdate, request: Request):
    flow = auth_flow()
    actor = _actor(request, flow, admin=True)
    try:
        flow.store.set_account_status(user_id, payload.status, actor_id=actor.id)
        return {"id": user_id, "status": payload.status}
    except ValueError as exc:
        raise _error(exc) from None


@sso_router.post("/admin/auth/retirement-cleanup")
def retirement_cleanup(request: Request):
    flow = auth_flow()
    _actor(request, flow, admin=True)
    try:
        flow.store.retirement_cleanup()
        return {"status": "complete"}
    except ValueError as exc:
        raise _error(exc) from None
