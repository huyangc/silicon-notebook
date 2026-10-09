"""The pre-upgrade world of the Agent-token-tiers migration, shared by SQLite
``_migration_89`` (``test_agent_token_tiers_migration.py``) and PostgreSQL
``0069_agent_token_tiers.sql``
(``postgres/test_agent_token_tiers_migration_pg.py``).

Both backends seed these tokens, run their migration, and must arrive at the
same ``EXPECTED`` tiers -- the cross-backend equality. ``token_plain`` exists
afterwards and is NULL on every pre-existing row.
"""
from __future__ import annotations

import json

NOW = "2026-10-09T00:00:00+00:00"
USER = "u-tt"
NOTEBOOK = "nb-tt"
PROFILE = "agent-tt"

# id -> (stored scopes_json text, revoked?, expected tiers after migration)
TOKENS = {
    "tk-full": (json.dumps([
        "knowledge:read", "memory:read", "memory:read_candidates", "ask:execute",
        "agent_profile:read", "memory:propose", "knowhow:code", "sources:write",
        "sources:delete", "maintenance:execute", "agent_observation:write",
    ]), False, ["read", "ask", "contribute", "manage", "delete"]),
    "tk-know": ('["knowledge:read"]', False, ["read"]),
    # Memory reading folds into read (the user accepted that a knowledge-only
    # token now reads its owner's Memory too).
    "tk-mem": ('["memory:read", "memory:read_candidates"]', False, ["read"]),
    "tk-ask": ('["knowledge:read", "ask:execute"]', False, ["read", "ask"]),
    "tk-ask-only": ('["ask:execute"]', False, ["ask"]),
    "tk-propose": ('["memory:propose", "knowhow:code"]', False, ["contribute"]),
    # Only secondary capabilities: no main permission -> no tier.
    "tk-knowhow": ('["knowhow:code", "agent_observation:write"]', False, []),
    "tk-profile": ('["agent_profile:read"]', False, []),
    "tk-maint": ('["maintenance:execute"]', False, ["manage"]),
    "tk-write-del": ('["sources:delete", "sources:write"]', False, ["manage", "delete"]),
    # Revoked rows are rewritten too (one vocabulary in the table).
    "tk-revoked": ('["knowledge:read", "sources:delete"]', True, ["read", "delete"]),
    # Already tiers: kept, put in canonical order.
    "tk-tiers": ('["delete", "read"]', False, ["read", "delete"]),
    "tk-mixed": ('["read", "ask:execute", "bogus"]', False, ["read", "ask"]),
    "tk-empty": ("[]", False, []),
    "tk-object": ('{"knowledge:read": true}', False, []),
    "tk-nonstring": ('[1, null, "knowledge:read"]', False, ["read"]),
}
EXPECTED = {token_id: tiers for token_id, (_, _, tiers) in TOKENS.items()}
# Live (not revoked) tokens that end with no tier: the operator check counts these.
EMPTIED_LIVE = sum(
    1 for _, revoked, tiers in TOKENS.values() if not revoked and not tiers
)
assert EMPTIED_LIVE == 4  # tk-knowhow, tk-profile, tk-empty, tk-object


def seed(db, *, postgres: bool) -> None:
    p = "%s" if postgres else "?"
    js = "%s::jsonb" if postgres else "?"
    if postgres:
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            f"username,password_hash,password_salt,password_iterations) VALUES "
            f"({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p})",
            (USER, "tt@example.test", USER, "user", "active", NOW, NOW,
             "t00000001", "", "", 0),
        )
    else:
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at)"
            f" VALUES ({p},{p},{p},{p},{p},{p},{p})",
            (USER, "tt@example.test", USER, "user", "active", NOW, NOW),
        )
    db.execute(
        "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
        f"created_at,updated_at) VALUES ({p},{p},{p},{p},{p},{p},{p},{p})",
        (NOTEBOOK, NOTEBOOK, "", "", "ready", USER, NOW, NOW),
    )
    db.execute(
        "INSERT INTO agent_profiles(id,owner_id,name,description,status,created_at,"
        f"updated_at) VALUES ({p},{p},{p},{p},{p},{p},{p})",
        (PROFILE, USER, "Agent", "", "active", NOW, NOW),
    )
    for token_id, (scopes_json, revoked, _) in TOKENS.items():
        db.execute(
            "INSERT INTO agent_access_tokens(id,agent_profile_id,token_hash,scopes_json,"
            f"default_notebook_id,revoked_at,created_at) VALUES "
            f"({p},{p},{p},{js},{p},{p},{p})",
            (token_id, PROFILE, f"hash-{token_id}", scopes_json, NOTEBOOK,
             NOW if revoked else None, NOW),
        )


def snapshot(db) -> dict:
    """``{token id: (tiers, token_plain)}`` after the migration."""
    rows = db.execute(
        "SELECT id, scopes_json, token_plain FROM agent_access_tokens ORDER BY id"
    ).fetchall()
    result = {}
    for row in rows:
        scopes = row["scopes_json"]
        if isinstance(scopes, str):
            scopes = json.loads(scopes)
        result[row["id"]] = (scopes, row["token_plain"])
    return result


def assert_migrated(snap: dict) -> None:
    assert snap == {token_id: (tiers, None) for token_id, tiers in EXPECTED.items()}
