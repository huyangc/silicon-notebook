"""Fill in one notebook's missing vectors, leg by leg (job type ``notebook-reembed``).

Four legs, run in this order within one pass:

``chunks``
    Chunk vectors. A document chunk is embedded with ``chunk_embedding``; a Knowhow
    table's hidden source is never embedded here, because its chunks belong to
    ``knowhow_embedding``: the table is reprojected instead
    (``KnowhowProjector.project_table``), whose self-heal re-embeds every text-unchanged
    chunk that has no vector. The leg runs only while ``chunk_embedding`` is bound; the
    Knowhow half additionally needs ``knowhow_embedding``.
``elements``
    Source element vectors (``source_element_embedding``). The discovery query already
    leaves Memory, Knowhow and promotion sources out.
``kg_objects``
    Knowledge-object vectors (``maintenance.backfill_node_embeddings``: keyset pages, no
    connection held across a model call). A pass that wrote any marks the unified graph
    dirty through ``mark_unified_kg_dirty``, the one online dirty entry.
``kg_relations``
    Relation vectors (``maintenance.backfill_relation_embeddings``). Runs after
    ``kg_objects`` and after a Knowhow reprojection, which deletes and rewrites the
    table's relations (and so their vectors).

Every leg fills only what is missing, so a second run over a complete notebook makes no
model call. ``chunks`` and ``elements`` are the body of ``POST
/notebooks/{id}/backfill-vectors`` (moved here from ``source_routes``): per source, under
that source's chunk lock, one id discovery then primary-key hydration page by page, and
one H4/H5 boundary notification per pass.

**One run per notebook.** ``_inflight`` is the per-notebook single-flight guard that
``backfill-vectors`` and ``notebook-reembed`` share, so the two never embed the same
notebook concurrently. A request for a notebook whose run is in flight joins it: legs the
current pass is already running are covered by it, any other leg runs in one follow-up
pass of the same run before the guard is released. The guard is process-local, like
``NotebookCatalog.kg_building``.

**Progress and result.** Each run has a random id and keeps, per leg, a state and the
number of vectors written and still missing (``ReembedStatus``); the last
``_RESULT_HISTORY`` runs stay readable after they finish. A leg that cannot run (its
workload is unbound), that leaves vectors missing (a model batch failed) or that raised
makes the run ``failed``; what it wrote stays, and the next run picks up the rest.
"""
from __future__ import annotations

import dataclasses
import logging
import threading
import uuid
from collections import OrderedDict
from typing import Any, Iterable, Mapping

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
_KNOWHOW_SOURCE_TYPE = "knowhow"

JOB_NAME_PREFIX = "notebook-reembed-"
"""``background_jobs`` name prefix; the operation runs in the heavy maintenance pool."""

_BACKFILL_PAGE_ROWS = 500
"""Target rows per hydration page (rounded down to a multiple of ``embed_batch_size``,
see ``_backfill_page_rows``)."""

_RESULT_HISTORY = 256

_logger = logging.getLogger("silicon_notebook.backfill_vectors")

_inflight: set[str] = set()
_inflight_lock = threading.Lock()
# Both guarded by ``_inflight_lock``. ``_active_runs``: notebook id -> its in-flight run;
# ``_runs``: run id -> run, finished ones trimmed to ``_RESULT_HISTORY``.
_active_runs: dict[str, "_Run"] = {}
_runs: "OrderedDict[str, _Run]" = OrderedDict()


class ReembedInFlight(RuntimeError):
    """``NotebookReembedService.run`` found another run in flight for the notebook."""


@dataclasses.dataclass(frozen=True)
class LegProgress:
    """One leg of one run.

    ``state``: ``pending``, ``running``, ``done`` (nothing left missing),
    ``incomplete`` (ran, ``remaining`` vectors still missing), ``skipped`` (its
    workload is unbound; ``detail`` is ``not_configured``) or ``failed`` (raised;
    ``detail`` is the exception class name)."""

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
    run_id: "str | None"
    """The run that will cover the requested legs (``None`` only when the guard is held
    without a run, which no production path does)."""
    started: bool
    """``False``: the request joined the notebook's in-flight run."""


class _Run:
    """Mutable run record; every field is read and written under ``_inflight_lock``."""

    def __init__(self, notebook_id: str, legs: frozenset[str]) -> None:
        self.run_id = f"reembed-{uuid.uuid4().hex}"
        self.notebook_id = notebook_id
        self.legs: set[str] = set(legs)
        self.next_legs: set[str] = set(legs)
        self.pass_legs: frozenset[str] = frozenset()
        self.progress: dict[str, LegProgress] = {leg: LegProgress() for leg in legs}
        self.passes = 0
        self.state = "running"
        self.error = ""

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
            return
        unfinished = [
            f"{leg}: {p.state}" + (f"/{p.detail}" if p.detail else "")
            for leg in LEGS
            if (p := self.progress.get(leg)) is not None and p.state != "done"
        ]
        self.state = "failed" if unfinished else "succeeded"
        self.error = "; ".join(unfinished)


def _validated_legs(legs: Iterable[str]) -> frozenset[str]:
    chosen = frozenset(legs)
    unknown = chosen - set(LEGS)
    if not chosen or unknown:
        raise ValueError(f"legs must be a non-empty subset of {LEGS}: {sorted(unknown)}")
    return chosen


def _register_run(notebook_id: str, legs: frozenset[str]) -> _Run:
    run = _Run(notebook_id, legs)
    _inflight.add(notebook_id)
    _active_runs[notebook_id] = run
    _runs[run.run_id] = run
    finished = [rid for rid, r in _runs.items() if r.state != "running"]
    for rid in finished[: max(0, len(_runs) - _RESULT_HISTORY)]:
        del _runs[rid]
    return run


def _join_locked(notebook_id: str, legs: frozenset[str]) -> "_Run | None":
    """Fold ``legs`` into the notebook's in-flight run: a leg the current pass is running
    is covered by it, any other leg is owed one more pass (before the first pass starts,
    that is the first pass)."""
    run = _active_runs.get(notebook_id)
    if run is not None:
        run.next_legs |= legs - run.pass_legs
        for leg in legs - run.legs:
            run.progress[leg] = LegProgress()
        run.legs |= legs
    return run


def _release_locked(notebook_id: str, run: _Run) -> None:
    _inflight.discard(notebook_id)
    if _active_runs.get(notebook_id) is run:
        del _active_runs[notebook_id]


def _set_leg(run: _Run, leg: str, **fields: Any) -> None:
    with _inflight_lock:
        run.progress[leg] = dataclasses.replace(run.progress[leg], **fields)


def _backfill_page_rows(repo) -> int:
    """``_BACKFILL_PAGE_ROWS`` rounded down to a multiple of ``embed_batch_size`` (at
    least one whole batch).

    ``embed_chunks_batch`` / ``embed_elements_batch`` cut what they receive into
    ``embed_batch_size`` model calls; a page that is not a multiple would end in a short
    call, so the call count would change. Rounded, the count equals feeding every row at
    once (``ceil(N / embed_batch_size)``). Which rows share a call can differ (pages
    follow id order); the vectors written do not depend on their batch neighbours."""
    size = max(1, int(repo._runtime.settings.embed_batch_size))
    return max(size, (_BACKFILL_PAGE_ROWS // size) * size)


def _hydrate_in_id_order(rows: list) -> list:
    """Rows hydrated by primary key, re-sorted by ``id``: ``id IN (...)`` keeps no order,
    and discovery is ``ORDER BY id``, so this keeps page boundaries and batch contents
    reproducible across backends and plans."""
    return sorted(rows, key=lambda r: str(r["id"]))


class NotebookReembedService:
    """Entry points over the shared guard. Stateless apart from the repository facade,
    so a caller may build one per request or job."""

    def __init__(self, repo: Any) -> None:
        self._repo = repo

    # --- admission -------------------------------------------------------------
    @staticmethod
    def join_running(notebook_id: str, legs: Iterable[str]) -> bool:
        """Join the notebook's in-flight run if there is one (``True``); claim nothing
        otherwise (``False``)."""
        chosen = _validated_legs(legs)
        with _inflight_lock:
            if notebook_id not in _inflight:
                return False
            _join_locked(notebook_id, chosen)
            return True

    @staticmethod
    def claim(notebook_id: str, legs: Iterable[str]) -> ReembedSubmission:
        """Take the guard and register a run (``started=True``: the caller must now run
        ``run_claimed`` or ``release``), or join the in-flight run."""
        chosen = _validated_legs(legs)
        with _inflight_lock:
            if notebook_id in _inflight:
                run = _join_locked(notebook_id, chosen)
                return ReembedSubmission(run.run_id if run else None, False)
            return ReembedSubmission(_register_run(notebook_id, chosen).run_id, True)

    @staticmethod
    def release(notebook_id: str) -> None:
        """Give a claimed guard back when the job could not be handed off."""
        with _inflight_lock:
            run = _active_runs.get(notebook_id)
            _inflight.discard(notebook_id)
            if run is not None:
                _release_locked(notebook_id, run)
                run.state, run.error = "failed", "not_started"

    @staticmethod
    def status(run_id: str) -> "ReembedStatus | None":
        with _inflight_lock:
            run = _runs.get(run_id)
            return run.snapshot() if run is not None else None

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
        with _inflight_lock:
            if notebook_id in _inflight:
                raise ReembedInFlight(notebook_id)
            _register_run(notebook_id, chosen)
        return self.run_claimed(notebook_id)

    def run_claimed(
        self, notebook_id: str, *, legs: "Iterable[str] | None" = None
    ) -> ReembedStatus:
        """Run the notebook's claimed run until no leg is left, then release the guard.

        Only the guard holder's job calls this. ``legs`` registers a run when none is
        (a caller that runs a job body without claiming first, as the backfill job's own
        tests do). A leg's own failure is recorded on the leg; anything that aborts the
        pass (the indexing-pipeline gate, an unexpected error) fails the run, releases
        the guard and propagates; a follow-up pass still owed is dropped then."""
        with _inflight_lock:
            run = _active_runs.get(notebook_id)
            if run is None:
                if legs is None:
                    raise ValueError(f"no claimed reembed run for {notebook_id}")
                run = _register_run(notebook_id, _validated_legs(legs))
        try:
            while True:
                with _inflight_lock:
                    pending = frozenset(run.next_legs)
                    run.next_legs.clear()
                    if not pending:
                        _release_locked(notebook_id, run)
                        run.finish(None)
                        return run.snapshot()
                    run.pass_legs = pending
                    run.passes += 1
                self._run_pass(run, pending)
        except BaseException as exc:
            with _inflight_lock:
                _release_locked(notebook_id, run)
                run.finish(exc)
            raise

    def _run_pass(self, run: _Run, legs: frozenset[str]) -> None:
        repo = self._repo
        repo.require_indexing_pipeline_write(run.notebook_id)
        for leg in LEGS:
            if leg in legs:
                _set_leg(run, leg, state="pending", embedded=0, remaining=0, detail="")
        if legs & SOURCE_VECTOR_LEGS:
            self._source_vector_legs(run, legs)
        if LEG_KG_OBJECTS in legs:
            self._kg_objects_leg(run)
        if LEG_KG_RELATIONS in legs:
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
        primary-key range scan). One source failing does not stop the others.

        Knowhow hidden sources are taken out of the chunk loop and reprojected
        (``_knowhow_chunks``). Failures of discovery or of the loop itself are logged
        and fail the legs; the H4/H5 boundary notification is sent once, in
        ``finally``, whenever either workload is bound."""
        repo = self._repo
        nb = run.notebook_id
        mnt = repo.maintenance
        chunk_ok = LEG_CHUNKS in legs and repo.configured(_LEG_WORKLOADS[LEG_CHUNKS])
        elem_ok = LEG_ELEMENTS in legs and repo.configured(_LEG_WORKLOADS[LEG_ELEMENTS])
        for leg, ok in ((LEG_CHUNKS, chunk_ok), (LEG_ELEMENTS, elem_ok)):
            if leg in legs and not ok:
                _set_leg(run, leg, state="skipped", detail="not_configured")
        if not (chunk_ok or elem_ok):
            return
        live = [leg for leg, ok in ((LEG_CHUNKS, chunk_ok), (LEG_ELEMENTS, elem_ok)) if ok]
        for leg in live:
            _set_leg(run, leg, state="running")
        totals = {leg: [0, 0] for leg in live}  # leg -> [missing found, embedded]
        try:
            chunk_sources: set[str] = set()
            if chunk_ok:
                chunk_sources = set(mnt.missing_chunk_vector_source_ids(nb))
            sources = set(chunk_sources)
            if elem_ok:
                sources |= set(mnt.missing_element_vector_source_ids(nb))
            knowhow = self._knowhow_sources(chunk_sources)
            page = _backfill_page_rows(repo)
            ingestion = repo._runtime.source_ingestion
            for source_id in sorted(sources - knowhow):
                with ingestion.hold_source_chunk_lock(source_id):
                    # Rechecked at every source lock: a pipeline switch aborts the
                    # pass instead of being swallowed as one source's failure.
                    repo.require_indexing_pipeline_write(nb)
                    if chunk_ok:
                        self._embed_source_rows(
                            run, LEG_CHUNKS, totals, source_id, page,
                        )
                    if elem_ok:
                        self._embed_source_rows(
                            run, LEG_ELEMENTS, totals, source_id, page,
                        )
            if knowhow:
                found, embedded = self._knowhow_chunks(nb, knowhow)
                totals[LEG_CHUNKS][0] += found
                totals[LEG_CHUNKS][1] += embedded
            for leg in live:
                found, embedded = totals[leg]
                remaining = max(0, found - embedded)
                _set_leg(
                    run, leg, state="done" if remaining == 0 else "incomplete",
                    embedded=embedded, remaining=remaining,
                )
        except Exception as exc:  # noqa: BLE001 — logged and recorded on the legs
            _logger.exception("backfill-vectors job 失败(已跳过): notebook_id=%s", nb)
            for leg in live:
                found, embedded = totals[leg]
                _set_leg(
                    run, leg, state="failed", embedded=embedded,
                    remaining=max(0, found - embedded), detail=type(exc).__name__,
                )
        finally:
            # H4/H5 boundary event: a successful embed does not bump kg_mutation_seq, and
            # a job that falls between two polls leaves the lease snapshot unchanged, so
            # without this the memo would serve the pre-repair counts for its TTL. Sent
            # once per pass, not per page (the embed_*_batch primitives deliberately do
            # not notify). In finally: pages committed before a failure changed counts.
            repo._runtime.source_embedding.note_source_vectors_written(nb)

    def _embed_source_rows(
        self, run: _Run, leg: str, totals: dict, source_id: str, page: int
    ) -> None:
        """One source's missing rows of one leg, inside the caller's source lock.
        Best-effort: a failure leaves the rest of this source to the next run."""
        mnt = self._repo.maintenance
        nb = run.notebook_id
        try:
            if leg == LEG_CHUNKS:
                ids = mnt.missing_chunk_embedding_ids(nb, only_source_id=source_id)
            else:
                ids = mnt.missing_element_embedding_ids(nb, only_source_id=source_id)
            totals[leg][0] += len(ids)
            for start in range(0, len(ids), page):
                batch = ids[start:start + page]
                if leg == LEG_CHUNKS:
                    rows = _hydrate_in_id_order(mnt.chunk_texts_by_ids(batch))
                    if not rows:
                        continue
                    written = mnt.embed_chunks_batch(
                        nb, [{"_oid": r["id"], "payload": {"text": r["text"]}} for r in rows],
                    )
                else:
                    rows = _hydrate_in_id_order(mnt.element_texts_by_ids(batch))
                    if not rows:
                        continue
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
        except Exception:  # noqa: BLE001 — one source failing does not stop the others
            pass

    def _knowhow_sources(self, source_ids: set[str]) -> set[str]:
        if not source_ids:
            return set()
        metadata = self._repo.source_metadata(sorted(source_ids))
        return {
            source_id
            for source_id, row in metadata.items()
            if row.get("source_type") == _KNOWHOW_SOURCE_TYPE
        }

    def _knowhow_chunks(self, notebook_id: str, source_ids: set[str]) -> tuple[int, int]:
        """Reproject each Knowhow table whose hidden source has chunks without a vector
        (its self-heal embeds them with ``knowhow_embedding`` under the table's
        projection lock). Returns ``(missing found, embedded)``; with
        ``knowhow_embedding`` unbound, or for a hidden source no table owns, the chunks
        stay missing."""
        mnt = self._repo.maintenance
        before = {
            source_id: len(mnt.missing_chunk_embedding_ids(notebook_id, only_source_id=source_id))
            for source_id in sorted(source_ids)
        }
        found = sum(before.values())
        if not self._repo.configured(_KNOWHOW_WORKLOAD):
            return found, 0
        tables = {
            str(table.get("hidden_source_id") or ""): str(table["id"])
            for table in self._repo.list_knowhow_tables(notebook_id)
        }
        projector = knowhow_api.build_projector(self._repo)
        embedded = 0
        for source_id, missing in before.items():
            table_id = tables.get(source_id)
            if table_id is None:
                continue
            try:
                projector.project_table(table_id)
            except Exception:  # noqa: BLE001 — one table failing does not stop the others
                _logger.exception(
                    "notebook-reembed knowhow reprojection failed: notebook_id=%s",
                    notebook_id,
                )
            after = len(mnt.missing_chunk_embedding_ids(notebook_id, only_source_id=source_id))
            embedded += max(0, missing - after)
        return found, embedded

    # --- knowledge graph -----------------------------------------------------------
    def _kg_objects_leg(self, run: _Run) -> None:
        repo = self._repo
        nb = run.notebook_id
        if not repo.configured(_LEG_WORKLOADS[LEG_KG_OBJECTS]):
            _set_leg(run, LEG_KG_OBJECTS, state="skipped", detail="not_configured")
            return
        _set_leg(run, LEG_KG_OBJECTS, state="running")
        mnt = repo.maintenance
        embedded = 0
        try:
            embedded = int(mnt.backfill_node_embeddings(nb) or 0)
            remaining = int(mnt.count_missing_node_vectors(nb))
        except Exception as exc:  # noqa: BLE001 — logged and recorded on the leg
            _logger.exception("notebook-reembed kg_objects failed: notebook_id=%s", nb)
            # Pages written before the failure changed the clustering inputs.
            self._mark_kg_dirty(nb)
            _set_leg(run, LEG_KG_OBJECTS, state="failed", detail=type(exc).__name__)
            return
        if embedded:
            # New object vectors change the clustering inputs: a later rebuild must not
            # skip on unchanged object/decision counts alone (batch_ingest does the same).
            self._mark_kg_dirty(nb)
        _set_leg(
            run, LEG_KG_OBJECTS, state="done" if remaining == 0 else "incomplete",
            embedded=embedded, remaining=remaining,
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
        _set_leg(run, LEG_KG_RELATIONS, state="running")

        def _progress(embedded: int, attempted: int) -> None:
            _set_leg(
                run, LEG_KG_RELATIONS, embedded=embedded,
                remaining=max(0, attempted - embedded),
            )

        try:
            repo.maintenance.backfill_relation_embeddings(nb, progress=_progress)
        except Exception as exc:  # noqa: BLE001 — logged and recorded on the leg
            _logger.exception("notebook-reembed kg_relations failed: notebook_id=%s", nb)
            _set_leg(run, LEG_KG_RELATIONS, state="failed", detail=type(exc).__name__)
            return
        with _inflight_lock:
            remaining = run.progress[LEG_KG_RELATIONS].remaining
        _set_leg(run, LEG_KG_RELATIONS, state="done" if remaining == 0 else "incomplete")
