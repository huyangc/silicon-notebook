"""Request-local state used while one Ask turn is running.

These objects intentionally live outside the repository facade: the facade is
process-scoped, while both values are copied with the request context.
"""
from __future__ import annotations

import contextvars


_ASK_MODEL_ERRORS: "contextvars.ContextVar[list | None]" = contextvars.ContextVar(
    "ask_model_errors", default=None
)

_ASK_EMBED_CACHE: "contextvars.ContextVar[dict | None]" = contextvars.ContextVar(
    "ask_embed_cache", default=None
)
