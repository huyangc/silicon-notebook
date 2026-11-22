import test from "node:test";
import assert from "node:assert/strict";
import { getPathMatch } from "next/dist/shared/lib/router/utils/path-match.js";
import { prepareDestination } from "next/dist/shared/lib/router/utils/prepare-destination.js";
import redirectStatus from "next/dist/lib/redirect-status.js";

const PROXY_VARIABLES = ["BACKEND_PROXY_TARGET", "CANVAS_PROXY_TARGET"];
const originalValues = new Map(PROXY_VARIABLES.map((name) => [name, process.env[name]]));
test.after(() => {
  for (const [name, value] of originalValues) setVariable(name, value);
});

function setVariable(name, value) {
  if (value === undefined) delete process.env[name];
  else process.env[name] = value;
}

// The config reads both variables once, at module load, so every combination
// needs its own module instance: the cache key carries both values.
async function loadConfig(backendTarget, canvasTarget) {
  setVariable("BACKEND_PROXY_TARGET", backendTarget);
  setVariable("CANVAS_PROXY_TARGET", canvasTarget);
  const url = new URL("../../next.config.mjs", import.meta.url);
  url.searchParams.set("backend", JSON.stringify(backendTarget ?? null));
  url.searchParams.set("canvas", JSON.stringify(canvasTarget ?? null));
  return (await import(url.href)).default;
}

// Next's real path matcher and destination compiler, without binding a port.
// The matcher is built the way Next's server builds one for a rewrite
// (buildCustomRoute in router-utils/filesystem.js), including the regex
// modifier that lets a source also match with one trailing slash.
function rewrite(rules, pathname, query = {}, sensitive = false) {
  for (const rule of rules) {
    const params = getPathMatch(rule.source, {
      strict: true,
      removeUnnamedParams: true,
      regexModifier: (regex) => redirectStatus.modifyRouteRegex(regex),
      sensitive,
    })(pathname);
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

function assertBackendRoutes(rules, backendTarget) {
  const backend = new URL(backendTarget ?? "http://127.0.0.1:8000");

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
}

const BACKEND_TARGETS = [undefined, "http://127.0.0.1:18000"];

for (const backendTarget of BACKEND_TARGETS) {
  test(`public MCP and API paths reach the ${backendTarget ?? "default"} backend`, async () => {
    const config = await loadConfig(backendTarget, undefined);
    assertBackendRoutes(await config.rewrites(), backendTarget);
  });

  // A deployment that does not use the canvas companion keeps exactly the two
  // rules it always had: same sources, same destinations, same order.
  for (const [label, canvasTarget] of [["unset", undefined], ["empty", ""]]) {
    test(`${label} CANVAS_PROXY_TARGET adds no rule for the ${backendTarget ?? "default"} backend`, async () => {
      const config = await loadConfig(backendTarget, canvasTarget);
      const backend = backendTarget ?? "http://127.0.0.1:8000";
      assert.deepEqual(await config.rewrites(), [
        { source: "/api/:path*", destination: `${backend}/api/:path*` },
        { source: "/mcp", destination: `${backend}/mcp/` },
      ]);
    });
  }
}

const CANVAS_MOUNT = "/@notebook/canvas";

// The upstream canonicalises the path it receives, so the proxied path must
// still sit under the mount after canonicalisation. A path-segment check:
// `/@notebook/canvas-other` is outside.
function assertStaysUnderMount(destination) {
  const canonical = new URL(destination.pathname, "http://upstream.invalid").pathname;
  assert.ok(
    canonical === CANVAS_MOUNT || canonical.startsWith(`${CANVAS_MOUNT}/`),
    `${destination.pathname} leaves ${CANVAS_MOUNT} as ${canonical}`,
  );
}

const CANVAS_TARGETS = [
  ["http://127.0.0.1:4310", "127.0.0.1", "4310"],
  ["http://localhost:4310", "localhost", "4310"],
  ["http://127.0.0.1:4310/", "127.0.0.1", "4310"],
  ["https://localhost", "localhost", ""],
];

for (const [canvasTarget, hostname, port] of CANVAS_TARGETS) {
  for (const backendTarget of BACKEND_TARGETS) {
    test(`/canvas reaches ${canvasTarget} beside the ${backendTarget ?? "default"} backend`, async () => {
      const config = await loadConfig(backendTarget, canvasTarget);
      const rules = await config.rewrites();

      assert.equal(rules.length, 4);
      assert.equal(rules[2].source, "/canvas", "the explicit root rule comes first");
      assertBackendRoutes(rules, backendTarget);

      const expectCanvas = (pathname, query) => {
        const destination = rewrite(rules, pathname, query);
        assert.ok(destination, `${pathname} must reach the canvas companion`);
        assert.equal(destination.protocol, new URL(canvasTarget).protocol);
        assert.equal(destination.hostname, hostname);
        assert.equal(destination.port ?? "", port);
        assertStaysUnderMount(destination);
        return destination;
      };

      assert.equal(
        expectCanvas("/canvas").pathname,
        `${CANVAS_MOUNT}/`,
        "avoid the upstream's slash redirect",
      );
      assert.equal(expectCanvas("/canvas/editor").pathname, `${CANVAS_MOUNT}/editor`);
      const api = expectCanvas("/canvas/api/projects", { x: "1" });
      assert.equal(api.pathname, `${CANVAS_MOUNT}/api/projects`);
      assert.deepEqual(api.query, { x: "1" });
      assert.deepEqual(expectCanvas("/canvas", { new: "1" }).query, { new: "1" });

      assert.equal(rewrite(rules, "/canvasx"), undefined);
      assert.equal(rewrite(rules, "/canvas-other/editor"), undefined);
    });
  }
}

test("canvas paths are forwarded unchanged and never leave the upstream mount", async () => {
  const config = await loadConfig(undefined, "http://127.0.0.1:4310");
  const rules = await config.rewrites();

  for (const tail of [
    "/editor",
    "/a.b/c.svg",
    "/api/projects/x/versions/x%3A3/preview.svg",
    "/...",
    "/..../x",
    "/.well-known/x",
    "/..a/b..",
    "/assets/index-D4f_x.js",
    "/a%21b/%7e/%E4%B8%AD",
    "/a%252e%252e/b",
  ]) {
    const destination = rewrite(rules, `/canvas${tail}`);
    assert.ok(destination, `/canvas${tail} must be forwarded`);
    assert.equal(destination.pathname, `${CANVAS_MOUNT}${tail}`);
    assertStaysUnderMount(destination);
  }

  for (const pathname of [
    "/canvas/../api/projects",
    "/canvas/x/../y",
    "/canvas/x/..",
    "/canvas/..",
    "/canvas/.",
    "/canvas/./x",
    "/canvas/x/.",
    "/canvas/%2e%2e/api",
    "/canvas/%2E%2e/api",
    "/canvas/%2E%2E/api",
    "/canvas/.%2e/api",
    "/canvas/%2e/api",
    "/canvas/..%2fapi",
    "/canvas/..%2Fapi",
    "/canvas/a%2Fb",
    "/canvas/a%5cb",
    "/canvas/a%5Cb",
    "/canvas/..\\api",
    "/canvas/a\\b",
    "/canvas\\..\\api",
    "/canvas//editor",
    "/canvas/../",
    "/canvas/./",
    "/canvas/x/../",
    "/canvas/%2e%2e/",
    // Control and space characters, raw or escaped: a URL parser drops or
    // trims some of them, which would turn `.<TAB>.` into `..` upstream.
    "/canvas/.%09./api",
    "/canvas/.%0a./api",
    "/canvas/.%0D./api",
    "/canvas/%09../api",
    "/canvas/..%20",
    "/canvas/..%00",
    "/canvas/a%20b",
    "/canvas/a%7fb",
    "/canvas/a%7Fb",
    "/canvas/%1F",
    "/canvas/%1f/x",
    "/canvas/.\t./api",
    "/canvas/.\n./api",
    "/canvas/.. ",
    "/canvas/a b",
    `/canvas/a${String.fromCharCode(0)}b`,
    `/canvas/a${String.fromCharCode(0x7f)}b`,
  ]) {
    // Next matches case-insensitively unless `caseSensitiveRoutes` is on; the
    // escapes must be refused in either letter case under both settings.
    for (const sensitive of [false, true]) {
      assert.equal(
        rewrite(rules, pathname, {}, sensitive),
        undefined,
        `${pathname} must not be proxied`,
      );
    }
  }
});

test("an invalid CANVAS_PROXY_TARGET fails when the config loads", async () => {
  for (const value of [
    "http://10.0.0.5:4310",
    "http://example.com",
    "http://127.0.0.1.example.com:4310",
    "http://127a0b0c1:4310",
    "http://127.0.0.1.:4310",
    "http://localhost.:4310",
    "http://LOCALHOST:4310",
    "http://localhost.example.com",
    "http://[::1]:4310",
    "http://127.0.0.1:4310/x",
    "http://127.0.0.1:4310//",
    "http://user:secret@127.0.0.1:4310",
    "http://user@localhost:4310",
    "http://127.0.0.1:4310?x=1",
    "http://127.0.0.1:4310/?x=1",
    "http://127.0.0.1:4310#frag",
    "http://127.0.0.1:0",
    "http://127.0.0.1:65536",
    "http://127.0.0.1:",
    "ftp://127.0.0.1:4310",
    "ws://localhost:4310",
    "127.0.0.1:4310",
    "//127.0.0.1:4310",
    " http://127.0.0.1:4310",
    "http://127.0.0.1:4310\n",
    " ",
  ]) {
    await assert.rejects(
      loadConfig(undefined, value),
      (error) => {
        assert.match(error.message, /^CANVAS_PROXY_TARGET must be /);
        // The value may carry credentials; the error must not repeat it.
        assert.ok(!error.message.includes("secret"));
        return true;
      },
      `${JSON.stringify(value)} must be rejected`,
    );
  }
});
