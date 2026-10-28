-- Hot-path batch 7: covering index for "this source's own paper metadata"
-- (SQLite twin: v103, ``_migration_103``).
--
-- A source_paper_meta row counts as its source's metadata only when its own
-- notebook_id is the source's notebook -- the reading report_source_rows,
-- the source-tab search leg and paper_meta_for_sources already use
-- (source_store.PAPER_META_OWN_ROW_SQL). The missing-metadata predicate
-- (PAPER_META_NO_META_SQL: the backfill queue, the paper_meta_missing button
-- probe, the analytics missing count) and the analytics has_meta / marker
-- counts check that once per source of the notebook. pk_source_paper_meta
-- carries only source_id, so every probe would visit the heap for
-- notebook_id (and is_paper); this index answers both from the index:
--
--   idx_source_paper_meta_source_nb ON source_paper_meta(source_id)
--     INCLUDE (notebook_id, is_paper)
--
-- Key on source_id alone: the readers join on source_id and test the
-- notebook with IS NOT DISTINCT FROM (a filter, not an equivalence-class
-- key), so the plans keep the pre-narrowing source_id-driven shapes and only
-- swap pk_source_paper_meta for this index. A (notebook_id, source_id) key
-- was measured too: the narrowing then becomes a notebook-range scan with a
-- per-row source_id join filter on mid-size notebooks (+80% on the EXISTS
-- probe).
--
-- Measured on a 49k-source notebook (39k metadata rows, production-wide
-- raw_json) beside 100 notebooks of 200 sources, the real reader statements
-- before (master SQL, no index) and after (narrowed SQL, this index), three
-- alternating blocks: the analytics counts (one pass now, two statements
-- before) -42% custom / -30% generic; the backfill list and the button probe
-- +5% / +6% custom (about 0.8 ms: index entries wider than the primary
-- key's) and within +-1% generic; the display joins, the activity feed and
-- the delete snapshot within the run-to-run spread. A 200-source notebook:
-- every reader within a few microseconds.
--
-- Write amplification: one more btree entry per source_paper_meta
-- INSERT/UPDATE of source_id / notebook_id / is_paper (one row per source;
-- upsert_paper_meta is the only writer). Because is_paper (and notebook_id)
-- are now indexed columns, an UPDATE that changes only is_paper -- a
-- re-extraction flipping the paper / not-a-paper verdict -- no longer
-- qualifies for a HOT update and writes a new index entry; at one row per
-- source and a handful of re-extractions per source, the volume is
-- negligible. Footprint: about the size of
-- pk_source_paper_meta plus the notebook ids. idx_source_paper_meta_nb
-- (0003) keeps serving the notebook-scoped readers and is not retired.
--
-- Rollback: `DROP INDEX CONCURRENTLY idx_source_paper_meta_source_nb;`. The
-- readers still run without it (they then visit the heap per probe).
--
-- Relationship to the offline CONCURRENTLY builder
-- (scripts/build_hotpath_indexes.py): same advisory-locked builder/inspector
-- as batches 1-6 (HOTPATH_INDEX_SPECS). On a database with production
-- traffic, run that script's --apply FIRST, online, with CREATE INDEX
-- CONCURRENTLY (this runner executes every migration inside a transaction,
-- where CONCURRENTLY cannot run); this migration is then a ledger no-op. On
-- a fresh deploy the IF NOT EXISTS clause is sufficient by itself.
--
-- Pre-existing same-named index validation: same DO-block pattern as
-- 0049 / 0051 (INVALID residue or a wrong-shape name collision fails the
-- migration loudly instead of being skipped by IF NOT EXISTS).
-- backend/tests/test_hotpath_indexes_batch7.py re-derives every expected value
-- below from HOTPATH_INDEX_SPECS and asserts it appears verbatim in this file.
DO $$
DECLARE
  rec record;
  existing record;
  actual_keys text[];
  actual_includes text[];
  actual_opclasses text[];
  actual_collations text[];
BEGIN
  FOR rec IN
    SELECT * FROM (VALUES
      ('idx_source_paper_meta_source_nb',
       'source_paper_meta', 'btree', false,
       ARRAY['source_id'],
       ARRAY['notebook_id', 'is_paper'],
       ARRAY['pg_catalog:text_ops'],
       ARRAY['pg_catalog:C'])
    ) AS v(index_name, expected_table, expected_am, expected_unique,
           expected_keys, expected_includes, expected_opclasses,
           expected_collations)
  LOOP
    SELECT i.indexrelid, i.indisvalid, i.indisready, i.indisunique,
           i.indnkeyatts, i.indnatts,
           i.indclass::oid[] AS opclass_oids,
           i.indcollation::oid[] AS collation_oids,
           am.amname,
           tbl.relname AS table_name
    INTO existing
    FROM pg_index i
    JOIN pg_class idx ON idx.oid = i.indexrelid
    JOIN pg_class tbl ON tbl.oid = i.indrelid
    JOIN pg_am am ON am.oid = idx.relam
    JOIN pg_namespace ns ON ns.oid = idx.relnamespace
    WHERE ns.nspname = current_schema() AND idx.relname = rec.index_name;
    IF NOT FOUND THEN
      CONTINUE;
    END IF;
    IF NOT existing.indisvalid OR NOT existing.indisready THEN
      RAISE EXCEPTION 'hotpath batch 7: pre-existing index % is INVALID (an interrupted CONCURRENTLY build left it unusable); run DROP INDEX CONCURRENTLY %, rerun scripts/build_hotpath_indexes.py --apply, then migrate again',
        rec.index_name, rec.index_name;
    END IF;
    actual_keys := ARRAY(
      SELECT btrim(regexp_replace(replace(
               lower(pg_get_indexdef(existing.indexrelid, n, true)),
               '::text', ''), '\s+', ' ', 'g'))
      FROM generate_series(1, existing.indnkeyatts) AS n ORDER BY n);
    actual_includes := ARRAY(
      SELECT btrim(regexp_replace(replace(
               lower(pg_get_indexdef(existing.indexrelid, n, true)),
               '::text', ''), '\s+', ' ', 'g'))
      FROM generate_series(existing.indnkeyatts + 1, existing.indnatts) AS n
      ORDER BY n);
    actual_opclasses := ARRAY(
      SELECT opc_ns.nspname || ':' || opc.opcname
      FROM unnest(existing.opclass_oids) WITH ORDINALITY op(oid, ord)
      JOIN pg_opclass opc ON opc.oid = op.oid
      JOIN pg_namespace opc_ns ON opc_ns.oid = opc.opcnamespace
      ORDER BY op.ord);
    actual_collations := ARRAY(
      SELECT COALESCE(coll_ns.nspname || ':' || coll.collname, '')
      FROM unnest(existing.collation_oids) WITH ORDINALITY co(oid, ord)
      LEFT JOIN pg_collation coll ON coll.oid = co.oid
      LEFT JOIN pg_namespace coll_ns ON coll_ns.oid = coll.collnamespace
      ORDER BY co.ord);
    IF existing.table_name <> rec.expected_table
       OR existing.amname <> rec.expected_am
       OR existing.indisunique <> rec.expected_unique
       OR existing.indnkeyatts <> array_length(rec.expected_keys, 1)
       OR existing.indnatts <> array_length(rec.expected_keys, 1)
            + COALESCE(array_length(rec.expected_includes, 1), 0)
       OR actual_keys <> rec.expected_keys
       OR actual_includes <> COALESCE(rec.expected_includes, ARRAY[]::text[])
       OR actual_opclasses <> rec.expected_opclasses
       OR actual_collations <> rec.expected_collations THEN
      RAISE EXCEPTION 'hotpath batch 7: pre-existing index % does not match the expected definition; resolve the name collision manually, then migrate again',
        rec.index_name;
    END IF;
  END LOOP;
END
$$;

CREATE INDEX IF NOT EXISTS idx_source_paper_meta_source_nb
  ON source_paper_meta(source_id)
  INCLUDE (notebook_id, is_paper);
