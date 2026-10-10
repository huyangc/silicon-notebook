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
