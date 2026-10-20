-- KG concept seed space (S1 of the KG analysis findings design, PR-4).
-- Mirrors SQLite v102 / _migration_102.
-- Design: docs/superpowers/specs/2026-10-19-kg-analysis-findings-design_zh.md
-- (section 五 S1, "实施细化").
--
-- concept_merge_candidates.seed_version records the concept seed space a
-- row's decision keys were minted in: seed_a / seed_b, or, for a row without
-- seeds (bridge candidates, early rows), canonical_a / canonical_b without
-- their "K-" prefix. Every existing row was minted in space 1. The seeds of a
-- v1 decision cannot be recomputed in SQL (the Python normalizer folds NFKC
-- and \w, which PostgreSQL's regular expressions do not reproduce byte for
-- byte on non-ASCII text, and the origin of a v1 seed's spaces is lost), so
-- nothing is rewritten here: each library's first full rebuild under seed
-- space 2 recomputes its v1 decisions from the current objects' names inside
-- its pending refresh (kg_merge.translate_v1_decisions), the precedent of the
-- Memory isolation's "migration marks, Python rebuilds". Writers always name
-- the column, so its default only ever reaches the rows that exist now.
--
-- unified_kg_state.concept_seed_version records the concept seed space of
-- the library's PUBLISHED merge. The statement that publishes a full rebuild
-- writes it; incremental fusion, catch-up and bridge ids follow it until the
-- library's next full rebuild. Every existing row gets 1 (their merges were
-- built in space 1); a row created from now on starts in space 2 (a new
-- notebook, a copy's dirty row, the lazy upsert of a library without one):
-- such a library has nothing published in space 1, and a copy carries the
-- published clusters of its source, built in space 2 once that source was
-- rebuilt. A notebook without a state row reads as space 2 for the same reason.
--
-- No backfill beyond the ADD COLUMN default, no index: the recompute reads one
-- notebook's decided rows through idx_candidates_nb_status (notebook_id, status).
ALTER TABLE concept_merge_candidates ADD COLUMN IF NOT EXISTS seed_version bigint NOT NULL DEFAULT 1;

ALTER TABLE unified_kg_state ADD COLUMN IF NOT EXISTS concept_seed_version bigint NOT NULL DEFAULT 1;
ALTER TABLE unified_kg_state ALTER COLUMN concept_seed_version SET DEFAULT 2;
