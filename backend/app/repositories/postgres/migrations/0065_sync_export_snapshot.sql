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
-- snapshot's xmin", so txid is the whole access path: a range scan over a
-- small tail of the log rather than a walk of the whole table. Created on
-- SQLite too, where txid is always NULL and the index is inert, so the two
-- backends' catalogs stay symmetric.
CREATE INDEX idx_sync_change_log_txid
    ON sync_change_log (txid);
