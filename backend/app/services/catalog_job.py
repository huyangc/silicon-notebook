"""Command-catalog extraction: the job, the model calls, and the apply step.

This is stage C1b of Plan C. ``app/services/command_catalog.py`` (C1a) decides
everything that can be decided without IO — is this a manual, which elements
belong to which command, which names may an entry claim, how is one command
split across calls, and what survives grounding. This module is the part that
touches the world: it reads a source's elements, drives one model call per
slice, writes the reviewable candidate rows, and — on an explicit human
confirmation — lands the confirmed rows in a knowhow table.

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
    MAX_SECTION_REJECTIONS,
    MIN_SECTIONS_BEFORE_ALERT,
    AssignmentCoverage,
    CommandSection,
    ExtractionSlice,
    SectionOutcome,
    ValidationResult,
    assignment_coverage,
    catalog_stats,
    command_candidates,
    command_sections,
    detect_manual_shape,
    extraction_slices,
    section_metas,
    section_outcome,
    validate_entry,
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
# one section into an unbounded row.
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

_SCHEMA_HINT = (
    '{"command_name": "string", "syntax": "string", "description": "string", '
    '"args": [{"name": "string", "required": true, "desc": "string", '
    '"default": "string"}], "examples": ["string"]}'
)

# User-readable copy. Everything a route hands to `user_error()` comes from
# here, and everything else this module records is diagnostic-only.
CIRCUIT_OPEN_MESSAGE = (
    "校验拦截率异常，疑似文档格式与识别规则不兼容；本次命令识别已停止，未生成命令目录。"
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
# How long apply/dismiss wait for the source's parse barrier before answering
# `SOURCE_BUSY_MESSAGE`. Any positive value is CORRECT (the barrier is what
# makes the write safe, not the length of the wait); this one is tuned for the
# only two holders that exist — `process_source` and the checkup backfill —
# both of which hold for far longer than any wait worth making a user sit
# through. Seconds rather than milliseconds only so that a barrier changing
# hands under a slow disk is absorbed instead of reported as contention.
SOURCE_LOCK_WAIT_SECONDS = 2.0
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
    source_id: str
    source_title: str
    signal: dict
    estimated_sections: int
    estimated_calls: int
    sampled: bool
    element_limit: int


@dataclass
class _SliceOutcome:
    """One model call's result, reduced to the three cases that matter."""

    payload: Mapping[str, Any] | None = None
    kind: str = ""  # "" | "empty" | "malformed"


@dataclass
class _SectionWork:
    rows: list[dict] = field(default_factory=list)
    results: list[ValidationResult] = field(default_factory=list)
    candidates: tuple[str, ...] = ()
    slices: int = 0
    slice_failures: int = 0
    calls: int = 0
    # Assigned parameters no slice of this section ever answered for. Bounded
    # by the section's own parameter list (itself bounded by
    # `MAX_SECTION_CHARS`), and capped again when it reaches `SectionOutcome`.
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
    """A candidate row's `reject_info`, bounded at `MAX_SECTION_REJECTIONS`.

    `overflow` is the count of records that got cut, carried alongside rather
    than silently dropped — mirroring `SectionOutcome.rejections_overflow` in
    `command_catalog.py`. Both write sites (a single slice's own rejections,
    and `_merge_entry`'s cross-slice accumulator) already hand this at most
    `MAX_SECTION_REJECTIONS` items; the cap here is the last line of defence
    for whichever one changes first.

    `desc_overflow` is a SEPARATE key rather than more of `overflow`, and
    deliberately so: the two count different losses (rejection records that did
    not fit the ledger vs parameter descriptions cut by
    `MODEL_ARG_DESC_TOTAL_CHARS`), and folding them together would leave a
    number that answers neither question. Omitted when zero, like `overflow`.
    """
    all_fields = list(fields)
    kept = all_fields[:MAX_SECTION_REJECTIONS]
    total_overflow = max(0, overflow) + (len(all_fields) - len(kept))
    info: dict[str, Any] = {"fields": kept}
    if total_overflow:
        info["overflow"] = total_overflow
    if desc_overflow > 0:
        info["desc_overflow"] = int(desc_overflow)
    return info


def _extend_rejections(
    entry: dict, records: Sequence[Mapping[str, Any]]
) -> None:
    """Append to a merged entry's bounded rejection ledger, counting the cut.

    One helper rather than two copies of the same three lines, because there
    are now two writers into that ledger — a slice's own grounding rejections
    and the parameters it was assigned and never answered — and the cap has to
    be shared between them, not applied twice to two independent budgets.
    """
    room = max(0, MAX_SECTION_REJECTIONS - len(entry["rejections"]))
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
    def preview(self, notebook_id: str, source_id: str) -> CatalogPreview:
        """Estimate, with zero model calls and a bounded read, what extracting
        this source would cost.

        Both the row count and each row's text are clipped in SQL (see
        ``CatalogStorePort.preview_elements``), and ``sampled`` is true if
        EITHER bound bit. The numbers are then a lower bound rather than a
        census: a manual whose parameter tables sit past a cap really does cost
        more than this says, and pretending otherwise would be the one failure
        mode a cost preview must not have.

        R8: the parse-status precondition is checked HERE as well as in
        ``start``. A cost preview over a source that has not been parsed yet is
        not merely useless, it is misleading in the one direction this estimate
        must never be wrong in — it reads whatever prefix of elements happens to
        exist (usually none) and reports "约 0 个命令节", which reads as "this
        document has nothing to extract" rather than "come back in a minute".
        """
        source = self._require_parsed(notebook_id, source_id)
        rows, clipped = self.catalog.preview_elements(
            source_id, limit=PREVIEW_ELEMENT_LIMIT, text_chars=PREVIEW_ELEMENT_CHARS
        )
        # BOTH bounds feed `sampled`, not just the row cap. Per-element
        # truncation is the one that actually distorts the estimate: clipping an
        # options table drops parameter names, which drops slices, so a manual
        # whose option tables run past PREVIEW_ELEMENT_CHARS can easily cost
        # several times the reported call count. Reporting `sampled=False` there
        # would be a promise this cannot keep.
        sampled = clipped or len(rows) >= PREVIEW_ELEMENT_LIMIT
        sections = command_sections(rows)
        # `section_metas`, NOT `command_sections()`'s own output: the latter
        # already GROUPED blocks under a command heading and dropped every
        # block that never joined one, so feeding it back in would make
        # `total_sections` trivially equal `command_shaped_sections` and the
        # ratio gate would never have anything left to reject. `estimated_*`
        # below still comes from `sections` — that number IS "how many
        # commands would be extracted", which is what a cost estimate needs.
        signal = detect_manual_shape(section_metas(rows))
        estimated_calls = sum(len(extraction_slices(section)) for section in sections)
        return CatalogPreview(
            source_id=source_id,
            source_title=self._canonical_source_title(source),
            signal={
                "total_sections": signal.total_sections,
                "identifier_headings": signal.identifier_headings,
                "syntax_sections": signal.syntax_sections,
                "flag_sections": signal.flag_sections,
                "command_shaped_sections": signal.command_shaped_sections,
                "command_ratio": signal.command_ratio,
                "flag_ratio": signal.flag_ratio,
                "is_manual": signal.is_manual,
                "reason": signal.reason,
            },
            estimated_sections=len(sections),
            estimated_calls=estimated_calls,
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
            result = self._run_sections(job, cancel)
            self.catalog.finish_job(job_id, "succeeded")
            self._emit(
                "catalog_job_finished",
                self.catalog.get_job(job_id),
                latency_ms=latency_ms(),
                model_calls=result["calls"],
            )
            return {**result, "job_id": job_id}
        except CatalogCancelled:
            self._settle(job_id, "cancelled", CANCELLED_MESSAGE, "cancelled_by_owner")
            self._emit(
                "catalog_job_finished",
                self.catalog.get_job(job_id),
                latency_ms=latency_ms(),
            )
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
            self._emit(
                "catalog_job_finished",
                self.catalog.get_job(job_id),
                latency_ms=latency_ms(),
            )
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

    def _run_sections(self, job: Mapping[str, Any], cancel: CancelEvent) -> dict:
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
        # the FIRST per-slice check inside `_process_section` — by which
        # point the whole-source read right below has already run for
        # nothing, for a job that was cancelled before it did any real work.
        # Combined with the liveness probe for the same reason: see
        # `_raise_if_stopped`.
        self._raise_if_stopped(job["id"], cancel)
        # One whole source's elements, deliberately: this is the exact fetch
        # chunking already performs for every source that is ingested, and C1a
        # is built to consume its rows unchanged. Sectioning needs document
        # order and heading boundaries across the whole file, so a keyset page
        # would have to be reassembled into the same list anyway. The per-run
        # memory ceiling is one source's element text — the same ceiling
        # `build_chunks_for_source` already accepts — and each resulting section
        # is then clipped to `MAX_SECTION_CHARS` before any model sees it.
        elements = self.chunks.source_elements_for_chunking(job["source_id"])
        sections = command_sections(elements)
        del elements  # sections own clipped copies; drop the full-source list
        # The section total is only knowable after sectioning, so it lands in a
        # second write. `set_section_total` is `running`-scoped, so a cancel
        # that arrived during the read simply leaves it at 0 and the loop's
        # first cancellation check ends the run.
        self.catalog.set_section_total(job["id"], len(sections))
        client = self._client()
        outcomes: list[SectionOutcome] = []
        position = 0
        total_calls = 0
        total_slices = 0
        total_slice_failures = 0
        for section in sections:
            self._raise_if_stopped(job["id"], cancel)
            work = self._process_section(client, job, section, position, cancel)
            position += len(work.rows)
            total_calls += work.calls
            total_slices += work.slices
            total_slice_failures += work.slice_failures
            if work.rows:
                self.catalog.add_candidates(work.rows)
            outcome = section_outcome(
                section,
                work.candidates,
                work.results,
                uncovered_args=work.uncovered_args,
            )
            outcomes.append(outcome)
            accepted = len(outcome.accepted_names)
            rejected = outcome.command_rejects + work.slice_failures
            self.catalog.record_section(
                job["id"],
                entries=accepted,
                rejected=rejected,
                uncovered=len(outcome.uncovered_candidates),
                truncated=1 if outcome.truncation.applied else 0,
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
        stats = catalog_stats(outcomes)
        return {
            "sections": stats.sections,
            "entries_seen": stats.entries_seen,
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

    def _check_circuit(
        self,
        outcomes: Sequence[SectionOutcome],
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
        its tenth clean section. (Flagless no longer implies an empty
        denominator: since `_prompt`'s no-flag branch asks for positional
        arguments, such a section contributes ``args_seen`` whenever the model
        answers one, and the axis then judges those names by grounding like any
        other. The guard still matters for the sections that really do take no
        arguments at all.) Reading ``args_seen`` alone would NOT do: a
        model that answers nothing at all for 20 assigned parameters leaves
        ``args_seen`` at 0 and would sail past this axis while its assignment
        vanished section after section.

        The third axis reads slices that produced NO parseable answer at all,
        and it exists because the other two cannot name that failure even now
        that the args axis counts unanswered assignments: a slice that never
        returns anything contributes no entry, so `entries_seen` stays 0 and
        the name ratio stays innocuous, while the args ratio it does drag down
        reports the SYMPTOM ("parameters went missing") instead of the CAUSE
        ("the endpoint is returning garbage"). Without it, a deployment pointed
        at an incompatible model endpoint runs the entire manual — every
        section, every halving retry — and reports `succeeded` with an empty
        catalog. That is precisely the silent-empty outcome this breaker exists
        to prevent, so it must be a breaker axis rather than a per-section note.

        Which one is REPORTED when several fire is a diagnosis question, not a
        severity one, and the order below is most-specific-first: an unusable
        response explains a bad args ratio, never the other way round. (Before
        the args axis counted unanswered assignments the two could not overlap
        at all — a slice that answers nothing produced no `args_seen` either —
        so the old order never had to choose.)
        """
        stats = catalog_stats(outcomes)
        if stats.sections < MIN_SECTIONS_BEFORE_ALERT:
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
                    "sections": stats.sections,
                    "entries_seen": stats.entries_seen,
                    "command_rejects": stats.command_rejects,
                    "command_reject_ratio": stats.command_reject_ratio,
                    "args_seen": stats.args_seen,
                    "args_kept": stats.args_kept,
                    "args_uncovered": stats.args_uncovered,
                    "args_keep_ratio": stats.args_keep_ratio,
                    "samples": [
                        {
                            "section_path": outcome.section_path,
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

    # ------------------------------------------------------------- one section
    def _process_section(
        self,
        client: Any,
        job: Mapping[str, Any],
        section: CommandSection,
        position: int,
        cancel: CancelEvent,
    ) -> _SectionWork:
        candidates = command_candidates(section)
        work = _SectionWork(candidates=tuple(candidates))
        merged: dict[str, dict] = {}
        anchors = list(section.element_ids)[:MAX_ANCHOR_ELEMENTS]
        excerpt = _clip(section.text, CANDIDATE_EXCERPT_CHARS)
        next_position = position
        for extraction in extraction_slices(section):
            self._raise_if_stopped(job["id"], cancel)
            work.slices += 1
            payloads, calls, failed = self._extract_slice(
                client, section, candidates, extraction, cancel, job["id"]
            )
            work.calls += calls
            if failed:
                work.slice_failures += 1
                next_position += 1
                work.rows.append(
                    self._row(
                        job,
                        position=next_position,
                        section=section,
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
            accepted_here: list[str] = []
            for payload in payloads:
                # `extraction.param_names` — the ORIGINAL slice's assignment,
                # not whichever half a payload came back from. A halving is an
                # internal remedy and its halves partition this same list, so
                # holding one half's answer to the other half's names would
                # reject data the slice was legitimately asked for. Answering
                # a DIFFERENT slice's parameters is still caught: those names
                # are not in this list either.
                result = validate_entry(
                    payload, section, candidates, assigned=extraction.param_names
                )
                work.results.append(result)
                if result.accepted and result.entry is not None:
                    self._merge_entry(merged, result)
                    accepted_here.append(result.entry.command_name)
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
                        section=section,
                        command_name=claimed,
                        payload={"excerpt": excerpt, "anchors": anchors},
                        state="rejected",
                        reject_info=_reject_info(_rejection_records(result)),
                    )
                )
            # Coverage is a property of the SLICE, so it is settled once here
            # over everything the slice answered — not inside the payload loop,
            # where a halved slice would count each half as having ignored the
            # other's parameters. A slice that failed outright still lands here
            # with an empty payload list, which is exactly right: its whole
            # assignment went unanswered and the ledger must say so.
            uncovered = assignment_coverage(
                payloads, extraction.param_names
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
                    for name in uncovered[:MAX_SECTION_REJECTIONS]
                ]
                for name in dict.fromkeys(accepted_here):
                    _extend_rejections(merged[name], records)
        for name, entry in merged.items():
            next_position += 1
            work.rows.append(
                self._row(
                    job,
                    position=next_position,
                    section=section,
                    command_name=name,
                    payload={
                        "syntax": entry["syntax"],
                        "description": entry["description"],
                        "args": entry["args"],
                        "examples": entry["examples"],
                        "anchors": anchors,
                        "excerpt": excerpt,
                        "suspect_related": entry["suspect_related"],
                    },
                    state="candidate",
                    reject_info=_reject_info(
                        entry["rejections"],
                        entry["rejections_overflow"],
                        desc_overflow=entry["desc_overflow"],
                    ),
                )
            )
        return work

    @staticmethod
    def _merge_entry(
        merged: dict[str, dict],
        result: ValidationResult,
    ) -> None:
        """Fold one slice's accepted entry into that command's single row.

        A multi-slice command produces one accepted entry per slice, all naming
        the same command; the catalog holds ONE row per command, so the args
        union and the overview fields (only slice 0 was asked for them) merge
        here. Args dedupe by name, first writer wins. Slices partition the
        parameter list, so across slices a duplicate means the model answered
        outside its assignment; WITHIN one slice it is the ordinary case, since
        a halved slice re-asks for parameters its first answer may already have
        covered (see `_extract_slice`).

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
        current = merged.setdefault(
            entry.command_name,
            {
                "syntax": "",
                "description": "",
                "args": [],
                "examples": [],
                "suspect_related": entry.suspect_related,
                "rejections": [],
                "rejections_overflow": 0,
                "desc_chars": 0,
                "desc_overflow": 0,
            },
        )
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
        current["suspect_related"] = current["suspect_related"] and entry.suspect_related
        _extend_rejections(current, _rejection_records(result))

    @staticmethod
    def _row(
        job: Mapping[str, Any],
        *,
        position: int,
        section: CommandSection,
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
            "section_path": section.section_path or section.title,
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
        section: CommandSection,
        candidates: Sequence[str],
        extraction: ExtractionSlice,
        cancel: CancelEvent,
        job_id: str,
        depth: int = 0,
    ) -> tuple[list[Mapping[str, Any]], int, bool]:
        """One slice's payloads, the calls it cost, and whether it failed.

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
        (``MAX_SLICE_WINDOW_CHARS``) and the failure being treated is on the
        OUTPUT side, so narrowing the input would trade a real context loss for
        no gain.

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
        outcome = self._call(client, section, candidates, extraction, cancel, job_id)
        calls = 1
        if outcome.payload is not None:
            payloads = [outcome.payload]
            if depth == 0:
                coverage = assignment_coverage(payloads, extraction.param_names)
                if _coverage_retry_warranted(coverage):
                    recovered, extra_calls = self._halve_and_ask(
                        client, section, candidates, extraction, cancel, job_id, depth
                    )
                    payloads.extend(recovered)
                    calls += extra_calls
            return payloads, calls, False
        if (
            outcome.kind == "malformed"
            and depth < MAX_SLICE_SPLIT_DEPTH
            and len(extraction.param_names) > 1
        ):
            payloads, half_calls = self._halve_and_ask(
                client, section, candidates, extraction, cancel, job_id, depth
            )
            calls += half_calls
            # NOT the OR of each half's own `failed` — see the docstring: one
            # successful half means this slice, as a whole, produced usable
            # output.
            return payloads, calls, not payloads
        retry = self._call(client, section, candidates, extraction, cancel, job_id)
        calls += 1
        if retry.payload is not None:
            return [retry.payload], calls, False
        return [], calls, True

    def _halve_and_ask(
        self,
        client: Any,
        section: CommandSection,
        candidates: Sequence[str],
        extraction: ExtractionSlice,
        cancel: CancelEvent,
        job_id: str,
        depth: int,
    ) -> tuple[list[Mapping[str, Any]], int]:
        """Split this slice's assignment in two and ask for each half.

        Shared by both callers so the split — and the fact that only the FIRST
        half inherits the overview responsibility — has one definition. The
        halves reuse the parent's ``text_window`` unchanged (see the caller's
        docstring: the failure being treated is on the output side).
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
        payloads: list[Mapping[str, Any]] = []
        calls = 0
        for half in halves:
            half_payloads, half_calls, _half_failed = self._extract_slice(
                client, section, candidates, half, cancel, job_id, depth + 1
            )
            payloads.extend(half_payloads)
            calls += half_calls
        return payloads, calls

    def _call(
        self,
        client: Any,
        section: CommandSection,
        candidates: Sequence[str],
        extraction: ExtractionSlice,
        cancel: CancelEvent,
        job_id: str,
    ) -> _SliceOutcome:
        # The liveness probe here is what makes the guarantee actually cover
        # "every model call": `_extract_slice`'s halving and coverage-retry
        # branches call `_call` directly, recursing through `_halve_and_ask`
        # without ever returning to `_process_section`'s per-slice loop — so
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
                section, candidates, extraction
            )
            kwargs["cancel_event"] = cancel
        try:
            raw = client.chat_json(
                [{"role": "user", "content": _prompt(section, candidates, extraction)}],
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
            # outage into "this section had no commands" is the silent-empty
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
        notebook's table, so a title round-trip through THIS notebook
        (``knowhow_table_id_by_title(notebook_id, current_title) ==
        candidate_table_id``) is required, not assumed. Either failure falls
        through identically to the fast path's own dangling reference: create
        or find by the derived title, never guess.

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
        both inside the same ``try``: ``knowhow_table_columns`` answers
        existence (and is what the caller actually needs, so fetching it here
        avoids a second read on the fast path this feeds), and
        ``knowhow_table_title`` supplies the CURRENT title membership is
        checked against. A ``KeyError`` from either — the table is gone, full
        stop — is treated as nothing to inherit; there is no partial state
        worth distinguishing (a table missing its title but not its columns,
        or vice versa, is not a shape SQLite or PostgreSQL can produce).

        Membership: ``knowhow_table_id_by_title(notebook_id, title)`` must
        resolve back to the SAME candidate id. A title that resolves to
        NOTHING, or to a DIFFERENT id (the table moved to another notebook, or
        this notebook happens to have an unrelated table sharing that exact
        title), is treated identically — not "ours", fall through and let
        create-or-find decide. This never guesses: an id this method returns
        is one the caller can prove is both alive and local.
        """
        candidate_table_id = self.catalog.latest_applied_table_id(
            str(job.get("source_id") or "")
        )
        if not candidate_table_id:
            return None
        try:
            columns = self.knowhow.knowhow_table_columns(candidate_table_id)
            title = self.knowhow.knowhow_table_title(candidate_table_id)
        except KeyError:
            return None
        if self.knowhow.knowhow_table_id_by_title(notebook_id, title) != candidate_table_id:
            return None
        return candidate_table_id, columns, title


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


def _entry_validator(
    section: CommandSection,
    candidates: Sequence[str],
    extraction: ExtractionSlice,
) -> Callable[[str], bool]:
    """Cache-admission gate judged by running the DOWNSTREAM grounding itself.

    Same reasoning as the KG extractors' own gates: "is this reply usable" IS
    the consumption logic, and every shape approximation of it leaks. A reply
    that parses but names a command this section does not document, or that
    invents every parameter it was asked for, produces nothing downstream —
    freezing that nothing for the cache TTL is the poisoning case this closes.

    The second clause is the args axis: when the slice asked for specific
    parameters, at least one OF THOSE has to survive grounding. "Of those" is
    load-bearing and is why the assignment is handed to `validate_entry` here
    too: a reply that answers a DIFFERENT slice's parameters grounds perfectly
    against the section text, so without attribution it would clear this gate
    and be frozen into the content-addressed cache for the full TTL — served
    back on every later hit of a prompt it never actually answered.
    `validate_entry` rejects those as `arg_outside_slice`, which leaves
    `args_kept` at 0 and closes the gate. A slice with no parameters (a
    flagless command, or a later slice whose whole assignment was rejected
    upstream) is exempt, because zero kept args is then the correct answer
    rather than a failure. That exemption still holds now that the no-flag
    branch of `_prompt` asks for positional arguments: plenty of flagless
    commands (`report_dont_use`) genuinely take none, and an empty `args` from
    one of those is a correct, cacheable answer — the invented-argument case is
    caught where it always was, by grounding, not by counting.

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
    """

    def validator(content: str) -> bool:
        payload = safe_json(content)
        if not payload or "error" in payload:
            return False
        raw_args = payload.get("args")
        if raw_args is not None and not isinstance(raw_args, list):
            return False
        result = validate_entry(
            payload, section, candidates, assigned=extraction.param_names
        )
        if not result.accepted:
            return False
        if extraction.param_names and result.stats.args_kept < 1:
            return False
        return True

    return validator


def _prompt(
    section: CommandSection,
    candidates: Sequence[str],
    extraction: ExtractionSlice,
) -> str:
    """The one prompt this feature has.

    The served candidate list is a constraint, not a menu: C0 measured 5/5
    command-name accuracy with a list, and `validate_entry` vetoes any name off
    it. The parameter list is the slice's assignment, spelled with its original
    leading dash because dropping that dash is the single most common
    infidelity C0 saw — and one a naive containment check would not catch,
    since the manual's own text does contain the bare word.

    The two parameter branches are NOT "ask" and "do not ask". An empty
    assignment only means `parameter_names` found no flag-shaped name in the
    section, and `parameter_names` is a FLAG scanner — a command documented as
    `set_dont_use lib_cells` has a real parameter that regex can never produce.
    Ordering the model to return `args: []` there (as this prompt used to) threw
    away the argument metadata of an entire command class that every layer
    downstream already supports: `_usage_identifier` accepts the positional form
    as command evidence, and `validate_entry`'s dash test is deliberately
    evidence-based so a bare `lib_cells` grounds. So the no-flag branch asks for
    positional arguments instead — with no list to copy from, since there is
    none to serve; grounding, not an assignment, is what keeps that honest
    (`_check_arg_name` still requires the name verbatim in the section text).

    What that does to the ledger, in one place because three readers care:
    `args_seen`/`args_kept` count these answers like any other (the keep ratio
    stays "of what came back, how much was really in the manual"), while
    `assignment_coverage` contributes nothing — with no assignment there is
    nothing that can go unanswered. So a flagless section can now make the
    breaker's args axis live, and what it measures there is exactly right: a
    positional argument copied from the usage line keeps the ratio at 1.0, and
    a run inventing them section after section is a run worth stopping.
    """
    candidate_block = "\n".join(f"- {name}" for name in candidates) or "- (none)"
    if extraction.param_names:
        params = "\n".join(f"- {name}" for name in extraction.param_names)
        params_block = (
            "\nExtract ONLY these parameters, and nothing else:\n"
            f"{params}\n"
            "Copy each name character for character, including any leading dash.\n"
        )
    else:
        params_block = (
            "\nThis section documents no flag-shaped parameters (`-name`), so "
            "there is no assigned list. The command may still take POSITIONAL "
            "arguments: return those, taking each name from the usage line that "
            "invokes the command — `set_dont_use lib_cells` takes one argument, "
            "named `lib_cells`. Copy each name character for character from the "
            "source text below, and return `args`: [] when the command really "
            "takes none.\n"
        )
    if extraction.include_overview:
        overview_block = (
            "Also return `syntax` (a contiguous copy of one usage line from the "
            "source text), `description` (one or two sentences), and `examples` "
            "(verbatim example invocations, at most three).\n"
        )
    else:
        overview_block = (
            "Return `syntax`, `description` and `examples` as empty — another "
            "call already covers them. Only `command_name` and `args` matter here.\n"
        )
    return f"""Catalogue ONE command from this command-reference section.

Choose the command name from this list, copied character for character:
{candidate_block}
{params_block}{overview_block}
Rules:
- Never invent. Every `name`, every `default` and the `syntax` line must appear
  in the source text below exactly as written; if you cannot find it, leave the
  field empty rather than guessing.
- Do not translate, expand or tidy any identifier.
- `required` is true only when the source text says the parameter is required.

Return JSON only, matching: {_SCHEMA_HINT}

Source text (section: {section.section_path or section.title}):
<<<
{extraction.text_window or section.text}
>>>
"""


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
    provenance = " · ".join(part for part in (source_title, section_path) if part)
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
    "PARSED_SOURCE_STATUSES",
    "SLICE_COVERAGE_RETRY_RATIO",
    "SLICE_FAILURE_ALERT_RATIO",
    "SOURCE_BUSY_MESSAGE",
    "SOURCE_LOCK_WAIT_SECONDS",
    "SOURCE_NOT_PARSED_MESSAGE",
    "SOURCE_PARSE_FAILED_MESSAGE",
    "SOURCE_REPARSED_REASON",
    "SOURCE_STALE_MESSAGE",
    "SUBMISSION_FAILED_MESSAGE",
    "USER_DISMISSED_REASON",
    "pending_candidates_message",
]
