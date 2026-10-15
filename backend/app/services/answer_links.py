"""MCP answer links: issue an expiring anonymous link to one answered ``ask``
job, and resolve one when an anonymous reader opens it.

Both the MCP tools (issuing) and the public conversation routes (resolving)
call only this module, so the rules live in one place:

* **Issuing** reuses the issuing token's live link to the same job when one is
  still valid (its original ``expires_at`` is reported, never rewritten) and
  mints a new one otherwise; the URL is the public web base plus ``/c/<token>``.
  No base configured means the deployment cannot hand out links at all, raised
  as ``AnswerLinkUnavailable`` before anything is written.

* **Resolving** runs on the ANONYMOUS router. ⚠ No request user exists there and
  nothing below may read the request-user ContextVar (it falls back to the
  seeded admin when unset): every identity -- the link's owner, the issuing
  token -- comes from the link row and is passed explicitly to store methods
  that take ids. Order (plan D11): an unknown token is ``None`` (404); an
  expired row still present raises ``AnswerLinkExpired`` (410) whatever the
  other gates would say; any failing re-check is ``None`` (404) without saying
  which. The re-checks are the ones ``get_ask`` applies to the issuing token
  NOW (token alive, ``ask`` tier, allowlist, owner's read access, Memory replay
  rule) plus the snapshot predicate the session share runs on every open --
  shared code, not a copy (plan D6).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.domain.agent_tools import principal_has_capability
from app.services.public_share_recheck import notebook_snapshot_still_readable

NOTEBOOK_JOB_PREFIX = "askjob-"
GLOBAL_JOB_PREFIX = "gask-"


@dataclass(frozen=True)
class IssuedAnswerLink:
    url: str
    expires_at: str
    reused: bool


class AnswerLinkUnavailable(Exception):
    """``PUBLIC_WEB_BASE_URL`` is not configured: no link can be built."""


class AnswerLinkExpired(Exception):
    """The link row still exists but is past its ``expires_at`` (410)."""


def answer_link_job_kind(job_id: str) -> str:
    """``notebook`` / ``global`` by the job id's prefix; anything else is a
    programming error (only ask jobs can be linked)."""
    clean = str(job_id or "")
    if clean.startswith(NOTEBOOK_JOB_PREFIX):
        return "notebook"
    if clean.startswith(GLOBAL_JOB_PREFIX):
        return "global"
    raise ValueError(f"not an ask job id: {job_id!r}")


def issue_answer_link(
    store: Any,
    settings: Any,
    *,
    job_id: str,
    principal: Any,
    ttl_hours: int | None,
) -> IssuedAnswerLink:
    """The issuing token's link to ``job_id``: its live one if any, else new.

    ``ttl_hours`` (``None`` = the deployment default) only shapes a NEW link
    and must lie in ``1..MCP_ANSWER_LINK_TTL_HOURS`` -- out of range is a
    ``ValueError``, never clamped (the MCP layer reports ``invalid_argument``
    before calling); so is anything but a plain ``int`` (a ``bool``, or a
    ``1.5`` that ``int()`` would quietly truncate). Authorization to issue (the token's ``allow_answer_links``
    and its right to read the job) is the caller's gate."""
    base = str(getattr(settings, "public_web_base_url", "") or "").rstrip("/")
    if not base:
        raise AnswerLinkUnavailable()
    job_kind = answer_link_job_kind(job_id)
    ceiling = int(settings.mcp_answer_link_ttl_hours)
    if ttl_hours is not None and (
        isinstance(ttl_hours, bool) or not isinstance(ttl_hours, int)
    ):
        raise ValueError("link_ttl_hours must be a whole number of hours")
    hours = ceiling if ttl_hours is None else ttl_hours
    if hours < 1 or hours > ceiling:
        raise ValueError(f"link_ttl_hours must be within 1..{ceiling}")
    issued = store.issue_answer_link(
        job_kind=job_kind,
        job_id=job_id,
        owner_id=principal.owner_id,
        agent_token_id=principal.token_id,
        ttl_seconds=hours * 3600,
        sweep_grace_seconds=int(settings.mcp_answer_link_grace_hours) * 3600,
    )
    return IssuedAnswerLink(
        url=f"{base}/c/{issued['token']}",
        expires_at=str(issued["expires_at"]),
        reused=bool(issued["reused"]),
    )


def resolve_answer_link(
    token: str, *, store: Any, repo: Any, global_service: Any
) -> dict | None:
    """The public snapshot row an answer link serves right now, or ``None``.

    The row has the shape ``conversation_public_view.public_conversation_payload``
    projects (one turn), plus ``expires_at`` and the GATE fields the caller
    pops before projection: ``notebook_id`` / ``created_by`` for a notebook
    job, ``notebook_ids`` / ``created_by`` for a global one. Raises
    ``AnswerLinkExpired`` for an expired row that has not been swept yet.
    Every gate has already run here, including the session share's snapshot
    predicate for a notebook job; the caller adds nothing but projection."""
    link = store.answer_link(token)
    if link is None:
        return None
    if link.get("expired"):
        raise AnswerLinkExpired()
    owner = str(link.get("owner_id") or "")
    if not owner:
        return None
    principal = _live_issuer(repo, str(link.get("agent_token_id") or ""), owner)
    if principal is None:
        return None
    job_id = str(link.get("job_id") or "")
    if link.get("job_kind") == "notebook":
        row = _notebook_row(repo, principal, job_id, owner)
    elif link.get("job_kind") == "global":
        row = global_service.public_answer_job(
            job_id,
            owner_id=owner,
            # The token's allowlist is always a concrete list (issuing fills in
            # the default notebook when none is given), so it is passed as the
            # ceiling as is -- never ``None``, which would mean "no ceiling".
            # An allowlist emptied by deletions ceils at nothing (fail closed),
            # as ``require_agent_access`` treats it.
            allowed_notebook_ids=set(principal.notebook_ids or ()),
        )
    else:
        return None
    if row is None:
        return None
    row["expires_at"] = str(link.get("expires_at") or "")
    return row


def _live_issuer(repo: Any, token_id: str, owner: str) -> Any:
    """The issuing token as it stands now, if it may still back a link: alive
    (not revoked/expired, profile active, owner eligible), same owner, the
    link switch on, and the ``ask`` tier held."""
    if not token_id:
        return None
    try:
        principal = repo.refresh_agent_principal(token_id)
    except PermissionError:
        return None
    if (
        principal is None
        or principal.owner_id != owner
        or not principal.allow_answer_links
        or not principal_has_capability(principal, "ask:execute")
    ):
        return None
    return principal


def _notebook_row(repo: Any, principal: Any, job_id: str, owner: str) -> dict | None:
    """``get_ask``'s delivery rule for a notebook job, then the session share's
    snapshot predicate, all as the link's owner."""
    origin = repo.ask_job_origin(job_id)
    if (
        origin is None
        or origin.get("created_by") != owner
        or origin.get("submitted_via") != "mcp"
    ):
        return None
    notebook_id = str(origin.get("notebook_id") or "")
    # ``require_agent_access``'s checks, run on the principal ``_live_issuer``
    # refreshed a moment ago rather than re-reading the token row for each
    # capability: that refresh already found the token alive (not revoked or
    # expired, profile active, owner eligible) under the link's owner, so
    # what is left is the tier and the allowlist -- judged against ONE
    # snapshot of the token instead of three. The owner's read access to the
    # notebook is the first check of the snapshot predicate below (same
    # ``user_can_read_notebook(notebook_id, owner)``), so it is not read twice.
    if (
        not principal_has_capability(principal, "ask:execute")
        or notebook_id not in set(principal.notebook_ids or ())
    ):
        return None
    # A run that had the private-Memory channel open (a row without the fact
    # counts as open) is replayed only to a token that may read Memory now --
    # the token holder's standing consent replaces the share's per-publish
    # disclosure handshake. Same notebook, so only the tier is left to check.
    if origin.get("memory_access", True) and not principal_has_capability(
        principal, "memory:read"
    ):
        return None
    # Where the job stands comes with its origin: no trace is loaded on an
    # anonymous open.
    if origin.get("status") != "done" or origin.get("output") == "evidence":
        return None
    answer = repo.ask_answer_detail(str(origin.get("answer_id") or ""))
    if answer is None:
        return None
    row = {
        "title": "",
        "created_at": answer.get("created_at") or "",
        "shared_through_at": "",
        "turns": [answer],
        "notebook_id": notebook_id,
        "created_by": owner,
    }
    if not notebook_snapshot_still_readable(
        row, can_read=repo.user_can_read_notebook,
        participant_notebook_ids=repo.participant_notebook_ids,
    ):
        return None
    return row
