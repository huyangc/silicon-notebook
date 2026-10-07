"""The Knowhow table transfer's refusal, shared by both repository backends and the
route that answers it."""
from __future__ import annotations

MEMORY_SOURCE = "memory_source"
CHUNK_SOURCE_MISMATCH = "chunk_source_mismatch"


class KnowhowTransferRefused(ValueError):
    """The transfer insert refused its payload; the whole transfer rolled back.

    ``reason`` is ``MEMORY_SOURCE`` (the payload's passages would belong to a Memory
    source, which is private per user and never owns passages in the shared index)
    or ``CHUNK_SOURCE_MISMATCH`` (a passage row names a source other than the
    table's own hidden source). The payload is built from the stored table, so a
    retry refuses the same way; the route answers with a classified user error
    instead of an internal one."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason
