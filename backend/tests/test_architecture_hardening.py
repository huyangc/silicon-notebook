"""Cross-cutting architecture invariants introduced by the hardening pass."""
from __future__ import annotations

import contextvars
import threading
import time

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository


def _settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 't.db'}",
        storage_dir=str(tmp_path / "storage"),
        event_log_enabled=False,
        llm_log_enabled=False,
        auth_optional=True,
    )


def test_settings_accept_field_names_even_when_fields_have_validation_aliases(tmp_path):
    settings = _settings(tmp_path)
    assert settings.sqlite_path == str(tmp_path / "t.db")
    assert settings.storage_dir == str(tmp_path / "storage")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgres://user:pass@db.example/db", "postgresql://user:pass@db.example/db"),
        ("postgresql://user:pass@db.example/db?sslmode=require", "postgresql://user:pass@db.example/db?sslmode=require"),
    ],
)
def test_postgresql_database_urls_are_accepted_and_legacy_scheme_is_normalized(url, expected):
    assert Settings(database_url=url).database_url == expected


def test_mysql_database_url_fails_closed_without_leaking_credentials():
    raw = "mysql://redacted-user:redacted-password@db.example/db?access_token=redacted-token#fragment"

    with pytest.raises(ValidationError) as captured:
        Settings(database_url=raw)

    diagnostics = (str(captured.value), repr(captured.value.errors()), captured.value.json())
    assert "unsupported database URL scheme: mysql" in diagnostics[0]
    assert "mysql://db.example" in diagnostics[0]
    for diagnostic in diagnostics:
        assert raw not in diagnostic
        assert "redacted-user" not in diagnostic
        assert "redacted-password" not in diagnostic
        assert "access_token=redacted-token" not in diagnostic
        assert "#fragment" not in diagnostic


def test_multi_query_retrieval_copies_context_per_worker(tmp_path, monkeypatch):
    repo = SQLiteRepository(_settings(tmp_path))
    owner = contextvars.ContextVar("owner", default="missing")
    token = owner.set("request-owner")
    calls: list[tuple[str, str]] = []
    lock = threading.Lock()

    def fake_retrieve(notebook_id, query):
        with lock:
            calls.append((query, owner.get()))
        # Keep each Context entered long enough for concurrent reuse to fail.
        time.sleep(0.03)
        return [], [], None

    monkeypatch.setattr(repo.retrieval.candidates, "_retrieve_chunks", fake_retrieve)
    try:
        repo.retrieval.candidates._retrieve_chunks_multi("nb", ["q1", "q2", "q3", "q4"])
    finally:
        owner.reset(token)

    assert sorted(calls) == [
        ("q1", "request-owner"),
        ("q2", "request-owner"),
        ("q3", "request-owner"),
        ("q4", "request-owner"),
    ]


def test_scale_graph_excludes_rejected_relations(tmp_path):
    repo = SQLiteRepository(_settings(tmp_path))
    nb = repo.create_notebook(NotebookCreate(name="graph"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "claim", "payload": {"name": "A"}, "evidence": []},
        {"local_id": "b", "object_type": "claim", "payload": {"name": "B"}, "evidence": []},
    ], [{
        "source_local_id": "a", "target_local_id": "b", "edge_type": "supports", "evidence": [],
    }])
    with repo._connect() as db:
        rel = db.execute(
            "SELECT id, source_object_id, target_object_id FROM knowledge_relations WHERE notebook_id=?",
            (nb.id,),
        ).fetchone()
    repo.set_edge_review(nb.id, rel["id"], "rejected")

    _nodes, edges, _chunks, _kg_nodes, _counts = repo._gather_kg_graph(nb.id)

    assert (rel["source_object_id"], rel["target_object_id"], 1.0) not in edges
    assert (rel["target_object_id"], rel["source_object_id"], 1.0) not in edges

    graph, key_to_idx, _chunk_map = repo.retrieval.graph._ppr_graph(nb.id)
    assert not graph.has_edge(
        key_to_idx[rel["source_object_id"]], key_to_idx[rel["target_object_id"]]
    )


def test_federated_large_guard_includes_base_notebooks(tmp_path, monkeypatch):
    repo = SQLiteRepository(_settings(tmp_path))
    personal = repo.create_notebook(NotebookCreate(name="personal"))
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    repo.replace_notebook_bases(personal.id, [base.id], "user-local")

    monkeypatch.setattr(
        repo.retrieval.candidates,
        "notebook_copy_stats",
        lambda notebook_id: {"copyable": notebook_id != base.id},
    )

    assert repo._federated_graph_is_large(personal.id) is True


def test_session_resolution_does_not_write_on_every_request(tmp_path):
    repo = SQLiteRepository(_settings(tmp_path))
    token = repo.create_session("user-local")
    with repo._connect() as db:
        before = db.execute(
            "SELECT last_seen_at, expires_at FROM auth_sessions WHERE token=?", (token,)
        ).fetchone()

    assert repo.resolve_session(token).id == "user-local"

    with repo._connect() as db:
        after = db.execute(
            "SELECT last_seen_at, expires_at FROM auth_sessions WHERE token=?", (token,)
        ).fetchone()
    assert tuple(after) == tuple(before)


def test_facade_composition_is_flat_and_static(tmp_path):
    """The compatibility wrapper has one neutral facade base and no mixins."""
    from app.services.repository_facade import RepositoryFacade

    assert SQLiteRepository.__mro__ == (
        SQLiteRepository,
        RepositoryFacade,
        object,
    )
    assert "__getattr__" not in SQLiteRepository.__dict__
    assert "__getattribute__" not in SQLiteRepository.__dict__

    repo = SQLiteRepository(_settings(tmp_path))
    assert repo._runtime.settings is repo.settings
