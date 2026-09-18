-- SQLite v75 parity. Old non-placeholder names have ambiguous provenance;
-- preserve them as manual rather than guessing whether a person wrote them.
ALTER TABLE notebooks ADD COLUMN name_auto integer NOT NULL DEFAULT 0;
ALTER TABLE notebooks ADD COLUMN metadata_generation bigint NOT NULL DEFAULT 0;
UPDATE notebooks SET name_auto=1
WHERE btrim(name) IN ('','未命名笔记本','Untitled notebook');
