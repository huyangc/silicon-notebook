-- Attribute visible source creation to the user who performed the upload.
-- Existing rows predate actor provenance, so they are attributed best-effort
-- to their notebook owner. Hidden synthetic projections are not uploads.
ALTER TABLE sources ADD COLUMN uploaded_by text COLLATE "C";

UPDATE sources s
SET uploaded_by = n.created_by
FROM notebooks n
WHERE n.id = s.notebook_id
  AND s.uploaded_by IS NULL
  AND s.source_type NOT IN ('memory', 'knowhow');

CREATE INDEX idx_sources_uploaded_by_created
  ON sources (uploaded_by, created_at, id)
  WHERE uploaded_by IS NOT NULL
    AND source_type NOT IN ('memory', 'knowhow');
