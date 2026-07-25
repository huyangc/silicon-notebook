from __future__ import annotations

from contextlib import contextmanager
import threading

import psycopg
import pytest
from psycopg_pool import PoolTimeout

from app.migration.shadow.replicator import (
    BatchResult,
    CrashInjected,
    Direction,
    OrderingBlocked,
    PoisonEventRecorded,
    RetryExhausted,
    ShadowReplicator,
    StaleFailureDiscarded,
    WorkerLeaseUnavailable,
    _Poison,
    _build_unique_surfaces,
    _fk_max_ancestor_closure,
    _is_transient,
    _validate_key_value,
)
from app.migration.shadow.manifest import MANIFEST
from app.repositories.postgres.database import PostgresPoolTimeout
from tests.postgres.shadow.test_forward_replicator import (
    _checkpoint,
    _replicator,
    _run,
    _target_setting,
    clean_control_schema,
    forward_run,
)


pytestmark = pytest.mark.postgres_integration

# Imported pytest fixtures must remain visible to this module's collector.
assert clean_control_schema and forward_run


@pytest.mark.parametrize(
    "boundary",
    [
        "before_target",
        "mid_batch",
        "before_checkpoint",
        "after_checkpoint_before_commit",
        "after_commit_before_next_source_read",
    ],
)
def test_crash_boundary_is_atomic_and_rerun_converges(forward_run, boundary: str):
    source, target, run_id = forward_run
    with source.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('crash','value',CURRENT_TIMESTAMP)"
        )
    fired = False

    def inject(current: str) -> None:
        nonlocal fired
        if current == boundary and not fired:
            fired = True
            raise CrashInjected()

    replicator = _replicator(forward_run, crash_injector=inject)
    with pytest.raises(CrashInjected):
        _run(replicator, run_id)
    assert fired
    committed = boundary == "after_commit_before_next_source_read"
    assert _checkpoint(target, run_id) == int(committed)
    assert (_target_setting(target, "crash") is not None) is committed

    result = _run(replicator, run_id)
    assert result.checkpoint_after == 1
    assert _target_setting(target, "crash")["value"] == "value"
    with target.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS count FROM app_settings WHERE key='crash'"
            ).fetchone()["count"]
            == 1
        )


def test_mid_batch_crash_rolls_back_every_statement_and_checkpoint(forward_run):
    source, target, run_id = forward_run
    with source.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('first','1',CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('second','2',CURRENT_TIMESTAMP)"
        )
    fired = False

    def inject(boundary: str) -> None:
        nonlocal fired
        if boundary == "mid_batch" and not fired:
            fired = True
            raise CrashInjected()

    replicator = _replicator(forward_run, crash_injector=inject)
    with pytest.raises(CrashInjected):
        _run(replicator, run_id)
    assert _checkpoint(target, run_id) == 0
    assert _target_setting(target, "first") is None
    assert _target_setting(target, "second") is None
    result = _run(replicator, run_id)
    assert result.checkpoint_after == 2
    assert _target_setting(target, "first") is not None
    assert _target_setting(target, "second") is not None


def test_transient_target_failure_retries_whole_transaction(forward_run):
    source, target, run_id = forward_run
    with source.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('retry','ok',CURRENT_TIMESTAMP)"
        )
    fired = False

    def transient(boundary: str) -> None:
        nonlocal fired
        if boundary == "before_target" and not fired:
            fired = True
            raise psycopg.OperationalError("injected safe transient")

    result = _run(_replicator(forward_run, crash_injector=transient), run_id)
    assert fired
    assert result.retries == 1
    assert _checkpoint(target, run_id) == 1
    assert _target_setting(target, "retry")["value"] == "ok"


def _raise_after_committed_apply(target, replicator, monkeypatch, *, on_retry=None):
    original_write = target.write
    armed = False
    raised = False

    def arm(boundary: str) -> None:
        nonlocal armed
        if boundary == "after_checkpoint_before_commit":
            armed = True

    replicator.crash_injector = arm

    @contextmanager
    def ambiguous_write(*args, **kwargs):
        nonlocal raised
        with original_write(*args, **kwargs) as conn:
            yield conn
        if armed and not raised:
            raised = True
            raise psycopg.OperationalError("commit acknowledgement lost")

    monkeypatch.setattr(target, "write", ambiguous_write)
    if on_retry is not None:
        replicator.sleep = lambda _seconds: on_retry()
    return lambda: raised


def test_commit_ack_loss_is_recognized_without_replay_or_poison(
    forward_run, monkeypatch
):
    source, target, run_id = forward_run
    with source.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('ambiguous','once',CURRENT_TIMESTAMP)"
        )
    replicator = _replicator(forward_run)
    was_raised = _raise_after_committed_apply(target, replicator, monkeypatch)
    result = _run(replicator, run_id)
    assert was_raised()
    assert result.retries == 1
    assert result.checkpoint_after == 1
    assert _target_setting(target, "ambiguous")["value"] == "once"
    with target.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS count FROM silicon_shadow.poison_events "
                "WHERE run_id=%s",
                (run_id,),
            ).fetchone()["count"]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) AS count FROM notebooks WHERE id='cascade-notebook'"
            ).fetchone()["count"]
            == 0
        )


def test_commit_ack_loss_does_not_bypass_a_new_worker_lease(
    forward_run, postgres_scope, monkeypatch
):
    source, target, run_id = forward_run
    with source.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('takeover','committed',CURRENT_TIMESTAMP)"
        )
    replicator = _replicator(forward_run)

    def take_over() -> None:
        with psycopg.connect(postgres_scope.url) as conn:
            conn.execute(
                "UPDATE silicon_shadow.workers SET worker_id='replacement',"
                "lease_expires_at=clock_timestamp()+interval '1 hour' "
                "WHERE run_id=%s AND direction='sqlite_to_postgres'",
                (run_id,),
            )

    was_raised = _raise_after_committed_apply(
        target, replicator, monkeypatch, on_retry=take_over
    )
    with pytest.raises(WorkerLeaseUnavailable):
        _run(replicator, run_id)
    assert was_raised()
    # The first transaction did commit; lease ownership only prevents this
    # stale worker from declaring/replaying it.
    assert _checkpoint(target, run_id) == 1
    assert _target_setting(target, "takeover")["value"] == "committed"
    with target.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS count FROM silicon_shadow.poison_events "
                "WHERE run_id=%s",
                (run_id,),
            ).fetchone()["count"]
            == 0
        )


def test_commit_ack_loss_does_not_bypass_target_rebinding(forward_run, monkeypatch):
    source, target, run_id = forward_run
    with source.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('target-rebind-ack','committed',CURRENT_TIMESTAMP)"
        )
    replicator = _replicator(forward_run)

    def rebind() -> None:
        with target.write() as conn:
            conn.execute(
                "UPDATE silicon_shadow.runs SET target_identity_hash='replacement' "
                "WHERE run_id=%s",
                (run_id,),
            )

    was_raised = _raise_after_committed_apply(
        target, replicator, monkeypatch, on_retry=rebind
    )
    with pytest.raises(StaleFailureDiscarded):
        _run(replicator, run_id)
    assert was_raised()
    assert _checkpoint(target, run_id) == 1
    assert _target_setting(target, "target-rebind-ack")["value"] == "committed"
    with target.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS count FROM silicon_shadow.poison_events WHERE run_id=%s",
                (run_id,),
            ).fetchone()["count"]
            == 0
        )


def test_poison_ack_loss_is_idempotent(forward_run, monkeypatch):
    _source, target, run_id = forward_run
    replicator = _replicator(forward_run)
    snapshot = replicator._target_snapshot(run_id, Direction.SQLITE_TO_POSTGRES)
    original_write = target.write
    raised = False

    @contextmanager
    def ambiguous_write(*args, **kwargs):
        nonlocal raised
        with original_write(*args, **kwargs) as conn:
            yield conn
        if not raised:
            raised = True
            raise psycopg.OperationalError("poison commit acknowledgement lost")

    monkeypatch.setattr(target, "write", ambiguous_write)
    replicator._record_poison(
        run_id,
        Direction.SQLITE_TO_POSTGRES,
        _Poison(1, "conversion", "source row conversion failed"),
        snapshot,
    )
    assert raised
    with target.connect() as conn:
        rows = conn.execute(
            "SELECT source_seq,category FROM silicon_shadow.poison_events WHERE run_id=%s",
            (run_id,),
        ).fetchall()
    assert [(row["source_seq"], row["category"]) for row in rows] == [(1, "conversion")]


def test_old_worker_cannot_publish_stale_poison_after_takeover(forward_run):
    _source, target, run_id = forward_run
    stale = _replicator(forward_run)
    snapshot = stale._target_snapshot(run_id, Direction.SQLITE_TO_POSTGRES)
    with target.write() as conn:
        conn.execute(
            "UPDATE silicon_shadow.workers SET worker_id='replacement',"
            "lease_expires_at=clock_timestamp()+interval '1 hour' "
            "WHERE run_id=%s AND direction='sqlite_to_postgres'",
            (run_id,),
        )
    with pytest.raises(WorkerLeaseUnavailable):
        stale._record_poison(
            run_id,
            Direction.SQLITE_TO_POSTGRES,
            _Poison(1, "conversion", "source row conversion failed"),
            snapshot,
        )
    with target.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS count FROM silicon_shadow.poison_events WHERE run_id=%s",
                (run_id,),
            ).fetchone()["count"]
            == 0
        )


def test_old_worker_reclaims_lease_but_stops_on_takeover_poison_before_apply(
    forward_run,
):
    source, target, run_id = forward_run
    with source.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('takeover-poison','must-not-apply',CURRENT_TIMESTAMP)"
        )
    stale = _replicator(forward_run)
    snapshot = stale._target_snapshot(run_id, Direction.SQLITE_TO_POSTGRES)
    applies, source_max, accepted_last_seq, _oversized = stale._read_source(
        snapshot,
        run_id=run_id,
        max_events=100,
        max_bytes=1_000_000,
    )

    with target.write() as conn:
        conn.execute(
            "UPDATE silicon_shadow.workers "
            "SET lease_expires_at=clock_timestamp()-interval '1 second' "
            "WHERE run_id=%s AND direction='sqlite_to_postgres'",
            (run_id,),
        )
    replacement = _replicator(forward_run)
    replacement._record_poison(
        run_id,
        Direction.SQLITE_TO_POSTGRES,
        _Poison(1, "conversion", "source row conversion failed"),
        snapshot,
    )
    with target.write() as conn:
        conn.execute(
            "UPDATE silicon_shadow.workers "
            "SET lease_expires_at=clock_timestamp()-interval '1 second' "
            "WHERE run_id=%s AND direction='sqlite_to_postgres'",
            (run_id,),
        )

    with pytest.raises(PoisonEventRecorded) as error:
        stale._apply_batch(
            snapshot,
            applies,
            run_id=run_id,
            direction=Direction.SQLITE_TO_POSTGRES,
            last_seq=accepted_last_seq,
            has_future_events=source_max > accepted_last_seq,
        )

    assert (error.value.source_seq, error.value.category) == (1, "existing")
    assert _checkpoint(target, run_id) == 0
    assert _target_setting(target, "takeover-poison") is None
    with target.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS count FROM silicon_shadow.poison_events "
                "WHERE run_id=%s AND direction='sqlite_to_postgres'",
                (run_id,),
            ).fetchone()["count"]
            == 1
        )


def test_old_worker_cannot_publish_a_different_seq_after_takeover_poison(
    forward_run,
):
    _source, target, run_id = forward_run
    stale = _replicator(forward_run)
    snapshot = stale._target_snapshot(run_id, Direction.SQLITE_TO_POSTGRES)
    with target.write() as conn:
        conn.execute(
            "UPDATE silicon_shadow.workers "
            "SET lease_expires_at=clock_timestamp()-interval '1 second' "
            "WHERE run_id=%s AND direction='sqlite_to_postgres'",
            (run_id,),
        )
    replacement = _replicator(forward_run)
    replacement._record_poison(
        run_id,
        Direction.SQLITE_TO_POSTGRES,
        _Poison(1, "conversion", "source row conversion failed"),
        snapshot,
    )
    with target.write() as conn:
        conn.execute(
            "UPDATE silicon_shadow.workers "
            "SET lease_expires_at=clock_timestamp()-interval '1 second' "
            "WHERE run_id=%s AND direction='sqlite_to_postgres'",
            (run_id,),
        )

    with pytest.raises(StaleFailureDiscarded, match="poison already exists"):
        stale._record_poison(
            run_id,
            Direction.SQLITE_TO_POSTGRES,
            _Poison(2, "schema", "shadow schema validation failed"),
            snapshot,
        )

    with target.connect() as conn:
        rows = conn.execute(
            "SELECT source_seq,category,safe_summary "
            "FROM silicon_shadow.poison_events "
            "WHERE run_id=%s AND direction='sqlite_to_postgres'",
            (run_id,),
        ).fetchall()
    assert [
        (row["source_seq"], row["category"], row["safe_summary"]) for row in rows
    ] == [(1, "conversion", "source row conversion failed")]


def test_checkpoint_progress_discards_stale_poison(forward_run):
    _source, target, run_id = forward_run
    replicator = _replicator(forward_run)
    snapshot = replicator._target_snapshot(run_id, Direction.SQLITE_TO_POSTGRES)
    with target.write() as conn:
        conn.execute(
            "UPDATE silicon_shadow.checkpoints SET last_seq=1,revision=revision+1 "
            "WHERE run_id=%s AND direction='sqlite_to_postgres'",
            (run_id,),
        )
    with pytest.raises(StaleFailureDiscarded):
        replicator._record_poison(
            run_id,
            Direction.SQLITE_TO_POSTGRES,
            _Poison(1, "conversion", "source row conversion failed"),
            snapshot,
        )
    with target.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS count FROM silicon_shadow.poison_events WHERE run_id=%s",
                (run_id,),
            ).fetchone()["count"]
            == 0
        )


def test_apply_rejects_source_binding_change_before_target_write(
    forward_run, monkeypatch
):
    source, target, run_id = forward_run
    with source.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('binding-change','x',CURRENT_TIMESTAMP)"
        )
    replicator = _replicator(forward_run)
    original_read = replicator._read_source

    def change_binding(*args, **kwargs):
        result = original_read(*args, **kwargs)
        with target.write() as conn:
            conn.execute(
                "UPDATE silicon_shadow.runs SET source_identity_hash='changed' "
                "WHERE run_id=%s",
                (run_id,),
            )
        return result

    monkeypatch.setattr(replicator, "_read_source", change_binding)
    with pytest.raises(StaleFailureDiscarded):
        _run(replicator, run_id)
    assert _checkpoint(target, run_id) == 0
    assert _target_setting(target, "binding-change") is None
    with target.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS count FROM silicon_shadow.poison_events WHERE run_id=%s",
                (run_id,),
            ).fetchone()["count"]
            == 0
        )


def test_apply_rejects_target_binding_change_before_target_write(
    forward_run, monkeypatch
):
    source, target, run_id = forward_run
    with source.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('target-binding-change','x',CURRENT_TIMESTAMP)"
        )
    replicator = _replicator(forward_run)
    original_read = replicator._read_source

    def change_binding(*args, **kwargs):
        result = original_read(*args, **kwargs)
        with target.write() as conn:
            conn.execute(
                "UPDATE silicon_shadow.runs SET target_identity_hash='changed' "
                "WHERE run_id=%s",
                (run_id,),
            )
        return result

    monkeypatch.setattr(replicator, "_read_source", change_binding)
    with pytest.raises(StaleFailureDiscarded):
        _run(replicator, run_id)
    assert _checkpoint(target, run_id) == 0
    assert _target_setting(target, "target-binding-change") is None
    with target.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS count FROM silicon_shadow.poison_events WHERE run_id=%s",
                (run_id,),
            ).fetchone()["count"]
            == 0
        )


def test_cascade_event_order_preserves_foreign_keys(forward_run):
    source, target, run_id = forward_run
    replicator = _replicator(forward_run)
    with source.write() as conn:
        conn.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "VALUES('cascade-notebook','N',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO answers(id,notebook_id,question,payload,created_at) "
            "VALUES('cascade-answer','cascade-notebook','q','{}',CURRENT_TIMESTAMP)"
        )
    inserted = _run(replicator, run_id)
    assert inserted.events_read == 2
    with source.write() as conn:
        conn.execute("DELETE FROM notebooks WHERE id='cascade-notebook'")
    deleted = _run(replicator, run_id)
    assert deleted.events_read >= 2
    with target.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS count FROM answers WHERE id='cascade-answer'"
            ).fetchone()["count"]
            == 0
        )


@pytest.mark.parametrize(
    "error",
    [
        psycopg.errors.ConnectionException(),
        PoolTimeout(),
        PostgresPoolTimeout("pool timeout"),
        psycopg.errors.SerializationFailure(),
        psycopg.errors.DeadlockDetected(),
        psycopg.errors.LockNotAvailable(),
        psycopg.errors.QueryCanceled(),
    ],
)
def test_transient_taxonomy_and_capped_backoff(error):
    assert _is_transient(error)
    sleeps: list[float] = []

    class FixedRandom:
        @staticmethod
        def random() -> float:
            return 1.0

    replicator = ShadowReplicator(
        object(),
        object(),
        max_retries=3,
        base_backoff_seconds=0.1,
        max_backoff_seconds=0.25,
        random_source=FixedRandom(),
        sleep=sleeps.append,
    )
    calls = 0

    def fail():
        nonlocal calls
        calls += 1
        raise error

    with pytest.raises(RetryExhausted):
        replicator._retry(fail)
    assert calls == 4
    assert sleeps == [0.1, 0.2, 0.25]


def test_retry_exhaustion_never_poison_or_advance(forward_run):
    source, target, run_id = forward_run
    with source.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('exhaust','x',CURRENT_TIMESTAMP)"
        )

    def fail(_boundary: str) -> None:
        raise psycopg.OperationalError("transient")

    replicator = _replicator(forward_run, crash_injector=fail, max_retries=2)
    with pytest.raises(RetryExhausted):
        _run(replicator, run_id)
    assert _checkpoint(target, run_id) == 0
    assert _target_setting(target, "exhaust") is None
    with target.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS count FROM silicon_shadow.poison_events "
                "WHERE run_id=%s",
                (run_id,),
            ).fetchone()["count"]
            == 0
        )


def test_worker_lease_is_exclusive_and_expired_lease_can_be_taken(forward_run):
    _source, target, run_id = forward_run
    first = _replicator(forward_run)
    second = _replicator(forward_run)
    assert _run(first, run_id).events_read == 0
    with pytest.raises(WorkerLeaseUnavailable):
        _run(second, run_id)
    with target.write() as conn:
        conn.execute(
            "UPDATE silicon_shadow.workers SET lease_expires_at=clock_timestamp()-interval '1 second' "
            "WHERE run_id=%s AND direction='sqlite_to_postgres'",
            (run_id,),
        )
    assert _run(second, run_id).events_read == 0


def test_apply_holds_ledger_and_business_write_exclusion_locks(
    forward_run, postgres_scope
):
    source, target, run_id = forward_run
    with source.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) "
            "VALUES('locked','x',CURRENT_TIMESTAMP)"
        )
    entered = threading.Event()
    release = threading.Event()

    def hold(boundary: str) -> None:
        if boundary == "before_target":
            entered.set()
            assert release.wait(5)

    replicator = _replicator(forward_run, crash_injector=hold)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            _run(replicator, run_id)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(5)
    try:
        statements = (
            "INSERT INTO app_settings(key,value,updated_at) VALUES('rogue','x',clock_timestamp())",
            "ALTER TABLE app_settings ADD COLUMN rogue text",
            "UPDATE silicon_schema_migrations SET checksum=checksum WHERE version=1",
        )
        for statement in statements:
            with psycopg.connect(postgres_scope.url) as conn:
                conn.execute("SET lock_timeout='100ms'")
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    conn.execute(statement)
                conn.rollback()
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and not errors
    assert _checkpoint(target, run_id) == 1


def test_run_forever_stops_after_idle_wait(monkeypatch):
    replicator = ShadowReplicator(object(), object(), idle_seconds=0)
    stop = threading.Event()
    calls = 0

    def once(**_kwargs):
        nonlocal calls
        calls += 1
        stop.set()
        return BatchResult(
            "run",
            Direction.SQLITE_TO_POSTGRES,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0.0,
        )

    monkeypatch.setattr(replicator, "run_batch", once)
    replicator.run_forever(
        run_id="run", direction=Direction.SQLITE_TO_POSTGRES, stop=stop
    )
    assert calls == 1


def test_run_forever_adaptively_widens_ordering_window(monkeypatch):
    replicator = ShadowReplicator(object(), object(), idle_seconds=0)
    stop = threading.Event()
    calls: list[tuple[int, int]] = []

    def widen(**kwargs):
        calls.append((kwargs["max_events"], kwargs["max_bytes"]))
        if kwargs["max_events"] == 256:
            raise OrderingBlocked("wider window required")
        stop.set()
        return BatchResult(
            kwargs["run_id"],
            kwargs["direction"],
            0,
            1,
            1,
            1,
            1,
            1,
            0,
            0.0,
        )

    monkeypatch.setattr(replicator, "run_batch", widen)
    replicator.run_forever(
        run_id="run", direction=Direction.SQLITE_TO_POSTGRES, stop=stop
    )
    assert calls == [(256, 8 * 1024 * 1024), (512, 16 * 1024 * 1024)]


def test_run_forever_stops_at_hard_ordering_window(monkeypatch):
    replicator = ShadowReplicator(object(), object(), idle_seconds=0)
    calls: list[tuple[int, int]] = []

    def blocked(**kwargs):
        calls.append((kwargs["max_events"], kwargs["max_bytes"]))
        raise OrderingBlocked("still blocked")

    monkeypatch.setattr(replicator, "run_batch", blocked)
    with pytest.raises(OrderingBlocked, match="still blocked"):
        replicator.run_forever(
            run_id="run",
            direction=Direction.SQLITE_TO_POSTGRES,
            stop=threading.Event(),
        )
    assert calls[-1] == (4096, 64 * 1024 * 1024)
    assert len(calls) == 5


def test_fixed_v9_unique_surfaces_have_total_safe_parking_plans():
    surfaces = _build_unique_surfaces(MANIFEST)
    counts: dict[str, int] = {}
    for surface in surfaces.values():
        counts[surface.strategy.value] = counts.get(surface.strategy.value, 0) + 1
    assert len(surfaces) == 82
    assert counts == {
        "replication_key": 60,
        "sentinel_bigint": 8,
        "sentinel_text": 6,
        "leaf_delete": 5,
        "null": 3,
    }
    assert {
        surface.name
        for surface in surfaces.values()
        if surface.strategy.value == "leaf_delete"
    } == {
        "idx_kg_build_jobs_one_running",
        "uq_knowhow_cell_code_row_id_column_id",
        "uq_knowhow_cells_row_id_column_id",
        "uq_memory_provenance_memory_id",
        "uq_memory_revisions_memory_id_revision",
    }


def test_fixed_v9_fk_dag_stays_below_runtime_closure_cap():
    assert _fk_max_ancestor_closure() == 9
    assert _fk_max_ancestor_closure() < 64


def test_sqlite_integer_replication_key_is_explicitly_int64_bounded():
    _validate_key_value("INTEGER", -(2**63))
    _validate_key_value("INTEGER", 2**63 - 1)
    with pytest.raises(ValueError, match="int64"):
        _validate_key_value("INTEGER", 2**63)
    with pytest.raises(ValueError, match="int64"):
        _validate_key_value("INTEGER", -(2**63) - 1)


@pytest.mark.parametrize(
    ("max_events", "max_bytes"),
    [
        (4097, 1),
        (1, 64 * 1024 * 1024 + 1),
    ],
)
def test_batch_limits_have_hard_upper_bounds(max_events: int, max_bytes: int):
    replicator = ShadowReplicator(object(), object())
    with pytest.raises(ValueError, match="between one and"):
        replicator.run_batch(
            run_id="run",
            direction=Direction.SQLITE_TO_POSTGRES,
            max_events=max_events,
            max_bytes=max_bytes,
        )


def test_one_replicator_instance_rejects_reentrant_batches(monkeypatch):
    replicator = ShadowReplicator(object(), object())
    entered = threading.Event()
    release = threading.Event()
    calls: list[tuple[int, int]] = []

    def hold(**kwargs):
        calls.append((kwargs["max_events"], kwargs["max_bytes"]))
        entered.set()
        assert release.wait(5)
        return BatchResult(
            kwargs["run_id"],
            kwargs["direction"],
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0.0,
        )

    monkeypatch.setattr(replicator, "_run_batch", hold)
    errors: list[BaseException] = []

    def first() -> None:
        try:
            replicator.run_batch(
                run_id="run",
                direction=Direction.SQLITE_TO_POSTGRES,
                max_events=1,
                max_bytes=10,
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=first)
    thread.start()
    assert entered.wait(5)
    try:
        with pytest.raises(WorkerLeaseUnavailable, match="active batch"):
            replicator.run_batch(
                run_id="run",
                direction=Direction.SQLITE_TO_POSTGRES,
                max_events=2,
                max_bytes=20,
            )
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and not errors
    assert calls == [(1, 10)]
