"""The synthetic paper-topics corpus (design 2026-10-28 §8, layer 1) for the
``find_sources`` / ``extract_from_sources`` / paper-table tests.

``tests/fixtures/paper_topics/papers.json`` holds 40 papers written for this
repository (title, abstract, method, a results table with numbers, a one-line
conclusion; each well under 3 KB): five on each of six topics, six cross-topic
papers and four unrelated ones.  ``labels.json`` maps each topic to its query,
its literal terms and the ``strong`` / ``related`` paper keys.

``TopicEmbedder`` is a deterministic bag-of-words embedder: every content word
hashes to one signed dimension, so cosine similarity follows word overlap -- a
stand-in for a semantic model that keeps topics apart without any network.
Tests built on it assert tiers and recall, never relevance values or threshold
numbers (those belong to the calibration script, ``app.eval.find_sources_calibrate``).

``seed_paper_topics`` writes the corpus into a notebook through the runtime's
ports (sources, elements, chunks + vectors, chunk FTS, paper metadata), so the
same loader serves the SQLite and the PostgreSQL suites.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Iterator, List, Mapping, Optional

from app.repositories.ports import SourceElementWrite

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "paper_topics"
CREATED_AT = "2026-01-01T00:00:00+00:00"
TOPIC_EMBED_DIM = 256

_WORD_RE = re.compile(r"[a-z][a-z0-9]+|[0-9]+(?:\.[0-9]+)?")
_STOPWORDS = frozenset(
    "a an and are as at be by can each for from has in into is it its of on or "
    "our so that the their them then this to we with without which while than "
    "use used using per all any new one two more most only over under".split()
)


@lru_cache(maxsize=1)
def load_papers() -> tuple[Mapping[str, Any], ...]:
    papers = json.loads((FIXTURE_DIR / "papers.json").read_text(encoding="utf-8"))
    return tuple(papers)


@lru_cache(maxsize=1)
def load_labels() -> Mapping[str, Any]:
    return json.loads((FIXTURE_DIR / "labels.json").read_text(encoding="utf-8"))["topics"]


def paper(key: str) -> Mapping[str, Any]:
    return next(item for item in load_papers() if item["key"] == key)


def content_words(text: str) -> List[str]:
    words = []
    for word in _WORD_RE.findall((text or "").lower()):
        if word in _STOPWORDS or word[0].isdigit():
            continue
        if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        words.append(word)
    return words


class TopicEmbedder:
    """Deterministic bag-of-words vectors (``embed_texts`` / ``embed_query``)."""

    def __init__(self, dim: int = TOPIC_EMBED_DIM) -> None:
        self.dim = dim

    def _vec(self, text: str) -> List[float]:
        out = [0.0] * self.dim
        for word in content_words(text) or ["<empty>"]:
            digest = hashlib.blake2b(word.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self.dim
            out[index] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(value * value for value in out)) or 1.0
        return [value / norm for value in out]

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        return [self._vec(text) for text in texts]

    def embed_query(self, text: str) -> List[float]:
        return self._vec(text)


def paper_elements(item: Mapping[str, Any], source_id: str) -> list[SourceElementWrite]:
    parts = [
        ("title", item["title"]),
        ("text", f"Abstract. {item['abstract']}"),
        ("text", f"Method. {item['method']}"),
        ("table", item["table"]),
        ("text", f"Conclusion. {item['conclusion']}"),
    ]
    return [
        SourceElementWrite(
            id=f"el-{source_id}-{index:02d}", element_type=kind,
            location_label=f"p{index + 1}", text=text, metadata={},
        )
        for index, (kind, text) in enumerate(parts)
    ]


def source_id_for(prefix: str, key: str) -> str:
    return f"{prefix}{key}"


def seed_paper_topics(
    repo: Any,
    notebook_id: str,
    *,
    keys: Optional[Iterable[str]] = None,
    prefix: str = "pt-",
    with_meta: bool = True,
    embed: bool = True,
) -> dict[str, str]:
    """Write the corpus (or ``keys`` of it) into ``notebook_id``; returns
    ``{paper key: source_id}``.  ``embed=False`` leaves the chunks without
    vectors (the extraction service's FTS fallback); ``with_meta=False`` leaves
    the papers without a paper-meta row (lazily extracted, ``meta_pending``).
    The caller binds the embedding clients (``TopicEmbedder``) first."""
    runtime = repo._runtime
    store = runtime.source_store
    wanted = None if keys is None else set(keys)
    seeded: dict[str, str] = {}
    for item in load_papers():
        if wanted is not None and item["key"] not in wanted:
            continue
        source_id = source_id_for(prefix, item["key"])
        store.insert_source(
            source_id=source_id, notebook_id=notebook_id, title=item["title"],
            source_type="document", status="parsed", parse_status="parsed",
            file_name=f"{item['key']}.pdf", file_path=f"/nonexistent/{source_id}.pdf",
            file_size=0, file_hash=f"h-{source_id}", summary=item["abstract"],
            doc_type="academic_paper",
        )
        with repo._write() as db:
            store.replace_elements(
                db, source_id, paper_elements(item, source_id), created_at=CREATED_AT,
            )
        if embed:
            runtime.source_chunking.chunk_and_embed_source(source_id)
        else:
            runtime.source_chunking.build_chunks_for_source(source_id)
        if with_meta:
            store.upsert_paper_meta(source_id, notebook_id, {
                "is_paper": True, "paper_title": item["title"], "venue": item["venue"],
                "pub_year": item["year"], "doi": None, "keywords": list(item["keywords"]),
                "authors": [
                    {"position": index, "name": name, "affiliation": ""}
                    for index, name in enumerate(item["authors"])
                ],
                "model": "fixture", "raw_json": "{}",
            })
        seeded[item["key"]] = source_id
    runtime.knowledge_query.backfill_chunk_fts(notebook_id)
    return seeded


#: Test values for ``find_sources``' two relevance lines (not the calibrated
#: configuration): tests pin them so a recalibration never moves an assertion.
STRONG_LINE, RELATED_LINE = 0.7, 0.3


def sqlite_corpus(tmp_path: Path, monkeypatch: Any) -> Iterator[tuple[Any, str, dict[str, str]]]:
    """A SQLite repository with the whole corpus seeded into one notebook,
    ``TopicEmbedder`` bound and the two lines pinned: yields
    ``(repository, notebook_id, {paper key: source_id})``.  For a fixture body:
    ``yield from sqlite_corpus(tmp_path, monkeypatch)``."""
    from app.core.config import Settings
    from app.models.schemas import NotebookCreate
    from app.services.sqlite_repository import (
        SQLiteRepository,
        reset_request_user,
        set_request_user,
    )
    from tests.model_testkit import bind_all_embedding_clients

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", str(TOPIC_EMBED_DIM))
    instance = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(instance, TopicEmbedder())
    monkeypatch.setattr(instance.settings, "find_sources_strong_relevance", STRONG_LINE)
    monkeypatch.setattr(instance.settings, "find_sources_related_relevance", RELATED_LINE)
    alice = instance._runtime.identity.create_user("a00123456", "password-12")
    token = set_request_user(alice)
    try:
        nb = instance.create_notebook(NotebookCreate(name="papers")).id
        yield instance, nb, seed_paper_topics(instance, nb)
    finally:
        reset_request_user(token)
        instance.close()
