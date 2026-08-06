"""Shared parsing for model-authored evidence citation markers.

Models are instructed to emit ``[k1]`` but some localised models substitute
Chinese book-title brackets and punctuation (for example ``【k1，k2】``).  Both
forms name the same server-owned evidence keys; consumers should not each grow
their own, subtly different regular expression.
"""
from __future__ import annotations

import re


MARKER_RE = re.compile(
    r"(?:\[(?:k\d+\s*[,，]\s*)*k\d+\]|【(?:k\d+\s*[,，]\s*)*k\d+】)"
)
LOOSE_MARKER_RE = re.compile(
    r"(?:\[\s*k\d+(?:\s*[,，]\s*k\d+)*\s*\]|"
    r"【\s*k\d+(?:\s*[,，]\s*k\d+)*\s*】)"
)


def marker_keys(marker: str) -> list[str]:
    """Return normalized evidence keys from one matched marker token."""
    return [part.strip() for part in re.split(r"[,，]", marker[1:-1]) if part.strip()]
