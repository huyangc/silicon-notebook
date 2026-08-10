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
documents. What survived the change untouched is the part C0 measured as
working — the grounding machinery below, which decides what of the model's
answer is the manual's own words.

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
from dataclasses import dataclass
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
# C0: ~100 parameters overruns the output budget. 20 keeps a slice's answer
# comfortably inside it with room for syntax/description/examples on slice 0.
SLICE_PARAM_LIMIT = 20
# Bounded scans. Windows are already capped at WINDOW_CHARS; these keep the
# per-window work constant rather than proportional to a pathological paste.
MAX_SCAN_LINES = 200
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
    """

    ordinal: int
    elements: tuple[WindowElement, ...]
    text: str
    provenance: str = ""

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


def _usage_identifiers(text: str, *, max_lines: int = MAX_SCAN_LINES) -> list[str]:
    """Every command invoked by a usage line in `text`, deduplicated in order.

    Deliberately parser-blind. The spec's shape is "the first code block's
    first line", but MinerU emits no code_block element type at all — a PDF
    manual's usage line arrives as an ordinary paragraph — and C0 measured that
    flattened MinerU text extracts as well as native markdown. Recognising the
    *line* rather than the *element type* is what keeps that true, and
    `_usage_identifier` is strict enough to survive the wider haystack.
    """
    found: dict[str, None] = {}
    for index, line in enumerate((text or "").splitlines()):
        if index >= max_lines:
            break
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
    """
    elements: list[WindowElement] = []
    for raw in rows or ():
        text = str(raw.get("text") or "").strip()
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


def extraction_windows(
    rows: Sequence[Mapping[str, Any]],
    *,
    max_chars: int = WINDOW_CHARS,
) -> list[ExtractionWindow]:
    """Pack one source's ordered elements into bounded windows. Nothing is lost.

    Input is exactly what `source_elements_for_chunking` returns
    (`id`, `element_type`, `text`, `section_path`), so the job layer reuses the
    fetch it already has.

    Two rules, and no third:

    * an element goes into the current window whole; if it does not fit, the
      window is closed and it starts the next one. Keeping elements whole is
      what makes `window.text` a faithful rendering of `window.elements` —
      a table cut in half mid-row grounds worse than the same table in the
      next window;
    * an element longer than the whole budget is cut into consecutive pieces,
      each carrying the element's own id. This is the only place a boundary
      falls inside an element, and it exists because the alternative — v1's
      "drop what does not fit" — is silent data loss: measured, a
      120-parameter table vanished whole and left a section that was nothing
      but its own heading, with a command-reject ratio of 0.0 because there
      was no longer anything left to fail.

    So every character of every element lands in exactly one window, in
    document order. Where the caller needs to prove that, `element_ids` says
    which boundaries are continuations (window i's last id == window i+1's
    first id) and which are element joins.
    """
    limit = max(1, int(max_chars))
    elements = _window_elements(rows)

    windows: list[ExtractionWindow] = []
    pieces: list[WindowElement] = []
    used = 0
    carried = ""  # last heading seen, at or before the window being filled

    def flush() -> None:
        nonlocal pieces, used, carried
        if not pieces:
            return
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
        provenance = headings[0] if headings else carried
        windows.append(
            ExtractionWindow(
                ordinal=len(windows),
                elements=tuple(pieces),
                text=_ELEMENT_JOIN.join(item.text for item in pieces),
                provenance=provenance,
            )
        )
        carried = headings[-1] if headings else carried
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
                # It may still fit in a window of its own — or it may not, in
                # which case the next pass starts an empty window and splits.
                # Either way the current window is done.
                flush()
                continue
            # An empty window that still cannot hold it: this element is longer
            # than the entire budget, so it is cut here and continues into the
            # next window.
            pieces.append(_piece(element, remaining[:limit]))
            used += limit
            remaining = remaining[limit:]
            flush()
    flush()
    return windows


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
    """
    bound = max(0, int(limit))
    ordered: dict[str, None] = {}

    def add(term: str) -> bool:
        """Record `term`; return whether there is still room for another."""
        if term and term not in ordered:
            ordered[term] = None
        return len(ordered) < bound

    if not bound:
        return []
    for element in window.elements:
        if element.element_type != "heading":
            continue
        for term in _identifiers(element.text):
            if not add(term):
                return list(ordered)
    for code_first in (True, False):
        for element in window.elements:
            if (element.element_type in _CODE_TYPES) is not code_first:
                continue
            for term in _usage_identifiers(element.text):
                if not add(term):
                    return list(ordered)
    for element in window.elements:
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
    """
    names = tuple(str(name) for name in assigned or ())
    if not names:
        return AssignmentCoverage()
    claimed: set[str] = set()
    returned = 0
    for entry in entries or ():
        for raw_arg in (entry or {}).get("args") or ():
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
) -> ValidationResult:
    """Ground ONE extracted entry against the window text, field by field.

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

    Everything else is searched in `window.text` — the text the model was
    shown, in full. Never a slice's `text_window`, even though the two hold the
    same string today: grounding must not be able to drift with a prompt-sizing
    field a caller can set by hand, and the invariant it needs ("this is what
    the window actually holds") is only true of `window.text`. That includes a
    carried claim's own body: `syntax`, every parameter and every default must
    still ground HERE, because those are what this window is being asked about
    (a continuation window has no usage line, so its `syntax` simply comes back
    empty — cleared, never fatal).
    """
    text = window.text
    payload: Mapping[str, Any] = entry or {}
    relayed = tuple(str(name) for name in carried or ())
    allowed = tuple(
        dict.fromkeys(tuple(str(name) for name in candidates or ()) + relayed)
    )
    assignment = tuple(str(name) for name in assigned or ())
    rejections: list[Rejection] = []

    name = str(payload.get("command_name") or "").strip()
    if not name:
        rejections.append(_reject(text, "command_name", "", "empty"))
    elif name not in allowed:
        rejections.append(
            _reject(text, "command_name", name, "not_in_candidates")
        )
    elif name not in relayed and not _token_present(name, text):
        rejections.append(_reject(text, "command_name", name, "not_in_text"))
    if rejections:
        return ValidationResult(
            accepted=False,
            rejections=tuple(rejections),
            stats=ValidationStats(command_rejected=True),
        )

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
                text, "args", str(raw_args)[:MAX_REJECT_VALUE_CHARS],
                "model_response_unusable",
            )
        )
        raw_args = ()
    for raw_arg in raw_args:
        if not isinstance(raw_arg, Mapping):
            rejections.append(
                _reject(text, "arg", str(raw_arg)[:MAX_REJECT_VALUE_CHARS], "not_object")
            )
            continue
        args_seen += 1
        arg_name = str(raw_arg.get("name") or "").strip()
        reason = _check_arg_name(arg_name, text)
        if not reason and assignment and arg_name not in assignment:
            reason = "arg_outside_slice"
        if reason:
            rejections.append(_reject(text, "arg", arg_name, reason))
            continue
        default = str(raw_arg.get("default") or "").strip()
        if default:
            defaults_seen += 1
            # Token-bounded, not bare `in`: the manual's own "default value is
            # 500" would let a hallucinated "5" ground as a substring of 500.
            if _token_present(default, text):
                defaults_kept += 1
            else:
                rejections.append(
                    _reject(text, "default", default, "not_in_text")
                )
                default = ""
        required, bad_required = _coerce_required(raw_arg.get("required"))
        if bad_required:
            rejections.append(_reject(text, "required", bad_required, "not_boolean"))
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
    syntax_kept = syntax_seen and normalize_syntax(syntax) in normalize_syntax(text)
    if syntax_seen and not syntax_kept:
        rejections.append(_reject(text, "syntax", syntax, "not_contiguous"))
        syntax = ""

    # The window's own headings, not its `provenance`: provenance is a display
    # label inherited from a previous window, and a command "named in the
    # heading" must mean a heading whose text this window actually holds.
    heading_haystack = "\n".join(
        element.text
        for element in window.elements
        if element.element_type == "heading"
    )
    suspect = name not in relayed and not (
        _token_present(name, heading_haystack) or name in _usage_identifiers(text)
    )
    examples, examples_rejections = _coerce_examples(payload.get("examples"), text)
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
