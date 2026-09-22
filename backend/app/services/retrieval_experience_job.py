"""Agentic Memory P2 (A / T5): the distillation chain for the
retrieval-strategy experience library, PARTITIONED BY NOTEBOOK since SQLite
v79 / PostgreSQL 0059.

Design doc §6.1, and the partitioning design doc
``docs/superpowers/specs/2026-09-22-retrieval-experience-per-notebook-design_zh.md``
§4. TWO chains run through this one worker, parameterised by a partition id:
the deployment-wide chain (partition ``''``, every N completed asks anywhere)
and one chain per notebook (that library's own partition, every N completed
asks in it). Either way ONE bounded model call looks at aggregated statistics
from the most recent runs plus the entries that partition already holds for
similar situations, and decides what — if anything — to record.

⚠ **The privacy is structural, not a prompt rule** — the same sentence the
agent-profile base chain opens with, and it did not weaken when the partition
arrived. The boundary moved by exactly one notch and no further: this chain
now knows WHICH LIBRARY an ask belonged to, and still does not know WHO asked
it. The partition id is a parameter and only ever a parameter — it picks the
rows the read counts and the column the write lands in, and it reaches
``RunObservation`` / ``ObservedRun`` / ``render_observations`` /
``render_existing`` / the prompt exactly never. A partitioned batch's prompt
is byte-identical to a global batch's over the same runs; the privacy guard
pins that at runtime (criterion nine) rather than leaving it to review.

What makes the observed runs safe is unchanged and is still the SHAPE of what
comes back from the one read, ``recent_completed_ask_runs``: every run is
projected to ``RunObservation``, whose reachable fields are ints, bools and
closed ``Literal``s and nothing else, so the model that writes an entry's
``rationale`` has never seen a question, an answer, a document title, a
notebook name or an id. See ``retrieval_experience_projection.py`` — that
module is the boundary, this one is its only consumer.

Because of that, the rule for this file is short and absolute: it may read a
run only through ``project_run``, and it may never reach for the ask/answer
stores itself. A privacy guard scans this module and the projection module
TOGETHER for exactly that reason — moving a forbidden read from one to the
other must not help. That guard's forbidden-name table still contains
``notebook_id``, with ONE narrow exemption for this module: the name may be a
parameter, a read of that parameter, or a keyword argument's name, and
nothing else — ``row["notebook_id"]``, ``.notebook_id`` and the bare string
are violations here exactly as they are in the other two modules.

Terminal-state discipline is simpler than the agent-profile chains': the
single-flight slot is a process-local flag rather than a durable row, so a run
that dies takes its own claim with it and the next trigger proceeds. That is
affordable here precisely because distillation is a pure increment — losing a
batch costs a batch, never correctness — and it is why this feature needs no
job table of its own.

⚠ **Two open concerns, registered rather than fixed (Agentic Memory P2, T6
fix round, item 7)** — neither changes behaviour, both are worth a future
reader knowing were considered:

* **In-process ABA on the injection-side memo — closed, not just registered
  (codex #524 R12 P2).** The injection side (``reasoning_retrieval.py``)
  memoises the rendered block against
  ``RetrievalExperienceStorePort.version_signal()`` — ``(mutation revision,
  row count, MAX(updated_at))``. The two DB-derived halves alone are not a
  content identity: ``updated_at`` is offset-carrying ISO text compared
  lexicographically, so a UTC-offset change or a clock step backwards could
  make a real update invisible, and a batch that evicts as many rows as it
  writes could leave both unchanged. The first element — an in-process
  monotonic revision the store bumps on every ``upsert_experience`` /
  ``evict_to_limit`` — closes the whole class for in-process writes, which is
  exactly the boundary the cache lives at (its key also requires the same
  live store object via weakref). Cross-process writes are still covered
  only by the DB halves; the only cross-process writer is the offline
  ``merge_dbs.py``, which does not run beside a serving deployment.
* **``support`` is a positive-feedback signal by design, not by oversight.**
  An entry with higher ``support`` sorts first among tied-similarity
  candidates on the injection side (``select_experiences``) and survives
  eviction longer (``evict_to_limit`` removes the LOWEST ``(adopted,
  support, updated_at)`` first, WITHIN ONE PARTITION — since schema v79 the
  library is partitioned, entries compete for survival only against the
  entries of the same partition, never against another library's or against
  the global fallback's) — so an entry that has already accumulated
  support is both more likely to be shown again and less likely to be
  evicted before it accumulates more. A newer entry about a genuinely
  useful but less frequently observed shape of question has a structurally
  harder time catching up. This is the same shape of feedback loop most
  "what's popular gets shown, what's shown gets popular" ranking systems
  have, and this design accepts it rather than fights it: the alternative
  (recency-weighted or exploration-biased selection) would need its own
  design pass and is out of scope for P2.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from typing import Any, Mapping, Sequence

from app.core.model_values import as_text
from app.repositories.ports import (
    RETRIEVAL_EXPERIENCE_BATCH_RUNS,
    RETRIEVAL_EXPERIENCE_BATCH_STEPS,
    RETRIEVAL_EXPERIENCE_MAX_ENTRIES,
    RETRIEVAL_EXPERIENCE_NOTEBOOK_MAX_ENTRIES,
    RETRIEVAL_EXPERIENCE_PROVENANCE_MAX,
    RETRIEVAL_EXPERIENCE_RATIONALE_MAX_CHARS,
    AskStateStorePort,
    RetrievalExperienceStorePort,
)
from app.services import background_jobs
from app.services.kg.json_utils import safe_json
from app.services.prompts import (
    RETRIEVAL_EXPERIENCE_SCHEMA_HINT,
    retrieval_experience_prompt,
)
from app.services.retrieval_experience_projection import (
    EXPERIENCE_POLARITIES,
    RETRIEVAL_ACTIONS,
    ObservedRun,
    experience_id,
    project_run,
    situation_similarity,
    validate_situation,
)

_log = logging.getLogger("silicon_notebook.retrieval_experience")

#: The model channel. Its own workload rather than borrowing
#: ``agent_profile_consolidate``: a deployment must be able to point the two
#: somewhere different (this one is deployment-wide and runs far less often),
#: and sharing a workload id would make "turn the experience library's model
#: off" impossible without also turning library-understanding off.
RETRIEVAL_EXPERIENCE_WORKLOAD = "retrieval_experience_distill"

#: Output budget for the one call. Smaller than
#: ``AGENT_PROFILE_MAX_OUTPUT_TOKENS`` and deliberately not borrowed from it:
#: that budget covers three prose blocks written to a per-character cap, while
#: this reply is at most ``_MAX_SITUATIONS_PER_BATCH`` short objects whose only
#: free-text field is capped at ``RETRIEVAL_EXPERIENCE_RATIONALE_MAX_CHARS``.
#: A budget large enough for a reply this schema cannot legally produce buys
#: nothing and pays for a longer timeout on every call.
RETRIEVAL_EXPERIENCE_MAX_OUTPUT_TOKENS = 1024

#: How many distinct SITUATIONS one batch may present to the model, and how
#: many similar existing entries accompany each of them.
#:
#: The situation cap is what keeps a batch's prompt bounded without capping the
#: batch's RUNS: forty runs can span forty different question shapes, and a
#: shape seen once is precisely the shape rule 1 of the prompt tells the model
#: to ignore. Presenting the most frequently observed ones instead spends the
#: prompt on the only shapes that could establish a pattern.
_MAX_SITUATIONS_PER_BATCH = 4
_MAX_SIMILAR_ENTRIES = 3

#: codex #524 R14 P2:一条 ADD 至少要有几个**不同 run** 真用过该动作。prompt
#: 的规则 1 已经写明"一个 run 从不构成模式",但那是对模型的嘱咐不是闸——服务端
#: 不强制,单 run 结论照样落库、照样可注入。UPDATE 命中真实 offered 条目时豁免
#: 到 ≥1(R10 闸已保证):既有条目的历史 support 补齐了模式的另一半,本批一个
#: run 是"新证据",不是"孤证"。被降级成 ADD 的 UPDATE 按 ADD 判。
_MIN_SUPPORTING_RUNS = 2

#: How close an existing entry's situation has to be before it is shown beside
#: a new observation. Below it the entry is about a different shape of
#: question, and including it invites an UPDATE that overwrites a conclusion
#: drawn from evidence this batch never saw.
_SIMILARITY_FLOOR = 0.5

#: The three operations the model may return.
_OPS = frozenset({"ADD", "UPDATE", "NOOP"})

#: An id-shaped run of hex. Every id this repository mints is a prefix plus a
#: full uuid hex, so anything with a long hex run in it is an id — and an id in
#: a rationale means something reached the model that this design says cannot.
#: The entry is DISCARDED rather than scrubbed: a rationale containing an id is
#: evidence that the input narrowing failed, and a scrubbed copy would hide the
#: failure while keeping the entry.
_ID_SHAPE = re.compile(r"[0-9a-fA-F]{16,}")

#: The SECOND id tripwire, added with per-notebook partitioning. The run above
#: needs SIXTEEN hex characters before it fires, and this repository's notebook
#: ids are a ``nb-`` prefix plus a SHORTER hex run (a real one on the author's
#: machine: ``nb-a73f16940c``, ten hex characters) — so the general tripwire
#: would have watched a partition id go past without a word. Either pattern
#: matching DISCARDS the entry, for the same reason the first one does: an id
#: in a rationale is evidence that the input narrowing failed, and this feature
#: gained a new id to narrow away the moment it gained partitions.
#:
#: Defence in depth, not the main guarantee: the model is shown no id at all
#: (the partition never enters the prompt — module docstring, privacy-guard
#: criterion nine), so this pattern should be unreachable, and the day it fires
#: is the day something above it broke.
_NOTEBOOK_ID_SHAPE = re.compile(r"\bnb-[0-9a-f]{6,}")

#: How many NOTEBOOK partitions may wait for the single-flight slot at once.
#: (The global partition never queues — see ``_claim_next_locked``.) Small on
#: purpose: the queue exists so a burst across many libraries does not lose the
#: batches it earned, NOT so a deployment can accumulate hours of backlog — a
#: distillation that runs an hour late is reading a sample of runs its own
#: threshold count no longer describes.
#:
#: ⚠ A full room REFUSES the newcomer; it never evicts a waiter (T2 review
#: round, P1). Dropping from the head is what the first version did, and the
#: head is also where the claim pops from — so under sustained pressure the
#: room churned instead of draining, and every refusal cost an event. Refusing
#: instead costs the newcomer nothing: its pending counter is untouched, so the
#: next re-arm (or its next completed ask) puts it back in line, and the
#: refusals of one pass are reported as ONE aggregated count rather than one
#: event per library.
_MAX_QUEUED_PARTITIONS = 64

#: How long after a partition's batch FINISHED the manual "distil now" button
#: refuses to schedule another one for it — but only while that library has no
#: pending asks at all (see ``distill_now``). Ten minutes: the button exists
#: for design §13-Q4's restart case ("the counters are process-local, so a
#: restart can strand a backlog"), not as a way to bill one bounded model call
#: per click. Without it, every press pays for a batch that re-reads the same
#: asks and, having nothing new to absorb, writes nothing.
_MANUAL_DISTILL_COOLDOWN_SECONDS = 600

#: ``distill_now``'s four outcomes. A string rather than the old ``bool``
#: because three different "no batch was scheduled for you" cases have to
#: become three different sentences on a button: the pre-PR-2 conflation was
#: fine while nothing rendered it, and stopped being fine the moment something
#: did. ``invalid_partition`` is the fifth and is unreachable from the HTTP
#: endpoint (the partition is a path segment), kept because ``distill_now`` is
#: callable from anywhere.
MANUAL_DISTILL_STARTED = "started"
MANUAL_DISTILL_DISABLED = "disabled"
MANUAL_DISTILL_BUSY = "busy"
MANUAL_DISTILL_COOLDOWN = "cooldown"
MANUAL_DISTILL_INVALID = "invalid_partition"


def distillation_wiring_active(settings: Any, store: Any) -> bool:
    """Whether the distillation chain is wired at all (kill switch + store).

    ONE predicate, for the same reason ``profile_wiring_active`` is one: it has
    a second caller (the ask-completion trigger) besides the run itself, and
    two spellings of a kill switch always leave a half-off state behind —
    here it would be "no new entries, but every finished ask still pays for a
    counter bump and a scheduling decision".

    ``store is None`` spells "this composition root did not wire it", which is
    how narrow test doubles and offline CLI roots stay byte-identical to the
    pre-feature behaviour.
    """
    return bool(
        getattr(settings, "retrieval_experience_enabled", True) and store is not None
    )


def _partition_label(partition: str) -> str:
    """The closed value an event may carry about a batch's partition.

    TWO words, and never an id. The event log is a different disclosure
    surface from the table: a stream of ``partition="nb-…"`` values beside
    their timestamps would let an operator reconstruct which libraries are
    busy and when they changed shape, which is the aggregate ``_emit``'s
    counts-only rule exists to withhold. "Which of the two chains ran" is what
    operations actually needs, and it is all that is published.
    """
    return "notebook" if partition else "global"


def _partition_cap(partition: str) -> int:
    """The row ceiling for ONE partition — 300 global, 100 per notebook.

    Read from the partition rather than passed in by the caller: eviction and
    the "existing entries" read must use the SAME number (reading 300 rows out
    of a partition capped at 100 would offer the model entries the next
    eviction is about to remove), and one function is how they cannot drift.
    """
    return (
        RETRIEVAL_EXPERIENCE_NOTEBOOK_MAX_ENTRIES if partition
        else RETRIEVAL_EXPERIENCE_MAX_ENTRIES
    )


class RetrievalExperienceDistillationService:
    """Per-partition threshold gates, ONE in-process single flight, one bounded
    call per batch.

    Backend-neutral by construction (ports and plain callables only), so it
    lives on the neutral repository runtime beside its P1 sibling.

    ⚠ ONE single-flight slot for ALL partitions, not one per partition. Two
    reasons, both structural: two batches writing ``retrieval_experiences``
    concurrently would interleave their eviction passes over a table they both
    trim, and a deployment where ten libraries cross their threshold in the
    same minute would bill ten model calls at once. Waiting NOTEBOOK partitions
    go into a bounded, de-duplicated queue instead
    (``_MAX_QUEUED_PARTITIONS``), which is why a library that reached its
    threshold while another batch was in flight still gets its batch — just
    later.

    ⚠ The GLOBAL partition is NOT in that queue, and that asymmetry is load
    bearing (T2 review round, P1). It has its own seat: whenever the slot frees
    and the deployment-wide counter is over its threshold, the global chain is
    claimed ahead of every waiting library. When it shared the queue, hundreds
    of ready libraries could hold it behind them indefinitely — a deployment
    busy enough to need the global fallback was exactly the one that never
    produced it.

    ⚠ The priority is STRICT only while the deployment can distil faster than
    it accumulates, and it yields one turn when it cannot (T2 review round 2,
    P2). The alternation rule, in full: the global chain goes first whenever it
    is ready, EXCEPT when it took the previous batch and at least one library
    is waiting — then that library goes. So with slack the global chain is
    never delayed, and under saturation (throughput at or below one batch per
    ``RETRIEVAL_EXPERIENCE_TRIGGER`` asks, where the global counter is over its
    threshold every single time the slot frees) the two chains alternate and
    the libraries take at least half the batches. Strict priority there meant
    200 libraries sharing zero batches; no priority meant the global chain
    starving. Neither can happen now.
    """

    def __init__(
        self,
        *,
        settings: Any,
        experiences: RetrievalExperienceStorePort,
        ask_state: "AskStateStorePort | None",
        models: Any,
        event_log: Any,
    ) -> None:
        self.settings = settings
        self.experiences = experiences
        self.ask_state = ask_state
        self.models = models
        self.event_log = event_log
        # ⚠ The threshold counters are PROCESS-LOCAL and reset on restart.
        # Registered, not overlooked: the library is a pure increment, so a
        # restart costs at most one skipped distillation round and never
        # correctness. Persisting them would mean a cursor table of its own
        # (design doc §13-Q4 parks that until production shows restarts are
        # frequent enough to starve the threshold).
        self._lock = threading.Lock()
        # The GLOBAL partition's counter keeps its old name: every existing
        # reader of ``_pending`` means this one.
        self._pending = 0
        # One counter per notebook that has had a completed reasoning ask IN
        # THIS PROCESS. A key appears on that library's first such ask; it is
        # removed when that library's batch is CLAIMED, and otherwise stays
        # until the process exits — a library that stops at nine asks keeps its
        # key and its nine. That is registered rather than cleaned up (T2
        # review round, P2): the key is a short id and a small int, five
        # thousand of them are on the order of half a megabyte, and the
        # alternative (expiring counters) would silently discard backlog that
        # a library is still entitled to. The dict is walked once per worker
        # exit (``_rearm_locked``) and nowhere else, so its size costs nothing
        # on the ask path.
        self._notebook_pending: dict[str, int] = {}
        # When each partition's last batch FINISHED (``time.monotonic``), for
        # the manual button's cooldown only. PROCESS-LOCAL and empty after a
        # restart, and that is the behaviour design §13-Q4 asks for rather than
        # a gap: a restart is exactly when the pending counters were lost and
        # the button has to be pressable again, so an empty dict must read as
        # "no cooldown", never as "cooled down long ago".
        #
        # Monotonic, not wall clock: a cooldown is a duration, and a clock
        # stepped backwards (NTP, DST on a naive clock) would otherwise freeze
        # the button for however far it jumped.
        #
        # Bounded the same way ``_notebook_pending`` is — one short id and one
        # float per library that has actually run a batch in this process,
        # which is a subset of the libraries with traffic. Registered, not
        # cleaned up, for the same reason.
        self._last_finished: dict[str, float] = {}
        self._running = False
        # NOTEBOOK partitions waiting for the slot, oldest first, de-duplicated
        # by the companion set: a library that crosses its threshold twice
        # while a batch is in flight has ONE batch waiting, not two (the second
        # would read a sample almost entirely overlapping the first). The
        # global partition is deliberately absent — see the class docstring.
        self._queue: "deque[str]" = deque()
        self._queued: set[str] = set()
        # Libraries the full room has already turned away and REPORTED. It
        # bounds the capacity signal to one event per library per saturated
        # period; membership is cleared when that library's batch is claimed.
        # Bounded by the queue's overflow, and every entry is also a key of
        # ``_notebook_pending``.
        self._refused: set[str] = set()
        # Whether the previous claim was the global chain's. The whole of the
        # weak yield in ``_claim_next_locked`` — one bool, so "global went
        # last" can cost it exactly one turn and never two.
        self._last_claimed_global = False

    # ------------------------------------------------------------- triggering
    def note_ask_completed(self, notebook_id: str) -> None:
        """One reasoning ask finished, in the library this id names.

        ⚠ FAIL-OPEN IN FULL, and it hangs off a hook that fires AFTER an answer
        has already been delivered: a delivered answer must never be affected by
        a background bookkeeping failure. Every ordinary exception is logged and
        swallowed; ``KeyboardInterrupt``/``SystemExit`` keep propagating.

        ⚠ It takes the notebook and STILL takes no user argument, and the
        asymmetry is the whole of this feature's privacy position. P2 shipped
        with a zero-argument signature and the note "a trigger that knew whose
        ask it was would be one refactor away from being a trigger that
        recorded it" — 2026-09-22 overturns that for the LIBRARY half only
        (design doc §12 deviation ①), because recording "in this library, this
        tactic pays off" is exactly what was asked for, and it is a statement
        about a shared corpus rather than about a person. The per-person half
        of the old argument stands untouched: there is no ``user_id`` here, and
        there is nowhere for one to go.
        What keeps the widening honest is not this signature. It is that the id
        goes into exactly three places — a read predicate, a written column and
        an id hash — and into ``RunObservation``/``ObservedRun``/the prompt
        never; the privacy guard's criteria eight and nine pin both halves.

        Both counters move: the deployment-wide one (which feeds the global
        fallback partition) and this library's own. Each crosses its own
        threshold on its own schedule, so one completed ask can arm two
        batches — they still run one at a time, through the shared slot.
        """
        try:
            if not distillation_wiring_active(self.settings, self.experiences):
                return
            partition = notebook_id if isinstance(notebook_id, str) else ""
            refused = False
            with self._lock:
                # 全局计数每次都 +1,但全局分区**不排队**:它在认领时单独优先
                # 判定(``_claim_next_locked``),所以拥挤的等待室饿不死它。
                self._pending += 1
                if partition:
                    self._notebook_pending[partition] = (
                        self._notebook_pending.get(partition, 0) + 1
                    )
                    if self._ready_locked(partition):
                        refused = not self._enqueue_locked(partition)
                # codex #524 R1/R2/R3 P2 三轮收敛出的形态:单飞认领与快照消费
                # 必须在**同一个**临界区完成——消费晚于认领哪怕一个锁间隙,
                # 快 worker 就可能已经跑完并释放槽位,并发的下一次完成会看到
                # 「阈值仍未被消费」而排出第二个重复批次(双份模型账单)。
                # busy 时只入队、不消费(整批留在计数里,出队时才结算);提交
                # 失败在 _submit_claimed 里恢复计数并释放槽位。
                claimed = self._claim_next_locked()
            if refused:
                # 一次完成最多一条事件,而且是**计数**不是名单——排队被拒是
                # 容量信号,不是某个库的事件。只有笔记本分区会排队,所以标签
                # 恒为 ``notebook``。
                self._emit(
                    "skipped", latency_ms=0, reason="queue_full", dropped=1,
                    partition="notebook",
                )
            if claimed is not None:
                self._submit_claimed(*claimed)
        except Exception:  # noqa: BLE001 — never break a delivered answer
            _log.exception("retrieval experience trigger failed")

    def distill_now(self, notebook_id: str) -> str:
        """Distil ONE partition right now; returns one of the
        ``MANUAL_DISTILL_*`` outcomes above.

        The manual control the P2 module docstring anticipated (design doc §8's
        button, wired in PR-2). It gates ITSELF on
        ``distillation_wiring_active`` rather than trusting ``start()`` to do
        it — ``start()``'s docstring has always said the shared entry point
        does not self-gate, and "a caller that believes this method self-gates
        is how a disabled feature keeps running" was written there before this
        caller existed.

        ⚠ **A refusal is never an error and each refusal is its own word.**
        This returned a bare ``False`` for every refusal until PR-2 put a
        button on it, and the conflation stopped being harmless the moment
        something had to render it: "the feature is off", "one is already
        running" and "you just ran one" call for three different sentences,
        and one of them is the difference between a user waiting and a user
        pressing again. Nothing here raises.

        ⚠ **The cooldown is the bill, not the politeness.** Every press costs
        one bounded model call over a sample of that library's most recent
        asks, so pressing twice with nothing in between pays twice to absorb
        the same runs and write nothing. Within
        ``_MANUAL_DISTILL_COOLDOWN_SECONDS`` of this partition's last finished
        batch the button is refused — **unless that library has pending asks**,
        which is the one case where a new batch genuinely has new input. Note
        which way the two conditions compose: pending traffic OVERRIDES the
        cooldown, it does not add to it.

        That same asymmetry is what keeps the §13-Q4 restart case working.
        After a restart the pending counters are gone (they are process-local,
        which is the whole reason this button exists) — but so is
        ``_last_finished``, so an unknown partition reads as "not cooling
        down" and the very first press goes through. An empty dict must never
        be read as "finished long ago"; it means "this process has not run a
        batch for that library at all".

        ⚠ A missing or non-string partition is REFUSED, not defaulted to the
        global one (T2 review round, P3). This button hangs off one library's
        panel, so an empty id here means the id was lost between the endpoint
        and this call — and "distil the deployment-wide library instead" is the
        most plausible-looking wrong answer available: it would succeed, bill a
        model call, and write entries into the partition every library reads.
        The ``skipped`` event says only that a call named no partition; it
        carries no value, because the value is exactly the thing this method is
        refusing to trust.
        """
        if not distillation_wiring_active(self.settings, self.experiences):
            return MANUAL_DISTILL_DISABLED
        if not isinstance(notebook_id, str) or not notebook_id:
            self._emit(
                "skipped", latency_ms=0, reason="invalid_partition",
                partition="notebook",
            )
            return MANUAL_DISTILL_INVALID
        with self._lock:
            finished = self._last_finished.get(notebook_id)
            cooling = (
                self._pending_locked(notebook_id) == 0
                and finished is not None
                and (time.monotonic() - finished) < _MANUAL_DISTILL_COOLDOWN_SECONDS
            )
        if cooling:
            # Outside the lock, like every other emit in this class. The event
            # carries the closed partition LABEL only, never the id — the same
            # rule the rest of this chain's telemetry follows.
            self._emit(
                "skipped", latency_ms=0, reason="cooldown", partition="notebook",
            )
            return MANUAL_DISTILL_COOLDOWN
        if not self.start(notebook_id):
            return MANUAL_DISTILL_BUSY
        return MANUAL_DISTILL_STARTED

    # ------------------------------------------------- the waiting-room state
    # Every method below runs WITH ``_lock`` held (``_locked`` suffix) and does
    # no I/O of any kind: the whole point of the queue is that deciding what
    # runs next is a few dict and deque operations inside one critical section,
    # while everything that can block (submitting, emitting) happens outside it.
    def _notebook_threshold(self) -> int:
        """The per-library trigger count — the SAME number for every library.

        Split out from ``_threshold`` so a pass over many libraries can read it
        once (``_rearm_locked``) instead of once per key: the value depends on
        which CHAIN a partition belongs to, never on which library it is.
        """
        return max(1, int(
            getattr(self.settings, "retrieval_experience_notebook_trigger", 10)
        ))

    def _threshold(self, partition: str) -> int:
        """This partition's trigger count, floored at 1 (0 would schedule a
        bounded LLM call on every single ask)."""
        if partition:
            return self._notebook_threshold()
        return max(1, int(
            getattr(self.settings, "retrieval_experience_trigger", 40)
        ))

    def _pending_locked(self, partition: str) -> int:
        if partition:
            return self._notebook_pending.get(partition, 0)
        return self._pending

    def _ready_locked(self, partition: str) -> bool:
        return self._pending_locked(partition) >= self._threshold(partition)

    def _consume_locked(self, partition: str) -> int:
        """Take this partition's whole backlog and zero it.

        The notebook half POPS rather than assigning 0: a library with no
        further traffic should leave no key behind, so the dict stays the size
        of live traffic rather than of the notebook table.

        ⚠ It also clears this library's "already reported as refused" mark: it
        just got its batch, so the NEXT time a full room turns it away is a new
        fact and deserves its own event. That pairing is what bounds the event
        stream to "once per library per saturated period" instead of "once per
        completed ask" (T2 review round 2, P2).
        """
        if partition:
            self._refused.discard(partition)
            return self._notebook_pending.pop(partition, 0)
        snapshot, self._pending = self._pending, 0
        return snapshot

    def _restore_locked(self, partition: str, snapshot: int) -> None:
        if partition:
            self._notebook_pending[partition] = (
                self._notebook_pending.get(partition, 0) + snapshot
            )
            return
        self._pending += snapshot

    def _enqueue_locked(self, partition: str) -> bool:
        """Put a ready NOTEBOOK partition in the waiting room.

        ``True`` = it has a place in line (including "it already did");
        ``False`` = the room is full, this library was REFUSED, **and this is
        the first refusal since its last batch** — so the caller should report
        it. A refusal costs the library nothing: its counter is untouched, so
        the next ``_rearm_locked`` pass — or its next completed ask — offers it
        again.

        ⚠ A repeat refusal returns ``True``. That reads backwards for a moment
        and is the point: the boolean answers "does the caller have something
        NEW to say", not "did this one get in". Under saturation a library is
        turned away on every single completed ask, and reporting each one
        turned a capacity signal into one log line + one file append per ask
        (measured: 12830 events across 20000 asks). The mark is cleared when
        that library's batch is finally claimed (``_consume_locked``), so a
        second saturated period does get a second event.

        De-duplicating is not an optimisation: two queued batches for the same
        library would read almost the same forty runs, and the second would
        spend a model call re-deriving conclusions the first just wrote (the
        provenance de-duplication keeps that from inflating ``support``, but
        nothing refunds the call).

        The global partition is refused outright — it does not wait in line at
        all, it is claimed ahead of the queue (see ``_claim_next_locked``).
        """
        if not partition:
            return False
        if partition in self._queued:
            return True
        if len(self._queue) >= _MAX_QUEUED_PARTITIONS:
            if partition in self._refused:
                return True                 # 已经报过一次,不再重复报
            self._refused.add(partition)
            return False
        self._queue.append(partition)
        self._queued.add(partition)
        return True

    def _rearm_locked(self) -> int:
        """Offer every over-threshold library a place in line; count refusals.

        The safety net the pre-partition ``_maybe_requeue`` was: a backlog that
        is ready but NOT queued (the room was full when it arrived, or its
        submission failed) would otherwise sit there until that library happens
        to be used again. Only called on a worker's way out, so the walk over
        live counters costs nothing on the ask path.

        ⚠ Insertion order, NOT sorted: determinism here only has to mean "the
        same pass twice produces the same queue", which dict order already
        gives, and sorting put an ``O(n log n)`` over every live counter inside
        the lock (T2 review round, P2). Nothing in the loop mutates the dict —
        ``_enqueue_locked`` touches only the queue — so iterating it directly
        is safe.

        Returns how many libraries the full room turned away FOR THE FIRST TIME
        since their last batch, for ONE aggregated event; the caller emits it
        outside the lock. A library that was already reported as refused is not
        counted again — see ``_enqueue_locked``.
        """
        # 阈值对整趟是常量,提到循环外:一趟要走遍每个活跃库的计数,而每次
        # 取阈值都要读一次 settings(T2 评审轮 2,P3)。
        threshold = self._notebook_threshold()
        refused = 0
        for partition, pending in self._notebook_pending.items():
            if pending >= threshold and not self._enqueue_locked(partition):
                refused += 1
        return refused

    def _claim_next_locked(self) -> "tuple[str, int] | None":
        """Claim the slot for the next partition to run, consuming its backlog.

        Returns ``(partition, snapshot)`` for the caller to submit OUTSIDE the
        lock, or ``None`` when the slot is taken or nobody is ready. Claim and
        consume happen here, together, for the reason spelled out in
        ``note_ask_completed``.

        ⚠ The GLOBAL partition is checked FIRST and never from the queue. That
        ordering is what makes starving it structurally impossible: it is ready
        only once per ``RETRIEVAL_EXPERIENCE_TRIGGER`` completed asks, and when
        it is, it goes next. A shared FIFO gave the opposite guarantee — the
        more libraries a deployment had, the longer the global fallback waited,
        without bound.

        ⚠ …with ONE weak yield: if the global chain took the previous batch and
        libraries are waiting, it stands aside for one (T2 review round 2, P2).
        Unconditional priority is only cheap while batches are cheaper than
        arrivals; a deployment whose throughput is one batch per forty asks is
        exactly a deployment where the global chain is ready EVERY time the
        slot frees, and there it took every batch — 200 libraries, zero. The
        yield makes the two chains alternate under saturation (libraries get at
        least half the batches) and changes nothing when there is slack: with
        an empty queue, or after any notebook batch, the global claim is
        immediate as before. It is still unstarvable — one yield, never two,
        because the flag is set the moment the global chain does run.
        """
        if self._running:
            return None
        global_ready = self._ready_locked("")
        if global_ready and not (self._queue and self._last_claimed_global):
            partition = ""
        elif self._queue:
            partition = self._queue.popleft()
            self._queued.discard(partition)
        else:
            return None
        self._last_claimed_global = not partition
        self._running = True
        return partition, self._consume_locked(partition)

    def _maybe_requeue(self) -> None:
        """Hand the freed slot to the next waiting partition.

        Same critical-section shape as ``note_ask_completed`` — claim and
        consume atomically, submit outside the lock. Fail-open: a stranded
        batch is an optimization loss, never an error surface.
        """
        try:
            with self._lock:
                refused = self._rearm_locked()
                claimed = self._claim_next_locked()
            if refused:
                # 一趟至多一条,而且只报**数量**:一个 300 个就绪库的部署每次
                # 轮转会拒掉两百多次,逐个发事件就是一场事件风暴,而运维要看的
                # 是「等待室满了多少」这一个数(T2 评审轮 P1)。
                self._emit(
                    "skipped", latency_ms=0, reason="queue_full",
                    dropped=refused, partition="notebook",
                )
            if claimed is not None:
                self._submit_claimed(*claimed)
        except Exception:  # noqa: BLE001 — requeue is best-effort
            _log.exception("retrieval experience requeue failed")

    def _submit_claimed(self, partition: str, snapshot: int) -> None:
        """Submit the worker for a claim ALREADY taken by the caller.

        The caller consumed ``snapshot`` pending completions inside the same
        critical section that set ``_running`` — on a submission failure both
        must be restored, or the signals are lost and the slot is stranded.
        The restored backlog is still over its threshold, so the next completed
        ask in that partition re-enqueues it.

        ⚠ A failure here also leaves whatever else is in the waiting room
        waiting: nothing re-arms until the next completed ask (or the next
        worker's exit) comes along. Fail-open and registered — the cost is a
        deferred optimisation, never a wrong entry — and a submission that
        cannot start a thread is a condition the next ask will hit too.
        """
        try:
            background_jobs.submit(
                self.run,
                partition,
                name=f"retrievalexperience-{_partition_label(partition)}",
                notify_pending=False,
            )
        except BaseException:
            with self._lock:
                self._running = False
                self._restore_locked(partition, snapshot)
            self._emit(
                "failed", latency_ms=0, reason="job_submission_failed",
                partition=_partition_label(partition),
            )
            raise

    def start(self, partition: str = "") -> bool:
        """Claim the single-flight slot and submit the worker; ``False`` = busy.

        The claim happens HERE, before the thread exists — the same order as
        the agent-profile chains and ``catalog_job``: a claim taken inside the
        worker leaves a window in which a second trigger schedules a second
        writer over the same table. A submit failure releases it on the spot,
        because a stranded in-process flag is held until the process dies.

        ⚠ This method does not consult ``distillation_wiring_active``: it is
        the shared entry point, and each caller gates itself
        (``note_ask_completed`` checks before counting, ``distill_now`` checks
        before delegating here). A caller that believes this method self-gates
        is how a disabled feature keeps running.

        ⚠ It does NOT consume any partition's pending counter, because it did
        not reach a threshold — a manual run is extra, not instead of the
        batch that library was accumulating. The queue is untouched too: this
        claim jumps whatever is waiting, which is the point of a button.
        """
        with self._lock:
            if self._running:
                return False
            self._running = True
        try:
            background_jobs.submit(
                self.run,
                partition,
                name=f"retrievalexperience-{_partition_label(partition)}",
                notify_pending=False,
            )
        except BaseException:
            with self._lock:
                self._running = False
            self._emit(
                "failed", latency_ms=0, reason="job_submission_failed",
                partition=_partition_label(partition),
            )
            raise
        return True

    # --------------------------------------------------------------- the run
    def run(self, partition: str = "") -> None:
        """One distillation batch, for ONE partition. Never raises to the job
        runner.

        Reads that partition's most recent completed asks (the whole
        deployment's when ``partition`` is empty), aggregates them by
        situation, shows the model the busiest situations alongside the entries
        that same partition already holds for similar ones, and applies
        whatever comes back — after validating it against the closed
        vocabularies, which is where a malformed reply dies.

        ⚠ ``partition`` defaults to the global one so that a bare ``run()``
        keeps its pre-partition meaning: every direct call in the tests, and
        any caller written before partitions existed, distils the deployment
        library exactly as it used to.

        Every exit path releases the single-flight flag, ``BaseException``
        included: ``KeyboardInterrupt``/``SystemExit`` inherit from it and sail
        past ``except Exception``, and a flag left set means this deployment
        never distils again until it restarts.

        ⚠ The release is gated on ``claimed_here`` — a plain read of
        ``_running`` taken ONCE, under the lock, before any work starts — and
        ``finally`` only clears the flag when that read found it already
        ``True``. This method has to stay safe to call directly: every test in
        this module does, and the module docstring already anticipates a
        future manual "distil now" control doing the same. ``start()`` is the
        only place that may transition the flag ``False -> True`` (its own
        docstring explains why the claim has to happen there, before the
        thread exists, rather than in here); a bare call to ``run()`` that
        finds the flag still ``False`` never claimed the slot, so its
        ``finally`` must leave the flag alone. Without the gate, two
        interleaved calls to this method would each decide "I own the slot,
        release it when I finish" from the SAME shared flag, and whichever
        finishes first would release it out from under the other — exactly
        the two-workers race ``start()``'s pre-claim exists to prevent, just
        moved one method over.
        """
        started = time.monotonic()
        label = _partition_label(partition)

        def latency_ms() -> int:
            return int((time.monotonic() - started) * 1000)

        with self._lock:
            claimed_here = self._running

        try:
            if not distillation_wiring_active(self.settings, self.experiences):
                self._emit(
                    "skipped", latency_ms=latency_ms(), reason="disabled",
                    partition=label,
                )
                return
            if self.ask_state is None:
                self._emit(
                    "skipped", latency_ms=latency_ms(), reason="not_wired",
                    partition=label,
                )
                return
            if not self.models.configured(RETRIEVAL_EXPERIENCE_WORKLOAD):
                # Checked before the read: an unconfigured deployment should
                # pay nothing to learn it is unconfigured.
                self._emit(
                    "skipped", latency_ms=latency_ms(),
                    reason="model_unavailable", partition=label,
                )
                return
            outcome = self._distill(partition)
            self._emit("done", latency_ms=latency_ms(), partition=label, **outcome)
        except BaseException as exc:  # noqa: BLE001 — see docstring
            self._emit(
                "failed",
                latency_ms=latency_ms(),
                reason=type(exc).__name__,
                partition=label,
            )
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            _log.exception("retrieval experience distillation failed")
        finally:
            # 这一批(不论成功、早退还是失败)到此为止——记下完成时刻,供手动
            # 「立即整理」的冷却判据。记在**所有**退出路径上而不只是成功路径:
            # 冷却问的是「刚刚为这个库跑过一趟吗」,而一趟因关闸/未配置模型早退
            # 的批次同样已经把这个库看过一遍,连按十次也不会有不同结果。
            # ``claimed_here`` 与它无关——直接调 ``run()`` 也是真的跑过一批。
            with self._lock:
                self._last_finished[partition] = time.monotonic()
            if claimed_here:
                with self._lock:
                    self._running = False
                # codex #524 R4 P2:释放后原子复查积压——busy 期间攒满的整批
                # 若得不到后续流量会永远滞留。复用与 note_ask_completed 同一
                # 临界区形态(认领+消费原子),fail-open。分区化之后这里还负责
                # 把等待队列里的下一个分区接上,所以一次突发之后每个攒够的库
                # 都会依次轮到,而不是只有最后那一个。
                self._maybe_requeue()

    def _distill(self, partition: str = "") -> dict:
        cap = _partition_cap(partition)
        runs = self._observe(partition)
        if not runs:
            return {"runs": 0, "situations": 0, "written": 0, "evicted": 0}
        groups = _group_by_situation(runs)
        if not groups:
            return {
                "runs": len(runs), "situations": 0, "written": 0, "evicted": 0,
            }
        existing = self.experiences.read_partition(partition, cap)
        offered = _offered_entries(groups, existing)
        client = self.models.chat(RETRIEVAL_EXPERIENCE_WORKLOAD)
        prompt = retrieval_experience_prompt(
            render_observations(groups),
            render_existing(offered),
            actions=RETRIEVAL_ACTIONS,
            rationale_max_chars=RETRIEVAL_EXPERIENCE_RATIONALE_MAX_CHARS,
        )
        raw = client.chat_json(
            [{"role": "user", "content": prompt}],
            RETRIEVAL_EXPERIENCE_SCHEMA_HINT,
            max_tokens=RETRIEVAL_EXPERIENCE_MAX_OUTPUT_TOKENS,
        )
        parsed = parse_distillation_reply(safe_json(raw), groups, offered)
        written = 0
        # codex #524 R12 P2:每条 upsert 独立提交,驱逐必须放 finally——
        # 中途一条失败若跳过驱逐,300 上限被突破后注入端按 id 只读前 300,
        # 更好的条目会被任意遮蔽,且要等到下一次成功蒸馏才自愈。进程被硬杀
        # 的残余超额同样由下一个批次的这次无条件驱逐收走。
        try:
            for entry in parsed:
                situation = entry["situation"]
                self.experiences.upsert_experience(
                    # 内容寻址 id 必须与写入列同源:分区既进哈希输入,又进
                    # ``notebook_id`` 列。两者取自同一个 ``partition`` 变量,
                    # store 侧还会在合并分支上复核一次(不一致直接
                    # ValueError),所以"id 说 A、列说 B"这种形态既拼不出来、
                    # 也落不了库。
                    experience_id(situation, entry["action"], partition=partition),
                    situation=situation,
                    action=entry["action"],
                    polarity=entry["polarity"],
                    rationale=entry["rationale"],
                    provenance=entry["provenance"],
                    provenance_max=RETRIEVAL_EXPERIENCE_PROVENANCE_MAX,
                    replace_conclusion=entry["replace"],
                    notebook_id=partition,
                )
                written += 1
        finally:
            evicted = self.experiences.evict_to_limit(cap, notebook_id=partition)
        return {
            "runs": len(runs),
            "situations": len(groups),
            "written": written,
            "evicted": evicted,
        }

    def _observe(self, partition: str = "") -> list[ObservedRun]:
        """The chain's ENTIRE view of its partition: one bounded read, then
        the projection.

        ⚠ ``project_run`` is not a convenience here — it is the only way a run
        may enter this module. Reading any other field off these rows, or
        reaching for a different store, would defeat the structural guarantee
        that makes a table read by everyone in a library safe at all.

        ⚠ The partition is spent HERE, on the read's predicate, and nowhere
        downstream: the rows that come back are the same shape either way, so
        everything after this line — grouping, rendering, the prompt — cannot
        tell which chain it is serving. ``None`` rather than ``""`` for the
        global chain, because on the port ``""`` would be a real predicate
        matching nothing.

        ⚠ The batch size is capped by ``RETRIEVAL_EXPERIENCE_BATCH_RUNS``, and
        the fact that it does not exceed ``RETRIEVAL_EXPERIENCE_PROVENANCE_MAX``
        is an INVARIANT rather than a coincidence: an entry remembers at most
        that many run ids, and re-observing a run it has forgotten counts that
        run's support a second time. A test pins the relationship.
        """
        rows = self.ask_state.recent_completed_ask_runs(
            job_limit=RETRIEVAL_EXPERIENCE_BATCH_RUNS,
            step_limit=RETRIEVAL_EXPERIENCE_BATCH_STEPS,
            notebook_id=partition or None,
        )
        observed: list[ObservedRun] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            run = project_run(row)
            if run is not None:
                observed.append(run)
        return observed

    # ------------------------------------------------------------ bookkeeping
    def _emit(self, status: str, *, latency_ms: int, **extra: Any) -> None:
        """Counts and ONE closed word — never a rationale, never an action
        word, never an id.

        ``rationale`` is model-written prose and the event log is a different
        disclosure surface from the table it lives in; ``action`` and the
        situation values are excluded for a subtler reason — a stream of events
        carrying (situation, action) pairs beside their timestamps would let an
        operator reconstruct which shapes of question the deployment is
        currently seeing, which is the aggregate this feature is careful not to
        publish anywhere else.

        ``partition`` is the one non-count field, and it is a CLOSED value
        (``global``/``notebook``, see ``_partition_label``) precisely because
        the same argument applies to library ids: "which chain ran" is what
        operations needs to read a latency number correctly, and "which library
        ran, and when" is a usage profile. It carries a default here so the
        event's key set is the same on every path — a caller that forgot it
        would otherwise emit an event shaped differently from its neighbours,
        which is how a dashboard silently loses a series.
        """
        try:
            self.event_log.emit(
                {
                    "kind": "retrieval_experience_distilled",
                    "status": status,
                    "latency_ms": int(latency_ms),
                    "partition": "global",
                    "runs": 0,
                    "situations": 0,
                    "written": 0,
                    "evicted": 0,
                    **extra,
                }
            )
        except Exception:  # noqa: BLE001 — diagnostics never break a run
            pass


# --------------------------------------------------------------- aggregation

#: Index layout of ``_SituationGroup.actions``' per-action tally list.
#: Agentic Memory P4 (T4) widened it from 2 entries to 4 — ``ANCHORED``/
#: ``ATTRIBUTABLE_RUNS`` fold in ``ActionObservation.anchored_hits`` /
#: ``attributable`` (see that type's docstring in
#: ``retrieval_experience_projection.py`` for what the two source fields
#: mean). Named indices rather than four more ``__slots__`` entries: the
#: tally is already a plain list keyed by action id, and a fifth parallel
#: dict would just be the same information split across two containers that
#: have to stay in lock-step.
_TALLY_INVOCATIONS = 0
_TALLY_ZERO_HITS = 1
_TALLY_ANCHORED = 2
_TALLY_ATTRIBUTABLE_RUNS = 3


class _SituationGroup:
    """One question shape plus everything the batch saw under it."""

    __slots__ = (
        "key", "situation", "runs", "runs_with_actions", "run_ids",
        "citations", "actions", "action_run_ids",
    )

    def __init__(self, key: str, situation: dict) -> None:
        self.key = key
        self.situation = situation
        self.runs = 0
        # ⚠ NOT the same denominator as ``runs``. A run's trace steps are
        # capped (``RETRIEVAL_EXPERIENCE_BATCH_STEPS``), and a run truncated
        # down to just its intent step still belongs to this situation — its
        # question shape happened — but it carries zero actions, and folding
        # it into the SAME "runs=N" the action tallies are read against would
        # understate how common each action actually is among the runs that
        # had any action to tally at all. Kept separate so the prompt can show
        # both numbers rather than silently picking one.
        self.runs_with_actions = 0
        self.run_ids: list[str] = []
        self.citations = 0
        # Agentic Memory P4 (T4): four ints per action, see the module-level
        # ``_TALLY_*`` indices above — invocations, zero-hit count, and the
        # two new step→anchor attribution scalars folded in from
        # ``ActionObservation``.
        self.actions: dict[str, list[int]] = {}
        # codex #524 R9 P2:每个动作记下**哪些 run** 真的用过它——一个 run 的
        # 重试不是跨 run 的模式,而条目的 provenance/support 若归属全组 run,
        # 没用过该动作的 run 也会被算进支持数。
        self.action_run_ids: dict[str, list[str]] = {}

    def absorb(self, run: ObservedRun) -> None:
        self.runs += 1
        self.run_ids.append(run.run_id)
        self.citations += run.observation.citations
        if run.observation.actions:
            self.runs_with_actions += 1
        for action in run.observation.actions:
            tally = self.actions.setdefault(action.action, [0, 0, 0, 0])
            tally[_TALLY_INVOCATIONS] += action.invocations
            tally[_TALLY_ZERO_HITS] += action.zero_hits
            tally[_TALLY_ANCHORED] += action.anchored_hits
            # ``attributable`` is a per-(run, action) verdict already —
            # ``ActionObservation`` aggregates every invocation of this
            # action WITHIN one run before this loop ever sees it — so
            # incrementing once per absorbed run is exactly "how many runs
            # in this group could this action's contribution be checked
            # against the answer's anchors", not a double count.
            if action.attributable:
                tally[_TALLY_ATTRIBUTABLE_RUNS] += 1
            self.action_run_ids.setdefault(action.action, []).append(run.run_id)

    def runs_for(self, action: str) -> list[str]:
        """The run ids that actually invoked ``action`` in this group."""
        return list(self.action_run_ids.get(action, ()))


def _group_by_situation(runs: Sequence[ObservedRun]) -> list[_SituationGroup]:
    """Bucket the batch by question shape, busiest first, capped.

    Ordering is ``(-runs, key)`` — frequency, then the situation's own
    fingerprint. The tie-break is what makes the batch deterministic: two
    situations seen the same number of times must always be offered in the same
    order, or the same batch of runs distils differently on a re-run and the
    provenance de-duplication is comparing against a different set of entries.
    """
    groups: dict[str, _SituationGroup] = {}
    for run in runs:
        situation = run.observation.situation()
        key = experience_id(situation, "")
        group = groups.get(key)
        if group is None:
            group = _SituationGroup(key, situation)
            groups[key] = group
        group.absorb(run)
    ordered = sorted(groups.values(), key=lambda g: (-g.runs, g.key))
    return ordered[:_MAX_SITUATIONS_PER_BATCH]


def _offered_entries(
    groups: Sequence[_SituationGroup], existing: Sequence[Mapping[str, Any]]
) -> list[tuple[int, Mapping[str, Any]]]:
    """The existing entries worth showing beside each offered situation.

    Returns ``(situation index, entry)`` pairs, de-duplicated across groups so
    an entry similar to two of the offered situations is rendered once. The
    index is what an UPDATE names, which is why the pairing is computed here
    rather than being re-derived from the reply.
    """
    offered: list[tuple[int, Mapping[str, Any]]] = []
    seen: set[str] = set()
    for index, group in enumerate(groups):
        scored = []
        for entry in existing:
            situation = entry.get("situation")
            if not isinstance(situation, Mapping):
                continue
            score = situation_similarity(group.situation, situation)
            if score >= _SIMILARITY_FLOOR:
                scored.append((score, str(entry.get("id") or ""), entry))
        scored.sort(key=lambda item: (-item[0], item[1]))
        # codex #524 R6 P2:同一 (situation index, action) 至多展示**一条**——
        # 模型的 UPDATE 只能用 `sN | action` 指认目标,两条相似旧条目共享同一
        # 标签时解析必然歧义(首个匹配可能不是模型想改的那条,一次 UPDATE 就
        # 污染另一条打法)。只留相似度最高的那条,解析按构造无歧义。
        actions_taken: set[str] = set()
        accepted = 0
        # codex #524 R11 P2:名额在**去重之后**才消耗——先切片再去重会让排前
        # 的重复动作/已展示条目白占名额,低一名的合格条目明明存在却展示不出。
        for _score, entry_id, entry in scored:
            if accepted >= _MAX_SIMILAR_ENTRIES:
                break
            if entry_id in seen:
                continue
            action = str(entry.get("action") or "")
            if action in actions_taken:
                continue
            actions_taken.add(action)
            seen.add(entry_id)
            offered.append((index, entry))
            accepted += 1
    return offered


# ----------------------------------------------------------------- rendering

def render_observations(groups: Sequence[_SituationGroup]) -> str:
    """The IF/statistics half of the prompt.

    Every token here is a count or a closed vocabulary word. There is no branch
    in this function that can emit text originating from a document, a question
    or a user — which is the property the whole feature rests on, and the
    reason this renderer takes ``_SituationGroup`` rather than the raw rows.

    The ``runs=N (M with sampled actions)`` split matters: ``N`` is how often
    this question shape happened, ``M`` is how many of those runs actually
    carried a sampled action (a step-truncated run belongs to neither the
    action tallies nor their denominator). Reading the per-action tallies
    against ``N`` instead of ``M`` would make a busy shape with many
    step-truncated runs read as rarer per action than it really is among the
    runs that had anything to tally.

    Agentic Memory P4 (T4): each action line may carry a trailing
    ``anchored=N (attributable in M of K runs)`` clause — ``N`` the summed
    ``anchored_hits`` across the group, ``M`` how many of the ``K`` runs that
    invoked this action could be checked against the answer's anchors at all
    (``_TALLY_ATTRIBUTABLE_RUNS``), ``K`` the same ``runs_using`` the existing
    ``came_back_empty`` clause already reads against. The clause is rendered
    **only when** ``M > 0`` — never a bare ``anchored=0 (attributable in 0 of
    K runs)`` — so a batch entirely predating step→anchor attribution (every
    run in it old-shape) renders BYTE-IDENTICAL to what this function emitted
    before T4 landed. A batch mixing old and new runs therefore shows the
    clause only on the actions some run in it could actually attribute;
    actions only ever invoked by old-shape runs stay silent about it, exactly
    like the batch that predates the feature entirely.
    """
    lines = ["[Recent searches, grouped by question shape]"]
    for index, group in enumerate(groups):
        shape = ", ".join(
            f"{key}={_render_value(group.situation[key])}"
            for key in sorted(group.situation)
        )
        lines.append(f"s{index}: {shape}")
        lines.append(
            f"  runs={group.runs} ({group.runs_with_actions} with sampled "
            f"actions) total_citations={group.citations}"
        )
        for action in RETRIEVAL_ACTIONS:
            tally = group.actions.get(action)
            if tally is None:
                continue
            runs_using = len(group.action_run_ids.get(action, ()))
            line = (
                f"  {action}: used={tally[_TALLY_INVOCATIONS]} "
                f"came_back_empty={tally[_TALLY_ZERO_HITS]} "
                f"(in {runs_using} of {group.runs_with_actions} runs)"
            )
            attributable_runs = tally[_TALLY_ATTRIBUTABLE_RUNS]
            if attributable_runs > 0:
                line += (
                    f" anchored={tally[_TALLY_ANCHORED]} "
                    f"(attributable in {attributable_runs} of {runs_using} runs)"
                )
            lines.append(line)
    return "\n".join(lines)


def render_existing(offered: Sequence[tuple[int, Mapping[str, Any]]]) -> str:
    """The half of the prompt that says what the library already holds.

    ``rationale`` is the only free text rendered anywhere in this prompt, and
    it is text this same chain wrote, in an earlier round, from an input of the
    same shape. It is not a new disclosure surface — it is the library reading
    back its own notes.
    """
    if not offered:
        return "[Existing entries for similar shapes]\n(none)"
    lines = ["[Existing entries for similar shapes]"]
    for index, entry in offered:
        lines.append(
            f"s{index} | {entry.get('action')} | {entry.get('polarity')} | "
            f"support={entry.get('support')} | {entry.get('rationale')}"
        )
    return "\n".join(lines)


def _render_value(value: object) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


# ------------------------------------------------------------------- parsing

def parse_distillation_reply(
    payload: object,
    groups: Sequence[_SituationGroup],
    offered: Sequence[tuple[int, Mapping[str, Any]]] = (),
) -> list[dict]:
    """Validate the model's reply against the closed vocabularies.

    Rejection is PER ENTRY, not per reply — one hallucinated action word should
    not discard the three sound conclusions beside it — with one exception: a
    payload that is not an object with an ``entries`` list is discarded whole,
    because there is nothing in it to salvage per entry.

    What each entry has to survive:

    * ``op`` is one of ADD / UPDATE / NOOP. NOOP writes nothing and is the
      expected answer most of the time.
    * ``situation`` names one of the offered indices. It is never CONSTRUCTED
      by the model — the server hands out ``s0``/``s1`` keys and resolves them
      back here, so an entry's situation is by construction one the deployment
      actually observed. (Same discipline as the evidence keys elsewhere in
      this codebase, and for the same reason: a model-built situation map would
      have to be validated against the registry anyway, and any value it got
      wrong would file the entry under a shape of question that never occurs.)
    * ``action`` matches ``RETRIEVAL_ACTIONS`` exactly, AND at least one run
      in the entry's situation group actually invoked it this batch — the
      vocabulary says the word is legal, only an observed execution says there
      is evidence. No prefix matching and
      no nearest-neighbour repair: ``ppr_retrieve`` is not ``ppr``, and
      guessing which one was meant is how an entry ends up about a channel the
      model was not writing about.
    * ``polarity`` is exactly ``good`` or ``bad``.
    * ``rationale`` is non-empty, within the character cap, and matches
      NEITHER id tripwire (a long hex run, or a ``nb-`` prefixed notebook id —
      the second one exists because the first needs sixteen hex characters and
      a notebook id carries about ten). Over-length is a rejection rather than a clip: a
      truncated line of advice reads as confident and complete having lost its
      qualifier. The id check is a tripwire on the input narrowing rather than
      a sanitiser — if an id ever reaches this point, the entry is discarded
      and the narrowing needs fixing, not the entry.
    * the resolved situation re-validates through ``validate_situation``. It
      came from the server, so this can only fail if the registry and the
      projection have drifted apart — which is exactly when a silent pass would
      be worst.
    """
    if not isinstance(payload, Mapping):
        return []
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return []
    by_key = {
        f"s{index}": (index, group) for index, group in enumerate(groups)
    }
    accepted: list[dict] = []
    for item in entries:
        if not isinstance(item, Mapping):
            continue
        op = str(item.get("op") or "").strip().upper()
        if op not in _OPS or op == "NOOP":
            continue
        resolved = by_key.get(str(item.get("situation") or "").strip().lower())
        if resolved is None:
            continue
        index, group = resolved
        action = str(item.get("action") or "").strip()
        if action not in RETRIEVAL_ACTIONS:
            continue
        supporting_runs = group.runs_for(action)
        if not supporting_runs:
            # codex #524 R10 P2:合法词表里的动作 ≠ 被观测过的动作。本批
            # 没有任何 run 真用过它,这条结论就没有一次执行作证据——落库
            # 会得到 support=0 的条目,而注入不要求正支持,幻觉打法会
            # 反过来指挥真实检索。ADD/UPDATE 统一丢弃:UPDATE 的"新证据"
            # 同样只能来自本批用过该动作的 run。
            continue
        polarity = str(item.get("polarity") or "").strip().lower()
        if polarity not in EXPERIENCE_POLARITIES:
            continue
        rationale = " ".join(as_text(item.get("rationale")).split())
        if not rationale or len(rationale) > RETRIEVAL_EXPERIENCE_RATIONALE_MAX_CHARS:
            continue
        if _ID_SHAPE.search(rationale) or _NOTEBOOK_ID_SHAPE.search(rationale):
            _log.warning(
                "retrieval experience entry rejected: rationale carried an "
                "id-shaped token, which means the observation narrowing let "
                "something through"
            )
            continue
        situation = validate_situation(group.situation)
        if situation is None:
            continue
        replace = op == "UPDATE"
        if replace:
            # codex #524 R2 P2: an UPDATE names an OFFERED entry, and the
            # offered entry's situation may be merely SIMILAR to this group's
            # (that is what the similarity floor admits). Hashing the group's
            # situation would then write a brand-new row while the entry the
            # model actually revised stays behind — two contradictory tactics
            # both injectable. Resolve the UPDATE to the offered entry's own
            # stored situation (its identity); when no offered entry with this
            # action exists under this index, the model updated something it
            # was never shown — downgrade to a non-replacing ADD.
            target = next(
                (
                    entry for offered_index, entry in offered
                    if offered_index == index
                    and str(entry.get("action") or "") == action
                ),
                None,
            )
            if target is not None:
                stored = validate_situation(target.get("situation"))
                if stored is not None:
                    situation = stored
                else:
                    replace = False
            else:
                replace = False
        if not replace and len(supporting_runs) < _MIN_SUPPORTING_RUNS:
            # ADD(含被降级的 UPDATE)必须 ≥ _MIN_SUPPORTING_RUNS 个不同 run;
            # 真正的 UPDATE 走上面的 ≥1 豁免。
            continue
        accepted.append(
            {
                "situation": situation,
                "action": action,
                "polarity": polarity,
                "rationale": rationale,
                # codex #524 R9 P2:provenance 只归属真用过该动作的 run——
                # 全组归属会让 support 被没用过它的 run 虚增。
                "provenance": group.runs_for(action),
                # ADD landing on an existing entry must not rewrite its
                # conclusion — the model said "the library does not hold this
                # yet", so it was not reasoning about what is stored there.
                # Only an explicit UPDATE (resolved to the offered entry's own
                # identity above) replaces polarity and rationale.
                "replace": replace,
            }
        )
    return accepted


__all__ = [
    "RETRIEVAL_EXPERIENCE_MAX_OUTPUT_TOKENS",
    "RETRIEVAL_EXPERIENCE_WORKLOAD",
    "RetrievalExperienceDistillationService",
    "distillation_wiring_active",
    "parse_distillation_reply",
    "render_existing",
    "render_observations",
]
