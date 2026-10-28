"""Response shapes of the web notebook export / import jobs
(``app.api.notebook_transfer_routes``; the jobs: ``app.services.notebook_transfer.jobs``)."""
from __future__ import annotations

from typing import Any, List, Literal, Optional

from pydantic import BaseModel, ConfigDict


class NotebookTransferError(BaseModel):
    """Why a job failed: ``code`` for the client to branch on, ``message`` a
    complete Chinese sentence that may be shown as it is. Neither ever carries a
    path, an exception message or file content."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str


class NotebookTransferJob(BaseModel):
    """One export or import job of the current user.

    ``state``: ``queued`` -> ``running`` -> ``succeeded`` / ``failed`` /
    ``cancelled``. ``phase`` names the step a running job is in (export:
    ``queued``, ``writing``, ``done``; import: ``queued``, ``preflight``,
    ``files``, ``rows``, ``validate``, ``publish``, ``project``, ``reembed``,
    ``index``, ``done``). ``notebook_id`` is the exported notebook, or the new
    notebook of an import once it is published (``""`` before). A finished job
    is kept until ``expires_at``; a ``succeeded`` export is ``downloadable``
    through a one-time ticket until then."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["export", "import"]
    state: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    phase: str
    notebook_id: str
    notebook_name: str
    created_at: str
    updated_at: str
    finished_at: Optional[str] = None
    expires_at: Optional[str] = None
    progress: dict[str, Any]
    archive_bytes: int
    downloadable: bool
    downloaded: bool
    report: Optional[dict[str, Any]] = None
    reembed: Optional[dict[str, Any]] = None
    index: Optional[dict[str, Any]] = None
    error: Optional[NotebookTransferError] = None
    #: An import's client upload id (``X-Upload-Id`` of the upload that created
    #: it; ``""`` when none was sent, and always for an export).
    client_upload_id: str = ""


class NotebookTransferList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jobs: List[NotebookTransferJob]


class NotebookTransferDeleted(BaseModel):
    """``DELETE /notebook-transfers/{id}``. ``outcome``: ``deleted`` (a finished
    job is gone), ``cancelled`` (an active job was stopped; nothing of it
    stays), ``kept`` (an import had already created its notebook: it stays and
    is the caller's, ``kept_notebook_id`` names it) or ``pending`` (a running
    import still settling after 30 s: the job stays readable and ends
    ``cancelled`` -- or, had it published meanwhile, carries on with its
    notebook; read it again for the outcome)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    outcome: Literal["deleted", "cancelled", "kept", "pending"]
    kept_notebook_id: Optional[str] = None


class NotebookTransferDownloadTicket(BaseModel):
    """``url`` downloads the export once, without an authorization header, until
    ``expires_at``."""

    model_config = ConfigDict(extra="forbid")

    url: str
    expires_at: str
