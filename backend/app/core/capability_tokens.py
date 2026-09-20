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

# The two conversation-share namespaces. They matter beyond documentation
# because ONE anonymous endpoint pair (`/public/conversations/{token}` and its
# image sibling) serves BOTH features: a notebook-scoped conversation and a
# global (cross-library) one are stored in different tables with different
# re-authorization rules, and the prefix is what tells the route which branch
# owns a token. Minting and dispatch therefore read the same two names, so a
# renamed prefix cannot silently stop resolving.
NOTEBOOK_CONVERSATION_SHARE_PREFIX = "cshr"
GLOBAL_CONVERSATION_SHARE_PREFIX = "gshr"


def new_capability_token(prefix: str) -> str:
    """Return `<prefix>-<256 random bits>`; never reuse a row-id generator."""
    clean = str(prefix or "").strip()
    if not clean:
        raise ValueError("capability tokens need a namespace prefix")
    return f"{clean}-{secrets.token_urlsafe(CAPABILITY_TOKEN_BYTES)}"


def is_global_conversation_share_token(token: str) -> bool:
    """Whether this share token belongs to the GLOBAL conversation feature.

    The separator is part of the test on purpose: matching the bare prefix
    would also claim a hypothetical future `gshrx-...` namespace, and the two
    branches must partition the token space rather than overlap. A notebook
    conversation's `cshr-...` token can never satisfy this, so it keeps falling
    through to the notebook-scoped branch unchanged.
    """
    return str(token or "").startswith(f"{GLOBAL_CONVERSATION_SHARE_PREFIX}-")
