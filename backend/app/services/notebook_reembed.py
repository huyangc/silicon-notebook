"""Fill in one notebook's missing vectors, leg by leg (job type ``notebook-reembed``).

Four legs, run in this order within one pass:

``chunks``
    Chunk vectors, each on its own workload. A document chunk is embedded with
    ``chunk_embedding`` (per source, under its chunk lock). A Knowhow table's chunks
    belong to ``knowhow_embedding``: the generic discovery leaves Knowhow hidden sources
    out, and ``KnowhowProjector.fill_missing_chunk_vectors`` embeds the table's chunks
    that have no vector, batched, under the table's projection lock, rewriting nothing
    else (no reprojection: relation vectors, ``kg_mutation_seq`` and row statuses are
    untouched). Each half runs when its own workload is bound.
``elements``
    Source element vectors (``source_element_embedding``). The discovery query already
    leaves Memory, Knowhow and promotion sources out.
``kg_objects``
    Knowledge-object vectors (``maintenance.backfill_node_embeddings``: keyset pages, no
    connection held across a model call). A pass that wrote any marks the unified graph
    dirty through ``mark_unified_kg_dirty``, the one online dirty entry.
``kg_relations``
    Relation vectors (``maintenance.backfill_relation_embeddings``, keyset pages on both
    backends).

Every leg fills only what is missing and counts only what it can embed (a blank chunk, an
object without payload text or a relation without text is never "missing"), so a second
run over a complete notebook makes no model call and reports nothing left. ``chunks`` and
``elements`` are the body of ``POST /notebooks/{id}/backfill-vectors`` (moved here from
``source_routes``): per source, under that source's chunk lock, one id discovery then
primary-key hydration page by page, and one H4/H5 boundary notification per pass.

**One run per notebook.** ``_active_runs`` is the per-notebook single-flight guard that
``backfill-vectors`` and ``notebook-reembed`` share, so the two never embed the same
notebook concurrently. A request for a notebook whose run is in flight joins it: a leg the
current pass has not started yet covers the request; every other leg (one the pass has
already started discovering, or one it does not run) is owed one more pass of the same run
before the guard is released. The guard is process-local, like
``NotebookCatalog.kg_building``.

**Indexing pipeline gate.** ``require_indexing_pipeline_write`` is checked before each
leg and at every source lock; a pipeline switch aborts the pass (the run fails and the
guard is released) instead of being recorded as one source's failure.

**Progress and result.** Each run has a random id and keeps, per leg, a state, the number
of vectors written over the whole run, and the number still missing after the last pass
that ran the leg (``ReembedStatus``); the last ``_RESULT_HISTORY`` runs stay readable
after they finish. A leg that could not run (its workload is unbound), that leaves vectors
missing (a model batch failed) or whose reads failed makes the run ``failed``; what it
wrote stays, and the next run picks up the rest.
"""
from __future__ import annotations

import dataclasses
import logging
import threading
import uuid
from collections import OrderedDict
from typing import Any, Iterable, Mapping

from app.domain.indexing_pipeline import IndexingPipelineUnavailableError
from app.services import background_jobs
from app.services.knowhow import api as knowhow_api

LEG_CHUNKS = "chunks"
LEG_ELEMENTS = "elements"
LEG_KG_OBJECTS = "kg_objects"
LEG_KG_RELATIONS = "kg_relations"
LEGS: tuple[str, ...] = (LEG_CHUNKS, LEG_ELEMENTS, LEG_KG_OBJECTS, LEG_KG_RELATIONS)
"""Every leg, in the order a pass runs them."""
SOURCE_VECTOR_LEGS = frozenset({LEG_CHUNKS, LEG_ELEMENTS})
"""What ``POST /notebooks/{id}/backfill-vectors`` runs."""

_LEG_WORKLOADS = {
    LEG_CHUNKS: "chunk_embedding",
    LEG_ELEMENTS: "source_element_embedding",
    LEG_KG_OBJECTS: "knowledge_object_embedding",
    LEG_KG_RELATIONS: "relation_embedding",
}
_KNOWHOW_WORKLOAD = "knowhow_embedding"

JOB_NAME_PREFIX = "notebook-reembed-"
"""``background_jobs`` name prefix; the operation runs in the heavy maintenance pool."""

_BACKFILL_PAGE_ROWS = 500
"""Target rows per hydration page (rounded down to a multiple of ``embed_batch_size``,
see ``_backfill_page_rows``)."""

_RESULT_HISTORY = 256

_logger = logging.getLogger("silicon_notebook.backfill_vectors")

_lock = threading.Lock()
# Both guarded by ``_lock``. ``_active_runs``: notebook id -> its in-flight run, the
# single-flight guard itself; ``_runs``: run id -> run, finished ones trimmed to
# ``_RESULT_HISTORY``.
_active_runs: dict[str, "_Run"] = {}
_runs: "OrderedDict[str, _Run]" = OrderedDict()


class ReembedInFlight(RuntimeError):
    """``NotebookReembedService.run`` found another run in flight for the notebook."""


@dataclasses.dataclass(frozen=True)
class LegProgress:
    """One leg of one run.

    ``state``: ``pending``, ``running``, ``done`` (nothing left missing),
    ``incomplete`` (ran, but ``remaining`` vectors are still missing, or half of the leg
    could not run: ``detail`` names the unbound workload), ``skipped`` (its workload is
    unbound; ``detail`` is ``not_configured``) or ``failed`` (a read or write raised;
    ``detail`` is the exception class name). ``embedded`` counts the whole run;
    ``remaining`` is what the last pass that ran the leg left missing."""

    state: str = "pending"
    embedded: int = 0
    remaining: int = 0
    detail: str = ""


@dataclasses.dataclass(frozen=True)
class ReembedStatus:
    run_id: str
    notebook_id: str
    state: str
    """``running``, ``succeeded`` (every leg ``done``) or ``failed``."""
    legs: tuple[str, ...]
    progress: Mapping[str, LegProgress]
    passes: int
    error: str
    """``leg: state[/detail]`` for every leg that is not ``done``, or the class name of
    the exception that aborted the run."""


@dataclasses.dataclass(frozen=True)
class ReembedSubmission:
    run_id: str
    """The run that covers the requested legs."""
    started: bool
    """``False``: the request joined the notebook's in-flight run."""


class _Run:
    """Mutable run record; every field is read and written under ``_lock``."""

    def __init__(self, notebook_id: str, legs: frozenset[str]) -> None:
        self.run_id = f"reembed-{uuid.uuid4().hex}"
        self.notebook_id = notebook_id
        self.legs: set[str] = set(legs)
        self.next_legs: set[str] = set(legs)
        self.pass_legs: frozenset[str] = frozenset()
        # Legs of the current pass whose discovery has begun: a request joining after
        # that point is not covered by this pass.
        self.started_legs: set[str] = set()
        # Vectors each leg wrote in earlier passes (``embedded`` is cumulative).
        self.base: dict[str, int] = {}
        self.progress: dict[str, LegProgress] = {leg: LegProgress() for leg in legs}
        self.passes = 0
        self.executing = False
        self.state = "running"
        self.error = ""
        self.finished = threading.Event()

    def snapshot(self) -> ReembedStatus:
        legs = tuple(leg for leg in LEGS if leg in self.legs)
        return ReembedStatus(
            run_id=self.run_id,
            notebook_id=self.notebook_id,
            state=self.state,
            legs=legs,
            progress={leg: self.progress[leg] for leg in legs},
            passes=self.passes,
            error=self.error,
        )

    def finish(self, failure: "BaseException | None") -> None:
        if failure is not None:
            self.state, self.error = "failed", type(failure).__name__
        else:
            unfinished = [
                f"{leg}: {p.state}" + (f"/{p.detail}" if p.detail else "")
                for leg in LEGS
                if (p := self.progress.get(leg)) is not None and p.state != "done"
            ]
            self.state = "failed" if unfinished else "succeeded"
            self.error = "; ".join(unfinished)
        self.finished.set()


def _validated_legs(legs: Iterable[str]) -> frozenset[str]:
    chosen = frozenset(legs)
    unknown = chosen - set(LEGS)
    if not chosen or unknown:
        raise ValueError(f"legs must be a non-empty subset of {LEGS}: {sorted(unknown)}")
    return chosen


def _register_run(notebook_id: str, legs: frozenset[str]) -> _Run:
    run = _Run(notebook_id, legs)
    _active_runs[notebook_id] = run
    _runs[run.run_id] = run
    finished = [rid for rid, r in _runs.items() if r.state != "running"]
    for rid in finished[: max(0, len(_runs) - _RESULT_HISTORY)]:
        del _runs[rid]
    return run


def _join_locked(run: _Run, legs: frozenset[str]) -> None:
    """Fold ``legs`` into ``run``: a leg of the current pass whose discovery has not
    begun covers the request; any other leg is owed one more pass (before the first
    pass starts, that is the first pass).

    A joined request runs on whatever pool the run already lives on: the endpoint's run
    is a ``kg_scheduler`` job, ``notebook-reembed`` a heavy-pool job, so legs requested
    through one entry may execute on the other's pool. That is acceptable because the
    guard, not the pool, is what keeps one notebook from being embedded twice at once,
    and both pools are bounded background pools; the alternative (a second runner on
    the requester's pool) is exactly the concurrent duplicate embedding the guard
    exists to prevent."""
    covered = run.pass_legs - run.started_legs
    run.next_legs |= legs - covered
    for leg in legs - run.legs:
        run.progress[leg] = LegProgress()
    run.legs |= legs


def _set_leg(run: _Run, leg: str, **fields: Any) -> None:
    """Update one leg; an ``embedded`` value is this pass's count and is added to what
    earlier passes wrote."""
    with _lock:
        if "embedded" in fields:
            fields["embedded"] = run.base.get(leg, 0) + int(fields["embedded"])
        run.progress[leg] = dataclasses.replace(run.progress[leg], **fields)


def _start_legs(run: _Run, *legs: str) -> None:
    with _lock:
        run.started_legs.update(legs)


def _backfill_page_rows(repo) -> int:
    """``_BACKFILL_PAGE_ROWS`` rounded down to a multiple of ``embed_batch_size`` (at
    least one whole batch).

    ``embed_chunks_batch`` / ``embed_elements_batch`` cut what they receive into
    ``embed_batch_size`` model calls; a page that is not a multiple would end in a short
    call, so the call count would change. Rounded, the count equals feeding every row at
    once (``ceil(N / embed_batch_size)``). Which rows share a call can differ (pages
    follow id order); the vectors written do not depend on their batch neighbours."""
    size = max(1, int(repo.settings.embed_batch_size))
    return max(size, (_BACKFILL_PAGE_ROWS // size) * size)


def _hydrate_in_id_order(rows: list) -> list:
    """Rows hydrated by primary key, re-sorted by ``id``: ``id IN (...)`` keeps no order,
    and discovery is ``ORDER BY id``, so this keeps page boundaries and batch contents
    reproducible across backends and plans."""
    return sorted(rows, key=lambda r: str(r["id"]))


class NotebookReembedService:
    """Entry points over the shared guard. Stateless apart from the repository facade,
    so a caller may build one per request or job.

    Facade members are used where they exist (``settings``, ``configured``,
    ``maintenance``, ``require_indexing_pipeline_write``, ``list_knowhow_tables``). Two
    runtime seams have no facade member and are reached through ``repo._runtime``, as
    the backfill job always did: the source chunk lock with its lease
    (``source_ingestion.hold_source_chunk_lock``) and the H4/H5 boundary event
    (``source_embedding.note_source_vectors_written``)."""

    def __init__(self, repo: Any) -> None:
        self._repo = repo

    # --- admission -------------------------------------------------------------
    @staticmethod
    def join_running(notebook_id: str, legs: Iterable[str]) -> bool:
        """Join the notebook's in-flight run if there is one (``True``); claim nothing
        otherwise (``False``)."""
        chosen = _validated_legs(legs)
        with _lock:
            run = _active_runs.get(notebook_id)
            if run is None:
                return False
            _join_locked(run, chosen)
            return True

    @staticmethod
    def claim(notebook_id: str, legs: Iterable[str]) -> ReembedSubmission:
        """Take the guard and register a run (``started=True``: the caller must now run
        ``run_claimed`` or ``release``), or join the in-flight run."""
        chosen = _validated_legs(legs)
        with _lock:
            run = _active_runs.get(notebook_id)
            if run is not None:
                _join_locked(run, chosen)
                return ReembedSubmission(run.run_id, False)
            return ReembedSubmission(_register_run(notebook_id, chosen).run_id, True)

    @staticmethod
    def release(notebook_id: str) -> None:
        """Give a claimed guard back when the job could not be handed off."""
        with _lock:
            run = _active_runs.pop(notebook_id, None)
            if run is not None:
                run.state, run.error = "failed", "not_started"
                run.finished.set()

    @staticmethod
    def status(run_id: str) -> "ReembedStatus | None":
        with _lock:
            run = _runs.get(run_id)
            return run.snapshot() if run is not None else None

    @staticmethod
    def wait(run_id: str, timeout: "float | None" = None) -> "ReembedStatus | None":
        """Block until the run finishes (or ``timeout`` passes); its status, or ``None``
        for an unknown run id."""
        with _lock:
            run = _runs.get(run_id)
        if run is None:
            return None
        run.finished.wait(timeout)
        with _lock:
            return run.snapshot()

    # --- execution -------------------------------------------------------------
    def submit(self, notebook_id: str, *, legs: Iterable[str]) -> ReembedSubmission:
        """Run ``legs`` for the notebook in the heavy maintenance pool."""
        submission = self.claim(notebook_id, legs)
        if not submission.started:
            return submission
        try:
            background_jobs.submit(
                self.run_claimed, notebook_id, name=f"{JOB_NAME_PREFIX}{notebook_id}"
            )
        except BaseException:
            self.release(notebook_id)
            raise
        return submission

    def run(self, notebook_id: str, *, legs: Iterable[str]) -> ReembedStatus:
        """Run ``legs`` now, on this thread, and return the finished run. Refuses
        (without joining) when the notebook already has a run in flight."""
        chosen = _validated_legs(legs)
        with _lock:
            if notebook_id in _active_runs:
                raise ReembedInFlight(notebook_id)
            _register_run(notebook_id, chosen)
        return self.run_claimed(notebook_id)

    def run_claimed(self, notebook_id: str) -> ReembedStatus:
        """Execute the notebook's claimed run until no leg is left, then release the
        guard. Exactly one executor per run: a call without a claimed run, or for a run
        that is already executing, raises.

        A leg's own failure is recorded on the leg; anything that aborts the pass (the
        indexing-pipeline gate, an unexpected error) fails the run, releases the guard
        and propagates; a follow-up pass still owed is dropped then."""
        with _lock:
            run = _active_runs.get(notebook_id)
            if run is None:
                raise ValueError(f"no claimed reembed run for {notebook_id}")
            if run.executing:
                raise RuntimeError(f"reembed run {run.run_id} is already executing")
            run.executing = True
        try:
            while True:
                with _lock:
                    pending = frozenset(run.next_legs)
                    run.next_legs.clear()
                    if not pending:
                        _active_runs.pop(notebook_id, None)
                        run.finish(None)
                        return run.snapshot()
                    run.pass_legs = pending
                    run.started_legs = set()
                    run.passes += 1
                    for leg in pending:
                        run.base[leg] = run.progress[leg].embedded
                        run.progress[leg] = dataclasses.replace(
                            run.progress[leg], state="pending", remaining=0, detail=""
                        )
                self._run_pass(run, pending)
        except BaseException as exc:
            with _lock:
                if _active_runs.get(notebook_id) is run:
                    del _active_runs[notebook_id]
                run.finish(exc)
            raise

    def _run_pass(self, run: _Run, legs: frozenset[str]) -> None:
        gate = self._repo.require_indexing_pipeline_write
        if legs & SOURCE_VECTOR_LEGS:
            gate(run.notebook_id)
            self._source_vector_legs(run, legs)
        if LEG_KG_OBJECTS in legs:
            gate(run.notebook_id)
            self._kg_objects_leg(run)
        if LEG_KG_RELATIONS in legs:
            gate(run.notebook_id)
            self._kg_relations_leg(run)

    # --- chunks + elements -------------------------------------------------------
    def _source_vector_legs(self, run: _Run, legs: frozenset[str]) -> None:
        """Per source, under the source's chunk lock (P1.5): read the missing rows and
        embed them inside the lock. Element and chunk ids are reused by a reparse; a
        read outside the lock committed after the replacement would attach a stale
        vector no later check can see. The lock also registers an active lease, so
        ``process_source`` cannot drop the lock while this job holds it.

        Discovery is one light ``DISTINCT source_id`` query per bound workload; inside
        the lock, one id discovery per source, then text hydrated by primary key page by
        page (a whole large source's text never sits in memory, and the keyset page
        query is not re-run per page: without ``ANALYZE`` it degrades to a whole-table
        primary-key range scan). One source's failure does not stop the others, but it
        fails its leg. Knowhow chunks are filled after the loop
        (``knowhow_api.fill_missing_knowhow_chunk_vectors``). The H4/H5 boundary
        notification is sent once, in ``finally``, whenever anything ran."""
        repo = self._repo
        nb = run.notebook_id
        mnt = repo.maintenance
        configured = repo.configured
        doc_ok = LEG_CHUNKS in legs and configured(_LEG_WORKLOADS[LEG_CHUNKS])
        knowhow_ok = LEG_CHUNKS in legs and configured(_KNOWHOW_WORKLOAD)
        elem_ok = LEG_ELEMENTS in legs and configured(_LEG_WORKLOADS[LEG_ELEMENTS])
        live: list[str] = []
        unbound: dict[str, str] = {}
        if LEG_CHUNKS in legs:
            if doc_ok or knowhow_ok:
                live.append(LEG_CHUNKS)
                if not doc_ok:
                    unbound[LEG_CHUNKS] = f"not_configured:{_LEG_WORKLOADS[LEG_CHUNKS]}"
                elif not knowhow_ok:
                    unbound[LEG_CHUNKS] = f"not_configured:{_KNOWHOW_WORKLOAD}"
            else:
                _set_leg(run, LEG_CHUNKS, state="skipped", detail="not_configured")
        if LEG_ELEMENTS in legs:
            if elem_ok:
                live.append(LEG_ELEMENTS)
            else:
                _set_leg(run, LEG_ELEMENTS, state="skipped", detail="not_configured")
        if not live:
            return
        totals = {leg: [0, 0] for leg in live}  # leg -> [missing found, embedded]
        failures: dict[str, str] = {}
        _start_legs(run, *live)
        for leg in live:
            _set_leg(run, leg, state="running")
        try:
            sources: set[str] = set()
            if doc_ok:
                sources |= set(mnt.missing_chunk_vector_source_ids(nb))
            if elem_ok:
                sources |= set(mnt.missing_element_vector_source_ids(nb))
            page = _backfill_page_rows(repo)
            ingestion = repo._runtime.source_ingestion
            for source_id in sorted(sources):
                with ingestion.hold_source_chunk_lock(source_id):
                    # Rechecked at every source lock: a pipeline switch raises and
                    # aborts the pass (re-raised below), it is never recorded as one
                    # source's failure.
                    repo.require_indexing_pipeline_write(nb)
                    if doc_ok:
                        self._embed_source_rows(
                            run, LEG_CHUNKS, totals, failures, source_id, page,
                        )
                    if elem_ok:
                        self._embed_source_rows(
                            run, LEG_ELEMENTS, totals, failures, source_id, page,
                        )
            if knowhow_ok:
                try:
                    found, embedded = knowhow_api.fill_missing_knowhow_chunk_vectors(
                        repo, nb
                    )
                    totals[LEG_CHUNKS][0] += found
                    totals[LEG_CHUNKS][1] += embedded
                except IndexingPipelineUnavailableError:
                    raise
                except Exception as exc:  # noqa: BLE001 — recorded on the leg
                    _logger.exception(
                        "notebook-reembed knowhow chunk fill failed: notebook_id=%s", nb
                    )
                    failures.setdefault(LEG_CHUNKS, type(exc).__name__)
        except IndexingPipelineUnavailableError as exc:
            for leg in live:
                self._settle(run, leg, totals, {leg: type(exc).__name__}, {})
            raise
        except Exception as exc:  # noqa: BLE001 — logged and recorded on the legs
            _logger.exception("backfill-vectors job 失败(已跳过): notebook_id=%s", nb)
            for leg in live:
                failures.setdefault(leg, type(exc).__name__)
        finally:
            # H4/H5 boundary event: a successful embed does not bump kg_mutation_seq, and
            # a job that falls between two polls leaves the lease snapshot unchanged, so
            # without this the memo would serve the pre-repair counts for its TTL. Sent
            # once per pass, not per page (the embed_*_batch primitives deliberately do
            # not notify). In finally: pages committed before a failure changed counts.
            repo._runtime.source_embedding.note_source_vectors_written(nb)
        for leg in live:
            self._settle(run, leg, totals, failures, unbound)

    @staticmethod
    def _settle(
        run: _Run, leg: str, totals: dict, failures: dict, unbound: dict
    ) -> None:
        found, embedded = totals[leg]
        remaining = max(0, found - embedded)
        if leg in failures:
            state, detail = "failed", failures[leg]
        elif remaining or leg in unbound:
            state, detail = "incomplete", unbound.get(leg, "")
        else:
            state, detail = "done", ""
        _set_leg(
            run, leg, state=state, embedded=embedded, remaining=remaining, detail=detail,
        )

    def _embed_source_rows(
        self,
        run: _Run,
        leg: str,
        totals: dict,
        failures: dict,
        source_id: str,
        page: int,
    ) -> None:
        """One source's missing rows of one leg, inside the caller's source lock. Only
        embeddable rows count as missing (a blank chunk never gets a vector). A failed
        read is logged and fails the leg; the other sources still run."""
        mnt = self._repo.maintenance
        nb = run.notebook_id
        try:
            if leg == LEG_CHUNKS:
                ids = mnt.missing_chunk_embedding_ids(nb, only_source_id=source_id)
            else:
                ids = mnt.missing_element_embedding_ids(nb, only_source_id=source_id)
            for start in range(0, len(ids), page):
                batch = ids[start:start + page]
                if leg == LEG_CHUNKS:
                    rows = [
                        r for r in _hydrate_in_id_order(mnt.chunk_texts_by_ids(batch))
                        if str(r["text"] or "").strip()
                    ]
                    if not rows:
                        continue
                    totals[leg][0] += len(rows)
                    written = mnt.embed_chunks_batch(
                        nb, [{"_oid": r["id"], "payload": {"text": r["text"]}} for r in rows],
                    )
                else:
                    rows = _hydrate_in_id_order(mnt.element_texts_by_ids(batch))
                    if not rows:
                        continue
                    totals[leg][0] += len(rows)
                    written = mnt.embed_elements_batch(
                        nb,
                        [
                            {"element_id": r["id"], "source_id": r["source_id"], "text": r["text"]}
                            for r in rows
                        ],
                    )
                totals[leg][1] += int(written or 0)
                found, embedded = totals[leg]
                _set_leg(run, leg, embedded=embedded, remaining=max(0, found - embedded))
        except IndexingPipelineUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 — recorded on the leg; other sources go on
            _logger.exception(
                "backfill-vectors source failed: notebook_id=%s leg=%s", nb, leg
            )
            failures.setdefault(leg, type(exc).__name__)

    # --- knowledge graph -----------------------------------------------------------
    def _kg_objects_leg(self, run: _Run) -> None:
        repo = self._repo
        nb = run.notebook_id
        if not repo.configured(_LEG_WORKLOADS[LEG_KG_OBJECTS]):
            _set_leg(run, LEG_KG_OBJECTS, state="skipped", detail="not_configured")
            return
        _start_legs(run, LEG_KG_OBJECTS)
        _set_leg(run, LEG_KG_OBJECTS, state="running")
        counts = [0, 0]  # [written, attempted] so far, kept if the leg fails

        def _tally(written: int, attempted: int) -> None:
            counts[:] = [written, attempted]
            _set_leg(
                run, LEG_KG_OBJECTS, embedded=written,
                remaining=max(0, attempted - written),
            )

        try:
            repo.maintenance.backfill_node_embeddings(nb, tally=_tally)
        except Exception as exc:  # noqa: BLE001 — logged and recorded on the leg
            _logger.exception("notebook-reembed kg_objects failed: notebook_id=%s", nb)
            # Pages written before the failure changed the clustering inputs.
            self._mark_kg_dirty(nb)
            _set_leg(
                run, LEG_KG_OBJECTS, state="failed", embedded=counts[0],
                remaining=max(0, counts[1] - counts[0]), detail=type(exc).__name__,
            )
            return
        written, attempted = counts
        if written:
            # New object vectors change the clustering inputs: a later rebuild must not
            # skip on unchanged object/decision counts alone (batch_ingest does the same).
            self._mark_kg_dirty(nb)
        remaining = max(0, attempted - written)
        _set_leg(
            run, LEG_KG_OBJECTS, state="done" if remaining == 0 else "incomplete",
            embedded=written, remaining=remaining,
        )

    def _mark_kg_dirty(self, notebook_id: str) -> None:
        try:
            self._repo.maintenance.mark_unified_kg_dirty(notebook_id)
        except Exception:  # noqa: BLE001 — the vectors are written; logged, not fatal
            _logger.exception(
                "notebook-reembed could not mark the graph dirty: notebook_id=%s",
                notebook_id,
            )

    def _kg_relations_leg(self, run: _Run) -> None:
        repo = self._repo
        nb = run.notebook_id
        if not repo.configured(_LEG_WORKLOADS[LEG_KG_RELATIONS]):
            _set_leg(run, LEG_KG_RELATIONS, state="skipped", detail="not_configured")
            return
        _start_legs(run, LEG_KG_RELATIONS)
        _set_leg(run, LEG_KG_RELATIONS, state="running")
        counts = [0, 0]  # [written, attempted] so far, kept if the leg fails

        def _progress(written: int, attempted: int) -> None:
            counts[:] = [written, attempted]
            _set_leg(
                run, LEG_KG_RELATIONS, embedded=written,
                remaining=max(0, attempted - written),
            )

        try:
            repo.maintenance.backfill_relation_embeddings(nb, progress=_progress)
        except Exception as exc:  # noqa: BLE001 — logged and recorded on the leg
            _logger.exception("notebook-reembed kg_relations failed: notebook_id=%s", nb)
            _set_leg(
                run, LEG_KG_RELATIONS, state="failed", embedded=counts[0],
                remaining=max(0, counts[1] - counts[0]), detail=type(exc).__name__,
            )
            return
        written, attempted = counts
        remaining = max(0, attempted - written)
        _set_leg(
            run, LEG_KG_RELATIONS, state="done" if remaining == 0 else "incomplete",
            embedded=written, remaining=remaining,
        )
