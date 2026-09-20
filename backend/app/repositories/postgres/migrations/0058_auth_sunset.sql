ALTER TABLE users ADD COLUMN local_login_name TEXT COLLATE "C";
ALTER TABLE users ADD COLUMN auth_revision INTEGER NOT NULL DEFAULT 0;
UPDATE users SET local_login_name=username WHERE username<>'';
CREATE UNIQUE INDEX idx_users_local_login_name ON users(local_login_name) WHERE local_login_name IS NOT NULL;
ALTER TABLE auth_sessions ADD COLUMN auth_source TEXT NOT NULL DEFAULT 'local';
ALTER TABLE auth_sessions ADD COLUMN absolute_expires_at TIMESTAMPTZ;
ALTER TABLE auth_sessions ADD COLUMN provider_namespace TEXT NOT NULL DEFAULT '';
ALTER TABLE auth_sessions ADD COLUMN external_subject TEXT NOT NULL DEFAULT '';
CREATE TABLE auth_policy (
 id INTEGER NOT NULL,
 mode TEXT NOT NULL DEFAULT 'local',
 revision INTEGER NOT NULL DEFAULT 0,
 provider_id TEXT NOT NULL DEFAULT '',
 provider_namespace TEXT NOT NULL DEFAULT '',
 config_generation TEXT NOT NULL DEFAULT '',
 plugin_id TEXT NOT NULL DEFAULT '',
 retired_at TIMESTAMPTZ,
 updated_by TEXT NOT NULL DEFAULT '',
 CONSTRAINT pk_auth_policy PRIMARY KEY(id),
 CONSTRAINT ck_auth_policy_singleton CHECK(id=1),
 CONSTRAINT ck_auth_policy_mode CHECK(mode IN ('local','dual','binding_required','sso_only','retired'))
);
CREATE TABLE external_identities (
 provider_namespace TEXT COLLATE "C" NOT NULL,
 subject TEXT COLLATE "C" NOT NULL,
 user_id TEXT COLLATE "C" NOT NULL,
 status TEXT NOT NULL DEFAULT 'active',
 created_at TIMESTAMPTZ NOT NULL,
 updated_at TIMESTAMPTZ NOT NULL,
 last_login_at TIMESTAMPTZ,
 CONSTRAINT pk_external_identities PRIMARY KEY(provider_namespace,subject),
 CONSTRAINT fk_external_identities_user FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE UNIQUE INDEX idx_external_identities_active_user
 ON external_identities(user_id,provider_namespace) WHERE status='active';
CREATE TABLE auth_transactions (
 token_digest TEXT COLLATE "C" NOT NULL,
 purpose TEXT NOT NULL,
 browser_digest TEXT COLLATE "C" NOT NULL,
 payload TEXT NOT NULL,
 expires_at BIGINT NOT NULL,
 CONSTRAINT pk_auth_transactions PRIMARY KEY(token_digest)
);
CREATE INDEX idx_auth_transactions_expiry ON auth_transactions(expires_at);
CREATE TABLE auth_policy_audit (
 id TEXT COLLATE "C" NOT NULL,
 actor_id TEXT COLLATE "C" NOT NULL,
 previous_mode TEXT NOT NULL,
 mode TEXT NOT NULL,
 revision INTEGER NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 CONSTRAINT pk_auth_policy_audit PRIMARY KEY(id)
);
CREATE TABLE auth_identity_audit (
 id TEXT COLLATE "C" NOT NULL,
 actor_id TEXT COLLATE "C" NOT NULL,
 target_user_id TEXT COLLATE "C" NOT NULL,
 action TEXT NOT NULL,
 provider_namespace TEXT COLLATE "C" NOT NULL,
 subject TEXT COLLATE "C" NOT NULL,
 grant_reference TEXT COLLATE "C" NOT NULL DEFAULT '',
 created_at TIMESTAMPTZ NOT NULL,
 CONSTRAINT pk_auth_identity_audit PRIMARY KEY(id)
);
CREATE INDEX idx_auth_identity_audit_created
 ON auth_identity_audit(created_at,id);
