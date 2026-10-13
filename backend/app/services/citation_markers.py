"""Shared parsing for model-authored evidence citation markers.

Models are instructed to emit ``[k1]`` but some localised models substitute
localised brackets and punctuation (for example ``【k1，k2】`` or ``(k5、k9、k7)``).
These forms name the same server-owned evidence keys; consumers should not each grow
their own, subtly different regular expression.
"""
from __future__ import annotations

import re


_KEY_GROUP = r"k\d+(?:\s*[,，、]\s*k\d+)*"


def _marker_pattern(body: str) -> str:
    return rf"(?:\[{body}\]|【{body}】|\({body}\)|（{body}）)"


MARKER_RE = re.compile(_marker_pattern(_KEY_GROUP))
LOOSE_MARKER_RE = re.compile(_marker_pattern(r"\s*" + _KEY_GROUP + r"\s*"))


def marker_keys(marker: str) -> list[str]:
    """Return normalized evidence keys from one matched marker token."""
    return [part.strip() for part in re.split(r"[,，、]", marker[1:-1]) if part.strip()]
