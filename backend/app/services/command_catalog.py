"""Command-manual catalog extraction — the pure layer (Plan C, stage C1a).

A tool's *command reference* is the shape ordinary ingestion handles worst. The
600-char chunker splits `set_db` into description / Arguments / Examples, the
KG extractor turns a parameter table into free-floating claims, and the answer
that comes back is prose about a command rather than the command's contract.
Plan C ingests those manuals as structured entries instead — name, syntax,
arguments, defaults — and this module is everything in that pipeline that can
be decided without a database, a model call or a job:

    detect_manual_shape   is this source a command manual at all?
    command_sections      which elements belong to which command?
    command_candidates    which names may an entry legally claim?
    extraction_slices     how is one command split across model calls?
    validate_entry        which parts of a model's answer survive grounding?
    assignment_coverage   how much of a slice's assignment came back at all?
    section_outcome       what does the job report about this section?

Every one of them is a pure function over data the caller already holds, so
C1b (job + persistence) and C1c (UI) can be written and tested against them
without touching a model. The layering mirrors `chunking.py` / `exact_lookup.py`:
no IO here, ever.

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
wrong) is *reported* here as a ratio and *acted on* by C1b.

Contract for C1b: every model call built from an `ExtractionSlice` MUST pass
`response_validator` to `chat_json` (`app/services/model_provider.py`). That
argument is not optional plumbing — it is the sole admission ticket into the
content-addressed cache (gating BOTH whether a cached reply may be served and
whether this call's own reply may be written), the same gating the KG
extractors (`app/services/kg/extract.py`) already rely on. It has nothing to
do with retrying a malformed reply — that remedy is C1b's own halving logic in
`catalog_job.py`, entirely local to that module. Skip `response_validator` and
a slice is not just uncached — every retry of that slice silently repays the
full model cost with no caching benefit at all.
"""
from __future__ import annotations

import re
import string
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

from app.repositories.lexical_query import exact_probe_terms


# --------------------------------------------------------------------- bounds
# One section's text as the model will see it. Also the haystack every
# grounding check searches: validating against text the model was never shown
# would let a hallucination pass because it happens to appear in the part that
# was cut.
MAX_SECTION_CHARS = 12_000
# The candidate list rides in the prompt, so it is a constraint as much as a
# menu: every extra name weakens the one veto this layer has.
MAX_CANDIDATES = 16
# C0: ~100 parameters overruns the output budget. 20 keeps a slice's answer
# comfortably inside it with room for syntax/description/examples on slice 0.
SLICE_PARAM_LIMIT = 20
# "Enough command-shaped sections, and enough of the document" — a five-command
# appendix inside a 300-section paper is not a manual.
MIN_COMMAND_SECTIONS = 5
MIN_COMMAND_RATIO = 0.15
# A section needs this many flag-bearing lines before it counts as carrying a
# parameter table (reported as corroborating evidence, not as a verdict).
MIN_FLAG_LINES = 3
# Bounded scans. Sections are already capped at MAX_SECTION_CHARS; these keep
# the per-section work constant rather than proportional to a pathological
# paste.
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
# section's full text through its own failure records.
REJECT_WINDOW_CHARS = 200
MAX_REJECT_VALUE_CHARS = 120
# A section's rejection ledger is bounded too — a pathological entry (or a
# model that never stops inventing parameters) must not let a job report
# inherit an unbounded list through its own failure records. Overflow is
# counted, never silently dropped.
MAX_SECTION_REJECTIONS = 24
# C1b's circuit breaker reads this; this module only publishes the ratio. A
# veto rate alone can miss a bad run — an entry can pick the right command
# name and still invent every parameter — so the breaker reads both axes:
# reject-ratio above this ratio, OR args-keep-ratio (below) below its own,
# and only once MIN_SECTIONS_BEFORE_ALERT sections give the ratios a sample
# worth trusting.
COMMAND_REJECT_ALERT_RATIO = 0.20
ARGS_KEEP_ALERT_RATIO = 0.50
MIN_SECTIONS_BEFORE_ALERT = 10
# C1b's per-slice prompt window: the relevant original-text lines for that
# slice's own parameters, plus a short overview head — bounded well under
# MAX_SECTION_CHARS because a multi-slice command's later slices should not
# each re-carry the whole section every other slice already carries.
MAX_SLICE_WINDOW_CHARS = 4000
OVERVIEW_HEAD_CHARS = 600
# The strong/weak split `detect_manual_shape` draws on identifier-named
# headings: a dotted-only heading needs this many *strong* (underscore-joined)
# siblings before it counts toward `is_manual` on its own.
STRONG_IDENTIFIER_HEADING_MIN = 3


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
# nothing this way, which is exactly why code evidence is a *preference* below
# rather than a requirement.
_CODE_TYPES = frozenset({"code_block", "code"})
# A manual's own worked-examples appendix, by title rather than shape: every
# other rule here reads a usage line as command evidence, and a section named
# this way is genuinely command-shaped — measured on OpenROAD `rsz`, whose
# `## Example scripts` cites `read_sdc` (a command rsz's manual never
# documents anywhere else) purely as a usage illustration, and on `cts`,
# whose own `## Example scripts` re-cites `clock_tree_synthesis` (already
# documented above it) the same way. Case-insensitive; matched against the
# whole (stripped) heading text, not a substring.
_EXAMPLE_HEADING_WORDS = frozenset({
    "example", "examples", "example scripts", "sample", "samples",
})
# One segment of a document's numbering: `2`, `A`, `S1`, `iv`-ish.
_NUMBERING_SEGMENT_RE = re.compile(r"[A-Za-z]?[0-9]+|[A-Za-z]")
# Token boundary for every grounding check. `-` belongs to the token: without
# it, a model that answered `density` would be "found" inside the manual's
# `-density`, which is precisely the dash infidelity C0 measured.
_BOUNDARY_CHARS = frozenset(string.ascii_letters + string.digits + "_-")


# =========================================================== data definitions
@dataclass(frozen=True)
class SectionElement:
    """One source element, reduced to what this layer reads.

    Deliberately the same field names `source_elements_for_chunking` already
    returns, so C1b feeds this module the rows it already fetches for chunking.
    """

    id: str
    element_type: str
    text: str


@dataclass(frozen=True)
class SectionTruncation:
    """What the `MAX_SECTION_CHARS` budget cost this section, in the open."""

    applied: bool = False
    original_chars: int = 0
    kept_chars: int = 0
    dropped_elements: int = 0


@dataclass(frozen=True)
class CommandSection:
    """One command's slice of a document.

    `text` is the authority: it is what a prompt shows the model and what every
    grounding check searches, and it holds only the elements listed in
    `elements` (the heading among them — a section whose command name appears
    solely in its own heading is a normal manual shape, and vetoing it would be
    a false kill).
    """

    title: str
    section_path: str
    shape: str  # "identifier_heading" | "syntax_block"
    command_hint: str  # the identifier that established the shape; a section
    # only ever exists because `_classify` returned a non-empty one
    elements: tuple[SectionElement, ...]
    text: str
    truncation: SectionTruncation = SectionTruncation()

    @property
    def element_ids(self) -> tuple[str, ...]:
        return tuple(element.id for element in self.elements)


@dataclass(frozen=True)
class SectionMeta:
    """The cheap per-section view `detect_manual_shape` runs on.

    Callers assemble it from chunks or elements — whichever they already have —
    because shape detection must be answerable before any per-command work is
    scheduled. No breadcrumb here: `detect_manual_shape` counts sections, it
    does not group them, so it never reads one.
    """

    heading_text: str = ""
    text_sample: str = ""


@dataclass(frozen=True)
class ManualShapeSignal:
    """Counted evidence, plus a threshold verdict that never stands alone.

    `is_manual` exists so a caller can default sensibly, but the counts are the
    point: C1b and the UI show "found about N command sections" and let a person
    decide, which is the only honest thing to do with a heuristic.
    """

    total_sections: int = 0
    identifier_headings: int = 0
    syntax_sections: int = 0
    flag_sections: int = 0
    command_shaped_sections: int = 0
    command_ratio: float = 0.0
    flag_ratio: float = 0.0
    is_manual: bool = False
    reason: str = "no_sections"


@dataclass(frozen=True)
class ExtractionSlice:
    """One model call's worth of a command.

    Pure data: C1b turns `param_names` into "extract ONLY these parameters",
    `include_overview` into "also give me syntax / description / examples",
    and `text_window` into the prompt's own view of the source (bounded by
    `MAX_SLICE_WINDOW_CHARS`) — the overview head plus this slice's own
    parameter lines, not the whole section every other slice already carries.

    `text_window` is a prompt-sizing convenience only. `validate_entry` never
    reads it: grounding always searches `section.text`, the full text the
    section actually holds, so a later slice's answer is never held to a
    narrower — or laxer — standard than an earlier one's.
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

    `anchor_element_ids` is reserved: `description` / `examples` are prose and
    cannot be checked verbatim, so C1b will bind them to element ids instead.
    Leaving the seat empty here keeps that a schema decision rather than a
    later shape change.
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
class SectionOutcome:
    """The per-section ledger a C1b job report renders directly.

    `rejections` is capped at `MAX_SECTION_REJECTIONS` — a pathological entry
    (or a model that never stops inventing parameters) must not let a job
    report inherit an unbounded list through its own failure records.
    `rejections_overflow` counts what got cut, so the cap never reads as
    "everything after entry N was clean" when it was actually "we stopped
    counting".

    `uncovered_args` is the same idea one level down: the parameters this
    section's slices were ASSIGNED and never got an answer for. It is bounded
    like `rejections` is, with the true total in `args_uncovered` — which is
    also what keeps `args_keep_ratio` honest (see `catalog_stats`).
    """

    section_path: str
    title: str
    candidates: tuple[str, ...] = ()
    accepted_names: tuple[str, ...] = ()
    uncovered_candidates: tuple[str, ...] = ()
    rejections: tuple[Rejection, ...] = ()
    rejections_overflow: int = 0
    truncation: SectionTruncation = SectionTruncation()
    entries_seen: int = 0
    command_rejects: int = 0
    args_seen: int = 0
    args_kept: int = 0
    uncovered_args: tuple[str, ...] = ()
    args_uncovered: int = 0


@dataclass(frozen=True)
class CatalogStats:
    """Run-level totals. The ratios are published; the verdict belongs to C1b.

    Two independent axes, because either alone can miss a bad run: an entry
    can pick the right command name (a clean `command_reject_ratio`) and
    still invent every parameter, and a run that mostly rejects args can
    still be picking legitimate command names. C1b's circuit breaker should
    read both — command-name veto rate above `COMMAND_REJECT_ALERT_RATIO`
    (0.20) OR args-keep rate below `ARGS_KEEP_ALERT_RATIO` (0.50) — and only
    once `MIN_SECTIONS_BEFORE_ALERT` (10) sections have been processed, since
    neither ratio means anything on a sample of one or two.
    """

    sections: int = 0
    entries_seen: int = 0
    command_rejects: int = 0
    command_reject_ratio: float = 0.0
    args_seen: int = 0
    args_kept: int = 0
    args_uncovered: int = 0
    args_keep_ratio: float = 0.0
    truncated_sections: int = 0


# ================================================================= primitives
def _is_numbering(term: str) -> bool:
    """True for `2.1`, `A.1.2`, `B-3` — a document's numbering, not a name.

    The wide identifier regex cannot tell them apart, and papers are full of
    them: `A.1.2 Notation` yields `A.1.2`, which has an ASCII letter, a dot and
    five characters, so every other gate lets it through and a survey paper
    starts looking like a command manual.

    A term is numbering when it splits into two or more segments, contains a
    digit, and *every* segment is either a bare number, a number with one
    leading letter, or a single letter. The digit requirement is what keeps
    `set_db` and `config.yaml` out; the per-segment test is what keeps `GPT-4`
    in (`GPT` is neither). `v1-2` is classified as numbering and that is
    accepted: no command is named that, and erring toward "not a command" is
    the safe direction for both the shape signal and the candidate list.
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
    an untreated syntax block would hand thirty parameter names to a
    16-slot candidate list and crowd out the one name that matters.
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
    read as usage lines the moment either sentence happens to mention a flag
    — eight sentences like that is enough to make a paper with no commands at
    all read as a manual (`detect_manual_shape`'s only cost gate is this
    function returning ""). Positional arguments get one more condition on
    top (few, placeholder-shaped tokens): `global_placement performs the
    placement` still needs to fail there too, since it has neither a flag nor
    a placeholder-shaped rest.

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


# ========================================================= 1. shape detection
def _as_meta(item: Any) -> SectionMeta:
    """Accept the dataclass, a 2-tuple, or a mapping — callers differ.

    A mapping may carry extra keys (a `source_elements_for_chunking` row's own
    `section_path`, say) — they are simply not read, the same tolerance a real
    caller assembling this from chunks already needs.
    """
    if isinstance(item, SectionMeta):
        return item
    if isinstance(item, Mapping):
        return SectionMeta(
            heading_text=str(item.get("heading_text") or item.get("title") or ""),
            text_sample=str(item.get("text_sample") or item.get("text") or ""),
        )
    values = list(item) + ["", ""]
    return SectionMeta(
        heading_text=str(values[0] or ""),
        text_sample=str(values[1] or ""),
    )


def detect_manual_shape(sections_meta: Sequence[Any]) -> ManualShapeSignal:
    """Estimate, with zero model calls, whether a source is a command manual.

    Three independent signals are counted:

    * **identifier-named headings** — `### set_db`. The strongest shape, and
      the one that needs the numbering filter: without it a paper's
      `A.1.2 Notation` headings read as command names and a survey is offered
      command extraction.
    * **usage-line sections** — a plain heading (`### Global Placement`) whose
      body opens a code block with `global_placement`. Measured on OpenROAD:
      this is the *majority* shape, so a detector that only looked at headings
      would score a real manual at zero.
    * **flag-dense sections** — parameter tables.

    The verdict uses the first two only. Flag density is reported because it is
    genuinely informative to a person, but it must not vote: an `Options`
    sub-section is flag-dense and is not a command, so counting it would score
    one command twice and let any page of shell examples pass.

    Identifier headings are not all equal evidence, though. `_is_command_identifier`
    accepts both underscore-joined names (`set_db`) and dot-joined ones
    (`README.md`, `config.yaml`) — real commands are never dotted-only, but a
    filename repeated as a heading eight times clears every other gate (ASCII
    letter, minimum length, dotted separator) exactly like a real command name
    would. So underscore-joined headings are the *strong* signal and dotted-only
    headings are the *weak* one, and `is_manual` requires either enough strong
    signal on its own (`STRONG_IDENTIFIER_HEADING_MIN`) or a weak signal backed
    by the same corroborating evidence a syntax-line section already needs
    (`syntax_sections` or `flag_sections` present somewhere in the source) —
    never a stack of filenames alone.
    """
    metas = [_as_meta(item) for item in sections_meta or ()]
    total = len(metas)
    if not total:
        return ManualShapeSignal()

    identifier_headings = 0
    strong_identifier_headings = 0
    syntax_sections = 0
    flag_sections = 0
    for meta in metas:
        heading_ids = _identifiers(meta.heading_text)
        if heading_ids:
            identifier_headings += 1
            if any("_" in term for term in heading_ids):
                strong_identifier_headings += 1
        elif _usage_identifiers(meta.text_sample):
            syntax_sections += 1
        flag_lines = 0
        for index, line in enumerate((meta.text_sample or "").splitlines()):
            if index >= MAX_SCAN_LINES:
                break
            if _FLAG_RE.search(line):
                flag_lines += 1
        if flag_lines >= MIN_FLAG_LINES:
            flag_sections += 1

    command_shaped = identifier_headings + syntax_sections
    ratio = command_shaped / total
    weak_identifier_headings_only = (
        identifier_headings > 0
        and strong_identifier_headings < STRONG_IDENTIFIER_HEADING_MIN
        and syntax_sections == 0
        and flag_sections == 0
    )
    if command_shaped < MIN_COMMAND_SECTIONS:
        reason = "too_few_command_sections"
    elif ratio < MIN_COMMAND_RATIO:
        reason = "command_ratio_below_threshold"
    elif weak_identifier_headings_only:
        reason = "weak_identifier_headings_only"
    else:
        reason = "manual"
    return ManualShapeSignal(
        total_sections=total,
        identifier_headings=identifier_headings,
        syntax_sections=syntax_sections,
        flag_sections=flag_sections,
        command_shaped_sections=command_shaped,
        command_ratio=round(ratio, 4),
        flag_ratio=round(flag_sections / total, 4),
        is_manual=reason == "manual",
        reason=reason,
    )


# ========================================================== 2. section groups
@dataclass
class _Block:
    """A heading and everything under it, before command grouping."""

    title: str = ""
    section_path: str = ""
    elements: list[SectionElement] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(element.text for element in self.elements)


@dataclass
class _Draft:
    title: str
    section_path: str
    shape: str
    command_hint: str
    elements: list[SectionElement]
    closed: bool = False


def _blocks(elements: Sequence[Mapping[str, Any]]) -> list[_Block]:
    """Split the element stream on headings — `build_chunks`' own boundary.

    The breadcrumb fallback is `build_chunks`' too: markdown parsing stores a
    full `" > "` path on every heading, MinerU stores none, and a bare title is
    the honest degradation rather than a second code path.
    """
    blocks: list[_Block] = []
    current = _Block()
    for raw in elements or ():
        etype = str(raw.get("element_type") or raw.get("type") or "").lower()
        text = str(raw.get("text") or "").strip()
        item = SectionElement(
            id=str(raw.get("id") or ""), element_type=etype, text=text
        )
        if etype == "heading":
            if current.elements:
                blocks.append(current)
            crumbs = [
                seg.strip()
                for seg in str(raw.get("section_path") or "").split(" > ")
                if seg.strip()
            ]
            current = _Block(
                title=text,
                section_path=" > ".join(crumbs) or text,
                elements=[item],
            )
            continue
        if not text:
            continue
        current.elements.append(item)
    if current.elements:
        blocks.append(current)
    return blocks


def section_metas(elements: Sequence[Mapping[str, Any]]) -> list[SectionMeta]:
    """Every heading-delimited block's shape signal — the whole document, not
    just the subset that ends up grouped into a command section.

    C1b's ``preview`` used to feed `detect_manual_shape` the OUTPUT of
    `command_sections()` instead of this: that function already GROUPS blocks
    under a command heading and drops everything that never joins one, so its
    section count is, by construction, the same number as
    `command_shaped_sections`. Fed that, `command_ratio` is trivially 1.0 on
    every source that has even one recognisable command, and the ratio
    gate / `flag_ratio` / a five-command appendix inside a 300-section paper —
    the whole reason those signals exist — never has anything left to reject,
    because the denominator was silently made equal to the numerator.

    This is `_blocks()` alone, deliberately short of `_classify`/grouping:
    shape detection has to see the document's real section count BEFORE any
    per-command work is scheduled, exactly like a caller assembling
    `SectionMeta` straight from `source_elements_for_chunking` rows would.
    """
    return [
        SectionMeta(heading_text=block.title, text_sample=block.text)
        for block in _blocks(elements)
    ]


def _is_descendant(child: str, parent: str) -> bool:
    """Breadcrumb containment. Meaningless — and false — without breadcrumbs."""
    return bool(child and parent and child.startswith(parent + " > "))


def _breadcrumb_parent(path: str) -> str:
    return path.rsplit(" > ", 1)[0] if " > " in (path or "") else ""


def _is_sibling(one: str, other: str) -> bool:
    """Same breadcrumb parent, both nested. Requires real breadcrumbs."""
    parent = _breadcrumb_parent(one)
    return bool(parent) and parent == _breadcrumb_parent(other)


def _is_example_heading(title: str) -> bool:
    """Whether `title` names a worked-examples appendix rather than a command.

    A name-based carve-out, deliberately, not a shape one: `_EXAMPLE_HEADING_WORDS`
    names the whole (case-folded, whitespace-trimmed) heading, so `README.md` and
    a real `### set_db` heading are untouched.
    """
    return (title or "").strip().casefold() in _EXAMPLE_HEADING_WORDS


def _usage_evidence(block: _Block) -> tuple[str, bool]:
    """The command this block invokes, and whether a code block said so.

    Code blocks outrank prose, which is what the spec's "the first code block's
    first line" means once it is generalised past markdown. Measured: a section
    documenting `replace_arith_modules` opens with prose containing
    `link_design top -hier`, and reading in document order picks the
    cross-referenced command instead of the documented one.

    The flag is the *strength* of the evidence, and callers weigh it: MinerU
    labels nothing as code, so a PDF manual's evidence is always weak and the
    weak-evidence guards degrade to the plain "fold into the previous command"
    rule rather than to a second code path.
    """
    for element in block.elements:
        if element.element_type in _CODE_TYPES:
            found = _usage_identifiers(element.text)
            if found:
                return found[0], True
    for element in block.elements:
        if element.element_type in _CODE_TYPES:
            continue
        found = _usage_identifiers(element.text)
        if found:
            return found[0], False
    return "", False


def _classify(block: _Block, current: _Draft | None) -> tuple[str, str]:
    """Decide whether `block` opens a new command. Returns (shape, hint).

    Guards scale with how strongly the block claims to be a command:

    * an **identifier-named heading** naming a command other than the current
      one always opens a section, however deeply nested — a heading that spells
      a different command *is* a different command;
    * a **code-block usage line** opens a section unless it repeats the current
      command or sits inside it. `#### Examples` holding
      `global_placement -density 0.6` is caught by either half;
    * a **prose usage line** additionally loses to a sibling. `#### SEE ALSO`
      whose entire body is the word `replace_hier_module` is a cross-reference,
      not a command section, and it is a sibling of the `#### Options` and
      `#### EXAMPLES` blocks that belong to the command above it. Siblings must
      not gate the code-block case: every command in a manual is a sibling of
      every other, so that guard there would fold a whole manual into one
      section.
    * a **worked-examples appendix** (`_is_example_heading`) is the final
      backstop, checked only once the guards above have already failed to
      settle it. Measured on OpenROAD `rsz`: `## Example scripts` is a
      top-level sibling of nothing in particular — neither descendant nor
      sibling of whatever command was last documented — and its code block
      cites `read_sdc`, a command `rsz` never documents. Checking title last
      (not first) keeps a *nested* `#### Examples` citing another command
      caught by the descendant/sibling guards above, exactly like `#### SEE
      ALSO` is; this backstop only fires for the shape those guards cannot
      reach at all.
    """
    heading_ids = _identifiers(block.title)
    if heading_ids:
        if current is None or heading_ids[0] != current.command_hint:
            return "identifier_heading", heading_ids[0]
        return "", ""
    usage, from_code = _usage_evidence(block)
    if not usage:
        return "", ""
    if current is not None:
        if usage == current.command_hint:
            return "", ""
        if _is_descendant(block.section_path, current.section_path):
            return "", ""
        if not from_code and _is_sibling(block.section_path, current.section_path):
            return "", ""
    if _is_example_heading(block.title):
        return "", ""
    return "syntax_block", usage


def _pack(
    elements: Sequence[SectionElement], max_chars: int
) -> tuple[list[SectionElement], str, SectionTruncation]:
    """Concatenate under a hard character budget, accounting for the loss.

    Elements are kept whole so `text` and `elements` describe the same content
    — with one exception: an element that arrives with no real content kept
    yet (the heading is the only thing in `kept` so far, or `kept` is still
    empty) is clipped in place rather than dropped whole.

    The naive version of this test was `not kept`, which only ever fires on
    the very first element. A heading is *always* that first element, so
    every real section had already made `kept` non-empty by the time its
    first substantial element (a 12000-char parameter table, say) blew the
    budget — and that element took the whole-block drop below, leaving a
    section that is nothing but its own heading. Measured: a 120-parameter
    section came out of this as 36 characters of heading text, 1/120 args
    kept, and a command-reject ratio of 0.0 — the circuit breaker never saw
    the failure because there was nothing left to see it in. Testing "is
    `kept` still heading-only" instead of "is `kept` still empty" is what
    keeps that table's first N characters instead of losing the section.
    """
    limit = max(0, int(max_chars))
    original = len("\n".join(element.text for element in elements))
    kept: list[SectionElement] = []
    used = 0
    dropped = 0
    for index, element in enumerate(elements):
        extra = len(element.text) + (1 if kept else 0)
        if used + extra <= limit:
            kept.append(element)
            used += extra
            continue
        no_real_content_yet = all(item.element_type == "heading" for item in kept)
        if no_real_content_yet and limit > 0:
            room = max(0, limit - used - (1 if kept else 0))
            kept.append(replace(element, text=element.text[:room]))
            dropped = len(elements) - index - 1
        else:
            dropped = len(elements) - index
        break
    text = "\n".join(element.text for element in kept)
    return kept, text, SectionTruncation(
        applied=len(text) < original,
        original_chars=original,
        kept_chars=len(text),
        dropped_elements=dropped,
    )


def command_sections(
    elements: Sequence[Mapping[str, Any]],
    *,
    max_chars: int = MAX_SECTION_CHARS,
) -> list[CommandSection]:
    """Group one source's ordered elements into per-command sections.

    Input is exactly what `source_elements_for_chunking` returns
    (`id`, `element_type`, `text`, `section_path`), so C1b reuses the fetch it
    already has.

    Grouping folds sub-sections onto the nearest command heading — the same
    insight as `exact_lookup._group_path`, where `set_db`, `set_db > Arguments`
    and `set_db > Examples` are three breadcrumbs and one command. Two details
    beyond the plain "fold everything into the previous command":

    * content before the first command section is **dropped**. A document
      title, a table of contents and an introduction belong to no command, and
      attaching them to the first one would poison its extraction.
    * with breadcrumbs available, a non-command block joins the current section
      only if it is a descendant or a sibling; anything else closes the
      section. Descendants are the ordinary `### set_db` / `#### Options`
      shape. Siblings matter because manuals nest unevenly — one OpenROAD
      command is documented under an `####` heading whose parameter table lives
      in an `####` sibling, and a descendant-only rule drops that table
      entirely. Everything shallower still closes the section, so a manual's
      trailing `## Limitations`, `## Authors` and `## License` do not land
      inside the last command. Without breadcrumbs (MinerU) neither test can
      fire, so the plain "fold into the previous command" rule applies and the
      character budget bounds the damage.
    * a worked-examples appendix (`_is_example_heading`) folds into whatever
      command is still open, bypassing the descendant/sibling depth check
      entirely — `## Example scripts` sits at document top level while the
      command it should attach to may be nested several headings deep, so the
      ordinary depth test would close the section instead of feeding it this
      trailing evidence. It still respects a section that already closed:
      Examples after a `## License`-shaped chapter drop like anything else
      would.
    """
    drafts: list[_Draft] = []
    for block in _blocks(elements):
        current = drafts[-1] if drafts else None
        shape, hint = _classify(block, current)
        if shape:
            drafts.append(
                _Draft(
                    title=block.title,
                    section_path=block.section_path,
                    shape=shape,
                    command_hint=hint,
                    elements=list(block.elements),
                )
            )
            continue
        if current is None or current.closed:
            continue
        if _is_example_heading(block.title):
            current.elements.extend(block.elements)
            continue
        if _breadcrumb_parent(block.section_path) and _breadcrumb_parent(
            current.section_path
        ):
            if not (
                _is_descendant(block.section_path, current.section_path)
                or _is_sibling(block.section_path, current.section_path)
            ):
                current.closed = True
                continue
        current.elements.extend(block.elements)

    sections: list[CommandSection] = []
    for draft in drafts:
        kept, text, truncation = _pack(draft.elements, max_chars)
        sections.append(
            CommandSection(
                title=draft.title,
                section_path=draft.section_path,
                shape=draft.shape,
                command_hint=draft.command_hint,
                elements=tuple(kept),
                text=text,
                truncation=truncation,
            )
        )
    return sections


# ============================================================= 3. candidates
def command_candidates(
    section: CommandSection, *, limit: int = MAX_CANDIDATES
) -> list[str]:
    """The names an entry from this section may legally claim.

    Ordered by how much the source commits to each: the identifier that made
    this a command section, then its heading, then usage lines, then inline
    code. The order matters because the list is truncated — C0 measured 5/5
    command-name accuracy with a served list, and that only holds while the
    right name is on it.

    Flags are excluded (see `_identifiers`); the list is deduplicated and
    capped. Being slightly over-inclusive is safe — every candidate still has
    to survive the verbatim text check in `validate_entry` — while missing the
    real name costs the whole entry.
    """
    bound = max(0, int(limit))
    ordered: dict[str, None] = {}

    def add(term: str) -> bool:
        if term and term not in ordered:
            ordered[term] = None
        return len(ordered) < bound

    if not bound:
        return []
    if not add(section.command_hint):
        return list(ordered)
    for term in _identifiers(section.title):
        if not add(term):
            return list(ordered)
    for code_first in (True, False):
        for element in section.elements:
            if (element.element_type in _CODE_TYPES) is not code_first:
                continue
            for term in _usage_identifiers(element.text):
                if not add(term):
                    return list(ordered)
    for element in section.elements:
        for match in _INLINE_CODE_RE.finditer(element.text):
            for term in _identifiers(match.group(1)):
                if not add(term):
                    return list(ordered)
    return list(ordered)


# ================================================================= 4. slicing
def _extraction_window(
    section_text: str, names: Sequence[str], *, limit: int = MAX_SLICE_WINDOW_CHARS
) -> str:
    """One slice's own view of the source: the overview head plus every line
    mentioning `names`, bounded to `limit` characters.

    The overview head (`OVERVIEW_HEAD_CHARS`, document order) is what makes
    slice 0's window "overview head + syntax block" without any special
    casing — the heading, intro prose and syntax block are ordinarily the
    first thing a command section holds. Later slices carry the same head for
    command-name context, plus their own parameter lines pulled from wherever
    in the section those actually live (an Options table, usually, far past
    the head). This is a prompt-sizing view only: `validate_entry` always
    grounds against `section.text` in full, never this window.
    """
    head = (section_text or "")[:OVERVIEW_HEAD_CHARS]
    if not names:
        return head[:limit]
    lines = (section_text or "").splitlines()
    relevant = "\n".join(
        line for line in lines if any(_token_present(name, line) for name in names)
    )
    window = f"{head}\n{relevant}" if relevant else head
    return window[:limit]


def extraction_slices(
    section: CommandSection, *, params_per_slice: int = SLICE_PARAM_LIMIT
) -> list[ExtractionSlice]:
    """Split one command across as many model calls as its parameters need.

    Mandatory, not an optimisation: C0 watched a ~100-parameter section hit
    `finish_reason=length` and then return empty content on retry, i.e. the
    failure mode is silent data loss, not a visible error.

    A command with no parameters still gets one slice — `remove_fillers` has
    syntax and a description worth extracting. Slice 0 always carries the
    overview responsibility (syntax / description / examples) so those are
    asked for exactly once; later slices ask only for their parameter subset.
    The subsets partition the parameter list: disjoint, complete, in document
    order. Each slice's `text_window` is `_extraction_window`'s bounded view of
    the section for exactly that slice — see `ExtractionSlice` for why it never
    substitutes for `section.text` at grounding time.
    """
    names = parameter_names(section.text)
    size = max(1, int(params_per_slice))
    if len(names) <= size:
        return [
            ExtractionSlice(
                index=0,
                total=1,
                param_names=tuple(names),
                include_overview=True,
                text_window=_extraction_window(section.text, names),
            )
        ]
    groups = [names[start:start + size] for start in range(0, len(names), size)]
    return [
        ExtractionSlice(
            index=index,
            total=len(groups),
            param_names=tuple(group),
            include_overview=index == 0,
            text_window=_extraction_window(section.text, group),
        )
        for index, group in enumerate(groups)
    ]


# =============================================================== 5. grounding
def _window(text: str, value: str) -> str:
    """A bounded look at where the value was searched for — diagnostics only.

    Best effort by design: for a value that is not in the text there is no
    match to centre on, so a case-insensitive, dash-insensitive probe locates
    the near miss when there is one (which is exactly the interesting case:
    `density` vs `-density`) and the head of the section stands in when there
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
        window=_window(text, value),
    )


def _check_arg_name(raw: str, text: str) -> str:
    """Return a rejection reason for this parameter name, or "" if it grounds.

    The dash test is conditional on evidence rather than on shape. Demanding
    that every parameter start with `-` would be wrong — plenty of commands
    take positional arguments — but a bare `density` while the section itself
    writes `-density` is not a positional argument, it is the dropped dash C0
    measured.

    A verbatim text check on its own cannot catch it, which is why this test
    exists as a separate rule: the bare word is genuinely present in the
    section, both in prose and as the placeholder in `[-density
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
    several calls (C1b halves a slice whose answer overran the output budget)
    and the assignment is covered by their union — computing this per payload
    would report each half as having ignored the other half's parameters.

    An empty assignment yields an empty ledger, and that is a statement rather
    than a degenerate case: nothing was asked for, so nothing can be missing.
    It is what keeps a flagless command's positional arguments (which C1b's
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
    section: CommandSection,
    candidates: Sequence[str],
    *,
    assigned: Sequence[str] | None = None,
) -> ValidationResult:
    """Ground one extracted entry against the section text, field by field.

    Four layers, with deliberately different severities — a manual is worth
    extracting only if what comes back is the manual's own words, but a
    grounding rule strict enough to be useful will also fire on things worth
    keeping:

    * **command_name** — the only whole-entry veto. It must be on the served
      candidate list *and* appear verbatim at token boundaries. Failing either
      means the entry is about a command this section does not document, and
      nothing else in it can be trusted.
    * **command_name not in the heading or any usage line** — grounded, but
      possibly a *mentioned* command rather than the documented one. Recorded
      as `suspect_related` on the entry; a veto here would drop correct entries
      from manuals that name the command only in prose.
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
    parameter (or a scan of the whole section), and accepting it makes the
    per-slice ledger meaningless — a model answering one slice's assignment
    with another's would show a clean keep ratio while its own assignment
    silently vanished. The check runs AFTER `_check_arg_name` on purpose: a
    dropped dash is the accurate, measured diagnosis and must not be relabelled
    `arg_outside_slice` just because the stripped form is not in the list.

    An EMPTY (or omitted) assignment means "unconstrained", not "nothing may be
    returned". A flagless command's slice carries no parameter names, and
    positional arguments (`set_dont_use lib_cells`) are exactly what such a
    section documents; vetoing them would delete a real capability to enforce a
    rule about a list that does not exist. This mirrors the same exemption
    C1b's cache-admission validator already makes, and C1b's prompt now asks
    for those arguments outright in its no-flag branch — so the exemption is
    load-bearing, not merely permissive. The exemption is also strictly scoped
    to the empty case: with an assignment in hand, an unassigned name is still
    `arg_outside_slice`, because there the list DOES exist and answering some
    other slice from it is the infidelity this check was added for.

    `description` is passed through unchecked: prose cannot be matched
    verbatim, and binding it to element anchors is C1b's job (see
    `ValidatedEntry.anchor_element_ids`). `examples` is prose too and is never
    grounded against `text`, but its outer shape (list vs. scalar vs. anything
    else) IS checked — see `_coerce_examples` — because the failure mode there
    is not "wrong content" but "wrong container", which grounding could never
    have caught anyway.

    Everything is searched in `section.text` — the text the model was shown.
    """
    text = section.text
    payload: Mapping[str, Any] = entry or {}
    allowed = tuple(str(name) for name in candidates or ())
    assignment = tuple(str(name) for name in assigned or ())
    rejections: list[Rejection] = []

    name = str(payload.get("command_name") or "").strip()
    if not name:
        rejections.append(_reject(text, "command_name", "", "empty"))
    elif name not in allowed:
        rejections.append(
            _reject(text, "command_name", name, "not_in_candidates")
        )
    elif not _token_present(name, text):
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

    heading_haystack = f"{section.title}\n{section.section_path}"
    suspect = not (
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


# ================================================================ 6. outcomes
def section_outcome(
    section: CommandSection,
    candidates: Sequence[str],
    results: Sequence[ValidationResult],
    *,
    uncovered_args: Sequence[str] = (),
) -> SectionOutcome:
    """The per-section ledger, including what was *not* extracted.

    Uncovered candidates are the point of keeping the list: a manual section
    that served four plausible names and produced one entry is exactly the
    thing a person should look at, and no amount of per-entry validation
    surfaces it.

    `uncovered_args` is the parameter-level version of the same idea, and it
    arrives from the caller rather than being derived from `results` for a
    structural reason: coverage is a property of a SLICE (its assignment vs
    the union of everything its calls answered), while a `ValidationResult` is
    a property of one payload. Deriving it here would count each half of a
    halved slice as having ignored the other half's parameters. Each name is
    also folded into the rejection ledger — after the real rejections, so a
    long uncovered list can never push a grounding failure out of the report.
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
            if len(rejections) < MAX_SECTION_REJECTIONS:
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
        if len(rejections) < MAX_SECTION_REJECTIONS:
            rejections.append(_reject(section.text, "arg", name, "arg_not_returned"))
        else:
            rejections_overflow += 1
    return SectionOutcome(
        section_path=section.section_path,
        title=section.title,
        candidates=served,
        accepted_names=tuple(accepted),
        uncovered_candidates=tuple(
            name for name in served if name not in accepted
        ),
        rejections=tuple(rejections),
        rejections_overflow=rejections_overflow,
        truncation=section.truncation,
        entries_seen=entries_seen,
        command_rejects=command_rejects,
        args_seen=args_seen,
        args_kept=args_kept,
        uncovered_args=missing[:MAX_SECTION_REJECTIONS],
        args_uncovered=len(missing),
    )


def catalog_stats(outcomes: Sequence[SectionOutcome]) -> CatalogStats:
    """Run-level totals for C1b's circuit breaker.

    The ratio is published, the threshold constant is published, and the
    decision is not made here: "abort the job" is a policy that needs the job's
    own context (how far in, what the source is, whether a person is waiting).

    `args_keep_ratio`'s denominator is `args_seen + args_uncovered`, NOT
    `args_seen` alone, and the difference is the whole point of the ledger: a
    model answering 1 of 20 assigned parameters scores 1/1 on what it returned
    and 1/20 on what it was asked for. Only the second number can tell that run
    apart from a clean one, and a keep ratio that a model can raise by
    answering LESS is worse than no ratio at all.
    """
    rows = list(outcomes or ())
    entries_seen = sum(row.entries_seen for row in rows)
    command_rejects = sum(row.command_rejects for row in rows)
    args_seen = sum(row.args_seen for row in rows)
    args_kept = sum(row.args_kept for row in rows)
    args_uncovered = sum(row.args_uncovered for row in rows)
    args_asked = args_seen + args_uncovered
    return CatalogStats(
        sections=len(rows),
        entries_seen=entries_seen,
        command_rejects=command_rejects,
        command_reject_ratio=(
            round(command_rejects / entries_seen, 4) if entries_seen else 0.0
        ),
        args_seen=args_seen,
        args_kept=args_kept,
        args_uncovered=args_uncovered,
        args_keep_ratio=(round(args_kept / args_asked, 4) if args_asked else 0.0),
        truncated_sections=sum(1 for row in rows if row.truncation.applied),
    )
