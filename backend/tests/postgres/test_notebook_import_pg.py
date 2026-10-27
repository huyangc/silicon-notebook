"""Notebook import (``.snbk``) on real PostgreSQL: the twin of
``tests/test_notebook_import.py`` (SQLite -> SQLite), running the same checks
(``tests/notebook_import_cases.py``) on the three directions that involve
PostgreSQL -- SQLite -> PostgreSQL, PostgreSQL -> SQLite, PostgreSQL ->
PostgreSQL -- plus every failure and recovery check on a PostgreSQL target."""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.repository_factory import create_repository
from app.services.sqlite_repository import SQLiteRepository
from tests import notebook_import_cases as cases
from tests.postgres.test_notebook_archive_export_pg import BACKEND as POSTGRES
from tests.test_notebook_archive_export import BACKEND as SQLITE

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_notebook_import"),
]


def _environment(monkeypatch, tmp_path, name: str, database_url: str) -> None:
    monkeypatch.setenv("SILICON_NOTEBOOK_ENV_FILE", "")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / f"{name}-storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    for key, value in cases.ENV.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def pg(postgres_scope, tmp_path, monkeypatch):
    _environment(monkeypatch, tmp_path, "pg", postgres_scope.url)
    repository = create_repository(Settings())
    try:
        yield repository
    finally:
        repository.close()


@pytest.fixture
def sqlite(pg, tmp_path, monkeypatch):
    """A SQLite deployment beside the PostgreSQL one (built after it)."""
    _environment(monkeypatch, tmp_path, "sqlite", f"sqlite:///{tmp_path / 'other.db'}")
    repository = SQLiteRepository(Settings())
    try:
        yield repository
    finally:
        repository.close()


@pytest.fixture
def out_dir(tmp_path):
    path = tmp_path / "out"
    path.mkdir()
    return path


@pytest.mark.parametrize("world", ["full", "memory"])
def test_round_trip_sqlite_to_postgres(sqlite, pg, out_dir, monkeypatch, world):
    cases.check_round_trip(sqlite, SQLITE, pg, out_dir, monkeypatch, world=world)


@pytest.mark.parametrize("world", ["full", "memory"])
def test_round_trip_postgres_to_sqlite(pg, sqlite, out_dir, monkeypatch, world):
    cases.check_round_trip(pg, POSTGRES, sqlite, out_dir, monkeypatch, world=world)


@pytest.mark.parametrize("world", ["full", "memory"])
def test_round_trip_postgres_to_postgres(pg, out_dir, monkeypatch, world):
    cases.check_round_trip(pg, POSTGRES, pg, out_dir, monkeypatch, world=world)


def test_naive_sqlite_timestamps_and_empty_sentinels_land_in_postgres(sqlite, pg, out_dir):
    cases.check_naive_sqlite_timestamps_land_as_utc(sqlite, SQLITE, pg, out_dir)


def test_importing_one_file_twice_makes_two_independent_notebooks_pg(pg, out_dir, monkeypatch):
    cases.check_importing_twice_makes_two_notebooks(pg, POSTGRES, pg, out_dir, monkeypatch)


def test_vectors_of_the_same_model_land_byte_for_byte_pg(sqlite, pg, out_dir, monkeypatch):
    cases.check_vectors_kept_byte_for_byte(sqlite, SQLITE, pg, out_dir, monkeypatch)


def test_knowhow_objects_end_with_vectors_after_the_reembed_legs_pg(sqlite, pg, out_dir, monkeypatch):
    cases.check_knowhow_objects_end_with_vectors(sqlite, SQLITE, pg, out_dir, monkeypatch)


def test_vectors_of_another_model_are_dropped_and_questions_embedded_again_pg(
    pg, out_dir, monkeypatch
):
    cases.check_vectors_of_another_model(pg, POSTGRES, pg, out_dir, monkeypatch)


@pytest.mark.filterwarnings("ignore:Duplicate name")
def test_tampered_archives_are_refused_and_leave_nothing_pg(pg, out_dir, monkeypatch):
    cases.check_tampered_archives_are_refused(pg, POSTGRES, out_dir, monkeypatch)


def test_attachment_mimes_and_source_links_a_serving_path_trusts_are_checked_pg(pg, out_dir):
    cases.check_unservable_values_are_refused(pg, POSTGRES, out_dir)


def test_dangling_ids_inside_json_are_cleared_and_counted_pg(pg, out_dir):
    cases.check_dangling_ids_inside_json_are_cleared(pg, POSTGRES, out_dir)


def test_every_dangling_reference_is_reported_under_its_label_pg(pg, out_dir, monkeypatch):
    cases.check_dangling_references_are_caught(pg, POSTGRES, out_dir, monkeypatch)


def test_a_crashed_import_is_reaped_by_the_startup_sweep_pg(pg, out_dir, monkeypatch):
    cases.check_crash_leaves_a_sentinel_the_sweep_reaps(pg, POSTGRES, out_dir, monkeypatch)


def test_a_large_failed_import_compensates_through_the_delete_job_pg(pg, out_dir, monkeypatch):
    cases.check_large_import_compensates_through_the_delete_job(pg, POSTGRES, out_dir, monkeypatch)


def test_knowhow_is_projected_before_the_reembed_legs_pg(pg, out_dir, monkeypatch):
    cases.check_post_publish_order(pg, POSTGRES, out_dir, monkeypatch)


def test_dry_run_writes_nothing_and_predicts_the_import_pg(pg, out_dir):
    cases.check_dry_run_writes_nothing(pg, POSTGRES, out_dir)


def test_cli_import_pg(pg, out_dir, capsys):
    cases.check_cli_import(pg, POSTGRES, out_dir, capsys, confirm=("--confirm-service-stopped",))


def test_cli_import_requires_the_stopped_service_on_postgres(pg, out_dir, capsys):
    """Writing on PostgreSQL needs ``--confirm-service-stopped`` (refused before any
    connection is opened); a dry run does not, since it writes nothing."""
    notebook_id = cases.seed_world(pg, POSTGRES, "full")
    path = out_dir / "unconfirmed.snbk"
    cases.export(pg, notebook_id, path)
    cases.ensure_importer(pg)
    before = cases.residue(pg)
    status, payload = cases._cli(["import", str(path), "--owner", cases.IMPORTER, "--json"], capsys)
    assert status == 2 and "--confirm-service-stopped" in payload["failed"][0]["error"]
    status, payload = cases._cli(
        ["import", str(path), "--owner", cases.IMPORTER, "--dry-run", "--json"], capsys
    )
    assert status == 0 and payload["imported"][0]["import"]["dry_run"] is True
    cases.assert_no_residue(pg, before)


def test_cluster_seed_ids_are_reminted_from_the_new_objects_pg(pg, out_dir):
    cases.check_cluster_seeds_are_reminted(pg, POSTGRES, out_dir)


@pytest.mark.parametrize("direction", ["sqlite_to_postgres", "postgres_to_sqlite", "postgres_to_postgres"])
def test_seed_space_travels_pg(pg, sqlite, out_dir, direction):
    src, src_backend, dst = {
        "sqlite_to_postgres": (sqlite, SQLITE, pg),
        "postgres_to_sqlite": (pg, POSTGRES, sqlite),
        "postgres_to_postgres": (pg, POSTGRES, pg),
    }[direction]
    cases.check_seed_space_travels(src, src_backend, dst, out_dir)


def test_heartbeat_due_during_file_extraction_finds_the_sentinel_pg(pg, out_dir, monkeypatch):
    cases.check_heartbeat_during_file_extraction(pg, POSTGRES, out_dir, monkeypatch)


def test_a_crash_while_extracting_files_is_reaped_pg(pg, out_dir, monkeypatch):
    cases.check_crash_while_extracting_is_reaped(pg, POSTGRES, out_dir, monkeypatch)


def test_deep_json_and_an_archive_changed_after_preflight_are_refused_pg(pg, out_dir, monkeypatch):
    cases.check_hostile_content_is_refused(pg, POSTGRES, out_dir, monkeypatch)


def test_object_types_follow_the_effective_definition_pg(sqlite, pg, out_dir):
    cases.check_object_types(sqlite, SQLITE, pg, out_dir)


def test_unfinished_sources_fail_and_orphan_attachments_are_dropped_pg(sqlite, pg, out_dir):
    cases.check_unfinished_sources_and_orphan_assets(sqlite, SQLITE, pg, out_dir)


def test_cli_reports_every_failure_pg(pg, out_dir, capsys, monkeypatch):
    cases.check_cli_reports_every_failure(
        pg, POSTGRES, out_dir, capsys, monkeypatch, confirm=("--confirm-service-stopped",)
    )


def test_dangling_checks_plan_as_anti_joins_pg(pg):
    """Each reference check of ``validate_import`` and ``validate_copy`` plans as an
    anti-join: no SubPlan (the ``NOT IN (subquery)`` shape PostgreSQL scans
    linearly once its hash outgrows work_mem)."""
    from app.repositories.postgres import sharing_store as module

    statements = [*module._IMPORT_DANGLING_SQL,
                  ("validate_copy", module._DANGLING_RELATION_ENDPOINTS_SQL)]
    with pg._runtime.database.connect() as db:
        for label, query in statements:
            params = ("nb-plan",) * query.count("%s")
            plan = "\n".join(
                row["QUERY PLAN"] for row in db.execute("EXPLAIN " + query, params).fetchall()
            )
            assert "Anti Join" in plan and "SubPlan" not in plan, (label, plan)
