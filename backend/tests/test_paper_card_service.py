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
from tests.model_testkit import RecordingModelProvider


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
    """The fake sits behind the real scheduled adapter, so every failure it raises
    reaches the service wrapped (``ModelInvocationError`` with a stable code)."""
    fake = kit.FakePaperModel()
    with kit.scheduled_paper_model(repo, fake):
        yield fake


@pytest.fixture
def nb(repo) -> str:
    return repo.create_notebook(NotebookCreate(name="nb")).id


@pytest.fixture
def service(repo):
    return repo._runtime.paper_cards


@pytest.fixture
def events(service, monkeypatch):
    seen: list[dict] = []
    original = service._event_log.emit

    def capture(event, **kwargs):
        if event.get("kind") == "paper_card":
            seen.append(dict(event))
        else:
            original(event, **kwargs)

    monkeypatch.setattr(service._event_log, "emit", capture)
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


@pytest.mark.parametrize(("metadata_status", "expected", "row"), [
    ("not_paper", ("not_paper", ""), False),
    ("disabled", ("meta_pending", "metadata_disabled"), False),
    ("no_llm", ("meta_pending", "metadata_no_llm"), False),
    ("no_text", ("meta_pending", "metadata_no_text"), False),
    ("skipped", ("meta_pending", "metadata_skipped"), False),
    ("failed", ("failed", "metadata_failed"), True),      # content failure: cooled down
    ("error", ("failed", "metadata_error"), False),       # auth/config: no row
    ("something-new", ("failed", "metadata_error"), False),
])
def test_every_metadata_outcome_is_mapped(repo, nb, service, model, monkeypatch, metadata_status, expected, row):
    _paper(repo, nb, "s1", meta=None)
    monkeypatch.setattr(
        service._ingestion, "extract_paper_metadata_cancellable",
        lambda source, **_: metadata_status,
    )
    [result] = _get(service, nb, ["s1"])
    assert (result.status, result.reason) == expected
    assert model.card_calls == 0
    stored = _card_row(repo, nb, "s1")
    if row:
        assert (stored["status"], stored["raw_json"]["reason"], stored["attempts"]) == ("failed", "metadata_failed", 1)
        assert stored["basis"] == repo._runtime.source_store.source_card_basis("s1")
    else:
        assert stored is None


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


def _malformed(prompt):
    return "[]"   # the scheduled adapter rejects it: code malformed_response


def test_content_failure_cools_down_then_retries(repo, nb, service, model, monkeypatch):
    _paper(repo, nb, "s1")
    model.reply = _malformed
    [first] = _get(service, nb, ["s1"])
    assert (first.status, first.reason) == ("failed", "malformed_response")
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
    model.reply = _malformed
    clock = [datetime.now(timezone.utc)]
    monkeypatch.setattr(service, "_now", lambda: clock[0])
    for _attempt in range(3):
        clock[0] += timedelta(hours=2)
        assert _get(service, nb, ["s1"])[0].reason == "malformed_response"
    assert _card_row(repo, nb, "s1")["attempts"] == 3
    clock[0] += timedelta(hours=2)
    assert _get(service, nb, ["s1"])[0].reason == "cooldown"
    clock[0] += timedelta(hours=23)
    assert _get(service, nb, ["s1"])[0].reason == "malformed_response"


def test_an_unparsable_object_is_a_content_failure(repo, nb, service, model, monkeypatch):
    _paper(repo, nb, "s1")
    # A valid object for the scheduled contract but nothing the verifier can keep.
    model.reply = lambda prompt: {"tldr": {"text": "x", "anchors": ["e99"]}, "problem": {"text": None, "anchors": []},
                                  "method": {"text": None, "anchors": []}, "contributions": [],
                                  "metrics": [], "datasets": []}
    [result] = _get(service, nb, ["s1"])
    assert (result.status, result.reason) == ("failed", "empty_card")
    row = _card_row(repo, nb, "s1")
    assert row["card"] == {} and row["raw_json"]["reason"] == "empty_card"
    assert row["raw_json"]["dropped"]["tldr"][0]["reason"] == "no_valid_anchor"


@pytest.mark.parametrize("error", [
    lambda: ModelQueueTimeout(),
    lambda: ModelQueueFull(),
    lambda: kit.StatusError(429),
    lambda: kit.StatusError(503),
    lambda: TimeoutError("read timed out"),
], ids=["queue_timeout", "queue_full", "rate_limited", "unavailable_5xx", "timeout"])
def test_transient_failures_defer_and_never_touch_the_row(repo, nb, service, model, monkeypatch, error):
    _paper(repo, nb, "s1")
    clock = [datetime.now(timezone.utc)]
    monkeypatch.setattr(service, "_now", lambda: clock[0])
    model.reply = _malformed
    _get(service, nb, ["s1"])                         # one real content failure: attempts=1
    clock[0] += timedelta(hours=2)                   # cooldown over

    def busy(prompt):
        raise error()

    model.reply = busy
    [result] = _get(service, nb, ["s1"])
    assert (result.status, result.reason) == ("deferred", "model_unavailable")
    row = _card_row(repo, nb, "s1")
    assert (row["attempts"], row["raw_json"]["reason"]) == (1, "malformed_response")


def test_a_transient_failure_on_a_fresh_source_writes_nothing(repo, nb, service, model):
    _paper(repo, nb, "s1")

    def busy(prompt):
        raise ModelQueueTimeout()

    model.reply = busy
    assert _get(service, nb, ["s1"])[0].status == "deferred"
    assert _card_row(repo, nb, "s1") is None


def test_an_auth_failure_is_failed_without_a_row(repo, nb, service, model):
    _paper(repo, nb, "s1")

    def denied(prompt):
        raise kit.StatusError(401)

    model.reply = denied
    [result] = _get(service, nb, ["s1"])
    assert (result.status, result.reason) == ("failed", "model_error:provider_auth")
    assert _card_row(repo, nb, "s1") is None


class _CodedError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@pytest.mark.parametrize(("error", "reason"), [
    (lambda: _CodedError("context_length_exceeded"), "model_error:model_context_exceeded"),
    (lambda: _CodedError("model_rejected"), "model_error:model_rejected"),
    (lambda: kit.StatusError(400), "model_error:provider_error"),
    (lambda: kit.StatusError(422), "model_error:provider_error"),
], ids=["context_exceeded", "rejected", "http_400", "http_422"])
def test_input_deterministic_failures_are_cooled_down(repo, nb, service, model, error, reason):
    _paper(repo, nb, "s1")

    def fail(prompt):
        raise error()

    model.reply = fail
    [first] = _get(service, nb, ["s1"])
    assert (first.status, first.reason) == ("failed", reason)
    row = _card_row(repo, nb, "s1")
    assert (row["status"], row["raw_json"]["reason"], row["attempts"]) == ("failed", reason, 1)
    assert _get(service, nb, ["s1"])[0].reason == "cooldown"
    assert model.card_calls == 1


@pytest.mark.parametrize(("error", "reason"), [
    (lambda: kit.StatusError(403), "model_error:provider_auth"),
    (lambda: _CodedError("unknown_model"), "model_error:unknown_model"),
    (lambda: _CodedError("protocol_mismatch"), "model_error:protocol_mismatch"),
], ids=["auth", "unknown_model", "protocol_mismatch"])
def test_global_config_failures_write_no_row(repo, nb, service, model, error, reason):
    _paper(repo, nb, "s1")

    def fail(prompt):
        raise error()

    model.reply = fail
    [result] = _get(service, nb, ["s1"])
    assert (result.status, result.reason) == ("failed", reason)
    assert _card_row(repo, nb, "s1") is None


def test_an_input_deterministic_metadata_failure_is_cooled_down(repo, nb, service, model):
    _paper(repo, nb, "s1", meta=None)

    def too_long(prompt):
        raise _CodedError("context_length_exceeded")

    model.on_meta = too_long
    assert _get(service, nb, ["s1"])[0].reason == "metadata_failed"
    assert _get(service, nb, ["s1"])[0].reason == "cooldown"
    assert model.meta_calls == 1


@pytest.mark.parametrize("error", [
    lambda: ModelQueueTimeout(), lambda: ModelQueueFull(), lambda: kit.StatusError(429),
    lambda: kit.StatusError(502), lambda: TimeoutError("timed out"),
], ids=["queue_timeout", "queue_full", "rate_limited", "unavailable_5xx", "timeout"])
def test_transient_failures_in_the_metadata_step_defer(repo, nb, service, model, error):
    _paper(repo, nb, "s1", meta=None)

    def busy(prompt):
        raise error()

    model.on_meta = busy
    source = repo._runtime.source_store.get_source("s1")
    ingestion = service._ingestion
    scheduled = repo._runtime.models.chat("paper_metadata")
    with pytest.raises(Exception) as raised:
        ingestion.extract_paper_metadata_cancellable(source, cancel_event=None, client=scheduled)
    assert raised.value.code in {"model_queue_timeout", "model_queue_full", "provider_rate_limited",
                                 "provider_unavailable", "model_service_unavailable"}
    # the original entry still swallows everything
    assert ingestion.ensure_paper_metadata(source, client=scheduled) == "failed"
    [result] = _get(service, nb, ["s1"])
    assert (result.status, result.reason) == ("deferred", "model_unavailable")
    assert repo._runtime.source_store.get_paper_meta("s1", nb) is None
    assert _card_row(repo, nb, "s1") is None


def test_a_metadata_content_failure_cools_down(repo, nb, service, model):
    _paper(repo, nb, "s1", meta=None)
    model.meta_reply = lambda prompt: {"is_paper": "maybe", "title": "", "authors": [], "venue": "",
                                       "year": 2024, "doi": "", "keywords": []}
    [first] = _get(service, nb, ["s1"])
    assert (first.status, first.reason) == ("failed", "metadata_failed")
    assert _card_row(repo, nb, "s1")["status"] == "failed"
    [second] = _get(service, nb, ["s1"])
    assert (second.status, second.reason) == ("failed", "cooldown")
    assert model.meta_calls == 1


def test_a_metadata_auth_failure_is_not_cooled_down(repo, nb, service, model):
    _paper(repo, nb, "s1", meta=None)

    def denied(prompt):
        raise kit.StatusError(403)

    model.on_meta = denied
    assert _get(service, nb, ["s1"])[0].reason == "metadata_error"
    assert _card_row(repo, nb, "s1") is None     # no failed row, so no cooldown
    # (the scheduler's breaker may now hold the service open; that is transient)
    assert _get(service, nb, ["s1"])[0].status in {"failed", "deferred"}
    assert _card_row(repo, nb, "s1") is None


def test_cancellation_in_the_metadata_variant_propagates(repo, nb, service, model):
    _paper(repo, nb, "s1", meta=None)
    cancel = threading.Event()
    cancel.set()
    source = repo._runtime.source_store.get_source("s1")
    with pytest.raises(AskCancelled):
        service._ingestion.extract_paper_metadata_cancellable(source, cancel_event=cancel, client=model)
    assert model.meta_calls == 0


def test_failed_rows_of_another_basis_or_version_do_not_cool_down(repo, nb, service, model):
    _paper(repo, nb, "s1")
    _paper(repo, nb, "s2")
    store = repo._runtime.source_store
    for _ in range(3):   # attempts=3: a same-basis row would cool down for a day
        store.upsert_paper_card("s1", nb, basis=store.source_card_basis("s1"), card_version=PAPER_CARD_VERSION,
                                status="failed", card={}, raw_json={}, model="m")
        store.upsert_paper_card("s2", nb, basis=store.source_card_basis("s2"),
                                card_version=PAPER_CARD_VERSION - 1, status="failed", card={}, raw_json={}, model="m")
    assert _get(service, nb, ["s1"])[0].reason == "cooldown"
    kit.replace_elements(repo, "s1", created_at="2026-02-01T00:00:00+00:00")   # stale basis now
    assert [r.status for r in _get(service, nb, ["s1", "s2"])] == ["generated", "generated"]


def test_failed_never_overwrites_a_concurrently_written_ready_card(repo, nb, service, model):
    _paper(repo, nb, "s1")
    store = repo._runtime.source_store

    def race_then_malformed(prompt):
        store.upsert_paper_card(
            "s1", nb, basis=store.source_card_basis("s1"), card_version=PAPER_CARD_VERSION,
            status="ready", card={"tldr": {"text": "other", "anchors": []}}, raw_json={}, model="other",
        )
        return "[]"

    model.reply = race_then_malformed
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


# ----------------------------------------------------------------- narrowing / paging


def _legacy_meta(repo, nb, sid, *, is_paper):
    """A ``source_paper_meta`` row whose own notebook_id disagrees with its source."""
    other = repo.create_notebook(NotebookCreate(name=f"legacy-{sid}")).id
    kit.set_paper_meta(repo, nb, sid, is_paper=is_paper)
    with repo._write() as db:
        db.execute("UPDATE source_paper_meta SET notebook_id = ? WHERE source_id = ?", (other, sid))


def test_a_legacy_cross_notebook_not_paper_row_is_ignored(repo, nb, service, model):
    _paper(repo, nb, "s1", meta=None)
    _legacy_meta(repo, nb, "s1", is_paper=False)
    [result] = _get(service, nb, ["s1"], limit=2)
    assert result.status == "generated"          # not "not_paper": the stray row is not its metadata
    assert model.meta_calls == 1


def test_a_legacy_cross_notebook_paper_row_is_ignored(repo, nb, service, model):
    _paper(repo, nb, "s1", meta=None)
    _legacy_meta(repo, nb, "s1", is_paper=True)
    model.is_paper = False
    [result] = _get(service, nb, ["s1"], limit=2)
    assert result.status == "not_paper"          # re-extracted, not trusted from the stray row
    assert (model.meta_calls, model.card_calls) == (1, 0)


def test_the_metadata_variant_pages_the_opening_only(repo, nb, service, model, monkeypatch):
    kit.seed_source(repo, nb, "s1", filler=400)
    ingestion = service._ingestion
    store = repo._runtime.source_store
    monkeypatch.setattr(ingestion, "source_elements", lambda *_: pytest.fail("full element load"))
    # keyset pages only: the OFFSET reader re-counts the source on every page
    monkeypatch.setattr(store, "source_elements_page", lambda *_a, **_k: pytest.fail("counting page read"))
    pages = []
    original = store.source_elements_after

    def counting(*args, **kwargs):
        pages.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "source_elements_after", counting)
    source = store.get_source("s1")
    head_chars = repo.settings.paper_meta_head_chars
    head = ingestion._paper_meta_paged_head(source, head_chars, None)
    assert len(pages) <= 3                         # ~45 chars per element, 32 per page, 4000 chars
    monkeypatch.undo()
    assert head == ingestion._paper_meta_full_head(source, None, head_chars)


@pytest.mark.parametrize("content", ["", "# Gated Widgets\nAbstract: tokens."])
def test_the_paged_head_reads_a_text_file_like_the_original(repo, nb, service, model, tmp_path, content):
    kit.seed_source(repo, nb, "s1")
    ingestion = service._ingestion
    path = tmp_path / "s1.md"
    path.write_text(content, encoding="utf-8")
    source = repo._runtime.source_store.get_source("s1").model_copy(update={"file_path": str(path)})
    head = ingestion._paper_meta_paged_head(source, 4000, None)
    assert head == ingestion._paper_meta_full_head(source, None, 4000) == content
    if not content:   # an empty file is "no text", never a fallback to the elements
        assert ingestion.extract_paper_metadata_cancellable(
            source, cancel_event=None, client=repo._runtime.models.chat("paper_metadata")) == "no_text"


def test_a_non_content_value_error_is_not_cooled_down(repo, nb, service, model, monkeypatch):
    _paper(repo, nb, "s1", meta=None)

    def broken(*_args):
        raise ValueError("bad storage configuration")

    monkeypatch.setattr(service._ingestion.source_files, "read_text_file", broken)
    [result] = _get(service, nb, ["s1"])
    assert (result.status, result.reason) == ("failed", "metadata_error")
    assert _card_row(repo, nb, "s1") is None


def test_the_metadata_variant_reads_and_writes_inside_fanout_slots(repo, nb, service, model, monkeypatch):
    from app.services.retrieval_run import holding_fanout_slot, retrieval_run

    _paper(repo, nb, "s1", meta=None)
    store = repo._runtime.source_store
    seen = {}
    for name in ("get_paper_meta", "source_elements_after", "upsert_paper_meta"):
        original = getattr(store, name)

        def spy(*args, _name=name, _original=original, **kwargs):
            seen.setdefault(_name, holding_fanout_slot())
            return _original(*args, **kwargs)

        monkeypatch.setattr(store, name, spy)
    with retrieval_run(run_kind="test", fanout_limit=2):
        status = service._ingestion.extract_paper_metadata_cancellable(
            store.get_source("s1"), cancel_event=None, client=repo._runtime.models.chat("paper_metadata"),
        )
    assert status == "stored"
    assert seen == {"get_paper_meta": True, "source_elements_after": True, "upsert_paper_meta": True}


def test_get_cards_refuses_to_run_inside_a_fanout_slot(repo, nb, service, model):
    from app.services.retrieval_run import retrieval_fanout_slot, retrieval_run

    _paper(repo, nb, "s1")
    # fanout_limit=2 so that, without the guard, the call completes (and the
    # pytest.raises fails fast) instead of deadlocking on the one slot.
    with retrieval_run(run_kind="test", fanout_limit=2):
        with retrieval_fanout_slot():
            with pytest.raises(RuntimeError, match="retrieval_fanout_slot"):
                _get(service, nb, ["s1"])
        assert _get(service, nb, ["s1"])[0].status == "generated"   # outside the slot it runs


# ----------------------------------------------------------------- concurrency


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.01)


def _block_cards(model):
    entered, release = threading.Event(), threading.Event()

    def slow(prompt):
        entered.set()
        assert release.wait(10)

    model.on_card = slow
    return entered, release


def _waiting_on(service, sid):
    flight = service._flights.get(sid)
    return flight is not None and flight.waiters >= 1


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
    assert service._flights._flights == {}


def test_cancellation_still_emits_events_for_finished_sources(repo, nb, service, model, events):
    _paper(repo, nb, "s1")
    _paper(repo, nb, "s2")
    _get(service, nb, ["s1"])
    events.clear()
    cancel = threading.Event()

    def cancelled(prompt):
        cancel.set()
        raise AskCancelled()

    model.reply = cancelled
    with pytest.raises(AskCancelled):
        _get(service, nb, ["s1", "s2"], cancel_event=cancel)
    assert [(e["source_id"], e["status"]) for e in events] == [("s1", "ready")]


def test_event_elapsed_is_per_source(repo, nb, service, model, events):
    _paper(repo, nb, "s1")
    _paper(repo, nb, "s2")
    _get(service, nb, ["s1"])
    events.clear()
    model.on_card = lambda prompt: time.sleep(0.4)
    _get(service, nb, ["s1", "s2"])
    elapsed = {e["source_id"]: e["elapsed_ms"] for e in events}
    assert elapsed["s2"] >= 400
    assert elapsed["s1"] < 400


def test_concurrent_requests_for_one_source_generate_once(repo, nb, service, model):
    _paper(repo, nb, "s1")
    entered, release = _block_cards(model)
    results: dict[str, object] = {}
    leader = threading.Thread(target=lambda: results.update(a=_get(service, nb, ["s1"])[0]))
    leader.start()
    assert entered.wait(5)
    waiter = threading.Thread(target=lambda: results.update(b=_get(service, nb, ["s1"])[0]))
    waiter.start()
    _wait_until(lambda: _waiting_on(service, "s1"))   # parked on the leader's flight
    time.sleep(0.2)
    assert waiter.is_alive() and "b" not in results
    release.set()
    leader.join(5)
    waiter.join(5)
    assert results["a"].status == "generated"
    assert results["b"].status == "ready"
    assert model.card_calls == 1


def test_a_waiter_still_honours_its_own_cancellation(repo, nb, service, model):
    _paper(repo, nb, "s1")
    entered, release = _block_cards(model)
    leader = threading.Thread(target=lambda: _get(service, nb, ["s1"]))
    leader.start()
    try:
        assert entered.wait(5)
        cancel = threading.Event()
        outcome: dict[str, object] = {}

        def wait_then_record():
            started = time.monotonic()
            try:
                _get(service, nb, ["s1"], cancel_event=cancel)
            except AskCancelled:
                outcome["cancelled_after"] = time.monotonic() - started

        waiter = threading.Thread(target=wait_then_record)
        waiter.start()
        _wait_until(lambda: _waiting_on(service, "s1"))
        cancel.set()
        waiter.join(5)
        assert outcome["cancelled_after"] < 2.0   # well under the leader's 10 s hold
        assert not release.is_set()
    finally:
        release.set()
        leader.join(5)


def test_waiters_do_not_consume_the_generate_limit(repo, nb, service, model):
    _paper(repo, nb, "s1")
    _paper(repo, nb, "s2")
    entered, release = _block_cards(model)
    leader = threading.Thread(target=lambda: _get(service, nb, ["s1"]))
    leader.start()
    assert entered.wait(5)
    results: dict[str, object] = {}
    waiter = threading.Thread(target=lambda: results.update(r=_get(service, nb, ["s1", "s2"], limit=1)))
    waiter.start()
    _wait_until(lambda: model.card_calls == 2)        # s2 admitted although s1 came first
    release.set()
    leader.join(5)
    waiter.join(5)
    assert _statuses(results["r"]) == [("s1", "ready", ""), ("s2", "generated", "")]


def test_a_waiter_adopts_the_leaders_terminal_status(repo, nb, service, model):
    # The leader ends in a terminal state that leaves NO row (an auth failure), so
    # re-reading the cache could not tell the waiter anything: only adopting the
    # leader's own result gives failed/model_error with a single model call.
    _paper(repo, nb, "s1")
    entered, release = threading.Event(), threading.Event()

    def denied(prompt):
        entered.set()
        assert release.wait(10)
        raise kit.StatusError(401)

    model.on_card = denied
    results: dict[str, object] = {}
    leader = threading.Thread(target=lambda: results.update(a=_get(service, nb, ["s1"])[0]))
    leader.start()
    assert entered.wait(5)
    waiter = threading.Thread(target=lambda: results.update(b=_get(service, nb, ["s1"])[0]))
    waiter.start()
    _wait_until(lambda: _waiting_on(service, "s1"))
    release.set()
    leader.join(5)
    waiter.join(5)
    assert (results["a"].status, results["a"].reason) == ("failed", "model_error:provider_auth")
    assert (results["b"].status, results["b"].reason) == ("failed", "model_error:provider_auth")
    assert model.card_calls == 1
    # Only the leader's reservation is charged: the waiter adopted the outcome,
    # never its ``calls`` (it would spend the waiter's run budget a second time).
    assert results["a"].calls == 1
    assert results["b"].calls == 0
    assert _card_row(repo, nb, "s1") is None


def test_a_waiter_adopts_the_leaders_not_paper(repo, nb, service, model):
    _paper(repo, nb, "s1", meta=None)
    model.is_paper = False
    entered, release = threading.Event(), threading.Event()
    model.on_meta = lambda prompt: (entered.set(), release.wait(10))
    results: dict[str, object] = {}
    leader = threading.Thread(target=lambda: results.update(a=_get(service, nb, ["s1"])[0]))
    leader.start()
    assert entered.wait(5)
    waiter = threading.Thread(target=lambda: results.update(b=_get(service, nb, ["s1"])[0]))
    waiter.start()
    _wait_until(lambda: _waiting_on(service, "s1"))
    release.set()
    leader.join(5)
    waiter.join(5)
    assert results["a"].status == results["b"].status == "not_paper"
    assert model.meta_calls == 1
    assert results["a"].calls >= 1 and results["b"].calls == 0


def test_a_round_that_fails_to_start_releases_its_flights(repo, nb, service, model, monkeypatch):
    _paper(repo, nb, "s1")

    def broken(_workload):
        raise RuntimeError("registry reload in progress")

    monkeypatch.setattr(service._models, "parallelism", broken)
    with pytest.raises(RuntimeError, match="registry reload"):
        _get(service, nb, ["s1"])
    assert service._flights.get("s1") is None
    monkeypatch.undo()
    results: dict[str, object] = {}
    later = threading.Thread(target=lambda: results.update(r=_get(service, nb, ["s1"])[0]))
    later.start()
    later.join(5)
    assert not later.is_alive(), "a later request hung on a leaked flight"
    assert results["r"].status == "generated"


def test_waiting_for_another_request_is_bounded(repo, nb, service, model, monkeypatch):
    _paper(repo, nb, "s1")
    entered, release = _block_cards(model)
    leader = threading.Thread(target=lambda: _get(service, nb, ["s1"]))
    leader.start()
    try:
        assert entered.wait(5)
        monkeypatch.setattr(service, "_wait_bound_seconds", lambda: 0.3)
        started = time.monotonic()
        [result] = _get(service, nb, ["s1"])
        assert (result.status, result.reason) == ("deferred", "in_flight")
        assert time.monotonic() - started < 2.0
    finally:
        release.set()
        leader.join(5)


def test_a_waiter_generates_itself_when_the_leader_was_cancelled(repo, nb, service, model):
    _paper(repo, nb, "s1")
    entered, release = threading.Event(), threading.Event()
    leader_cancel = threading.Event()
    calls = []

    def first_call_cancelled(prompt):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(10)
            leader_cancel.set()
            raise AskCancelled()

    model.on_card = first_call_cancelled
    leader = threading.Thread(target=lambda: pytest.raises(AskCancelled, _get, service, nb, ["s1"],
                                                           cancel_event=leader_cancel))
    leader.start()
    assert entered.wait(5)
    results: dict[str, object] = {}
    waiter = threading.Thread(target=lambda: results.update(b=_get(service, nb, ["s1"])[0]))
    waiter.start()
    _wait_until(lambda: _waiting_on(service, "s1"))
    release.set()
    leader.join(5)
    waiter.join(5)
    assert results["b"].status == "generated"
    assert model.card_calls == 2


def test_a_waiter_without_budget_stays_deferred_in_flight(repo, nb, service, model):
    _paper(repo, nb, "s1")
    entered, release = threading.Event(), threading.Event()
    leader_cancel = threading.Event()

    def cancelled(prompt):
        entered.set()
        assert release.wait(10)
        leader_cancel.set()
        raise AskCancelled()

    model.on_card = cancelled
    leader = threading.Thread(target=lambda: pytest.raises(AskCancelled, _get, service, nb, ["s1"],
                                                           cancel_event=leader_cancel))
    leader.start()
    assert entered.wait(5)
    results: dict[str, object] = {}
    waiter = threading.Thread(target=lambda: results.update(b=_get(service, nb, ["s1"], limit=0)[0]))
    waiter.start()
    _wait_until(lambda: _waiting_on(service, "s1"))
    release.set()
    leader.join(5)
    waiter.join(5)
    assert (results["b"].status, results["b"].reason) == ("deferred", "generate_limit")
    assert model.card_calls == 1


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
