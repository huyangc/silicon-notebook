-- PostgreSQL-only index repair: an unbounded seed cannot be a btree key.
-- Keep the complete seed as data and use its fixed-width digest only to find
-- candidates. Readers must also compare the original seed to reject collisions.
-- Non-unique, as before: this changes neither seed identity nor multiplicity.
-- Apply with application writers stopped. Rebuilding the persistent cluster
-- index may need a maintenance-specific PostgreSQL statement timeout on large
-- libraries; the deployment setting can be raised for the migration process.
DROP INDEX idx_kg_canonical_scratch_nb_run_seed;
CREATE INDEX idx_kg_canonical_scratch_nb_run_seed
  ON kg_canonical_scratch(notebook_id, run_id, md5(seed));

-- The published canonical name retains the same unbounded user text. Its
-- case-insensitive lookup needs the same fixed-width candidate index.
DROP INDEX idx_clusters_nb_canonical_name_lower;
CREATE INDEX idx_clusters_nb_canonical_name_lower
  ON concept_clusters(notebook_id, md5(lower(canonical_name)));
