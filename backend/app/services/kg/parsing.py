"""Compatibility re-export shim (definitions sunk to app.domain in B3).

Markdown source-element parsing and section-tree construction now live in
``app.domain.kg.parsing`` (zero app.services/app.repositories dependency —
its own internal dependency, app.domain.structural_markdown, was sunk in the
same change — so app.repositories.sqlite.maintenance can call
``parse_elements`` directly). This module re-exports every name unchanged so
existing importers (kg/windowing.py, kg/filters.py, kg/extract.py, and the
KG test suite) keep resolving to the SAME objects without any call-site
changes.
"""
from __future__ import annotations

from app.domain.kg.parsing import (
    SectionNode,
    SourceElementQ,
    build_section_tree,
    parse_elements,
)

__all__ = [
    "SectionNode",
    "SourceElementQ",
    "build_section_tree",
    "parse_elements",
]
