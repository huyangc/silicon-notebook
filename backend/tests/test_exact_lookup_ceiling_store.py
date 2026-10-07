"""The exact-identifier channel under a source ceiling, on a real store.

``CandidateRetrievalService._exact_lookup_chunks`` (E2-2) pushes the run's
frozen ceiling into ``chunk_exact_search(allowed_source_ids=...)`` (E2-4).
The cases run the service over the store itself, with no double in between:
a narrowed run still finds its one in-ceiling section behind a probe window
full of out-of-ceiling matches, and a run whose ceiling leaves out a Memory
source never returns that source's section. Neither case may record a model
error. ``postgres/test_exact_lookup_ceiling_store_pg.py`` runs the same
checks on PostgreSQL."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.core.config import Settings
from app.services.embedding import FakeEmbedder
from app.services.source_scope import source_scope_context
from tests.model_testkit import bind_all_embedding_clients

T0 = datetime(2026, 9, 30, tzinfo=timezone.utc)
QUERY = "zebra_quartz_cmd 命令是怎样的"


def _scope(visible, hidden, owner, narrowed):
    return {"mode": "include", "source_ids": list(visible),
            "hidden_source_ids": list(hidden), "narrowed": narrowed,
            "owner_id": owner}


class SqliteSeed:
    def __init__(self, repo, nb):
        self.repo, self.nb = repo, nb

    def source(self, sid, kind="md", memory_id=None):
        with self.repo._write() as db:
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,status,"
                "memory_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (sid, self.nb, sid, kind, "ready", memory_id, T0.isoformat(),
                 T0.isoformat()),
            )

    def memory(self, memory_id, owner):
        with self.repo._write() as db:
            db.execute(
                "INSERT INTO memory_items(id,notebook_id,created_by,agent_profile_id,"
                "source_answer_id,origin,status,title,content_md,created_at,updated_at) "
                "VALUES (?,?,?,NULL,NULL,'ask_answer','confirmed','m','m',?,?)",
                (memory_id, self.nb, owner, T0.isoformat(), T0.isoformat()),
            )

    def chunk(self, cid, sid, text, section):
        with self.repo._write() as db:
            db.execute(
                "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
                "element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
                (cid, self.nb, sid, text, section, json.dumps([f"el-{cid}"]),
                 T0.isoformat()),
            )
            db.execute(
                "INSERT INTO chunks_fts(chunk_id,notebook_id,text) VALUES (?,?,?)",
                (cid, self.nb, text),
            )


class PgSeed(SqliteSeed):
    def source(self, sid, kind="markdown", memory_id=None):
        with self.repo._runtime.database.write() as db:
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,status,"
                "parse_status,memory_id,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,'extracted','parsed',%s,%s,%s)",
                (sid, self.nb, sid, kind, memory_id, T0, T0),
            )

    def memory(self, memory_id, owner):
        with self.repo._runtime.database.write() as db:
            db.execute(
                "INSERT INTO memory_items (id,notebook_id,created_by,origin,status,"
                "title,content_md,created_at,updated_at) VALUES "
                "(%s,%s,%s,'ask_answer','confirmed','t','c',%s,%s)",
                (memory_id, self.nb, owner, T0, T0),
            )

    def chunk(self, cid, sid, text, section):
        with self.repo._runtime.database.write() as db:
            db.execute(
                "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
                "element_ids,created_at) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s)",
                (cid, self.nb, sid, text, section, json.dumps([f"el-{cid}"]), T0),
            )


@pytest.fixture
def backend(tmp_path, monkeypatch):
    from app.models.schemas import NotebookCreate
    from app.services.sqlite_repository import (
        SQLiteRepository, reset_request_user, set_request_user,
    )
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'x.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(repo, FakeEmbedder(dim=16))
    bob = repo.create_user("b00654321", "password-12")
    token = set_request_user(bob)
    try:
        nb = repo.create_notebook(NotebookCreate(name="manual")).id
        yield repo, SqliteSeed(repo, nb), bob.id
    finally:
        reset_request_user(token)


def check_narrowed_window(backend):
    repo, seed, _bob = backend
    seed.source("src-in")
    seed.source("src-out")
    for i in range(50):
        seed.chunk(f"out-{i:02d}", "src-out", "zebra_quartz_cmd zebra_quartz_cmd",
                   f"Out {i:02d} > zebra_quartz_cmd")
    seed.chunk("in-1", "src-in",
               "zebra_quartz_cmd arguments " + "padding words " * 40,
               "Ref > zebra_quartz_cmd")
    candidates = repo.retrieval.candidates
    assert candidates.settings.exact_lookup_fts_k == 50
    errors = []
    candidates._note_model_error = lambda *a, **k: errors.append(a)
    with source_scope_context(seed.nb, _scope(["src-in"], [], "", True)):
        chunks = candidates._exact_lookup_chunks(seed.nb, QUERY)
    assert errors == []
    assert [chunk.chunk_id for chunk in chunks] == ["in-1"]


def check_closed_channel(backend):
    repo, seed, bob = backend
    seed.source("src-doc")
    seed.memory("mem-1", bob)
    seed.source("src-mem", kind="memory", memory_id="mem-1")
    seed.chunk("cX", "src-mem", "zebra_quartz_cmd SECRETMEMO arguments: --private",
               "zebra_quartz_cmd")
    seed.chunk("cD", "src-doc", "zebra_quartz_cmd public usage", "Doc > zebra_quartz_cmd")
    candidates = repo.retrieval.candidates
    with source_scope_context(seed.nb, _scope(["src-doc"], [], "", False)):
        assert candidates._unsafe_source_scope_restricted(seed.nb) is False
        chunks = candidates._exact_lookup_chunks(seed.nb, QUERY)
    assert [chunk.chunk_id for chunk in chunks] == ["cD"]
    assert "SECRETMEMO" not in repr(chunks)


def check_probe_reads_the_ceiling_memo(backend):
    """The store reads a run ceiling's bound form (``CeilingSet.bound_forms``)
    BEFORE normalising the ids, under a key naming the backend AND the SQL
    form: a fresh ceiling leaves its ``member_of`` form there; a form already
    there is what binds (it is the run's own, built once); and another form's
    entry on the same object -- ``source_ceiling.ceiling_param``'s, keyed by
    the backend alone -- is never mistaken for it."""
    from app.services.source_scope import CeilingSet

    repo, seed, _bob = backend
    if isinstance(seed, PgSeed):
        from app.repositories.postgres.id_binding import bind_ids
        form, bind = ("postgres", "member_of"), (lambda ids: bind_ids(sorted(ids)))
        other = "postgres"
    else:
        from app.repositories.sqlite.id_binding import bind_ids
        form, bind = ("sqlite", "member_of"), (lambda ids: bind_ids(ids, sort=True))
        other = "sqlite"
    for source_id in ("src-in", "src-x"):
        seed.source(source_id)
        seed.chunk(f"c-{source_id}", source_id, "zebra_quartz_cmd body",
                   f"{source_id} > zebra_quartz_cmd")
    deps = repo.retrieval.candidates._exact_lookup_deps()

    def probe(ceiling):
        with deps.connect() as db:
            return sorted(row["chunk_id"] for row in deps.exact_search(
                db, seed.nb, "zebra_quartz_cmd", 50, allowed_source_ids=ceiling))

    fresh = CeilingSet({"src-in"})
    assert probe(fresh) == ["c-src-in"]
    assert set(fresh.bound_forms) == {form}

    memoised = CeilingSet({"src-in"})
    memoised.bound_forms[form] = bind(["src-x"])
    assert probe(memoised) == ["c-src-x"], "the memo is read before the ids"

    foreign_form = CeilingSet({"src-in"})
    foreign_form.bound_forms[other] = bind(["src-x"])
    assert probe(foreign_form) == ["c-src-in"], "another form's key is not this one"
    assert probe(CeilingSet()) == []


def test_the_probe_reads_the_ceiling_memo(backend):
    check_probe_reads_the_ceiling_memo(backend)


def test_a_narrowed_run_finds_the_in_ceiling_section_behind_a_full_window(backend):
    check_narrowed_window(backend)


def test_closed_channel_exact_lookup_returns_no_memory_section(backend):
    check_closed_channel(backend)
