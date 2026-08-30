-- Hot-path fix batch 3: one keyset-covering composite btree index on
-- concept_clusters. Pure index addition: no query, no service code, and no
-- table/column/FK/unique-surface shape changes here.
--
-- Serves knowledge_store.py's concept_cluster_detail_rows (both backends'
-- concept-detail hub-cluster pagination) and its member_total sibling:
--
--   SELECT cc.member_object_id, cc.canonical_name, ko.object_type, ...
--   FROM concept_clusters cc JOIN knowledge_objects ko ON ko.id=cc.member_object_id
--   WHERE cc.notebook_id=%s AND cc.canonical_id=%s AND ko.status!='deprecated'
--     [AND cc.member_object_id COLLATE "C" > %s AND ko.id COLLATE "C" > %s]
--   ORDER BY cc.member_object_id COLLATE "C"
--   [LIMIT %s]
--
-- idx_clusters_nb_canonical (migration 0039) already covers the
-- `notebook_id=%s AND canonical_id=%s` equality prefix, but stops there: it
-- carries no information about member_object_id order, so a hub concept's
-- later keyset pages (after the first) force a Sort node over every member
-- of that cluster before the seek predicate and LIMIT can trim it down. This
-- migration's idx_clusters_nb_canonical_member ON concept_clusters
-- (notebook_id, canonical_id, member_object_id) closes that gap: the
-- WHERE-prefix equality on (notebook_id, canonical_id) and the trailing
-- member_object_id both live in the SAME index, so the planner can walk it
-- directly in the query's own ORDER BY order -- seek to the equality prefix,
-- then (when `after` is set) seek again to the keyset cursor within that
-- prefix, with no separate sort step. See
-- backend/tests/postgres/test_hotpath_indexes_batch3_live.py for the EXPLAIN
-- (COSTS OFF) proof (Index Scan/Index Only Scan on this index, no Sort node).
--
-- All three key columns are declared `text COLLATE "C"` at table-creation
-- time (migrations/0001_initial.sql), so a plain (unqualified) btree index
-- inherits that same collation on every key -- matching the query's own
-- explicit `COLLATE "C"` comparisons/ORDER BY above byte-for-byte. No
-- opclass or COLLATE qualifier is needed in this index's DDL for that
-- reason (contrast migration 0042's payload GIN, whose expression key needed
-- an explicit `COLLATE "C"` because a GIN index over a cast expression does
-- not inherit a source column's collation the way a plain btree over a bare
-- column does).
--
-- Write-amplification: a plain, non-partial btree over three already-narrow
-- text columns -- builds in seconds even at production scale (9.65M+ row
-- tables elsewhere in this schema), none of the GIN-specific concerns
-- (fastupdate, multi-minute builds, double-digit-GB footprint) migration
-- 0042's payload trigram index carries. Rollback:
-- `DROP INDEX CONCURRENTLY idx_clusters_nb_canonical_member;` -- purely
-- additive, every consuming query still runs (just with an extra Sort on
-- later hub-cluster pages) with the index absent.
--
-- Registered write-amplification debt (not addressed here -- dropping a live
-- index is a separate, deliberate operator call): the pre-existing
-- idx_clusters_nb_canonical (migration 0039) is now a strict prefix of this
-- new index -- any query that could use the former's two-column key can
-- equally use this migration's three-column one. Same convention as
-- idx_chunks_source's retirement note in migration 0039's own header
-- comment: an operator can retire it with
-- `DROP INDEX CONCURRENTLY idx_clusters_nb_canonical;` once production has
-- verified the new index is stable.
--
-- Relationship to the offline CONCURRENTLY builder
-- (scripts/build_hotpath_indexes.py): on a database with pre-existing
-- production traffic, an operator runs that script's --apply mode FIRST,
-- online, with CREATE INDEX CONCURRENTLY (this migration runner executes
-- every migration inside a transaction, and CONCURRENTLY cannot run inside
-- one), using the SAME shared advisory-locked builder/inspector
-- (app/repositories/postgres/hotpath_indexes.py's HOTPATH_INDEX_SPECS, which
-- now carries all eleven indexes across the three batches). On a fresh
-- deploy this migration's IF NOT EXISTS clause is sufficient by itself.
--
-- Pre-existing same-named index validation (codex #636 R1 P2, same pattern
-- as migration 0042's own DO block -- see that migration's header comment
-- for the full rationale): a leftover INVALID catalog row from an
-- interrupted CONCURRENTLY build, or an operator-hand-built index with the
-- right name but the wrong column list / opclass / collation, would
-- otherwise make `IF NOT EXISTS` silently skip creation while this
-- migration still records itself as applied. The comparison reads the same
-- semantic catalog dimensions hotpath_indexes.py's _matches_shape does --
-- access method, per-key pg_get_indexdef(...,n,true) echo, opclasses,
-- collations, table ownership, uniqueness, key count -- normalized the same
-- way (lowercase, collapse whitespace, strip ::text). This index has no
-- partial predicate, so the predicate dimension is simply the empty string
-- on both sides. Both failure modes RAISE (fail the migration transaction,
-- ledger not advanced) instead of auto-dropping: a non-CONCURRENT rebuild
-- inside this transaction would take a long blocking lock on a large
-- production table, so the operator resolves the residue online (DROP INDEX
-- CONCURRENTLY + rerun the builder script) and only then migrates.
-- backend/tests/test_hotpath_indexes_batch3.py re-derives every expected
-- value below from HOTPATH_INDEX_SPECS and asserts it appears verbatim in
-- this file, and backend/tests/postgres/test_hotpath_indexes_batch3_live.py
-- covers the accept path, the wrong-shape reject, and the INVALID-residue
-- reject.
DO $$
DECLARE
  rec record;
  existing record;
  actual_keys text[];
  actual_opclasses text[];
  actual_collations text[];
BEGIN
  FOR rec IN
    SELECT * FROM (VALUES
      ('idx_clusters_nb_canonical_member',
       'concept_clusters',
       'btree',
       ARRAY['notebook_id', 'canonical_id', 'member_object_id'],
       ARRAY['pg_catalog:text_ops', 'pg_catalog:text_ops', 'pg_catalog:text_ops'],
       ARRAY['pg_catalog:C', 'pg_catalog:C', 'pg_catalog:C'])
    ) AS v(index_name, expected_table, expected_am, expected_keys,
           expected_opclasses, expected_collations)
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
      RAISE EXCEPTION 'hotpath batch 3: pre-existing index % is INVALID (an interrupted CONCURRENTLY build left it unusable); run DROP INDEX CONCURRENTLY %, rerun scripts/build_hotpath_indexes.py --apply, then migrate again',
        rec.index_name, rec.index_name;
    END IF;
    actual_keys := ARRAY(
      SELECT btrim(regexp_replace(replace(
               lower(pg_get_indexdef(existing.indexrelid, n, true)),
               '::text', ''), '\s+', ' ', 'g'))
      FROM generate_series(1, existing.indnkeyatts) AS n ORDER BY n);
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
       OR existing.indisunique
       OR existing.indnkeyatts <> array_length(rec.expected_keys, 1)
       OR existing.indnatts <> array_length(rec.expected_keys, 1)
       OR actual_keys <> rec.expected_keys
       OR actual_opclasses <> rec.expected_opclasses
       OR actual_collations <> rec.expected_collations THEN
      RAISE EXCEPTION 'hotpath batch 3: pre-existing index % does not match the expected definition; resolve the name collision manually, then migrate again',
        rec.index_name;
    END IF;
  END LOOP;
END
$$;

CREATE INDEX IF NOT EXISTS idx_clusters_nb_canonical_member
  ON concept_clusters(notebook_id, canonical_id, member_object_id);
