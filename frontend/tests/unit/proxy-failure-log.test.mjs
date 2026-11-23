import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { Readable, Writable } from "node:stream";
import url from "node:url";
import { inspect } from "node:util";
import proxyRequestModule from "next/dist/server/lib/router-utils/proxy-request.js";

import { register } from "../../instrumentation.ts";
import {
  installProxyFailureLogRedaction,
  loggableProxyError,
  loggableProxyTarget,
  redactProxyFailureLog,
} from "../../proxy-failure-log.ts";

const SEARCH_TEXT = "private search text";
const SHARE_TOKEN = "share-token-value";
const UPSTREAM_PATH = `/api/public/reports/${SHARE_TOKEN}`;

// Swap console.error for a recorder; `run` sees what would have been written.
async function recordingConsoleError(run) {
  const original = console.error;
  const written = [];
  console.error = (...args) => {
    written.push(args);
  };
  try {
    await run(written);
  } finally {
    console.error = original;
  }
  return written;
}

// Next's own rewrite proxy and its logging run for real. Only the outgoing
// HTTP request is replaced, by one that fails the way a refused connection
// does, so no socket is opened and the result does not depend on the host.
async function failProxiedRequest(t) {
  const refused = Object.assign(new Error("connect ECONNREFUSED 127.0.0.1:8000"), {
    errno: -61,
    code: "ECONNREFUSED",
    syscall: "connect",
    address: "127.0.0.1",
    port: 8000,
  });
  const transport = t.mock.method(http, "request", () => {
    const outgoing = Object.assign(new Writable({ write: (_chunk, _encoding, done) => done() }), {
      setTimeout() {},
      abort() {},
      getHeader() {},
    });
    queueMicrotask(() => outgoing.emit("error", refused));
    return outgoing;
  });
  const request = Object.assign(Readable.from([]), {
    method: "GET",
    url: UPSTREAM_PATH,
    headers: { host: "notebook.test" },
    socket: { destroyed: false },
  });
  const response = new http.ServerResponse(request);
  const upstream = url.parse(`http://proxy-user:proxy-secret@127.0.0.1:8000${UPSTREAM_PATH}`);
  upstream.query = { q: SEARCH_TEXT };
  try {
    await assert.rejects(proxyRequestModule.proxyRequest(request, response, upstream), refused);
    assert.equal(transport.mock.callCount(), 1, "the proxy went through the replaced transport");
  } finally {
    transport.mock.restore();
  }
  return response;
}

const proxyFailures = (written) =>
  written.filter(([message]) => typeof message === "string" && message.startsWith("Failed to proxy"));

test("an unreachable upstream is logged without the request's query string, path tokens or credentials", async (t) => {
  // Control: this is the line Next writes on its own. If a Next upgrade changes
  // it, this fails first and the redaction below has to be revisited.
  const unredacted = await recordingConsoleError(() => failProxiedRequest(t));
  assert.equal(proxyFailures(unredacted).length, 1);
  assert.ok(proxyFailures(unredacted)[0][0].includes(SEARCH_TEXT));
  assert.ok(proxyFailures(unredacted)[0][0].includes(SHARE_TOKEN));

  const written = await recordingConsoleError(async () => {
    installProxyFailureLogRedaction(console);
    const response = await failProxiedRequest(t);
    assert.equal(response.statusCode, 500, "the failure itself is reported as before");
  });
  const failures = proxyFailures(written);
  assert.equal(failures.length, 1);
  const [message, detail] = failures[0];
  assert.equal(message, "Failed to proxy http://127.0.0.1:8000");
  // What failed stays diagnosable: the connection error's code and peer.
  assert.deepEqual(detail, {
    name: "Error",
    code: "ECONNREFUSED",
    syscall: "connect",
    address: "127.0.0.1",
    port: 8000,
  });
  for (const args of written) {
    for (const value of args) {
      const text = inspect(value, { depth: 6, showHidden: true });
      for (const withheld of [SEARCH_TEXT, "q=", "search", SHARE_TOKEN, "/api", "proxy-secret", "proxy-user", "connect ECONNREFUSED"]) {
        assert.ok(!text.includes(withheld), `${withheld} was logged: ${text}`);
      }
    }
  }
});

test("the server's startup hook applies the redaction to the process console", async () => {
  const written = await recordingConsoleError(() => {
    register();
    console.error("Failed to proxy http://127.0.0.1:8000/api/x?q=1", "detail");
    console.error("unrelated", { q: 1 });
  });
  assert.deepEqual(written, [
    ["Failed to proxy http://127.0.0.1:8000", "detail"],
    ["unrelated", { q: 1 }],
  ]);
});

test("only the upstream scheme, host and port are kept", () => {
  for (const [target, expected] of [
    ["http://127.0.0.1:8000/api/notebooks/nb/search?q=a b&k=5", "http://127.0.0.1:8000"],
    ["http://127.0.0.1:8000/api/public/reports/share-token", "http://127.0.0.1:8000"],
    ["http://127.0.0.1:8000/api/group-invites/invite-token/join", "http://127.0.0.1:8000"],
    // A path prefix in BACKEND_PROXY_TARGET is deployment configuration and may be private.
    ["https://gateway.example/tenant-key/api/notebooks", "https://gateway.example"],
    ["http://127.0.0.1:8000/mcp/", "http://127.0.0.1:8000"],
    ["http://127.0.0.1:8000/", "http://127.0.0.1:8000"],
    ["http://127.0.0.1:8000", "http://127.0.0.1:8000"],
    ["https://backend.internal/api/x#fragment", "https://backend.internal"],
    ["http://127.0.0.1:8000?next=http://other:9/y", "http://127.0.0.1:8000"],
    ["http://127.0.0.1:8000#f@other:9", "http://127.0.0.1:8000"],
    ["http://user:pass@127.0.0.1:8000/api/x?q=1", "http://127.0.0.1:8000"],
    ["http://user:p@ss@backend:8000/api", "http://backend:8000"],
    ["http://token@[::1]:8000/api/x", "http://[::1]:8000"],
    ["http://127.0.0.1:8000/api/a@b/c", "http://127.0.0.1:8000"],
    ["http://127.0.0.1:4310/@notebook/canvas/editor?new=1", "http://127.0.0.1:4310"],
  ]) {
    assert.equal(loggableProxyTarget(target), expected, target);
  }
  // A shape this module does not recognise is withheld, never echoed.
  for (const target of ["", "/api/x?q=1", "ftp://host/x", "http://", "http://user@/x", "not a url"]) {
    const logged = loggableProxyTarget(target);
    assert.equal(logged, "[upstream address withheld]", target);
  }
});

test("an upstream's malformed response is not copied into the log with the error", () => {
  // The shape Node gives an HTTP parse error: the bytes it received ride on it.
  const parseError = Object.assign(new Error("Parse Error: Header overflow"), {
    code: "HPE_HEADER_OVERFLOW",
    reason: "Header overflow",
    bytesParsed: 12,
    rawPacket: Buffer.from("set-cookie: session=upstream-session-value"),
  });
  const unrelated = Object.assign(new Error("boom"), { rawPacket: Buffer.from("kept") });
  const calls = [];
  const sink = { error: (...args) => void calls.push(args) };
  installProxyFailureLogRedaction(sink);

  sink.error("Failed to proxy http://127.0.0.1:8000/api/x", parseError);
  // Next then logs the same error object again, without the prefix.
  sink.error(parseError);
  sink.error("Error handling upgrade request", parseError);
  sink.error(unrelated);

  const summary = { name: "Error", code: "HPE_HEADER_OVERFLOW" };
  assert.deepEqual(calls, [
    ["Failed to proxy http://127.0.0.1:8000", summary],
    [summary],
    ["Error handling upgrade request", summary],
    [unrelated],
  ]);
  assert.equal(calls[3][0], unrelated, "an error that never failed a proxy is not rewritten");
});

test("only fields of a fixed shape are taken from a proxy error", () => {
  assert.deepEqual(
    loggableProxyError(
      Object.assign(new Error("connect ECONNREFUSED"), {
        errno: -61,
        code: "ECONNREFUSED",
        syscall: "connect",
        address: "127.0.0.1",
        port: 8000,
        hostname: "backend.internal",
      }),
    ),
    { name: "Error", code: "ECONNREFUSED", syscall: "connect", address: "127.0.0.1", port: 8000 },
  );
  assert.deepEqual(loggableProxyError({ address: "::1", port: 0 }), { address: "::1", port: 0 });
  // Free text in a recognised field is dropped, not passed through.
  assert.deepEqual(
    loggableProxyError({
      name: "Error: private text",
      code: "private text",
      syscall: "connect /api/x?q=1",
      address: "127.0.0.1/api/x?q=1",
      port: "8000",
      message: "private text",
    }),
    {},
  );
  assert.deepEqual(loggableProxyError({ code: 404, port: 65536, address: 1 }), {});
  assert.deepEqual(loggableProxyError({ port: 80.5 }), {});
  const throwing = new Proxy({}, { get: () => { throw new Error("no"); } });
  assert.deepEqual(loggableProxyError(throwing), {});
});

test("every other console.error call is passed through untouched", () => {
  const error = new Error("boom");
  for (const args of [
    [],
    ["Failed to proxy"],
    ["failed to proxy http://h/x?q=1"],
    ["Request to http://h/x?q=1: Failed to proxy http://h/x?q=1"],
    [error],
    [{ message: "Failed to proxy http://h/x?q=1" }],
    [{ toString: () => "Failed to proxy http://h/x?q=1" }],
    ["%s", "Failed to proxy http://h/x?q=1"],
  ]) {
    assert.equal(redactProxyFailureLog(args), args);
  }
  assert.deepEqual(redactProxyFailureLog(["Failed to proxy http://h/x?q=1", error, 2]), [
    "Failed to proxy http://h",
    { name: "Error" },
    2,
  ]);
});

test("installing twice wraps once and keeps the receiver and return value", () => {
  const calls = [];
  const sink = {
    error(...args) {
      calls.push([this, args]);
      return "written";
    },
  };
  installProxyFailureLogRedaction(sink);
  const once = sink.error;
  installProxyFailureLogRedaction(sink);
  assert.equal(sink.error, once);

  assert.equal(sink.error("Failed to proxy http://h:1/x?q=1"), "written");
  assert.deepEqual(calls, [[sink, ["Failed to proxy http://h:1"]]]);
});
