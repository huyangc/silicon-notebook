-- Memory, Knowhow, model-status, and promotion operational indexes.
CREATE INDEX idx_knowhow_cell_code_row ON knowhow_cell_code(row_id);
CREATE INDEX idx_knowhow_cells_column_normalized_anchor_row ON knowhow_cells (column_id, (btrim(content_md, chr(9) || chr(10) || chr(11) || chr(12) || chr(13) || chr(32) || chr(160) || chr(5760) || chr(8192) || chr(8193) || chr(8194) || chr(8195) || chr(8196) || chr(8197) || chr(8198) || chr(8199) || chr(8200) || chr(8201) || chr(8202) || chr(8232) || chr(8233) || chr(8239) || chr(8287) || chr(12288) || chr(65279))), row_id);
CREATE INDEX idx_knowhow_cells_row ON knowhow_cells(row_id);
CREATE INDEX idx_knowhow_columns_table ON knowhow_columns(table_id);
CREATE INDEX idx_knowhow_rows_table ON knowhow_rows(table_id);
CREATE INDEX idx_knowhow_tables_nb ON knowhow_tables(notebook_id);
CREATE INDEX idx_memory_agent_candidate ON memory_items(created_by, notebook_id, status, agent_profile_id);
CREATE INDEX idx_memory_embeddings_model ON memory_embeddings(model, dimension);
CREATE INDEX idx_memory_owner_notebook_status ON memory_items(created_by, notebook_id, status, updated_at DESC);
CREATE INDEX idx_memory_revisions_memory ON memory_revisions(memory_id, revision DESC);
CREATE INDEX idx_model_service_status_user_checked ON model_service_status(user_id, checked_at DESC);
CREATE INDEX idx_promotion_nb ON promotion_candidates(notebook_id, status);
CREATE INDEX idx_promotion_status ON promotion_candidates(status);
