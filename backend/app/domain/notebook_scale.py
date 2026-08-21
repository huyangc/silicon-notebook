"""Notebook scale value objects."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NotebookScaleFacts:
    bytes: int
    sources: int
    chunks: int
    nodes: int
    edges: int

    def as_size_dict(self) -> dict[str, int]:
        return {
            "bytes": self.bytes,
            "sources": self.sources,
            "chunks": self.chunks,
            "nodes": self.nodes,
            "edges": self.edges,
        }
