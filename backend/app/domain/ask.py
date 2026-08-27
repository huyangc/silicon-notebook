"""Stable Ask registry values."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AskMode:
    id: str
    handler: str  # method name on SQLiteRepository
    group: str  # "general" | "strict" | "extension"
    streaming: bool  # handler accepts on_trace and emits stream progress
    requires_kg: bool
    user_facing: bool
