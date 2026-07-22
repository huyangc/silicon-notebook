-- PostgreSQL-owned candidate search and operational indexes.
-- pg_trgm is a database-wide prerequisite. Keep it in the stable shared
-- namespace so dropping a disposable/application schema cannot remove it.
-- A restricted production migration role may require the DBA to preinstall
-- the extension here; an incompatible existing namespace fails closed below.
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;

DO $migration$
DECLARE
  extension_schema text;
BEGIN
  SELECT n.nspname INTO extension_schema
  FROM pg_extension e
  JOIN pg_namespace n ON n.oid = e.extnamespace
  WHERE e.extname = 'pg_trgm';
  IF extension_schema IS DISTINCT FROM 'public' THEN
    RAISE EXCEPTION 'pg_trgm must be installed in the public schema';
  END IF;
END
$migration$;

CREATE INDEX idx_agent_profiles_owner_status ON agent_profiles(owner_id, status, updated_at DESC);
CREATE INDEX idx_agent_token_notebooks_notebook ON agent_token_notebooks(notebook_id, token_id);
CREATE INDEX idx_agent_tokens_profile ON agent_access_tokens(agent_profile_id, revoked_at, expires_at);
CREATE INDEX idx_answers_conversation ON answers(conversation_id);
CREATE INDEX idx_answers_nb ON answers(notebook_id);
CREATE INDEX idx_ask_jobs_conv ON ask_jobs(conversation_id);
CREATE INDEX idx_ask_jobs_nb_status ON ask_jobs(notebook_id, status);
CREATE INDEX idx_auth_sessions_user ON auth_sessions(user_id);
CREATE INDEX idx_candidates_nb_status ON concept_merge_candidates(notebook_id, status);
CREATE INDEX idx_chunk_embeddings_nb ON chunk_embeddings(notebook_id);
CREATE INDEX idx_chunks_nb ON chunks(notebook_id);
CREATE INDEX idx_chunks_nb_created ON chunks(notebook_id, created_at);
CREATE INDEX idx_chunks_source ON chunks(source_id);
CREATE INDEX idx_clusters_member ON concept_clusters(member_object_id);
CREATE INDEX idx_clusters_nb ON concept_clusters(notebook_id);
CREATE INDEX idx_clusters_nb_created ON concept_clusters(notebook_id, created_at);
CREATE INDEX idx_comentions_nb_b ON concept_comentions(notebook_id, canonical_b);
CREATE INDEX idx_commmem_nb_can ON community_members(notebook_id, canonical_id);
CREATE INDEX idx_commmem_nb_comm ON community_members(notebook_id, community_id);
CREATE INDEX idx_communities_nb_level ON communities(notebook_id, level);
CREATE INDEX idx_conflict_candidates_nb_status ON kg_conflict_candidates(notebook_id, status);
CREATE INDEX idx_conversations_created_by ON conversations(created_by);
CREATE INDEX idx_element_embeddings_nb ON element_embeddings(notebook_id);
CREATE INDEX idx_element_embeddings_source ON element_embeddings(source_id);
CREATE INDEX idx_extraction_runs_source_created ON extraction_runs(source_id, created_at);
CREATE INDEX idx_feedback_answer ON feedback(answer_id);
CREATE INDEX idx_feedback_nb_rating ON feedback(notebook_id, rating);
CREATE INDEX idx_kg_build_jobs_nb_created ON kg_build_jobs(notebook_id, created_at DESC, id DESC);
CREATE UNIQUE INDEX idx_kg_build_jobs_one_running ON kg_build_jobs(notebook_id) WHERE status = 'running';
CREATE INDEX idx_kg_cluster_scratch_nb_run ON kg_cluster_scratch(notebook_id, run_id);
CREATE INDEX idx_knowhow_cell_code_row ON knowhow_cell_code(row_id);
CREATE INDEX idx_knowhow_cells_column_normalized_anchor_row ON knowhow_cells (column_id, (btrim(content_md, chr(9) || chr(10) || chr(11) || chr(12) || chr(13) || chr(32) || chr(160) || chr(5760) || chr(8192) || chr(8193) || chr(8194) || chr(8195) || chr(8196) || chr(8197) || chr(8198) || chr(8199) || chr(8200) || chr(8201) || chr(8202) || chr(8232) || chr(8233) || chr(8239) || chr(8287) || chr(12288) || chr(65279))), row_id);
CREATE INDEX idx_knowhow_cells_row ON knowhow_cells(row_id);
CREATE INDEX idx_knowhow_columns_table ON knowhow_columns(table_id);
CREATE INDEX idx_knowhow_rows_table ON knowhow_rows(table_id);
CREATE INDEX idx_knowhow_tables_nb ON knowhow_tables(notebook_id);
CREATE INDEX idx_knowledge_embeddings_nb ON knowledge_embeddings(notebook_id);
CREATE INDEX idx_knowledge_embeddings_nb_created ON knowledge_embeddings(notebook_id, created_at);
CREATE INDEX idx_knowledge_objects_nb_status ON knowledge_objects(notebook_id, status);
CREATE INDEX idx_knowledge_objects_nb_type_created ON knowledge_objects(notebook_id, object_type, created_at, id);
CREATE INDEX idx_knowledge_objects_nb_type_status ON knowledge_objects(notebook_id, object_type, status);
CREATE INDEX idx_knowledge_objects_nb_updated ON knowledge_objects(notebook_id, updated_at);
CREATE INDEX idx_knowledge_objects_source ON knowledge_objects(source_id);
CREATE INDEX idx_knowledge_relations_nb_created ON knowledge_relations(notebook_id, created_at);
CREATE INDEX idx_knowledge_relations_nb_review ON knowledge_relations(notebook_id, review_status);
CREATE INDEX idx_knowledge_relations_nb_source ON knowledge_relations(notebook_id, source_object_id);
CREATE INDEX idx_knowledge_relations_nb_target ON knowledge_relations(notebook_id, target_object_id);
CREATE INDEX idx_knowledge_relations_source ON knowledge_relations(source_id);
CREATE INDEX idx_kos_notebook ON knowledge_object_sources(notebook_id);
CREATE INDEX idx_kos_object ON knowledge_object_sources(object_id);
CREATE INDEX idx_kos_source ON knowledge_object_sources(source_id);
CREATE INDEX idx_memory_agent_candidate ON memory_items(created_by, notebook_id, status, agent_profile_id);
CREATE UNIQUE INDEX idx_memory_answer_once ON memory_items(created_by, source_answer_id) WHERE source_answer_id IS NOT NULL;
CREATE INDEX idx_memory_embeddings_model ON memory_embeddings(model, dimension);
CREATE INDEX idx_memory_owner_notebook_status ON memory_items(created_by, notebook_id, status, updated_at DESC);
CREATE INDEX idx_memory_revisions_memory ON memory_revisions(memory_id, revision DESC);
CREATE INDEX idx_model_service_status_user_checked ON model_service_status(user_id, checked_at DESC);
CREATE INDEX idx_notebook_assets_nb ON notebook_assets(notebook_id);
CREATE INDEX idx_notebook_assets_source ON notebook_assets(source_id);
CREATE INDEX idx_notebook_bases_base ON notebook_bases(base_notebook_id);
CREATE INDEX idx_notebook_members_user ON notebook_members(user_id);
CREATE INDEX idx_notebooks_created_by ON notebooks(created_by);
CREATE UNIQUE INDEX idx_notebooks_share_token ON notebooks(share_token) WHERE share_token IS NOT NULL;
CREATE INDEX idx_promotion_nb ON promotion_candidates(notebook_id, status);
CREATE UNIQUE INDEX idx_promotion_object ON promotion_candidates(object_id) WHERE status NOT IN ('approved', 'rejected');
CREATE INDEX idx_promotion_status ON promotion_candidates(status);
CREATE INDEX idx_relation_embeddings_nb ON relation_embeddings(notebook_id);
CREATE INDEX idx_reports_nb_created ON reports(notebook_id, created_at DESC);
CREATE INDEX idx_source_authors_nb ON source_authors(notebook_id);
CREATE INDEX idx_source_authors_source ON source_authors(source_id);
CREATE INDEX idx_source_elements_source ON source_elements(source_id);
CREATE INDEX idx_source_elements_source_created ON source_elements(source_id, created_at, id);
CREATE INDEX idx_source_paper_meta_nb ON source_paper_meta(notebook_id);
CREATE UNIQUE INDEX idx_sources_memory_id ON sources(memory_id) WHERE memory_id IS NOT NULL AND memory_id != '';
CREATE INDEX idx_sources_nb_parse_status ON sources(notebook_id, parse_status);
CREATE INDEX idx_sources_nb_parse_status_type ON sources(notebook_id, parse_status, source_type);
CREATE INDEX idx_sources_notebook_created ON sources(notebook_id, created_at);
CREATE INDEX idx_sources_notebook_status ON sources(notebook_id, status);
CREATE UNIQUE INDEX idx_users_username ON users(username) WHERE username != '';

-- Rebuild SQLite FTS5 candidate fields with pg_trgm; ranking remains application-owned.
CREATE INDEX idx_chunks_text_trgm ON chunks USING gin (text public.gin_trgm_ops);
CREATE INDEX idx_knowledge_objects_name_trgm
  ON knowledge_objects USING gin ((payload ->> 'name') public.gin_trgm_ops);
CREATE INDEX idx_memory_items_title_trgm
  ON memory_items USING gin (title public.gin_trgm_ops);
CREATE INDEX idx_memory_items_content_md_trgm
  ON memory_items USING gin (content_md public.gin_trgm_ops);
CREATE INDEX idx_memory_items_tags_trgm
  ON memory_items USING gin ((tags_json::text) public.gin_trgm_ops);
