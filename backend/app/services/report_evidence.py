"""The attached deep report in one turn's synthesis: its evidence partition
and the turn's state around it (plan 2026-10-21 D1, §2.7, T7/T8).

A turn that reads an attached report puts two things into its answer prompt:
the report block (``report_followup.render_report_block``, not evidence) and
the items the read text cites, read back from the notebook's current text, as
ordinary evidence of their own native kinds. This module assembles that
evidence partition -- which items, in which order, under which keys, what was
cut -- from renderers the caller hands in, so the same rules serve the chunk
engine and the reasoning engine; ``AskService`` supplies the renderers (they
need its stores) and builds the citation cards.

* ``assemble_report_evidence`` -- the partition (``ReportEvidencePartition``);
* ``ReportTurn`` -- the report as one turn's synthesis uses it: the selected
  sections, the partition the assembler recorded, whether the block entered an
  answer prompt, and the receipt;
* ``report_readable`` / ``with_report_selected`` / ``citation_identity`` --
  the small predicates the engines share; ``ReportCards`` is what the citation
  step returns.

No database and no model here: the renderers are the caller's.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from itertools import chain, groupby
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Optional

from app.domain.report_followup import (
    RESTORED_KIND_CHUNK,
    RESTORED_KIND_ELEMENT,
    RESTORED_KIND_KG,
    RESTORED_KIND_MEMORY,
    RESTORED_KINDS,
    ReportFollowup,
    RestoredReportEvidence,
    SectionSelection,
)
from app.models.ask import Citation, LinkedReportReceipt
from app.services.ask_evidence import kind_for_key
from app.services.context_spans import joined
from app.services.evidence_context import CitationLookups, KnowledgeContextOutcome
from app.services.report_followup import (
    ReportAdmissionCounts,
    followup_receipt,
    render_report_block,
)
from app.services.source_element_selection import source_chunk_content_key

#: The partition's one heading in the synthesis context.
REPORT_EVIDENCE_HEADING = "Evidence the attached report cited"


def report_readable(followup: Optional[ReportFollowup]) -> bool:
    """Whether this run reads its attached report into the synthesis: the
    verdict lets it AND the run itself parsed the body and read the cited
    items back (``AskService._read_report_followup``; a followup that carries
    only the meta digest is never rendered). Reads nothing but the value."""
    return (
        followup is not None and followup.verdict.readable
        and followup.digest is not None and followup.restored is not None
    )


class ReportEvidencePartition(NamedTuple):
    """``assemble_report_evidence``'s result: the synthesis context with the
    attached report's evidence partition appended.

    ``key_map`` maps each report key of the read text whose item is in the
    prompt to the key that item has there -- its own partition key, the key
    of the same object the turn retrieved itself, or the key of the object it
    was folded into (an identical passage, a knowledge object of the same
    canonical cluster); ``admitted_keys`` are those turn keys.
    ``not_loaded_keys`` are the report keys whose item was read back but cut
    by the partition budget or its native key band: the report block shows
    them as ``REPORT_EVIDENCE_NOT_LOADED`` rather than removing them.
    ``counts`` is what the receipt reports, per report key: ``restored``
    (mapped) and ``over_budget`` (not loaded). A key whose item is gone,
    outside the ceiling, of a skipped type, or withheld by the knowledge
    renderer for what it is (``KnowledgeContextOutcome.withheld``) is in
    none of them. ``selected_by_kind`` counts the objects per evidence kind
    the partition tried to render (not those it reused); ``lookups`` are the
    batch reads its passage rendering made, for the citation step."""

    context_block: str
    id_map: dict
    key_map: Mapping[str, str]
    admitted_keys: frozenset
    counts: ReportAdmissionCounts
    selected_by_kind: Mapping[str, int]
    not_loaded_keys: frozenset
    lookups: CitationLookups


def _restored_values(restored: RestoredReportEvidence) -> dict[str, dict[str, Any]]:
    return {
        RESTORED_KIND_CHUNK: {item.chunk_id: item for item in restored.chunks},
        RESTORED_KIND_ELEMENT: {item.element_id: item for item in restored.elements},
        RESTORED_KIND_KG: {item.object_id: item for item in restored.knowledge},
        RESTORED_KIND_MEMORY: {item.memory_id: item for item in restored.memory},
    }


def _band_start(keys: Iterable[str], low: int, high: int) -> int:
    """The first free key number of the band ``[low, high]``: one past the
    highest key already taken in it (``AskService._external_key_offset``'s
    rule), ``low`` when none is."""
    taken = [
        int(match.group(1)) for key in keys
        if (match := re.fullmatch(r"k(\d+)", str(key))) is not None
        and low <= int(match.group(1)) <= high
    ]
    return max(taken, default=low - 1) + 1


def assemble_report_evidence(
    context_block: str,
    id_map: dict,
    followup: ReportFollowup,
    selection: SectionSelection,
    *,
    budget_chars: int,
    bands: Mapping[str, tuple[int, int]],
    render: Callable[..., tuple[str, dict]],
    append: Callable[..., tuple[str, dict]],
    key_offset: int = 0,
) -> ReportEvidencePartition:
    """Append the attached report's evidence partition (plan D1, §2.7): the
    items the report cites in the text this turn reads, as the notebook holds
    them now, rendered as ordinary evidence.

    Order: ``selection.evidence_keys`` -- the read sections by relevance to
    the turn's query, each section's keys in first-appearance order -- so
    when the budget runs out it is the evidence of the sections the question
    is NOT about that is cut. Each wanted object (``followup.restored``, read
    back under the run's ceiling by the retrieval layer):

    * already in ``id_map`` as the same kind and object (``kind_for_key``
      over what the turn retrieved itself) -> not rendered again; its report
      keys map to that key;
    * a passage whose content repeats an earlier wanted passage of the same
      source -> shares that passage's key;
    * otherwise rendered, in order, by ``render(kind, items, id_offset=,
      budget_chars=, outcome=, lookups=)`` -- the caller's native renderer
      for the kind -- one call per run of consecutive objects of one kind, in
      the kind's native band (``bands[kind]`` plus ``key_offset``, continuing
      after the band's highest key already taken). A knowledge object the
      renderer folds into another of its canonical cluster takes that one's
      key; one it withholds for what it is (``KnowledgeContextOutcome``) is
      neither loaded nor counted.

    ``budget_chars`` (heading included) is the partition's own, apart from
    the turn's retrieval budget; the partition goes after everything the
    caller budgeted, clipped and refined, and before external evidence. An
    object the budget or a full band leaves out is ``not loaded``. One
    heading (``REPORT_EVIDENCE_HEADING``) through ``append`` -- the caller's
    ``_bounded_context_append`` -- so the retrieval-only output's spans record
    it as glue. Nothing wanted or nothing fitting: the context and ``id_map``
    come back as they were.

    Deterministic for its inputs: an answer helper that is retried assembles
    (and reads) the same partition again rather than caching it."""
    restored = followup.restored or RestoredReportEvidence()
    values = _restored_values(restored)
    object_of = {
        key: obj for obj, keys in restored.key_by_object.items() for key in keys
    }
    wanted: dict[tuple[str, str], list[str]] = {}
    for key in selection.evidence_keys:
        obj = object_of.get(key)
        if obj is not None and obj[1] in values.get(obj[0], {}):
            wanted.setdefault(obj, []).append(key)
    retrieved: dict[tuple[str, str], str] = {}
    for key, entry in id_map.items():
        kind = kind_for_key(key, id_map)
        if kind in RESTORED_KINDS:
            retrieved.setdefault((kind, str(entry.get("object_id") or "")), key)
    key_map: dict[str, str] = {}
    alias: dict[tuple[str, str], tuple[str, str]] = {}
    contents: dict[tuple[str, str], tuple[str, str]] = {}
    queue: list[tuple[str, str]] = []
    for obj, keys in wanted.items():
        if obj in retrieved:
            key_map.update(dict.fromkeys(keys, retrieved[obj]))
            continue
        if obj[0] == RESTORED_KIND_CHUNK:
            first = contents.setdefault(
                source_chunk_content_key(values[obj[0]][obj[1]]), obj,
            )
            if first != obj:
                alias[obj] = first
                continue
        queue.append(obj)

    prefix = ("\n\n" if context_block else "") + f"[{REPORT_EVIDENCE_HEADING}]\n"
    room = int(budget_chars) - len(prefix)
    lookups = CitationLookups()
    parts: list[str] = []
    block_map: dict = {}
    rendered: dict[tuple[str, str], str] = {}
    folded: dict[tuple[str, str], str] = {}
    withheld: set[tuple[str, str]] = set()
    used = 0
    for kind, run in groupby(queue, key=lambda obj: obj[0]):
        low, high = bands[kind]
        start = _band_start(chain(id_map, block_map), low + key_offset, high + key_offset)
        objects = list(run)[:max(0, high + key_offset - start + 1)]
        remaining = room - used - (1 if parts else 0)
        if not objects or remaining <= 0:
            continue
        outcome = KnowledgeContextOutcome()
        block, kind_map = render(
            kind, [values[kind][object_id] for _kind, object_id in objects],
            id_offset=start - 1, budget_chars=remaining, outcome=outcome,
            lookups=lookups,
        )
        folded.update(((kind, object_id), key) for object_id, key in outcome.folded.items())
        withheld.update((kind, object_id) for object_id in outcome.withheld)
        if not kind_map:
            continue
        used += (1 if parts else 0) + len(block)
        parts.append(block)
        block_map.update(kind_map)
        for key, entry in kind_map.items():
            rendered[(kind, str(entry.get("object_id") or ""))] = key

    merged = id_map
    if parts:
        partition = joined(parts, "\n")
        context_block, merged = append(
            context_block, id_map, partition, block_map,
            # The renderers already spent the partition budget; this bound
            # only keeps the append from cutting the partition again.
            budget_chars=len(context_block) + len(prefix) + len(partition),
            heading=REPORT_EVIDENCE_HEADING,
        )
    not_loaded: list[str] = []
    for obj, keys in wanted.items():
        if keys[0] in key_map:
            continue
        representative = alias.get(obj, obj)
        target = rendered.get(representative) or folded.get(representative)
        if target is not None and target in merged:
            key_map.update(dict.fromkeys(keys, target))
        elif representative not in withheld:
            not_loaded.extend(keys)
    return ReportEvidencePartition(
        context_block=context_block,
        id_map=merged,
        key_map=key_map,
        admitted_keys=frozenset(key_map.values()),
        counts=ReportAdmissionCounts(restored=len(key_map), over_budget=len(not_loaded)),
        selected_by_kind=dict(Counter(kind for kind, _object_id in queue)),
        not_loaded_keys=frozenset(not_loaded),
        lookups=lookups,
    )


@dataclass
class ReportTurn:
    """The attached report as one turn's synthesis uses it (plan T7/T8).

    Built once the turn's query is known (``AskService._report_prompt_parts``
    selects the sections for it); the context assembler records the evidence
    partition it appended (``admit``) and the answer helper renders the report
    block from that partition (``enter_prompt``), which is also what makes the
    receipt ``read`` (``receipt``). Mutable on purpose: the answer helpers run
    inside ``_answer_with_retry`` and hand back only the answer, so the
    assembly's outcome travels on this object -- each attempt assembles the
    same partition and overwrites the same fields."""

    followup: ReportFollowup
    selection: SectionSelection
    partition: Optional[ReportEvidencePartition] = None
    prompted: bool = False

    def admit(self, partition: ReportEvidencePartition) -> None:
        self.partition = partition

    @property
    def key_map(self) -> Mapping[str, str]:
        return self.partition.key_map if self.partition is not None else {}

    @property
    def admitted_keys(self) -> frozenset:
        return self.partition.admitted_keys if self.partition is not None else frozenset()

    @property
    def not_loaded_keys(self) -> frozenset:
        return self.partition.not_loaded_keys if self.partition is not None else frozenset()

    def enter_prompt(self) -> str:
        """The report block for the answer prompt being built: markers
        rewritten through the partition's ``key_map``, cut ones shown as not
        loaded. Records that the block entered an answer prompt."""
        self.prompted = True
        return render_report_block(
            self.followup.digest, self.selection, self.key_map,
            not_loaded=self.not_loaded_keys,
        )

    def receipt(self, *, read: Optional[bool] = None) -> LinkedReportReceipt:
        """The turn's receipt: ``read`` when the block entered the answer
        prompt (``read`` overrides it -- the retrieval-only output, which
        stands in for that prompt, passes True), else ``skipped`` /
        ``no_synthesis`` (``followup_receipt``)."""
        counts = (
            self.partition.counts if self.partition is not None
            else ReportAdmissionCounts()
        )
        return followup_receipt(
            self.followup, selection=self.selection, admission_counts=counts,
            read=self.prompted if read is None else read,
        )


class ReportCards(NamedTuple):
    """``AskService._report_citations_and_keys``'s result: the citation cards
    of the restored items the answer bound (deduplicated against the turn's
    own cards), the bound restored passages (whose element ids join the
    anchor image candidates) and the bound keys for ``classify_evidence``'s
    deterministic door. Empty without a report."""

    citations: tuple = ()
    chunks: tuple = ()
    exact_keys: frozenset = frozenset()


def with_report_selected(
    selected: Mapping[str, int], report: Optional[ReportTurn],
) -> dict:
    """``selected`` (``_recount_selected``'s per-kind input) plus the objects
    the report partition tried to render, each under its own kind."""
    merged = dict(selected)
    if report is not None and report.partition is not None:
        for kind, number in report.partition.selected_by_kind.items():
            merged[kind] = merged.get(kind, 0) + int(number)
    return merged


def citation_identity(citation: Citation) -> tuple:
    """What makes two citation cards the same card: the Memory entry, else
    the (source, element) pair; a snapshot card naming neither is told apart
    by its label and quote."""
    if citation.memory_id:
        return ("memory", citation.memory_id)
    if citation.source_id or citation.element_id:
        return ("element", citation.source_id, citation.element_id)
    return ("snapshot", citation.label, citation.quoted_span)


__all__ = [
    "REPORT_EVIDENCE_HEADING",
    "ReportCards",
    "ReportEvidencePartition",
    "ReportTurn",
    "assemble_report_evidence",
    "citation_identity",
    "report_readable",
    "with_report_selected",
]
