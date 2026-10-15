"""MCP answer links on the anonymous public routes (T6).

``GET /api/public/conversations/{token}`` and its image endpoint dispatch an
``alnk-`` token to ``resolve_answer_link`` (every re-check lives there, T5);
the routes only map the outcome and project. What these scenarios pin:

* the page: one whitelisted turn plus ``expires_at``, no gate field, no
  addressable id, ``Cache-Control: no-store``, no session needed;
* expiry: the page says 410 with the Chinese user message (marked as a user
  error) while the row waits for the sweep; the image endpoint says 404;
* after the sweep, a revoked issuing token, or an unknown link: plain 404;
* image aliases are keyed by the link token -- a ``cshr`` alias of the same
  image does not open under the link, nor the reverse;
* the three namespaces never resolve one another's tokens, and a session or
  global share's page carries no ``expires_at`` key at all.

Time is an injected clock on the answer-link store (wall clock and the sweep
throttle's monotonic clock), never a sleep. Backend-agnostic:
``tests/test_public_answer_link_api.py`` (SQLite) and
``tests/postgres/test_public_answer_link_api_pg.py`` run every case in ``CASES``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from itertools import count
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.api.deps import USER_MESSAGE_HEADER
from app.core.request_context import reset_request_user, set_request_user
from app.models.identity import UserProfile
from app.services.knowhow.assets import AssetService
from tests.answer_link_service_cases import (
    _issue,
    _link_token,
    _notebook,
    _payload,
    _seed_job,
    _set_access,
    _token,
    _user,
    _write,
    make_env,
)
from tests.global_ask_share_cases import insert_conversation, insert_job

__all__ = ["CASES", "make_env"]

_IMAGE_BYTES = b"\x89PNG\r\n\x1a\nT6-answer-link-image"
_IDS = count(1)
_GATE_FIELDS = ("notebook_id", "notebook_ids", "created_by", "source_id")


class _Clock:
    """Drives the answer-link store's wall clock (expiry, issuing, sweeping)
    and its monotonic clock (the once-an-hour sweep throttle) together."""

    def __init__(self, env) -> None:
        store = env.store
        as_text = isinstance(store.now(), str)  # SQLite keeps text, PG datetimes
        self.at = datetime.now(timezone.utc)
        self.mono = 0.0
        store.now = (lambda: self.at.isoformat()) if as_text else (lambda: self.at)
        store._monotonic = lambda: self.mono

    def advance(self, *, hours: float) -> None:
        self.at += timedelta(hours=hours)
        self.mono += hours * 3600


def _page(env, token: str):
    return env.client.get(f"/api/public/conversations/{token}")


def _image(env, token: str, alias: str):
    return env.client.get(f"/api/public/conversations/{token}/assets/{alias}")


def _save_asset(env, notebook_id: str, owner_id: str) -> str:
    asset = AssetService(env.repo).save(
        notebook_id, "figure.png", "image/png", _IMAGE_BYTES, owner_id
    )
    return asset["id"]


def _with_image(payload: dict, asset_id: str) -> dict:
    payload["anchors"][0]["images"] = [
        {"element_id": "IMGELE-secret", "asset_id": asset_id, "caption": "配图说明"}
    ]
    # The full anchor shape: the session-share issue path parses the stored
    # answer as an ``AskResponse``.
    payload["anchors"][0].update(source_id="SRC-secret", element_id="ELE-secret",
                                 object_type="concept", name="名称")
    return payload


def _seeded_ids(job_id: str) -> tuple[str, ...]:
    """Every row id seeding put behind a job: the job itself and, for a
    notebook job, its answer and conversation (``_seed_job`` numbers all
    three alike)."""
    if job_id.startswith("askjob-"):
        n = job_id.rsplit("-", 1)[1]
        return (job_id, f"ans-t5-{n}", f"conv-t5-{n}")
    return (job_id,)


def _assert_served(env, token: str, *, expires_at: str, job_id: str,
                   page=None) -> dict:
    page = page if page is not None else _page(env, token)
    assert page.status_code == 200, page.text
    assert page.headers["cache-control"] == "no-store"
    body = page.json()
    assert body["expires_at"] == expires_at
    assert len(body["turns"]) == 1
    for field in _GATE_FIELDS:
        assert field not in body
        assert f'"{field}"' not in page.text
    for secret in ("IMGELE-secret", "SRC-secret", "ELE-secret"):
        assert secret not in page.text
    # No row id behind the link reaches the reader, under any namespace.
    for row_id in _seeded_ids(job_id):
        assert row_id not in page.text, row_id
    for namespace in ("askjob-", "gask-", "ans-t5-", "conv-t5-", "gconv-t6-"):
        assert namespace not in page.text, namespace
    return body


def _assert_expired_page(env, token: str) -> None:
    page = _page(env, token)
    assert page.status_code == 410, page.text
    assert page.json()["detail"] == "链接已过期，请重新提问"
    assert page.headers.get(USER_MESSAGE_HEADER) == "1"


def _seed_global_job(env, owner_id: str, notebook_id: str, payload: dict) -> str:
    store = env.repo._runtime.global_ask_store
    n = next(_IDS)
    conversation_id, _ = insert_conversation(store, f"gconv-t6-{n}", owner_id)
    job_id = f"gask-t6-{n}"
    insert_job(store, conversation_id, owner_id, job_id, "2026-01-01T00:00:01",
               payload={
                   "job_id": job_id, "status": "done", "question": "跨库问题",
                   "resolved_notebook_ids": [notebook_id],
                   "cited_notebook_ids": [notebook_id],
                   "answer": payload,
               })
    with store.database.write() as db:
        db.execute(store._sql(
            "UPDATE global_ask_jobs SET submitted_via='mcp' WHERE id=?"), (job_id,))
    return job_id


def _lifecycle(env, *, kind: str) -> None:
    """Issue -> page 200 with expires_at -> image 200 -> expired: page 410,
    image 404 -> sweep -> 404; a second token's link dies on revocation."""
    clock = _Clock(env)
    owner, owner_id = _user(env)
    nb = _notebook(env, owner)
    asset_id = _save_asset(env, nb, owner_id)
    if kind == "notebook":
        job_id, _ = _seed_job(env, nb, owner_id, payload=_with_image(_payload(), asset_id))
    else:
        job_id = _seed_global_job(env, owner_id, nb,
                                  _with_image(_payload(nb), asset_id))
    short_lived = _token(env, owner_id, [nb])
    other = _token(env, owner_id, [nb])
    first = _issue(env, job_id, short_lived, ttl_hours=1)
    second = _issue(env, job_id, other)
    token, kept = _link_token(first), _link_token(second)

    body = _assert_served(env, token, expires_at=first.expires_at, job_id=job_id)
    assert body["title"] == ""
    assert body["turns"][0]["question"] in ("链接问题?", "跨库问题")
    alias = body["turns"][0]["images"][0]["alias"]
    image = _image(env, token, alias)
    assert image.status_code == 200, image.text
    assert image.content == _IMAGE_BYTES
    assert image.headers["cache-control"] == "no-store"

    # Past expiry, the row still waiting for the sweep: the page says so, the
    # image endpoint does not tell it from an unknown link.
    clock.advance(hours=2)
    _assert_expired_page(env, token)
    assert _image(env, token, alias).status_code == 404
    _assert_served(env, kept, expires_at=second.expires_at, job_id=job_id)

    # Revoking the issuing token kills its link at once.
    env.repo.revoke_agent_token(owner_id, other.token_id)
    assert _page(env, kept).status_code == 404

    # Past the grace period, the next issue sweeps the expired row: 404.
    clock.advance(hours=170)
    assert env.store.answer_link(token) is not None
    third, _ = _seed_job(env, nb, owner_id)
    _issue(env, third, short_lived)
    assert env.store.answer_link(token) is None
    page = _page(env, token)
    assert page.status_code == 404
    assert USER_MESSAGE_HEADER not in page.headers
    assert _image(env, token, alias).status_code == 404


def notebook_link_lifecycle_end_to_end(env):
    _lifecycle(env, kind="notebook")


def global_link_lifecycle_end_to_end(env):
    _lifecycle(env, kind="global")


def unknown_alnk_token_is_404_on_page_and_image(env):
    page = _page(env, "alnk-doesnotexist")
    assert page.status_code == 404
    assert USER_MESSAGE_HEADER not in page.headers
    assert _image(env, "alnk-doesnotexist", "0" * 32).status_code == 404


def global_link_rechecks_every_cited_library(env):
    """Losing read access to one quoted library kills the whole global link."""
    owner, owner_id = _user(env)
    nb_a = _notebook(env, owner, "甲")
    reader, reader_id = _user(env)
    nb_b = _notebook(env, reader, "乙")
    group_id = env.client.post("/api/groups", json={"name": "组"},
                               headers=reader).json()["id"]
    assert env.client.put(f"/api/groups/{group_id}/members/{owner_id}",
                          json={"role": "member"}, headers=reader).status_code == 200
    assert env.client.post(
        f"/api/notebooks/{nb_b}/grants",
        json={"principal_type": "group", "principal_id": group_id, "role": "viewer"},
        headers=reader,
    ).status_code == 200
    principal = _token(env, owner_id, [nb_a, nb_b])
    store = env.repo._runtime.global_ask_store
    n = next(_IDS)
    conversation_id, _ = insert_conversation(store, f"gconv-t6-{n}", owner_id)
    job_id = f"gask-t6-{n}"
    insert_job(store, conversation_id, owner_id, job_id, "2026-01-01T00:00:01",
               payload={
                   "job_id": job_id, "status": "done", "question": "跨库问题",
                   "resolved_notebook_ids": [nb_a, nb_b],
                   "cited_notebook_ids": [nb_a, nb_b],
                   "answer": {"answer": "跨库答案",
                              "citations": [{"notebook_id": nb_a},
                                            {"notebook_id": nb_b}]},
               })
    with store.database.write() as db:
        db.execute(store._sql(
            "UPDATE global_ask_jobs SET submitted_via='mcp' WHERE id=?"), (job_id,))
    link = _issue(env, job_id, principal)
    token = _link_token(link)
    _assert_served(env, token, expires_at=link.expires_at, job_id=job_id)

    assert env.client.delete(f"/api/groups/{group_id}/members/{owner_id}",
                             headers=reader).status_code in (200, 204)
    assert _page(env, token).status_code == 404


def cshr_alias_does_not_open_under_the_link_nor_the_reverse(env):
    _Clock(env)
    owner, owner_id = _user(env)
    nb = _notebook(env, owner)
    asset_id = _save_asset(env, nb, owner_id)
    job_id, cid = _seed_job(env, nb, owner_id,
                            payload=_with_image(_payload(), asset_id))
    link = _link_token(_issue(env, job_id, _token(env, owner_id, [nb])))
    shared = env.client.post(f"/api/notebooks/{nb}/conversations/{cid}/share",
                             headers=owner)
    assert shared.status_code == 200, shared.text
    share = shared.json()["share_token"]

    share_page = _page(env, share)
    assert share_page.status_code == 200
    # A session share's page has no expiry: the key is absent, not null.
    assert "expires_at" not in share_page.json()
    share_alias = share_page.json()["turns"][0]["images"][0]["alias"]
    link_alias = _page(env, link).json()["turns"][0]["images"][0]["alias"]
    assert share_alias != link_alias

    assert _image(env, share, share_alias).status_code == 200
    assert _image(env, link, link_alias).status_code == 200
    assert _image(env, link, share_alias).status_code == 404
    assert _image(env, share, link_alias).status_code == 404


def prefixes_never_resolve_one_anothers_tokens(env):
    """The capability namespaces partition the token space: the same random
    body under another prefix opens nothing."""
    owner, owner_id = _user(env)
    nb = _notebook(env, owner)
    payload = _payload()
    payload["anchors"][0].update(object_type="concept", name="名称")
    job_id, cid = _seed_job(env, nb, owner_id, payload=payload)
    link = _link_token(_issue(env, job_id, _token(env, owner_id, [nb])))
    share = env.client.post(f"/api/notebooks/{nb}/conversations/{cid}/share",
                            headers=owner).json()["share_token"]
    assert _page(env, link).status_code == 200
    assert _page(env, share).status_code == 200
    link_body = link.split("-", 1)[1]
    share_prefix, share_body = share.split("-", 1)
    for forged in (f"gshr-{link_body}", f"{share_prefix}-{link_body}",
                   f"alnk-{share_body}", link_body):
        assert _page(env, forged).status_code == 404, forged


def expired_page_is_410_even_when_other_gates_also_fail(env):
    """Plan D11's order on the wire: an expired row is 410 whatever the rest
    would say; once it is gone it is the plain 404."""
    clock = _Clock(env)
    owner, owner_id = _user(env)
    nb = _notebook(env, owner)
    principal = _token(env, owner_id, [nb])
    job_id, cid = _seed_job(env, nb, owner_id)
    token = _link_token(_issue(env, job_id, principal, ttl_hours=1))
    clock.advance(hours=1)  # expiry is inclusive: at expires_at it is expired
    env.repo.revoke_agent_token(owner_id, principal.token_id)
    env.repo.delete_conversation(cid)
    _assert_expired_page(env, token)
    with _write(env) as conn:
        conn.execute("DELETE FROM answer_links WHERE token = ?", (token,))
    assert _page(env, token).status_code == 404


def _client_bound_to(env, user: UserProfile) -> TestClient:
    """A client whose every request runs with the request-user ContextVar set
    to ``user`` -- what a leaked ambient identity would look like on the
    anonymous router (set inside the request's own context, so the sync
    endpoint's worker thread inherits it)."""
    inner = env.client.app

    async def bound(scope, receive, send):
        marker = set_request_user(user)
        try:
            await inner(scope, receive, send)
        finally:
            reset_request_user(marker)

    return TestClient(bound)


def ambient_request_user_does_not_change_page_or_image(env):
    """An administrator or an unrelated user bound as the request user neither
    changes what a live link serves nor revives a dead one -- page and image,
    notebook and global link alike."""
    owner, owner_id = _user(env)
    nb = _notebook(env, owner)
    asset_id = _save_asset(env, nb, owner_id)
    notebook_job, _ = _seed_job(env, nb, owner_id,
                                payload=_with_image(_payload(), asset_id))
    global_job = _seed_global_job(env, owner_id, nb, _with_image(_payload(nb), asset_id))
    principal = _token(env, owner_id, [nb])
    live = [_link_token(_issue(env, job, principal)) for job in (notebook_job, global_job)]
    other_nb = _notebook(env, owner, "另一个库")
    narrowed = _token(env, owner_id, [nb])
    dead = [_link_token(_issue(env, job, narrowed)) for job in (notebook_job, global_job)]
    _set_access(env, narrowed, notebooks=[other_nb])

    baseline = {}
    for token in live:
        page = _page(env, token)
        assert page.status_code == 200, page.text
        alias = page.json()["turns"][0]["images"][0]["alias"]
        image = _image(env, token, alias)
        assert image.status_code == 200
        baseline[token] = (page.json(), alias, image.content)
    for token in dead:
        assert _page(env, token).status_code == 404

    admin = env.repo.current_user()
    assert admin.role == "admin"
    _, stranger_id = _user(env)
    for user in (
        UserProfile(id=admin.id, email=admin.email,
                    display_name=admin.display_name, role="admin"),
        UserProfile(id=stranger_id, email="", display_name="", role="user"),
        UserProfile(id=owner_id, email="", display_name="", role="user"),
    ):
        client = _client_bound_to(env, user)
        for token in live:
            body, alias, content = baseline[token]
            page = client.get(f"/api/public/conversations/{token}")
            assert page.status_code == 200, (user.id, page.text)
            assert page.json() == body
            image = client.get(f"/api/public/conversations/{token}/assets/{alias}")
            assert image.status_code == 200 and image.content == content
        for token in dead:
            assert client.get(f"/api/public/conversations/{token}").status_code == 404
        assert client.get("/api/public/conversations/alnk-doesnotexist").status_code == 404


CASES = {
    "ambient_request_user_does_not_change_page_or_image": ambient_request_user_does_not_change_page_or_image,
    "notebook_link_lifecycle_end_to_end": notebook_link_lifecycle_end_to_end,
    "global_link_lifecycle_end_to_end": global_link_lifecycle_end_to_end,
    "unknown_alnk_token_is_404_on_page_and_image": unknown_alnk_token_is_404_on_page_and_image,
    "global_link_rechecks_every_cited_library": global_link_rechecks_every_cited_library,
    "cshr_alias_does_not_open_under_the_link_nor_the_reverse": cshr_alias_does_not_open_under_the_link_nor_the_reverse,
    "prefixes_never_resolve_one_anothers_tokens": prefixes_never_resolve_one_anothers_tokens,
    "expired_page_is_410_even_when_other_gates_also_fail": expired_page_is_410_even_when_other_gates_also_fail,
}
