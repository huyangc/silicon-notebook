"""Compatibility re-export shim (definitions sunk to app.domain in B3).

The document-type extraction profiles now live in
``app.domain.extraction_profiles`` (zero ``app.services``/
``app.repositories`` dependency, only ``app.models.sources``, so
``app.repositories`` adapters can import it directly). This module
re-exports every public name unchanged so existing importers keep resolving
to the SAME objects without any call-site changes.
"""
from __future__ import annotations

from app.domain.extraction_profiles import (
    DEFAULT_PROFILE_ID,
    ENUM_FIELDS,
    LIST_FIELDS,
    OBJECT_SCHEMAS,
    OBJECT_TYPE_LABELS,
    PROFILES,
    TEMPLATE_PROFILE,
    ExtractionProfile,
    ObjectSchema,
    detect_doc_type,
    detect_doc_type_from_sample,
    get_profile,
    profile_for_template,
    resolve_profile,
)

__all__ = [
    "DEFAULT_PROFILE_ID",
    "ENUM_FIELDS",
    "LIST_FIELDS",
    "OBJECT_SCHEMAS",
    "OBJECT_TYPE_LABELS",
    "PROFILES",
    "TEMPLATE_PROFILE",
    "ExtractionProfile",
    "ObjectSchema",
    "detect_doc_type",
    "detect_doc_type_from_sample",
    "get_profile",
    "profile_for_template",
    "resolve_profile",
]
