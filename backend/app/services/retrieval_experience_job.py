"""Agentic Memory P2 (A / T5): the distillation chain for the deployment-GLOBAL
retrieval-strategy experience library.

Design doc §6.1. Every N completed asks across the whole deployment, ONE
bounded model call looks at aggregated statistics from the most recent runs
plus the entries the library already holds for similar situations, and decides
what — if anything — to record.

⚠ **The privacy is structural, not a prompt rule** — the same sentence the
agent-profile base chain opens with, but carrying more weight here because
there is no tenancy predicate to fall back on. This chain reads ONE thing,
``recent_completed_ask_runs``, which has no user and no notebook predicate by
design; what makes that safe is the SHAPE of what comes back. Every run is
projected to ``RunObservation``, whose reachable fields are ints, bools and
closed ``Literal``s and nothing else, so the model that writes an entry's
``rationale`` has never seen a question, an answer, a document title, a
notebook name or an id. See ``retrieval_experience_projection.py`` — that
module is the boundary, this one is its only consumer.

Because of that, the rule for this file is short and absolute: it may read a
run only through ``project_run``, and it may never reach for the ask/answer
stores itself. A privacy guard scans this module and the projection module
TOGETHER for exactly that reason — moving a forbidden read from one to the
other must not help.

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
  support, updated_at)`` first) — so an entry that has already accumulated
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
from typing import Any, Mapping, Sequence

from app.repositories.ports import (
    RETRIEVAL_EXPERIENCE_BATCH_RUNS,
    RETRIEVAL_EXPERIENCE_BATCH_STEPS,
    RETRIEVAL_EXPERIENCE_MAX_ENTRIES,
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


class RetrievalExperienceDistillationService:
    """Global threshold gate, in-process single flight, one bounded call.

    Backend-neutral by construction (ports and plain callables only), so it
    lives on the neutral repository runtime beside its P1 sibling.
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
        # ⚠ The threshold counter is PROCESS-LOCAL and resets on restart.
        # Registered, not overlooked: the library is a pure increment, so a
        # restart costs at most one skipped distillation round and never
        # correctness. Persisting it would mean either a table of its own for a
        # single integer, or squeezing a deployment-wide counter into a
        # per-notebook table — the second is how a global fact ends up
        # attributed to whichever notebook happened to be first.
        self._lock = threading.Lock()
        self._pending = 0
        self._running = False

    # ------------------------------------------------------------- triggering
    def note_ask_completed(self) -> None:
        """One ask finished somewhere in this deployment.

        ⚠ FAIL-OPEN IN FULL, and it hangs off a hook that fires AFTER an answer
        has already been delivered: a delivered answer must never be affected by
        a background bookkeeping failure. Every ordinary exception is logged and
        swallowed; ``KeyboardInterrupt``/``SystemExit`` keep propagating.

        Takes no notebook and no user argument — not because they are
        unavailable at the call site, but because this chain must not have them.
        A trigger that knew whose ask it was would be one refactor away from
        being a trigger that recorded it.
        """
        try:
            if not distillation_wiring_active(self.settings, self.experiences):
                return
            trigger = max(1, int(
                getattr(self.settings, "retrieval_experience_trigger", 40)
            ))
            with self._lock:
                self._pending += 1
                if self._pending < trigger:
                    return
                # codex #524 R1/R2/R3 P2 三轮收敛出的形态:单飞认领与快照消费
                # 必须在**同一个**临界区完成——消费晚于认领哪怕一个锁间隙,
                # 快 worker 就可能已经跑完并释放槽位,并发的下一次完成会看到
                # 「阈值仍未被消费」而排出第二个重复批次(双份模型账单)。
                # busy 时不消费(保留整批,下一次完成补触发);提交失败在
                # _submit_claimed 里恢复计数并释放槽位。
                if self._running:
                    return
                self._running = True
                snapshot = self._pending
                self._pending = 0
            self._submit_claimed(snapshot)
        except Exception:  # noqa: BLE001 — never break a delivered answer
            _log.exception("retrieval experience trigger failed")

    def _maybe_requeue(self) -> None:
        """Re-arm a full pending batch left behind by a busy window.

        Same critical-section shape as ``note_ask_completed`` — claim and
        consume atomically, submit outside the lock. Fail-open: a stranded
        batch is an optimization loss, never an error surface.
        """
        try:
            trigger = max(1, int(
                getattr(self.settings, "retrieval_experience_trigger", 40)
            ))
            with self._lock:
                if self._running or self._pending < trigger:
                    return
                self._running = True
                snapshot = self._pending
                self._pending = 0
            self._submit_claimed(snapshot)
        except Exception:  # noqa: BLE001 — requeue is best-effort
            _log.exception("retrieval experience requeue failed")

    def _submit_claimed(self, snapshot: int) -> None:
        """Submit the worker for a claim ALREADY taken by the caller.

        The caller consumed ``snapshot`` pending completions inside the same
        critical section that set ``_running`` — on a submission failure both
        must be restored, or the signals are lost and the slot is stranded.
        """
        try:
            background_jobs.submit(
                self.run,
                name="retrievalexperience-global",
                notify_pending=False,
            )
        except BaseException:
            with self._lock:
                self._running = False
                self._pending += snapshot
            self._emit("failed", latency_ms=0, reason="job_submission_failed")
            raise

    def start(self) -> bool:
        """Claim the single-flight slot and submit the worker; ``False`` = busy.

        The claim happens HERE, before the thread exists — the same order as
        the agent-profile chains and ``catalog_job``: a claim taken inside the
        worker leaves a window in which a second trigger schedules a second
        writer over the same table. A submit failure releases it on the spot,
        because a stranded in-process flag is held until the process dies.

        ⚠ This method does not consult ``distillation_wiring_active``: it is
        the shared entry point, and each caller gates itself
        (``note_ask_completed`` checks before counting). A future manual
        "distil now" control must check at its own layer — a caller that
        believes this method self-gates is how a disabled feature keeps
        running.
        """
        with self._lock:
            if self._running:
                return False
            self._running = True
        try:
            background_jobs.submit(
                self.run,
                name="retrievalexperience-global",
                notify_pending=False,
            )
        except BaseException:
            with self._lock:
                self._running = False
            self._emit("failed", latency_ms=0, reason="job_submission_failed")
            raise
        return True

    # --------------------------------------------------------------- the run
    def run(self) -> None:
        """One distillation batch. Never raises to the job runner.

        Reads the deployment's most recent completed asks, aggregates them by
        situation, shows the model the busiest situations alongside the entries
        the library already holds for similar ones, and applies whatever comes
        back — after validating it against the closed vocabularies, which is
        where a malformed reply dies.

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

        def latency_ms() -> int:
            return int((time.monotonic() - started) * 1000)

        with self._lock:
            claimed_here = self._running

        try:
            if not distillation_wiring_active(self.settings, self.experiences):
                self._emit("skipped", latency_ms=latency_ms(), reason="disabled")
                return
            if self.ask_state is None:
                self._emit("skipped", latency_ms=latency_ms(), reason="not_wired")
                return
            if not self.models.configured(RETRIEVAL_EXPERIENCE_WORKLOAD):
                # Checked before the read: an unconfigured deployment should
                # pay nothing to learn it is unconfigured.
                self._emit(
                    "skipped", latency_ms=latency_ms(), reason="model_unavailable"
                )
                return
            outcome = self._distill()
            self._emit("done", latency_ms=latency_ms(), **outcome)
        except BaseException as exc:  # noqa: BLE001 — see docstring
            self._emit(
                "failed",
                latency_ms=latency_ms(),
                reason=type(exc).__name__,
            )
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            _log.exception("retrieval experience distillation failed")
        finally:
            if claimed_here:
                with self._lock:
                    self._running = False
                # codex #524 R4 P2:释放后原子复查积压——busy 期间攒满的整批
                # 若得不到后续流量会永远滞留。复用与 note_ask_completed 同一
                # 临界区形态(认领+消费原子),fail-open。
                self._maybe_requeue()

    def _distill(self) -> dict:
        runs = self._observe()
        if not runs:
            return {"runs": 0, "situations": 0, "written": 0, "evicted": 0}
        groups = _group_by_situation(runs)
        if not groups:
            return {
                "runs": len(runs), "situations": 0, "written": 0, "evicted": 0,
            }
        existing = self.experiences.read_all(RETRIEVAL_EXPERIENCE_MAX_ENTRIES)
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
                    experience_id(situation, entry["action"]),
                    situation=situation,
                    action=entry["action"],
                    polarity=entry["polarity"],
                    rationale=entry["rationale"],
                    provenance=entry["provenance"],
                    provenance_max=RETRIEVAL_EXPERIENCE_PROVENANCE_MAX,
                    replace_conclusion=entry["replace"],
                )
                written += 1
        finally:
            evicted = self.experiences.evict_to_limit(
                RETRIEVAL_EXPERIENCE_MAX_ENTRIES
            )
        return {
            "runs": len(runs),
            "situations": len(groups),
            "written": written,
            "evicted": evicted,
        }

    def _observe(self) -> list[ObservedRun]:
        """The chain's ENTIRE view of the deployment: one bounded read, then
        the projection.

        ⚠ ``project_run`` is not a convenience here — it is the only way a run
        may enter this module. Reading any other field off these rows, or
        reaching for a different store, would defeat the structural guarantee
        that makes a deployment-global table safe at all.

        ⚠ The batch size is capped by ``RETRIEVAL_EXPERIENCE_BATCH_RUNS``, and
        the fact that it does not exceed ``RETRIEVAL_EXPERIENCE_PROVENANCE_MAX``
        is an INVARIANT rather than a coincidence: an entry remembers at most
        that many run ids, and re-observing a run it has forgotten counts that
        run's support a second time. A test pins the relationship.
        """
        rows = self.ask_state.recent_completed_ask_runs(
            job_limit=RETRIEVAL_EXPERIENCE_BATCH_RUNS,
            step_limit=RETRIEVAL_EXPERIENCE_BATCH_STEPS,
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
        """Counts only — never a rationale, never an action word, never an id.

        ``rationale`` is model-written prose and the event log is a different
        disclosure surface from the table it lives in; ``action`` and the
        situation values are excluded for a subtler reason — a stream of events
        carrying (situation, action) pairs beside their timestamps would let an
        operator reconstruct which shapes of question the deployment is
        currently seeing, which is the aggregate this feature is careful not to
        publish anywhere else.
        """
        try:
            self.event_log.emit(
                {
                    "kind": "retrieval_experience_distilled",
                    "status": status,
                    "latency_ms": int(latency_ms),
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
    * ``rationale`` is non-empty, within the character cap, and contains no
      id-shaped token. Over-length is a rejection rather than a clip: a
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
        rationale = " ".join(str(item.get("rationale") or "").split())
        if not rationale or len(rationale) > RETRIEVAL_EXPERIENCE_RATIONALE_MAX_CHARS:
            continue
        if _ID_SHAPE.search(rationale):
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
