-- Bounded identity-only roster for reasoning source-scope resolution.
CREATE INDEX idx_sources_visible_identity
ON sources(notebook_id, created_at, id)
WHERE source_type NOT IN ('memory','knowhow');
