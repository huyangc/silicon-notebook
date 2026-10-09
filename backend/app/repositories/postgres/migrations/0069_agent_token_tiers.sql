-- Agent token permissions become five tiers (read / ask / contribute /
-- manage / delete) and a token's plaintext is kept so its owner can copy it
-- again. Mirrors SQLite v89 (_migration_89, a frozen copy of the same rule);
-- tests/agent_token_tiers_migration_cases.py seeds one world that both
-- backends migrate to the same rows.
--
-- 1. token_plain: nullable text, no backfill. Tokens issued before this
--    migration only ever stored a hash, so they stay NULL (not copyable).
--    Revoking a token clears it.
--
-- 2. scopes_json is rewritten on EVERY row, revoked ones included, so the
--    table holds one vocabulary. A tier is granted when the row holds that
--    tier's main capability (or already holds the tier itself):
--      read       <- knowledge:read | memory:read
--      ask        <- ask:execute
--      contribute <- memory:propose
--      manage     <- sources:write | maintenance:execute
--      delete     <- sources:delete
--    The result lists the granted tiers in that order. A row holding only
--    secondary capabilities (e.g. only agent_profile:read) ends with [] and
--    is refused at run time until its owner picks a tier. Values that are not
--    a JSON array, and array items that are not strings, grant nothing.
--
-- A content-free summary goes to the PostgreSQL server log (RAISE LOG):
-- tokens=<rows> emptied=<live rows left with no tier>. Those tokens are refused
-- at run time until their owner re-grants a tier on /agents
-- (docs/operations.md, "Agent token permission tiers").
--
-- Idempotent: tier names map to themselves, so a second run computes the
-- same array and the WHERE clause skips every row. No index, FK or unique
-- surface changes; agent_access_tokens is read by primary key only.
ALTER TABLE agent_access_tokens ADD COLUMN IF NOT EXISTS token_plain text COLLATE "C";

UPDATE agent_access_tokens AS t
SET scopes_json = x.tiers
FROM (
  SELECT tok.id,
         COALESCE((
           SELECT jsonb_agg(to_jsonb(r.tier) ORDER BY r.ord)
           FROM (VALUES
             (1, 'read', ARRAY['read', 'knowledge:read', 'memory:read']),
             (2, 'ask', ARRAY['ask', 'ask:execute']),
             (3, 'contribute', ARRAY['contribute', 'memory:propose']),
             (4, 'manage', ARRAY['manage', 'sources:write', 'maintenance:execute']),
             (5, 'delete', ARRAY['delete', 'sources:delete'])
           ) AS r(ord, tier, granting)
           WHERE jsonb_typeof(tok.scopes_json) = 'array'
             AND EXISTS (
               SELECT 1
               FROM jsonb_array_elements(
                 CASE WHEN jsonb_typeof(tok.scopes_json) = 'array'
                      THEN tok.scopes_json ELSE '[]'::jsonb END
               ) AS e(item)
               WHERE jsonb_typeof(e.item) = 'string'
                 AND (e.item #>> '{}') = ANY (r.granting)
             )
         ), '[]'::jsonb) AS tiers
  FROM agent_access_tokens AS tok
) AS x
WHERE x.id = t.id
  AND t.scopes_json IS DISTINCT FROM x.tiers;

DO $tt$
DECLARE
  total bigint;
  emptied bigint;
BEGIN
  SELECT count(*),
         count(*) FILTER (WHERE scopes_json = '[]'::jsonb AND revoked_at IS NULL)
    INTO total, emptied
    FROM agent_access_tokens;
  RAISE LOG 'agent-token-tiers migration: tokens=% emptied=%', total, emptied;
END
$tt$;
