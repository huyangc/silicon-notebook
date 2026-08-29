-- Hot-path fix batch 1: six query-family groups (eight indexes) confirmed
-- missing by the production audit that also produced scripts/diag_pg_hotpaths.py's
-- INDEX_AUDIT_CANDIDATES / audit_reverse_fk_indexes (see that script's
-- module docstring "Index-audit note" -- this migration is the DDL half of
-- the same audit). Pure index additions: no query, no service code, and no
-- table/column/FK/unique-surface shape changes here.
--
-- Relationship to the offline CONCURRENTLY builder
-- (scripts/build_hotpath_indexes.py): on any database with pre-existing
-- production traffic, an operator runs that script's --apply mode FIRST,
-- online, with CREATE INDEX CONCURRENTLY (this migration runner executes
-- every migration inside a transaction, and CONCURRENTLY cannot run inside
-- one). Once all eight indexes exist, this migration's IF NOT EXISTS clauses
-- make it a no-op ledger entry -- it only takes a lock to record that schema
-- version 39 has been reached. On a fresh deploy (new database, no traffic
-- yet) this migration is the only step needed; the offline script has
-- nothing left to build and its default (no-argument) inspect mode reports
-- every index "ready" -- the script has no separate --inspect flag.
--
-- Each index's DDL text (table, columns, predicate) is duplicated by hand in
-- scripts/build_hotpath_indexes.py's HOTPATH_INDEX_SPECS for the CONCURRENTLY
-- form; backend/tests/test_hotpath_indexes.py cross-checks the two files so
-- they cannot drift apart.
--
-- Query families served (grep-verified against the current tree; each
-- lacked any covering index before this migration):
--
--  1. idx_clusters_nb_canonical ON concept_clusters(notebook_id, canonical_id)
--     Concept-detail / co-mention peer-name / relation-endpoint-name lookups
--     that key on canonical_id within a notebook (unified_kg_store.py's
--     canonical_description read, concept_map, cluster_map,
--     canonical_names_for, member_canonical_map, relation_endpoint_name_rows'
--     join predicate) -- previously a whole-notebook-segment scan of
--     concept_clusters, called out in that file's own comments.
--
--  2. idx_clusters_nb_canonical_name_lower
--     ON concept_clusters(notebook_id, lower(canonical_name))
--     unified_kg_store.py's resolve_focal: "focal 归一键 ->
--     canonical_id (lower(canonical_name)==key, 多簇取成员最多者)". No prior
--     index makes lower(canonical_name) selective, so every comparison
--     diagnostics answer that resolves a focal-node key pays a full scan.
--
--  3. Three reverse-FK covering indexes. Each of these tables has a foreign
--     key to notebooks(id) ON DELETE CASCADE but no index whose leading
--     column is notebook_id, so notebook deletion cascades (and any other
--     notebook_id-scoped read) degrade to a sequential scan:
--       idx_extraction_runs_notebook ON extraction_runs(notebook_id)
--       idx_knowledge_source_fact_elements_notebook
--         ON knowledge_source_fact_elements(notebook_id)
--       idx_memory_items_notebook ON memory_items(notebook_id)
--     (memory_items already has two composite indexes, but both lead with
--     created_by, not notebook_id, so neither serves a notebook-only scan.)
--
--  4. idx_knowledge_relations_nb_source_target_edge
--     ON knowledge_relations(notebook_id, source_object_id, target_object_id,
--     edge_type)
--     Serves in_network_relation_rows (answer assembly's in_network_relations
--     call): "WHERE notebook_id=%s AND review_status!='rejected' AND
--     source_object_id IN (...) AND target_object_id IN (...)". The existing
--     idx_knowledge_relations_nb_source / _nb_target indexes only cover one
--     endpoint each; a hub object (high fan-out on one endpoint) can make the
--     planner pick the wrong single-endpoint index and then filter the other
--     endpoint row-by-row. This composite gives the planner a single index
--     that already narrows on both endpoints.
--
--  5. idx_chunks_source_ordinal ON chunks(source_id, ordinal)
--     Serves chunk_section_rows (exact_lookup's chunks_by_section): "WHERE
--     c.notebook_id=%s AND c.source_id=%s AND (c.section_path=%s OR
--     c.section_path LIKE %s) ORDER BY c.ordinal LIMIT %s". The existing
--     idx_chunks_source only covers source_id; without ordinal in the same
--     index, a hub source with many chunks lets the planner misjudge the
--     LIMIT-driven plan and choose a full sort instead of an index-ordered
--     scan.
--
--  6. idx_sources_nb_hidden_type ON sources(notebook_id, source_type)
--     WHERE source_type IN ('memory','knowhow')  -- PARTIAL, not a full index.
--     Serves hidden_source_ids's own predicate, "s.source_type IN
--     ('memory','knowhow')" (source_store.py) -- memory/knowhow rows are the
--     rare minority of a notebook's sources, so a partial index keyed
--     exactly on that predicate turns hidden_source_ids's read into a narrow
--     index scan instead of the full per-notebook table scan the ~48k-row
--     production case pays today.
--
--     A full (non-partial) index was deliberately rejected: the OTHER
--     consumer of source_type on this table filters the complementary
--     majority case, "source_type NOT IN ('memory','knowhow')"
--     (VISIBLE_SOURCE_TYPES_PREDICATE), and that predicate is evaluated as a
--     projected column on a scan the caller was already doing for other
--     reasons (source_change_signal_rows's own comment: "求值放在投影里而不是
--     另开一条 id 查询,是因为 source_type 上没有索引"). Since NOT IN matches
--     nearly every row in a notebook, an index over the full table would
--     buy that path nothing (the planner would still visit almost every
--     row) while paying full write-amplification on every source insert.
--     The partial predicate captures 100% of the benefit at a fraction of
--     the index's steady-state size and maintenance cost.
--
-- Known write-amplification debt (registered here, not addressed in this
-- batch -- deleting a live index is a separate, deliberate operator call,
-- not something a migration should ever do automatically):
--
--   * idx_chunks_source (0003_core_indexes.sql), ON chunks(source_id), is now
--     fully covered by group 5's idx_chunks_source_ordinal above -- any query
--     that could use the former's single-column leading key can equally use
--     the latter's two-column one. Once production has verified
--     idx_chunks_source_ordinal is stable, an operator can retire the
--     now-redundant one with `DROP INDEX CONCURRENTLY idx_chunks_source;`.
--   * knowledge_relations already carries three same-leading-prefix indexes
--     on the source_object_id side (idx_knowledge_relations_nb_source,
--     idx_knowledge_relations_nb_source_id, and now group 4's
--     idx_knowledge_relations_nb_source_target_edge above) -- a pre-existing
--     overlap this migration does not introduce and leaves as-is.

CREATE INDEX IF NOT EXISTS idx_clusters_nb_canonical
  ON concept_clusters(notebook_id, canonical_id);

CREATE INDEX IF NOT EXISTS idx_clusters_nb_canonical_name_lower
  ON concept_clusters(notebook_id, lower(canonical_name));

CREATE INDEX IF NOT EXISTS idx_extraction_runs_notebook
  ON extraction_runs(notebook_id);

CREATE INDEX IF NOT EXISTS idx_knowledge_source_fact_elements_notebook
  ON knowledge_source_fact_elements(notebook_id);

CREATE INDEX IF NOT EXISTS idx_memory_items_notebook
  ON memory_items(notebook_id);

CREATE INDEX IF NOT EXISTS idx_knowledge_relations_nb_source_target_edge
  ON knowledge_relations(notebook_id, source_object_id, target_object_id, edge_type);

CREATE INDEX IF NOT EXISTS idx_chunks_source_ordinal
  ON chunks(source_id, ordinal);

CREATE INDEX IF NOT EXISTS idx_sources_nb_hidden_type
  ON sources(notebook_id, source_type)
  WHERE source_type IN ('memory', 'knowhow');
