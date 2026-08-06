"""Unguessable tokens that are themselves an authorization grant.

Row identifiers and capability tokens have different threat models and must not
share a generator.  `event_logging.new_id` truncates a UUID to eight hex
characters — fine for a primary key behind a UNIQUE constraint, but only 32 bits
of entropy, which is not a credential.  A public share link is checked by
nothing except the token, is served by an unauthenticated endpoint, and is not
rate limited, so it has to be drawn from a cryptographic source and be wide
enough that enumeration and birthday collisions are both out of reach.
"""
from __future__ import annotations

import secrets

# 256 bits. `token_urlsafe` returns base64url, so the rendered token stays
# `[A-Za-z0-9_-]` and survives path segments and diagnostics redaction patterns.
CAPABILITY_TOKEN_BYTES = 32


def new_capability_token(prefix: str) -> str:
    """Return `<prefix>-<256 random bits>`; never reuse a row-id generator."""
    clean = str(prefix or "").strip()
    if not clean:
        raise ValueError("capability tokens need a namespace prefix")
    return f"{clean}-{secrets.token_urlsafe(CAPABILITY_TOKEN_BYTES)}"
