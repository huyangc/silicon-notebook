-- Hot-path fix batch 2 (R6): search + checkup H5 index pair, confirmed against a
-- production diag (nb 9.65M knowledge objects / 5.77M source_elements). Pure
-- index additions plus one query-side literal-inlining change in
-- backend/app/repositories/postgres/maintenance.py -- no table/column/FK/
-- unique-surface shape changes here. See migrations/0039_hotpath_batch1_indexes.sql
-- for batch 1 (six query-family groups, eight indexes); this migration is
-- batch 2's own second, independent no-op-once-online-built ledger entry, using
-- the SAME shared advisory-locked builder/inspector
-- (app/repositories/postgres/hotpath_indexes.py's HOTPATH_INDEX_SPECS, which now
-- carries all ten indexes across both batches) and the same
-- CONCURRENTLY-outside-a-transaction relationship documented in that file's own
-- header comment: on a database with pre-existing production traffic, an
-- operator runs scripts/build_hotpath_indexes.py --apply FIRST, online; on a
-- fresh deploy this migration's two IF NOT EXISTS statements are sufficient by
-- themselves. Before creating anything, the DO block below validates any
-- PRE-EXISTING same-named index (codex #636 R1 P2): a leftover INVALID
-- catalog row from an interrupted CONCURRENTLY build, or an
-- operator-hand-built index with the right name but the wrong access method /
-- opclass / collation / expression, would otherwise make `IF NOT EXISTS`
-- silently skip creation while this migration still records itself as
-- applied -- the ledger would then claim an index that cannot serve its
-- query. Both cases RAISE (fail the migration transaction, ledger not
-- advanced) instead of auto-dropping: a non-CONCURRENT rebuild inside this
-- transaction would take a long blocking lock on a large production table,
-- so the operator resolves the residue online (DROP INDEX CONCURRENTLY +
-- rerun the builder script) and only then migrates.
--
-- Group 1: idx_knowledge_objects_nb_payload_trgm (composite partial GIN, trigram)
--   ON knowledge_objects USING gin (
--     notebook_id public.text_ops,
--     ((payload::text) COLLATE "C") public.gin_trgm_ops
--   ) WHERE status != 'deprecated'
--   Serves search.py:notebook_knowledge_rows's payload-JSON ILIKE arm
--   (`(payload::text) COLLATE "C" ILIKE %s`, search.py line ~846) -- the
--   collection page's "search knowledge" leg. Production diag:
--   `knowledge_payload_ilike_probe` cost 5.9s on a rare-term needle with no
--   supporting index, forcing the planner to walk the WHOLE
--   `uq_knowledge_objects_ordinal` ordinal sequence rather than narrow first.
--   A one-time 200k-row EXPLAIN (two disposable databases, both since dropped)
--   showed the rare-term case drop to 3.6ms -- measured on the PRE-REVIEW
--   single-expression global GIN shape; the composite shape below is a strict
--   narrowing of that index's bitmap (same trigram keys, intersected with the
--   notebook equality), so the milliseconds-scale conclusion carries over
--   even though the exact figure was not re-benchmarked (the live tests
--   assert the composite plan shape instead). The common-term case is
--   UNCHANGED (planner still prefers the cheap
--   ordinal walk at ~0.02ms when the ILIKE pattern is not selective) -- this
--   index only removes the rare-term catastrophic branch, and the query text
--   itself is untouched (the expression here is byte-identical to search.py's
--   own `(payload::text) COLLATE "C"`, so the planner can match it without any
--   application-side change).
--   WHY COMPOSITE + PARTIAL, not a bare single-expression GIN (codex #636 R1
--   P1): docs/operations.md's "PostgreSQL notebook-aware lexical indexes"
--   section documents this exact cross-notebook scaling failure for the
--   legacy single-expression trigram indexes -- on a large shared table, a
--   term that is selective globally but concentrated in OTHER notebooks
--   still builds a global bitmap and discards almost every row only after
--   heap recheck, reproducing the very timeout this index exists to fix.
--   The fix is the same shape retrieval_indexes.py already ships for
--   idx_knowledge_objects_nb_name_trgm: prepend `notebook_id` via
--   `public.text_ops` (btree_gin, installed below by a guarded DO block --
--   see its own comment; a DBA-preinstalled public.btree_gin satisfies it
--   like 0002's pg_trgm) so the
--   mandatory `notebook_id=%s` equality intersects INSIDE index access, and
--   scope the index with `WHERE status != 'deprecated'` -- safe under
--   generic plans too, because notebook_knowledge_rows spells
--   `status!='deprecated'` as a literal in its SQL text (a bound parameter
--   there could not prove the partial-predicate implication; see Group 2's
--   generic-plan discussion for the full mechanics). The `nb_` name prefix
--   follows the same notebook-scoped-composite convention as
--   idx_knowledge_objects_nb_name_trgm / idx_chunks_nb_text_trgm.
--   Write-amplification registered up front, not discovered later: a GIN
--   trigram index over a whole jsonb payload cast to text runs roughly 1.5x
--   the base table's own storage footprint (measured 60MB index / 40MB table
--   at 200k rows in the disposable benchmark database; the notebook_id
--   btree_gin key and the partial predicate shave a little off that, they do
--   not change the order of magnitude) -- at the production scale of ~9.65M
--   knowledge objects this is a double-digit-GB structure, and every
--   knowledge_objects INSERT/UPDATE now pays one more GIN maintenance
--   write. Rollback if this proves not worth its footprint:
--   `DROP INDEX CONCURRENTLY idx_knowledge_objects_nb_payload_trgm;` -- purely
--   additive, no other code path depends on this index existing.
--
-- Group 2: idx_source_elements_nonblank (partial btree)
--   ON source_elements(source_id, id)
--   WHERE btrim(text, <the exact PY_WHITESPACE charset, see below>) != ''
--   Serves checkup/backfill's "how many eligible elements still lack a
--   vector" family in postgres/maintenance.py: count_missing_element_vectors,
--   missing_element_embedding_ids, missing_element_embedding_rows (the
--   unbounded reference implementation the equivalence tests still drive),
--   missing_element_embedding_page, and missing_element_vector_source_ids --
--   every one of these joins source_elements to sources and filters
--   `btrim(e.text, PY_WHITESPACE) != ''` (an element whose text is only
--   whitespace/control characters is never eligible for embedding; see
--   PY_WHITESPACE's own module docstring for why the charset must be this
--   exact derived set, not bare `btrim(x)`). Production diag: before this
--   migration there was no index at all backing this predicate, and H5's cold
--   aggregate scan cost 2.6s evaluating it row-by-row across 5.77M
--   source_elements -- this index alone (regardless of the query-text change
--   below) fixes that once PostgreSQL's normal per-call custom planning (its
--   default behavior, and the only mode this repository's connections ever
--   exercise) can see the actual bound value and prove the query predicate
--   implies this index's predicate; a one-time disposable-database EXPLAIN
--   confirmed both an ordinary `%s`-bound parameter and an inlined literal
--   pick up this partial index equally well under that normal custom-plan
--   path. The one scenario where the two forms provably diverge is a
--   GENERIC (parameter-value-blind) plan -- PostgreSQL falls back to one once
--   it decides a cached plan is worth reusing across differing bind values,
--   e.g. under `SET plan_cache_mode = force_generic_plan` or, in principle,
--   sustained repeated same-shape execution that tips its own cost-based
--   generic-vs-custom heuristic -- where an ordinary bound parameter cannot
--   prove the implication (its value is unknown at plan time) and the query
--   falls back to a full sequential scan, while an inlined literal has no
--   such parameter at all and is immune regardless of plan-cache state
--   (verified both ways with `PREPARE`/`EXECUTE` under a forced generic plan
--   on the same seeded table; see
--   backend/tests/postgres/test_hotpath_indexes_batch2_live.py). See
--   postgres/maintenance.py's `_NONBLANK_TEXT_SQL` module constant for the
--   query-side half of this change: it renders PY_WHITESPACE through
--   `psycopg.sql.Literal(...).as_string(None)` into the SAME literal text
--   this index's predicate uses, inlined directly into the SQL text instead
--   of passed as a bound parameter -- a worst-case-hardening change, not a
--   fix for an observed default-path regression. This is the one query-text
--   change in this batch; it is semantically a no-op (identical runtime
--   value, only the parameter-vs-literal transport differs) --
--   backend/tests/test_hotpath_indexes_batch2.py's equivalence oracle proves
--   old-bound-param and new-inlined-literal forms return the same counts
--   across a range of whitespace-edge-case rows.
--
--   *** IMPORTANT: THIS PREDICATE'S LITERAL MUST STAY BYTE-FOR-BYTE IDENTICAL
--   TO backend/app/repositories/text_whitespace.py's PY_WHITESPACE CONSTANT. ***
--   This migration file is static SQL -- it cannot import Python at apply
--   time -- so the 29-codepoint whitespace charset below is written out by
--   hand as a `chr(N) || chr(N) || ...` concatenation (same style as
--   0005_memory_knowhow_governance_indexes.sql's normalized-anchor index and
--   knowhow_store.py's `_PG_TRIM_CHARS`), generated from PY_WHITESPACE at
--   authoring time rather than hand-transcribed digit-by-digit.
--   backend/tests/test_hotpath_indexes_batch2.py re-derives this exact string
--   from PY_WHITESPACE and asserts it appears verbatim in this file, so any
--   future edit to PY_WHITESPACE (or a stray edit here) fails that test
--   loudly instead of silently drifting the two "non-blank element" judgments
--   apart. The codepoints in order (decimal): 9, 10, 11, 12, 13, 28, 29, 30,
--   31, 32, 133, 160, 5760, 8192, 8193, 8194, 8195, 8196, 8197, 8198, 8199,
--   8200, 8201, 8202, 8232, 8233, 8239, 8287, 12288.
--
--   Write-amplification registered up front: this is a PARTIAL index (only
--   non-blank-text rows qualify), so its footprint tracks the eligible subset
--   of source_elements rather than the whole table -- materially smaller than
--   a full index would be, and the predicate is immutable-function-only
--   (btrim/chr), so PostgreSQL accepts it without a functional-index caveat.
--   Rollback: `DROP INDEX CONCURRENTLY idx_source_elements_nonblank;` --
--   purely additive; every consuming query still runs (just slower) with the
--   index absent, since none of the query text itself depends on the index
--   existing.
--
-- Relationship to SQLite: this batch is PostgreSQL-only. Neither the
-- knowledge_objects payload search nor the H5 element-eligibility judgment
-- gets a SQLite-side index change in this batch -- SQLite has no partial-index
-- planner benefit worth chasing here and no equivalent to a GIN trigram index
-- for this shape of query, so backend/app/repositories/sqlite/maintenance.py's
-- twin keeps its bound-parameter `TRIM(e.text, ?)` form unchanged (the
-- divergence is registered in postgres/maintenance.py's _NONBLANK_TEXT_SQL
-- block comment; the sqlite file itself is untouched). SQLITE_SCHEMA_VERSION is
-- therefore untouched by this migration; only postgres_version advances.

-- btree_gin supplies the gin-AM `public.text_ops` opclass the composite key
-- needs. Unlike 0002's bare pg_trgm line, this install is wrapped so BOTH
-- failure modes surface as an operator-actionable message instead of a raw
-- permission/packaging error or (worse) a later cryptic unresolvable-opclass
-- failure: the extension may be preinstalled by a DBA exactly like pg_trgm
-- (docs/deployment-and-configuration.md lists both as prerequisites), and a
-- migration-role CREATE needs CREATE on the database even for a trusted
-- extension.
DO $$
DECLARE
  ext_schema text;
BEGIN
  SELECT n.nspname INTO ext_schema
  FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
  WHERE e.extname = 'btree_gin';
  IF ext_schema IS NULL THEN
    BEGIN
      CREATE EXTENSION btree_gin WITH SCHEMA public;
    EXCEPTION WHEN OTHERS THEN
      RAISE EXCEPTION 'hotpath batch 2: installing btree_gin failed (%); preinstall it with a privileged role -- CREATE EXTENSION btree_gin WITH SCHEMA public; -- then migrate again',
        SQLERRM;
    END;
  ELSIF ext_schema <> 'public' THEN
    RAISE EXCEPTION 'hotpath batch 2: btree_gin is installed in schema % but must live in public (the composite index references its opclass as public.text_ops); reinstall it in public, then migrate again',
      ext_schema;
  END IF;
END
$$;

-- Pre-existing same-named index validation (codex #636 R1 P2) -- see the
-- header comment. The comparison deliberately reads the SAME semantic
-- catalog dimensions hotpath_indexes.py's _matches_shape does -- access
-- method, per-key pg_get_indexdef(...,n,true) echo, opclasses, collations,
-- partial predicate via pg_get_expr, uniqueness, key count -- normalized the
-- way _normalized_expr does (lowercase, collapse whitespace, strip ::text),
-- NOT the full pg_get_indexdef() statement text: the full text also renders
-- storage-only clauses (reloptions like `WITH (fastupdate=off)` -- the
-- standard GIN write-amplification mitigation an operator may legitimately
-- apply to exactly this index -- or a TABLESPACE), which would false-RAISE
-- on a perfectly usable index while inspect_hotpath_indexes simultaneously
-- reports it ready. Keeping both validators on one set of dimensions means
-- they can never disagree about the same catalog row.
-- backend/tests/test_hotpath_indexes_batch2.py re-derives every expected
-- value below from HOTPATH_INDEX_SPECS and asserts it appears verbatim in
-- this file, and the live tests cover the accept path (a script-built index,
-- with and without reloptions), the wrong-shape reject, and the
-- INVALID-residue reject.
DO $$
DECLARE
  rec record;
  existing record;
  actual_keys text[];
  actual_opclasses text[];
  actual_collations text[];
  actual_predicate text;
BEGIN
  FOR rec IN
    SELECT * FROM (VALUES
      ('idx_knowledge_objects_nb_payload_trgm',
       'knowledge_objects',
       'gin',
       ARRAY['notebook_id', '(payload)'],
       ARRAY['public:text_ops', 'public:gin_trgm_ops'],
       ARRAY['pg_catalog:C', 'pg_catalog:C'],
       $pred$status <> 'deprecated'$pred$),
      ('idx_source_elements_nonblank',
       'source_elements',
       'btree',
       ARRAY['source_id', 'id'],
       ARRAY['pg_catalog:text_ops', 'pg_catalog:text_ops'],
       ARRAY['pg_catalog:C', 'pg_catalog:C'],
       $pred$btrim(text, (((((((((((((((((((((((((((chr(9) || chr(10)) || chr(11)) || chr(12)) || chr(13)) || chr(28)) || chr(29)) || chr(30)) || chr(31)) || chr(32)) || chr(133)) || chr(160)) || chr(5760)) || chr(8192)) || chr(8193)) || chr(8194)) || chr(8195)) || chr(8196)) || chr(8197)) || chr(8198)) || chr(8199)) || chr(8200)) || chr(8201)) || chr(8202)) || chr(8232)) || chr(8233)) || chr(8239)) || chr(8287)) || chr(12288)) <> ''$pred$)
    ) AS v(index_name, expected_table, expected_am, expected_keys,
           expected_opclasses, expected_collations, expected_predicate)
  LOOP
    -- tbl join (codex #636 R2 P2): index names are schema-wide, so a
    -- same-named index on ANOTHER table (chunks has the very same
    -- (source_id, id, text COLLATE "C") surface) could otherwise pass every
    -- shape dimension while CREATE INDEX IF NOT EXISTS silently skips the
    -- intended table.
    SELECT i.indexrelid, i.indisvalid, i.indisready, i.indisunique,
           i.indnkeyatts, i.indnatts,
           i.indclass::oid[] AS opclass_oids,
           i.indcollation::oid[] AS collation_oids,
           am.amname,
           tbl.relname AS table_name,
           pg_get_expr(i.indpred, i.indrelid, true) AS predicate
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
      RAISE EXCEPTION 'hotpath batch 2: pre-existing index % is INVALID (an interrupted CONCURRENTLY build left it unusable); run DROP INDEX CONCURRENTLY %, rerun scripts/build_hotpath_indexes.py --apply, then migrate again',
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
    actual_predicate := btrim(regexp_replace(replace(
      lower(COALESCE(existing.predicate, '')), '::text', ''), '\s+', ' ', 'g'));
    IF existing.table_name <> rec.expected_table
       OR existing.amname <> rec.expected_am
       OR existing.indisunique
       OR existing.indnkeyatts <> array_length(rec.expected_keys, 1)
       OR existing.indnatts <> array_length(rec.expected_keys, 1)
       OR actual_keys <> rec.expected_keys
       OR actual_opclasses <> rec.expected_opclasses
       OR actual_collations <> rec.expected_collations
       OR actual_predicate <> rec.expected_predicate THEN
      RAISE EXCEPTION 'hotpath batch 2: pre-existing index % does not match the expected definition; resolve the name collision manually, then migrate again',
        rec.index_name;
    END IF;
  END LOOP;
END
$$;

CREATE INDEX IF NOT EXISTS idx_knowledge_objects_nb_payload_trgm
  ON knowledge_objects USING gin (
    notebook_id public.text_ops,
    ((payload::text) COLLATE "C") public.gin_trgm_ops
  ) WHERE status != 'deprecated';

CREATE INDEX IF NOT EXISTS idx_source_elements_nonblank
  ON source_elements(source_id, id)
  WHERE btrim(text, chr(9) || chr(10) || chr(11) || chr(12) || chr(13) || chr(28) || chr(29) || chr(30) || chr(31) || chr(32) || chr(133) || chr(160) || chr(5760) || chr(8192) || chr(8193) || chr(8194) || chr(8195) || chr(8196) || chr(8197) || chr(8198) || chr(8199) || chr(8200) || chr(8201) || chr(8202) || chr(8232) || chr(8233) || chr(8239) || chr(8287) || chr(12288)) != '';
