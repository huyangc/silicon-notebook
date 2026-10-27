"""KG lexical budgets are optional ANN supplements, never lexical-only gates."""
from __future__ import annotations

import contextvars
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from types import MethodType, SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.services.retrieval_candidates import _KG_TYPES, CandidateRetrievalService
from app.services.retrieval_run import retrieval_run


class _Log:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def _service(*, capable=True, exhausted=False, budget=5.0, failing=False):
    calls = []
    partial = [{"object_id": "lexical-id", "name": "private-name", "score": 0.7}]

    def fts_search(_db, notebook_id, query, k, **options):
        calls.append((notebook_id, options))
        if failing:
            raise RuntimeError("private-error-text")
        if options.get("time_budget_seconds"):
            options["routing_stats"].update({
                "budget_exhausted": int(exhausted),
                "term_count": 2,
            })
        return list(partial)

    service = SimpleNamespace(
        settings=Settings(
            _env_file=None, postgres_kg_lexical_timeout_seconds=budget,
            kg_canonical_fold_enabled=False, retrieval_rrf_enabled=False,
        ),
        knowledge=SimpleNamespace(
            lexical_budget_capable=capable, fts_search=fts_search,
            source_index_backfilled=lambda *_: True,
        ),
        event_log=_Log(),
    )
    service._lexical_object_hits = MethodType(
        CandidateRetrievalService._lexical_object_hits, service,
    )
    return service, calls, partial


def _hits(service, notebook_id="nb-a", **options):
    return service._lexical_object_hits(
        object(), notebook_id, "private-query", 8,
        site=options.pop("site", "kg_ann_fts"), corpus_langs=None, **options,
    )


def test_partial_lexical_hits_survive_timeout_and_open_only_that_run_library():
    service, calls, partial = _service(exhausted=True)
    stats = {}
    with retrieval_run(run_kind="ask_reasoning") as run:
        assert _hits(service, supplemental=True, routing_stats=stats) == partial
        assert _hits(service, supplemental=True) == []
        assert len(calls) == 1
        assert calls[0][1]["time_budget_seconds"] == 5.0
        assert stats["budget_exhausted"] == 1
        assert run.kg_lexical_timeouts == 1
        assert run.kg_lexical_circuit_skips == 1
        # Another library is independent; the chunk circuit remains usable.
        assert _hits(service, "nb-b", supplemental=True) == partial
        assert run.chunk_fts_permitted("nb-a") is True
        assert run.chunk_fts_timeouts == 0
        assert run.event()["kg_lexical_timeouts"] == 2
    with retrieval_run(run_kind="ask_reasoning"):
        assert _hits(service, supplemental=True) == partial

    events = service.event_log.events
    assert [e["status"] for e in events] == [
        "timeout", "skipped_circuit_open", "timeout", "timeout",
    ]
    assert events[0]["hits"] == 1
    assert events[1]["latency_ms"] == 0
    assert all(e["stage"] == "kg_lexical" for e in events)
    assert "private-query" not in json.dumps(events)
    assert "private-name" not in json.dumps(events)
    assert "lexical-id" not in json.dumps(events)


@pytest.mark.parametrize("options", [
    {},
    {"supplemental": True, "site": "kg_source_scoped_fts"},
    {"supplemental": True, "site": "kg_large_fallback_fts"},
    {"supplemental": True, "site": "relation_endpoint_fts"},
    {"supplemental": True, "allowed_source_ids": ["source-a"]},
])
def test_lexical_only_paths_bypass_an_open_supplement_circuit(options):
    service, calls, partial = _service(exhausted=True)
    with retrieval_run(run_kind="ask_reasoning") as run:
        run.note_kg_lexical_timeout("nb-a")
        assert _hits(service, **options) == partial
        assert run.kg_lexical_circuit_skips == 0
    assert "time_budget_seconds" not in calls[0][1]
    assert service.event_log.events == []


@pytest.mark.parametrize(("capable", "budget"), [(False, 5.0), (True, 0.0)])
def test_incapable_adapter_and_zero_rollback_keep_historical_call(capable, budget):
    service, calls, partial = _service(capable=capable, budget=budget)
    with retrieval_run(run_kind="ask_reasoning") as run:
        run.note_kg_lexical_timeout("nb-a")
        assert _hits(service, supplemental=True) == partial
        assert run.kg_lexical_circuit_skips == 0
    assert "time_budget_seconds" not in calls[0][1]
    assert service.event_log.events == []


def test_success_and_failure_status_and_chunk_circuit_are_independent():
    service, calls, partial = _service()
    with retrieval_run(run_kind="ask_reasoning") as run:
        run.note_chunk_fts_timeout("nb-a")
        assert _hits(service, supplemental=True) == partial
        assert run.kg_lexical_timeouts == 0
    assert service.event_log.events[0]["status"] == "ok"
    assert len(calls) == 1

    failed, calls, _ = _service(failing=True)
    assert _hits(failed, supplemental=True) == []
    assert failed.event_log.events[-1]["status"] == "failed_open"
    assert "private-error-text" not in json.dumps(failed.event_log.events)


def test_already_running_supplements_settle_after_a_sibling_opens_the_circuit():
    service, _, partial = _service()
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    lock = threading.Lock()
    next_slot = 0

    def fts_search(_db, _notebook_id, _query, k, **options):
        nonlocal next_slot
        with lock:
            slot = next_slot
            next_slot += 1
        entered[slot].set()
        assert release[slot].wait(timeout=2)
        options["routing_stats"]["budget_exhausted"] = 1
        return list(partial)

    service.knowledge.fts_search = fts_search
    with retrieval_run(run_kind="ask_reasoning") as run:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                contextvars.copy_context().run, _hits, service, supplemental=True,
            )
            assert entered[0].wait(timeout=2)
            second = pool.submit(
                contextvars.copy_context().run, _hits, service, supplemental=True,
            )
            try:
                assert entered[1].wait(timeout=2)
                release[0].set()
                assert first.result(timeout=2) == partial
                assert _hits(service, supplemental=True) == []
                release[1].set()
                assert second.result(timeout=2) == partial
            finally:
                for event in release:
                    event.set()
        assert run.kg_lexical_timeouts == 1
        assert run.kg_lexical_circuit_skips == 1
    assert next_slot == 2


@pytest.mark.parametrize(("ann", "source_ids", "has_index", "embed"), [
    ({"ann-id": 0.9}, None, True, [1.0]),
    ({}, None, True, [1.0]),
    ({"ann-id": 0.9}, ["source-a"], True, [1.0]),
    ({"ann-id": 0.9}, None, False, [1.0]),
    ({"ann-id": 0.9}, None, True, None),
])
@pytest.mark.parametrize("scored_types", [
    _KG_TYPES, ("claim",), ("formula",), ("claim", "formula"),
])
def test_scored_retrieval_opts_in_only_after_nonempty_ann(
    ann, source_ids, has_index, embed, scored_types,
):
    service, calls, _ = _service(exhausted=True)
    hydrated = []

    def knowledge_objects(_db, _notebook, _kind, *, id_filter):
        hydrated.append(id_filter)
        return []

    service._scored_type_scope = lambda *_: (list(scored_types), ())
    service._embed_query = lambda *_: embed
    service._scale_index = lambda *a, **k: (
        SimpleNamespace(ann_labels=["ann-id"]) if has_index else None
    )
    service.maybe_auto_index = lambda *_: None
    service._kg_object_candidates = lambda *a, **k: dict(ann)
    service._lexical_corpus_langs = lambda *a, **k: None
    service._lexical_knn_allowed = lambda *a, **k: False
    service._connect = lambda: nullcontext(object())
    service._knowledge_objects = knowledge_objects
    service._keyword_token_sets = lambda *a, **k: {}
    service._in_batches = lambda values: []
    service.notebook_copy_stats = lambda *_: {"copyable": False}
    narrowed_types = not set(_KG_TYPES).issubset(scored_types)
    with retrieval_run(run_kind="ask_reasoning") as run:
        if narrowed_types:
            run.note_kg_lexical_timeout("nb-a")
        assert CandidateRetrievalService._retrieve_scored(
            service, "nb-a", "question", types=scored_types,
            allowed_source_ids=source_ids,
        ) == []
        qualified = bool(
            ann and source_ids is None and has_index and embed and not narrowed_types
        )
        assert ("time_budget_seconds" in calls[0][1]) is qualified
        assert run.kg_lexical_timeouts == int(qualified) + int(narrowed_types)
        assert run.kg_lexical_circuit_skips == 0
    assert "lexical-id" in hydrated[0]
    if qualified:
        assert "ann-id" in hydrated[0]


def test_kg_budget_default_environment_and_low_pool_timeout(monkeypatch):
    monkeypatch.delenv("POSTGRES_KG_LEXICAL_TIMEOUT_SECONDS", raising=False)
    assert Settings(_env_file=None).postgres_kg_lexical_timeout_seconds == 5.0
    monkeypatch.setenv("POSTGRES_KG_LEXICAL_TIMEOUT_SECONDS", "0")
    assert Settings(_env_file=None).postgres_kg_lexical_timeout_seconds == 0.0
    monkeypatch.setenv("POSTGRES_KG_LEXICAL_TIMEOUT_SECONDS", "0.75")
    assert Settings(_env_file=None).postgres_kg_lexical_timeout_seconds == 0.75
    # Runtime caps the private budget, rather than breaking low-timeout test
    # deployments or requiring another startup setting just to retain 2s.
    monkeypatch.delenv("POSTGRES_KG_LEXICAL_TIMEOUT_SECONDS")
    assert Settings(
        _env_file=None, postgres_statement_timeout_seconds=2,
        postgres_chunk_fts_timeout_seconds=1,
    ).postgres_kg_lexical_timeout_seconds == 5.0


@pytest.mark.parametrize("value", [-1, 30.1, float("inf"), float("nan")])
def test_kg_budget_rejects_invalid_limits(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, postgres_kg_lexical_timeout_seconds=value)
