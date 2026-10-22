"""Memory recall skips the query embed when no candidate can use it (SQLite;
scenarios in ``memory_embed_skip_cases``, shared with
``postgres/test_memory_embed_skip_pg.py``)."""
from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.models.memory import MemoryRecord
from app.services.embedding import FakeEmbedder
from app.services.memory_retrieval import MemoryRetriever
from app.services.sqlite_repository import SQLiteRepository
from tests import memory_embed_skip_cases as cases


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings(_env_file=None, model_services_config=""))
    try:
        yield cases.build(repo)
    finally:
        repo.close()


def test_no_memory_never_embeds(world):
    cases.no_memory_never_embeds(world)


def test_memory_without_vectors_never_embeds_and_scores_alike(world):
    cases.memory_without_vectors_never_embeds_and_scores_alike(world)


def test_memory_with_a_vector_embeds_once(world):
    cases.memory_with_a_vector_embeds_once(world)


def test_closed_channel_is_unchanged(world):
    cases.closed_channel_is_unchanged(world)


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def memory_retrieval_rows(self, *args, **kwargs):
        return self.rows


def _record(memory_id: str, content: str) -> MemoryRecord:
    return MemoryRecord(
        id=memory_id, notebook_id="nb", created_by="u", origin="external_agent",
        status="confirmed", title=memory_id, content_md=content, tags=[],
        created_at="t", updated_at="t",
    )


@pytest.mark.parametrize(
    ("stored", "embeds"),
    [
        (None, 0),
        (b"", 0),
        ("", 0),
        # A legacy JSON row that decodes to an EMPTY vector is not None: it
        # flips ``has_vector`` and so the fusion denominator, so the query must
        # still be embedded -- the skip tests ``is not None``, not truthiness.
        (json.dumps([]), 1),
        (json.dumps([0.5] * cases.EMBED_DIM), 1),
    ],
)
def test_skip_follows_the_decoded_vector_exactly(stored, embeds):
    rows = [{"record": _record("m-1", "timing closure signed off"), "vector": stored},
            {"record": _record("m-2", "timing closure review"), "vector": None}]
    embedder = cases.CountingEmbedder()
    retriever = MemoryRetriever(_Rows(rows), embedder)
    new = cases.as_bytes(retriever.notebook_memory_hits("u", "nb", cases.QUERY, 8))
    old = cases.as_bytes(cases.legacy_hits(
        retriever, FakeEmbedder(dim=cases.EMBED_DIM), "u", "nb", cases.QUERY,
        ("confirmed",), 8))
    assert len(embedder.queries) == embeds
    assert new == old and new
