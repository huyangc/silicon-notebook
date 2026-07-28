from __future__ import annotations

from contextlib import contextmanager

from app.core.audit_actor import session_audit_principal
from app.models.identity import UserProfile
from app.services.knowhow.audit import project_change, resolve_audit_labels


def _user(*, user_id="user-1", username="", display_name="") -> UserProfile:
    return UserProfile(
        id=user_id,
        email="",
        username=username,
        display_name=display_name,
        role="user",
    )


def test_session_audit_principal_uses_canonical_fallback_chain():
    assert session_audit_principal(
        _user(username="  a00123456  ", display_name=" Alice ")
    ).audit_label == "a00123456"
    assert session_audit_principal(
        _user(username="   ", display_name=" Alice ")
    ).audit_label == "Alice"
    principal = session_audit_principal(_user(username="", display_name="   "))
    assert principal.identity_id == "user-1"
    assert principal.audit_label == "user-1"


class _Rows:
    def fetchall(self):
        return []


class _Connection:
    def __init__(self):
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def execute(self, statement, params):
        self.calls.append((statement, tuple(params)))
        return _Rows()


class _Database:
    def __init__(self):
        self.connection = _Connection()

    @contextmanager
    def connect(self):
        yield self.connection


class _ResolverRepo:
    def __init__(self):
        self.received = None

    def audit_labels_for_user_ids(self, user_ids):
        self.received = list(user_ids)
        return {"user-1": "a00123456"}


def test_response_resolver_preserves_unknown_and_caps_candidates():
    repo = _ResolverRepo()
    candidates = ["user-1", "Agent自由文本", "user-1"] + [
        f"unknown-{index}" for index in range(700)
    ]

    labels = resolve_audit_labels(repo, candidates)

    assert labels["user-1"] == "a00123456"
    assert labels["Agent自由文本"] == "Agent自由文本"
    assert len(repo.received) == 512
    assert repo.received[:2] == ["user-1", "Agent自由文本"]


def test_agent_delete_resolves_legacy_human_updater_only_in_before_snapshot():
    repo = _ResolverRepo()
    change = {
        "origin": "agent",
        "actor": "DeleteAgent",
        "payload": {
            "row_id": "r1",
            "column_id": "c1",
            "before": {"code_text": "old", "updated_by": "user-1"},
            "after": None,
        },
    }

    projected = project_change(repo, change)
    assert projected["actor"] == "DeleteAgent"
    assert projected["payload"]["before"]["updated_by"] == "a00123456"
    assert repo.received == ["user-1"]


def test_agent_name_collision_never_resolves_actor_after_or_current():
    repo = _ResolverRepo()
    change = {
        "origin": "agent",
        "actor": "user-1",
        "payload": {
            "before": {"updated_by": "legacy-unknown"},
            "after": {"updated_by": "user-1"},
            "current": {"updated_by": "user-1"},
        },
    }

    projected = project_change(repo, change)
    assert projected["actor"] == "user-1"
    assert projected["payload"]["before"]["updated_by"] == "legacy-unknown"
    assert projected["payload"]["after"]["updated_by"] == "user-1"
    assert projected["payload"]["current"]["updated_by"] == "user-1"
    assert repo.received == ["legacy-unknown"]
