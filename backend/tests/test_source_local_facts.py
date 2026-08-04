from __future__ import annotations

import json

import pytest


def _repo(tmp_path, monkeypatch):
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'facts.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    repo = SQLiteRepository(Settings(_env_file=None))
    lifecycle = repo._runtime.knowledge_lifecycle
    monkeypatch.setattr(lifecycle, "_embed_objects_batch", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(lifecycle, "_embed_relations_batch", lambda *_args, **_kwargs: None)
    return repo


def _seed_source(repo, notebook_id: str, source_id: str, element_id: str) -> None:
    store = repo._runtime.source_store
    store.insert_source(
        source_id=source_id,
        notebook_id=notebook_id,
        title=source_id,
        source_type="markdown",
        status="extracted",
        parse_status="extracted",
        file_name=f"{source_id}.md",
        file_path=f"/tmp/{source_id}.md",
        file_size=1,
        file_hash="",
        summary="",
        doc_type="",
    )
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO source_elements "
            "(id, source_id, element_type, location_label, text, metadata, created_at) "
            "VALUES (?, ?, 'paragraph', 'L1', 'grounded text', '{}', ?)",
            (element_id, source_id, "2026-08-04T00:00:00+00:00"),
        )


def _begin(repo, notebook_id: str, source_id: str, run_id: str, stamp: str) -> None:
    with repo._runtime.database.write() as db:
        repo._runtime.knowledge.begin_extraction_run(
            db,
            source_id,
            notebook_id,
            run_id,
            stamp,
            preserve_existing=True,
        )


def _object(source_id: str, element_id: str, name: str = "Fact") -> dict:
    return {
        "local_id": f"local-{name}",
        "object_type": "claim",
        "payload": {"name": name, "statement": "grounded text"},
        "evidence": [
            {
                "source_id": source_id,
                "element_id": element_id,
                "quoted_span": "grounded text",
            }
        ],
    }


def test_ingestion_generation_publishes_source_fact_and_element_binding(
    tmp_path, monkeypatch
):
    from app.models.schemas import NotebookCreate

    repo = _repo(tmp_path, monkeypatch)
    notebook_id = repo.create_notebook(NotebookCreate(name="facts")).id
    _seed_source(repo, notebook_id, "src-a", "el-a")
    _begin(repo, notebook_id, "src-a", "run-a", "2026-08-04T00:00:01+00:00")

    assert repo._runtime.knowledge_lifecycle.store_kg(
        notebook_id,
        "src-a",
        [_object("src-a", "el-a")],
        [],
        source_generation="run-a",
    ) == (1, 0)

    with repo._runtime.database.connect() as db:
        fact = db.execute("SELECT * FROM knowledge_source_facts").fetchone()
        binding = db.execute("SELECT * FROM knowledge_source_fact_elements").fetchone()
    assert fact["source_id"] == "src-a"
    assert fact["source_generation"] == "run-a"
    assert fact["global_object_id"] != fact["id"]
    assert fact["local_object_id"] == "local-Fact"
    assert json.loads(fact["payload"])["name"] == "Fact"
    assert binding["fact_id"] == fact["id"]
    assert binding["element_id"] == "el-a"

    # A same-generation retry derives the same fact identity from the source,
    # generation and extractor-local id, so provenance stays idempotent even
    # though the legacy global-object writer remains append-oriented.
    repo._runtime.knowledge_lifecycle.store_kg(
        notebook_id,
        "src-a",
        [_object("src-a", "el-a")],
        [],
        source_generation="run-a",
    )
    with repo._runtime.database.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM knowledge_source_facts").fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM knowledge_source_fact_elements"
        ).fetchone()[0] == 1


def test_stale_generation_and_cross_source_element_fail_before_global_publish(
    tmp_path, monkeypatch
):
    from app.models.schemas import NotebookCreate

    repo = _repo(tmp_path, monkeypatch)
    notebook_id = repo.create_notebook(NotebookCreate(name="facts")).id
    _seed_source(repo, notebook_id, "src-a", "el-a")
    _seed_source(repo, notebook_id, "src-b", "el-b")
    _begin(repo, notebook_id, "src-a", "run-old", "2026-08-04T00:00:01+00:00")
    _begin(repo, notebook_id, "src-a", "run-new", "2026-08-04T00:00:02+00:00")

    with pytest.raises(RuntimeError, match="stale source fact generation"):
        repo._runtime.knowledge_lifecycle.store_kg(
            notebook_id,
            "src-a",
            [_object("src-a", "el-a", "stale")],
            [],
            source_generation="run-old",
        )
    with pytest.raises(RuntimeError, match="crosses source generation boundary"):
        repo._runtime.knowledge_lifecycle.store_kg(
            notebook_id,
            "src-a",
            [_object("src-a", "el-b", "foreign-element")],
            [],
            source_generation="run-new",
        )

    with repo._runtime.database.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM knowledge_objects").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM knowledge_source_facts").fetchone()[0] == 0


def test_ungrounded_global_object_is_not_promoted_to_a_source_fact(tmp_path, monkeypatch):
    from app.models.schemas import NotebookCreate

    repo = _repo(tmp_path, monkeypatch)
    notebook_id = repo.create_notebook(NotebookCreate(name="facts")).id
    _seed_source(repo, notebook_id, "src-a", "el-a")
    _begin(repo, notebook_id, "src-a", "run-a", "2026-08-04T00:00:01+00:00")
    obj = _object("src-a", "el-a", "ungrounded")
    obj["evidence"] = []

    assert repo._runtime.knowledge_lifecycle.store_kg(
        notebook_id, "src-a", [obj], [], source_generation="run-a"
    ) == (1, 0)
    with repo._runtime.database.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM knowledge_objects").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM knowledge_source_facts").fetchone()[0] == 0


def test_replacement_swaps_fact_generation_and_global_delete_does_not_erase_fact(
    tmp_path, monkeypatch
):
    from app.models.schemas import NotebookCreate

    repo = _repo(tmp_path, monkeypatch)
    notebook_id = repo.create_notebook(NotebookCreate(name="facts")).id
    _seed_source(repo, notebook_id, "src-a", "el-a")
    _begin(repo, notebook_id, "src-a", "run-1", "2026-08-04T00:00:01+00:00")
    repo._runtime.knowledge_lifecycle.store_kg(
        notebook_id,
        "src-a",
        [_object("src-a", "el-a", "first")],
        [],
        source_generation="run-1",
    )

    _begin(repo, notebook_id, "src-a", "run-2", "2026-08-04T00:00:02+00:00")
    repo._runtime.knowledge_lifecycle.store_kg(
        notebook_id,
        "src-a",
        [_object("src-a", "el-a", "second")],
        [],
        replace_source=True,
        source_generation="run-2",
    )

    with repo._runtime.database.write() as db:
        facts = db.execute(
            "SELECT id, source_generation, global_object_id, projection_origin "
            "FROM knowledge_source_facts"
        ).fetchall()
        assert [row["source_generation"] for row in facts] == ["run-2"]
        assert [row["projection_origin"] for row in facts] == ["live"]
        deleted = db.execute(
            "DELETE FROM knowledge_objects WHERE id=?", (facts[0]["global_object_id"],)
        )
        assert deleted.rowcount == 1
        remaining = db.execute(
            "SELECT COUNT(*) FROM knowledge_source_facts WHERE id=?", (facts[0]["id"],)
        ).fetchone()[0]
    assert remaining == 1
