import json
from types import SimpleNamespace

import pytest
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository, _now
from app.core.config import Settings
from app.services.source_ingestion import PartialKgRetryIncomplete
from uuid import uuid4
from tests.model_testkit import bind_chat_client


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    settings = Settings()
    return SQLiteRepository(settings)


def test_store_kg_writes_objects_and_relations(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    objects = [
        {"local_id": "C1", "object_type": "concept",
         "payload": {"name": "Engram", "section_path": "Abstract"},
         "evidence": [{"source_id": "s1", "source_title": "Doc", "element_id": "e1",
                       "element_type": "paragraph", "location_label": "p1",
                       "quoted_span": "Engram", "confidence": 1.0}]},
        {"local_id": "K1", "object_type": "claim",
         "payload": {"name": "Engram improves perplexity", "section_path": "Abstract"},
         "evidence": [{"source_id": "s1", "source_title": "Doc", "element_id": "e1",
                       "element_type": "paragraph", "location_label": "p1",
                       "quoted_span": "improves perplexity", "confidence": 1.0}]},
    ]
    relations = [{"source_local_id": "K1", "target_local_id": "C1",
                  "edge_type": "about", "evidence": [{"quote": "Engram improves perplexity"}]}]
    n_obj, n_rel = repo.store_kg(nb.id, None, objects, relations)
    assert (n_obj, n_rel) == (2, 1)
    # raw object rows
    with repo._connect() as db:
        rows = db.execute(
            "SELECT id, object_type, status, payload FROM knowledge_objects WHERE notebook_id=? ORDER BY object_type",
            (nb.id,)).fetchall()
    assert [r["object_type"] for r in rows] == ["claim", "concept"]
    assert all(r["status"] == "approved" for r in rows)
    ids = {r["id"] for r in rows}
    # relation endpoints are real knowledge_object ids, not the local ids
    rels = repo.relations_for_notebook(nb.id)
    assert len(rels) == 1 and rels[0]["edge_type"] == "about"
    assert rels[0]["source_object_id"] in ids and rels[0]["target_object_id"] in ids


def test_store_kg_skips_unresolved_relations(repo):
    """Relations that reference a local_id not present in the objects list are
    silently skipped: they are excluded from the returned count and from
    relations_for_notebook."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    objects = [
        {"local_id": "A1", "object_type": "concept",
         "payload": {"name": "Alpha", "section_path": "S1"},
         "evidence": []},
    ]
    relations = [
        # Valid: both ends exist in objects.
        # There is only one object so no valid self-referential edge either —
        # use two objects to ensure at least one valid rel in a different test.
        # Here: target "MISSING" is not in objects → must be skipped.
        {"source_local_id": "A1", "target_local_id": "MISSING",
         "edge_type": "related", "evidence": []},
    ]
    n_obj, n_rel = repo.store_kg(nb.id, None, objects, relations)
    assert n_obj == 1
    assert n_rel == 0                                  # skipped relation not counted
    rels = repo.relations_for_notebook(nb.id)
    assert rels == []                                  # nothing written to DB


def test_add_and_read_relations(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    a = repo._test_insert_object(nb.id, "concept", {"name": "MOSFET"})
    b = repo._test_insert_object(nb.id, "claim", {"name": "MOSFET has threshold voltage"})
    repo.add_relations(nb.id, None, [
        {"source_object_id": b, "target_object_id": a, "edge_type": "about",
         "evidence": [{"quote": "threshold voltage of the MOSFET"}]},
    ])
    rels = repo.relations_for_notebook(nb.id)
    assert len(rels) == 1
    assert rels[0]["source_object_id"] == b
    assert rels[0]["target_object_id"] == a
    assert rels[0]["edge_type"] == "about"
    assert rels[0]["evidence"] == [{"quote": "threshold voltage of the MOSFET"}]


# ---------------------------------------------------------------------------
# Helpers + KG extraction path tests
# ---------------------------------------------------------------------------

def _test_insert_source(repo, notebook_id, title, file_name, doc_type, text):
    """Insert a minimal source row + one source_elements row. Returns SourceDetail."""
    source_id = f"src-{uuid4().hex[:10]}"
    now = _now()
    with repo._connect() as db:
        db.execute(
            """INSERT INTO sources
               (id, notebook_id, title, source_type, status, parse_status,
                file_name, file_path, file_size, file_hash, summary, doc_type,
                created_at, updated_at)
               VALUES (?, ?, ?, 'markdown', 'extracted', 'parsed',
                       ?, '', 0, '', '', ?, ?, ?)""",
            (source_id, notebook_id, title, file_name, doc_type, now, now),
        )
        elem_id = f"el-{uuid4().hex[:10]}"
        db.execute(
            """INSERT INTO source_elements
               (id, source_id, element_type, location_label, text, metadata, created_at)
               VALUES (?, ?, 'paragraph', 'p1', ?, '{}', ?)""",
            (elem_id, source_id, text, now),
        )
    return repo.get_source(source_id)


class _FakeLLM:
    """Faithful stub for OpenAICompatibleClient used in tests.

    Matches the real signature: chat_json(messages, response_schema_hint) -> str.
    The KG-extraction path passes a list of message dicts + a schema hint string;
    the ask path does the same.  Both receive the canned payload.
    """
    configured = True

    def __init__(self, payload):
        self._p = payload

    def chat_json(self, messages: list, response_schema_hint: str) -> str:
        return self._p

    def embed(self, text: str) -> list:
        return [0.0, 0.0]


def test_run_extraction_kg_path(repo):
    bind_chat_client(repo, "kg_extract", _FakeLLM(json.dumps({
        "nodes": [{"local_id": "a", "type": "Concept", "name": "Engram",
                   "evidence": "Engram is a memory architecture"}],
        "edges": []})))
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    src = _test_insert_source(repo, nb.id, "Doc", "doc.md", "academic_paper",
                              "Engram is a memory architecture.")
    repo._run_extraction(src.id)
    with repo._connect() as db:
        rows = db.execute(
            "SELECT object_type, payload, status FROM knowledge_objects WHERE notebook_id=?",
            (nb.id,)).fetchall()
    assert any(
        r["object_type"] == "concept"
        and r["status"] == "approved"
        and json.loads(r["payload"])["name"] == "Engram"
        for r in rows
    )


def test_run_extraction_with_surviving_edge(repo):
    """Full path: extract_graph -> build_records -> store_kg -> relations.

    Two nodes + one edge; evidence quotes are substrings of the inserted source
    text so both nodes survive evidence-binding.  After _run_extraction the
    knowledge_relations row must reference real knowledge_objects ids.
    """
    bind_chat_client(repo, "kg_extract", _FakeLLM(json.dumps({
        "nodes": [
            {"local_id": "a", "type": "Concept", "name": "Engram",
             "evidence": "Engram is a memory architecture"},
            {"local_id": "b", "type": "Claim", "name": "Engram improves perplexity",
             "evidence": "Engram improves perplexity"},
        ],
        "edges": [{"type": "about", "source": "b", "target": "a",
                   "evidence": "Engram improves perplexity"}],
    })))
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    src = _test_insert_source(
        repo, nb.id, "Doc", "doc.md", "academic_paper",
        "Engram is a memory architecture. Engram improves perplexity.",
    )
    repo._run_extraction(src.id)

    rels = repo.relations_for_notebook(nb.id)
    assert len(rels) == 1, f"expected 1 relation, got {rels}"
    rel = rels[0]

    # Both endpoints must be real knowledge_objects ids, not local ids.
    with repo._connect() as db:
        obj_ids = {
            r["id"]
            for r in db.execute(
                "SELECT id FROM knowledge_objects WHERE notebook_id=?", (nb.id,)
            ).fetchall()
        }
    assert rel["source_object_id"] in obj_ids, "source_object_id not a real object id"
    assert rel["target_object_id"] in obj_ids, "target_object_id not a real object id"


def test_reextraction_is_idempotent(repo):
    bind_chat_client(repo, "kg_extract", _FakeLLM(json.dumps({
        "nodes": [{"local_id": "a", "type": "Concept", "name": "Engram",
                   "evidence": "Engram is a memory architecture"}],
        "edges": []})))
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    src = _test_insert_source(repo, nb.id, "Doc", "doc.md", "academic_paper",
                              "Engram is a memory architecture.")
    sid = src.id
    repo._run_extraction(sid)
    repo._run_extraction(sid)
    with repo._connect() as db:
        (count,) = db.execute(
            "SELECT COUNT(*) FROM knowledge_objects WHERE notebook_id=?",
            (nb.id,)).fetchone()
    assert count == 1   # not doubled


class _FlakyLLM:
    """Raises APIConnectionError for the first `fail_n` windows it sees, then
    returns the canned payload. Used to drive failed-window counting."""
    configured = True

    def __init__(self, payload, fail_n):
        self._p = payload
        self._fail_n = fail_n
        self._seen = 0

    def chat_json(self, messages: list, response_schema_hint: str) -> str:
        from openai import APIConnectionError
        import httpx
        i = self._seen
        self._seen += 1
        if i < self._fail_n:
            raise APIConnectionError(request=httpx.Request("POST", "https://x"))
        return self._p

    def embed(self, text: str) -> list:
        return [0.0, 0.0]


def test_extraction_warning_surfaced_on_failed_windows(repo, monkeypatch):
    """A window that fails with a network error yields a non-empty
    extraction_warning on the source summary; a clean run yields None."""
    payload = json.dumps({
        "nodes": [{"local_id": "a", "type": "Concept", "name": "Engram",
                   "evidence": "Engram is a memory architecture"}],
        "edges": []})
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    src = _test_insert_source(repo, nb.id, "Doc", "doc.md", "academic_paper",
                              "Engram is a memory architecture.")
    # Force a single window, and fail it.
    bind_chat_client(repo, "kg_extract", _FlakyLLM(payload, fail_n=1))
    repo._run_extraction(src.id)

    detail = repo.get_source(src.id)
    assert detail.extraction_warning, "expected a warning when a window failed"
    assert "1/1" in detail.extraction_warning
    # The success run record carries the windows_failed token.
    with repo._connect() as db:
        row = db.execute(
            "SELECT error_message FROM extraction_runs WHERE source_id=? "
            "ORDER BY created_at DESC LIMIT 1", (src.id,)).fetchone()
    assert "windows_failed=1/1" in row["error_message"]


def test_extraction_warning_empty_on_clean_run(repo):
    payload = json.dumps({
        "nodes": [{"local_id": "a", "type": "Concept", "name": "Engram",
                   "evidence": "Engram is a memory architecture"}],
        "edges": []})
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    src = _test_insert_source(repo, nb.id, "Doc", "doc.md", "academic_paper",
                              "Engram is a memory architecture.")
    bind_chat_client(repo, "kg_extract", _FlakyLLM(payload, fail_n=0))
    repo._run_extraction(src.id)
    detail = repo.get_source(src.id)
    assert not detail.extraction_warning


def test_partial_retry_preserves_old_graph_until_clean_replacement(repo):
    repo.settings.paper_meta_enabled = False
    repo.settings.kg_gleaning_enabled = False
    repo.settings.kg_refine_enabled = False
    old_payload = json.dumps({
        "nodes": [{"local_id": "old", "type": "Concept", "name": "Engram",
                   "evidence": "Engram is a memory architecture"}],
        "edges": [],
    })
    new_payload = json.dumps({
        "nodes": [{"local_id": "new", "type": "Concept", "name": "Memory architecture",
                   "evidence": "memory architecture"}],
        "edges": [],
    })
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    src = _test_insert_source(
        repo, nb.id, "Doc", "doc.md", "academic_paper",
        "Engram is a memory architecture.",
    )
    bind_chat_client(repo, "kg_extract", _FakeLLM(old_payload))
    repo._run_extraction(src.id)
    with repo._connect() as db:
        before = {
            row["id"]: json.loads(row["payload"])["name"]
            for row in db.execute(
                "SELECT id,payload FROM knowledge_objects WHERE source_id=?",
                (src.id,),
            ).fetchall()
        }
    assert list(before.values()) == ["Engram"]

    bind_chat_client(repo, "kg_extract", _FlakyLLM(new_payload, fail_n=1))
    with pytest.raises(PartialKgRetryIncomplete, match="existing KG preserved"):
        repo._runtime.source_ingestion.run_extraction(
            src.id, preserve_existing_until_complete=True
        )
    with repo._connect() as db:
        after_failed = {
            row["id"]: json.loads(row["payload"])["name"]
            for row in db.execute(
                "SELECT id,payload FROM knowledge_objects WHERE source_id=?",
                (src.id,),
            ).fetchall()
        }
        failed_run = db.execute(
            "SELECT status,error_message FROM extraction_runs WHERE source_id=? "
            "ORDER BY created_at DESC,rowid DESC LIMIT 1",
            (src.id,),
        ).fetchone()
    assert after_failed == before
    assert failed_run["status"] == "completed"
    assert "windows_failed=1/1" in failed_run["error_message"]

    bind_chat_client(
        repo,
        "kg_extract",
        _FakeLLM(json.dumps({"nodes": [], "edges": []})),
    )
    with pytest.raises(PartialKgRetryIncomplete, match="existing KG preserved"):
        repo._runtime.source_ingestion.run_extraction(
            src.id, preserve_existing_until_complete=True
        )
    with repo._connect() as db:
        after_empty = {
            row["id"]: json.loads(row["payload"])["name"]
            for row in db.execute(
                "SELECT id,payload FROM knowledge_objects WHERE source_id=?",
                (src.id,),
            ).fetchall()
        }
        empty_run = db.execute(
            "SELECT status,error_message FROM extraction_runs WHERE source_id=? "
            "ORDER BY created_at DESC,rowid DESC LIMIT 1",
            (src.id,),
        ).fetchone()
    assert after_empty == before
    assert empty_run["status"] == "completed"
    assert "retry_incomplete=1" in empty_run["error_message"]
    assert "empty_result=1" in empty_run["error_message"]

    bind_chat_client(repo, "kg_extract", _FlakyLLM(new_payload, fail_n=0))
    repo._runtime.source_ingestion.run_extraction(
        src.id, preserve_existing_until_complete=True
    )
    with repo._connect() as db:
        after_success = {
            row["id"]: json.loads(row["payload"])["name"]
            for row in db.execute(
                "SELECT id,payload FROM knowledge_objects WHERE source_id=?",
                (src.id,),
            ).fetchall()
        }
        completed_run = db.execute(
            "SELECT status,error_message FROM extraction_runs WHERE source_id=? "
            "ORDER BY created_at DESC,rowid DESC LIMIT 1",
            (src.id,),
        ).fetchone()
    assert list(after_success.values()) == ["Memory architecture"]
    assert set(after_success).isdisjoint(before)
    assert completed_run["status"] == "completed"
    assert "windows_failed=0/1" in completed_run["error_message"]


def test_partial_retry_store_failure_rolls_back_to_old_graph(repo, monkeypatch):
    repo.settings.paper_meta_enabled = False
    repo.settings.kg_gleaning_enabled = False
    repo.settings.kg_refine_enabled = False
    payload = json.dumps({
        "nodes": [{"local_id": "a", "type": "Concept", "name": "Engram",
                   "evidence": "Engram is a memory architecture"}],
        "edges": [],
    })
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    src = _test_insert_source(
        repo, nb.id, "Doc", "doc.md", "academic_paper",
        "Engram is a memory architecture.",
    )
    bind_chat_client(repo, "kg_extract", _FakeLLM(payload))
    repo._run_extraction(src.id)
    with repo._connect() as db:
        before_ids = {
            row["id"] for row in db.execute(
                "SELECT id FROM knowledge_objects WHERE source_id=?", (src.id,)
            ).fetchall()
        }

    def fail_insert(*_args, **_kwargs):
        raise RuntimeError("replacement insert failed")

    monkeypatch.setattr(repo._runtime.knowledge, "insert_object_chunk", fail_insert)
    with pytest.raises(RuntimeError, match="replacement insert failed"):
        repo._runtime.source_ingestion.run_extraction(
            src.id, preserve_existing_until_complete=True
        )

    with repo._connect() as db:
        after_ids = {
            row["id"] for row in db.execute(
                "SELECT id FROM knowledge_objects WHERE source_id=?", (src.id,)
            ).fetchall()
        }
    assert after_ids == before_ids


def test_knowledge_graph_from_kg_tables(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    c = repo._test_insert_object(nb.id, "concept", {"name": "Engram"})
    k = repo._test_insert_object(nb.id, "claim", {"name": "Engram improves perplexity"})
    repo.add_relations(nb.id, None, [{"source_object_id": k, "target_object_id": c,
                                      "edge_type": "about", "evidence": []}])
    g = repo.knowledge_graph(nb.id)
    assert {n.object_type for n in g.nodes} == {"concept", "claim"}
    assert any(n.headline == "Engram" for n in g.nodes)
    assert any(n.headline == "Engram improves perplexity" for n in g.nodes)
    assert len(g.edges) == 1
    e = g.edges[0]
    assert e.from_id == k and e.to_id == c and e.relation == "about"


# ---------------------------------------------------------------------------
# Task 7 tests: KG node-type weights + KG-native ask
# ---------------------------------------------------------------------------

def test_ask_returns_kg_knowledge(repo):
    # P4-5: ask_fast retired; verify KG retrieval via _retrieve_scored directly.
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo._test_insert_object(nb.id, "claim", {"name": "Engram improves perplexity"})
    hits = repo._retrieve_scored(nb.id, "does engram improve perplexity?")
    assert any("Engram" in (h.payload.get("name") or "") for h in hits)


def test_1hop_relation_is_stored(repo):
    """A concept linked by 'about' to a claim must have a knowledge_relations row,
    and the claim must be retrievable via _retrieve_scored."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))

    # Claim that matches the query by name.
    claim_id = repo._test_insert_object(
        nb.id, "claim", {"name": "Engram improves perplexity"}
    )
    # Concept whose name does NOT appear in the query; would never surface
    # through text scoring alone.
    concept_id = repo._test_insert_object(
        nb.id, "concept", {"name": "XyZzY_unrelated_concept"}
    )
    # Wire them with an 'about' relation: claim -> concept.
    repo.add_relations(nb.id, None, [
        {"source_object_id": claim_id, "target_object_id": concept_id,
         "edge_type": "about", "evidence": [{"quote": "Engram improves perplexity"}]},
    ])

    # Verify the claim is retrieved by _retrieve_scored (direct scoring path).
    hits = repo._retrieve_scored(nb.id, "does engram improve perplexity?")
    assert any("Engram" in (h.payload.get("name") or "") for h in hits), \
        "matching claim must be in results"

    # Verify the 1-hop relation is stored (concept reachable from claim via DB).
    with repo._connect() as db:
        row = db.execute(
            "SELECT target_object_id FROM knowledge_relations "
            "WHERE notebook_id=? AND source_object_id=?",
            (nb.id, claim_id),
        ).fetchone()
    assert row is not None, "knowledge_relations must link claim -> concept"
    assert row["target_object_id"] == concept_id


def test_builtin_whitelist_seeded(repo):
    terms = repo.concept_whitelist_terms()
    assert "vco" in terms
    assert "mosfet" in terms
    assert "ic" in terms  # 2-char EE acronym protected from the too_short rule


def test_whitelist_add_list_remove(repo):
    repo.concept_whitelist_add("Gm Cell", note="custom")
    assert "gm cell" in repo.concept_whitelist_terms()
    listed = repo.concept_whitelist_list()
    assert any(e["term"] == "gm cell" and e["note"] == "custom" for e in listed)
    repo.concept_whitelist_remove("gm cell")
    assert "gm cell" not in repo.concept_whitelist_terms()


def test_extract_source_delegates_to_run_extraction(repo, monkeypatch):
    called = []
    monkeypatch.setattr(
        repo._runtime.source_store,
        "get_source",
        lambda source_id: SimpleNamespace(
            id=source_id, notebook_id="nb-admitted"
        ),
    )
    monkeypatch.setattr(
        repo,
        "require_indexing_pipeline_write",
        lambda notebook_id: called.append(("admit", notebook_id)),
    )
    monkeypatch.setattr(repo._runtime.source_ingestion, "run_extraction", lambda sid: called.append(sid))
    repo.extract_source("src-z")
    assert called == [("admit", "nb-admitted"), "src-z"]


def test_delete_notebook_kg_clears_kg_but_keeps_elements(repo):
    from app.services.sqlite_repository import _now
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    now = _now()
    with repo._connect() as db:
        db.execute(
            "INSERT INTO sources (id, notebook_id, title, source_type, status, parse_status, "
            "file_name, file_path, file_size, file_hash, summary, doc_type, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("src-x", nb.id, "t", "markdown", "extracted", "parsed", "f", "", 0, "", "", "", now, now),
        )
        db.execute(
            "INSERT INTO source_elements (id, source_id, element_type, location_label, text, metadata, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("el-x", "src-x", "paragraph", "p1", "hello", "{}", now),
        )
        db.execute(
            "INSERT INTO knowledge_objects (id, notebook_id, object_type, status, owner, payload, evidence, "
            "source_candidate_id, source_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("ko-x", nb.id, "concept", "approved", "", "{}", "[]", None, "src-x", now, now),
        )
        db.execute(
            "INSERT INTO knowledge_relations (id, notebook_id, source_id, source_object_id, target_object_id, "
            "edge_type, evidence, created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("rel-x", nb.id, "src-x", "ko-x", "ko-x", "about", "[]", now),
        )
    counts = repo.delete_notebook_kg(nb.id)
    with repo._connect() as db:
        assert db.execute("SELECT COUNT(*) c FROM knowledge_objects WHERE notebook_id=?", (nb.id,)).fetchone()["c"] == 0
        assert db.execute("SELECT COUNT(*) c FROM knowledge_relations WHERE notebook_id=?", (nb.id,)).fetchone()["c"] == 0
        assert db.execute("SELECT COUNT(*) c FROM source_elements WHERE source_id='src-x'").fetchone()["c"] == 1
    assert counts["knowledge_objects"] == 1


def test_delete_notebook_kg_preserves_memory_projection_artifacts(repo):
    """A document rebuild must not erase confirmed Memory's derived KG rows."""
    from app.services.sqlite_repository import _now

    nb = repo.create_notebook(NotebookCreate(name="memory-preserve"))
    now = _now()
    with repo._connect() as db:
        for source_id, source_type in (("src-doc", "markdown"), ("src-mem", "memory")):
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
                "file_name,file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (source_id, nb.id, source_id, source_type, "extracted", "parsed", "", "", 0,
                 source_id, "", "", now, now),
            )
        for object_id, source_id in (("ko-doc", "src-doc"), ("ko-mem", "src-mem")):
            db.execute(
                "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,"
                "evidence,source_candidate_id,source_id,created_at,updated_at) "
                "VALUES (?,?,?,'approved','','{}','[]',NULL,?,?,?)",
                (object_id, nb.id, "concept", source_id, now, now),
            )
            db.execute(
                "INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) "
                "VALUES (?,?,?,?)", (object_id, nb.id, "[0.1,0.2]", now),
            )
            db.execute(
                "INSERT INTO kg_objects_fts (object_id,notebook_id,name) VALUES (?,?,?)",
                (object_id, nb.id, object_id),
            )
            db.execute(
                "INSERT INTO knowledge_object_sources (object_id,source_id,notebook_id) "
                "VALUES (?,?,?)", (object_id, source_id, nb.id),
            )
            db.execute(
                "INSERT INTO knowledge_relations (id,notebook_id,source_id,source_object_id,"
                "target_object_id,edge_type,evidence,created_at) VALUES (?,?,?,?,?,?,'[]',?)",
                (f"rel-{source_id}", nb.id, source_id, object_id, object_id, "relates_to", now),
            )
            db.execute(
                "INSERT INTO extraction_runs (id,notebook_id,source_id,run_type,status,"
                "error_message,created_at,updated_at) VALUES (?,?,?,'kg','completed','',?,?)",
                (f"run-{source_id}", nb.id, source_id, now, now),
            )

    repo.delete_notebook_kg(nb.id)

    with repo._connect() as db:
        for table, id_column, kept, removed in (
            ("knowledge_objects", "id", "ko-mem", "ko-doc"),
            ("knowledge_embeddings", "object_id", "ko-mem", "ko-doc"),
            ("kg_objects_fts", "object_id", "ko-mem", "ko-doc"),
            ("knowledge_object_sources", "object_id", "ko-mem", "ko-doc"),
            ("knowledge_relations", "id", "rel-src-mem", "rel-src-doc"),
            ("extraction_runs", "id", "run-src-mem", "run-src-doc"),
        ):
            assert db.execute(
                f"SELECT COUNT(*) c FROM {table} WHERE {id_column}=?", (kept,)
            ).fetchone()["c"] == 1
            assert db.execute(
                f"SELECT COUNT(*) c FROM {table} WHERE {id_column}=?", (removed,)
            ).fetchone()["c"] == 0
