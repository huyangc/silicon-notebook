"""Compatibility re-export shim (definitions sunk to app.domain in B3).

The source display-title rule now lives in ``app.domain.source_display``
(zero app.* dependency, so app.repositories adapters can import
``summary_display_title`` directly). This module re-exports both names
unchanged so the three existing display-path importers (evidence_context,
retrieval_candidates, collection_enumeration) keep resolving to the SAME
objects without any call-site changes.

``backend/tests/test_source_display_title.py``'s ``DEFINITION_SITE`` was
updated to point at ``app/domain/source_display.py`` — the single-
implementation source-scan guard needs a real ``def source_display_title``
to anchor its allowlist, and this shim only imports the name.
"""
from __future__ import annotations

from app.domain.source_display import source_display_title, summary_display_title

__all__ = ["source_display_title", "summary_display_title"]
