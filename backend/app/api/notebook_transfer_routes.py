"""Web export and import of single notebooks (``.snbk``).

The jobs and their rules live in ``app.services.notebook_transfer.jobs``; this
module is the HTTP edge: who may start what, the upload stream, and the ticketed
download (docs/superpowers/plans/2026-10-22-notebook-export-import.md §7, §9).

* ``POST /notebooks/{id}/export`` -- capability ``notebook:export`` (owner; a
  sync mirror may be exported). 413 when the source files it would carry (Memory
  left out) already exceed ``NOTEBOOK_TRANSFER_WEB_MAX_MB``; the job also stops
  when the archive it writes grows past it. Asking again while the caller's
  export of that notebook is active returns that job; a caller with
  ``MAX_ACTIVE_JOBS_PER_USER`` active jobs gets 409.
* ``POST /notebook-imports`` -- any signed-in user; a slot is reserved before
  the body is read (the same 409 when none is free) and given back on any
  failure or disconnect. The body is the raw archive (``application/zip``), streamed to disk
  in bounded writes and refused with 413 the moment it passes the web limit (a
  declared ``Content-Length`` above it is refused before reading). Before the job
  starts, the zip directory is held to ``WEB_IMPORT_MAX_INFLATION`` times the web
  limit (413, nothing decompressed); for a user who is not an administrator the
  archive's sources are counted (``archive_document_count``) against the
  per-notebook document limit, the count stopping as soon as it passes it.
  A file that is not an archive or is damaged is 400 (``user_error``); one
  written by a newer build is 400 with ``{"code": "archive_too_new"}`` -- the
  remedy is an upgrade, not another file.
* ``GET /notebook-transfers[/{job}]``, ``DELETE /notebook-transfers/{job}`` -- the
  caller's own jobs; another user's job is 404 like a missing one. ``DELETE``
  says what became of the job (``outcome``) and names the notebook an import
  had already created, which stays.
* ``POST /notebook-transfers/{job}/download-ticket`` and
  ``GET /notebook-transfers/download?ticket=`` -- the one-time download. The
  download takes no authorization header (a browser navigates to it); every
  refusal is the same 404. Its query string, like the import's, never reaches a
  log (``app.core.log_paths``).
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.api.deps import (
    get_current_user,
    identity_repository,
    notebook_catalog_repository,
    notebook_transfer_jobs,
    repository,
    require_notebook_capability,
    user_error,
)
from app.api.download_headers import attachment_content_disposition, safe_download_name
from app.core.config import get_settings
from app.models.identity import UserProfile
from app.models.notebook_transfer import (
    NotebookTransferDeleted,
    NotebookTransferDownloadTicket,
    NotebookTransferJob,
    NotebookTransferList,
)
from app.services.notebook_transfer.import_ import (
    NotebookImportError,
    NotebookImportTooLarge,
    NotebookImportTooNew,
    archive_document_count,
    check_web_archive,
)
from app.services.notebook_transfer.jobs import (
    MAX_ACTIVE_JOBS_PER_USER,
    TransferBusy,
    TransferJobNotFound,
    TransferNotReady,
)

router = APIRouter()
#: The ticketed download: no session (a browser navigates to it with the ticket
#: alone), so ``app.main`` includes it without the router-wide
#: ``get_current_user`` -- and before that router, whose
#: ``/notebook-transfers/{job_id}`` would otherwise match ``download`` first.
download_router = APIRouter()

#: The upload is written in blocks of at most this many bytes (one request
#: chunk past it at most), never held whole.
UPLOAD_WRITE_BYTES = 1 << 20
#: A notebook name given to an import (the new notebook's name).
_NAME_MAX_CHARS = 200
_ARCHIVE_MEDIA_TYPE = "application/zip"
#: Every refused download answers exactly this (§9.4: no reason is told apart).
_DOWNLOAD_REFUSED = {"detail": "Not found"}
_DOWNLOAD_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
_JOB_NOT_FOUND = "Transfer job not found"
#: 400 for an archive written by a newer build (a newer format version, or a
#: column this deployment lacks): ``code`` for the client to branch on,
#: ``message`` a complete Chinese sentence.
_ARCHIVE_TOO_NEW = {
    "code": "archive_too_new",
    "message": "这个文件来自更新版本的部署，请先升级本部署再导入。",
}


def _megabytes(limit_bytes: int) -> int:
    return limit_bytes // (1024 * 1024)


def _busy() -> HTTPException:
    return user_error(
        409,
        f"你已有 {MAX_ACTIVE_JOBS_PER_USER} 个导出或导入正在进行，请等其中一个完成后再试。",
    )


@router.post(
    "/notebooks/{notebook_id}/export",
    status_code=202,
    response_model=NotebookTransferJob,
    dependencies=[Depends(require_notebook_capability("notebook:export"))],
)
def export_notebook_route(
    notebook_id: str, user: UserProfile = Depends(get_current_user)
) -> dict:
    """Start exporting the notebook to one ``.snbk`` file (202 + the job; the
    caller's export of it that is still active, if there is one). The job runs in
    the background; once it has ``succeeded``, a download ticket fetches the file
    until the job expires."""
    limit = get_settings().notebook_transfer_web_max_bytes
    jobs = notebook_transfer_jobs()
    if jobs.export_source_bytes(notebook_id) > limit:
        raise user_error(
            413,
            f"这本笔记本的原文件已超过网页导出上限（{_megabytes(limit)} MB），"
            "请联系管理员用命令行导出。",
        )
    try:
        name = notebook_catalog_repository().get_notebook(notebook_id).name
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found") from None
    try:
        return jobs.start_export(notebook_id=notebook_id, notebook_name=name, user_id=user.id)
    except TransferBusy:
        raise _busy() from None


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _upload_too_large(limit: int) -> HTTPException:
    return user_error(
        413,
        f"导入文件超过网页导入上限（{_megabytes(limit)} MB），请联系管理员用命令行导入。",
    )


async def _receive_upload(request: Request, part: Path, limit: int) -> int:
    """Stream the request body into ``part`` (created here, ``O_EXCL``), at most
    ``UPLOAD_WRITE_BYTES`` held at a time; on any failure the part file is
    removed. Returns the bytes received."""
    fd = os.open(
        part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    received = 0
    pending = bytearray()
    try:
        async for chunk in request.stream():
            received += len(chunk)
            if received > limit:
                raise _upload_too_large(limit)
            pending += chunk
            if len(pending) >= UPLOAD_WRITE_BYTES:
                block, pending = bytes(pending), bytearray()
                await run_in_threadpool(_write_all, fd, block)
        if pending:
            await run_in_threadpool(_write_all, fd, bytes(pending))
    except BaseException:
        os.close(fd)
        part.unlink(missing_ok=True)
        raise
    os.close(fd)
    return received


@router.post(
    "/notebook-imports",
    status_code=202,
    response_model=NotebookTransferJob,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {_ARCHIVE_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
async def import_notebook_route(
    request: Request,
    name: str = Query("", max_length=_NAME_MAX_CHARS),
    user: UserProfile = Depends(get_current_user),
) -> dict:
    """Import one ``.snbk`` file as a new notebook owned by the caller (202 + the
    job). The body is the raw file (``Content-Type: application/zip``); ``name``
    renames the new notebook (empty: the exported name). A file that is not an
    archive or is damaged is 400; one written by a newer build is 400 with the
    detail ``{"code": "archive_too_new", "message"}`` (upgrade this deployment)."""
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != _ARCHIVE_MEDIA_TYPE:
        raise user_error(415, "请上传笔记本导出文件（.snbk）。")
    limit = get_settings().notebook_transfer_web_max_bytes
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise _upload_too_large(limit)
    jobs = notebook_transfer_jobs()
    try:
        # The slot is taken before a byte of the body is read, and given back on
        # every outcome but the job (below).
        job_id, part = jobs.reserve_import(user.id)
    except TransferBusy:
        raise _busy() from None
    try:
        received = await _receive_upload(request, part, limit)
        if received == 0:
            raise user_error(400, "上传的文件是空的。")
        bound = jobs.import_inflation_bound()
        allowed = None
        if user.role != "admin":
            allowed = await run_in_threadpool(repository().effective_document_limit, user.id)
        try:
            if allowed is None:
                # An administrator is not held to the document limit: the archive
                # is admitted on its directory and manifest alone.
                await run_in_threadpool(lambda: check_web_archive(part, max_inflated_bytes=bound))
                documents = 0
            else:
                documents = await run_in_threadpool(
                    lambda: archive_document_count(
                        part, max_inflated_bytes=bound, stop_above=allowed,
                    )
                )
        except NotebookImportTooLarge:
            raise user_error(
                413, "这个文件解压后过大，超出网页导入能处理的范围，请联系管理员用命令行导入。"
            ) from None
        except NotebookImportTooNew:
            # Not a damaged file: the remedy is an upgrade, so the client must be
            # able to tell it apart -- a structured detail with a stable code.
            raise HTTPException(status_code=400, detail=_ARCHIVE_TOO_NEW) from None
        except NotebookImportError:
            raise user_error(
                400, "这个文件无法导入：它不是笔记本导出文件，或者已经损坏。"
            ) from None
        if allowed is not None and documents > allowed:
            raise user_error(
                409,
                f"这个文件的文档超过每本笔记本最多 {allowed} 篇的上限；请联系管理员用命令行导入。",
            )
        return await run_in_threadpool(
            lambda: jobs.start_import(
                repository(), job_id=job_id, upload=part, user_id=user.id,
                name=name.strip(),
            )
        )
    except BaseException:
        # Refused, failed or disconnected before the job owns the file and the
        # slot (start_import takes both): give both back.
        part.unlink(missing_ok=True)
        jobs.release_upload(job_id)
        raise


@router.get("/notebook-transfers", response_model=NotebookTransferList)
def list_notebook_transfers(user: UserProfile = Depends(get_current_user)) -> dict:
    """The caller's export and import jobs, newest first."""
    return {"jobs": notebook_transfer_jobs().list_jobs(user.id)}


def _file_blocks(handle, on_complete):
    """The export's blocks, read from ``handle`` -- a file object, so the
    descriptor is closed even when the response never starts (the object is
    collected with the generator) -- and ``on_complete()`` once every block was
    handed over."""
    with handle:
        while True:
            block = handle.read(UPLOAD_WRITE_BYTES)
            if not block:
                break
            yield block
    on_complete()


@download_router.get("/notebook-transfers/download", response_model=None)
def download_notebook_transfer(ticket: str = Query("")) -> Response:
    """Stream a finished export once, by its ticket (no authorization header).
    Any refusal -- an unknown, replayed or expired ticket, a job that expired or
    was deleted, a user who was disabled -- is the same 404."""
    jobs = notebook_transfer_jobs()
    download = jobs.redeem_ticket(ticket, user_active=identity_repository().auth.owner_eligible)
    if download is None:
        return JSONResponse(_DOWNLOAD_REFUSED, status_code=404, headers=_DOWNLOAD_HEADERS)
    handle = os.fdopen(download.fd, "rb", buffering=0)
    stem = safe_download_name(download.notebook_name, fallback=download.notebook_id)
    return StreamingResponse(
        _file_blocks(handle, lambda: jobs.mark_downloaded(download.job_id)),
        media_type=_ARCHIVE_MEDIA_TYPE,
        headers={
            **_DOWNLOAD_HEADERS,
            "Content-Length": str(download.size),
            "Content-Disposition": attachment_content_disposition(
                f"{stem}.snbk", fallback=f"{download.notebook_id}.snbk"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/notebook-transfers/{job_id}", response_model=NotebookTransferJob)
def get_notebook_transfer(job_id: str, user: UserProfile = Depends(get_current_user)) -> dict:
    try:
        return notebook_transfer_jobs().job(user.id, job_id)
    except TransferJobNotFound:
        raise HTTPException(status_code=404, detail=_JOB_NOT_FOUND) from None


@router.post(
    "/notebook-transfers/{job_id}/download-ticket",
    response_model=NotebookTransferDownloadTicket,
)
def create_notebook_transfer_ticket(
    job_id: str, user: UserProfile = Depends(get_current_user)
) -> dict:
    """A one-time ticket for the caller's finished export: ``url`` downloads it
    once within 60 seconds."""
    try:
        return notebook_transfer_jobs().issue_ticket(user.id, job_id)
    except TransferJobNotFound:
        raise HTTPException(status_code=404, detail=_JOB_NOT_FOUND) from None
    except TransferNotReady:
        raise user_error(409, "导出还没有完成，暂时不能下载。") from None


@router.delete("/notebook-transfers/{job_id}", response_model=NotebookTransferDeleted)
def delete_notebook_transfer(job_id: str, user: UserProfile = Depends(get_current_user)) -> dict:
    """Cancel the caller's queued or running job, or delete a finished one; its
    files go. A running import is waited for (up to 30 s) until it is known
    whether its notebook stays: ``outcome`` is ``deleted``, ``cancelled``,
    ``kept`` (``kept_notebook_id`` names the notebook, which stays the caller's)
    or ``pending`` (still settling: the job stays readable and ends
    ``cancelled``, or goes on with its notebook if it had published)."""
    try:
        result = notebook_transfer_jobs().delete(user.id, job_id)
    except TransferJobNotFound:
        raise HTTPException(status_code=404, detail=_JOB_NOT_FOUND) from None
    return {"id": job_id, **result}
