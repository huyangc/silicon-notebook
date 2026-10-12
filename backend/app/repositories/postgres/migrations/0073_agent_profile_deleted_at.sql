-- Agent Profile soft delete. Mirrors SQLite v93 / _migration_93.
--
-- deleted_at is NULL for a listed Profile and the deletion time once its owner
-- deleted it (status is 'revoked' and its tokens are revoked in the same
-- write). Memory, source and observation rows keep their agent_profile_id.
-- A new nullable column leaves ck_agent_profiles_status unchanged.
ALTER TABLE agent_profiles ADD COLUMN deleted_at timestamp with time zone;
