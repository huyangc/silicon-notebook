"""Conservative retention for the SQLite forward-shadow change log.

PostgreSQL owns the durable consumer, verification, replay, and poison
checkpoints.  Those facts are read in one repeatable-read transaction and the
resulting immutable bounds are then applied in a short SQLite write
transaction.  No PostgreSQL connection is held while SQLite is locked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.migration.shadow.identity import fingerprint_sqlite
from app.migration.shadow.manifest import MANIFEST, validate_sqlite_schema
from app.migration.shadow.types import Manifest


_FORWARD_DIRECTION = "sqlite_to_postgres"
_UNBOUNDED_SEQ = 2**63 - 1
DEFAULT_RETENTION_SECONDS = 7 * 24 * 60 * 60
DEFAULT_TAIL_EVENTS = 100_000
DEFAULT_DELETE_BATCH = 10_000


@dataclass(frozen=True)
class RetentionBounds:
    verified_checkpoint: int
    active_verifier_barrier: int | None
    replay_checkpoint: int | None
    poison_seq: int | None
    cutoff_utc: str

    @property
    def exclusive_seq_ceiling(self) -> int:
        """First sequence that must be retained, ignoring the tail window."""

        return min(
            self.verified_checkpoint + 1,
            self.active_verifier_barrier
            if self.active_verifier_barrier is not None
            else _UNBOUNDED_SEQ,
            self.replay_checkpoint
            if self.replay_checkpoint is not None
            else _UNBOUNDED_SEQ,
            self.poison_seq if self.poison_seq is not None else _UNBOUNDED_SEQ,
        )


@dataclass(frozen=True)
class RetentionReport:
    run_id: str
    deleted_rows: int
    source_high_water: int
    tail_floor: int
    exclusive_seq_ceiling: int
    verified_checkpoint: int
    cutoff_utc: str


def deletion_exclusive_ceiling(
    bounds: RetentionBounds, *, source_high_water: int, tail_events: int
) -> int:
    """Return the exclusive seq ceiling satisfying every sequence bound."""

    if isinstance(source_high_water, bool) or source_high_water < 0:
        raise ValueError("shadow retention source high water is invalid")
    if isinstance(tail_events, bool) or tail_events < 1:
        raise ValueError("shadow retention tail must be positive")
    # Keep seq >= max(seq)-tail_events.  Therefore only strictly older seqs
    # are eligible, even when the log contains gaps after an earlier cleanup.
    tail_floor = max(0, source_high_water - tail_events)
    return max(0, min(bounds.exclusive_seq_ceiling, tail_floor))


def _row_value(row: Any, name: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[name]
    return row[position]


def read_retention_bounds(
    target: Any,
    *,
    run_id: str,
    retention_seconds: int = DEFAULT_RETENTION_SECONDS,
) -> tuple[RetentionBounds, str, int]:
    """Read all target-side deletion bounds from one database snapshot."""

    if not run_id or run_id.strip() != run_id:
        raise ValueError("shadow retention run id is invalid")
    if (
        isinstance(retention_seconds, bool)
        or not 60 <= retention_seconds <= 365 * 24 * 60 * 60
    ):
        raise ValueError("shadow retention age is invalid")
    with target.connect() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        row = conn.execute(
            "SELECT r.source_identity_hash,r.schema_epoch,"
            "COALESCE((SELECT MAX(v.target_checkpoint) "
            "FROM silicon_shadow.verification_runs v "
            "WHERE v.run_id=r.run_id AND v.level IN ('full','cutover') "
            "AND v.status='complete' AND v.target_checkpoint IS NOT NULL),0) "
            "AS verified_checkpoint,"
            "(SELECT MIN(b.source_barrier) FROM silicon_shadow.verifier_barriers b "
            "WHERE b.run_id=r.run_id AND b.expires_at>clock_timestamp()) "
            "AS active_verifier_barrier,"
            "(SELECT MIN(c.source_seq) FROM silicon_shadow.retention_checkpoints c "
            "WHERE c.run_id=r.run_id) AS replay_checkpoint,"
            "(SELECT MIN(p.source_seq) FROM silicon_shadow.poison_events p "
            "WHERE p.run_id=r.run_id AND p.direction=%s) AS poison_seq,"
            "to_char((clock_timestamp() AT TIME ZONE 'UTC')-"
            "(%s * interval '1 second'),'YYYY-MM-DD HH24:MI:SS') AS cutoff_utc,"
            "COALESCE((SELECT last_seq FROM silicon_shadow.checkpoints k "
            "WHERE k.run_id=r.run_id AND k.direction=%s),-1) AS checkpoint "
            "FROM silicon_shadow.runs r WHERE r.run_id=%s",
            (_FORWARD_DIRECTION, retention_seconds, _FORWARD_DIRECTION, run_id),
        ).fetchone()
        if row is None:
            raise ValueError("shadow retention run does not exist")
        checkpoint = int(_row_value(row, "checkpoint", 7))
        if checkpoint < 0:
            raise ValueError("shadow retention forward checkpoint is missing")
        verified = int(_row_value(row, "verified_checkpoint", 2))
        if verified < 0 or verified > checkpoint:
            raise ValueError("shadow retention verification checkpoint is invalid")

        def optional_int(name: str, position: int) -> int | None:
            value = _row_value(row, name, position)
            if value is None:
                return None
            result = int(value)
            if result < 0:
                raise ValueError("shadow retention sequence bound is invalid")
            return result

        bounds = RetentionBounds(
            verified_checkpoint=verified,
            active_verifier_barrier=optional_int("active_verifier_barrier", 3),
            replay_checkpoint=optional_int("replay_checkpoint", 4),
            poison_seq=optional_int("poison_seq", 5),
            cutoff_utc=str(_row_value(row, "cutoff_utc", 6)),
        )
        return (
            bounds,
            str(_row_value(row, "source_identity_hash", 0)),
            int(_row_value(row, "schema_epoch", 1)),
        )


def retain_change_log(
    source: Any,
    target: Any,
    *,
    run_id: str,
    retention_seconds: int = DEFAULT_RETENTION_SECONDS,
    tail_events: int = DEFAULT_TAIL_EVENTS,
    delete_batch: int = DEFAULT_DELETE_BATCH,
    manifest: Manifest = MANIFEST,
) -> RetentionReport:
    """Delete one bounded eligible prefix without weakening any safety bound."""

    if isinstance(delete_batch, bool) or not 1 <= delete_batch <= 100_000:
        raise ValueError("shadow retention delete batch is invalid")
    bounds, expected_source_hash, expected_epoch = read_retention_bounds(
        target,
        run_id=run_id,
        retention_seconds=retention_seconds,
    )
    if expected_epoch != manifest.schema_pair.epoch:
        raise ValueError("shadow retention schema epoch changed")

    conn = source.connect()
    if conn.in_transaction:
        raise ValueError("shadow retention requires an idle SQLite connection")
    conn.execute("BEGIN IMMEDIATE")
    try:
        validate_sqlite_schema(conn, manifest, expected_pair=manifest.schema_pair)
        live_source = fingerprint_sqlite(source.settings.database_url, conn)
        if live_source.identity_hash != expected_source_hash:
            raise ValueError("shadow retention source binding changed")
        control = conn.execute(
            "SELECT enabled,write_frozen,apply_active,run_id,schema_epoch "
            "FROM shadow_capture_control WHERE singleton=1"
        ).fetchone()
        if control is None or tuple(control) != (
            1,
            0,
            0,
            run_id,
            manifest.schema_pair.epoch,
        ):
            raise ValueError("shadow retention capture binding is invalid")
        row = conn.execute(
            "SELECT COALESCE(MAX(seq),0) FROM shadow_change_log"
        ).fetchone()
        source_high_water = int(row[0])
        ceiling = deletion_exclusive_ceiling(
            bounds,
            source_high_water=source_high_water,
            tail_events=tail_events,
        )
        cursor = conn.execute(
            "DELETE FROM shadow_change_log WHERE seq IN ("
            "SELECT seq FROM shadow_change_log "
            "WHERE run_id=? AND seq<? AND captured_at<? "
            "ORDER BY seq LIMIT ?)",
            (run_id, ceiling, bounds.cutoff_utc, delete_batch),
        )
        deleted = int(cursor.rowcount)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return RetentionReport(
        run_id=run_id,
        deleted_rows=deleted,
        source_high_water=source_high_water,
        tail_floor=max(0, source_high_water - tail_events),
        exclusive_seq_ceiling=ceiling,
        verified_checkpoint=bounds.verified_checkpoint,
        cutoff_utc=bounds.cutoff_utc,
    )
