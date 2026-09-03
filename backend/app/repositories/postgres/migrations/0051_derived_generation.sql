-- Batch 3 · W2 · PR-1: generational cluster/community swap -- schema half
-- (design doc docs/superpowers/specs/
-- 2026-09-03-batch3-w2-generational-cluster-swap-design_zh.md, Sec 1.1).
--
-- Columns: the three derived-graph tables gain a row-level `generation`
-- (BIGINT DEFAULT 0 -- a metadata-only ADD COLUMN on PG 11+, no heap
-- rewrite), and unified_kg_state gains the generational control block:
-- two published pointers (cluster_generation / community_generation), the
-- monotonic claim counter (derived_generation_counter -- NEVER reset, not
-- even by delete_notebook_kg's final UPSERT: a re-climbing counter would
-- collide with rows that survived a reset's REPEATABLE READ snapshot, see
-- design Sec 2.3), the in-flight claim pair (derived_building_generation /
-- derived_building_claimed_at -- release channels: flip / finally-CAS /
-- wall-clock TTL crash fallback), and the durable catch-up marker
-- (derived_catchup_from). All defaults keep every pre-existing row and the
-- birth row byte-compatible: generation=0 rows + pointer=0 means readers'
-- `generation = COALESCE((SELECT cluster_generation ...), 0)` predicates
-- select exactly what they selected before this migration.
--
-- Index rework (three entries, batch 6 of the shared hotpath builder --
-- HOTPATH_INDEX_SPECS in app/repositories/postgres/hotpath_indexes.py):
--
-- 1. uq_clusters_nb_type_member_generation REPLACES migration 0007's
--    three-column uq_clusters_notebook_type_member. The old unique index
--    physically forbids two generations of the same member coexisting --
--    today's swap is only legal because swap_cluster_map_from_scratch
--    DELETEs the slice in the same transaction. Generational writes (PR-2)
--    insert generation G while generation P still holds the same members,
--    so `generation` must join the unique key. While every writer still
--    writes generation=0 (all of PR-1), the four-column index enforces the
--    exact same per-generation uniqueness the old one did.
-- 2. idx_clusters_nb_canonical_member_gen: 0043's keyset-covering index
--    plus INCLUDE (generation). Measured on a 500k-row replica: with the
--    reader predicate added, the old index loses Index Only Scan (8.8x
--    buffers, plan switches to Incremental Sort + Index Scan); INCLUDE
--    restores IOS at ~1x steady-state and ~2.4x during the bounded
--    dual-generation window.
-- 3. idx_clusters_nb_created_gen: (notebook_id, created_at) INCLUDE
--    (generation). Serves the two live aggregate readers that the
--    "version identity must only count the published generation" red line
--    forces behind the predicate (index_projection_store.version_facts'
--    COUNT/MAX cluster component -- part of the on-disk manifest.version
--    vector -- and unified_kg_store.concept_clusters_count, the rebuild
--    skip-gate's second leg), and doubles as the boundedness evidence for
--    the catch-up scan (design Sec 1.5: generation=P AND created_at >= TS).
--
-- FIVE superseded indexes are DROPPED here (IF EXISTS):
-- uq_clusters_notebook_type_member / idx_clusters_nb_canonical_member /
-- idx_clusters_nb_created are strictly covered by their replacements (and
-- the old unique MUST be gone before PR-2's dual-generation writes can
-- ever run); idx_clusters_nb (0004) and idx_clusters_nb_canonical (0039,
-- already a registered retirement debt as a strict prefix of 0043's index)
-- are strict prefixes of the two covering replacements and MUST go with
-- the rework rather than linger: measured on live plan probes, a narrower
-- prefix index HIJACKS the generation-predicated readers into a plain
-- Index Scan + heap Filter -- exactly the regression the INCLUDE columns
-- exist to prevent. Every bare-prefix scan they served is equally served
-- by the replacements' leading columns.
-- Production operators run scripts/build_hotpath_indexes.py --apply FIRST
-- (online CREATE INDEX CONCURRENTLY for the three new entries -- including
-- their idempotent ADD COLUMN prerequisite, same advisory-locked builder
-- as batches 1-5), then `DROP INDEX CONCURRENTLY` the FIVE old names, then
-- migrate -- this migration's IF NOT EXISTS / IF EXISTS clauses make it a
-- pure ledger no-op in that flow. Runbook: docs/deployment-and-
-- configuration.md's hotpath-index section. On a fresh deploy the tables
-- are empty and the in-transaction create/drop below is instantaneous.
--
-- Pre-existing same-named index validation (same DO-block pattern as 0042/
-- 0043 -- see 0043's header for the full rationale) extended with the two
-- shape dimensions this batch introduces: expected uniqueness per entry,
-- and INCLUDE columns (indnatts > indnkeyatts, with the non-key suffix
-- compared against the expected include list).
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
      ('uq_clusters_nb_type_member_generation',
       'concept_clusters', 'btree', true,
       ARRAY['notebook_id', 'object_type', 'member_object_id', 'generation'],
       ARRAY[]::text[],
       ARRAY['pg_catalog:text_ops', 'pg_catalog:text_ops', 'pg_catalog:text_ops', 'pg_catalog:int8_ops'],
       ARRAY['pg_catalog:C', 'pg_catalog:C', 'pg_catalog:C', '']),
      ('idx_clusters_nb_canonical_member_gen',
       'concept_clusters', 'btree', false,
       ARRAY['notebook_id', 'canonical_id', 'member_object_id'],
       ARRAY['generation'],
       ARRAY['pg_catalog:text_ops', 'pg_catalog:text_ops', 'pg_catalog:text_ops'],
       ARRAY['pg_catalog:C', 'pg_catalog:C', 'pg_catalog:C']),
      ('idx_clusters_nb_created_gen',
       'concept_clusters', 'btree', false,
       ARRAY['notebook_id', 'created_at'],
       ARRAY['generation'],
       ARRAY['pg_catalog:text_ops', 'pg_catalog:timestamptz_ops'],
       ARRAY['pg_catalog:C', ''])
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
      RAISE EXCEPTION 'W2 batch 6: pre-existing index % is INVALID (an interrupted CONCURRENTLY build left it unusable); run DROP INDEX CONCURRENTLY %, rerun scripts/build_hotpath_indexes.py --apply, then migrate again',
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
      RAISE EXCEPTION 'W2 batch 6: pre-existing index % does not match the expected definition; resolve the name collision manually, then migrate again',
        rec.index_name;
    END IF;
  END LOOP;
END
$$;

ALTER TABLE concept_clusters
  ADD COLUMN IF NOT EXISTS generation bigint NOT NULL DEFAULT 0;
ALTER TABLE communities
  ADD COLUMN IF NOT EXISTS generation bigint NOT NULL DEFAULT 0;
ALTER TABLE community_members
  ADD COLUMN IF NOT EXISTS generation bigint NOT NULL DEFAULT 0;
ALTER TABLE unified_kg_state
  ADD COLUMN IF NOT EXISTS cluster_generation bigint NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS community_generation bigint NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS derived_generation_counter bigint NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS derived_building_generation bigint NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS derived_building_claimed_at timestamp with time zone,
  ADD COLUMN IF NOT EXISTS derived_catchup_from timestamp with time zone;

CREATE UNIQUE INDEX IF NOT EXISTS uq_clusters_nb_type_member_generation
  ON concept_clusters(notebook_id, object_type, member_object_id, generation);
CREATE INDEX IF NOT EXISTS idx_clusters_nb_canonical_member_gen
  ON concept_clusters(notebook_id, canonical_id, member_object_id)
  INCLUDE (generation);
CREATE INDEX IF NOT EXISTS idx_clusters_nb_created_gen
  ON concept_clusters(notebook_id, created_at)
  INCLUDE (generation);

DROP INDEX IF EXISTS uq_clusters_notebook_type_member;
DROP INDEX IF EXISTS idx_clusters_nb_canonical_member;
DROP INDEX IF EXISTS idx_clusters_nb_created;
DROP INDEX IF EXISTS idx_clusters_nb;
DROP INDEX IF EXISTS idx_clusters_nb_canonical;
