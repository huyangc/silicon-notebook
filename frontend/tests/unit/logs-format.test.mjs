import test from "node:test";
import assert from "node:assert/strict";

import { statusClass, formatLatency, formatTokens, prettyJson, shortId } from "../../app/dev/logs/format.ts";

test("statusClass maps known statuses", () => {
  assert.equal(statusClass("ok"), "ok");
  assert.equal(statusClass("retry"), "retry");
  assert.equal(statusClass("error"), "error");
  assert.equal(statusClass("weird"), "muted");
});

test("formatLatency", () => {
  assert.equal(formatLatency(null), "—");
  assert.equal(formatLatency(250), "250ms");
  assert.equal(formatLatency(1500), "1.5s");
});

test("formatTokens", () => {
  assert.equal(formatTokens(null), "—");
  assert.equal(formatTokens(42), "42");
  assert.equal(formatTokens(3896), "3.9k");
});

test("prettyJson pretty-prints valid and passes through invalid", () => {
  const a = prettyJson('{"k":1}');
  assert.equal(a.ok, true);
  assert.match(a.pretty, /\n {2}"k": 1/);
  const b = prettyJson("not json");
  assert.equal(b.ok, false);
  assert.equal(b.pretty, "not json");
});

test("shortId strips llm- prefix", () => {
  assert.equal(shortId("llm-abc123"), "abc123");
  assert.equal(shortId(null), "—");
});
