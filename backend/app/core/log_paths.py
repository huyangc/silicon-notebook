"""Request paths as logs record them: route shape and opaque ids, no content.

A few routes carry user or source content -- or a credential -- in a path segment:
the KG analysis finding dismissal (F4's subject key is a source-derived concept name,
F2's is a pair of source ids), the KG detail reads addressed by a merged object's
canonical id (the id carries a member's normalized name), group invite and public
share tokens, custom object schema type names. Every place that records a request path (the uvicorn access log,
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

# Each entry: (a literal every match contains, the pattern, its replacement). The
# literal is a substring test run before the pattern, so a path that cannot match
# costs no regex search -- every request's log line goes through this table.
_CONTENT_SEGMENTS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "/kg-analysis/findings/",
        re.compile(r"(/api/notebooks/[^/?#]+/kg-analysis/findings/[^/?#]+/).+(/dismissal)$"),
        r"\1{subject_key}\2",
    ),
    ("/group-invites/", re.compile(r"(/api/group-invites/)[^/?#]+(/join)$"), r"\1{token}\2"),
    ("/public/reports/", re.compile(r"(/api/public/reports/)[^/?#]+$"), r"\1{token}"),
    (
        "/public/conversations/",
        re.compile(r"(/api/public/conversations/)[^/?#]+(/assets/)[^/?#]+$"),
        r"\1{token}\2{alias}",
    ),
    ("/public/conversations/", re.compile(r"(/api/public/conversations/)[^/?#]+$"), r"\1{token}"),
    (
        "/object-schemas/",
        re.compile(r"(/api/(?:notebooks/[^/?#]+/)?object-schemas/)[^/?#]+$"),
        r"\1{object_type}",
    ),
    # KG detail reads addressed by a merged object's canonical id
    # (``concepts/{id}/detail``, ``objects/{id}/neighbors``, ``objects/{id}/context``):
    # ``K-`` / ``KL-`` / ``KF-`` / ``KP-`` plus a member's normalized name, which may
    # contain "/". An opaque object id (``ko-…``) on the same routes is kept.
    (
        "/K",
        re.compile(
            r"(/api/notebooks/[^/?#]+/(?:concepts|objects)/)K[LFP]?-.+"
            r"(/(?:detail|neighbors|context))$"
        ),
        r"\1{canonical_id}\2",
    ),
)


def loggable_path(path: str) -> str:
    """``path`` (no query string) with any content-bearing segment replaced."""
    for needle, pattern, replacement in _CONTENT_SEGMENTS:
        if needle not in path:
            continue
        templated, count = pattern.subn(replacement, path, count=1)
        if count:
            return templated
    return path


def loggable_target(target: str) -> str:
    """A request target (path plus optional ``?query``): the path part made loggable."""
    path, separator, query = target.partition("?")
    return f"{loggable_path(path)}{separator}{query}"
