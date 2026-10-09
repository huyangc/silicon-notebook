"""Ruling-M1 cleanup migration, SQLite side (``_migration_87``; PostgreSQL twin
``tests/postgres/test_memory_isolation_migration_pg.py`` seeds the same world
from ``memory_isolation_cases.py`` and asserts the same statements).

Pinned here:

* upgrade: a v86 database carrying pre-isolation rows (a shared cluster with a
  Memory member, a canonical id minted from a Memory seed, a shared object with
  Memory evidence, a chunk under a Memory source with all four derived chunk
  rows) comes out with none of them, the affected notebook marked 0 and its
  derived-layer gates reset, and the control notebook untouched;
* fresh database: the column exists with DEFAULT 1 and new notebooks read 1;
* idempotent: re-running the migration changes nothing, and in particular
  never re-queues a notebook whose isolated rebuild already finished;
* the "Memory-derived" predicates inlined in both migrations are exactly what
  ``memory_sql`` renders today (drift is a failing test, not a silent fork).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.core.config import Settings
from app.repositories.postgres import memory_sql as pg_memory_sql
from app.repositories.sqlite import memory_sql as sqlite_memory_sql
from app.repositories.sqlite import migrations as sqlite_migrations
from app.repositories.sqlite.migrations import SqliteMigrator
from app.services.sqlite_repository import SQLiteRepository
from tests import memory_isolation_cases as cases

PG_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "app" / "repositories" / "postgres" / "migrations"
    / "0067_memory_kg_isolation.sql"
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'mki.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def _marker(database, notebook_id):
    with database.connect() as db:
        row = db.execute(
            "SELECT memory_isolation_version FROM unified_kg_state WHERE notebook_id=?",
            (notebook_id,),
        ).fetchone()
    return None if row is None else int(row[0])


def _snapshot(database):
    with database.connect() as db:
        return cases.snapshot(db, include_fts=True)


def _roll_back_to_v86(database) -> None:
    """Forge the deployed v86 schema the data was written under: the marker
    column did not exist yet."""
    with database.write() as db:
        db.execute("ALTER TABLE unified_kg_state DROP COLUMN memory_isolation_version")
        db.execute("PRAGMA user_version = 86")


@pytest.fixture
def upgraded(repo):
    database = repo._runtime.database
    with database.write() as db:
        cases.seed(db, postgres=False)
    before = _snapshot(database)
    _roll_back_to_v86(database)
    # v87 alone: v88 (PR-E8) then rewrites the public library's promotion
    # copy that this world keeps (``ko-pub``), which is v88's own acceptance
    # (tests/test_promotion_provenance_migration.py), not v87's.
    SqliteMigrator(database, repo.settings)._migration_87()
    return repo, before, _snapshot(database)


def test_upgrade_removes_every_pre_isolation_memory_derived_row(upgraded):
    _repo, before, after = upgraded
    cases.assert_isolated(before, after)


def test_migration_logs_counts_only(repo, caplog):
    """The one log line is content-free: counts, never ids or text; the
    Memory object that keeps merged shared evidence is counted."""
    import logging

    database = repo._runtime.database
    with database.write() as db:
        cases.seed(db, postgres=False)
    _roll_back_to_v86(database)
    with caplog.at_level(logging.INFO, logger="silicon_notebook.sqlite.maintenance"):
        SqliteMigrator(database, repo.settings).migrate()
    lines = [r.getMessage() for r in caplog.records
             if "memory-kg-isolation migration" in r.getMessage()]
    assert lines == [
        f"memory-kg-isolation migration: affected_notebooks={len(cases.F_NOTEBOOKS)} "
        f"memory_objects={len(cases.MEMORY_OBJECTS | cases.OTHER_MEMORY_OBJECTS)} "
        "clusters_removed=4 communities_removed=1 memory_chunks_removed=1 "
        f"private_kept=1 promotions_rejected={len(cases.PROMOTIONS_REJECTED)} "
        f"conflicts_applied_on_shared_nodes={cases.CONFLICTS_APPLIED_ON_SHARED} "
        f"conflicts_applied_on_shared_edges={cases.CONFLICTS_APPLIED_ON_SHARED_EDGES} "
        f"cross_owner_stripped={cases.CROSS_OWNER_STRIPPED} "
        f"evidence_scan_notebooks={len(cases.EVIDENCE_SCANNED)} "
        f"seed_check_notebooks={len(cases.SEED_CHECKED)}"
    ]


def test_upgrade_marks_only_notebooks_holding_a_memory_source(upgraded):
    repo, _before, _after = upgraded
    database = repo._runtime.database
    for notebook in cases.F_NOTEBOOKS:
        assert _marker(database, notebook) == 0, notebook
    # outside F with clusters -- public libraries and the copy without a state
    # row included: marked 2 for the worker's post-readiness check (never
    # cleaned by the migration: assert_isolated section 6)
    for notebook in cases.SEED_CHECKED:
        assert _marker(database, notebook) == 2, notebook
    with database.connect() as db:
        assert int(db.execute("PRAGMA user_version").fetchone()[0]) == 87


def test_rerun_changes_nothing_and_never_requeues_a_finished_notebook(upgraded):
    repo, _before, after = upgraded
    database = repo._runtime.database
    migrator = SqliteMigrator(database, repo.settings)
    migrator._migration_87()
    assert _snapshot(database) == after
    assert _marker(database, cases.NB_F) == 0
    # The isolated rebuild finished (marker 1) and regenerated F's analysis
    # precompute and a checkpoint: a replay must not put the notebook back in
    # the queue, bump its sequences again, or delete what the rebuild made.
    with database.write() as db:
        db.execute(
            "UPDATE unified_kg_state SET memory_isolation_version=1 WHERE notebook_id=?",
            (cases.NB_F,),
        )
        db.execute(
            "INSERT INTO kg_analysis_artifacts(notebook_id,kind,payload,created_at) "
            "VALUES (?,'boards','{}',?)", (cases.NB_F, cases.NOW))
        db.execute(
            "INSERT INTO kg_rebuild_checkpoint(notebook_id,input_version,stage,"
            "item_key,payload,created_at) VALUES (?,'v2','merge_review','k2','{}',?)",
            (cases.NB_F, cases.NOW))
    settled = _snapshot(database)
    assert settled["kg_analysis_artifacts"] and settled["kg_rebuild_checkpoint"]
    migrator._migration_87()
    assert _marker(database, cases.NB_F) == 1
    assert _snapshot(database) == settled


def test_fresh_database_has_the_marker_defaulting_to_isolated(repo):
    database = repo._runtime.database
    with database.connect() as db:
        assert int(db.execute("PRAGMA user_version").fetchone()[0]) == 89
        column = {
            row[1]: (row[2], row[3], row[4])
            for row in db.execute("PRAGMA table_info(unified_kg_state)")
        }["memory_isolation_version"]
    assert column == ("INTEGER", 1, "1")
    from app.models.schemas import NotebookCreate

    notebook = repo.create_notebook(NotebookCreate(name="after upgrade"))
    assert _marker(database, notebook.id) == 1


def test_migration_is_atomic_with_its_version_stamp(repo, monkeypatch):
    """v87 stamps user_version inside its own transaction: a failure half-way
    leaves the database at v86 with every row as it was."""
    database = repo._runtime.database
    with database.write() as db:
        cases.seed(db, postgres=False)
    _roll_back_to_v86(database)
    before = _snapshot(database)

    original = sqlite_migrations._V87_MEMORY_DERIVED_KR
    monkeypatch.setattr(
        sqlite_migrations, "_V87_MEMORY_DERIVED_KR", original + " AND no_such_column"
    )
    with pytest.raises(sqlite3.OperationalError):
        SqliteMigrator(database, repo.settings).migrate()
    with database.connect() as db:
        assert int(db.execute("PRAGMA user_version").fetchone()[0]) == 86
        columns = {row[1] for row in db.execute("PRAGMA table_info(unified_kg_state)")}
    assert "memory_isolation_version" not in columns
    assert _snapshot(database) == before


# ------------------------------------------------ frozen predicate text
_INLINED = {
    # (memory_sql function, alias) -> SQLite frozen constant
    ("memory_source_type_predicate", "s.source_type"): "_V87_MEMORY_SOURCE_S",
    ("memory_derived_object", "ko"): "_V87_MEMORY_DERIVED_KO",
    ("memory_derived_relation", "kr"): "_V87_MEMORY_DERIVED_KR",
}


@pytest.mark.parametrize("column", ("canonical_a", "canonical_b"))
def test_both_migrations_decode_sentinels_exactly_as_memory_sql_does(column):
    """The unique-seed sentinel decoding of step 4 is memory_sql's
    ``cluster_seed_object_id`` over a merge candidate's column (PR-E5's
    ``column`` parameter), frozen in both migrations."""
    assert sqlite_migrations._v87_sentinel_object_id(
        f"concept_merge_candidates.{column}"
    ) == sqlite_memory_sql.cluster_seed_object_id("concept_merge_candidates", column)
    live = pg_memory_sql.cluster_seed_object_id("c", column)
    assert live in PG_MIGRATION.read_text(encoding="utf-8")


def test_promotion_kind_literal_matches_the_memory_promotion_path():
    """The generic/Memory promotion split keys on the object_type the
    creator-only path writes; pin both migrations to that exact value."""
    import inspect

    from app.services import knowledge_governance

    source = inspect.getsource(
        knowledge_governance.KnowledgeGovernanceService.propose_memory_promotion
    )
    assert 'item.notebook_id, item.id, "memory", now,' in source
    assert sqlite_migrations._V87_PROMOTION_MEMORY_KIND == "'memory'"
    assert "pc.object_type <> 'memory'" in PG_MIGRATION.read_text(encoding="utf-8")


def test_promotion_rejection_writes_one_reason_literal_on_both_backends():
    """The frozen literal is the same in both migrations and in the shared
    test world (always checked, whatever branch the tree is on)."""
    sql = PG_MIGRATION.read_text(encoding="utf-8")
    literal = sqlite_migrations._V87_PROMOTION_REJECTED_REASON
    assert literal == cases.PROMOTION_REJECTED_REASON
    assert f"reason = '{literal}'" in sql
    assert sql.count("reason = '") == 1


def test_promotion_rejection_reason_equals_the_write_side_constant():
    """The approval-time refusal (E4-3) writes
    ``MEMORY_PROMOTION_REJECTED_REASON``; the migration closes the same
    proposals with the same reason. Skipped ONLY while that module is not in
    the tree (it lands with the write-side branch); once it exists, any
    import error or a different value is a failure."""
    import importlib
    import importlib.util

    name = "app.domain.memory_kg_isolation"
    if importlib.util.find_spec(name) is None:
        pytest.skip(f"{name} is not in this tree yet (write-side branch)")
    constant = importlib.import_module(name).MEMORY_PROMOTION_REJECTED_REASON
    assert sqlite_migrations._V87_PROMOTION_REJECTED_REASON == constant
    assert f"reason = '{constant}'" in PG_MIGRATION.read_text(encoding="utf-8")


def test_open_promotion_statuses_are_the_curator_queue_statuses():
    """"Open" in the migration is exactly what the curator queue lists by
    default on both backends (schema statuses: proposed | under_review |
    approved | rejected), so a new open status cannot slip past it."""
    import inspect
    import re

    from app.repositories.postgres import governance_store as pg_gov
    from app.repositories.sqlite import governance_store as sqlite_gov

    frozen = sqlite_migrations._V87_PROMOTION_OPEN_STATUSES
    assert f"pc.status IN {frozen}" in PG_MIGRATION.read_text(encoding="utf-8")
    wanted = set(re.findall(r"'([a-z_]+)'", frozen))
    for module in (pg_gov, sqlite_gov):
        source = inspect.getsource(module.GovernanceStore.promotion_queue_rows)
        (queue,) = re.findall(r"status IN \(([^)]*)\)", source)
        assert set(re.findall(r"'([a-z_]+)'", queue)) == wanted, module.__name__


@pytest.mark.parametrize("fragment", sorted(_INLINED))
def test_sqlite_migration_inlines_exactly_what_memory_sql_renders(fragment):
    function, alias = fragment
    live = getattr(sqlite_memory_sql, function)(alias)
    assert getattr(sqlite_migrations, _INLINED[fragment]) == live


@pytest.mark.parametrize("fragment", sorted(_INLINED))
def test_postgres_migration_inlines_exactly_what_memory_sql_renders(fragment):
    function, alias = fragment
    live = getattr(pg_memory_sql, function)(alias)
    sql = PG_MIGRATION.read_text(encoding="utf-8")
    assert live in sql, (function, alias)


def test_postgres_migration_classifies_memory_only_through_those_fragments():
    """Every ``'memory'`` literal of the PostgreSQL file sits inside one of
    the rendered fragments: no second, hand-written definition of
    "Memory source" hides in it."""
    sql = PG_MIGRATION.read_text(encoding="utf-8")
    body = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    rendered = {
        getattr(pg_memory_sql, function)(alias) for function, alias in _INLINED
    }
    for text in rendered:
        body = body.replace(text, "")
    # the one other 'memory' is a promotion KIND (propose_memory_promotion's
    # object_type), not a source classification
    body = body.replace("pc.object_type <> 'memory'", "")
    assert "'memory'" not in body


# ------------------------------------------------ pre-deploy census (read-only)
def _census_module():
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "scripts" / "memory_isolation_census.py"
    spec = importlib.util.spec_from_file_location("memory_isolation_census_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: what ``--all-signals`` finds on every G notebook of the shared world
ALL_SIGNALS = {
    cases.NB_C: set(), cases.NB_COPY: set(), cases.NB_PUB: set(),
    cases.NB_G: {"seed"}, cases.NB_H: {"seed"},
    cases.NB_GX: {"dirty", "stale_reference"}, cases.NB_GD: {"dirty"},
    cases.NB_GK: {"dirty"},
    cases.NB_GM: {"stale_reference"}, cases.NB_GC: {"stale_reference"},
}


def _by_set(result, name):
    return {r["notebook_id"]: r for r in result["notebooks"] if r["set"] == name}


def test_the_census_reports_every_signal_and_writes_nothing(repo, capsys, monkeypatch, tmp_path):
    """scripts/memory_isolation_census.py on a PRE-upgrade snapshot (no marker
    column): F (always rebuilt) and G with the first signal the check will
    act on and its cost, every signal on its own with --all-signals, the
    objects and the last rebuild's merge-review pairs of each, and the
    non-copyable notebooks with a pre-isolation scale index -- and the
    database file is byte-for-byte unchanged. After the upgrade it reads
    markers 0 and 2."""
    import hashlib

    census = _census_module()
    database = repo._runtime.database
    with database.write() as db:
        cases.seed(db, postgres=False)
    _roll_back_to_v86(database)
    url = repo.settings.database_url
    db_file = Path(database.db_path)
    storage = tmp_path / "census-storage"
    for notebook_id, manifest in ((cases.NB_C, {}), (cases.NB_COPY, {}),
                                  (cases.NB_G, {"memory_isolation": 1})):
        (storage / "kg_index" / notebook_id).mkdir(parents=True)
        (storage / "kg_index" / notebook_id / "manifest.json").write_text(
            json.dumps(manifest))
    # standalone visualisations without a scale root: GK (4 objects) is over
    # the synchronous budget of 3, G (3 objects) is rebuilt on its first read
    for notebook_id in (cases.NB_GK, cases.NB_G):
        (storage / "kg_viz" / notebook_id).mkdir(parents=True)
        (storage / "kg_viz" / notebook_id / "manifest.json").write_text("{}")
    monkeypatch.setenv("NOTEBOOK_COPY_MAX_ROWS", "1")
    monkeypatch.setenv("VIZ_SYNC_BUILD_MAX_OBJECTS", "3")

    def digest():
        return hashlib.sha256(db_file.read_bytes()).hexdigest()

    before = digest()
    result = census.census(url, all_signals=True, storage_dir=str(storage))
    assert digest() == before
    assert result["phase"] == "pre-upgrade"
    assert sorted(_by_set(result, "F")) == sorted(cases.F_NOTEBOOKS)
    g = _by_set(result, "G")
    assert {nb: r["signal"] for nb, r in g.items()} == cases.SIGNALS
    assert {nb: {k for k, v in r["signals"].items() if v}
            for nb, r in g.items()} == ALL_SIGNALS
    assert all(r["statements"] >= 1 and r["seconds"] >= 0 for r in g.values())
    # the copyable copy's and the stamped index are not scale work
    assert {nb: r["signal"] for nb, r in _by_set(result, "SCALE").items()} == {
        cases.NB_C: "index", cases.NB_GK: "viz"}
    assert _by_set(result, "F")[cases.NB_F]["merge_review_pairs"] == 1
    assert g[cases.NB_GK]["objects"] == 4
    summary = census.summarise(result)
    assert summary["f_rebuilt"] == len(cases.F_NOTEBOOKS)
    assert summary["g_queued"] == sum(1 for s in cases.SIGNALS.values() if s)
    assert summary["g_dirty_only"] == 2  # GD and GK
    assert summary["g_by_signal"] == {"dirty": 3, "seed": 2, "stale_reference": 3}
    assert summary["scale_builds"] == 2
    # the CLI entry prints one line per notebook and the summary
    assert census.main(["--database-url", url, "--notebook", cases.NB_GX,
                        "--storage-dir", ""]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith(f"G\t{cases.NB_GX}\tdirty\tobjects=2\t")
    assert json.loads(out[-1])["g_queued"] == 1
    assert digest() == before

    # v87 alone: v88 (PR-E8) rewrites and marks dirty this world's public
    # promotion copy (ko-pub), which the census would then rightly report
    SqliteMigrator(database, repo.settings)._migration_87()
    after = census.census(url)
    assert after["phase"] == "post-upgrade"
    assert sorted(_by_set(after, "F")) == sorted(cases.F_NOTEBOOKS)
    assert {nb: r["signal"] for nb, r in _by_set(after, "G").items()} == cases.SIGNALS


def test_the_census_connection_cannot_write(repo):
    from app.repositories.sqlite.memory_isolation_store import read_only_connection

    with read_only_connection(repo.settings.database_url) as db:
        with pytest.raises(sqlite3.OperationalError):
            db.execute("DELETE FROM notebooks")
