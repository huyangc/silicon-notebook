"""Stable values of "ask a question about a finished deep report".

A question may name one finished report of the notebook (``report_id``). The
turn then reads that report's body as context that is *not* evidence, restores
the items the report cited from the notebook's current text, and answers from
those -- or, when the run's retrieval ceiling would not let it read what the
report was written from, it does not read the report at all and says why.

This module holds only the shared vocabulary of that feature, below the
services and the repositories that both use it:

* the reason codes and the Chinese clauses the receipt and the pre-send check
  show (one place, so the tag next to the input and the turn's receipt can
  never word the same fact differently);
* the two exceptions of the report gate;
* the frozen values that travel between the pure functions
  (``app.services.report_followup``), the retrieval-layer restore
  (``AskCandidatePort.restore_report_references``) and ``AskService``.

No behaviour lives here, and nothing here reads a database, a model, or the
retrieval ceiling.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Mapping, Optional

from app.domain.retrieval import RetrievedChunk, RetrievedElement, RetrievedKnowledge
from app.models.memory import MemoryHit


# --- reason codes -----------------------------------------------------------
# Stored on the turn's receipt (``LinkedReportReceipt.reason_code``) and returned
# by the pre-send check (``ReportLinkCheck.reason_code``). ``report_link_verdict``
# decides ``engine_unsupported`` first (from ``mode_supported``, before anything
# is read) and then ``library_unavailable``, ``scope_libraries``,
# ``scope_sources`` and ``memory_closed`` in that order; ``report_unavailable``
# (the report gate), ``read_failed`` and ``no_synthesis`` are decided by the
# caller around it.
REASON_LIBRARY_UNAVAILABLE = "library_unavailable"
REASON_SCOPE_LIBRARIES = "scope_libraries"
REASON_SCOPE_SOURCES = "scope_sources"
REASON_MEMORY_CLOSED = "memory_closed"
REASON_ENGINE_UNSUPPORTED = "engine_unsupported"
REASON_REPORT_UNAVAILABLE = "report_unavailable"
REASON_READ_FAILED = "read_failed"
REASON_NO_SYNTHESIS = "no_synthesis"

#: Every code above.
REASON_CODES = (
    REASON_LIBRARY_UNAVAILABLE,
    REASON_SCOPE_LIBRARIES,
    REASON_SCOPE_SOURCES,
    REASON_MEMORY_CLOSED,
    REASON_ENGINE_UNSUPPORTED,
    REASON_REPORT_UNAVAILABLE,
    REASON_READ_FAILED,
    REASON_NO_SYNTHESIS,
)

# --- reason text ------------------------------------------------------------
# ``reason`` = ``REASON_PREFIX`` + one clause. The wording states what happened
# and never guesses why. ``{n}`` is a count and ``{names}`` a run of library
# names written ``《A》《B》`` (the names the notebook mounts them under now);
# the callers fill them with ``str.format``.
REASON_PREFIX = "本轮不读取这份报告："

REASON_CLAUSES = MappingProxyType({
    REASON_LIBRARY_UNAVAILABLE: "它引用的 {n} 个参考库本轮无法使用",
    REASON_SCOPE_LIBRARIES: "它引用的参考库{names}没有勾选",
    REASON_SCOPE_SOURCES: "它引用的 {n} 份资料不在当前检索范围内",
    REASON_ENGINE_UNSUPPORTED: "当前回答方式不读取关联报告，请改用「通用问答」或「逐步推理」",
    REASON_REPORT_UNAVAILABLE: "这份报告已删除或不再是已完成状态",
    REASON_READ_FAILED: "读取报告时出错，请重试",
    REASON_NO_SYNTHESIS: "本轮没有调用回答模型",
})

#: ``memory_closed`` has two clauses because it has two causes the reader can
#: act on differently. The retrieval scope was narrowed, which closes the
#: personal-memory channel for the turn ...
REASON_CLAUSE_MEMORY_CLOSED_NARROWED = (
    "它用到了你的个人记忆，而收窄检索范围后本轮不读取个人记忆"
)
#: ... or the channel is closed for this caller altogether (for example an
#: external Agent token without the memory read grant).
REASON_CLAUSE_MEMORY_CLOSED_CHANNEL = (
    "它用到了个人记忆，而本次提问不能读取个人记忆"
)


# --- the report gate --------------------------------------------------------
class LinkedReportNotFound(Exception):
    """The named report does not exist, is not the caller's own, or belongs to
    another notebook. One exception for the three on purpose: the gate must not
    answer "does this id exist" for a report the caller cannot read."""


class LinkedReportNotReady(Exception):
    """The named report is the caller's own but is not ``done`` yet, so there is
    no finished body to follow up."""


# --- values -----------------------------------------------------------------
@dataclass(frozen=True)
class ReportSection:
    """One section of a report, aligned to the report's own section order.

    ``body`` is the section's markdown as it reads in the stored report (its
    ``[kN]`` markers still the report's own numbering); empty when the section
    heading could not be found in the stored body. ``keys`` are the report
    reference keys that appear in ``body``, in order of first appearance."""

    index: int  # 1-based
    title: str
    body: str = ""
    keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReportDigest:
    """A finished report reduced to what a follow-up question reads.

    ``title`` is ``reports.question`` (the question the author typed). The
    stored body is split into the executive ``summary`` and the ``sections``
    (no references list, no source-basis block). ``references`` are the stored
    reference rows (``key``, ``object_type``, ``object_id``, ``source_id``,
    ``element_id`` ...) and ``memory_used`` the ids of the author's personal
    memory the report was written from; both cover the WHOLE report, not just
    the sections a turn reads, because the summary is synthesised from all of
    them. ``summary_keys`` is ``ReportSection.keys`` for the summary."""

    title: str
    summary: str
    sections: tuple[ReportSection, ...]
    references: tuple[Mapping[str, Any], ...] = ()
    memory_used: tuple[str, ...] = ()
    summary_keys: tuple[str, ...] = ()

    @property
    def section_titles(self) -> tuple[str, ...]:
        return tuple(section.title for section in self.sections)


@dataclass(frozen=True)
class ReportLinkVerdict:
    """Whether a turn under the installed retrieval ceiling may read the report.

    ``readable`` is derived, not stored: the report is readable exactly when
    ``reason_code`` is empty, so a verdict can never say "readable" while naming
    a reason. ``gone`` counts cited items that no longer exist (never a
    conflict). ``unavailable_libs`` are cited reference libraries the turn
    cannot use at all, ``unchecked_libs`` cited libraries the turn has not
    ticked, ``out_of_scope_count`` cited sources of ticked libraries that the
    scope leaves out; ``uses_memory`` is whether the report was written from
    personal memory."""

    reason_code: str = ""
    reason: str = ""
    gone: int = 0
    unavailable_libs: frozenset[str] = frozenset()
    unchecked_libs: frozenset[str] = frozenset()
    out_of_scope_count: int = 0
    uses_memory: bool = False

    @property
    def readable(self) -> bool:
        return not self.reason_code


@dataclass(frozen=True)
class SectionSelection:
    """Which parts of the report enter one turn's prompt.

    ``read_indices`` are the 1-based sections whose body is included (headings
    of every section are always included). ``summary`` is the executive summary
    as it enters the block -- clipped at a sentence end when the heading
    skeleton alone overflowed the budget, with ``summary_truncated`` set.
    ``keys`` are the report reference keys that appear in the included text, in
    order of first appearance (summary first, then the sections in order)."""

    read_indices: tuple[int, ...] = ()
    summary: str = ""
    summary_truncated: bool = False
    keys: tuple[str, ...] = ()


#: The kinds of object a report reference is restored as -- a subset of
#: ``AskEvidenceKind``, named after the evidence kind each one renders as.
RESTORED_KIND_CHUNK = "chunk"
RESTORED_KIND_ELEMENT = "element"
RESTORED_KIND_KG = "kg"
RESTORED_KIND_MEMORY = "memory"
RestoredKind = Literal["chunk", "element", "kg", "memory"]
RESTORED_KINDS = (
    RESTORED_KIND_CHUNK,
    RESTORED_KIND_ELEMENT,
    RESTORED_KIND_KG,
    RESTORED_KIND_MEMORY,
)


@dataclass(frozen=True)
class CitedReportReference:
    """One stored report reference the retrieval layer is asked to read back
    (``AskCandidatePort.restore_report_references``).

    ``key`` is the report's own marker key (``"k3"``); ``object_type``,
    ``object_id``, ``source_id`` and ``element_id`` are the stored reference
    row's fields; ``notebook_id`` is the library that owns the cited item, as
    the verdict resolved it ("" = the notebook being asked). A REQUEST, not an
    authority: the restore judges every field again under the retrieval
    ceiling installed for the run, so a reference that names a library or a
    source the ceiling does not admit reads back nothing.

    How each ``object_type`` is read back: ``chunk`` as its passage,
    ``element`` as its element, ``relation`` as its evidence element (the
    stored ``element_id``, else the relation's first current evidence element),
    ``memory`` as the asker's own confirmed Memory entry, and any other graph
    type as the knowledge object ``object_id``. ``external`` and ``source``
    rows (and a row with no ``object_type``) name nothing that can be read
    back and count as gone."""

    key: str
    object_type: str
    object_id: str
    notebook_id: str = ""
    source_id: str = ""
    element_id: str = ""


@dataclass(frozen=True)
class RestoredReportEvidence:
    """The report's cited items, read back from the notebook's current text.

    Each tuple holds the retrieval layer's native objects, so the existing
    renderers and citation builders handle them unchanged. ``key_by_object``
    maps ``(kind, object_id)`` to the report reference keys that point at that
    object (several keys can name one object). The key names the RESTORED
    object, not the reference's own type: a chunk reference is
    ``("chunk", chunk_id)``, a KG object ``("kg", object_id)``, a memory entry
    ``("memory", memory_id)``, and a relation reference -- restored as its
    evidence element -- ``("element", element_id)``. ``kind`` is one of
    ``RESTORED_KINDS`` (anything else raises ``ValueError``), and the mapping
    is made read-only on construction. ``gone`` counts references whose item no
    longer exists or lies outside the ceiling the read ran under."""

    chunks: tuple[RetrievedChunk, ...] = ()
    elements: tuple[RetrievedElement, ...] = ()
    knowledge: tuple[RetrievedKnowledge, ...] = ()
    memory: tuple[MemoryHit, ...] = ()
    key_by_object: Mapping[tuple[RestoredKind, str], tuple[str, ...]] = field(
        default_factory=dict
    )
    gone: int = 0

    def __post_init__(self) -> None:
        frozen: dict[tuple[RestoredKind, str], tuple[str, ...]] = {}
        for (kind, object_id), keys in self.key_by_object.items():
            if kind not in RESTORED_KINDS:
                raise ValueError(f"unknown restored kind {kind!r}")
            frozen[(kind, object_id)] = tuple(keys)
        object.__setattr__(self, "key_by_object", MappingProxyType(frozen))


@dataclass(frozen=True)
class ReportFollowup:
    """What one run knows about the report its request attached.

    ``digest`` is None when the report could not be read at all (gone, not
    finished, read error, extension engine) and ``restored`` is None unless the
    verdict is readable and the references were read back."""

    report_id: str
    title: str
    digest: Optional[ReportDigest]
    verdict: ReportLinkVerdict
    restored: Optional[RestoredReportEvidence] = None


__all__ = [
    "CitedReportReference",
    "LinkedReportNotFound",
    "LinkedReportNotReady",
    "REASON_CLAUSES",
    "REASON_CLAUSE_MEMORY_CLOSED_CHANNEL",
    "REASON_CLAUSE_MEMORY_CLOSED_NARROWED",
    "REASON_CODES",
    "REASON_ENGINE_UNSUPPORTED",
    "REASON_LIBRARY_UNAVAILABLE",
    "REASON_MEMORY_CLOSED",
    "REASON_NO_SYNTHESIS",
    "REASON_PREFIX",
    "REASON_READ_FAILED",
    "REASON_REPORT_UNAVAILABLE",
    "REASON_SCOPE_LIBRARIES",
    "REASON_SCOPE_SOURCES",
    "RESTORED_KINDS",
    "RESTORED_KIND_CHUNK",
    "RESTORED_KIND_ELEMENT",
    "RESTORED_KIND_KG",
    "RESTORED_KIND_MEMORY",
    "ReportDigest",
    "ReportFollowup",
    "ReportLinkVerdict",
    "ReportSection",
    "RestoredKind",
    "RestoredReportEvidence",
    "SectionSelection",
]
