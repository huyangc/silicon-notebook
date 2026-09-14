import test from "node:test";
import assert from "node:assert/strict";

import {
  AGENT_ACCESS_PAGE_SIZE,
  AGENT_SCOPE_OPTIONS,
  agentPageHasMore,
  agentPagePath,
  agentTokenAccessChanged,
  agentTokenAccessPath,
  agentTokenAccessRequest,
  agentTokenDraft,
  agentTokenEditDraft,
  agentTokenRequest,
  canIssueAgentToken,
  canSaveAgentTokenAccess,
  mergeAgentPage,
  localDateTimeToUtcIso,
  utcIsoToLocalDateTime,
} from "../../app/agent-token-model.ts";
import {
  declarations,
  importsFrom,
  jsxElements,
  jsxTextValues,
  parseModule,
} from "../../test-support/semantic-source.mjs";

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
    "sources:write",
    "sources:delete",
    "maintenance:execute",
    "agent_profile:read",
    "agent_observation:write",
  ]);
});

test("the Agent access page owns one semantic Agent-access surface", async () => {
  const manager = await parseModule("agent-access-manager.tsx");
  const declarationNames = new Set(
    declarations(manager).map((finding) => finding.name),
  );
  const modelImports = new Set(
    importsFrom(manager, "./agent-token-model").map((item) => item.imported),
  );
  const copyImports = new Set(
    importsFrom(manager, "./copy-text").map((item) => item.imported),
  );
  const visibleCopy = jsxTextValues(manager).join(" ");

  assert.equal(declarationNames.has("AgentAccessManager"), true);
  assert.equal(modelImports.has("agentTokenRequest"), true);
  assert.equal(modelImports.has("agentTokenAccessRequest"), true);
  assert.equal(modelImports.has("agentPagePath"), true);
  assert.equal(copyImports.has("copyTextSafely"), true);
  assert.match(visibleCopy, /笔记本白名单/);
  assert.match(visibleCopy, /过期时间/);
  assert.match(visibleCopy, /明文 token 仅显示这一次/);
  assert.match(visibleCopy, /自动复制失败/);
  assert.match(visibleCopy, /Agent MCP 接入说明链接/);
  assert.match(visibleCopy, /链接本身不包含 token/);
  assert.match(visibleCopy, /修改权限/);

  const page = await parseModule("agents/page.tsx");
  assert.equal(
    importsFrom(page, "../agent-access-manager").some((item) => item.imported === "AgentAccessManager"),
    true,
  );
});

test("global Memory keeps only a link to the Agent access page", async () => {
  const panel = await parseModule("memory-panel.tsx");
  const declarationNames = new Set(declarations(panel).map((finding) => finding.name));

  assert.equal(declarationNames.has("AgentAccessManager"), false);
  assert.deepEqual(importsFrom(panel, "./agent-token-model"), []);
  assert.equal(
    jsxElements(panel, "a").some((element) => element.attributes.href === "/agents"),
    true,
  );
});

test("edit drafts round-trip the stored access and drop retired scopes", () => {
  const token = {
    default_notebook_id: "notebook-1",
    notebook_ids: ["notebook-2", "notebook-1"],
    scopes: ["memory:read", "retired:scope"],
    expires_at: "2030-01-01T19:04:00Z",
  };
  const draft = agentTokenEditDraft(token);

  assert.deepEqual(draft.notebook_ids, ["notebook-1", "notebook-2"]);
  assert.deepEqual(draft.scopes, ["memory:read"]);
  assert.equal(draft.expires_at, utcIsoToLocalDateTime(token.expires_at));
  assert.equal(localDateTimeToUtcIso(draft.expires_at), "2030-01-01T19:04:00.000Z");
});

test("UTC expiry is rendered as the browser's local wall clock for datetime-local", () => {
  assert.equal(utcIsoToLocalDateTime("2030-01-01T19:04:05Z", -480), "2030-01-02T03:04");
  assert.equal(utcIsoToLocalDateTime(null), "");
  assert.equal(utcIsoToLocalDateTime("not a date"), "");
});

test("access updates are full replacements that never carry a profile id", () => {
  const payload = agentTokenAccessRequest({
    default_notebook_id: "notebook-1",
    notebook_ids: ["notebook-2"],
    scopes: ["knowledge:read", "knowledge:read"],
    expires_at: "",
  });

  assert.deepEqual(Object.keys(payload).sort(), ["default_notebook_id", "expires_at", "notebook_ids", "scopes"]);
  assert.deepEqual(payload.notebook_ids, ["notebook-1", "notebook-2"]);
  assert.deepEqual(payload.scopes, ["knowledge:read"]);
  assert.equal(payload.expires_at, null);
  assert.equal(agentTokenAccessPath("token/1"), "/agent-tokens/token%2F1/access");
});

test("saving an edit requires a complete draft that differs from the stored access", () => {
  const token = {
    default_notebook_id: "notebook-1",
    notebook_ids: ["notebook-1"],
    scopes: ["memory:read"],
    expires_at: "2030-01-01T19:04:00Z",
  };
  const unchanged = agentTokenEditDraft(token);

  assert.equal(agentTokenAccessChanged(token, unchanged), false);
  assert.equal(canSaveAgentTokenAccess(token, unchanged), false);
  assert.equal(canSaveAgentTokenAccess(token, { ...unchanged, scopes: ["memory:read", "knowledge:read"] }), true);
  assert.equal(canSaveAgentTokenAccess(token, { ...unchanged, notebook_ids: ["notebook-1", "notebook-2"] }), true);
  assert.equal(canSaveAgentTokenAccess(token, { ...unchanged, scopes: [] }), false);
  assert.equal(canSaveAgentTokenAccess(token, { ...unchanged, expires_at: "" }), false);
  assert.equal(
    canSaveAgentTokenAccess(token, { ...unchanged, default_notebook_id: "notebook-2", notebook_ids: ["notebook-1"] }),
    false,
  );
  // 已下线的 scope 在草稿里被剔除,于是「原样保存」也算一次有效修改——正是它把废 scope 清掉。
  assert.equal(
    canSaveAgentTokenAccess({ ...token, scopes: ["memory:read", "retired:scope"] }, unchanged),
    true,
  );
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
