from __future__ import annotations

import sqlite3
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

from app.migration.shadow import replicator as replicator_module
from app.migration.shadow.replicator import (
    CapacityExceeded,
    Direction,
    OrderingBlocked,
    PoisonEventRecorded,
    RetryExhausted,
    ShadowReplicator,
    StaleFailureDiscarded,
    WorkerLeaseUnavailable,
    _MAX_DEFERRED_STATEMENT_FACTOR,
    _Apply,
    _BundleAccumulator,
    _Deferred,
    _EventFailure,
    _ParkStrategy,
    _Poison,
    _SourceEvent,
    _SourceBindingError,
    _StatementBudget,
    _TargetSnapshot,
    _UniqueSurface,
    _validate_existing_poison,
)
from app.migration.shadow.transform import PostgresColumn


class _Result:
    def __init__(self, *, row: Any = None, rows: list[Any] | None = None) -> None:
        self._row = row
        self._rows = rows or []

    def fetchone(self) -> Any:
        return self._row

    def fetchall(self) -> list[Any]:
        return self._rows


class _CandidateConnection:
    def __init__(
        self,
        rows: list[Any],
        *,
        query_error: type[psycopg.Error] | None = None,
    ) -> None:
        self._rows = iter(rows)
        self.query_error = query_error
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, statement: Any, parameters: tuple[Any, ...] = ()) -> _Result:
        self.calls.append((statement.as_string(), parameters))
        if self.query_error is not None:
            raise self.query_error("injected candidate query failure")
        return _Result(row=next(self._rows))


def _statement_counter() -> tuple[Callable[[], None], list[int]]:
    calls: list[int] = []

    def consume() -> None:
        calls.append(1)

    return consume, calls


def test_bigint_candidate_uses_indexable_composite_scope_without_gap_scan():
    replicator = ShadowReplicator(object(), object())
    surface = replicator._unique_surfaces["uq_knowhow_changes_table_seq"]
    conn = _CandidateConnection([{"lo": 10, "hi": 20}])
    consume, statements = _statement_counter()

    candidate = replicator._parking_candidate(
        conn,
        "business",
        surface,
        {"table_id": "table-a", "seq": 15},
        consume,
    )

    assert candidate == 9
    assert len(statements) == 1
    assert len(conn.calls) == 1
    statement, parameters = conn.calls[0]
    assert 'SELECT MIN("seq") AS lo,MAX("seq") AS hi' in statement
    assert '"table_id" = %s' in statement
    assert "SELECT DISTINCT" not in statement
    assert "lead(" not in statement
    assert parameters == ("table-a",)


def test_bigint_candidate_scans_a_gap_only_when_both_bounds_are_occupied():
    replicator = ShadowReplicator(object(), object())
    surface = replicator._unique_surfaces["uq_knowhow_changes_table_seq"]
    conn = _CandidateConnection(
        [
            {"lo": -(2**63), "hi": 2**63 - 1},
            {"candidate": 27},
        ]
    )
    consume, statements = _statement_counter()

    candidate = replicator._parking_candidate(
        conn,
        "business",
        surface,
        {"table_id": "table-b", "seq": 4},
        consume,
    )

    assert candidate == 27
    assert len(statements) == 2
    assert len(conn.calls) == 2
    gap_statement, gap_parameters = conn.calls[1]
    assert 'SELECT DISTINCT "seq" AS value' in gap_statement
    assert "lead(value::numeric)" in gap_statement
    assert '"table_id" = %s' in gap_statement
    assert conn.calls[0][1] == gap_parameters == ("table-b",)


def test_candidate_scope_uses_indexable_equality_and_explicit_nulls():
    replicator = ShadowReplicator(object(), object())
    surface = _UniqueSurface(
        "test_nullable_composite",
        "objects",
        ("notebook_id", "object_type", "rank"),
        _ParkStrategy.SENTINEL_BIGINT,
        "rank",
        None,
    )
    conn = _CandidateConnection([{"lo": 10, "hi": 20}])
    consume, _statements = _statement_counter()

    assert (
        replicator._parking_candidate(
            conn,
            "business",
            surface,
            {"notebook_id": None, "object_type": "claim", "rank": 11},
            consume,
        )
        == 9
    )
    statement, parameters = conn.calls[0]
    assert '"notebook_id" IS NULL' in statement
    assert '"object_type" = %s' in statement
    assert '"notebook_id" = %s' not in statement
    assert parameters == ("claim",)


def test_text_candidate_keeps_the_pinned_partial_unique_predicate():
    replicator = ShadowReplicator(object(), object())
    surface = replicator._unique_surfaces["idx_users_username"]
    conn = _CandidateConnection([{"candidate": "last\x01"}])
    consume, statements = _statement_counter()

    candidate = replicator._parking_candidate(
        conn,
        "business",
        surface,
        {"username": "wanted"},
        consume,
    )

    assert candidate == "last\x01"
    assert len(statements) == 1
    statement, parameters = conn.calls[0]
    assert 'MAX("username" COLLATE "C")' in statement
    assert "AND (username != '')" in statement
    assert parameters == ()


@pytest.mark.parametrize(
    "error_type",
    [psycopg.errors.ProgramLimitExceeded, psycopg.DataError],
)
def test_candidate_query_capacity_errors_remain_non_poison(error_type):
    replicator = ShadowReplicator(object(), object())
    surface = replicator._unique_surfaces["idx_users_username"]
    conn = _CandidateConnection([], query_error=error_type)
    consume, statements = _statement_counter()

    with pytest.raises(CapacityExceeded, match="candidate search"):
        replicator._parking_candidate(
            conn,
            "business",
            surface,
            {"username": "wanted"},
            consume,
        )

    assert len(statements) == len(conn.calls) == 1


class _ParkingConnection:
    def __init__(
        self,
        update_error: type[psycopg.Error] | None,
        *,
        conflicts: dict[str, str] | None = None,
    ) -> None:
        self.update_error = update_error
        self.conflicts = conflicts or {}
        self.calls: list[str] = []

    def execute(self, statement: Any, parameters: tuple[Any, ...] = ()) -> _Result:
        rendered = statement.as_string()
        self.calls.append(rendered)
        if rendered.startswith("SELECT"):
            source_id = str(parameters[-1])
            return _Result(rows=[{"id": self.conflicts.get(source_id, "conflict")}])
        if rendered.startswith("UPDATE") and self.update_error is not None:
            raise self.update_error("injected parking update failure")
        return _Result()


def _text_parking_items(
    replicator: ShadowReplicator, suffix: str = ""
) -> tuple[_Deferred, _Apply]:
    spec = next(item for item in replicator.manifest.replicated if item.name == "users")
    columns = (
        PostgresColumn("id", "text", False),
        PostgresColumn("username", "text", False),
    )
    blocked = _Apply(
        _SourceEvent(7, "users", "upsert", (("id", f"source{suffix}"),)),
        spec,
        columns,
        (f"source{suffix}", f"wanted{suffix}"),
        0,
    )
    restore = _Apply(
        _SourceEvent(7, "users", "upsert", (("id", f"conflict{suffix}"),)),
        spec,
        columns,
        (f"conflict{suffix}", f"replacement{suffix}"),
        0,
    )
    return _Deferred(blocked, "23505", "idx_users_username"), restore


@pytest.mark.parametrize(
    "error_type",
    [
        psycopg.errors.ProgramLimitExceeded,
        psycopg.DataError,
    ],
)
def test_text_parking_update_capacity_failure_rolls_back_before_non_poison(
    monkeypatch, error_type: type[psycopg.Error]
):
    replicator = ShadowReplicator(object(), object())
    blocked, restore = _text_parking_items(replicator)
    conn = _ParkingConnection(error_type)
    monkeypatch.setattr(
        replicator,
        "_parking_candidate",
        lambda *_args, **_kwargs: "parking\x01",
    )
    consume, statements = _statement_counter()

    with pytest.raises(CapacityExceeded, match="parking update"):
        replicator._park_unique_cycles(
            conn,
            "business",
            [blocked],
            {restore.event.identity: restore},
            set(),
            consume,
        )

    assert len(statements) == len(conn.calls) == 5
    assert conn.calls[-2:] == [
        'ROLLBACK TO SAVEPOINT "shadow_park_0"',
        'RELEASE SAVEPOINT "shadow_park_0"',
    ]


def test_unique_parking_update_collision_is_not_misclassified_as_capacity(
    monkeypatch,
):
    replicator = ShadowReplicator(object(), object())
    blocked, restore = _text_parking_items(replicator)
    conn = _ParkingConnection(psycopg.errors.UniqueViolation)
    monkeypatch.setattr(
        replicator,
        "_parking_candidate",
        lambda *_args, **_kwargs: "parking\x01",
    )
    parked: set[tuple[str, tuple[str, tuple[tuple[str, Any], ...]]]] = set()
    consume, statements = _statement_counter()

    restores = replicator._park_unique_cycles(
        conn,
        "business",
        [blocked],
        {restore.event.identity: restore},
        parked,
        consume,
    )

    assert len(statements) == len(conn.calls) == 5
    assert restores == ()
    assert parked == set()
    assert conn.calls[-2:] == [
        'ROLLBACK TO SAVEPOINT "shadow_park_0"',
        'RELEASE SAVEPOINT "shadow_park_0"',
    ]


def test_successful_parking_counts_every_executed_statement(monkeypatch):
    replicator = ShadowReplicator(object(), object())
    blocked, restore = _text_parking_items(replicator)
    conn = _ParkingConnection(None)
    monkeypatch.setattr(
        replicator,
        "_parking_candidate",
        lambda *_args, **_kwargs: "parking\x01",
    )
    parked: set[tuple[str, tuple[str, tuple[tuple[str, Any], ...]]]] = set()
    consume, statements = _statement_counter()

    restores = replicator._park_unique_cycles(
        conn,
        "business",
        [blocked],
        {restore.event.identity: restore},
        parked,
        consume,
    )

    assert restores == (restore,)
    assert parked == {("idx_users_username", restore.event.identity)}
    assert len(statements) == len(conn.calls) == 4
    assert conn.calls[0].startswith("SELECT")
    assert conn.calls[1] == 'SAVEPOINT "shadow_park_0"'
    assert conn.calls[2].startswith("UPDATE")
    assert conn.calls[3] == 'RELEASE SAVEPOINT "shadow_park_0"'


def test_one_stagnant_pass_parks_five_independent_unique_swaps(monkeypatch):
    replicator = ShadowReplicator(object(), object())
    pairs = [_text_parking_items(replicator, f"-{index}") for index in range(5)]
    blocked = [pair[0] for pair in pairs]
    restores = [pair[1] for pair in pairs]
    conflicts = {f"source-{index}": f"conflict-{index}" for index in range(5)}
    conn = _ParkingConnection(None, conflicts=conflicts)
    candidate = 0

    def next_candidate(*_args, **_kwargs):
        nonlocal candidate
        candidate += 1
        return f"parking-{candidate}"

    monkeypatch.setattr(replicator, "_parking_candidate", next_candidate)
    parked: set[tuple[str, tuple[str, tuple[tuple[str, Any], ...]]]] = set()
    consume, statements = _statement_counter()

    result = replicator._park_unique_cycles(
        conn,
        "business",
        blocked,
        {item.event.identity: item for item in restores},
        parked,
        consume,
    )

    assert result == tuple(restores)
    assert parked == {("idx_users_username", item.event.identity) for item in restores}
    assert len(statements) == len(conn.calls) == 5 * 4
    assert sum(call.startswith("UPDATE") for call in conn.calls) == 5


def test_statement_budget_strictly_blocks_the_next_execute():
    budget = _StatementBudget(2)
    executed: list[int] = []

    def execute() -> None:
        budget.consume()
        executed.append(1)

    execute()
    execute()
    with pytest.raises(OrderingBlocked, match="statement safety budget"):
        execute()

    assert budget.used == len(executed) == 2


def test_unique_surface_shapes_cover_null_leaf_partial_and_composite_plans():
    surfaces = ShadowReplicator(object(), object())._unique_surfaces

    nullable = surfaces["idx_notebooks_share_token"]
    assert (nullable.strategy.value, nullable.park_column, nullable.predicate) == (
        "null",
        "share_token",
        "share_token IS NOT NULL",
    )

    leaf = surfaces["idx_kg_build_jobs_one_running"]
    assert (leaf.strategy.value, leaf.park_column, leaf.predicate) == (
        "leaf_delete",
        None,
        "status = 'running'",
    )

    partial = surfaces["idx_users_username"]
    assert (partial.columns, partial.park_column, partial.predicate) == (
        ("username",),
        "username",
        "username != ''",
    )

    composite = surfaces["uq_knowhow_changes_table_seq"]
    assert (composite.columns, composite.park_column, composite.predicate) == (
        ("table_id", "seq"),
        "seq",
        None,
    )

    non_key_surfaces_per_table: dict[str, int] = {}
    for surface in surfaces.values():
        if surface.strategy.value == "replication_key":
            continue
        non_key_surfaces_per_table[surface.table] = (
            non_key_surfaces_per_table.get(surface.table, 0) + 1
        )
    max_surfaces = max(non_key_surfaces_per_table.values())
    assert max_surfaces == 2
    pessimistic_per_apply = 4 + max_surfaces * (5 + 4) + 3 + 1
    assert pessimistic_per_apply == 26 < _MAX_DEFERRED_STATEMENT_FACTOR == 32


def _metric_snapshot() -> _TargetSnapshot:
    return _TargetSnapshot(3, 8, "source", "target", "business", {})


def _metric_apply(replicator: ShadowReplicator) -> _Apply:
    spec = next(
        item for item in replicator.manifest.replicated if item.name == "app_settings"
    )
    return _Apply(
        _SourceEvent(4, "app_settings", "upsert", (("key", "safe"),)),
        spec,
        (PostgresColumn("key", "text", False),),
        ("safe",),
        17,
    )


def _coalescing_apply(
    replicator: ShadowReplicator,
    *,
    table: str,
    key_name: str,
    key_value: str,
    seq: int,
    operation: str,
    encoded_bytes: int,
    synthetic: bool = False,
) -> _Apply:
    spec = next(item for item in replicator.manifest.replicated if item.name == table)
    return _Apply(
        _SourceEvent(seq, table, operation, ((key_name, key_value),)),
        spec,
        (),
        None if operation == "delete" else (key_value,),
        encoded_bytes,
        synthetic,
    )


@pytest.mark.parametrize(
    "actual_operation,actual_bytes", [("delete", 0), ("upsert", 70)]
)
@pytest.mark.parametrize("actual_first", [False, True])
def test_actual_parent_contribution_overrides_synthetic_dependency_bytes(
    actual_operation, actual_bytes, actual_first
):
    replicator = ShadowReplicator(object(), object())
    synthetic_parent = _coalescing_apply(
        replicator,
        table="notebooks",
        key_name="id",
        key_value="parent",
        seq=1,
        operation="upsert",
        encoded_bytes=100,
        synthetic=True,
    )
    child = _coalescing_apply(
        replicator,
        table="answers",
        key_name="id",
        key_value="child",
        seq=1,
        operation="upsert",
        encoded_bytes=10,
    )
    actual_parent = _coalescing_apply(
        replicator,
        table="notebooks",
        key_name="id",
        key_value="parent",
        seq=2,
        operation=actual_operation,
        encoded_bytes=actual_bytes,
    )
    accumulator = _BundleAccumulator(1_000)

    if actual_first:
        assert accumulator.accept(1, (actual_parent,))
        assert accumulator.accept(2, (synthetic_parent, child))
        expected = [actual_parent, child]
    else:
        assert accumulator.accept(1, (synthetic_parent, child))
        assert accumulator.accept(2, (actual_parent,))
        expected = [child, actual_parent]

    assert accumulator.applies() == expected
    assert accumulator.total_bytes == child.encoded_bytes + actual_bytes


def test_replacement_that_grows_past_cap_rolls_back_with_other_accepted_bundle():
    replicator = ShadowReplicator(object(), object())
    small_a = _coalescing_apply(
        replicator,
        table="app_settings",
        key_name="key",
        key_value="a",
        seq=1,
        operation="upsert",
        encoded_bytes=10,
    )
    delete_b = _coalescing_apply(
        replicator,
        table="app_settings",
        key_name="key",
        key_value="b",
        seq=2,
        operation="delete",
        encoded_bytes=0,
    )
    large_b = _coalescing_apply(
        replicator,
        table="app_settings",
        key_name="key",
        key_value="b",
        seq=3,
        operation="upsert",
        encoded_bytes=100,
    )
    accumulator = _BundleAccumulator(50)

    assert accumulator.accept(1, (small_a,))
    assert accumulator.accept(2, (delete_b,))
    assert not accumulator.accept(3, (large_b,))
    assert accumulator.applies() == [small_a, delete_b]
    assert accumulator.total_bytes == 10
    assert not accumulator.oversized

    next_batch = _BundleAccumulator(50)
    assert next_batch.accept(3, (large_b,))
    assert next_batch.applies() == [large_b]
    assert next_batch.total_bytes == 100
    assert next_batch.oversized


def test_full_source_window_probes_the_immediate_suffix_gap():
    snapshot = _metric_snapshot()
    events = [_SourceEvent(4096, "users", "upsert", (("id", "last-in-window"),))]

    ShadowReplicator._validate_source_window(
        snapshot,
        events,
        row_count=4096,
        max_events=4096,
        source_max=4098,
        next_seq_present=True,
    )
    with pytest.raises(_EventFailure) as error:
        ShadowReplicator._validate_source_window(
            snapshot,
            events,
            row_count=4096,
            max_events=4096,
            source_max=4098,
            next_seq_present=False,
        )

    assert error.value.poison == _Poison(
        4097, "gap", "source sequence continuity failed"
    )
    assert (error.value.batch_events, error.value.source_max_seq) == (4096, 4098)


def test_suffix_gap_wins_before_a_legal_ordering_blocked_prefix(monkeypatch):
    replicator = ShadowReplicator(object(), object())
    snapshot = _metric_snapshot()
    events = [
        _SourceEvent(4, "users", "upsert", (("id", "swap-a"),)),
        _SourceEvent(5, "users", "upsert", (("id", "swap-b"),)),
    ]
    monkeypatch.setattr(replicator, "_target_snapshot", lambda *_args: snapshot)

    def suffix_gap(*_args, **_kwargs):
        replicator._validate_source_window(
            snapshot,
            events,
            row_count=2,
            max_events=10,
            source_max=6,
        )
        raise AssertionError("unreachable")

    applied = False

    def ordering_blocked(*_args, **_kwargs):
        nonlocal applied
        applied = True
        raise OrderingBlocked("swap")

    monkeypatch.setattr(replicator, "_read_source", suffix_gap)
    monkeypatch.setattr(replicator, "_apply_batch", ordering_blocked)
    monkeypatch.setattr(replicator, "_record_poison", lambda *_args: 0)

    with pytest.raises(PoisonEventRecorded) as error:
        replicator.run_batch(
            run_id="run",
            direction=Direction.SQLITE_TO_POSTGRES,
            max_events=10,
            max_bytes=1_000,
        )

    assert (error.value.source_seq, error.value.category) == (6, "gap")
    assert not applied
    metric = replicator.metrics.snapshot()[0]
    assert (metric.batch_events, metric.source_max_seq, metric.poison_count) == (
        2,
        6,
        1,
    )


def test_candidate_query_cancellation_retries_whole_batch_and_counts_metric(
    monkeypatch,
):
    replicator = ShadowReplicator(
        object(),
        object(),
        max_retries=2,
        base_backoff_seconds=0,
        max_backoff_seconds=0,
    )
    snapshot = _metric_snapshot()
    item = _metric_apply(replicator)
    surface = replicator._unique_surfaces["idx_users_username"]
    attempts = 0
    monkeypatch.setattr(replicator, "_target_snapshot", lambda *_args: snapshot)
    monkeypatch.setattr(
        replicator,
        "_read_source",
        lambda *_args, **_kwargs: ([item], 4, 4, False),
    )

    def apply(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        error = psycopg.errors.QueryCanceled if attempts == 1 else None
        conn = _CandidateConnection([{"candidate": "parking\x01"}], query_error=error)
        replicator._parking_candidate(
            conn,
            "business",
            surface,
            {"username": "wanted"},
            lambda: None,
        )
        return 4

    monkeypatch.setattr(replicator, "_apply_batch", apply)

    result = replicator.run_batch(
        run_id="run",
        direction=Direction.SQLITE_TO_POSTGRES,
        max_events=10,
        max_bytes=1_000,
    )

    assert (attempts, result.retries) == (2, 1)
    assert len(replicator.metrics.snapshot()) == 1
    assert replicator.metrics.snapshot()[0].retries == 1


def test_parking_update_cancellation_retries_whole_batch_and_counts_metric(
    monkeypatch,
):
    replicator = ShadowReplicator(
        object(),
        object(),
        max_retries=2,
        base_backoff_seconds=0,
        max_backoff_seconds=0,
    )
    snapshot = _metric_snapshot()
    item = _metric_apply(replicator)
    blocked, restore = _text_parking_items(replicator)
    attempts = 0
    connections: list[_ParkingConnection] = []
    monkeypatch.setattr(replicator, "_target_snapshot", lambda *_args: snapshot)
    monkeypatch.setattr(
        replicator,
        "_read_source",
        lambda *_args, **_kwargs: ([item], 4, 4, False),
    )
    monkeypatch.setattr(
        replicator,
        "_parking_candidate",
        lambda *_args, **_kwargs: "parking\x01",
    )

    def apply(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        error = psycopg.errors.QueryCanceled if attempts == 1 else None
        conn = _ParkingConnection(error)
        connections.append(conn)
        replicator._park_unique_cycles(
            conn,
            "business",
            [blocked],
            {restore.event.identity: restore},
            set(),
            lambda: None,
        )
        return 4

    monkeypatch.setattr(replicator, "_apply_batch", apply)

    result = replicator.run_batch(
        run_id="run",
        direction=Direction.SQLITE_TO_POSTGRES,
        max_events=10,
        max_bytes=1_000,
    )

    assert (attempts, result.retries) == (2, 1)
    assert connections[0].calls[-1].startswith("UPDATE")
    assert not any("ROLLBACK" in statement for statement in connections[0].calls)
    assert len(replicator.metrics.snapshot()) == 1
    assert replicator.metrics.snapshot()[0].retries == 1


def test_checkpoint_progress_mismatch_is_a_schema_failure():
    snapshot = _metric_snapshot()
    ShadowReplicator._validate_checkpoint_progress(
        SimpleNamespace(progress={"applied_seq": 3}), 3, snapshot
    )
    with pytest.raises(_EventFailure) as error:
        ShadowReplicator._validate_checkpoint_progress(
            SimpleNamespace(progress={"applied_seq": 4}), 3, snapshot
        )
    assert error.value.poison == _Poison(
        4, "schema", "shadow control progress does not match checkpoint"
    )
    assert error.value.snapshot is snapshot


def test_source_binding_error_maps_to_identity_without_message_matching():
    poison = ShadowReplicator._poison_for(
        _SourceBindingError(batch_events=2, source_max_seq=9), 4
    )

    assert poison == _Poison(4, "identity", "shadow run identity validation failed")


def test_source_disappearing_between_preflight_and_open_is_a_binding_failure(
    monkeypatch,
):
    replicator = ShadowReplicator(object(), object())
    monkeypatch.setattr(
        replicator_module,
        "open_fresh_live_sqlite",
        lambda *_args: (_ for _ in ()).throw(
            sqlite3.OperationalError("unable to open database file")
        ),
    )

    with pytest.raises(_SourceBindingError):
        replicator._read_source(
            _metric_snapshot(),
            run_id="run",
            max_events=1,
            max_bytes=1,
        )


def test_busy_source_open_remains_transient_and_retries(monkeypatch):
    attempts = 0
    replicator = ShadowReplicator(
        object(),
        object(),
        max_retries=2,
        base_backoff_seconds=0,
        max_backoff_seconds=0,
    )

    def busy_open(*_args):
        nonlocal attempts
        attempts += 1
        raise sqlite3.OperationalError("database is busy")

    monkeypatch.setattr(replicator_module, "open_fresh_live_sqlite", busy_open)

    with pytest.raises(RetryExhausted) as error:
        replicator._retry(
            lambda: replicator._read_source(
                _metric_snapshot(),
                run_id="run",
                max_events=1,
                max_bytes=1,
            )
        )

    assert error.value.retries == 2
    assert attempts == 3


@pytest.mark.parametrize(
    "existing",
    [
        {"source_seq": 2, "category": "conversion", "safe_summary": "safe"},
        {"source_seq": 1, "category": "schema", "safe_summary": "safe"},
        {"source_seq": 1, "category": "conversion", "safe_summary": "other"},
    ],
)
def test_existing_poison_accepts_only_an_exact_ack_replay(existing):
    poison = _Poison(1, "conversion", "safe")
    _validate_existing_poison(
        {"source_seq": 1, "category": "conversion", "safe_summary": "safe"},
        poison,
    )

    with pytest.raises(StaleFailureDiscarded, match="poison already exists"):
        _validate_existing_poison(existing, poison)


def test_successful_batch_emits_exactly_one_metric(monkeypatch):
    replicator = ShadowReplicator(object(), object())
    snapshot = _metric_snapshot()
    item = _metric_apply(replicator)
    monkeypatch.setattr(replicator, "_target_snapshot", lambda *_args: snapshot)
    monkeypatch.setattr(
        replicator,
        "_read_source",
        lambda *_args, **_kwargs: ([item], 9, 4, False),
    )
    monkeypatch.setattr(replicator, "_apply_batch", lambda *_args, **_kwargs: 4)

    result = replicator.run_batch(
        run_id="run",
        direction=Direction.SQLITE_TO_POSTGRES,
        max_events=10,
        max_bytes=1_000,
    )

    assert (result.events_read, result.rows_applied) == (1, 1)
    assert len(replicator.metrics.snapshot()) == 1
    metric = replicator.metrics.snapshot()[0]
    assert (metric.batch_events, metric.batch_rows, metric.batch_bytes) == (1, 1, 17)
    assert (metric.checkpoint_before, metric.checkpoint_after, metric.lag) == (3, 4, 5)


@pytest.mark.parametrize("error_type", [OrderingBlocked, CapacityExceeded])
def test_non_poison_batch_failures_emit_exactly_one_metric(monkeypatch, error_type):
    replicator = ShadowReplicator(object(), object())
    snapshot = _metric_snapshot()
    item = _metric_apply(replicator)
    monkeypatch.setattr(replicator, "_target_snapshot", lambda *_args: snapshot)
    monkeypatch.setattr(
        replicator,
        "_read_source",
        lambda *_args, **_kwargs: ([item], 20, 4, False),
    )

    def fail(*_args, **_kwargs):
        raise error_type("bounded failure")

    monkeypatch.setattr(replicator, "_apply_batch", fail)

    with pytest.raises(error_type):
        replicator.run_batch(
            run_id="run",
            direction=Direction.SQLITE_TO_POSTGRES,
            max_events=10,
            max_bytes=1_000,
        )

    assert len(replicator.metrics.snapshot()) == 1
    metric = replicator.metrics.snapshot()[0]
    assert (metric.batch_events, metric.batch_rows, metric.batch_bytes) == (1, 1, 17)
    assert (metric.checkpoint_before, metric.checkpoint_after, metric.lag) == (3, 3, 17)
    assert metric.poison_count == 0


def test_poison_metric_uses_actual_batch_events_and_poison_retries(monkeypatch):
    replicator = ShadowReplicator(object(), object())
    snapshot = _metric_snapshot()
    item = _metric_apply(replicator)
    monkeypatch.setattr(replicator, "_target_snapshot", lambda *_args: snapshot)
    monkeypatch.setattr(
        replicator,
        "_read_source",
        lambda *_args, **_kwargs: ([item], 100, 4, False),
    )

    def poison(*_args, **_kwargs):
        raise _EventFailure(_Poison(4, "constraint", "safe"), snapshot)

    monkeypatch.setattr(replicator, "_apply_batch", poison)
    monkeypatch.setattr(replicator, "_record_poison", lambda *_args: 2)

    with pytest.raises(PoisonEventRecorded):
        replicator.run_batch(
            run_id="run",
            direction=Direction.SQLITE_TO_POSTGRES,
            max_events=10,
            max_bytes=1_000,
        )

    assert len(replicator.metrics.snapshot()) == 1
    metric = replicator.metrics.snapshot()[0]
    assert metric.batch_events == 1
    assert metric.lag == 97
    assert metric.retries == 2
    assert metric.poison_count == 1


def test_retry_exhaustion_and_lease_failure_each_emit_one_metric(monkeypatch):
    retrying = ShadowReplicator(
        object(),
        object(),
        max_retries=2,
        base_backoff_seconds=0,
        max_backoff_seconds=0,
    )

    def transient(*_args):
        raise psycopg.OperationalError("transient")

    monkeypatch.setattr(retrying, "_target_snapshot", transient)
    with pytest.raises(RetryExhausted):
        retrying.run_batch(
            run_id="run",
            direction=Direction.SQLITE_TO_POSTGRES,
            max_events=10,
            max_bytes=1_000,
        )
    assert len(retrying.metrics.snapshot()) == 1
    assert retrying.metrics.snapshot()[0].retries == 2

    leased = ShadowReplicator(object(), object())

    def lease(*_args):
        raise WorkerLeaseUnavailable("leased")

    monkeypatch.setattr(leased, "_target_snapshot", lease)
    with pytest.raises(WorkerLeaseUnavailable):
        leased.run_batch(
            run_id="run",
            direction=Direction.SQLITE_TO_POSTGRES,
            max_events=10,
            max_bytes=1_000,
        )
    assert len(leased.metrics.snapshot()) == 1
    assert leased.metrics.snapshot()[0].retries == 0
