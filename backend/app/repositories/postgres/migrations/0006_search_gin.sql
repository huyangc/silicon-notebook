-- Rebuild SQLite FTS5 candidate fields with pg_trgm; ranking remains application-owned.
CREATE INDEX idx_chunks_text_trgm ON chunks USING gin (text public.gin_trgm_ops);
CREATE INDEX idx_knowledge_objects_name_trgm
  ON knowledge_objects USING gin (((payload ->> 'name') COLLATE "C") public.gin_trgm_ops);
CREATE INDEX idx_memory_items_title_trgm
  ON memory_items USING gin (title public.gin_trgm_ops);
CREATE INDEX idx_memory_items_content_md_trgm
  ON memory_items USING gin (content_md public.gin_trgm_ops);
CREATE INDEX idx_memory_items_tags_trgm
  ON memory_items USING gin (((tags_json::text) COLLATE "C") public.gin_trgm_ops);
