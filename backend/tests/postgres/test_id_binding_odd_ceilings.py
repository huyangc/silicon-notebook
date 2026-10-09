"""One membership semantics on both backends, for the ids nobody expects.

The binding layer never changes membership: the empty string, a blank id, an
id containing PostgreSQL's text-form separator, duplicates and a reversed
list all select exactly the rows a Python reference selects -- and the SQLite
and PostgreSQL stores return the same rows for the same ceiling, in include
and in exclude mode.  Both databases are seeded with identical rows.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.core.config import Settings
from app.domain.repository import RepositoryCompatibilitySeams
from app.repositories.postgres.chunk_store import ChunkStore as PgChunkStore
from app.repositories.postgres.knowledge_store import (
    KnowledgeStore as PgKnowledgeStore,
)
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.sqlite.chunk_store import ChunkStore as SqChunkStore
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.knowledge_store import (
    KnowledgeStore as SqKnowledgeStore,
)

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_id_binding_odd_ceilings"),
]

NB = "nb-odd"
USER = "u-odd"
NOW = "2026-09-29T00:00:00+00:00"
SOURCES = ["s-a", "s-b", "", "   ", "sep\x1finside", " lead", "s-c", "sep", "inside"]
CHUNKS = [
    (f"c-{index}-{part}", source, f"wafer etching n{index} p{part}")
    for index, source in enumerate(SOURCES) for part in (0, 1)
]
CEILINGS = {
    "empty_id": [""],
    "blank_id": ["   "],
    "separator": ["sep\x1finside"],
    "odd_mix": ["", "   ", "sep\x1finside"],
    "reversed_dups": ["s-c", "", "s-a", "   ", "s-c", "sep\x1finside", "", "s-a"],
    "halves_of_the_separator_id": ["sep", "inside"],
    "none_present": ["missing", "sep\x1fmissing"],
}


def _seams() -> RepositoryCompatibilitySeams:
    return RepositoryCompatibilitySeams(
        new_id=lambda prefix: f"{prefix}-odd", now=lambda: NOW,
        copy_chunk_size=lambda: 100, remap_json_ids=lambda value, _map: value,
        in_chunk_size=lambda: 900,
    )


@pytest.fixture
def backends(postgres_database, tmp_path, _sqlite_schema_template):
    assert PostgresMigrator(postgres_database).migrate() == 70
    with postgres_database.write() as db:
        db.execute(
            "INSERT INTO users(id,email,display_name,role,created_at,updated_at) "
            "VALUES (%s,'odd@example.test','Odd','admin',%s,%s)", (USER, NOW, NOW),
        )
        db.execute(
            "INSERT INTO notebooks(id,name,created_by,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s)", (NB, NB, USER, NOW, NOW),
        )
        for source in SOURCES:
            db.execute(
                "INSERT INTO sources(id,notebook_id,title,source_type,status,"
                "parse_status,created_at,updated_at) "
                "VALUES (%s,%s,%s,'file','ready','ready',%s,%s)",
                (source, NB, f"T {source!r}", NOW, NOW),
            )
        for ordinal, (chunk, source, text) in enumerate(CHUNKS, start=1):
            db.execute(
                "INSERT INTO chunks(id,notebook_id,source_id,text,ordinal,"
                "element_ids,created_at) VALUES (%s,%s,%s,%s,%s,'[]',%s)",
                (chunk, NB, source, text, ordinal, NOW),
            )
            db.execute(
                "INSERT INTO chunk_questions(id,chunk_id,notebook_id,source_id,"
                "question,vector,created_at) VALUES (%s,%s,%s,%s,'q',%s,%s)",
                (f"q-{chunk}", chunk, NB, source, b"\0" * 4, NOW),
            )
    shutil.copyfile(_sqlite_schema_template, tmp_path / "odd.db")
    sqlite_db = SqliteDatabase(
        Settings(database_url=f"sqlite:///{tmp_path / 'odd.db'}"), tmp_path,
    )
    with sqlite_db.write() as db:
        db.execute(
            "INSERT INTO users(id,email,display_name,role,created_at,updated_at) "
            "VALUES (?,'odd@example.test','Odd','admin',?,?)", (USER, NOW, NOW),
        )
        db.execute(
            "INSERT INTO notebooks(id,name,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?)", (NB, NB, USER, NOW, NOW),
        )
        db.executemany(
            "INSERT INTO sources(id,notebook_id,title,source_type,status,"
            "parse_status,created_at,updated_at) "
            "VALUES (?,?,?,'file','ready','ready',?,?)",
            [(source, NB, f"T {source!r}", NOW, NOW) for source in SOURCES],
        )
        for chunk, source, text in CHUNKS:
            db.execute(
                "INSERT INTO chunks(id,notebook_id,source_id,text,created_at) "
                "VALUES (?,?,?,?,?)", (chunk, NB, source, text, NOW),
            )
            db.execute(
                "INSERT INTO chunks_fts(chunk_id,notebook_id,text) VALUES (?,?,?)",
                (chunk, NB, text),
            )
            db.execute(
                "INSERT INTO chunk_questions(id,chunk_id,notebook_id,source_id,"
                "question,vector,created_at) VALUES (?,?,?,?,'q',?,?)",
                (f"q-{chunk}", chunk, NB, source, b"\0" * 4, NOW),
            )
    yield postgres_database, sqlite_db
    sqlite_db.close_local()


def _reference(ceiling, *, include: bool) -> list[str]:
    allowed = set(ceiling)
    return sorted(
        chunk for chunk, source, _text in CHUNKS
        if (source in allowed) == include
    )


@pytest.mark.parametrize("label", list(CEILINGS))
def test_both_backends_select_exactly_the_listed_ids(backends, label):
    pg, sq = backends
    ceiling = CEILINGS[label]
    candidates = [chunk for chunk, _source, _text in CHUNKS]
    results = {}
    with pg.connect() as pg_db, sq.connect() as sq_db:
        for name, store, db in (("pg", PgChunkStore, pg_db), ("sq", SqChunkStore, sq_db)):
            results[name] = {
                mode: sorted(
                    row["id"] for row in store.retrieval_contribution_rows(
                        db, NB, candidates, actor_id=USER, source_mode=mode,
                        source_ids=ceiling,
                    )
                )
                for mode in ("include", "exclude")
            }
            results[name]["ids"] = sorted(
                row["id"] for row in store.ids_for_sources(db, NB, ceiling)
            )
            results[name]["presence"] = [
                row["source_id"] for row in store.ids_for_sources(
                    db, NB, ceiling, presence_only=True,
                )
            ]
        results["pg"]["fts"] = sorted(
            hit["chunk_id"] for hit in PgKnowledgeStore(pg, _seams()).chunk_fts_search(
                pg_db, NB, "wafer etching", k=100, allowed_source_ids=ceiling,
            )
        )
        results["sq"]["fts"] = sorted(
            hit["chunk_id"] for hit in SqKnowledgeStore.chunk_fts_search(
                sq_db, NB, "wafer etching", k=100, allowed_source_ids=ceiling,
            )
        )
    for name in ("pg", "sq"):
        results[name]["questions"] = [
            row["id"] for row in (PgChunkStore(pg) if name == "pg" else SqChunkStore(sq))
            .question_index_rows(NB, actor_id=USER, allowed_source_ids=ceiling, limit=100)
        ]
    included = _reference(ceiling, include=True)
    expected = {
        "include": included,
        "exclude": _reference(ceiling, include=False),
        "ids": included,
        "presence": [s for s in dict.fromkeys(ceiling) if s in set(SOURCES)],
        "fts": included,
        "questions": sorted(f"q-{chunk}" for chunk in included),
    }
    assert results["pg"] == expected, label
    assert results["sq"] == expected, label
