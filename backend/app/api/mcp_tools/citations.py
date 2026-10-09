"""``read_reference``: one point-read behind every ``ref`` this surface hands out."""

from dataclasses import dataclass
from typing import Any, Callable, Mapping

import anyio
from mcp.server.fastmcp import Context, FastMCP

from app.api.source_routes import source_readable_in_participant_scope
from app.domain.agent_tools import principal_has_capability
from app.services.agent_profile_block import resolve_agent_profile_names
from app.services.evidence_context import _knowhow_ref
from app.services.source_display import source_display_title

from ._shared import (
    TEXT_LIMIT,
    AgentToolError,
    _budget_response,
    _owner_request_context,
    _record_agent_call,
    _run_with_progress,
    _selected_notebook,
)
from . import global_ask as _global_ask
from .refs import decode_ref


@dataclass(frozen=True)
class _SourceScopeFacts:
    """The narrow authority facts used by source participant admission."""

    notebook_id: str
    type: str


# The first page of an element's / a Memory's text. Halved until the page
# fits the response budget whole; ``next_offset`` continues it.
_ELEMENT_PAGE_CHARS = 6_000


def _read_element(
    repo: Any, principal: Any, notebook_id: str, source_id: str,
    element_id: str,
) -> dict[str, Any]:
    """``ref`` kind ``el``: one source element, read in notebook ``notebook_id``.

    The full authorization the retired ``get_cited_element`` ran. The caller
    has already passed ``_selected_notebook(..., "knowledge:read")`` for
    ``notebook_id`` (token, tier, allowlist, notebook read access).
    """
    with _owner_request_context(principal):
        # Memory owner gate FIRST, before anything about the source is read. A
        # Memory projection belongs to its creator: another member's is
        # unreadable, and without `memory:read` so is the token owner's own
        # (viewer `''` reads no Memory at all -- the same capability
        # ask/search apply). `memory:read` is read off the principal
        # `_selected_notebook` just refreshed from the live token row for THIS
        # call (its `knowledge:read` check re-validated that same token, the
        # allowlist and notebook read access a moment ago), so it is the same
        # live answer `require_agent_access(principal, "memory:read",
        # notebook_id)` would give -- without paying its three repeated reads
        # on every call of a tool Agents run in a loop. The gate is one
        # statement that answers None for a missing id and a refused Memory
        # source alike, so both raise the identical KeyError after the
        # identical reads. Since the tier merge both capabilities map to the
        # ``read`` tier, so this always opens the owner's own Memory here; it
        # stays an explicit capability check so the mapping stays the one rule.
        viewer_id = (
            principal.owner_id
            if principal_has_capability(principal, "memory:read")
            else ""
        )
        readable_notebook_id = repo.source_notebook_id(
            source_id, viewer_id=viewer_id
        )
        if readable_notebook_id is None:
            raise KeyError(source_id)
        # Then TWO narrow reads, both primary-key/indexed, because Agents call
        # this in a loop: `source_metadata` is the source-card projection
        # (owning notebook, source type, display-title columns) and
        # `evidence_elements` is the element row by id. NOT `get_source` +
        # `source_elements_page`, which cost ~11-13 statements between them
        # (paper authors, an element COUNT(*), a KG EXISTS, the private
        # error_message, two more COUNTs) and discard every one of those
        # results here.
        meta = repo.source_metadata([source_id]).get(source_id)
        if meta is None:
            raise KeyError(source_id)
        # Same contract as the browser's active-notebook proxy read
        # (source_routes' `/notebooks/{active}/sources/{id}`): the source
        # declares which notebook it belongs to, and anything outside the
        # ref's notebook's effective participant set is indistinguishable
        # from "does not exist" (deny by default, no existence disclosure).
        # The predicate itself is imported, not restated -- see source_routes'
        # comment above it.
        if not source_readable_in_participant_scope(
            notebook_id,
            _SourceScopeFacts(
                notebook_id=str(meta["notebook_id"]),
                type=str(meta["source_type"]),
            ),
            # The participant set is read AS the token's owner (M3: a mount is
            # effective for its mounter, or for whoever can read the mounted
            # library) -- NOT the Memory gate's `viewer_id` above, which is
            # `''` when the token lacks `memory:read`.
            lambda nb: repo.participant_notebook_ids(
                nb, viewer_id=principal.owner_id
            ),
            readable_notebook_id=readable_notebook_id,
        ):
            raise KeyError(source_id)
        element = repo.evidence_elements([element_id]).get(element_id)
        # `evidence_elements` looks the element up by its OWN id, with no
        # source predicate -- so the ownership recheck is the whole
        # authorization here, not a tidiness assert. Without it, any element
        # id in the database reads back through whichever source_id the caller
        # happens to be allowed to see.
        if element is None or element["source_id"] != source_id:
            raise KeyError(element_id)
        row: dict[str, Any] = {
            "kind": "el",
            "source_id": element["source_id"],
            "element_id": element["id"],
            "element_type": element["element_type"],
            "text": element["text"],
            "location_label": element["location_label"],
            # The one definition point for naming a source; `meta` is already
            # the row shape it reads. Never a second title rule.
            "source_title": source_display_title(meta),
            "content_is_untrusted_evidence": True,
        }
        # Only when the evidence came from a mounted reference library: for
        # the overwhelmingly common same-notebook case the field would just
        # restate the notebook the ref already names.
        if meta["notebook_id"] != notebook_id:
            row["notebook_id"] = meta["notebook_id"]
        knowhow = _knowhow_ref(element)
        if knowhow is not None:
            row["knowhow"] = {
                "table_id": knowhow.table_id, "row_id": knowhow.row_id,
            }
        return row


def _read_memory(
    repo: Any, principal: Any, notebook_id: str, memory_id: str
) -> dict[str, Any]:
    """``ref`` kind ``mem``: one of the token owner's Memory items.

    The caller passed ``_selected_notebook(..., "memory:read", record=False)``:
    this read has one more gate after it -- a candidate needs
    ``memory:read_candidates`` -- so it books the call itself once that gate
    cleared (codex #616 R5 P2).
    """
    item = repo.get_memory(memory_id, principal.owner_id)
    if item.notebook_id != notebook_id or item.status in {
        "rejected",
        "deprecated",
    }:
        raise KeyError(memory_id)
    if item.status == "candidate":
        repo.require_agent_access(
            principal, "memory:read_candidates", notebook_id
        )
    # 每一道闸都过了才记。已登记的边界:查不到(或落在别的笔记本/已废弃)的那次
    # 同样不记——它在闸判完之前就抬手了,而记账记的是「到达了这个库的数据」,
    # 不是「有人试过这个 id」。
    _record_agent_call(repo, principal, notebook_id, "memory:read")
    profiles = resolve_agent_profile_names(
        repo.list_agent_profiles, principal.owner_id
    )
    return {
        "kind": "mem",
        "memory_id": item.id,
        "notebook_id": item.notebook_id,
        "title": item.title,
        "text": item.content_md,
        "tags": list(item.tags),
        "status": item.status,
        "unconfirmed": item.status == "candidate",
        "formal_notebook_conclusion": item.status == "confirmed",
        "created_by_agent": profiles.get(item.agent_profile_id or "", ""),
        "provenance": item.provenance,
        "content_is_untrusted_evidence": True,
    }


def _read_global_element(
    repo: Any, job_id: str, element_id: str
) -> dict[str, Any]:
    """``ref`` kind ``gel``: an element cited by an owned global Ask job.

    The full authorization the retired ``get_global_cited_element`` ran: the
    ``read`` tier, citation membership in the job, live read rights and the
    token allowlist; a citation that failed the answer's citation check
    cannot be opened (``FLAGGED_CITATION_MESSAGE``).

    The global error rule (``global_ask.global_ask_tool_error``): only the
    service's own ``GlobalAskError`` copy reaches the Agent; any other
    exception (a provider/database one, even an incidental ValueError) is
    ``internal`` and never echoed.
    """
    principal = _global_ask._authorize(repo, answer=False)
    try:
        with _owner_request_context(principal):
            element = _global_ask.global_ask_service().cited_element(
                job_id, element_id, user_id=principal.owner_id,
                allowed_notebook_ids=principal.notebook_ids,
            )
    except Exception as exc:  # noqa: BLE001 - translated, never echoed
        raise _global_ask.global_ask_tool_error(exc) or AgentToolError(
            "internal", _global_ask._GLOBAL_FALLBACK
        ) from None
    data = (
        element.model_dump(mode="json") if hasattr(element, "model_dump")
        else dict(element)
    )
    return {
        "kind": "gel",
        "job_id": job_id,
        "element_id": element_id,
        "source_id": data.get("source_id", ""),
        "element_type": data.get("element_type", ""),
        "location_label": data.get("location_label", ""),
        "text": data.get("text", ""),
        "content_is_untrusted_evidence": True,
    }


_REFERENCE_FIELD_LIMITS = {
    "element_type": 100,
    "location_label": 300,
    "source_title": 300,
    "title": 300,
    "tags": 200,
    "created_by_agent": 200,
}
_EXACT_KEYS = ("kind", "source_id", "element_id", "memory_id", "job_id",
               "notebook_id", "next_offset")


def _page_reference(row: Mapping[str, Any], offset: int) -> dict[str, Any]:
    """Page ``row['text']`` from ``offset`` so every page fits the budget whole."""
    full_text = str(row.get("text") or "")
    count = _ELEMENT_PAGE_CHARS if row.get("kind") != "gel" else TEXT_LIMIT
    while True:
        text = full_text[offset:offset + count]
        payload = {
            **row,
            "text": text,
            "offset": offset,
            "total_characters": len(full_text),
            "next_offset": (
                offset + len(text) if offset + len(text) < len(full_text) else None
            ),
        }
        packed = _budget_response(
            payload,
            field_limits={**_REFERENCE_FIELD_LIMITS, "text": max(1, count)},
            provenance_budget_chars=2_000 if "provenance" in payload else None,
            tags_budget_chars=1_500 if "tags" in payload else None,
        )
        if packed.get("text") == text and all(
            packed.get(key) == payload.get(key)
            for key in _EXACT_KEYS if key in payload
        ):
            return packed
        if count <= 1:
            raise AgentToolError("internal", "原文暂时无法完整返回，请稍后重试")
        count //= 2


def register_citation_tools(
    server: FastMCP, repository_provider: Callable[[], Any]
) -> None:
    @server.tool(
        description=(
            "Read the full text behind a `ref` returned by search, ask "
            "or get_ask: a source element (its own text, location inside "
            "the document, and the document's display title), one of your "
            "owner's Memory items, or an element cited by a global answer. "
            "Pass the ref exactly as returned; follow next_offset for long "
            "text. Use it to verify a claim's evidence before acting on it. "
            "Every call re-runs the full authorization of the read behind the "
            "ref: an element is readable only within what an answer in the "
            "ref's notebook may cite (its own sources plus the reference "
            "libraries it currently mounts; a memory-derived source only by "
            "the person who saved that memory); a Memory item only by its "
            "owner (candidates included); a global citation only while the "
            "job is yours, the citation belongs to it and passed the answer's "
            "citation check. Requires the read permission."
        ),
        tier="read",
    )
    async def read_reference(
        ref: str, ctx: Context, offset: int = 0
    ) -> dict[str, Any]:
        kind, ids = decode_ref(ref)
        if offset < 0:
            raise AgentToolError(
                "invalid_argument", "offset 不能小于零，请从第一页重新读取"
            )
        repo = repository_provider()
        # ``gel`` authorizes inside the worker (``_authorize``): a global job is
        # not bound to one notebook.
        principal: Any = None
        notebook_id = ""
        if kind == "el":
            principal, notebook_id = await anyio.to_thread.run_sync(
                _selected_notebook, repo, ids["n"], "knowledge:read"
            )
        elif kind == "mem":
            # ``record=False``:这个分支在收口之后**还有一道**鉴权——候选条目
            # 要求 ``memory:read_candidates``。让收口自动记账会把被那道闸拒掉的
            # 读也写进调用记录,与「被拒绝的调用不留痕」相反(codex #616 R5 P2);
            # ``_read_memory`` 在所有闸都过了之后自己补记。
            principal, notebook_id = await anyio.to_thread.run_sync(
                _selected_notebook, repo, ids["n"], "memory:read", False
            )

        def load() -> dict[str, Any]:
            if kind == "gel":
                return _read_global_element(repo, ids["j"], ids["e"])
            if kind == "mem":
                return _read_memory(repo, principal, notebook_id, ids["m"])
            return _read_element(
                repo, principal, notebook_id, ids["s"], ids["e"]
            )

        row = await _run_with_progress(ctx, load, label="read_reference")
        return _page_reference(row, offset)
