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
  step returns;
* ``scrub_report_marks`` -- the answer-side guarantee that the block's
  not-loaded mark never reaches an answer.

No database and no model here: the renderers are the caller's.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Collection, Iterable, Mapping, NamedTuple, Optional

from app.domain.report_followup import (
    REPORT_EVIDENCE_NOT_LOADED,
    RESTORED_KIND_CHUNK,
    RESTORED_KIND_ELEMENT,
    RESTORED_KIND_KG,
    RESTORED_KIND_MEMORY,
    RESTORED_KINDS,
    ReportFollowup,
    RestoredReportEvidence,
    SectionSelection,
)
from app.domain.retrieval import KnowledgeContextOutcome
from app.models.ask import Citation, LinkedReportReceipt
from app.services.ask_evidence import kind_for_key
from app.services.context_spans import entry_lines, joined, recorded_spans, recording_spans
from app.services.evidence_context import CitationLookups
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


#: The least content a CUT evidence entry must keep to enter the partition:
#: characters of the item's own text after the renderer's head (the key and
#: the labels, titles and location it writes before the text --
#: ``_entry_head``). A cut entry carries a key, so the report block points a
#: claim at it and rule 15 has the model check the claim against it; a few
#: dozen characters of a passage rarely hold the sentence the report cited,
#: and the model would then call a supported claim unsupported -- the very
#: failure ``REPORT_EVIDENCE_NOT_LOADED`` exists to prevent. 160 is about a
#: quarter of a passage at the chunker's 600-character target. Below it the
#: item is not loaded (its marker says so) instead. An entry that is short in
#: full is never held to it.
REPORT_EVIDENCE_MIN_CUT_CHARS = 160

#: Kept free for the ``relations:`` items among the admitted knowledge objects
#: when two or more are wanted: the knowledge renderer writes them only into
#: the budget its entries leave, so a list sized to fill the whole budget
#: would always lose them. About six relation items (``kA -[edge]-> kB``).
REPORT_RELATIONS_RESERVE_CHARS = 240

#: ``knowledge_context`` writes at most this much of an object's definition
#: into its entry (its ``min(300, ...)``); the knowledge share counts every
#: object at this upper bound before it is read (``_knowledge_share``).
_KNOWLEDGE_DEFINITION_CAP = 300
_DEFINITION_LEAD = " — def: "

_RELATIONS_PREFIX = "relations: "
_RELATION_ITEM = re.compile(r"(k\d+) -\[[^\]]*\]-> (k\d+)(?: \(×\d+源\))?")


def _knowledge_head(key: str, object_type: Any, tier: Any, name: Any) -> str:
    """The head ``knowledge_context`` writes before an object's definition."""
    return f"{key}: [{object_type}][{tier}] {name}"


def _entry_head(kind: str, key: str, entry: Mapping[str, Any], text: str) -> int:
    """How many characters of a rendered entry are its renderer's head (key,
    labels, title, location) rather than the item's own text, rebuilt from
    the renderer's own id_map entry with the renderer's line shapes
    (``evidence_context.chunk_context`` / ``element_context`` /
    ``knowledge_context``, ``MemoryRetriever.context``). An entry that does
    not start with the rebuilt head (a renderer whose shape changed) counts
    as all head: a cut of it is never admitted, a whole one is unaffected."""
    if kind == RESTORED_KIND_CHUNK:
        head = f"{key}: "
    elif kind == RESTORED_KIND_ELEMENT:
        head = (
            f"{key}: [source-element][{entry.get('tier', '')}] "
            f"{entry.get('source_title', '')} · {entry.get('location_label', '')} — "
        )
    elif kind == RESTORED_KIND_MEMORY:
        head = f"{key}: [memory][personal][confirmed] {entry.get('name', '')} — "
    else:
        head = _knowledge_head(key, entry.get("object_type"), entry.get("tier"), entry.get("name"))
        if text.startswith(head + _DEFINITION_LEAD):
            head += _DEFINITION_LEAD
    return len(head) if text.startswith(head) else len(text)


class _Candidate(NamedTuple):
    """One entry as the admission walk sees it: its full length, how much of
    it is the renderer's head, and whether it is whole (not cut by its
    renderer)."""

    length: int
    head: int
    whole: bool


def _walk(
    order: Iterable, candidate: Callable[[Any], Optional[_Candidate]], room: int,
) -> dict:
    """``{object: characters admitted}``, deciding in ``order`` (relevance)
    across kinds: an entry that fits whole goes in; one that does not is cut
    to the room left if it keeps ``REPORT_EVIDENCE_MIN_CUT_CHARS`` of its own
    text after its head (a renderer's cut is held to the same floor),
    otherwise it is skipped and later, smaller entries may still fit. One
    separator between admitted entries."""
    taken: dict = {}
    used = 0
    for obj in order:
        entry = candidate(obj)
        if entry is None or used >= room:
            continue
        separator = 1 if taken else 0
        if entry.whole and used + separator + entry.length <= room:
            taken[obj] = entry.length
            used += separator + entry.length
            continue
        keep = min(entry.length, room - used - separator)
        if keep - entry.head < REPORT_EVIDENCE_MIN_CUT_CHARS:
            continue
        taken[obj] = keep
        used += separator + keep
    return taken


class _RenderedKinds(NamedTuple):
    """What the one renderer call per kind produced: each rendered object's
    ``(key, entry text, head length)`` and whether its renderer cut it, each
    kind's id_map entries, the knowledge kind's ``relations:`` line, and the
    objects the knowledge renderer folded (-> key) or withheld."""

    entries: dict
    cut: frozenset
    id_maps: dict
    relations: str
    folded: dict
    withheld: frozenset


def _knowledge_share(
    queue: list, kg_objects: list, hits: Mapping[str, Any], start: int,
    entries: Mapping[Any, tuple], cut: Collection, room: int,
) -> tuple[list, int]:
    """``(knowledge objects to render, their renderer budget)``, decided
    BEFORE any of them is read: the admission walk (``_walk``) over the
    already rendered entries of the other kinds and the knowledge objects at
    their largest possible entry (head plus ``_KNOWLEDGE_DEFINITION_CAP`` --
    an upper bound, so the share errs on the side of rendering fewer),
    within the room less ``REPORT_RELATIONS_RESERVE_CHARS`` when two or more
    are wanted. Only the objects that walk admits are rendered (each costs
    the renderer a read), and the budget is the room less what the more
    relevant entries of other kinds take, so the renderer's entries leave
    the reserve to its ``relations:`` line."""
    heads: dict = {}
    for index, obj in enumerate(kg_objects):
        hit = hits[obj[1]]
        heads[obj] = len(_knowledge_head(
            f"k{start + index}", hit.object_type, getattr(hit, "tier", "personal"),
            str(hit.payload.get("name", "")).strip(),
        )) + len(_DEFINITION_LEAD)

    def candidate(obj: Any) -> Optional[_Candidate]:
        if obj in heads:
            return _Candidate(heads[obj] + _KNOWLEDGE_DEFINITION_CAP, heads[obj], True)
        if obj in entries:
            _key, text, head = entries[obj]
            return _Candidate(len(text), head, obj not in cut)
        return None

    reserve = REPORT_RELATIONS_RESERVE_CHARS if len(kg_objects) >= 2 else 0
    taken = _walk(queue, candidate, room - reserve)
    others = sum(keep + 1 for obj, keep in taken.items() if obj not in heads)
    return [obj for obj in kg_objects if obj in taken], room - others


def _render_kinds(
    queue: list, values: Mapping[str, Mapping[str, Any]], id_map: Mapping[str, Any],
    *, room: int, bands: Mapping[str, tuple[int, int]], key_offset: int,
    render: Callable[..., tuple[str, dict]], lookups: CitationLookups,
) -> _RenderedKinds:
    """Render the queued objects, ONE call per kind (relevance order within
    the kind), and read each entry back at the boundaries the renderer
    recorded (``context_spans``) -- never by searching the text.

    The passage, element and Memory kinds go first, each with the whole
    partition as its budget (no kind may need more). The knowledge kind goes
    last with only the objects and the budget ``_knowledge_share`` leaves it:
    its renderer reads every object it is handed, and writes its
    ``relations:`` line only into what its entries leave. A kind whose block
    (relations line aside) fills its budget exactly had its last entry cut
    by its renderer."""
    by_kind: dict[str, list] = {}
    for obj in queue:
        by_kind.setdefault(obj[0], []).append(obj)
    starts: dict[str, int] = {}
    for kind in by_kind:
        low, high = bands[kind]
        starts[kind] = _band_start(id_map, low + key_offset, high + key_offset)
        by_kind[kind] = by_kind[kind][:max(0, high + key_offset - starts[kind] + 1)]
    entries: dict = {}
    cut: set = set()
    id_maps: dict = {}
    relations = ""
    folded: dict = {}
    withheld: set = set()

    def run(kind: str, objects: list, budget: int) -> None:
        nonlocal relations
        if not objects or budget <= 0:
            return
        outcome = KnowledgeContextOutcome()
        block, kind_map = render(
            kind, [values[kind][object_id] for _kind, object_id in objects],
            id_offset=starts[kind] - 1, budget_chars=budget, outcome=outcome,
            lookups=lookups,
        )
        folded.update(((kind, object_id), key) for object_id, key in outcome.folded.items())
        withheld.update((kind, object_id) for object_id in outcome.withheld)
        id_maps[kind] = dict(kind_map)
        texts: dict[str, str] = {}
        trailer = 0
        for span in recorded_spans(block) or ():
            text = block[span.start:span.end]
            if span.key:
                texts[span.key] = text
            elif not span.glue and kind == RESTORED_KIND_KG and text.startswith(_RELATIONS_PREFIX):
                relations, trailer = text, len(text) + 1
        last = None
        for key, entry in kind_map.items():
            if key in texts:
                obj = (kind, str(entry.get("object_id") or ""))
                entries[obj] = (key, texts[key], _entry_head(kind, key, entry, texts[key]))
                last = obj
        # The chunk, element and knowledge renderers spend their budget to
        # the last character only by cutting their last entry; Memory's
        # entries are whole (``AskService._render_report_kind``).
        if last is not None and kind != RESTORED_KIND_MEMORY and len(block) - trailer >= budget:
            cut.add(last)

    if room > 0:
        with recording_spans():
            for kind, objects in by_kind.items():
                if kind != RESTORED_KIND_KG:
                    run(kind, objects, room)
            if RESTORED_KIND_KG in by_kind:
                run(RESTORED_KIND_KG, *_knowledge_share(
                    queue, by_kind[RESTORED_KIND_KG], values[RESTORED_KIND_KG],
                    starts[RESTORED_KIND_KG], entries, cut, room,
                ))
    return _RenderedKinds(entries, frozenset(cut), id_maps, relations, folded, frozenset(withheld))


def _admit_entries(queue: list, rendering: _RenderedKinds, room: int) -> tuple[dict, str]:
    """``({object: entry text as admitted}, relations line)``: the rendered
    entries the partition takes (``_walk``, relevance order across kinds),
    and the knowledge kind's ``relations:`` items among the admitted entries
    when they still fit."""

    def candidate(obj: Any) -> Optional[_Candidate]:
        if obj not in rendering.entries:
            return None
        _key, text, head = rendering.entries[obj]
        return _Candidate(len(text), head, obj not in rendering.cut)

    taken = _walk(queue, candidate, room)
    admitted: dict = {}
    for obj, keep in taken.items():
        text = rendering.entries[obj][1]
        admitted[obj] = text if keep == len(text) else text[:keep - 1] + "…"
    used = sum(taken.values()) + max(0, len(taken) - 1)
    relations = _relations_among(
        rendering.relations,
        {rendering.entries[obj][0] for obj in admitted if obj[0] == RESTORED_KIND_KG},
    )
    if relations and used + 1 + len(relations) > room:
        relations = ""
    return admitted, relations


def _relations_among(line: str, keys: set) -> str:
    """The knowledge renderer's ``relations:`` line keeping only the items
    whose two ends are both among ``keys``; "" when none is left."""
    if not line.startswith(_RELATIONS_PREFIX) or len(keys) < 2:
        return ""
    kept = [
        item for item in line[len(_RELATIONS_PREFIX):].split("; ")
        if (match := _RELATION_ITEM.fullmatch(item)) is not None
        and match.group(1) in keys and match.group(2) in keys
    ]
    return _RELATIONS_PREFIX + "; ".join(kept) if kept else ""


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
    * otherwise rendered by ``render(kind, items, id_offset=, budget_chars=,
      outcome=, lookups=)`` -- the caller's native renderer for the kind --
      ONE call per kind with that kind's objects in relevance order (the
      knowledge kind last, handed only the objects and the budget its share
      admits -- ``_knowledge_share``), in the kind's native band
      (``bands[kind]`` plus ``key_offset``,
      continuing after the band's highest key already taken; objects past
      the band's end are not rendered). One call per kind is the renderers'
      own contract: ``knowledge_context`` derives its ``relations:`` line and
      its cluster fold from the objects of ONE call, and pays its fixed reads
      once per call. A knowledge object it folds into another of its cluster
      takes that one's key; one it withholds for what it is
      (``KnowledgeContextOutcome``) is neither loaded nor counted.

    Admission is then decided across kinds, in relevance order, over the
    rendered entries (``_admit_entries``): an entry that fits whole goes in;
    the first that does not is cut to the room left -- if the item's own text
    it keeps after the renderer's head (key, labels, title, location --
    ``_entry_head``) is at least ``REPORT_EVIDENCE_MIN_CUT_CHARS``, otherwise
    it is not loaded and smaller entries after it may still fit. A
    renderer's own cut (a kind that alone overflowed the room) is held to
    the same floor. So the section the question is about takes the budget
    first whatever its items' kinds, and no entry shrinks to a fragment that
    carries a key but no checkable text. The partition lists the admitted
    entries kind by kind (kinds in order of their most relevant admitted
    entry), the knowledge kind followed by the ``relations:`` items among
    its admitted entries when they fit.

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
    rendering = _render_kinds(
        queue, values, id_map, room=room, bands=bands, key_offset=key_offset,
        render=render, lookups=lookups,
    )
    admitted, relations = _admit_entries(queue, rendering, room)

    merged = id_map
    if admitted:
        kinds = list(dict.fromkeys(obj[0] for obj in queue if obj in admitted))
        parts: list[str] = []
        block_map: dict = {}
        for kind in kinds:
            items = [admitted[obj] for obj in queue if obj[0] == kind and obj in admitted]
            if kind == RESTORED_KIND_KG and relations:
                items.append(relations)
            parts.append(entry_lines(items))
        for obj in admitted:
            key = rendering.entries[obj][0]
            block_map[key] = rendering.id_maps[obj[0]][key]
        partition = joined(parts, "\n")
        context_block, merged = append(
            context_block, id_map, partition, block_map,
            # The admission already spent the partition budget; this bound
            # only keeps the append from cutting the partition again.
            budget_chars=len(context_block) + len(prefix) + len(partition),
            heading=REPORT_EVIDENCE_HEADING,
        )
    admitted_keys = {rendering.entries[obj][0]: obj for obj in admitted}
    not_loaded: list[str] = []
    for obj, keys in wanted.items():
        if keys[0] in key_map:
            continue
        representative = alias.get(obj, obj)
        target = rendering.entries[representative][0] if representative in admitted else None
        folded_into = rendering.folded.get(representative)
        if target is None and folded_into in admitted_keys:
            target = folded_into
        if target is not None and target in merged:
            key_map.update(dict.fromkeys(keys, target))
        elif representative not in rendering.withheld:
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


#: The answer's text fields the model's prose reaches.
_ANSWER_TEXT_FIELDS = ("answer", "conclusion")


def scrub_report_marks(target: Any) -> None:
    """Remove ``REPORT_EVIDENCE_NOT_LOADED`` from an answer's text, in place.

    The mark tells the answer model which report claims have evidence it was
    not given (rule 15); it is prompt vocabulary, never answer text, and rule
    15 forbids copying it -- this is the server-side guarantee behind that
    instruction. Called once per answer of a turn that attached a report, at
    the persistence seam every engine's answer passes
    (``AskService._stamp_report_receipt`` from ``_save_answer``); a target
    without those fields (the retrieval-only output) is left as it is."""
    for name in _ANSWER_TEXT_FIELDS:
        value = getattr(target, name, None)
        if isinstance(value, str) and REPORT_EVIDENCE_NOT_LOADED in value:
            setattr(target, name, value.replace(REPORT_EVIDENCE_NOT_LOADED, ""))


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
    "REPORT_EVIDENCE_MIN_CUT_CHARS",
    "REPORT_RELATIONS_RESERVE_CHARS",
    "citation_identity",
    "report_readable",
    "scrub_report_marks",
    "with_report_selected",
]
