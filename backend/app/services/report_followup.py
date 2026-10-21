"""Pure logic of "ask a question about a finished deep report".

A question may attach one finished report of the notebook (``report_id``).
Everything here is a pure function over values the caller already read: no
database, no model, no retrieval ceiling of its own. ``AskService`` reads the
report row, the cited items' owners and the run's installed ceiling, and hands
them to these functions; the retrieval layer reads the cited items back
(``AskCandidatePort.restore_report_references``). The shared vocabulary -- the
reason codes, their Chinese clauses and the frozen values -- lives one layer
down in ``app.domain.report_followup``.

* ``load_linked_report`` -- the report gate (own report, this notebook, done);
* ``report_meta_digest`` -- the stored report without its body (title,
  section titles, references, Memory use): all the verdict, the pre-send check
  and the understanding step read;
* ``report_digest`` -- the same with the executive summary and the sections'
  bodies parsed from ``content_md`` (the only body whose ``[kN]`` markers pair
  with the stored ``references``), for the run that renders the report block;
* ``report_link_verdict`` -- may this turn read the report under the ceiling
  it runs with; one implementation for the run, the reasoning intent preview
  and the pre-send check;
* ``select_report_sections`` / ``render_report_block`` /
  ``rewrite_report_markers`` / ``report_outline_text`` -- what of the report
  enters the prompts, bounded by ``ask_report_context_max_chars``;
* ``followup_receipt`` / ``skipped_receipt`` / ``report_trace_step`` -- what
  the turn tells the reader it did with the report;
* ``report_followup_context`` / ``current_report_followup`` -- the run's
  ``ReportFollowup``, computed once where the engine is entered
  (``AskService._engine_scope``) and read by the engines and the answer's
  persistence seam; ``ReportFollowupReaders`` are the store reads behind it,
  ``report_restore_request`` what it asks the retrieval layer to read back,
  ``report_mode_supported`` which engines read a report at all and
  ``followup_outline`` the outline line the understanding and rewriting steps
  see.
"""
from __future__ import annotations

import re
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import (
    Any,
    Callable,
    Collection,
    Iterable,
    Iterator,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Union,
)

from app.domain.report_followup import (
    REASON_CLAUSE_MEMORY_CLOSED_CHANNEL,
    REASON_CLAUSE_MEMORY_CLOSED_NARROWED,
    REASON_CLAUSES,
    REASON_ENGINE_UNSUPPORTED,
    REASON_LIBRARY_UNAVAILABLE,
    REASON_MEMORY_CLOSED,
    REASON_NO_SYNTHESIS,
    REASON_PREFIX,
    REASON_SCOPE_LIBRARIES,
    REASON_SCOPE_SOURCES,
    RESTORE_SKIPPED_OBJECT_TYPES,
    CitedReportReference,
    LinkedReportNotFound,
    LinkedReportNotReady,
    ReportDigest,
    ReportFollowup,
    ReportLinkVerdict,
    ReportSection,
    SectionSelection,
)
from app.models.ask import LinkedReportReceipt, LinkedReportSection, TraceStep
from app.services.ask_modes import ASK_MODES
from app.services.citation_markers import LOOSE_MARKER_RE, MARKER_RE, marker_keys
from app.services.retrieval import KeywordBasis, _tokens, keyword_basis


# --- the report gate --------------------------------------------------------
def load_linked_report(
    read_report: Callable[[str, str], Mapping[str, Any]],
    notebook_id: str,
    report_id: str,
    user_id: Optional[str],
) -> Mapping[str, Any]:
    """The report row a question may follow up, or the gate's exception.

    ``read_report`` is ``ReportStore.get_report(notebook_id, report_id)``: it
    reads by notebook only and raises ``KeyError`` for an unknown id. Missing,
    another member's and another notebook's report all raise
    ``LinkedReportNotFound`` -- one answer for the three, the same predicate as
    ``report_routes._own_report_or_404``, so the gate never tells a caller that
    an id exists. A report of the caller's own that is not ``done`` raises
    ``LinkedReportNotReady``.
    """
    try:
        row = read_report(notebook_id, report_id)
    except KeyError:
        raise LinkedReportNotFound(report_id) from None
    if str(row.get("created_by") or "") != user_id:
        raise LinkedReportNotFound(report_id)
    if str(row.get("notebook_id") or notebook_id) != notebook_id:
        raise LinkedReportNotFound(report_id)
    if str(row.get("status") or "") != "done":
        raise LinkedReportNotReady(report_id)
    return row


# --- digest -----------------------------------------------------------------
# The fixed headings and tail notes ``ReportEngine._assemble`` (and
# ``corpus_profile_reader_markdown``) write around the sections of
# ``content_md``. ``tests/test_report_followup.py`` parses a body the real
# engine assembled, so a change of these strings there turns that test red.
_SUMMARY_HEADING = "执行摘要"
_SOURCE_BASIS_HEADING = "资料基础"
_REFERENCES_HEADING = "参考文献"
_TAIL_NOTE_PREFIXES = ("> 引证分布：", "> **范围与证据局限**")
_TOP_HEADING = "## "
_FENCES = ("```", "~~~")

# A section heading the model wrote is matched to the outline title after
# folding what models vary on: width (NFKC), whitespace, emphasis marks, a
# trailing colon or full stop, and one leading ordinal ("1.", "一、", "第二节",
# "(3)"). Both sides are folded the same way, so the fold can only make two
# spellings of one title meet.
_EMPHASIS = re.compile(r"[*_`]+")
_LEADING_ORDINAL = re.compile(
    r"^\s*(?:"
    r"第\s*[0-9一二三四五六七八九十百]+\s*[章节部分篇]"
    r"|[(（]\s*[0-9一二三四五六七八九十]+\s*[)）]"
    r"|[0-9]+(?:\.[0-9]+)*(?:[.、:：)）]|\s)"
    r"|[一二三四五六七八九十]+[、.:：)）]"
    r")\s*"
)
_HEADING_TRAILERS = ":：。."


def _heading_key(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text or "")
    folded = _EMPHASIS.sub("", folded)
    folded = _LEADING_ORDINAL.sub("", folded, count=1)
    return "".join(folded.split()).rstrip(_HEADING_TRAILERS)


def _fenced(lines: list[str]) -> list[bool]:
    """Per line: is it inside (or delimiting) a fenced code block?"""
    mask: list[bool] = []
    fence = ""
    for line in lines:
        stripped = line.lstrip()
        if fence:
            mask.append(True)
            if stripped.startswith(fence):
                fence = ""
            continue
        opener = next((f for f in _FENCES if stripped.startswith(f)), "")
        mask.append(bool(opener))
        fence = opener
    return mask


def _body_end(lines: list[str], fenced: list[bool]) -> int:
    """Index of the first line after the report body.

    The body ends at the first tail note, or before that at the engine's
    references heading. The engine writes its ``## 参考文献`` after every
    section and right before the ``> 引证分布：`` note, so it is the LAST such
    heading before the tail; an earlier one is a heading the model wrote
    inside a section and stays part of that section.
    """
    tail = next(
        (
            i for i, line in enumerate(lines)
            if not fenced[i] and line.startswith(_TAIL_NOTE_PREFIXES)
        ),
        len(lines),
    )
    references = [
        i for i in range(tail)
        if not fenced[i] and lines[i].startswith(_TOP_HEADING)
        and lines[i][len(_TOP_HEADING):].strip() == _REFERENCES_HEADING
    ]
    return references[-1] if references else tail


# Which markers of the stored body name the report's references. The engine
# renumbers each section's markers to the report-wide keys of ``references``
# with the strict ``citation_markers.MARKER_RE`` (``ReportEngine._assemble``),
# and only the section bodies: the executive summary is stored as the model
# wrote it (its prompt forbids markers, so any it carries is unnumbered). A
# marker only the loose pattern matches -- inner spaces, ``[ k2 ]`` -- was
# never renumbered either. Such markers name nothing in ``references``: they
# are removed whole, never counted as keys and never rewritten (rewriting one
# would point this turn's answer at an unrelated item).
def _global_marker(marker: str) -> bool:
    return MARKER_RE.fullmatch(marker) is not None


def _keys_in(text: str) -> tuple[str, ...]:
    """Report reference keys of a section body's renumbered markers, in order
    of first appearance."""
    seen: dict[str, None] = {}
    for match in LOOSE_MARKER_RE.finditer(text or ""):
        if _global_marker(match.group(0)):
            for key in marker_keys(match.group(0)):
                seen.setdefault(key, None)
    return tuple(seen)


def _strip_markers(text: str) -> str:
    """``text`` without any citation marker (the executive summary's)."""
    return LOOSE_MARKER_RE.sub("", text or "")


def _aligned_section(
    keys: list[str], start: int, wanted: str, at: int, last_at: Mapping[str, int],
) -> Optional[int]:
    """The section a heading at line ``at`` opens: the first title from
    ``start`` on whose folded form is ``wanted``. Skipping titles to reach it
    is allowed only when none of the skipped titles is still a heading further
    down -- otherwise the heading is the model's own text that happens to
    repeat a later title (a section's closing ``## 结论`` before the report's
    own 结论 section) and the sections in between would be misaligned."""
    for index in range(start, len(keys)):
        if keys[index] and keys[index] == wanted:
            skipped = keys[start:index]
            if any(key and last_at.get(key, -1) > at for key in skipped):
                return None
            return index
    return None


def _split_body(content_md: str, titles: tuple[str, ...]) -> tuple[str, list[str]]:
    """``(executive summary, one body per title)`` of a stored ``content_md``.

    Top-level ``## `` headings outside code fences drive the split. Before the
    first section: ``## 执行摘要`` opens the summary, ``## 资料基础`` a block that
    is dropped, the ``# `` title line is dropped. A heading that aligns with a
    title still ahead (``_heading_key``, ``_aligned_section``) opens that
    section -- titles it skips keep an empty body. A heading that aligns with
    none is the model's own: it stays in the summary or the current section,
    and only after the source-basis block, with no section open yet, does it
    open the next section (everything there is section text). The references
    list and the tail notes are never read.
    """
    lines = (content_md or "").splitlines()
    fenced = _fenced(lines)
    end = _body_end(lines, fenced)
    headings = {
        index: lines[index][len(_TOP_HEADING):].strip()
        for index in range(end)
        if not fenced[index] and lines[index].startswith(_TOP_HEADING)
    }
    folded = {index: _heading_key(heading) for index, heading in headings.items()}
    last_at = {key: index for index, key in folded.items()}
    keys = [_heading_key(title) for title in titles]
    summary: list[str] = []
    bodies: list[list[str]] = [[] for _ in titles]
    target: Optional[list[str]] = None
    next_section = 0
    opened = seen_summary = False
    for index, line in enumerate(lines[:end]):
        if index in headings:
            heading = headings[index]
            if not opened and not seen_summary and heading == _SUMMARY_HEADING:
                seen_summary, target = True, summary
                continue
            if not opened and heading == _SOURCE_BASIS_HEADING:
                target = None
                continue
            found = _aligned_section(keys, next_section, folded[index], index, last_at)
            if found is None and not opened and target is not summary:
                if next_section >= len(titles):
                    target = None
                    continue
                found = next_section
            if found is not None:
                opened, next_section, target = True, found + 1, bodies[found]
                continue
        if target is not None:
            target.append(line)
    return "\n".join(summary).strip(), ["\n".join(body).strip() for body in bodies]


def report_meta_digest(row: Mapping[str, Any]) -> ReportDigest:
    """The stored report WITHOUT its body: what the verdict, the pre-send
    check and the understanding step read (plan D4/D5).

    ``title`` is ``reports.question`` whole; ``sections`` are the stored
    section list (``row["sections"]``) by title, in order, with NO body and no
    keys; ``summary`` is empty; ``references`` and ``memory_used`` are copied
    whole (the verdict judges the entire report). ``content_md`` is not
    parsed: an empty body here means "not read", not "no body text" -- only
    ``report_digest`` may feed ``select_report_sections`` /
    ``render_report_block``. Total: any malformed field reads as empty."""
    stored_sections = row.get("sections")
    stored_references = row.get("references")
    stored_memory = row.get("memory_used")
    return ReportDigest(
        title=str(row.get("question") or ""),
        summary="",
        sections=tuple(
            ReportSection(
                index=index,
                title=str(section.get("title") or "") if isinstance(section, Mapping) else "",
            )
            for index, section in enumerate(
                stored_sections if isinstance(stored_sections, (list, tuple)) else (),
                start=1,
            )
        ),
        references=tuple(
            MappingProxyType(dict(reference))
            for reference in (
                stored_references if isinstance(stored_references, (list, tuple)) else ()
            )
            if isinstance(reference, Mapping)
        ),
        memory_used=tuple(
            item for item in (stored_memory if isinstance(stored_memory, (list, tuple)) else ())
            if isinstance(item, str) and item
        ),
        summary_keys=(),
    )


def report_digest(row: Mapping[str, Any]) -> ReportDigest:
    """A stored report reduced to what a follow-up question reads -- the
    meta digest (``report_meta_digest``) with the body parsed in: only the run
    that renders the report block needs it.

    Total: any malformed field reads as empty, never raises. ``summary`` is
    the executive summary without citation markers; each section gets its
    body from ``content_md`` (empty when its heading cannot be found there; a
    failed section's body is the stored neutral failure note) and the report
    keys of its renumbered markers.
    """
    meta = report_meta_digest(row)
    content = row.get("content_md")
    summary, bodies = _split_body(
        content if isinstance(content, str) else "", meta.section_titles,
    )
    return replace(
        meta,
        # The summary's markers were never renumbered to the report's keys
        # (see ``_global_marker``): they are dropped here, so the summary has
        # no keys -- ``summary_keys`` stays () -- and nothing of it is restored
        # or rewritten.
        summary=_strip_markers(summary),
        sections=tuple(
            replace(section, body=body, keys=_keys_in(body))
            for section, body in zip(meta.sections, bodies)
        ),
    )


# --- verdict ----------------------------------------------------------------
class ReportScope(Protocol):
    """The three faces of ``source_scope.ActiveSourceScope`` the verdict reads."""

    withheld_hidden_source_ids: Collection[str]

    def covers_notebook(self, notebook_id: str) -> bool: ...

    def allows(self, notebook_id: str, source_id: str) -> bool: ...


_MEMORY_TYPE = "memory"
# Cited web material (an ``ask.reflect_action`` / external-evidence item): it
# has no row in any notebook, so there is nothing to look up and nothing the
# turn could read back. Like a whole-source reference it is neither restored
# nor counted as gone (``RESTORE_SKIPPED_OBJECT_TYPES``), and never a conflict;
# its marker is dropped from the report block like any key the turn lacks.
_EXTERNAL_TYPE = "external"


def _field(reference: Mapping[str, Any], name: str) -> str:
    value = reference.get(name)
    return value.strip() if isinstance(value, str) else ""


def report_owner_ids(digest: ReportDigest) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(source ids, object ids)`` the verdict needs owners for, each once.

    Every cited ``source_id`` (``source_owners``), and the ``object_id`` of
    each reference that names no source (``object_owners``). Memory and
    external references need no owner. The caller reads each list in ONE batch
    and passes the two maps to ``report_link_verdict`` and
    ``report_reference_owner``."""
    sources: dict[str, None] = {}
    objects: dict[str, None] = {}
    for reference in digest.references:
        if _field(reference, "object_type") in (_MEMORY_TYPE, _EXTERNAL_TYPE):
            continue
        source_id = _field(reference, "source_id")
        if source_id:
            sources.setdefault(source_id, None)
            continue
        object_id = _field(reference, "object_id")
        if object_id:
            objects.setdefault(object_id, None)
    return tuple(sources), tuple(objects)


def report_reference_owner(
    reference: Mapping[str, Any],
    *,
    source_owners: Mapping[str, str],
    object_owners: Mapping[str, str],
) -> Optional[str]:
    """The notebook that owns a non-memory reference's item now, or None when
    the item no longer exists (or never named one): by its source when it
    names one, else by its object."""
    if _field(reference, "object_type") == _EXTERNAL_TYPE:
        return None
    source_id = _field(reference, "source_id")
    if source_id:
        return source_owners.get(source_id) or None
    object_id = _field(reference, "object_id")
    return (object_owners.get(object_id) or None) if object_id else None


def report_reason_text(
    reason_code: str, *, n: int = 0, names: str = "", memory_channel_open: bool = True,
) -> str:
    """The displayable ``reason`` for ``reason_code`` -- prefix plus clause.

    The single place a reason is worded (the receipt, the pre-send check and
    the trace all carry this string). ``memory_closed`` has two clauses:
    ``memory_channel_open`` says whether the caller may read Memory at all
    (closed only by narrowing the scope) or not (for example an Agent token
    without the Memory read grant). An unknown code raises ``ValueError``."""
    if reason_code == REASON_MEMORY_CLOSED:
        clause = (
            REASON_CLAUSE_MEMORY_CLOSED_NARROWED
            if memory_channel_open else REASON_CLAUSE_MEMORY_CLOSED_CHANNEL
        )
    elif reason_code in REASON_CLAUSES:
        clause = REASON_CLAUSES[reason_code].format(n=n, names=names)
    else:
        raise ValueError(f"unknown report reason code {reason_code!r}")
    return REASON_PREFIX + clause


def unread_verdict(reason_code: str) -> ReportLinkVerdict:
    """The verdict of a turn that does not read the report for a reason decided
    outside ``report_link_verdict`` -- the extension engine, the report gate
    (``report_unavailable``) or a failed read (``read_failed``). Nothing was
    read, so every count stays 0."""
    return ReportLinkVerdict(reason_code=reason_code, reason=report_reason_text(reason_code))


def _library_names(libraries: Iterable[str], names: Mapping[str, str]) -> str:
    return "".join(f"《{names[lib]}》" for lib in libraries if names.get(lib))


def report_link_verdict(
    digest: ReportDigest,
    *,
    notebook_id: str,
    scope: ReportScope,
    participants: Collection[str],
    skipped_libraries: Collection[str],
    source_owners: Mapping[str, str],
    object_owners: Mapping[str, str],
    memory_open: bool,
    memory_channel_open: bool,
    mode_supported: bool,
    library_names: Mapping[str, str],
) -> ReportLinkVerdict:
    """May this turn read the report, judged under the ceiling it runs with.

    The single implementation for the run, the reasoning intent preview and
    the pre-send check (D4). It judges EVERY reference and the recorded
    Memory use, not just the sections a turn would read: the executive summary
    is always read and was written from all of them.

    Inputs are what the caller read once: ``scope`` is the installed
    ``current_source_scope()``; ``participants`` the libraries the asker may
    use (``participant_notebook_ids`` with an explicit viewer);
    ``skipped_libraries`` those the ceiling froze empty this run
    (``current_skipped_mounted_libraries()``); ``source_owners`` /
    ``object_owners`` the two batch reads over ``report_owner_ids(digest)``
    (existing rows only, hidden projections included); ``memory_open`` whether
    this turn reads the asker's Memory, ``memory_channel_open`` whether the
    caller may read Memory at all; ``library_names`` the names the notebook
    mounts its libraries under now.

    ``engine_unsupported`` is decided first and touches none of the other
    inputs. Then, per reference: Memory -> uses Memory; owner gone -> ``gone``
    (never a conflict), except a whole-source or external reference, which
    names no item to read back and is never gone
    (``RESTORE_SKIPPED_OBJECT_TYPES``); a withheld Memory projection source
    -> uses Memory; a
    library the asker cannot use -> unavailable; an unticked library ->
    unchecked; a source the scope leaves out -> out of scope. The first reason
    that holds wins: ``library_unavailable``, ``scope_libraries``,
    ``scope_sources``, ``memory_closed``.
    """
    if not mode_supported:
        return unread_verdict(REASON_ENGINE_UNSUPPORTED)
    withheld = scope.withheld_hidden_source_ids
    gone = 0
    uses_memory = bool(digest.memory_used)
    unavailable: dict[str, None] = {}
    unchecked: dict[str, None] = {}
    out_of_scope: dict[tuple[str, str], None] = {}
    for reference in digest.references:
        object_type = _field(reference, "object_type")
        if object_type == _MEMORY_TYPE:
            uses_memory = True
            continue
        library = report_reference_owner(
            reference, source_owners=source_owners, object_owners=object_owners
        )
        if library is None:
            # A whole-source or external reference names no item the turn
            # could read back, so it is never gone (``RESTORE_SKIPPED_OBJECT_
            # TYPES``); a live whole source is still judged below.
            if object_type not in RESTORE_SKIPPED_OBJECT_TYPES:
                gone += 1
            continue
        source_id = _field(reference, "source_id")
        if source_id and source_id in withheld:
            uses_memory = True
            continue
        if library != notebook_id:
            if library not in participants or library in skipped_libraries:
                unavailable.setdefault(library, None)
                continue
            if not scope.covers_notebook(library):
                unchecked.setdefault(library, None)
                continue
        if source_id and not scope.allows(library, source_id):
            out_of_scope.setdefault((library, source_id), None)
    if unavailable:
        code, text = REASON_LIBRARY_UNAVAILABLE, report_reason_text(
            REASON_LIBRARY_UNAVAILABLE, n=len(unavailable)
        )
    elif unchecked:
        code, text = REASON_SCOPE_LIBRARIES, report_reason_text(
            REASON_SCOPE_LIBRARIES, names=_library_names(unchecked, library_names)
        )
    elif out_of_scope:
        code, text = REASON_SCOPE_SOURCES, report_reason_text(
            REASON_SCOPE_SOURCES, n=len(out_of_scope)
        )
    elif uses_memory and not memory_open:
        code, text = REASON_MEMORY_CLOSED, report_reason_text(
            REASON_MEMORY_CLOSED, memory_channel_open=memory_channel_open
        )
    else:
        code, text = "", ""
    return ReportLinkVerdict(
        reason_code=code,
        reason=text,
        gone=gone,
        unavailable_libs=frozenset(unavailable),
        unchecked_libs=frozenset(unchecked),
        out_of_scope_count=len(out_of_scope),
        uses_memory=uses_memory,
    )


# --- markers ----------------------------------------------------------------
def rewrite_report_markers(text: str, key_map: Mapping[str, str]) -> str:
    """Point the report's ``[kN]`` markers at this turn's keys (D3).

    ``key_map`` maps a report reference key to the key its restored item has
    in this turn's prompt. A marker the engine renumbered (``MARKER_RE``:
    ``[k1]``, ``[k1, k3]``, ``【k2】``, ``(k4)`` ...) keeps only its mapped keys,
    written ``[kA, kB]``, and is removed whole when none is mapped. A marker
    only ``LOOSE_MARKER_RE`` matches (``[ k2 ]``) was never renumbered and is
    always removed (see ``_global_marker``). No report key reaches the prompt
    unmapped: it would name an unrelated item of this turn."""

    def rewrite(match: re.Match[str]) -> str:
        if not _global_marker(match.group(0)):
            return ""
        kept: list[str] = []
        for key in marker_keys(match.group(0)):
            mapped = key_map.get(key)
            if mapped and mapped not in kept:
                kept.append(mapped)
        return "[" + ", ".join(kept) + "]" if kept else ""

    return LOOSE_MARKER_RE.sub(rewrite, text or "")


# Wider than any key a restored item can take in the prompt (the widest
# native segment, element, ends at k4999). Measuring every marker as if each
# of its keys had this width bounds the block BEFORE ``key_map`` exists:
# selection decides which keys are needed, and only then are they restored.
_WIDEST_TURN_KEY = "k99999"


def _widened(text: str) -> str:
    """``text`` as long as ``rewrite_report_markers`` can ever make it."""

    def widen(match: re.Match[str]) -> str:
        if not _global_marker(match.group(0)):
            return ""
        return "[" + ", ".join(
            _WIDEST_TURN_KEY for _key in marker_keys(match.group(0))
        ) + "]"

    return LOOSE_MARKER_RE.sub(widen, text or "")


# --- report block and section selection -------------------------------------
REPORT_BLOCK_HEADER = (
    "Attached report the user is asking about (written earlier by this system; "
    "NOT evidence — see rule 15):"
)
_SECTION_NOT_LOADED = "(body not loaded this turn: over the report-context budget)"
_SECTION_NO_BODY = "(this section has no separate body text in the stored report)"
SUMMARY_TRUNCATED_SUFFIX = "（执行摘要已截断）"
_SENTENCE_ENDS = frozenset("。！？；.!?")


def _block(
    title: str, summary: str, digest: ReportDigest, read: Collection[int],
    body: Callable[[str], str],
) -> str:
    lines = [REPORT_BLOCK_HEADER, f"Title: 《{title}》"]
    if summary:
        lines += ["Executive summary:", summary]
    if digest.sections:
        lines.append(f"Sections ({len(digest.sections)} in total):")
        for section in digest.sections:
            lines.append(f"## {section.index}. {section.title}")
            if not section.body:
                lines.append(_SECTION_NO_BODY)
            elif section.index in read:
                lines.append(body(section.body))
            else:
                lines.append(_SECTION_NOT_LOADED)
    return "\n".join(lines)


def _clip_summary(summary: str, room: int) -> str:
    """The longest prefix of the marker-free ``summary`` within ``room``
    characters that ends at a sentence end; a hard cut at ``room`` when no
    sentence ends inside it. One scan of the kept prefix."""
    if room <= 0:
        return ""
    head = summary[:room]
    end = max(head.rfind(char) for char in _SENTENCE_ENDS)
    return head[:end + 1] if end >= 0 else head.rstrip()


def _section_score(basis: KeywordBasis, section: ReportSection) -> float:
    text = f"{section.title}\n{section.body}"
    return basis.coverage(set(_tokens(text)), text)


def select_report_sections(
    digest: ReportDigest, query: str, budget_chars: int,
) -> SectionSelection:
    """Which parts of the report enter this turn's prompt (D12, §2.6).

    The skeleton -- header, title, executive summary (without markers, see
    ``report_digest``) and every section heading -- always enters. When the
    skeleton alone exceeds ``budget_chars`` the summary is clipped at its last
    sentence end that fits (a hard cut when none does) and
    ``SUMMARY_TRUNCATED_SUFFIX`` is appended here, the only place it is
    added. Section bodies then fill what is left WHOLE (never half a section):
    all of them when they fit, otherwise by lexical relevance to ``query``
    (``retrieval.keyword_basis(query).coverage`` over title and body; ties by
    section order), skipping a body that does not fit and trying the next. A
    body shorter than the placeholder it replaces is always taken. A section
    with no body text is never read: it is listed by its heading only.

    Lengths are measured with every marker at its widest possible rewrite, so
    the rendered block stays within ``budget_chars`` whatever keys the items
    take. Two parts are never cut and can push a block past it: the report
    title (``reports.question``, at most ``REPORT_QUESTION_MAX_CHARS``) and
    the section titles (user-confirmed outline titles); the summary then
    shrinks to the suffix alone and no body is read. ``query`` is the turn's
    retrieval query (chunk) or research question (reasoning).
    """
    summary, truncated = _strip_markers(digest.summary), False
    skeleton = len(_block(digest.title, summary, digest, (), _widened))
    if summary and skeleton > budget_chars:
        # Everything but the summary's own text: the block with a one-char summary.
        overhead = len(_block(digest.title, "x", digest, (), _widened)) - 1
        summary = _clip_summary(
            summary, budget_chars - overhead - len(SUMMARY_TRUNCATED_SUFFIX)
        ) + SUMMARY_TRUNCATED_SUFFIX
        truncated = True
        skeleton = len(_block(digest.title, summary, digest, (), _widened))
    remaining = budget_chars - skeleton
    costs = {
        section.index: len(_widened(section.body)) - len(_SECTION_NOT_LOADED)
        for section in digest.sections if section.body
    }
    if sum(costs.values()) <= remaining:
        chosen = set(costs)
    else:
        chosen = {index for index, cost in costs.items() if cost <= 0}
        remaining -= sum(costs[index] for index in chosen)
        basis = keyword_basis(query or "")
        ranked = sorted(
            (s for s in digest.sections if s.index in costs and s.index not in chosen),
            key=lambda s: (-_section_score(basis, s), s.index),
        )
        for section in ranked:
            if costs[section.index] <= remaining:
                chosen.add(section.index)
                remaining -= costs[section.index]
    # The summary carries no keys (see ``report_digest``): the keys are the
    # read sections', in report order.
    keys: dict[str, None] = {}
    for section in digest.sections:
        if section.index in chosen:
            for key in section.keys:
                keys.setdefault(key, None)
    return SectionSelection(
        read_indices=tuple(sorted(chosen)),
        summary=summary,
        summary_truncated=truncated,
        keys=tuple(keys),
    )


def render_report_block(
    digest: ReportDigest, selection: SectionSelection, key_map: Mapping[str, str],
) -> str:
    """The attached-report block of the answer prompt (§2.6), sections in the
    report's order: a read section with its body, an unread one with a
    placeholder line, one without body text with a line saying so.
    ``selection.summary`` is used as clipped and marked by
    ``select_report_sections``, with any marker removed (the summary's are
    never the report's keys); section markers are rewritten through
    ``key_map`` (``rewrite_report_markers``)."""

    def rewrite(text: str) -> str:
        return rewrite_report_markers(text, key_map)

    return _block(
        digest.title, _strip_markers(selection.summary), digest,
        set(selection.read_indices), rewrite,
    )


def report_outline_text(
    digest_or_title: Union[ReportDigest, str], readable: bool,
) -> str:
    """The short form of the attached report for understanding, rewriting,
    expansion and planning: the title, plus the section titles when the turn
    may read the report. An unreadable report contributes its title only (D5):
    section titles were written from evidence this turn may not see."""
    if isinstance(digest_or_title, ReportDigest):
        title, sections = digest_or_title.title, digest_or_title.section_titles
    else:
        title, sections = str(digest_or_title or ""), ()
    head = f"Attached report: 《{title}》"
    if not readable:
        return head + " (it cannot be read in this turn; answer from the notebook evidence)"
    if not sections:
        return head
    return head + "\nReport sections: " + " | ".join(
        f"{index}. {section}" for index, section in enumerate(sections, start=1)
    )


# --- receipts and trace -----------------------------------------------------
@dataclass(frozen=True)
class ReportAdmissionCounts:
    """What the turn's report-evidence partition admitted (built where the
    partition is assembled, read by ``followup_receipt``).

    ``restored``: cited items in the prompt, including those reused from the
    turn's own retrieval. ``over_budget``: cited items read back but left out
    by the partition budget. Both are non-negative integers."""

    restored: int = 0
    over_budget: int = 0

    def __post_init__(self) -> None:
        for name in ("restored", "over_budget"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative int, got {value!r}")


def skipped_receipt(followup: ReportFollowup, reason_code: str) -> LinkedReportReceipt:
    """The receipt of a turn that did not read the report -- the only place a
    ``skipped`` receipt is built. It carries no sections and no counts (what
    was not read is not reported as read). The reason is the verdict's own
    wording when the code is the verdict's (it has the counts and names
    filled in), otherwise ``report_reason_text(reason_code)``."""
    verdict = followup.verdict
    if verdict.reason_code == reason_code and verdict.reason:
        reason = verdict.reason
    else:
        reason = report_reason_text(reason_code)
    return LinkedReportReceipt(
        report_id=followup.report_id,
        title=followup.title,
        status="skipped",
        reason_code=reason_code,
        reason=reason,
    )


def followup_receipt(
    followup: ReportFollowup,
    *,
    selection: SectionSelection,
    admission_counts: ReportAdmissionCounts,
    read: bool,
) -> LinkedReportReceipt:
    """The turn's receipt for the attached report.

    ``read`` is whether the report block entered the answer prompt. When it did
    not, the receipt is ``skipped_receipt`` with the verdict's reason, or
    ``no_synthesis`` when the verdict allowed reading. When it did, a section
    is ``read`` exactly when its body entered the prompt (a section without
    body text never does), ``admission_counts`` is what the evidence
    partition admitted, and ``evidence_gone`` adds the references whose item
    no longer existed for the verdict and for the restore. ``memory_used`` is
    the author's Memory the report was written from (D14).
    """
    verdict = followup.verdict
    if not read:
        return skipped_receipt(followup, verdict.reason_code or REASON_NO_SYNTHESIS)
    if not verdict.readable or followup.digest is None:
        raise ValueError("a report the verdict did not let this turn read cannot be read")
    if not isinstance(admission_counts, ReportAdmissionCounts):
        raise TypeError("admission_counts must be a ReportAdmissionCounts")
    digest = followup.digest
    read_indices = set(selection.read_indices)
    restored_gone = followup.restored.gone if followup.restored is not None else 0
    return LinkedReportReceipt(
        report_id=followup.report_id,
        title=followup.title,
        status="read",
        sections=[
            LinkedReportSection(
                index=section.index, title=section.title,
                read=bool(section.body) and section.index in read_indices,
            )
            for section in digest.sections
        ],
        summary_truncated=selection.summary_truncated,
        evidence_restored=admission_counts.restored,
        evidence_gone=verdict.gone + restored_gone,
        evidence_over_budget=admission_counts.over_budget,
        memory_used=list(digest.memory_used),
    )


def _report_name(title: str) -> str:
    return f"《{title}》" if title else ""


def _listed(numbers: Iterable[int]) -> str:
    return "、".join(str(number) for number in numbers)


def _evidence_note(receipt: LinkedReportReceipt) -> str:
    if not (receipt.evidence_restored or receipt.evidence_gone or receipt.evidence_over_budget):
        return ""
    note = f"；复原引用证据 {receipt.evidence_restored} 条"
    if receipt.evidence_gone:
        note += f"，{receipt.evidence_gone} 条已失效"
    if receipt.evidence_over_budget:
        note += f"，{receipt.evidence_over_budget} 条超出篇幅未读入"
    return note


def _read_summary(receipt: LinkedReportReceipt, blank: Collection[int]) -> str:
    """What a reading turn read. ``blank`` are the sections without body text:
    never read, never described as held back by the budget."""
    name = _report_name(receipt.title)
    total = len(receipt.sections)
    read = [section.index for section in receipt.sections if section.read]
    empty = sorted(section.index for section in receipt.sections if section.index in blank)
    dropped = [
        section.index for section in receipt.sections
        if not section.read and section.index not in blank
    ]
    # Concatenated rather than interpolated wherever there is wording, so the
    # UI-vocabulary guard reads it (it drops interpolations).
    blank_note = "第 " + _listed(empty) + " 节没有单独的正文"
    if not receipt.summary_truncated and not dropped:
        whole = f"已读取关联报告{name}全文"
        return whole + "（" + blank_note + "）" if empty else whole
    opening = f"已读取关联报告{name}：" + (
        "执行摘要（已截断）" if receipt.summary_truncated else "执行摘要"
    )
    if not total:
        return opening
    if not dropped and not empty:
        return opening + f"与全部 {total} 节"
    if read:
        part = "与第 " + _listed(read) + " 节"
    elif dropped:
        part = "与各节标题"
    else:
        part = ""
    note = f"共 {total} 节"
    if dropped and read:
        note += "，按与问题的相关度选取"
    elif dropped:
        note += "，正文超出篇幅未读入"
    if empty:
        note += "，" + blank_note
    return opening + part + "（" + note + "）"


def report_trace_step(
    receipt: LinkedReportReceipt, *, digest: Optional[ReportDigest],
) -> TraceStep:
    """The reasoning trace's ``report`` step: what the turn read of the
    attached report and how many cited items it read back, or why it read
    nothing (the receipt's own reason clause). ``digest`` is the turn's
    ``ReportFollowup.digest`` (None when the report was not read): it tells
    a section without body text from one the budget left out, which the
    receipt's ``read`` flags alone cannot."""
    if receipt.status == "read":
        blank = {
            section.index for section in (digest.sections if digest is not None else ())
            if not section.body
        }
        summary = _read_summary(receipt, blank) + _evidence_note(receipt)
    else:
        clause = receipt.reason.removeprefix(REASON_PREFIX)
        summary = f"未读取关联报告{_report_name(receipt.title)}：{clause}"
    detail: dict[str, Any] = {
        "report_id": receipt.report_id,
        "status": receipt.status,
        "sections_total": len(receipt.sections),
        "sections_read": [section.index for section in receipt.sections if section.read],
        "summary_truncated": receipt.summary_truncated,
        "evidence_restored": receipt.evidence_restored,
        "evidence_gone": receipt.evidence_gone,
        "evidence_over_budget": receipt.evidence_over_budget,
    }
    if receipt.reason_code:
        detail["reason_code"] = receipt.reason_code
    return TraceStep(step_type="report", summary=summary, detail=detail)


# --- the run's attached report ------------------------------------------------
@dataclass(frozen=True)
class ReportFollowupReaders:
    """The store reads ``AskService`` judges an attached report with (the
    runtime binds them; ``None`` on the service = the feature is not wired).

    ``read_report(notebook_id, report_id)`` is ``ReportStore.get_report`` (by
    notebook only, ``KeyError`` when absent -- ``load_linked_report`` applies
    the owner and status gate). ``source_owners`` / ``object_owners`` take a
    list of ids and answer ``{id: notebook_id}`` for the rows that still exist,
    hidden projections included, in ONE statement each.
    ``participant_notebook_ids(notebook_id, viewer_id=...)`` is the libraries
    the named viewer may use with the notebook (the same read the ceiling's
    participant reader makes, its viewer passed explicitly)."""

    read_report: Callable[[str, str], Mapping[str, Any]]
    source_owners: Callable[[Sequence[str]], Mapping[str, str]]
    object_owners: Callable[[Sequence[str]], Mapping[str, str]]
    participant_notebook_ids: Callable[..., Collection[str]]


#: The engines that read an attached report: the built-in ones. An extension
#: engine (``ask.engine``, plan D7) is handed no history and no intent, and
#: has no way to receive a report either.
_REPORT_READING_HANDLERS = frozenset(mode.handler for mode in ASK_MODES.values())


def report_mode_supported(mode: Any) -> bool:
    """Whether the engine a resolved Ask mode (``ask_modes.resolve_mode``)
    dispatches to reads an attached report. False for an extension engine:
    that turn answers as usual and its receipt says the report was not read
    (``engine_unsupported``), decided before anything is read."""
    return getattr(mode, "handler", None) in _REPORT_READING_HANDLERS


def report_restore_request(
    digest: ReportDigest,
    *,
    source_owners: Mapping[str, str],
    object_owners: Mapping[str, str],
) -> tuple[CitedReportReference, ...]:
    """The references a readable turn asks the retrieval layer to read back
    (``AskCandidatePort.restore_report_references``), in report order.

    Left out: the references whose item the verdict already counted gone (no
    owner any more -- restoring them would count them gone a second time) and
    the types the restore skips (``RESTORE_SKIPPED_OBJECT_TYPES``). Each names
    the library that owns its item now, as the verdict resolved it; a Memory
    reference names none. The request is not an authority: the restore judges
    every one again under the run's ceiling."""
    cited: list[CitedReportReference] = []
    for reference in digest.references:
        object_type = _field(reference, "object_type")
        if object_type in RESTORE_SKIPPED_OBJECT_TYPES:
            continue
        library = ""
        if object_type != _MEMORY_TYPE:
            owner = report_reference_owner(
                reference, source_owners=source_owners, object_owners=object_owners,
            )
            if owner is None:
                continue
            library = owner
        cited.append(CitedReportReference(
            key=_field(reference, "key"),
            object_type=object_type,
            object_id=_field(reference, "object_id"),
            notebook_id=library,
            source_id=_field(reference, "source_id"),
            element_id=_field(reference, "element_id"),
        ))
    return tuple(cited)


def followup_outline(followup: Optional[ReportFollowup]) -> str:
    """The attached report as the understanding, rewriting, expansion and
    planning steps see it (``report_outline_text``): title and section titles
    when the turn may read the report, the title alone otherwise (D5 -- the
    section titles were written from evidence the turn may not see). "" when
    the request attached no report."""
    if followup is None:
        return ""
    readable = followup.verdict.readable and followup.digest is not None
    return report_outline_text(
        followup.digest if readable else followup.title, readable,
    )


_CURRENT_REPORT_FOLLOWUP: ContextVar[Optional[ReportFollowup]] = ContextVar(
    "current_report_followup", default=None
)


@contextmanager
def report_followup_context(followup: Optional[ReportFollowup]) -> Iterator[None]:
    """Carry the run's ``ReportFollowup`` from the engine entry to the engines
    and to the answer's persistence seam (plan D11; the same shape as the
    display-only retrieval-scope receipt's context in ``source_scope``). Entered by
    ``AskService._engine_scope`` alone -- with ``None`` too, so a run without
    a report never sees an outer run's. ``background_jobs.submit`` copies the
    caller's context, so a worker thread of the run reads the same value."""
    token = _CURRENT_REPORT_FOLLOWUP.set(followup)
    try:
        yield
    finally:
        _CURRENT_REPORT_FOLLOWUP.reset(token)


def current_report_followup() -> Optional[ReportFollowup]:
    """The run's attached report (read or why not), or None when its request
    attached none, it is a global run, or no engine entry installed one."""
    return _CURRENT_REPORT_FOLLOWUP.get()


__all__ = [
    "REPORT_BLOCK_HEADER",
    "ReportAdmissionCounts",
    "ReportFollowupReaders",
    "ReportScope",
    "SUMMARY_TRUNCATED_SUFFIX",
    "current_report_followup",
    "followup_outline",
    "followup_receipt",
    "load_linked_report",
    "render_report_block",
    "report_digest",
    "report_followup_context",
    "report_link_verdict",
    "report_meta_digest",
    "report_mode_supported",
    "report_outline_text",
    "report_owner_ids",
    "report_reason_text",
    "report_reference_owner",
    "report_restore_request",
    "report_trace_step",
    "rewrite_report_markers",
    "select_report_sections",
    "skipped_receipt",
    "unread_verdict",
]
