from __future__ import annotations

from app.repositories.sqlite.identity_store import _now, _session_expiry, _new_user_id
from app.core.request_context import (
    _REQUEST_USER, get_request_user, request_user_id, set_request_user, reset_request_user,
)

# Compatibility re-exports for historical tests/scripts. IdentityStore is the
# production owner; patch new code at that owner.


class SQLiteIdentityMixin:
    """Compatibility marker retained for historical imports only."""

    pass
