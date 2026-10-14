"""The scale build's explicit full collections run for large builds only.

``ScaleIndexBuilder._build_attempt`` used to call ``gc.collect()`` six times per
build whatever its size. A full collection costs time in proportion to every
object the process holds, so a server with a large heap paid that for every
small notebook too. ``_StageCollector`` keeps the six collections for builds
that have handled ``STAGE_COLLECT_MIN_ROWS`` rows and skips them below that.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services import scale_index_builder
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients


class _CountingGc:
    def __init__(self) -> None:
        self.collections = 0

    def collect(self, *_args, **_kwargs) -> int:
        self.collections += 1
        return 0


@pytest.fixture
def counting_gc(monkeypatch) -> _CountingGc:
    spy = _CountingGc()
    monkeypatch.setattr(scale_index_builder, "gc", spy)
    return spy


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    repository = SQLiteRepository(Settings())
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    try:
        yield repository
    finally:
        repository.close()


def _small_notebook(repo) -> str:
    notebook = repo.create_notebook(NotebookCreate(name="base"))
    repo.store_kg(
        notebook.id,
        None,
        [
            {"local_id": "a", "object_type": "concept",
             "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
            {"local_id": "b", "object_type": "concept",
             "payload": {"name": "current mirror", "section_path": ""}, "evidence": []},
        ],
        [{"source_local_id": "b", "target_local_id": "a",
          "edge_type": "depends_on", "evidence": []}],
    )
    repo.rebuild_unified_kg(notebook.id)
    return notebook.id


def test_the_threshold_is_ten_thousand_rows():
    # The collections were added for multi-GB builds (hundreds of thousands of
    # objects and up); at this many rows a 1024-wide float32 vector set is
    # ~40 MB, and every stage collection measured on a 50k-object build
    # reclaimed zero unreachable objects. Moving the threshold is a production
    # memory decision, not a test tweak.
    assert scale_index_builder.STAGE_COLLECT_MIN_ROWS == 10_000


def test_no_collection_below_the_threshold(counting_gc):
    collect = scale_index_builder._StageCollector()
    collect(scale_index_builder.STAGE_COLLECT_MIN_ROWS - 1)
    collect()

    assert counting_gc.collections == 0


def test_rows_accumulate_across_stages_and_every_later_stage_collects(counting_gc):
    threshold = scale_index_builder.STAGE_COLLECT_MIN_ROWS
    collect = scale_index_builder._StageCollector()
    collect(threshold // 2)
    collect(threshold // 2 - 1)
    assert counting_gc.collections == 0

    collect(1)  # the stage that crosses the threshold collects
    collect(0)  # and so does every stage after it, whatever it handled
    collect()

    assert counting_gc.collections == 3


def test_a_small_build_runs_no_explicit_collection(repo, counting_gc):
    manifest = repo._runtime.scale_builder.build(_small_notebook(repo))

    assert manifest["n_nodes"] > 0
    assert counting_gc.collections == 0


def test_a_large_build_still_collects_after_each_of_the_six_heavy_stages(
    repo, counting_gc, monkeypatch
):
    # Same build, threshold at one row: the first stage already crosses it, as
    # it does for any library with STAGE_COLLECT_MIN_ROWS KG objects, so every
    # stage point collects exactly as the ungated build did.
    monkeypatch.setattr(scale_index_builder, "STAGE_COLLECT_MIN_ROWS", 1)

    repo._runtime.scale_builder.build(_small_notebook(repo))

    assert counting_gc.collections == 6
