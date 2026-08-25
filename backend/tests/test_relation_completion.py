from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.domain.indexing_pipeline import IndexingPipelineUnavailableError
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository

from app.services.kg.relation_completion import (
    candidate_batches,
    completion_mode_for_notebook,
    generate_candidates,
    propose_batch,
    stable_relation_id,
    verify_batch,
)
from app.services.kg.edge_schema import EDGE_SPECS


def _completion_contract_args():
    return (
        tuple(
            (edge_type, source_type, target_type)
            for edge_type, spec in EDGE_SPECS.items()
            for source_type, target_type in sorted(spec.allowed_pairs)
        ),
        tuple(EDGE_SPECS),
        ("concept", "claim", "formula", "procedure"),
    )


def _object(local_id, oid, name, element_id, section, object_type="claim"):
    return {
        "local_id": local_id,
        "_oid": oid,
        "object_type": object_type,
        "payload": {"name": name, "section_path": section},
        "evidence": [{
            "element_id": element_id,
            "location_label": section,
            "quoted_span": name,
        }],
    }


def _elements():
    return [
        SimpleNamespace(
            id="e1", element_type="paragraph", location_label="Chapter 1",
            text="Low supply operation requires a stable bias reference.",
        ),
        SimpleNamespace(
            id="e2", element_type="paragraph", location_label="Chapter 8",
            text="A stable bias reference therefore supports low supply operation.",
        ),
    ]


def _candidates():
    return generate_candidates(
        [
            _object("a", "ko-a", "low supply operation", "e1", "Chapter 1"),
            _object("b", "ko-b", "stable bias operation", "e2", "Chapter 8"),
        ],
        [],
        _elements(),
        run_id="run-1",
        max_objects=10,
        max_pairs=10,
        excerpt_chars=200,
    )


def test_candidate_generation_is_bounded_cross_window_and_deterministic():
    first = _candidates()
    second = _candidates()
    assert first == second
    assert 0 < len(first) <= 10
    assert all(candidate.allowed_element_ids == ("e1", "e2") for candidate in first)
    assert {candidate.source_object_id for candidate in first} == {"ko-a", "ko-b"}


def test_candidate_generation_enforces_per_source_section_quota():
    objects = [
        _object(
            f"a{index}", f"ko-a{index}", f"shared topic {index}",
            "e1" if index % 2 == 0 else "e2", "Chapter 1",
        )
        for index in range(6)
    ]
    candidates = generate_candidates(
        objects, [], _elements(), run_id="quota", max_objects=20,
        max_pairs=20, excerpt_chars=200, section_quota=3,
    )
    assert len(candidates) <= 3


def test_existing_edge_only_excludes_the_equivalent_type_not_the_whole_pair():
    candidates = generate_candidates(
        [
            _object("a", "ko-a", "low supply operation", "e1", "Chapter 1"),
            _object("b", "ko-b", "stable bias operation", "e2", "Chapter 8"),
        ],
        [{"source_local_id": "a", "target_local_id": "b", "edge_type": "supports"}],
        _elements(), run_id="run-1", max_objects=10, max_pairs=10,
        excerpt_chars=200,
    )
    assert candidates
    assert all(not (
        item.source_object_id == "ko-a" and item.target_object_id == "ko-b"
        and "supports" in item.allowed_edge_types
    ) for item in candidates)


class _Proposer:
    configured = True

    def chat_json(self, messages, _schema):
        candidate = json.loads(messages[1]["content"])[0]
        return json.dumps({"relations": [{
            "candidate_id": candidate["candidate_id"],
            "source_object_id": candidate["source_object_id"],
            "target_object_id": candidate["target_object_id"],
            "edge_type": candidate["allowed_edge_types"][0],
            "evidence_element_ids": [candidate["allowed_evidence"][0]["element_id"]],
            "confidence": 0.8,
        }]})


class _Verifier:
    configured = True

    def chat_json(self, messages, _schema):
        candidate = json.loads(messages[1]["content"])[0]
        return json.dumps({"verdicts": [{
            "candidate_id": candidate["candidate_id"], "valid": True, "reason": "explicit",
        }]})


class _Clients:
    def chat(self, workload):
        return _Verifier() if workload == "kg_refine" else _Proposer()


def test_models_can_only_select_server_issued_candidate_and_evidence_ids():
    candidates = _candidates()
    proposed = propose_batch(_Proposer(), candidates)
    assert len(proposed) == 1
    assert verify_batch(_Verifier(), proposed) == proposed


def test_malformed_model_envelopes_fail_the_page_for_retry():
    class _Malformed:
        configured = True

        def chat_json(self, _messages, _schema):
            return "{}"

    with pytest.raises(ValueError, match="proposal envelope"):
        propose_batch(_Malformed(), _candidates())
    proposal = propose_batch(_Proposer(), _candidates())[0]
    with pytest.raises(ValueError, match="verifier envelope"):
        verify_batch(_Malformed(), [proposal])

    candidates = _candidates()

    class _Inventing(_Proposer):
        def chat_json(self, messages, schema):
            payload = json.loads(super().chat_json(messages, schema))
            payload["relations"][0]["candidate_id"] = "invented"
            return json.dumps(payload)

    assert propose_batch(_Inventing(), candidates) == []

    class _ExtraField(_Proposer):
        def chat_json(self, messages, schema):
            payload = json.loads(super().chat_json(messages, schema))
            payload["relations"][0]["quote"] = "model supplied text"
            return json.dumps(payload)

    assert propose_batch(_ExtraField(), candidates) == []


def test_verifier_only_receives_proposer_selected_server_excerpt():
    proposal = propose_batch(_Proposer(), _candidates())[0]

    class _InspectingVerifier(_Verifier):
        def chat_json(self, messages, schema):
            payload = json.loads(messages[1]["content"])[0]
            assert [row["element_id"] for row in payload["allowed_evidence"]] == list(
                proposal.evidence_element_ids
            )
            return super().chat_json(messages, schema)

    assert verify_batch(_InspectingVerifier(), [proposal]) == [proposal]


def test_candidate_batch_rails_count_and_complete_serialized_payload_chars():
    candidates = _candidates() * 4
    batches = candidate_batches(
        candidates, pair_limit=2, char_limit=2_000, max_batches=2
    )
    assert len(batches) <= 2
    assert all(len(batch) <= 2 for batch in batches)


def test_completion_relation_id_is_retry_stable_and_generation_specific():
    proposal = propose_batch(_Proposer(), _candidates())[0]
    assert stable_relation_id("nb", "run-1", proposal) == stable_relation_id(
        "nb", "run-1", proposal
    )
    assert stable_relation_id("nb", "run-1", proposal) != stable_relation_id(
        "nb", "run-2", proposal
    )


def test_rollout_is_off_by_default_and_allowlist_is_explicit():
    settings = SimpleNamespace(
        kg_relation_completion_mode="write",
        kg_relation_completion_notebook_allowlist="nb-enabled",
        kg_relation_completion_rollout_percent=0,
    )
    assert completion_mode_for_notebook(settings, "nb-disabled") == "off"
    assert completion_mode_for_notebook(settings, "nb-enabled") == "write"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'completion.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings(_env_file=None))


def test_generation_cas_and_completion_idempotence(repo):
    notebook = repo.create_notebook(NotebookCreate(name="completion"))
    now = "2026-07-28T00:00:00+00:00"
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("source", notebook.id, "Book", "md", "ready", now, now),
        )
        db.execute(
            "INSERT INTO source_elements "
            "(id,source_id,element_type,location_label,text,metadata,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("element", "source", "paragraph", "Chapter 1", "evidence", "{}", now),
        )
        for object_id in ("a", "b"):
            db.execute(
                "INSERT INTO knowledge_objects "
                "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (object_id, notebook.id, "claim", "approved", "", "{}", "[]", "source", now, now),
            )
        db.execute(
            "INSERT INTO extraction_runs "
            "(id,notebook_id,source_id,run_type,status,error_message,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("run-1", notebook.id, "source", "kg", "running", "", now, now),
        )
        assert repo._runtime.knowledge.completion_validate_scope(
            db, notebook.id, "source", "run-1", ["a", "b"], ["element"]
        )
        row = (
            "relc-stable", notebook.id, "source", "a", "b", "supports",
            "[]", now,
        )
        assert repo._runtime.knowledge.insert_completion_relations(db, [row]) == 1
        assert repo._runtime.knowledge.insert_completion_relations(db, [row]) == 0
        db.execute(
            "INSERT INTO extraction_runs "
            "(id,notebook_id,source_id,run_type,status,error_message,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                "run-2", notebook.id, "source", "kg", "running", "",
                "2026-07-28T00:00:01+00:00", "2026-07-28T00:00:01+00:00",
            ),
        )
        assert not repo._runtime.knowledge.completion_validate_scope(
            db, notebook.id, "source", "run-1", ["a", "b"], ["element"]
        )


def test_completion_keyset_state_advances_without_skipping_or_reusing_generation(repo):
    notebook = repo.create_notebook(NotebookCreate(name="pages"))
    now = "2026-07-28T00:00:00+00:00"
    store = repo._runtime.knowledge
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)", ("source-page", notebook.id, "Book", "md", "ready", now, now),
        )
        for object_id in ("a", "b", "c"):
            db.execute(
                "INSERT INTO knowledge_objects "
                "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (object_id, notebook.id, "claim", "approved", "", "{}", "[]", "source-page", now, now),
            )
        db.execute(
            "INSERT INTO extraction_runs "
            "(id,notebook_id,source_id,run_type,status,error_message,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("run-page", notebook.id, "source-page", "kg", "running", "", now, now),
        )
        first = store.completion_page(
            db, notebook.id, "source-page", "run-page", "shadow", 1,
            ("supports", "depends_on"), *_completion_contract_args(), 2, now
        )
        assert [row["id"] for row in first["rows"]] == ["a", "b"]
        assert first["cursor"] == "" and first["next_cursor"] == "b"
        assert not first["exhausted"]
        assert store.completion_advance_state(
            db, notebook.id, "source-page", "run-page", "shadow", 1,
            "", "b", "pending", now,
        )
        second = store.completion_page(
            db, notebook.id, "source-page", "run-page", "shadow", 1,
            ("supports", "depends_on"), *_completion_contract_args(), 2, now
        )
        assert [row["id"] for row in second["rows"]] == ["c"]
        assert second["cursor"] == "b" and second["exhausted"]
        assert not store.completion_advance_state(
            db, notebook.id, "source-page", "run-page", "shadow", 1,
            "", "c", "completed", now,
        )
        assert store.completion_advance_state(
            db, notebook.id, "source-page", "run-page", "shadow", 1,
            "b", "c", "completed", now,
        )
        counts = store.delete_notebook_graph_rows(db, notebook.id)
        assert counts["kg_relation_completion_state"] == 1


def test_book_scale_keyset_pages_are_bounded_and_exhaust_exactly_once(repo):
    notebook = repo.create_notebook(NotebookCreate(name="large pages"))
    now = "2026-07-28T00:00:00+00:00"
    store = repo._runtime.knowledge
    object_ids = [f"ko-{index:04d}" for index in range(401)]
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("source-large", notebook.id, "Book", "md", "ready", now, now),
        )
        db.executemany(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    object_id, notebook.id, "claim", "approved", "",
                    json.dumps({"name": object_id}), "[]", "source-large", now, now,
                )
                for object_id in object_ids
            ],
        )
        db.execute(
            "INSERT INTO extraction_runs "
            "(id,notebook_id,source_id,run_type,status,error_message,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("run-large", notebook.id, "source-large", "kg", "running", "", now, now),
        )
        seen = []
        for _ in range(3):
            page = store.completion_page(
                db, notebook.id, "source-large", "run-large", "shadow", 1,
                ("supports", "depends_on"), *_completion_contract_args(), 160, now,
            )
            assert len(page["rows"]) <= 160
            seen.extend(row["id"] for row in page["rows"])
            assert store.completion_advance_state(
                db, notebook.id, "source-large", "run-large", "shadow", 1,
                page["cursor"], page["next_cursor"],
                "completed" if page["exhausted"] else "pending", now,
            )
        assert seen == object_ids
        completed = store.completion_page(
            db, notebook.id, "source-large", "run-large", "shadow", 1,
            ("supports", "depends_on"), *_completion_contract_args(), 160, now,
        )
        assert completed["already_completed"] and completed["rows"] == []


def test_completion_anchor_flags_ignore_historical_illegal_edges(repo):
    notebook = repo.create_notebook(NotebookCreate(name="edge anchors"))
    now = "2026-07-28T00:00:00+00:00"
    store = repo._runtime.knowledge
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("source-edge", notebook.id, "Book", "md", "ready", now, now),
        )
        for object_id, object_type in (
            ("claim-valid", "claim"),
            ("concept-valid", "concept"),
            ("claim-illegal", "claim"),
            ("procedure-illegal", "procedure"),
        ):
            db.execute(
                "INSERT INTO knowledge_objects "
                "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (object_id, notebook.id, object_type, "approved", "", "{}", "[]",
                 "source-edge", now, now),
            )
        db.execute(
            "INSERT INTO knowledge_relations "
            "(id,notebook_id,source_id,source_object_id,target_object_id,edge_type,evidence,created_at,review_status) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("valid-edge", notebook.id, "source-edge", "concept-valid", "claim-valid",
             "supports", "[]", now, "pending"),
        )
        db.execute(
            "INSERT INTO knowledge_relations "
            "(id,notebook_id,source_id,source_object_id,target_object_id,edge_type,evidence,created_at,review_status) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("illegal-edge", notebook.id, "source-edge", "procedure-illegal",
             "claim-illegal", "supports", "[]", now, "verified"),
        )
        db.execute(
            "INSERT INTO extraction_runs "
            "(id,notebook_id,source_id,run_type,status,error_message,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("run-edge", notebook.id, "source-edge", "kg", "completed", "", now, now),
        )
        page = store.completion_page(
            db, notebook.id, "source-edge", "run-edge", "shadow", 1,
            ("supports", "depends_on"), *_completion_contract_args(), 10, now,
        )
    flags = {
        row["id"]: (bool(row["has_relation"]), bool(row["has_reasoning_relation"]))
        for row in page["rows"]
    }
    assert flags["claim-valid"] == (True, True)
    assert flags["concept-valid"] == (True, True)
    assert flags["claim-illegal"] == (False, False)
    assert flags["procedure-illegal"] == (False, False)


def test_sqlite_completion_element_hydration_chunks_more_than_999_ids(repo):
    notebook = repo.create_notebook(NotebookCreate(name="large evidence hydration"))
    now = "2026-07-28T00:00:00+00:00"
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("source-elements", notebook.id, "Book", "md", "ready", now, now),
        )
        ids = [f"element-large-{index:04d}" for index in range(1001)]
        db.executemany(
            "INSERT INTO source_elements "
            "(id,source_id,element_type,location_label,text,metadata,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [(element_id, "source-elements", "paragraph", "", element_id, "{}", now)
             for element_id in ids],
        )
        rows = repo._runtime.knowledge.completion_element_rows(
            db, "source-elements", ids
        )
    assert {row["id"] for row in rows} == set(ids)


def test_unavailable_ann_and_fts_fail_closed_without_advancing_cursor(
    repo, monkeypatch
):
    notebook = repo.create_notebook(NotebookCreate(name="no indexes"))
    now = "2026-07-28T00:00:00+00:00"
    lifecycle = repo._runtime.knowledge_lifecycle
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("source-no-index", notebook.id, "Book", "md", "ready", now, now),
        )
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "ko-no-index", notebook.id, "claim", "approved", "",
                json.dumps({"name": "indexed anchor"}), "[]", "source-no-index", now, now,
            ),
        )
        db.execute(
            "INSERT INTO extraction_runs "
            "(id,notebook_id,source_id,run_type,status,error_message,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("run-no-index", notebook.id, "source-no-index", "kg", "running", "", now, now),
        )
    monkeypatch.setattr(lifecycle.settings, "kg_relation_completion_mode", "shadow")
    monkeypatch.setattr(
        lifecycle.settings, "kg_relation_completion_notebook_allowlist", notebook.id
    )
    monkeypatch.setattr(lifecycle.scale_artifacts, "load", lambda *args, **kwargs: None)
    monkeypatch.setattr(lifecycle, "model_clients", _Clients())

    def unavailable(*args, **kwargs):
        raise RuntimeError("fts unavailable")

    monkeypatch.setattr(lifecycle.knowledge, "fts_search", unavailable)
    stats = lifecycle.complete_relations_for_source(
        notebook.id, "source-no-index", "Book", "run-no-index", [], [], []
    )
    assert stats["skip_reason"] == "ann_fts_unavailable"
    with repo._connect() as db:
        state = db.execute(
            "SELECT next_object_id,status FROM kg_relation_completion_state "
            "WHERE source_id=? AND source_generation=? AND mode='shadow'",
            ("source-no-index", "run-no-index"),
        ).fetchone()
    assert dict(state) == {"next_object_id": "", "status": "pending"}


def _seed_resumable_source(repo, notebook_id: str, *, count: int) -> tuple[str, str]:
    source_id = "source-resume"
    run_id = "run-resume"
    now = "2026-07-28T00:00:00+00:00"
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (source_id, notebook_id, "Large Book", "md", "ready", now, now),
        )
        for index in range(count):
            element_id = f"element-{index:04d}"
            marker = "x" * 450 + f" relation-marker-{index}"
            db.execute(
                "INSERT INTO source_elements "
                "(id,source_id,element_type,location_label,text,metadata,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (element_id, source_id, "paragraph", f"Chapter {index}", marker, "{}", now),
            )
            db.execute(
                "INSERT INTO knowledge_objects "
                "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    f"ko-{index:04d}", notebook_id, "claim", "approved", "",
                    json.dumps({"name": f"shared topic {index}", "section_path": f"Chapter {index}"}),
                    json.dumps([{
                        "element_id": element_id,
                        "location_label": f"Chapter {index}",
                        "quoted_span": f"shared topic {index}",
                    }]),
                    source_id, now, now,
                ),
            )
        db.execute(
            "INSERT INTO extraction_runs "
            "(id,notebook_id,source_id,run_type,status,error_message,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (run_id, notebook_id, source_id, "kg", "completed", "", now, now),
        )
    return source_id, run_id


def _configure_completion(monkeypatch, lifecycle, notebook_id: str, *, mode: str) -> None:
    monkeypatch.setattr(lifecycle.settings, "kg_relation_completion_mode", mode)
    monkeypatch.setattr(
        lifecycle.settings, "kg_relation_completion_notebook_allowlist", notebook_id
    )
    monkeypatch.setattr(lifecycle.settings, "kg_relation_completion_max_objects", 2)
    monkeypatch.setattr(lifecycle.settings, "kg_relation_completion_max_pages_per_run", 1)
    monkeypatch.setattr(lifecycle.settings, "kg_relation_completion_max_batches", 1)
    monkeypatch.setattr(lifecycle, "model_clients", _Clients())


def test_pending_completion_drains_across_bounded_jobs_and_restart_entrypoint(
    repo, monkeypatch
):
    notebook = repo.create_notebook(NotebookCreate(name="resume book"))
    source_id, run_id = _seed_resumable_source(repo, notebook.id, count=5)
    lifecycle = repo._runtime.knowledge_lifecycle
    _configure_completion(monkeypatch, lifecycle, notebook.id, mode="shadow")

    hydrated_counts = []
    original_elements = lifecycle.knowledge.completion_element_rows

    def tracked_elements(db, bounded_source_id, element_ids):
        hydrated_counts.append(len(element_ids))
        return original_elements(db, bounded_source_id, element_ids)

    monkeypatch.setattr(
        lifecycle.knowledge, "completion_element_rows", tracked_elements
    )
    first = lifecycle.complete_relations_for_source(
        notebook.id, source_id, "Large Book", run_id, [], [], []
    )
    assert not first["exhausted"] and first["pages"] == 1

    queued = []
    from app.services.kg import scheduler as kg_scheduler

    def submit_job(fn, *args, **kwargs):
        queued.append(lambda: fn(*args, **kwargs))
        return SimpleNamespace()

    monkeypatch.setattr(kg_scheduler, "submit_job", submit_job)
    ingestion = repo._runtime.source_ingestion
    assert ingestion.resume_pending_relation_completions(page_size=1) == 1
    runs = 0
    while queued:
        queued.pop(0)()
        runs += 1
        assert runs < 10

    with repo._connect() as db:
        state = db.execute(
            "SELECT status,next_object_id FROM kg_relation_completion_state "
            "WHERE source_id=? AND source_generation=? AND mode='shadow'",
            (source_id, run_id),
        ).fetchone()
    assert dict(state) == {"status": "completed", "next_object_id": "ko-0004"}
    assert hydrated_counts and max(hydrated_counts) <= 4


def test_pending_completion_recovery_admits_before_submit_and_worker(
    repo, monkeypatch
):
    notebook = repo.create_notebook(NotebookCreate(name="completion admission"))
    source_id, run_id = _seed_resumable_source(repo, notebook.id, count=5)
    lifecycle = repo._runtime.knowledge_lifecycle
    _configure_completion(monkeypatch, lifecycle, notebook.id, mode="shadow")
    first = lifecycle.complete_relations_for_source(
        notebook.id, source_id, "Large Book", run_id, [], [], []
    )
    assert not first["exhausted"]

    queued = []
    from app.services.kg import scheduler as kg_scheduler

    monkeypatch.setattr(
        kg_scheduler,
        "submit_job",
        lambda fn, *args, **kwargs: queued.append(lambda: fn(*args, **kwargs)),
    )
    ingestion = repo._runtime.source_ingestion
    monkeypatch.setattr(
        ingestion,
        "require_indexing_write",
        lambda _notebook_id: (_ for _ in ()).throw(
            IndexingPipelineUnavailableError("pending.pipeline")
        ),
    )
    assert ingestion.resume_pending_relation_completions(page_size=1) == 0
    assert queued == []

    monkeypatch.setattr(ingestion, "require_indexing_write", lambda _notebook_id: None)
    assert ingestion.resume_pending_relation_completions(page_size=1) == 1
    assert len(queued) == 1
    completed = []
    monkeypatch.setattr(
        lifecycle,
        "complete_relations_for_source",
        lambda *_args, **_kwargs: completed.append(True),
    )
    monkeypatch.setattr(
        ingestion,
        "require_indexing_write",
        lambda _notebook_id: (_ for _ in ()).throw(
            IndexingPipelineUnavailableError("pending.pipeline")
        ),
    )
    queued.pop()()
    assert completed == []


def test_completion_ignores_transient_full_source_objects(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="durable authority"))
    source_id, run_id = _seed_resumable_source(repo, notebook.id, count=2)
    lifecycle = repo._runtime.knowledge_lifecycle
    _configure_completion(monkeypatch, lifecycle, notebook.id, mode="shadow")
    forged = [
        {
            "_oid": f"ko-{index:04d}",
            "object_type": "claim",
            "payload": {"name": f"unrelated-{index}"},
            "evidence": [],
        }
        for index in range(2)
    ]
    stats = lifecycle.complete_relations_for_source(
        notebook.id, source_id, "Large Book", run_id, forged, [], []
    )
    assert stats["candidates"] > 0


def test_startup_resume_retires_shadow_cursor_and_starts_write(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="mode transition"))
    source_id, run_id = _seed_resumable_source(repo, notebook.id, count=5)
    lifecycle = repo._runtime.knowledge_lifecycle
    _configure_completion(monkeypatch, lifecycle, notebook.id, mode="shadow")
    first = lifecycle.complete_relations_for_source(
        notebook.id, source_id, "Large Book", run_id, [], [], []
    )
    assert not first["exhausted"]

    queued = []
    from app.services.kg import scheduler as kg_scheduler

    monkeypatch.setattr(
        kg_scheduler, "submit_job",
        lambda fn, *args, **kwargs: queued.append(lambda: fn(*args, **kwargs)),
    )
    monkeypatch.setattr(lifecycle.settings, "kg_relation_completion_mode", "write")
    ingestion = repo._runtime.source_ingestion
    assert ingestion.resume_pending_relation_completions(page_size=1) == 1
    with repo._connect() as db:
        old_state = db.execute(
            "SELECT status FROM kg_relation_completion_state "
            "WHERE source_id=? AND source_generation=? AND mode='shadow'",
            (source_id, run_id),
        ).fetchone()
        new_state = db.execute(
            "SELECT status FROM kg_relation_completion_state "
            "WHERE source_id=? AND source_generation=? AND mode='write'",
            (source_id, run_id),
        ).fetchone()
    assert old_state["status"] == "stale"
    # Recovery authority exists before the submitted function starts. A clean
    # shutdown at this point must still be recoverable on the next startup.
    assert new_state["status"] == "pending"
    while queued:
        queued.pop(0)()
    with repo._connect() as db:
        write_state = db.execute(
            "SELECT status FROM kg_relation_completion_state "
            "WHERE source_id=? AND source_generation=? AND mode='write'",
            (source_id, run_id),
        ).fetchone()
    assert write_state["status"] == "completed"


def test_mode_transition_keeps_new_pending_state_when_submission_fails(
    repo, monkeypatch
):
    notebook = repo.create_notebook(NotebookCreate(name="failed mode scheduling"))
    source_id, run_id = _seed_resumable_source(repo, notebook.id, count=5)
    lifecycle = repo._runtime.knowledge_lifecycle
    _configure_completion(monkeypatch, lifecycle, notebook.id, mode="shadow")
    lifecycle.complete_relations_for_source(
        notebook.id, source_id, "Large Book", run_id, [], [], []
    )
    monkeypatch.setattr(lifecycle.settings, "kg_relation_completion_mode", "write")
    from app.services.kg import scheduler as kg_scheduler

    monkeypatch.setattr(
        kg_scheduler, "submit_job",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("queue down")),
    )
    ingestion = repo._runtime.source_ingestion
    assert ingestion.resume_pending_relation_completions(page_size=1) == 0
    with repo._connect() as db:
        states = {
            row["mode"]: row["status"]
            for row in db.execute(
                "SELECT mode,status FROM kg_relation_completion_state "
                "WHERE source_id=? AND source_generation=?",
                (source_id, run_id),
            ).fetchall()
        }
    assert states == {"shadow": "stale", "write": "pending"}


def test_new_mode_pending_survives_worker_failure_before_first_page(
    repo, monkeypatch
):
    notebook = repo.create_notebook(NotebookCreate(name="failed mode worker"))
    source_id, run_id = _seed_resumable_source(repo, notebook.id, count=5)
    lifecycle = repo._runtime.knowledge_lifecycle
    _configure_completion(monkeypatch, lifecycle, notebook.id, mode="shadow")
    lifecycle.complete_relations_for_source(
        notebook.id, source_id, "Large Book", run_id, [], [], []
    )
    monkeypatch.setattr(lifecycle.settings, "kg_relation_completion_mode", "write")
    queued = []
    from app.services.kg import scheduler as kg_scheduler

    monkeypatch.setattr(
        kg_scheduler, "submit_job",
        lambda fn, *args, **kwargs: queued.append(lambda: fn(*args, **kwargs)),
    )
    ingestion = repo._runtime.source_ingestion
    original = lifecycle.complete_relations_for_source
    assert ingestion.resume_pending_relation_completions(page_size=1) == 1
    monkeypatch.setattr(
        lifecycle, "complete_relations_for_source",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("early worker failure")),
    )
    queued.pop(0)()
    monkeypatch.setattr(lifecycle, "complete_relations_for_source", original)
    with repo._connect() as db:
        state = db.execute(
            "SELECT status FROM kg_relation_completion_state "
            "WHERE source_id=? AND source_generation=? AND mode='write'",
            (source_id, run_id),
        ).fetchone()
    assert state["status"] == "pending"


def test_startup_resume_retires_pending_cursor_when_mode_turns_off(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="mode off"))
    source_id, run_id = _seed_resumable_source(repo, notebook.id, count=5)
    lifecycle = repo._runtime.knowledge_lifecycle
    _configure_completion(monkeypatch, lifecycle, notebook.id, mode="shadow")
    first = lifecycle.complete_relations_for_source(
        notebook.id, source_id, "Large Book", run_id, [], [], []
    )
    assert not first["exhausted"]
    monkeypatch.setattr(lifecycle.settings, "kg_relation_completion_mode", "off")
    ingestion = repo._runtime.source_ingestion
    assert ingestion.resume_pending_relation_completions(page_size=1) == 0
    with repo._connect() as db:
        state = db.execute(
            "SELECT status FROM kg_relation_completion_state "
            "WHERE source_id=? AND source_generation=? AND mode='shadow'",
            (source_id, run_id),
        ).fetchone()
    assert state["status"] == "stale"


def test_persisted_quote_is_exactly_the_server_excerpt_seen_by_verifier(
    repo, monkeypatch
):
    notebook = repo.create_notebook(NotebookCreate(name="verified excerpt"))
    source_id, run_id = _seed_resumable_source(repo, notebook.id, count=2)
    lifecycle = repo._runtime.knowledge_lifecycle
    _configure_completion(monkeypatch, lifecycle, notebook.id, mode="write")
    stats = lifecycle.complete_relations_for_source(
        notebook.id, source_id, "Large Book", run_id, [], [], []
    )
    assert stats["inserted"] == 1
    with repo._connect() as db:
        row = db.execute(
            "SELECT evidence FROM knowledge_relations WHERE source_id=?",
            (source_id,),
        ).fetchone()
    evidence = json.loads(row["evidence"])
    assert len(evidence[0]["quote"]) > 400
    assert "relation-marker-" in evidence[0]["quote"]
    assert evidence[0]["quote"] == evidence[0]["quoted_span"]


def test_zero_model_budget_fails_closed_without_creating_or_advancing_state(
    repo, monkeypatch
):
    notebook = repo.create_notebook(NotebookCreate(name="invalid completion rails"))
    source_id, run_id = _seed_resumable_source(repo, notebook.id, count=2)
    lifecycle = repo._runtime.knowledge_lifecycle
    _configure_completion(monkeypatch, lifecycle, notebook.id, mode="shadow")
    monkeypatch.setattr(lifecycle.settings, "kg_relation_completion_max_batches", 0)
    stats = lifecycle.complete_relations_for_source(
        notebook.id, source_id, "Large Book", run_id, [], [], []
    )
    assert stats["skip_reason"] == "invalid_limits"
    with repo._connect() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM kg_relation_completion_state WHERE source_id=?",
            (source_id,),
        ).fetchone()[0]
    assert count == 0
