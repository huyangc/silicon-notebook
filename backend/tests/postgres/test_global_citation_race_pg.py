"""PR-D PostgreSQL twins: the in-SQL digest equals the shared Python digest, and
the federated passage producer racing the terminal read reaches the same
verdicts as on SQLite (``tests/test_global_ask_citation_check.py``).

PostgreSQL is the primary environment, so these are the authoritative half:
element bodies are hashed IN SQL here and never cross the wire, which is
exactly why a twin has to pin that the two spellings cannot drift.
"""
from __future__ import annotations

import hashlib

import pytest

from app.domain.evidence_fingerprint import element_text_sha
from app.repositories.postgres.migrator import PostgresMigrator
from tests.citation_check_testkit import (
    NOW, RACE_EXPECTATIONS, TWIN_TEXTS, passage_race, seed_source,
)


pytestmark = pytest.mark.postgres_integration

_NOTEBOOK = "nb-cite"


@pytest.fixture
def pg_sources(postgres_database):
    from app.repositories.postgres.source_store import SourceStore

    PostgresMigrator(postgres_database).migrate()
    with postgres_database.write() as db:
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            ("cite-owner", "cite@example.test", "Cite", "user", "active", NOW, NOW),
        )
        db.execute(
            "INSERT INTO notebooks(id,name,created_by,created_at,updated_at) "
            "VALUES(%s,%s,%s,%s,%s)",
            (_NOTEBOOK, "Citations", "cite-owner", NOW, NOW),
        )
    sources = object.__new__(SourceStore)
    sources.database = postgres_database
    return sources


def _sqlite_prints(tmp_path, texts):
    """The SQLite store's prints for the same rows: the other half of the twin."""
    from app.core.config import Settings
    from app.models.notebooks import NotebookCreate
    from app.repositories.sqlite.source_store import SourceStore
    from app.services.sqlite_repository import SQLiteRepository

    repo = SQLiteRepository(Settings(
        _env_file=None, database_url=f"sqlite:///{tmp_path / 'twin.db'}",
        storage_dir=str(tmp_path / "storage"),
    ))
    try:
        notebook = repo.create_notebook(NotebookCreate(name="twin"))
        database = repo._runtime.source_store.database
        with database.write() as db:
            seed_source(db, "?", notebook_id=notebook.id, source_id="src-twin",
                        elements=texts)
        sources = object.__new__(SourceStore)
        sources.database = database
        return sources.evidence_fingerprints(list(texts))
    finally:
        repo.close()


def test_postgres_prints_equal_the_shared_digest_and_the_sqlite_twin(pg_sources, tmp_path):
    from app.services.chunk_federation import _text_sha

    elements = {f"el-twin-{name}": text for name, text in TWIN_TEXTS.items()}
    with pg_sources.database.write() as db:
        seed_source(db, "%s", notebook_id=_NOTEBOOK, source_id="src-twin",
                    elements=elements)
        for name, text in TWIN_TEXTS.items():
            seed_source(db, "%s", notebook_id=_NOTEBOOK, source_id=f"src-p-{name}",
                        elements={}, chunk=(f"chunk-{name}", [f"el-twin-{name}"], text))

    prints = pg_sources.evidence_fingerprints(list(elements))
    passages = pg_sources.passage_evidence_snapshot(
        [f"chunk-{name}" for name in TWIN_TEXTS]
    )
    assert prints == _sqlite_prints(tmp_path, elements)
    for name, text in TWIN_TEXTS.items():
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert element_text_sha(text) == expected == _text_sha(text)
        assert prints[f"el-twin-{name}"] == ("src-twin", expected)
        assert passages[f"chunk-{name}"]["text_sha"] == expected
        assert passages[f"chunk-{name}"]["elements"][f"el-twin-{name}"] == (
            "src-twin", expected,
        )
    assert prints["el-twin-combining"] != prints["el-twin-precomposed"]


def test_the_sql_digest_ignores_a_non_utf8_server_encoding(postgres_non_utf_database):
    """The application refuses to migrate a non-UTF8 database, so there is no
    ``source_elements`` table to read here; what can drift is the EXPRESSION.
    ``convert_to(text,'UTF8')`` hashes the UTF-8 bytes whatever the server
    encoding, so a stored row hashes exactly as the Python helper does."""
    with postgres_non_utf_database.connect() as conn:
        conn.execute("SET client_encoding TO 'UTF8'")
        encoding = conn.execute(
            "SELECT current_setting('server_encoding') AS value"
        ).fetchone()["value"]
        assert encoding != "UTF8"
        conn.execute("CREATE TEMP TABLE twin_texts(id text, text text)")
        for name, text in TWIN_TEXTS.items():
            conn.execute("INSERT INTO twin_texts VALUES(%s,%s)", (name, text))
        rows = conn.execute(
            "SELECT id,encode(sha256(convert_to(text,'UTF8')),'hex') AS evidence_hash "
            "FROM twin_texts"
        ).fetchall()
    assert {row["id"]: row["evidence_hash"] for row in rows} == {
        name: element_text_sha(text) for name, text in TWIN_TEXTS.items()
    }


@pytest.mark.parametrize("mutation", sorted(RACE_EXPECTATIONS))
def test_postgres_federated_passage_race_before_the_terminal_read(pg_sources, mutation):
    response = passage_race(pg_sources, pg_sources.database, "%s", _NOTEBOOK, mutation)

    expected = RACE_EXPECTATIONS[mutation]
    assert response.answer == "答案"
    assert response.citations[0].verification == expected
    if expected is None:
        assert response.citation_check is None
        assert response.evidence_level == "grounded"
    else:
        assert getattr(response.citation_check, expected) == 1
        assert response.evidence_level == "overview"
        assert response.grounded is False
