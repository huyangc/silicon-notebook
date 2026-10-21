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
* ``report_digest`` -- the stored report reduced to title, executive summary
  and sections, parsed from ``content_md`` (the only body whose ``[kN]``
  markers pair with the stored ``references``);
* ``report_link_verdict`` -- may this turn read the report under the ceiling
  it runs with; one implementation for the run, the reasoning intent preview
  and the pre-send check;
* ``select_report_sections`` / ``render_report_block`` /
  ``rewrite_report_markers`` / ``report_outline_text`` -- what of the report
  enters the prompts, bounded by ``ask_report_context_max_chars``;
* ``followup_receipt`` / ``skipped_receipt`` / ``report_trace_step`` -- what
  the turn tells the reader it did with the report.
"""
from __future__ import annotations

import re
import unicodedata
from types import MappingProxyType
from typing import Any, Callable, Collection, Iterable, Mapping, Optional, Protocol, Union

from app.domain.report_followup import (
    LinkedReportNotFound,
    LinkedReportNotReady,
    REASON_CLAUSES,
    REASON_CLAUSE_MEMORY_CLOSED_CHANNEL,
    REASON_CLAUSE_MEMORY_CLOSED_NARROWED,
    REASON_ENGINE_UNSUPPORTED,
    REASON_LIBRARY_UNAVAILABLE,
    REASON_MEMORY_CLOSED,
    REASON_NO_SYNTHESIS,
    REASON_PREFIX,
    REASON_SCOPE_LIBRARIES,
    REASON_SCOPE_SOURCES,
    ReportDigest,
    ReportFollowup,
    ReportLinkVerdict,
    ReportSection,
    SectionSelection,
)
from app.models.ask import LinkedReportReceipt, LinkedReportSection, TraceStep
from app.services.citation_markers import LOOSE_MARKER_RE, marker_keys
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


def _keys_in(text: str) -> tuple[str, ...]:
    """Report reference keys of ``text``'s markers, in order of first appearance."""
    seen: dict[str, None] = {}
    for match in LOOSE_MARKER_RE.finditer(text or ""):
        for key in marker_keys(match.group(0)):
            seen.setdefault(key, None)
    return tuple(seen)


def _split_body(content_md: str, titles: tuple[str, ...]) -> tuple[str, list[str]]:
    """``(executive summary, one body per title)`` of a stored ``content_md``.

    Top-level ``## `` headings outside code fences drive the split. Before the
    first section: ``## 执行摘要`` opens the summary, ``## 资料基础`` a block that
    is dropped, the ``# `` title line is dropped. A heading that matches a
    title still ahead (``_heading_key``) opens that section -- titles it skips
    keep an empty body. A heading that matches none is the model's own: it
    stays in the current section's body, or -- when no section is open yet --
    opens the next one, since everything after the source-basis block is
    section text. The references list and the tail notes are never read.
    """
    lines = (content_md or "").splitlines()
    fenced = _fenced(lines)
    keys = [_heading_key(title) for title in titles]
    summary: list[str] = []
    bodies: list[list[str]] = [[] for _ in titles]
    target: Optional[list[str]] = None
    next_section = 0
    opened = seen_summary = False
    for index, line in enumerate(lines[:_body_end(lines, fenced)]):
        if not fenced[index] and line.startswith(_TOP_HEADING):
            heading = line[len(_TOP_HEADING):].strip()
            if not opened and not seen_summary and heading == _SUMMARY_HEADING:
                seen_summary, target = True, summary
                continue
            if not opened and heading == _SOURCE_BASIS_HEADING:
                target = None
                continue
            wanted = _heading_key(heading)
            found = next(
                (j for j in range(next_section, len(keys)) if keys[j] and keys[j] == wanted),
                None,
            )
            if found is None and not opened:
                found = next_section if next_section < len(titles) else None
                if found is None:
                    target = None
                    continue
            if found is not None:
                opened, next_section, target = True, found + 1, bodies[found]
                continue
        if target is not None:
            target.append(line)
    return "\n".join(summary).strip(), ["\n".join(body).strip() for body in bodies]


def report_digest(row: Mapping[str, Any]) -> ReportDigest:
    """A stored report reduced to what a follow-up question reads.

    Total: any malformed field reads as empty, never raises. ``title`` is
    ``reports.question`` whole; ``sections`` follow the stored section list
    (``row["sections"]``), each with its body from ``content_md`` (empty when
    its heading cannot be found there; a failed section's body is the stored
    neutral failure note). ``references`` and ``memory_used`` are copied whole:
    the verdict judges the entire report, not the part a turn reads.
    """
    stored_sections = row.get("sections")
    titles = tuple(
        str(section.get("title") or "") if isinstance(section, Mapping) else ""
        for section in (stored_sections if isinstance(stored_sections, (list, tuple)) else ())
    )
    content = row.get("content_md")
    summary, bodies = _split_body(content if isinstance(content, str) else "", titles)
    stored_references = row.get("references")
    references = tuple(
        MappingProxyType(dict(reference))
        for reference in (
            stored_references if isinstance(stored_references, (list, tuple)) else ()
        )
        if isinstance(reference, Mapping)
    )
    stored_memory = row.get("memory_used")
    memory_used = tuple(
        item for item in (stored_memory if isinstance(stored_memory, (list, tuple)) else ())
        if isinstance(item, str) and item
    )
    return ReportDigest(
        title=str(row.get("question") or ""),
        summary=summary,
        sections=tuple(
            ReportSection(index=index, title=title, body=body, keys=_keys_in(body))
            for index, (title, body) in enumerate(zip(titles, bodies), start=1)
        ),
        references=references,
        memory_used=memory_used,
        summary_keys=_keys_in(summary),
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
# turn could read back. It counts as gone, never as a conflict.
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
    (never a conflict); a withheld Memory projection source -> uses Memory; a
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
        if _field(reference, "object_type") == _MEMORY_TYPE:
            uses_memory = True
            continue
        library = report_reference_owner(
            reference, source_owners=source_owners, object_owners=object_owners
        )
        if library is None:
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
    in this turn's prompt. Each marker (``[k1]``, ``[k1, k3]``, ``【k2】``,
    ``(k4)`` ...; ``citation_markers.LOOSE_MARKER_RE``, so a marker with inner
    spaces is rewritten too instead of surviving as a stray key) keeps only its
    mapped keys, written ``[kA, kB]``; a marker none of whose keys is mapped
    is removed whole. A report key never reaches the prompt unmapped: it would
    name an unrelated item of this turn."""

    def rewrite(match: re.Match[str]) -> str:
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
    return LOOSE_MARKER_RE.sub(
        lambda match: "[" + ", ".join(
            _WIDEST_TURN_KEY for _key in marker_keys(match.group(0))
        ) + "]",
        text or "",
    )


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
    """The longest prefix of ``summary`` whose widened length fits ``room``,
    ending at the last sentence end that fits; a hard cut when none does,
    never inside a marker (half a marker would read as a stray key)."""
    if room <= 0:
        return ""
    best = 0
    for index, char in enumerate(summary):
        if char not in _SENTENCE_ENDS:
            continue
        if len(_widened(summary[:index + 1])) > room:
            break
        best = index + 1
    if best:
        return summary[:best]
    cut = min(len(summary), room)
    while cut > 0:
        inside = next(
            (m.start() for m in LOOSE_MARKER_RE.finditer(summary) if m.start() < cut < m.end()),
            None,
        )
        if inside is not None:
            cut = inside
        elif len(_widened(summary[:cut])) <= room:
            break
        else:
            cut -= 1
    return summary[:cut].rstrip()


def _section_score(basis: KeywordBasis, section: ReportSection) -> float:
    text = f"{section.title}\n{section.body}"
    return basis.coverage(set(_tokens(text)), text)


def select_report_sections(
    digest: ReportDigest, query: str, budget_chars: int,
) -> SectionSelection:
    """Which parts of the report enter this turn's prompt (D12, §2.6).

    The skeleton -- header, title, executive summary and every section
    heading -- always enters. When the skeleton alone exceeds ``budget_chars``
    the summary is clipped at its last sentence end that fits (a hard cut when
    none does) and ``SUMMARY_TRUNCATED_SUFFIX`` is appended here, the only
    place it is added. Section bodies then fill what is left WHOLE (never half
    a section): all of them when they fit, otherwise by lexical relevance to
    ``query`` (``retrieval.keyword_basis(query).coverage`` over title and
    body; ties by section order), skipping a body that does not fit and trying
    the next. A body shorter than the placeholder it replaces is always taken;
    a section without a body is always "read" (nothing of it is held back).

    Lengths are measured with every marker at its widest possible rewrite, so
    the rendered block stays within ``budget_chars`` whatever keys the items
    take; only section headings -- user-confirmed outline titles, which are
    never cut -- can push a block past it. ``query`` is the turn's retrieval
    query (chunk) or research question (reasoning).
    """
    always = {section.index for section in digest.sections if not section.body}
    summary, truncated = digest.summary, False
    skeleton = len(_block(digest.title, _widened(summary), digest, (), _widened))
    if summary and skeleton > budget_chars:
        # Everything but the summary's own text: the block with a one-char summary.
        overhead = len(_block(digest.title, "x", digest, (), _widened)) - 1
        summary = _clip_summary(
            digest.summary, budget_chars - overhead - len(SUMMARY_TRUNCATED_SUFFIX)
        ) + SUMMARY_TRUNCATED_SUFFIX
        truncated = True
        skeleton = len(_block(digest.title, _widened(summary), digest, (), _widened))
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
    keys: dict[str, None] = dict.fromkeys(_keys_in(summary))
    for section in digest.sections:
        if section.index in chosen:
            for key in section.keys:
                keys.setdefault(key, None)
    return SectionSelection(
        read_indices=tuple(sorted(chosen | always)),
        summary=summary,
        summary_truncated=truncated,
        keys=tuple(keys),
    )


def render_report_block(
    digest: ReportDigest, selection: SectionSelection, key_map: Mapping[str, str],
) -> str:
    """The attached-report block of the answer prompt (§2.6), sections in the
    report's order: a read section with its body, an unread one with a
    placeholder line. ``selection.summary`` is used as is (already clipped and
    marked by ``select_report_sections``); every marker is rewritten through
    ``key_map`` (``rewrite_report_markers``)."""

    def rewrite(text: str) -> str:
        return rewrite_report_markers(text, key_map)

    return _block(
        digest.title, rewrite(selection.summary), digest,
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
    admission_counts: Mapping[str, int],
    read: bool,
) -> LinkedReportReceipt:
    """The turn's receipt for the attached report.

    ``read`` is whether the report block entered the answer prompt. When it did
    not, the receipt is ``skipped_receipt`` with the verdict's reason, or
    ``no_synthesis`` when the verdict allowed reading. When it did,
    ``admission_counts`` is what the evidence partition admitted --
    ``"restored"`` (cited items in the prompt, including those reused from the
    turn's own retrieval) and ``"over_budget"`` (read back but left out by the
    partition budget); ``evidence_gone`` adds the references whose item no
    longer existed for the verdict and for the restore. ``memory_used`` is the
    author's Memory the report was written from (D14).
    """
    verdict = followup.verdict
    if not read:
        return skipped_receipt(followup, verdict.reason_code or REASON_NO_SYNTHESIS)
    if not verdict.readable or followup.digest is None:
        raise ValueError("a report the verdict did not let this turn read cannot be read")
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
                read=section.index in read_indices,
            )
            for section in digest.sections
        ],
        summary_truncated=selection.summary_truncated,
        evidence_restored=int(admission_counts.get("restored", 0)),
        evidence_gone=verdict.gone + restored_gone,
        evidence_over_budget=int(admission_counts.get("over_budget", 0)),
        memory_used=list(digest.memory_used),
    )


def _report_name(title: str) -> str:
    return f"《{title}》" if title else ""


def _evidence_note(receipt: LinkedReportReceipt) -> str:
    if not (receipt.evidence_restored or receipt.evidence_gone or receipt.evidence_over_budget):
        return ""
    note = f"；复原引用证据 {receipt.evidence_restored} 条"
    if receipt.evidence_gone:
        note += f"，{receipt.evidence_gone} 条已失效"
    if receipt.evidence_over_budget:
        note += f"，{receipt.evidence_over_budget} 条超出篇幅未读入"
    return note


def _read_summary(receipt: LinkedReportReceipt) -> str:
    name = _report_name(receipt.title)
    total = len(receipt.sections)
    numbers = [section.index for section in receipt.sections if section.read]
    if not receipt.summary_truncated and len(numbers) == total:
        return f"已读取关联报告{name}全文"
    # Concatenated rather than interpolated, so the UI-vocabulary guard reads
    # the opening words as part of the summary (it drops interpolations).
    opening = f"已读取关联报告{name}：" + (
        "执行摘要（已截断）" if receipt.summary_truncated else "执行摘要"
    )
    if not total:
        return opening
    if len(numbers) == total:
        return opening + f"与全部 {total} 节"
    if not numbers:
        return opening + f"与各节标题（共 {total} 节，正文超出篇幅未读入）"
    listed = "、".join(str(number) for number in numbers)
    return opening + f"与第 {listed} 节（共 {total} 节，按与问题的相关度选取）"


def report_trace_step(receipt: LinkedReportReceipt) -> TraceStep:
    """The reasoning trace's ``report`` step: what the turn read of the
    attached report and how many cited items it read back, or why it read
    nothing (the receipt's own reason clause)."""
    if receipt.status == "read":
        summary = _read_summary(receipt) + _evidence_note(receipt)
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


__all__ = [
    "REPORT_BLOCK_HEADER",
    "ReportScope",
    "SUMMARY_TRUNCATED_SUFFIX",
    "followup_receipt",
    "load_linked_report",
    "render_report_block",
    "report_digest",
    "report_link_verdict",
    "report_outline_text",
    "report_owner_ids",
    "report_reason_text",
    "report_reference_owner",
    "report_trace_step",
    "rewrite_report_markers",
    "select_report_sections",
    "skipped_receipt",
    "unread_verdict",
]
