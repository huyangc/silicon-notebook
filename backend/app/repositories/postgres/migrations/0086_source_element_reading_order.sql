-- Hot-path batch 9: a source's elements in reading order past 9 999 elements
-- (SQLite twin: v105, ``_migration_105``). A pre-existing defect fix.
--
-- Parsed elements are minted el-<source_id>-NNNN (four-digit zero padding,
-- source_ingestion) and every reader ordered them by (created_at, id COLLATE
-- "C"), or by id alone. That is the document order only while every index has
-- four digits: from the 10 000th element on, ...-10000 sorts between ...-1000
-- and ...-1001 and ...-11340 between ...-1134 and ...-1135, so the source
-- reader, the keyset walks (embedding, KG extraction), positional reads,
-- chunking, the navigation heading pass, the catalog preview and the image
-- backfill plan all read such a source out of order.
--
-- The readers now order by created_at, <index digits>, id
-- (app/repositories/element_reading_order.py): the number of leading digits
-- after el-<source_id>- (0 for any other id shape), computed from id and
-- source_id alone. A source of at most 9 999 parsed elements has a constant
-- 4 there and reads byte for byte as before; knowhow / promotion / copied ids
-- have a constant 0 and read as before. No column, no backfill: existing rows
-- read in the right order the moment this index exists.
--
-- The two indexes that served (created_at, id) are REPLACED by successors with
-- the digits term before id, so every reader keeps its index range walk:
--
--   idx_source_elements_reading_order
--     ON source_elements(source_id, created_at, <digits>, id)
--     replaces idx_source_elements_source_created (0003): full read, keyset
--     walk (its row comparison is an Index Cond), offset/anchor page, both
--     positional-read shapes, the first-excerpt probe.
--   idx_source_elements_type_reading_order
--     ON source_elements(source_id, element_type, created_at, <digits>, id)
--     replaces idx_source_elements_source_type (0015): typed-collection
--     keyset enumeration, and the per-type counts (still Index Only Scans on
--     the (source_id, element_type) prefix).
--
-- Every prefix the old indexes served (source_id; source_id, created_at --
-- MAX(created_at) per source; source_id, element_type) is a prefix of a
-- successor, so the old two are DROPPED here (IF EXISTS) rather than kept as
-- write amplification on the largest table. The readers avoid evaluating the
-- digits expression per row wherever it cannot change the order (keyset
-- continuations order by id inside one (created_at, digits) range; id-ordered
-- readers keep their old statement after a two-row probe). Measured against
-- master in alternating runs (415k elements: one 15 003-element source, one of
-- 9 000, 2 000 of 200; PR description): sources of at most 9 999 elements read
-- within about +-10% / +0.15 ms except typed-collection walks spanning several
-- pages (+0.2 to +0.4 ms: one extra statement on the last page); deep pages are
-- faster (OFFSET 12 000: 9.2 -> 2.4 ms). Entries are 4 bytes wider.
--
-- Write amplification: unchanged in count (two indexes replace two); each
-- entry evaluates the digits expression once on insert (string functions over
-- two short ids). Footprint: about the old two plus 4 bytes per entry.
--
-- Relationship to the offline CONCURRENTLY builder
-- (scripts/build_hotpath_indexes.py): batch 9 of HOTPATH_INDEX_SPECS. On a
-- database with production traffic, run that script's --apply FIRST (online
-- CREATE INDEX CONCURRENTLY of the two successors), then
-- `DROP INDEX CONCURRENTLY idx_source_elements_source_created;` and
-- `DROP INDEX CONCURRENTLY idx_source_elements_source_type;`, then migrate --
-- this migration is then a ledger no-op (same flow as 0051). On a fresh deploy
-- the in-transaction create/drop below is enough by itself.
--
-- Rollback: rebuild the two old indexes CONCURRENTLY (definitions in 0003 /
-- 0015) and revert the readers; the successors can then be dropped.
--
-- Pre-existing same-named index validation: same DO-block pattern as 0084.
-- backend/tests/test_hotpath_indexes_batch9.py re-derives every expected value
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
      ('idx_source_elements_reading_order',
       'source_elements', 'btree', false,
       ARRAY['source_id', 'created_at', '( case when substr(id, 1, length(source_id) + 4) = ((''el-'' || source_id) || ''-'') then length(substr(id, length(source_id) + 5)) - length(ltrim(substr(id, length(source_id) + 5), ''0123456789'')) else 0 end)', 'id'],
       ARRAY[]::text[],
       ARRAY['pg_catalog:text_ops', 'pg_catalog:timestamptz_ops', 'pg_catalog:int4_ops', 'pg_catalog:text_ops'],
       ARRAY['pg_catalog:C', '', '', 'pg_catalog:C']),
      ('idx_source_elements_type_reading_order',
       'source_elements', 'btree', false,
       ARRAY['source_id', 'element_type', 'created_at', '( case when substr(id, 1, length(source_id) + 4) = ((''el-'' || source_id) || ''-'') then length(substr(id, length(source_id) + 5)) - length(ltrim(substr(id, length(source_id) + 5), ''0123456789'')) else 0 end)', 'id'],
       ARRAY[]::text[],
       ARRAY['pg_catalog:text_ops', 'pg_catalog:text_ops', 'pg_catalog:timestamptz_ops', 'pg_catalog:int4_ops', 'pg_catalog:text_ops'],
       ARRAY['pg_catalog:C', 'pg_catalog:C', '', '', 'pg_catalog:C'])
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
      RAISE EXCEPTION 'hotpath batch 9: pre-existing index % is INVALID (an interrupted CONCURRENTLY build left it unusable); run DROP INDEX CONCURRENTLY %, rerun scripts/build_hotpath_indexes.py --apply, then migrate again',
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
      RAISE EXCEPTION 'hotpath batch 9: pre-existing index % does not match the expected definition; resolve the name collision manually, then migrate again',
        rec.index_name;
    END IF;
  END LOOP;
END
$$;

CREATE INDEX IF NOT EXISTS idx_source_elements_reading_order
  ON source_elements(source_id, created_at, (CASE WHEN substr(id,1,length(source_id)+4)=('el-'||source_id||'-') THEN length(substr(id,length(source_id)+5))-length(ltrim(substr(id,length(source_id)+5),'0123456789')) ELSE 0 END), id);

CREATE INDEX IF NOT EXISTS idx_source_elements_type_reading_order
  ON source_elements(source_id, element_type, created_at, (CASE WHEN substr(id,1,length(source_id)+4)=('el-'||source_id||'-') THEN length(substr(id,length(source_id)+5))-length(ltrim(substr(id,length(source_id)+5),'0123456789')) ELSE 0 END), id);

DROP INDEX IF EXISTS idx_source_elements_source_created;
DROP INDEX IF EXISTS idx_source_elements_source_type;
