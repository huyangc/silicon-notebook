-- SQLite v85 parity (_migration_85). Export-watermark state the incremental
-- exporter reads back: whether the capture gate was open when the watermark
-- was written, the transaction snapshot that export ran under, and the index
-- that makes the next export's compensation pass a range scan.
--
-- Hand-written, unlike 0064: none of this DDL comes from
-- app/migration/sync/capture.py, so scripts/generate_sync_capture_migration.py
-- neither renders nor checks this file. See docs/incremental-sync-design.md
-- section 7.
--
-- captured records whether sync_capture_control was OPEN while this watermark
-- was being written. Only then does sync_change_log actually hold every write
-- made up to exported_through_seq, which is the precondition for reading the
-- next export as a window over the log instead of a full snapshot. It
-- defaults to false, so every watermark row that already exists when this
-- migration runs reads as "not captured" and the first export after the
-- upgrade is necessarily a full one -- correct, because those rows predate
-- the column and carry no snapshot either.
--
-- exported_snapshot stores pg_current_snapshot()::text as taken inside the
-- export's own read transaction. The next export needs it to compensate for
-- transactions that were still in flight then and committed afterwards: their
-- log rows carry a seq BELOW the stored watermark yet became visible only
-- later, so a plain "seq > watermark" window would skip them forever. It is
-- nullable because SQLite has no equivalent and leaves it NULL -- one writer
-- at a time means seq order and commit order are the same there and no such
-- gap exists.
ALTER TABLE sync_export_state
  ADD COLUMN captured boolean NOT NULL DEFAULT false,
  ADD COLUMN exported_snapshot text COLLATE "C";

-- The compensation pass reads "seq <= watermark AND txid >= the previous
-- snapshot's xmin" -- TWO ranges, in opposite directions, and neither column
-- can be an equality prefix for the other. txid leads because it is the
-- selective one (a small tail of recently-written transactions), and seq
-- rides along as a second key column so the "seq <= watermark" half is
-- evaluated inside the index scan. On a (txid) index alone every row of that
-- txid range has to be fetched from the heap before the seq half can discard
-- it, which is the whole window.
--
-- Partial on txid IS NOT NULL: on PostgreSQL every captured row has a txid,
-- so the predicate excludes nothing and "txid >= x" implies it (the planner's
-- predicate test derives IS NOT NULL from any strict operator clause), while
-- on SQLite txid is ALWAYS NULL, so the index stays empty and costs no B-tree
-- insert per captured write. The index is still created on both backends --
-- one catalog shape, no backend-conditional DDL for the snapshot verifier and
-- the PostgreSQL catalog guard to special-case.
CREATE INDEX idx_sync_change_log_txid
    ON sync_change_log (txid, seq) WHERE txid IS NOT NULL;

-- sync_export_runs is the in-flight export lease, one row per target
-- environment. Adapter-internal like every other sync_* table here (see
-- backend/app/migration/shadow/manifest.py's LOCAL_EPHEMERAL registration):
-- it describes a run happening in THIS environment right now and must never
-- travel through the shadow-migration copy path, a notebook deep-copy or a
-- sync package. It exists because an unfinished export is otherwise invisible
-- to the two things that would corrupt it:
--
--   * sync prune-log. The log rows a running export is about to read still
--     look prunable, because the watermark that run will publish does not
--     exist yet. The row is inserted BEFORE the export takes its read
--     snapshot, carrying floor_seq = the change log's MAX(seq) at that moment
--     -- a lower bound on the watermark this run will eventually publish.
--     prune-log takes its seq floor as the minimum over the captured
--     watermarks AND every live lease's floor_seq.
--   * a second unscoped export to the same target. Two of them race to
--     publish a watermark, and the loser's package describes a window the
--     winner's watermark already claims as exported. PRIMARY KEY (target_env)
--     IS the mutual exclusion: the second insert fails and that export
--     refuses.
--
-- heartbeat_at is refreshed by the export's existing staging heartbeat, so a
-- crashed run stops refreshing it; a row older than an hour is a DEAD lease,
-- which prune-log ignores (it must not hold the floor down forever) and a new
-- export replaces with a warning rather than refusing. The live row is
-- deleted in the same transaction that publishes the watermark, and on
-- failure or abort as well.
CREATE TABLE sync_export_runs (
  target_env text COLLATE "C" NOT NULL,
  run_id text COLLATE "C" NOT NULL,
  package_id text COLLATE "C" NOT NULL,
  started_at timestamp with time zone NOT NULL,
  heartbeat_at timestamp with time zone NOT NULL,
  floor_seq bigint NOT NULL DEFAULT 0,
  CONSTRAINT pk_sync_export_runs PRIMARY KEY (target_env)
);
