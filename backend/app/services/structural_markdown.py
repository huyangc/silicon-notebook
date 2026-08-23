"""Compatibility re-export shim (definitions sunk to app.domain in B3).

The structural Markdown parser now lives in ``app.domain.structural_markdown``
(zero app.services/app.repositories dependency, only markdown_it). This
module re-exports every public name unchanged so existing importers
(parsers.py, kg/parsing.py, and the structural-markdown test suite) keep
resolving to the SAME objects without any call-site changes.
"""
from __future__ import annotations

from app.domain.structural_markdown import (
    Block,
    contains_data_uri_image_literal,
    parse_blocks,
    strip_data_uri_image_literals,
)

__all__ = [
    "Block",
    "contains_data_uri_image_literal",
    "parse_blocks",
    "strip_data_uri_image_literals",
]
