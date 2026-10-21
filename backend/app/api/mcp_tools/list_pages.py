"""Shared pieces of the MCP list tools over a token owner's own history.

``list_conversations`` and ``list_reports`` both list what the token's OWNER
made, across every allowlisted notebook the owner can still read, newest first.
They need the same small things, kept here so neither tool module carries its
own copy:

* ``history_principal`` -- the authorization of a call that names no notebook;
* ``readable_allowlist`` -- the notebooks such a call spans;
* ``instant_key`` -- the sort key of a stored timestamp;
* ``paged_items`` -- the page loop that advances past only what was delivered;
* ``CONTINUATION_QUESTION_CHARS`` -- how much of the question a continuation
  page of ``get_report`` / ``get_conversation`` repeats.

``list_notebooks`` has its own copy of the page loop and is deliberately left
alone: it is not part of this feature.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence

from app.api.deps import notebook_read_authority
from app.core.activity_time import UNRESOLVED_INSTANT, parse_activity_instant
from app.domain.agent_tools import (
    AgentAccessDenied, capability_tier, principal_has_capability,
)
from app.models.identity import AgentPrincipal

from ._shared import RESULT_LIMIT, AgentToolError, _budget_response, _live_principal

#: The one capability every history read (conversations and reports) asks for.
HISTORY_CAPABILITY = "history:read"
NEGATIVE_OFFSET = "分页位置不能小于零，请从第一页重新读取"
#: The question on a continuation page (an offset above zero) of
#: ``get_report`` / ``get_conversation``: the caller has the whole question
#: from the first page, and repeating up to 2,000 characters of it on every
#: page would take a large share of each page's budget.
CONTINUATION_QUESTION_CHARS = 200


def history_principal(repo: Any) -> AgentPrincipal:
    """The live principal of a call that names no single notebook.

    The token row is re-read (``_live_principal``: revoked, expired or disabled
    is ``token_inactive``), then the tier is checked -- before the caller learns
    anything about what exists. A call that does name a notebook goes through
    ``_selected_notebook`` instead, which checks the same capability together
    with the allowlist and the owner's read access.
    """
    principal = _live_principal(repo)
    if not principal_has_capability(principal, HISTORY_CAPABILITY):
        raise AgentAccessDenied(
            "scope_missing", tier=capability_tier(HISTORY_CAPABILITY)
        )
    return principal


def readable_allowlist(principal: AgentPrincipal) -> list[str]:
    """The allowlisted notebooks the token's owner can read right now, in
    allowlist order. One the owner can no longer read is skipped silently, like
    ``list_notebooks`` does: a listing must not turn into an error because one
    notebook lost its access.

    One statement for the whole allowlist (``readable_notebook_ids``, the
    predicate of ``user_can_read_notebook``), not one read per notebook: these
    lists span every notebook of a token on every page."""
    if not principal.notebook_ids:
        return []
    readable = notebook_read_authority().readable_notebook_ids(
        principal.notebook_ids, principal.owner_id
    )
    return [
        notebook_id for notebook_id in principal.notebook_ids
        if notebook_id in readable
    ]


def instant_key(value: Any) -> datetime:
    """A stored timestamp as an absolute instant, for ordering.

    Rows come from different tables and writers, so the spellings differ (a
    ``Z`` suffix, an offset, a bare local-looking string); comparing the text
    would interleave them wrongly. One that cannot be read at all sorts as the
    oldest instant instead of failing the whole listing.
    """
    try:
        return parse_activity_instant(str(value or ""), field="timestamp")
    except ValueError:
        return UNRESOLVED_INSTANT


def paged_items(
    rows: Sequence[Mapping[str, Any]], *, limit: int, offset: int, id_key: str,
    field_limits: Mapping[str, int], failure: str,
) -> dict[str, Any]:
    """One page of ``rows`` with an exact ``next_offset``.

    ``limit`` is clamped to ``1..RESULT_LIMIT``. The output budget may cut rows
    off the page, so the cursor advances only past the identities that were
    actually delivered: following ``next_offset`` to ``None`` can never skip a
    row. A single row that cannot be packed at all is ``internal`` (``failure``
    is its Chinese copy) rather than an empty page that looks like the end.
    """
    cap = max(1, min(int(limit), RESULT_LIMIT))
    page = list(rows[offset:offset + cap])
    while True:
        next_offset = offset + len(page)
        response = _budget_response(
            {
                "items": page, "total": len(rows),
                "next_offset": next_offset if next_offset < len(rows) else None,
            },
            field_limits=field_limits,
        )
        if [row.get(id_key) for row in response["items"]] == [
            row[id_key] for row in page
        ]:
            return response
        if len(page) <= 1:
            raise AgentToolError("internal", failure)
        page = page[:-1]
