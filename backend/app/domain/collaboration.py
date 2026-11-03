"""Data-only collaboration protocol values shared by HTTP and job publishers."""
from dataclasses import dataclass
from typing import Literal

PulseKind = Literal["sources", "kg", "knowhow", "knowledge", "catalog", "notebook", "understanding", "scale_index", "none"]
PULSE_KINDS = frozenset({"sources", "kg", "knowhow", "knowledge", "catalog", "notebook", "understanding", "scale_index", "none"})
PULSE_COALESCE_SECONDS = 1.0
PULSE_PRESENCE_INTERVALS = 3
PULSE_POOL_WINDOW_SECONDS = 10.0
PULSE_POOL_SAMPLE_SECONDS = 1.0

# Producer dependencies, not a request to eagerly hydrate every workspace.
# Source extraction can publish graph objects and automatic notebook metadata;
# Knowhow projection rewrites graph objects/relations and notebook kg_ready.
SOURCE_TERMINAL_PULSE_KINDS = ("sources", "knowledge", "kg", "notebook")
KNOWHOW_TERMINAL_PULSE_KINDS = ("knowhow", "knowledge", "kg", "notebook")
# Graph maintenance changes graph-derived content; durable builds also settle
# per-source extraction status. Conflict review keeps its separate kg-only hint.
KG_TERMINAL_PULSE_KINDS = ("kg", "knowledge", "notebook")
KG_BUILD_TERMINAL_PULSE_KINDS = ("kg", "knowledge", "notebook", "sources")


@dataclass(frozen=True)
class PulseActor:
    user_id: str
    display_name: str
    role: Literal["owner", "admin", "editor", "reader"]


@dataclass(frozen=True)
class CollabMark:
    source: Literal["guard", "explicit"]
    notebook_id: str
    capability: str
    owner_id: str
    actor: PulseActor
