"""Request paths as logs record them: route shape and opaque ids, no content.

A few routes carry user or source content -- or a credential -- in a path segment:
the KG analysis finding dismissal (F4's subject key is a source-derived concept name,
F2's is a pair of source ids), group invite and public share tokens, custom object
schema type names. Every place that records a request path (the uvicorn access log,
the ``requests`` event channel) passes it through :func:`loggable_path`, which keeps
the route and replaces those segments with a placeholder. The request itself is
untouched -- only the copy a log line records -- and the API shape does not change.

The patterns match the decoded path (``request.url.path`` / ``scope["path"]``) and
the percent-encoded request target uvicorn logs alike, with or without a root path
in front of ``/api``; F4 keys may contain ``/``, so the dismissal subject spans
everything between the rule id and the trailing ``/dismissal``.
"""

from __future__ import annotations

import re

_CONTENT_SEGMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(/api/notebooks/[^/?#]+/kg-analysis/findings/[^/?#]+/).+(/dismissal)$"),
        r"\1{subject_key}\2",
    ),
    (re.compile(r"(/api/group-invites/)[^/?#]+(/join)$"), r"\1{token}\2"),
    (re.compile(r"(/api/public/reports/)[^/?#]+$"), r"\1{token}"),
    (re.compile(r"(/api/public/conversations/)[^/?#]+(/assets/)[^/?#]+$"), r"\1{token}\2{alias}"),
    (re.compile(r"(/api/public/conversations/)[^/?#]+$"), r"\1{token}"),
    (re.compile(r"(/api/(?:notebooks/[^/?#]+/)?object-schemas/)[^/?#]+$"), r"\1{object_type}"),
)


def loggable_path(path: str) -> str:
    """``path`` (no query string) with any content-bearing segment replaced."""
    for pattern, replacement in _CONTENT_SEGMENTS:
        templated, count = pattern.subn(replacement, path, count=1)
        if count:
            return templated
    return path


def loggable_target(target: str) -> str:
    """A request target (path plus optional ``?query``): the path part made loggable."""
    path, separator, query = target.partition("?")
    return f"{loggable_path(path)}{separator}{query}"
