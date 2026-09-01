-- Hot-path fix batch 4: the source-tab search predicate. Three notebook-scoped
-- composite GIN trigram indexes, paired with a query-side rewrite in
-- backend/app/repositories/postgres/source_store.py's list_sources_page (and
-- its SQLite twin). No table/column/FK/unique-surface shape changes here.
-- See migrations/0039_hotpath_batch1_indexes.sql for batch 1 (six query-family
-- groups, eight indexes), 0042_hotpath_batch2_search_indexes.sql for batch 2
-- (payload-search GIN + checkup-H5 partial btree) and
-- 0043_concept_cluster_keyset_index.sql for batch 3 (concept-cluster keyset
-- index). This migration is batch 4's own independent
-- no-op-once-online-built ledger entry, using the SAME shared
-- advisory-locked builder/inspector (app/repositories/postgres/
-- hotpath_indexes.py's HOTPATH_INDEX_SPECS, which now carries all fourteen
-- indexes across the four batches) and the same
-- CONCURRENTLY-outside-a-transaction relationship documented in that file's
-- own header comment: on a database with pre-existing production traffic an
-- operator runs scripts/build_hotpath_indexes.py --apply FIRST, online; on a
-- fresh deploy this migration's three IF NOT EXISTS statements are sufficient
-- by themselves.
--
-- ===========================================================================
-- The production evidence
-- ===========================================================================
-- On a notebook holding 49k sources, the source tab's server-side search
-- (`GET /notebooks/{id}/sources?q=…`) measured 363ms for the COUNT alone, and
-- the page query pays the very same predicate a second time in the same
-- request. The pre-rewrite predicate was one cross-table boolean OR:
--
--   LOWER(title) LIKE %s OR LOWER(file_name) LIKE %s
--   OR EXISTS(SELECT 1 FROM source_authors a
--             WHERE a.source_id=sources.id AND LOWER(a.name) LIKE %s)
--   OR EXISTS(SELECT 1 FROM source_paper_meta m
--             WHERE m.source_id=sources.id AND LOWER(m.paper_title) LIKE %s)
--
-- The planner answers an OR spanning three tables with hashed subplans: it
-- materialized ALL of source_authors (210k rows, parallel sequential scan) and
-- ALL of source_paper_meta (39k rows, sequential scan) once per execution,
-- because nothing inside an OR arm can narrow the OTHER arms' relations. The
-- load-bearing diagnostic is that a two-character CJK needle and a
-- seven-character ASCII needle cost the SAME (360ms vs 363ms): if trigram
-- matching were the bottleneck the two would diverge sharply, so the cost is
-- the full scans, not the LIKE evaluation.
--
-- Indexes alone could not fix that shape: no index can serve an OR whose arms
-- live in different tables. So the query became a three-leg UNION over source
-- ids, semi-joined back with `sources.id IN (…)` -- three independent legs,
-- each rooted at its OWN table's `notebook_id=` equality, each therefore
-- indexable. The three indexes below are what make those legs cheap. The
-- rewrite's full semantic-equivalence argument (why repeating the visible
-- predicate in leg 1 is safe, why the child legs may key on their own
-- notebook_id, how NULL paper_title behaves) lives next to the query in
-- postgres/source_store.py:list_sources_page -- read it there, it is not
-- duplicated here.
--
-- Local benchmark. Corpus: 20k sources in the notebook under test plus two 5k
-- neighbours, 4 author rows per source (production's 210k-over-49k ratio), 80%
-- carrying paper metadata -- 30k / 120k / 24k rows. Built through
-- PostgresMigrator so EVERY competing index this schema ships (migration 0003's
-- idx_sources_notebook_status, idx_source_paper_meta_nb, ...) is present and
-- the planner's choices are the ones production would make. EXPLAIN (ANALYZE,
-- BUFFERS) run twice, second run reported. "before" = old predicate, no new
-- index; "after" = new predicate with all three. Disposable schema, dropped.
--
--   needle                        COUNT before -> after    page before -> after
--   'zqxjtitle' (9 ch, 1 hit)     154.46ms -> 0.16ms        7.00ms -> 0.14ms
--   'wu'        (2 ch, 2341 hits) 190.92ms -> 30.71ms      23.62ms -> 33.13ms
--   'qz'        (2 ch, 0 hits)    188.54ms -> 28.88ms      33.03ms -> 25.21ms
--
-- Per user action (the request issues COUNT and page against the same
-- predicate, so the honest unit is their sum): 161.5ms -> 0.30ms (538x),
-- 214.5ms -> 63.8ms (3.4x), 221.6ms -> 54.1ms (4.1x).
--
-- On the selective needle all three new indexes are in the plan and the two
-- OR'd arms BitmapOr two scans of the composite -- the designed shape, chosen
-- by default with no planner knobs. The live tests pin exactly that.
--
-- REGISTERED TRADE-OFF, measured rather than discovered later: pg_trgm
-- extracts NO trigram keys from a pattern shorter than three characters, so on
-- a one- or two-character needle each leg's GIN scan degenerates to "all rows
-- of this notebook" (the btree_gin notebook_id key alone) plus a heap recheck.
-- The COUNT still improves ~6x there because it no longer scans the child
-- tables whole, but the PAGE query can lose its old escape hatch: the old
-- shape could walk an ordered notebook_id btree in the query's own ORDER BY
-- order and stop at 50 rows, while an `IN (…)` semi-join must materialize the
-- whole match set before the sort. That shows up as the one regression in the
-- table above (23.62ms -> 33.13ms on 'wu'); the zero-hit short needle 'qz'
-- improves anyway, and both short-needle cases are 3-4x faster per user
-- action. The q-EMPTY path (the common one) is untouched: its page query still
-- uses idx_sources_notebook_created's ordered walk (0.059ms, unchanged) and
-- its COUNT gets slightly cheaper (4.31ms -> 3.65ms) because the partial GIN
-- below doubles as a "visible rows of this notebook" bitmap.
--
-- *** OPERATIONAL NOTE -- GIN FASTUPDATE PENDING LIST. Read this before
-- concluding from an EXPLAIN that one of these indexes "is not being used".
-- A GIN index with fastupdate on (the default) parks freshly inserted entries
-- in an unindexed PENDING LIST, and gincostestimate charges every GIN plan for
-- scanning it. Right after a bulk load -- a CREATE INDEX followed by heavy
-- ingestion, a restore, or a benchmark's own seeding -- that surcharge inflates
-- the estimate roughly TENFOLD and the planner will reject its own index.
-- Measured on the corpus above, before VACUUM vs after:
--
--   query                        before VACUUM              after VACUUM
--   title arm                    Seq Scan (1379)            composite GIN (85.52)
--   title OR file_name           idx_sources_notebook_status  BitmapOr, 2 scans of
--                                (1501)                     the composite (170.80)
--   paper-title leg              idx_source_paper_meta_nb   its own GIN (85.38)
--                                (740.55)
--
-- One VACUUM of the three tables merges the pending list into the tree and all
-- three plans flip to the intended index. Autovacuum reaches the same state on
-- its own; the point is that a plan measured minutes after a bulk load is a
-- transient, not the steady state. An earlier draft of this batch's own live
-- tests was misled by exactly this and nearly concluded that two of the three
-- indexes were not worth shipping -- backend/tests/postgres/
-- test_hotpath_indexes_batch4_live.py now VACUUMs in its fixture and asserts
-- every leg's plan with no planner knobs at all. Operators who want the
-- surcharge gone permanently for these three can build them WITH
-- (fastupdate=off); the pre-existing-index validation DO block below
-- deliberately tolerates that reloption. ***
--
-- CONSIDERED AND REJECTED -- splitting the first leg's `OR` into two separate
-- single-arm UNION legs. Once the pending-list artifact above is removed the
-- BitmapOr is chosen by default, so the split buys nothing, and it measurably
-- costs: a wash on the selective needle (COUNT 0.13ms vs 0.16ms, inside noise)
-- and consistently worse on short ones (COUNT 33.94 vs 30.71, page 37.16 vs
-- 33.13 on 'wu'; 30.96 vs 28.88 and 30.37 vs 25.21 on 'qz'), because a fourth
-- Append branch means a second full pass over `sources` whenever the pattern
-- is too short for trigram extraction. BitmapOr inside one scan node is
-- strictly the better shape. The other rejected variant, two two-key indexes
-- instead of one three-key composite, is discussed under Index 1 below.
--
-- The structural win is separate from all of that and holds at every scale:
-- the three legs are now independent, separately-plannable relations under an
-- Append, so NOTHING is a hashed subplan any more. That is what removes the
-- "materialize all of source_authors, then all of source_paper_meta, once per
-- execution" cost the production diag measured.
--
-- ===========================================================================
-- Index 1: idx_sources_nb_title_file_trgm (composite partial GIN, trigram)
-- ===========================================================================
--   ON sources USING gin (
--     notebook_id public.text_ops,
--     lower(title) public.gin_trgm_ops,
--     lower(file_name) public.gin_trgm_ops
--   ) WHERE source_type NOT IN ('memory','knowhow')
--
--   Serves the UNION's first leg: `notebook_id=%s AND <visible> AND
--   (LOWER(title) LIKE %s OR LOWER(file_name) LIKE %s)`.
--
--   THREE KEYS IN ONE INDEX, NOT TWO TWO-KEY INDEXES. The two LIKE arms are
--   OR'd, so one index can only serve both if the planner is willing to scan
--   it TWICE and BitmapOr the results. That it does was verified by live
--   EXPLAIN on the benchmark schema, not assumed:
--
--     Bitmap Heap Scan on sources
--       ->  BitmapOr
--             ->  Bitmap Index Scan on idx_sources_nb_title_file_trgm
--                   Index Cond: ((notebook_id = 'nb-main') AND (lower(title) ~~ '%…%'))
--             ->  Bitmap Index Scan on idx_sources_nb_title_file_trgm
--                   Index Cond: ((notebook_id = 'nb-main') AND (lower(file_name) ~~ '%…%'))
--
--   A multi-column GIN lets a scan constrain any SUBSET of its keys, so each
--   arm uses (notebook_id, its own trigram column) and leaves the third key
--   unconstrained. Had the planner refused to BitmapOr, the fallback was two
--   separate two-key indexes (title and file_name); it did not, so one index
--   carries both arms -- and note the fallback would have changed nothing that
--   mattered, since two two-key indexes would each still carry the same
--   notebook_id key at the same per-scan cost.
--   backend/tests/postgres/test_hotpath_indexes_batch4_live.py pins this plan
--   shape -- by default, with no planner knobs -- so a future planner or
--   opclass change fails loudly there instead of silently reverting to a
--   sequential scan. A companion test pins each trigram key separately.
--
--   WHY notebook_id LEADS: the same cross-notebook scaling lesson
--   docs/operations.md's "PostgreSQL notebook-aware lexical indexes" section
--   records for the legacy single-expression trigram indexes, and the same
--   shape retrieval_indexes.py ships for idx_knowledge_objects_nb_name_trgm
--   and migration 0042 ships for idx_knowledge_objects_nb_payload_trgm: a term
--   that is selective globally but concentrated in OTHER notebooks would
--   otherwise build a global bitmap and discard nearly every row only after
--   heap recheck. `public.text_ops` is btree_gin's gin-AM text opclass.
--
--   WHY PARTIAL: `source_type NOT IN ('memory','knowhow')` is the same
--   VISIBLE_SOURCE_TYPES_PREDICATE the query itself spells, and the query
--   spells it as an INLINE LITERAL (a module constant interpolated into the
--   SQL text, never a bound parameter), so the partial-predicate implication
--   holds even under a generic, parameter-value-blind plan -- exactly the
--   mechanism migration 0042's Group 2 discussion works through at length.
--   Scoping the index this way also keeps the hidden Memory/knowhow
--   projection rows out of it entirely.
--
--   NO EXPLICIT COLLATE, unlike migration 0042's payload GIN: `title` and
--   `file_name` are declared `text COLLATE "C"` at table-creation time
--   (0001_initial.sql), and `lower()` derives its result collation from its
--   argument, so both expression keys already carry pg_catalog:C. Verified
--   against a live PostgreSQL 16 catalog read (indcollation resolves to
--   {C,C,C} with no COLLATE anywhere in the DDL). 0042 needed the explicit
--   qualifier because `(payload::text)` is a cast of a jsonb column, which
--   inherits nothing.
--
-- ===========================================================================
-- Index 2: idx_source_authors_nb_name_trgm (composite GIN, trigram)
-- ===========================================================================
--   ON source_authors USING gin (notebook_id public.text_ops,
--                                lower(name) public.gin_trgm_ops)
--   Serves the UNION's second leg: `a.notebook_id=%s AND LOWER(a.name) LIKE %s`
--   -- the 210k-row parallel sequential scan named in the production evidence
--   above. Non-partial: source_authors has no visibility dimension of its own
--   (the outer query's intersection with `sources` handles that), and every
--   author row is a legitimate search target.
--
-- ===========================================================================
-- Index 3: idx_source_paper_meta_nb_ptitle_trgm (composite GIN, trigram)
-- ===========================================================================
--   ON source_paper_meta USING gin (notebook_id public.text_ops,
--                                   lower(paper_title) public.gin_trgm_ops)
--   Serves the UNION's third leg: `m.notebook_id=%s AND
--   LOWER(m.paper_title) LIKE %s` -- the 39k-row sequential scan. `paper_title`
--   is NULLABLE; a GIN trigram index simply stores no entry for a NULL
--   expression, and `LOWER(NULL) LIKE %s` is NULL (falsy in a WHERE) in both
--   the old and the new query shape, so nothing is lost by their absence.
--
--   THIS INDEX WAS PUT UP FOR REMOVAL AND KEPT ON EVIDENCE. source_paper_meta
--   is the smallest of the three tables (39k rows in the production notebook's
--   deployment) and it already carries a plain idx_source_paper_meta_nb btree,
--   so it is the entry in this batch with the most to prove. In steady state
--   the planner picks the GIN by default and by a wide margin: 85.38 against
--   740.55 for the btree-plus-filter alternative, measured on a corpus of
--   24k paper-meta rows -- i.e. SMALLER than production's 39k, so the margin
--   only widens there. (The reading that briefly suggested otherwise -- a seq
--   scan at 1017 beating the GIN -- was taken before VACUUM, and was the
--   pending-list artifact documented in the operational note above.) It stays.
--
-- ===========================================================================
-- Relationship to the pre-existing btree indexes on these tables
-- ===========================================================================
-- migration 0003 already ships idx_source_authors_nb(notebook_id) and
-- idx_source_paper_meta_nb(notebook_id). These are NOT retired by this batch
-- and must not be: query_store.py's paper-meta dashboard counts
-- (`SELECT is_paper, COUNT(*) … WHERE notebook_id=%s GROUP BY is_paper`) want
-- an ordered btree, which a GIN cannot provide -- unlike migration 0043's
-- strict-prefix case, these are different access shapes, not a superset
-- relationship. No retirement note is registered here.
--
-- ===========================================================================
-- Write amplification, registered up front
-- ===========================================================================
-- Measured on the benchmark schema above (30k sources / 120k author rows /
-- 24k paper-meta rows), index size against its own table's heap size:
--   idx_sources_nb_title_file_trgm         3712 kB  vs sources           3568 kB
--   idx_source_authors_nb_name_trgm        3704 kB  vs source_authors      13 MB
--   idx_source_paper_meta_nb_ptitle_trgm   3112 kB  vs source_paper_meta 2696 kB
-- i.e. roughly 1.0x, 0.3x and 1.2x their tables. Extrapolating linearly to the
-- 49k-source production notebook's fleet-wide row counts puts all three in the
-- tens-of-megabytes range -- three orders of magnitude below migration 0042's
-- payload GIN, because a short title/name/paper-title yields far fewer
-- trigrams than a whole jsonb payload cast to text. Every INSERT/UPDATE of
-- sources.title/file_name, source_authors.name and source_paper_meta.paper_title
-- now pays one more GIN maintenance write; all three are low-rate columns
-- (upload, and one paper-metadata extraction per source) rather than
-- hot-loop writes. Rollback, per index, purely additive and independent:
--   DROP INDEX CONCURRENTLY idx_sources_nb_title_file_trgm;
--   DROP INDEX CONCURRENTLY idx_source_authors_nb_name_trgm;
--   DROP INDEX CONCURRENTLY idx_source_paper_meta_nb_ptitle_trgm;
-- The rewritten query keeps working (just slower) with any or all of them
-- absent -- no code path depends on these indexes existing.
--
-- ===========================================================================
-- Relationship to SQLite
-- ===========================================================================
-- The QUERY rewrite is applied to BOTH backends (sqlite/source_store.py's
-- list_sources_page carries the isomorphic three-leg UNION), but the INDEXES
-- are PostgreSQL-only -- the same split migration 0042 registered for its own
-- batch. SQLite has no equivalent of a GIN trigram index, and a `LIKE '%…%'`
-- pattern cannot use a B-tree prefix, so there is nothing for a SQLite index
-- to do here; the rewrite lands there purely to keep the two backends' SQL
-- from diverging. SQLITE_SCHEMA_VERSION is therefore untouched by this
-- migration; only postgres_version advances (47 -> 48).
--
-- ===========================================================================
-- btree_gin guard (repeated from 0042 on purpose)
-- ===========================================================================
-- Migration 0042 already installs btree_gin into public, and the runner always
-- applies 0042 before this file, so on a healthy database the DO block below
-- finds the extension and does nothing. It is repeated anyway because the one
-- scenario it exists for is not ordered: a DBA who dropped or relocated the
-- extension between the two migrations would otherwise get a raw
-- "operator class public.text_ops does not exist" failure here instead of the
-- preinstall-then-retry instruction 0042 established. pg_trgm (migration 0002)
-- is not re-guarded -- 0042 does not re-guard it either.

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
      RAISE EXCEPTION 'hotpath batch 4: installing btree_gin failed (%); preinstall it with a privileged role -- CREATE EXTENSION btree_gin WITH SCHEMA public; -- then migrate again',
        SQLERRM;
    END;
  ELSIF ext_schema <> 'public' THEN
    RAISE EXCEPTION 'hotpath batch 4: btree_gin is installed in schema % but must live in public (the composite indexes reference its opclass as public.text_ops); reinstall it in public, then migrate again',
      ext_schema;
  END IF;
END
$$;

-- Pre-existing same-named index validation (same block, same rationale, as
-- migrations 0042 and 0043 -- see 0042's header comment for the full
-- argument). `IF NOT EXISTS` alone would silently skip creation over a
-- leftover INVALID catalog row from an interrupted CONCURRENTLY build, or over
-- an operator-hand-built index with the right name but the wrong access
-- method / key list / opclass / collation / predicate, while this migration
-- still recorded itself as applied -- the ledger would claim an index that
-- cannot serve its query. The comparison reads exactly the semantic catalog
-- dimensions hotpath_indexes.py's _matches_shape reads (access method, per-key
-- pg_get_indexdef(...,n,true) echo, opclasses, collations, table ownership,
-- uniqueness, key count, partial predicate via pg_get_expr), normalized the
-- way _normalized_expr does (lowercase, collapse whitespace, strip ::text) --
-- NOT the full pg_get_indexdef() statement text, which would also render
-- storage-only clauses (a `WITH (fastupdate=off)` reloption is a legitimate
-- GIN write-amplification mitigation for exactly these indexes) and
-- false-RAISE on a perfectly usable index. Both failure modes RAISE (failing
-- the migration transaction, ledger not advanced) instead of auto-dropping: a
-- non-CONCURRENT rebuild inside this transaction would take a long blocking
-- lock on a large production table, so the operator resolves the residue
-- online (DROP INDEX CONCURRENTLY + rerun the builder script) and only then
-- migrates. backend/tests/test_hotpath_indexes_batch4.py re-derives every
-- expected value below from HOTPATH_INDEX_SPECS and asserts it appears
-- verbatim in this file; backend/tests/postgres/test_hotpath_indexes_batch4_live.py
-- covers the accept path, the wrong-shape reject and the INVALID-residue
-- reject against a real catalog.
DO $do$
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
      ('idx_sources_nb_title_file_trgm',
       'sources',
       'gin',
       ARRAY['notebook_id', 'lower(title)', 'lower(file_name)'],
       ARRAY['public:text_ops', 'public:gin_trgm_ops', 'public:gin_trgm_ops'],
       ARRAY['pg_catalog:C', 'pg_catalog:C', 'pg_catalog:C'],
       $pred$source_type <> all (array['memory', 'knowhow'])$pred$),
      ('idx_source_authors_nb_name_trgm',
       'source_authors',
       'gin',
       ARRAY['notebook_id', 'lower(name)'],
       ARRAY['public:text_ops', 'public:gin_trgm_ops'],
       ARRAY['pg_catalog:C', 'pg_catalog:C'],
       $pred$$pred$),
      ('idx_source_paper_meta_nb_ptitle_trgm',
       'source_paper_meta',
       'gin',
       ARRAY['notebook_id', 'lower(paper_title)'],
       ARRAY['public:text_ops', 'public:gin_trgm_ops'],
       ARRAY['pg_catalog:C', 'pg_catalog:C'],
       $pred$$pred$)
    ) AS v(index_name, expected_table, expected_am, expected_keys,
           expected_opclasses, expected_collations, expected_predicate)
  LOOP
    -- tbl join: index names are schema-wide, so a same-named index on ANOTHER
    -- table could otherwise pass every shape dimension while
    -- CREATE INDEX IF NOT EXISTS silently skips the intended table.
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
      RAISE EXCEPTION 'hotpath batch 4: pre-existing index % is INVALID (an interrupted CONCURRENTLY build left it unusable); run DROP INDEX CONCURRENTLY %, rerun scripts/build_hotpath_indexes.py --apply, then migrate again',
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
      RAISE EXCEPTION 'hotpath batch 4: pre-existing index % does not match the expected definition; resolve the name collision manually, then migrate again',
        rec.index_name;
    END IF;
  END LOOP;
END
$do$;

CREATE INDEX IF NOT EXISTS idx_sources_nb_title_file_trgm
  ON sources USING gin (
    notebook_id public.text_ops,
    lower(title) public.gin_trgm_ops,
    lower(file_name) public.gin_trgm_ops
  ) WHERE source_type NOT IN ('memory','knowhow');

CREATE INDEX IF NOT EXISTS idx_source_authors_nb_name_trgm
  ON source_authors USING gin (
    notebook_id public.text_ops,
    lower(name) public.gin_trgm_ops
  );

CREATE INDEX IF NOT EXISTS idx_source_paper_meta_nb_ptitle_trgm
  ON source_paper_meta USING gin (
    notebook_id public.text_ops,
    lower(paper_title) public.gin_trgm_ops
  );
