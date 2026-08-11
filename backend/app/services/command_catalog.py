"""Command-manual catalog extraction — the pure layer (Plan C, v2).

A tool's *command reference* is the shape ordinary ingestion handles worst. The
600-char chunker splits `set_db` into description / Arguments / Examples, the
KG extractor turns a parameter table into free-floating claims, and the answer
that comes back is prose about a command rather than the command's contract.
Plan C ingests those manuals as structured entries instead — name, syntax,
arguments, defaults — and this module is everything in that pipeline that can
be decided without a database, a model call or a job:

    extraction_windows    how is a document cut into model-sized pieces?
    window_candidates     which names does this window itself put on offer?
    carry_candidates      which names does the previous window hand forward?
    window_needs_model    is this window worth a model call at all?
    extraction_slices     how is one window split across model calls?
    validate_entry        which parts of a model's answer survive grounding?
    assignment_coverage   how much of a slice's assignment came back at all?
    window_outcome        what does the job report about this window?

Every one of them is a pure function over data the caller already holds, so
the job + persistence layer (`catalog_job.py`) and the UI can be written and
tested against them without touching a model. The layering mirrors
`chunking.py` / `exact_lookup.py`: no IO here, ever.

**Why windows and not sections.** v1 decided, with layout rules alone, which
elements formed one command's section, and showed the model only those. Two
things were wrong with that, and only one of them was fixable. First, measured
on real manuals the grouping was simply inaccurate. Second — the structural
half — a rule that decides what the model is *allowed to see* fails closed: a
command whose section the rules never opened was not extracted badly, it was
never offered at all, and no downstream check can recover a command that was
never in a prompt. So the geometry is now trivial and lossless: the document is
packed, in document order, into `WINDOW_CHARS` windows with nothing dropped and
nothing overlapping, and the model is asked which commands each window
documents. A window that turns out to document more commands than one candidate
list can carry is SPLIT rather than truncated, for the same reason: windows are
never revisited, so a truncated list is not a command served later, it is a
command served nowhere. What survived the change untouched is the part C0
measured as working — the grounding machinery below, which decides what of the
model's answer is the manual's own words.

Calibration. The thresholds are measured, not guessed — a C0 spike ran real
OpenROAD command references (Apache-2.0) through the production model:

* a large section (~100 parameters) hits `finish_reason=length` at 8192 output
  tokens and the retry can come back with empty content, so **slicing is
  mandatory**, not an optimisation (`extraction_slices`);
* a served candidate list got the command name right 5/5, so "pick from this
  list" is the veto worth enforcing (`validate_entry`);
* dash fidelity has to be *checked*, not requested: the prompt asks for
  `-guide_file` and the model still returns `guide_file`, and a naive
  containment test passes it because the manual's own text contains
  `-guide_file` (`_check_arg_name`);
* MinerU's flattened tables recall as well as native markdown, so nothing here
  branches on the parser — the shapes are recognised from text.

Nothing in this module decides policy. Anomaly detection (a command-name veto
rate above `COMMAND_REJECT_ALERT_RATIO` means the model or the source went
wrong) is *reported* here as a ratio and *acted on* by the job layer.

Contract for the job layer: every model call built from an `ExtractionSlice`
MUST pass `response_validator` to `chat_json` (`app/services/model_provider.py`).
That argument is not optional plumbing — it is the sole admission ticket into
the content-addressed cache (gating BOTH whether a cached reply may be served
and whether this call's own reply may be written), the same gating the KG
extractors (`app/services/kg/extract.py`) already rely on. It has nothing to
do with retrying a malformed reply — that remedy is the job layer's own halving
logic in `catalog_job.py`, entirely local to that module. Skip
`response_validator` and a slice is not just uncached — every retry of that
slice silently repays the full model cost with no caching benefit at all.
"""
from __future__ import annotations

import re
import string
from dataclasses import dataclass, field
from typing import Any, Collection, Mapping, Sequence

from app.repositories.lexical_query import exact_probe_terms


# --------------------------------------------------------------------- bounds
# One window's text as the model will see it. Also the haystack every grounding
# check searches: validating against text the model was never shown would let a
# hallucination pass because it happens to appear in the part that was cut.
# Carried over unchanged from v1's per-section budget — the number was sized
# against the model's input/output budget, which the geometry change does not
# move.
WINDOW_CHARS = 12_000
# The candidate list rides in the prompt, so it is a constraint as much as a
# menu: every extra name weakens the one veto this layer has. 32, not v1's 16:
# a window is a slab of the document rather than one command's section, so
# several commands routinely share one, and a list that truncates before the
# last of them is a list that vetoes a real command out of existence.
MAX_CANDIDATES = 32
# When a window offers MORE names than that, the answer is to make the window
# smaller, not to cut the list. Windows do not overlap, so a truncated list is
# not "the 33rd command is served later" — it is served NOWHERE, in this run or
# any other, which is the same permanent, silent loss v1's discard branch had.
# Splitting is free of that: the pieces are real windows, every character still
# lands in exactly one of them, and each piece offers its own (shorter) list.
#
# The floor is where splitting stops being worth it. A window this small that
# STILL names more than 32 commands is not documentation whose commands got
# crowded out, it is a name list — an index page, a "see also" block, a summary
# table of every command in the tool. Splitting one of those buys nothing (each
# piece is still a name list) and costs a model call per piece, so the list is
# truncated there instead and the cut is DISCLOSED
# (`ExtractionWindow.candidates_overflowed`) rather than swallowed.
# WINDOW_CHARS // 16: at 750 characters, 33 names leaves ~22 characters per
# command, which no real command reference fits into.
WINDOW_SPLIT_FLOOR_CHARS = WINDOW_CHARS // 16
# How far back a split point may travel to land on whitespace. A cut is placed
# at a character budget, and a budget lands wherever it lands — including the
# middle of `global_placement` or of `-density`, which produces two windows
# neither of which contains the token, so the name is not a candidate in either
# and no grounding check can ever match it. Neither a command name nor a flag
# contains whitespace, so backing up to the nearest whitespace (a newline for
# preference — it keeps a table row or a usage line whole) is enough to
# guarantee every token lands complete in exactly one piece. Bounded, and
# best-effort: a 200-character run with no whitespace in it at all (a base64
# blob, a minified line) is cut where the budget said, because there is no
# boundary to find and refusing to cut would break the budget instead.
SPLIT_BOUNDARY_LOOKBACK_CHARS = 200
# C0: ~100 parameters overruns the output budget. 20 keeps a slice's answer
# comfortably inside it with room for syntax/description/examples on slice 0.
SLICE_PARAM_LIMIT = 20
# (v1's `MAX_SCAN_LINES = 200` line cap lived here and is GONE. It bounded the
# usage-line scan back when the unit was one command's section, where 200 lines
# was far more than a section could be. In v2 the unit is a `WINDOW_CHARS` slab
# of a document, so the character budget IS the bound and the line cap only ever
# subtracted from it — a single 300-line element (a flattened options table, a
# long code block) hid every command documented after line 200 from
# `window_candidates`, hence from `_dense_overflow`, hence from the split; the
# names were never served, and a name that is never served cannot be claimed.
# That is the same silent, permanent loss the whole geometry change exists to
# remove, so the scan reads every line of a window it is already only allowed
# 12,000 characters of.)
# Mirrors `lexical_query.identifier_terms`'s own length floor. Not imported:
# that constant is a private literal (`len(value) < 4`) inside a function
# body, not a name `lexical_query` exports, and this module's own gate is
# `_is_command_identifier`, layered on `exact_probe_terms` rather than on
# `identifier_terms` directly (see there) — duplicating one bound number is
# cheaper than reaching into another module's internals for it.
MIN_IDENTIFIER_CHARS = 4
# How many positional arguments a usage line may carry and still read as a
# usage line rather than a sentence (`set_dont_use lib_cells`).
MAX_USAGE_ARG_TOKENS = 4
# Diagnostics carried on a rejection, bounded so a job report cannot inherit a
# window's full text through its own failure records.
REJECT_WINDOW_CHARS = 200
MAX_REJECT_VALUE_CHARS = 120
# A window's rejection ledger is bounded too — a pathological entry (or a
# model that never stops inventing parameters) must not let a job report
# inherit an unbounded list through its own failure records. Overflow is
# counted, never silently dropped.
MAX_WINDOW_REJECTIONS = 24
# The job layer's circuit breaker reads this; this module only publishes the
# ratio. A veto rate alone can miss a bad run — an entry can pick the right
# command name and still invent every parameter — so the breaker reads both
# axes: reject-ratio above this ratio, OR args-keep-ratio (below) below its
# own, and only once MIN_WINDOWS_BEFORE_ALERT windows give the ratios a sample
# worth trusting.
COMMAND_REJECT_ALERT_RATIO = 0.20
ARGS_KEEP_ALERT_RATIO = 0.50
MIN_WINDOWS_BEFORE_ALERT = 10


# --------------------------------------------------------------------- shapes
# A flag in its original form. The leading dash is part of the token: dropping
# it is the model's single most common infidelity, and `-density` must not be
# found inside `--density` or `-density_scale`.
_FLAG_RE = re.compile(r"(?<![A-Za-z0-9_\-])-{1,2}[A-Za-z][A-Za-z0-9_]*")
# A command name at the start of a usage line: `global_placement`,
# `report.timing`. Separators are `_`/`.` only — a command is not spelled with
# hyphens, while `state-of-the-art` is.
_LEADING_IDENT_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[._][A-Za-z0-9]+)+")
_INLINE_CODE_RE = re.compile(r"`([^`\n]{1,120})`")
_SEPARATOR_RE = re.compile(r"[._\-]")
_DIGIT_RE = re.compile(r"[0-9]")
# A token that reads as a placeholder rather than an English word.
_ARGUMENT_TOKEN_RE = re.compile(r"[_*\[\]<>{}|/]|[0-9]")
# Sentence punctuation never terminates a usage line.
_SENTENCE_TAIL = ".,:;!?，。；：、"
# Fenced/structured code, as the markdown parser labels it. MinerU labels
# nothing this way, which is exactly why code evidence is a *preference* in
# `window_candidates` rather than a requirement.
_CODE_TYPES = frozenset({"code_block", "code"})
# One segment of a document's numbering: `2`, `A`, `S1`, `iv`-ish.
_NUMBERING_SEGMENT_RE = re.compile(r"[A-Za-z]?[0-9]+|[A-Za-z]")
# Token boundary for every grounding check. `-` belongs to the token: without
# it, a model that answered `density` would be "found" inside the manual's
# `-density`, which is precisely the dash infidelity C0 measured.
_BOUNDARY_CHARS = frozenset(string.ascii_letters + string.digits + "_-")
# Elements are joined by a newline when a window's text is assembled. It is a
# separator, not content: `set_db` and the `Arguments` heading under it must
# not run together on one line, or every line-oriented scan below (usage
# lines, flag lines, a slice's own parameter lines) reads one long line.
_ELEMENT_JOIN = "\n"
# The whitespace an element is normalised by before it is packed — PINNED, character
# for character, to the set the store's own `TRIM`/`BTRIM` aggregate strips
# (`CatalogStorePort.source_text_stats`, `preview_elements.full_chars`).
#
# Not `str.strip()`, which strips every Unicode space: the cost preview
# publishes a window count as a LOWER bound, and that bound is only sound while
# the two sides count the same characters. Strip more here than SQL does and a
# document padded with U+3000 (the ideographic space Chinese typesetting is
# full of) or NBSP has a smaller real total than the SQL sum reports — the
# arithmetic floor then sits ABOVE the truth, which is the one direction it may
# not err in. Aligning to the narrower, portable set is the direction that
# keeps both backends and this module on one definition; the cost is that a
# U+3000 run counts as content and occupies window budget, which is
# conservative (it can only make the estimate smaller than reality) and is what
# a CJK manual's spacing arguably is anyway.
#
# `_split_point` deliberately keeps Unicode-wide `str.isspace()`: that is
# choosing WHERE to cut, not counting how much there is, and a U+3000 is a
# perfectly good token boundary.
# Public because it is a CROSS-MODULE contract, not an implementation detail:
# the packer normalises by it, the cost preview judges "was this row truncated"
# by it, and the store's SQL aggregate strips exactly it. Three readers, one
# definition — a second spelling anywhere breaks the bound below.
STRIP_CHARS = " \t\n\r"


# =========================================================== data definitions
@dataclass(frozen=True)
class WindowElement:
    """One source element (or one piece of an oversized one), reduced to what
    this layer reads.

    Deliberately the same field names `source_elements_for_chunking` already
    returns, so the job layer feeds this module the rows it already fetches for
    chunking. `text` is the piece that landed in *this* window, not necessarily
    the element's whole text: an element larger than `WINDOW_CHARS` is split
    across consecutive windows and each piece keeps the element's own id, so a
    citation anchor still points at the element a person can open.
    """

    id: str
    element_type: str
    text: str
    section_path: str = ""


@dataclass(frozen=True)
class ExtractionWindow:
    """One model call's slab of the document.

    `text` is the authority: it is what a prompt shows the model and what every
    grounding check searches, and it holds exactly the pieces listed in
    `elements` — `text == "\\n".join(e.text for e in elements)` is an invariant,
    because a grounding check that searched text the model never saw would pass
    hallucinations.

    `provenance` is a display label only (the candidate row's section column),
    inherited from the last heading at or before this window. It deliberately
    takes part in no decision: it is a best-effort breadcrumb, and a window
    boundary falls wherever the character budget put it, not where a section
    starts.

    `candidates_overflowed` is how many candidate names this window has BEYOND
    what its list can carry — normally 0, because `extraction_windows` splits a
    window that offers too many rather than truncating it. It is non-zero only
    for a window that reached `WINDOW_SPLIT_FLOOR_CHARS` and is still that
    dense (a bare index of command names), where truncating is the deliberate
    choice. It exists because that truncation is the one remaining place a
    command can be dropped without any downstream trace: an entry can only be
    wrong about a name it was served, so a name that was never served produces
    no rejection, no ratio movement and no report line. Carried to the job
    layer's run ledger so the number is at least visible.
    """

    ordinal: int
    elements: tuple[WindowElement, ...]
    text: str
    provenance: str = ""
    candidates_overflowed: int = 0

    @property
    def element_ids(self) -> tuple[str, ...]:
        """Contributing element ids, in appearance order, deduplicated.

        Deduplication is defensive rather than load-bearing: the packer never
        puts two pieces of one element in the same window (a split always ends
        the window it overflowed). It costs nothing and it keeps the anchor
        list honest if that ever changes.
        """
        return tuple(dict.fromkeys(element.id for element in self.elements))


@dataclass(frozen=True)
class ExtractionSlice:
    """One model call's worth of a window.

    Pure data: the job layer turns `param_names` into "extract ONLY these
    parameters", `include_overview` into "also give me syntax / description /
    examples", and `text_window` into the prompt's own view of the source —
    which is the WHOLE window, always.

    That last one used to be a narrower view (a short overview head plus the
    lines mentioning this slice's own parameters, capped at 4000 characters),
    and the narrowing was correct for v1 and systematically wrong for v2. v1's
    unit was one command's section, so a head-plus-parameter-lines view held
    the command's name, its syntax block and the parameters being asked about
    — everything a slice could legitimately answer with. v2's unit is a 12k
    slab of a document that routinely documents thirty commands, and the same
    head shows only the FIRST of them: every later command's name is served in
    the candidate list, asked about by the prompt, and then hidden from the
    text the model is given, which is the one failure this whole module is
    built to avoid (a name that is offered but not shown produces either
    nothing or a fabrication). Carrying the whole window per slice costs at
    most `WINDOW_CHARS` characters per call on a multi-slice window, and that
    cost is accepted deliberately.

    `text_window` therefore no longer narrows anything, and `validate_entry`
    still never reads it: grounding always searches `window.text` directly, so
    the two cannot drift apart even if a caller builds a slice by hand.
    """

    index: int
    total: int
    param_names: tuple[str, ...] = ()
    include_overview: bool = True
    text_window: str = ""


@dataclass(frozen=True)
class ValidatedArg:
    name: str
    required: bool = False
    description: str = ""
    default: str = ""


@dataclass(frozen=True)
class ValidatedEntry:
    """The part of a model's answer that survived grounding.

    One entry is one command. A window's reply carries a LIST of these, because
    a window is a slab of the document rather than one command's section — the
    job layer validates each element of that list through `validate_entry`
    separately, so one bad entry vetoes itself and nothing else.

    `relayed` says this entry's `suspect_related=False` was granted by the
    RELAY rather than earned in this window: the command is claimable here only
    because `carry_candidates` handed the name over, and this window holds no
    heading and no usage line for it. It exists for the cross-window merge. A
    command's parameters routinely span windows, so the same command produces
    one entry per window and the merge folds them into one row; `suspect` is
    merged with AND (any window that documents the command properly clears the
    mark), and a relayed entry's False is not evidence of that — it is the
    absence of evidence. Merging it with AND would let a continuation window
    holding nothing but a parameter table erase the warning the window that
    actually mentioned the command in passing had earned. The job layer
    therefore lets only a NON-relayed entry clear the mark.

    `anchor_element_ids` is reserved: `description` / `examples` are prose and
    cannot be checked verbatim, so the job layer binds them to element ids
    instead. Leaving the seat empty here keeps that a schema decision rather
    than a later shape change.
    """

    command_name: str
    syntax: str = ""
    description: str = ""
    args: tuple[ValidatedArg, ...] = ()
    examples: tuple[str, ...] = ()
    suspect_related: bool = False
    relayed: bool = False
    anchor_element_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Rejection:
    """Everything needed to explain one dropped value, bounded."""

    field: str  # command_name | args | arg | required | syntax | default | examples
    value: str
    reason: str
    window: str = ""


@dataclass(frozen=True)
class ValidationStats:
    command_rejected: bool = False
    args_seen: int = 0
    args_kept: int = 0
    syntax_seen: bool = False
    syntax_kept: bool = False
    defaults_seen: int = 0
    defaults_kept: int = 0


@dataclass(frozen=True)
class AssignmentCoverage:
    """How much of ONE slice's assignment its answers actually addressed.

    Counted over the raw model payloads rather than over accepted entries,
    because this is a different question from grounding: `covered` asks "did
    the model even try to answer this parameter", and an attempt that was then
    vetoed (the dropped dash) is still an attempt. Splitting the two keeps a
    single mistake from being charged twice — once as a returned arg that
    failed grounding, once as an assigned parameter that never came back.

    `returned` is every well-formed arg the model produced, including ones
    belonging to no assigned parameter at all. It is what tells "the answer
    was SHORT" (a truncation-shaped failure, which asking for fewer parameters
    can fix) from "the answer was WRONG" (which it cannot).
    """

    assigned: int = 0
    returned: int = 0
    covered: int = 0
    uncovered: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValidationResult:
    accepted: bool
    entry: ValidatedEntry | None = None
    rejections: tuple[Rejection, ...] = ()
    stats: ValidationStats = ValidationStats()


@dataclass(frozen=True)
class WindowOutcome:
    """The per-window ledger a job report renders directly.

    `rejections` is capped at `MAX_WINDOW_REJECTIONS` — a pathological entry
    (or a model that never stops inventing parameters) must not let a job
    report inherit an unbounded list through its own failure records.
    `rejections_overflow` counts what got cut, so the cap never reads as
    "everything after entry N was clean" when it was actually "we stopped
    counting".

    `uncovered_args` is the same idea one level down: the parameters this
    window's slices were ASSIGNED and never got an answer for. It is bounded
    like `rejections` is, with the true total in `args_uncovered` — which is
    also what keeps `args_keep_ratio` honest (see `catalog_stats`).
    """

    ordinal: int
    provenance: str = ""
    candidates: tuple[str, ...] = ()
    accepted_names: tuple[str, ...] = ()
    uncovered_candidates: tuple[str, ...] = ()
    rejections: tuple[Rejection, ...] = ()
    rejections_overflow: int = 0
    entries_seen: int = 0
    command_rejects: int = 0
    args_seen: int = 0
    args_kept: int = 0
    uncovered_args: tuple[str, ...] = ()
    args_uncovered: int = 0
    # Straight from the window (see `ExtractionWindow.candidates_overflowed`).
    # Carried on the outcome so the run ledger can total it without holding on
    # to the windows themselves.
    candidates_overflowed: int = 0


@dataclass(frozen=True)
class CatalogStats:
    """Run-level totals. The ratios are published; the verdict belongs to the
    job layer.

    Two independent axes, because either alone can miss a bad run: an entry
    can pick the right command name (a clean `command_reject_ratio`) and
    still invent every parameter, and a run that mostly rejects args can
    still be picking legitimate command names. The circuit breaker should
    read both — command-name veto rate above `COMMAND_REJECT_ALERT_RATIO`
    (0.20) OR args-keep rate below `ARGS_KEEP_ALERT_RATIO` (0.50) — and only
    once `MIN_WINDOWS_BEFORE_ALERT` (10) windows have been processed, since
    neither ratio means anything on a sample of one or two.
    """

    windows: int = 0
    entries_seen: int = 0
    command_rejects: int = 0
    command_reject_ratio: float = 0.0
    args_seen: int = 0
    args_kept: int = 0
    args_uncovered: int = 0
    args_keep_ratio: float = 0.0


# ================================================================= primitives
def _is_numbering(term: str) -> bool:
    """True for `2.1`, `A.1.2`, `B-3` — a document's numbering, not a name.

    The wide identifier regex cannot tell them apart, and papers are full of
    them: `A.1.2 Notation` yields `A.1.2`, which has an ASCII letter, a dot and
    five characters, so every other gate lets it through and a survey paper
    starts offering command candidates.

    A term is numbering when it splits into two or more segments, contains a
    digit, and *every* segment is either a bare number, a number with one
    leading letter, or a single letter. The digit requirement is what keeps
    `set_db` and `config.yaml` out; the per-segment test is what keeps `GPT-4`
    in (`GPT` is neither). `v1-2` is classified as numbering and that is
    accepted: no command is named that, and erring toward "not a command" is
    the safe direction for a list whose whole job is to be a veto.
    """
    segments = [seg for seg in _SEPARATOR_RE.split(term) if seg]
    if len(segments) < 2 or not _DIGIT_RE.search(term):
        return False
    return all(_NUMBERING_SEGMENT_RE.fullmatch(seg) for seg in segments)


def _is_command_identifier(term: str) -> bool:
    """Whether `term` on its own is a plausible command name.

    Delegates the identifier definition to `exact_probe_terms` — the repository
    has exactly one narrow identifier gate and this is it (`_`/`.`-joined, or
    hyphen-joined with a digit; ASCII letter; >= 4 chars). Requiring the probe
    view to return the term *whole* rejects the case where only a fragment
    qualifies. The numbering filter above is the one rule layered on top.
    """
    if len(term) < MIN_IDENTIFIER_CHARS:
        return False
    return exact_probe_terms(term) == [term] and not _is_numbering(term)


def _identifiers(text: str) -> list[str]:
    """Command-shaped names in `text`, in appearance order.

    Flags are stripped *before* the scan rather than filtered after: the
    identifier regex sees `-timing_driven` as the bare name `timing_driven`, so
    an untreated syntax block would hand thirty parameter names to the
    candidate list and crowd out the names that matter.
    """
    stripped = _FLAG_RE.sub(" ", text or "")
    return [
        term for term in exact_probe_terms(stripped) if not _is_numbering(term)
    ]


def _usage_identifier(line: str) -> str:
    """The command a usage line invokes, or "".

    Three accepted forms, measured against the OpenROAD corpus:

    * **alone on the line** — `global_placement`, with its bracketed options
      following;
    * **carrying flags** — `get_global_placement_uniform_density -pad_left`;
    * **carrying positional arguments** — `set_dont_use lib_cells`,
      `set_macro_extension extension`. Omitting this third form is not a corner
      case: it lost five real commands in one corpus, because a flagless
      command is exactly the kind that gets documented in a single line.

    The second and third forms are the ones that could swallow prose, so both
    share the same two guards: the name must be snake_case (no English
    sentence opens with a `_`-joined token, whereas `Fig.2 shows the layout`
    and `config.yaml is the file` are dotted and would otherwise qualify), and
    the line must not end in sentence punctuation (English or Chinese). Those
    two checks have to run *before* the flag-carrying form is accepted, not
    after: a naive `has a flag -> accept` short-circuit lets `Fig.2 shows the
    -density sweep.` and `global_placement accepts -density values.` both
    read as usage lines the moment either sentence happens to mention a flag,
    and every one of those is a candidate name a hallucinated entry could then
    legally claim. Positional arguments get one more condition on top (few,
    placeholder-shaped tokens): `global_placement performs the placement`
    still needs to fail there too, since it has neither a flag nor a
    placeholder-shaped rest.

    Trailing continuation backslashes are ignored so `set_db \\` still reads as
    a bare invocation.
    """
    stripped = (line or "").strip()
    match = _LEADING_IDENT_RE.match(stripped)
    if not match:
        return ""
    term = match.group(0)
    if not _is_command_identifier(term):
        return ""
    rest = stripped[match.end():].strip().strip("\\").strip()
    if not rest:
        return term
    if "_" not in term or rest[-1] in _SENTENCE_TAIL:
        return ""
    if _FLAG_RE.search(stripped):
        return term
    tokens = rest.split()
    if not tokens or len(tokens) > MAX_USAGE_ARG_TOKENS:
        return ""
    if len(tokens) == 1 or any(_ARGUMENT_TOKEN_RE.search(tok) for tok in tokens):
        return term
    return ""


def _usage_identifiers(text: str) -> list[str]:
    """Every command invoked by a usage line in `text`, deduplicated in order.

    Deliberately parser-blind. The spec's shape is "the first code block's
    first line", but MinerU emits no code_block element type at all — a PDF
    manual's usage line arrives as an ordinary paragraph — and C0 measured that
    flattened MinerU text extracts as well as native markdown. Recognising the
    *line* rather than the *element type* is what keeps that true, and
    `_usage_identifier` is strict enough to survive the wider haystack.

    EVERY line, with no cap. v1 stopped at 200 because its unit was one
    command's section; a v2 window is a slab of a document, and one element can
    hold 300 lines of flattened options table on its own. Stopping early there
    does not save work worth having — the window is already capped at
    `WINDOW_CHARS`, so this is bounded either way — while it silently hides
    every command documented past the cut from `window_candidates`, from
    `_dense_overflow`, and therefore from the split that exists to keep them
    claimable. A name that is never served can never be claimed and never
    produces a rejection, a ratio movement or a report line: the exact silent
    loss the v2 geometry was built to remove, reintroduced one layer down.
    """
    found: dict[str, None] = {}
    for line in (text or "").splitlines():
        term = _usage_identifier(line)
        if term:
            found[term] = None
    return list(found)


def _token_present(needle: str, haystack: str) -> bool:
    """Case-sensitive containment at identifier boundaries.

    Substring containment is not good enough in either direction: `set_d` would
    be "found" in `set_db` (a hallucinated near-miss silently accepted), and
    `density` would be "found" in `-density` (the dash the model dropped
    silently restored). Treating `-` as part of the token closes both.
    """
    if not needle:
        return False
    start = haystack.find(needle)
    while start != -1:
        end = start + len(needle)
        left_ok = start == 0 or haystack[start - 1] not in _BOUNDARY_CHARS
        right_ok = end == len(haystack) or haystack[end] not in _BOUNDARY_CHARS
        if left_ok and right_ok:
            return True
        start = haystack.find(needle, start + 1)
    return False


def normalize_syntax(text: str) -> str:
    """Collapse a syntax block to a single comparable line.

    A manual wraps its usage across a dozen backslash-continued lines and the
    model answers with one line, or with the wrapping preserved, or with the
    backslashes dropped. All three mean the same syntax, so continuations are
    erased and whitespace runs collapsed on *both* sides before comparison —
    the check stays "contiguous copy of the source", it just stops failing on
    line breaks.
    """
    without_continuations = re.sub(r"\\(?=\s|$)", " ", text or "")
    return re.sub(r"\s+", " ", without_continuations).strip()


def parameter_names(text: str) -> list[str]:
    """Flag-shaped parameter names in original form, in appearance order.

    Original form means the leading dash is kept: it is what a prompt must ask
    for and what `validate_entry` matches against. The left-boundary guard is
    what keeps prose out — `state-of-the-art`, `non-virtual` and a markdown
    `- bullet` all fail to produce a name, while `[-density target_density]`
    and a table's `` `-density` `` both yield `-density`.
    """
    found: dict[str, None] = {}
    for match in _FLAG_RE.finditer(text or ""):
        found[match.group(0)] = None
    return list(found)


# =================================================================== 1. windows
def _breadcrumb(raw: Mapping[str, Any], title: str) -> str:
    """A heading's display path: its `" > "` breadcrumb, or its own title.

    The fallback is `build_chunks`' own: markdown parsing stores a full path on
    every heading, MinerU stores none, and a bare title is the honest
    degradation rather than a second code path.
    """
    crumbs = [
        seg.strip()
        for seg in str(raw.get("section_path") or "").split(" > ")
        if seg.strip()
    ]
    return " > ".join(crumbs) or title


def _window_elements(rows: Sequence[Mapping[str, Any]]) -> list[WindowElement]:
    """Normalise the caller's rows, in document order.

    Elements whose text is blank are dropped, and that is not a content loss:
    they contribute no characters to any window and nothing in this module can
    ground against, count or cite them. Everything else is kept — the packer
    below has no discard branch at all.

    "Blank" means blank by ``STRIP_CHARS``, the same set the store aggregate
    strips — so an element of nothing but U+3000 is CONTENT here, exactly as
    SQL counted it. See that constant: the preview's lower bound is only sound
    while both sides agree on which characters exist.
    """
    elements: list[WindowElement] = []
    for raw in rows or ():
        # `STRIP_CHARS`, never a bare `.strip()`: see that constant for why the
        # set has to be the store aggregate's, character for character.
        text = str(raw.get("text") or "").strip(STRIP_CHARS)
        if not text:
            continue
        etype = str(raw.get("element_type") or raw.get("type") or "").lower()
        elements.append(
            WindowElement(
                id=str(raw.get("id") or ""),
                element_type=etype,
                text=text,
                section_path=_breadcrumb(raw, text) if etype == "heading" else "",
            )
        )
    return elements


def _piece(element: WindowElement, text: str) -> WindowElement:
    """One piece of `element`, keeping its identity (id, type, breadcrumb)."""
    return WindowElement(
        id=element.id,
        element_type=element.element_type,
        text=text,
        section_path=element.section_path,
    )


def _split_point(
    text: str,
    target: int,
    *,
    lookback: int = SPLIT_BOUNDARY_LOOKBACK_CHARS,
) -> int:
    """Where to cut `text` near `target` so the cut lands between tokens.

    Returns an index in ``[1, target]`` (or ``len(text)`` when the whole string
    fits), so the caller always makes progress and never loses a character:
    the piece before the cut and the piece after it concatenate back to `text`
    exactly, whitespace included.

    A cut at a raw character budget is the one boundary that can destroy a name
    outright. `global_placement` split as `global_pl` + `acement` is in neither
    piece, so it is a candidate in neither window; every entry claiming it is
    vetoed `not_in_candidates` in one and `not_in_text` in the other, and the
    command is simply gone. The same cut through `-density` loses a parameter
    the same way. Since no command name and no flag contains whitespace, a cut
    that lands on whitespace cannot fall inside one — so this walks BACKWARDS
    from the target to the nearest whitespace, newlines first because a
    newline is also a table row's or a usage line's own boundary.

    Backwards, never forwards: the target is a budget the caller has already
    committed to (a window's `max_chars`), and moving the cut past it would
    overrun the budget the model call is sized by. Bounded by `lookback`, and
    best-effort past it — a 200-character run with no whitespace at all (a
    base64 blob, a minified line) is cut at the target, because there is no
    boundary to find and refusing to cut breaks the budget instead.
    """
    if target >= len(text):
        return len(text)
    target = max(1, target)
    floor = max(1, target - max(0, int(lookback)))
    # `rfind` over [floor - 1, target): a newline at i means a cut at i + 1,
    # which keeps the newline itself in the left piece.
    newline = text.rfind(_ELEMENT_JOIN, floor - 1, target)
    if newline >= 0:
        return newline + 1
    for index in range(target - 1, floor - 2, -1):
        if text[index].isspace():
            return index + 1
    return target


def _pack(
    elements: Sequence[WindowElement], limit: int
) -> list[list[WindowElement]]:
    """Group ordered elements into `limit`-sized runs. Nothing is lost.

    Two rules, and no third:

    * an element goes into the current group whole; if it does not fit, the
      group is closed and it starts the next one. Keeping elements whole is
      what makes a window's text a faithful rendering of its elements — a
      table cut in half mid-row grounds worse than the same table in the next
      window;
    * an element longer than the whole budget is cut into consecutive pieces
      (at a token-safe boundary, see `_split_point`), each carrying the
      element's own id. This is the only place a boundary falls inside an
      element, and it exists because the alternative — v1's "drop what does
      not fit" — is silent data loss: measured, a 120-parameter table vanished
      whole and left a section that was nothing but its own heading, with a
      command-reject ratio of 0.0 because there was no longer anything left to
      fail.
    """
    groups: list[list[WindowElement]] = []
    pieces: list[WindowElement] = []
    used = 0

    def flush() -> None:
        nonlocal pieces, used
        if pieces:
            groups.append(pieces)
            pieces = []
            used = 0

    for element in elements:
        remaining = element.text
        while remaining:
            separator = len(_ELEMENT_JOIN) if pieces else 0
            room = limit - used - separator
            if len(remaining) <= room:
                pieces.append(
                    element
                    if remaining == element.text
                    else _piece(element, remaining)
                )
                used += separator + len(remaining)
                break
            if pieces:
                # It may still fit in a group of its own — or it may not, in
                # which case the next pass starts an empty group and splits.
                # Either way the current group is done.
                flush()
                continue
            # An empty group that still cannot hold it: this element is longer
            # than the entire budget, so it is cut here and continues into the
            # next group.
            cut = _split_point(remaining, limit)
            pieces.append(_piece(element, remaining[:cut]))
            used += cut
            remaining = remaining[cut:]
            flush()
    flush()
    return groups


def _group_chars(pieces: Sequence[WindowElement]) -> int:
    """The character length of the window these pieces would become."""
    if not pieces:
        return 0
    return sum(len(piece.text) for piece in pieces) + len(_ELEMENT_JOIN) * (
        len(pieces) - 1
    )


def _dense_overflow(pieces: Sequence[WindowElement], bound: int) -> int:
    """How many candidate names this group has beyond `bound` (0 when it fits).

    Two scans rather than one, and only ever one on the ordinary window: the
    BOUNDED scan is the one the caller pays for anyway, it short-circuits the
    moment it fills the list, and a list that comes back short is proof there
    is no overflow to count. Only a window that saturated the list is scanned
    again without a bound — which is exactly the window whose true count is
    the question.
    """
    if bound <= 0:
        return 0
    if len(_scan_candidates(pieces, bound)) < bound:
        return 0
    return max(0, len(_scan_candidates(pieces, None)) - bound)


def _halve(
    pieces: Sequence[WindowElement],
) -> tuple[list[WindowElement], list[WindowElement]]:
    """Cut one group roughly in half, at the best boundary it has.

    Element boundaries first — the group is split at whichever one leaves the
    two halves closest to equal, so an over-dense window becomes two windows of
    whole elements. A group of ONE element has no such boundary and is cut
    inside it, at a token-safe character split.

    Returns an empty right half when there is nothing left to cut (a
    single-character element), which is the caller's signal to stop.
    """
    if len(pieces) > 1:
        total = _group_chars(pieces)
        used = 0
        best_index = 1
        best_delta: float | None = None
        for index in range(1, len(pieces)):
            used += len(pieces[index - 1].text) + (
                len(_ELEMENT_JOIN) if index > 1 else 0
            )
            delta = abs(used - total / 2)
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best_index = index
        return list(pieces[:best_index]), list(pieces[best_index:])
    element = pieces[0]
    cut = _split_point(element.text, len(element.text) // 2)
    if cut <= 0 or cut >= len(element.text):
        return list(pieces), []
    return (
        [_piece(element, element.text[:cut])],
        [_piece(element, element.text[cut:])],
    )


def _split_dense_groups(
    groups: Sequence[Sequence[WindowElement]], *, bound: int, floor: int
) -> list[tuple[list[WindowElement], int]]:
    """Split every group that offers more candidate names than `bound`.

    The alternative — truncating the list, which is what the list's own `limit`
    does — is a permanent, silent loss of every command past the cut. Windows
    do not overlap and are not revisited, so the 33rd command of a window is
    not "served later": it is served nowhere, in this run or any other, and no
    downstream check can see it go. That is the same failure mode as v1's
    discard branch, one layer up: a name that is never served produces no
    rejection, no ratio movement and no report line.

    Splitting has none of that cost. The pieces are ordinary windows, every
    character still lands in exactly one of them (`_halve` only moves a
    boundary), and each piece offers its own shorter list. It buys the extra
    model call the split window now needs, which is the correct trade for a
    command that would otherwise not be extractable at all.

    Recursion stops at `floor`: below it a window that still names more than
    `bound` commands is a name list rather than documentation (see
    `WINDOW_SPLIT_FLOOR_CHARS`), and the residual overflow is returned
    alongside the group so the job layer can report it.

    Iterative, not recursive, and deliberately: the depth is provably small
    (every split strictly shrinks both halves), but "provably small" on a
    pathological document is not a reason to put an unbounded document shape
    on the interpreter's C stack.
    """
    out: list[tuple[list[WindowElement], int]] = []
    for group in groups:
        pending: list[list[WindowElement]] = [list(group)]
        while pending:
            current = pending.pop()
            overflow = _dense_overflow(current, bound)
            if not overflow or _group_chars(current) <= floor:
                out.append((current, overflow))
                continue
            left, right = _halve(current)
            if not left or not right:
                out.append((current, overflow))
                continue
            # LIFO, so the left half (and everything it splits into) is
            # emitted before the right one: document order is the whole
            # contract of this layer.
            pending.append(right)
            pending.append(left)
    return out


def _label(
    groups: Sequence[tuple[Sequence[WindowElement], int]]
) -> list[ExtractionWindow]:
    """Turn packed groups into numbered windows with inherited breadcrumbs.

    Runs after splitting, never before: a window's ordinal and the heading it
    inherits are properties of the final sequence, and numbering a group that
    is about to become two would leave gaps or duplicates in both.
    """
    windows: list[ExtractionWindow] = []
    carried = ""  # last heading seen, at or before the window being labelled
    for pieces, overflow in groups:
        headings = [
            item.section_path
            for item in pieces
            if item.element_type == "heading" and item.section_path
        ]
        # This window's own label is its FIRST heading — that is the section
        # the window opens in, and it is what a reviewer looking at the row
        # expects to see. What is handed to the NEXT window is the LAST one:
        # a window that opens under `set_a` and ends under `set_e` leaves the
        # document positioned in `set_e`, so labelling the continuation
        # `set_a` (which taking the first heading again would do) points a
        # reviewer at a section several commands back. Both halves matter and
        # they are different questions, which is why they read different ends
        # of the same list.
        windows.append(
            ExtractionWindow(
                ordinal=len(windows),
                elements=tuple(pieces),
                text=_ELEMENT_JOIN.join(item.text for item in pieces),
                provenance=headings[0] if headings else carried,
                candidates_overflowed=overflow,
            )
        )
        carried = headings[-1] if headings else carried
    return windows


def extraction_windows(
    rows: Sequence[Mapping[str, Any]],
    *,
    max_chars: int = WINDOW_CHARS,
    max_candidates: int = MAX_CANDIDATES,
    split_floor: int = WINDOW_SPLIT_FLOOR_CHARS,
) -> list[ExtractionWindow]:
    """Pack one source's ordered elements into bounded windows. Nothing is lost.

    Input is exactly what `source_elements_for_chunking` returns
    (`id`, `element_type`, `text`, `section_path`), so the job layer reuses the
    fetch it already has.

    Three passes, each with one job: pack to the character budget (`_pack`),
    split whatever is too DENSE for one candidate list to carry
    (`_split_dense_groups`), then number and label the result (`_label`). Only
    the first two move a boundary and neither ever moves a character across
    one, so every character of every element lands in exactly one window, in
    document order. Where the caller needs to prove that, `element_ids` says
    which boundaries are continuations (window i's last id == window i+1's
    first id) and which are element joins.

    `max_candidates` is the same bound `window_candidates` truncates at, passed
    here because that truncation is what the second pass exists to avoid — see
    `_split_dense_groups`.
    """
    limit = max(1, int(max_chars))
    return _label(
        _split_dense_groups(
            _pack(_window_elements(rows), limit),
            bound=max(0, int(max_candidates)),
            floor=max(1, int(split_floor)),
        )
    )


# ============================================================= 2. candidates
def window_candidates(
    window: ExtractionWindow, *, limit: int = MAX_CANDIDATES
) -> list[str]:
    """The names an entry from this window may legally claim.

    Ordered by how much the source commits to each: headings first, then usage
    lines (code blocks ahead of prose), then inline code. The order matters
    because the list is truncated — C0 measured 5/5 command-name accuracy with
    a served list, and that only holds while the right names are on it.

    Code blocks outrank prose for a measured reason: a section documenting
    `replace_arith_modules` opens with prose containing `link_design top
    -hier`, so reading strictly in document order puts a cross-referenced
    command ahead of the documented one. MinerU labels nothing as code, so on a
    PDF manual both passes see the same elements and the order degrades to
    document order rather than to a second code path.

    Flags are excluded (see `_identifiers`); the list is deduplicated and
    capped. Being slightly over-inclusive is safe — every candidate still has
    to survive the verbatim text check in `validate_entry` — while missing a
    real name costs that whole command.

    Truncation here is a last resort, not the primary defence: a window whose
    names do not fit is SPLIT by `extraction_windows` before it ever reaches
    this function, precisely because a truncated list drops a real command with
    no downstream trace. What still truncates is the pathological case the
    splitter's floor stops at, and that residue is counted on the window
    (`candidates_overflowed`).
    """
    return _scan_candidates(window.elements, max(0, int(limit)))


def _scan_candidates(
    elements: Sequence[WindowElement], bound: int | None
) -> list[str]:
    """`window_candidates`' scan, over raw pieces and with an optional bound.

    Split out for two callers with different questions. `window_candidates`
    asks "what may be claimed here", which is a bounded list because the list
    rides in a prompt. `_dense_overflow` asks "how many are there", which
    cannot be answered by a list that stops counting at the bound — and it
    asks it about a group of pieces that is not a window yet.
    """
    ordered: dict[str, None] = {}

    def add(term: str) -> bool:
        """Record `term`; return whether there is still room for another."""
        if term and term not in ordered:
            ordered[term] = None
        return bound is None or len(ordered) < bound

    if bound is not None and bound <= 0:
        return []
    for element in elements:
        if element.element_type != "heading":
            continue
        for term in _identifiers(element.text):
            if not add(term):
                return list(ordered)
    for code_first in (True, False):
        for element in elements:
            if (element.element_type in _CODE_TYPES) is not code_first:
                continue
            for term in _usage_identifiers(element.text):
                if not add(term):
                    return list(ordered)
    for element in elements:
        for match in _INLINE_CODE_RE.finditer(element.text):
            for term in _identifiers(match.group(1)):
                if not add(term):
                    return list(ordered)
    return list(ordered)


def carry_candidates(
    prev_candidates: Sequence[str],
    prev_carry: Sequence[str],
    *,
    limit: int = MAX_CANDIDATES,
) -> list[str]:
    """The names the previous window hands forward to the next one.

    A command's documentation routinely outlives the window it starts in: a
    120-parameter options table is several windows long, and every window after
    the first holds parameters with no command name anywhere in them. Those
    windows have no candidate of their own, so without a relay the model has
    nothing it may legally claim and the window is provably empty — measured,
    that is most of every large command's parameter list.

    The chain is deliberately short-memoried::

        carry(0)  = ()
        carry(i)  = candidates(i-1)  or  carry(i-1) if candidates(i-1) is empty

    so it passes THROUGH windows that name nothing (a multi-window table) and
    RESETS at the next window that names something (a new command's heading is
    the old command's end). Without the reset a manual's first command would
    stay claimable on its last page; without the pass-through the relay would
    stop at the first table window, which is the very case it exists for.

    Truncation prefers the nearer window's names: `limit` is the same
    prompt-borne constraint `window_candidates` is capped by, and the names
    that just went out of view are better evidence than the ones that left
    several windows ago. (The two sources are mutually exclusive under the rule
    above, so the ordering is a statement of precedence rather than a
    tie-break that fires today.)

    Grounding is not weakened by this. Every name here was scanned verbatim out
    of the window it came from — the candidate list is constructive, not
    declarative — so the relay carries a witness rather than a guess, and the
    model still cannot produce a name that no window ever contained.

    **Registered trade-off: mis-attribution ACROSS commands stays possible.**
    A window's candidate list is every command-shaped name the window mentions,
    not just the one it documents — inline code cross-references neighbours all
    the time — so a relay can hand forward a name the previous window merely
    MENTIONED, and an orphaned parameter table can then be keyed onto it. The
    result is a parameter filed under the wrong command, and no grounding rule
    can catch it: the name is real, the parameter is real, and both are in the
    document. Accepted rather than closed, because every closure considered
    (relay only the name a usage line invoked, relay only from a heading) also
    drops the ordinary case the relay exists for, and the review step is where
    a person sees the row before it lands.
    """
    own = [str(name) for name in prev_candidates or () if name]
    inherited = [] if own else [str(name) for name in prev_carry or () if name]
    ordered = dict.fromkeys(own + inherited)
    return list(ordered)[:max(0, int(limit))]


def window_needs_model(
    window: ExtractionWindow,
    carried: Collection[str] = (),
    *,
    own: Sequence[str] | None = None,
) -> bool:
    """Whether this window is worth a model call at all.

    The deterministic, zero-model-call cost gate that replaces v1's sectioning.
    Two conditions, both necessary, in one sentence: a call happens when the
    window has **a name that may legally be claimed** AND **evidence there is
    something to extract**.

        needs_model  ⟺  own  or  (flags and carried)

    Four shapes, which is the whole rule:

    * **own non-empty** → call. The ordinary case: this window itself names a
      command, so it can document one.
    * **own empty, flags present, carried non-empty** → call. A continuation
      window: the parameter table of a command named one or more windows back
      (see `carry_candidates`). This is the case the relay exists for and it is
      most of every large command's documentation.
    * **own empty, flags present, carried empty** → skip. Parameters with no
      claimable name anywhere — a table that opens the document, or one whose
      relay an intervening command already reset. Grounding guarantees the
      output is empty (`validate_entry` vetoes every name off the served list,
      and the served list is empty), so the call is provably pure spend.
    * **own empty, flags absent** → skip, WHATEVER the relay holds. Prose. The
      relay alone used to keep the gate open here, on the theory that a
      continuation window's prose is sometimes the command's own description;
      measured against a real manual that theory bills every prose page of the
      book, because the relay never empties once a command has been seen. The
      description is worth having and it is not worth a call per page.

    `carry` still passes THROUGH a skipped window (`carry_candidates` is a pure
    function of the previous window and this gate does not feed it), so a
    command's name survives an intervening prose page and reaches the table on
    the far side of it.

    `own` is the window's own candidate list, accepted pre-computed because
    the job layer needs it anyway (for the prompt and for advancing the relay)
    and scanning a 12k window twice per window is measurable on a long manual.
    Passing it is an optimisation only — omit it and this computes the same
    list itself.
    """
    names = window_candidates(window) if own is None else own
    if names:
        return True
    return bool(carried) and bool(parameter_names(window.text))


# ======================================================== 2b. evidence segments
@dataclass(frozen=True)
class WindowSegments:
    """Which part of a window documents which command.

    A window is a slab of a document and routinely documents several commands
    at once, so "does this parameter appear in the window" is the wrong
    question to ground an entry with — it is the question that made both of
    these pass:

    * a window holding `foo_cmd density` and `bar_cmd -density` accepts
      `-density` filed under **foo_cmd**, because the flag really is in the
      window (just in the other command's table);
    * and it REJECTS `density` filed under foo_cmd — its own, correct,
      positional argument — because `_check_arg_name` sees `-density` in the
      window and reports the dash infidelity it was written to catch.

    Both are the same defect from opposite sides: the haystack was the whole
    slab when the claim was about one command. So the window is cut into
    per-command evidence segments and each entry grounds against its own.

    `prelude` is everything before the first anchor: on a continuation window
    that is the whole window (a multi-window options table has no anchor of
    its own), which is exactly the text a RELAYED claim must be allowed to
    ground in.
    """

    prelude: str = ""
    by_command: Mapping[str, str] = field(default_factory=dict)

    def evidence(self, command_name: str, *, relayed: bool = False) -> str:
        """The text `command_name`'s entry may ground against.

        A relayed claim also gets the prelude — that IS its parameter table.
        A claim with no segment and no relay grounds against nothing, so every
        parameter and the syntax are rejected: the window only MENTIONS that
        command (an inline cross-reference), which is the same reading
        `suspect_related` already reports, now with teeth. Registered
        consequence, not an accident: an entry about a command this window
        merely name-drops keeps its name (the candidate list vouched for it)
        and loses its body.
        """
        own = self.by_command.get(command_name, "")
        if not relayed:
            return own
        return _ELEMENT_JOIN.join(part for part in (self.prelude, own) if part)


def _anchor_names(
    line: str, heading: bool, claimable: Collection[str]
) -> list[str]:
    """The claimable commands this line STRUCTURALLY documents.

    Two forms, and deliberately not a third: the line belongs to a heading
    element and names the command, or the command is the leading token of a
    usage line (`_usage_identifier`, the same judgement `window_candidates`
    admits names by).

    An inline-code mention does NOT open a segment, and that exclusion is the
    entire point. `See also \\`bar_cmd\\` for the density options` is how a
    manual cross-references its neighbours, and treating it as an anchor would
    hand the following parameter table to `bar_cmd` — recreating, inside one
    window, precisely the mis-attribution segmenting exists to remove.
    """
    if heading:
        return [term for term in _identifiers(line) if term in claimable]
    term = _usage_identifier(line)
    return [term] if term and term in claimable else []


def window_segments(
    window: ExtractionWindow,
    candidates: Sequence[str],
    carried: Collection[str] = (),
) -> WindowSegments:
    """Cut a window into one evidence segment per command it documents.

    One pass over the window's lines. A line that structurally anchors one or
    more claimable commands (see `_anchor_names`) OPENS their segment and
    closes whatever was open; everything up to the next anchor belongs to the
    commands that anchor opened. A command anchored several times (a heading,
    then an `Examples` usage line further down) owns the UNION of its runs —
    manuals interleave, and taking only the first run would drop the second
    half of a command's own documentation.

    Anchor lines belong to the segment they open, so a usage line grounds the
    syntax of the command it invokes.

    Cost is one pass over the window's lines with a set membership test each,
    i.e. O(lines) — not O(lines x candidates) — and lines are bounded by
    `WINDOW_CHARS`. Every line, no cap, for the same reason
    `_usage_identifiers` has none: the character budget is the bound, and a
    line skipped here would silently join a neighbouring command's segment,
    which is the wrong direction (it would accept a mis-attribution rather
    than reject a real parameter).
    """
    claimable = {str(name) for name in candidates or ()} | {
        str(name) for name in carried or ()
    }
    prelude: list[str] = []
    runs: dict[str, list[str]] = {}
    open_names: list[str] = []
    for line, heading in _window_lines(window):
        anchors = _anchor_names(line, heading, claimable) if claimable else []
        if anchors:
            open_names = anchors
            for name in anchors:
                runs.setdefault(name, [])
        if not open_names:
            prelude.append(line)
            continue
        for name in open_names:
            runs[name].append(line)
    return WindowSegments(
        prelude=_ELEMENT_JOIN.join(prelude),
        by_command={
            name: _ELEMENT_JOIN.join(lines) for name, lines in runs.items()
        },
    )


def _window_lines(window: ExtractionWindow) -> list[tuple[str, bool]]:
    """The window's text as `(line, is_heading)` pairs.

    Reconstructed from the ELEMENTS rather than from `window.text`, because
    "is this line part of a heading" is an element property that the joined
    string has thrown away. The two agree exactly —
    `"\\n".join(line for line, _ in ...) == window.text` — since the window
    joins elements with the same newline the split uses.
    """
    lines: list[tuple[str, bool]] = []
    for element in window.elements:
        heading = element.element_type == "heading"
        for line in element.text.split(_ELEMENT_JOIN):
            lines.append((line, heading))
    return lines


# ================================================================= 3. slicing
def extraction_slices(
    window: ExtractionWindow, *, params_per_slice: int = SLICE_PARAM_LIMIT
) -> list[ExtractionSlice]:
    """Split one window across as many model calls as its parameters need.

    Mandatory, not an optimisation: C0 watched a ~100-parameter section hit
    `finish_reason=length` and then return empty content on retry, i.e. the
    failure mode is silent data loss, not a visible error.

    A window with no parameters still gets one slice — `remove_fillers` has
    syntax and a description worth extracting. Slice 0 always carries the
    overview responsibility (syntax / description / examples per entry) so
    those are asked for exactly once; later slices ask only for their parameter
    subset. The subsets partition the window's parameter list: disjoint,
    complete, in document order. Every slice's `text_window` is the WHOLE
    window — see `ExtractionSlice` for why a narrower per-slice view, correct
    when a slice was one command's section, became systematic blindness once a
    slice became a chunk of a multi-command slab.

    The assignment is the WINDOW's flag list, not one command's: a window may
    document several commands, and the model keys each returned parameter to
    the entry it belongs to. Attribution stays exact anyway, because
    `validate_entry` checks a returned name against this same assignment.
    """
    names = parameter_names(window.text)
    size = max(1, int(params_per_slice))
    if len(names) <= size:
        return [
            ExtractionSlice(
                index=0,
                total=1,
                param_names=tuple(names),
                include_overview=True,
                text_window=window.text,
            )
        ]
    groups = [names[start:start + size] for start in range(0, len(names), size)]
    return [
        ExtractionSlice(
            index=index,
            total=len(groups),
            param_names=tuple(group),
            include_overview=index == 0,
            text_window=window.text,
        )
        for index, group in enumerate(groups)
    ]


# =============================================================== 4. grounding
def _reject_window(text: str, value: str) -> str:
    """A bounded look at where the value was searched for — diagnostics only.

    Best effort by design: for a value that is not in the text there is no
    match to centre on, so a case-insensitive, dash-insensitive probe locates
    the near miss when there is one (which is exactly the interesting case:
    `density` vs `-density`) and the head of the window stands in when there
    is not.
    """
    probe = (value or "").lstrip("-")
    index = text.lower().find(probe.lower()) if probe else -1
    if index < 0:
        return text[:REJECT_WINDOW_CHARS]
    start = max(0, index - REJECT_WINDOW_CHARS // 2)
    return text[start:start + REJECT_WINDOW_CHARS]


def _reject(text: str, field_name: str, value: str, reason: str) -> Rejection:
    return Rejection(
        field=field_name,
        value=value[:MAX_REJECT_VALUE_CHARS],
        reason=reason,
        window=_reject_window(text, value),
    )


def _check_arg_name(raw: str, text: str) -> str:
    """Return a rejection reason for this parameter name, or "" if it grounds.

    The dash test is conditional on evidence rather than on shape. Demanding
    that every parameter start with `-` would be wrong — plenty of commands
    take positional arguments — but a bare `density` while the window itself
    writes `-density` is not a positional argument, it is the dropped dash C0
    measured.

    A verbatim text check on its own cannot catch it, which is why this test
    exists as a separate rule: the bare word is genuinely present in the
    window, both in prose and as the placeholder in `[-density
    target_density]`, so `not_in_text` would never fire and the stripped name
    would be accepted. The order of the two tests below decides only which
    reason gets reported when both apply; `dash_stripped` goes first because it
    is the accurate diagnosis.
    """
    if not raw:
        return "empty"
    if not raw.startswith("-") and _token_present("-" + raw, text):
        return "dash_stripped"
    if not _token_present(raw, text):
        return "not_in_text"
    return ""


def _coerce_required(raw: Any) -> tuple[bool, str]:
    """Coerce a model's `required` value to bool, degrading rather than lying.

    Absent (`None`) is the ordinary, valid case — most parameters omit it and
    default to optional — so it is not reported. A JSON boolean passes
    through unchanged. A case-insensitive `"true"`/`"false"` STRING is the one
    other shape worth accepting, since models occasionally quote booleans; it
    is coerced by meaning, never by truthiness — `bool("false")` is `True`,
    which is the exact silent flip this function exists to close. Anything
    else (a number, a list, an unrecognised string) cannot be trusted to mean
    either value, so it degrades to `False` — the same "cleared, never fatal"
    treatment `syntax` and `default` already get on a failed grounding check —
    and the second return value carries the original text so the caller can
    record a rejection rather than let the degrade happen silently.
    """
    if raw is None:
        return False, ""
    if isinstance(raw, bool):
        return raw, ""
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized == "true":
            return True, ""
        if normalized == "false":
            return False, ""
    return False, str(raw)[:MAX_REJECT_VALUE_CHARS]


def _coerce_examples(raw: Any, text: str) -> tuple[tuple[str, ...], list[Rejection]]:
    """Coerce a model's `examples` value to `list[str]`, degrading item by item.

    A bare STRING is the one scalar shape worth folding rather than dropping:
    a model answering "one example" with `"cluster_flops -x 1"` instead of
    `["cluster_flops -x 1"]` got the wrapper wrong, not the content, so it is
    treated as a one-element list. This also closes the bug that motivated the
    check — the old code iterated `payload.get("examples")` directly, and
    iterating a raw string walks its CHARACTERS, silently shredding it into
    one-letter "examples". Any other non-list shape (an object, a number)
    carries no salvageable per-example structure and the whole field is
    dropped, reported so the drop is visible rather than quietly yielding an
    empty tuple. Within a list, each item stands on its own: a string is kept
    (blank ones filtered, as before); anything else is dropped and reported
    rather than `str()`-coerced, since `str({"foo": 1})` would print as a
    plausible-looking example the manual never contained.
    """
    rejections: list[Rejection] = []
    if raw is None:
        return (), rejections
    if isinstance(raw, str):
        raw = [raw] if raw.strip() else []
    elif not isinstance(raw, list):
        rejections.append(
            _reject(text, "examples", str(raw)[:MAX_REJECT_VALUE_CHARS], "not_list")
        )
        return (), rejections
    kept: list[str] = []
    for item in raw:
        if isinstance(item, str):
            if item.strip():
                kept.append(item)
        else:
            rejections.append(
                _reject(text, "examples", str(item)[:MAX_REJECT_VALUE_CHARS], "not_string")
            )
    return tuple(kept), rejections


def _assignment_claim(raw: str, assigned: Sequence[str]) -> str:
    """Which assigned parameter a returned name is an ATTEMPT at, or "".

    Exact match first; failing that, a dash-insensitive match. The second
    clause exists for the one infidelity C0 actually measured: a model that
    answers `density` when it was assigned `-density` has attempted that
    parameter and got it wrong — it has not left it unanswered. Without the
    clause the same single mistake is charged twice, once as a returned arg
    that failed grounding and once as an assigned parameter nothing came back
    for, which would drag `args_keep_ratio` below the truth.

    Never used to ACCEPT a name (`_check_arg_name` still vetoes the stripped
    dash); only to decide what counts as covered.
    """
    if not raw:
        return ""
    if raw in assigned:
        return raw
    key = raw.lstrip("-")
    for name in assigned:
        if name.lstrip("-") == key:
            return name
    return ""


def assignment_coverage(
    entries: Sequence[Mapping[str, Any] | None],
    assigned: Sequence[str],
) -> AssignmentCoverage:
    """How much of `assigned` the given raw model payloads addressed at all.

    Takes a LIST of payloads, not one, because a slice may answer across
    several calls (the job layer halves a slice whose answer overran the output
    budget) and because one call now answers with several entries — a window's
    assignment is covered by the union of all of them. Computing this per
    payload would report each half, or each entry, as having ignored the rest.

    An empty assignment yields an empty ledger, and that is a statement rather
    than a degenerate case: nothing was asked for, so nothing can be missing.
    It is what keeps a flagless command's positional arguments (which the
    prompt asks for WITHOUT an assignment, since no list can be derived for
    them) from being charged as unanswered — those are judged by grounding
    alone, never by coverage.

    This reads RAW model payloads, so it inherits every shape a legal JSON
    reply can have and none of the guarantees `validate_entry` produces. A
    scalar `args` (`{"args": 5}`) is legal JSON and `5 or ()` is `5`, so
    iterating it raises `TypeError` — out of a pure function, through the job
    layer's coverage call, into the generic "internal error" path, failing a
    paid job over an answer the validator was about to turn into an ordinary
    visible rejection. Non-list `args` is therefore normalised to empty here,
    exactly as `validate_entry` normalises it, and the slice's whole
    assignment reads as unanswered — which is precisely what happened.

    A non-object ENTRY (`entries: [5]`) is skipped for the same reason, one
    level up. The job layer's `_payload_entries` already replaces those with
    `None` before this is called, so today that is belt and braces — but it is
    a CALLER's guarantee about a function whose whole job is to survive raw
    model output, and the scalar-`args` defect above was the same shape of
    assumption. `.get` on an `int` raises `AttributeError`, which lands in the
    same generic failure path.
    """
    names = tuple(str(name) for name in assigned or ())
    if not names:
        return AssignmentCoverage()
    claimed: set[str] = set()
    returned = 0
    for entry in entries or ():
        if entry is not None and not isinstance(entry, Mapping):
            continue
        raw_args = (entry or {}).get("args")
        for raw_arg in raw_args if isinstance(raw_args, list) else ():
            if not isinstance(raw_arg, Mapping):
                continue
            returned += 1
            hit = _assignment_claim(str(raw_arg.get("name") or "").strip(), names)
            if hit:
                claimed.add(hit)
    return AssignmentCoverage(
        assigned=len(names),
        returned=returned,
        covered=len(claimed),
        uncovered=tuple(name for name in names if name not in claimed),
    )


def validate_entry(
    entry: Mapping[str, Any] | None,
    window: ExtractionWindow,
    candidates: Sequence[str],
    *,
    assigned: Sequence[str] | None = None,
    carried: Collection[str] = (),
    segments: WindowSegments | None = None,
) -> ValidationResult:
    """Ground ONE extracted entry against its command's evidence, field by field.

    One call per entry: a window's reply lists every command it documents, and
    each is judged alone, so a hallucinated entry vetoes itself and leaves its
    neighbours untouched.

    `carried` is the relay from `carry_candidates`, and this function takes the
    UNION of it and `candidates` as the served list itself rather than asking
    the caller to merge them. Deliberate: the two facts a carried name needs —
    "it may be claimed" and "it need not appear in this window" — would
    otherwise live in two arguments that a caller can desynchronise, and each
    half alone fails silently in the direction that looks like a clean run.
    Merge but forget to pass `carried` and every continuation entry is vetoed
    `not_in_text`; pass `carried` but forget to merge and every one is vetoed
    `not_in_candidates`. Both read as "the model found nothing", which is
    exactly the outcome the relay exists to prevent. Keeping `candidates` as
    the window's OWN list also leaves `window_outcome`'s uncovered-candidate
    ledger meaning what it says.

    Four layers, with deliberately different severities — a manual is worth
    extracting only if what comes back is the manual's own words, but a
    grounding rule strict enough to be useful will also fire on things worth
    keeping:

    * **command_name** — the only whole-entry veto. It must be on the served
      list *and* appear verbatim at token boundaries. Failing either means the
      entry is about a command this window does not document, and nothing else
      in it can be trusted. A CARRIED name is exempt from the verbatim half and
      from that half only: it was scanned verbatim out of the window that
      handed it over, so the witness exists — it is simply one window back. The
      list-membership half never relaxes, so a fabricated name is still vetoed
      whatever the relay holds.
    * **command_name not in a heading or any usage line** — grounded, but
      possibly a *mentioned* command rather than a documented one. Recorded
      as `suspect_related` on the entry; a veto here would drop correct entries
      from manuals that name the command only in prose. A carried claim is not
      suspect on this basis: a continuation window is not *mentioning* the
      command, it is still documenting it, and flagging every relayed entry
      would put a warning on precisely the entries the relay exists to
      produce.
    * **args** — the field itself must be a JSON list (a model that returns an
      object or a string here has not answered per-parameter at all — see the
      shape guard below); within it, each name is grounded in its original
      form, and — when the caller says which parameters this answer was ASKED
      for — belonging to that assignment; failures drop that one parameter and
      are recorded.
    * **syntax / default / required / examples** — cleared or coerced on
      failure, never fatal to the entry.

    A LEGAL JSON reply can still have the wrong SHAPE for a field — a string
    where a list was asked for, a truthy-but-wrong string where a boolean was
    asked for — and Python is happy to iterate or truth-test the wrong shape
    without raising, which is exactly how these arrive silently corrupted
    rather than loudly rejected: `for x in {"name": "-d"}` walks the dict's
    KEYS, `for c in "one example"` walks its CHARACTERS, and `bool("false")`
    is `True`. Every field below is coerced by an explicit type/value check
    before use, with a rejection recorded whenever a shape had to be degraded
    rather than merely re-typed (`str(5)` for a numeric `command_name` /
    `syntax` / `default` / arg `name` needs no separate check — that coercion
    already happens for free inside the existing `str(x or "")` idiom, and a
    result that still fails to ground reports through the normal grounding
    rejections above, not a new shape one).

    `assigned` is the slice's parameter list. Passing it turns on attribution:
    a name that grounds perfectly but was never asked for is another slice's
    parameter (or a scan of the whole window), and accepting it makes the
    per-slice ledger meaningless — a model answering one slice's assignment
    with another's would show a clean keep ratio while its own assignment
    silently vanished. The check runs AFTER `_check_arg_name` on purpose: a
    dropped dash is the accurate, measured diagnosis and must not be relabelled
    `arg_outside_slice` just because the stripped form is not in the list.

    An EMPTY (or omitted) assignment means "unconstrained", not "nothing may be
    returned". A flagless command's window carries no parameter names, and
    positional arguments (`set_dont_use lib_cells`) are exactly what such a
    window documents; vetoing them would delete a real capability to enforce a
    rule about a list that does not exist. This mirrors the same exemption the
    job layer's cache-admission validator already makes, and its prompt asks
    for those arguments outright in its no-flag branch — so the exemption is
    load-bearing, not merely permissive. The exemption is also strictly scoped
    to the empty case: with an assignment in hand, an unassigned name is still
    `arg_outside_slice`, because there the list DOES exist and answering some
    other slice from it is the infidelity this check was added for.

    `description` is passed through unchecked: prose cannot be matched
    verbatim, and binding it to element anchors is the job layer's job (see
    `ValidatedEntry.anchor_element_ids`). `examples` is prose too and is never
    grounded against `text`, but its outer shape (list vs. scalar vs. anything
    else) IS checked — see `_coerce_examples` — because the failure mode there
    is not "wrong content" but "wrong container", which grounding could never
    have caught anyway.

    **The body grounds against this command's SEGMENT, not the whole window**
    (`window_segments`). A window documents several commands, so "is this flag
    somewhere in the window" is the wrong question for a claim about one of
    them: it accepts `bar_cmd`'s `-density` filed under `foo_cmd`, and in the
    same breath rejects `foo_cmd`'s own positional `density` because the
    window contains `-density` somewhere and that reads as the dropped-dash
    infidelity. Both are the whole-slab haystack, from opposite sides. So
    `syntax`, every parameter name and every default are searched in the text
    that documents THIS command — plus, for a relayed claim, the prelude,
    which on a continuation window is its parameter table. A claim with
    neither a segment nor the relay (the window only name-drops it in inline
    code) grounds against nothing and loses its whole body; it keeps its name,
    since the candidate list vouched for that.
    `assigned` and the coverage ledger stay WINDOW-level on purpose: they
    record what the slice was ASKED, which is a property of the window's flag
    list, and segmenting them would turn "the model ignored this parameter"
    into "the model filed it elsewhere".

    The command NAME's own verbatim check still reads `window.text` in full,
    unchanged: list membership plus "some part of this window says it" is what
    the served list means, and narrowing that to a segment would veto every
    entry the segmentation is meant to judge. Never a slice's `text_window`
    either, even though it holds the whole window today: grounding must not be
    able to drift with a prompt-sizing field a caller can set by hand.
    """
    text = window.text
    if segments is None:
        # Accepted pre-computed because a window's reply carries one entry per
        # command it documents and the cut is identical for all of them; the
        # job layer therefore does it once per window. Omitting it computes the
        # same thing — this is an optimisation, never a behaviour switch.
        segments = window_segments(window, candidates, carried)
    payload: Mapping[str, Any] = entry or {}
    relayed = tuple(str(name) for name in carried or ())
    allowed = tuple(
        dict.fromkeys(tuple(str(name) for name in candidates or ()) + relayed)
    )
    assignment = tuple(str(name) for name in assigned or ())
    rejections: list[Rejection] = []

    name = str(payload.get("command_name") or "").strip()
    # Computed once and unconditionally, because two different questions read
    # it: the veto below (which the relay may waive) and `relayed` further
    # down (which asks whether the waiver was what carried the claim).
    present = bool(name) and _token_present(name, text)
    if not name:
        rejections.append(_reject(text, "command_name", "", "empty"))
    elif name not in allowed:
        rejections.append(
            _reject(text, "command_name", name, "not_in_candidates")
        )
    elif name not in relayed and not present:
        rejections.append(_reject(text, "command_name", name, "not_in_text"))
    if rejections:
        return ValidationResult(
            accepted=False,
            rejections=tuple(rejections),
            stats=ValidationStats(command_rejected=True),
        )

    # From here down the haystack is THIS command's evidence, never the whole
    # window — see the docstring for the two-sided defect that fixes. The
    # rejection diagnostics quote it too, so a reviewer sees the text the
    # decision was actually made against.
    evidence = segments.evidence(name, relayed=name in relayed)
    args: list[ValidatedArg] = []
    args_seen = 0
    defaults_seen = 0
    defaults_kept = 0
    raw_args = payload.get("args")
    if raw_args is None:
        raw_args = ()
    elif not isinstance(raw_args, list):
        # The field itself is the wrong shape (an object, a string, a number)
        # rather than any one entry inside it being wrong. `for raw_arg in
        # raw_args` on an object walks its KEYS — every one of those is a
        # plain string, fails `isinstance(raw_arg, Mapping)` below, and used
        # to vanish via a bare `continue` with no record at all. Treat the
        # whole field as unusable instead of looping it: one rejection that
        # says so, then proceed as if no args were returned (never fatal to
        # the entry — a malformed `args` field is not evidence the command
        # itself is wrong).
        rejections.append(
            _reject(
                evidence, "args", str(raw_args)[:MAX_REJECT_VALUE_CHARS],
                "model_response_unusable",
            )
        )
        raw_args = ()
    for raw_arg in raw_args:
        if not isinstance(raw_arg, Mapping):
            rejections.append(
                _reject(
                    evidence, "arg", str(raw_arg)[:MAX_REJECT_VALUE_CHARS],
                    "not_object",
                )
            )
            continue
        args_seen += 1
        arg_name = str(raw_arg.get("name") or "").strip()
        reason = _check_arg_name(arg_name, evidence)
        if not reason and assignment and arg_name not in assignment:
            reason = "arg_outside_slice"
        if reason:
            rejections.append(_reject(evidence, "arg", arg_name, reason))
            continue
        default = str(raw_arg.get("default") or "").strip()
        if default:
            defaults_seen += 1
            # Token-bounded, not bare `in`: the manual's own "default value is
            # 500" would let a hallucinated "5" ground as a substring of 500.
            if _token_present(default, evidence):
                defaults_kept += 1
            else:
                rejections.append(
                    _reject(evidence, "default", default, "not_in_text")
                )
                default = ""
        required, bad_required = _coerce_required(raw_arg.get("required"))
        if bad_required:
            rejections.append(
                _reject(evidence, "required", bad_required, "not_boolean")
            )
        args.append(
            ValidatedArg(
                name=arg_name,
                required=required,
                description=str(raw_arg.get("desc") or raw_arg.get("description") or ""),
                default=default,
            )
        )

    syntax = str(payload.get("syntax") or "").strip()
    syntax_seen = bool(syntax)
    syntax_kept = syntax_seen and (
        normalize_syntax(syntax) in normalize_syntax(evidence)
    )
    if syntax_seen and not syntax_kept:
        rejections.append(_reject(evidence, "syntax", syntax, "not_contiguous"))
        syntax = ""

    # The window's own headings, not its `provenance`: provenance is a display
    # label inherited from a previous window, and a command "named in the
    # heading" must mean a heading whose text this window actually holds.
    heading_haystack = "\n".join(
        element.text
        for element in window.elements
        if element.element_type == "heading"
    )
    # `direct` is this window's OWN evidence that it documents the command
    # rather than merely mentioning it. `suspect` is unchanged: no direct
    # evidence and no relay to excuse its absence.
    direct = _token_present(name, heading_haystack) or name in _usage_identifiers(text)
    suspect = name not in relayed and not direct
    # And `relayed` is the difference between the two: a claim standing on the
    # relay alone. Its `suspect_related=False` is an exemption, not a finding,
    # so the cross-window merge must not read it as one — see `ValidatedEntry`.
    # Note that a carried name WITH direct evidence here is not relayed: it
    # earned the clean mark in this window and may clear an earlier warning.
    relayed_claim = name in relayed and not direct
    # `evidence` here is for the rejection diagnostics only — `_coerce_examples`
    # checks the CONTAINER's shape and never grounds an example's content
    # (prose cannot be matched verbatim).
    examples, examples_rejections = _coerce_examples(
        payload.get("examples"), evidence
    )
    rejections.extend(examples_rejections)
    return ValidationResult(
        accepted=True,
        entry=ValidatedEntry(
            command_name=name,
            syntax=syntax,
            description=str(payload.get("description") or ""),
            args=tuple(args),
            examples=examples,
            suspect_related=suspect,
            relayed=relayed_claim,
        ),
        rejections=tuple(rejections),
        stats=ValidationStats(
            command_rejected=False,
            args_seen=args_seen,
            args_kept=len(args),
            syntax_seen=syntax_seen,
            syntax_kept=syntax_kept,
            defaults_seen=defaults_seen,
            defaults_kept=defaults_kept,
        ),
    )


# ================================================================ 5. outcomes
def window_outcome(
    window: ExtractionWindow,
    candidates: Sequence[str],
    results: Sequence[ValidationResult],
    *,
    uncovered_args: Sequence[str] = (),
) -> WindowOutcome:
    """The per-window ledger, including what was *not* extracted.

    Uncovered candidates are the point of keeping the list: a window that
    served four plausible names and produced one entry is exactly the thing a
    person should look at, and no amount of per-entry validation surfaces it.
    (A candidate is legitimately uncovered more often here than in v1 — inline
    code cites commands documented elsewhere in the document — so this is a
    ledger line, never a verdict.)

    `uncovered_args` is the parameter-level version of the same idea, and it
    arrives from the caller rather than being derived from `results` for a
    structural reason: coverage is a property of a SLICE (its assignment vs
    the union of everything its calls answered), while a `ValidationResult` is
    a property of one entry. Deriving it here would count each entry — and each
    half of a halved slice — as having ignored every parameter the others
    answered. Each name is also folded into the rejection ledger — after the
    real rejections, so a long uncovered list can never push a grounding
    failure out of the report.
    """
    served = tuple(str(name) for name in candidates or ())
    missing = tuple(str(name) for name in uncovered_args or ())
    accepted: dict[str, None] = {}
    rejections: list[Rejection] = []
    rejections_overflow = 0
    entries_seen = 0
    command_rejects = 0
    args_seen = 0
    args_kept = 0
    for result in results or ():
        entries_seen += 1
        for rejection in result.rejections:
            if len(rejections) < MAX_WINDOW_REJECTIONS:
                rejections.append(rejection)
            else:
                rejections_overflow += 1
        args_seen += result.stats.args_seen
        args_kept += result.stats.args_kept
        if result.stats.command_rejected:
            command_rejects += 1
        if result.accepted and result.entry is not None:
            accepted[result.entry.command_name] = None
    for name in missing:
        if len(rejections) < MAX_WINDOW_REJECTIONS:
            rejections.append(_reject(window.text, "arg", name, "arg_not_returned"))
        else:
            rejections_overflow += 1
    return WindowOutcome(
        ordinal=window.ordinal,
        provenance=window.provenance,
        candidates=served,
        accepted_names=tuple(accepted),
        uncovered_candidates=tuple(
            name for name in served if name not in accepted
        ),
        rejections=tuple(rejections),
        rejections_overflow=rejections_overflow,
        entries_seen=entries_seen,
        command_rejects=command_rejects,
        args_seen=args_seen,
        args_kept=args_kept,
        uncovered_args=missing[:MAX_WINDOW_REJECTIONS],
        args_uncovered=len(missing),
        candidates_overflowed=window.candidates_overflowed,
    )


def catalog_stats(outcomes: Sequence[WindowOutcome]) -> CatalogStats:
    """Run-level totals for the job layer's circuit breaker.

    The ratio is published, the threshold constant is published, and the
    decision is not made here: "abort the job" is a policy that needs the job's
    own context (how far in, what the source is, whether a person is waiting).

    `args_keep_ratio`'s denominator is `args_seen + args_uncovered`, NOT
    `args_seen` alone, and the difference is the whole point of the ledger: a
    model answering 1 of 20 assigned parameters scores 1/1 on what it returned
    and 1/20 on what it was asked for. Only the second number can tell that run
    apart from a clean one, and a keep ratio that a model can raise by
    answering LESS is worse than no ratio at all.

    `windows` counts the outcomes it was given, which is the job layer's own
    "windows that actually made a call" — a window skipped by
    `window_needs_model` produced no entries and must not dilute either ratio.
    """
    rows = list(outcomes or ())
    entries_seen = sum(row.entries_seen for row in rows)
    command_rejects = sum(row.command_rejects for row in rows)
    args_seen = sum(row.args_seen for row in rows)
    args_kept = sum(row.args_kept for row in rows)
    args_uncovered = sum(row.args_uncovered for row in rows)
    args_asked = args_seen + args_uncovered
    return CatalogStats(
        windows=len(rows),
        entries_seen=entries_seen,
        command_rejects=command_rejects,
        command_reject_ratio=(
            round(command_rejects / entries_seen, 4) if entries_seen else 0.0
        ),
        args_seen=args_seen,
        args_kept=args_kept,
        args_uncovered=args_uncovered,
        args_keep_ratio=(round(args_kept / args_asked, 4) if args_asked else 0.0),
    )
