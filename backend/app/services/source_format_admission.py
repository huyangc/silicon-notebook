"""Narrow source-format admission view for non-routing consumers.

The parser capability registry remains the single owner of supported formats.
Callers such as the MCP tool bundle need only its immutable upload allowlist,
not the registry topology or any provider/extension composition surface.
"""

from app.services.parser_registry import (
    SUPPORTED_SOURCE_EXTENSIONS,
    SUPPORTED_SOURCE_SUFFIXES,
)


def supported_source_extensions() -> tuple[str, ...]:
    return SUPPORTED_SOURCE_EXTENSIONS


def supported_source_suffixes() -> frozenset[str]:
    return frozenset(SUPPORTED_SOURCE_SUFFIXES)
