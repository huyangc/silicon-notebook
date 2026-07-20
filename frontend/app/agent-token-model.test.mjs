import test from "node:test";
import assert from "node:assert/strict";

import {
  AGENT_ACCESS_PAGE_SIZE,
  AGENT_SCOPE_OPTIONS,
  agentPageHasMore,
  agentPagePath,
  agentTokenDraft,
  agentTokenRequest,
  canIssueAgentToken,
  mergeAgentPage,
  localDateTimeToUtcIso,
} from "./agent-token-model.ts";
import {
  declarations,
  importsFrom,
  jsxTextValues,
  parseModule,
} from "./test/semantic-source.mjs";

test("new token drafts stay least-privileged and default to one notebook", () => {
  const draft = agentTokenDraft("notebook-1");

  assert.deepEqual(draft.notebook_ids, ["notebook-1"]);
  assert.deepEqual(draft.scopes, ["knowledge:read", "memory:read"]);
  assert.equal(draft.scopes.includes("memory:propose"), false);
});

test("token request always includes its default notebook exactly once", () => {
  const payload = agentTokenRequest("profile-1", {
    default_notebook_id: "notebook-1",
    notebook_ids: ["notebook-2", "notebook-1", "notebook-2"],
    scopes: ["memory:read", "memory:read"],
    expires_at: "2030-01-02T03:04:05",
  });

  assert.deepEqual(payload.notebook_ids, ["notebook-1", "notebook-2"]);
  assert.deepEqual(payload.scopes, ["memory:read"]);
  assert.equal(payload.agent_profile_id, "profile-1");
  assert.equal(payload.expires_at, new Date("2030-01-02T03:04:05").toISOString());
});

test("datetime-local expiry is converted from a non-UTC browser zone to UTC", () => {
  assert.equal(
    localDateTimeToUtcIso("2030-01-02T03:04:05", -480),
    "2030-01-01T19:04:05.000Z",
  );
});

test("issue validation requires a profile, default notebook, scopes, and expiry", () => {
  const valid = {
    default_notebook_id: "notebook-1",
    notebook_ids: ["notebook-1"],
    scopes: ["memory:read"],
    expires_at: "2030-01-02T03:04:05",
  };

  assert.equal(canIssueAgentToken("profile-1", valid), true);
  assert.equal(canIssueAgentToken("", valid), false);
  assert.equal(canIssueAgentToken("profile-1", { ...valid, scopes: [] }), false);
  assert.equal(canIssueAgentToken("profile-1", { ...valid, expires_at: "" }), false);
});

test("scope options expose all and only the approved capabilities", () => {
  assert.deepEqual(AGENT_SCOPE_OPTIONS.map((item) => item.value), [
    "knowledge:read",
    "memory:read",
    "memory:read_candidates",
    "memory:propose",
    "ask:execute",
    "knowhow:code",
  ]);
});

test("global Memory owns one semantic Agent-access surface", async () => {
  const panel = await parseModule("memory-panel.tsx");
  const declarationNames = new Set(
    declarations(panel).map((finding) => finding.name),
  );
  const modelImports = new Set(
    importsFrom(panel, "./agent-token-model").map((item) => item.imported),
  );
  const visibleCopy = jsxTextValues(panel).join(" ");

  assert.equal(declarationNames.has("AgentAccessManager"), true);
  assert.equal(modelImports.has("agentTokenRequest"), true);
  assert.equal(modelImports.has("agentPagePath"), true);
  assert.match(visibleCopy, /笔记本白名单/);
  assert.match(visibleCopy, /过期时间/);
  assert.match(visibleCopy, /明文 token 仅显示这一次/);
});

test("profile and token page paths retain independent offsets", () => {
  assert.equal(
    agentPagePath("/agent-profiles", 25),
    `/agent-profiles?offset=25&limit=${AGENT_ACCESS_PAGE_SIZE}`,
  );
  assert.equal(
    agentPagePath("/agent-tokens", 75),
    `/agent-tokens?offset=75&limit=${AGENT_ACCESS_PAGE_SIZE}`,
  );
});

test("incremental pages deduplicate records without disturbing prior order", () => {
  assert.deepEqual(
    mergeAgentPage(
      [{ id: "profile-1", name: "One" }, { id: "profile-2", name: "Old" }],
      [{ id: "profile-2", name: "Updated" }, { id: "profile-3", name: "Three" }],
    ),
    [
      { id: "profile-1", name: "One" },
      { id: "profile-2", name: "Updated" },
      { id: "profile-3", name: "Three" },
    ],
  );
});

test("a full bounded page exposes a next page while a short page terminates", () => {
  assert.equal(agentPageHasMore(Array(AGENT_ACCESS_PAGE_SIZE).fill({})), true);
  assert.equal(agentPageHasMore(Array(AGENT_ACCESS_PAGE_SIZE - 1).fill({})), false);
});
