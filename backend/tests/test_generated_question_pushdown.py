"""#822 codex r1 P2: the generated-question supplement verifies its scan on
read when the run's list is pushed down.

Sequence: the default ceiling's verdict says "binds nothing" (all selected,
no drift, no other member's Memory), so ``scoped_allowed_source_ids`` hands
the supplement no list.  A source finishes after the verdict and brings
generated questions of its own; unbound, they push the scan past
``generated_question_max_scan_rows``.  Before the fix the supplement then fell
back to the baseline -- no drift recorded, no re-run, the outer chunk check
only ever saw the baseline -- and the in-ceiling question match was silently
lost (the frozen list used to exclude those rows below the scan limit).  Now
the scan's rows are checked against the ceiling taken before the read, the
verdict flips and the scan re-runs with the frozen list: the supplement is
complete.

``build`` / ``assert_*`` are backend neutral;
``tests/postgres/test_generated_question_pushdown_pg.py`` runs them on
PostgreSQL.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.config import Settings
from app.extensions import default_extension_runtime
from app.models.notebooks import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.retrieval_run import retrieval_run
from app.services.source_scope import (
    current_source_scope,
    default_ceiling_context,
    run_ceiling_binds,
    scoped_allowed_source_ids,
)
from tests.model_testkit import bind_all_embedding_clients

QUESTION = "How long does ZX-81 holdover acquisition take?"
T0 = datetime(2026, 10, 7, tzinfo=timezone.utc)


def _source(repo, nb, source_id, chunk_id, text, placeholder):
    ph = placeholder
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            f"parse_status,created_at,updated_at) VALUES ({ph},{ph},{ph},'markdown',"
            f"'extracted','parsed',{ph},{ph})",
            (source_id, nb, source_id, T0, T0),
        )
        db.execute(
            "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
            f"element_ids,created_at) VALUES ({ph},{ph},{ph},{ph},'','[]',{ph})",
            (chunk_id, nb, source_id, text, T0),
        )


def _questions(repo, nb, source_id, chunk_id, ids, question):
    embed = repo._runtime.models.embedding("chunk_embedding").embed_texts
    texts = [question if index == 0 else f"{question} ({index})"
             for index in range(len(ids))]
    repo._runtime.chunk_store.replace_chunk_questions(
        chunk_id, nb, source_id,
        tuple(zip(ids, texts, embed(texts))),
        created_at=T0,
    )


def build(repo, placeholder: str) -> dict:
    bind_all_embedding_clients(repo, FakeEmbedder(dim=16))
    repo.settings.generated_question_index_mode = "on"
    repo.settings.generated_question_max_scan_rows = 2
    owner = repo.current_user().id
    nb = repo.create_notebook(NotebookCreate(name="question-pushdown")).id
    _source(repo, nb, "source-in", "chunk-in",
            "The original passage names a precision timing controller.", placeholder)
    # ``zz`` ids: the late source's questions sort AFTER the in-ceiling one, so
    # an unbound scan reads the in-ceiling question and then runs over.
    _questions(repo, nb, "source-in", "chunk-in", ["q-in"], QUESTION)
    return {"repo": repo, "nb": nb, "owner": owner, "placeholder": placeholder}


def assert_a_late_sources_questions_do_not_drop_the_supplement(env) -> None:
    repo, nb, owner = env["repo"], env["nb"], env["owner"]
    candidates = repo.retrieval.candidates
    with retrieval_run(run_kind="ask_chunk", actor_id=owner):
        with default_ceiling_context(nb, owner, repo._runtime.ceiling_readers()):
            scope = current_source_scope()
            assert run_ceiling_binds(scope, nb) is False     # the verdict, first
            assert scoped_allowed_source_ids(nb) is None
            _source(repo, nb, "source-late", "chunk-late", "late passage",
                    env["placeholder"])
            _questions(repo, nb, "source-late", "chunk-late",
                       ["zz-late-1", "zz-late-2"], QUESTION)
            scored, _ids, _matrix = candidates._run_chunk_candidate_contributors(
                nb, QUESTION, ([], [], None),
            )
            flipped = nb in scope._ceiling_bound_libraries

    assert [chunk.chunk_id for chunk in scored] == ["chunk-in"], scored
    assert {support.origin for support in scored[0].retrieval_supports} == {
        "generated_question"
    }
    assert flipped, "the outsider flipped the run's verdict"


@pytest.fixture
def sqlite_env(tmp_path, monkeypatch):
    from app.services.sqlite_repository import SQLiteRepository

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'q.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    monkeypatch.setenv("EMBED_DIM", "16")
    repo = SQLiteRepository(
        Settings(),
        retrieval_contributor_host=default_extension_runtime().retrieval_contributors,
    )
    try:
        yield build(repo, "?")
    finally:
        repo.close()


def test_a_late_sources_questions_do_not_drop_the_supplement_on_sqlite(sqlite_env):
    assert_a_late_sources_questions_do_not_drop_the_supplement(sqlite_env)
