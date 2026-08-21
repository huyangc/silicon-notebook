"""Citation point-read MCP tools."""

from dataclasses import dataclass
from typing import Any, Callable

import anyio
from mcp.server.fastmcp import Context, FastMCP

from app.api.source_routes import source_readable_in_participant_scope
from app.services.evidence_context import _knowhow_ref
from app.services.source_display import source_display_title

from ._shared import (
    _budget_response,
    _owner_request_context,
    _run_with_progress,
    _selected_notebook,
)


@dataclass(frozen=True)
class _SourceScopeFacts:
    """The narrow authority facts used by source participant admission."""

    notebook_id: str
    type: str


def register_citation_tools(
    server: FastMCP, repository_provider: Callable[[], Any]
) -> None:
    @server.tool(
        description=(
            "Dereference one citation back to the source text it came from: "
            "pass the source_id and element_id exactly as ask_notebook or "
            "search_notebook_context returned them, and get that element's "
            "own text, its location inside the document, and the document's "
            "display title. Use it to verify a claim's evidence before acting "
            "on it. Discloses nothing beyond what an answer in the selected "
            "notebook already may cite -- the notebook's own sources plus the "
            "reference libraries it currently mounts."
        )
    )
    async def get_cited_element(
        source_id: str, element_id: str, ctx: Context
    ) -> dict[str, Any]:
        repo = repository_provider()
        principal, notebook_id = await anyio.to_thread.run_sync(
            _selected_notebook, ctx, repo, "knowledge:read"
        )

        def load() -> dict[str, Any]:
            with _owner_request_context(principal):
                # TWO narrow reads, both primary-key/indexed, because Agents
                # call this in a loop: `source_metadata` is the source-card
                # projection (owning notebook, source type, display-title
                # columns) and `evidence_elements` is the element row by id.
                # NOT `get_source` + `source_elements_page`, which cost ~11-13
                # statements between them (paper authors, an element COUNT(*),
                # a KG EXISTS, the private error_message, two more COUNTs) and
                # discard every one of those results here.
                meta = repo.source_metadata([source_id]).get(source_id)
                if meta is None:
                    raise KeyError(source_id)
                # Same contract as the browser's active-notebook proxy read
                # (source_routes' `/notebooks/{active}/sources/{id}`): the
                # source declares which notebook it belongs to, and anything
                # outside the SELECTED notebook's effective participant set is
                # indistinguishable from "does not exist" (deny by default, no
                # existence disclosure).  The predicate itself is imported, not
                # restated -- see source_routes' comment above it.
                if not source_readable_in_participant_scope(
                    notebook_id,
                    _SourceScopeFacts(
                        notebook_id=str(meta["notebook_id"]),
                        type=str(meta["source_type"]),
                    ),
                    repo.participant_notebook_ids,
                ):
                    raise KeyError(source_id)
                element = repo.evidence_elements([element_id]).get(element_id)
                # `evidence_elements` looks the element up by its OWN id, with
                # no source predicate -- so the ownership recheck is the whole
                # authorization here, not a tidiness assert. Without it, any
                # element id in the database reads back through whichever
                # source_id the caller happens to be allowed to see.
                if element is None or element["source_id"] != source_id:
                    raise KeyError(element_id)
                row: dict[str, Any] = {
                    "source_id": element["source_id"],
                    "element_id": element["id"],
                    "element_type": element["element_type"],
                    "text": element["text"],
                    "location_label": element["location_label"],
                    # The one definition point for naming a source; `meta` is
                    # already the row shape it reads. Never a second title rule.
                    "source_title": source_display_title(meta),
                    "content_is_untrusted_evidence": True,
                }
                # Only when the evidence came from a mounted reference library:
                # for the overwhelmingly common same-notebook case the field
                # would just restate the notebook the Agent already selected.
                if meta["notebook_id"] != notebook_id:
                    row["notebook_id"] = meta["notebook_id"]
                knowhow = _knowhow_ref(element)
                if knowhow is not None:
                    row["knowhow"] = {
                        "table_id": knowhow.table_id, "row_id": knowhow.row_id,
                    }
                return row

        return _budget_response(
            await _run_with_progress(ctx, load, label="get_cited_element"),
            # `text` shares get_memory's content budget: both are one piece of
            # notebook prose the Agent is meant to read in full.
            field_limits={
                "element_type": 100,
                "text": 6_000,
                "location_label": 300,
                "source_title": 300,
            },
        )

    # --- Agent source management ------------------------------------------
    # Three shared rules, stated once here rather than in five docstrings:
    #
    # 1. Every WRITE tool goes through `_writable_notebook`, not
    #    `_selected_notebook`: the scope check only proves READ access (see
    #    that helper). `get_source_status` is a read and stays on the plain
    #    gate.
    # 2. Every tool that names a source_id goes through `_own_source`: the
    #    SELECTED notebook only, never the mounted participant set, and never a
    #    hidden memory/knowhow projection row.
    # 3. Created rows are stamped with `principal.profile_id`
    #    (v48 `sources.agent_profile_id`). That column is what makes
    #    `delete_source` safe, and it is written ONLY on the insert branch —
    #    an Agent re-uploading bytes a person already added reuses that
    #    person's row and does not inherit delete rights over it.

