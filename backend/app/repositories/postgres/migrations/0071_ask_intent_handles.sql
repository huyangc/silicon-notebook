-- MCP ``ask`` clarification handles. Mirrors SQLite v91 / _migration_91.
--
-- One row per clarification the MCP ``ask`` tool handed out. ``token`` (>= 128
-- random bits) is the opaque handle the Agent sends back; the understood
-- contract stays here instead of travelling through the 12 KB MCP output
-- budget. ``scope_key`` (single notebook: its id; global: the sorted resolved
-- notebook ids plus the conversation id) and ``question_sha256`` bind the
-- handle to what it was issued for; ``owner_id`` to whom. Rows expire after an
-- hour and are purged opportunistically on every write; a handle is never
-- consumed by use, so a failed run can be retried with the same answers.
--
-- Adapter-local bookkeeping, not replicated business data: registered
-- LOCAL / LOCAL_EPHEMERAL in the sync and shadow manifests.
CREATE TABLE ask_intent_handles (
  token text COLLATE "C" NOT NULL,
  owner_id text COLLATE "C" NOT NULL,
  scope_key text COLLATE "C" NOT NULL,
  question_sha256 text COLLATE "C" NOT NULL,
  contract_json jsonb NOT NULL,
  understanding_ms integer NOT NULL DEFAULT 0,
  created_at timestamp with time zone NOT NULL,
  expires_at timestamp with time zone NOT NULL,
  CONSTRAINT pk_ask_intent_handles PRIMARY KEY (token)
);

CREATE INDEX idx_ask_intent_handles_expires ON ask_intent_handles (expires_at);

-- Whether an Ask job ran with the private-Memory channel open (memory:read).
-- The MCP replay paths (get_ask, a keyed retry) refuse a job that ran with it
-- open to a caller whose channel is closed now: its stored answer and trace
-- may hold Memory. An integer flag (1/0) for SQLite parity. DEFAULT 1 is the
-- safe reading of every earlier row ("may hold Memory"), so no backfill.
ALTER TABLE ask_jobs ADD COLUMN IF NOT EXISTS memory_access integer NOT NULL DEFAULT 1;
