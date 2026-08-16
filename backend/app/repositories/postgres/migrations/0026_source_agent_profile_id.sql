-- Agent provenance on sources. NULL means "a person added this source", so
-- nothing is backfilled: every already-deployed row is user-added by
-- definition. Same nullable, un-indexed, FK-free shape as sources.memory_id
-- (0001) -- the Agent-facing delete check is a single-row primary-key read,
-- nothing enumerates "sources of this agent", and provenance must outlive the
-- agent profile row.

ALTER TABLE sources ADD COLUMN agent_profile_id text COLLATE "C";
