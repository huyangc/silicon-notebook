"""Task 28: backup-only snapshot verifier for pre-refactor databases.

The verifier (``scripts/verify_repository_snapshot.py``) must prove that the
refactored repository can open a real, pre-refactor SQLite database WITHOUT
ever touching the original files: the original is only read via a
``mode=ro`` URI long enough for ``Connection.backup()``, the repository is
constructed exclusively on the temporary backup + a temporary storage
directory, and the only rows allowed to change on open are the documented
startup normalizations (interrupted-job recovery, seed inserts, the admin
in-place upgrade) and deterministic versioned migration cleanup.  Its stdout
may carry table names / counts / digests — never row content.
"""
from __future__ import annotations

import importlib.util
import shutil
import socket
import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
VERIFIER_PATH = ROOT / "scripts" / "verify_repository_snapshot.py"
FIXTURE_ROOT = ROOT / "backend" / "tests" / "fixtures" / "repository_v9"

# Strings seeded into the committed v9 fixture (see
# scripts/generate_repository_contract_fixtures.py) that must NEVER leak to
# the verifier's stdout/stderr: usernames, titles, sources, prompts, answers,
# report content and credentials/tokens.
FIXTURE_SECRETS = (
    "a00123456",
    "b00654321",
    "Fixture Owner",
    "Fixture amplifier notes",
    "Repository contract fixture",
    "Why is fixture gain stable?",
    "Why is gain stable?",
    "Source degeneration stabilizes gain",
    "source degeneration",
    "Explain gain stabilization",
    "Gain stabilization",
    "fixture-password",
    "session-fixture-token",
    "shr-fixture-token",
)


def _rollback_v68(db: sqlite3.Connection) -> None:
    """Undo _migration_68 (batch-3-W1 PR-2's unified_kg_state.kg_reset_epoch)
    before forging any older deployed schema. No index to drop -- a pure
    column addition, same shape as _rollback_v66's uploaded_by column."""
    db.execute("ALTER TABLE unified_kg_state DROP COLUMN kg_reset_epoch")


def _rollback_v67(db: sqlite3.Connection) -> None:
    """Undo _migration_67 before forging any older deployed schema."""
    db.execute("DROP TABLE wish_votes")
    db.execute("DROP TABLE wishes")


def _rollback_v66(db: sqlite3.Connection) -> None:
    """Undo _migration_66's visible-source upload attribution."""
    db.execute("DROP INDEX idx_sources_uploaded_by_created")
    db.execute("ALTER TABLE sources DROP COLUMN uploaded_by")


def _rollback_v65(db: sqlite3.Connection) -> None:
    """Undo _migration_65's standalone retained-activity table."""
    db.execute("DROP TABLE retained_user_activity")


def _rollback_v64(db: sqlite3.Connection) -> None:
    """Undo _migration_64 (concept_clusters keyset covering index).

    Same rule as the siblings below: a new migration has to be undone in the
    forged "before" snapshot too, or its object already exists there and the
    verifier reports it as a manifested addition that never happened.
    Rollback runs newest-first, so this precedes _rollback_v63 (and every
    older rollback) at every call site. No separate table drop: the index
    sits on concept_clusters, which exists since v1 and is never dropped by
    any rollback helper below.
    """
    db.execute("DROP INDEX idx_clusters_nb_canonical_member")


def _rollback_v61(db: sqlite3.Connection) -> None:
    """Undo _migration_61 (hot-path fix batch 1's five index groups, seven
    SQLite indexes).

    Unlike v60 (agent_observations.kind — never needed its own rollback
    because every deployed-vNN target below 55 already drops the whole
    agent_observations table via _rollback_v55, taking the column with it),
    these seven indexes sit on foundational tables (concept_clusters,
    extraction_runs, knowledge_source_fact_elements, memory_items,
    knowledge_relations, sources) that exist since v1 and are never dropped
    by any rollback helper below. Without an explicit undo here, every
    forged "before" snapshot (built by upgrading the v9 fixture all the way
    to current, then rolling back everything after the target version) would
    still carry these indexes, so the verifier would see them as already
    present in "before" and flag the (target, 61) manifest's expected
    addition as ``manifest-addition-missing`` — the migration's OWN indexes
    would look undone, at every target version, not just this one.
    """
    db.execute("DROP INDEX idx_sources_nb_hidden_type")
    db.execute("DROP INDEX idx_knowledge_relations_nb_source_target_edge")
    db.execute("DROP INDEX idx_memory_items_notebook")
    db.execute("DROP INDEX idx_knowledge_source_fact_elements_notebook")
    db.execute("DROP INDEX idx_extraction_runs_notebook")
    db.execute("DROP INDEX idx_clusters_nb_canonical_name_lower")
    db.execute("DROP INDEX idx_clusters_nb_canonical")


def _rollback_v62(db: sqlite3.Connection) -> None:
    """Undo _migration_62 (creator-wide Ask activity keyset index)."""
    db.execute("DROP INDEX idx_ask_jobs_creator_activity")


def _rollback_v63(db: sqlite3.Connection) -> None:
    """Undo _migration_63 (extension_runtime_toggles).

    Same rule as the siblings below: a new migration has to be undone in the
    forged "before" snapshot too, or its object already exists there and the
    verifier reports it as a manifested addition that never happened.
    Rollback runs newest-first, so this precedes _rollback_v62/_rollback_v61
    (and every older rollback) at every call site. No separate DROP INDEX:
    the table has no secondary index, so the table drop is the whole
    rollback.
    """
    db.execute("DROP TABLE extension_runtime_toggles")


def _rollback_v59(db: sqlite3.Connection) -> None:
    """Undo _migration_59 (backend-local unpublished indexing stages)."""
    db.execute("DROP TABLE indexing_pipeline_stage_sources")
    db.execute("DROP TABLE indexing_pipeline_stages")


def _rollback_v58(db: sqlite3.Connection) -> None:
    """Undo _migration_58 (desired/published indexing-pipeline identity)."""
    db.execute("ALTER TABLE extraction_runs DROP COLUMN indexing_pipeline_version")
    db.execute("ALTER TABLE extraction_runs DROP COLUMN indexing_pipeline_id")
    db.execute("ALTER TABLE unified_kg_state DROP COLUMN indexing_pipeline_version")
    db.execute("ALTER TABLE unified_kg_state DROP COLUMN indexing_pipeline_id")
    db.execute("ALTER TABLE notebooks DROP COLUMN indexing_pipeline_job_id")
    db.execute("ALTER TABLE notebooks DROP COLUMN indexing_pipeline_generation")
    db.execute("ALTER TABLE notebooks DROP COLUMN indexing_pipeline_version")
    db.execute("ALTER TABLE notebooks DROP COLUMN indexing_pipeline")


def _rollback_v57(db: sqlite3.Connection) -> None:
    """Undo _migration_57 (the reusable group invitation capability)."""
    db.execute("DROP INDEX idx_groups_invite_token")
    db.execute("ALTER TABLE groups DROP COLUMN invite_created_by")
    db.execute("ALTER TABLE groups DROP COLUMN invite_created_at")
    db.execute("ALTER TABLE groups DROP COLUMN invite_token")


def _rollback_v56(db: sqlite3.Connection) -> None:
    """Undo _migration_56 (the live group owner pointer)."""
    db.execute("ALTER TABLE groups DROP COLUMN owner_id")


def _rollback_v55(db: sqlite3.Connection) -> None:
    """Undo _migration_55 (Agentic Memory P3, T1: agent_observations +
    user_profiles.search_profile_json).

    Same rule as the siblings below: a new migration has to be undone in the
    forged "before" snapshot too, or its objects already exist there and the
    verifier reports them as manifested additions that never happened.
    Rollback runs newest-first, so this precedes _rollback_v54 at every call
    site. No separate DROP INDEX: SQLite's DROP TABLE takes the table's
    indexes with it, so both idx_agent_observations_request (the T1
    idempotency index) and idx_agent_observations_scope (the T2/T6 fix
    round's non-unique scope index, added to the still-unmerged v55
    migration in place rather than as a new hop) disappear with the table
    (a standalone DROP INDEX here would be a dead line — the T1 quality
    review proved deleting the first one changes nothing, and the same
    argument applies to the second). agent_observations has no incoming
    foreign key from anywhere, so the table drop is the whole table-side
    rollback; the unrelated user_profiles column drop comes last.
    """
    db.execute("DROP TABLE agent_observations")
    db.execute("ALTER TABLE user_profiles DROP COLUMN search_profile_json")


def _rollback_v54(db: sqlite3.Connection) -> None:
    """Undo _migration_54 (Agentic Memory P2: retrieval_experiences).

    Same rule as the siblings below: a new migration has to be undone in the
    forged "before" snapshot too, or its objects already exist there and the
    verifier reports them as manifested additions that never happened.
    Rollback runs newest-first, so this precedes _rollback_v52 at every call
    site (and follows _rollback_v55, which must run first). The table has no
    incoming foreign key from anywhere (it has no foreign key at all, in
    either direction) and the migration creates no index, so this one DROP
    is the whole rollback.

    There is deliberately no ``_rollback_v53``: v53 only adds the
    ``agent_profile_jobs.claim_token`` COLUMN, and ``_rollback_v51`` below
    drops that whole table, so undoing v53 separately would be dropping a
    column off a table that is about to disappear.
    """
    db.execute("DROP TABLE retrieval_experiences")


def _rollback_v52(db: sqlite3.Connection) -> None:
    """Undo _migration_52 (conversation public share tokens + read watermark).

    Same rule as the siblings below: a new migration has to be undone in the
    forged "before" snapshot too, or its objects already exist there and the
    verifier reports them as manifested additions that never happened.
    Rollback runs newest-first, so this precedes _rollback_v51 at every call
    site (and follows _rollback_v54, which must run first). All three columns
    are on ``conversations`` — an existing table with
    no incoming foreign key of its own from the group-sharing tables below —
    so column order relative to _rollback_v51/_rollback_v50 does not matter
    for correctness; DROP INDEX before DROP COLUMN mirrors the migration's
    own build order (index depends on the column existing).
    """
    db.execute("DROP INDEX idx_conversations_share_token")
    db.execute("ALTER TABLE conversations DROP COLUMN shared_through_id")
    db.execute("ALTER TABLE conversations DROP COLUMN shared_through_at")
    db.execute("ALTER TABLE conversations DROP COLUMN share_token")


def _rollback_v51(db: sqlite3.Connection) -> None:
    """Undo _migration_51 (Agentic Memory P1: agent_notebook_profile /
    agent_profile_jobs).

    Same rule as the siblings below: a new migration has to be undone in the
    forged "before" snapshot too, or its objects already exist there and the
    verifier reports them as manifested additions that never happened.
    Rollback runs newest-first, so this precedes _rollback_v50 at every call
    site (and follows _rollback_v52, which must run first). Neither table has
    an incoming foreign key from any OTHER table and the migration creates no
    index, so DROP order is for readability only.
    """
    db.execute("DROP TABLE agent_profile_jobs")
    db.execute("DROP TABLE agent_notebook_profile")


def _rollback_v50(db: sqlite3.Connection) -> None:
    """Undo _migration_50 (group knowledge sharing P2: notebook_share_requests).

    Same rule as the siblings below: a new migration has to be undone in the
    forged "before" snapshot too, or its objects already exist there and the
    verifier reports them as manifested additions that never happened.
    Rollback runs newest-first, so this precedes _rollback_v49 at every call
    site (and follows _rollback_v51, which must run first). Unlike
    notebook_grants, notebook_share_requests.group_id is a real foreign key
    onto groups(id) — but that's a downstream table dependency, not an
    incoming one, so dropping notebook_share_requests here (before
    _rollback_v49 drops groups) is still the correct child-before-parent
    order.
    """
    db.execute("DROP TABLE notebook_share_requests")


def _rollback_v49(db: sqlite3.Connection) -> None:
    """Undo _migration_49 (group knowledge sharing P1: groups/group_members/
    notebook_grants).

    Same rule as the siblings below: a new migration has to be undone in the
    forged "before" snapshot too, or its objects already exist there and the
    verifier reports them as manifested additions that never happened.
    Rollback runs newest-first, so this precedes _rollback_v48 at every call
    site (and follows _rollback_v50, which must run first — it drops
    notebook_share_requests, a downstream FK dependent of groups). DROP
    order is child-before-parent for readability only (none of these three
    tables has an incoming foreign key from any OTHER table now that
    notebook_share_requests is already gone, so order does not matter for
    correctness here) — notebook_grants and group_members before groups,
    matching the DROP-newest-first convention used throughout this module.
    """
    db.execute("DROP TABLE notebook_grants")
    db.execute("DROP TABLE group_members")
    db.execute("DROP TABLE groups")


def _rollback_v48(db: sqlite3.Connection) -> None:
    """Undo _migration_48 (sources.agent_profile_id provenance column).

    Same rule as the siblings below: a new migration has to be undone in the
    forged "before" snapshot too, or its objects already exist there and the
    verifier reports them as manifested additions that never happened.
    Rollback runs newest-first, so this precedes _rollback_v47 at every call
    site.
    """
    db.execute("ALTER TABLE sources DROP COLUMN agent_profile_id")


def _rollback_v47(db: sqlite3.Connection) -> None:
    """Undo _migration_47's notebook-owned schema table."""
    db.execute("DROP TABLE notebook_object_schemas")


def _load_verifier():
    assert VERIFIER_PATH.is_file(), "scripts/verify_repository_snapshot.py is missing"
    spec = importlib.util.spec_from_file_location(
        "repository_snapshot_verifier", VERIFIER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclasses can resolve the module's
    # `from __future__ import annotations` string annotations (py3.13).
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _copy_fixture(tmp_path: Path) -> tuple[Path, Path]:
    database = tmp_path / "original.db"
    storage = tmp_path / "original-storage"
    shutil.copyfile(FIXTURE_ROOT / "baseline.db", database)
    shutil.copytree(FIXTURE_ROOT / "storage", storage)
    return database, storage


def _rollback_v46(db: sqlite3.Connection) -> None:
    """Undo _migration_46 (element -> chunk reverse index + backfill ledger).

    Same rule as _rollback_v43 below: a new migration has to be undone in the
    forged "before" snapshot too, or its objects already exist there and the
    verifier reports them as manifested additions that never happened.
    Rollback runs newest-first, so this precedes _rollback_v45 (ui_mode) and
    _rollback_v43 (which carries the v44 question-index undo inline) at every
    call site."""
    db.execute("DROP TABLE chunk_element_backfills")
    db.execute("DROP TABLE chunk_elements")
    db.execute("ALTER TABLE unified_kg_state DROP COLUMN chunk_elements_indexed")


def _rollback_v45(db: sqlite3.Connection) -> None:
    """Undo _migration_45 (user_profiles.ui_mode interface-mode preference).

    Same rationale as _rollback_v43: every deployed-vNN fixture below forges
    an older schema by upgrading to current then rolling back everything
    later migrations added, so a new migration needs its own undo too.
    Rollback runs newest-first, so this precedes _rollback_v43 — which also
    carries the v44 question-index undo inline — at every call site.
    """
    db.execute("ALTER TABLE user_profiles DROP COLUMN ui_mode")


def _rollback_v43(db: sqlite3.Connection) -> None:
    """Undo _migration_43 (per-report share tokens).

    Every deployed-vNN fixture below is built by upgrading to the current
    schema and rolling back exactly what each later migration added, so a new
    migration has to be undone here too — otherwise its objects already exist
    in the "before" snapshot and the verifier reports them as manifested
    additions that never happened.  Index first: the column it covers cannot
    be dropped while it exists.
    """
    # The helper is called by every forged pre-current deployment. Undo the
    # latest additive question-index migration before the historical v43 hop.
    db.execute("DROP TABLE chunk_questions")
    db.execute("ALTER TABLE chunks DROP COLUMN question_indexed_at")
    db.execute("DROP INDEX idx_reports_share_token")
    db.execute("ALTER TABLE reports DROP COLUMN shared_at")
    db.execute("ALTER TABLE reports DROP COLUMN share_token")


def _rollback_v34(db: sqlite3.Connection) -> None:
    """Remove the v34-v40 additions before forging an older deployment.

    A faithful pre-v34 shape lacks everything EVERY later migration adds, not
    just v34's: leaving _migration_36's tables behind would make the replay
    observe no addition for them and fail closed as manifest-addition-missing.
    Roll back newest-first; DROP TABLE takes each table's own indexes with it
    (idx_kg_source_profiles_nb_mainstream, and _migration_39's six). The
    seventh index _migration_39 installs sits on the PRE-EXISTING
    knowhow_tables, so no DROP TABLE reaches it and it must be named.
    """
    db.execute("DROP TABLE source_index_backfills")            # _migration_42
    db.execute("DROP TABLE knowledge_source_fact_backfills")   # _migration_41
    db.execute("DROP INDEX idx_kos_source_object")              # _migration_41
    db.execute("DROP INDEX idx_knowledge_source_facts_source_generation_global")
    db.execute("DROP TABLE knowledge_source_fact_elements")    # _migration_40
    db.execute("DROP TABLE knowledge_source_facts")            # _migration_40
    db.execute("DROP INDEX idx_knowhow_tables_nb_title")       # _migration_39
    db.execute("DROP TABLE catalog_candidates")                # _migration_39
    db.execute("DROP TABLE catalog_jobs")                      # _migration_39
    db.execute("DROP INDEX idx_sources_visible_identity")      # _migration_38
    db.execute("DROP INDEX idx_source_elements_source_type")   # _migration_37
    db.execute("DROP TABLE kg_analysis_artifacts")             # _migration_36
    db.execute("DROP TABLE kg_community_edges")                # _migration_36
    db.execute("DROP TABLE kg_source_profiles")                # _migration_36
    db.execute("ALTER TABLE ask_jobs DROP COLUMN asked_at")
    db.execute("DROP TABLE kg_relation_completion_state")
    db.execute("DROP INDEX idx_knowledge_objects_source_id")


def _storage_stat(storage: Path) -> list[tuple[str, int, int]]:
    return sorted(
        (
            str(path.relative_to(storage)),
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in storage.rglob("*")
        if path.is_file()
    )


def _insert_running_jobs(database: Path) -> None:
    """Degrade the copy into a crashed-process shape: one running merge-review
    job plus one running ask job (cloned from the committed done job)."""
    with sqlite3.connect(database) as db:
        db.execute(
            "INSERT INTO merge_review_jobs (notebook_id, status) "
            "VALUES ('nb-fixture', 'running')"
        )
        db.execute(
            "INSERT INTO ask_jobs "
            "SELECT 'askjob-running', notebook_id, conversation_id, created_by, "
            "mode, question, 'running', trace_json, answer_id, error, "
            "created_at, updated_at FROM ask_jobs WHERE id='askjob-fixture'"
        )
    db.close()


def _insert_running_kg_build(database: Path) -> None:
    """Create the complete crash shape recovered by the v22 startup path."""
    with sqlite3.connect(database) as db:
        source_id, notebook_id = db.execute(
            "SELECT id, notebook_id FROM sources ORDER BY id LIMIT 1"
        ).fetchone()
        db.execute(
            "UPDATE sources SET status='extracting', parse_status='extracting', "
            "error_message='raw-provider-secret', updated_at='crash-source' "
            "WHERE id=?",
            (source_id,),
        )
        db.execute(
            "INSERT INTO extraction_runs "
            "(id, notebook_id, source_id, run_type, status, error_message, "
            "created_at, updated_at) VALUES "
            "('run-kg-crashed', ?, ?, 'kg', 'running', "
            "'raw-provider-secret', 'crash-run', 'crash-run')",
            (notebook_id, source_id),
        )
        db.execute(
            "INSERT INTO kg_build_jobs "
            "(id, notebook_id, created_by, mode, status, stage, total_sources, "
            "completed_sources, failed_sources, error_code, error_message, "
            "created_at, updated_at, finished_at) VALUES "
            "('job-kg-crashed', ?, 'user-local', 'incremental', 'running', "
            "'extracting', 1, 0, 0, 'raw_code', 'raw-provider-secret', "
            "'crash-job', 'crash-job', '')",
            (notebook_id,),
        )
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("PRAGMA journal_mode=DELETE")


def _verify_with_repository_mutation(module, database, storage, mutation, monkeypatch):
    real_repository = module.SQLiteRepository

    class MutatingRepository(real_repository):
        def __init__(self, settings):
            super().__init__(settings)
            with self._write() as db:
                mutation(db)

    monkeypatch.setattr(module, "SQLiteRepository", MutatingRepository)
    return module.verify_snapshot(database, storage)


def test_repository_is_never_constructed_with_original_database_path(
    tmp_path, monkeypatch
):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)

    captured: list = []
    real_repository = module.SQLiteRepository

    class RecordingRepository(real_repository):
        def __init__(self, settings):
            captured.append(settings)
            super().__init__(settings)

    real_connect = sqlite3.connect

    def recording_connect(target, *args, **kwargs):
        if str(database) in str(target):
            # The original may only ever be opened via a read-only URI.
            assert str(target).startswith("file:"), target
            assert "mode=ro" in str(target), target
            assert kwargs.get("uri") or (args and args[-1] is True)
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(module, "SQLiteRepository", RecordingRepository)
    monkeypatch.setattr(module.sqlite3, "connect", recording_connect)

    result = module.verify_snapshot(database, storage)
    assert result.ok, result.discrepancies
    assert captured, "verifier never constructed the repository"
    repo_db = Path(captured[0].sqlite_path).resolve()
    assert repo_db != database.resolve()
    assert database.resolve() not in repo_db.parents
    assert repo_db.is_absolute()


def test_repository_is_never_constructed_with_original_storage_path_or_symlink(
    tmp_path, monkeypatch
):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    storage_link = tmp_path / "storage-link"
    storage_link.symlink_to(storage, target_is_directory=True)

    captured: list = []
    real_repository = module.SQLiteRepository

    class RecordingRepository(real_repository):
        def __init__(self, settings):
            captured.append(settings)
            super().__init__(settings)

    monkeypatch.setattr(module, "SQLiteRepository", RecordingRepository)
    result = module.verify_snapshot(database, storage_link)
    assert result.ok, result.discrepancies
    assert captured
    repo_storage = Path(captured[0].storage_dir)
    assert not repo_storage.is_symlink()
    resolved = repo_storage.resolve()
    original = storage.resolve()
    assert resolved != original
    assert original not in resolved.parents
    assert resolved != storage_link.resolve()


def test_wal_committed_rows_are_present_in_backup(tmp_path):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)

    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at) "
            "VALUES ('nb-wal', 'wal notebook', 't0', 't0')"
        )
        writer.commit()
        wal = Path(f"{database}-wal")
        assert wal.is_file() and wal.stat().st_size > 0, (
            "test setup: committed row must still live in the WAL sidecar"
        )
        # Keep the writer connection open so the WAL cannot be checkpointed
        # away — the verifier's read-only backup must still see the row.
        result = module.verify_snapshot(database, storage)
    finally:
        writer.close()

    assert result.ok, result.discrepancies
    assert result.table_counts["notebooks"] == 2


def test_live_wal_shm_mtime_is_an_explicit_metadata_exception(tmp_path):
    import os

    module = _load_verifier()
    database, _storage = _copy_fixture(tmp_path)

    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at) "
            "VALUES ('nb-shm', 'shm exception', 't0', 't0')"
        )
        writer.commit()
        shm = Path(f"{database}-shm")
        assert shm.is_file(), "test setup: live WAL must have an SHM sidecar"
        before = module._source_metadata(database)
        stat = shm.stat()
        shm_size = stat.st_size
        os.utime(
            shm,
            ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000),
        )
        assert shm.stat().st_mtime_ns != stat.st_mtime_ns
        after = module._source_metadata(database)
    finally:
        writer.close()

    assert before == after
    assert before[-1] == (f"{database.name}-shm", shm_size, 0)


def test_live_wal_shm_size_change_fails_source_metadata_guard(tmp_path, monkeypatch):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)

    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at) "
            "VALUES ('nb-shm-size', 'shm size guard', 't0', 't0')"
        )
        writer.commit()
        shm = Path(f"{database}-shm")
        assert shm.is_file(), "test setup: live WAL must have an SHM sidecar"
        initial_size = shm.stat().st_size
        real_exercise_reads = module.exercise_reads

        def resize_shm(repo, backup_path):
            counts = real_exercise_reads(repo, backup_path)
            with shm.open("ab") as stream:
                stream.write(b"size-change")
            assert shm.stat().st_size > initial_size
            return counts

        monkeypatch.setattr(module, "exercise_reads", resize_shm)
        result = module.verify_snapshot(database, storage)
    finally:
        writer.close()

    assert not result.ok
    assert result.source_unchanged is False
    assert "original-database-metadata-changed" in result.discrepancies


def test_schema_tables_counts_pks_and_digests_are_preserved(tmp_path):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)

    result = module.verify_snapshot(database, storage)

    assert result.ok, result.discrepancies
    assert result.source_user_version == 9
    assert result.final_user_version == module.SCHEMA_VERSION
    assert result.changed_tables == []
    # Representative preserved counts from the committed fixture.
    assert result.table_counts["notebooks"] == 1
    assert result.table_counts["sources"] == 1
    assert result.table_counts["knowledge_objects"] == 2
    assert result.table_counts["knowledge_relations"] == 1
    # Every non-virtual table carries a digest; digests are opaque hex.
    assert result.table_digests["knowledge_objects"]
    assert all(
        set(d) <= set("0123456789abcdef") for d in result.table_digests.values() if d
    )
    # v9 -> v10 migration adds kg_rebuild_checkpoint; it must arrive empty and
    # be reported as a migration table, not a data change.
    assert "kg_rebuild_checkpoint" in result.migration_added_tables
    # Reads were exercised on the populated fixture.
    assert result.reads["notebooks"] == 1
    assert result.reads["sources"] == 1
    assert result.reads["knowledge_types"] >= 1
    assert result.reads["conversations"] >= 1
    assert result.reads["ask_jobs"] >= 1
    assert result.reads["reports"] >= 1


def test_deployed_v13_database_verifies_through_migrations_14_to_34(tmp_path):
    """The v13 hop is the one EVERY currently-deployed production database
    takes: v13 was the shipping schema before the memory-kg-extract feature.
    Post-v13 migrations are _migration_14 (sources.memory_id column + its
    partial unique index), _migration_15 (the parse_status/source_type
    covering index, Task 5), _migration_16 (the five knowhow/notebook_assets
    tables + their indexes, knowhow-tables PR-1 Task 1), _migration_17 (the
    two paper-metadata tables + their indexes, paper-metadata Task 1),
    _migration_18 (the knowhow_cell_code table + its index, knowhow-tables
    PR-2+3 Task 1 — its role-value remap is row data, invisible here),
    _migration_19 (notebook_assets.source_id column + its index, MinerU
    image-retention Task 2 — dropping notebook_assets below for
    _migration_16 already clears both, so no extra rollback step is
    needed), _migration_20 (notebook_bases table + its index, and
    promotion_candidates.target_base_id, multi-domain-base Task 1 — neither
    is absorbed by another table's drop here, since promotion_candidates is
    a foundational table nothing else in this rollback removes, so both get
    their own explicit rollback statement below, mirroring how
    sources.memory_id is handled). The v9-fixture tests above only exercise
    the (9, SCHEMA_VERSION) manifest key, so a missing (13, SCHEMA_VERSION)
    entry would make the backup verifier fail closed (migration-manifest-
    missing + unmanifested column/index/table) on every real upgrade while
    staying green in CI. Build the faithful v13 shape by upgrading the
    fixture copy to current and rolling back EXACTLY what every post-v13
    migration adds — including _migration_15's index, _migration_16's tables
    (which also absorb _migration_19's column + index), _migration_17's
    tables, _migration_18's table, _migration_20's table + column, the
    _migration_21 normalized-anchor index, _migration_22's durable KG
    build-job table, _migration_23's per-user model-service status, and
    _migration_24's kg_canonical_scratch table (write-lock slimming
    improvement point 2's cluster-map-swap preparation scratch table — no
    separate index rollback needed, DROP TABLE takes its index with it),
    _migration_25's system model-service status table, and
    _migration_26's two knowhow-history tables (knowhow_changes +
    knowhow_milestones — leaf tables nothing else absorbs, so each gets its
    own explicit rollback statement below), _migration_27's source completion
    marker, _migration_28's document-limit schema, and _migration_29's cluster
    membership uniqueness guard, or the constructed 'v13' would retain them
    and the hop would under-report its additions."""
    from app.core.config import Settings

    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)

    # One real open migrates the copy 9 -> current and applies the startup
    # normalizations once, so the verification open below is a pure 13 ->
    # current migration replay (scratch storage keeps the fixture storage
    # pristine).
    upgraded = module.SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{database}",
            storage_dir=str(tmp_path / "upgrade-storage"),
        )
    )
    upgraded.close_local()
    rollback = sqlite3.connect(database)
    try:
        _rollback_v68(rollback)
        _rollback_v67(rollback)
        _rollback_v66(rollback)
        _rollback_v65(rollback)
        _rollback_v64(rollback)
        _rollback_v63(rollback)
        _rollback_v62(rollback)
        _rollback_v61(rollback)
        _rollback_v34(rollback)
        rollback.execute("DROP INDEX idx_knowledge_relations_nb_source_id")  # _migration_33
        rollback.execute("DROP INDEX idx_knowledge_relations_nb_target_id")  # _migration_33
        rollback.execute("ALTER TABLE reports DROP COLUMN understanding_json")  # _migration_32
        rollback.execute("DROP TABLE shadow_capture_control")            # _migration_31
        rollback.execute("DROP TABLE shadow_change_log")                 # _migration_31
        rollback.execute("DROP INDEX idx_sources_notebook_file_hash")    # _migration_30
        rollback.execute("DROP INDEX uq_clusters_notebook_type_member")  # _migration_29
        rollback.execute("DROP TABLE app_settings")                      # _migration_28
        rollback.execute(
            "ALTER TABLE user_profiles DROP COLUMN upload_document_limit"
        )                                                                # _migration_28
        rollback.execute("ALTER TABLE sources DROP COLUMN chunked_at")   # _migration_27
        rollback.execute("DROP TABLE system_model_service_status")        # _migration_25
        rollback.execute("DROP TABLE kg_canonical_scratch")              # _migration_24
        rollback.execute("DROP TABLE model_service_status")               # _migration_23
        rollback.execute("DROP TABLE kg_build_jobs")                     # _migration_22
        rollback.execute("DROP INDEX idx_knowhow_cells_column_normalized_anchor_row")  # _migration_21
        rollback.execute("DROP TABLE notebook_bases")                     # _migration_20
        rollback.execute(
            "ALTER TABLE promotion_candidates DROP COLUMN target_base_id"
        )                                                                 # _migration_20
        rollback.execute("DROP TABLE source_authors")                    # _migration_17
        rollback.execute("DROP TABLE source_paper_meta")                 # _migration_17
        # knowhow_cell_code FKs onto BOTH knowhow_rows and knowhow_columns,
        # so it must drop before either parent (same child-before-parent
        # rule as knowhow_cells on the next line).
        rollback.execute("DROP TABLE knowhow_cell_code")                  # _migration_18
        rollback.execute("DROP TABLE knowhow_cells")                      # _migration_16
        rollback.execute("DROP TABLE knowhow_rows")                       # _migration_16
        rollback.execute("DROP TABLE knowhow_columns")                    # _migration_16
        # knowhow_changes / knowhow_milestones FK onto knowhow_tables, so
        # both must drop before it (same child-before-parent rule as above).
        rollback.execute("DROP INDEX idx_knowhow_milestones_table")       # _migration_26
        rollback.execute("DROP INDEX idx_knowhow_changes_table")          # _migration_26
        rollback.execute("DROP TABLE knowhow_milestones")                 # _migration_26
        rollback.execute("DROP TABLE knowhow_changes")                    # _migration_26
        rollback.execute("DROP TABLE knowhow_tables")                     # _migration_16
        rollback.execute("DROP TABLE notebook_assets")                   # _migration_16
        rollback.execute("DROP INDEX idx_sources_nb_parse_status_type")  # _migration_15
        rollback.execute("DROP INDEX idx_sources_memory_id")             # _migration_14
        rollback.execute("ALTER TABLE sources DROP COLUMN memory_id")    # _migration_14
        _rollback_v59(rollback)
        _rollback_v58(rollback)
        _rollback_v57(rollback)
        _rollback_v56(rollback)
        _rollback_v55(rollback)
        _rollback_v54(rollback)
        _rollback_v52(rollback)
        _rollback_v51(rollback)
        _rollback_v50(rollback)
        _rollback_v49(rollback)
        _rollback_v48(rollback)
        _rollback_v47(rollback)
        _rollback_v46(rollback)
        _rollback_v45(rollback)
        _rollback_v43(rollback)
        rollback.execute("PRAGMA user_version = 13")
        rollback.commit()
    finally:
        rollback.close()

    result = module.verify_snapshot(database, storage)

    assert result.ok, result.discrepancies
    assert result.source_user_version == 13
    assert result.final_user_version == module.SCHEMA_VERSION
    assert result.changed_tables == []


def test_deployed_v20_database_verifies_through_migrations_21_to_34(tmp_path):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)

    upgraded = module.SQLiteRepository(
        module.offline_settings(database, tmp_path / "upgrade-storage")
    )
    upgraded.close_local()
    rollback = sqlite3.connect(database)
    try:
        _rollback_v68(rollback)
        _rollback_v67(rollback)
        _rollback_v66(rollback)
        _rollback_v65(rollback)
        _rollback_v64(rollback)
        _rollback_v63(rollback)
        _rollback_v62(rollback)
        _rollback_v61(rollback)
        _rollback_v34(rollback)
        rollback.execute("DROP INDEX idx_knowledge_relations_nb_source_id")  # _migration_33
        rollback.execute("DROP INDEX idx_knowledge_relations_nb_target_id")  # _migration_33
        rollback.execute("ALTER TABLE reports DROP COLUMN understanding_json")  # _migration_32
        rollback.execute("DROP TABLE shadow_capture_control")            # _migration_31
        rollback.execute("DROP TABLE shadow_change_log")                 # _migration_31
        rollback.execute("DROP INDEX idx_sources_notebook_file_hash")    # _migration_30
        rollback.execute("DROP INDEX uq_clusters_notebook_type_member")  # _migration_29
        rollback.execute("DROP TABLE app_settings")                      # _migration_28
        rollback.execute(
            "ALTER TABLE user_profiles DROP COLUMN upload_document_limit"
        )                                                                # _migration_28
        rollback.execute("ALTER TABLE sources DROP COLUMN chunked_at")   # _migration_27
        rollback.execute("DROP TABLE system_model_service_status")       # _migration_25
        rollback.execute("DROP TABLE kg_canonical_scratch")
        rollback.execute("DROP INDEX idx_knowhow_milestones_table")
        rollback.execute("DROP INDEX idx_knowhow_changes_table")
        rollback.execute("DROP TABLE knowhow_milestones")
        rollback.execute("DROP TABLE knowhow_changes")
        rollback.execute("DROP TABLE model_service_status")
        rollback.execute("DROP TABLE kg_build_jobs")
        rollback.execute("DROP INDEX idx_knowhow_cells_column_normalized_anchor_row")
        _rollback_v59(rollback)
        _rollback_v58(rollback)
        _rollback_v57(rollback)
        _rollback_v56(rollback)
        _rollback_v55(rollback)
        _rollback_v54(rollback)
        _rollback_v52(rollback)
        _rollback_v51(rollback)
        _rollback_v50(rollback)
        _rollback_v49(rollback)
        _rollback_v48(rollback)
        _rollback_v47(rollback)
        _rollback_v46(rollback)
        _rollback_v45(rollback)
        _rollback_v43(rollback)
        rollback.execute("PRAGMA user_version = 20")
        rollback.commit()
    finally:
        rollback.close()

    result = module.verify_snapshot(database, storage)

    assert result.ok, result.discrepancies
    assert result.source_user_version == 20
    assert result.final_user_version == module.SCHEMA_VERSION


def test_offline_settings_use_an_empty_system_model_registry(tmp_path, monkeypatch):
    module = _load_verifier()
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "must-not-leak")
    database, storage = _copy_fixture(tmp_path)

    settings = module.offline_settings(
        tmp_path / "snapshot.db", tmp_path / "storage"
    )
    registry = module.SystemModelServiceRegistry.load(settings, environ={})

    assert settings.model_services_config == ""
    assert registry.service_for("ask_answer") is None
    assert registry.service_for("retrieval_query_embedding") is None
    assert module.verify_snapshot(database, storage).ok


def test_deployed_v21_database_verifies_through_migrations_22_to_34(tmp_path):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)

    upgraded = module.SQLiteRepository(
        module.offline_settings(database, tmp_path / "upgrade-storage")
    )
    upgraded.close_local()
    rollback = sqlite3.connect(database)
    try:
        _rollback_v68(rollback)
        _rollback_v67(rollback)
        _rollback_v66(rollback)
        _rollback_v65(rollback)
        _rollback_v64(rollback)
        _rollback_v63(rollback)
        _rollback_v62(rollback)
        _rollback_v61(rollback)
        _rollback_v34(rollback)
        rollback.execute("DROP INDEX idx_knowledge_relations_nb_source_id")  # _migration_33
        rollback.execute("DROP INDEX idx_knowledge_relations_nb_target_id")  # _migration_33
        rollback.execute("ALTER TABLE reports DROP COLUMN understanding_json")  # _migration_32
        rollback.execute("DROP TABLE shadow_capture_control")            # _migration_31
        rollback.execute("DROP TABLE shadow_change_log")                 # _migration_31
        rollback.execute("DROP INDEX idx_sources_notebook_file_hash")    # _migration_30
        rollback.execute("DROP INDEX uq_clusters_notebook_type_member")  # _migration_29
        rollback.execute("DROP TABLE app_settings")                      # _migration_28
        rollback.execute(
            "ALTER TABLE user_profiles DROP COLUMN upload_document_limit"
        )                                                                # _migration_28
        rollback.execute("ALTER TABLE sources DROP COLUMN chunked_at")   # _migration_27
        rollback.execute("DROP TABLE system_model_service_status")       # _migration_25
        rollback.execute("DROP TABLE kg_canonical_scratch")
        rollback.execute("DROP INDEX idx_knowhow_milestones_table")
        rollback.execute("DROP INDEX idx_knowhow_changes_table")
        rollback.execute("DROP TABLE knowhow_milestones")
        rollback.execute("DROP TABLE knowhow_changes")
        rollback.execute("DROP TABLE model_service_status")
        rollback.execute("DROP TABLE kg_build_jobs")
        _rollback_v59(rollback)
        _rollback_v58(rollback)
        _rollback_v57(rollback)
        _rollback_v56(rollback)
        _rollback_v55(rollback)
        _rollback_v54(rollback)
        _rollback_v52(rollback)
        _rollback_v51(rollback)
        _rollback_v50(rollback)
        _rollback_v49(rollback)
        _rollback_v48(rollback)
        _rollback_v47(rollback)
        _rollback_v46(rollback)
        _rollback_v45(rollback)
        _rollback_v43(rollback)
        rollback.execute("PRAGMA user_version = 21")
        rollback.commit()
    finally:
        rollback.close()

    result = module.verify_snapshot(database, storage)

    assert result.ok, result.discrepancies
    assert result.source_user_version == 21
    assert result.final_user_version == module.SCHEMA_VERSION


def test_deployed_v22_database_verifies_through_migrations_23_to_34(tmp_path):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)

    upgraded = module.SQLiteRepository(
        module.offline_settings(database, tmp_path / "upgrade-storage")
    )
    upgraded.close_local()
    rollback = sqlite3.connect(database)
    try:
        _rollback_v68(rollback)
        _rollback_v67(rollback)
        _rollback_v66(rollback)
        _rollback_v65(rollback)
        _rollback_v64(rollback)
        _rollback_v63(rollback)
        _rollback_v62(rollback)
        _rollback_v61(rollback)
        _rollback_v34(rollback)
        rollback.execute("DROP INDEX idx_knowledge_relations_nb_source_id")  # _migration_33
        rollback.execute("DROP INDEX idx_knowledge_relations_nb_target_id")  # _migration_33
        rollback.execute("ALTER TABLE reports DROP COLUMN understanding_json")  # _migration_32
        rollback.execute("DROP TABLE shadow_capture_control")            # _migration_31
        rollback.execute("DROP TABLE shadow_change_log")                 # _migration_31
        rollback.execute("DROP INDEX idx_sources_notebook_file_hash")    # _migration_30
        rollback.execute("DROP INDEX uq_clusters_notebook_type_member")  # _migration_29
        rollback.execute("DROP TABLE app_settings")                      # _migration_28
        rollback.execute(
            "ALTER TABLE user_profiles DROP COLUMN upload_document_limit"
        )                                                                # _migration_28
        # current is now eleven hops past v22 (v23 model_service_status, v24
        # kg_canonical_scratch, v25 system_model_service_status + credential
        # scrub, v26 knowhow_changes/knowhow_milestones, v27 sources.chunked_at,
        # v28 document limits, v29 cluster membership uniqueness, v30 source
        # hash lookup, v31 shadow capture metadata, v32 report understanding,
        # v33 relation indexes);
        # all must roll back so this forged source truly has nothing beyond v22,
        # or those additions would already be present pre-migration and
        # manifest-addition-missing would fire (they'd never appear in the
        # after-minus-before "newly added" set).
        rollback.execute("ALTER TABLE sources DROP COLUMN chunked_at")   # _migration_27
        rollback.execute("DROP TABLE system_model_service_status")       # _migration_25
        rollback.execute("DROP TABLE kg_canonical_scratch")
        rollback.execute("DROP INDEX idx_knowhow_milestones_table")
        rollback.execute("DROP INDEX idx_knowhow_changes_table")
        rollback.execute("DROP TABLE knowhow_milestones")
        rollback.execute("DROP TABLE knowhow_changes")
        rollback.execute("DROP TABLE model_service_status")
        _rollback_v59(rollback)
        _rollback_v58(rollback)
        _rollback_v57(rollback)
        _rollback_v56(rollback)
        _rollback_v55(rollback)
        _rollback_v54(rollback)
        _rollback_v52(rollback)
        _rollback_v51(rollback)
        _rollback_v50(rollback)
        _rollback_v49(rollback)
        _rollback_v48(rollback)
        _rollback_v47(rollback)
        _rollback_v46(rollback)
        _rollback_v45(rollback)
        _rollback_v43(rollback)
        rollback.execute("PRAGMA user_version = 22")
        rollback.commit()
    finally:
        rollback.close()

    result = module.verify_snapshot(database, storage)

    assert result.ok, result.discrepancies
    assert result.source_user_version == 22
    assert result.final_user_version == module.SCHEMA_VERSION


def test_deployed_v23_database_verifies_through_migrations_24_to_34(tmp_path):
    """A v23 database (has model_service_status + populated model_settings,
    missing _migration_24's kg_canonical_scratch, _migration_25's system
    model-service scrub, and _migration_26's knowhow-history tables) must
    verify clean through every remaining hop to the current version, and the v25
    scrub must fire on the restored credential/status rows."""
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)

    upgraded = module.SQLiteRepository(
        module.offline_settings(database, tmp_path / "upgrade-storage")
    )
    upgraded.close_local()
    with sqlite3.connect(database) as rollback:
        _rollback_v68(rollback)
        _rollback_v67(rollback)
        _rollback_v66(rollback)
        _rollback_v65(rollback)
        _rollback_v64(rollback)
        _rollback_v63(rollback)
        _rollback_v62(rollback)
        _rollback_v61(rollback)
        _rollback_v34(rollback)
        rollback.execute("DROP INDEX idx_knowledge_relations_nb_source_id")  # _migration_33
        rollback.execute("DROP INDEX idx_knowledge_relations_nb_target_id")  # _migration_33
        rollback.execute("ALTER TABLE reports DROP COLUMN understanding_json")  # _migration_32
        rollback.execute("DROP TABLE shadow_capture_control")            # _migration_31
        rollback.execute("DROP TABLE shadow_change_log")                 # _migration_31
        rollback.execute("DROP INDEX idx_sources_notebook_file_hash")    # _migration_30
        rollback.execute("DROP INDEX uq_clusters_notebook_type_member")  # _migration_29
        rollback.execute("DROP TABLE app_settings")                      # _migration_28
        rollback.execute(
            "ALTER TABLE user_profiles DROP COLUMN upload_document_limit"
        )                                                                # _migration_28
        rollback.execute("ALTER TABLE sources DROP COLUMN chunked_at")   # _migration_27
        rollback.execute("DROP TABLE system_model_service_status")       # _migration_25
        rollback.execute("DROP TABLE kg_canonical_scratch")
        rollback.execute("DROP INDEX idx_knowhow_milestones_table")
        rollback.execute("DROP INDEX idx_knowhow_changes_table")
        rollback.execute("DROP TABLE knowhow_milestones")
        rollback.execute("DROP TABLE knowhow_changes")
        rollback.execute(
            "UPDATE user_profiles SET model_settings=? WHERE user_id='user-local'",
            ('{"llm":{"api_key":"credential-must-be-scrubbed"}}',),
        )
        rollback.execute(
            "INSERT INTO model_service_status VALUES (?,?,?,?,?,?,?,?)",
            (
                "user-local", "llm", "old-fingerprint", "error", 0,
                "upstream_error", "observed_failure",
                "2030-01-01T00:00:00+00:00",
            ),
        )
        _rollback_v59(rollback)
        _rollback_v58(rollback)
        _rollback_v57(rollback)
        _rollback_v56(rollback)
        _rollback_v55(rollback)
        _rollback_v54(rollback)
        _rollback_v52(rollback)
        _rollback_v51(rollback)
        _rollback_v50(rollback)
        _rollback_v49(rollback)
        _rollback_v48(rollback)
        _rollback_v47(rollback)
        _rollback_v46(rollback)
        _rollback_v45(rollback)
        _rollback_v43(rollback)
        rollback.execute("PRAGMA user_version = 23")

    result = module.verify_snapshot(database, storage)

    assert result.ok, result.discrepancies
    assert result.source_user_version == 23
    assert result.final_user_version == module.SCHEMA_VERSION
    assert result.normalized["scrubbed_model_profiles"] == 1
    assert result.normalized["scrubbed_model_statuses"] == 1


def test_deployed_v32_database_verifies_relation_keyset_indexes(tmp_path):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)

    upgraded = module.SQLiteRepository(
        module.offline_settings(database, tmp_path / "upgrade-storage")
    )
    upgraded.close_local()
    with sqlite3.connect(database) as rollback:
        _rollback_v68(rollback)
        _rollback_v67(rollback)
        _rollback_v66(rollback)
        _rollback_v65(rollback)
        _rollback_v64(rollback)
        _rollback_v63(rollback)
        _rollback_v62(rollback)
        _rollback_v61(rollback)
        _rollback_v34(rollback)
        rollback.execute("DROP INDEX idx_knowledge_relations_nb_source_id")
        rollback.execute("DROP INDEX idx_knowledge_relations_nb_target_id")
        _rollback_v59(rollback)
        _rollback_v58(rollback)
        _rollback_v57(rollback)
        _rollback_v56(rollback)
        _rollback_v55(rollback)
        _rollback_v54(rollback)
        _rollback_v52(rollback)
        _rollback_v51(rollback)
        _rollback_v50(rollback)
        _rollback_v49(rollback)
        _rollback_v48(rollback)
        _rollback_v47(rollback)
        _rollback_v46(rollback)
        _rollback_v45(rollback)
        _rollback_v43(rollback)
        rollback.execute("PRAGMA user_version = 32")

    result = module.verify_snapshot(database, storage)

    assert result.ok, result.discrepancies
    assert result.source_user_version == 32
    assert result.final_user_version == module.SCHEMA_VERSION
    assert result.changed_tables == []


def test_deployed_v33_database_verifies_relation_completion_state(tmp_path):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    upgraded = module.SQLiteRepository(
        module.offline_settings(database, tmp_path / "upgrade-storage")
    )
    upgraded.close_local()
    with sqlite3.connect(database) as rollback:
        _rollback_v68(rollback)
        _rollback_v67(rollback)
        _rollback_v66(rollback)
        _rollback_v65(rollback)
        _rollback_v64(rollback)
        _rollback_v63(rollback)
        _rollback_v62(rollback)
        _rollback_v61(rollback)
        _rollback_v34(rollback)
        _rollback_v59(rollback)
        _rollback_v58(rollback)
        _rollback_v57(rollback)
        _rollback_v56(rollback)
        _rollback_v55(rollback)
        _rollback_v54(rollback)
        _rollback_v52(rollback)
        _rollback_v51(rollback)
        _rollback_v50(rollback)
        _rollback_v49(rollback)
        _rollback_v48(rollback)
        _rollback_v47(rollback)
        _rollback_v46(rollback)
        _rollback_v45(rollback)
        _rollback_v43(rollback)
        rollback.execute("PRAGMA user_version = 33")

    result = module.verify_snapshot(database, storage)

    assert result.ok, result.discrepancies
    assert result.source_user_version == 33
    assert result.final_user_version == module.SCHEMA_VERSION
    assert result.changed_tables == []


def test_deployed_v36_database_verifies_source_element_type_index(tmp_path):
    """A deployed v36 database is missing the v37 and v38 indexes plus
    _migration_39's two tables (both asked_at and the three KG-analysis
    precompute tables already landed at v35 and v36). Rolling back the full
    v34-v39 helper would also strip those, which a genuine v36 deployment still
    has, so this test drops exactly the later additions instead of reusing
    `_rollback_v34`."""
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    upgraded = module.SQLiteRepository(
        module.offline_settings(database, tmp_path / "upgrade-storage")
    )
    upgraded.close_local()
    with sqlite3.connect(database) as rollback:
        _rollback_v68(rollback)
        _rollback_v67(rollback)
        _rollback_v66(rollback)
        _rollback_v65(rollback)
        _rollback_v64(rollback)
        _rollback_v63(rollback)
        _rollback_v62(rollback)
        _rollback_v61(rollback)
        rollback.execute("DROP TABLE source_index_backfills")          # _migration_42
        rollback.execute("DROP TABLE knowledge_source_fact_backfills") # _migration_41
        rollback.execute("DROP INDEX idx_kos_source_object")            # _migration_41
        rollback.execute("DROP INDEX idx_knowledge_source_facts_source_generation_global")
        rollback.execute("DROP TABLE knowledge_source_fact_elements")  # _migration_40
        rollback.execute("DROP TABLE knowledge_source_facts")          # _migration_40
        rollback.execute("DROP INDEX idx_knowhow_tables_nb_title")      # _migration_39
        rollback.execute("DROP TABLE catalog_candidates")               # _migration_39
        rollback.execute("DROP TABLE catalog_jobs")                     # _migration_39
        rollback.execute("DROP INDEX idx_sources_visible_identity")     # _migration_38
        rollback.execute("DROP INDEX idx_source_elements_source_type")  # _migration_37
        _rollback_v59(rollback)
        _rollback_v58(rollback)
        _rollback_v57(rollback)
        _rollback_v56(rollback)
        _rollback_v55(rollback)
        _rollback_v54(rollback)
        _rollback_v52(rollback)
        _rollback_v51(rollback)
        _rollback_v50(rollback)
        _rollback_v49(rollback)
        _rollback_v48(rollback)
        _rollback_v47(rollback)
        _rollback_v46(rollback)
        _rollback_v45(rollback)
        _rollback_v43(rollback)
        rollback.execute("PRAGMA user_version = 36")

    result = module.verify_snapshot(database, storage)

    assert result.ok, result.discrepancies
    assert result.source_user_version == 36
    assert result.final_user_version == module.SCHEMA_VERSION
    assert result.changed_tables == []


def test_deployed_v38_database_verifies_command_catalog_tables(tmp_path):
    """A deployed v38 database is missing exactly _migration_39's additions:
    its two tables AND the one index it puts on a pre-existing table.

    This is the hop that catches DDL smuggled into a sealed migration: the
    version gate short-circuits on `current >= SCHEMA_VERSION`, so anything
    added to an already-released migration never runs on a deployed database
    and only a forged deployment at the previous version can see it.

    `idx_knowhow_tables_nb_title` (R14 P2) is the reason this test names an
    index at all. Every earlier v39 index rides on `catalog_jobs`/
    `catalog_candidates`, so dropping those tables removed them implicitly;
    this one is on `knowhow_tables`, which a v38 deployment already has. If the
    backfill path did not install it, the replay would find the index present
    afterwards but absent from the forged v38 source and fail closed — which is
    precisely the assertion that proves the deployed-database upgrade really
    creates it, rather than it only ever existing on freshly built schemas.
    """
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    upgraded = module.SQLiteRepository(
        module.offline_settings(database, tmp_path / "upgrade-storage")
    )
    upgraded.close_local()

    # The committed fixture is a real v9 DEPLOYED database, so the upgrade just
    # run is itself the backfill path: a migration that only ever installed
    # this index on freshly built schemas would leave it absent here.
    with sqlite3.connect(database) as check:
        assert check.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_knowhow_tables_nb_title'"
        ).fetchone() is not None

    with sqlite3.connect(database) as rollback:
        _rollback_v68(rollback)
        _rollback_v67(rollback)
        _rollback_v66(rollback)
        _rollback_v65(rollback)
        _rollback_v64(rollback)
        _rollback_v63(rollback)
        _rollback_v62(rollback)
        _rollback_v61(rollback)
        rollback.execute("DROP TABLE source_index_backfills")
        rollback.execute("DROP TABLE knowledge_source_fact_backfills")
        rollback.execute("DROP INDEX idx_kos_source_object")
        rollback.execute("DROP INDEX idx_knowledge_source_facts_source_generation_global")
        rollback.execute("DROP TABLE knowledge_source_fact_elements")
        rollback.execute("DROP TABLE knowledge_source_facts")
        rollback.execute("DROP INDEX idx_knowhow_tables_nb_title")
        rollback.execute("DROP TABLE catalog_candidates")
        rollback.execute("DROP TABLE catalog_jobs")
        _rollback_v59(rollback)
        _rollback_v58(rollback)
        _rollback_v57(rollback)
        _rollback_v56(rollback)
        _rollback_v55(rollback)
        _rollback_v54(rollback)
        _rollback_v52(rollback)
        _rollback_v51(rollback)
        _rollback_v50(rollback)
        _rollback_v49(rollback)
        _rollback_v48(rollback)
        _rollback_v47(rollback)
        _rollback_v46(rollback)
        _rollback_v45(rollback)
        _rollback_v43(rollback)
        rollback.execute("PRAGMA user_version = 38")

    result = module.verify_snapshot(database, storage)

    assert result.ok, result.discrepancies
    assert result.source_user_version == 38
    assert result.final_user_version == module.SCHEMA_VERSION
    assert result.changed_tables == []


def test_deployed_v39_database_verifies_source_local_fact_tables(tmp_path):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    upgraded = module.SQLiteRepository(
        module.offline_settings(database, tmp_path / "upgrade-storage")
    )
    upgraded.close_local()
    with sqlite3.connect(database) as rollback:
        _rollback_v68(rollback)
        _rollback_v67(rollback)
        _rollback_v66(rollback)
        _rollback_v65(rollback)
        _rollback_v64(rollback)
        _rollback_v63(rollback)
        _rollback_v62(rollback)
        _rollback_v61(rollback)
        rollback.execute("DROP TABLE source_index_backfills")
        rollback.execute("DROP TABLE knowledge_source_fact_backfills")
        rollback.execute("DROP INDEX idx_kos_source_object")
        rollback.execute("DROP INDEX idx_knowledge_source_facts_source_generation_global")
        rollback.execute("DROP TABLE knowledge_source_fact_elements")
        rollback.execute("DROP TABLE knowledge_source_facts")
        _rollback_v59(rollback)
        _rollback_v58(rollback)
        _rollback_v57(rollback)
        _rollback_v56(rollback)
        _rollback_v55(rollback)
        _rollback_v54(rollback)
        _rollback_v52(rollback)
        _rollback_v51(rollback)
        _rollback_v50(rollback)
        _rollback_v49(rollback)
        _rollback_v48(rollback)
        _rollback_v47(rollback)
        _rollback_v46(rollback)
        _rollback_v45(rollback)
        _rollback_v43(rollback)
        rollback.execute("PRAGMA user_version = 39")

    result = module.verify_snapshot(database, storage)

    assert result.ok, result.discrepancies
    assert result.source_user_version == 39
    assert result.final_user_version == module.SCHEMA_VERSION
    assert result.changed_tables == []


def test_deployed_v40_database_verifies_source_fact_backfill_upgrade(tmp_path):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    upgraded = module.SQLiteRepository(
        module.offline_settings(database, tmp_path / "upgrade-storage")
    )
    upgraded.close_local()
    with sqlite3.connect(database) as rollback:
        rollback.execute("DROP TABLE source_index_backfills")
        rollback.execute("DROP TABLE knowledge_source_fact_backfills")
        rollback.execute("DROP INDEX idx_kos_source_object")
        rollback.execute(
            "DROP INDEX idx_knowledge_source_facts_source_generation_global"
        )
        rollback.execute(
            "ALTER TABLE knowledge_source_facts DROP COLUMN projection_origin"
        )
        _rollback_v68(rollback)
        _rollback_v67(rollback)
        _rollback_v66(rollback)
        _rollback_v65(rollback)
        _rollback_v64(rollback)
        _rollback_v63(rollback)
        _rollback_v62(rollback)
        _rollback_v61(rollback)
        _rollback_v59(rollback)
        _rollback_v58(rollback)
        _rollback_v57(rollback)
        _rollback_v56(rollback)
        _rollback_v55(rollback)
        _rollback_v54(rollback)
        _rollback_v52(rollback)
        _rollback_v51(rollback)
        _rollback_v50(rollback)
        _rollback_v49(rollback)
        _rollback_v48(rollback)
        _rollback_v47(rollback)
        _rollback_v46(rollback)
        _rollback_v45(rollback)
        _rollback_v43(rollback)
        rollback.execute("PRAGMA user_version = 40")

    result = module.verify_snapshot(database, storage)

    assert result.ok, result.discrepancies
    assert result.source_user_version == 40
    assert result.final_user_version == module.SCHEMA_VERSION
    assert result.changed_tables == []


def test_deployed_v41_database_verifies_source_index_progress_upgrade(tmp_path):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    upgraded = module.SQLiteRepository(
        module.offline_settings(database, tmp_path / "upgrade-storage")
    )
    upgraded.close_local()
    with sqlite3.connect(database) as rollback:
        _rollback_v68(rollback)
        _rollback_v67(rollback)
        _rollback_v66(rollback)
        _rollback_v65(rollback)
        _rollback_v64(rollback)
        _rollback_v63(rollback)
        _rollback_v62(rollback)
        _rollback_v61(rollback)
        _rollback_v59(rollback)
        _rollback_v58(rollback)
        _rollback_v57(rollback)
        _rollback_v56(rollback)
        _rollback_v55(rollback)
        _rollback_v54(rollback)
        _rollback_v52(rollback)
        _rollback_v51(rollback)
        _rollback_v50(rollback)
        _rollback_v49(rollback)
        _rollback_v48(rollback)
        _rollback_v47(rollback)
        _rollback_v46(rollback)
        _rollback_v45(rollback)
        _rollback_v43(rollback)
        rollback.execute("DROP TABLE source_index_backfills")
        rollback.execute("PRAGMA user_version = 41")

    result = module.verify_snapshot(database, storage)

    assert result.ok, result.discrepancies
    assert result.source_user_version == 41
    assert result.final_user_version == module.SCHEMA_VERSION
    assert result.changed_tables == []


def test_deployed_v45_database_verifies_chunk_element_index_upgrade(tmp_path):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    upgraded = module.SQLiteRepository(
        module.offline_settings(database, tmp_path / "upgrade-storage")
    )
    upgraded.close_local()
    with sqlite3.connect(database) as rollback:
        _rollback_v68(rollback)
        _rollback_v67(rollback)
        _rollback_v66(rollback)
        _rollback_v65(rollback)
        _rollback_v64(rollback)
        _rollback_v63(rollback)
        _rollback_v62(rollback)
        _rollback_v61(rollback)
        _rollback_v59(rollback)
        _rollback_v58(rollback)
        _rollback_v57(rollback)
        _rollback_v56(rollback)
        _rollback_v55(rollback)
        _rollback_v54(rollback)
        _rollback_v52(rollback)
        _rollback_v51(rollback)
        _rollback_v50(rollback)
        _rollback_v49(rollback)
        _rollback_v48(rollback)
        _rollback_v47(rollback)
        _rollback_v46(rollback)
        rollback.execute("PRAGMA user_version = 45")

    result = module.verify_snapshot(database, storage)

    assert result.ok, result.discrepancies
    assert result.source_user_version == 45
    assert result.final_user_version == module.SCHEMA_VERSION
    assert result.changed_tables == []


def test_deployed_v46_database_verifies_notebook_schema_relocation(tmp_path):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    upgraded = module.SQLiteRepository(
        module.offline_settings(database, tmp_path / "upgrade-storage")
    )
    upgraded.close_local()
    with sqlite3.connect(database) as rollback:
        _rollback_v68(rollback)
        _rollback_v67(rollback)
        _rollback_v66(rollback)
        _rollback_v65(rollback)
        _rollback_v64(rollback)
        _rollback_v63(rollback)
        _rollback_v62(rollback)
        _rollback_v61(rollback)
        _rollback_v59(rollback)
        _rollback_v58(rollback)
        _rollback_v57(rollback)
        _rollback_v56(rollback)
        _rollback_v55(rollback)
        _rollback_v54(rollback)
        _rollback_v52(rollback)
        _rollback_v51(rollback)
        _rollback_v50(rollback)
        _rollback_v49(rollback)
        _rollback_v48(rollback)
        _rollback_v47(rollback)
        rollback.execute(
            "INSERT INTO object_schemas "
            "(object_type,notebook_id,plural,fields,primary_field,description,"
            "label,list_fields,source,status,rationale,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy_recipe", "nb-fixture", "legacy_recipes", '["name"]',
                "name", "legacy", "Legacy recipe", "[]", "induced",
                "proposed", "legacy proposal", "2026-08-01T00:00:00+00:00",
                "2026-08-01T00:00:00+00:00",
            ),
        )
        rollback.execute("PRAGMA user_version = 46")

    result = module.verify_snapshot(database, storage)

    assert result.ok, result.discrepancies
    assert result.normalized["relocated_object_schemas"] == 1
    assert result.changed_tables == []


def test_deployed_v48_database_verifies_group_sharing_tables(tmp_path):
    """A deployed v48 database is missing exactly _migration_49's AND
    _migration_50's additions: the three P1 group-sharing tables (groups,
    group_members, notebook_grants), the P2 notebook_share_requests table,
    and their five indexes combined (three from v49, plus v50's
    idx_share_requests_group and the partial-unique
    uq_share_requests_one_pending).

    Same rationale as test_deployed_v38_database_verifies_command_catalog_tables
    above: the version gate short-circuits on `current >= SCHEMA_VERSION`, so
    anything smuggled into an already-released migration never runs on a
    deployed database and only a forged deployment at the previous version can
    see it. All five indexes here ride on the four NEW tables they cover
    (group_members, notebook_grants, notebook_share_requests twice), so
    `DROP TABLE` inside `_rollback_v50`/`_rollback_v49` removes them
    implicitly — unlike v39's `idx_knowhow_tables_nb_title`, no index in
    either migration lands on a pre-existing table, so nothing needs naming
    separately.
    """
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    upgraded = module.SQLiteRepository(
        module.offline_settings(database, tmp_path / "upgrade-storage")
    )
    upgraded.close_local()

    with sqlite3.connect(database) as rollback:
        _rollback_v68(rollback)
        _rollback_v67(rollback)
        _rollback_v66(rollback)
        _rollback_v65(rollback)
        _rollback_v64(rollback)
        _rollback_v63(rollback)
        _rollback_v62(rollback)
        _rollback_v61(rollback)
        _rollback_v59(rollback)
        _rollback_v58(rollback)
        _rollback_v57(rollback)
        _rollback_v56(rollback)
        _rollback_v55(rollback)
        _rollback_v54(rollback)
        _rollback_v52(rollback)
        _rollback_v51(rollback)
        _rollback_v50(rollback)
        _rollback_v49(rollback)
        rollback.execute("PRAGMA user_version = 48")

    result = module.verify_snapshot(database, storage)

    assert result.ok, result.discrepancies
    assert result.source_user_version == 48
    assert result.final_user_version == module.SCHEMA_VERSION
    assert result.changed_tables == []


def test_deployed_v49_database_verifies_share_request_table(tmp_path):
    """A deployed v49 database is missing exactly _migration_50's addition:
    notebook_share_requests and its two indexes (idx_share_requests_group,
    the plain lookup index, and uq_share_requests_one_pending, the partial
    unique index enforcing at most one pending request per (notebook_id,
    group_id)).

    Same rationale as test_deployed_v48_database_verifies_group_sharing_tables
    above, isolated to just the P2 hop: a forged deployment at v49 (P1's
    group-sharing tables present, P2's request table absent) is the only way
    to exercise _migration_50 in isolation, since a live database already at
    or past v50 short-circuits the version gate. Both indexes ride on the one
    new table they cover, so `DROP TABLE` inside `_rollback_v50` removes them
    implicitly.
    """
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    upgraded = module.SQLiteRepository(
        module.offline_settings(database, tmp_path / "upgrade-storage")
    )
    upgraded.close_local()

    with sqlite3.connect(database) as rollback:
        _rollback_v68(rollback)
        _rollback_v67(rollback)
        _rollback_v66(rollback)
        _rollback_v65(rollback)
        _rollback_v64(rollback)
        _rollback_v63(rollback)
        _rollback_v62(rollback)
        _rollback_v61(rollback)
        _rollback_v59(rollback)
        _rollback_v58(rollback)
        _rollback_v57(rollback)
        _rollback_v56(rollback)
        _rollback_v55(rollback)
        _rollback_v54(rollback)
        _rollback_v52(rollback)
        _rollback_v51(rollback)
        _rollback_v50(rollback)
        rollback.execute("PRAGMA user_version = 49")

    result = module.verify_snapshot(database, storage)

    assert result.ok, result.discrepancies
    assert result.source_user_version == 49
    assert result.final_user_version == module.SCHEMA_VERSION
    assert result.changed_tables == []


def test_deployed_v50_database_verifies_agent_profile_tables(tmp_path):
    """A deployed v50 database is missing exactly _migration_51's additions:
    the two Agentic Memory P1 tables (agent_notebook_profile,
    agent_profile_jobs).

    Same rationale as test_deployed_v48_database_verifies_group_sharing_tables
    above: the version gate short-circuits on `current >= SCHEMA_VERSION`, so
    anything smuggled into an already-released migration never runs on a
    deployed database and only a forged deployment at the previous version can
    see it. _migration_51 creates no index at all (the composite primary keys
    cover every read path), so unlike v49 there is nothing index-shaped for
    `_rollback_v51`'s `DROP TABLE` to carry away or to name separately.
    """
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    upgraded = module.SQLiteRepository(
        module.offline_settings(database, tmp_path / "upgrade-storage")
    )
    upgraded.close_local()

    with sqlite3.connect(database) as rollback:
        _rollback_v68(rollback)
        _rollback_v67(rollback)
        _rollback_v66(rollback)
        _rollback_v65(rollback)
        _rollback_v64(rollback)
        _rollback_v63(rollback)
        _rollback_v62(rollback)
        _rollback_v61(rollback)
        _rollback_v59(rollback)
        _rollback_v58(rollback)
        _rollback_v57(rollback)
        _rollback_v56(rollback)
        _rollback_v55(rollback)
        _rollback_v54(rollback)
        _rollback_v52(rollback)
        _rollback_v51(rollback)
        rollback.execute("PRAGMA user_version = 50")

    result = module.verify_snapshot(database, storage)

    assert result.ok, result.discrepancies
    assert result.source_user_version == 50
    assert result.final_user_version == module.SCHEMA_VERSION
    assert result.changed_tables == []


def test_deployed_v53_database_verifies_retrieval_experience_table(tmp_path):
    """A deployed v53 database is missing exactly _migration_54's addition:
    the deployment-global `retrieval_experiences` table.

    Same rationale as the v50 case above — the version gate short-circuits on
    `current >= SCHEMA_VERSION`, so anything smuggled into an already-released
    migration never runs on a deployed database and only a forged deployment at
    the previous version can see it. This hop is the narrowest possible one: a
    single CREATE TABLE with no index, no foreign key in either direction, and
    no column added to any existing table, so `changed_tables` must stay empty
    — a non-empty list here would mean the migration touched something it does
    not claim to.
    """
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    upgraded = module.SQLiteRepository(
        module.offline_settings(database, tmp_path / "upgrade-storage")
    )
    upgraded.close_local()

    with sqlite3.connect(database) as rollback:
        _rollback_v68(rollback)
        _rollback_v67(rollback)
        _rollback_v66(rollback)
        _rollback_v65(rollback)
        _rollback_v64(rollback)
        _rollback_v63(rollback)
        _rollback_v62(rollback)
        _rollback_v61(rollback)
        _rollback_v59(rollback)
        _rollback_v58(rollback)
        _rollback_v57(rollback)
        _rollback_v56(rollback)
        _rollback_v55(rollback)
        _rollback_v54(rollback)
        rollback.execute("PRAGMA user_version = 53")

    result = module.verify_snapshot(database, storage)

    assert result.ok, result.discrepancies
    assert result.source_user_version == 53
    assert result.final_user_version == module.SCHEMA_VERSION
    assert result.changed_tables == []


_V23_CLUSTER_ROWS = (
    (
        "cluster-old",
        "nb-fixture",
        "canonical-old",
        "member-duplicate",
        "old",
        "concept",
        "old-description",
        "old-signature",
        "2026-07-20T00:00:00+00:00",
    ),
    (
        "cluster-tie-a",
        "nb-fixture",
        "canonical-a",
        "member-duplicate",
        "a",
        "concept",
        "a-description",
        "a-signature",
        "2026-07-21T00:00:00+00:00",
    ),
    (
        "cluster-tie-z",
        "nb-fixture",
        "canonical-z",
        "member-duplicate",
        "z",
        "concept",
        "z-description",
        "z-signature",
        "2026-07-21T00:00:00+00:00",
    ),
    (
        "cluster-singleton",
        "nb-fixture",
        "canonical-singleton",
        "member-singleton",
        "singleton",
        "concept",
        "singleton-description",
        "singleton-signature",
        "2026-07-19T00:00:00+00:00",
    ),
)


def _prepare_v28_cluster_duplicates(module, database, tmp_path):
    upgraded = module.SQLiteRepository(
        module.offline_settings(database, tmp_path / "upgrade-storage")
    )
    upgraded.close_local()
    db = sqlite3.connect(database)
    try:
        _rollback_v68(db)
        _rollback_v67(db)
        _rollback_v66(db)
        _rollback_v65(db)
        _rollback_v64(db)
        _rollback_v63(db)
        _rollback_v62(db)
        _rollback_v61(db)
        _rollback_v34(db)
        db.execute("DROP INDEX idx_knowledge_relations_nb_source_id")  # _migration_33
        db.execute("DROP INDEX idx_knowledge_relations_nb_target_id")  # _migration_33
        db.execute("ALTER TABLE reports DROP COLUMN understanding_json")  # _migration_32
        db.execute("DROP TABLE shadow_capture_control")          # _migration_31
        db.execute("DROP TABLE shadow_change_log")               # _migration_31
        db.execute("DROP INDEX idx_sources_notebook_file_hash")  # _migration_30
        db.execute("DROP INDEX uq_clusters_notebook_type_member")
        db.executemany(
            "INSERT INTO concept_clusters "
            "(id,notebook_id,canonical_id,member_object_id,canonical_name,"
            "object_type,canonical_description,canonical_desc_sig,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            _V23_CLUSTER_ROWS,
        )
        _rollback_v59(db)
        _rollback_v58(db)
        _rollback_v57(db)
        _rollback_v56(db)
        _rollback_v55(db)
        _rollback_v54(db)
        _rollback_v52(db)
        _rollback_v51(db)
        _rollback_v50(db)
        _rollback_v49(db)
        _rollback_v48(db)
        _rollback_v47(db)
        _rollback_v46(db)
        _rollback_v45(db)
        _rollback_v43(db)
        db.execute("PRAGMA user_version = 28")
        db.commit()
    finally:
        db.close()


def test_v29_verifier_accepts_only_deterministic_cluster_duplicate_cleanup(tmp_path):
    module = _load_verifier()
    assert "concept_clusters" not in module.SPECIAL_TABLES
    database, storage = _copy_fixture(tmp_path)
    _prepare_v28_cluster_duplicates(module, database, tmp_path)

    result = module.verify_snapshot(database, storage)

    assert result.ok, result.discrepancies
    assert result.normalized["concept_clusters"] == 2
    assert result.changed_tables == []


@pytest.mark.parametrize(
    "corruption",
    ("wrong-survivor", "changed-singleton", "deleted-singleton"),
)
def test_v29_verifier_rejects_cluster_cleanup_corruption(
    tmp_path, monkeypatch, corruption
):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    _prepare_v28_cluster_duplicates(module, database, tmp_path)
    real_repository = module.SQLiteRepository

    class CorruptingRepository(real_repository):
        def __init__(self, settings):
            super().__init__(settings)
            with sqlite3.connect(settings.sqlite_path) as db:
                if corruption == "wrong-survivor":
                    db.execute(
                        "DELETE FROM concept_clusters WHERE id='cluster-tie-z'"
                    )
                    db.execute(
                        "INSERT INTO concept_clusters "
                        "(id,notebook_id,canonical_id,member_object_id,canonical_name,"
                        "object_type,canonical_description,canonical_desc_sig,created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        _V23_CLUSTER_ROWS[1],
                    )
                elif corruption == "changed-singleton":
                    db.execute(
                        "UPDATE concept_clusters SET canonical_description='corrupt' "
                        "WHERE id='cluster-singleton'"
                    )
                else:
                    db.execute(
                        "DELETE FROM concept_clusters WHERE id='cluster-singleton'"
                    )

    monkeypatch.setattr(module, "SQLiteRepository", CorruptingRepository)
    result = module.verify_snapshot(database, storage)

    assert not result.ok
    assert "concept_clusters" in result.changed_tables
    assert any(
        item.startswith("table=concept_clusters reason=")
        for item in result.discrepancies
    )


@pytest.mark.parametrize(
    "mutation, expected_fragment",
    [
        (
            lambda db: db.execute("CREATE TABLE verifier_backdoor (id TEXT)"),
            "verifier_backdoor",
        ),
        (
            lambda db: db.execute("ALTER TABLE notebooks ADD COLUMN verifier_flag TEXT"),
            "notebooks",
        ),
        (
            lambda db: (
                db.execute("DROP INDEX idx_chunks_nb"),
                db.execute("CREATE INDEX idx_chunks_nb ON chunks(source_id)"),
            ),
            "idx_chunks_nb",
        ),
        (
            lambda db: db.execute(
                "CREATE TRIGGER verifier_trigger AFTER INSERT ON notebooks "
                "BEGIN SELECT 1; END"
            ),
            "verifier_trigger",
        ),
        (
            lambda db: db.execute(
                "CREATE VIEW verifier_view AS SELECT id FROM notebooks"
            ),
            "verifier_view",
        ),
    ],
    ids=["empty-table", "column", "index-definition", "trigger", "view"],
)
def test_unmanifested_schema_changes_fail_verification(
    tmp_path, monkeypatch, mutation, expected_fragment
):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)

    result = _verify_with_repository_mutation(
        module, database, storage, mutation, monkeypatch
    )

    assert not result.ok
    assert expected_fragment in " ".join(result.discrepancies)


def test_fake_builtin_seed_row_fails_verification(tmp_path, monkeypatch):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)

    result = _verify_with_repository_mutation(
        module,
        database,
        storage,
        lambda db: db.execute(
            "INSERT INTO concept_whitelist (term, note, created_at) "
            "VALUES ('verifier-backdoor', 'builtin', 'private-row-content')"
        ),
        monkeypatch,
    )

    assert not result.ok
    assert "concept_whitelist" in result.changed_tables


@pytest.mark.parametrize("filename", ["snapshot?.db", "snapshot#.db", "snapshot%.db"])
def test_database_uri_punctuation_is_percent_encoded(tmp_path, filename):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    punctuated = database.with_name(filename)
    database.rename(punctuated)

    result = module.verify_snapshot(punctuated, storage)

    assert result.ok, result.discrepancies
    assert result.source_user_version == 9


def test_cleanup_failure_is_authoritative_and_reports_only_retained_path(
    tmp_path, monkeypatch, capsys
):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    retained: list[Path] = []

    def fail_cleanup(path, *args, **kwargs):
        retained.append(Path(path))
        raise OSError("private-row-content cleanup secret")

    monkeypatch.setattr(module.shutil, "rmtree", fail_cleanup)

    result = module.verify_snapshot(database, storage)
    module._print_report(result)
    output = capsys.readouterr().out

    assert not result.ok
    assert retained
    assert f"temporary_backup_retained={retained[0]}" in output
    assert "private-row-content" not in output


def test_primary_failure_is_preserved_when_cleanup_succeeds(tmp_path, monkeypatch):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)

    class ExplodingRepository:
        def __init__(self, _settings):
            raise RuntimeError("primary private row content")

    monkeypatch.setattr(module, "SQLiteRepository", ExplodingRepository)

    with pytest.raises(RuntimeError, match="primary private row content"):
        module.verify_snapshot(database, storage)


def test_cleanup_failure_wins_over_primary_failure_without_leaking_errors(
    tmp_path, monkeypatch
):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    retained: list[Path] = []

    class ExplodingRepository:
        def __init__(self, _settings):
            raise RuntimeError("primary private row content")

    def fail_cleanup(path, *args, **kwargs):
        retained.append(Path(path))
        raise OSError("cleanup private row content")

    monkeypatch.setattr(module, "SQLiteRepository", ExplodingRepository)
    monkeypatch.setattr(module.shutil, "rmtree", fail_cleanup)

    with pytest.raises(SystemExit) as caught:
        module.verify_snapshot(database, storage)

    assert retained
    assert str(caught.value) == (
        "repository-snapshot: FAIL "
        f"temporary_backup_retained={retained[0]}"
    )
    assert "primary private row content" not in str(caught.value)
    assert "cleanup private row content" not in str(caught.value)


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE knowledge_objects SET status='approved' WHERE id='ko-fixture-claim'",
        "DELETE FROM knowledge_relations WHERE id='rel-fixture'",
    ],
)
def test_row_mutation_or_deletion_after_open_fails_verification(
    tmp_path, monkeypatch, mutation
):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    real_repository = module.SQLiteRepository

    class MutatingRepository(real_repository):
        def __init__(self, settings):
            super().__init__(settings)
            with self._write() as db:
                db.execute(mutation)

    monkeypatch.setattr(module, "SQLiteRepository", MutatingRepository)
    result = module.verify_snapshot(database, storage)
    assert not result.ok
    assert result.changed_tables


def test_only_recovery_seed_and_admin_upgrade_fields_are_normalized(tmp_path):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    _insert_running_jobs(database)
    with sqlite3.connect(database) as db:
        # Stale admin credentials: the seed's in-place upgrade rewrites them.
        db.execute(
            "UPDATE users SET password_hash='stale', password_salt='stale', "
            "password_iterations=1, updated_at='2020-01-01T00:00:00' "
            "WHERE id='user-local'"
        )
        # Drop one builtin seed row from each seeded table: reseed must
        # restore them without counting as a data change.
        db.execute(
            "DELETE FROM object_schemas WHERE source='builtin' AND object_type="
            "(SELECT object_type FROM object_schemas WHERE source='builtin' LIMIT 1)"
        )
        db.execute(
            "DELETE FROM concept_whitelist WHERE term="
            "(SELECT term FROM concept_whitelist LIMIT 1)"
        )
    db.close()

    result = module.verify_snapshot(database, storage)

    assert result.ok, result.discrepancies
    assert result.changed_tables == []
    assert result.normalized["merge_review_jobs"] == 1
    assert result.normalized["ask_jobs"] == 1
    assert result.normalized["admin_upgraded"] == 1
    assert result.normalized["seeded_object_schemas"] >= 1
    assert result.normalized["seeded_concept_whitelist"] >= 1


def test_v22_kg_crash_recovery_is_normalized_field_by_field(tmp_path):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)

    upgraded = module.SQLiteRepository(
        module.offline_settings(database, tmp_path / "upgrade-storage")
    )
    upgraded.close_local()
    _insert_running_kg_build(database)

    result = module.verify_snapshot(database, storage)

    assert result.ok, result.discrepancies
    assert result.changed_tables == []
    assert result.normalized["sources"] == 1
    assert result.normalized["extraction_runs"] == 1
    assert result.normalized["kg_build_jobs"] == 1


def test_v22_kg_recovery_never_excuses_other_job_field_changes(
    tmp_path, monkeypatch
):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)

    upgraded = module.SQLiteRepository(
        module.offline_settings(database, tmp_path / "upgrade-storage")
    )
    upgraded.close_local()
    _insert_running_kg_build(database)
    real_repository = module.SQLiteRepository

    class TamperingRepository(real_repository):
        def __init__(self, settings):
            super().__init__(settings)
            with self._write() as db:
                db.execute(
                    "UPDATE kg_build_jobs SET mode='rebuild' "
                    "WHERE id='job-kg-crashed'"
                )

    monkeypatch.setattr(module, "SQLiteRepository", TamperingRepository)
    result = module.verify_snapshot(database, storage)

    assert not result.ok
    assert "kg_build_jobs" in result.changed_tables


def test_normalization_never_excuses_other_job_field_changes(tmp_path, monkeypatch):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    _insert_running_jobs(database)
    real_repository = module.SQLiteRepository

    class TamperingRepository(real_repository):
        def __init__(self, settings):
            super().__init__(settings)
            with self._write() as db:
                # Same rows recovery touches, but a non-allowed column.
                db.execute(
                    "UPDATE ask_jobs SET mode='graph' WHERE id='askjob-running'"
                )

    monkeypatch.setattr(module, "SQLiteRepository", TamperingRepository)
    result = module.verify_snapshot(database, storage)
    assert not result.ok
    assert "ask_jobs" in result.changed_tables


def test_stdout_never_includes_seeded_secrets(tmp_path, capsys):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    _insert_running_jobs(database)

    exit_code = module.main(
        ["--database", str(database), "--storage-dir", str(storage)]
    )
    out = capsys.readouterr()
    assert exit_code == 0
    combined = out.out + out.err
    assert "repository-snapshot: PASS schema=v9 changed_tables=0" in out.out
    for secret in FIXTURE_SECRETS:
        assert secret not in combined, f"stdout leaked seeded content: {secret!r}"


def test_original_storage_list_size_mtime_remain_unchanged(tmp_path):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    before_storage = _storage_stat(storage)
    before_db = (database.stat().st_size, database.stat().st_mtime_ns)

    result = module.verify_snapshot(database, storage)

    assert result.ok, result.discrepancies
    assert result.storage_unchanged is True
    assert _storage_stat(storage) == before_storage
    assert (database.stat().st_size, database.stat().st_mtime_ns) == before_db
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()


def test_missing_database_or_storage_is_a_hard_failure(tmp_path):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)
    with pytest.raises(SystemExit):
        module.verify_snapshot(tmp_path / "absent.db", storage)
    with pytest.raises(SystemExit):
        module.verify_snapshot(database, tmp_path / "absent-storage")


def test_hostile_model_environment_creates_no_configured_client_and_no_network(
    tmp_path, monkeypatch, capsys
):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)

    hostile_env = {
        "OPENAI_COMPAT_BASE_URL": "http://127.0.0.1:9/hostile",
        "OPENAI_COMPAT_API_KEY": "hostile-key",
        "OPENAI_COMPAT_MODEL": "hostile-model",
        "REASONING_LLM_BASE_URL": "http://127.0.0.1:9/hostile",
        "REASONING_LLM_API_KEY": "hostile-key",
        "REASONING_LLM_MODEL": "hostile-reasoning",
        "REWRITE_LLM_BASE_URL": "http://127.0.0.1:9/hostile",
        "REWRITE_LLM_API_KEY": "hostile-key",
        "REWRITE_LLM_MODEL": "hostile-rewrite",
        "KG_LLM_BASE_URL": "http://127.0.0.1:9/hostile",
        "KG_LLM_API_KEY": "hostile-key",
        "KG_LLM_MODEL": "hostile-kg",
        "EMBED_PROVIDER": "dashscope",
        "EMBED_BASE_URL": "http://127.0.0.1:9/hostile",
        "EMBED_API_KEY": "hostile-key",
        "EMBED_MODEL": "hostile-embed",
        "RERANK_MODEL": "hostile-rerank",
        "RERANK_BASE_URL": "http://127.0.0.1:9/hostile",
        "RERANK_API_KEY": "hostile-key",
        "MINERU_MODE": "http",
        "MINERU_API_URL": "http://127.0.0.1:9/hostile",
        "MINERU_API_TOKEN": "hostile-token",
        "SCALE_INDEX_AUTO_ENABLED": "true",
    }
    for key, value in hostile_env.items():
        monkeypatch.setenv(key, value)

    def no_network(*_args, **_kwargs):
        raise AssertionError("verifier attempted a network call")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)

    from app.core import llm as llm_module
    from app.services import embedding as embedding_module
    from app.services import mineru_client as mineru_module
    from app.services import mineru_cloud_client as mineru_cloud_module
    from app.services import rerank_client as rerank_module

    def guard_init(cls, label):
        real_init = cls.__init__

        def guarded(self, *args, **kwargs):
            real_init(self, *args, **kwargs)
            assert not self.configured, (
                f"hostile environment leaked into a configured {label} client"
            )

        return guarded

    monkeypatch.setattr(
        llm_module.OpenAICompatibleClient,
        "__init__",
        guard_init(llm_module.OpenAICompatibleClient, "LLM"),
    )
    monkeypatch.setattr(
        rerank_module.RerankClient,
        "__init__",
        guard_init(rerank_module.RerankClient, "rerank"),
    )
    monkeypatch.setattr(
        mineru_module.MinerUClient,
        "__init__",
        guard_init(mineru_module.MinerUClient, "MinerU"),
    )
    monkeypatch.setattr(
        mineru_cloud_module.MinerUCloudClient,
        "__init__",
        guard_init(mineru_cloud_module.MinerUCloudClient, "MinerU cloud"),
    )

    real_make_embedder = embedding_module.make_embedder

    def guarded_make_embedder(settings):
        embedder = real_make_embedder(settings)
        assert isinstance(embedder, embedding_module.FakeEmbedder), (
            "hostile environment leaked into a network embedder"
        )
        return embedder

    monkeypatch.setattr(embedding_module, "make_embedder", guarded_make_embedder)

    exit_code = module.main(
        ["--database", str(database), "--storage-dir", str(storage)]
    )
    out = capsys.readouterr()
    assert exit_code == 0
    assert "repository-snapshot: PASS schema=v9 changed_tables=0" in out.out
