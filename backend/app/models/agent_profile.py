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

#: The five app-layer labels, in render order. Canonical definition lives
#: here (not in ``app.services.agent_profile_block``, which imports it back)
#: because ``app.models`` must stay import-clean of ``app.services`` --
#: see ``scripts/architecture_boundary_baseline.json`` ::
#: core_models_service_imports (now empty). This tuple is also the renderer's
#: whitelist: a row whose label is not here renders nothing at all -- see
#: ``agent_profile_block.selected_profile_blocks`` for why dropping beats
#: rendering-at-the-end.
PROFILE_LABEL_ORDER: tuple[str, ...] = (
    "corpus_shape",
    "key_entities",
    "corpus_gaps",
    "retrieval_notes",
    "usage_gaps",
)

#: Wire-level scope discriminator. ``"shared"`` = the notebook-wide base layer
#: (write requires ``agent_profile:write``); ``"mine"`` = the caller's own
#: overlay (any reader may write it — ownership is intrinsic, since the
#: server always resolves ``owner_id`` to the caller's own id and a request
#: can never name someone else's).
UnderstandingScope = Literal["shared", "mine"]

#: The five app-layer labels, restated as a FastAPI path-parameter type so an
#: unknown label 422s before any handler code runs (both PUT and DELETE wire
#: it into their ``label`` path parameter). Kept in sync with
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


# ---------------------------------------------------------------------------
# Agentic Memory P3 (T5): "Agent 记录" — the caller's own read/clear view over
# ``agent_observations`` (T2's store, T3's ``add_observation`` MCP tool). Both
# endpoints below are ``scope="mine"`` in every sense the ``Understanding*``
# models above use that word: ``owner_id`` is always the authenticated caller,
# never a request field, so there is nothing here a client could point at
# someone else's rows.
# ---------------------------------------------------------------------------


class AgentObservationOut(BaseModel):
    """One line an external Agent wrote via ``add_observation``, projected
    for the caller's own "Agent 记录" list. Wraps the store's four-field
    ``project_observation_row`` shape (``id``/``agent_profile_id``/``text``/
    ``created_at``) plus a resolved display name — never the raw
    ``owner_id``, which this endpoint never returns because it is always
    exactly the caller who asked for it (mirrors ``project_observation_row``'s
    own reasoning for omitting it)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    #: Raw Agent id, carried through so the browser can pass it back as the
    #: ``agent_profile_id`` filter on ``DELETE .../agent-observations``
    #: ("清空这个 Agent 的记录"). Never shown to the user directly — that is
    #: what ``agent_name`` is for.
    agent_profile_id: str
    agent_name: str
    text: str
    created_at: str


class AgentObservationsResponse(BaseModel):
    """``GET /notebooks/{id}/agent-observations``. ``enabled=false`` (总闸关闭,
    or the deployment never wired a profile store) always carries an empty
    list rather than 404 — same "off" vs "on but nothing written yet"
    distinction ``UnderstandingResponse`` makes."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    items: list[AgentObservationOut] = Field(default_factory=list)


class AgentObservationsCleared(BaseModel):
    """``DELETE /notebooks/{id}/agent-observations`` response."""

    model_config = ConfigDict(extra="forbid")

    removed: int
