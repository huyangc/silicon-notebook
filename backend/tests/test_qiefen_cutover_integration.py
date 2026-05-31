"""End-to-end cutover check: a paper markdown source ingested through the live
process_source path yields qiefen-typed candidates that approve into knowledge
objects browsable by type. Makes real LLM calls; skips if no LLM is configured.
"""
import uuid

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate, SourceImportFile, SourceImportRequest


def _repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.services.sqlite_repository import SQLiteRepository
    settings = Settings()
    if not settings.llm_configured:
        pytest.skip("no LLM configured")
    return SQLiteRepository(settings)


_PAPER = """# Abstract

We introduce Foo, a sparse memory module that provides O(1) lookup. Guided by a
U-shaped scaling law, Foo achieves superior accuracy over a strong baseline
(MMLU +3.4). Mechanistic analysis shows Foo frees attention capacity.
"""


def test_paper_ingestion_produces_qiefen_candidates(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    nb = repo.create_notebook(NotebookCreate(name="Papers", template="article"))

    # import + attach a markdown file as an academic_paper source
    [summary] = repo.import_sources(nb.id, SourceImportRequest(
        files=[SourceImportFile(file_name="foo.md", doc_type="academic_paper")]))
    fpath = (repo.storage_dir / f"{summary.id}.md").resolve()
    fpath.write_text(_PAPER, encoding="utf-8")
    with repo._connect() as db:
        db.execute("UPDATE sources SET file_path = ?, doc_type = ? WHERE id = ?",
                   (str(fpath), "academic_paper", summary.id))

    repo.process_source(summary.id)

    cands = repo.list_candidates(nb.id, None)
    assert cands, "expected qiefen candidates"
    qiefen_types = {"ArticleClaim", "ArticleMethod", "ScalingLaw",
                    "ExperimentResult", "MechanisticExplanation", "SystemDesignClaim"}
    assert any(c.candidate_type in qiefen_types for c in cands), \
        f"got types {[c.candidate_type for c in cands]}"
    # candidates carry atom-grounded evidence
    assert any(c.evidence and c.evidence[0].quoted_span for c in cands)

    # approve one -> it becomes a browsable knowledge object of that type
    target = next(c for c in cands if c.candidate_type in qiefen_types)
    repo.approve_candidate(target.id)
    recs = repo.list_knowledge(nb.id, target.candidate_type)
    assert any(r.object_type == target.candidate_type for r in recs)
