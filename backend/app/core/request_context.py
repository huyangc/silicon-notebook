from __future__ import annotations

import contextvars
from typing import Any, Callable

from app.core.event_logging import reset_log_owner, set_log_owner
from app.models.identity import UserProfile

RequestUserToken = tuple[contextvars.Token, contextvars.Token]

_REQUEST_USER: contextvars.ContextVar[UserProfile | None] = contextvars.ContextVar(
    "request_user", default=None
)


def get_request_user() -> UserProfile | None:
    return _REQUEST_USER.get()


def request_user_id() -> str | None:
    user = get_request_user()
    return user.id if user is not None else None


def set_request_user(user: UserProfile | None) -> RequestUserToken:
    tok_user = _REQUEST_USER.set(user)
    tok_owner = set_log_owner(user.id if user is not None else None)
    return tok_user, tok_owner


def reset_request_user(token: RequestUserToken) -> None:
    tok_user, tok_owner = token
    _REQUEST_USER.reset(tok_user)
    reset_log_owner(tok_owner)
