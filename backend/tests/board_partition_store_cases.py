"""Shared world and expectations for the board partition store primitives
(SQLite v98 / PostgreSQL 0078, placeholder numbers until the PR-4 merge;
design docs/superpowers/specs/2026-10-19-kg-analysis-findings-design_zh.md §五 S2).

``tests/test_board_partition_store.py`` runs every case on SQLite and
``tests/postgres/test_board_partition_store_pg.py`` on PostgreSQL; both compare
the observed value with the SAME literal below. Every case goes through
``repo._runtime.<store>.<method>``.
"""
from __future__ import annotations

from contextlib import closing

import pytest

from app.models.schemas import NotebookCreate

NOW = "2026-10-21T08:00:00.123456+00:00"


class World:
    def __init__(self, notebook_id: str, other_id: str) -> None:
        self.nb = notebook_id
        self.other = other_id


def seed(repo) -> World:
    """Two notebooks. In the first: generation 1 holds two level-0 boards and one
    level-1 board, generation 2 one level-0 board; the second notebook has a
    generation-1 board of its own. Every board carries its own board_key."""
    rt = repo._runtime
    world = World(repo.create_notebook(NotebookCreate(name="partition")).id,
                  repo.create_notebook(NotebookCreate(name="partition-other")).id)
    names = {"K1": "one", "K2": "two", "K3": "three", "K4": "four", "K9": "nine"}
    deg = {"K1": 2.0, "K2": 1.0, "K3": 1.0, "K4": 1.0, "K9": 1.0}
    with rt.database.write() as db:
        write = rt.unified_kg.write_communities_generation
        write(db, world.nb, 0, [("cm-1a", ["K1", "K2"]), ("cm-1b", ["K3"])], names, deg,
              NOW, 1, board_keys=["bk-a", "bk-b"])
        write(db, world.nb, 1, [("cm-1l", ["K4"])], names, deg, NOW, 1,
              board_keys=["bk-level-one"])
        write(db, world.nb, 0, [("cm-2a", ["K1"])], names, deg, NOW, 2,
              board_keys=["bk-c"])
        write(db, world.other, 0, [("cm-x", ["K9"])], names, deg, NOW, 1,
              board_keys=["bk-x"])
    return world


def board_key_rows(rt, notebook_id: str, level: int, generation: int) -> list[tuple[str, str]]:
    with rt.database.connect() as db:
        with closing(rt.unified_kg.community_board_key_rows(
                db, notebook_id, level, generation)) as rows:
            return sorted((row["canonical_id"], row["board_key"]) for row in rows)


def check_board_key_rows_read_one_generation_and_level(repo) -> None:
    """The previous-partition read: exactly the given generation's members at the
    given level, each with its own board's key, and nothing of another notebook,
    generation or level."""
    rt = repo._runtime
    world = seed(repo)
    assert board_key_rows(rt, world.nb, 0, 1) == [("K1", "bk-a"), ("K2", "bk-a"), ("K3", "bk-b")]
    assert board_key_rows(rt, world.nb, 0, 2) == [("K1", "bk-c")]
    assert board_key_rows(rt, world.nb, 1, 1) == [("K4", "bk-level-one")]
    assert board_key_rows(rt, world.nb, 0, 3) == []
    assert board_key_rows(rt, world.other, 0, 1) == [("K9", "bk-x")]


def check_board_keys_are_written_and_copied_forward(repo) -> None:
    """``write_communities_generation`` stores each row's board_key (and refuses a
    key list that does not line up with the rows); copy-forward re-mints the id
    and keeps the key; the board overview returns it."""
    rt = repo._runtime
    world = seed(repo)
    with rt.database.write() as db:
        with pytest.raises(ValueError):
            rt.unified_kg.write_communities_generation(
                db, world.nb, 0, [("cm-bad", ["K1"])], {}, {}, NOW, 5, board_keys=[])
        copied = rt.unified_kg.copy_forward_communities(
            db, world.nb, 0, 1, 3, lambda: "cm-copied")
    assert copied == 1
    assert board_key_rows(rt, world.nb, 1, 3) == [("K4", "bk-level-one")]
    with rt.database.connect() as db:
        level_one = db.execute(
            "SELECT id, board_key FROM communities WHERE generation = 3").fetchall()
    assert [(row["id"], row["board_key"]) for row in level_one] == [("cm-copied", "bk-level-one")]


def partition_state(rt, notebook_id: str) -> tuple[int, str]:
    with rt.unified_kg.read_snapshot() as db:
        row = rt.unified_kg.state_row(db, notebook_id)
    return int(row["community_partition_version"]), row["community_partition_mode"]


def check_partition_columns_round_trip(repo) -> None:
    """``set_community_partition`` writes the published partition's version and
    mode; a fresh notebook reads 0 / '' (built by the algorithm before this one)."""
    rt = repo._runtime
    world = seed(repo)
    assert partition_state(rt, world.nb) == (0, "")
    with rt.database.write() as db:
        rt.unified_kg.set_community_partition(db, world.nb, version=1, mode="warm")
    assert partition_state(rt, world.nb) == (1, "warm")
    assert partition_state(rt, world.other) == (0, "")


def check_partition_version_cas(repo) -> None:
    """With ``expected_version`` the version write is a CAS against the value
    the publish read after its claim: a ``--cold-partition`` 0 written in
    between survives (False is returned), while the mode -- how this publish
    was built -- is written either way."""
    rt = repo._runtime
    world = seed(repo)
    with rt.database.write() as db:
        assert rt.unified_kg.set_community_partition(
            db, world.nb, version=1, mode="warm", expected_version=0) is True
    assert partition_state(rt, world.nb) == (1, "warm")
    with rt.database.write() as db:
        rt.unified_kg.set_community_partition(db, world.nb, version=0, mode="warm")
        assert rt.unified_kg.set_community_partition(
            db, world.nb, version=1, mode="cold", expected_version=1) is False
    assert partition_state(rt, world.nb) == (0, "cold")
    assert partition_state(rt, world.other) == (0, "")


def check_kg_delete_resets_the_partition_columns(repo) -> None:
    """Deleting the knowledge graph takes the partition columns back to 0 / '':
    the next rebuild has no previous partition to warm-start from. Another
    notebook's columns stay."""
    rt = repo._runtime
    world = seed(repo)
    with rt.database.write() as db:
        for notebook_id in (world.nb, world.other):
            rt.unified_kg.set_community_partition(db, notebook_id, version=1, mode="warm")
    rt.knowledge_lifecycle.delete_notebook_kg(world.nb)
    assert partition_state(rt, world.nb) == (0, "")
    assert partition_state(rt, world.other) == (1, "warm")


def check_index_publication_resets_the_partition_columns(repo, *, postgres: bool) -> None:
    """The indexing-pipeline publication voids every derived KG product and
    resets the community pointer, so the partition columns go back to 0 / ''
    with it."""
    rt = repo._runtime
    p = "%s" if postgres else "?"
    notebook_id = repo.create_notebook(NotebookCreate(name="publish")).id
    pipeline = ("test.pipeline", "v1", "g1")
    job = rt.kg_build_jobs.create_job(notebook_id, "user-local", "rebuild", 0)
    with rt.database.write() as db:
        db.execute(
            f"UPDATE notebooks SET indexing_pipeline={p}, indexing_pipeline_version={p}, "
            f"indexing_pipeline_generation={p}, indexing_pipeline_job_id={p} "
            f"WHERE id={p}",
            (*pipeline, job["id"], notebook_id),
        )
        rt.unified_kg.set_community_partition(db, notebook_id, version=1, mode="cold")
    rt.kg_build_jobs.begin_indexing_pipeline_stage(job["id"], notebook_id, *pipeline, [])
    assert rt.kg_build_jobs.publish_indexing_pipeline_success(
        job["id"], notebook_id, *pipeline) is True
    assert partition_state(rt, notebook_id) == (0, "")


def check_board_overview_returns_board_keys(repo) -> None:
    """The board overview (`/kg-analysis` boards) returns each published board's
    key beside its re-minted id."""
    rt = repo._runtime
    notebook_id = repo.create_notebook(NotebookCreate(name="overview")).id
    # Publish through the generation protocol (the overview reads the published one).
    with rt.database.write() as db:
        claim = rt.unified_kg.claim_derived_generation(db, notebook_id, ttl_seconds=3600)
        assert claim is not None
    with rt.database.write() as db:
        rt.unified_kg.write_communities_generation(
            db, notebook_id, 0, [("cm-p1", ["K1", "K2"]), ("cm-p2", ["K3"])],
            {"K1": "one", "K2": "two", "K3": "three"}, {"K1": 2.0, "K2": 1.0, "K3": 1.0},
            NOW, claim["generation"], board_keys=["bk-big", "bk-small"])
        assert rt.unified_kg.flip_community_generation(
            db, notebook_id, published_from=claim["community_generation"],
            generation=claim["generation"], now=NOW)
    overview = rt.unified_kg.community_overview(notebook_id, limit=10, top_k=5)
    assert [(c["id"], c["board_key"], c["size"]) for c in overview["communities"]] == [
        ("cm-p1", "bk-big", 2), ("cm-p2", "bk-small", 1)]
