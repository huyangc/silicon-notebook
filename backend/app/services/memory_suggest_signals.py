"""Auto user-memory suggestions -- pure signal functions (plan T7a).

Design: ``docs/superpowers/specs/2026-10-28-auto-user-memory-design_zh.md``
(§3.1, §3.2, §4.2 steps 3/4/7, §5.1, §5.2). Everything here is a pure function
over rows the store has already read (``MemorySuggestStore``: sampling reads,
``answer_projections``, ``latest_feedback``, ``suggest_neighbors``): no I/O, no
clock, no repository import. The job (T7b) owns the order of the calls; this
module owns what each one means.

The flow a run follows, and where each function sits in it::

    scan   = store.clarified_scan(...)         # ascending, <= CLARIFIED_SCAN_MAX
    window = store.repeated_window(...)        # descending, <= REPEATED_WINDOW_LIMIT
    pairs  = store.corrected_scan(...)         # enabled class only, same ascending bound
    clarified = clarified_signals(scan, projections, notebook_id=...)
    repeated  = repeated_signals(window, cursor, projections, feedback, ...)
    corrected = corrected_signals(pairs, projections, notebook_id=...)
    # prefilter: drop item.known_in(fingerprints_present) ...
    items, cursor_end = select_items(clarified, repeated, enabled_signals=...,
                                     corrected=corrected, cap=..., scan_end=...)
    text = render_items(items, neighbors, limits)      # -> memory_suggest_prompt
    decisions = parse_suggest_reply(payload, ...)      # whole-batch validation

Cursor rule (G25 / §4.2 table): a run's cursor may advance only over asks the
run has fully processed. The ascending scans bound how far it may advance:
the cursor reaches *at most* their shared prefix, and stops *before* the
first clarified/corrected item the item cap cut off. The newest ask of the
descending window is never a cursor candidate: the window is read newest-first
and may lie entirely beyond what the 40-row scan covered.

Wire note: fingerprints are computed here (they define "the same thing") but
stored by the store; this module never sees the fingerprint table, only the
set of fingerprints already present.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, replace
from typing import Any

from app.core.memory_inputs import (
    MEMORY_TAG_MAX_CHARS,
    MEMORY_TAG_MAX_COUNT,
    MEMORY_TITLE_MAX_CHARS,
    MemoryInputError,
    normalize_tags,
)
from app.core.model_values import as_text, as_text_list
from app.domain.instant_order import utc_microsecond_key
from app.domain.memory_suggest_contracts import (
    CLUSTER_FINGERPRINT_MAX,
    FINGERPRINT_MIN_TERMS,
    GROUNDED_EVIDENCE_LEVEL,
    REPEATED_JACCARD_MIN,
    REPEATED_MIN_CLUSTER_SIZE,
    SIGNAL_CLARIFIED,
    SIGNAL_CORRECTED,
    SIGNAL_REPEATED,
    SUGGESTION_KINDS,
)
from app.services.retrieval import keyword_basis

#: ``feedback.rating`` value that marks an answer the person found useful.
RATING_USEFUL = "useful"
#: Most member questions of a repeated cluster shown to the model.
REPEATED_QUESTIONS_SHOWN = 3
#: Longest ``content`` a kept suggestion may carry (the prompt states it).
SUGGESTION_CONTENT_MAX_CHARS = 800
#: Server-side item ids are ``i1``, ``i2``, ...
ITEM_ID_PREFIX = "i"

_EQUALS_RUN = re.compile(r"={3,}")


# ------------------------------------------------------------------ cursor
def _ask_key(created_at: str, ask_id: str) -> tuple[int, str]:
    instant = utc_microsecond_key(created_at)
    if instant is None:
        raise ValueError("invalid signal timestamp")
    return instant, ask_id


@dataclass(frozen=True)
class SignalCursor:
    """The job row's ``(created_at, id)`` cursor.

    ``inclusive`` follows ``memory_suggest_jobs.cursor_inclusive``: a freshly
    built row includes the triggering ask itself; after the first advance the
    cursor is strictly exclusive.
    """

    created_at: str
    id: str
    inclusive: bool = False

    def admits(self, created_at: str, ask_id: str) -> bool:
        """Whether an ask at ``(created_at, ask_id)`` is after this cursor."""
        mine = _ask_key(self.created_at, self.id)
        key = _ask_key(created_at, ask_id)
        return key >= mine if self.inclusive else key > mine


def _cursor_position(cursor: SignalCursor) -> tuple[int, str, int]:
    """Where the next scan starts: AT an inclusive cursor's ask, just after an
    exclusive one's."""
    return (*_ask_key(cursor.created_at, cursor.id), 0 if cursor.inclusive else 1)


def earlier_cursor(
    first: SignalCursor | None, second: SignalCursor | None
) -> SignalCursor | None:
    """The cursor that lets the next scan start earlier (``None`` = no bound).
    A run may advance only as far as every one of its limits allows."""
    if first is None:
        return second
    if second is None:
        return first
    return first if _cursor_position(first) <= _cursor_position(second) else second


def scan_end_cursor(scan_rows: Sequence[Mapping[str, Any]]) -> SignalCursor | None:
    """Cursor on the last row of the ascending clarified scan (exclusive), or
    ``None`` for an empty scan. This is the furthest a run may advance."""
    if not scan_rows:
        return None
    last = max(scan_rows, key=lambda row: _ask_key(str(row["created_at"]), str(row["id"])))
    return SignalCursor(str(last["created_at"]), str(last["id"]), inclusive=False)


def common_scan_end_cursor(
    cursor: SignalCursor,
    clarified_scan: Sequence[Mapping[str, Any]],
    corrected_scan: Sequence[Mapping[str, Any]],
    enabled_signals: Collection[str],
) -> SignalCursor | None:
    """An empty enabled scan has observed nothing beyond the original cursor.

    Separate reads may straddle Ask completion. ``None`` cannot stand for
    that empty prefix, because ``earlier_cursor`` treats it as no bound.
    Without corrected, preserve the original two-class scan behavior.
    """
    if SIGNAL_CORRECTED not in enabled_signals:
        return scan_end_cursor(clarified_scan)
    corrected_end = scan_end_cursor(corrected_scan) or cursor
    if SIGNAL_CLARIFIED not in enabled_signals:
        return corrected_end
    return earlier_cursor(scan_end_cursor(clarified_scan) or cursor, corrected_end)


# ------------------------------------------------------------------ keywords
def question_keywords(text: str) -> frozenset[str]:
    """The stopword-free keyword-basis terms of a text, flattened to one set.

    ``keyword_basis`` already drops stopwords; a spaced model name ("Cosmos 3")
    is one alias unit there and counts as one term here too.
    """
    basis = keyword_basis(text or "", honor_quotes=False)
    return frozenset(basis.tokens) | frozenset(alias for alias, _ in basis.alias_units)


def _jaccard(left: AbstractSet[str], right: AbstractSet[str]) -> float:
    union = len(left | right)
    return len(left & right) / union if union else 0.0


def leader_clusters(keyword_sets: Sequence[frozenset[str]]) -> list[list[int]]:
    """Leader clustering over keyword sets given in ASCENDING ask-time order.

    Returns clusters as lists of indices into ``keyword_sets`` (each in time
    order, first index = the leader). Sets with fewer than
    ``FINGERPRINT_MIN_TERMS`` terms are not clustered at all (they appear in no
    cluster). A set joins the leader with the highest Jaccard that reaches
    ``REPEATED_JACCARD_MIN``; equal Jaccard goes to the EARLIEST leader; no
    leader reaching the threshold makes it a new leader. No single-link
    chaining: similarity is always to the leader, never to another member.
    """
    clusters: list[list[int]] = []
    for index, terms in enumerate(keyword_sets):
        if len(terms) < FINGERPRINT_MIN_TERMS:
            continue
        best: int | None = None
        best_score = 0.0
        for position, cluster in enumerate(clusters):
            score = _jaccard(terms, keyword_sets[cluster[0]])
            if score >= REPEATED_JACCARD_MIN and score > best_score:
                best, best_score = position, score  # strict ">" keeps the earliest leader on a tie
        if best is None:
            clusters.append([index])
        else:
            clusters[best].append(index)
    return clusters


# ------------------------------------------------------------------ fingerprints
def _fingerprint(kind: str, notebook_id: str, terms: Iterable[str]) -> str:
    material = "|".join((kind, notebook_id, " ".join(sorted(set(terms)))))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def clarified_fingerprint_terms(clarifications: Sequence[tuple[str, str]]) -> frozenset[str]:
    """Keyword terms that identify one ask's clarification answers.

    Per clarification row: the answer's keyword basis; when that has fewer than
    ``FINGERPRINT_MIN_TERMS`` terms ("the former", "2024"), the SAME row's
    question keyword basis is merged in, because a short answer means nothing
    without its question. (Plan appendix B2: a persisted intent carries no
    ``options``, only ``{id, question, answer}``.) The union over rows is the
    item's term set.
    """
    terms: set[str] = set()
    for question, answer in clarifications:
        row_terms = set(question_keywords(answer))
        if len(row_terms) < FINGERPRINT_MIN_TERMS:
            row_terms |= question_keywords(question)
        terms |= row_terms
    return frozenset(terms)


def clarified_fingerprint(
    notebook_id: str, clarifications: Sequence[tuple[str, str]]
) -> tuple[str, bool]:
    """``(fingerprint, registrable)`` of a clarified ask, or ``("", False)``
    when there is no term at all. ``registrable`` is False below
    ``FINGERPRINT_MIN_TERMS`` terms: such a hash may serve as a negative-cache
    entry (a "no" verdict) but is never registered as ``proposed``."""
    terms = clarified_fingerprint_terms(clarifications)
    if not terms:
        return "", False
    return (
        _fingerprint(SIGNAL_CLARIFIED, notebook_id, terms),
        len(terms) >= FINGERPRINT_MIN_TERMS,
    )


def clarified_ask_fingerprint(notebook_id: str, ask_id: str) -> str:
    """The identity hash of ONE clarified ask (plan appendix B18).

    The content fingerprint of a short answer stays under
    ``FINGERPRINT_MIN_TERMS`` terms and may not be registered ``proposed``
    (it would suppress unrelated asks). This hash names the ask itself, so it
    suppresses nothing else: registered ``proposed`` with the draft (and
    ``declined`` with a "no"), it keeps an ask the cursor is held before --
    a kept item past the per-run cap, an earlier ask still in flight, a
    settle that failed -- from being proposed twice when it is scanned again.
    """
    return _fingerprint("clarified-ask", notebook_id, (ask_id,))


def repeated_member_fingerprints(
    notebook_id: str, member_questions: Sequence[str]
) -> tuple[str, ...]:
    """One fingerprint per DISTINCT member question (keyword set), at most
    ``CLUSTER_FINGERPRINT_MAX``; questions are given oldest first and the
    newest are kept (a sliding window keeps the newest members, so they are
    what the next run's cluster still contains)."""
    seen: dict[str, None] = {}
    for question in member_questions:
        terms = question_keywords(question)
        if len(terms) >= FINGERPRINT_MIN_TERMS:
            seen.setdefault(_fingerprint(SIGNAL_REPEATED, notebook_id, terms), None)
    ordered = list(seen)
    return tuple(ordered[-CLUSTER_FINGERPRINT_MAX:])


def cluster_hit_majority(fingerprints: Iterable[str], present: AbstractSet[str]) -> bool:
    """Whether MORE THAN HALF of a cluster's distinct member fingerprints are
    already known. Distinct, so one question asked twice casts one vote; and
    strictly more than half, so one old question re-asked inside a new topic
    cluster cannot suppress it."""
    distinct = set(fingerprints)
    if not distinct:
        return False
    return 2 * len(distinct & present) > len(distinct)


# ------------------------------------------------------------------ items
@dataclass(frozen=True)
class SignalItem:
    """One signal the model may turn into a draft memory.

    * ``clarified``: one ask whose intent carries clarification answers.
      ``answer_id`` is the answer of that ask (provenance only -- never bound
      as ``source_answer_id``, design §3.1).
    * ``repeated``: a cluster of similar asks. ``answer_id`` is the selected
      grounded answer (becomes ``source_answer_id``); ``answer_ids`` lists the
      grounded answers of the whole cluster; ``questions`` the newest member
      questions.

    * ``corrected`` pairs the prior answer excerpt with the later question and
      answer. Both answer ids are provenance only, with no bound answer slot.

    ``ask_id`` / ``ask_created_at`` anchor the item in time: the ask itself
    (clarified/corrected) or the earliest ask of the cluster after the cursor
    (repeated).
    ``fingerprints`` is every hash to test against the fingerprint table (and
    to register as ``declined``); ``proposed_fingerprints`` is what to register
    as ``proposed`` when the draft is written (empty when the clarified answer
    stays under ``FINGERPRINT_MIN_TERMS`` terms). A clarified item also
    carries ``ask_fingerprint`` (``clarified_ask_fingerprint``), registered and
    looked up beside them (``lookup_fingerprints`` / ``on_write`` /
    ``on_decline``).
    """

    signal: str
    ask_id: str
    ask_created_at: str
    question: str
    answer_id: str
    conversation_id: str
    mode: str
    evidence_level: str
    excerpt: str
    fingerprints: tuple[str, ...]
    proposed_fingerprints: tuple[str, ...]
    clarifications: tuple[tuple[str, str], ...] = ()
    questions: tuple[str, ...] = ()
    answer_ids: tuple[str, ...] = ()
    cluster_size: int = 0
    item_id: str = ""
    ask_fingerprint: str = ""
    previous_excerpt: str = ""

    def known_in(self, present: AbstractSet[str]) -> bool:
        """Whether the fingerprint table already knows this item (prefilter).

        Clarified: its content fingerprint or its ask fingerprint is present.
        Repeated: a strict majority of the cluster's member fingerprints is
        present.
        """
        if self.signal == SIGNAL_REPEATED:
            return cluster_hit_majority(self.fingerprints, present)
        if self.ask_fingerprint and self.ask_fingerprint in present:
            return True
        return bool(self.fingerprints) and self.fingerprints[0] in present

    def _with_ask(self, fingerprints: tuple[str, ...]) -> tuple[str, ...]:
        return (*fingerprints, self.ask_fingerprint) if self.ask_fingerprint else fingerprints

    @property
    def lookup_fingerprints(self) -> tuple[str, ...]:
        """Every hash ``known_in`` may test."""
        return self._with_ask(self.fingerprints)

    @property
    def on_write(self) -> tuple[str, ...]:
        """What to register ``proposed`` when this item's draft is written."""
        return self._with_ask(self.proposed_fingerprints)

    @property
    def on_decline(self) -> tuple[str, ...]:
        """What to register ``declined`` when the model says no."""
        return self._with_ask(self.fingerprints)

    @property
    def anchor(self) -> tuple[int, str]:
        return _ask_key(self.ask_created_at, self.ask_id)


def _ask_sort_key(row: Mapping[str, Any]) -> tuple[int, str]:
    return _ask_key(str(row["created_at"]), str(row["id"]))


def clarified_signals(
    scan_rows: Sequence[Mapping[str, Any]],
    projections: Mapping[str, Mapping[str, Any]],
    *,
    notebook_id: str,
    answers_with_memory: AbstractSet[str] = frozenset(),
) -> list[SignalItem]:
    """Clarified-class items from the ascending scan (oldest ask first).

    An ask is a signal when its answer's persisted intent carries at least one
    clarification row with a non-blank answer. Asks whose answer is unknown to
    ``projections`` (not the owner's) or already has a memory of the owner's
    are dropped (design §4.2 step 4).
    """
    items: list[SignalItem] = []
    for row in sorted(scan_rows, key=_ask_sort_key):
        answer_id = str(row.get("answer_id") or "")
        projection = projections.get(answer_id)
        if projection is None or answer_id in answers_with_memory:
            continue
        pairs = tuple(
            (as_text(entry.get("question")), as_text(entry.get("answer")))
            for entry in projection.get("clarification_answers") or ()
            if isinstance(entry, Mapping) and as_text(entry.get("answer"))
        )
        if not pairs:
            continue
        fingerprint, registrable = clarified_fingerprint(notebook_id, pairs)
        items.append(SignalItem(
            signal=SIGNAL_CLARIFIED,
            ask_id=str(row["id"]),
            ask_created_at=str(row["created_at"]),
            question=str(row.get("question") or ""),
            answer_id=answer_id,
            conversation_id=str(row.get("conversation_id") or ""),
            mode=str(row.get("mode") or ""),
            evidence_level=str(projection.get("evidence_level") or ""),
            excerpt="",
            fingerprints=(fingerprint,) if fingerprint else (),
            proposed_fingerprints=(fingerprint,) if fingerprint and registrable else (),
            clarifications=pairs,
            ask_fingerprint=clarified_ask_fingerprint(notebook_id, str(row["id"])),
        ))
    return items


def _choose_answer(
    members: Sequence[Mapping[str, Any]],
    projections: Mapping[str, Mapping[str, Any]],
    feedback: Mapping[str, str],
) -> Mapping[str, Any] | None:
    """The cluster member whose answer becomes the suggestion's source.

    Only grounded answers qualify (G6). Among them, one the owner's latest
    feedback rated useful wins; then the most recent. ``None`` when no member
    has a grounded answer (the cluster is then no signal at all).
    """
    grounded = [
        member for member in members
        if (projections.get(str(member.get("answer_id") or "")) or {}).get("evidence_level")
        == GROUNDED_EVIDENCE_LEVEL
    ]
    if not grounded:
        return None
    return max(
        grounded,
        key=lambda member: (
            feedback.get(str(member["answer_id"])) == RATING_USEFUL,
            _ask_sort_key(member),
        ),
    )


def repeated_signals(
    window_rows: Sequence[Mapping[str, Any]],
    cursor: SignalCursor,
    projections: Mapping[str, Mapping[str, Any]],
    feedback: Mapping[str, str],
    *,
    notebook_id: str,
    answers_with_memory: AbstractSet[str] = frozenset(),
) -> list[SignalItem]:
    """Repeated-class items from the descending window (any order is accepted).

    A cluster is a signal when it has at least ``REPEATED_MIN_CLUSTER_SIZE``
    questions, at least one of them after ``cursor`` (an old cluster is not
    re-evaluated every run), none of its answers already has a memory of the
    owner's, and at least one answer is grounded (see ``_choose_answer``).
    Items come out ordered by the earliest post-cursor ask of the cluster.
    """
    rows = sorted(window_rows, key=_ask_sort_key)
    keyword_sets = [question_keywords(str(row.get("question") or "")) for row in rows]
    items: list[SignalItem] = []
    for indices in leader_clusters(keyword_sets):
        members = [rows[index] for index in indices]
        if len(members) < REPEATED_MIN_CLUSTER_SIZE:
            continue
        fresh = [m for m in members if cursor.admits(str(m["created_at"]), str(m["id"]))]
        if not fresh:
            continue
        answer_ids = [str(m.get("answer_id") or "") for m in members]
        if any(answer_id in answers_with_memory for answer_id in answer_ids if answer_id):
            continue
        chosen = _choose_answer(members, projections, feedback)
        if chosen is None:
            continue
        projection = projections[str(chosen["answer_id"])]
        questions = [str(m.get("question") or "") for m in members]
        fingerprints = repeated_member_fingerprints(notebook_id, questions)
        items.append(SignalItem(
            signal=SIGNAL_REPEATED,
            ask_id=str(fresh[0]["id"]),
            ask_created_at=str(fresh[0]["created_at"]),
            question=str(chosen.get("question") or ""),
            answer_id=str(chosen["answer_id"]),
            conversation_id=str(chosen.get("conversation_id") or ""),
            mode=str(chosen.get("mode") or ""),
            evidence_level=str(projection.get("evidence_level") or ""),
            excerpt=str(projection.get("excerpt") or ""),
            fingerprints=fingerprints,
            proposed_fingerprints=fingerprints,
            questions=tuple(questions[-REPEATED_QUESTIONS_SHOWN:]),
            answer_ids=tuple(dict.fromkeys(
                answer_id for answer_id in answer_ids
                if answer_id and (projections.get(answer_id) or {}).get("evidence_level")
                == GROUNDED_EVIDENCE_LEVEL
            )),
            cluster_size=len(members),
        ))
    return sorted(items, key=lambda item: item.anchor)


def _answer_could_precede_question(previous: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    """A necessary ordering check, not proof of browser delivery/read receipt.

    Answer created_at is stamped at save entry, not at Ask submission. An
    answer stamped later cannot have been corrected by an already asked
    question. Missing/invalid legacy stamps cannot establish this ordering.
    """
    previous_key = utc_microsecond_key(previous.get("created_at"))
    question_key = utc_microsecond_key(row.get("created_at"))
    return previous_key is not None and question_key is not None and previous_key <= question_key


def corrected_signals(
    scan_rows: Sequence[Mapping[str, Any]],
    projections: Mapping[str, Mapping[str, Any]],
    *,
    notebook_id: str,
) -> list[SignalItem]:
    """Adjacent turn pairs offered for model classification, not auto-judged
    corrections. Both answers must survive the owner-bound projection read.
    Neither answer's manual-save uniqueness slot is consumed by this signal.
    """
    items: list[SignalItem] = []
    for row in sorted(scan_rows, key=_ask_sort_key):
        answer_id = str(row.get("answer_id") or "")
        previous_id = str(row.get("previous_answer_id") or "")
        current, previous = projections.get(answer_id), projections.get(previous_id)
        conversation_id = str(row.get("conversation_id") or "")
        if (current is None or previous is None
                or current.get("conversation_id") != conversation_id
                or previous.get("conversation_id") != conversation_id
                or not _answer_could_precede_question(previous, row)):
            continue
        question = str(row.get("question") or "")
        material = "|".join((SIGNAL_CORRECTED, notebook_id,
                             " ".join(sorted(question_keywords(question))), previous_id))
        fingerprint = hashlib.sha256(material.encode("utf-8")).hexdigest()
        items.append(SignalItem(
            signal=SIGNAL_CORRECTED, ask_id=str(row["id"]),
            ask_created_at=str(row["created_at"]), question=question,
            answer_id=answer_id, conversation_id=conversation_id,
            mode=str(row.get("mode") or ""),
            evidence_level=str(current.get("evidence_level") or ""),
            excerpt=str(current.get("excerpt") or ""),
            previous_excerpt=str(previous.get("excerpt") or ""),
            fingerprints=(fingerprint,), proposed_fingerprints=(fingerprint,),
            answer_ids=(previous_id, answer_id),
        ))
    return items


def select_items(
    clarified: Sequence[SignalItem],
    repeated: Sequence[SignalItem],
    *,
    enabled_signals: Collection[str],
    cap: int,
    scan_end: SignalCursor | None = None,
    corrected: Sequence[SignalItem] = (),
) -> tuple[list[SignalItem], SignalCursor | None]:
    """Pick at most ``cap`` items for the model and decide how far the cursor
    may move.

    * Classes whose own switch is off are removed first (G26); a disabled
      class is not a signal, so the cursor still passes its asks.
    * Clarified/corrected items share ascending ask order before repeated
      items (cluster anchor order); the list is cut to ``cap``; ids ``i1..iN`` are assigned
      to what remains.
    * ``cursor_end`` never passes ``scan_end`` -- the common ascending scan
      bound. If the cap cuts off an earlier ascending item, it stops BEFORE
      that item (inclusive on that ask, so the next scan starts with it).
      It is ``None`` when there is nothing to advance to (empty scan). The
      newest ask of the descending repeated window is never used.
    """
    clarified_pool = (
        sorted(clarified, key=lambda item: item.anchor)
        if SIGNAL_CLARIFIED in enabled_signals else []
    )
    corrected_pool = list(corrected) if SIGNAL_CORRECTED in enabled_signals else []
    ascending_pool = sorted([*clarified_pool, *corrected_pool], key=lambda item: item.anchor)
    repeated_pool = (
        sorted(repeated, key=lambda item: item.anchor)
        if SIGNAL_REPEATED in enabled_signals else []
    )
    limit = max(0, int(cap))
    chosen = [*ascending_pool, *repeated_pool][:limit]
    items = [
        replace(item, item_id=f"{ITEM_ID_PREFIX}{number}")
        for number, item in enumerate(chosen, start=1)
    ]
    cursor_end = scan_end
    if len(ascending_pool) > limit:
        first_cut = ascending_pool[limit]
        cursor_end = earlier_cursor(
            scan_end, SignalCursor(first_cut.ask_created_at, first_cut.ask_id, inclusive=True)
        )
    return items, cursor_end


# ------------------------------------------------------------------ rendering
@dataclass(frozen=True)
class RenderLimits:
    """Character limits of the model input; the ``AUTO_MEMORY_SUGGEST_*``
    Settings fields are their only source."""

    question_chars: int
    clarification_chars: int
    excerpt_chars: int
    input_chars: int

    @classmethod
    def from_settings(cls, settings: Any) -> "RenderLimits":
        return cls(
            question_chars=int(settings.auto_memory_suggest_question_max_chars),
            clarification_chars=int(settings.auto_memory_suggest_clarification_max_chars),
            excerpt_chars=int(settings.auto_memory_suggest_answer_excerpt_max_chars),
            input_chars=int(settings.auto_memory_suggest_input_max_chars),
        )


def _clip(text: object, limit: int) -> str:
    """One line of data text: whitespace collapsed, ``===`` runs shortened (so
    a question cannot spell the prompt's closing delimiter), cut to ``limit``."""
    flat = _EQUALS_RUN.sub("==", " ".join(str(text or "").split()))
    if limit <= 0:
        return ""
    return flat if len(flat) <= limit else flat[: max(0, limit - 1)] + "…"


def _item_text(
    item: SignalItem,
    neighbors: Sequence[Mapping[str, Any]],
    limits: RenderLimits,
    scale: float,
) -> str:
    question = max(1, int(limits.question_chars * scale))
    clarification = max(1, int(limits.clarification_chars * scale))
    excerpt = max(1, int(limits.excerpt_chars * scale))
    lines = [f"[{item.item_id}] signal={item.signal}"]
    if item.signal == SIGNAL_CLARIFIED:
        lines.append(f"question: {_clip(item.question, question)}")
        lines.append("clarification Q/A:")
        lines += [
            f"- Q: {_clip(q, clarification)} | A: {_clip(a, clarification)}"
            for q, a in item.clarifications
        ]
    elif item.signal == SIGNAL_CORRECTED:
        lines.append(f"previous answer excerpt: {_clip(item.previous_excerpt, excerpt)}")
        lines.append(f"current question: {_clip(item.question, question)}")
        lines.append(f"current answer excerpt: {_clip(item.excerpt, excerpt)}")
    else:
        lines.append(f"asked {item.cluster_size} similar questions, latest:")
        lines += [f"- {_clip(q, question)}" for q in item.questions]
        lines.append(f"answer excerpt: {_clip(item.excerpt, excerpt)}")
    if neighbors:
        lines.append("existing memories (status | id | title):")
        lines += [
            f"- {_clip(n.get('status'), 20)} | {_clip(n.get('id'), 80)} | "
            f"{_clip(n.get('title'), MEMORY_TITLE_MAX_CHARS)}"
            for n in neighbors
        ]
    return "\n".join(lines)


def render_items(
    items: Sequence[SignalItem],
    neighbors: Mapping[str, Sequence[Mapping[str, Any]]],
    limits: RenderLimits,
) -> str:
    """The items block of the prompt (data only; the caller wraps it in
    ``memory_suggest_prompt``). ``neighbors`` maps ``item_id`` to the store's
    ``[{id, title, status, side, score}]``.

    Every item is rendered; the total stays within ``limits.input_chars``.
    Each item gets an equal share of the budget; an item over its share is
    re-rendered with the per-field limits scaled down (the cursor must not
    depend on what fits), and as a last resort cut at its share.
    """
    if not items:
        return ""
    separator = "\n\n"
    share = max(1, (limits.input_chars - len(separator) * (len(items) - 1)) // len(items))
    blocks: list[str] = []
    for item in items:
        nearby = neighbors.get(item.item_id, ())
        text = ""
        for scale in (1.0, 0.75, 0.5, 0.35, 0.25, 0.15, 0.1, 0.05):
            text = _item_text(item, nearby, limits, scale)
            if len(text) <= share:
                break
        blocks.append(text[:share])
    return separator.join(blocks)


def neighbor_id_sets(
    neighbors: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[frozenset[str], frozenset[str]]:
    """``(served, confirmed)`` neighbour memory ids across all items -- the
    two sets ``parse_suggest_reply`` validates ``duplicate_of`` / ``refines``
    against."""
    served: set[str] = set()
    confirmed: set[str] = set()
    for rows in neighbors.values():
        for row in rows:
            memory_id = str(row.get("id") or "")
            if not memory_id:
                continue
            served.add(memory_id)
            if row.get("status") == "confirmed":
                confirmed.add(memory_id)
    return frozenset(served), frozenset(confirmed)


# ------------------------------------------------------------------ parsing
class SuggestReplyRejected(ValueError):
    """The whole reply is unusable (design §4.2 step 7: a malformed reply is
    dropped as one batch and the run ends ``model_rejected``). ``reason`` is a
    stable code, never model text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SuggestionDecision:
    """The model's verdict on one served item.

    ``answered`` is False for an item the reply never mentioned: it counts as
    a "no" (``keep=False``) so it lands in the negative cache like any other.
    """

    item_id: str
    keep: bool
    kind: str = ""
    title: str = ""
    content: str = ""
    tags: tuple[str, ...] = ()
    duplicate_of: str | None = None
    refines: str | None = None
    answered: bool = True
    conflicts_with_sources: bool = False

    @property
    def writable(self) -> bool:
        """Kept and not a duplicate: a draft memory is written."""
        return self.keep and self.duplicate_of is None

    @property
    def declined(self) -> bool:
        """Goes to the negative cache: judged "no" or a duplicate."""
        return not self.writable


def _optional_id(raw: object, allowed: AbstractSet[str], reason: str) -> str | None:
    value = as_text(raw)
    if not value:
        return None
    if value not in allowed:
        raise SuggestReplyRejected(reason)
    return value


def _kept_fields(entry: Mapping[str, Any]) -> tuple[str, str, str, tuple[str, ...]]:
    kind = as_text(entry.get("kind"))
    if kind not in SUGGESTION_KINDS:
        raise SuggestReplyRejected("unknown_kind")
    title, content = as_text(entry.get("title")), as_text(entry.get("content"))
    if not title or not content:
        raise SuggestReplyRejected("blank_title_or_content")
    if len(title) > MEMORY_TITLE_MAX_CHARS:
        raise SuggestReplyRejected("title_too_long")
    if len(content) > SUGGESTION_CONTENT_MAX_CHARS:
        raise SuggestReplyRejected("content_too_long")
    raw_tags = as_text_list(entry.get("tags"))
    if len(raw_tags) > MEMORY_TAG_MAX_COUNT or any(
        len(tag) > MEMORY_TAG_MAX_CHARS for tag in raw_tags
    ):
        raise SuggestReplyRejected("tags_over_limit")
    try:
        tags = tuple(normalize_tags(raw_tags))
    except MemoryInputError:
        raise SuggestReplyRejected("tags_over_limit") from None
    return kind, title, content, tags


def parse_suggest_reply(
    payload: object,
    served_item_ids: AbstractSet[str],
    served_neighbor_ids: AbstractSet[str],
    confirmed_neighbor_ids: AbstractSet[str],
) -> dict[str, SuggestionDecision]:
    """Validate one reply into a decision per served item, or reject it all.

    ``payload`` is the parsed model object (``{"items": [...]}``). Whole-batch
    rejection (``SuggestReplyRejected``) for: a payload that is not an object
    with an ``items`` list, an entry that is not an object, an ``item_id``
    the server never served or one repeated, a non-boolean ``keep``, a kept
    entry with an unknown ``kind`` / blank or over-long ``title`` or
    ``content`` / more than ``MEMORY_TAG_MAX_COUNT`` tags or a tag over
    ``MEMORY_TAG_MAX_CHARS``, a ``duplicate_of`` that was not served as a
    neighbour, and a ``refines`` that is not a CONFIRMED neighbour served this
    run (G3). The same id checks run for ``keep=false`` entries; the content
    checks do not (their text is discarded).

    Every served item gets a decision; one the reply never mentions is
    ``keep=False, answered=False``. Entries are validated before anything is
    returned, so a bad entry anywhere discards the good ones too.
    """
    if not isinstance(payload, Mapping):
        raise SuggestReplyRejected("reply_not_an_object")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise SuggestReplyRejected("items_not_a_list")
    decisions: dict[str, SuggestionDecision] = {}
    for entry in raw_items:
        if not isinstance(entry, Mapping):
            raise SuggestReplyRejected("item_not_an_object")
        item_id = as_text(entry.get("item_id"))
        if item_id not in served_item_ids:
            raise SuggestReplyRejected("item_id_out_of_range")
        if item_id in decisions:
            raise SuggestReplyRejected("duplicate_item_id")
        keep = entry.get("keep")
        if not isinstance(keep, bool):
            raise SuggestReplyRejected("keep_not_a_boolean")
        conflicts = entry.get("conflicts_with_sources")
        if type(conflicts) is not bool:
            raise SuggestReplyRejected("conflicts_with_sources_not_a_boolean")
        duplicate_of = _optional_id(
            entry.get("duplicate_of"), served_neighbor_ids, "duplicate_of_not_served"
        )
        refines = _optional_id(
            entry.get("refines"), confirmed_neighbor_ids, "refines_not_confirmed_neighbor"
        )
        if keep:
            kind, title, content, tags = _kept_fields(entry)
            decisions[item_id] = SuggestionDecision(
                item_id, True, kind, title, content, tags, duplicate_of, refines,
                conflicts_with_sources=conflicts,
            )
        else:
            decisions[item_id] = SuggestionDecision(
                item_id, False, duplicate_of=duplicate_of, refines=refines,
                conflicts_with_sources=conflicts,
            )
    for item_id in sorted(set(served_item_ids) - set(decisions)):
        decisions[item_id] = SuggestionDecision(item_id, False, answered=False)
    return decisions
