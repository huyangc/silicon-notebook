"""Stable read shape for a loaded scale-index artifact."""
from __future__ import annotations

from typing import Any, Protocol


class ScaleIndexView(Protocol):
    node_ids: list
    node_index: dict
    transition: Any
    idf: Any
    chunk_index: Any
    manifest: dict

