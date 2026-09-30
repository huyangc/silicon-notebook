"""The terminal citation check of a global Ask: INTEGRITY, never permission.

Once the shared engine has produced an answer, every citation and every answer
anchor that names library material is held against the run's retrieval-time
evidence table (``FederatedRunPlan.on_evidence``, filled by the federated chunk
channel and by ``services.evidence_attestation``) and against the library as it
is NOW. The check reports, per cited piece of evidence, one of three things:

``changed``       the text read at retrieval time is not the text there now
                  (or part of the passage behind the citation is gone);
``source_gone``   the source, or the cited element itself, has been deleted;
``unverifiable``  there is nothing trustworthy to compare against: nobody
                  registered a retrieval-time snapshot (``unattested``), the
                  snapshot or the terminal read failed (``unreadable``), the
                  reference names no library (``unattributed``), or its source
                  was never in the run's frozen ceiling (``out_of_ceiling``).

The four parenthesised codes are INTERNAL. They travel in content-free events
so an operator can tell a race with an editing user from a producer that never
registered, or from a retrieval-layer bug; the wire carries only the three
kinds above.

⛔ NOTHING HERE WITHHOLDS ANSWER TEXT, AND NOTHING HERE IS A PERMISSION CHECK.
Permission and source-scope enforcement belong to the retrieval layer alone
(user ruling 2026-09-29): synthesis and this check assume everything that
reached the answer was readable by the asker. ``out_of_ceiling`` and
``unattributed`` therefore do not void anything -- they mark the reference
``unverifiable`` and raise a diagnostic, because reaching this point with such
a reference means some retrieval channel leaked or failed to normalise. A
partially failed answer is delivered whole, with the failed references marked
and a truthful summary (``AskResponse.citation_check``); it is never replaced,
blanked or trimmed (user ruling Q3).

EVERY reference is judged -- no early exit on the first failure -- because the
reader is shown a per-card reason and a per-kind count.

The source-level half reads, BY ID over the cited sources only, whether each
is still a visible source of its library: in a global run the frozen ceiling is
every visible source of each participant, and a source leaves that list only by
being deleted (notebook-level access loss is ``GlobalAskService._check``'s job
and fails the whole job before this runs).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Callable, Iterable, Mapping

from app.repositories.read_budget import read_budget
from app.services.cancellation import AskCancelled, raise_if_cancelled


CHANGED = "changed"
SOURCE_GONE = "source_gone"
UNVERIFIABLE = "unverifiable"
#: The only verification values that reach the wire, in severity order: when a
#: citation's own element and the rest of its passage disagree, the most
#: definite finding wins.
VERIFICATION_KINDS = (SOURCE_GONE, CHANGED, UNVERIFIABLE)

# Internal reason codes -- events only, never serialised onto an answer.
REASON_CHANGED = "changed"
REASON_SOURCE_GONE = "source_gone"
REASON_UNATTESTED = "unattested"
REASON_UNREADABLE = "unreadable"
REASON_UNATTRIBUTED = "unattributed"
REASON_OUT_OF_CEILING = "out_of_ceiling"
INTERNAL_REASONS = (
    REASON_CHANGED, REASON_SOURCE_GONE, REASON_UNATTESTED, REASON_UNREADABLE,
    REASON_UNATTRIBUTED, REASON_OUT_OF_CEILING,
)
#: The two reasons that can only mean a retrieval-layer defect, not a race.
DIAGNOSTIC_REASONS = (REASON_OUT_OF_CEILING, REASON_UNATTRIBUTED)

#: Card labels, one per wire kind.
VERIFICATION_LABELS = {
    CHANGED: "原文已改动",
    SOURCE_GONE: "资料已删除",
    UNVERIFIABLE: "无法核对",
}
# The notice under a partially failed answer. ONE wording across the stack:
# ``frontend/app/citation-verification.ts`` (``citationCheckNotice``) is the
# canonical spelling and ``scripts/check_citation_verification_contract.py``
# pins these literals against it verbatim. Clauses are "N 条<card label>" for
# every kind with a count, joined by "、"; with no per-kind count at all the
# clause falls back to the total. The public page speaks in the past tense
# ("snapshot"): its state is the one at answer time.
NOTICE_LEADS = {
    "live": "本次回答有部分引用未通过核对",
    "snapshot": "回答生成时，有部分引用未通过核对",
}
NOTICE_TAIL = "回答内容照常保留，带标记的引用可点开查看原因。"
REASON_JOINER = "、"
REASON_CLAUSE = "{count} 条{label}"
REASON_FALLBACK = "共 {count} 条"
# Display / wire order of the three kinds (the summary's key order).
_WIRE_ORDER = (CHANGED, SOURCE_GONE, UNVERIFIABLE)
_SUMMARY_COUNTS = ("checked", "failed", *_WIRE_ORDER)


class _Absent:
    """'Nobody published a snapshot for this element.' Distinct from ``None``,
    which is a STATED 'published, but could not be fingerprinted'."""

    __slots__ = ()


_ABSENT = _Absent()


def reference_key(reference) -> tuple[str, str, str]:
    """One piece of cited evidence; an anchor and a citation naming the same
    element are ONE reference for counting and judging."""
    return (
        str(getattr(reference, "notebook_id", "") or ""),
        str(getattr(reference, "source_id", "") or ""),
        str(getattr(reference, "element_id", "") or ""),
    )


def checkable_references(response) -> dict[tuple[str, str, str], Any]:
    """Every citation and anchor that names library material, de-duplicated.

    ONE rule decides what is not checked: a reference naming neither a source
    nor an element carries nothing whose integrity could be tested -- a memory
    row, a bare graph node, and URL-backed external material from a reflect
    plugin action (which by construction names no library row) -- and counting
    it as ``unverifiable`` would flag every such answer for a property it never
    claimed (J3). The tier is deliberately NOT consulted: an "external"
    reference that points at a source or an element is library material
    wearing the wrong tier and is judged like any other.
    """
    found: dict = {}
    for reference in [*(response.citations or ()), *(response.anchors or ())]:
        key = reference_key(reference)
        if not key[1] and not key[2]:
            continue
        found.setdefault(key, reference)
    return found


@dataclass(frozen=True)
class CitationCheckOutcome:
    """Per-reference verdicts (failures only) plus what was checked."""

    checked: int
    verdicts: Mapping[tuple[str, str, str], "tuple[str, str]"] = field(
        default_factory=dict,
    )

    @property
    def failed(self) -> int:
        return len(self.verdicts)

    def counts(self) -> dict[str, int]:
        """``{wire kind: count}`` over the failed references, every kind present."""
        counts = dict.fromkeys(_WIRE_ORDER, 0)
        for verification, _reason in self.verdicts.values():
            counts[verification] += 1
        return counts

    def reasons(self) -> dict[str, int]:
        """``{internal reason: count}``, only the reasons that occurred."""
        found: dict = {}
        for _verification, reason in self.verdicts.values():
            found[reason] = found.get(reason, 0) + 1
        return found

    def summary(self) -> dict | None:
        """The wire summary, or ``None`` when nothing failed."""
        if not self.failed:
            return None
        return {
            "outcome": "partial", "checked": self.checked, "failed": self.failed,
            **self.counts(),
        }


class GlobalCitationCheck:
    """One run's terminal check: two bounded reads, then pure judgement.

    ``sources`` needs ``evidence_fingerprints`` and ``visible_source_owners``
    (``GlobalAskSourceStorePort``). Both reads are BY ID over what the answer
    cites, so their cost follows the answer, never the size of the cited
    libraries. Reads are bounded by the per-library budget and poll ``event``;
    a cancellation propagates, any other read failure turns the references it
    would have decided into ``unverifiable`` (``unreadable``) -- the answer is
    delivered either way.
    """

    def __init__(self, sources, *, notebook_timeout_seconds: float,
                 emit: Callable[[dict], None] | None = None):
        self.sources = sources
        self.notebook_timeout_seconds = float(notebook_timeout_seconds)
        self.emit = emit

    def run(self, response, *, evidence: Mapping, siblings: Mapping,
            source_ceiling: Mapping, event=None) -> CitationCheckOutcome:
        references = checkable_references(response)
        if not references:
            return CitationCheckOutcome(checked=0)
        current = self._read_current(references, siblings, event)
        visible = self._read_visible(
            judged_sources(references, evidence, siblings, source_ceiling), event,
        )
        verdicts: dict = {}
        for key, reference in references.items():
            notebook_id = key[0]
            verdict = judge_reference(
                reference, evidence=evidence, current=current,
                siblings=siblings.get(key[2], ()) if key[2] else (),
                ceiling=source_ceiling.get(notebook_id, ()),
                live_sources=visible.get(notebook_id),
            )
            if verdict is not None:
                verdicts[key] = verdict
        return CitationCheckOutcome(checked=len(references), verdicts=verdicts)

    def _read_current(self, references: Mapping, siblings: Mapping, event):
        """ONE terminal read over cited elements and their passage siblings."""
        cited = [key[2] for key in references if key[2]]
        wanted = list(dict.fromkeys(cited + sorted({
            sibling for element_id in cited for sibling in siblings.get(element_id, ())
        })))
        if not wanted:
            return {}
        return self._bounded(
            "fingerprints", event,
            lambda: dict(self.sources.evidence_fingerprints(wanted)),
        )

    def _read_visible(self, wanted: Mapping[str, set], event) -> dict:
        """``{notebook_id: those of its judged sources still visible in it |
        None}`` in ONE by-id statement over the cited sources.

        ``wanted`` is ``judged_sources``: per library inside the frozen
        ceiling, exactly the source ids the judgement will look up, so the
        verdict is the one the whole visible list would give -- a source that
        left the visible set of its library (deleted, or moved out of it) is
        ``source_gone`` -- while the read is bounded by the answer's
        references (one id per distinct cited source) instead of returning
        every visible source of every cited library. Failure stays PER
        LIBRARY: if the statement fails, each library's own ids are re-read on
        their own under one shared deadline, and a library whose read also
        fails is the only one whose references become ``unreadable``.
        """
        if not wanted:
            return {}

        def owned(owners, notebook_id):
            return {
                source_id for source_id in wanted[notebook_id]
                if owners.get(source_id) == notebook_id
            }

        owners = self._bounded(
            "visible_sources", event,
            lambda: dict(self.sources.visible_source_owners(
                sorted({source_id for ids in wanted.values() for source_id in ids}),
            )),
        )
        if owners is not None:
            return {notebook_id: owned(owners, notebook_id) for notebook_id in wanted}
        deadline = time.monotonic() + self.notebook_timeout_seconds
        result: dict = {}
        for notebook_id in sorted(wanted):
            own = self._bounded(
                "visible_sources", event,
                lambda notebook_id=notebook_id: dict(
                    self.sources.visible_source_owners(sorted(wanted[notebook_id])),
                ),
                deadline=deadline,
            )
            result[notebook_id] = None if own is None else owned(own, notebook_id)
        return result

    def _bounded(self, read: str, event, call, *, deadline: float | None = None):
        """Run one read under the per-library budget; ``None`` when it failed."""
        raise_if_cancelled(event)
        if deadline is None:
            deadline = time.monotonic() + self.notebook_timeout_seconds
        try:
            with read_budget(deadline, event):
                return call()
        except AskCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - the answer is delivered anyway
            raise_if_cancelled(event)
            self._emit({
                "kind": "global_ask_citation_check_read_failed",
                "read": read, "error_type": type(exc).__name__,
            })
            return None

    def _emit(self, event: dict) -> None:
        if self.emit is None:
            return
        try:
            self.emit(event)
        except Exception:  # noqa: BLE001 - observability is fail-open
            pass


def judged_sources(references: Mapping, evidence: Mapping, siblings: Mapping,
                   source_ceiling: Mapping) -> dict[str, set]:
    """``{notebook_id: source ids}`` whose visibility ``judge_reference`` will
    look up -- the reference's own source (or the one its snapshot names) and
    the snapshot sources of its passage siblings -- per library, limited to
    the library's frozen ceiling: a source outside it is ``out_of_ceiling``
    before visibility is consulted. Mirrors ``judge_reference`` /
    ``_sibling_verdict`` exactly, so the by-id read answers every lookup."""
    wanted: dict = {}
    for key, reference in references.items():
        notebook_id, _source_id, element_id = key
        ceiling = source_ceiling.get(notebook_id) if notebook_id else None
        if not ceiling:
            continue
        before = evidence.get(element_id) if element_id else None
        candidates = [
            str(getattr(reference, "source_id", "") or "")
            or (before[0] if isinstance(before, tuple) else ""),
        ]
        if element_id:
            candidates += [
                snapshot[0]
                for snapshot in (evidence.get(sibling) for sibling in siblings.get(element_id, ()))
                if isinstance(snapshot, tuple)
            ]
        found = {source_id for source_id in candidates if source_id and source_id in ceiling}
        if found:
            wanted.setdefault(notebook_id, set()).update(found)
    return wanted


def judge_reference(reference, *, evidence: Mapping, current, siblings: Iterable[str],
                    ceiling: Iterable[str], live_sources) -> "tuple[str, str] | None":
    """``(wire kind, internal reason)`` for one reference, or ``None`` if it holds.

    ``current`` is the terminal by-id read (``None`` when it failed);
    ``live_sources`` is the library's current source set (``None`` when that
    read failed). The source-level half runs first and is the whole check for
    a reference with no element (J3).
    """
    notebook_id = str(getattr(reference, "notebook_id", "") or "")
    if not notebook_id:
        return UNVERIFIABLE, REASON_UNATTRIBUTED
    element_id = str(getattr(reference, "element_id", "") or "")
    before = evidence.get(element_id, _ABSENT) if element_id else _ABSENT
    source_id = str(getattr(reference, "source_id", "") or "") or (
        before[0] if isinstance(before, tuple) else ""
    )
    if not source_id:
        return UNVERIFIABLE, (REASON_UNREADABLE if before is None else REASON_UNATTESTED)
    source_verdict = _source_verdict(source_id, ceiling, live_sources)
    if source_verdict is not None or not element_id:
        return source_verdict
    return _most_severe([
        _element_verdict(reference, before, current, element_id),
        *(
            _sibling_verdict(
                evidence.get(sibling, _ABSENT), current, sibling, ceiling, live_sources,
            )
            for sibling in siblings
        ),
    ])


def _source_verdict(source_id, ceiling, live_sources):
    if source_id not in ceiling:
        return UNVERIFIABLE, REASON_OUT_OF_CEILING
    if live_sources is None:
        return UNVERIFIABLE, REASON_UNREADABLE
    if source_id not in live_sources:
        return SOURCE_GONE, REASON_SOURCE_GONE
    return None


def _element_verdict(reference, before, current, element_id):
    """The cited element against its retrieval-time snapshot.

    Absent -> ``unattested`` (the honest answer for a producer that never
    registered, including an id that was already dangling before the question
    -- that is not a deletion during the answer). Present now under a
    different source than the citation names -> ``changed``.
    """
    if before is _ABSENT:
        return UNVERIFIABLE, REASON_UNATTESTED
    if before is None or current is None:
        return UNVERIFIABLE, REASON_UNREADABLE
    after = current.get(element_id)
    if after is None:
        return SOURCE_GONE, REASON_SOURCE_GONE
    claimed = str(getattr(reference, "source_id", "") or "")
    if after != before or (claimed and after[0] != claimed):
        return CHANGED, REASON_CHANGED
    return None


def _sibling_verdict(before, current, sibling, ceiling, live_sources):
    """Another element of the passage one citation was minted from.

    A sibling is only known because the federated channel published its
    passage and fingerprinted every element of it in the same read, so a
    sibling with no stated snapshot is a contradiction and reads as
    ``unreadable``. A sibling deleted while the cited element survives means
    the quoted passage changed; the whole source being gone is caught by the
    source-level half.
    """
    if before is _ABSENT or before is None:
        return UNVERIFIABLE, REASON_UNREADABLE
    source_verdict = _source_verdict(before[0], ceiling, live_sources)
    if source_verdict is not None:
        return source_verdict
    if current is None:
        return UNVERIFIABLE, REASON_UNREADABLE
    if current.get(sibling) != before:
        return CHANGED, REASON_CHANGED
    return None


def _most_severe(verdicts):
    found = [verdict for verdict in verdicts if verdict is not None]
    if not found:
        return None
    return min(found, key=lambda verdict: VERIFICATION_KINDS.index(verdict[0]))


def apply_outcome(response, outcome: CitationCheckOutcome) -> None:
    """Mark the failed references and publish the summary. TEXT IS UNTOUCHED.

    On a partial failure the answer stops claiming it is fully grounded:
    ``grounded`` goes False and ``evidence_level`` is capped at ``overview``
    (``inferred`` stays ``inferred``; nothing is raised).
    """
    from app.models.ask import CitationCheckSummary

    if not outcome.failed:
        return
    for reference in [*(response.citations or ()), *(response.anchors or ())]:
        verdict = outcome.verdicts.get(reference_key(reference))
        if verdict is not None:
            reference.verification = verdict[0]
    response.citation_check = CitationCheckSummary(**outcome.summary())
    response.grounded = False
    if response.evidence_level == "grounded":
        response.evidence_level = "overview"


def check_trace_summary(outcome: CitationCheckOutcome) -> str:
    """The reasoning trace's 「核对」 step, stated as counted (same reason text
    as the notice, so the trace and the notice can never disagree)."""
    if not outcome.failed:
        return f"核对引用：共核对 {outcome.checked} 条，全部通过"
    reasons = citation_check_reason_text(outcome.summary())
    return f"核对引用：共核对 {outcome.checked} 条，{outcome.failed} 条未通过（{reasons}）"


def check_count(value) -> int:
    """One stored count, coerced: a positive whole number or 0. Never raises.

    The single coercion every reader of a stored summary applies -- the public
    projection (``conversation_public_view``), ``global_answer_check`` and the
    notice -- so a malformed or hand-edited value (a string, a list, a bool, a
    negative number) reads as 0 on every surface alike. A finite fraction is
    floored, exactly as the frontend's ``count`` does, so the two twins agree
    on every input of ``backend/tests/fixtures/citation_check_notice_cases.json``.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if value != value or value in (float("inf"), float("-inf")) or value <= 0:
        return 0
    return int(value)


def coerce_check_summary(value) -> dict | None:
    """A stored ``citation_check`` as the six-key wire dict, or ``None``.

    ``None`` when the value is not a mapping or records no failure. Every count
    goes through ``check_count``; ``outcome`` is always ``partial`` (a failed
    check never voids an answer), so a stored label cannot reach a response.
    """
    if not isinstance(value, Mapping):
        return None
    counts = {key: check_count(value.get(key)) for key in _SUMMARY_COUNTS}
    if not counts["failed"]:
        return None
    return {"outcome": "partial", **counts}


def citation_check_reason_text(check: Mapping) -> str:
    """「2 条原文已改动、1 条资料已删除」, or 「共 N 条」.

    The per-kind clauses are used only when they account for every failure;
    counts that do not add up to ``failed`` (a malformed or foreign summary)
    fall back to the total, so the sentence never under-reports.
    """
    failed = check_count(check.get("failed"))
    counts = {kind: check_count(check.get(kind)) for kind in _WIRE_ORDER}
    if sum(counts.values()) == failed:
        parts = [
            REASON_CLAUSE.format(count=counts[kind], label=VERIFICATION_LABELS[kind])
            for kind in _WIRE_ORDER if counts[kind]
        ]
        if parts:
            return REASON_JOINER.join(parts)
    return REASON_FALLBACK.format(count=failed)


def citation_check_notice(check: Mapping | None, tense: str = "live") -> str:
    """The one sentence shown under a partially failed answer, or ``""``.

    ``tense="snapshot"`` is the public page's past tense. This is the BACKEND
    TWIN of the frontend's ``citationCheckNotice``, which renders the sentence
    on every page that shows it; ``scripts/check_citation_verification_contract.py``
    pins the two against each other. No backend surface emits the sentence
    today (MCP carries the counts); the reasoning trace's 「核对」 step shares
    its reason text (``citation_check_reason_text``).
    """
    if not check or not check_count(check.get("failed")):
        return ""
    lead = NOTICE_LEADS["snapshot" if tense == "snapshot" else "live"]
    return f"{lead}：{citation_check_reason_text(check)}。{NOTICE_TAIL}"


def global_answer_check(job) -> dict | None:
    """The stored citation-check summary of one global turn, or ``None``.

    Accepts the job model (``GlobalAskJob``) or a stored row dict (``payload``
    holding ``answer``, or ``answer`` at top level). ``None`` whenever nothing
    failed, the turn predates the check, or it has no engine answer (legacy
    ``response`` rows were never checked this way and keep their stored text).
    A read path: every value goes through ``coerce_check_summary`` and nothing
    here raises on a malformed row.
    """
    if not isinstance(job, Mapping):
        answer = getattr(job, "answer", None)
        check = getattr(answer, "citation_check", None) if answer is not None else None
        return coerce_check_summary(check.model_dump() if check is not None else None)
    payload = job.get("payload") if isinstance(job.get("payload"), Mapping) else job
    stored = payload.get("answer")
    return coerce_check_summary(stored.get("citation_check")) if isinstance(stored, Mapping) else None


__all__ = [
    "CHANGED", "CitationCheckOutcome", "GlobalCitationCheck", "SOURCE_GONE",
    "UNVERIFIABLE", "VERIFICATION_KINDS", "VERIFICATION_LABELS",
    "NOTICE_LEADS", "NOTICE_TAIL", "REASON_CLAUSE", "REASON_FALLBACK",
    "REASON_JOINER", "apply_outcome", "check_trace_summary",
    "check_count", "checkable_references", "citation_check_notice",
    "citation_check_reason_text", "coerce_check_summary", "global_answer_check",
    "judge_reference", "reference_key",
]
