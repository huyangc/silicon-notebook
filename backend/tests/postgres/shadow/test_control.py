from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest

from app.core.config import Settings
from app.migration.shadow.capture import install_sqlite_capture
from app.migration.shadow.control import (
    BackupEvidence,
    DiskEvidence,
    ShadowPhase,
    commit_guard_report,
    committed_sqlite_guard_report,
    create_or_reuse_run,
    enable_sqlite_capture_after_guard_reports,
    get_run,
    install_postgres_control_schema,
    preflight,
    prepare_postgres_replication_key_guards,
    update_forward_state,
)
from app.migration.shadow.manifest import MANIFEST
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.unified_kg_store import UnifiedKgStore
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SqliteMigrator


pytestmark = pytest.mark.postgres_integration
REPO_ROOT = Path(__file__).resolve().parents[4]
SECRET = b"postgres-shadow-confirmation-secret-32bytes"


@pytest.fixture
def source_database(tmp_path: Path):
    path = tmp_path / "source.db"
    storage = tmp_path / "storage"
    storage.mkdir()
    settings = Settings(database_url=f"sqlite:///{path}", _env_file=None)
    database = SqliteDatabase(settings, REPO_ROOT)
    SqliteMigrator(database, settings).migrate()
    try:
        with database.connect() as conn:
            yield settings.database_url, conn, storage
    finally:
        database.close_local()


@pytest.fixture
def clean_control_schema(postgres_scope):
    with psycopg.connect(postgres_scope.base_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS silicon_shadow CASCADE")
    try:
        yield
    finally:
        with psycopg.connect(postgres_scope.base_url, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS silicon_shadow CASCADE")


def _read_only_preflight(source_database, postgres_database):
    source_url, source_conn, storage = source_database
    return preflight(
        run_id="integration-forward-1",
        source_url=source_url,
        source_conn=source_conn,
        storage_root=storage,
        target=postgres_database,
        disk_evidence=DiskEvidence("integration-disk", 10_000_000_000, True),
        backup_evidence=BackupEvidence("integration-backup", True, True),
        confirmation_secret=SECRET,
        now=1_000,
    )


def test_complete_control_guard_and_capture_enable_protocol(
    source_database,
    postgres_database,
    clean_control_schema,
):
    source_url, source_conn, _ = source_database
    report = _read_only_preflight(source_database, postgres_database)
    assert report.target_initial_schema_version == 0

    # The preflight is intentionally against an empty target.  The formal
    # checksummed migrator is the only component that creates the v10 business
    # schema before control/guards are allowed.
    assert PostgresMigrator(postgres_database).migrate() == 10
    checksum = install_postgres_control_schema(postgres_database)
    assert len(checksum) == 64
    state = create_or_reuse_run(
        postgres_database,
        report,
        confirmation_token=report.confirmation_token,
        confirmation_secret=SECRET,
        now=1_001,
    )
    assert state.phase is ShadowPhase.OFF
    assert state.revision == 0

    install_sqlite_capture(
        source_conn,
        run_id=report.run_id,
        schema_epoch=1,
        manifest=MANIFEST,
    )
    sqlite_guard = committed_sqlite_guard_report(
        source_conn,
        source_url=source_url,
        manifest=MANIFEST,
        run_id=report.run_id,
    )
    postgres_guard = prepare_postgres_replication_key_guards(
        postgres_database,
        manifest=MANIFEST,
        run_id=report.run_id,
    )
    assert len(postgres_guard.definitions) == 4
    assert {definition.name for definition in postgres_guard.definitions} == {
        "shadow_uq_community_members_replication_key",
        "shadow_uq_kg_canonical_scratch_replication_key",
        "shadow_uq_kg_cluster_scratch_replication_key",
        "shadow_uq_knowledge_object_sources_replication_key",
    }

    state = commit_guard_report(
        postgres_database,
        report=sqlite_guard,
        expected_revision=0,
    )
    assert state.revision == 1
    with pytest.raises(ValueError, match="both committed"):
        enable_sqlite_capture_after_guard_reports(
            source_conn,
            postgres_database,
            source_url=source_url,
            sqlite_report=sqlite_guard,
            postgres_report=postgres_guard,
            manifest=MANIFEST,
        )
    assert (
        source_conn.execute("SELECT enabled FROM shadow_capture_control").fetchone()[0]
        == 0
    )

    state = commit_guard_report(
        postgres_database,
        report=postgres_guard,
        expected_revision=1,
    )
    assert state.revision == 2
    enable_sqlite_capture_after_guard_reports(
        source_conn,
        postgres_database,
        source_url=source_url,
        sqlite_report=sqlite_guard,
        postgres_report=postgres_guard,
        manifest=MANIFEST,
    )
    assert (
        source_conn.execute("SELECT enabled FROM shadow_capture_control").fetchone()[0]
        == 1
    )
    assert get_run(postgres_database, report.run_id).revision == 3
    assert get_run(postgres_database, report.run_id).capture_enabled is True

    with pytest.raises(ValueError, match="illegal"):
        update_forward_state(
            postgres_database,
            run_id=report.run_id,
            expected_phase=ShadowPhase.OFF,
            expected_revision=3,
            new_phase=ShadowPhase.CUTOVER_READONLY,
            progress={},
            source_url=source_url,
            source_conn=source_conn,
        )

    state = update_forward_state(
        postgres_database,
        run_id=report.run_id,
        expected_phase=ShadowPhase.OFF,
        expected_revision=3,
        new_phase=ShadowPhase.SQLITE_TO_POSTGRES,
        progress={"stage": "capture-enabled"},
        source_url=source_url,
        source_conn=source_conn,
    )
    assert state.phase is ShadowPhase.SQLITE_TO_POSTGRES
    assert state.revision == 4
    with pytest.raises(ValueError, match="CAS"):
        update_forward_state(
            postgres_database,
            run_id=report.run_id,
            expected_phase=ShadowPhase.SQLITE_TO_POSTGRES,
            expected_revision=3,
            new_phase=ShadowPhase.SQLITE_TO_POSTGRES,
            progress={"stage": "forward"},
            source_url=source_url,
            source_conn=source_conn,
        )
    assert get_run(postgres_database, report.run_id).progress == {
        "stage": "capture-enabled"
    }


def test_control_schema_refuses_unknown_existing_schema(
    postgres_database,
    clean_control_schema,
):
    with postgres_database.write() as conn:
        conn.execute("CREATE SCHEMA silicon_shadow")
        conn.execute("CREATE TABLE silicon_shadow.unknown_object(id integer)")
    with pytest.raises(ValueError, match="checksum|unrecognized"):
        install_postgres_control_schema(postgres_database)


def test_postgres_guard_failure_rolls_back_all_four_and_retry_is_idempotent(
    source_database,
    postgres_database,
    clean_control_schema,
):
    report = _read_only_preflight(source_database, postgres_database)
    PostgresMigrator(postgres_database).migrate()
    install_postgres_control_schema(postgres_database)
    create_or_reuse_run(
        postgres_database,
        report,
        confirmation_token=report.confirmation_token,
        confirmation_secret=SECRET,
        now=1_001,
    )
    with postgres_database.write() as conn:
        conn.execute(
            "INSERT INTO kg_canonical_scratch(notebook_id,run_id,seed,canonical_id) "
            "VALUES ('nb','run','seed','a'),('nb','run','seed','b')"
        )
    with pytest.raises(ValueError, match="duplicate"):
        prepare_postgres_replication_key_guards(
            postgres_database,
            manifest=MANIFEST,
            run_id="integration-forward-1",
        )
    with postgres_database.connect() as conn:
        names = conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname=current_schema() "
            "AND indexname LIKE 'shadow_uq_%'"
        ).fetchall()
    assert names == []
    with postgres_database.write() as conn:
        conn.execute("DELETE FROM kg_canonical_scratch WHERE canonical_id='b'")
    first = prepare_postgres_replication_key_guards(
        postgres_database,
        manifest=MANIFEST,
        run_id="integration-forward-1",
    )
    second = prepare_postgres_replication_key_guards(
        postgres_database,
        manifest=MANIFEST,
        run_id="integration-forward-1",
    )
    assert first.report_hash == second.report_hash


def test_guard_rollout_preserves_ordered_query_results_and_explain_analyze(
    source_database,
    postgres_database,
    clean_control_schema,
):
    report = _read_only_preflight(source_database, postgres_database)
    PostgresMigrator(postgres_database).migrate()
    install_postgres_control_schema(postgres_database)
    create_or_reuse_run(
        postgres_database,
        report,
        confirmation_token=report.confirmation_token,
        confirmation_secret=SECRET,
        now=1_001,
    )
    with postgres_database.write() as conn:
        conn.execute(
            "INSERT INTO kg_cluster_scratch(notebook_id,run_id,object_id,seed) "
            "VALUES ('nb','run','z','s2'),('nb','run','a','s1')"
        )
        conn.execute(
            "INSERT INTO kg_canonical_scratch"
            "(notebook_id,run_id,seed,canonical_id) "
            "VALUES ('nb','run','s1','z'),('nb','run','s2','a')"
        )
        conn.execute(
            "INSERT INTO community_members"
            "(canonical_id,notebook_id,level,community_id) "
            "VALUES ('canonical','nb',1,'low'),('canonical','nb',2,'high')"
        )
        conn.execute(
            "INSERT INTO community_members"
            "(canonical_id,notebook_id,level,community_id) "
            "SELECT 'canonical-' || g::text, 'nb-' || (g % 200)::text, "
            "g % 8, 'community-' || g::text FROM generate_series(1,5000) g"
        )
        conn.execute(
            "INSERT INTO knowledge_object_sources(object_id,source_id,notebook_id) "
            "VALUES ('z','source','nb'),('a','source','nb')"
        )
        conn.execute(
            "ANALYZE kg_cluster_scratch, kg_canonical_scratch, "
            "community_members, knowledge_object_sources"
        )

    queries = (
        (
            "SELECT object_id,seed FROM kg_cluster_scratch "
            'WHERE notebook_id=%s AND run_id=%s ORDER BY object_id COLLATE "C"',
            ("nb", "run"),
        ),
        (
            "SELECT canonical_id FROM kg_canonical_scratch "
            "WHERE notebook_id=%s AND run_id=%s AND seed=%s "
            'ORDER BY canonical_id COLLATE "C"',
            ("nb", "run", "s1"),
        ),
        (
            "SELECT community_id FROM community_members "
            "WHERE notebook_id=%s AND canonical_id=%s "
            'ORDER BY level DESC, community_id COLLATE "C" ASC LIMIT 1',
            ("nb", "canonical"),
        ),
        (
            'SELECT DISTINCT object_id COLLATE "C" AS object_id '
            "FROM knowledge_object_sources "
            "WHERE source_id=%s AND notebook_id=%s ORDER BY object_id",
            ("source", "nb"),
        ),
    )

    def observations():
        with postgres_database.connect() as conn:
            result = []
            for query, parameters in queries:
                rows = conn.execute(query, parameters).fetchall()
                explain_row = conn.execute(
                    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query,
                    parameters,
                ).fetchone()
                result.append((rows, next(iter(explain_row.values()))[0]))
            return result

    before = observations()
    prepare_postgres_replication_key_guards(
        postgres_database,
        manifest=MANIFEST,
        run_id=report.run_id,
    )
    after = observations()
    assert [rows for rows, _plan in before] == [rows for rows, _plan in after]
    assert all(plan["Execution Time"] >= 0 for _rows, plan in before + after)
    assert all(plan["Plan"]["Actual Rows"] > 0 for _rows, plan in before + after)
    for _rows, community_plan in (before[2], after[2]):
        rendered = json.dumps(community_plan, sort_keys=True)
        assert "idx_commmem_nb_can" in rendered
        assert "shadow_uq_community_members_replication_key" not in rendered


def test_phase_enum_and_forward_scope_are_exact():
    assert {phase.value for phase in ShadowPhase} == {
        "off",
        "sqlite_to_postgres",
        "cutover_readonly",
        "postgres_to_sqlite",
        "retired",
    }


def test_postgres_scratch_streams_preserve_knowledge_ordinal_not_object_id(
    postgres_database,
):
    PostgresMigrator(postgres_database).migrate()
    with postgres_database.write() as conn:
        conn.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) VALUES "
            "('nb-order','ordering',clock_timestamp(),clock_timestamp())"
        )
        conn.execute(
            "INSERT INTO knowledge_objects"
            "(id,notebook_id,object_type,created_at,updated_at) VALUES "
            "('object-z','nb-order','concept',clock_timestamp(),clock_timestamp()),"
            "('object-a','nb-order','concept',clock_timestamp(),clock_timestamp()),"
            "('object-m','nb-order','concept',clock_timestamp(),clock_timestamp())"
        )
        conn.execute(
            "INSERT INTO kg_cluster_scratch(notebook_id,run_id,object_id,seed) VALUES "
            "('nb-order','run-order','object-a','seed-a'),"
            "('nb-order','run-order','object-m','seed-m'),"
            "('nb-order','run-order','object-z','seed-z')"
        )
        conn.execute(
            "INSERT INTO knowledge_embeddings(object_id,notebook_id,vector,created_at) VALUES "
            "('object-m','nb-order',decode('03','hex'),clock_timestamp()),"
            "('object-a','nb-order',decode('02','hex'),clock_timestamp()),"
            "('object-z','nb-order',decode('01','hex'),clock_timestamp())"
        )
    with postgres_database.connect() as conn:
        vector_rows = UnifiedKgStore.scratch_vector_rows(
            conn, "nb-order", "run-order"
        ).fetchall()
        scratch_rows = UnifiedKgStore.stream_scratch_rows(
            conn, "nb-order", "run-order"
        ).fetchall()
    assert [row["seed"] for row in vector_rows] == ["seed-z", "seed-a", "seed-m"]
    assert [row["object_id"] for row in scratch_rows] == [
        "object-z",
        "object-a",
        "object-m",
    ]


def test_concurrent_revision_cas_allows_exactly_one_winner(
    source_database, postgres_database, clean_control_schema
):
    report = _read_only_preflight(source_database, postgres_database)
    PostgresMigrator(postgres_database).migrate()
    install_postgres_control_schema(postgres_database)
    create_or_reuse_run(
        postgres_database,
        report,
        confirmation_token=report.confirmation_token,
        confirmation_secret=SECRET,
        now=1_001,
    )

    def attempt(marker: int):
        try:
            return update_forward_state(
                postgres_database,
                run_id=report.run_id,
                expected_phase=ShadowPhase.OFF,
                expected_revision=0,
                new_phase=ShadowPhase.OFF,
                progress={"stage": "preflight-confirmed", "applied_seq": marker},
            )
        except ValueError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, (1, 2)))
    assert sum(isinstance(result, Exception) for result in results) == 1
    assert get_run(postgres_database, report.run_id).revision == 1
