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
