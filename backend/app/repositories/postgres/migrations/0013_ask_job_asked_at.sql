-- Preserve the browser-captured submission instant while an Ask job is still
-- running; completed answers retain the same value in their payload JSON.
ALTER TABLE ask_jobs
  ADD COLUMN asked_at TEXT COLLATE "C" NOT NULL DEFAULT '';
