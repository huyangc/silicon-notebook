"""Shared JS-anchor normalization for SQLite migrations and guarded writes."""
from __future__ import annotations


# ECMAScript WhiteSpace ∪ LineTerminator, the exact set stripped by
# String.prototype.trim(). Python's default str.strip() differs at U+001C–
# U+001F/U+0085 and U+FEFF, so the group keys must use this explicit set.
JS_TRIM_WHITESPACE = (
    "\t\n\x0b\x0c\r \xa0"
    "\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)

_JS_TRIM_CODEPOINTS = ", ".join(str(ord(character)) for character in JS_TRIM_WHITESPACE)


def js_trim(value: str | None) -> str:
    """Return ``value`` normalized exactly like JS ``String.prototype.trim()``."""
    return (value or "").strip(JS_TRIM_WHITESPACE)


def sqlite_js_trim_expression(value_sql: str) -> str:
    """Build SQLite's equivalent normalization expression for a SQL value.

    ``value_sql`` is supplied only by repository-owned SQL (a column reference
    in the migration/store or a bound placeholder in tests), never user input.
    SQLite's two-argument ``trim`` plus ``char`` avoids an application-defined
    SQL function, allowing an expression index to remain portable and usable.
    """
    return f"trim({value_sql}, char({_JS_TRIM_CODEPOINTS}))"
