"""Stable Ask registry values."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AskMode:
    id: str
    handler: str
    group: str
    streaming: bool
    requires_kg: bool
    user_facing: bool

