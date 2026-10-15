"""MCP answer links, service layer (T5): issuing, and the anonymous resolve.

``resolve_answer_link`` is an anonymous security surface: every open re-runs
``get_ask``'s delivery rule against the ISSUING token as it stands now, plus
the session share's snapshot predicate. Each gate below has a case that turns
a serving link into ``None`` (404); an expired row still present raises
``AnswerLinkExpired`` (410) whatever the other gates say (plan D11).

Backend-agnostic: ``tests/test_answer_link_service.py`` (SQLite) and
``tests/postgres/test_answer_link_service_pg.py`` (PostgreSQL) both run every
case in ``CASES`` against the real repository and store of a running app.
Agent tokens are issued through the real service; notebook jobs are seeded as
rows the way ``test_public_conversation_api`` seeds conversations.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from itertools import count
from types import SimpleNamespace

import pytest

from app.core.capability_tokens import is_answer_link_token
from app.core.request_context import reset_request_user, set_request_user
from app.models.identity import UserProfile
from app.services.answer_links import (
    AnswerLinkExpired,
    AnswerLinkUnavailable,
    answer_link_job_kind,
    issue_answer_link,
    resolve_answer_link,
)
from tests.global_ask_share_cases import insert_conversation, insert_job

_PASSWORD = "pw12345678"
_USERNAMES = iter(f"t{index:08d}" for index in range(1, 9999))
_IDS = count(1)
# Passed as a parameter: a literal ``?`` in the SQL text would be rewritten
# into a PostgreSQL placeholder.
_QUESTION = "链接问题?"


class _TranslatingConnection:
    """Raw seeding SQL is written with ``?``; PostgreSQL wants its own marker."""

    def __init__(self, conn, sql):
        self._conn = conn
        self._sql = sql

    def execute(self, text, params=()):
        return self._conn.execute(self._sql(text), params)


@contextmanager
def _write(env):
    with env.db.write() as conn:
        yield _TranslatingConnection(conn, env.sql)


def make_env(client, repo):
    """The scenario environment over a running app's repository (either backend)."""
    store = repo._runtime.global_ask_store
    return SimpleNamespace(
        client=client, repo=repo, store=repo._runtime.answer_links,
        global_service=repo._runtime.global_ask_service(),
        db=store.database, sql=store._sql,
    )


def _settings(base: str = "http://10.0.0.1:3000/kb", ttl: int = 168, grace: int = 168):
    return SimpleNamespace(
        public_web_base_url=base,
        mcp_answer_link_ttl_hours=ttl,
        mcp_answer_link_grace_hours=grace,
    )


def _user(env) -> tuple[dict, str]:
    username = next(_USERNAMES)
    env.client.post("/api/auth/register",
                    json={"username": username, "password": _PASSWORD})
    token = env.client.post(
        "/api/auth/login", json={"username": username, "password": _PASSWORD}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    return headers, env.client.get("/api/me", headers=headers).json()["id"]


def _notebook(env, headers: dict, name: str = "库") -> str:
    return env.client.post("/api/notebooks", json={"name": name},
                           headers=headers).json()["id"]


def _token(env, owner_id: str, notebooks: list[str], *, scopes=("ask",),
           allow_links: bool = True):
    profile = env.repo.create_agent_profile(owner_id, f"bot-{next(_IDS)}", "")
    issued = env.repo.issue_agent_token(
        owner_id, profile.id, list(scopes), notebooks[0], list(notebooks), None,
        allow_answer_links=allow_links,
    )
    principal = env.repo.refresh_agent_principal(issued.id)
    assert principal is not None
    return principal


def _set_access(env, principal, *, scopes=None, notebooks=None, allow_links=None):
    notebooks = list(notebooks if notebooks is not None else principal.notebook_ids)
    env.repo.update_agent_token_access(
        principal.owner_id, principal.token_id,
        list(scopes if scopes is not None else principal.scopes),
        notebooks[0], notebooks, None,
        allow_answer_links=(principal.allow_answer_links if allow_links is None
                            else allow_links),
    )


def _payload(library_id: str = "") -> dict:
    anchor = {"key": "k1", "object_id": "OBJ-1", "label": "标签",
              "snippet": "锚点摘录", "source_title": "论文"}
    if library_id:
        anchor["notebook_id"] = library_id
    return {"conclusion": "结论 [k1]。", "answer": "答案 [k1]。",
            "evidence_level": "grounded", "anchors": [anchor], "citations": []}


def _seed_job(env, notebook_id: str, owner_id: str, *, via: str = "mcp",
              status: str = "done", memory: bool = False, output: str = "answer",
              payload: dict | None = None) -> tuple[str, str]:
    """One conversation + answer + ask job; returns (job_id, conversation_id)."""
    n = next(_IDS)
    job_id, cid, answer_id = f"askjob-t5-{n}", f"conv-t5-{n}", f"ans-t5-{n}"
    body = json.dumps(payload or _payload(), ensure_ascii=False)
    with _write(env) as conn:
        conn.execute(
            "INSERT INTO conversations (id, notebook_id, title, created_by, "
            "created_at, updated_at) VALUES (?, ?, '会话', ?, "
            "'2026-01-01T00:00:00', '2026-01-01T00:00:02')",
            (cid, notebook_id, owner_id),
        )
        conn.execute(
            "INSERT INTO answers (id, notebook_id, conversation_id, question, "
            "payload, created_at) VALUES (?, ?, ?, ?, ?, '2026-01-01T00:00:01')",
            (answer_id, notebook_id, cid, _QUESTION, body),
        )
        conn.execute(
            "INSERT INTO ask_jobs (id, notebook_id, conversation_id, created_by, "
            "mode, question, status, answer_id, created_at, updated_at, "
            "submitted_via, memory_access, output) VALUES "
            "(?, ?, ?, ?, 'reasoning', ?, ?, ?, '2026-01-01T00:00:00', "
            "'2026-01-01T00:00:01', ?, ?, ?)",
            (job_id, notebook_id, cid, owner_id, _QUESTION, status,
             # Every job names a stored answer, whatever its status: only the
             # status gate may keep a not-done job's answer off the page.
             answer_id, via, 1 if memory else 0, output),
        )
    return job_id, cid


def _issue(env, job_id: str, principal, ttl_hours: int | None = None):
    return issue_answer_link(env.store, _settings(), job_id=job_id,
                             principal=principal, ttl_hours=ttl_hours)


def _link_token(link) -> str:
    return link.url.rsplit("/c/", 1)[1]


def _resolve(env, token: str, *, global_service=None):
    return resolve_answer_link(
        token, store=env.store, repo=env.repo,
        global_service=global_service or env.global_service,
    )


def _ready(env, *, scopes=("ask",), memory=False, payload=None):
    owner, owner_id = _user(env)
    nb = _notebook(env, owner)
    principal = _token(env, owner_id, [nb], scopes=scopes)
    job_id, cid = _seed_job(env, nb, owner_id, memory=memory, payload=payload)
    token = _link_token(_issue(env, job_id, principal))
    assert _resolve(env, token) is not None
    return SimpleNamespace(owner=owner, owner_id=owner_id, nb=nb,
                           principal=principal, job_id=job_id, cid=cid, token=token)


def _expire(env, token: str) -> None:
    with _write(env) as conn:
        conn.execute("UPDATE answer_links SET expires_at = ? WHERE token = ?",
                     ("2020-01-01T00:00:00.000000+00:00", token))


# ------------------------------------------------------------------ issuing


def issue_builds_url_from_public_web_base_url_and_reuses(env):
    owner, owner_id = _user(env)
    nb = _notebook(env, owner)
    principal = _token(env, owner_id, [nb])
    job_id, _ = _seed_job(env, nb, owner_id)

    first = issue_answer_link(env.store, _settings("http://10.0.0.1:3000/kb/"),
                              job_id=job_id, principal=principal, ttl_hours=2)
    assert first.url.startswith("http://10.0.0.1:3000/kb/c/alnk-")
    assert is_answer_link_token(_link_token(first))
    assert first.reused is False
    row = env.store.answer_link(_link_token(first))
    assert row["job_kind"] == "notebook" and row["job_id"] == job_id
    assert row["owner_id"] == owner_id and row["agent_token_id"] == principal.token_id

    # A retry (or a get_ask re-issue) reuses the live link with its ORIGINAL
    # expiry: link_ttl_hours only shapes a new link (D10).
    again = issue_answer_link(env.store, _settings(), job_id=job_id,
                              principal=principal, ttl_hours=None)
    assert again.url.endswith(_link_token(first))
    assert again.reused is True and again.expires_at == first.expires_at

    # Another token of the same owner gets its own link (D2).
    other = _token(env, owner_id, [nb])
    theirs = _issue(env, job_id, other)
    assert _link_token(theirs) != _link_token(first) and theirs.reused is False


def issue_ttl_is_validated_never_clamped(env):
    owner, owner_id = _user(env)
    nb = _notebook(env, owner)
    principal = _token(env, owner_id, [nb])
    job_id, _ = _seed_job(env, nb, owner_id)
    for bad in (0, -1, 169):
        with pytest.raises(ValueError):
            _issue(env, job_id, principal, ttl_hours=bad)
    with pytest.raises(ValueError):
        _issue(env, "conv-not-a-job", principal)
    assert answer_link_job_kind("gask-1") == "global"
    assert answer_link_job_kind("askjob-1") == "notebook"


def issue_without_public_web_base_url_is_unavailable(env):
    owner, owner_id = _user(env)
    nb = _notebook(env, owner)
    principal = _token(env, owner_id, [nb])
    job_id, _ = _seed_job(env, nb, owner_id)
    with pytest.raises(AnswerLinkUnavailable):
        issue_answer_link(env.store, _settings(base=""), job_id=job_id,
                          principal=principal, ttl_hours=None)
    # Nothing was written: the next real issue is a fresh link.
    assert _issue(env, job_id, principal).reused is False


# ------------------------------------------------------------------ resolving


def resolve_valid_notebook_link_returns_one_turn(env):
    case = _ready(env)
    row = _resolve(env, case.token)
    assert row["title"] == "" and row["shared_through_at"] == ""
    assert row["notebook_id"] == case.nb and row["created_by"] == case.owner_id
    assert row["expires_at"] == env.store.answer_link(case.token)["expires_at"]
    assert row["expires_at"].endswith("+00:00")
    assert len(row["turns"]) == 1
    turn = row["turns"][0]
    assert turn["question"] == _QUESTION
    assert turn["payload"]["answer"] == "答案 [k1]。"
    assert "notebook_ids" not in row


def unknown_token_is_none(env):
    assert _resolve(env, "alnk-nope") is None


def expired_raises_expired_even_if_other_gates_fail(env):
    case = _ready(env)
    _expire(env, case.token)
    with pytest.raises(AnswerLinkExpired):
        _resolve(env, case.token)
    # Every other gate failing too: still the 410 signal, not a 404.
    env.repo.revoke_agent_token(case.owner_id, case.principal.token_id)
    env.repo.delete_conversation(case.cid)
    with pytest.raises(AnswerLinkExpired):
        _resolve(env, case.token)


def revoked_token_expired_token_disabled_profile_are_none(env):
    revoked = _ready(env)
    env.repo.revoke_agent_token(revoked.owner_id, revoked.principal.token_id)
    assert _resolve(env, revoked.token) is None

    expired = _ready(env)
    with _write(env) as conn:
        conn.execute("UPDATE agent_access_tokens SET expires_at = ? WHERE id = ?",
                     ("2020-01-01T00:00:00Z", expired.principal.token_id))
    assert _resolve(env, expired.token) is None

    disabled = _ready(env)
    env.repo.update_agent_profile(disabled.principal.profile_id,
                                  disabled.owner_id, {"status": "revoked"})
    assert _resolve(env, disabled.token) is None

    deleted = _ready(env)
    with _write(env) as conn:
        conn.execute("DELETE FROM agent_token_notebooks WHERE token_id = ?",
                     (deleted.principal.token_id,))
        conn.execute("DELETE FROM agent_access_tokens WHERE id = ?",
                     (deleted.principal.token_id,))
    assert _resolve(env, deleted.token) is None


def answer_links_switched_off_or_ask_tier_removed_is_none(env):
    switched = _ready(env)
    _set_access(env, switched.principal, allow_links=False)
    assert _resolve(env, switched.token) is None
    _set_access(env, switched.principal, allow_links=True)
    assert _resolve(env, switched.token) is not None  # same link revives

    tier = _ready(env)
    _set_access(env, tier.principal, scopes=["read"], allow_links=False)
    assert _resolve(env, tier.token) is None


def allowlist_narrowed_is_none(env):
    case = _ready(env)
    other = _notebook(env, case.owner, "另一个库")
    _set_access(env, case.principal, notebooks=[other])
    assert _resolve(env, case.token) is None
    _set_access(env, case.principal, notebooks=[other, case.nb])
    assert _resolve(env, case.token) is not None


def owner_lost_read_is_none(env):
    owner, owner_id = _user(env)
    nb = _notebook(env, owner)
    reader, reader_id = _user(env)
    group_id = env.client.post("/api/groups", json={"name": "组"},
                               headers=owner).json()["id"]
    assert env.client.put(f"/api/groups/{group_id}/members/{reader_id}",
                          json={"role": "member"}, headers=owner).status_code == 200
    grant = env.client.post(
        f"/api/notebooks/{nb}/grants",
        json={"principal_type": "group", "principal_id": group_id, "role": "viewer"},
        headers=owner,
    )
    assert grant.status_code == 200, grant.text
    principal = _token(env, reader_id, [nb])
    job_id, _ = _seed_job(env, nb, reader_id)
    token = _link_token(_issue(env, job_id, principal))
    assert _resolve(env, token) is not None

    assert env.client.delete(f"/api/groups/{group_id}/members/{reader_id}",
                             headers=owner).status_code in (200, 204)
    assert _resolve(env, token) is None


def unmounted_quoted_library_is_none(env):
    """The session share's D-3 predicate, run through the shared function."""
    owner, owner_id = _user(env)
    nb = _notebook(env, owner)
    library = _notebook(env, owner, "参考库")
    assert env.client.put(f"/api/notebooks/{nb}/bases",
                          json={"base_notebook_ids": [library]},
                          headers=owner).status_code == 200
    principal = _token(env, owner_id, [nb])
    job_id, _ = _seed_job(env, nb, owner_id, payload=_payload(library))
    token = _link_token(_issue(env, job_id, principal))
    assert _resolve(env, token) is not None
    assert env.client.put(f"/api/notebooks/{nb}/bases",
                          json={"base_notebook_ids": []},
                          headers=owner).status_code == 200
    assert _resolve(env, token) is None


def memory_job_without_memory_read_now_is_none(env):
    no_read = _ready(env, scopes=("ask", "read"), memory=True)
    _set_access(env, no_read.principal, scopes=["ask"])
    assert _resolve(env, no_read.token) is None
    _set_access(env, no_read.principal, scopes=["ask", "read"])
    assert _resolve(env, no_read.token) is not None

    # A run without the Memory channel needs no read tier.
    plain = _ready(env, scopes=("ask",), memory=False)
    assert _resolve(env, plain.token) is not None

    # A memory run linked by an ask-only token never serves.
    owner, owner_id = _user(env)
    nb = _notebook(env, owner)
    principal = _token(env, owner_id, [nb], scopes=("ask",))
    job_id, _ = _seed_job(env, nb, owner_id, memory=True)
    assert _resolve(env, _link_token(_issue(env, job_id, principal))) is None


def browser_job_running_job_evidence_job_deleted_job_are_none(env):
    owner, owner_id = _user(env)
    nb = _notebook(env, owner)
    principal = _token(env, owner_id, [nb])
    for kwargs in ({"via": "web"}, {"status": "running"}, {"output": "evidence"}):
        job_id, _ = _seed_job(env, nb, owner_id, **kwargs)
        assert _resolve(env, _link_token(_issue(env, job_id, principal))) is None, kwargs

    job_id, cid = _seed_job(env, nb, owner_id)
    token = _link_token(_issue(env, job_id, principal))
    assert _resolve(env, token) is not None
    env.repo.delete_conversation(cid)
    assert _resolve(env, token) is None

    # A job of another user, linked through this owner's token, is not served.
    stranger, stranger_id = _user(env)
    their_nb = _notebook(env, stranger)
    their_job, _ = _seed_job(env, their_nb, stranger_id)
    assert _resolve(env, _link_token(_issue(env, their_job, principal))) is None
    # ... nor one another user started inside this owner's own notebook.
    foreign_job, _ = _seed_job(env, nb, stranger_id)
    assert _resolve(env, _link_token(_issue(env, foreign_job, principal))) is None


def ambient_request_user_does_not_change_the_answer(env):
    """The anonymous resolve takes every identity from the link row: an
    administrator bound as the request user neither rescues a dead link nor
    changes whose access a live one is checked against."""
    dead = _ready(env)
    other = _notebook(env, dead.owner, "另一个库")
    _set_access(env, dead.principal, notebooks=[other])
    live = _ready(env)
    admin = env.repo.current_user()
    assert admin.role == "admin"
    marker = set_request_user(UserProfile(
        id=admin.id, email=admin.email, display_name=admin.display_name,
        role="admin",
    ))
    try:
        assert _resolve(env, dead.token) is None
        row = _resolve(env, live.token)
    finally:
        reset_request_user(marker)
    assert row is not None and row["created_by"] == live.owner_id


# ------------------------------------------------------------------ global jobs


class _RecordingGlobal:
    def __init__(self, result):
        self.result = result
        self.calls: list[tuple] = []

    def public_answer_job(self, job_id, *, owner_id, allowed_notebook_ids):
        self.calls.append((job_id, owner_id, allowed_notebook_ids))
        return None if self.result is None else dict(self.result)


def global_link_delegates_to_public_answer_job_with_allowlist(env):
    owner, owner_id = _user(env)
    nb_a = _notebook(env, owner, "甲")
    nb_b = _notebook(env, owner, "乙")
    principal = _token(env, owner_id, [nb_a, nb_b])
    link = _issue(env, "gask-t5-fake", principal)
    assert env.store.answer_link(_link_token(link))["job_kind"] == "global"

    fake = _RecordingGlobal({"title": "", "turns": [{}], "notebook_ids": frozenset({nb_a}),
                             "created_by": owner_id})
    row = _resolve(env, _link_token(link), global_service=fake)
    # The token's real allowlist is the ceiling -- a concrete set, never
    # ``None`` (which would mean "no ceiling").
    assert fake.calls == [("gask-t5-fake", owner_id, {nb_a, nb_b})]
    assert row["expires_at"] == link.expires_at
    assert row["notebook_ids"] == frozenset({nb_a})

    # Narrowed allowlist: the narrowed set is what is passed.
    _set_access(env, principal, notebooks=[nb_a])
    _resolve(env, _link_token(link), global_service=fake)
    assert fake.calls[-1] == ("gask-t5-fake", owner_id, {nb_a})

    # The global helper's refusal is a 404; dead token never reaches it.
    assert _resolve(env, _link_token(link), global_service=_RecordingGlobal(None)) is None
    env.repo.revoke_agent_token(owner_id, principal.token_id)
    untouched = _RecordingGlobal({})
    assert _resolve(env, _link_token(link), global_service=untouched) is None
    assert untouched.calls == []


def global_link_rechecks_the_issuer_before_delegating(env):
    """The issuer gates run for a global link too, before the global helper is
    consulted: the ``ask`` tier held now, and the link's owner still being the
    token's owner."""
    owner, owner_id = _user(env)
    nb = _notebook(env, owner)
    principal = _token(env, owner_id, [nb])
    token = _link_token(_issue(env, "gask-t5-issuer", principal))
    serving = {"title": "", "turns": [{}], "notebook_ids": frozenset({nb}),
               "created_by": owner_id}
    assert _resolve(env, token, global_service=_RecordingGlobal(serving)) is not None

    # The ask tier gone while the link switch stays on (a stored state the
    # access form cannot produce, so written directly).
    with _write(env) as conn:
        conn.execute("UPDATE agent_access_tokens SET scopes_json = ? WHERE id = ?",
                     ('["read"]', principal.token_id))
    untouched = _RecordingGlobal(serving)
    assert _resolve(env, token, global_service=untouched) is None
    assert untouched.calls == []
    with _write(env) as conn:
        conn.execute("UPDATE agent_access_tokens SET scopes_json = ? WHERE id = ?",
                     ('["ask"]', principal.token_id))
    assert _resolve(env, token, global_service=_RecordingGlobal(serving)) is not None

    # A link row naming another owner than the issuing token's.
    _, stranger_id = _user(env)
    with _write(env) as conn:
        conn.execute("UPDATE answer_links SET owner_id = ? WHERE token = ?",
                     (stranger_id, token))
    untouched = _RecordingGlobal(serving)
    assert _resolve(env, token, global_service=untouched) is None
    assert untouched.calls == []


def global_link_end_to_end_rechecks_allowlist_and_read(env):
    """Real ``GlobalAskService``: the allowlist ceiling and the owner's read
    access both gate every cited library."""
    owner, owner_id = _user(env)
    nb_a = _notebook(env, owner, "甲")
    nb_b = _notebook(env, owner, "乙")
    principal = _token(env, owner_id, [nb_a, nb_b])
    store = env.repo._runtime.global_ask_store
    conversation_id, _ = insert_conversation(store, "gconv-t5", owner_id)
    payload = {
        "job_id": "gask-t5-real", "status": "done", "question": "跨库问题",
        "resolved_notebook_ids": [nb_a, nb_b], "cited_notebook_ids": [nb_b],
        "answer": {"answer": "跨库答案", "citations": [{"notebook_id": nb_b}]},
    }
    insert_job(store, conversation_id, owner_id, "gask-t5-real",
               "2026-01-01T00:00:01", payload=payload)
    with store.database.write() as db:
        db.execute(store._sql(
            "UPDATE global_ask_jobs SET submitted_via='mcp' WHERE id=?"), ("gask-t5-real",))
    token = _link_token(_issue(env, "gask-t5-real", principal))

    row = _resolve(env, token)
    assert row is not None
    assert row["notebook_ids"] == frozenset({nb_a, nb_b})
    assert [turn["question"] for turn in row["turns"]] == ["跨库问题"]

    _set_access(env, principal, notebooks=[nb_a])
    assert _resolve(env, token) is None
    _set_access(env, principal, notebooks=[nb_a, nb_b])
    assert _resolve(env, token) is not None

    with _write(env) as conn:
        conn.execute("UPDATE agent_access_tokens SET expires_at = ? WHERE id = ?",
                     ("2020-01-01T00:00:00Z", principal.token_id))
    assert _resolve(env, token) is None


CASES = {
    "issue_builds_url_from_public_web_base_url_and_reuses": issue_builds_url_from_public_web_base_url_and_reuses,
    "issue_ttl_is_validated_never_clamped": issue_ttl_is_validated_never_clamped,
    "issue_without_public_web_base_url_is_unavailable": issue_without_public_web_base_url_is_unavailable,
    "resolve_valid_notebook_link_returns_one_turn": resolve_valid_notebook_link_returns_one_turn,
    "unknown_token_is_none": unknown_token_is_none,
    "expired_raises_expired_even_if_other_gates_fail": expired_raises_expired_even_if_other_gates_fail,
    "revoked_token_expired_token_disabled_profile_are_none": revoked_token_expired_token_disabled_profile_are_none,
    "answer_links_switched_off_or_ask_tier_removed_is_none": answer_links_switched_off_or_ask_tier_removed_is_none,
    "allowlist_narrowed_is_none": allowlist_narrowed_is_none,
    "owner_lost_read_is_none": owner_lost_read_is_none,
    "unmounted_quoted_library_is_none": unmounted_quoted_library_is_none,
    "memory_job_without_memory_read_now_is_none": memory_job_without_memory_read_now_is_none,
    "browser_job_running_job_evidence_job_deleted_job_are_none": browser_job_running_job_evidence_job_deleted_job_are_none,
    "ambient_request_user_does_not_change_the_answer": ambient_request_user_does_not_change_the_answer,
    "global_link_delegates_to_public_answer_job_with_allowlist": global_link_delegates_to_public_answer_job_with_allowlist,
    "global_link_rechecks_the_issuer_before_delegating": global_link_rechecks_the_issuer_before_delegating,
    "global_link_end_to_end_rechecks_allowlist_and_read": global_link_end_to_end_rechecks_allowlist_and_read,
}
