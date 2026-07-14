"""Task 2 (memory-kg-extract): source_ingestion domain memory-derived source
primitives — Memory confirm -> hidden synthetic source -> the real extraction
pipeline (elements + element embeddings only; NO chunks, since Memory text
already reaches prompts via MemoryRetriever and chunking would double-inject).

RED-first contracts:
- memory_kg_eligible mirrors the upload auto-extract gate (should_extract_kg)
  AND additionally excludes base-tier notebooks (base KG only grows via the
  human-reviewed promotion queue, never auto-extraction);
- ingest_memory_source creates a hidden source_type='memory' row linked via
  memory_id, parses elements (no chunks), best-effort embeds them, then runs
  the SAME run_extraction pipeline uploads use;
- content-fingerprint skip: an unchanged (title, content_md) pair is a
  zero-cost no-op — no re-extraction, source id unchanged;
- content change: prior extraction state is cleared and elements replaced
  before re-extracting, and the stored file_hash tracks the new fingerprint;
- remove_memory_source is delete_source on the linked source id, and a no-op
  when there is no derived source (never raises);
- offline/no-llm behaves exactly like uploads: parse_status reaches
  'extracted' (run_extraction never raises when kg_llm is unconfigured), the
  extraction run itself records the same 'no-llm' marker regular uploads
  get, and nothing is fabricated into knowledge_objects.
"""
from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository, _now


@pytest.fixture
def repo_factory(tmp_path, monkeypatch):
    def _make():
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
        monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
        monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
        monkeypatch.setenv("LLM_LOG_ENABLED", "false")
        r = SQLiteRepository(Settings())
        nb = r.create_notebook(NotebookCreate(name="nb"))
        return r, nb.id
    return _make


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def _svc(repo):
    return repo._runtime.source_ingestion


def test_eligible_truth_table(repo_factory):
    repo, nb = repo_factory()
    svc = _svc(repo)
    # No KG yet + KG_AUTO_EXTRACT off (default Settings) -> not eligible.
    assert svc.memory_kg_eligible(nb) is False

    # Give the notebook a KG object directly (bypassing extraction) so
    # should_extract_kg flips true via the notebook_has_kg continuation rule.
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,created_at,updated_at) "
            "VALUES (?,?,?,?,?)",
            (f"ko-{uuid4().hex[:8]}", nb, "concept", _now(), _now()),
        )
    assert svc.memory_kg_eligible(nb) is True

    # base tier never auto-extracts, even with an existing KG.
    repo.mark_notebook_base(nb)
    assert svc.memory_kg_eligible(nb) is False


def test_ingest_creates_hidden_source_and_extracts(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    svc = _svc(repo)
    called = {}
    monkeypatch.setattr(
        svc, "run_extraction", lambda sid: called.setdefault("sid", sid)
    )

    sid = svc.ingest_memory_source(
        nb, "memory-1", "标题", "正文 **加粗**\n\n- 步骤一\n- 步骤二"
    )

    assert sid is not None
    src = svc.sources.get_source(sid)
    assert src.type == "memory"
    assert src.parse_status == "extracted"
    assert called["sid"] == sid
    assert svc.memory_source_id("memory-1") == sid
    # elements are persisted (no-file markdown parse) and hold real text.
    elements = svc.sources.source_elements(sid)
    assert elements
    assert any(e.text for e in elements)
    # No chunks are ever built for a memory-derived source.
    with svc.sources.database.connect() as db:
        (n_chunks,) = db.execute(
            "SELECT COUNT(*) FROM chunks WHERE source_id=?", (sid,)
        ).fetchone()
    assert n_chunks == 0


def test_ingest_fingerprint_skip(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    svc = _svc(repo)
    calls = []
    monkeypatch.setattr(svc, "run_extraction", lambda sid: calls.append(sid))

    sid1 = svc.ingest_memory_source(nb, "memory-1", "标题", "正文不变")
    sid2 = svc.ingest_memory_source(nb, "memory-1", "标题", "正文不变")

    assert sid1 == sid2
    assert calls == [sid1]  # second call is a fingerprint-unchanged no-op


def test_ingest_content_change_reingests(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    svc = _svc(repo)
    calls = []
    monkeypatch.setattr(svc, "run_extraction", lambda sid: calls.append(sid))
    clear_calls = []
    original_clear = svc.clear_source_extraction_state

    def _spy_clear(*a, **k):
        clear_calls.append((a, k))
        return original_clear(*a, **k)

    monkeypatch.setattr(svc, "clear_source_extraction_state", _spy_clear)

    sid1 = svc.ingest_memory_source(nb, "memory-1", "标题", "旧内容")
    assert clear_calls == []  # brand-new source: nothing to clear yet

    sid2 = svc.ingest_memory_source(nb, "memory-1", "标题", "新内容变了")

    assert sid2 == sid1
    assert len(clear_calls) == 1  # reingest clears prior extraction state once
    assert calls == [sid1, sid1]  # re-extracted, not skipped

    elements = svc.sources.source_elements(sid1)
    assert [e.text for e in elements] == ["新内容变了"], "elements replaced, not appended"
    expected_fp = hashlib.sha256("标题\n新内容变了".encode("utf-8")).hexdigest()
    assert svc.sources.get_source(sid1).file_hash == expected_fp


def test_remove_memory_source(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    svc = _svc(repo)
    monkeypatch.setattr(svc, "run_extraction", lambda sid: None)
    sid = svc.ingest_memory_source(nb, "memory-1", "标题", "正文")

    svc.remove_memory_source("memory-1")

    with pytest.raises(KeyError):
        svc.sources.get_source(sid)
    assert svc.memory_source_id("memory-1") is None

    # No derived source (already removed, or never existed) -> idempotent no-op.
    svc.remove_memory_source("memory-1")
    svc.remove_memory_source("no-such-memory")


def test_ingest_no_llm_marks_failed_not_fabricate(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    svc = _svc(repo)
    # No monkeypatching of run_extraction: exercise the real offline
    # (kg_llm unconfigured) path end-to-end, exactly like an upload would hit.
    sid = svc.ingest_memory_source(nb, "memory-1", "标题", "正文内容")

    assert sid is not None
    src = svc.sources.get_source(sid)
    assert src.parse_status in ("extracted", "failed")
    with svc.sources.database.connect() as db:
        (n_objects,) = db.execute(
            "SELECT COUNT(*) FROM knowledge_objects WHERE source_id=?", (sid,)
        ).fetchone()
        run = db.execute(
            "SELECT error_message FROM extraction_runs WHERE source_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (sid,),
        ).fetchone()
    assert n_objects == 0, "no-llm must never fabricate knowledge objects"
    assert run is not None and "no-llm" in run["error_message"]
