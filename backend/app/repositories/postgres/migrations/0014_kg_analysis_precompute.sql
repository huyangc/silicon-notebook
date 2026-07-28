-- Mirror SQLite v32's three KG-quality-analysis precompute products so the
-- packaged PostgreSQL schema stays in one-for-one parity with the reviewed
-- SQLite contract.
--
-- All three tables hold derived data only: rebuild_communities rewrites them
-- wholesale inside ONE transaction, so a precompute batch is either entirely
-- visible or entirely invisible. No incremental writer exists, which is why
-- there is no updated_at column and no conflict-resolution semantics.
--
-- kg_analysis_artifacts is the product ledger: exactly one row per product,
-- carrying the kg_mutation_seq it was built at. Its row's existence — not the
-- detail tables' row counts — is what "this product is present" means, so a
-- legitimately empty product (a single-community graph produces zero
-- cross-board edges) stays distinguishable from a product that was never
-- computed.
--
-- None of the three tables carries a `level` column. The community layer's
-- freshness gate (unified_kg_state.community_seq) is not level-scoped, so one
-- precompute always produces ONE self-consistent product set; the level it
-- describes is recorded in the ledger payload's `level` field. A level column on
-- kg_community_edges alone would split the delete scope three ways: a level=1
-- rebuild would wipe the level-0 ledger yet leave level-0 detail rows behind —
-- rows that "do not exist" by the ledger rule but still answer WHERE level = 0.
--
-- source_id deliberately carries NO foreign key: knowledge_objects.source_id has
-- none either, and deployed databases do contain orphan references left by
-- historical cleanups. A foreign key here would make the precompute fail on
-- exactly those databases.

CREATE TABLE kg_community_edges (
  notebook_id text COLLATE "C" NOT NULL,
  src_community_id text COLLATE "C" NOT NULL,
  dst_community_id text COLLATE "C" NOT NULL,
  weight bigint NOT NULL DEFAULT 0,
  CONSTRAINT pk_kg_community_edges
    PRIMARY KEY (notebook_id, src_community_id, dst_community_id)
);

CREATE TABLE kg_source_profiles (
  notebook_id text COLLATE "C" NOT NULL,
  source_id text COLLATE "C" NOT NULL,
  n_objects bigint NOT NULL DEFAULT 0,
  n_graph_objects bigint NOT NULL DEFAULT 0,
  top_community_id text COLLATE "C" NOT NULL DEFAULT '',
  top_share double precision NOT NULL DEFAULT 0,
  community_spread bigint NOT NULL DEFAULT 0,
  mainstream_share double precision NOT NULL DEFAULT 0,
  CONSTRAINT pk_kg_source_profiles PRIMARY KEY (notebook_id, source_id)
);

CREATE TABLE kg_analysis_artifacts (
  notebook_id text COLLATE "C" NOT NULL,
  kind text COLLATE "C" NOT NULL,
  kg_mutation_seq bigint NOT NULL DEFAULT '-1',
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL,
  CONSTRAINT pk_kg_analysis_artifacts PRIMARY KEY (notebook_id, kind)
);

ALTER TABLE kg_community_edges
  ADD CONSTRAINT fk_kg_community_edges_notebook_id__notebooks
  FOREIGN KEY (notebook_id) REFERENCES notebooks (id)
  ON UPDATE NO ACTION ON DELETE CASCADE;

ALTER TABLE kg_source_profiles
  ADD CONSTRAINT fk_kg_source_profiles_notebook_id__notebooks
  FOREIGN KEY (notebook_id) REFERENCES notebooks (id)
  ON UPDATE NO ACTION ON DELETE CASCADE;

ALTER TABLE kg_analysis_artifacts
  ADD CONSTRAINT fk_kg_analysis_artifacts_notebook_id__notebooks
  FOREIGN KEY (notebook_id) REFERENCES notebooks (id)
  ON UPDATE NO ACTION ON DELETE CASCADE;

CREATE INDEX idx_kg_source_profiles_nb_mainstream
  ON kg_source_profiles(notebook_id, mainstream_share);
