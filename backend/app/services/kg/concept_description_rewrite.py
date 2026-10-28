"""``kg rewrite-descriptions``: rewrite one notebook's concept descriptions
under the description model and prompt in force now (user ruling 2026-10-28,
option b: an explicit maintenance command, run when the operator chooses; the
description signature and the rebuild's automatic reuse stay as they are).

Why a command and not a signature change: a fused description is reused by
the signature of its input (canonical name + quote set), which leaves out the
model and the prompt, so after either changes nothing rewrites a canonical
whose name and quotes did not move. Folding them into the signature would
make every rebuild after a model change re-describe the whole library; the
command makes that bill explicit and schedulable instead.

Three phases, per notebook:

1. **Plan** (reads only). One keyset pass over the published concept slice
   computes each multi-member canonical's input exactly as the rebuild's
   description stage does (``app.domain.concept_description_rewrite``: the
   same quote normalization, the same signature), against what is staged
   under the current *origin* -- the description model label plus a
   fingerprint of the prompt template (``rewrite_checkpoint_version``).
   A canonical is up to date when every one of its rows already carries the
   staged text and signature; an input with nothing staged is a model call.
   ``--dry-run`` stops here.
2. **Generate**. One model call per distinct input not yet staged (two
   canonicals with the same name and quotes share one), in parallel up to the
   bound service's width, each result staged in ``kg_rebuild_checkpoint``
   (stage ``concept_desc_rewrite``, version = origin, key = signature) every
   ``_FLUSH`` results. A staged input is never sent to the model again under
   the same origin, so an interrupted or limited run resumes where it stopped.
   A failed call (exception, non-JSON, empty description) is logged by class
   name only and counted; the run goes on.
3. **Publish**. Under the notebook's derived-generation claim (the rebuild's
   cross-process single flight: no rebuild can be between reading the old
   descriptions and publishing them while this runs, and a rebuild that
   starts afterwards reuses the new texts by signature), page by page in
   short write transactions: the concept slice's lock, the published
   generation and reset epoch re-checked against the plan, the rows of the
   page's canonicals read again (on PostgreSQL ``FOR UPDATE ... SKIP
   LOCKED``), each canonical's input recomputed from what is held, and the
   staged text written only where the input still equals the one it was
   generated from. ``cluster_mutation_seq`` moves in the same transaction when
   a row changed (a published-generation write, ``kg_mutation``'s two-tier
   rule); ``kg_mutation_seq`` does not (clustering inputs did not change), and
   the in-process KG caches are dropped after the last page.

Runs beside the serving process: phases 1-2 take no lock at all, phase 3 holds
the claim for seconds per notebook. A rebuild already in flight makes the
publish report ``busy`` (rerun later; nothing is generated twice); a rebuild
that published a new generation since the plan makes it re-plan.
"""
from __future__ import annotations

import concurrent.futures as cf
import contextvars
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from app.domain.concept_description_rewrite import (
    CONCEPT_DESC_REWRITE_STAGE,
    description_quotes,
    evidence_quotes,
    prompt_fingerprint,
    rewrite_checkpoint_version,
)

WORKLOAD = "kg_concept_description"
#: Rows per plan page (one bounded index range scan each).
_PLAN_PAGE_ROWS = 5000
#: Canonicals per publish transaction.
_PUBLISH_PAGE = 200
#: Staged results per checkpoint write (a kill loses at most this many calls).
_FLUSH = 16
#: Re-plans when a rebuild published a new generation between plan and publish.
_MAX_PLANS = 3

Progress = Optional[Callable[[str, int, int], None]]


class ConceptDescriptionRewriteUnavailable(RuntimeError):
    """The description stage is switched off or its workload is not bound."""


@dataclass
class ConceptDescriptionRewriteReport:
    """What one notebook's run did. Counts only, never text."""

    notebook_id: str
    status: str = "done"  # done | incomplete | busy | dry_run
    version: str = ""
    canonicals: int = 0   # multi-member canonicals with quotes (the rebuild's set)
    up_to_date: int = 0   # already carry the current origin's text
    to_rewrite: int = 0   # canonicals - up_to_date, at plan time
    model_calls: int = 0  # calls this run makes (dry run: would make)
    generated: int = 0
    failed: int = 0
    remaining: int = 0    # inputs left for a later run by the limit
    rewritten: int = 0    # canonicals whose rows changed at publish
    moved: int = 0        # skipped at publish: the input changed since the plan
    plans: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class _Plan:
    generation: int
    epoch: int
    #: (canonical_id, signature, up_to_date) for every eligible canonical.
    items: List[Tuple[str, str, bool]] = field(default_factory=list)
    #: signature -> (first canonical_id, name, quotes) for inputs not staged.
    pending: Dict[str, Tuple[str, str, List[str]]] = field(default_factory=dict)


class _Canonical:
    """Rows of one canonical as the plan pass meets them."""

    __slots__ = ("cid", "names", "descriptions", "sigs", "live", "spans")

    def __init__(self, cid: str) -> None:
        self.cid = cid
        self.names: set = set()
        self.descriptions: set = set()
        self.sigs: set = set()
        self.live = 0
        self.spans: List[str] = []


class ConceptDescriptionRewrite:
    """One run of the command over one notebook; ``host`` is the
    ``KnowledgeLifecycleService`` (store, transactions, model clients, the
    cluster-seq bump and the cache invalidation it already owns)."""

    def __init__(self, host: Any, *, signature: Callable[[str, List[str]], str]) -> None:
        self.host = host
        self.store = host.unified_kg
        self.signature = signature

    # ------------------------------------------------------------- entry
    def run(
        self, notebook_id: str, *, dry_run: bool = False,
        limit: Optional[int] = None, progress: Progress = None,
    ) -> ConceptDescriptionRewriteReport:
        host = self.host
        if not host.settings.kg_concept_desc_enabled:
            raise ConceptDescriptionRewriteUnavailable(
                "KG_CONCEPT_DESC_ENABLED=false: the description stage is switched off")
        if not host.model_clients.configured(WORKLOAD):
            raise ConceptDescriptionRewriteUnavailable(
                f"no model service is bound to the {WORKLOAD} workload")
        from app.services.prompts import CONCEPT_DESC_SCHEMA_HINT, concept_description_prompt

        client = host.model_clients.chat(WORKLOAD)
        version = rewrite_checkpoint_version(
            str(getattr(client, "model", "") or ""),
            prompt_fingerprint(
                concept_description_prompt("\x00name\x00", "\x00evidence\x00"),
                CONCEPT_DESC_SCHEMA_HINT),
        )
        report = ConceptDescriptionRewriteReport(notebook_id=notebook_id, version=version)
        staged = self.store.checkpoint_load(notebook_id, version, CONCEPT_DESC_REWRITE_STAGE)
        budget = None if limit is None else max(0, int(limit))
        plan = None
        for attempt in range(_MAX_PLANS):
            plan = self._plan(notebook_id, staged)
            report.plans = attempt + 1
            if attempt == 0:
                report.canonicals = len(plan.items)
                report.up_to_date = sum(1 for _c, _s, done in plan.items if done)
                report.to_rewrite = report.canonicals - report.up_to_date
            pending = list(plan.pending.items())
            take = pending if budget is None else pending[:budget]
            report.remaining = len(pending) - len(take)
            if dry_run:
                report.model_calls = len(take)
                report.status = "dry_run"
                return report
            report.model_calls += len(take)
            if budget is not None:
                budget -= len(take)
            generated, failed = self._generate(
                notebook_id, version, take, staged, client, progress)
            report.generated += generated
            report.failed += failed
            outcome, rewritten, moved = self._publish(notebook_id, plan, staged, progress)
            report.rewritten += rewritten
            report.moved = moved
            if outcome != "moved":
                break
        else:
            outcome = "moved"
        if outcome == "busy":
            report.status = "busy"
        elif outcome != "done" or report.failed or report.remaining or report.moved:
            report.status = "incomplete"
        else:
            report.status = "done"
            wanted = {sig for _cid, sig, _done in plan.items}
            self.store.checkpoint_prune_stage(
                notebook_id, CONCEPT_DESC_REWRITE_STAGE, version,
                sorted(key for key in staged if key not in wanted))
        host.event_log.emit({"kind": "kg_concept_description_rewrite", **report.as_dict()})
        return report

    # -------------------------------------------------------------- plan
    def _plan(self, notebook_id: str, staged: Dict[str, dict]) -> _Plan:
        host = self.host
        with host._connect() as db:
            state = self.store.state_row(db, notebook_id)
        generation = int(state["cluster_generation"]) if state is not None else 0
        epoch = int(state["kg_reset_epoch"] or 0) if state is not None else 0
        plan = _Plan(generation=generation, epoch=epoch)
        after: Optional[Tuple[str, str]] = None
        current: Optional[_Canonical] = None
        while True:
            with host._connect() as db:
                rows = self.store.concept_description_member_page(
                    db, notebook_id, generation, after=after, limit=_PLAN_PAGE_ROWS)
            for row in rows:
                cid = str(row["canonical_id"])
                if current is None or current.cid != cid:
                    if current is not None:
                        self._plan_canonical(plan, current, staged)
                    current = _Canonical(cid)
                current.names.add(row["canonical_name"] or "")
                current.descriptions.add(row["canonical_description"] or "")
                current.sigs.add(row["canonical_desc_sig"] or "")
                if int(row["live"]):
                    current.live += 1
                    current.spans.extend(evidence_quotes(row["evidence"]))
            if len(rows) < _PLAN_PAGE_ROWS:
                break
            after = (str(rows[-1]["canonical_id"]), str(rows[-1]["member_object_id"]))
        if current is not None:
            self._plan_canonical(plan, current, staged)
        return plan

    def _input(self, names: set, live: int, spans: List[str]) -> Optional[Tuple[str, List[str], str]]:
        """The canonical's description input, or None when the rebuild would
        not describe it (fewer than two live members, or no quote)."""
        if live < 2:
            return None
        quotes = description_quotes(spans)
        if not quotes:
            return None
        name = min(names) if names else ""
        return name, quotes, self.signature(name, quotes)

    def _plan_canonical(self, plan: _Plan, acc: _Canonical, staged: Dict[str, dict]) -> None:
        found = self._input(acc.names, acc.live, acc.spans)
        if found is None:
            return
        name, quotes, sig = found
        text = (staged.get(sig) or {}).get("description") or ""
        done = bool(text) and acc.descriptions == {text} and acc.sigs == {sig}
        plan.items.append((acc.cid, sig, done))
        if not text:
            plan.pending.setdefault(sig, (acc.cid, name, quotes))

    # ---------------------------------------------------------- generate
    def _generate(
        self, notebook_id: str, version: str,
        work: Sequence[Tuple[str, Tuple[str, str, List[str]]]],
        staged: Dict[str, dict], client: Any, progress: Progress,
    ) -> Tuple[int, int]:
        if not work:
            return 0, 0
        from app.services.prompts import CONCEPT_DESC_SCHEMA_HINT, concept_description_prompt

        host = self.host

        def generate(item):
            sig, (cid, name, quotes) = item
            block = "\n".join(f"- {q}" for q in quotes)
            try:
                raw = client.chat_json(
                    [{"role": "user", "content": concept_description_prompt(name, block)}],
                    CONCEPT_DESC_SCHEMA_HINT)
                value = json.loads(raw).get("description")
            except Exception as exc:  # noqa: BLE001 -- fail-soft per input, logged below
                return cid, sig, "", type(exc).__name__
            text = value.strip() if isinstance(value, str) else ""
            return cid, sig, text, "" if text else "EmptyDescription"

        generated = failed = done = 0
        buffer: List[Tuple[str, dict]] = []
        workers = max(1, min(host.model_clients.parallelism(WORKLOAD), len(work)))
        pool = cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="kg-desc-rewrite")
        try:
            futures = [pool.submit(contextvars.copy_context().run, generate, item) for item in work]
            for future in cf.as_completed(futures):
                cid, sig, text, error = future.result()
                done += 1
                if text:
                    generated += 1
                    staged[sig] = {"description": text}
                    buffer.append((sig, {"description": text}))
                else:
                    failed += 1
                    # Ids and the class name only: neither the prompt nor the
                    # reply (both carry source text) reaches the log.
                    host.event_log.logger.warning(
                        "concept description rewrite failed: notebook=%s canonical=%s error=%s",
                        notebook_id, cid, error)
                if len(buffer) >= _FLUSH:
                    self._stage(notebook_id, version, buffer)
                    buffer = []
                if progress is not None:
                    progress("generate", done, len(work))
        finally:
            # An interrupt (Ctrl-C) or a failed write keeps every result that
            # already came back: pending calls are cancelled, the finished
            # ones are staged, so the next run starts after them.
            pool.shutdown(wait=False, cancel_futures=True)
            if buffer:
                self._stage(notebook_id, version, buffer)
        return generated, failed

    def _stage(self, notebook_id: str, version: str, rows: List[Tuple[str, dict]]) -> None:
        self.store.checkpoint_put(
            notebook_id, version, CONCEPT_DESC_REWRITE_STAGE, list(rows), self.host._now())

    # ----------------------------------------------------------- publish
    def _publish(
        self, notebook_id: str, plan: _Plan, staged: Dict[str, dict], progress: Progress,
    ) -> Tuple[str, int, int]:
        """Returns ``(outcome, rewritten, moved)``; outcome is ``done``,
        ``busy`` (a rebuild holds the claim), ``moved`` (a new generation was
        published since the plan: re-plan) or ``claim_lost`` (the claim was
        reset under the run -- a KG delete, a server restart)."""
        targets = [
            (cid, sig, staged[sig]["description"])
            for cid, sig, done in plan.items
            if not done and sig in staged
        ]
        if not targets:
            return "done", 0, 0
        host = self.host
        with host._write() as db:
            claim = self.store.claim_derived_generation(
                db, notebook_id, ttl_seconds=host.settings.kg_derived_build_ttl_seconds)
        if claim is None:
            return "busy", 0, 0
        generation = int(claim["generation"])
        rewritten = moved = 0
        outcome = "done"
        try:
            if claim["catchup_from"]:
                # A previous rebuild crashed after its flip: settle its debt
                # first, as the rebuild and its skip path do.
                host._settle_generation_catchup(
                    notebook_id, published_generation=int(claim["cluster_generation"]),
                    since_ts=claim["catchup_from"])
            for start in range(0, len(targets), _PUBLISH_PAGE):
                page = targets[start:start + _PUBLISH_PAGE]
                outcome, page_rewritten, page_moved = self._publish_page(
                    notebook_id, plan, generation, page)
                rewritten += page_rewritten
                moved += page_moved
                if outcome != "done":
                    break
                if progress is not None:
                    progress("publish", min(start + len(page), len(targets)), len(targets))
        finally:
            with host._write() as db:
                self.store.release_derived_claim(db, notebook_id, generation)
            if rewritten:
                host._invalidate_unified_cache(notebook_id)
        return outcome, rewritten, moved

    def _publish_page(
        self, notebook_id: str, plan: _Plan, claim_generation: int,
        page: Sequence[Tuple[str, str, str]],
    ) -> Tuple[str, int, int]:
        host = self.host
        rewritten = moved = 0
        with host._write() as db:
            if self.store.lock_published_concept_clusters(db, notebook_id) != (
                    plan.generation, plan.epoch):
                return "moved", 0, 0
            # The state-row write comes before any cluster row is locked, so
            # this transaction never waits on a row lock while holding one.
            if not self.store.refresh_derived_claim(db, notebook_id, claim_generation):
                return "claim_lost", 0, 0
            rows = self.store.concept_description_publish_rows(
                db, notebook_id, plan.generation, [cid for cid, _sig, _text in page])
            held: Dict[str, Tuple[List[str], _Canonical]] = {}
            for row in rows:
                cid = str(row["canonical_id"])
                ids, acc = held.setdefault(cid, ([], _Canonical(cid)))
                ids.append(str(row["id"]))
                acc.names.add(row["canonical_name"] or "")
                if int(row["live"]):
                    acc.live += 1
                    acc.spans.extend(evidence_quotes(row["evidence"]))
            changed = 0
            for cid, sig, text in page:
                entry = held.get(cid)
                found = None if entry is None else self._input(
                    entry[1].names, entry[1].live, entry[1].spans)
                if found is None or found[2] != sig:
                    moved += 1
                    continue
                count = self.store.rewrite_concept_description(
                    db, notebook_id, entry[0], text, sig)
                if count:
                    rewritten += 1
                    changed += count
            if changed:
                # The rebuild reuses its own description checkpoint before the
                # published texts: dropped in the same commit, or the next
                # rebuild whose input version did not move would restore the
                # old texts. No rebuild is in flight (the claim); the entries
                # a crashed rebuild left behind are only a resume aid, and the
                # next rebuild reuses the published texts by signature anyway.
                self.store.drop_description_checkpoints(db, notebook_id)
                host._bump_cluster_mutation_seq(db, notebook_id)
        return "done", rewritten, moved
