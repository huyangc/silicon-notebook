"""``PaperCardService.get_cards`` on SQLite with a fake ``paper_metadata`` model
(spec 2026-10-28 §4.5). PostgreSQL end-to-end twin:
``tests/postgres/test_paper_card_service_pg.py``."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import Settings
from app.domain.cancellation import AskCancelled
from app.models.schemas import NotebookCreate
from app.services import paper_card_service as service_module
from app.services.model_work import ModelQueueFull, ModelQueueTimeout
from app.services.paper_cards import PAPER_CARD_VERSION
from app.services.sqlite_repository import SQLiteRepository
from tests import paper_card_testkit as kit
from tests.model_testkit import RecordingModelProvider, bind_chat_client


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    provider = RecordingModelProvider(parallelism_by_workload={"paper_metadata": 4})
    r = SQLiteRepository(Settings(), model_provider=provider)
    yield r
    r.close()


@pytest.fixture
def model(repo):
    fake = kit.FakePaperModel()
    bind_chat_client(repo, "paper_metadata", fake)
    return fake


@pytest.fixture
def nb(repo) -> str:
    return repo.create_notebook(NotebookCreate(name="nb")).id


@pytest.fixture
def service(repo):
    return repo._runtime.paper_cards


@pytest.fixture
def events(service, monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(service._event_log, "emit", lambda event, **_: seen.append(dict(event)))
    return seen


def _paper(repo, nb, sid, *, meta=True, **kwargs):
    kit.seed_source(repo, nb, sid, **kwargs)
    if meta is not None:
        kit.set_paper_meta(repo, nb, sid, is_paper=meta)


def _get(service, nb, sids, *, limit=10, allowed=None, cancel_event=None):
    items = [(nb, sid) for sid in sids]
    return service.get_cards(
        items, allowed=items if allowed is None else allowed,
        generate_limit=limit, cancel_event=cancel_event,
    )


def _statuses(results):
    return [(r.source_id, r.status, r.reason) for r in results]


def _card_row(repo, nb, sid):
    with repo._runtime.database.connect() as db:
        return repo._runtime.source_store.paper_cards_for_sources(db, [sid], notebook_id=nb).get(sid)


# ----------------------------------------------------------------- hit / miss


def test_generates_then_hits_the_cache(repo, nb, service, model, events):
    _paper(repo, nb, "s1")
    [first] = _get(service, nb, ["s1"])
    assert first.status == "generated"
    assert first.card["metrics"][0]["value"] == "85.2"
    assert first.card["metrics"][0]["anchor"] == "el-s1-0004"   # the table element id
    assert first.model == "fake-paper"
    row = _card_row(repo, nb, "s1")
    assert row["status"] == "ready" and row["card_version"] == PAPER_CARD_VERSION
    assert row["basis"] == repo._runtime.source_store.source_card_basis("s1")
    assert row["raw_json"]["input"]["elements"] >= 3
    assert model.card_calls == 1 and model.meta_calls == 0
    [again] = _get(service, nb, ["s1"])
    assert (again.status, again.card) == ("ready", first.card)
    assert model.card_calls == 1
    assert [e["status"] for e in events] == ["generated", "ready"]
    assert set(events[0]) == {
        "kind", "source_id", "notebook_id", "status", "reason", "dropped", "elapsed_ms",
    }


def test_a_new_element_generation_invalidates_the_card(repo, nb, service, model):
    _paper(repo, nb, "s1")
    _get(service, nb, ["s1"])
    kit.replace_elements(repo, "s1", created_at="2026-02-01T00:00:00+00:00")
    [result] = _get(service, nb, ["s1"])
    assert result.status == "generated"
    assert model.card_calls == 2


def test_a_new_card_version_invalidates_the_card(repo, nb, service, model, monkeypatch):
    _paper(repo, nb, "s1")
    _get(service, nb, ["s1"])
    monkeypatch.setattr(service_module, "PAPER_CARD_VERSION", PAPER_CARD_VERSION + 1)
    [result] = _get(service, nb, ["s1"])
    assert result.status == "generated"
    assert _card_row(repo, nb, "s1")["card_version"] == PAPER_CARD_VERSION + 1


def test_reclassified_as_not_paper_hides_the_cached_card(repo, nb, service, model):
    _paper(repo, nb, "s1")
    _get(service, nb, ["s1"])
    kit.set_paper_meta(repo, nb, "s1", is_paper=False)
    assert _statuses(_get(service, nb, ["s1"])) == [("s1", "not_paper", "")]


# ----------------------------------------------------------------- scope


def test_sources_outside_the_allowed_set_are_refused(repo, nb, service, model):
    _paper(repo, nb, "s1")
    _paper(repo, nb, "s2")
    results = _get(service, nb, ["s1", "s2"], allowed=[(nb, "s1")])
    assert _statuses(results)[1] == ("s2", "not_found", "out_of_scope")
    assert model.card_calls == 1


def test_a_source_claimed_under_the_wrong_notebook_is_not_found(repo, nb, service, model):
    other = repo.create_notebook(NotebookCreate(name="other")).id
    _paper(repo, nb, "s1")
    results = service.get_cards([(other, "s1")], allowed=[(other, "s1")], generate_limit=5)
    assert _statuses(results) == [("s1", "not_found", "missing")]
    assert model.card_calls == 0


def test_duplicates_collapse_and_order_is_kept(repo, nb, service, model):
    for sid in ("s1", "s2"):
        _paper(repo, nb, sid)
    results = _get(service, nb, ["s2", "s1", "s2"])
    assert [r.source_id for r in results] == ["s2", "s1"]


# ----------------------------------------------------------------- metadata step


def test_metadata_step_counts_against_the_limit(repo, nb, service, model):
    _paper(repo, nb, "s1", meta=None)
    [result] = _get(service, nb, ["s1"], limit=1)
    assert (result.status, result.reason) == ("deferred", "generate_limit")
    assert (model.meta_calls, model.card_calls) == (1, 0)
    assert repo._runtime.source_store.get_paper_meta("s1", nb)["is_paper"] is True
    [result] = _get(service, nb, ["s1"], limit=1)
    assert result.status == "generated"
    assert (model.meta_calls, model.card_calls) == (1, 1)


def test_metadata_then_card_within_one_call(repo, nb, service, model):
    _paper(repo, nb, "s1", meta=None)
    cancel = threading.Event()
    [result] = _get(service, nb, ["s1"], limit=2, cancel_event=cancel)
    assert result.status == "generated"
    assert (model.meta_calls, model.card_calls) == (1, 1)
    # the interactive path hands the cancel token to both model calls
    assert model.cancel_events == [cancel, cancel]


def test_metadata_says_not_a_paper(repo, nb, service, model):
    model.is_paper = False
    _paper(repo, nb, "s1", meta=None)
    assert _statuses(_get(service, nb, ["s1"])) == [("s1", "not_paper", "")]
    assert model.card_calls == 0


def test_ineligible_doc_type_or_synthetic_source_is_not_a_paper(repo, nb, service, model):
    _paper(repo, nb, "s1", meta=None, doc_type="textbook")
    _paper(repo, nb, "s2", meta=None, source_type="memory")
    assert [r.status for r in _get(service, nb, ["s1", "s2"])] == ["not_paper", "not_paper"]
    assert model.meta_calls == 0


@pytest.mark.parametrize(("metadata_status", "expected"), [
    ("not_paper", ("not_paper", "")),
    ("disabled", ("meta_pending", "metadata_disabled")),
    ("no_llm", ("meta_pending", "metadata_no_llm")),
    ("no_text", ("meta_pending", "metadata_no_text")),
    ("failed", ("failed", "metadata_failed")),
    ("skipped", ("meta_pending", "metadata_skipped")),
    ("something-new", ("failed", "metadata_failed")),
])
def test_every_metadata_outcome_is_mapped(repo, nb, service, model, monkeypatch, metadata_status, expected):
    _paper(repo, nb, "s1", meta=None)
    monkeypatch.setattr(
        service._ingestion, "extract_paper_metadata_cancellable",
        lambda source, **_: metadata_status,
    )
    [result] = _get(service, nb, ["s1"])
    assert (result.status, result.reason) == expected
    assert model.card_calls == 0
    assert _card_row(repo, nb, "s1") is None


def test_metadata_skipped_because_someone_just_stored_it(repo, nb, service, model, monkeypatch):
    _paper(repo, nb, "s1", meta=None)

    def concurrent_store(source, **_):
        kit.set_paper_meta(repo, nb, "s1", is_paper=True)
        return "skipped"

    monkeypatch.setattr(service._ingestion, "extract_paper_metadata_cancellable", concurrent_store)
    [result] = _get(service, nb, ["s1"])
    assert result.status == "generated"


def test_mirrored_notebook_writes_nothing(repo, nb, service, model):
    _paper(repo, nb, "s1", meta=None)
    _paper(repo, nb, "s2")
    repo._runtime.sharing_store.set_notebook_sync_origin(nb, "prod")
    assert _statuses(_get(service, nb, ["s1", "s2"])) == [
        ("s1", "meta_pending", "mirrored"), ("s2", "meta_pending", "mirrored"),
    ]
    assert (model.meta_calls, model.card_calls) == (0, 0)


def test_mirrored_notebook_still_serves_a_valid_card(repo, nb, service, model):
    _paper(repo, nb, "s1")
    _get(service, nb, ["s1"])
    repo._runtime.sharing_store.set_notebook_sync_origin(nb, "prod")
    assert _get(service, nb, ["s1"])[0].status == "ready"


# ----------------------------------------------------------------- limits


def test_generate_limit_defers_the_rest_in_input_order(repo, nb, service, model):
    for sid in ("s1", "s2", "s3"):
        _paper(repo, nb, sid)
    assert _statuses(_get(service, nb, ["s3", "s1", "s2"], limit=2)) == [
        ("s3", "generated", ""), ("s1", "generated", ""), ("s2", "deferred", "generate_limit"),
    ]


def test_disabled_deployment_serves_cache_only(repo, nb, service, model, monkeypatch):
    _paper(repo, nb, "s1")
    _paper(repo, nb, "s2")
    _get(service, nb, ["s1"])
    monkeypatch.setattr(service._settings, "paper_card_enabled", False)
    assert _statuses(_get(service, nb, ["s1", "s2"])) == [
        ("s1", "ready", ""), ("s2", "failed", "disabled"),
    ]
    assert model.card_calls == 1


# ----------------------------------------------------------------- failures


def test_failure_cools_down_then_retries(repo, nb, service, model, monkeypatch):
    _paper(repo, nb, "s1")
    model.reply = lambda prompt: (_ for _ in ()).throw(RuntimeError("provider down"))
    [first] = _get(service, nb, ["s1"])
    assert (first.status, first.reason) == ("failed", "model_error:RuntimeError")
    assert _card_row(repo, nb, "s1")["attempts"] == 1
    [second] = _get(service, nb, ["s1"])
    assert (second.status, second.reason) == ("failed", "cooldown")
    assert model.card_calls == 1
    later = datetime.now(timezone.utc) + timedelta(seconds=3601)
    monkeypatch.setattr(service, "_now", lambda: later)
    model.reply = kit.card_reply
    assert _get(service, nb, ["s1"])[0].status == "generated"


def test_three_failures_cool_down_for_a_day(repo, nb, service, model, monkeypatch):
    _paper(repo, nb, "s1")
    model.reply = lambda prompt: "[]"
    clock = [datetime.now(timezone.utc)]
    monkeypatch.setattr(service, "_now", lambda: clock[0])
    for attempt in range(3):
        clock[0] += timedelta(hours=2)
        assert _get(service, nb, ["s1"])[0].reason == "malformed"
    assert _card_row(repo, nb, "s1")["attempts"] == 3
    clock[0] += timedelta(hours=2)
    assert _get(service, nb, ["s1"])[0].reason == "cooldown"
    clock[0] += timedelta(hours=23)
    assert _get(service, nb, ["s1"])[0].reason == "malformed"


def test_an_all_empty_card_is_a_failure_with_its_audit(repo, nb, service, model):
    _paper(repo, nb, "s1")
    model.reply = lambda prompt: {"tldr": {"text": "x", "anchors": ["e99"]}}
    [result] = _get(service, nb, ["s1"])
    assert (result.status, result.reason) == ("failed", "empty_card")
    row = _card_row(repo, nb, "s1")
    assert row["card"] == {} and row["raw_json"]["reason"] == "empty_card"
    assert row["raw_json"]["dropped"]["tldr"][0]["reason"] == "no_valid_anchor"


@pytest.mark.parametrize("error", [ModelQueueTimeout, ModelQueueFull])
def test_queue_timeout_and_full_defer_without_recording_a_failure(repo, nb, service, model, error):
    _paper(repo, nb, "s1")

    def busy(prompt):
        raise error()

    model.reply = busy
    [result] = _get(service, nb, ["s1"])
    assert (result.status, result.reason) == ("deferred", "model_queue")
    assert _card_row(repo, nb, "s1") is None


@pytest.mark.parametrize("error", [ModelQueueTimeout, ModelQueueFull])
def test_queue_errors_in_the_metadata_step_propagate_from_the_variant(repo, nb, service, model, error):
    # The cancellable metadata variant re-raises scheduling errors (the original
    # entry swallows them); the card service turns them into ``deferred``.
    _paper(repo, nb, "s1", meta=None)

    class Busy:
        configured = True
        model = "busy"

        def chat_json(self, *args, **kwargs):
            raise error()

    source = repo._runtime.source_store.get_source("s1")
    ingestion = service._ingestion
    with pytest.raises(error):
        ingestion.extract_paper_metadata_cancellable(source, cancel_event=None, client=Busy())
    assert ingestion.ensure_paper_metadata(source, client=Busy()) == "failed"
    bind_chat_client(repo, "paper_metadata", Busy())
    [result] = _get(service, nb, ["s1"])
    assert (result.status, result.reason) == ("deferred", "model_queue")
    assert repo._runtime.source_store.get_paper_meta("s1", nb) is None


def test_cancellation_in_the_metadata_variant_propagates(repo, nb, service, model):
    _paper(repo, nb, "s1", meta=None)
    cancel = threading.Event()
    cancel.set()
    source = repo._runtime.source_store.get_source("s1")
    with pytest.raises(AskCancelled):
        service._ingestion.extract_paper_metadata_cancellable(source, cancel_event=cancel, client=model)
    assert model.meta_calls == 0


def test_failed_never_overwrites_a_concurrently_written_ready_card(repo, nb, service, model):
    _paper(repo, nb, "s1")
    store = repo._runtime.source_store

    def race_then_fail(prompt):
        store.upsert_paper_card(
            "s1", nb, basis=store.source_card_basis("s1"), card_version=PAPER_CARD_VERSION,
            status="ready", card={"tldr": {"text": "other", "anchors": []}}, raw_json={}, model="other",
        )
        raise RuntimeError("late failure")

    model.reply = race_then_fail
    [result] = _get(service, nb, ["s1"])
    assert (result.status, result.model) == ("ready", "other")
    assert _card_row(repo, nb, "s1")["status"] == "ready"


def test_a_card_for_a_stale_basis_is_not_stored(repo, nb, service, model):
    _paper(repo, nb, "s1")

    def reparse_then_answer(prompt):
        kit.replace_elements(repo, "s1", created_at="2026-03-01T00:00:00+00:00")
        return kit.card_reply(prompt)

    model.reply = reparse_then_answer
    [result] = _get(service, nb, ["s1"])
    assert (result.status, result.reason) == ("deferred", "source_changed")
    assert _card_row(repo, nb, "s1") is None


def test_source_deleted_before_the_write_is_not_found(repo, nb, service, model):
    _paper(repo, nb, "s1")

    def delete_then_answer(prompt):
        with repo._write() as db:
            db.execute("DELETE FROM sources WHERE id = ?", ("s1",))
        return kit.card_reply(prompt)

    model.reply = delete_then_answer
    assert _statuses(_get(service, nb, ["s1"])) == [("s1", "not_found", "missing")]


# ----------------------------------------------------------------- concurrency


def test_cancelled_before_start_raises(repo, nb, service, model):
    _paper(repo, nb, "s1")
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(AskCancelled):
        _get(service, nb, ["s1"], cancel_event=cancel)
    assert model.card_calls == 0


def test_cancellation_during_generation_propagates_and_records_nothing(repo, nb, service, model):
    _paper(repo, nb, "s1")
    cancel = threading.Event()

    def cancelled(prompt):
        cancel.set()
        raise AskCancelled()

    model.reply = cancelled
    with pytest.raises(AskCancelled):
        _get(service, nb, ["s1"], cancel_event=cancel)
    assert _card_row(repo, nb, "s1") is None
    assert model.cancel_events == [cancel]
    # the single-flight seat was released
    assert service._flights._flights == {}


def test_concurrent_requests_for_one_source_generate_once(repo, nb, service, model):
    _paper(repo, nb, "s1")
    entered, release = threading.Event(), threading.Event()

    def slow(prompt):
        entered.set()
        assert release.wait(5)

    model.on_card = slow
    results: dict[str, object] = {}
    leader = threading.Thread(target=lambda: results.update(a=_get(service, nb, ["s1"])[0]))
    leader.start()
    assert entered.wait(5)
    waiter = threading.Thread(target=lambda: results.update(b=_get(service, nb, ["s1"])[0]))
    waiter.start()
    time.sleep(0.3)
    assert waiter.is_alive()   # waiting outside the pool for the leader
    release.set()
    leader.join(5)
    waiter.join(5)
    assert results["a"].status == "generated"
    assert results["b"].status == "ready"
    assert model.card_calls == 1


def test_a_waiter_still_honours_its_own_cancellation(repo, nb, service, model):
    _paper(repo, nb, "s1")
    entered, release = threading.Event(), threading.Event()
    model.on_card = lambda prompt: (entered.set(), release.wait(5))
    leader = threading.Thread(target=lambda: _get(service, nb, ["s1"]))
    leader.start()
    assert entered.wait(5)
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    with pytest.raises(AskCancelled):
        _get(service, nb, ["s1"], cancel_event=cancel)
    release.set()
    leader.join(5)


def test_pool_is_bounded_by_the_service_parallelism(repo, nb, service, model, monkeypatch):
    for sid in ("s1", "s2", "s3", "s4", "s5"):
        _paper(repo, nb, sid)
    monkeypatch.setattr(service._models, "parallelism_by_workload", {"paper_metadata": 2})
    active, peak, lock = [0], [0], threading.Lock()

    def track(prompt):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        with lock:
            active[0] -= 1

    model.on_card = track
    results = _get(service, nb, ["s1", "s2", "s3", "s4", "s5"])
    assert {r.status for r in results} == {"generated"}
    assert peak[0] <= 2
