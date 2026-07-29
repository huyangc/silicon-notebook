"""Backend-neutral LIKE/ILIKE pattern escaping.

One definition for both adapters, because getting this wrong is silent rather
than loud: `_` is LIKE's single-character wildcard and command names carry it
by construction (`set_db`), so an unescaped needle degenerates into a fuzzy
match that also accepts `setXdb`.  Nothing raises — the query just returns
more rows than it promised.
"""
from __future__ import annotations


LIKE_ESCAPE_CHAR = "\\"


def escape_like_pattern(value: str, escape: str = LIKE_ESCAPE_CHAR) -> str:
    """Return ``value`` with every LIKE metacharacter neutralised.

    The escape character itself is doubled FIRST; doing it last would escape
    the escapes this function just introduced.

    The SQL side still differs per backend, and both are correct:

    * SQLite defines **no** default escape character, so its statements must
      spell ``ESCAPE '\\'`` out (SQLite string literals do not treat backslash
      specially, so the literal is one backslash character).
    * PostgreSQL's documented default LIKE escape character already is the
      backslash, and the pattern always reaches it as a bound parameter rather
      than a string literal — so ``standard_conforming_strings`` cannot change
      how it is read and no ESCAPE clause is needed.
    """
    return (
        (value or "")
        .replace(escape, escape + escape)
        .replace("%", escape + "%")
        .replace("_", escape + "_")
    )
