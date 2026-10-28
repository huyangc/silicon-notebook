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
   ``--dry-run`` stops here. A notebook whose last run completed is not read
   again while nothing in it changed (the completion record, below).
2. **Generate**. One model call per distinct input not yet staged (two
   canonicals with the same name and quotes share one), in parallel up to the
   bound service's width, each result staged in ``kg_rebuild_checkpoint``
   (stage ``concept_desc_rewrite``, version = origin, key = signature) every
   ``_FLUSH`` results. A staged input is never sent to the model again under
   the same origin, so an interrupted or limited run resumes where it stopped.
   A failed call (exception, non-JSON, empty description) is logged by input
   number, signature prefix and class name only, and counted; the run goes on.
   An interrupt does not wait for calls in flight: the finished results are
   staged and the interrupt propagates at once.
3. **Publish**. Under the notebook's derived-generation claim (the rebuild's
   cross-process single flight: no rebuild can be between reading the old
   descriptions and publishing them while this runs, and a rebuild that
   starts afterwards reuses the new texts by signature). The rebuild's own
   description checkpoint is dropped once, before the first write: the
   rebuild reuses it before the published texts, so otherwise the next
   rebuild whose input version did not move would put the old texts back.
   Then short write transactions of at most ``_PUBLISH_PAGE_ROWS`` cluster
   rows each: the concept slice's lock, the published generation and reset
   epoch re-checked against the plan, the claim renewed (the state-row write
   comes before any cluster row is locked), the page's rows read again (on
   PostgreSQL ``FOR UPDATE ... SKIP LOCKED``: never waiting on a row someone
   else holds), each canonical's input recomputed from what is held -- and a
   canonical written only when it holds every one of its rows (an unlocked
   count in the same transaction) and its input still equals the one the
   text was generated from; anything else is counted as ``moved`` and the
   next run picks it up. ``cluster_mutation_seq`` moves in the same
   transaction when a row changed (a published-generation write,
   ``kg_mutation``'s two-tier rule); ``kg_mutation_seq`` does not.
   A canonical with more rows than one page (a hub concept) is verified
   without holding any lock -- the state row's (generation, epoch,
   kg_mutation_seq, cluster_mutation_seq) read FIRST, then its rows -- and
   written in row batches, each its own short transaction that re-reads the
   state under the claim and writes only while it is exactly the verified
   state plus this run's own bumps (every write to these rows, their objects
   or the cluster slice moves one of the two sequences, ``kg_mutation``'s
   census). A batch that finds the state moved, or cannot lock all of its
   rows, stops the hub there (``moved``): its rows may then carry two texts
   until the next run, each a description the model wrote for that
   canonical.

Completion record: a run that ends ``done`` stores the state row's four
values (as read before the plan, plus exactly the bumps this run made) under
the same origin. While they are unchanged nothing a description depends on
changed, so the next run (or dry run) answers from the record without reading
the slice; any write in between invalidates it, and the run plans as usual.

Runs beside the serving process: phases 1-2 take no lock at all, phase 3 holds
the claim for seconds per notebook. A rebuild already in flight makes the
publish report ``busy`` (rerun later; nothing is generated twice); a rebuild
that published a new generation since the plan makes it re-plan.
"""
from __future__ import annotations

import contextvars
import json
import queue
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from app.domain.concept_description_rewrite import (
    CONCEPT_DESC_REWRITE_STAGE,
    REBUILD_DESC_CHECKPOINT_STAGE,
    description_quotes,
    evidence_quotes,
    prompt_fingerprint,
    rewrite_checkpoint_version,
)

WORKLOAD = "kg_concept_description"
#: Rows per plan page (one bounded index range scan each).
_PLAN_PAGE_ROWS = 5000
#: Cluster rows per publish transaction; a canonical with more rows than this
#: (a hub) is written in batches of this many rows.
_PUBLISH_PAGE_ROWS = 2000
#: Staged results per checkpoint write (a kill loses at most this many calls).
_FLUSH = 16
#: Re-plans when a rebuild published a new generation between plan and publish.
_MAX_PLANS = 3
#: Item key of the completion record (signatures are 40 hex characters).
_COMPLETE_KEY = "~complete"

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
    plans: int = 0        # 0 = answered from the completion record

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class _Plan:
    generation: int
    epoch: int
    #: (canonical_id, signature, up_to_date, row count) per eligible canonical.
    items: List[Tuple[str, str, bool, int]] = field(default_factory=list)
    #: signature -> (first canonical_id, name, quotes) for inputs not staged.
    pending: Dict[str, Tuple[str, str, List[str]]] = field(default_factory=dict)


class _Canonical:
    """Rows of one canonical as a pass meets them."""

    __slots__ = ("cid", "names", "descriptions", "sigs", "rows", "live", "spans")

    def __init__(self, cid: str) -> None:
        self.cid = cid
        self.names: set = set()
        self.descriptions: set = set()
        self.sigs: set = set()
        self.rows = 0
        self.live = 0
        self.spans: List[str] = []

    def add(self, row: Any) -> None:
        self.rows += 1
        self.names.add(row["canonical_name"] or "")
        if int(row["live"]):
            self.live += 1
            self.spans.extend(evidence_quotes(row["evidence"]))


def _state_key(state: Any) -> Tuple[int, int, int, int]:
    """(cluster_generation, kg_reset_epoch, kg_mutation_seq,
    cluster_mutation_seq) of a state row; no row reads as zeros."""
    if state is None:
        return 0, 0, 0, 0
    return (int(state["cluster_generation"] or 0), int(state["kg_reset_epoch"] or 0),
            int(state["kg_mutation_seq"] or 0), int(state["cluster_mutation_seq"] or 0))


class ConceptDescriptionRewrite:
    """One run of the command over one notebook; ``host`` is the
    ``KnowledgeLifecycleService`` (store, transactions, model clients, the
    cluster-seq bump and the cache invalidation it already owns)."""

    def __init__(self, host: Any, *, signature: Callable[[str, List[str]], str]) -> None:
        self.host = host
        self.store = host.unified_kg
        self.signature = signature
        self._bumps = 0
        self._recordable = True

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
        record = staged.pop(_COMPLETE_KEY, None)
        start_state = self._state(notebook_id)
        if record is not None and tuple(record.get("state") or ()) == start_state:
            report.canonicals = report.up_to_date = int(record.get("canonicals") or 0)
            report.status = "dry_run" if dry_run else "done"
            if not dry_run:
                host.event_log.emit({"kind": "kg_concept_description_rewrite",
                                     **report.as_dict()})
            return report
        budget = None if limit is None else max(0, int(limit))
        plan = None
        for attempt in range(_MAX_PLANS):
            plan = self._plan(notebook_id, staged)
            report.plans = attempt + 1
            if attempt == 0:
                report.canonicals = len(plan.items)
                report.up_to_date = sum(1 for item in plan.items if item[2])
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
            wanted = {item[1] for item in plan.items}
            self.store.checkpoint_prune_stage(
                notebook_id, CONCEPT_DESC_REWRITE_STAGE, version,
                sorted(key for key in staged if key not in wanted))
            self._record_completion(notebook_id, version, start_state, report)
        host.event_log.emit({"kind": "kg_concept_description_rewrite", **report.as_dict()})
        return report

    def _state(self, notebook_id: str) -> Tuple[int, int, int, int]:
        with self.host._connect() as db:
            return _state_key(self.store.state_row(db, notebook_id))

    def _record_completion(
        self, notebook_id: str, version: str, start: Tuple[int, int, int, int],
        report: ConceptDescriptionRewriteReport,
    ) -> None:
        """Store the completion record only when the state now is exactly the
        one the plan started from plus this run's own bumps: any other write
        in between (an append, an evidence edit, a rebuild) means the plan may
        not describe the notebook as it is now, so nothing is recorded."""
        now = self._state(notebook_id)
        expected = (start[0], start[1], start[2], start[3] + self._bumps)
        if not self._recordable or report.plans != 1 or now != expected:
            return
        self._stage(notebook_id, version, [(_COMPLETE_KEY, {
            "state": list(now), "canonicals": report.canonicals})])

    # -------------------------------------------------------------- plan
    def _plan(self, notebook_id: str, staged: Dict[str, dict]) -> _Plan:
        host = self.host
        generation, epoch = self._state(notebook_id)[:2]
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
                current.add(row)
                current.descriptions.add(row["canonical_description"] or "")
                current.sigs.add(row["canonical_desc_sig"] or "")
            if len(rows) < _PLAN_PAGE_ROWS:
                break
            after = (str(rows[-1]["canonical_id"]), str(rows[-1]["member_object_id"]))
        if current is not None:
            self._plan_canonical(plan, current, staged)
        return plan

    def _input(self, acc: _Canonical) -> Optional[Tuple[str, List[str], str]]:
        """The canonical's description input, or None when the rebuild would
        not describe it (fewer than two live members, or no quote)."""
        if acc.live < 2:
            return None
        quotes = description_quotes(acc.spans)
        if not quotes:
            return None
        name = min(acc.names) if acc.names else ""
        return name, quotes, self.signature(name, quotes)

    def _plan_canonical(self, plan: _Plan, acc: _Canonical, staged: Dict[str, dict]) -> None:
        found = self._input(acc)
        if found is None:
            return
        name, quotes, sig = found
        text = (staged.get(sig) or {}).get("description") or ""
        done = bool(text) and acc.descriptions == {text} and acc.sigs == {sig}
        plan.items.append((acc.cid, sig, done, acc.rows))
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
        jobs: "queue.Queue" = queue.Queue()
        results: "queue.Queue" = queue.Queue()
        stop = threading.Event()
        for number, item in enumerate(work, 1):
            jobs.put((number, item))

        def generate(number, item):
            sig, (_cid, name, quotes) = item
            block = "\n".join(f"- {q}" for q in quotes)
            try:
                raw = client.chat_json(
                    [{"role": "user", "content": concept_description_prompt(name, block)}],
                    CONCEPT_DESC_SCHEMA_HINT)
                value = json.loads(raw).get("description")
            except Exception as exc:  # noqa: BLE001 -- fail-soft per input, logged below
                return number, sig, "", type(exc).__name__
            text = value.strip() if isinstance(value, str) else ""
            return number, sig, text, "" if text else "EmptyDescription"

        def worker(context):
            while not stop.is_set():
                try:
                    number, item = jobs.get_nowait()
                except queue.Empty:
                    return
                try:
                    results.put((context.run(generate, number, item), None))
                except BaseException as exc:  # noqa: BLE001 -- handed to the consumer
                    results.put((None, exc))
                    return

        # Daemon threads, not a ThreadPoolExecutor: an interrupt must not wait
        # for the model calls in flight (an executor's workers are joined at
        # interpreter exit); their results are lost and generated again on
        # the next run, everything that already came back is staged below.
        width = max(1, min(host.model_clients.parallelism(WORKLOAD), len(work)))
        for index in range(width):
            threading.Thread(
                target=worker, args=(contextvars.copy_context(),),
                name=f"kg-desc-rewrite-{index}", daemon=True,
            ).start()
        generated = failed = done = 0
        buffer: List[Tuple[str, dict]] = []
        try:
            while done < len(work):
                outcome, error = results.get()
                if error is not None:
                    raise error
                number, sig, text, reason = outcome
                done += 1
                if text:
                    generated += 1
                    staged[sig] = {"description": text}
                    buffer.append((sig, {"description": text}))
                else:
                    failed += 1
                    # The input's number and a signature prefix: neither the
                    # concept name (canonical ids are normalized names) nor
                    # the prompt or the reply reaches the log.
                    host.event_log.logger.warning(
                        "concept description rewrite failed: notebook=%s input=%d/%d "
                        "sig=%s error=%s", notebook_id, number, len(work), sig[:12], reason)
                if len(buffer) >= _FLUSH:
                    self._stage(notebook_id, version, buffer)
                    buffer = []
                if progress is not None:
                    progress("generate", done, len(work))
        finally:
            stop.set()
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
            (cid, sig, staged[sig]["description"], rows)
            for cid, sig, done, rows in plan.items
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
                # first, as the rebuild and its skip path do. Its appends move
                # cluster_mutation_seq, so no completion record this run.
                host._settle_generation_catchup(
                    notebook_id, published_generation=int(claim["cluster_generation"]),
                    since_ts=claim["catchup_from"])
                self._recordable = False
            # Once, before the first write, under the claim (no rebuild can
            # write it again until the claim is released).
            self.store.checkpoint_prune_stage(
                notebook_id, REBUILD_DESC_CHECKPOINT_STAGE, None, ())
            done = 0
            for page in _pages(targets):
                if len(page) == 1 and page[0][3] > _PUBLISH_PAGE_ROWS:
                    outcome, page_rewritten, page_moved = self._publish_hub(
                        notebook_id, plan, generation, page[0])
                else:
                    outcome, page_rewritten, page_moved = self._publish_page(
                        notebook_id, plan, generation, page)
                rewritten += page_rewritten
                moved += page_moved
                if outcome != "done":
                    break
                done += len(page)
                if progress is not None:
                    progress("publish", done, len(targets))
        finally:
            try:
                with host._write() as db:
                    self.store.release_derived_claim(db, notebook_id, generation)
            except Exception as exc:  # noqa: BLE001 -- the claim then waits for its TTL
                host.event_log.logger.warning(
                    "concept description rewrite could not release its claim: "
                    "notebook=%s error=%s", notebook_id, type(exc).__name__)
            if rewritten:
                host._invalidate_unified_cache(notebook_id)
        return outcome, rewritten, moved

    def _begin_page(self, db: Any, notebook_id: str, plan: _Plan, claim_generation: int) -> str:
        """The checks every publish transaction starts with: the slice lock
        and the plan's generation/epoch under it, then the claim renewed (the
        state-row write before any cluster row is locked)."""
        if self.store.lock_published_concept_clusters(db, notebook_id) != (
                plan.generation, plan.epoch):
            return "moved"
        if not self.store.refresh_derived_claim(db, notebook_id, claim_generation):
            return "claim_lost"
        return "done"

    def _publish_page(
        self, notebook_id: str, plan: _Plan, claim_generation: int,
        page: Sequence[Tuple[str, str, str, int]],
    ) -> Tuple[str, int, int]:
        host = self.host
        rewritten = moved = 0
        with host._write() as db:
            began = self._begin_page(db, notebook_id, plan, claim_generation)
            if began != "done":
                return began, 0, 0
            cids = [item[0] for item in page]
            rows = self.store.concept_description_publish_rows(
                db, notebook_id, plan.generation, cids)
            held: Dict[str, Tuple[List[str], _Canonical]] = {}
            for row in rows:
                cid = str(row["canonical_id"])
                ids, acc = held.setdefault(cid, ([], _Canonical(cid)))
                ids.append(str(row["id"]))
                acc.add(row)
            # Every row of a canonical, or none of it: a row another
            # transaction holds (SKIP LOCKED left it out) still counts here.
            totals = self.store.concept_description_row_counts(
                db, notebook_id, plan.generation, cids)
            changed = 0
            for cid, sig, text, _rows in page:
                entry = held.get(cid)
                if entry is None or len(entry[0]) != totals.get(cid, 0):
                    moved += 1
                    continue
                found = self._input(entry[1])
                if found is None or found[2] != sig:
                    moved += 1
                    continue
                count = self.store.rewrite_concept_description(
                    db, notebook_id, entry[0], text, sig)
                if count:
                    rewritten += 1
                    changed += count
            if changed:
                host._bump_cluster_mutation_seq(db, notebook_id)
                self._bumps += 1
        return "done", rewritten, moved

    def _publish_hub(
        self, notebook_id: str, plan: _Plan, claim_generation: int,
        item: Tuple[str, str, str, int],
    ) -> Tuple[str, int, int]:
        """One canonical too large for a page (see the module docstring)."""
        host = self.host
        cid, sig, text, _rows = item
        with host._connect() as db:
            certified = _state_key(self.store.state_row(db, notebook_id))
            rows = self.store.concept_description_publish_rows(
                db, notebook_id, plan.generation, [cid], lock_rows=False)
        if certified[:2] != (plan.generation, plan.epoch):
            return "moved", 0, 0
        acc = _Canonical(cid)
        ids: List[str] = []
        for row in rows:
            ids.append(str(row["id"]))
            acc.add(row)
        found = self._input(acc)
        if found is None or found[2] != sig:
            return "done", 0, 1
        bumps = 0
        for start in range(0, len(ids), _PUBLISH_PAGE_ROWS):
            batch = ids[start:start + _PUBLISH_PAGE_ROWS]
            # The verified state plus exactly this hub's own bumps so far.
            expected = (*certified[:3], certified[3] + bumps)
            with host._write() as db:
                began = self._begin_page(db, notebook_id, plan, claim_generation)
                if began != "done":
                    return began, int(bumps > 0), 0
                if _state_key(self.store.state_row(db, notebook_id)) != expected:
                    return "done", int(bumps > 0), 1
                held = self.store.lock_concept_cluster_rows(db, notebook_id, batch)
                if len(held) != len(batch):
                    return "done", int(bumps > 0), 1
                if self.store.rewrite_concept_description(db, notebook_id, batch, text, sig):
                    host._bump_cluster_mutation_seq(db, notebook_id)
                    self._bumps += 1
                    bumps += 1
        return "done", int(bumps > 0), 0


def _pages(targets: Sequence[Tuple[str, str, str, int]]):
    """Consecutive targets cut into pages of at most ``_PUBLISH_PAGE_ROWS``
    cluster rows (the plan's count); a canonical larger than that is a page
    of its own (``_publish_hub``)."""
    page: List[Tuple[str, str, str, int]] = []
    rows = 0
    for target in targets:
        size = target[3]
        if size > _PUBLISH_PAGE_ROWS:
            if page:
                yield page
                page, rows = [], 0
            yield [target]
            continue
        if page and rows + size > _PUBLISH_PAGE_ROWS:
            yield page
            page, rows = [], 0
        page.append(target)
        rows += size
    if page:
        yield page
