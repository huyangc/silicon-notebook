import test from "node:test";
import assert from "node:assert/strict";

import {
  fetchAuthAccounts,
  fetchAuthAudit,
  fetchAuthMigration,
  issueAuthGrant,
  prepareAuthProviderMaintenance,
  updateAuthAccountStatus,
  updateAuthPolicy,
} from "../../app/admin/auth/api.ts";

const originalFetch = globalThis.fetch;
test.after(() => { globalThis.fetch = originalFetch; });

test("authentication migration admin API uses explicit policy revision and maintenance generation", async () => {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return new Response(JSON.stringify({ mode: "dual", revision: 4, config_generation: 8 }), { headers: { "Content-Type": "application/json" } });
  };
  await updateAuthPolicy("sso_only", 4, true);
  await prepareAuthProviderMaintenance(4, 9);
  assert.equal(calls[0].url, "http://127.0.0.1:8000/api/admin/auth/policy");
  assert.equal(calls[0].init.body, JSON.stringify({ mode: "sso_only", expected_revision: 4, allow_rollback: true }));
  assert.equal(calls[1].init.body, JSON.stringify({ expected_revision: 4, configuration_generation: 9 }));
});

test("account controls are paged and recovery grants carry only the explicit scoped target", async () => {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return new Response(JSON.stringify({ grant_token: "one-time", purpose: "recover", expires_in: 300 }), { headers: { "Content-Type": "application/json" } });
  };
  await fetchAuthAccounts(50, 50);
  await fetchAuthMigration();
  await updateAuthAccountStatus("u1", "disabled");
  await issueAuthGrant("recover", "operator-note", "u1");
  assert.equal(calls[0].url, "http://127.0.0.1:8000/api/admin/auth/accounts?offset=50&limit=50");
  assert.equal(calls[1].url, "http://127.0.0.1:8000/api/admin/auth/migration");
  assert.equal(calls[2].init.body, JSON.stringify({ status: "disabled" }));
  assert.equal(calls[3].init.body, JSON.stringify({ purpose: "recover", subject: "operator-note", target_user_id: "u1" }));
});

test("authentication audit is a paged read-only request", async () => {
  let captured;
  globalThis.fetch = async (url, init) => {
    captured = { url, init };
    return new Response(JSON.stringify({ items: [], total: 0, offset: 100, limit: 100 }), { headers: { "Content-Type": "application/json" } });
  };
  await fetchAuthAudit(100, 100);
  assert.equal(captured.url, "http://127.0.0.1:8000/api/admin/auth/audit?offset=100&limit=100");
  assert.equal(captured.init.method, undefined);
});

test("replacement grants remain scoped to one original account", async () => {
  let captured;
  globalThis.fetch = async (_url, init) => {
    captured = init;
    return new Response(JSON.stringify({ grant_token: "one-time", purpose: "replace", expires_in: 300 }), { headers: { "Content-Type": "application/json" } });
  };
  await issueAuthGrant("replace", "operator-note", "original-user-id");
  assert.equal(captured.body, JSON.stringify({ purpose: "replace", subject: "operator-note", target_user_id: "original-user-id" }));
});
