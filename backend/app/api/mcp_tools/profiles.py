"""Private observation MCP tool (and the profile projection get_notebook reads)."""

from typing import Any, Callable

import anyio
from mcp.server.fastmcp import Context, FastMCP

from app.core.config import get_settings
from app.core.memory_inputs import (
    normalize_client_request_id,
    normalize_observation_text,
)
from app.services.agent_profile_block import PROFILE_LABEL_ORDER
from app.services.reasoning_retrieval import profile_wiring_active

from ._shared import (
    AgentToolError,
    _budget_response,
    _owner_request_context,
    _run_with_progress,
    _selected_notebook,
)


def _profile_projection(rows: list[dict], owner_id: str) -> list[dict[str, Any]]:
    """Project the only fields exposed by the profile-read tool."""
    by_label = {
        str(row.get("label") or ""): row
        for row in rows
        if str(row.get("owner_id") or "") == owner_id
    }
    return [
        {
            "label": label,
            "value": str(by_label[label].get("value") or ""),
            "updated_at": str(by_label[label].get("updated_at") or ""),
        }
        for label in PROFILE_LABEL_ORDER
        if label in by_label
    ]


def register_profile_tools(
    server: FastMCP, repository_provider: Callable[[], Any]
) -> None:
    @server.tool(
        description=(
            "Append one short line to this notebook's per-Agent observation "
            "log: a usage note about how you just used it (what you searched "
            "for, what worked or did not), NOT a Memory candidate and NOT "
            "notebook content. It is written into a private, untrusted-"
            "marked queue that only a LATER background pass may fold into "
            "your own private overlay understanding -- it is never read as "
            "evidence and never answers a question by itself. Idempotent on "
            "client_request_id WHILE the observation is retained: the queue "
            "is a bounded ring per member, so once enough newer observations "
            "have evicted a row, retrying its old id writes a fresh row "
            "(codex #535 R4: a bounded idempotency window, registered -- a "
            "separate everlasting key table is not worth its own migration "
            "for a retry contract measured in seconds). Requires the "
            "contribute permission; unlike source-management writes, "
            "this one does NOT require notebook ownership -- get_notebook's "
            "profile is the read side of the same feature. notebook_id "
            "omitted = the token's default notebook."
        ),
        tier="contribute",
    )
    async def add_observation(
        text: str, client_request_id: str, ctx: Context, notebook_id: str = "",
    ) -> dict[str, Any]:
        clean_text = normalize_observation_text(text)
        clean_request_id = normalize_client_request_id(client_request_id)
        repo = repository_provider()
        # Deliberately `_selected_notebook`, NOT `_writable_notebook`: see
        # that helper's own docstring (point 2 of its "two writes" section)
        # for the full four-part argument. This is scope-driven access, the
        # same authority model as `put_knowhow_cell_code`.
        principal, notebook_id = await anyio.to_thread.run_sync(
            _selected_notebook, repo, notebook_id, "agent_observation:write"
        )

        def run() -> dict[str, Any]:
            # Same kill switch as get_notebook's profile read -- but a DIFFERENT contract on purpose: the read side
            # reports `enabled: False` because a caller cannot act on a
            # closed sign; the write side must not go on quietly
            # accumulating rows a now-disabled consolidation pass will never
            # read, so it fails loudly instead.
            if not profile_wiring_active(get_settings(), repo.agent_profile):
                raise AgentToolError("unavailable", "这项能力当前未开启")
            with _owner_request_context(principal):
                # `agent_profile_id` comes from the LIVE principal the bearer
                # middleware just re-verified above, never from the request
                # -- there is no argument on this tool that could name a
                # different Agent's profile.
                observation_id, deduplicated = (
                    repo.agent_observations.append_observation(
                        notebook_id, principal.owner_id, principal.profile_id,
                        text=clean_text, client_request_id=clean_request_id,
                    )
                )
            # codex #535 R11 P2: the access check above and the append are two
            # steps, so a member removed IN BETWEEN can have their rows cleared
            # by the removal path first and this append land after — an orphan
            # row that resurrects on rejoin, violating the blank-slate
            # contract. Recheck access AFTER the append (same posture as the
            # P2 overlay chains' pre-bump membership recheck): either the
            # removal's clear ran after our append (it took our row with it),
            # or it ran before — then this recheck sees the revocation and the
            # compensating clear removes what we just wrote. A recheck ERROR
            # keeps the row (fail-open: the append was legitimate under the
            # access state this tool verified moments ago).
            # codex #535 R13 P2: the recheck is NOTEBOOK read access only, not
            # the full ``require_agent_access`` — token-level failures
            # (revocation, expiry, scope loss, allowlist edits) between the
            # two steps do not make the row illegitimate (the owner is still
            # a member and it is their own private queue), and compensating
            # on them would wipe EVERY observation this user retains in the
            # notebook, including other Agents' rows, on what may be an
            # idempotent no-op retry. Member removal is the one event whose
            # cleanup this append can race, and clearing the member's whole
            # ``(notebook, owner)`` scope is exactly what that removal path
            # itself does — so the compensation matches its semantics.
            try:
                still_member = repo.user_can_read_notebook(
                    notebook_id, principal.owner_id
                )
            except Exception:  # noqa: BLE001 — fail-open, see above
                still_member = True
            if not still_member:
                repo.agent_observations.clear_observations(
                    notebook_id, principal.owner_id
                )
                raise AgentToolError(
                    "notebook_unreadable",
                    "写入期间凭证主人失去了这个笔记本的访问权限，这条观察已丢弃",
                )
            # Returns immediately -- the write itself is one bounded INSERT
            # plus one bounded eviction DELETE, zero model calls, so there is
            # nothing to queue. "Asynchronous" here means only that THIS
            # write never blocks on the later, separate consolidation pass
            # that may or may not fold it into an overlay block.
            return {
                "observation_id": observation_id,
                "notebook_id": notebook_id,
                "accepted": True,
                "deduplicated": deduplicated,
            }

        return _budget_response(
            await _run_with_progress(ctx, run, label="add_observation")
        )
