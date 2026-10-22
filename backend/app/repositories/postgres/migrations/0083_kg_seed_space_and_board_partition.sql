-- KG concept seed space and board partition (S1 and S2 of the KG analysis
-- findings design, PR-4). Mirrors SQLite v102 / _migration_102.
-- Design: docs/superpowers/specs/2026-10-19-kg-analysis-findings-design_zh.md
-- (section 五 S1 "实施细化" and section 五 S2); the partition algorithm lives in
-- backend/app/services/kg_partition.py. The two halves share one migration
-- because one event applies both: a library's first full rebuild after the
-- upgrade re-merges its concepts in seed space 2 and re-partitions its topic
-- boards cold.
--
-- Concept seed space (S1).
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
--
-- Board partition (S2).
--
-- unified_kg_state.community_partition_version is the
-- kg_partition.PARTITION_ALGO_VERSION the published board partition was built
-- with, and community_partition_mode how it was built (warm / cold /
-- cold_fallback). Both are written in the transaction that publishes the
-- partition. 0 / '' read as "built by the algorithm before this one", so the
-- next rebuild starts cold: the one-time re-partition the design announces.
--
-- communities.board_key is a board's identity across generations
-- (communities.id is re-minted every generation). Existing rows are
-- backfilled to their own id, so the first partition built by the new
-- algorithm can carry a matching board's key forward. communities is a
-- synced table: on a deployment with sync capture enabled the backfill logs
-- one upsert per board, and the receiving side backfills the same value, so
-- re-sending them is harmless (the ADD COLUMN defaults above fire no capture
-- trigger, and unified_kg_state is not touched row by row here, so unlike on
-- SQLite no state row is exported by this migration). No index: board_key is
-- only ever read through a notebook's published generation
-- (communities(notebook_id, level)).
--
-- Cost, stated as measured (PostgreSQL 16, 270,000 communities rows of which
-- 2% are large boards stored out of line in TOAST, with the production
-- indexes and the sync-capture trigger on the table, statement_timeout 30 s):
-- the board_key UPDATE took 4.1 s with capture off and 5.4 s with capture on
-- (which also writes 270,000 change-log rows); the ADD COLUMN statements
-- carry constant defaults and only touch the catalogue. Nothing came near the
-- statement timeout.
ALTER TABLE concept_merge_candidates ADD COLUMN IF NOT EXISTS seed_version bigint NOT NULL DEFAULT 1;

ALTER TABLE unified_kg_state ADD COLUMN IF NOT EXISTS concept_seed_version bigint NOT NULL DEFAULT 1;
ALTER TABLE unified_kg_state ALTER COLUMN concept_seed_version SET DEFAULT 2;

ALTER TABLE unified_kg_state ADD COLUMN IF NOT EXISTS community_partition_version bigint NOT NULL DEFAULT 0;
ALTER TABLE unified_kg_state ADD COLUMN IF NOT EXISTS community_partition_mode text COLLATE "C" NOT NULL DEFAULT '';
ALTER TABLE communities ADD COLUMN IF NOT EXISTS board_key text COLLATE "C" NOT NULL DEFAULT '';
UPDATE communities SET board_key = id WHERE board_key = '';
