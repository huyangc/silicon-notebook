from __future__ import annotations

import argparse
import threading
import time
import uuid
from pathlib import Path

import pytest

from app.core.config import Settings
from app.migration.shadow.cli import _preflight, _start_forward, _status, _verify
from app.migration.shadow.worker import ForwardWorker
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SqliteMigrator
from tests.postgres.shadow.test_forward_replicator import clean_control_schema


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_shadow_cli"),
]
REPO_ROOT = Path(__file__).resolve().parents[4]
assert clean_control_schema


def _args(**values):
    defaults = {"as_json": True, "work_dir": None}
    defaults.update(values)
    return argparse.Namespace(**defaults)


def test_cli_forward_flow_is_explicit_resumable_and_verifiable(
    tmp_path, postgres_scope, clean_control_schema
):
    run_id = f"cli-{uuid.uuid4().hex}"
    source_path = tmp_path / "source.db"
    storage = tmp_path / "storage"
    storage.mkdir()
    work_dir = tmp_path / run_id
    settings = Settings(
        database_url=f"sqlite:///{source_path}",
        shadow_database_url=postgres_scope.url,
        storage_dir=str(storage),
        postgres_pool_min_size=1,
        postgres_pool_max_size=4,
        _env_file=None,
    )
    source = SqliteDatabase(settings, REPO_ROOT)
    SqliteMigrator(source, settings).migrate()
    source.close()

    preflight = _preflight(
        _args(
            run_id=run_id,
            work_dir=str(work_dir),
            available_target_bytes=10_000_000_000,
            disk_evidence_id="cli-disk-evidence",
            backup_evidence_id="cli-backup-evidence",
            confirm_source_backup=True,
            confirm_target_restore=True,
        ),
        settings,
    )
    token = preflight["confirmation_token"]
    started = _start_forward(
        _args(run_id=run_id, work_dir=str(work_dir), confirmation_token=token),
        settings,
    )
    assert started["status"] == "ready"
    assert started["baseline_seq"] == 0

    source = SqliteDatabase(settings, REPO_ROOT)
    target_settings = settings.model_copy(
        update={"database_url": settings.shadow_database_url, "shadow_database_url": None}
    )
    target = PostgresDatabase(target_settings, REPO_ROOT)
    with source.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('cli-forward','replicated',CURRENT_TIMESTAMP)"
        )
    stop = threading.Event()
    worker = ForwardWorker(source, target, run_id=run_id)
    failure: list[BaseException] = []

    def run_worker() -> None:
        try:
            worker.run(stop)
        except BaseException as exc:
            failure.append(exc)

    thread = threading.Thread(target=run_worker)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with target.connect() as conn:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key='cli-forward'"
            ).fetchone()
        if row is not None:
            break
        time.sleep(0.05)
    else:
        pytest.fail("forward worker did not reach the target")
    stop.set()
    thread.join(10)
    assert not thread.is_alive()
    assert not failure

    current = _status(_args(run_id=run_id), settings)
    assert current["checkpoint"] == 1
    verified = _verify(
        _args(
            run_id=run_id,
            work_dir=str(work_dir),
            level="structural",
            confirmation_token=token,
            confirmation_token_file=None,
        ),
        settings,
    )
    assert verified["status"] == "complete"

    # Preflight is strictly read-only on first use, but a later call may safely
    # rebind and reissue a short-lived token for an existing identical run.
    reissued = _preflight(
        _args(
            run_id=run_id,
            work_dir=str(work_dir),
            available_target_bytes=None,
            disk_evidence_id=None,
            backup_evidence_id=None,
            confirm_source_backup=False,
            confirm_target_restore=False,
        ),
        settings,
    )
    assert reissued["reissued"] is True
    resumed = _start_forward(
        _args(
            run_id=run_id,
            work_dir=str(work_dir),
            confirmation_token=reissued["confirmation_token"],
        ),
        settings,
    )
    assert resumed["status"] == "ready"

    source.close()
    target.close()
