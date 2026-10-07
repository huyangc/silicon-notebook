"""E1-2: the drift probe reads one row, and an un-narrowed run binds no list.

E1-3's quality review (P1-C1) measured the cost of installing the default
ceiling where nothing was installed before: every retrieval call probes for
drift, and each probe re-read the notebook's whole visible set and the
asker's hidden half (14 probes per MCP reasoning ask, 134 per six-section
report, ~28k ids each at 49k sources).  And every chunk statement bound the
frozen 49k-id list even when it could exclude nothing (P2-C2).

Pinned here on real stores (``assert_*`` are backend neutral;
``tests/postgres/test_default_ceiling_probe_pg.py`` runs them on
PostgreSQL), with the production readers (``RepositoryRuntime
.ceiling_readers()``):

* the store's fingerprint -- ``all_visible_source_ids(nb,
  digest_for_owner=owner)`` -- equals ``source_scope.universe_digest`` of the
  two sets, for ids whose byte order and case differ and for empty halves;
* the probe reports drift for an added, a deleted and a same-count replaced
  source (an older timestamp included: a ``(count, max(created_at))``
  fingerprint misses it), for the asker's own new Memory, and not for another
  member's new Memory;
* one probe is ONE store call returning the fingerprint -- neither full read
  is made -- and it is never cached: a source added between two probes of the
  same run is drift on the second;
* the run verdict (``run_ceiling_binds``) pushes the ceiling down -- no list
  for the scope's own notebook -- only when nothing could be excluded, and
  binds it when the run is narrowed, another member's Memory is present, or
  the asker's Memory is withheld;
* verify-on-read: a source finished after the verdict is not returned by an
  unbound chunk recall; the verdict flips and the call re-runs bound.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.services.source_scope import (
    current_source_scope,
    default_ceiling_context,
    memory_access_context,
    run_ceiling_binds,
    scoped_allowed_source_ids,
    universe_digest,
)


NOW = "2026-09-30T00:00:00+00:00"
EARLIER = "2020-01-01T00:00:00+00:00"
QUERY = "orbitalsync telemetry"


def build_probe_fixture(repo, placeholder: str) -> dict[str, Any]:
    """Alice owns ``nb`` shared with Bob: visible sources whose ids differ in
    case and byte order, one Knowhow projection, Alice's confirmed Memory."""
    from app.models.schemas import NotebookCreate
    from app.services.sqlite_repository import reset_request_user, set_request_user

    alice = repo.create_user("a00130101", "password-12")
    bob = repo.create_user("b00130102", "password-12")
    token = set_request_user(alice)
    try:
        nb = repo.create_notebook(NotebookCreate(name="探针库")).id
        empty = repo.create_notebook(NotebookCreate(name="空库")).id
    finally:
        reset_request_user(token)
    repo._runtime.sharing.add_member(nb, bob.id)
    ids = {"repo": repo, "ph": placeholder, "nb": nb, "empty": empty,
           "alice": alice.id, "bob": bob.id}
    for source_id in ("B", "a", "é", "Z", "src-10", "src-9"):
        insert_source(ids, source_id, "pdf")
    insert_source(ids, "src-knowhow", "knowhow")
    insert_memory(ids, alice.id, "mem-alice", "src-memory-alice")
    return ids


def insert_source(ids, source_id, source_type, memory_id="", notebook_id=None):
    ids["repo"]._runtime.source_store.insert_source(
        source_id=source_id, notebook_id=notebook_id or ids["nb"], title=source_id,
        source_type=source_type, status="active", parse_status="parsed",
        file_name="", file_path="", file_size=0, file_hash="",
        summary="", doc_type="", memory_id=memory_id,
    )


def insert_memory(ids, user_id, memory_id, source_id):
    ph = ids["ph"]
    with ids["repo"]._write() as db:
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,agent_profile_id,"
            "source_answer_id,origin,status,title,content_md,created_at,updated_at) "
            f"VALUES ({ph},{ph},{ph},NULL,NULL,'ask_answer','confirmed',"
            f"{ph},{ph},{ph},{ph})",
            (memory_id, ids["nb"], user_id, "m", "m", NOW, NOW),
        )
    insert_source(ids, source_id, "memory", memory_id=memory_id)


def delete_source(ids, source_id):
    ph = ids["ph"]
    with ids["repo"]._write() as db:
        db.execute(f"DELETE FROM sources WHERE id={ph}", (source_id,))


def _readers(ids):
    return ids["repo"]._runtime.ceiling_readers()


def _probe(ids) -> bool:
    return ids["repo"].retrieval.candidates._unsafe_source_scope_restricted(ids["nb"])


def assert_the_store_digest_is_the_python_digest(ids) -> None:
    sources = ids["repo"]._runtime.source_store
    for notebook_id in (ids["nb"], ids["empty"]):
        for owner in (ids["alice"], ids["bob"], ""):
            expected = [
                universe_digest(sources.all_visible_source_ids(notebook_id)),
                universe_digest(sources.hidden_source_ids(notebook_id, owner)),
            ]
            assert sources.all_visible_source_ids(
                notebook_id, digest_for_owner=owner,
            ) == expected, (notebook_id, owner)
    assert sources.all_visible_source_ids(ids["empty"], digest_for_owner="x") == ["", ""]
    # The visible half really spans ids that sort differently by case/bytes.
    assert universe_digest(["B", "a", "é", "Z"]) == universe_digest(["é", "Z", "a", "B"])
    assert universe_digest(["B", "a"]) != universe_digest(["B", "a", "Z"])


def _fresh_drift(ids, change) -> bool:
    """Freeze Alice's default ceiling, apply ``change``, probe once."""
    with default_ceiling_context(ids["nb"], ids["alice"], _readers(ids)):
        assert _probe(ids) is False, "no change yet: the freeze matches"
        change()
        return _probe(ids)


def assert_the_probe_sees_every_change(ids) -> None:
    ph = ids["ph"]
    assert _fresh_drift(ids, lambda: insert_source(ids, "added", "pdf")) is True
    assert _fresh_drift(ids, lambda: delete_source(ids, "added")) is True

    def replace_with_an_older_one():
        # Same count, and the newcomer is OLDER than every survivor: a
        # (count, max(created_at)) fingerprint would not move.
        delete_source(ids, "src-9")
        insert_source(ids, "src-8", "pdf")
        with ids["repo"]._write() as db:
            db.execute(
                f"UPDATE sources SET created_at={ph} WHERE id={ph}", (EARLIER, "src-8"),
            )

    assert _fresh_drift(ids, replace_with_an_older_one) is True
    assert _fresh_drift(
        ids, lambda: insert_memory(ids, ids["alice"], "mem-alice-2", "src-memory-alice-2"),
    ) is True, "the asker's own new Memory changes her hidden half"
    assert _fresh_drift(
        ids, lambda: insert_memory(ids, ids["bob"], "mem-bob-late", "src-memory-bob-late"),
    ) is False, "another member's Memory is not in the asker's universe"


def assert_one_probe_is_one_fingerprint_read_and_never_cached(ids, monkeypatch) -> None:
    sources = ids["repo"]._runtime.source_store
    calls: list[tuple] = []
    visible, hidden = sources.all_visible_source_ids, sources.hidden_source_ids

    def spy_visible(notebook_id, *args, **kwargs):
        calls.append(("visible", kwargs.get("digest_for_owner")))
        return visible(notebook_id, *args, **kwargs)

    def spy_hidden(*args, **kwargs):
        calls.append(("hidden", None))
        return hidden(*args, **kwargs)

    with default_ceiling_context(ids["nb"], ids["alice"], _readers(ids)):
        monkeypatch.setattr(sources, "all_visible_source_ids", spy_visible)
        monkeypatch.setattr(sources, "hidden_source_ids", spy_hidden)
        assert _probe(ids) is False
        assert calls == [("visible", ids["alice"])], (
            "one probe is one fingerprint read, never the two full sets"
        )
        insert_source(ids, "arrived-mid-run", "pdf")
        assert _probe(ids) is True, "a probe is never cached across calls"
        assert len(calls) == 2


def assert_the_ceiling_is_pushed_down_only_when_it_excludes_nothing(ids) -> None:
    nb = ids["nb"]
    with default_ceiling_context(nb, ids["alice"], _readers(ids)):
        scope = current_source_scope()
        assert run_ceiling_binds(scope, nb) is False
        assert scoped_allowed_source_ids(nb) is None, (
            "un-narrowed, undrifted, no other member's Memory: no list in SQL"
        )
    with memory_access_context(False), default_ceiling_context(
        nb, ids["alice"], _readers(ids),
    ):
        assert scoped_allowed_source_ids(nb) is not None, (
            "the asker's own Memory is withheld: the list binds"
        )
    narrowed = {"mode": "include", "source_ids": ["B"], "narrowed": True}
    with default_ceiling_context(nb, ids["alice"], _readers(ids), local_scope=narrowed):
        assert scoped_allowed_source_ids(nb) == ("B",)
    insert_memory(ids, ids["bob"], "mem-bob", "src-memory-bob")
    with default_ceiling_context(nb, ids["alice"], _readers(ids)):
        bound = scoped_allowed_source_ids(nb)
        assert bound is not None and "src-memory-bob" not in bound, (
            "another member's Memory in the notebook: the list binds"
        )


def assert_an_unbound_read_is_verified_and_rerun_bound(ids) -> None:
    repo = ids["repo"]
    nb = ids["nb"]
    _chunked_source(ids, "chunked-before", f"{QUERY} before the freeze")
    candidates = repo.retrieval.candidates
    with default_ceiling_context(nb, ids["alice"], _readers(ids)):
        scope = current_source_scope()
        assert scoped_allowed_source_ids(nb) is None
        first, _ids, _matrix = candidates._retrieve_chunks(nb, QUERY)
        assert {chunk.source_id for chunk in first} == {"chunked-before"}
        # A source finishes after the run's verdict was taken.
        _chunked_source(ids, "chunked-after", f"{QUERY} after the freeze")
        second, _ids, _matrix = candidates._retrieve_chunks(nb, QUERY)
        assert {chunk.source_id for chunk in second} == {"chunked-before"}, (
            "a source outside the freeze never comes back from an unbound read"
        )
        assert run_ceiling_binds(scope, nb) is True, "the verdict flipped"
        assert scoped_allowed_source_ids(nb) is not None


def _chunked_source(ids, source_id, text):
    from app.repositories.ports import ChunkWrite, SourceElementWrite

    repo = ids["repo"]
    runtime = repo._runtime
    insert_source(ids, source_id, "markdown")
    with repo._write() as db:
        runtime.source_store.replace_elements(
            db, source_id,
            [SourceElementWrite(f"el-{source_id}", "paragraph", "p1", text, {})],
            created_at=NOW,
        )
        runtime.chunk_store.insert_rows(
            db, ids["nb"], source_id,
            [ChunkWrite(f"chunk-{source_id}", text, "1", (f"el-{source_id}",))],
            created_at=NOW,
        )
    repo.backfill_chunk_fts(ids["nb"])


@pytest.fixture
def sqlite_ids(tmp_path, monkeypatch):
    from app.core.config import Settings
    from app.services.embedding import FakeEmbedder
    from app.services.sqlite_repository import SQLiteRepository
    from tests.model_testkit import bind_all_embedding_clients

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'probe.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings())
    bind_all_embedding_clients(repo, FakeEmbedder(dim=16))
    return build_probe_fixture(repo, "?")


def test_store_digest_equals_python_digest(sqlite_ids):
    assert_the_store_digest_is_the_python_digest(sqlite_ids)


def test_probe_reports_every_change_and_only_the_askers(sqlite_ids):
    assert_the_probe_sees_every_change(sqlite_ids)


def test_probe_is_one_fingerprint_read_and_never_cached(sqlite_ids, monkeypatch):
    assert_one_probe_is_one_fingerprint_read_and_never_cached(sqlite_ids, monkeypatch)


def test_ceiling_is_pushed_down_only_when_it_excludes_nothing(sqlite_ids):
    assert_the_ceiling_is_pushed_down_only_when_it_excludes_nothing(sqlite_ids)


def test_unbound_read_is_verified_and_rerun_bound(sqlite_ids):
    assert_an_unbound_read_is_verified_and_rerun_bound(sqlite_ids)


def test_sqlite_digest_statement_reads_by_notebook_index(sqlite_ids):
    """SQLite pin: both halves of the fingerprint start from a ``sources``
    index on ``notebook_id``; neither scans the table."""
    from app.repositories.sqlite.source_store import _UNIVERSE_DIGEST_SQL

    repo = sqlite_ids["repo"]
    with repo._connect() as db:
        plan = "\n".join(
            str(row[-1]) for row in db.execute(
                "EXPLAIN QUERY PLAN " + _UNIVERSE_DIGEST_SQL,
                (sqlite_ids["nb"], sqlite_ids["nb"], sqlite_ids["alice"]),
            ).fetchall()
        )
    lines = plan.splitlines()
    assert not [line for line in lines if line.startswith(("SCAN s", "SCAN sources"))], plan
    assert any(
        line.startswith("SEARCH sources USING") and "(notebook_id=?" in line for line in lines
    ), plan
    assert any(
        line.startswith("SEARCH s USING INDEX idx_sources_nb_hidden_type") for line in lines
    ), plan
