"""Source creation, status, reparse, and deletion MCP tools."""

import hashlib
from typing import Any, Callable

import anyio
from mcp.server.fastmcp import Context, FastMCP

from app.api.source_routes import (
    _HIDDEN_SOURCE_TYPES,
    _document_capacity,
    document_capacity_message,
)
from app.core.config import get_settings
from app.core.memory_inputs import normalize_text
from app.models.sources import SourceDetail
from app.repositories.ports import UploadedSourceFile
from app.repositories.source_files import safe_filename
from app.services.kg import scheduler as kg_scheduler
from app.services.mineru_cloud_client import MinerUCloudNotConfigured

from ._shared import (
    _budget_response,
    _owner_request_context,
    _run_with_progress,
    _selected_notebook,
    _writable_notebook,
)


SOURCE_TITLE_MAX_CHARS = 200
SOURCE_FILE_NAME_MAX_BYTES = 200
SOURCE_BUSY_PROBE_SECONDS = 0.5


def _own_source(repo: Any, notebook_id: str, source_id: str) -> SourceDetail:
    """Resolve a visible source owned by the selected notebook itself."""
    detail = repo.get_source(source_id)
    if detail.notebook_id != notebook_id or detail.type in _HIDDEN_SOURCE_TYPES:
        raise KeyError(source_id)
    return detail


def _reject_when_notebook_is_full(
    repo: Any, notebook_id: str, adding: int
) -> None:
    """Apply the browser route's canonical notebook document ceiling."""
    capacity = _document_capacity(notebook_id, repo)
    if capacity is None:
        return
    current, limit = capacity
    if current + adding > limit:
        raise ValueError(document_capacity_message(current, limit, adding))


def _markdown_source_file_name(title: str) -> str:
    """Derive a filesystem-safe bounded Markdown file name."""
    stem = safe_filename(title)
    encoded = stem.encode("utf-8")
    if len(encoded) > SOURCE_FILE_NAME_MAX_BYTES:
        stem = encoded[:SOURCE_FILE_NAME_MAX_BYTES].decode("utf-8", "ignore")
    return f"{stem.strip() or 'source'}.md"


def register_source_tools(
    server: FastMCP, repository_provider: Callable[[], Any]
) -> None:
    @server.tool(
        description=(
            "Add a Markdown document to the selected notebook from text you "
            "provide, and start parsing it. Use this to file a finished piece "
            "of work (a design note, an extracted spec, a summary) as a "
            "first-class notebook source that later answers can cite -- not "
            "for scratch findings, which belong in propose_memory. The text is "
            "stored verbatim as a .md source named after `title`; parsing runs "
            "in the background, so poll get_source_status for the outcome. "
            "Re-adding byte-identical content returns the existing source "
            "(`reused: true`) instead of creating a duplicate. Requires the "
            "sources:write scope and ownership of the notebook."
        )
    )
    async def add_source_text(
        title: str, content_md: str, ctx: Context
    ) -> dict[str, Any]:
        # Validate the caller-controlled envelope BEFORE any repository work,
        # exactly as propose_memory does: a blank title or an oversized body is
        # the caller's bug and must not cost a lookup, a lock or a file write.
        clean_title = normalize_text(
            title, field="title", max_chars=SOURCE_TITLE_MAX_CHARS
        )
        if not isinstance(content_md, str):
            raise ValueError("content_md must be a string")
        if not content_md.strip():
            raise ValueError("content_md must not be blank")
        # The SAME ceiling the browser upload enforces per file
        # (`settings.source_upload_max_bytes`), measured on the bytes that will
        # actually be stored -- a character count would under-count CJK by 3x.
        payload = content_md.encode("utf-8")
        max_bytes = get_settings().source_upload_max_bytes
        if len(payload) > max_bytes:
            raise ValueError(
                f"content_md is {len(payload)} bytes, over this deployment's "
                f"{max_bytes}-byte limit for one source"
            )
        repo = repository_provider()
        principal, notebook_id = await anyio.to_thread.run_sync(
            _writable_notebook, ctx, repo, "sources:write"
        )

        # The dedup key `upload_sources` will compute from these same bytes.
        digest = hashlib.sha256(payload).hexdigest()

        def run() -> dict[str, Any]:
            with _owner_request_context(principal):
                # Order matters: a call that is about to REUSE an existing row
                # adds no document, so the per-notebook ceiling must not refuse
                # it. Checking capacity first made the documented idempotence
                # ("re-adding identical content returns the existing source")
                # fail exactly when a notebook was full -- the one state where
                # an Agent most needs a repeat call to be a safe no-op.
                #
                # ⚠ Window between this probe and the upload: if the matched row
                # is deleted in between, upload_sources creates a row and the
                # notebook ends up one document over. Bounded to one, and the
                # same shape as the browser's own check-then-insert TOCTOU
                # (documented on `_enforce_document_capacity`) -- the next call
                # refuses. Closing it would mean holding a write lock across the
                # whole ingest.
                if repo.source_id_by_hash(notebook_id, digest) is None:
                    _reject_when_notebook_is_full(repo, notebook_id, 1)
                # The synthetic-markdown upload seam (app/eval/speed.py uses the
                # same one): hand `upload_sources` an UploadedSourceFile and the
                # whole existing path -- content dedup, storage, background
                # parse, KG extraction -- runs unchanged. No second ingest path.
                created = repo.upload_sources(
                    notebook_id,
                    [UploadedSourceFile(
                        # `title` and `file_name` are DIFFERENT things here, and
                        # conflating them is what this pair fixes: the file name
                        # is sanitized, byte-truncated and suffixed, so writing
                        # it into `sources.title` would show the Agent (and the
                        # user, in the sources tab) a derived path string in
                        # place of the title that was submitted.
                        file_name=_markdown_source_file_name(clean_title),
                        content_type="text/markdown",
                        content=payload,
                        title=clean_title,
                    )],
                    lambda source_id: kg_scheduler.submit_job(
                        repo.process_source, source_id
                    ),
                    principal.profile_id,
                )
                source = created[0]
                return {
                    "source_id": source.id,
                    "title": source.title,
                    # True = byte-identical content already existed in this
                    # notebook and was reused. Report it rather than silently
                    # answering "created": the Agent needs to know it did not
                    # add a document, and `agent_created` below may then be
                    # False because the row is somebody else's.
                    "reused": bool(source.reused),
                    "parse_status": source.parse_status,
                    "status": source.status,
                    "agent_created": source.agent_created,
                }

        return _budget_response(
            await _run_with_progress(ctx, run, label="add_source_text"),
            field_limits={
                "title": 300, "parse_status": 40, "status": 40,
            },
        )

    @server.tool(
        description=(
            "Add a PDF to the selected notebook by URL and start parsing it. "
            "Only PDFs are accepted -- the server probes the URL first and "
            "refuses anything that is not one, or that it cannot reach. "
            "Parsing runs in the background; poll get_source_status. Requires "
            "the sources:write scope and ownership of the notebook."
        )
    )
    async def add_source_url(url: str, ctx: Context) -> dict[str, Any]:
        if not isinstance(url, str) or not url.strip():
            raise ValueError("url must not be blank")
        clean_url = url.strip()
        repo = repository_provider()
        principal, notebook_id = await anyio.to_thread.run_sync(
            _writable_notebook, ctx, repo, "sources:write"
        )

        def run() -> dict[str, Any]:
            with _owner_request_context(principal):
                # Refuse a full notebook up front, with the SAME sentence
                # add_source_text uses -- one condition must not have two
                # wordings. The browser's URL route cannot do this because a URL
                # import is naturally partial (unreachable / non-PDF entries are
                # skipped), so it charges the ceiling per successful probe and
                # reports over-limit entries in `rejected`; with exactly one URL
                # the two are equivalent.
                _reject_when_notebook_is_full(repo, notebook_id, 1)
                # Still passed as a BUDGET as well, keeping the service-level
                # accounting identical to the browser route. It stays the
                # authority if the count moves between the check and the call --
                # in that race the reason comes from add_url_sources itself,
                # which is that service's own user-facing wording (the browser
                # shows it too), not a second spelling of the sentence above.
                capacity = _document_capacity(notebook_id, repo)
                budget = None if capacity is None else max(0, capacity[1] - capacity[0])
                try:
                    result = repo.add_url_sources(
                        notebook_id,
                        [clean_url],
                        lambda source_id: kg_scheduler.submit_job(
                            repo.process_source, source_id
                        ),
                        budget,
                        principal.profile_id,
                    )
                except MinerUCloudNotConfigured:
                    # The exception's own text names the deployment's env vars.
                    # An Agent can act on "ask the operator", not on
                    # MINERU_API_TOKEN, and server configuration is not its
                    # business.
                    raise ValueError(
                        "this deployment has no PDF parsing service configured, "
                        "so PDFs cannot be added by URL; upload the text with "
                        "add_source_text instead, or ask the operator to "
                        "configure PDF parsing"
                    ) from None
                if not result.created:
                    # `reason` is the same user-facing sentence the browser shows
                    # for a rejected URL ("not a PDF", "unreachable", "over the
                    # document limit") and is the only actionable part of this
                    # failure. Bounded on the way out by the response budget's
                    # field limit below -- it is not re-raised anywhere it could
                    # carry a traceback.
                    reason = (
                        result.rejected[0].reason if result.rejected
                        else "the URL was skipped"
                    )
                    raise ValueError(f"the URL was not added: {reason}")
                source = result.created[0]
                return {
                    "source_id": source.id,
                    "title": source.title,
                    "parse_status": source.parse_status,
                    "status": source.status,
                    "agent_created": source.agent_created,
                }

        return _budget_response(
            await _run_with_progress(ctx, run, label="add_source_url"),
            field_limits={"title": 300, "parse_status": 40, "status": 40},
        )

    @server.tool(
        description=(
            "Check one source's parsing/extraction state in the selected "
            "notebook -- the way to find out whether a source you just added "
            "is ready to be asked about. Reports whether parsing finished, "
            "failed, or produced a degraded result, how many elements it "
            "yielded, and whether its knowledge has been extracted."
        )
    )
    async def get_source_status(source_id: str, ctx: Context) -> dict[str, Any]:
        repo = repository_provider()
        principal, notebook_id = await anyio.to_thread.run_sync(
            _selected_notebook, ctx, repo, "knowledge:read"
        )

        def load() -> dict[str, Any]:
            with _owner_request_context(principal):
                detail = _own_source(repo, notebook_id, source_id)
                return {
                    "source_id": detail.id,
                    "parse_status": detail.parse_status,
                    "status": detail.status,
                    # A DERIVED boolean, never `detail.error_message`: that
                    # field is `str(exc)` stored verbatim and routinely carries
                    # server-side absolute paths. Same narrowing
                    # `ScopedSourceDetail` applies to the browser's cross-
                    # notebook proxy read, for the same reason.
                    "parse_failed": detail.parse_status == "failed",
                    # MinerU fell back to a lossy local parser: layout, formulas
                    # and tables may be wrong even though the source is
                    # "extracted". An Agent about to cite it should know.
                    "parse_quality_warning": detail.parse_quality_warning,
                    "element_count": detail.element_count,
                    "kg_extracted": detail.kg_extracted,
                    # 分析跑完了、这篇里确实没有可整理的知识(正文极少 / 几乎全是
                    # 没有图注的图片)。与 kg_extracted 并列而不是复用它:Agent 需要
                    # 分清「还没分析」和「分析过、图谱里就是没有它」——前者值得重跑,
                    # 后者重跑多少次都一样,该做的是换解析方式(如开 OCR)。
                    "kg_analyzed_empty": detail.kg_analyzed_empty,
                    "agent_created": detail.agent_created,
                }

        return _budget_response(
            await _run_with_progress(ctx, load, label="get_source_status"),
            field_limits={"parse_status": 40, "status": 40},
        )

    @server.tool(
        description=(
            "Re-run parsing and knowledge extraction for one source in the "
            "selected notebook, discarding its previous elements. Use it after "
            "a failed or degraded parse. The work is queued in the background "
            "and this returns immediately -- poll get_source_status to see the "
            "result. Refuses while that source is already being parsed. "
            "Requires the sources:write scope and ownership of the notebook."
        )
    )
    async def reparse_source(source_id: str, ctx: Context) -> dict[str, Any]:
        repo = repository_provider()
        principal, notebook_id = await anyio.to_thread.run_sync(
            _writable_notebook, ctx, repo, "sources:write"
        )

        def run() -> dict[str, Any]:
            with _owner_request_context(principal):
                _own_source(repo, notebook_id, source_id)
                # There is no single-flight guard behind process_source: the
                # browser's re-parse route submits unconditionally, so calling
                # this in a loop would queue N full parse+embed+extract
                # pipelines for one document. Probe the ingestion service's own
                # per-source chunk lock instead of inventing a status signal.
                #
                # ⚠ Residual race, accepted on purpose: the lock is released
                # before the background job is submitted, so a parse starting in
                # that window is not seen here. It cannot corrupt anything --
                # process_source serializes on this same lock, so the second run
                # waits and then redoes the work. Closing it would mean holding
                # the lock across a scheduler submit, i.e. blocking this MCP
                # request thread for the minutes a parse can take.
                if repo.source_parse_busy(
                    source_id, timeout=SOURCE_BUSY_PROBE_SECONDS
                ):
                    raise ValueError(
                        "this source is being parsed right now; poll "
                        "get_source_status and retry once it settles"
                    )
                kg_scheduler.submit_job(repo.process_source, source_id)
                return {"source_id": source_id, "queued": True}

        return _budget_response(
            await _run_with_progress(ctx, run, label="reparse_source")
        )

    @server.tool(
        description=(
            "Delete one source that an Agent added to the selected notebook, "
            "together with everything derived from it. Sources a PERSON added "
            "are refused: use this only to clean up your own uploads. "
            "Irreversible. Requires the sources:delete scope (which "
            "sources:write does not imply) and ownership of the notebook."
        )
    )
    async def delete_source(source_id: str, ctx: Context) -> dict[str, Any]:
        repo = repository_provider()
        principal, notebook_id = await anyio.to_thread.run_sync(
            _writable_notebook, ctx, repo, "sources:delete"
        )

        def run() -> dict[str, Any]:
            with _owner_request_context(principal):
                detail = _own_source(repo, notebook_id, source_id)
                # THE safety gate of this whole surface. `agent_created` is the
                # projection of v48 `sources.agent_profile_id IS NOT NULL`,
                # taken from the very row that just proved notebook membership
                # and non-synthetic type -- one read, no window between the two
                # judgements, and no second spelling of the predicate.
                #
                # The criterion is "SOME Agent added this row", not "this
                # profile did": Agent identities are the owner's own
                # bookkeeping, they get rotated and revoked, and a source left
                # behind by a retired profile would otherwise be undeletable
                # through this surface forever. What the criterion does protect,
                # absolutely, is the user's own documents.
                #
                # It is safe against laundering because provenance is written on
                # the INSERT branch only: re-uploading a person's bytes reuses
                # their row and leaves the column NULL
                # (test_agent_reupload_of_a_user_source_stays_user_added), and a
                # notebook deep copy clears the column outright.
                #
                # Fails closed by construction: the projection defaults to False
                # whenever the column is absent from the row.
                if not detail.agent_created:
                    raise PermissionError(
                        "this source was added by a user; an Agent token may "
                        "only delete sources that an Agent added"
                    )
                # Synchronous. Safe against a CONCURRENT duplicate call:
                # delete_source re-checks the row inside its write transaction
                # (`source_exists_for_update_tx`), so the loser's destructive
                # work becomes a no-op instead of an error. A call REPEATED
                # after the first one finished fails above, in `_own_source`,
                # as an ordinary not-found.
                repo.delete_source(source_id)
                return {"source_id": source_id, "deleted": True}

        return _budget_response(
            await _run_with_progress(ctx, run, label="delete_source")
        )

    # --- Agent build/maintenance tools --------------------------------------
    # The consumer side of the "maintenance:execute" scope: an owner could
    # already issue a token carrying it (AGENT_SCOPES / the token-creation
    # UI), but until now nothing in this file checked for it. These three
    # tools are the Agent-side door to two builds the browser's own
    # maintenance panel already exposes -- knowledge-graph extraction and the
    # retrieval-index rebuild -- plus the one combined status read behind
    # both. No new capability, no new SQL: each tool below mirrors its HTTP
    # twin's precondition order and repository calls verbatim (kg_routes.py's
    # `build_kg` / `rebuild_scale_index` / `index_status`), including
    # `build_kg`'s exact `background_jobs.submit(...)` argument shape, so the
    # two surfaces cannot silently drift.
    #
    # `build_kg`/`build_retrieval_index` go through `_writable_notebook`
    # (owner-only), the same rule the source-management tools above use: a
    # token's allowlist can name a notebook its owner merely joined as a
    # read-only member, and starting a background build there would hand
    # that share's read side a write it was never granted. `get_build_status`
    # is a pure read and stays on `_selected_notebook`, mirroring
    # `index_status`'s HTTP twin using `require_notebook_read` (member-
    # readable) rather than `require_notebook_access` (owner-only).
