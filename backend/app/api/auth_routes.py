from fastapi import APIRouter, Request, Response

from app.api.deps import identity_repository, user_error
from app.models.identity import AuthRequest, AuthResult
from app.services.auth_utils import is_valid_username

auth_router = APIRouter(prefix="/auth")


@auth_router.post("/register", response_model=AuthResult, response_model_exclude_defaults=True)
def register(payload: AuthRequest) -> AuthResult:
    if not is_valid_username(payload.username):
        raise user_error(400, "用户名须为「单个小写字母+八位数字」，如 a12345678")
    if not (payload.password or "").strip():
        raise user_error(400, "密码不能为空")
    try:
        # 建用户与发首个会话在同一写事务:拆开会与管理员重置的会话吊销竞态
        # (重置落在两次提交之间时,注册插入的会话带着已被重置的密码存活)。
        user, token = identity_repository().register_user_with_session(
            payload.username, payload.password
        )
    except ValueError as exc:
        if str(exc) == "local_auth_disabled":
            raise user_error(403, "本站注册已关闭，请使用统一登录或联系管理员开通。") from None
        # 两个分支都是写给用户的中文文案（异常原文只用来分类，不外泄），
        # 所以同样带出处标记。AST 扫描只认字面量 detail，这处是变量间接
        # 引用，需要手工登记。
        detail = "用户名已被占用" if "exists" in str(exc) else "用户名不合法"
        raise user_error(400, detail)
    return AuthResult(token=token, user=user)


@auth_router.post("/login", response_model=AuthResult, response_model_exclude_defaults=True)
def login(payload: AuthRequest) -> AuthResult:
    """验证与建会话必须走同一个 store 方法(单写事务):拆成 authenticate_user +
    create_session 会与改密/重置的会话吊销竞态,让旧密码登录的会话逃过吊销。"""
    repo = identity_repository()
    if repo.auth.get_policy()["mode"] in {"sso_only", "retired"}:
        raise user_error(403, "本站密码登录已关闭，请使用统一登录。")
    try:
        result = repo.login_with_password(payload.username, payload.password)
    except ValueError:
        raise user_error(403, "本站密码登录已关闭，请使用统一登录。") from None
    if result is None:
        raise user_error(401, "用户名或密码错误")
    user, token = result
    return AuthResult(token=token, user=user,
                      migration_required=repo.auth.get_policy()["mode"] == "binding_required")


@auth_router.post("/logout", status_code=204)
def logout(request: Request, response: Response) -> None:
    """logout 须拿到原始 token 才能删 session，故直接读 Authorization 头
    （不走 get_current_user，避免 token 已失效时无法登出）。"""
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if token:
        identity_repository().auth.cancel_for_session(token)
        identity_repository().delete_session(token)
    for name in ("__Host-sn-auth-browser", "sn-auth-browser-dev"):
        proof = request.cookies.get(name, "")
        if proof:
            identity_repository().auth.cancel_for_browser(proof)
        response.delete_cookie(name, path="/", secure=name.startswith("__Host-"),
                               httponly=True, samesite="lax")
    response.headers["Cache-Control"] = "no-store"
    return None
