from fastapi import APIRouter, HTTPException, Request

from app.api.deps import repository
from app.models.schemas import AuthRequest, AuthResult
from app.services.auth_utils import is_valid_username

auth_router = APIRouter(prefix="/auth")


@auth_router.post("/register", response_model=AuthResult)
def register(payload: AuthRequest) -> AuthResult:
    if not is_valid_username(payload.username):
        raise HTTPException(status_code=400, detail="用户名须为「字母+00+六位数字」，如 zhang00123456")
    if not (payload.password or "").strip():
        raise HTTPException(status_code=400, detail="密码不能为空")
    try:
        user = repository().create_user(payload.username, payload.password)
    except ValueError as exc:
        detail = "用户名已被占用" if "exists" in str(exc) else "用户名不合法"
        raise HTTPException(status_code=400, detail=detail)
    token = repository().create_session(user.id)
    return AuthResult(token=token, user=user)


@auth_router.post("/login", response_model=AuthResult)
def login(payload: AuthRequest) -> AuthResult:
    user = repository().authenticate_user(payload.username, payload.password)
    if user is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = repository().create_session(user.id)
    return AuthResult(token=token, user=user)


@auth_router.post("/logout", status_code=204)
def logout(request: Request) -> None:
    """logout 须拿到原始 token 才能删 session，故直接读 Authorization 头
    （不走 get_current_user，避免 token 已失效时无法登出）。"""
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if token:
        repository().delete_session(token)
    return None
