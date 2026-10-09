"""Source inventory/status, creation, reparse, and deletion MCP tools."""

import base64
import binascii
import hashlib
import mimetypes
from pathlib import Path
from typing import Any, Callable

import anyio
from mcp.server.fastmcp import Context, FastMCP

from app.api.source_routes import (
    _HIDDEN_SOURCE_TYPES,
    _document_capacity,
    _document_capacity_limit,
    document_capacity_message,
)
from app.core.config import get_settings
from app.core.memory_inputs import normalize_text
from app.domain.promotion_source import PROMOTION_SOURCE_TYPE
from app.models.sources import SourceDetail
from app.repositories.ports import DocumentCapacityExceeded, UploadedSourceFile
from app.repositories.source_files import FILESYSTEM_NAME_MAX_BYTES, safe_filename
from app.services.kg import scheduler as kg_scheduler
from app.services.mineru_cloud_client import MinerUCloudNotConfigured
from app.services.source_format_admission import (
    supported_source_extensions,
    supported_source_suffixes,
)

from ._shared import (
    OUTPUT_INTEGER_LIMIT,
    RESULT_LIMIT,
    AgentToolError,
    _record_agent_call,
    _budget_response,
    _owner_request_context,
    _run_with_progress,
    _selected_notebook,
    _writable_notebook,
)


# This names a document rather than a Memory item. The full title is stored;
# only the derived file name below is shortened.
SOURCE_TITLE_MAX_CHARS = 200
# Keep ``{source_id}_{name}.md`` within a 255-byte path component on Linux. The
# budget is UTF-8 bytes, not characters, and applies only to the derived name.
SOURCE_FILE_NAME_MAX_BYTES = 200
# This is a bounded probe of the per-source parse lock, not a parse timeout.
SOURCE_BUSY_PROBE_SECONDS = 0.5
SOURCE_FILE_EXTENSIONS = supported_source_extensions()
SOURCE_FILE_SUFFIXES = supported_source_suffixes()


def _upload_agent_source(
    repo: Any,
    *,
    notebook_id: str,
    principal: Any,
    file_name: str,
    content_type: str,
    payload: bytes,
    title: str,
) -> Any:
    """Use the one canonical source upload/dedup/background scheduling path.

    Both pre-probe branches thread the notebook's document ceiling into
    ``upload_sources`` as ``capacity_limit``, where the store re-counts INSIDE
    the creation write transaction: the once-documented "matched row deleted
    between this probe and the upload" overshoot, and the browser's own
    check-then-insert race, are both closed by that gate (PR #584 codex R6) --
    the dedup re-check still runs first there, so a reuse is never refused at
    the limit. The pre-flight refusal here stays as the cheap early error; a
    racer that slips past it gets the SAME sentence from the except arm."""
    digest = hashlib.sha256(payload).hexdigest()
    if repo.source_id_by_hash(notebook_id, digest) is None:
        capacity_limit = _reject_when_notebook_is_full(repo, notebook_id, 1)
    else:
        # Dedup hit: a reuse adds no document, so no refusal here — but the
        # ceiling is still threaded through so the "matched row deleted before
        # the upload" race cannot overshoot. Limit-only fetch on purpose (no
        # COUNT): this is the Agent's idempotent-retry hot path, and the
        # current count would be computed only to be thrown away.
        capacity_limit = _document_capacity_limit(notebook_id, repo)
    try:
        created = repo.upload_sources(
            notebook_id,
            [UploadedSourceFile(
                file_name=file_name,
                content_type=content_type,
                content=payload,
                title=title,
            )],
            lambda source_id: kg_scheduler.submit_job(repo.process_source, source_id),
            principal.profile_id,
            capacity_limit=capacity_limit,
        )
    except DocumentCapacityExceeded as exc:
        raise AgentToolError(
            "unavailable", document_capacity_message(exc.current, exc.limit, 1)
        ) from None
    return created[0]


def _source_result(source: Any) -> dict[str, Any]:
    return {
        "source_id": source.id,
        "title": source.title,
        "reused": bool(source.reused),
        "parse_status": source.parse_status,
        "status": source.status,
        "agent_created": source.agent_created,
    }


def _own_source(repo: Any, notebook_id: str, source_id: str) -> SourceDetail:
    """Resolve a visible source owned by the selected notebook itself."""
    detail = repo.get_source(source_id)
    if detail.notebook_id != notebook_id or detail.type in _HIDDEN_SOURCE_TYPES:
        raise KeyError(source_id)
    return detail


def _reject_when_notebook_is_full(
    repo: Any, notebook_id: str, adding: int
) -> "int | None":
    """Apply the browser route's canonical notebook document ceiling.

    Returns the owner's effective limit (None = admin-exempt) so the caller can
    thread it into the creation call as ``capacity_limit`` — the pre-flight here
    is only the cheap early error; the authoritative enforcement is the COUNT
    inside the store's creation write transaction, mirroring the browser
    route's ``_enforce_document_capacity``."""
    capacity = _document_capacity(notebook_id, repo)
    if capacity is None:
        return None
    current, limit = capacity
    if current + adding > limit:
        raise AgentToolError(
            "unavailable", document_capacity_message(current, limit, adding)
        )
    return limit


def _markdown_source_file_name(title: str) -> str:
    """Derive a filesystem-safe bounded Markdown file name."""
    stem = safe_filename(title)
    encoded = stem.encode("utf-8")
    if len(encoded) > SOURCE_FILE_NAME_MAX_BYTES:
        stem = encoded[:SOURCE_FILE_NAME_MAX_BYTES].decode("utf-8", "ignore")
    return f"{stem.strip() or 'source'}.md"


def _source_status(detail: SourceDetail) -> dict[str, Any]:
    """One source's full parse/extraction state (``list_sources(source_id=)``)."""
    return {
        "source_id": detail.id,
        "parse_status": detail.parse_status,
        "status": detail.status,
        # A DERIVED boolean, never `detail.error_message`: that field is
        # `str(exc)` stored verbatim and routinely carries server-side absolute
        # paths. Same narrowing `ScopedSourceDetail` applies to the browser's
        # cross-notebook proxy read, for the same reason.
        "parse_failed": detail.parse_status == "failed",
        # MinerU fell back to a lossy local parser: layout, formulas and
        # tables may be wrong even though the source is "extracted". An Agent
        # about to cite it should know.
        "parse_quality_warning": detail.parse_quality_warning,
        "element_count": detail.element_count,
        "kg_extracted": detail.kg_extracted,
        # 分析跑完了、这篇里确实没有可整理的知识(正文极少 / 几乎全是没有图注的
        # 图片)。与 kg_extracted 并列而不是复用它:Agent 需要分清「还没分析」和
        # 「分析过、图谱里就是没有它」——前者值得重跑,后者重跑多少次都一样,该做的
        # 是换解析方式(如开 OCR)。
        "kg_analyzed_empty": detail.kg_analyzed_empty,
        "agent_created": detail.agent_created,
    }


def _text_source_input(title: Any, content_md: str) -> tuple[str, str, bytes]:
    """``add_source``'s Markdown group: ``(file_name, title, payload)``.

    Validated BEFORE any repository work, exactly as propose_memory does: a
    blank title or an oversized body is the caller's bug and must not cost a
    lookup, a lock or a file write."""
    clean_title = normalize_text(
        title, field="title", max_chars=SOURCE_TITLE_MAX_CHARS
    )
    if not content_md.strip():
        raise AgentToolError("invalid_argument", "content_md 不能为空")
    # The SAME ceiling the browser upload enforces per file
    # (`settings.source_upload_max_bytes`), measured on the bytes that will
    # actually be stored -- a character count would under-count CJK by 3x.
    payload = content_md.encode("utf-8")
    max_bytes = get_settings().source_upload_max_bytes
    if len(payload) > max_bytes:
        raise AgentToolError(
            "invalid_argument",
            f"content_md 有 {len(payload)} 字节，超过这个部署单个来源 "
            f"{max_bytes} 字节的上限",
        )
    return _markdown_source_file_name(clean_title), clean_title, payload


def _file_source_input(
    file_name: str, content_base64: str, title: Any
) -> tuple[str, str, bytes]:
    """``add_source``'s file group: ``(file_name, title, payload)``."""
    if not file_name.strip():
        raise AgentToolError("invalid_argument", "file_name 不能为空")
    clean_name = safe_filename(file_name.strip())
    if len(clean_name.encode("utf-8")) > FILESYSTEM_NAME_MAX_BYTES:
        raise AgentToolError(
            "invalid_argument",
            f"file_name 不能超过 {FILESYSTEM_NAME_MAX_BYTES} 个 UTF-8 字节",
        )
    suffix = Path(clean_name).suffix.lower()
    if suffix not in SOURCE_FILE_SUFFIXES:
        raise AgentToolError(
            "invalid_argument",
            "不支持的文件类型；支持的后缀："
            + ", ".join(sorted(SOURCE_FILE_SUFFIXES)),
        )
    if not content_base64:
        raise AgentToolError("invalid_argument", "content_base64 不能为空")
    max_bytes = get_settings().source_upload_max_bytes
    max_encoded_chars = ((max_bytes + 2) // 3) * 4
    if len(content_base64) > max_encoded_chars:
        raise AgentToolError(
            "invalid_argument",
            f"content_base64 超过这个部署单个来源解码后 {max_bytes} 字节的上限",
        )
    try:
        encoded = content_base64.encode("ascii")
        payload = base64.b64decode(encoded, validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError):
        raise AgentToolError(
            "invalid_argument",
            "content_base64 必须是标准 base64，不能含空白或 data-URI 前缀",
        ) from None
    if not payload:
        raise AgentToolError("invalid_argument", "解码后的文件不能为空")
    if len(payload) > max_bytes:
        raise AgentToolError(
            "invalid_argument",
            f"解码后的文件有 {len(payload)} 字节，超过这个部署单个来源 "
            f"{max_bytes} 字节的上限",
        )
    title_value = title.strip() if isinstance(title, str) else title
    clean_title = normalize_text(
        title_value or clean_name,
        field="title",
        max_chars=SOURCE_TITLE_MAX_CHARS,
    )
    return clean_name, clean_title, payload


_ADD_SOURCE_GROUPS = (
    "add_source 需要且只能提供一组输入：content_md（可带 title）、"
    "file_name + content_base64（可带 title）、或 url"
)


def _add_source_group(
    title: Any, content_md: Any, file_name: Any, content_base64: Any, url: Any,
) -> str:
    """Which ONE input group an ``add_source`` call supplied ("text"/"file"/"url").

    A group counts as supplied when any of its own fields is non-empty;
    ``title`` belongs to the text and file groups and is refused with ``url``.
    """
    groups = [
        group for group, supplied in (
            ("text", bool(content_md)),
            ("file", bool(file_name or content_base64)),
            ("url", bool(url)),
        ) if supplied
    ]
    if len(groups) != 1:
        raise AgentToolError("invalid_argument", _ADD_SOURCE_GROUPS)
    if groups[0] == "url" and title:
        raise AgentToolError(
            "invalid_argument", "url 方式不接受 title，标题取自下载到的文档"
        )
    if groups[0] == "url" and not url.strip():
        raise AgentToolError("invalid_argument", "url 不能为空")
    return groups[0]


def _add_url_source(
    repo: Any, notebook_id: str, principal: Any, clean_url: str
) -> dict[str, Any]:
    """``add_source``'s URL group (a PDF the server downloads)."""
    # Refuse a full notebook up front, with the SAME sentence the other two
    # groups use -- one condition must not have two wordings. The browser's
    # URL route cannot do this because a URL import is naturally partial
    # (unreachable / non-PDF entries are skipped), so it reports over-limit
    # entries in `rejected`; with exactly one URL the two are equivalent. The
    # returned limit is threaded into the creation call as `capacity_limit`,
    # where the store re-counts inside the INSERT's own write transaction --
    # the authority if the count moves between this check and the insert. In
    # that race the reason comes from add_url_sources' `rejected` entry,
    # which is that service's own user-facing wording (the browser shows it
    # too), not a second spelling of the sentence above.
    capacity_limit = _reject_when_notebook_is_full(repo, notebook_id, 1)
    try:
        result = repo.add_url_sources(
            notebook_id,
            [clean_url],
            lambda source_id: kg_scheduler.submit_job(
                repo.process_source, source_id
            ),
            capacity_limit,
            principal.profile_id,
        )
    except MinerUCloudNotConfigured:
        # The exception's own text names the deployment's env vars. An Agent
        # can act on "ask the operator", not on MINERU_API_TOKEN, and server
        # configuration is not its business.
        raise AgentToolError(
            "unavailable",
            "这个部署没有配置 PDF 解析服务，无法按 URL 添加 PDF；"
            "请改用 content_md 上传文本，或联系管理员配置 PDF 解析",
        ) from None
    if not result.created:
        # `reason` is the same user-facing sentence the browser shows for a
        # rejected URL ("not a PDF", "unreachable", "over the document limit")
        # and is the only actionable part of this failure. Bounded on the way
        # out by the response budget -- never re-raised with a traceback.
        reason = (
            result.rejected[0].reason if result.rejected
            else "这个 URL 被跳过了"
        )
        raise AgentToolError("invalid_argument", f"URL 没有添加成功：{reason}")
    source = result.created[0]
    return {
        "source_id": source.id,
        "title": source.title,
        "parse_status": source.parse_status,
        "status": source.status,
        "agent_created": source.agent_created,
    }


def register_source_tools(
    server: FastMCP, repository_provider: Callable[[], Any]
) -> None:
    @server.tool(
        description=(
            "List the user-visible sources owned by a notebook (notebook_id "
            "omitted = the token's default notebook), in the same stable order "
            "as its Sources panel. Returns a bounded page with source ids, "
            "display titles, file/type metadata, stored summary excerpts, and "
            "parse/extraction state. Hidden Memory and Knowhow projection rows "
            "and sources from mounted reference notebooks are excluded. Follow "
            "`next_offset` until it is null to read the complete inventory. "
            "Pass source_id instead to read ONE source's full parsing/"
            "extraction state -- the way to find out whether a source you just "
            "added is ready to be asked about (parse finished/failed/degraded, "
            "element count, knowledge extracted). Requires the read permission."
        ),
        tier="read",
    )
    async def list_sources(
        ctx: Context, notebook_id: str = "", source_id: str = "",
        offset: int = 0, limit: int = RESULT_LIMIT,
    ) -> dict[str, Any]:
        if offset < 0 or offset > OUTPUT_INTEGER_LIMIT - RESULT_LIMIT:
            raise AgentToolError(
                "invalid_argument", "offset 必须在零到 MCP 整数上限之间"
            )
        cap = max(1, min(int(limit), RESULT_LIMIT))
        repo = repository_provider()
        principal, notebook_id = await anyio.to_thread.run_sync(
            _selected_notebook, repo, notebook_id, "knowledge:read"
        )

        def load() -> dict[str, Any]:
            with _owner_request_context(principal):
                if source_id:
                    return _source_status(
                        _own_source(repo, notebook_id, source_id)
                    )
                page = repo.list_sources_page(
                    notebook_id, offset=offset, limit=cap
                )
                items = [
                    {
                        "source_id": item.id,
                        # Use the same single display-name rule as source cards,
                        # citations, and the Reasoning Ask source inventory.
                        # An empty value is the canonical unnamed-source result;
                        # do not reinterpret legacy whitespace-only titles here.
                        "title": item.display_title,
                        "file_name": item.file_name,
                        "source_type": item.type,
                        "doc_type": item.doc_type,
                        "summary": item.summary,
                        "parse_status": item.parse_status,
                        "status": item.status,
                        "parse_failed": item.parse_status == "failed",
                        "parse_quality_warning": item.parse_quality_warning,
                        "indexing_chunk_fallback": item.indexing_chunk_fallback,
                        "element_count": item.element_count,
                        "kg_extracted": item.kg_extracted,
                        "kg_analyzed_empty": item.kg_analyzed_empty,
                        "agent_created": item.agent_created,
                        "created_at": item.created_at,
                    }
                    for item in page.items
                ]
                return {
                    "notebook_id": notebook_id,
                    "items": items,
                    "total_count": page.total_count,
                    "offset": page.offset,
                    "limit": page.limit,
                    # Reserve the widest allowed scalar before the global
                    # response-budget pass. The real cursor is filled below;
                    # replacing this value can only make the payload smaller,
                    # never push an already-budgeted response back over 12 KB.
                    "next_offset": OUTPUT_INTEGER_LIMIT,
                }

        response = _budget_response(
            await _run_with_progress(ctx, load, label="list_sources"),
            field_limits={
                "title": 300,
                "file_name": 300,
                "source_type": 80,
                "doc_type": 80,
                "summary": 500,
                "parse_status": 40,
                "status": 40,
                "created_at": 80,
            },
        )
        if source_id:
            return response
        # The global MCP budget may have to drop trailing page rows after the
        # database read. Point the cursor at the first row not actually
        # delivered so following it can never skip user data.
        delivered = response.get("items", [])
        delivered_count = len(delivered) if isinstance(delivered, list) else 0
        response["next_offset"] = (
            offset + delivered_count
            if offset + delivered_count < int(response.get("total_count", 0))
            else None
        )
        return response

    @server.tool(
        description=(
            "Add ONE document to a notebook (notebook_id omitted = the token's "
            "default notebook) and start the ordinary background parsing "
            "pipeline; poll list_sources(source_id=...) for the outcome. Supply "
            "exactly one input group: (1) `content_md` (+ `title`, required): "
            "Markdown text you authored, stored verbatim as a .md source named "
            "after the title -- for a finished piece of work later answers can "
            "cite, not for scratch findings (those belong in propose_memory); "
            "(2) `file_name` + `content_base64` (+ optional `title`): exact file "
            "bytes as standard base64 (no data-URI prefix or whitespace); "
            "file_name must end in one of: "
            + ", ".join(SOURCE_FILE_EXTENSIONS)
            + " (ZIP is a Markdown bundle: every .md/.markdown member is parsed "
            "and relative png/jpeg/gif/webp images become source assets); "
            "(3) `url`: a PDF the server downloads -- it probes the URL first "
            "and refuses anything that is not a reachable PDF. Re-adding "
            "byte-identical content returns the existing source (`reused: "
            "true`) instead of a duplicate. Requires the manage permission and "
            "ownership of the notebook."
        ),
        tier="manage",
    )
    async def add_source(
        ctx: Context, notebook_id: str = "", title: str = "",
        content_md: str = "", file_name: str = "", content_base64: str = "",
        url: str = "",
    ) -> dict[str, Any]:
        # Validate the caller-controlled envelope BEFORE any repository work.
        group = _add_source_group(title, content_md, file_name, content_base64, url)
        upload = (
            _text_source_input(title, content_md) if group == "text"
            else _file_source_input(file_name, content_base64, title)
            if group == "file" else None
        )
        repo = repository_provider()
        principal, notebook_id = await anyio.to_thread.run_sync(
            _writable_notebook, repo, notebook_id, "sources:write"
        )

        def run() -> dict[str, Any]:
            with _owner_request_context(principal):
                if upload is None:
                    return _add_url_source(repo, notebook_id, principal, url.strip())
                stored_name, clean_title, payload = upload
                # Order matters: a call that is about to REUSE an existing row
                # adds no document, so the per-notebook ceiling must not refuse
                # it (see `_upload_agent_source`'s docstring for the mechanics).
                source = _upload_agent_source(
                    repo,
                    notebook_id=notebook_id,
                    principal=principal,
                    file_name=stored_name,
                    content_type=(
                        "text/markdown" if group == "text"
                        else mimetypes.guess_type(stored_name)[0]
                        or "application/octet-stream"
                    ),
                    payload=payload,
                    title=clean_title,
                )
                return _source_result(source)

        return _budget_response(
            await _run_with_progress(ctx, run, label="add_source"),
            field_limits={"title": 300, "parse_status": 40, "status": 40},
        )

    @server.tool(
        description=(
            "Re-run parsing and knowledge extraction for one source in a "
            "notebook (notebook_id omitted = the token's default notebook), "
            "discarding its previous elements. Use it after a failed or "
            "degraded parse. The work is queued in the background and this "
            "returns immediately -- poll list_sources(source_id=...) to see the "
            "result. Refuses with busy while that source is already being "
            "parsed. Requires the manage permission and ownership of the "
            "notebook."
        ),
        tier="manage",
    )
    async def reparse_source(
        source_id: str, ctx: Context, notebook_id: str = "",
    ) -> dict[str, Any]:
        repo = repository_provider()
        principal, notebook_id = await anyio.to_thread.run_sync(
            _writable_notebook, repo, notebook_id, "sources:write"
        )

        def run() -> dict[str, Any]:
            with _owner_request_context(principal):
                if _own_source(repo, notebook_id, source_id).type == (
                    PROMOTION_SOURCE_TYPE
                ):
                    # PR-E8: a promotion source has no file; parsing it would
                    # clear the promoted objects its elements support.
                    raise AgentToolError(
                        "forbidden",
                        "此操作不被允许：这份来源保存的是提升到公共库的内容，不能重新解析",
                    )
                # Fail before reporting a queued success. The worker repeats
                # this gate so a pipeline switch after submit is still closed.
                repo.require_indexing_pipeline_write(notebook_id)
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
                    raise AgentToolError(
                        "busy",
                        "这份来源正在解析；请轮询 list_sources(source_id=...)，"
                        "等它结束后再重试",
                    )
                kg_scheduler.submit_job(repo.process_source, source_id)
                return {"source_id": source_id, "queued": True}

        return _budget_response(
            await _run_with_progress(ctx, run, label="reparse_source")
        )

    @server.tool(
        description=(
            "Delete one source that an Agent added to a notebook (notebook_id "
            "omitted = the token's default notebook), together with everything "
            "derived from it. Sources a PERSON added are refused: use this only "
            "to clean up your own uploads. Irreversible. Requires the delete "
            "permission (which manage does not imply) and ownership of the "
            "notebook."
        ),
        tier="delete",
    )
    async def delete_source(
        source_id: str, ctx: Context, notebook_id: str = "",
    ) -> dict[str, Any]:
        repo = repository_provider()
        # ``record=False``:下面那条「只许删 Agent 自己添加的资料」是一道**鉴权**
        # (它抛 AgentToolError),而调用记账只记过了每一道鉴权的调用。记在这里
        # 会让一次被拒的删除在记录里长得和删成功一模一样(codex #616 R6 P2)。
        principal, notebook_id = await anyio.to_thread.run_sync(
            _writable_notebook, repo, notebook_id, "sources:delete", False
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
                    raise AgentToolError(
                        "forbidden",
                        "此操作不被允许：这份来源是用户添加的，Agent 凭证只能删除 Agent 添加的来源",
                    )
                # 每一道鉴权都过了才记账。⚠ 上面 ``_own_source`` 的「查不到」
                # **不**在此列(见产品文档里那条划线):那不是「不许你做」,是
                # 「这里没有这份资料」,而记账记的是「这个 Agent 对这个库发起过
                # 这种调用」——那件事确实发生了。
                _record_agent_call(repo, principal, notebook_id, "sources:delete")
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
