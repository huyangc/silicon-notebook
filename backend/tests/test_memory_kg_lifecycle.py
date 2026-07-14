"""Task 3 (memory-kg-extract): MemoryService lifecycle hooks into the
Task 2 source_ingestion memory-derived-source primitives, wired through the
runtime bridge (`set_memory_kg_service`) rather than a direct import.

RED-first contracts (a duck-typed `_KgStub` stands in for the real
`SourceIngestionService`, recording every call it receives):

- `confirm()` schedules `_kg_ingest_job` when the KG gate passes and the
  request did not explicitly opt out (`extract_kg=False`), and stays silent
  when the notebook is ineligible;
- `create_from_answer()` (born confirmed) follows the same default-on /
  explicit-opt-out contract;
- `update()` on an already-confirmed Memory re-schedules the same job only
  when a derived source already exists (content-fingerprint skip lives
  inside `ingest_memory_source` itself, per Task 2 — this hook schedules
  unconditionally and trusts that skip);
- `deprecate()` synchronously calls `remove_memory_source`;
- `reject()` and edits to a still-`candidate` Memory never touch the KG
  service at all;
- a job that is scheduled (but not yet run) before the Memory is deprecated
  re-reads Memory state when it finally runs and skips a non-`confirmed`
  item — the race resolves itself without a stale KG write;
- with no KG service injected (`set_memory_kg_service` never called), the
  whole lifecycle behaves exactly as it did before this feature existed.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import AskResponse, MemoryReviewRequest, MemoryUpdate, NotebookCreate
from app.services.sqlite_repository import (
    SQLiteRepository,
    reset_request_user,
    set_request_user,
)


class _KgStub:
    """Duck-typed stand-in for SourceIngestionService's four memory-KG
    primitives (see backend/app/services/source_ingestion.py)."""

    def __init__(self, eligible: bool = True) -> None:
        self.calls: list[tuple[str, str]] = []
        self._eligible = eligible
        self._sources: dict[str, str] = {}

    def memory_kg_eligible(self, notebook_id: str) -> bool:
        return self._eligible

    def memory_source_id(self, memory_id: str) -> "str | None":
        return self._sources.get(memory_id)

    def ingest_memory_source(self, notebook_id, memory_id, title, content_md) -> str:
        self.calls.append(("ingest", memory_id))
        self._sources[memory_id] = f"src-{memory_id}"
        return self._sources[memory_id]

    def remove_memory_source(self, memory_id: str) -> None:
        self.calls.append(("remove", memory_id))
        self._sources.pop(memory_id, None)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'memory-kg-lifecycle.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


@pytest.fixture
def owner(repo):
    return repo.create_user("a00123456", "pw")


@pytest.fixture
def notebook(repo, owner):
    token = set_request_user(owner)
    try:
        return repo.create_notebook(NotebookCreate(name="Memory KG lifecycle"))
    finally:
        reset_request_user(token)


@pytest.fixture
def memory_service(repo):
    return repo._runtime.memory_service


def _candidate(service, notebook_id, user_id, request_id, title="Title", body="Body"):
    return service.create_candidate(
        notebook_id, user_id, None, request_id, title, body, [], "reason", {}, [],
    )


def _save_answer(repo, notebook_id, user_id, question, answer_text):
    return repo._runtime.ask_state.save_answer(
        notebook_id,
        None,
        question,
        AskResponse(conclusion=answer_text, answer=answer_text),
        user_id,
    )


# 1) confirm defaults to triggering ingest; an explicit extract_kg=False
#    payload does not; neither does an ineligible notebook.
def test_confirm_hooks_default_ingest_explicit_optout_and_ineligible(
    memory_service, owner, notebook
):
    kg = _KgStub(eligible=True)
    memory_service.set_memory_kg_service(kg)
    memory_service.kg_ingest_scheduler = lambda fn, item: fn(item)

    default_item = _candidate(memory_service, notebook.id, owner.id, "req-confirm-default")
    memory_service.confirm(default_item.id, owner.id)
    assert kg.calls == [("ingest", default_item.id)]

    kg.calls.clear()
    optout_item = _candidate(memory_service, notebook.id, owner.id, "req-confirm-optout")
    memory_service.confirm(
        optout_item.id, owner.id, MemoryReviewRequest(extract_kg=False)
    )
    assert kg.calls == []

    kg.calls.clear()
    kg._eligible = False
    ineligible_item = _candidate(memory_service, notebook.id, owner.id, "req-confirm-ineligible")
    memory_service.confirm(ineligible_item.id, owner.id)
    assert kg.calls == []


# 2) create_from_answer (born confirmed) defaults to triggering ingest;
#    extract_kg=False does not.
def test_create_from_answer_hooks_default_ingest_and_explicit_optout(
    repo, memory_service, owner, notebook
):
    kg = _KgStub(eligible=True)
    memory_service.set_memory_kg_service(kg)
    memory_service.kg_ingest_scheduler = lambda fn, item: fn(item)

    default_answer = _save_answer(repo, notebook.id, owner.id, "Q1?", "A1.")
    created = memory_service.create_from_answer(
        notebook.id, owner.id, default_answer, "T1", "B1", []
    )
    assert kg.calls == [("ingest", created.id)]

    kg.calls.clear()
    optout_answer = _save_answer(repo, notebook.id, owner.id, "Q2?", "A2.")
    created_optout = memory_service.create_from_answer(
        notebook.id, owner.id, optout_answer, "T2", "B2", [], extract_kg=False
    )
    assert kg.calls == []
    assert kg.memory_source_id(created_optout.id) is None


# 3) update() on a confirmed Memory re-triggers ingest only when a derived
#    source already exists; a Memory that opted out at confirm time (so no
#    derived source was ever created) stays silent on every later edit.
def test_update_reingests_when_derived_source_exists_else_stays_silent(
    memory_service, owner, notebook
):
    kg = _KgStub(eligible=True)
    memory_service.set_memory_kg_service(kg)
    memory_service.kg_ingest_scheduler = lambda fn, item: fn(item)

    with_source = _candidate(memory_service, notebook.id, owner.id, "req-update-with-source")
    memory_service.confirm(with_source.id, owner.id)
    assert kg.calls == [("ingest", with_source.id)]
    kg.calls.clear()

    memory_service.update(
        with_source.id, owner.id, MemoryUpdate(content_md="Updated body")
    )
    assert kg.calls == [("ingest", with_source.id)]

    kg.calls.clear()
    without_source = _candidate(
        memory_service, notebook.id, owner.id, "req-update-without-source"
    )
    memory_service.confirm(
        without_source.id, owner.id, MemoryReviewRequest(extract_kg=False)
    )
    assert kg.calls == []

    memory_service.update(
        without_source.id, owner.id, MemoryUpdate(content_md="Still opted out")
    )
    assert kg.calls == []


# 4) deprecate() synchronously calls remove_memory_source.
def test_deprecate_removes_derived_source(memory_service, owner, notebook):
    kg = _KgStub(eligible=True)
    memory_service.set_memory_kg_service(kg)
    memory_service.kg_ingest_scheduler = lambda fn, item: fn(item)

    item = _candidate(memory_service, notebook.id, owner.id, "req-deprecate")
    memory_service.confirm(item.id, owner.id)
    kg.calls.clear()

    memory_service.deprecate(item.id, owner.id)
    assert kg.calls == [("remove", item.id)]
    assert kg.memory_source_id(item.id) is None


# 5) reject() and edits to a still-candidate Memory never call the KG
#    service.
def test_reject_and_candidate_edits_never_touch_kg_service(
    memory_service, owner, notebook
):
    kg = _KgStub(eligible=True)
    memory_service.set_memory_kg_service(kg)
    memory_service.kg_ingest_scheduler = lambda fn, item: fn(item)

    rejected = _candidate(memory_service, notebook.id, owner.id, "req-reject")
    memory_service.reject(rejected.id, owner.id)
    assert kg.calls == []

    candidate = _candidate(memory_service, notebook.id, owner.id, "req-candidate-edit")
    memory_service.update(
        candidate.id, owner.id, MemoryUpdate(title="Edited while still a candidate")
    )
    assert kg.calls == []


# 6) a job scheduled (but not yet run) before the Memory is deprecated
#    re-reads Memory state when it finally runs and skips.
def test_kg_job_skips_when_memory_deprecated_before_it_runs(
    memory_service, owner, notebook
):
    kg = _KgStub(eligible=True)
    memory_service.set_memory_kg_service(kg)
    scheduled: list[tuple] = []
    memory_service.kg_ingest_scheduler = lambda fn, item: scheduled.append((fn, item))

    item = _candidate(memory_service, notebook.id, owner.id, "req-race")
    memory_service.confirm(item.id, owner.id)
    assert len(scheduled) == 1
    assert kg.calls == []  # job merely queued, not run yet

    memory_service.deprecate(item.id, owner.id)
    assert kg.calls == [("remove", item.id)]

    fn, key = scheduled[0]
    fn(key)  # run the deferred confirm-triggered job now, after deprecation

    assert kg.calls == [("remove", item.id)]  # unchanged: ingest never lands


# 7) with no KG service injected, the lifecycle behaves exactly as before
#    this feature existed: zero side effects, zero crashes.
def test_lifecycle_is_unaffected_when_no_kg_service_is_injected(
    memory_service, owner, notebook
):
    memory_service.memory_kg = None  # as if set_memory_kg_service() was never called
    memory_service.kg_ingest_scheduler = lambda fn, item: fn(item)

    item = _candidate(memory_service, notebook.id, owner.id, "req-no-kg-service")
    confirmed = memory_service.confirm(item.id, owner.id)
    assert confirmed.status == "confirmed"

    updated = memory_service.update(
        confirmed.id, owner.id, MemoryUpdate(content_md="Updated without kg service")
    )
    assert updated.content_md == "Updated without kg service"

    deprecated = memory_service.deprecate(confirmed.id, owner.id)
    assert deprecated.status == "deprecated"


# 8) Review Important 1: deprecate landing WHILE ingest_memory_source is
#    still running (seconds-long in production). At that instant deprecate's
#    remove_memory_source is a no-op — the derived source doesn't exist yet —
#    and confirmed->deprecated is one-way so deprecate can never fire again.
#    Without a post-ingest re-check the job then creates a permanently
#    orphaned derived source.
def test_kg_job_cleans_up_derived_source_when_deprecate_lands_mid_ingest(
    memory_service, owner, notebook
):
    class _MidIngestDeprecate(_KgStub):
        def __init__(self, service, user_id):
            super().__init__(eligible=True)
            self._service = service
            self._user_id = user_id

        def ingest_memory_source(self, notebook_id, memory_id, title, content_md):
            # The user deprecates while extraction is still running: the
            # remove below fires against a not-yet-created source (no-op),
            # then the ingest completes and creates it.
            self._service.deprecate(memory_id, self._user_id)
            return super().ingest_memory_source(
                notebook_id, memory_id, title, content_md
            )

    kg = _MidIngestDeprecate(memory_service, owner.id)
    memory_service.set_memory_kg_service(kg)
    memory_service.kg_ingest_scheduler = lambda fn, item: fn(item)

    item = _candidate(memory_service, notebook.id, owner.id, "req-mid-ingest-race")
    memory_service.confirm(item.id, owner.id)

    # deprecate's no-op remove, the ingest that outraced it, then the job's
    # own post-ingest cleanup — the derived source must not leak.
    assert kg.calls == [
        ("remove", item.id),
        ("ingest", item.id),
        ("remove", item.id),
    ]
    assert kg.memory_source_id(item.id) is None
    assert memory_service.get(item.id, owner.id).status == "deprecated"


# 9) Review Minor 4: ingest_memory_source's documented notebook-not-found
#    KeyError (its guard precedes any insert, so nothing was created) must be
#    swallowed by the same job guard as the memory lookup — never crash the
#    scheduler thread / the synchronous caller.
def test_kg_job_tolerates_ingest_notebook_keyerror(memory_service, owner, notebook):
    class _NotebookGone(_KgStub):
        def ingest_memory_source(self, notebook_id, memory_id, title, content_md):
            self.calls.append(("ingest", memory_id))
            raise KeyError(notebook_id)

    kg = _NotebookGone(eligible=True)
    memory_service.set_memory_kg_service(kg)
    memory_service.kg_ingest_scheduler = lambda fn, item: fn(item)

    item = _candidate(memory_service, notebook.id, owner.id, "req-nb-gone")
    confirmed = memory_service.confirm(item.id, owner.id)  # must not raise

    assert confirmed.status == "confirmed"
    assert kg.calls == [("ingest", item.id)]


# 10) Review Minor 3: confirm()'s extract_kg read must see dict patches too
#     (the signature accepts Mapping), not only pydantic models.
def test_confirm_reads_extract_kg_from_mapping_patch(memory_service, owner, notebook):
    kg = _KgStub(eligible=True)
    memory_service.set_memory_kg_service(kg)
    memory_service.kg_ingest_scheduler = lambda fn, item: fn(item)

    optout = _candidate(memory_service, notebook.id, owner.id, "req-dict-optout")
    memory_service.confirm(
        optout.id, owner.id, {"extract_kg": False, "title": "Edited via dict"}
    )
    assert kg.calls == []
    assert memory_service.get(optout.id, owner.id).title == "Edited via dict"

    default = _candidate(memory_service, notebook.id, owner.id, "req-dict-default")
    memory_service.confirm(default.id, owner.id, {"reason": "looks right"})
    assert kg.calls == [("ingest", default.id)]


# 11) Review Minor 5a: the real runtime injects the real SourceIngestionService
#     instance (identity, not a wrapper) as MemoryService's kg bridge.
def test_runtime_wires_source_ingestion_as_memory_kg_service(repo):
    assert repo._runtime.memory_service.memory_kg is repo._runtime.source_ingestion


# 12) Review Minor 5b: end-to-end offline smoke through the REAL services (no
#     stub): confirm -> derived source_type='memory' row via the real
#     ingest pipeline; offline no-llm may land extracted or failed; and no
#     chunks are ever built for a memory-derived source.
def test_confirm_end_to_end_builds_memory_derived_source_offline(
    repo, memory_service, owner, notebook
):
    # Open the auto-extract gate exactly like test_memory_source_ingestion's
    # truth table: one KG object flips should_extract_kg via notebook_has_kg.
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,created_at,updated_at) "
            "VALUES ('ko-e2e-gate',?,'concept','t','t')",
            (notebook.id,),
        )
    # Real MemoryService + real SourceIngestionService; only the schedulers
    # are made synchronous (the constructor kwarg's documented test mode) so
    # assertions don't race background threads.
    memory_service.kg_ingest_scheduler = lambda fn, item: fn(item)
    memory_service.embedding_scheduler = lambda fn, job: fn(job)

    item = _candidate(
        memory_service, notebook.id, owner.id, "req-e2e-offline", body="正文内容"
    )
    memory_service.confirm(item.id, owner.id)

    source_id = repo._runtime.source_ingestion.memory_source_id(item.id)
    assert source_id is not None
    src = repo._runtime.source_ingestion.sources.get_source(source_id)
    assert src.type == "memory"
    assert src.parse_status in ("extracted", "failed")  # offline no-llm: either
    with repo._runtime.database.connect() as db:
        (n_chunks,) = db.execute(
            "SELECT COUNT(*) FROM chunks WHERE source_id=?", (source_id,)
        ).fetchone()
    assert n_chunks == 0
