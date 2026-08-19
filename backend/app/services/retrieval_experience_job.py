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

* **Second-granular ABA on the injection-side memo.** The injection side
  (``reasoning_retrieval.py``) memoises the rendered block against
  ``RetrievalExperienceStorePort.version_signal()`` — ``(row count,
  MAX(updated_at))``. SQLite's clock is second-granular (the same fact
  ``memory_revisions`` registered before this feature existed), so a batch
  that, within the SAME second, evicts as many rows as it writes and happens
  to leave both halves of the signature unchanged would be invisible to the
  memo. In practice this is UNREACHABLE at the cadence this chain actually
  runs at (once every ``RETRIEVAL_EXPERIENCE_TRIGGER`` completed asks
  deployment-wide, never inside a single request), and even if it were hit,
  it is SELF-HEALING: the mismatch can only persist for the remainder of that
  one second, because any later write — including the very next distillation
  batch, whenever it happens — lands in a different second and moves the
  signature. Not worth a table just to close a window nothing can open in
  practice and that heals itself if it somehow did.
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
            # codex #524 R1 P2: the counter is reset only AFTER a worker was
            # actually scheduled. Resetting before ``start()`` lost the whole
            # batch whenever the single-flight slot was busy — completions
            # arriving during a model call were neither in that batch nor
            # retained, so sustained traffic permanently skipped groups of
            # runs. Kept-on-decline means the very next completion retries,
            # which lands as soon as the in-flight worker settles.
            if self.start():
                with self._lock:
                    self._pending = 0
        except Exception:  # noqa: BLE001 — never break a delivered answer
            _log.exception("retrieval experience trigger failed")

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
        parsed = parse_distillation_reply(safe_json(raw), groups)
        written = 0
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
        evicted = self.experiences.evict_to_limit(RETRIEVAL_EXPERIENCE_MAX_ENTRIES)
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

class _SituationGroup:
    """One question shape plus everything the batch saw under it."""

    __slots__ = (
        "key", "situation", "runs", "runs_with_actions", "run_ids",
        "citations", "actions",
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
        self.actions: dict[str, list[int]] = {}

    def absorb(self, run: ObservedRun) -> None:
        self.runs += 1
        self.run_ids.append(run.run_id)
        self.citations += run.observation.citations
        if run.observation.actions:
            self.runs_with_actions += 1
        for action in run.observation.actions:
            tally = self.actions.setdefault(action.action, [0, 0])
            tally[0] += action.invocations
            tally[1] += action.zero_hits


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
        for _score, entry_id, entry in scored[:_MAX_SIMILAR_ENTRIES]:
            if entry_id in seen:
                continue
            seen.add(entry_id)
            offered.append((index, entry))
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
            lines.append(
                f"  {action}: used={tally[0]} came_back_empty={tally[1]}"
            )
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
    payload: object, groups: Sequence[_SituationGroup]
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
    * ``action`` matches ``RETRIEVAL_ACTIONS`` exactly. No prefix matching and
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
    by_key = {f"s{index}": group for index, group in enumerate(groups)}
    accepted: list[dict] = []
    for item in entries:
        if not isinstance(item, Mapping):
            continue
        op = str(item.get("op") or "").strip().upper()
        if op not in _OPS or op == "NOOP":
            continue
        group = by_key.get(str(item.get("situation") or "").strip().lower())
        if group is None:
            continue
        action = str(item.get("action") or "").strip()
        if action not in RETRIEVAL_ACTIONS:
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
        accepted.append(
            {
                "situation": situation,
                "action": action,
                "polarity": polarity,
                "rationale": rationale,
                "provenance": list(group.run_ids),
                # ADD landing on an existing entry must not rewrite its
                # conclusion — the model said "the library does not hold this
                # yet", so it was not reasoning about what is stored there.
                # Only an explicit UPDATE replaces polarity and rationale.
                "replace": op == "UPDATE",
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
