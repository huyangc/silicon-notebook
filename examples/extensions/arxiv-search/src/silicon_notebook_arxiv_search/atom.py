"""arXiv Atom feed parsing: bytes in, immutable papers out.

Pure by construction — no network, no filesystem, no clock, no ``app.*``
import.  Everything this module can do is decided by the bytes it is handed,
which is what makes it the layer an in-house variant replaces wholesale.

**Per-entry degradation.**  A feed entry that cannot yield an id or a title is
dropped and the rest of the page is still returned.  One malformed record must
never cost the user the nine good ones beside it.

**Registered limitation — XML entity expansion.**  ``xml.etree`` has no
defence against entity-expansion ("billion laughs") payloads.  The mitigation
here is twofold and deliberate rather than complete: the endpoint is
deployment-configured (it is not user input), and the caller reads the response
under a byte ceiling, so the parser never sees an unbounded document.  A plugin
pointed at an untrusted upstream should depend on ``defusedxml`` instead; this
sample keeps a zero-third-party-dependency footprint on purpose.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree import ElementTree

_ATOM_NS = "{http://www.w3.org/2005/Atom}"

# Plugin-private bounds.  Nothing here is a core rail: these only decide how
# much of one upstream record this plugin is willing to carry around.  Core
# applies its own, independent caps to whatever reaches a gap suggestion.
TITLE_MAX_CHARS = 200
SUMMARY_MAX_CHARS = 400
AUTHOR_MAX_CHARS = 80
PUBLISHED_MAX_CHARS = 40
MAX_AUTHORS = 20

_ABS_BASE = "https://arxiv.org/abs/"
_PDF_BASE = "https://arxiv.org/pdf/"
_INSECURE_PREFIX = "http://"
_SECURE_PREFIX = "https://"

# arXiv identifiers are either modern (`2401.00001v1`) or archive-qualified
# (`hep-th/9901001v2`) — the slash is part of the id, not a path separator.
_ID_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


@dataclass(frozen=True, slots=True)
class ArxivPaper:
    """One arXiv record, already bounded and whitespace-normalised."""

    arxiv_id: str
    title: str
    authors: tuple[str, ...]
    published: str
    summary: str
    pdf_url: str
    abs_url: str


def parse_atom(payload: bytes, *, limit: int) -> tuple[ArxivPaper, ...]:
    """Parse an arXiv Atom response into at most ``limit`` papers.

    Raises whatever ``xml.etree`` raises for a document it cannot parse at all
    (a truncated or non-XML response).  Callers map that to their own stable
    upstream-failure code; it is not caught here because "this response was not
    a feed" and "this feed had a bad entry" are different facts.
    """
    if limit <= 0:
        return ()
    root = ElementTree.fromstring(payload)
    papers: list[ArxivPaper] = []
    for entry in root.findall(f"{_ATOM_NS}entry"):
        if len(papers) >= limit:
            break
        paper = _entry_to_paper(entry)
        if paper is None:
            continue
        papers.append(paper)
    return tuple(papers)


def _entry_to_paper(entry: ElementTree.Element) -> ArxivPaper | None:
    arxiv_id = _arxiv_id(_text(entry, "id"))
    title = _collapse(_text(entry, "title"), TITLE_MAX_CHARS)
    if not arxiv_id or not title:
        return None
    return ArxivPaper(
        arxiv_id=arxiv_id,
        title=title,
        authors=_authors(entry),
        published=_collapse(_text(entry, "published"), PUBLISHED_MAX_CHARS),
        summary=_collapse(_text(entry, "summary"), SUMMARY_MAX_CHARS),
        pdf_url=_pdf_link(entry) or (_PDF_BASE + arxiv_id),
        abs_url=_ABS_BASE + arxiv_id,
    )


def _text(entry: ElementTree.Element, tag: str) -> str:
    return entry.findtext(f"{_ATOM_NS}{tag}") or ""


def _collapse(value: str, limit: int) -> str:
    """Fold every run of whitespace into one space, then cut to ``limit``.

    arXiv wraps titles and abstracts across lines with leading indentation, so
    the raw text is never display-ready.  The cut is a hard one: no ellipsis,
    because the result is also what the gap-consult path hands to core, and an
    appended marker would be indistinguishable from the record's own text.
    """
    return " ".join(value.split())[:limit]


def _authors(entry: ElementTree.Element) -> tuple[str, ...]:
    names = []
    for author in entry.findall(f"{_ATOM_NS}author"):
        if len(names) >= MAX_AUTHORS:
            break
        name = _collapse(author.findtext(f"{_ATOM_NS}name") or "", AUTHOR_MAX_CHARS)
        if name:
            names.append(name)
    return tuple(names)


def _arxiv_id(raw: str) -> str:
    """Extract the identifier from an ``<id>`` element.

    Splitting on ``/abs/`` rather than taking the last path segment keeps the
    archive prefix of legacy identifiers: ``.../abs/hep-th/9901001v2`` is the id
    ``hep-th/9901001v2``, and ``9901001v2`` alone resolves to nothing.
    """
    value = " ".join(raw.split())
    if not value:
        return ""
    if "/abs/" in value:
        value = value.split("/abs/", 1)[1]
    elif "/" in value:
        value = value.rsplit("/", 1)[1]
    return value if _ID_SHAPE.match(value) else ""


def _pdf_link(entry: ElementTree.Element) -> str:
    """Return the entry's declared PDF link, upgraded to https.

    arXiv still advertises these links as ``http://``.  Only the direct PDF link
    is of interest: the import path probes exactly the URL it is given and does
    not go hunting for one on a landing page.
    """
    for link in entry.findall(f"{_ATOM_NS}link"):
        if (link.get("title") or "").strip().lower() != "pdf":
            continue
        href = (link.get("href") or "").strip()
        if href:
            return _https(href)
    return ""


def _https(url: str) -> str:
    if url[: len(_INSECURE_PREFIX)].lower() == _INSECURE_PREFIX:
        return _SECURE_PREFIX + url[len(_INSECURE_PREFIX):]
    return url
