import test from "node:test";
import assert from "node:assert/strict";
import { getPathMatch } from "next/dist/shared/lib/router/utils/path-match.js";
import { prepareDestination } from "next/dist/shared/lib/router/utils/prepare-destination.js";

const originalBackendTarget = process.env.BACKEND_PROXY_TARGET;
test.after(() => {
  if (originalBackendTarget === undefined) delete process.env.BACKEND_PROXY_TARGET;
  else process.env.BACKEND_PROXY_TARGET = originalBackendTarget;
});

async function loadConfig(target) {
  if (target === undefined) delete process.env.BACKEND_PROXY_TARGET;
  else process.env.BACKEND_PROXY_TARGET = target;
  const url = new URL("../../next.config.mjs", import.meta.url);
  url.searchParams.set("target", target ?? "default");
  return (await import(url.href)).default;
}

function rewrite(rules, pathname, query = {}) {
  for (const rule of rules) {
    const params = getPathMatch(rule.source, { removeUnnamedParams: true })(pathname);
    if (params === false) continue;
    return prepareDestination({
      destination: rule.destination,
      params,
      query,
      appendParamsToQuery: true,
    }).parsedDestination;
  }
  return undefined;
}

for (const target of [undefined, "http://127.0.0.1:18000"]) {
  test(`public MCP and API paths reach the ${target ?? "default"} backend`, async () => {
    const config = await loadConfig(target);
    const rules = await config.rewrites();
    const backend = new URL(target ?? "http://127.0.0.1:8000");

    // Use Next's real path matcher/destination compiler, without binding a port.
    const mcp = rewrite(rules, "/mcp");
    assert.ok(mcp, "the public MCP endpoint must reach the backend");
    assert.equal(mcp.hostname, backend.hostname);
    assert.equal(mcp.port, backend.port);
    assert.equal(mcp.pathname, "/mcp/", "avoid the backend's slash redirect");

    const api = rewrite(rules, "/api/notebooks/nb/sources", { view: "all" });
    assert.equal(api.hostname, backend.hostname);
    assert.equal(api.port, backend.port);
    assert.equal(api.pathname, "/api/notebooks/nb/sources");
    assert.deepEqual(api.query, { view: "all" });
    assert.equal(rewrite(rules, "/notebooks/nb"), undefined);
    assert.equal(rewrite(rules, "/mcp-unrelated"), undefined);
  });
}
