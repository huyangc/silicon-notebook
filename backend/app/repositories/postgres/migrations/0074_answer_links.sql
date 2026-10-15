-- MCP answer links. Mirrors SQLite v94 / _migration_94.
--
-- One row per expiring, anonymous, read-only link to ONE answered MCP ask job
-- (single-notebook ``askjob-...`` or global ``gask-...``). ``token`` (>= 128
-- random bits, ``alnk-`` prefixed) is the capability itself; the row names the
-- job it opens, ``owner_id`` (whose answer it is) and ``agent_token_id`` (the
-- Agent token that issued it, so revoking the token kills the link). Opening a
-- link re-checks the owner's and the token's rights at read time against the
-- live state; ``expires_at`` is judged at read time too. Rows are purged
-- opportunistically on the issuing write path (past expiry plus a grace
-- period, or once the job or the issuing token is gone).
--
-- One live link per (job, issuing token): a retry or a later ``get_ask``
-- re-uses it instead of minting a second one. A plain unique index, because
-- ``expires_at > now()`` cannot be an index predicate (not immutable); the
-- issuing write deletes the expired row of its own key first.
--
-- No foreign keys on purpose: a job, a conversation or an Agent token can be
-- deleted by half a dozen unrelated paths, and a dangling link simply 404s on
-- open (the job or token no longer resolves); the sweep removes the rows.
--
-- Adapter-local bookkeeping bound to this environment's Agent tokens, not
-- replicated business data: registered LOCAL / LOCAL_EPHEMERAL in the sync and
-- shadow manifests.
CREATE TABLE answer_links (
  token text COLLATE "C" NOT NULL,
  job_kind text COLLATE "C" NOT NULL,
  job_id text COLLATE "C" NOT NULL,
  owner_id text COLLATE "C" NOT NULL,
  agent_token_id text COLLATE "C" NOT NULL,
  created_at timestamp with time zone NOT NULL,
  expires_at timestamp with time zone NOT NULL,
  CONSTRAINT pk_answer_links PRIMARY KEY (token),
  CONSTRAINT ck_answer_links_job_kind CHECK (job_kind IN ('notebook', 'global'))
);

CREATE UNIQUE INDEX uq_answer_links_job_token ON answer_links (job_kind, job_id, agent_token_id);
CREATE INDEX idx_answer_links_expires ON answer_links (expires_at);

-- Whether the Agent token may issue answer links (the ``ask`` tool's
-- delivery="link"). An integer flag (1/0) for SQLite parity; DEFAULT 0 = every
-- existing token stays unable to, no backfill.
ALTER TABLE agent_access_tokens ADD COLUMN IF NOT EXISTS allow_answer_links integer NOT NULL DEFAULT 0;
