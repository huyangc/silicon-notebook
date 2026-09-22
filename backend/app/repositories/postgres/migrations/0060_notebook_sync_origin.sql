-- SQLite v80 parity. Non-empty marks this notebook a MIRROR imported from
-- another environment; the value is that source environment's identifier.
-- Every pre-existing row is local, so '' (the default) is the correct and
-- complete backfill -- there is nothing to reconstruct.
ALTER TABLE notebooks ADD COLUMN sync_origin text COLLATE "C" NOT NULL DEFAULT '';
