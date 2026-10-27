"""The case fold that SQL ``lower()`` applies on both backends (pure stdlib).

SQLite's built-in ``lower()`` (no ICU) and PostgreSQL's ``lower()`` over a
``COLLATE "C"`` column fold only A-Z. A Python value compared with such an
expression, or predicting a collision on a ``lower(...)`` index, must fold the
same way: ``str.lower()`` also folds non-ASCII letters, so it disagrees with the
database on spellings such as ``Ä123``. Usernames (``app.domain.auth_utils``)
and the comparison focal check (``app.services.communities``, rule R4) share
this one table, so the two can never drift apart.
"""
from __future__ import annotations

_ASCII_LOWER = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
)


def ascii_lower(value: str) -> str:
    """Fold only A-Z to a-z; every other character is left unchanged."""
    return value.translate(_ASCII_LOWER)
