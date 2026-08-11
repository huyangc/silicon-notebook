"""Command-catalog extraction: the job, the model calls, and the apply step.

This is stage C1b of Plan C. ``app/services/command_catalog.py`` (C1a) decides
everything that can be decided without IO — how a document is packed into
model-sized windows, which names an entry from a window may claim, whether a
window is worth a call at all, how one window is split across calls, and what
survives grounding. This module is the part that touches the world: it reads a
source's elements, drives one model call per slice, writes the reviewable
candidate rows, and — on an explicit human confirmation — lands the confirmed
rows in a knowhow table.

**v2 geometry, in one paragraph, because it changes what every counter below
means.** v1 asked layout rules which elements formed one command's section and
showed the model only those; a command whose section the rules never opened was
never offered at all. v2 packs the whole document, in order, into
``WINDOW_CHARS`` windows with nothing dropped, and asks each window which
commands it documents — so one reply now carries a LIST of entries, one command
can span several windows (``carry_candidates`` relays the name, and this module
merges the later window's parameters into the row the first one wrote), and a
window with nothing claimable — or nothing to extract — costs nothing because
it is never sent (``window_needs_model``). The ``sections_*`` columns and the
``catalog_section_done`` event keep their names — they count windows now — for
the ordinary reason: renaming them is a migration and an observability break,
and neither buys anything. The two do NOT count the same thing any more,
deliberately: the COLUMNS count every window (they are the progress bar's
numerator and denominator, and a skipped window is still a window that is
done), while the EVENT is only emitted for windows that actually made a call.
A mostly-prose PDF has thousands of skipped windows and an event per no-op
would be the bulk of this run's observability for none of its work.

Four properties are load-bearing and every change here has to preserve them.

**Cost is proportional to slices, not to retries or reflection.** One planned
model call per ``ExtractionSlice``, no second opinion, no refinement pass. The
only extra calls are the two documented remedies below, and both are bounded.

**Nothing silently produces an empty catalog.** A source that is not a manual,
a model that answers about the wrong command, a grounding rule that rejects
everything — each of those has to end as a *visible* outcome. The circuit
breaker below turns a run that is mostly rejecting into a failed job with a
user-readable reason instead of a plausible-looking near-empty result, and the
rejected entries are written to the candidate table so a person can see why.

**The job row always reaches a terminal state — or stops mattering.**
Including on Ctrl-C/SIGTERM, which inherit ``BaseException`` and never reach
``except Exception``. The single-flight guard is a partial unique index
covering queued AND running, so a row stranded in either state locks that
source out of extraction forever — and an offline process has no standing to
clean up a row that may belong to a live backend, so it cannot be repaired
until the next restart. A source or notebook delete mid-run is the other side
of the same coin: it cascades the row (and every candidate under it) away
(``ON DELETE CASCADE``) with nobody calling ``cancel()``, so a liveness probe
(``_raise_if_stopped``) runs at every point a cancel check already runs —
including inside every model call's own halving and coverage retries — and
stops the run the moment the row is gone, via its own ``CatalogJobGone``
signal rather than piggy-backing on ``CatalogCancelled`` (see that type's
docstring for why the two must not share a handler). No further model calls,
no further writes: a row an FK already erased has nothing left to reach a
terminal state IN, and paying for more model calls on its behalf would be
pure waste.
"""
from __future__ import annotations

import json
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence

from app.core.llm import cap_kwargs
from app.repositories.ports import (
    CATALOG_MAX_CANDIDATE_PAGE,
    CATALOG_TERMINAL_STATUSES,
    CatalogJobAlreadyRunning,
    CatalogStorePort,
)
from app.services.cancellation import AskCancelled, CancelEvent
from app.services.command_catalog import (
    ARGS_KEEP_ALERT_RATIO,
    COMMAND_REJECT_ALERT_RATIO,
    MAX_WINDOW_REJECTIONS,
    MIN_WINDOWS_BEFORE_ALERT,
    WINDOW_CHARS,
    AssignmentCoverage,
    ExtractionSlice,
    ExtractionWindow,
    ValidationResult,
    WindowOutcome,
    assignment_coverage,
    carry_candidates,
    catalog_stats,
    extraction_slices,
    extraction_windows,
    validate_entry,
    window_candidates,
    window_needs_model,
    window_outcome,
    window_segments,
)
from app.services.kg.json_utils import safe_json
from app.services.model_work import ModelProviderError
from app.services.source_display import source_display_title


# The workload is `kg_extract`, deliberately reused rather than registered as a
# new one: this is the same kind of work (bounded, background, structured
# extraction from one document's text), and every new workload id is a new
# configuration surface a deployment has to bind before the feature works at
# all. Plan C's brief is explicit that no new workload configuration surface is
# introduced.
CATALOG_WORKLOAD = "kg_extract"

# --------------------------------------------------------------------- bounds
# The cost preview reads a bounded PREFIX of the source rather than the whole
# thing: it exists to tell a person what an extraction would cost, and a
# preview that scans the document it is estimating defeats its own purpose. The
# consequence is disclosed (`sampled`), never hidden.
PREVIEW_ELEMENT_LIMIT = 2_000
PREVIEW_ELEMENT_CHARS = 1_200
# How far a `length`-shaped failure may keep halving one slice.
#
# The bound is worth writing out rather than eyeballing, because "it halves, so
# it is logarithmic" is wrong — the halves are *both* re-asked. With
# f(d) = 1 + 2·f(d+1) while splitting and f(max) = 2 (the call plus its one
# retry), depth 2 costs at most 1 + 2·(1 + 2·2) = 11 calls for a single slice
# that never returns anything usable. That is a bounded worst case on a
# pathological slice, not the expected cost: C0's measured failure is a slice
# whose answer overruns the output budget, and one halving already fixes that.
# Raising this number multiplies, so raise it only with a measurement.
MAX_SLICE_SPLIT_DEPTH = 2
# Closed-form, not an independently enforced bound: `MAX_SLICE_SPLIT_DEPTH` is
# the actual limit the code checks (`_extract_slice`'s `depth < MAX_SLICE_SPLIT_DEPTH`
# guard); this is only that recursion's worst case spelled out as a number for
# `test_a_slice_that_never_answers_stays_within_its_call_bound` to pin. Raising
# `MAX_SLICE_SPLIT_DEPTH` means recomputing this by hand — nothing derives it
# automatically.
MAX_CALLS_PER_SLICE = 11  # 1 + 2·(1 + 2·2); see above
# The breaker's third axis: the share of slices that never produced a usable
# answer at all. Kept local rather than in C1a because C1a only ever sees
# what a model DID return — a slice that answers nothing produces no
# `ValidationResult` for it to publish a ratio about.
SLICE_FAILURE_ALERT_RATIO = 0.20
# When a slice answers, but for only a fraction of the parameters it was
# assigned, the remedy is the SAME halving `malformed` gets — ask for fewer
# parameters at a time — because it is the same underlying failure: the answer
# did not fit what was asked for. These two numbers are the gate, and both
# exist to keep that remedy from becoming a general-purpose retry:
#
# * below `MIN_ASSIGNED_FOR_COVERAGE_RETRY` parameters there is nothing to
#   halve into (one parameter answered out of two is a coin flip, not a
#   truncation signal), and
# * the answer must also be SHORT — see `_coverage_retry_warranted`. A model
#   that returned as many parameters as it was assigned and simply got them
#   wrong has a grounding problem, and asking it for half as many buys a
#   second wrong answer at full price. That case is the args axis of the
#   breaker's job, not this remedy's.
#
# Policy, so it lives here rather than in C1a — C1a publishes the coverage,
# C1b decides what to spend on it. The retry is allowed at depth 0 ONLY, which
# is what keeps `MAX_CALLS_PER_SLICE` unchanged (see `_extract_slice`).
SLICE_COVERAGE_RETRY_RATIO = 0.50
MIN_ASSIGNED_FOR_COVERAGE_RETRY = 4
# A candidate row carries a short look at where it came from, so the review UI
# can show provenance without re-reading the source.
CANDIDATE_EXCERPT_CHARS = 400
# `description`/`examples` are the two model-authored fields `validate_entry`
# deliberately does NOT ground against the source text (prose cannot be
# matched verbatim — see its docstring), so unlike every other candidate
# field, nothing else caps how much of either a model may write. A candidate
# row is written to the DB and rendered in the review UI on every accepted
# entry, so these bounds are the backstop against a misbehaving model turning
# one window into an unbounded row.
MODEL_DESCRIPTION_CHARS = 1000
MODEL_EXAMPLE_CHARS = 500
MAX_MODEL_EXAMPLES = 8
# `args[].desc` is the third model-authored, ungrounded field, and it is the
# one with a multiplier in front of it: `MODEL_DESCRIPTION_CHARS` caps ONE
# string per candidate, while a 200-parameter command carries 200 of these. So
# it needs both bounds — per description, and per candidate row. The aggregate
# budget cuts from the TAIL (later parameters lose their prose, never their
# name/required/default, which are grounded and are what the catalog is for)
# and the number of parameters it bit is reported, never silently applied.
MODEL_ARG_DESC_CHARS = 400
MODEL_ARG_DESC_TOTAL_CHARS = 8000
# Element anchors kept per candidate. `ValidatedEntry.anchor_element_ids` is the
# seat C1a reserved for exactly this: description/examples are prose and cannot
# be checked verbatim, so they are bound to element ids instead.
MAX_ANCHOR_ELEMENTS = 12
# Apply is a human-confirmed action on a page of candidates, never a whole-run
# sweep, so it inherits the store's own page ceiling.
MAX_APPLY_CANDIDATES = CATALOG_MAX_CANDIDATE_PAGE

# The knowhow table one applied catalog lands in. `命令` is the anchor column:
# it is the row title, which is what makes each row a graph node named after
# the command.
CATALOG_TABLE_TITLE_PREFIX = "命令目录："
CATALOG_COMMAND_COLUMN = "命令"
# Every non-anchor column is `attribute` on purpose. `entity` means "the names
# listed here are merged into the graph as tools/objects", which fits a column
# of tool names and does NOT fit a fenced block of example invocations — that
# would project shell snippets as graph entities.
CATALOG_TABLE_COLUMNS = (
    {"name": CATALOG_COMMAND_COLUMN, "role": "anchor"},
    {"name": "语法", "role": "attribute"},
    {"name": "参数", "role": "attribute"},
    {"name": "说明", "role": "attribute"},
    {"name": "示例", "role": "attribute"},
    {"name": "出处", "role": "attribute"},
)

# A LIST, because a window is a slab of the document rather than one command's
# section: the model is asked to enumerate every command the window documents,
# and `entries: []` is a legal answer (this window documents none) rather than a
# failure. See `_call` for where the shape is enforced and `_prompt` for how it
# is asked for.
_SCHEMA_HINT = (
    '{"entries": [{"command_name": "string", "syntax": "string", '
    '"description": "string", "args": [{"name": "string", "required": true, '
    '"desc": "string", "default": "string"}], "examples": ["string"]}]}'
)

# User-readable copy. Everything a route hands to `user_error()` comes from
# here, and everything else this module records is diagnostic-only.
CIRCUIT_OPEN_MESSAGE = (
    "校验拦截率异常，疑似文档格式与识别规则不兼容；本次命令识别已停止，未生成命令目录。"
)
# A run that paid for model calls and produced literally nothing — no
# candidate, no rejected row, not one entry the model even attempted. That is
# NOT a success with an empty result: `succeeded` next to an empty review
# panel reads as "this document has no commands", which is a claim this
# feature has no evidence for. Every other empty-ish outcome stays
# `succeeded`, because each of them leaves the user something to look at: a
# run with rejected rows shows WHY nothing was kept, and a run that skipped
# every window never called a model at all (a source that is simply not a
# manual, correctly costing nothing). Same provenance rule as every other
# constant here — the route hands this to `user_error()`, so it is curated
# copy and says what to do next.
NOTHING_EXTRACTED_MESSAGE = (
    "没有识别出任何命令；这份来源可能不是命令手册，或命令的写法与识别规则不符，"
    "请确认来源内容后再试。"
)
INTERNAL_FAILURE_MESSAGE = "命令目录识别失败，请稍后重试。"
# R6 P1 修正:旧文案「已生成的候选已保留，可重新发起识别」承诺了一件拦截逻辑
# 不兑现的事——保留不等于够得着:`.../job` 只返回最近一次任务，若这句话诱导
# 用户真的立刻重新发起，新任务一发起旧候选就永久孤儿化（见
# `_reject_if_pending_candidates` 的文档）。改成明确指向审阅面板，与新拦截行为
# 一致。三处措辞同步维护:本常量、`repositories/sqlite/migrations.py` 的启动
# 恢复 SQL、`repositories/postgres/maintenance.py` 的同款 SQL。
INTERRUPTED_MESSAGE = (
    "服务中断导致命令目录识别未完成；已生成的候选已保留，"
    "请先在审阅面板确认或跳过，再重新发起识别。"
)
CANCELLED_MESSAGE = "命令目录识别已取消。"
SUBMISSION_FAILED_MESSAGE = "命令目录识别任务未能启动，请稍后重试。"
MODEL_UNAVAILABLE_MESSAGE = "模型服务未配置或不可用，无法识别命令目录。"
APPLY_TABLE_SHAPE_MESSAGE = (
    "目标表缺少「命令」列，无法确认；请恢复该列或删除这张表后重新确认。"
)
# The reject_info["reason"] code `dismiss()` writes — the review panel's other
# writer of `dismissed` state (see `_apply_locked`'s conflict branch below)
# writes `conflict_existing_row`. Three codes, one shared `dismissed` state:
# apply's conflict is an AUTOMATIC skip ("this command already has a row");
# `user_dismissed` is a HUMAN choosing not to keep a candidate the R5/R6
# pending-candidates guard would otherwise leave unreachable forever; and
# `source_reparsed` (R8) is the system expiring candidates whose source was
# reparsed underneath them. The frontend's `dismissReasonText()` maps all
# three to distinct review copy.
USER_DISMISSED_REASON = "user_dismissed"
SOURCE_REPARSED_REASON = "source_reparsed"

# R8 (codex PR #412 review): the source's elements changed between the run and
# this confirm. Wording says what happened AND what to do; the candidates are
# expired in the same breath (see `_require_current_generation`), so "重新识别"
# is genuinely available by the time the user reads this.
SOURCE_STALE_MESSAGE = (
    "来源已重新解析，本次识别结果已过期，请重新识别。"
)
# R10 (codex PR #412 review): a reparse is IN FLIGHT — the elements have not
# been swapped yet, so `SOURCE_STALE_MESSAGE` would be a lie, and confirming
# now would land rows the swap is about to invalidate. Deliberately does NOT
# say 「请重新识别」 the way the stale copy does: nothing has been expired here
# (see `_source_write_barrier` for why expiring on a guess would be wrong), so
# the honest next step is to wait for the parse this message names.
SOURCE_BUSY_MESSAGE = (
    "这份来源正在重新解析，请等解析完成后再确认或跳过。"
)
# R4 (codex PR #494 review): the same fact — a reparse is in flight, nothing has
# been expired, waiting is the remedy — worded for the endpoint that is asking.
# `preview` neither confirms nor skips anything, so `SOURCE_BUSY_MESSAGE`'s verbs
# would name two actions the user did not take. The exception type is shared
# (`CatalogSourceBusy` means "a reparse is in flight and this call could not
# proceed"); only the copy differs, exactly as the empty/too-many pairs already
# differ between apply and dismiss.
SOURCE_REPARSING_MESSAGE = (
    "这份来源正在重新解析，请等解析完成后再看识别成本。"
)
# How long apply/dismiss wait for the source's parse barrier before answering
# `SOURCE_BUSY_MESSAGE`. Any positive value is CORRECT (the barrier is what
# makes the write safe, not the length of the wait); this one is tuned for the
# only two holders that exist — `process_source` and the checkup backfill —
# both of which hold for far longer than any wait worth making a user sit
# through. Seconds rather than milliseconds only so that a barrier changing
# hands under a slow disk is absorbed instead of reported as contention.
SOURCE_LOCK_WAIT_SECONDS = 2.0
# How many times `preview` re-reads its pair of statements when a reparse
# committed between them. One retry, not a loop: the second failure is evidence
# that a reparse is actively running rather than that this call was unlucky, and
# a preview that keeps re-reading to win a race is spending the user's wait on a
# number that will be stale the moment the swap lands anyway.
_PREVIEW_READ_ATTEMPTS = 2
# Parse-status preconditions. A source still being parsed has no elements (or
# only a prefix of them), so both the cost preview and the run itself would be
# reading a document that is not there yet — the preview would under-report and
# the run would extract a fraction of the manual and call it complete.
SOURCE_NOT_PARSED_MESSAGE = "来源尚未完成解析，请等待解析完成后再识别。"
SOURCE_PARSE_FAILED_MESSAGE = (
    "来源解析失败，无法识别命令目录；请重新解析或重新上传后再试。"
)
# The repository-wide whitelist for "terminal AND should already have parse
# output" (`sources_without_elements`, `sources_missing_paper_meta`, the
# retrieval predicates — same three values, same reason). `extracting` and
# `extracted` are KG-extraction stages that happen AFTER the elements have
# landed, so they are legal starting points; `queued`/`parsing`/`uploaded`/
# `metadata-only` are not (no elements yet, by design in the last case), and
# `failed` gets its own copy because the remedy is different.
PARSED_SOURCE_STATUSES = frozenset({"parsed", "extracting", "extracted"})


def pending_candidates_message(pending: int) -> str:
    """`CatalogPendingCandidates`'s 409 copy. A function, not a constant,
    because the count is per-request — but the WORDING still lives here
    rather than being assembled at the route, for the same reason every
    other message in this module does: one place decides what a user reads.

    R6 P1: worded to read IDENTICALLY to `command-catalog-model.ts`'s own
    `catalogPendingReviewNote` — this guard now fires behind either entry
    point (`catalogBlocksRestart` covers all three terminal statuses, not
    just `succeeded`), so a user who somehow reaches the 409 instead of the
    disabled button must not see a second, differently-worded explanation.
    """
    return f"仍有 {max(0, int(pending))} 条待审阅候选，请先确认或跳过后再重新识别。"


class CatalogCancelled(Exception):
    """The owner cancelled this run between slices."""


class CatalogJobGone(Exception):
    """The job row this run needs to settle or report on no longer exists.

    Deliberately NOT a subtype of `CatalogCancelled`: `run()`'s `except
    CatalogCancelled` clause reads the row back
    (`self.catalog.get_job(job_id)`) to build the `catalog_job_finished`
    event, and that read would raise this SAME `KeyError` a second time —
    uncaught, inside an `except` clause, past every remaining handler. This
    gets its OWN clause instead, one that settles through `_settle` (a
    documented no-op for a row that is not there) and skips the event
    entirely, because there is nothing left for a caller to read back.

    Raised by `CommandCatalogService._raise_if_stopped` when its liveness
    probe's `get_job` misses — a source or notebook delete cascaded
    `catalog_jobs`/`catalog_candidates` away (`ON DELETE CASCADE`) while this
    run was still going, and nobody called `cancel()`, so nothing ever set
    the run's own cancel event.
    """


class CatalogCircuitOpen(Exception):
    """Grounding rejected so much of this run that continuing is dishonest."""

    def __init__(self, diagnostic: str) -> None:
        super().__init__(diagnostic)
        self.diagnostic = diagnostic


class CatalogModelUnavailable(RuntimeError):
    """The extraction workload has no usable model service bound."""


class CatalogApplyTargetInvalid(ValueError):
    """The target knowhow table can no longer receive catalog rows.

    A dedicated type rather than a bare ``ValueError`` because its message IS
    user copy: the route hands it to ``user_error()``, and this codebase decides
    "may a user see this" by provenance, never by inspecting a string. Every
    other ``ValueError`` reaching that route stays a diagnostic 400.
    """


class CatalogPendingCandidates(RuntimeError):
    """The source's latest job still has unreviewed (``candidate``-state) rows.

    ``.../job`` only ever returns a source's MOST RECENT job, so starting a
    new one immediately shadows the old candidates: they stay in the table,
    reachable by nobody, forever — the review UI has no route back to a job
    id it never learned. The frontend already demotes its "重新识别" entry
    point for this case (R5 P2); this is the data-layer backstop for callers
    that reach ``start`` some other way (a retry, another tab, a stale page).

    ``.pending`` carries the count so the route can hand the user a specific
    number rather than a generic "some candidates are still pending".
    """

    def __init__(self, source_id: str, pending: int) -> None:
        super().__init__(source_id)
        self.pending = pending


class CatalogSourceNotParsed(RuntimeError):
    """The source has no usable parse output to extract commands from.

    ``.parse_status`` carries the row's own value so the route can pick between
    the two user messages (still parsing vs parse failed) WITHOUT parsing an
    exception string — "may a user see this" is decided by provenance here, and
    the copy itself lives in this module's curated constants.
    """

    def __init__(self, source_id: str, parse_status: str) -> None:
        super().__init__(source_id)
        self.parse_status = parse_status


class CatalogSourceChanged(RuntimeError):
    """The source was reparsed after this job read it.

    Raised by ``apply``/``dismiss`` when the live element generation no longer
    matches the one recorded on the job row. Every candidate of this job names
    a command, an excerpt and a section path taken from the OLD elements, so
    confirming one would write content the document no longer contains. The
    raising path also expires this job's remaining candidates (see
    ``_require_current_generation``), which is what makes the accompanying
    "请重新识别" copy honest: the pending-candidates guard is released in the
    same call that refuses the confirm.
    """


class CatalogSourceBusy(RuntimeError):
    """A reparse of this source is in flight; the confirm cannot be made safe.

    Distinct from ``CatalogSourceChanged`` in both fact and remedy. There, the
    elements ALREADY changed and the candidates are provably dead, so they are
    expired and the user is told to re-run. Here the swap has not happened yet
    — it may not even happen (a parse that fails before ``replace_elements``
    leaves the elements, and therefore every candidate, perfectly valid) — so
    nothing is expired and the user is told to wait.

    Two raise sites, one for each half of a reparse's lifecycle that the
    generation check alone cannot see:

    * After a bounded wait for the source's parse barrier (see
      ``CommandCatalogService._source_write_barrier``) — the swap itself is
      under way or queued right behind another writer.
    * ``CommandCatalogService._require_not_parsing`` (R12, codex PR #412
      review) — the parse STAGE is under way but has not reached the barrier
      yet (``process_source`` marks the source ``parsing`` long before it
      takes the chunk lock), so the barrier is free and the generation check
      passes even though a reparse is actively in flight.
    """


@dataclass(frozen=True)
class CatalogPreview:
    """What extracting this source would cost, in v2's own units.

    ``estimated_windows`` is exact when the bounded prefix turned out to be
    the whole document, and an explicit LOWER BOUND otherwise (``sampled``
    says which). ``estimated_calls`` describes the prefix and nothing else:
    the windows past it are not priced, because pricing text nobody read is
    wrong in both directions at once — the skip gate makes a prose window
    free, while a parameter-dense one is several slices. ``windows_in_prefix``
    is how much of the document that measurement covered, so a caller can say
    "the first X of at least N segments" without inventing the rest.

    ``skipped_windows_in_prefix`` is what the zero-model-call gate
    (``window_needs_model``) rejected inside that prefix: it is the number
    that explains why ``estimated_calls`` can be well under
    ``windows_in_prefix`` on a document that is mostly prose.

    v1's ``signal``/``estimated_sections`` are gone rather than kept as
    aliases: shape detection retired with sectioning, and a count of "command
    sections" is not a thing v2 can compute or would mean anything by.
    """

    source_id: str
    source_title: str
    estimated_windows: int
    estimated_calls: int
    windows_in_prefix: int
    skipped_windows_in_prefix: int
    sampled: bool
    element_limit: int


@dataclass
class _SliceOutcome:
    """One model call's result, reduced to the three cases that matter."""

    payload: Mapping[str, Any] | None = None
    kind: str = ""  # "" | "empty" | "malformed"


@dataclass
class _FlushedCandidate:
    """A candidate row this run already wrote, kept so a LATER window can add
    to it instead of appending a second row for the same command.

    ``entry`` is the same accumulator shape ``_merge_entry`` builds (see it):
    the merged syntax/description/args/examples plus the bounded ledgers and
    the anchors/excerpt. Holding it is what makes the cross-window merge
    first-writer-wins across windows for free — the next window seeds its own
    accumulator from this and merges into it, so the union is computed by the
    same code path a multi-slice command inside ONE window already uses.

    Bounded by the number of distinct commands in the document, and each entry
    is bounded by the same per-row caps that bound the DB row it mirrors.
    """

    id: str
    entry: dict


@dataclass
class _PendingUpdate:
    """A command an earlier window already wrote a row for, re-rendered with
    this window's contribution merged in.

    Carries the merge accumulator (``entry``) alongside the stored shapes
    because the update can FAIL — the row may have been applied or dismissed
    by a reviewer since it was written — and the fallback for that is to append
    a fresh row, which needs the accumulator to register in the run's
    ``flushed`` registry exactly like a first-time write does.
    """

    candidate_id: str
    command_name: str
    entry: dict
    payload: dict
    reject_info: dict


@dataclass
class _WindowWork:
    rows: list[dict] = field(default_factory=list)
    # Commands ALREADY written by an earlier window: revised in place rather
    # than appended a second time (see `_PendingUpdate`).
    updates: list[_PendingUpdate] = field(default_factory=list)
    # (position, command_name, accumulator) for commands written for the FIRST
    # time here. The row ids are assigned by the store, so `run` reads them
    # back by position after the insert and registers them for later windows.
    new_entries: list[tuple[int, str, dict]] = field(default_factory=list)
    results: list[ValidationResult] = field(default_factory=list)
    candidates: tuple[str, ...] = ()
    slices: int = 0
    slice_failures: int = 0
    calls: int = 0
    # Assigned parameters no slice of this window ever answered for. Bounded
    # by the window's own parameter list (itself bounded by `WINDOW_CHARS`),
    # and capped again when it reaches `WindowOutcome`.
    uncovered_args: list[str] = field(default_factory=list)


def _clip(value: str, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit]


def _rejection_records(result: ValidationResult) -> list[dict]:
    return [
        {
            "field": rejection.field,
            "value": rejection.value,
            "reason": rejection.reason,
            "window": rejection.window,
        }
        for rejection in result.rejections
    ]


def _reject_info(
    fields: Sequence[Mapping[str, Any]],
    overflow: int = 0,
    *,
    desc_overflow: int = 0,
) -> dict:
    """A candidate row's `reject_info`, bounded at `MAX_WINDOW_REJECTIONS`.

    `overflow` is the count of records that got cut, carried alongside rather
    than silently dropped — mirroring `WindowOutcome.rejections_overflow` in
    `command_catalog.py`. Both write sites (a single slice's own rejections,
    and `_merge_entry`'s cross-slice accumulator) already hand this at most
    `MAX_WINDOW_REJECTIONS` items; the cap here is the last line of defence
    for whichever one changes first.

    `desc_overflow` is a SEPARATE key rather than more of `overflow`, and
    deliberately so: the two count different losses (rejection records that did
    not fit the ledger vs parameter descriptions cut by
    `MODEL_ARG_DESC_TOTAL_CHARS`), and folding them together would leave a
    number that answers neither question. Omitted when zero, like `overflow`.
    """
    all_fields = list(fields)
    kept = all_fields[:MAX_WINDOW_REJECTIONS]
    total_overflow = max(0, overflow) + (len(all_fields) - len(kept))
    info: dict[str, Any] = {"fields": kept}
    if total_overflow:
        info["overflow"] = total_overflow
    if desc_overflow > 0:
        info["desc_overflow"] = int(desc_overflow)
    return info


_ORDINAL_LABEL_RE = re.compile(r"^window \d+$")


def _window_label(window: ExtractionWindow) -> str:
    """The candidate row's `section_path` column for a window.

    The window's inherited breadcrumb when it has one, and an ordinal label
    otherwise. This is an INTERNAL provenance label on a stored row, not UI
    copy: the review panel decides how to present it (T4), and the vocabulary
    guard's user-facing surface is that panel's, not this column's. A window
    boundary falls where the character budget put it, so this is a
    best-effort breadcrumb by construction — it takes part in no decision.
    """
    return window.provenance or f"window {window.ordinal + 1}"


def _seed_accumulator(
    record: "_FlushedCandidate | None",
    suspect_related: bool,
    anchors: Sequence[str],
    excerpt: str,
) -> dict:
    """A fresh merge accumulator — or a COPY of what an earlier window already
    wrote for this command, so the cross-window merge is the same code path as
    the cross-slice one (see `CommandCatalogService._merge_entry`).

    A copy rather than the record's own dict: nothing may mutate the registry's
    view of a persisted row until the revised payload is actually handed to the
    store, and the caller replaces the record wholesale when it is.
    """
    if record is None:
        return {
            "syntax": "",
            "description": "",
            "args": [],
            "examples": [],
            "suspect_related": suspect_related,
            "rejections": [],
            "rejections_overflow": 0,
            "desc_chars": 0,
            "desc_overflow": 0,
            "anchors": list(anchors),
            "excerpt": excerpt,
        }
    previous = record.entry
    return {
        "syntax": previous["syntax"],
        "description": previous["description"],
        "args": [dict(arg) for arg in previous["args"]],
        "examples": list(previous["examples"]),
        "suspect_related": previous["suspect_related"],
        "rejections": [dict(item) for item in previous["rejections"]],
        "rejections_overflow": previous["rejections_overflow"],
        "desc_chars": previous["desc_chars"],
        "desc_overflow": previous["desc_overflow"],
        "anchors": list(
            dict.fromkeys(list(previous["anchors"]) + list(anchors))
        )[:MAX_ANCHOR_ELEMENTS],
        "excerpt": previous["excerpt"],
    }


def _candidate_payload(entry: Mapping[str, Any]) -> dict:
    """A merge accumulator rendered as the candidate row's stored payload.

    One function because there are now two writers — the insert of a brand-new
    command's row and the revision of one an earlier window wrote — and a
    revision that assembled the payload differently from the insert would make
    a merged row a different SHAPE from an unmerged one for every reader
    downstream (`CommandCatalogCandidate.of`, `_catalog_cells`).
    """
    return {
        "syntax": entry["syntax"],
        "description": entry["description"],
        "args": entry["args"],
        "examples": entry["examples"],
        "anchors": entry["anchors"],
        "excerpt": entry["excerpt"],
        "suspect_related": entry["suspect_related"],
    }


def _extend_rejections(
    entry: dict, records: Sequence[Mapping[str, Any]]
) -> None:
    """Append to a merged entry's bounded rejection ledger, counting the cut.

    One helper rather than two copies of the same three lines, because there
    are now two writers into that ledger — a slice's own grounding rejections
    and the parameters it was assigned and never answered — and the cap has to
    be shared between them, not applied twice to two independent budgets.
    """
    room = max(0, MAX_WINDOW_REJECTIONS - len(entry["rejections"]))
    items = list(records)
    entry["rejections"].extend(items[:room])
    entry["rejections_overflow"] += max(0, len(items) - room)


class CommandCatalogService:
    """Preview, run, review and apply one source's command catalog.

    Backend-neutral by construction: every seam it holds is a port or a plain
    callable, so it lives on the neutral repository runtime rather than being
    built twice per backend.
    """

    def __init__(
        self,
        *,
        catalog: CatalogStorePort,
        sources: Any,
        chunks: Any,
        knowhow: Any,
        models: Any,
        event_log: Any,
        now: Callable[[], str],
        current_user_id: Callable[[], str],
    ) -> None:
        self.catalog = catalog
        self.sources = sources
        self.chunks = chunks
        self.knowhow = knowhow
        self.models = models
        self.event_log = event_log
        self.now = now
        self.current_user_id = current_user_id
        self._cancels: dict[str, threading.Event] = {}
        self._cancels_lock = threading.Lock()
        # Apply is read-then-write across two stores (does the table exist? does
        # this command already have a row?) and neither question can be asked
        # inside the knowhow write transaction without the catalog store
        # reaching into knowhow's write path. Two concurrent applies would
        # therefore both see "no table"/"no such command" and produce a
        # duplicate table or duplicate rows — and v1's whole promise is that it
        # never damages a table. The backend is single-process (the model
        # scheduler is process-local and enforces `--workers 1`), so one
        # per-NOTEBOOK lock closes both the realistic case (a double-clicked
        # confirm) and the one a per-SOURCE lock would miss: two different
        # sources whose derived title happens to match, applying for the
        # first time concurrently. The key is `notebook_id` and nothing else —
        # see `_target_lock_key` for why every finer identity tried here (the
        # derived title, the resolved table id) turned out to be mutable, and
        # why a key the locked write can change is not a key at all.
        self._apply_locks: dict[tuple, threading.Lock] = {}
        self._apply_locks_guard = threading.Lock()
        # R10: the parse barrier. A `weakref.ref` to the SourceIngestionService
        # (or any object exposing `try_hold_source_chunk_lock`), backfilled by
        # `wire_source_ingestion()` — this service is constructed eagerly, long
        # before ingestion exists, so it cannot be a constructor argument (same
        # deferred seam, and the same weakref-not-strong-ref reason, as
        # `catalog.source_ingestion` right next to it). Left `None` on runtimes
        # that never wire ingestion, which is CORRECT rather than a hole: see
        # `_source_write_barrier`.
        self.source_locks: Callable[[], Any] | None = None

    # ------------------------------------------------------------ diagnostics
    def _emit(self, kind: str, job: Mapping[str, Any], **extra: Any) -> None:
        """Counts only — never a prompt, a section title or a model answer.

        A manual's section titles are the command names themselves, and the
        event log is a diagnostics channel, not a content store.
        """
        try:
            self.event_log.emit(
                {
                    "kind": kind,
                    "job_id": job["id"],
                    "notebook_id": job["notebook_id"],
                    "source_id": job["source_id"],
                    "status": job["status"],
                    "sections_total": int(job["sections_total"]),
                    "sections_done": int(job["sections_done"]),
                    "entries": int(job["entries"]),
                    "rejected": int(job["rejected"]),
                    "uncovered": int(job["uncovered"]),
                    **extra,
                }
            )
        except Exception:  # noqa: BLE001 - diagnostics must never break a run
            pass

    # ---------------------------------------------------------------- preview
    def _coherent_source_reads(
        self, source_id: str
    ) -> tuple[int, int, list[dict], bool]:
        """``source_text_stats`` and ``preview_elements``, proven to describe
        ONE generation of the document.

        They are two statements on two connections, and a reparse committing
        between them mixes generations in a way neither number can reveal on
        its own: the character total of the NEW document paired with a prefix
        of the OLD one, or the reverse. Nothing about the result looks wrong —
        it is simply an estimate for a document that never existed, and the
        two failure directions do not even cancel (a reparse that shrinks a
        manual reports a window count for the longer version).

        The generation token is the one ``start`` and ``_require_current_
        generation`` already use (``source_element_generation``, the elements'
        ``MAX(created_at)``, which ``replace_elements`` moves exactly when it
        swaps them). Read on both sides of the pair: unequal means a swap
        landed somewhere inside, and the whole pair is re-read. Bracketing is
        deliberately conservative — a swap that commits between the second
        read and the closing token costs one harmless retry rather than a
        wrong answer.

        Bounded at ``_PREVIEW_READ_ATTEMPTS``, and a second failure is
        reported rather than retried: two swaps inside one bounded read means
        a reparse is actively running, and the honest answer is to come back
        after it, not to keep paying for reads that keep losing the race. That
        is `CatalogSourceBusy` — the same "in flight, nothing expired, wait"
        the apply/dismiss barrier raises, worded for this endpoint
        (`SOURCE_REPARSING_MESSAGE`).
        """
        for _attempt in range(_PREVIEW_READ_ATTEMPTS):
            opened = self.catalog.source_element_generation(source_id)
            element_count, total_chars = self.catalog.source_text_stats(source_id)
            rows, clipped = self.catalog.preview_elements(
                source_id,
                limit=PREVIEW_ELEMENT_LIMIT,
                text_chars=PREVIEW_ELEMENT_CHARS,
            )
            if str(opened or "") == str(
                self.catalog.source_element_generation(source_id) or ""
            ):
                return int(element_count), int(total_chars), rows, bool(clipped)
        raise CatalogSourceBusy(source_id)

    def preview(self, notebook_id: str, source_id: str) -> CatalogPreview:
        """Estimate, with zero model calls, what extracting this source would
        cost — two reads, each bounded a different way.

        **How many windows** has two answers, because character arithmetic
        alone is not one. ``⌈total characters ÷ WINDOW_CHARS⌉`` under-counts
        twice over: elements go into a window WHOLE, so the gap left by one
        that did not fit is real budget nobody spends (three 7,000-character
        elements are three windows, not two), and a window naming more
        commands than one candidate list holds is split again. Both only ever
        ADD windows, so the arithmetic is a floor and never a count.

        So: when the bounded prefix turned out to BE the whole document, the
        window count is the number the real packer produced over it —
        **exact**, and free, because the prefix was already read and packed to
        estimate the calls. When the prefix ran out, the answer is the larger
        of that partial packing and the arithmetic floor, both of which are
        lower bounds, and it is reported as an explicit **lower bound**
        (`sampled` says so, and the UI says "at least"). Reading the whole
        document to count them exactly would be the failure a cost preview
        must not have — a preview that performs the scan it is estimating.

        **How many CALLS** cannot be arithmetic: it depends on the
        zero-model-call gate (a prose window is free) and on each window's
        parameter count (a 100-flag window is several slices). Both need the
        text, so they are measured exactly over the bounded PREFIX
        ``preview_elements`` returns — including the relay, since a window's
        gate reads the previous window's candidates — and the windows past
        that prefix are NOT priced at all. They used to be charged one call
        each, and that number was wrong in both directions at once: the skip
        gate makes a prose window free, so a mostly-narrative book was quoted
        for calls it will never make, while a parameter-dense one is several
        slices per window and was quoted far too little. Either way it was a
        figure about text this preview never read, sitting next to copy
        promising the real total could only be higher. So ``estimated_calls``
        now describes exactly what was measured — the prefix — and
        ``windows_in_prefix`` says how much of the document that was, leaving
        the caller to say "the rest depends on what is in it" instead of
        inventing a number for it.

        Both of ``preview_elements``' bounds feed ``sampled``, not just the row
        cap. Per-element truncation is the one that actually distorts the
        estimate: clipping an options table drops parameter names, which drops
        slices, so a manual whose option tables run past
        ``PREVIEW_ELEMENT_CHARS`` can easily cost several times the reported
        call count. The row bound is read as ``element_count > len(rows)`` and
        NOT as ``len(rows) >= PREVIEW_ELEMENT_LIMIT``: a document holding
        exactly the cap's worth of elements is fully read, and calling that
        sampled would downgrade an exact count to a lower bound (and word the
        UI "at least") on the one document where the estimate is perfect. The
        comparison is only meaningful because both numbers come from the same
        generation — see ``_coherent_source_reads``.

        R8: the parse-status precondition is checked HERE as well as in
        ``start``. A cost preview over a source that has not been parsed yet is
        not merely useless, it is misleading in the one direction this estimate
        must never be wrong in — it reads whatever prefix of elements happens to
        exist (usually none) and reports "约 0 个窗口", which reads as "this
        document has nothing to extract" rather than "come back in a minute".
        """
        source = self._require_parsed(notebook_id, source_id)
        element_count, total_chars, rows, clipped = self._coherent_source_reads(
            source_id
        )
        sampled = clipped or element_count > len(rows)
        prefix = extraction_windows(rows)
        # Exact when the prefix is the whole document; a true floor otherwise.
        #
        # The characters the prefix did NOT cover come from subtracting each
        # prefix row's own stripped length (`full_chars`) from the stripped
        # total — never the length of the `text` that came back, since those
        # rows are clipped at `PREVIEW_ELEMENT_CHARS` and subtracting what was
        # transmitted would leave every clipped element's tail in the
        # remainder to be counted a second time.
        #
        # `len(prefix) + ceil(remainder / WINDOW_CHARS)` would NOT be a floor,
        # which is the whole subtlety here: the packer closes a window only
        # when the next element does not fit, so the prefix's LAST window is
        # still open and the unread elements keep filling it rather than
        # starting a new one. (Four short elements with the row cap at three
        # is one window in reality and would be quoted two.) So only the
        # closed windows are counted as certain, and the open one's characters
        # go back in the pot with the remainder. Every input is conservative:
        # the prefix's own text is clipped, so its packing and its tail length
        # both understate the real document, and the join separators the
        # packer inserts are not counted at all.
        remainder = max(
            0,
            int(total_chars) - sum(int(row.get("full_chars") or 0) for row in rows),
        )
        #
        # Three independent floors, and the answer is the tightest of them.
        # The STRUCTURAL one (closed prefix windows plus the open tail and the
        # remainder) is usually the strongest, but not always: a prefix whose
        # every element was clipped to a few characters packs into one window
        # and says almost nothing, while the whole-document arithmetic still
        # proves the real count. `len(prefix)` is the third, and it is what
        # keeps the answer honest when the other two round down.
        if not sampled:
            estimated_windows = len(prefix)
        else:
            open_tail = (len(prefix[-1].text) if prefix else 0) + remainder
            estimated_windows = max(
                len(prefix),
                max(0, len(prefix) - 1) + -(-open_tail // WINDOW_CHARS),
                -(-max(0, int(total_chars)) // WINDOW_CHARS),
            )
        prefix_calls = 0
        skipped = 0
        carry: list[str] = []
        for window in prefix:
            own = window_candidates(window)
            if window_needs_model(window, carry, own=own):
                prefix_calls += len(extraction_slices(window))
            else:
                skipped += 1
            # The relay is advanced for skipped windows too — exactly as `run`
            # does. Dropping it here would make the preview's own gate answer
            # differently from the run's on the very case the relay exists for
            # (a multi-window parameter table).
            carry = carry_candidates(own, carry)
        return CatalogPreview(
            source_id=source_id,
            source_title=self._canonical_source_title(source),
            estimated_windows=estimated_windows,
            # ONLY what the prefix measured. See the docstring: a per-window
            # guess for text this preview never read is wrong in both
            # directions (prose windows are free, dense ones are several
            # slices) and it contradicts the "could only be more" wording
            # sitting next to it.
            estimated_calls=prefix_calls,
            windows_in_prefix=len(prefix),
            skipped_windows_in_prefix=skipped,
            sampled=sampled,
            element_limit=PREVIEW_ELEMENT_LIMIT,
        )

    # ------------------------------------------------------------------ jobs
    def latest_job(self, source_id: str) -> dict | None:
        return self.catalog.latest_job(source_id)

    def scoped_job(
        self, notebook_id: str, source_id: str, job_id: str
    ) -> dict | None:
        """Resolve a job that provably belongs to this notebook AND source.

        ``job_id`` is a client-supplied opaque id, so it is re-scoped here
        rather than trusted: without this, a caller who knows any job id could
        read another notebook's candidates through a notebook they do own.
        An empty ``job_id`` means "this source's latest run".
        """
        if not job_id:
            return self.catalog.latest_job(source_id)
        try:
            job = self.catalog.get_job(job_id)
        except KeyError:
            return None
        if job["notebook_id"] != notebook_id or job["source_id"] != source_id:
            return None
        return job

    def _apply_lock(self, key: tuple) -> threading.Lock:
        with self._apply_locks_guard:
            return self._apply_locks.setdefault(key, threading.Lock())

    @contextmanager
    def _source_write_barrier(self, source_id: str):
        """Hold the source's parse barrier for the whole confirm, or refuse.

        R10 (codex PR #412 review) — the hole this closes. R8 put the
        source-generation guard inside the catalog lock, which serializes
        confirms against each other but says nothing about the OTHER writer:
        ``replace_elements`` runs under ``SourceIngestionService``'s own
        per-SOURCE chunk lock, a completely different mutex. So the check and
        the write were a textbook TOCTOU — generation read as current, reparse
        commits, ``append_knowhow_rows`` then lands rows describing sections
        the document no longer has, and the job is marked applied. The target
        lock could never have caught that; the two writers were never on the
        same lock at all.

        The fix is to take the mutex the swapper actually takes.
        ``try_hold_source_chunk_lock`` is the ingestion service's own guard for
        exactly this — an external holder of that per-source lock — and it is
        the SAME lock ``process_source`` holds continuously from
        ``replace_elements`` through ``build_chunks``. Held here, no element
        swap can commit between ``_require_current_generation`` and the knowhow
        write, which is what makes the generation check mean anything.

        **Lock order: source barrier OUTSIDE, catalog lock INSIDE.** Nothing
        acquires them in the other order, so no cycle exists to deadlock on.
        The exhaustive enumeration behind that claim lives in
        ``_target_lock_key``, which owns the whole panorama now that the inner
        lock is one per-notebook mutex.

        Source-outside is also the only order that is safe under contention
        even ignoring deadlock: the barrier can be held by a reparse for
        minutes, and waiting for it while holding the catalog lock would stall
        every OTHER confirm in that notebook (including confirms of sources
        that are not being reparsed at all) behind an unrelated pipeline.

        Why ``start`` deliberately stays outside this barrier: it writes no
        document-derived content. Its worst case under a concurrent reparse is
        recording a generation that is one swap old — which the very guard
        above then reports as stale, refusing a confirm that would have been
        wrong anyway. That is a retryable refusal, not a bad row, so paying a
        possible multi-second wait on the run-start path buys nothing.

        The wait is bounded (``SOURCE_LOCK_WAIT_SECONDS``) and a timeout raises
        ``CatalogSourceBusy`` — never ``CatalogSourceChanged``. The difference
        matters: ``CatalogSourceChanged`` EXPIRES the whole candidate set, and
        expiring on "someone is holding the barrier" would be destroying a
        user's reviewable work on a guess. A parse can fail before
        ``replace_elements`` ever runs, in which case those candidates are
        still perfectly valid.

        This method's own coverage starts where ``replace_elements`` becomes
        reachable, i.e. from ``hold_source_chunk_lock`` onward. It says
        nothing about the PARSE STAGE that precedes that lock — ``apply`` and
        ``dismiss`` close that separately, via ``_require_not_parsing``, in
        the same locked window right after this context manager and the
        catalog lock. See that method's docstring for why the gap exists and
        why it needs a status check rather than another lock.

        An unwired ``source_locks`` yields straight through. That is not a
        fail-open: the barrier only defends against ``replace_elements``, whose
        only caller for an already-existing document source is
        ``process_source``, which lives on the very service that is missing. A
        runtime without ingestion cannot reparse anything, so there is nothing
        to serialize against. (This is also single-process by construction —
        the deployment is pinned to ``--workers 1`` — so an in-process mutex IS
        the authority here, the same premise `_apply_lock` already rests on.)
        """
        holder = self.source_locks() if self.source_locks is not None else None
        if holder is None:
            yield
            return
        with holder.try_hold_source_chunk_lock(
            source_id, timeout=SOURCE_LOCK_WAIT_SECONDS
        ) as acquired:
            if not acquired:
                raise CatalogSourceBusy(source_id)
            yield

    def _target_lock_key(self, notebook_id: str) -> tuple:
        """The identity every catalog writer serializes on: the NOTEBOOK, and
        nothing else.

        ``notebook_id`` is a caller-supplied routing id that is fixed for the
        entire life of every writer that could collide, needs no read to
        compute, and is the same value all of them already hold. That is the
        entire specification a lock key has to meet, and the two candidates
        this replaced each failed it in a different way.

        **Why not the derived title (R14).** ``_display_source_title`` resolves
        through ``source_display_title``, which prefers a grounded paper title
        over the upload name — and paper-metadata grounding runs
        asynchronously, so it can land BETWEEN two applies of one job. The
        title is therefore mutable state, and R2's title key inherited exactly
        the defect R2 had diagnosed in R1's table key: two writers for one
        target computing two different keys and both entering. Concretely, a
        double-clicked confirm whose backfill lands between the two clicks
        takes ``("title", nb, 命令目录：<upload name>)`` and
        ``("title", nb, 命令目录：<paper title>)`` — no mutual exclusion, both
        past the anchor-column existence check, two rows for one command, or
        two tables.

        **Why not the finer split key.** The obvious repair is per-target
        rather than per-notebook: lock ``("table", job.applied_table_id)`` when
        the job already landed rows, and a per-notebook creation lock only for
        the first-apply path, taking the table lock nested inside it once the
        target resolves (order create→table, never the reverse). That is sound
        for the two races R1/R2 were about, and it was rejected anyway,
        because a full enumeration turns up two more:

        1. ``applied_table_id`` is mutable in the one way that matters — it
           transitions ``""``→``T``. A writer that read the job row while it
           was still empty keys on the creation lock; a writer that reads it
           after keys on ``("table", T)``. Those two are concurrent. Reachable
           by a dismiss and a second apply of ONE job, which is precisely the
           pair ``dismiss``'s lock exists to separate. Closing it needs a
           re-read of the job inside the lock plus a re-lock when it moved.
        2. ``_resolve_target_table`` falls through to create-or-find when
           ``applied_table_id`` names a table that has since been DELETED. That
           fall-through creates a table while holding only ``("table", T)`` —
           outside the creation lock — so it races a genuine first-time
           applier. Closing it needs the creation lock, which the fixed
           create→table order forbids acquiring from there; the only way out is
           to drop the lock and retry the whole body.

        Both repairs are validate-and-retry loops, i.e. more machinery than
        this entire method, guarding a distinction that buys nothing real:
        every writer here is bounded (at most ``MAX_APPLY_CANDIDATES`` rows),
        makes no model or network call, and is triggered by a person clicking
        confirm on one source. What the coarse key costs is that two confirms
        for DIFFERENT sources of the same notebook serialize; on the shipped
        SQLite backend they largely serialize anyway, since
        ``append_knowhow_rows`` takes the process-wide write lock. This module
        has had four lock defects found in review (R1, R2, R10, R12); a key
        with no state in it and no ordering to get wrong is worth more than the
        concurrency it gives up.

        **Lock-order panorama.** Two mutexes exist on these paths and they are
        always taken in this order, so no cycle exists:

        1. ``_source_write_barrier`` — the ingestion service's per-SOURCE chunk
           lock (see that method for why it is outermost and why a reparse must
           not be waited on while holding anything else).
        2. this per-NOTEBOOK catalog lock.

        Exhaustively, every holder of either:

        * ``process_source`` and the checkup backfill take the source lock and
          never call into this service, so they can never hold it and wait for
          the catalog lock.
        * ``start``'s stale sweep (``_reject_if_pending_candidates``) takes the
          catalog lock and deliberately NOT the barrier, so it can never hold
          the catalog lock and wait for a barrier.
        * the extraction worker's per-window write-back (``_persist_window``)
          takes the catalog lock and NOT the barrier, for the same reason —
          and its critical section holds no model call, so a reviewer's
          confirm never queues behind a provider.
        * ``apply``/``dismiss`` are the only holders of both, and both take
          them in the order above.

        Nothing nests a second catalog lock inside the first, so the notebook
        lock does not need to be re-entrant.

        ``applied_table_id`` keeps its real job — deciding WHICH table a given
        apply writes to (``_resolve_target_table``), resolved INSIDE the held
        lock. It is a resolution input, not an identity the lock keys on, and
        that separation is what lets it be revised (rename, deletion) without
        ever splitting the mutual exclusion.
        """
        return ("catalog", notebook_id)

    def _scoped_source(self, notebook_id: str, source_id: str):
        """The source, only if it belongs to ``notebook_id``; else ``KeyError``.

        The routes already guard this, and this re-check is deliberately not
        trusting that: every public method here takes a caller-supplied
        ``source_id``, and a service that is only safe because of what its
        current caller happens to do is one refactor away from not being safe
        at all. Same reasoning as ``scoped_job`` for job ids.
        """
        source = self.sources.get_source(source_id)
        if getattr(source, "notebook_id", "") != notebook_id:
            raise KeyError(source_id)
        return source

    def _canonical_source_title(self, source: Any) -> str:
        """The name THIS source is called everywhere else in the product —
        ``""`` if it has no name at all (never a placeholder).

        ``source.title`` alone is the file it was uploaded under — for a
        grounded paper that is a different string from what the citation
        cards, the retrieval evidence and the enumeration list already show
        for the same source (``app.services.source_display.source_display_title``,
        the one frozen rule those three share). Deriving the catalog's own
        table title from the raw upload name would give a grounded paper two
        visible names in the same session: its real title everywhere else,
        its filename on the 「命令目录：<name>」 table.

        ``get_source`` already hydrates ``paper_meta`` on every call this
        service makes (``_scoped_source``), so this reads what is already in
        hand rather than issuing a second query — the row shape
        ``source_display_title`` wants is assembled from the ``SourceDetail``
        model's own fields here, not fetched again.
        """
        paper_meta = getattr(source, "paper_meta", None)
        row = {
            "is_paper": bool(getattr(paper_meta, "is_paper", False)),
            "paper_title": getattr(paper_meta, "title", None),
            "title": getattr(source, "title", None),
            "file_name": getattr(source, "file_name", None),
        }
        return source_display_title(row)

    def _display_source_title(self, source_id: str, source: Any) -> str:
        """``_canonical_source_title``, falling back to the opaque
        ``source_id`` when the source has no name at all — matching every
        call site below that previously fell back to the same thing on the
        raw upload title.

        Every caller that derives the「命令目录：<title>」table name goes
        through this one function, and that is now its ONLY job: what this
        returns can change under a running job (paper-metadata grounding
        completes asynchronously and promotes the upload name to the paper
        title), so it names things and never identifies them.

        R14 is the review that drew that line. The per-notebook catalog lock
        used to key on this value, and a backfill landing between two applies
        of one job therefore put them on two different keys — mutual exclusion
        silently gone at exactly the moment it was needed. ``_target_lock_key``
        keys on ``notebook_id`` alone now and calls nothing here.

        A later title change still does not rename or split a table: the title
        is only read again at the moment a NEW table would be created
        (``_find_table``/``_ensure_table``), and a job that already landed rows
        keeps writing to the same table through its remembered
        ``applied_table_id`` (see ``_resolve_target_table``) whatever this
        returns on a later call.

        ``preview`` deliberately does NOT go through this: it shows the
        source's canonical name in a cost estimate, not a table/lock
        identity, so it keeps ``_canonical_source_title``'s own ``""``
        fallback rather than surfacing the opaque id as if it were a name.
        """
        return self._canonical_source_title(source) or source_id

    def _require_parsed(self, notebook_id: str, source_id: str):
        """``_scoped_source`` plus the R8 parse-status precondition.

        Both public entry points that read the document (``preview`` and
        ``start``) go through this, so there is exactly one place that decides
        what "this source is ready to be read" means, and it is the SAME
        whitelist the rest of the repository already uses for that question
        (``PARSED_SOURCE_STATUSES``). A whitelist rather than a blacklist for
        the reason the checkup queries spell out: ``metadata-only`` sources have
        no elements BY DESIGN, and a blacklist would keep letting them through.
        """
        source = self._scoped_source(notebook_id, source_id)
        parse_status = str(getattr(source, "parse_status", "") or "")
        if parse_status not in PARSED_SOURCE_STATUSES:
            raise CatalogSourceNotParsed(source_id, parse_status)
        return source

    def _require_current_generation(
        self, job: Mapping[str, Any]
    ) -> None:
        """Refuse to act on candidates whose source has since been reparsed,
        and expire them in the same breath.

        Called INSIDE the catalog lock by both ``apply`` and ``dismiss``, so
        the check and the sweep are serialized against each other and against a
        concurrent confirm of the same target.

        Expiring (rather than merely refusing) is not a courtesy: the R5/R6
        guard blocks a new run while the latest job still has unreviewed
        candidates, so a job frozen by a reparse would otherwise lock the source
        out of extraction permanently — refusing every confirm AND blocking the
        re-run that would replace them. The reason code is recorded, never a
        silent state flip, so the 「已跳过」 tab can say why.

        The sweep goes through ``expire_pending_candidates`` (ONE statement over
        the whole job) rather than the id-taking ``mark_candidates_dismissed``
        that ``apply``'s conflict branch uses. The two are different shapes on
        purpose: apply dismisses an explicit page-sized SELECTION, while this
        dismisses a COMPLETE SET. Reusing the page-bounded path here would leave
        a job with more than ``MAX_APPLY_CANDIDATES`` candidates still holding
        the restart guard after its own expiry — the exact deadlock this sweep
        exists to prevent.

        A job with an empty recorded generation is one created before this
        column existed... which cannot happen (the column ships with the table),
        but the comparison handles it the only sane way: an empty snapshot
        compares equal only to an empty live token, i.e. to a source that has no
        elements at all.
        """
        live = self.catalog.source_element_generation(job["source_id"])
        if str(job.get("source_generation") or "") == str(live or ""):
            return
        self.catalog.expire_pending_candidates(
            job["id"], reject_info={"reason": SOURCE_REPARSED_REASON}
        )
        raise CatalogSourceChanged(job["id"])

    def _require_not_parsing(self, notebook_id: str, source_id: str) -> None:
        """Refuse apply/dismiss while a reparse's PARSE STAGE is under way —
        the half of a reparse's lifecycle neither the barrier nor the
        generation check can see.

        R12 (codex PR #412 review) — the hole this closes. ``process_source``
        calls ``set_source_status(source_id, "parsing")`` as literally the
        first thing it does (see its own comment on why: even that write's
        own failure must not leak the in-flight lease), long BEFORE it ever
        reaches ``hold_source_chunk_lock`` — the parse itself (MinerU, minutes
        for a real document) runs entirely outside that lock. A confirm that
        lands inside this window sails through both existing guards: the
        barrier is free (nobody holds the chunk lock yet), so
        ``_source_write_barrier`` yields immediately, and the swap has not
        happened yet, so ``_require_current_generation`` sees the live
        generation still matching the job's snapshot and lets it pass. The
        write then lands, and only afterward does the reparse take the lock
        and call ``replace_elements`` — landing rows that describe a document
        already known to be getting replaced, with no later signal that ever
        marks them stale (the generation comparison on THIS job already
        happened; there is no second check).

        Must be called INSIDE the same locked window as
        ``_require_current_generation`` — after the barrier, after the catalog
        lock — so the two checks together are atomic with respect to a
        concurrent reparse. Reads the source FRESH here rather than trusting
        the copy ``apply``/``dismiss`` fetched earlier for its scope check (and,
        in ``apply``, for the table title): that earlier read happened before
        either lock was taken, so by the time this runs it may already be
        behind.

        The whitelist is ``PARSED_SOURCE_STATUSES`` — the SAME one
        ``_require_parsed`` uses to decide a source is readable at all
        (``parsed``/``extracting``/``extracted``) — PLUS ``failed``, which
        deliberately does NOT block a confirm even though it blocks a new
        extraction (``_require_parsed``): a parse that failed BEFORE
        ``replace_elements`` never touched the elements, so the candidates
        are still grounded in the live document and the generation snapshot
        still matches; a parse that failed AFTER the swap moved the
        generation, and ``_require_current_generation`` (same locked window)
        catches that case on its own authority. Blocking ``failed`` here
        would both over-restrict and lie — the busy copy says a parse is in
        progress when none is. Only in-flight transitions (``parsing`` and
        any unknown state) are refused, because their outcome this confirm
        cannot yet know.

        Raises ``CatalogSourceBusy``, never ``CatalogSourceChanged`` — R10's
        principle applies here unchanged: the parse may still fail before it
        ever reaches ``replace_elements``, in which case the elements (and
        every candidate) were never touched, so nothing is expired. The user
        is told to wait, with the SAME copy the barrier-contention case uses,
        because from the outside the two situations are indistinguishable and
        the remedy is identical either way.

        R17 (codex PR #412 review, rebutted) — this is DELIBERATELY a
        point-in-time read, not a value re-checked against the write that
        follows it. The claim was that a reparse flipping the source to
        ``parsing`` right AFTER this check returns — but before
        ``_apply_locked`` lands its rows — leaves "permanently stale" knowhow
        rows, and that the fix is to make the status flip and this check
        synchronous. Rebutted on two independent grounds: first, ``apply``
        already holds ``_source_write_barrier(source_id)`` for the ENTIRE
        write, and ``replace_elements`` cannot run without that same barrier
        (see ``_reparse_the_way_the_pipeline_does`` in the tests) — so no
        element the write reads can have changed inside this call no matter
        when the status column itself flips; the row lands describing the
        SAME generation this check just confirmed. Second, a reparse whose
        parse stage begins only after this check returns is, from the source's
        own perspective, indistinguishable from a user clicking 「重新解析」 the
        instant AFTER this apply call finishes — a plain serial ordering, not
        a race this call is any part of. That ordering already has an owner:
        the NEXT touch of this job runs ``_require_current_generation`` first
        and finds the generation this apply's write landed against no longer
        current, expiring every surviving ``candidate`` row as
        ``source_reparsed`` (see that guard, and
        ``test_apply_tolerates_a_reparse_flipping_to_parsing_after_the_status_check``
        below for both halves pinned together). Making this check synchronous
        with the write would not shrink the set of reachable terminal states —
        the reparse still eventually completes and the generation guard still
        eventually catches it — it would only require ``SourceIngestionService``
        to reach back for the catalog's own lock, the one cycle
        ``_target_lock_key``'s docstring refuses to create, in exchange for
        zero additional correctness.
        """
        source = self._scoped_source(notebook_id, source_id)
        parse_status = str(getattr(source, "parse_status", "") or "")
        if parse_status not in PARSED_SOURCE_STATUSES and parse_status != "failed":
            raise CatalogSourceBusy(source_id)

    def _reject_if_pending_candidates(
        self, source_id: str, notebook_id: str, live: str
    ) -> None:
        """Block a new run while the source's latest job reached ANY terminal
        status and still has unreviewed candidates (see
        ``CatalogPendingCandidates``) — UNLESS those candidates are already
        stale, in which case they are expired here and the run is let through.

        R6 P1: widened from ``succeeded``-only. The narrower rule shipped in
        R5 on the theory that `failed`/`cancelled` are a different situation
        because their own terminal copy (`INTERRUPTED_MESSAGE`,
        `CANCELLED_MESSAGE`) "invites the user to restart" — but *retained*
        candidates were never the same thing as *reachable* ones: `.../job`
        only ever returns a source's MOST RECENT job, so a restart from a
        failed/cancelled run orphans its candidates exactly the way a restart
        from a succeeded run does. Retained-but-orphaned is not a real
        recovery route, so this guard now covers every status in
        ``CATALOG_TERMINAL_STATUSES`` (`succeeded`, `failed`, `cancelled`)
        alike, and `INTERRUPTED_MESSAGE`/`CANCELLED_MESSAGE` were reworded to
        stop promising an unconditional restart.

        An active latest job (queued or running) is left to `create_job`'s own
        partial-unique-index guard, which raises the more specific
        `CatalogJobAlreadyRunning` — checking pending candidates first would
        mis-diagnose that case with the wrong message.

        R8: the stale escape hatch. Once the source has been reparsed, the old
        job's candidates describe a document that no longer exists — they can
        never be confirmed (``_require_current_generation`` refuses), so leaving
        them holding this guard would lock the source out of extraction forever.
        The reparse is precisely the reason a user wants to re-run, so the run
        is allowed and the dead candidates are expired with a recorded reason.
        The sweep takes the SAME catalog lock ``apply``/``dismiss`` use, so it
        cannot interleave with an in-flight confirm of the same target. It
        needs no title of its own to do that (R14): the key is the notebook,
        which this method is handed directly, so there is no derived value here
        that could drift out of step with what a confirm computes.
        """
        latest = self.catalog.latest_job(source_id)
        if latest is None or latest.get("status") not in CATALOG_TERMINAL_STATUSES:
            return
        pending = self.catalog.candidate_counts(latest["id"]).get("candidate", 0)
        if pending <= 0:
            return
        if str(latest.get("source_generation") or "") != str(live or ""):
            with self._apply_lock(self._target_lock_key(notebook_id)):
                self.catalog.expire_pending_candidates(
                    latest["id"], reject_info={"reason": SOURCE_REPARSED_REASON}
                )
            return
        raise CatalogPendingCandidates(source_id, pending)

    def start(self, notebook_id: str, source_id: str) -> dict:
        """Claim the source's single-flight guard and publish a queued job.

        The row is inserted BEFORE the worker thread starts, and the guard
        covers queued as well as running, so a duplicate request landing in
        that window is rejected instead of scheduling a second writer for the
        same candidate set.

        R8: the live element generation is read ONCE here and used twice — to
        decide whether the previous job's unreviewed candidates are already
        stale, and as the snapshot recorded on the new row. One read, not two,
        so those two decisions can never disagree about what "now" is.

        The snapshot is taken at CREATE time rather than at the worker's own
        whole-source read. The window between them is one thread hand-off, and
        a reparse landing inside it makes the recorded generation older than
        what the worker read — which this feature's guard then reports as
        stale. That is a false positive in a vanishingly rare race, and it
        fails in the safe direction (refuse the confirm, offer a re-run);
        recording it later would need a second write per run to buy that back.
        """
        self._require_parsed(notebook_id, source_id)
        if not self.models.configured(CATALOG_WORKLOAD):
            raise CatalogModelUnavailable(MODEL_UNAVAILABLE_MESSAGE)
        generation = self.catalog.source_element_generation(source_id)
        self._reject_if_pending_candidates(source_id, notebook_id, generation)
        job = self.catalog.create_job(
            notebook_id,
            source_id,
            self.current_user_id(),
            source_generation=generation,
        )
        # Between create_job's commit and the worker's own try/finally there is
        # a window (register the cancel event, emit the event) where a signal
        # would escape with the row left in `queued`. Settle it here for the
        # same reason the KG build path settles its own unentered window.
        try:
            with self._cancels_lock:
                self._cancels[job["id"]] = threading.Event()
            self._emit("catalog_job_started", job)
        except (KeyboardInterrupt, SystemExit):
            self._settle(
                job["id"], "failed", INTERRUPTED_MESSAGE, "worker_interrupted"
            )
            raise
        return job

    def fail_submission(self, job_id: str) -> bool:
        """Settle a job whose worker thread could not be started at all."""
        settled = self._settle(
            job_id, "failed", SUBMISSION_FAILED_MESSAGE, "job_submission_failed"
        )
        self._discard_cancel(job_id)
        return settled

    def cancel(self, source_id: str) -> dict:
        """Ask the active run to stop at its next slice boundary.

        If no worker is registered for that row, this process is not the one
        running it (a restart lost the thread but not the row), so settle it
        directly — otherwise the guard would hold the source hostage until the
        next boot.
        """
        job = self.catalog.active_job(source_id)
        if job is None:
            return {"status": "not_running", "job": self.catalog.latest_job(source_id)}
        with self._cancels_lock:
            event = self._cancels.get(job["id"])
        if event is not None:
            event.set()
        else:
            self._settle(
                job["id"], "cancelled", CANCELLED_MESSAGE, "cancelled_no_worker"
            )
        # Re-read and derive the status from the ROW, never from which branch
        # ran. The worker can settle between `active_job` above and here, and
        # answering "cancelling" next to `status: succeeded` is a contradiction
        # a caller cannot resolve. The row is the only authority.
        current = self.catalog.get_job(job["id"])
        settled = current["status"] not in {"queued", "running"}
        return {
            "status": current["status"] if settled else "cancelling",
            "job": current,
        }

    def _settle(
        self, job_id: str, status: str, failure_reason: str, diagnostic: str
    ) -> bool:
        """Write a terminal status — or, when the row is not there anymore,
        do nothing and say so quietly.

        `finish_job`'s `UPDATE ... WHERE id=? AND status IN ('queued',
        'running')` already returns `False` without raising when nothing
        matched, and that covers two DIFFERENT situations: an ordinary
        idempotent double-settle (the row exists, already terminal — see
        `finish_job`'s own docstring, and see NOTHING further below, this is
        the expected, unremarkable case), or the row is not there at all
        anymore — a cascaded source/notebook delete removed it mid-run (`ON
        DELETE CASCADE`). Telling the two apart costs one more bounded point
        lookup, paid ONLY on this already-uncommon "nothing matched" branch,
        and it exists purely for the log line: "terminal status is not the
        job's row anymore" is a race worth a line, "there is no job's row
        anymore" is not a failure at all. The "构建任务必须落终态" redline is
        a claim about rows that still EXIST; a row a foreign key already
        erased has no terminal state left to reach, and returning quietly —
        not raising, not retrying, not resurrecting the row — is the only
        contract that still makes sense for it.

        This never raises for a missing row, on either branch: the worker
        thread's `finally` (which releases the in-process cancel-event entry)
        has to run regardless of which of these it hits.
        """
        try:
            settled = self.catalog.finish_job(
                job_id,
                status,
                failure_reason=failure_reason,
                diagnostic=diagnostic,
            )
        except Exception:  # noqa: BLE001
            # A settle that fails must never replace the original interrupt or
            # exception with an unrelated traceback. Log and let the caller
            # re-raise; the row then falls back to the startup sweep, the same
            # way a SIGKILL leftover does.
            try:
                self.event_log.logger.exception(
                    "failed to settle catalog job %s", job_id
                )
            except Exception:  # noqa: BLE001
                pass
            return False
        if settled:
            return True
        try:
            self.catalog.get_job(job_id)
        except KeyError:
            # Confirmed gone, not merely already-terminal. A log line, not an
            # exception: see the docstring above for why this is the correct
            # outcome rather than a degraded one.
            try:
                self.event_log.logger.info(
                    "catalog job %s: row gone before settle could land "
                    "(status=%s) — source or notebook deleted mid-run",
                    job_id,
                    status,
                )
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass
        return False

    def _discard_cancel(self, job_id: str) -> None:
        with self._cancels_lock:
            self._cancels.pop(job_id, None)

    def _raise_if_stopped(self, job_id: str, cancel: CancelEvent) -> None:
        """Cancel-or-gone, checked together, at every point the run already
        checks `cancel` for an owner-initiated stop — including every model
        call `_call` makes, halving and coverage retries included, since
        every one of those funnels through it.

        The liveness probe (`self.catalog.get_job`) is one bounded point
        lookup on `catalog_jobs`' primary key — microseconds, next to a model
        call's whole seconds — paid only where a call, or the work leading up
        to one, is about to happen, never on a tighter loop. It exists
        because a source or notebook delete mid-run cascades this job's row
        (and every candidate row under it) away (`ON DELETE CASCADE`)
        without anybody calling `cancel()`: nothing sets the in-process
        cancel event, so `raise_if_catalog_cancelled` alone would let the
        worker keep paying for calls whose output has nowhere left to land,
        all the way until the next place it happens to read the row back.

        `raise_if_catalog_cancelled` runs FIRST so an explicit owner cancel
        keeps taking priority and keeps producing exactly the outcomes the
        existing cancel tests pin (`cancelled_by_owner`); this probe only
        ever fires for a deletion that outright removed the row, which a
        real cancel never does. Raises `CatalogJobGone`, not
        `CatalogCancelled` — see that type's docstring for why the two must
        not share a handler.
        """
        raise_if_catalog_cancelled(cancel)
        try:
            self.catalog.get_job(job_id)
        except KeyError:
            raise CatalogJobGone() from None

    def _record_window(
        self,
        job_id: str,
        cancel: CancelEvent,
        *,
        entries: int,
        rejected: int,
        uncovered: int,
    ) -> None:
        """Tick one window off the progress bar — and read the answer.

        ``record_section``'s ``UPDATE ... WHERE id=? AND status='running'``
        returns whether it matched, and both call sites used to throw that
        away. On the SKIPPED-window path that silence is the whole bug: a
        cascaded source/notebook delete removes the job row mid-run without
        anyone calling ``cancel()``, and the skip path deliberately carries no
        liveness probe (it spends nothing, and a mostly-prose PDF has
        thousands of these windows). So the worker walked every remaining
        window writing to a row that no longer existed and only discovered it
        at the closing ``get_job`` — as an uncaught ``KeyError``, instead of
        the ``job_deleted`` outcome this module already has a settled,
        documented handler for. The called path has the probe as a backstop,
        but the same one-line check there costs nothing and closes the gap
        between the probe and this write.

        Zero new queries: this reads a value the write already returned.

        **A cancel outranks a deletion.** An owner's explicit stop is the more
        specific fact and it is what the existing cancel tests pin
        (``cancelled_by_owner``), so it is checked first and both can be true
        at once — see ``_emit_finished`` for the one thing that has to hold for
        that combination to end cleanly.

        A non-matching write means the row is gone rather than merely
        non-``running``: ``run`` claims it to ``running`` before the loop, and
        the only writer that could settle it underneath a live worker is
        ``cancel``'s ``cancelled_no_worker`` branch — which by construction
        runs only when no cancel event is registered for the job, i.e. never
        while this worker holds one. That is also why no probe is added to
        tell the two apart: ``CatalogJobGone``'s handler is a no-op settle
        plus a return value, which is the right outcome either way.
        """
        if self.catalog.record_section(
            job_id, entries=entries, rejected=rejected, uncovered=uncovered
        ):
            return
        raise_if_catalog_cancelled(cancel)
        raise CatalogJobGone()

    def _emit_finished(self, job_id: str, **extra: Any) -> None:
        """``catalog_job_finished`` for a row that may not exist anymore.

        Every terminal path used to build this event by reading the row back
        inline (``self._emit(..., self.catalog.get_job(job_id), ...)``), and
        that read is evaluated by the CALLER — so a row a cascade deleted
        mid-run raises ``KeyError`` from inside an ``except`` clause, replacing
        a settled, reportable outcome with an uncaught traceback. Not
        hypothetical: an owner cancel and a source delete can both be true at
        once, and the cancel branch wins by design, which lands it on exactly
        that read.

        A missing row is not an error here. It is the same conclusion
        ``CatalogJobGone``'s own branch already reached and documented: an
        event nobody can read the job back for is not "user visible", it is a
        dangling pointer. So it is skipped, and the caller's return value —
        which never depended on the event — stands.
        """
        try:
            job = self.catalog.get_job(job_id)
        except KeyError:
            return
        self._emit("catalog_job_finished", job, **extra)

    # ------------------------------------------------------------------- run
    def run(self, job_id: str) -> dict:
        """Execute one extraction job to a terminal state.

        Every exit path settles the job row. ``KeyboardInterrupt``/``SystemExit``
        get their own clause because they inherit ``BaseException`` and would
        otherwise sail past ``except Exception`` with the row left running —
        and the guard released only at the next backend restart.
        """
        job = self.catalog.get_job(job_id)
        with self._cancels_lock:
            cancel = self._cancels.setdefault(job_id, threading.Event())
        started = time.perf_counter()

        def latency_ms() -> int:
            return round((time.perf_counter() - started) * 1000)

        try:
            result = self._run_windows(job, cancel)
            # Command names this document held that no prompt could carry (see
            # `_run_windows`). Reported only when non-zero: it is 0 on every
            # ordinary document, so its mere presence on the event is the
            # signal, and the ordinary event shape stays exactly as it was.
            overflow = (
                {"candidates_overflowed": result["candidates_overflowed"]}
                if result["candidates_overflowed"]
                else {}
            )
            if _nothing_extracted(result):
                # Paid for calls, produced no row of any kind. Settled
                # `failed` rather than `succeeded` for the same reason the
                # breaker exists: an empty review panel under a green status
                # asserts "this document documents no commands", and this run
                # has no evidence for that. See `NOTHING_EXTRACTED_MESSAGE`.
                self._settle(
                    job_id, "failed", NOTHING_EXTRACTED_MESSAGE, "nothing_extracted"
                )
                self._emit_finished(
                    job_id,
                    latency_ms=latency_ms(),
                    model_calls=result["calls"],
                    **overflow,
                )
                return {**result, "job_id": job_id, "nothing_extracted": True}
            self.catalog.finish_job(job_id, "succeeded")
            self._emit_finished(
                job_id,
                latency_ms=latency_ms(),
                model_calls=result["calls"],
                **overflow,
            )
            return {**result, "job_id": job_id}
        except CatalogCancelled:
            self._settle(job_id, "cancelled", CANCELLED_MESSAGE, "cancelled_by_owner")
            # A cancel and a cascaded delete can both be true; the cancel wins
            # (see `_record_window`), so this branch has to survive the row
            # being gone — that is `_emit_finished`'s whole job.
            self._emit_finished(job_id, latency_ms=latency_ms())
            return {"job_id": job_id, "cancelled": True}
        except CatalogJobGone:
            # See `CatalogJobGone`'s own docstring for why this cannot share
            # `except CatalogCancelled`'s handler: the row is not merely
            # terminal, it does not exist, so building the `_emit` payload
            # above (`self.catalog.get_job(job_id)`) would raise this SAME
            # `KeyError` a second time. `_settle` below is a documented
            # no-op for exactly this case (nothing matches its `UPDATE`), and
            # is called anyway so every exit path still funnels through one
            # place — but there is no row left to build an event about, so
            # this branch, unlike every other one here, does not call
            # `_emit` at all: an event nobody can read the job back for is
            # not "user visible", it is a dangling pointer.
            self._settle(job_id, "cancelled", CANCELLED_MESSAGE, "job_deleted_mid_run")
            return {"job_id": job_id, "job_deleted": True}
        except CatalogCircuitOpen as exc:
            self._settle(job_id, "failed", CIRCUIT_OPEN_MESSAGE, exc.diagnostic)
            self._emit_finished(job_id, latency_ms=latency_ms())
            return {"job_id": job_id, "circuit_open": True}
        except (KeyboardInterrupt, SystemExit):
            # See this module's docstring: these never reach `except Exception`,
            # and a row left queued/running holds this source's single-flight
            # guard until the next backend restart.
            #
            # Deliberately NOT wrapped in the KG build path's
            # `_absorbing_repeated_termination` retry loop. That machinery exists
            # because KG builds are the long-running body of an offline CLI,
            # where Ctrl-C is the ordinary way to stop and a second signal
            # landing inside the settle is a real occurrence. This job only ever
            # runs in a server-owned daemon thread, and `KeyboardInterrupt` is
            # delivered to the main thread — so the residual window here is a
            # `SystemExit` raised inside `finish_job`'s own write, which the
            # startup sweep already covers. Add the loop if a CLI entry point is
            # ever built on top of this service.
            self._settle(
                job_id, "failed", INTERRUPTED_MESSAGE, "worker_interrupted"
            )
            raise
        except Exception:
            self._settle(
                job_id, "failed", INTERNAL_FAILURE_MESSAGE, "internal_error"
            )
            raise
        finally:
            self._discard_cancel(job_id)

    def _run_windows(self, job: Mapping[str, Any], cancel: CancelEvent) -> dict:
        if not self.catalog.start_job(job["id"], 0):
            # Claim the row BEFORE paying for the read. A job cancelled while it
            # sat in the queue should cost nothing, and the read below is the
            # single most expensive non-model step in the run.
            #
            # `start_job`'s `UPDATE ... WHERE id=? AND status='queued'` fails
            # to match for the SAME two reasons `_settle`'s own `finish_job`
            # can: the row is already past `queued` (the ordinary cancel
            # race this branch exists for), or the row is not there at all —
            # deleted between `start()` publishing it and this thread
            # reaching here. Telling them apart here, rather than always
            # raising `CatalogCancelled`, matters because `run()`'s `except
            # CatalogCancelled` clause reads the row back for its event; for
            # a row that is truly gone that read raises the SAME `KeyError` a
            # second time, uncaught. See `CatalogJobGone`'s docstring.
            try:
                self.catalog.get_job(job["id"])
            except KeyError:
                raise CatalogJobGone() from None
            raise CatalogCancelled()
        # `start_job` claiming the row is not itself a cancellation check: a
        # cancel that lands in the instant between `cancel()` setting the
        # event and this thread reaching here is otherwise invisible until
        # the FIRST per-slice check inside `_process_window` — by which
        # point the whole-source read right below has already run for
        # nothing, for a job that was cancelled before it did any real work.
        # Combined with the liveness probe for the same reason: see
        # `_raise_if_stopped`.
        self._raise_if_stopped(job["id"], cancel)
        # One whole source's elements, deliberately: this is the exact fetch
        # chunking already performs for every source that is ingested, and C1a
        # is built to consume its rows unchanged. Packing needs document order
        # across the whole file, so a keyset page would have to be reassembled
        # into the same list anyway. The per-run memory ceiling is one source's
        # element text — the same ceiling `build_chunks_for_source` already
        # accepts — and each window is bounded at `WINDOW_CHARS` before any
        # model sees it.
        elements = self.chunks.source_elements_for_chunking(job["source_id"])
        windows = extraction_windows(elements)
        del elements  # windows own their own copies; drop the full-source list
        # The window total is only knowable after packing, so it lands in a
        # second write. `set_section_total` is `running`-scoped, so a cancel
        # that arrived during the read simply leaves it at 0 and the loop's
        # first cancellation check ends the run.
        #
        # SKIPPED windows are counted in the total, and each one still calls
        # `record_section` below. The progress bar's denominator is "windows in
        # this document", so leaving them out would leave a run at 12/40 when
        # it is finished, and counting them without ticking them off is the
        # same lie from the other side.
        self.catalog.set_section_total(job["id"], len(windows))
        client = self._client()
        outcomes: list[WindowOutcome] = []
        # Commands already written by an earlier window of THIS run, so a
        # continuation window revises that row instead of appending a second
        # one for the same name. Bounded by the document's command count.
        flushed: dict[str, _FlushedCandidate] = {}
        carry: list[str] = []
        position = 0
        total_calls = 0
        total_slices = 0
        total_slice_failures = 0
        windows_skipped = 0
        candidate_rows = 0
        rejected_rows = 0
        # Candidate names `extraction_windows` could not fit even after
        # splitting — normally 0 (see `WINDOW_SPLIT_FLOOR_CHARS`). Accumulated
        # over EVERY window, not just the ones that made a call, so the number
        # never depends on the skip gate: this counts what was never offered to
        # a model, which is the one loss no rejection, ratio or report line
        # downstream can show.
        candidates_overflowed = sum(
            window.candidates_overflowed for window in windows
        )
        for window in windows:
            # The cheap check first, unconditionally: an owner cancel must be
            # honoured on a skipped window too, and the in-process event costs
            # nothing to read. The DB liveness probe is deliberately NOT here
            # — see the skip branch.
            raise_if_catalog_cancelled(cancel)
            own = window_candidates(window)
            if not window_needs_model(window, carry, own=own):
                # The zero-model-call gate. No claimable name, or nothing that
                # looks like something to extract: every entry a model could
                # return would be vetoed by name, so the call's grounded output
                # is provably empty. The window is still ticked off the
                # progress bar (a denominator that counts it and a numerator
                # that does not is a bar that never finishes), and the relay is
                # still advanced.
                #
                # That last one is an INVARIANT, not a behaviour: `carry` is
                # defined as a pure function of the previous window, and this
                # branch keeps it that way. Under the current gate it is also
                # load-bearing rather than merely tidy — a prose window between
                # a command and its continuation IS skipped now, and dropping
                # this line would strand every table on the far side of one.
                #
                # No liveness probe and no `catalog_section_done` event on this
                # path, deliberately. Both exist to protect model SPEND (stop
                # paying for a job whose row a cascade deleted) or to report
                # work that happened; a skipped window spends nothing and does
                # nothing, while a mostly-prose PDF has thousands of them — the
                # probe and the event would triple this run's database traffic
                # to observe a no-op. `record_section` stays: the progress bar
                # the frontend polls reads the job ROW, not the event stream.
                windows_skipped += 1
                self._record_window(
                    job["id"], cancel, entries=0, rejected=0, uncovered=0
                )
                carry = carry_candidates(own, carry)
                continue
            self._raise_if_stopped(job["id"], cancel)
            work = self._process_window(
                client, job, window, own, carry, position, cancel, flushed
            )
            self._persist_window(job, window, work, position, flushed)
            position += len(work.rows)
            total_calls += work.calls
            total_slices += work.slices
            total_slice_failures += work.slice_failures
            outcome = window_outcome(
                window,
                work.candidates,
                work.results,
                uncovered_args=work.uncovered_args,
            )
            outcomes.append(outcome)
            # `entries` counts what this window ADDED to the review queue, not
            # every accepted entry: a continuation window that merged its
            # parameters into an earlier window's row created no new candidate,
            # and counting it would make the job's `entries` column disagree
            # with the number of rows a reviewer is shown.
            accepted = len(work.new_entries)
            rejected = outcome.command_rejects + work.slice_failures
            candidate_rows += accepted
            rejected_rows += rejected
            # v2 has no truncation concept — `extraction_windows` splits an
            # oversized element across windows rather than dropping it. The
            # column stays (dropping it would be a migration) and stays 0.
            self._record_window(
                job["id"],
                cancel,
                entries=accepted,
                rejected=rejected,
                uncovered=len(outcome.uncovered_candidates),
            )
            self._emit(
                "catalog_section_done",
                self.catalog.get_job(job["id"]),
                model_calls=work.calls,
            )
            self._check_circuit(
                outcomes,
                slices=total_slices,
                slice_failures=total_slice_failures,
            )
            carry = carry_candidates(own, carry)
        stats = catalog_stats(outcomes)
        return {
            # `windows` is windows that actually made a call — the same sample
            # the breaker's ratios are computed over. `windows_skipped` and
            # `windows_total` are reported next to it so the pair cannot be
            # mistaken for each other.
            "windows": stats.windows,
            "windows_skipped": windows_skipped,
            "windows_total": len(windows),
            # Names this document offered that no prompt could carry. See the
            # accumulator above; `run` also puts it on the finished event when
            # it is non-zero, which is the only place an operator can see it.
            "candidates_overflowed": candidates_overflowed,
            "entries_seen": stats.entries_seen,
            # Rows this run put in front of a reviewer, of either kind. They
            # are what `_nothing_extracted` reads: a run with either is a run
            # with something to look at, whatever the ratios say.
            "candidate_rows": candidate_rows,
            "rejected_rows": rejected_rows,
            "command_rejects": stats.command_rejects,
            "args_seen": stats.args_seen,
            "args_kept": stats.args_kept,
            # Reported next to `args_seen`/`args_kept` because it is the third
            # number the other two are meaningless without: a run can keep
            # everything it returned and still have answered a fifth of what it
            # was asked for.
            "args_uncovered": stats.args_uncovered,
            "calls": total_calls,
        }

    def _persist_window(
        self,
        job: Mapping[str, Any],
        window: ExtractionWindow,
        work: _WindowWork,
        position: int,
        flushed: dict[str, _FlushedCandidate],
    ) -> None:
        """Everything one window WRITES, under the notebook's catalog lock.

        The three steps are one critical section because a reviewer's confirm
        interleaving with them corrupts a row silently. ``apply`` is a
        read-then-write across two stores — it reads a candidate's payload,
        renders it into knowhow cells, appends those rows, and only then marks
        the candidate ``applied`` — and this run thread's cross-window merge
        rewrites exactly that payload. Landing between apply's read and its
        write leaves the knowhow table holding the pre-merge parameters while
        the candidate row that is marked ``applied`` holds the merged ones:
        two disagreeing records of "what was confirmed", neither of them
        wrong-looking on its own. ``dismiss`` breaks the other way — it can
        take the row out of ``candidate`` state AFTER this update already
        succeeded, so the continuation window's parameters are swallowed with
        no degraded row appended and nothing in the queue to show for them.
        Both are closed by taking the mutex those two already hold.

        Scope is deliberate at both ends. The window's MODEL CALLS happen in
        ``_process_window``, before this is entered, so a reviewer never waits
        on a model. And the False-degrade append plus the ``flushed`` registry
        update are INSIDE, not merely adjacent: the degrade decision is a
        direct consequence of what the update saw, and a registry pointing at
        a row a concurrent apply has since taken away would send the next
        window's merge at a row that is no longer reviewable.

        **Lock order.** This thread takes the per-notebook catalog lock and
        NOTHING else — never the source write barrier, which is the one order
        that could close a cycle (see ``_target_lock_key`` for the exhaustive
        panorama; ``apply``/``dismiss`` remain the only holders of both, always
        barrier-outside). Nothing here re-enters the catalog lock either, so it
        stays non-reentrant.

        An in-process mutex is the authority because the deployment is pinned
        to ``--workers 1`` — the same premise ``_apply_lock`` and the whole
        apply path already rest on.
        """
        with self._apply_lock(self._target_lock_key(job["notebook_id"])):
            # Before the insert, so a revision that could not land becomes one
            # more row in the very batch this window is about to write.
            self._settle_updates(job, window, work, position)
            if work.rows:
                self.catalog.add_candidates(work.rows)
                self._register_flushed(job["id"], flushed, work)

    def _settle_updates(
        self,
        job: Mapping[str, Any],
        window: ExtractionWindow,
        work: _WindowWork,
        position: int,
    ) -> None:
        """Revise the rows earlier windows wrote — and append a fresh row for
        any revision the store refused.

        ``update_candidate_payload`` writes only rows still in ``candidate``
        state, and returns ``False`` when it matched none. That is not an
        error: between the window that wrote the row and this one, a reviewer
        can have confirmed it into the knowhow table or dismissed it, and both
        are terminal. Overwriting either would rewrite something a person
        already acted on.

        So the merge degrades to what v1 always did — a second row for the same
        command, visible in the review queue — rather than dropping this
        window's parameters on the floor. A duplicate a reviewer can see and
        skip is a far better failure than a silent loss: the parameters this
        window found exist nowhere else, and the row that would have held them
        is out of reach.

        The new row is registered like any other first-time write (it goes into
        ``new_entries``), so the run's registry now points at the row that is
        actually live and a THIRD window merges into that one instead of
        retrying the same refused update.

        Positions continue past the rows this window already built, which keeps
        ``position`` monotonic for the whole run and lets ``_register_flushed``
        read every row of this batch back with one keyset scan.
        """
        if not work.updates:
            return
        next_position = position + len(work.rows)
        for update in work.updates:
            if self.catalog.update_candidate_payload(
                update.candidate_id, update.payload, update.reject_info
            ):
                continue
            next_position += 1
            work.rows.append(
                self._row(
                    job,
                    position=next_position,
                    window=window,
                    command_name=update.command_name,
                    payload=update.payload,
                    state="candidate",
                    reject_info=update.reject_info,
                )
            )
            work.new_entries.append(
                (next_position, update.command_name, update.entry)
            )

    def _register_flushed(
        self,
        job_id: str,
        flushed: dict[str, _FlushedCandidate],
        work: _WindowWork,
    ) -> None:
        """Learn the store-assigned ids of the candidate rows just inserted, so
        a LATER window can revise them instead of appending a second row for
        the same command.

        `add_candidates` assigns ids inside the store (they are minted there,
        like every other surrogate id in this codebase) and returns nothing, so
        the ids come back through one bounded keyset read on the index the
        review page already uses — `position > (the lowest position this window
        just wrote) - 1`, limited to the number of rows expected. `position` is
        the join key rather than `command_name` because it is the column that
        index orders by and the one this module assigned itself; a name join
        would have to assume the store wrote back exactly what it was handed.

        Rows that do not come back are simply not registered: the merge is an
        optimisation over "one row per command", and the fallback — a later
        window appending its own row for that command — is the v1 behaviour,
        visible in the review queue rather than lost. Never a raise: a run must
        not fail because a bookkeeping read came up short.
        """
        if not work.new_entries:
            return
        wanted = {
            int(position): (name, entry)
            for position, name, entry in work.new_entries
        }
        cursor = min(wanted) - 1
        while wanted:
            rows = self.catalog.list_candidates(
                job_id,
                state="candidate",
                cursor=cursor,
                limit=min(len(wanted), CATALOG_MAX_CANDIDATE_PAGE),
            )
            if not rows:
                return
            for row in rows:
                cursor = int(row["position"])
                hit = wanted.pop(cursor, None)
                if hit is None:
                    continue
                name, entry = hit
                flushed[name] = _FlushedCandidate(id=str(row["id"]), entry=entry)

    def _check_circuit(
        self,
        outcomes: Sequence[WindowOutcome],
        *,
        slices: int,
        slice_failures: int,
    ) -> None:
        """Three independent axes, all gated on a sample worth trusting.

        The first two read what the model DID return — plus, on the args axis,
        what it was ASKED for and did not (``args_uncovered``): an entry can
        pick the right command name and still invent every parameter, and a run
        that mostly rejects args can still be picking legitimate names, so
        neither alone catches the other's failure. The guard is "was anything
        asked for at all", i.e. ``args_seen + args_uncovered > 0``, and it is a
        real guard rather than defensive noise — ``catalog_stats`` reports
        ``args_keep_ratio`` as 0.0 when the denominator is empty, and without
        it a manual of genuinely flagless commands would trip the breaker on
        its tenth clean window. (Flagless no longer implies an empty
        denominator: since `_prompt`'s no-flag branch asks for positional
        arguments, such a window contributes ``args_seen`` whenever the model
        answers one, and the axis then judges those names by grounding like any
        other. The guard still matters for the windows that really do take no
        arguments at all.) Reading ``args_seen`` alone would NOT do: a
        model that answers nothing at all for 20 assigned parameters leaves
        ``args_seen`` at 0 and would sail past this axis while its assignment
        vanished window after window.

        The third axis reads slices that produced NO parseable answer at all,
        and it exists because the other two cannot name that failure even now
        that the args axis counts unanswered assignments: a slice that never
        returns anything contributes no entry, so `entries_seen` stays 0 and
        the name ratio stays innocuous, while the args ratio it does drag down
        reports the SYMPTOM ("parameters went missing") instead of the CAUSE
        ("the endpoint is returning garbage"). Without it, a deployment pointed
        at an incompatible model endpoint runs the entire manual — every
        window, every halving retry — and reports `succeeded` with an empty
        catalog. That is precisely the silent-empty outcome this breaker exists
        to prevent, so it must be a breaker axis rather than a per-window note.

        Which one is REPORTED when several fire is a diagnosis question, not a
        severity one, and the order below is most-specific-first: an unusable
        response explains a bad args ratio, never the other way round. (Before
        the args axis counted unanswered assignments the two could not overlap
        at all — a slice that answers nothing produced no `args_seen` either —
        so the old order never had to choose.)
        """
        stats = catalog_stats(outcomes)
        if stats.windows < MIN_WINDOWS_BEFORE_ALERT:
            return
        name_axis = stats.command_reject_ratio > COMMAND_REJECT_ALERT_RATIO
        args_asked = stats.args_seen + stats.args_uncovered
        args_axis = args_asked > 0 and stats.args_keep_ratio < ARGS_KEEP_ALERT_RATIO
        unusable_ratio = round(slice_failures / slices, 4) if slices else 0.0
        unusable_axis = slices > 0 and unusable_ratio > SLICE_FAILURE_ALERT_RATIO
        if not (name_axis or args_axis or unusable_axis):
            return
        axis = (
            "command_name" if name_axis
            else "unusable_response" if unusable_axis
            else "args"
        )
        raise CatalogCircuitOpen(
            json.dumps(
                {
                    "axis": axis,
                    "slices": slices,
                    "slice_failures": slice_failures,
                    "slice_failure_ratio": unusable_ratio,
                    "windows": stats.windows,
                    "entries_seen": stats.entries_seen,
                    "command_rejects": stats.command_rejects,
                    "command_reject_ratio": stats.command_reject_ratio,
                    "args_seen": stats.args_seen,
                    "args_kept": stats.args_kept,
                    "args_uncovered": stats.args_uncovered,
                    "args_keep_ratio": stats.args_keep_ratio,
                    # The window ORDINAL, not its provenance label: a manual's
                    # headings ARE its command names, and this diagnostic is
                    # written to `catalog_jobs.diagnostic`. An ordinal locates
                    # the window in the document just as well without putting
                    # document content in a diagnostics column.
                    "samples": [
                        {
                            "window": outcome.ordinal,
                            "reasons": sorted(
                                {r.reason for r in outcome.rejections}
                            ),
                        }
                        for outcome in outcomes[-3:]
                    ],
                },
                ensure_ascii=False,
            )[:4000]
        )

    # -------------------------------------------------------------- one window
    def _process_window(
        self,
        client: Any,
        job: Mapping[str, Any],
        window: ExtractionWindow,
        candidates: Sequence[str],
        carried: Sequence[str],
        position: int,
        cancel: CancelEvent,
        flushed: Mapping[str, _FlushedCandidate],
    ) -> _WindowWork:
        """Everything one window costs and produces, with no store writes.

        The window's OWN candidate list and the relayed one stay separate all
        the way down — the prompt lists them under different headings, and
        `validate_entry` takes `carried` as its own argument so a relayed name
        is exempt from the verbatim check while `window_outcome`'s
        uncovered-candidate ledger keeps meaning "names THIS window offered".

        Commands already written by an earlier window (`flushed`) do not get a
        second row: their accumulator is seeded from what that row already
        holds, this window merges into it, and the caller revises the row in
        place. Everything else — rejected rows, coverage bookkeeping, position
        monotonicity — is v1's, unchanged.
        """
        work = _WindowWork(candidates=tuple(candidates))
        merged: dict[str, dict] = {}
        # Cut once, here: every entry of every slice of this window grounds
        # against the same per-command segmentation, and recomputing it per
        # entry would walk a 12k window's lines once for each of the thirty
        # commands a slab can document.
        segments = window_segments(window, candidates, carried)
        anchors = list(window.element_ids)[:MAX_ANCHOR_ELEMENTS]
        excerpt = _clip(window.text, CANDIDATE_EXCERPT_CHARS)
        next_position = position
        # Parameters assigned somewhere in this window that no slice ever
        # answered for, collected across ALL slices and settled once the window
        # is done — see the settlement below for why attribution has to wait
        # until the whole window's accepted set is known. Bounded like every
        # other ledger here, with the cut counted rather than dropped: the list
        # would otherwise grow with the window's slice count while only the
        # first `MAX_WINDOW_REJECTIONS` of it can ever be written.
        unanswered: list[dict] = []
        unanswered_total = 0
        for extraction in extraction_slices(window):
            self._raise_if_stopped(job["id"], cancel)
            work.slices += 1
            entries, calls, failed = self._extract_slice(
                client, window, candidates, carried, extraction, cancel, job["id"]
            )
            work.calls += calls
            if failed:
                work.slice_failures += 1
                next_position += 1
                work.rows.append(
                    self._row(
                        job,
                        position=next_position,
                        window=window,
                        command_name="",
                        payload={"excerpt": excerpt, "anchors": anchors},
                        state="rejected",
                        reject_info={
                            "fields": [
                                {
                                    "field": "slice",
                                    "value": (
                                        f"{extraction.index + 1}/{extraction.total}"
                                    ),
                                    "reason": "model_response_unusable",
                                    "window": "",
                                }
                            ]
                        },
                    )
                )
            for entry in entries:
                # `extraction.param_names` — the ORIGINAL slice's assignment,
                # not whichever half an entry came back from. A halving is an
                # internal remedy and its halves partition this same list, so
                # holding one half's answer to the other half's names would
                # reject data the slice was legitimately asked for. Answering
                # a DIFFERENT slice's parameters is still caught: those names
                # are not in this list either.
                result = validate_entry(
                    entry,
                    window,
                    candidates,
                    assigned=extraction.param_names,
                    carried=carried,
                    segments=segments,
                )
                work.results.append(result)
                if result.accepted and result.entry is not None:
                    self._merge_entry(
                        merged,
                        result,
                        flushed=flushed,
                        anchors=anchors,
                        excerpt=excerpt,
                    )
                    continue
                next_position += 1
                claimed = ""
                for rejection in result.rejections:
                    if rejection.field == "command_name":
                        claimed = rejection.value
                        break
                work.rows.append(
                    self._row(
                        job,
                        position=next_position,
                        window=window,
                        command_name=claimed,
                        payload={"excerpt": excerpt, "anchors": anchors},
                        state="rejected",
                        reject_info=_reject_info(_rejection_records(result)),
                    )
                )
            # Coverage is a property of the SLICE, so it is settled once here
            # over everything the slice answered — not inside the entry loop,
            # where a halved slice would count each half as having ignored the
            # other's parameters, and a multi-command window would count each
            # entry as having ignored the other entries' parameters. A slice
            # that failed outright still lands here with an empty entry list,
            # which is exactly right: its whole assignment went unanswered and
            # the ledger must say so.
            uncovered = assignment_coverage(
                entries, extraction.param_names
            ).uncovered
            if uncovered:
                work.uncovered_args.extend(uncovered)
                records = [
                    {
                        "field": "arg",
                        "value": name,
                        "reason": "arg_not_returned",
                        "window": "",
                    }
                    for name in uncovered[:MAX_WINDOW_REJECTIONS]
                ]
                unanswered_total += len(records)
                room = max(0, MAX_WINDOW_REJECTIONS - len(unanswered))
                unanswered.extend(records[:room])
        # Who to blame for an unanswered parameter, decided once per window.
        #
        # The window's assignment is the window's WHOLE flag list, so an
        # unanswered `-density` says a parameter went missing, not WHICH
        # command lost it — the model keys parameters onto entries and a
        # parameter nobody returned was keyed onto nothing. With exactly one
        # accepted command in the window there is no ambiguity and the note
        # belongs on its row, where a reviewer sees the gap next to the command
        # it is a gap in. With several, every attribution is a guess, and
        # writing the same note onto each of them (as this used to) marks up
        # commands that answered everything they were asked for — the reviewer
        # reads "this command is missing -density" on a command that never had
        # it. The window-level ledger (`WindowOutcome.uncovered_args`, fed by
        # `work.uncovered_args` above) records the fact either way, so nothing
        # is lost by declining to name a culprit.
        if unanswered_total and len(merged) == 1:
            sole = next(iter(merged.values()))
            _extend_rejections(sole, unanswered)
            # What the bounded collection above already dropped. `_extend_
            # rejections` counts only what IT could not fit, so without this
            # the reported overflow would understate the loss by everything
            # trimmed before it ever saw the list.
            sole["rejections_overflow"] += unanswered_total - len(unanswered)
        for name, entry in merged.items():
            payload = _candidate_payload(entry)
            reject_info = _reject_info(
                entry["rejections"],
                entry["rejections_overflow"],
                desc_overflow=entry["desc_overflow"],
            )
            record = flushed.get(name)
            if record is not None:
                # An earlier window already wrote this command's row. Revise it
                # — never a second row for one command, which is the catalog's
                # standing shape contract. (`_settle_updates` is what happens
                # when the store refuses because a reviewer already acted on
                # that row.)
                work.updates.append(
                    _PendingUpdate(
                        candidate_id=record.id,
                        command_name=name,
                        entry=entry,
                        payload=payload,
                        reject_info=reject_info,
                    )
                )
                # The registry has to carry the MERGED accumulator forward, not
                # the one it was seeded from: a command spanning three windows
                # would otherwise have window 3 merge onto window 1's state and
                # silently drop window 2's parameters.
                record.entry = entry
                continue
            next_position += 1
            work.rows.append(
                self._row(
                    job,
                    position=next_position,
                    window=window,
                    command_name=name,
                    payload=payload,
                    state="candidate",
                    reject_info=reject_info,
                )
            )
            work.new_entries.append((next_position, name, entry))
        return work

    @staticmethod
    def _merge_entry(
        merged: dict[str, dict],
        result: ValidationResult,
        *,
        flushed: Mapping[str, _FlushedCandidate],
        anchors: Sequence[str],
        excerpt: str,
    ) -> None:
        """Fold one accepted entry into that command's single row.

        A multi-slice command produces one accepted entry per slice, all naming
        the same command; the catalog holds ONE row per command, so the args
        union and the overview fields (only slice 0 was asked for them) merge
        here. Args dedupe by name, first writer wins. Slices partition the
        parameter list, so across slices a duplicate means the model answered
        outside its assignment; WITHIN one slice it is the ordinary case, since
        a halved slice re-asks for parameters its first answer may already have
        covered (see `_extract_slice`).

        v2 adds a second axis with no new merge rule: a command whose
        documentation crosses a WINDOW boundary is merged by SEEDING this
        window's accumulator from the row an earlier window already wrote
        (`flushed`). First-writer-wins then means the earlier window's syntax
        and description survive, its parameters are already in the dedupe set,
        and every bound (examples, rejections, the description budget) keeps
        counting from where it was rather than restarting per window — which is
        the whole reason the merge is expressed as a seed rather than as a
        second, cross-window merge function.

        `anchors` are UNIONED across windows (bounded by `MAX_ANCHOR_ELEMENTS`)
        because a command really is evidenced by elements from each window it
        spans. `excerpt` is the FIRST window's and stays that way: it is a look
        at where the command is documented, and the place it is introduced is
        the useful one.

        `description`/`examples`/`args[].desc` are capped HERE, not later: they
        are the fields `validate_entry` deliberately never grounds (prose
        cannot be matched verbatim), so this is the only choke point between a
        model's free text and the row that eventually reaches the DB.
        `rejections`/`rejections_overflow` are capped here for a stronger
        reason than "cap it before the write" — a pathological multi-slice
        command (every slice rejecting a full parameter list) would otherwise
        let this in-memory accumulator itself grow without bound across dozens
        of slices, not just the final row `_reject_info` writes.
        """
        entry = result.entry
        assert entry is not None
        current = merged.get(entry.command_name)
        if current is None:
            current = _seed_accumulator(
                flushed.get(entry.command_name),
                entry.suspect_related,
                anchors,
                excerpt,
            )
            merged[entry.command_name] = current
        if entry.syntax and not current["syntax"]:
            current["syntax"] = entry.syntax
        if entry.description and not current["description"]:
            current["description"] = _clip(entry.description, MODEL_DESCRIPTION_CHARS)
        seen = {arg["name"] for arg in current["args"]}
        for arg in entry.args:
            if arg.name in seen:
                continue
            seen.add(arg.name)
            # Two bounds, in this order: each description on its own, then the
            # running total for this row. Only the SECOND one counts as
            # overflow — a 900-character description clipped to 400 is the
            # ordinary per-field cap doing its job, while a description cut
            # short (or emptied) because earlier parameters used up the row's
            # budget is a loss the reviewer has to be told about.
            desc = _clip(arg.description, MODEL_ARG_DESC_CHARS)
            room = max(0, MODEL_ARG_DESC_TOTAL_CHARS - current["desc_chars"])
            if len(desc) > room:
                desc = desc[:room]
                current["desc_overflow"] += 1
            current["desc_chars"] += len(desc)
            current["args"].append(
                {
                    "name": arg.name,
                    "required": bool(arg.required),
                    "desc": desc,
                    "default": arg.default,
                }
            )
        for example in entry.examples:
            if len(current["examples"]) >= MAX_MODEL_EXAMPLES:
                break
            clipped = _clip(str(example), MODEL_EXAMPLE_CHARS)
            if clipped not in current["examples"]:
                current["examples"].append(clipped)
        # AND, so any window that documents the command properly clears the
        # "possibly only mentioned" mark — but ONLY a window that actually
        # produced that evidence gets a vote. A RELAYED entry's
        # `suspect_related=False` is the relay's exemption, not a finding (see
        # `ValidatedEntry.relayed`): a continuation window holding nothing but
        # a parameter table has no heading and no usage line for the command,
        # so folding its False in with AND would erase the warning the window
        # that merely mentioned the command had earned — and erase it on
        # exactly the shape the relay exists to produce, i.e. always. First
        # writes are unaffected: the seed above takes this entry's own value
        # whatever it is, because there is no earlier finding to protect.
        if not entry.relayed:
            current["suspect_related"] = (
                current["suspect_related"] and entry.suspect_related
            )
        _extend_rejections(current, _rejection_records(result))

    @staticmethod
    def _row(
        job: Mapping[str, Any],
        *,
        position: int,
        window: ExtractionWindow,
        command_name: str,
        payload: Mapping[str, Any],
        state: str,
        reject_info: Mapping[str, Any],
    ) -> dict:
        return {
            "job_id": job["id"],
            "notebook_id": job["notebook_id"],
            "source_id": job["source_id"],
            "position": position,
            "section_path": _window_label(window),
            "command_name": command_name,
            "payload": dict(payload),
            "state": state,
            "reject_info": dict(reject_info),
        }

    # -------------------------------------------------------------- one slice
    def _client(self) -> Any:
        if not self.models.configured(CATALOG_WORKLOAD):
            raise CatalogModelUnavailable(MODEL_UNAVAILABLE_MESSAGE)
        return self.models.chat(CATALOG_WORKLOAD)

    def _extract_slice(
        self,
        client: Any,
        window: ExtractionWindow,
        candidates: Sequence[str],
        carried: Sequence[str],
        extraction: ExtractionSlice,
        cancel: CancelEvent,
        job_id: str,
        depth: int = 0,
    ) -> tuple[list[Mapping[str, Any] | None], int, bool]:
        """One slice's ENTRIES, the calls it cost, and whether it failed.

        Three remedies, all bounded, and all keyed on what this seam can
        actually observe.

        ``finish_reason`` is NOT observable here: ``chat_json`` returns the
        content string alone (``JsonChatClientPort.chat_json``'s signature is
        pinned by a contract test, so widening it is a change to a heavily
        guarded seam, not a local one). What a length-truncated reply looks
        like from this side is a non-empty body that will not parse as a JSON
        object — which the provider reports with the stable error code
        ``malformed_response`` (see ``_call`` for why the code, not the
        exception class, is what this branches on). So "malformed" is the
        length signal and the remedy is C0's: halve the slice's parameter list
        and ask again, up to ``MAX_SLICE_SPLIT_DEPTH``.

        An empty body is C0's other measured failure (a retry came back empty).
        Be precise about what this layer can actually tell apart, because the
        obvious description is wrong: ``_validate_json_object`` raises the SAME
        ``malformed_response`` for an empty body as for a truncated one, so on
        the production path empty and truncated are ONE signal, and both take
        the halving branch. The dedicated ``kind="empty"`` below only fires for
        a client that hands back a string (an offline double, or a raw client
        used directly).

        That is acceptable, and the reason is the cost bound: a slice that
        never answers costs at most ``MAX_CALLS_PER_SLICE``, and the failure is
        recorded as a rejected row either way — never silently dropped, which
        is the entire point. It is NOT "one retry", and any doc that says so is
        describing the double rather than production.

        The halves reuse the parent's ``text_window`` unchanged. That is
        deliberate, not laziness: the window is already bounded
        (``WINDOW_CHARS``) and the failure being treated is on the OUTPUT side,
        so narrowing the input would trade a real context loss for no gain.

        The returned ``failed`` is defined as "this slice produced NO usable
        payload at all", not "something along the way went wrong" — so after a
        halving, one successful half is enough to make the WHOLE original
        slice not count as a failure, even though its sibling half may have
        failed outright. This matters for the third circuit-breaker axis
        (``SLICE_FAILURE_ALERT_RATIO`` in ``_check_circuit``): that axis exists
        to catch a slice that answers NOTHING, and a half-successful slice —
        which still contributes real candidates — is exactly the case it must
        NOT trip on. Counting it as a failure anyway would only ever
        UNDER-count how much of the manual actually got extracted while
        OVER-counting how broken the run looks.

        The THIRD remedy treats an answer that arrives intact but covers only a
        fraction of the parameters it was assigned — the failure the R2 review
        found: before it, one returned parameter out of twenty was a complete
        success by this seam's judgement and by the ledger's. It reuses the same
        halving, because it is the same underlying complaint (the answer did
        not fit the ask), under the gate in `_coverage_retry_warranted`. It runs
        at depth 0 ONLY, which is what leaves `MAX_CALLS_PER_SLICE` untouched:
        the two paths are mutually exclusive at a given depth (`malformed` means
        no payload, coverage means there is one), and both cost
        `1 + 2·f(1)` = 11 in the worst case.

        The recovered halves are ADDED to the original payload rather than
        replacing it: the original is the one that carries the overview fields
        (`include_overview` belongs to the parent slice), `_merge_entry` dedupes
        args by name so a parameter answered twice still lands once, and never
        discarding an answer already paid for is this module's standing rule.
        """
        outcome = self._call(
            client, window, candidates, carried, extraction, cancel, job_id
        )
        calls = 1
        if outcome.payload is not None:
            entries = _payload_entries(outcome.payload)
            if depth == 0:
                coverage = assignment_coverage(entries, extraction.param_names)
                if _coverage_retry_warranted(coverage):
                    recovered, extra_calls, _usable = self._halve_and_ask(
                        client, window, candidates, carried, extraction,
                        cancel, job_id, depth,
                    )
                    entries.extend(recovered)
                    calls += extra_calls
            return entries, calls, False
        if (
            outcome.kind == "malformed"
            and depth < MAX_SLICE_SPLIT_DEPTH
            and len(extraction.param_names) > 1
        ):
            entries, half_calls, usable = self._halve_and_ask(
                client, window, candidates, carried, extraction, cancel,
                job_id, depth,
            )
            calls += half_calls
            # NOT the OR of each half's own `failed` — see the docstring: one
            # successful half means this slice, as a whole, produced usable
            # output. And NOT `not entries` either, which is the v2 trap: a
            # half that answered `entries: []` answered CORRECTLY (this text
            # documents no command), so emptiness is not failure. `usable`
            # carries the distinction the entry list cannot.
            return entries, calls, not usable
        retry = self._call(
            client, window, candidates, carried, extraction, cancel, job_id
        )
        calls += 1
        if retry.payload is not None:
            return _payload_entries(retry.payload), calls, False
        return [], calls, True

    def _halve_and_ask(
        self,
        client: Any,
        window: ExtractionWindow,
        candidates: Sequence[str],
        carried: Sequence[str],
        extraction: ExtractionSlice,
        cancel: CancelEvent,
        job_id: str,
        depth: int,
    ) -> tuple[list[Mapping[str, Any] | None], int, bool]:
        """Split this slice's assignment in two and ask for each half.

        Shared by both callers so the split — and the fact that only the FIRST
        half inherits the overview responsibility — has one definition. The
        halves reuse the parent's ``text_window`` unchanged (see the caller's
        docstring: the failure being treated is on the output side).

        The third return value is "at least one half produced a USABLE answer",
        which the entry list itself cannot express: both halves answering
        `entries: []` is a legal, complete answer that returns no entries at
        all, and reading emptiness as failure would charge the breaker's
        unusable-response axis for a model doing exactly as told.
        """
        middle = len(extraction.param_names) // 2
        halves = (
            replace(extraction, param_names=extraction.param_names[:middle]),
            replace(
                extraction,
                param_names=extraction.param_names[middle:],
                include_overview=False,
            ),
        )
        entries: list[Mapping[str, Any] | None] = []
        calls = 0
        usable = False
        for half in halves:
            half_entries, half_calls, half_failed = self._extract_slice(
                client, window, candidates, carried, half, cancel, job_id,
                depth + 1,
            )
            entries.extend(half_entries)
            calls += half_calls
            usable = usable or not half_failed
        return entries, calls, usable

    def _call(
        self,
        client: Any,
        window: ExtractionWindow,
        candidates: Sequence[str],
        carried: Sequence[str],
        extraction: ExtractionSlice,
        cancel: CancelEvent,
        job_id: str,
    ) -> _SliceOutcome:
        # The liveness probe here is what makes the guarantee actually cover
        # "every model call": `_extract_slice`'s halving and coverage-retry
        # branches call `_call` directly, recursing through `_halve_and_ask`
        # without ever returning to `_process_window`'s per-slice loop — so
        # THIS is the one choke point every `chat_json` call, including every
        # retry, is guaranteed to pass through. See `_raise_if_stopped`.
        self._raise_if_stopped(job_id, cancel)
        kwargs: dict[str, Any] = dict(cap_kwargs(client, "kg_extract_max_tokens"))
        if getattr(client, "settings", None) is not None:
            # The sole admission ticket into the content-addressed cache — read
            # AND write — nothing more (see `app/services/command_catalog.py`'s
            # own contract note: it has no bearing on retrying a malformed
            # reply, which is `_extract_slice`'s own halving logic below).
            # Gated on `settings` exactly like the KG extractors are, so the
            # hand-rolled doubles that accept neither kwarg keep working.
            kwargs["response_validator"] = _entry_validator(
                window, candidates, carried, extraction
            )
            kwargs["cancel_event"] = cancel
        try:
            raw = client.chat_json(
                [
                    {
                        "role": "user",
                        "content": _prompt(
                            window, candidates, carried, extraction
                        ),
                    }
                ],
                _SCHEMA_HINT,
                **kwargs,
            )
        except AskCancelled:
            # `chat_json` polls the SAME `cancel_event` this module passed it
            # (see `kwargs["cancel_event"] = cancel` above) and raises this —
            # a SIBLING of `ModelProviderError`, not a subclass, so the clause
            # below never catches it. Every other cancellable job in this
            # codebase treats an in-flight `AskCancelled` as ITS OWN
            # cancellation signal (`report_engine.py`, `ask_service.py`, …);
            # this run's own signal is `CatalogCancelled`, and translating it
            # here is what lets `run()`'s single `except CatalogCancelled`
            # clause settle the row as `cancelled`. Without this, a cancel
            # that lands WHILE a model call is in flight falls through to
            # `run()`'s `except Exception`, settles as `failed` with
            # `INTERNAL_FAILURE_MESSAGE`, and re-raises into
            # `background_jobs`' error log — for a stop the owner asked for.
            raise CatalogCancelled() from None
        except ModelProviderError as exc:
            # Classify by the provider's STABLE ERROR CODE, not by exception
            # class. `MalformedModelResponse` is what the raw client raises, but
            # nothing downstream ever sees that type: the scheduled adapter's
            # `_resolve` re-raises everything as `ModelInvocationError`, a
            # SIBLING of `MalformedModelResponse` under `ModelProviderError`
            # (`app/services/model_provider.py::_invocation_error`). An
            # `except MalformedModelResponse` here therefore never fires in
            # production — it would only ever match a test double raising the
            # raw type, which is exactly how such a hole survives a green suite.
            # `code` is the deliberately stable, credential-safe surface
            # (`_stable_error_code` / `_MODEL_ERROR_CODES`), so it is the thing
            # to branch on.
            #
            # Only `malformed_response` gets the halve-and-retry remedy. A
            # transient provider failure (rate limit, upstream 5xx, auth) is NOT
            # a too-long answer: the raw client already retried it with backoff,
            # asking for fewer parameters cannot help, and quietly turning an
            # outage into "this window had no commands" is the silent-empty
            # failure this module exists to prevent. Those propagate and fail
            # the job with a terminal state a person can see.
            if getattr(exc, "code", "") != "malformed_response":
                raise
            return _SliceOutcome(kind="malformed")
        if not str(raw or "").strip():
            # Only reachable for a client that RETURNS an empty string rather
            # than raising: an offline double, or the raw client used directly.
            # Through the scheduled adapter an empty body already arrived above
            # as `malformed_response`. Kept because the two are genuinely
            # different observations and collapsing them in code would hide
            # which one a given client made.
            return _SliceOutcome(kind="empty")
        data = safe_json(raw)
        if not data:
            return _SliceOutcome(kind="malformed")
        if not isinstance(data.get("entries"), list):
            # The v2 shape check, and it belongs HERE rather than downstream:
            # a reply without an `entries` LIST has not answered the question
            # that was asked, which is the same observation a truncated reply
            # makes, so it gets the same remedy (halve and re-ask). An EMPTY
            # list is not this case — that is a complete answer meaning "this
            # text documents no command", and it flows through as a payload
            # with zero entries.
            return _SliceOutcome(kind="malformed")
        return _SliceOutcome(payload=data)

    # ----------------------------------------------------------- review/apply
    def candidates_page(
        self, job_id: str, *, state: str, cursor: int, limit: int
    ) -> dict:
        """One keyset page plus the run's per-state totals.

        ``has_more`` is "this page came back full", which can be one page
        optimistic when the collection ends exactly on a page boundary; the
        next request then returns an empty page. That is the ordinary keyset
        trade-off and the honest one — the alternative (over-fetching one row to
        peek) costs a row on every page to save an empty request on some. The
        `counts` are exact, so a caller that needs a real total has one.
        """
        rows = self.catalog.list_candidates(
            job_id, state=state, cursor=cursor, limit=limit
        )
        counts = self.catalog.candidate_counts(job_id)
        last = rows[-1]["position"] if rows else cursor
        # Derived from the exact per-state total, not from "the page came back
        # full". Re-deriving the store's own clamp here would put the same
        # arithmetic in two places, and the counts query is already being made.
        return {
            "items": rows,
            "next_cursor": last,
            "has_more": bool(rows) and self._has_more_after(
                job_id, state=state, cursor=last
            ),
            "counts": counts,
        }

    def _has_more_after(self, job_id: str, *, state: str, cursor: int) -> bool:
        """Whether a further page exists, by asking for exactly one more row.

        One bounded keyset read against the same index the page just used. The
        alternative — inferring from a full page — is one page optimistic and
        makes a caller issue a request that returns nothing.
        """
        return bool(
            self.catalog.list_candidates(
                job_id, state=state, cursor=cursor, limit=1
            )
        )

    def apply(
        self,
        notebook_id: str,
        source_id: str,
        job_id: str,
        *,
        candidate_ids: Sequence[str] = (),
        all_pending: bool = False,
        actor: str = "",
    ) -> dict:
        """Land confirmed candidates in this source's knowhow catalog table.

        v1 merge semantics are deliberately conservative and the boundary is
        one rule: **an existing row is never touched.** A candidate whose
        command already has a row is reported as a conflict and left alone;
        only genuinely new commands are appended. The alternative — updating in
        place — would silently overwrite whatever a person edited by hand after
        the previous apply, and there is no way to tell an unedited stale row
        from a deliberately corrected one. A real diff/merge is a later task.

        ``all_pending`` confirms at most ``MAX_APPLY_CANDIDATES`` rows per call
        and reports what is left in ``pending_remaining``. Silently applying a
        prefix and answering ``rows_added: 100`` on a 300-candidate run would
        read as "done" — the same "claimed everything, delivered a page" shape
        the collection-enumeration contract forbids elsewhere in this codebase.

        The target table is resolved through ``job["applied_table_id"]`` first
        (see ``_resolve_target_table``), not by title on every call — a job
        that already landed rows once keeps writing to that SAME table even if
        it gets renamed in between two apply calls. That resolution happens
        INSIDE the lock; the lock itself keys on the NOTEBOOK alone (see
        ``_target_lock_key``), which is computable without reading anything,
        cannot change under a writer, and is therefore identical for every
        writer that could collide — two DIFFERENT sources deriving the same
        title, a job whose ``applied_table_id`` is already set racing a
        first-time apply, the same job applying twice from two tabs, or a
        confirm straddling the paper-title backfill that made the OLD
        title-derived key mutable (R14). None of them can race each other past
        the anchor-column existence check into a duplicate row or table.

        R8: a reparse between the run and this confirm makes every candidate
        describe a document that no longer exists, so the source-generation
        guard runs INSIDE the lock, before anything is read or written (see
        ``_require_current_generation``).

        R10: that guard is only meaningful while the source's parse barrier is
        held — otherwise the swap it checks for is free to commit between the
        check and ``append_knowhow_rows``. The barrier is taken OUTSIDE the
        catalog lock; see ``_source_write_barrier`` for the ordering argument.

        R12: the barrier and the generation check together still miss the
        PARSE STAGE of a reparse — ``process_source`` marks the source
        ``parsing`` long before it ever takes the chunk lock the barrier
        waits on, so a confirm landing in that window sees a free barrier and
        an unchanged generation and writes anyway. ``_require_not_parsing``
        closes that half, in the SAME locked window as the generation check
        (see its own docstring for why the two together close the whole
        lifecycle).
        """
        job = self.catalog.get_job(job_id)
        if job["notebook_id"] != notebook_id or job["source_id"] != source_id:
            raise KeyError(job_id)
        source = self._scoped_source(notebook_id, source_id)
        source_title = self._display_source_title(source_id, source)
        lock_key = self._target_lock_key(notebook_id)
        with self._source_write_barrier(source_id):
            with self._apply_lock(lock_key):
                self._require_current_generation(job)
                self._require_not_parsing(notebook_id, source_id)
                return self._apply_locked(
                    job, notebook_id, source_title,
                    candidate_ids=candidate_ids,
                    all_pending=all_pending,
                    actor=actor,
                )

    def _apply_locked(
        self,
        job: Mapping[str, Any],
        notebook_id: str,
        source_title: str,
        *,
        candidate_ids: Sequence[str],
        all_pending: bool,
        actor: str,
    ) -> dict:
        """The whole read-then-write, run with both mutexes already held.

        Entered ONLY from ``apply``, and only inside
        ``_source_write_barrier(source_id)`` → ``_apply_lock(("catalog", nb))``,
        in that order. Everything below is therefore free to read state and
        act on it several statements later — "does a table exist by this
        title", "does this command already have a row", "which candidates are
        still `candidate`" — because no other writer for this notebook's
        catalog can run between the read and the write.

        Nothing here may acquire a third lock, and in particular must not
        reach back for a source barrier (that is the one order that would
        close a cycle; see ``_target_lock_key`` for the full panorama and the
        enumeration of every holder of either mutex). ``_resolve_target_table``
        may CREATE the table from inside this window, which is exactly why the
        lock key cannot be anything the creation reveals or changes.
        """
        job_id = job["id"]
        if all_pending:
            rows = self.catalog.pending_candidates(
                job_id, limit=MAX_APPLY_CANDIDATES
            )
        else:
            rows = self.catalog.candidates_by_ids(
                job_id, candidate_ids, limit=MAX_APPLY_CANDIDATES
            )
        selected = [row for row in rows if row["state"] == "candidate"]
        if not any(str(row.get("command_name") or "").strip() for row in selected):
            # Nothing nameable to write. Do NOT create the table here: an empty
            # 「命令目录：X」 conjured by a no-op confirm is a user-visible object
            # nobody asked for. Prefer the job's own remembered target over a
            # fresh by-title lookup for the same reason `_resolve_target_table`
            # does — a rename between two applies must not surface the WRONG
            # table_id even on this read-only branch.
            applied_table_id = str(job.get("applied_table_id") or "")
            if applied_table_id:
                existing_id = applied_table_id
                table_title = self._known_table_title(applied_table_id, source_title)
            else:
                existing_id = self._find_table(notebook_id, source_title)
                # Found (or not) BY the derived title, so the derived string
                # is exact here — no drift possible, nothing to re-read.
                table_title = (
                    f"{CATALOG_TABLE_TITLE_PREFIX}{source_title}" if existing_id else ""
                )
            return {
                "table_id": existing_id or "",
                "table_title": table_title,
                "created": False,
                "applied": [],
                "rows_added": 0,
                "conflicts": [],
                "pending_remaining": self._pending_remaining(job_id, 0),
            }
        table_id, created, columns, table_title = self._resolve_target_table(
            job, notebook_id, source_title, actor
        )
        # Columns are addressed BY NAME, never by position. The target table is
        # an ordinary knowhow table with live add/delete/reorder endpoints, so a
        # positional zip against `columns` would keep "working" after a user
        # reorders them and quietly file every syntax block under 说明. Names
        # the table no longer has are dropped; a table that lost its command
        # column cannot be written to at all.
        #
        # A name match alone is NOT enough: a user can rename the anchor
        # column away from 「命令」 while adding an ordinary attribute column
        # also called 「命令」, or add a SECOND 「命令」 column outright. Either
        # shape must be refused, not silently written to whichever column a
        # dict comprehension happens to keep last —
        # `append_knowhow_rows_skipping_existing_anchors` writes the ANCHOR
        # value into whatever id this resolves to, and the anchor is what
        # makes a row a graph node named after the command (see the module
        # header comment). Exactly one column may be named 「命令」, and it
        # must BE the table's anchor (`role == "anchor"`).
        command_columns = [
            column for column in columns
            if str(column.get("name") or "") == CATALOG_COMMAND_COLUMN
        ]
        if len(command_columns) != 1 or command_columns[0].get("role") != "anchor":
            raise CatalogApplyTargetInvalid(APPLY_TABLE_SHAPE_MESSAGE)
        command_column = command_columns[0]["id"]
        column_ids_by_name = {
            str(column.get("name") or ""): column["id"] for column in columns
        }
        # Bounded existence check: which of THIS PAGE's candidate names (at
        # most `MAX_APPLY_CANDIDATES`) already have a row, answered by one
        # indexed lookup on the anchor column — never a full `get_knowhow_table`
        # hydrate of a target table that may already hold thousands of rows.
        #
        # This is a PRE-READ, not the decision: it only lets an already-taken
        # name be classified as a conflict without paying for a doomed write.
        # `_apply_lock` serializes concurrent catalog APPLIES for this
        # notebook, but does not cover the ordinary knowhow row/cell edit
        # endpoints — a user can add a row naming the same command through
        # the live table UI in the window between this read and the write
        # below. Whatever actually lands is decided authoritatively by
        # `append_knowhow_rows_skipping_existing_anchors`, which re-checks
        # anchor membership INSIDE the same write transaction as the insert.
        candidate_names = sorted(
            {
                str(row.get("command_name") or "").strip()
                for row in selected
                if str(row.get("command_name") or "").strip()
            }
        )
        existing = self.knowhow.knowhow_anchor_existing_values(
            command_column, candidate_names
        )

        applied: list[str] = []
        conflicts: list[dict] = []
        batch: list[dict] = []
        candidate_by_name: dict[str, str] = {}
        claimed: set[str] = set()
        for row in selected:
            name = str(row.get("command_name") or "").strip()
            if not name:
                continue
            if name in existing or name in claimed:
                conflicts.append({"candidate_id": row["id"], "command_name": name})
                continue
            claimed.add(name)
            candidate_by_name[name] = row["id"]
            cells = _catalog_cells(
                name,
                row.get("payload") or {},
                source_title,
                row.get("section_path") or "",
            )
            batch.append(
                {
                    column_ids_by_name[column]: value
                    for column, value in cells.items()
                    if column in column_ids_by_name
                }
            )
        rows_added = 0
        if batch:
            # Two writes, in this order on purpose. They are not one
            # transaction — they live in different stores, and forcing them
            # into one would mean the catalog store reaching into knowhow's
            # write path, which is exactly what "go through the knowhow service
            # layer" (and its change-history guarantee) forbids.
            #
            # Crashing between them leaves the rows appended and the candidates
            # still marked `candidate`. That is the safe direction: a re-apply
            # then finds those commands already present and reports them as
            # conflicts, so it adds nothing and changes nothing. The opposite
            # order would mark candidates applied and then fail to write them —
            # silently losing work with no way to notice.
            #
            # The knowhow write itself is now atomic against a concurrent
            # ordinary edit (see the method's own docstring), so its
            # `skipped_anchor_values` is AUTHORITATIVE — it overrides this
            # pre-read's belief that these names were free. A name it reports
            # inserted is applied; a name it reports skipped is reclassified
            # as a conflict, exactly like a pre-read hit above.
            result = self.knowhow.append_knowhow_rows_skipping_existing_anchors(
                table_id, command_column, batch, actor=actor, origin="import"
            )
            skipped_names = result["skipped_anchor_values"]
            for name, candidate_id in candidate_by_name.items():
                if name in skipped_names:
                    conflicts.append(
                        {"candidate_id": candidate_id, "command_name": name}
                    )
                else:
                    applied.append(candidate_id)
            rows_added = len(result["row_ids"])
            if applied:
                self.catalog.mark_candidates_applied(job_id, applied)
        if conflicts:
            # A conflict candidate is never applied, but it must still leave
            # `candidate` state — otherwise `pending_candidates`'s cursor=0
            # keyset read returns this exact page forever, and a source whose
            # first page is entirely conflicts (e.g. a re-run after a prior
            # apply already landed every command) could never be confirmed
            # past it: repeated "confirm all" would sit at the same
            # `(rows_added=0, len(conflicts), pending_remaining)` triple.
            # `dismissed` is a terminal, non-`candidate` state distinct from
            # `rejected` — this row WAS a legitimate command, it just already
            # has a row, so the reason is recorded rather than silently
            # dropped.
            self.catalog.mark_candidates_dismissed(
                job_id,
                [conflict["candidate_id"] for conflict in conflicts],
                reject_info={"reason": "conflict_existing_row"},
            )
        return {
            "table_id": table_id,
            "table_title": table_title,
            "created": created,
            "applied": applied,
            "rows_added": rows_added,
            "conflicts": conflicts,
            "pending_remaining": self._pending_remaining(job_id, len(applied)),
        }

    # ------------------------------------------------------------- dismiss
    def dismiss(
        self,
        notebook_id: str,
        source_id: str,
        job_id: str,
        *,
        candidate_ids: Sequence[str] = (),
        all_pending: bool = False,
    ) -> dict:
        """Mark selected candidates `dismissed` without landing any row.

        R7 (codex PR #412 review): the R5/R6 pending-candidates guard blocks a
        new run while the source's latest job still has unreviewed
        (`candidate`-state) rows — but the ONLY writer that ever moved a row
        out of `candidate` without applying it was `_apply_locked`'s own
        conflict branch, reached only when the candidate's command already has
        a row elsewhere. A reviewer who looks at a candidate and simply does
        not want it had no route at all: the guard's own copy
        (`pending_candidates_message`, `catalogPendingReviewNote`) has always
        said "confirm OR DISMISS", promising an action this module never
        implemented. This is that action.

        Selection is `candidate_ids` xor `all_pending`, capped at
        `MAX_APPLY_CANDIDATES` — identical to `apply`'s own contract, because
        this is a page-scoped human decision, never a whole-run sweep.

        The catalog lock is taken here too, even though dismiss never
        touches knowhow at all. It has to be: `apply`'s conflict branch calls
        the SAME `mark_candidates_dismissed` this method calls, and the two
        are a read-then-write sequence each (read which rows are still
        `candidate`, then write). Without a shared lock, a dismiss racing an
        in-flight apply for the SAME candidate can win the `state` column
        AFTER that apply has already written the candidate's row into the
        knowhow table — the candidate would end up reporting `dismissed`
        (which every other caller reads as "never written") while the target
        table holds it, and `mark_candidates_applied`'s own `WHERE
        state='candidate'` guard would then silently update zero rows instead
        of surfacing the clash. Sharing `apply`'s lock closes that: whichever
        writer reaches the target first finishes its whole read-then-write
        sequence — table write included — before the other's own read can
        even start, so a dismiss that loses the race always observes the
        candidate as already `applied` and correctly excludes it (see
        `_dismiss_locked`), never re-labels it.

        R8: the source-generation guard runs here too, and NOT because a
        dismiss could write stale content — it writes nothing. It runs because
        the guard's own sweep is what releases the restart block: a reviewer
        clicking 「跳过全部待审阅」 on a reparsed source would otherwise clear
        one page at a time under a `user_dismissed` reason that misreports why
        those rows died. One refusal that expires the whole set with the honest
        reason is both correct and fewer clicks.

        R10: the parse barrier is taken here too, and again not because this
        path writes document content — it does not. It is taken because the
        R8 guard's SWEEP is a write: without the barrier, a dismiss can read
        the generation as current, a reparse can commit, and the sweep then
        records `user_dismissed` on a set that in fact died of
        `source_reparsed` — mislabelling the very reason the 「已跳过」 tab
        exists to show. Same order as `apply`, so the two remain deadlock-free
        against each other for the trivial reason that they are identical.

        R12: `_require_not_parsing` runs here too, for the same mislabelling
        reason R10 gives above — a reparse's PARSE STAGE runs entirely before
        the barrier exists to catch it (see that method's docstring), and a
        dismiss landing in that window would record `user_dismissed` on
        candidates a reparse is actively in the middle of possibly
        invalidating.
        """
        job = self.catalog.get_job(job_id)
        if job["notebook_id"] != notebook_id or job["source_id"] != source_id:
            raise KeyError(job_id)
        # Scoping is still re-checked here even though nothing below reads the
        # source: `dismiss` takes a caller-supplied `source_id`, and the lock
        # key no longer needs a title to be derived from it (R14).
        self._scoped_source(notebook_id, source_id)
        lock_key = self._target_lock_key(notebook_id)
        with self._source_write_barrier(source_id):
            with self._apply_lock(lock_key):
                self._require_current_generation(job)
                self._require_not_parsing(notebook_id, source_id)
                return self._dismiss_locked(
                    job_id, candidate_ids=candidate_ids, all_pending=all_pending
                )

    def _dismiss_locked(
        self,
        job_id: str,
        *,
        candidate_ids: Sequence[str],
        all_pending: bool,
    ) -> dict:
        if all_pending:
            rows = self.catalog.pending_candidates(job_id, limit=MAX_APPLY_CANDIDATES)
        else:
            rows = self.catalog.candidates_by_ids(
                job_id, candidate_ids, limit=MAX_APPLY_CANDIDATES
            )
        # Only rows still `candidate` are eligible — the same filter `apply`
        # applies to its own `selected` list. A candidate that has already
        # moved on (applied by a racing apply that reached the lock first, or
        # already dismissed by an earlier call) is silently excluded rather
        # than re-reported: it is not an error, it is just not this call's to
        # report a second time.
        selected = [row["id"] for row in rows if row["state"] == "candidate"]
        if selected:
            self.catalog.mark_candidates_dismissed(
                job_id, selected, reject_info={"reason": USER_DISMISSED_REASON}
            )
        return {
            "dismissed": selected,
            "pending_remaining": self._pending_remaining(job_id, 0),
        }

    def _pending_remaining(self, job_id: str, just_applied: int) -> int:
        """Candidates still awaiting confirmation after this call.

        Read from the exact per-state counts rather than inferred, and computed
        AFTER the write so a caller can trust it as the real remainder.
        """
        del just_applied  # counts are authoritative; the delta is not needed
        return int(self.catalog.candidate_counts(job_id).get("candidate", 0))

    def _find_table(self, notebook_id: str, source_title: str) -> str:
        title = f"{CATALOG_TABLE_TITLE_PREFIX}{source_title}"
        # R11 P2: a bounded point lookup on (notebook_id, title), not
        # `list_knowhow_tables` — that scan health-aggregates EVERY table in
        # the notebook (row counts, projection status, cell activity, code
        # inputs) just to throw away everything but a title match. See
        # `KnowhowStorePort.knowhow_table_id_by_title` for the tie-break
        # contract when more than one table shares the derived title.
        return self.knowhow.knowhow_table_id_by_title(notebook_id, title)

    def _ensure_table(
        self, notebook_id: str, source_title: str, actor: str
    ) -> tuple[str, bool]:
        existing = self._find_table(notebook_id, source_title)
        if existing:
            return existing, False
        title = f"{CATALOG_TABLE_TITLE_PREFIX}{source_title}"
        table_id = self.knowhow.create_knowhow_table(
            notebook_id,
            title,
            "",
            [dict(column) for column in CATALOG_TABLE_COLUMNS],
            self.current_user_id(),
            actor=actor,
            origin="import",
        )
        return str(table_id), True

    def _known_table_title(self, table_id: str, source_title: str) -> str:
        """The REAL current title of ``table_id`` — for a caller that already
        resolved it through the job's REMEMBERED ``applied_table_id`` rather
        than by a fresh by-title lookup.

        That fast path is the one place a derived
        ``f"{CATALOG_TABLE_TITLE_PREFIX}{source_title}"`` guess can be wrong:
        ``source_title`` can drift AFTER this job's first apply already
        created/landed rows in a table (async paper-metadata backfill
        completing mid-job — see
        ``test_apply_table_name_snapshot_survives_a_paper_title_backfill_mid_job``),
        or the table can have been renamed by hand between two applies. Both
        by-title paths (create, or find-by-title) do not have this problem —
        the derived string IS how they located or named the row — so only
        this one caller needs the extra point read. ``KeyError`` (the table
        vanished between its own existence check and this call) falls back to
        the derived guess rather than raising: this is a display value on an
        already-successful write, not a write-path invariant.
        """
        try:
            return self.knowhow.knowhow_table_title(table_id)
        except KeyError:
            return f"{CATALOG_TABLE_TITLE_PREFIX}{source_title}"

    def _resolve_target_table(
        self,
        job: Mapping[str, Any],
        notebook_id: str,
        source_title: str,
        actor: str,
    ) -> tuple[str, bool, list[dict], str]:
        """The table THIS apply writes to, its columns, whether it was just
        created, and its REAL current title — resolved WITHOUT ever
        hydrating the target's rows or cells (see
        ``KnowhowStorePort.knowhow_table_columns``).

        ``job["applied_table_id"]`` is tried first: it is the table THIS job
        has already been landing rows in, so reusing it is what survives the
        table being renamed between two apply calls for the same job — a
        by-title lookup would either miss it (rename) or, worse, resolve a
        DIFFERENT table that happens to share the derived title with another
        source. It is a hint, not trusted blindly: the table it names may
        since have been deleted, so ``knowhow_table_columns`` is asked to
        prove it is still there, and a ``KeyError`` falls through to the
        ordinary by-title create-or-find. Every path that settles on a
        ``table_id`` DIFFERENT from what the job currently has recorded
        writes it back, so the NEXT apply for this job takes the fast path.

        The title returned alongside it takes the SAME split: the fast path
        asks for the table's actual title (``_known_table_title`` — it is the
        one path where the derived string can be stale), while the create/
        by-title path returns the derived string directly — it is exact by
        construction there, so there is nothing to re-read.

        **R18 (codex PR #412 review round 18).** A job whose OWN
        ``applied_table_id`` is empty is not necessarily a source's FIRST
        job — it is exactly what a rerun's brand-new job looks like
        (「重新识别」 after a reparse, or simply confirming a second page of a
        fresh run). Falling straight through to by-title create-or-find in
        that case only finds the EARLIER job's table when the derived title
        still matches it — and two ordinary events break that match without
        touching the table's identity at all: a person renaming the table by
        hand, or paper-metadata grounding completing asynchronously and
        promoting the source's canonical title (``source_title`` is the
        derived string's only input). Either one used to fork a second
        「命令目录：<来源>」 for the SAME source, and same-name conflict
        detection could no longer see the original table's rows at all —
        the one irreversible thing this whole feature protects against.

        So between the fast path and create-or-find, one more hint is tried:
        ``_inherit_applied_table`` asks the catalog store for the most
        recently applied target ACROSS EVERY JOB THIS SOURCE HAS EVER HAD
        (``latest_applied_table_id`` — a bounded point query, not a scan),
        and — same discipline as the fast path — re-proves it before trusting
        it: the table must still exist, AND it must still belong to THIS
        notebook. The second half matters because a knowhow table can be
        copied or moved to a different notebook (``app/services/knowhow/
        transfer.py``); inheriting a stale reference to a table that no
        longer lives here would land this notebook's confirm in ANOTHER
        notebook's table, so a direct point lookup of the candidate's own
        ``notebook_id`` column (``knowhow_table_notebook_id``, R20) is
        required, not assumed. Either failure falls through identically to
        the fast path's own dangling reference: create or find by the
        derived title, never guess.

        An inherited target that no longer has the required anchor shape
        (checked by the caller, ``_apply_locked``) still fails loudly
        (``CatalogApplyTargetInvalid``) rather than silently forking a new
        table — the shape check already applies to whatever ``table_id`` this
        method returns, from any of the three paths.
        """
        applied_table_id = str(job.get("applied_table_id") or "")
        if applied_table_id:
            try:
                columns = self.knowhow.knowhow_table_columns(applied_table_id)
            except KeyError:
                columns = None
            if columns is not None:
                title = self._known_table_title(applied_table_id, source_title)
                return applied_table_id, False, columns, title
        inherited = self._inherit_applied_table(job, notebook_id)
        if inherited is not None:
            inherited_table_id, inherited_columns, inherited_title = inherited
            if inherited_table_id != applied_table_id:
                self.catalog.set_applied_table_id(job["id"], inherited_table_id)
            return inherited_table_id, False, inherited_columns, inherited_title
        table_id, created = self._ensure_table(notebook_id, source_title, actor)
        if table_id != applied_table_id:
            self.catalog.set_applied_table_id(job["id"], table_id)
        return (
            table_id,
            created,
            self.knowhow.knowhow_table_columns(table_id),
            f"{CATALOG_TABLE_TITLE_PREFIX}{source_title}",
        )

    def _inherit_applied_table(
        self, job: Mapping[str, Any], notebook_id: str
    ) -> tuple[str, list[dict], str] | None:
        """R18: the most recent OTHER job's applied target for this job's
        source — see the R18 paragraph on ``_resolve_target_table`` for why
        this exists. Returns ``None`` (never a stale id) when there is
        nothing safe to inherit.

        Existence and notebook membership are proven in ONE round trip each,
        all three inside the same ``try``: ``knowhow_table_columns`` answers
        existence (and is what the caller actually needs, so fetching it here
        avoids a second read on the fast path this feeds), ``knowhow_table_
        title`` supplies the table's CURRENT title (returned to the caller as
        a display value — see R20 below for why it is no longer part of the
        membership check itself), and ``knowhow_table_notebook_id`` answers
        membership. A ``KeyError`` from any of the three — the table is gone,
        full stop — is treated as nothing to inherit; there is no partial
        state worth distinguishing (a table missing one column but not
        another is not a shape SQLite or PostgreSQL can produce).

        Membership: the candidate's own ``notebook_id`` column must equal
        ``notebook_id``. A candidate that has moved to another notebook is
        treated identically to a deleted one — not "ours", fall through and
        let create-or-find decide. This never guesses: an id this method
        returns is one the caller can prove is both alive and local.

        **R20 (codex PR #412 review round 20).** Membership used to be proven
        by a title round-trip instead
        (``knowhow_table_id_by_title(notebook_id, title) ==
        candidate_table_id``), but a table's title is not unique — a user is
        free to rename tables to collide. When the inherited candidate had
        been renamed to a title that collides with an EARLIER, unrelated
        table in the SAME notebook, ``knowhow_table_id_by_title``'s
        documented creation-order tie-break resolved to that earlier table
        instead of the candidate, so the equality check failed for a target
        that in fact still belonged here — silently forking a second
        「命令目录：<来源>」 table, exactly the failure this whole method
        exists to prevent, reintroduced by its own membership check.
        ``knowhow_table_notebook_id`` reads the row's own column directly, so
        title collisions elsewhere in the notebook cannot perturb the answer.
        """
        candidate_table_id = self.catalog.latest_applied_table_id(
            str(job.get("source_id") or "")
        )
        if not candidate_table_id:
            return None
        try:
            columns = self.knowhow.knowhow_table_columns(candidate_table_id)
            title = self.knowhow.knowhow_table_title(candidate_table_id)
            owner_notebook_id = self.knowhow.knowhow_table_notebook_id(
                candidate_table_id
            )
        except KeyError:
            return None
        if owner_notebook_id != notebook_id:
            return None
        return candidate_table_id, columns, title


def _nothing_extracted(result: Mapping[str, Any]) -> bool:
    """Whether this run spent model calls and produced literally nothing.

    Four conditions, and each one excludes an outcome that is legitimately
    empty:

    * ``windows >= 1`` — at least one window actually made a call. A run whose
      every window was skipped by the cost gate never asked a model anything;
      it is a source that is not a manual, correctly costing nothing, and
      failing it would fail the gate's own success case.
    * ``entries_seen == 0`` — the model never even ATTEMPTED an entry. One
      attempt that grounding then vetoed is a different run: it produced a
      rejected row explaining itself.
    * ``candidate_rows == 0`` and ``rejected_rows == 0`` — nothing reached the
      review panel by either route. The second is the one worth spelling out:
      a run where every slice came back unusable writes rejected rows, and
      those ARE the answer ("the model returned garbage, here is where"), so it
      stays a success as far as this check is concerned. The breaker's
      unusable-response axis is what judges that run, on its own threshold.

    Deliberately not a fourth breaker axis: the breaker aborts a run mid-way on
    a ratio, while this is a verdict on a run that already finished, needs no
    threshold, and cannot be evaluated until the last window is done.
    """
    return (
        int(result.get("windows") or 0) >= 1
        and int(result.get("entries_seen") or 0) == 0
        and int(result.get("candidate_rows") or 0) == 0
        and int(result.get("rejected_rows") or 0) == 0
    )


def raise_if_catalog_cancelled(cancel: CancelEvent) -> None:
    if cancel is not None and cancel.is_set():
        raise CatalogCancelled()


def _coverage_retry_warranted(coverage: AssignmentCoverage) -> bool:
    """Whether an intact but partial answer is worth halving and re-asking.

    Three conditions, and the middle one is the one that keeps this from
    becoming a retry-everything loop:

    * a big enough assignment to halve meaningfully
      (``MIN_ASSIGNED_FOR_COVERAGE_RETRY``);
    * a SHORT answer — fewer parameters returned than were assigned. A model
      that returned twenty names for twenty assigned parameters answered the
      whole ask and simply got the names wrong; halving buys a second wrong
      answer at full price, and the breaker's args axis is what that case is
      for. Without this clause, the pathological "every parameter invented"
      run would triple its model spend on the way to being rejected anyway.
    * and coverage genuinely below ``SLICE_COVERAGE_RETRY_RATIO``.
    """
    return (
        coverage.assigned >= MIN_ASSIGNED_FOR_COVERAGE_RETRY
        and coverage.returned < coverage.assigned
        and coverage.covered < SLICE_COVERAGE_RETRY_RATIO * coverage.assigned
    )


def _payload_entries(
    payload: Mapping[str, Any]
) -> list[Mapping[str, Any] | None]:
    """One reply's entry list, with every non-object item turned into ``None``.

    ``_call`` has already proven ``entries`` is a list; this only normalises
    what is INSIDE it. ``None`` rather than "dropped" is the deliberate part:
    ``validate_entry(None, ...)`` produces an ordinary rejected result, so a
    model that answers `entries: ["set_db"]` leaves a visible rejected row
    instead of a silent gap, and ``assignment_coverage`` already documents
    ``Sequence[Mapping | None]`` as its input shape.
    """
    return [
        item if isinstance(item, Mapping) else None
        for item in payload.get("entries") or ()
    ]


def _entry_validator(
    window: ExtractionWindow,
    candidates: Sequence[str],
    carried: Sequence[str],
    extraction: ExtractionSlice,
) -> Callable[[str], bool]:
    """Cache-admission gate judged by running the DOWNSTREAM grounding itself.

    Same reasoning as the KG extractors' own gates: "is this reply usable" IS
    the consumption logic, and every shape approximation of it leaks. A reply
    that parses but names a command this window does not document, or that
    invents every parameter it was asked for, produces nothing downstream —
    freezing that nothing for the cache TTL is the poisoning case this closes.

    v2 judges a LIST of entries, and the clauses are per list rather than per
    entry: at least one entry has to survive grounding, and — when the slice
    asked for specific parameters — at least one assigned parameter has to
    survive somewhere across them. Per-entry would be wrong in both directions;
    a window legitimately documents several commands and one hallucinated
    neighbour must not veto a good reply, while a reply whose every entry is
    hallucinated must not be admitted because one of them was well-formed.

    An entries list that is EMPTY or that grounds nothing is refused. That is
    deliberately stricter than "it is a legal answer": `entries: []` really is
    legal downstream (`_extract_slice` treats it as a complete answer, and the
    breaker's unusable axis does not count it), but the cost of refusing it
    HERE is one cache miss, while the cost of admitting it is freezing "this
    window has nothing" for the full TTL on the strength of one lazy turn. The
    two judgements differ on purpose, and only in the conservative direction.

    "Of those" in the args clause is load-bearing and is why the assignment is
    handed to `validate_entry` here too: a reply that answers a DIFFERENT
    slice's parameters grounds perfectly against the window text, so without
    attribution it would clear this gate and be frozen into the
    content-addressed cache for the full TTL — served back on every later hit
    of a prompt it never actually answered. `validate_entry` rejects those as
    `arg_outside_slice`, which leaves `args_kept` at 0 and closes the gate. A
    slice with no parameters (a flagless command, or a later slice whose whole
    assignment was rejected upstream) is exempt, because zero kept args is then
    the correct answer rather than a failure. That exemption still holds now
    that the no-flag branch of `_prompt` asks for positional arguments: plenty
    of flagless commands (`report_dont_use`) genuinely take none, and an empty
    `args` from one of those is a correct, cacheable answer — the
    invented-argument case is caught where it always was, by grounding, not by
    counting.

    A structural check on `args` runs BEFORE any of that, and deliberately
    does not delegate to `validate_entry`'s own (non-fatal) handling of the
    same shape problem: `validate_entry` degrades a non-list `args` field to
    an empty list so one malformed field never vetoes an otherwise-good entry
    downstream, but that same degrade would read as "zero kept args" here —
    exactly the shape the flagless-command exemption above treats as a
    correct, cacheable answer. Without a check here, a structurally broken
    reply to a flagless slice (`args` returned as an object instead of a
    list) would sail through both this gate's clauses and freeze into the
    cache for the full TTL. Rejecting on shape alone, before grounding even
    runs, closes that gap without touching `validate_entry`'s own leniency.
    A non-object ENTRY is refused the same way and for the same reason: the
    run path turns it into a visible rejected row (`_payload_entries`), which
    is the right treatment for one bad answer and the wrong thing to freeze.
    """

    # Same cut the run path makes, hoisted out of the closure: the validator
    # runs the real grounding over every entry of every reply, so recomputing
    # the segmentation per entry would repeat it for each command a window
    # documents, on every admission check.
    segments = window_segments(window, candidates, carried)

    def validator(content: str) -> bool:
        payload = safe_json(content)
        if not payload or "error" in payload:
            return False
        entries = payload.get("entries")
        if not isinstance(entries, list):
            return False
        accepted = 0
        args_kept = 0
        for item in entries:
            if not isinstance(item, Mapping):
                return False
            raw_args = item.get("args")
            if raw_args is not None and not isinstance(raw_args, list):
                return False
            result = validate_entry(
                item,
                window,
                candidates,
                assigned=extraction.param_names,
                carried=carried,
                segments=segments,
            )
            if result.accepted:
                accepted += 1
                args_kept += result.stats.args_kept
        if not accepted:
            return False
        if extraction.param_names and args_kept < 1:
            return False
        return True

    return validator


def _prompt(
    window: ExtractionWindow,
    candidates: Sequence[str],
    carried: Sequence[str],
    extraction: ExtractionSlice,
) -> str:
    """The one prompt this feature has.

    v2 asks for EVERY command the window documents, because a window is a slab
    of the document rather than one command's section: the reply is a list, and
    an empty list is a legal answer (a prose window that slipped past the gate
    because it carries a relayed name really does document nothing new).

    The served candidate list is a constraint, not a menu: C0 measured 5/5
    command-name accuracy with a list, and `validate_entry` vetoes any name off
    it. The RELAYED list is printed as its own block rather than merged into
    the first, and that separation is the point of the block: the model has to
    be able to key an orphaned parameter table onto a command whose name is one
    window back, and it can only do that if it is told which names are in that
    position. Merging the two lists would serve the same names while hiding the
    one fact that makes them useful.

    When the window has NO candidates of its own, though, the relay is not a
    supplementary block — it is the entire list of names that may be claimed,
    and it is rendered as such. The two-block form would print
    「choose a name from this list: - (none)」 above it, which is an instruction
    to return nothing, addressed to a model whose actual job here is to
    attribute an orphaned parameter table to the command in the block below.
    Continuation windows are most of every large command's documentation, so
    that reads as a small wording detail and behaves as switching the feature
    off for them.

    The parameter list is the slice's assignment, spelled with its original
    leading dash because dropping that dash is the single most common
    infidelity C0 saw — and one a naive containment check would not catch,
    since the manual's own text does contain the bare word. The assignment is
    the WINDOW's flag list, so a multi-command window's parameters arrive as
    one list and the model keys each onto the entry it belongs to;
    `validate_entry` then holds every returned name to that same list, so
    attribution stays exact without the prompt having to partition it.

    The two parameter branches are NOT "ask" and "do not ask". An empty
    assignment only means `parameter_names` found no flag-shaped name in the
    window, and `parameter_names` is a FLAG scanner — a command documented as
    `set_dont_use lib_cells` has a real parameter that regex can never produce.
    Ordering the model to return `args: []` there (as this prompt used to) threw
    away the argument metadata of an entire command class that every layer
    downstream already supports: `_usage_identifier` accepts the positional form
    as command evidence, and `validate_entry`'s dash test is deliberately
    evidence-based so a bare `lib_cells` grounds. So the no-flag branch asks for
    positional arguments instead — with no list to copy from, since there is
    none to serve; grounding, not an assignment, is what keeps that honest
    (`_check_arg_name` still requires the name verbatim in the window text).

    What that does to the ledger, in one place because three readers care:
    `args_seen`/`args_kept` count these answers like any other (the keep ratio
    stays "of what came back, how much was really in the manual"), while
    `assignment_coverage` contributes nothing — with no assignment there is
    nothing that can go unanswered. So a flagless window can now make the
    breaker's args axis live, and what it measures there is exactly right: a
    positional argument copied from the usage line keeps the ratio at 1.0, and
    a run inventing them window after window is a run worth stopping.
    """
    if candidates:
        names_block = (
            "Choose each command name from this list, copied character for "
            "character:\n"
            + "\n".join(f"- {name}" for name in candidates)
            + "\n"
        )
        if carried:
            names_block += (
                "\nCommands possibly continuing from earlier text — parameters "
                "in this text may belong to them, and their own name may not "
                "appear here at all:\n"
                + "\n".join(f"- {name}" for name in carried)
                + "\n"
            )
    elif carried:
        # The relay IS the list. See the docstring: `- (none)` above a relay
        # block tells the model to answer nothing on exactly the windows the
        # relay exists to rescue.
        names_block = (
            "Commands possibly continuing from earlier text — this excerpt "
            "continues their documentation, so choose each command name from "
            "this list, copied character for character; their own name may not "
            "appear here at all:\n"
            + "\n".join(f"- {name}" for name in carried)
            + "\n"
        )
    else:
        # Unreachable from `run`: `window_needs_model` requires a claimable
        # name, so a window with neither list never becomes a call. Kept so
        # this function is total for a caller that builds a prompt by hand.
        names_block = (
            "Choose each command name from this list, copied character for "
            "character:\n- (none)\n"
        )
    if extraction.param_names:
        params = "\n".join(f"- {name}" for name in extraction.param_names)
        params_block = (
            "\nExtract ONLY these parameters, and nothing else, keying each one "
            "onto the command it belongs to:\n"
            f"{params}\n"
            "Copy each name character for character, including any leading dash.\n"
        )
    else:
        params_block = (
            "\nThis text documents no flag-shaped parameters (`-name`), so "
            "there is no assigned list. A command may still take POSITIONAL "
            "arguments: return those, taking each name from the usage line that "
            "invokes the command — `set_dont_use lib_cells` takes one argument, "
            "named `lib_cells`. Copy each name character for character from the "
            "source text below, and return `args`: [] for a command that really "
            "takes none.\n"
        )
    if extraction.include_overview:
        overview_block = (
            "For each entry also return `syntax` (a contiguous copy of one usage "
            "line from the source text), `description` (one or two sentences), "
            "and `examples` (verbatim example invocations, at most three).\n"
        )
    else:
        overview_block = (
            "Return `syntax`, `description` and `examples` as empty — another "
            "call already covers them. Only `command_name` and `args` matter here.\n"
        )
    return f"""Catalogue EVERY command documented in this excerpt of a command reference.

{names_block}{params_block}{overview_block}
Rules:
- Return one entry per command the text below documents, and `entries`: [] when
  it documents none. Never repeat a command in two entries.
- Never invent. Every `command_name`, every `name`, every `default` and each
  `syntax` line must appear in the source text below exactly as written (a name
  listed above as possibly continuing from earlier text is the one exception —
  that name may be claimed without appearing here); if you cannot find it,
  leave the field empty rather than guessing.
- Do not translate, expand or tidy any identifier.
- `required` is true only when the source text says the parameter is required.

Return JSON only, matching: {_SCHEMA_HINT}

Source text (window {window.ordinal + 1}{_provenance_suffix(window)}):
<<<
{extraction.text_window}
>>>
"""


def _provenance_suffix(window: ExtractionWindow) -> str:
    """`", <breadcrumb>"` for the source-text label, or `""`.

    Best effort by construction — a window boundary falls where the character
    budget put it, not where a section starts — so it is offered as extra
    orientation and never as a claim about what the window contains.
    """
    return f", {window.provenance}" if window.provenance else ""


def _catalog_cells(
    command_name: str,
    payload: Mapping[str, Any],
    source_title: str,
    section_path: str,
) -> dict[str, str]:
    """One candidate rendered into the catalog table's six columns, BY NAME.

    Keyed by column name rather than returned as an ordered list, because the
    caller writes into a live user-editable table: a name that no longer exists
    is simply dropped, whereas a positional list would keep filling whatever
    column now sits at that index.

    Everything here is Markdown because a knowhow cell is Markdown; the syntax
    and example blocks are fenced so a shell line survives verbatim instead of
    being reflowed as prose.

    The 「出处」 column is where the candidate row's INTERNAL provenance label
    stops being internal. On the review panel that label is transient and the
    frontend translates the ordinal form (`window 3`) into readable copy; here
    it is written into a knowhow table, where a person keeps it, reads it
    months later and sees it in a graph. `window 3` means nothing in that
    context — it names a boundary the character budget put somewhere in a
    document, using a word that is not even the product's own vocabulary — so
    the ordinal form degrades to the source name alone. A real breadcrumb
    (`Global Placement > Commands`) is kept: that one genuinely says where in
    the document the command lives.

    The match is anchored, deliberately: a manual whose own section is called
    「window 3 configuration」 has a real breadcrumb that happens to start with
    the same two words, and dropping it would lose provenance the reader wants.
    Only a label that is EXACTLY the internal form is the internal form.
    """
    syntax = str(payload.get("syntax") or "").strip()
    description = str(payload.get("description") or "").strip()
    args = payload.get("args") or []
    examples = payload.get("examples") or []
    arg_lines = []
    for arg in args:
        if not isinstance(arg, Mapping):
            continue
        name = str(arg.get("name") or "").strip()
        if not name:
            continue
        parts = [f"`{name}`"]
        if arg.get("required"):
            parts.append("（必填）")
        desc = str(arg.get("desc") or "").strip()
        if desc:
            parts.append(f"— {desc}")
        default = str(arg.get("default") or "").strip()
        if default:
            parts.append(f"（默认 `{default}`）")
        arg_lines.append("- " + " ".join(parts))
    example_lines = [
        f"```\n{str(item).strip()}\n```"
        for item in examples
        if str(item).strip()
    ]
    label = str(section_path or "").strip()
    if _ORDINAL_LABEL_RE.match(label):
        label = ""
    provenance = " · ".join(part for part in (source_title, label) if part)
    return {
        CATALOG_COMMAND_COLUMN: command_name,
        "语法": f"```\n{syntax}\n```" if syntax else "",
        "参数": "\n".join(arg_lines),
        "说明": description,
        "示例": "\n\n".join(example_lines),
        "出处": provenance,
    }


__all__ = [
    "APPLY_TABLE_SHAPE_MESSAGE",
    "CATALOG_COMMAND_COLUMN",
    "CATALOG_TABLE_COLUMNS",
    "CATALOG_TABLE_TITLE_PREFIX",
    "CATALOG_WORKLOAD",
    "CIRCUIT_OPEN_MESSAGE",
    "CatalogApplyTargetInvalid",
    "CatalogCancelled",
    "CatalogCircuitOpen",
    "CatalogJobAlreadyRunning",
    "CatalogModelUnavailable",
    "CatalogPendingCandidates",
    "CatalogPreview",
    "CatalogSourceBusy",
    "CatalogSourceChanged",
    "CatalogSourceNotParsed",
    "CommandCatalogService",
    "INTERNAL_FAILURE_MESSAGE",
    "INTERRUPTED_MESSAGE",
    "MAX_APPLY_CANDIDATES",
    "MAX_CALLS_PER_SLICE",
    "MAX_MODEL_EXAMPLES",
    "MIN_ASSIGNED_FOR_COVERAGE_RETRY",
    "MODEL_ARG_DESC_CHARS",
    "MODEL_ARG_DESC_TOTAL_CHARS",
    "MODEL_DESCRIPTION_CHARS",
    "MODEL_EXAMPLE_CHARS",
    "MODEL_UNAVAILABLE_MESSAGE",
    "NOTHING_EXTRACTED_MESSAGE",
    "PARSED_SOURCE_STATUSES",
    "SLICE_COVERAGE_RETRY_RATIO",
    "SLICE_FAILURE_ALERT_RATIO",
    "SOURCE_BUSY_MESSAGE",
    "SOURCE_LOCK_WAIT_SECONDS",
    "SOURCE_NOT_PARSED_MESSAGE",
    "SOURCE_PARSE_FAILED_MESSAGE",
    "SOURCE_REPARSED_REASON",
    "SOURCE_REPARSING_MESSAGE",
    "SOURCE_STALE_MESSAGE",
    "SUBMISSION_FAILED_MESSAGE",
    "USER_DISMISSED_REASON",
    "pending_candidates_message",
]
