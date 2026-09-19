CREATE TABLE global_ask_conversations (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL,
    scope_json TEXT NOT NULL, submitted_via TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX idx_global_conversations_owner
    ON global_ask_conversations(user_id, updated_at, id);
CREATE TABLE global_ask_jobs (
    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL
        REFERENCES global_ask_conversations(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL, client_request_id TEXT,
    request_json TEXT NOT NULL, status TEXT NOT NULL,
    payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_global_ask_request
    ON global_ask_jobs(user_id, client_request_id) WHERE client_request_id IS NOT NULL;
CREATE UNIQUE INDEX idx_global_ask_running
    ON global_ask_jobs(conversation_id) WHERE status='running';
CREATE INDEX idx_global_jobs_conversation
    ON global_ask_jobs(conversation_id, created_at, id);
