"""Agentic Memory P1 (T4): the SHARED-BASE consolidation chain.

Design doc §5.3, first chain. Source additions/reparses/deletions bump a
deterministic counter; once it reaches the deployment's threshold, ONE bounded
model call refreshes the notebook's three shared-base blocks (``corpus_shape`` /
``key_entities`` / ``corpus_gaps``).

⚠ **The isolation is structural, not a prompt rule** (design §5.3 / §12-Q2, and
the acceptance criterion of this task). The base chain reads exactly three
things — the current base blocks, corpus statistics, and KG object aggregates —
and every one of them is notebook-level data that every member of a shared
notebook can already see. No read here can reach ``ask_jobs``, ``ask_trace_
steps``, ``answers``, ``memory_items``, ``conversations`` or ``reports``: the
shared base cannot leak one member's usage to another because it never has it.
``backend/tests/test_agent_profile_isolation_guard.py`` pins that statically —
a promise a reviewer has to re-check by hand is a promise that erodes.

The per-(notebook, member) OVERLAY chain, which DOES read one member's own
trace (under a ``WHERE user_id = ?`` predicate written into the reading SQL),
is T5 and lands in this module beside this one.

Terminal-state discipline is the ``kg_build_jobs`` / ``catalog_jobs`` protocol,
for the same reason: the chain's single-flight slot is a durable row, so a run
that exits without settling holds that notebook's slot until the next process
restart — and ``KeyboardInterrupt``/``SystemExit`` inherit ``BaseException``
and sail straight past ``except Exception``.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from app.core.llm import cap_kwargs
from app.repositories.ports import (
    AGENT_PROFILE_INTERNAL_FAILURE_MESSAGE,
    AGENT_PROFILE_INTERRUPTED_MESSAGE,
    AGENT_PROFILE_MALFORMED_MESSAGE,
    AGENT_PROFILE_MODEL_UNAVAILABLE_MESSAGE,
    AGENT_PROFILE_SUBMISSION_FAILED_MESSAGE,
    AgentProfileRevisionConflict,
    AgentProfileStorePort,
    QueryStorePort,
    RepositoryDatabasePort,
    SourceStorePort,
)
from app.services import background_jobs
from app.services.agent_profile_block import AGENT_PROFILE_VALUE_MAX_CHARS
from app.services.collection_catalog import ENUMERABLE_ELEMENT_KINDS
from app.services.kg.json_utils import safe_json
from app.services.knowledge_contracts import USABLE_STATUSES
from app.services.prompts import (
    AGENT_PROFILE_SCHEMA_HINT,
    agent_profile_base_prompt,
)
from app.services.reasoning_retrieval import profile_wiring_active

_log = logging.getLogger("silicon_notebook.agent_profile")

#: The model channel. Registered in ``model_registry`` as a background chat
#: workload rather than reusing an existing one: this call has its own shape
#: (aggregate statistics in, a handful of prose blocks out) and a deployment
#: must be able to point it somewhere cheap without dragging KG extraction
#: along with it.
AGENT_PROFILE_WORKLOAD = "agent_profile_consolidate"

#: ``owner_id`` of the shared base chain. ``''`` is the sentinel the schema
#: uses (see ``_migration_50``) — not NULL, so the primary key actually
#: constrains it.
BASE_CHAIN_OWNER = ""

#: The three blocks this chain owns. ``retrieval_notes``/``usage_gaps`` belong
#: to the overlay chain (T5) and must never be written from here: they are
#: derived from one member's usage, which this chain structurally cannot read.
BASE_LABELS: tuple[str, ...] = ("corpus_shape", "key_entities", "corpus_gaps")

_JOB_NAME_PREFIX = "agentprofile-"

#: How many per-document lines the statistics block may carry. The block is a
#: prompt input on a bounded budget, and a 3 000-document library would
#: otherwise render 3 000 lines of opaque ids. Documents are ordered by how
#: much extractable content they hold, so the ones a shape/gap statement is
#: actually about come first; the count of what was left out is disclosed on
#: the line itself, so the model can never read a clipped list as the whole
#: library.
AGENT_PROFILE_STATS_MAX_DOCUMENTS = 40

#: Evidence ids kept per block. The evidence column exists so a claim can be
#: traced back to the documents behind it (design §5.1); a list longer than
#: this is not traceability, it is the model copying the roster back.
AGENT_PROFILE_EVIDENCE_MAX_IDS = 8


class AgentProfileModelUnavailable(RuntimeError):
    """No chat service is bound to ``agent_profile_consolidate``."""


class AgentProfileOutputRejected(RuntimeError):
    """The model's reply could not be used, so the previous blocks stand.

    Deliberately terminal — there is no retry. A malformed reply costs a call;
    retrying it costs two, and the fail-open outcome (keep the blocks that are
    already there) is already correct. ``diagnostic`` is an internal stable
    token, never shown to a user and never carrying model text.
    """

    def __init__(self, diagnostic: str) -> None:
        super().__init__(diagnostic)
        self.diagnostic = diagnostic


@dataclass(frozen=True)
class CorpusStats:
    """Everything the base prompt is allowed to know about the library.

    Every field here comes from one of the three permitted reads. There is
    deliberately no free-text field (no titles, no summaries, no snippets):
    this chain's inputs are aggregates, and the prompt tells the model to omit
    any block the aggregates cannot support rather than invent one.
    """

    documents: int
    #: ``[(source_id, {kind: count})]``, richest first, already clipped to
    #: ``AGENT_PROFILE_STATS_MAX_DOCUMENTS``.
    per_document: tuple[tuple[str, Mapping[str, int]], ...]
    #: Visible documents that yielded none of the listed element kinds — the
    #: single most useful ``corpus_gaps`` signal available from aggregates.
    documents_without_elements: int
    element_totals: Mapping[str, int]
    element_document_counts: Mapping[str, int]
    kg_objects: tuple[tuple[str, int], ...]
    #: The ids the prompt actually served, i.e. the only ids an evidence list
    #: may legally contain.
    served_ids: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class _BaseOutcome:
    written: int
    chars: int
    evidence: int
    diagnostic: str


def _clip_value(value: str) -> str:
    """One line, capped at the per-block budget.

    The renderer collapses whitespace on the way OUT
    (``agent_profile_block._clean``) so a stored multi-line value cannot forge
    prompt structure; this collapses it on the way IN as well, because the
    same value is also shown in the panel and edited by users there, and a
    block whose stored form differs from every form anyone sees is a block
    nobody can reason about.
    """
    text = " ".join(str(value or "").split())
    if len(text) > AGENT_PROFILE_VALUE_MAX_CHARS:
        return text[: AGENT_PROFILE_VALUE_MAX_CHARS - 1] + "…"
    return text


def render_corpus_block(stats: CorpusStats) -> str:
    """The statistics half of the prompt.

    English scaffolding for the same reason as the collection map and the
    understanding block itself: it is prompt structure sitting next to other
    English instructions, not user-facing copy.
    """
    lines = ["[Corpus statistics]", f"documents: {stats.documents}"]
    elements = ", ".join(
        f"{kind} {stats.element_totals.get(kind, 0)}"
        + (
            f" (in {stats.element_document_counts.get(kind, 0)} documents)"
            if stats.element_totals.get(kind, 0)
            else ""
        )
        for kind in ENUMERABLE_ELEMENT_KINDS
    )
    lines.append(f"elements by kind: {elements}")
    lines.append(
        "documents with none of those element kinds: "
        f"{stats.documents_without_elements}"
    )
    kg_objects = ", ".join(
        f"{object_type} {count}" for object_type, count in stats.kg_objects
    ) or "none"
    lines.append(f"extracted knowledge objects: {kg_objects}")
    if stats.per_document:
        hidden = max(0, stats.documents - len(stats.per_document))
        suffix = f" (+{hidden} more documents not listed)" if hidden else ""
        lines.append(f"per document, richest first{suffix}:")
        for source_id, counts in stats.per_document:
            detail = ", ".join(
                f"{kind} {counts[kind]}"
                for kind in ENUMERABLE_ELEMENT_KINDS
                if counts.get(kind)
            )
            lines.append(f"- {source_id}: {detail or 'no listed elements'}")
    return "\n".join(lines)


def render_current_blocks(blocks: Sequence[Mapping[str, Any]]) -> str:
    """The "what you already believe" half of the prompt.

    ``(user-authored)`` is the load-bearing marker: design §5.4 makes a
    human-edited block authoritative input rather than a draft to be replaced,
    and it is also the cold-start channel (a user can simply TELL the agent
    what this library is). Without the marker the model cannot tell its own
    previous guess apart from a person's correction of it.
    """
    lines = ["[Current understanding]"]
    by_label = {
        str(block.get("label") or ""): block
        for block in blocks or ()
        if str(block.get("owner_id") or "") == BASE_CHAIN_OWNER
    }
    for label in BASE_LABELS:
        block = by_label.get(label)
        value = _clip_value(block.get("value") if block else "")
        if not value:
            lines.append(f"- {label}: (empty)")
            continue
        authored = str((block or {}).get("updated_origin") or "") == "user"
        marker = " (user-authored)" if authored else ""
        lines.append(f"- {label}{marker}: {value}")
    return "\n".join(lines)


def parse_base_reply(payload: object, served_ids: frozenset[str]) -> list[dict]:
    """Validate one reply into the blocks that may be written.

    Whole-payload rejection (rather than per-block salvage) for anything
    STRUCTURAL — not a JSON object, no ``blocks`` list, a non-object entry, an
    unknown label. A reply that invents a label is a reply that did not answer
    the question that was asked, and the fail-open outcome (keep the existing
    blocks) is strictly safer than writing the half of it that happened to
    parse. Overlay labels are rejected here too, by construction: they are not
    in ``BASE_LABELS``, and this chain has read nothing that could support
    them.

    Per-entry salvage applies to exactly one thing: evidence ids the
    statistics never served are dropped. Those are a citation error, not a
    structural one — the claim itself may still be sound, and dropping the
    whole refresh over one hallucinated id would trade a real improvement for
    a bookkeeping detail. The count of what was dropped rides out in the
    diagnostic.
    """
    if not isinstance(payload, Mapping):
        raise AgentProfileOutputRejected("reply_not_an_object")
    raw_blocks = payload.get("blocks")
    if not isinstance(raw_blocks, list):
        raise AgentProfileOutputRejected("blocks_not_a_list")
    parsed: list[dict] = []
    seen: set[str] = set()
    for entry in raw_blocks:
        if not isinstance(entry, Mapping):
            raise AgentProfileOutputRejected("block_not_an_object")
        label = str(entry.get("label") or "").strip()
        if label not in BASE_LABELS:
            raise AgentProfileOutputRejected("unknown_label")
        if label in seen:
            raise AgentProfileOutputRejected("duplicate_label")
        seen.add(label)
        value = _clip_value(entry.get("value"))
        if not value:
            # An empty value means "I have nothing to say about this block",
            # which is the prompt's own instruction to OMIT it. It must not
            # clear an existing block: clearing is a user action (the panel's
            # own control), never a side effect of a quiet consolidation run.
            continue
        raw_evidence = entry.get("evidence")
        evidence = [
            source_id
            for source_id in (raw_evidence if isinstance(raw_evidence, list) else [])
            if isinstance(source_id, str) and source_id in served_ids
        ]
        dropped = (
            len(raw_evidence) - len(evidence)
            if isinstance(raw_evidence, list)
            else 0
        )
        parsed.append(
            {
                "label": label,
                "value": value,
                "evidence": evidence[:AGENT_PROFILE_EVIDENCE_MAX_IDS],
                "evidence_dropped": max(0, dropped),
            }
        )
    return parsed


class AgentProfileConsolidationService:
    """Threshold gate, single-flight claim, one bounded call, terminal settle.

    Backend-neutral by construction (ports and plain callables only), so it
    lives on the neutral repository runtime rather than being built twice per
    backend — same rationale as ``CommandCatalogService`` next to it.
    """

    def __init__(
        self,
        *,
        settings: Any,
        profiles: AgentProfileStorePort,
        database: RepositoryDatabasePort,
        sources: SourceStorePort,
        queries: QueryStorePort,
        models: Any,
        event_log: Any,
    ) -> None:
        self.settings = settings
        self.profiles = profiles
        self.database = database
        self.sources = sources
        self.queries = queries
        self.models = models
        self.event_log = event_log

    # ------------------------------------------------------------- triggering
    def note_corpus_change(self, notebook_id: str) -> None:
        """One source-lifecycle event happened in this notebook.

        ⚠ FAIL-OPEN IN FULL. This hangs off the ingestion pipeline: an upload
        that succeeded must not be reported as failed because a background
        understanding refresh could not be scheduled. Every ordinary exception
        is logged and swallowed here; ``KeyboardInterrupt``/``SystemExit`` are
        not "errors" and keep propagating.

        The gate itself costs ONE primary-key upsert and no model call —
        that is the whole point of keeping the counter in the durable job row
        rather than deciding "is it time yet?" with a model.
        """
        try:
            if not profile_wiring_active(self.settings, self.profiles):
                return
            pending = self.profiles.bump_signal(notebook_id, BASE_CHAIN_OWNER)
            # Read straight off Settings, with no local fallback default: a
            # second spelling of "5" here would be the number that silently
            # wins whenever the real one moves.
            if pending < int(self.settings.agent_profile_base_trigger):
                return
            self.start_base(notebook_id)
        except Exception:  # noqa: BLE001 — never break the ingestion pipeline
            _log.exception(
                "agent profile corpus-change notification failed for notebook %s",
                notebook_id,
            )

    def start_base(self, notebook_id: str) -> bool:
        """Claim the chain's slot and submit the worker; ``False`` = busy.

        The claim happens HERE, before the thread exists, exactly like
        ``catalog_job``'s row-before-worker order: a claim taken inside the
        worker leaves a window in which a second trigger schedules a second
        writer for the same blocks. The price is that a submit failure would
        strand the claim, so it is settled on the spot.

        Shared with T6's manual "rebuild now" button, which is the same two
        steps without the threshold gate.
        """
        claimed = self.profiles.claim(notebook_id, BASE_CHAIN_OWNER)
        if claimed is None:
            return False
        try:
            background_jobs.submit(
                self.run_base,
                notebook_id,
                int(claimed),
                name=f"{_JOB_NAME_PREFIX}{notebook_id}",
                # Not a pending-actions item: nothing here waits for a human
                # decision, so ringing the bell would train users to ignore it.
                notify_pending=False,
            )
        except BaseException:
            # The row is claimed but no thread will ever run it. Without this
            # the chain's slot is held until the next restart's sweep — and
            # every later trigger silently no-ops against it.
            self._safe_settle(
                notebook_id,
                "failed",
                failure_reason=AGENT_PROFILE_SUBMISSION_FAILED_MESSAGE,
                diagnostic="job_submission_failed",
                consumed=0,
            )
            self._emit("failed", notebook_id, latency_ms=0)
            raise
        return True

    def sweep_on_start(self) -> int:
        """Startup crash recovery for both chains. Returns the row count swept.

        Deliberately NOT gated on ``profile_wiring_active``: a deployment that
        turned the feature off after a crash would otherwise keep rows stuck in
        ``running`` forever, and turning it back on would find every notebook
        permanently "busy". Cleaning up after a previous process is not a
        feature, it is hygiene.
        """
        try:
            return int(self.profiles.sweep_stale_on_start() or 0)
        except Exception:  # noqa: BLE001 — startup must never fail on this
            _log.exception("agent profile startup sweep failed")
            return 0

    # --------------------------------------------------------------- the run
    def run_base(self, notebook_id: str, claimed_signal: int = 0) -> dict:
        """Execute one shared-base consolidation to a terminal state.

        ``claimed_signal`` is the ``pending_signal`` snapshot ``claim``
        returned: on success it is exactly what this run consumed, so signals
        that arrived WHILE it ran survive to trigger the next round. A failed
        run consumes nothing — its triggering changes still count toward the
        retry.

        Every exit path settles. ``KeyboardInterrupt``/``SystemExit`` get their
        own clause because ``except Exception`` cannot see them, and a row left
        ``running`` holds this notebook's chain until the next restart.
        """
        started = time.perf_counter()

        def latency_ms() -> int:
            return round((time.perf_counter() - started) * 1000)

        try:
            outcome = self._consolidate_base(notebook_id)
        except AgentProfileModelUnavailable:
            return self._fail(
                notebook_id,
                AGENT_PROFILE_MODEL_UNAVAILABLE_MESSAGE,
                "model_unconfigured",
                latency_ms(),
            )
        except AgentProfileOutputRejected as exc:
            # Fail-open: the blocks that were already there stand untouched.
            return self._fail(
                notebook_id,
                AGENT_PROFILE_MALFORMED_MESSAGE,
                exc.diagnostic,
                latency_ms(),
            )
        except (KeyboardInterrupt, SystemExit):
            self._safe_settle(
                notebook_id,
                "failed",
                failure_reason=AGENT_PROFILE_INTERRUPTED_MESSAGE,
                diagnostic="worker_interrupted",
                consumed=0,
            )
            self._emit("failed", notebook_id, latency_ms=latency_ms())
            raise
        except Exception:
            self._fail(
                notebook_id,
                AGENT_PROFILE_INTERNAL_FAILURE_MESSAGE,
                "internal_error",
                latency_ms(),
            )
            raise
        self._safe_settle(
            notebook_id,
            "done",
            diagnostic=outcome.diagnostic,
            blocks_written=outcome.written,
            consumed=max(0, int(claimed_signal)),
        )
        self._emit(
            "done",
            notebook_id,
            blocks=outcome.written,
            chars=outcome.chars,
            evidence=outcome.evidence,
            latency_ms=latency_ms(),
        )
        return {
            "notebook_id": notebook_id,
            "blocks_written": outcome.written,
            "diagnostic": outcome.diagnostic,
        }

    def _consolidate_base(self, notebook_id: str) -> _BaseOutcome:
        if not self.models.configured(AGENT_PROFILE_WORKLOAD):
            # Checked before the statistics reads: an unconfigured deployment
            # should pay nothing to learn it is unconfigured.
            raise AgentProfileModelUnavailable()
        blocks = self.profiles.read_blocks(notebook_id, BASE_CHAIN_OWNER)
        stats = self.corpus_stats(notebook_id)
        client = self.models.chat(AGENT_PROFILE_WORKLOAD)
        prompt = agent_profile_base_prompt(
            render_corpus_block(stats),
            render_current_blocks(blocks),
            value_max_chars=AGENT_PROFILE_VALUE_MAX_CHARS,
        )
        raw = client.chat_json(
            [{"role": "user", "content": prompt}],
            AGENT_PROFILE_SCHEMA_HINT,
            **cap_kwargs(client, "kg_extract_max_tokens"),
        )
        if not str(raw or "").strip():
            raise AgentProfileOutputRejected("empty_reply")
        data = safe_json(raw)
        if not data:
            # ``safe_json`` flattens "not JSON at all" and "JSON that is not an
            # object" into an empty dict, so this branch — not
            # ``parse_base_reply``'s type check — is what an unparsable reply
            # actually reaches. Kept as two distinct diagnostics because they
            # are two distinct observations about the provider.
            raise AgentProfileOutputRejected("unparsable_reply")
        parsed = parse_base_reply(data, stats.served_ids)
        return self._write_blocks(notebook_id, blocks, parsed)

    def _write_blocks(
        self,
        notebook_id: str,
        current: Sequence[Mapping[str, Any]],
        parsed: Sequence[Mapping[str, Any]],
    ) -> _BaseOutcome:
        by_label = {
            str(block.get("label") or ""): block
            for block in current
            if str(block.get("owner_id") or "") == BASE_CHAIN_OWNER
        }
        written = 0
        chars = 0
        evidence_ids = 0
        conflicts: list[str] = []
        dropped = 0
        for block in parsed:
            label = str(block["label"])
            existing = by_label.get(label)
            expected = int(existing["revision"]) if existing else 0
            evidence = list(block["evidence"])
            dropped += int(block.get("evidence_dropped") or 0)
            try:
                self.profiles.write_block(
                    notebook_id,
                    BASE_CHAIN_OWNER,
                    label,
                    value=str(block["value"]),
                    # One entry, ``claim_index`` 0: the base prompt asks for
                    # block-level evidence rather than per-sentence evidence,
                    # so there is exactly one claim to index. The column's
                    # shape stays the documented one so a future per-claim
                    # prompt needs no migration.
                    evidence=[{"claim_index": 0, "source_ids": evidence}],
                    expected_revision=expected,
                    origin="job",
                    actor="",
                )
            except AgentProfileRevisionConflict:
                # A person edited this block while the run was in flight.
                # Their edit wins and this block is skipped — NOT retried: a
                # retry would re-apply a value computed before their edit, i.e.
                # overwrite it with a slower race. The next run starts from
                # their text (which it will see marked user-authored).
                conflicts.append(label)
                continue
            written += 1
            chars += len(str(block["value"]))
            evidence_ids += len(evidence)
        diagnostic_parts: list[str] = []
        if conflicts:
            diagnostic_parts.append("cas_conflict:" + ",".join(sorted(conflicts)))
        if dropped:
            diagnostic_parts.append(f"evidence_dropped:{dropped}")
        return _BaseOutcome(
            written=written,
            chars=chars,
            evidence=evidence_ids,
            # Internal only (labels and counts, never model text) — the store's
            # ``diagnostic`` column is documented as never reaching a screen.
            diagnostic=" ".join(diagnostic_parts),
        )

    # ---------------------------------------------------------------- reading
    def corpus_stats(self, notebook_id: str) -> CorpusStats:
        """The base chain's ENTIRE view of the library.

        Three reads, all of them existing bounded aggregates:

        * ``source_change_signal_rows`` — one query for the whole notebook,
          and it already excludes private Memory synthetic rows. Only rows it
          marks ``user_visible`` are used, so hidden Knowhow/Memory projections
          stay out of the shared base entirely; that also makes the document
          count here mean the same thing the source tab shows.
        * ``element_type_count_rows`` — one grouped, index-covered count per
          batch of those visible ids.
        * ``knowledge_type_count_rows`` — the seq-gated KG type counts.

        Nothing in this method can reach usage data. That is the property
        ``test_agent_profile_isolation_guard.py`` pins, and the reason this
        method takes no query text and no caller-supplied predicate.
        """
        with self.database.connect() as db:
            signals = list(self.sources.source_change_signal_rows(db, notebook_id))
            visible_ids = [
                str(row[0]) for row in signals if bool(row[3])
            ]
            element_rows = (
                list(
                    self.sources.element_type_count_rows(
                        db, visible_ids, ENUMERABLE_ELEMENT_KINDS
                    )
                )
                if visible_ids
                else []
            )
            kg_rows = list(
                self.queries.knowledge_type_count_rows(
                    db, notebook_id, USABLE_STATUSES
                )
            )
        per_source: dict[str, dict[str, int]] = {}
        totals: dict[str, int] = {kind: 0 for kind in ENUMERABLE_ELEMENT_KINDS}
        document_counts: dict[str, int] = {
            kind: 0 for kind in ENUMERABLE_ELEMENT_KINDS
        }
        for source_id, element_type, count in element_rows:
            if element_type not in totals or not count:
                continue
            per_source.setdefault(str(source_id), {})[element_type] = int(count)
            totals[element_type] += int(count)
            document_counts[element_type] += 1
        ranked = sorted(
            per_source.items(),
            key=lambda item: (-sum(item[1].values()), item[0]),
        )[:AGENT_PROFILE_STATS_MAX_DOCUMENTS]
        kg_objects = tuple(
            (str(row["object_type"]), int(row["c"]))
            for row in kg_rows
            if int(row["c"] or 0) > 0
        )
        return CorpusStats(
            documents=len(visible_ids),
            per_document=tuple((sid, dict(counts)) for sid, counts in ranked),
            documents_without_elements=max(0, len(visible_ids) - len(per_source)),
            element_totals=totals,
            element_document_counts=document_counts,
            kg_objects=tuple(sorted(kg_objects, key=lambda item: (-item[1], item[0]))),
            served_ids=frozenset(sid for sid, _counts in ranked),
        )

    # ------------------------------------------------------------ bookkeeping
    def _fail(
        self,
        notebook_id: str,
        failure_reason: str,
        diagnostic: str,
        latency_ms: int,
    ) -> dict:
        self._safe_settle(
            notebook_id,
            "failed",
            failure_reason=failure_reason,
            diagnostic=diagnostic,
            consumed=0,
        )
        self._emit("failed", notebook_id, latency_ms=latency_ms)
        return {"notebook_id": notebook_id, "failed": diagnostic}

    def _safe_settle(self, notebook_id: str, status: str, **kwargs: Any) -> bool:
        """Settle, and never let a settle failure replace the real outcome.

        Mirrors ``catalog_job._settle``: if the write itself fails (or the row
        was cascade-deleted mid-run) the caller's own exception/interrupt must
        still be the thing that propagates, and the row falls back to the
        startup sweep the same way a SIGKILL leftover does.
        """
        try:
            return bool(
                self.profiles.settle(notebook_id, BASE_CHAIN_OWNER, status, **kwargs)
            )
        except Exception:  # noqa: BLE001
            _log.exception(
                "failed to settle agent profile base chain for notebook %s",
                notebook_id,
            )
            return False

    def _emit(
        self, status: str, notebook_id: str, *, chain: str = "base", **extra: Any
    ) -> None:
        """Counts only — never a block value, a document title or model text.

        ``owner_id`` is deliberately absent even though the overlay chain (T5)
        will emit through here too: which MEMBER a consolidation ran for is
        exactly the usage fact this feature's isolation exists to keep out of
        shared channels. ``chain`` is a parameter for that same reason — an
        overlay run reported as ``"base"`` would make the two indistinguishable
        in the only channel that can tell them apart.
        """
        try:
            self.event_log.emit(
                {
                    "kind": "agent_profile_consolidated",
                    "chain": chain,
                    "notebook_id": notebook_id,
                    "status": status,
                    "blocks": 0,
                    "chars": 0,
                    "evidence": 0,
                    **extra,
                }
            )
        except Exception:  # noqa: BLE001 — diagnostics never break a run
            pass
