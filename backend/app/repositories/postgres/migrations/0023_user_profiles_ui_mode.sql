-- Per-user interface mode preference ("auto" | "advanced").
-- Nullable, same pattern as upload_document_limit (0008): NULL means "no
-- explicit choice yet" and the application layer's read path falls back to
-- "auto" rather than this migration backfilling every existing row.

ALTER TABLE user_profiles ADD COLUMN ui_mode text COLLATE "C";
