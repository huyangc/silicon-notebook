-- Indexed per-source, per-element-type keyset order for bounded collection
-- enumeration (formula/table/image/code_block listings).
CREATE INDEX idx_source_elements_source_type
  ON source_elements(source_id, element_type, created_at, id);
