"""Wire models for Agentic Memory P1's per-notebook "understanding" API (T6).

Two disjoint scopes ride every endpoint here: ``"shared"`` is the one shared
base layer everyone in the notebook sees (``owner_id=''`` on the store), and
``"mine"`` is the caller's own private overlay (``owner_id=<caller's user
id>``) — never anyone else's. The routes always derive ``owner_id`` from the
authenticated caller rather than from any request field, so there is nowhere
in this module a client could name a different member's overlay.

``extra="forbid"`` on every model: an unknown field in a request body is a
client bug worth surfacing as 422, not a value to silently drop.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.agent_profile_block import PROFILE_LABEL_ORDER

#: Wire-level scope discriminator. ``"shared"`` = the notebook-wide base layer
#: (write requires ``agent_profile:write``); ``"mine"`` = the caller's own
#: overlay (any reader may write it — ownership is intrinsic, since the
#: server always resolves ``owner_id`` to the caller's own id and a request
#: can never name someone else's).
UnderstandingScope = Literal["shared", "mine"]

#: The five app-layer labels, restated as a FastAPI path-parameter type so an
#: unknown label 422s before any handler code runs. Kept in sync with
#: ``PROFILE_LABEL_ORDER`` by the assertion below — the same "duplicate the
#: names as a literal, then assert the two agree" idiom
#: ``agent_profile_block.py`` already uses for its own label table, so a
#: label added in one place and not the other fails at import time instead of
#: quietly 422ing (or worse, silently accepting) requests for it.
UnderstandingLabel = Literal[
    "corpus_shape", "key_entities", "corpus_gaps", "retrieval_notes", "usage_gaps",
]
assert set(UnderstandingLabel.__args__) == set(PROFILE_LABEL_ORDER), (
    "UnderstandingLabel and PROFILE_LABEL_ORDER must name exactly the same labels"
)


class UnderstandingBlockOut(BaseModel):
    """One block row, projected for display/edit. ``evidence`` rides through
    as-is (its shape differs by label and by which chain wrote it — see
    ``agent_profile_job.py``'s base/overlay writers — so this stays a bare
    list rather than a typed union the browser has no use for)."""

    model_config = ConfigDict(extra="forbid")

    label: str
    value: str
    evidence: list = Field(default_factory=list)
    revision: int
    updated_at: str
    updated_origin: str


class UnderstandingJobStatus(BaseModel):
    """One consolidation chain's pollable status. Never the store's internal
    ``diagnostic`` column — that one is documented as never reaching a
    screen (``AgentProfileOutputRejected`` / store docstrings)."""

    model_config = ConfigDict(extra="forbid")

    status: str
    pending: int
    updated_at: str
    failure_reason: str


class UnderstandingJobs(BaseModel):
    """Both chains' status in one shot: the shared base and the caller's own
    overlay. Either side is ``None`` when that chain has never been claimed
    (no job row yet) — a normal cold-start state, not an error."""

    model_config = ConfigDict(extra="forbid")

    base: UnderstandingJobStatus | None = None
    mine: UnderstandingJobStatus | None = None


class UnderstandingResponse(BaseModel):
    """``GET /notebooks/{id}/understanding``. ``enabled=false`` (the total
    kill switch, or the deployment simply never wired a profile store) always
    carries empty lists and an empty ``job`` rather than 404 — the panel
    needs to tell "off" apart from "on but nothing written yet"."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    base: list[UnderstandingBlockOut] = Field(default_factory=list)
    mine: list[UnderstandingBlockOut] = Field(default_factory=list)
    job: UnderstandingJobs = Field(default_factory=UnderstandingJobs)
    can_edit_base: bool


class UnderstandingBlockUpdate(BaseModel):
    """``PUT /notebooks/{id}/understanding/{label}`` body. ``expected_revision``
    is required (not defaulted to 0) so a caller cannot accidentally CAS-create
    over a row it never read; ``0`` is still legal — it is exactly what a
    caller creating the row for the first time is expected to send."""

    model_config = ConfigDict(extra="forbid")

    scope: UnderstandingScope
    value: str
    expected_revision: int = Field(ge=0)


class UnderstandingRebuildRequest(BaseModel):
    """``POST /notebooks/{id}/understanding/rebuild`` body."""

    model_config = ConfigDict(extra="forbid")

    scope: UnderstandingScope


class UnderstandingRebuildResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    started: bool
