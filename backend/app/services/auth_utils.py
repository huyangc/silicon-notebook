"""Compatibility re-export shim (definitions sunk to app.domain in B3).

The username/password helpers now live in ``app.domain.auth_utils`` (pure
stdlib, zero ``app.services``/``app.repositories`` dependency, so
``app.repositories`` adapters can import them directly). This module
re-exports every name unchanged so existing importers keep resolving to the
SAME objects without any call-site changes.

⚠ Test-speed seam: ``backend/tests/conftest.py``'s
``_fast_default_password_hashing`` fixture must monkeypatch
``app.domain.auth_utils.hash_password`` (not this module's re-exported
name) — repository call sites do a per-call lazy
``from app.domain.auth_utils import hash_password`` now, which resolves the
attribute on THAT module at call time, not on this shim.
"""
from __future__ import annotations

from app.domain.auth_utils import (
    USERNAME_RE,
    hash_password,
    is_valid_username,
    normalize_username,
    verify_password,
)

__all__ = [
    "USERNAME_RE",
    "hash_password",
    "is_valid_username",
    "normalize_username",
    "verify_password",
]
