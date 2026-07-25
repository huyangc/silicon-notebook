from __future__ import annotations

import uuid

import pytest

from app.migration.shadow.replicator import Direction, WorkerLeaseUnavailable
from app.migration.shadow.retention import retain_change_log
from app.migration.shadow.worker import (
    acquire_worker_lease,
    heartbeat_worker_lease,
    release_worker_lease,
)
from tests.postgres.shadow.test_forward_replicator import (
    _replicator,
    _run,
    clean_control_schema,
    forward_run,
)


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_shadow_worker_lease"),
]
assert clean_control_schema and forward_run


def test_worker_lease_rejects_live_takeover_and_allows_database_expiry(forward_run):
    _source, target, run_id = forward_run
    first = uuid.uuid4().hex
    second = uuid.uuid4().hex
    direction = Direction.SQLITE_TO_POSTGRES

    acquire_worker_lease(
        target,
        run_id=run_id,
        direction=direction,
        worker_id=first,
        lease_seconds=30,
    )
    with pytest.raises(WorkerLeaseUnavailable):
        acquire_worker_lease(
            target,
            run_id=run_id,
            direction=direction,
            worker_id=second,
            lease_seconds=30,
        )
    assert heartbeat_worker_lease(
        target,
        run_id=run_id,
        direction=direction,
        worker_id=first,
        lease_seconds=30,
    )
    assert not release_worker_lease(
        target,
        run_id=run_id,
        direction=direction,
        worker_id=second,
    )

    with target.write() as conn:
        conn.execute(
            "UPDATE silicon_shadow.workers SET "
            "lease_expires_at=clock_timestamp()-interval '1 second' "
            "WHERE run_id=%s AND direction='sqlite_to_postgres' AND worker_id=%s",
            (run_id, first),
        )
    acquire_worker_lease(
        target,
        run_id=run_id,
        direction=direction,
        worker_id=second,
        lease_seconds=30,
    )
    assert not heartbeat_worker_lease(
        target,
        run_id=run_id,
        direction=direction,
        worker_id=first,
        lease_seconds=30,
    )
    assert release_worker_lease(
        target,
        run_id=run_id,
        direction=direction,
        worker_id=second,
    )


def test_retention_deletes_only_old_fully_verified_prefix_before_poison(forward_run):
    source, target, run_id = forward_run
    with source.write() as conn:
        conn.executemany(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES(?,?,CURRENT_TIMESTAMP)",
            [(f"retention-{number}", str(number)) for number in range(5)],
        )
    result = _run(_replicator(forward_run), run_id)
    assert result.checkpoint_after == 5
    with source.write() as conn:
        conn.execute(
            "UPDATE shadow_change_log SET captured_at=datetime('now','-8 days')"
        )
    with target.write() as conn:
        conn.execute(
            "INSERT INTO silicon_shadow.verification_runs"
            "(verification_id,run_id,level,status,source_barrier,target_checkpoint,completed_at) "
            "VALUES (%s,%s,'full','complete',5,5,clock_timestamp())",
            (uuid.uuid4().hex, run_id),
        )
        conn.execute(
            "INSERT INTO silicon_shadow.poison_events"
            "(run_id,direction,source_seq,category,safe_summary) "
            "VALUES (%s,'sqlite_to_postgres',3,'test','bounded test poison')",
            (run_id,),
        )

    report = retain_change_log(
        source,
        target,
        run_id=run_id,
        retention_seconds=7 * 24 * 60 * 60,
        tail_events=1,
    )
    assert report.deleted_rows == 2
    with source.connect() as conn:
        assert [row[0] for row in conn.execute(
            "SELECT seq FROM shadow_change_log ORDER BY seq"
        ).fetchall()] == [3, 4, 5]
