-- Unified authentication simplified. Mirrors SQLite v92 / _migration_92.
--
-- The staged-cutover policy, its audit, the (namespace, subject) identity
-- mapping and the separate local login name are gone: an external login now
-- signs in the account whose username equals the provider username, and
-- whether unified authentication is on follows the provider plugin switch, not
-- a stored policy. Production never left the 'local' policy and the mapping
-- table was empty, so nothing is carried over. Pending OAuth transactions use
-- a new payload shape and are dropped. auth_identity_audit keeps its rows; the
-- grant reference column served only the removed administrator grants.
DROP TABLE auth_policy_audit;
DROP TABLE auth_policy;
DROP TABLE external_identities;
DROP INDEX idx_users_local_login_name;
ALTER TABLE users DROP COLUMN local_login_name;
ALTER TABLE auth_identity_audit DROP COLUMN grant_reference;
DELETE FROM auth_transactions;

-- users.sso_linked_at: when an account first signed in through unified
-- authentication (direct match, link or creation). NULL means never. An
-- account that carries it can no longer be claimed by someone else's
-- unified login through the "link an existing account" password step. No
-- backfill: no account had a unified login before this migration.
ALTER TABLE users ADD COLUMN sso_linked_at timestamp with time zone;

-- Local login looks an account up by lower(username); this keeps that lookup
-- to one row (it replaces the dropped idx_users_local_login_name). Production
-- registration has always lower-cased names, so no clash is expected; if one
-- exists, refuse the upgrade with the clashing names instead of picking one.
DO $$
DECLARE
  clashes text;
BEGIN
  SELECT string_agg(name, ', ' ORDER BY name) INTO clashes FROM (
    SELECT lower(username) AS name FROM users WHERE username <> ''
    GROUP BY lower(username) HAVING count(*) > 1
  ) AS duplicated;
  IF clashes IS NOT NULL THEN
    RAISE EXCEPTION '0072_auth_simplify: usernames that differ only by letter case must be resolved before upgrading (rename all but one of each, then migrate again): %', clashes;
  END IF;
END
$$;
CREATE UNIQUE INDEX idx_users_username_lower ON users (lower(username)) WHERE username <> '';
