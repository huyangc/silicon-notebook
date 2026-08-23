"""Whitespace/case name normalization shared by KG denoising and migrations
(sunk from app.services.kg.filters._norm in B3).

Pure, zero app.* dependency. ``app.services.kg.filters`` keeps its own
``_norm`` name bound to this function (``_norm = normalize_name``) so its
existing internal callers are unaffected; app.repositories adapters that
only need the normalizer (concept-whitelist matching during migrations/
bundle restore) import ``normalize_name`` directly from here instead of
reaching into app.services.
"""
from __future__ import annotations

import re

_WS_RE = re.compile(r"[\s\-_]+")


def normalize_name(name: str) -> str:
    return _WS_RE.sub(" ", (name or "").strip().lower())
