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


# ── batch-3-W1 PR-2: unified_kg_state seq semantics (design doc Sec 3.3) ──


def test_delete_notebook_kg_resets_unified_kg_state_in_place_and_advances_epoch(repo):
    """``delete_notebook_kg`` no longer DELETEs the ``unified_kg_state`` row
    (which used to make ``kg_mutation_seq`` restart from 0 and alias with a
    prior version — kg_mutation.py's FULL CENSUS, ``review_queue_memo``'s
    former gap 2). It now RESETS the row in place, in the SAME transaction as
    the graph-row deletes, and advances the new ``kg_reset_epoch`` column by
    exactly 1. The row must still exist afterward (not re-inserted bare)."""
    nb = repo.create_notebook(NotebookCreate(name="epoch reset"))
    n_obj, n_rel = repo.store_kg(
        nb.id, None,
        [{"local_id": "C1", "object_type": "concept", "payload": {"name": "x"}, "evidence": []}],
        [],
    )
    assert (n_obj, n_rel) == (1, 0)

    with repo._connect() as db:
        before = db.execute(
            "SELECT * FROM unified_kg_state WHERE notebook_id=?", (nb.id,)
        ).fetchone()
    assert before is not None
    assert int(before["kg_mutation_seq"]) > 0
    assert int(before["kg_reset_epoch"]) == 0

    repo.delete_notebook_kg(nb.id)

    with repo._connect() as db:
        after = db.execute(
            "SELECT * FROM unified_kg_state WHERE notebook_id=?", (nb.id,)
        ).fetchone()
    assert after is not None, "the row must be RESET in place, never dropped"
    assert int(after["kg_mutation_seq"]) == 0
    assert int(after["cluster_mutation_seq"]) == 0
    assert int(after["community_seq"]) == -1
    assert int(after["canonical_rel_seq"]) == -1
    assert int(after["mention_seq"]) == -1
    assert after["cluster_input_version"] == ""
    assert (after["last_rebuild_at"] or "") == ""
    assert int(after["object_count"]) == 0
    assert int(after["relation_count"]) == 0
    assert int(after["cluster_count"]) == 0
    assert int(after["kg_reset_epoch"]) == 1, "the ONE writer of kg_reset_epoch must bump it by 1"

    # A second delete (on the now-empty graph) advances the epoch again —
    # only increases, never decreases or resets.
    repo.delete_notebook_kg(nb.id)
    with repo._connect() as db:
        twice = db.execute(
            "SELECT kg_reset_epoch FROM unified_kg_state WHERE notebook_id=?", (nb.id,)
        ).fetchone()
    assert int(twice["kg_reset_epoch"]) == 2


def test_delete_notebook_kg_matches_a_freshly_created_birth_row_byte_for_byte(repo):
    """kg_analysis._state_view treats ``kg_mutation_seq==0 and not
    last_rebuild_at`` as byte-identical to "row absent" (the never-written
    notebook contract, test_born_state_row_reports_like_a_never_written_
    notebook — deliberately NOT touched by this PR). This asserts the
    RESET row after delete_notebook_kg matches a truly fresh notebook's birth
    row on every column that contract reads, so the KG-analysis overview
    keeps reporting "never computed" post-delete exactly like it always has
    for a brand-new notebook — kg_reset_epoch is the one column that
    legitimately differs (it is not part of that contract)."""
    born = repo.create_notebook(NotebookCreate(name="birth"))
    deleted = repo.create_notebook(NotebookCreate(name="to be reset"))
    repo.store_kg(
        deleted.id, None,
        [{"local_id": "C1", "object_type": "concept", "payload": {"name": "x"}, "evidence": []}],
        [],
    )
    repo.delete_notebook_kg(deleted.id)

    with repo._connect() as db:
        born_row = dict(db.execute(
            "SELECT * FROM unified_kg_state WHERE notebook_id=?", (born.id,)
        ).fetchone())
        reset_row = dict(db.execute(
            "SELECT * FROM unified_kg_state WHERE notebook_id=?", (deleted.id,)
        ).fetchone())

    for column in (
        "dirty", "kg_mutation_seq", "cluster_mutation_seq", "cluster_input_version",
        "last_rebuild_at", "object_count", "relation_count", "cluster_count",
        "community_seq", "canonical_rel_seq", "mention_seq",
    ):
        assert reset_row[column] == born_row[column], (
            f"{column} diverged from a fresh birth row: "
            f"{reset_row[column]!r} != {born_row[column]!r}"
        )
    # kg_reset_epoch is deliberately NOT part of the birth-row contract.
    assert int(born_row["kg_reset_epoch"]) == 0
    assert int(reset_row["kg_reset_epoch"]) == 1


def test_delete_notebook_kg_removes_this_notebooks_kg_analysis_artifacts_ledger(repo):
    """design doc Sec 3.2 table #15: the analysis-artifact LEDGER
    (``kg_analysis_artifacts``) is blanket-deleted alongside the graph, not
    reset — a ledger row's whole meaning is "built at this seq", and there is
    no meaningful reset shape for it. Before this, a cleared graph left
    ledger rows carrying a pre-reset kg_mutation_seq, making
    kg_analysis._artifact_freshness compute a NEGATIVE seq_behind (the
    module's own contract treats that as "database was hand-edited").

    R1 (P2-1, post-review): the ledger row and its DETAIL tables
    (``kg_community_edges`` / ``kg_source_profiles``) are a documented single
    unit — ``discard_board_dependent_kg_analysis_artifacts``'s own docstring
    states plainly that leaving the detail half behind dangles pointers to a
    board partition with no governing ledger row. This test now also seeds
    and asserts on those two detail tables.

    变异锚点:把 kg_community_edges/kg_source_profiles 从清空序列里去掉,本条
    必须报红(明细行残留)。"""
    nb = repo.create_notebook(NotebookCreate(name="artifact ledger"))
    from app.services.sqlite_repository import _now
    now = _now()
    with repo._connect() as db:
        db.execute(
            "INSERT INTO kg_analysis_artifacts (notebook_id,kind,kg_mutation_seq,payload,created_at) "
            "VALUES (?,?,?,?,?)",
            (nb.id, "cross_board_edges", 5, "{}", now),
        )
        db.execute(
            "INSERT INTO kg_analysis_artifacts (notebook_id,kind,kg_mutation_seq,payload,created_at) "
            "VALUES (?,?,?,?,?)",
            (nb.id, "source_board_profiles", 5, "{}", now),
        )
        db.execute(
            "INSERT INTO kg_community_edges (notebook_id,src_community_id,dst_community_id,weight) "
            "VALUES (?,?,?,?)",
            (nb.id, "cid-a", "cid-b", 3),
        )
        db.execute(
            "INSERT INTO kg_source_profiles (notebook_id,source_id) VALUES (?,?)",
            (nb.id, "src-x"),
        )
    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) c FROM kg_analysis_artifacts WHERE notebook_id=?", (nb.id,)
        ).fetchone()["c"] == 2
        assert db.execute(
            "SELECT COUNT(*) c FROM kg_community_edges WHERE notebook_id=?", (nb.id,)
        ).fetchone()["c"] == 1
        assert db.execute(
            "SELECT COUNT(*) c FROM kg_source_profiles WHERE notebook_id=?", (nb.id,)
        ).fetchone()["c"] == 1

    counts = repo.delete_notebook_kg(nb.id)

    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) c FROM kg_community_edges WHERE notebook_id=?", (nb.id,)
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) c FROM kg_source_profiles WHERE notebook_id=?", (nb.id,)
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) c FROM kg_analysis_artifacts WHERE notebook_id=?", (nb.id,)
        ).fetchone()["c"] == 0
    assert counts["kg_analysis_artifacts"] == 2


def test_delete_notebook_kg_upserts_when_the_state_row_was_already_missing(repo):
    """R1 (P0-2, post-review): scripts/merge_dbs.py's ``KG_STATE_TABLES``
    deliberately DELETEs a merged notebook's ``unified_kg_state`` row so the
    deployment recomputes it fresh (nothing re-creates it until the next real
    mutation). If ``delete_notebook_kg`` runs against such a notebook before
    anything else touches the row, a bare ``UPDATE ... WHERE notebook_id=?``
    is a silent no-op (rowcount 0): the row stays absent, ``kg_reset_epoch``
    never advances, and every reader's "row is None" fallback keeps computing
    the SAME ``(epoch=0, seq=0)`` version key both before and after the call
    — even though this same transaction just deleted real graph rows. This
    test reproduces that exact sequence and asserts the row is
    resurrected (not left absent) with ``kg_reset_epoch=1``, and that a memo
    warmed while the row was absent does NOT keep serving its stale value
    afterward.

    变异锚点:把 UPSERT 改回裸 UPDATE,本条必须报红——行缺失路径下 epoch 不动、
    memo 端出清图前的旧计数。
    """
    from app.repositories.sqlite import knowledge_counts_cache as kcc

    nb = repo.create_notebook(NotebookCreate(name="missing state row"))
    repo.store_kg(
        nb.id, None,
        [
            {"local_id": "C1", "object_type": "concept", "payload": {"name": "x"}, "evidence": []},
            {"local_id": "C2", "object_type": "concept", "payload": {"name": "y"}, "evidence": []},
        ],
        [],
    )
    with repo._connect() as db:
        before = db.execute(
            "SELECT * FROM unified_kg_state WHERE notebook_id=?", (nb.id,)
        ).fetchone()
    assert before is not None
    assert int(before["kg_mutation_seq"]) > 0

    # Simulate scripts/merge_dbs.py's KG_STATE_TABLES clearing: the state row
    # is gone, but the real knowledge_objects rows it described are still here.
    with repo._write() as db:
        db.execute("DELETE FROM unified_kg_state WHERE notebook_id=?", (nb.id,))
    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) c FROM unified_kg_state WHERE notebook_id=?", (nb.id,)
        ).fetchone()["c"] == 0

    # Warm a memo while the row is absent: its version key is the "row is
    # None" sentinel (epoch=0, seq=0), and it caches the CURRENT (pre-delete)
    # object count.
    kcc.invalidate()
    with repo._connect() as db:
        stale_counts = kcc.type_status_counts(db, nb.id)
    assert sum(stale_counts.values()) == 2

    counts = repo.delete_notebook_kg(nb.id)
    assert counts["knowledge_objects"] == 2

    with repo._connect() as db:
        after = db.execute(
            "SELECT * FROM unified_kg_state WHERE notebook_id=?", (nb.id,)
        ).fetchone()
    assert after is not None, "the row must be resurrected, not left absent"
    assert int(after["kg_mutation_seq"]) == 0
    assert int(after["kg_reset_epoch"]) == 1, (
        "a missing-row delete is itself a reset event: epoch must start at "
        "1, not the 0 a bare re-insert would default to"
    )

    with repo._connect() as db:
        fresh_counts = kcc.type_status_counts(db, nb.id)
    assert sum(fresh_counts.values()) == 0, (
        "the memo warmed while the row was absent must NOT keep being "
        "served after delete_notebook_kg -- its version key changed from "
        "(epoch=0, seq=0) to (epoch=1, seq=0), a genuine miss"
    )


def test_delete_notebook_kg_upsert_epoch_still_only_increases_across_repeats(repo):
    """The missing-row UPSERT path and the normal existing-row UPDATE path
    must compose: once the row is resurrected at epoch=1, further deletes
    keep incrementing normally (never resets, never repeats)."""
    nb = repo.create_notebook(NotebookCreate(name="missing then present"))
    repo.store_kg(
        nb.id, None,
        [{"local_id": "C1", "object_type": "concept", "payload": {"name": "x"}, "evidence": []}],
        [],
    )
    with repo._write() as db:
        db.execute("DELETE FROM unified_kg_state WHERE notebook_id=?", (nb.id,))

    repo.delete_notebook_kg(nb.id)
    with repo._connect() as db:
        first = db.execute(
            "SELECT kg_reset_epoch FROM unified_kg_state WHERE notebook_id=?", (nb.id,)
        ).fetchone()
    assert int(first["kg_reset_epoch"]) == 1

    repo.delete_notebook_kg(nb.id)
    with repo._connect() as db:
        second = db.execute(
            "SELECT kg_reset_epoch FROM unified_kg_state WHERE notebook_id=?", (nb.id,)
        ).fetchone()
    assert int(second["kg_reset_epoch"]) == 2
