"""Dependency-light declarations for build-time workspace UI contributions.

The backend owns capability availability but never ships executable browser
code.  These declarations are therefore metadata only: a matching static
frontend contribution is required before anything can render.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


WorkspaceUiSlot = Literal["workspace.side_panel", "source.detail_section"]

@dataclass(frozen=True)
class UiContributionDeclaration:
    id: str
    slot: WorkspaceUiSlot
    capability: str
