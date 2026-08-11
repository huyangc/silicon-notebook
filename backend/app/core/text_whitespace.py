"""The exact character set ``str.strip()`` removes.

Lives in ``core`` for the same reason ``query_syntax`` does: both the service
layer and the repository adapters need it.  ``app.services.kg.edge_trust``
decides *what* "an anchored quote" means (a quote that is non-empty after
``str.strip()``); the SQLite and PostgreSQL governance stores evaluate that same
predicate in SQL so a whole notebook's evidence JSON never has to be shipped
into Python and re-parsed.

SQLite ``trim(X, Y)`` and PostgreSQL ``btrim(X, Y)`` both default to U+0020
only, so the SQL side must be handed this alphabet explicitly — otherwise a
quote of ``"\\n"`` or ``"\\u3000"`` scores as anchored where ``.strip()`` scores
it 0.0.

Spelled out rather than derived: deriving it means scanning 0x110000 code points
on every import.  ``tests/test_edge_trust.py::test_py_strip_whitespace_matches_str_strip``
recomputes it and compares.
"""
from __future__ import annotations

PY_STRIP_WHITESPACE = (
    "\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f \x85\xa0"
    "\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)
