import test from "node:test";
import assert from "node:assert/strict";

import { takeNdjsonLines } from "./ask-stream.ts";

test("takes complete NDJSON lines and keeps an incomplete remainder", () => {
  const parsed = takeNdjsonLines('{"event":"progress"}\n{"event":"final"');

  assert.deepEqual(parsed.lines, ['{"event":"progress"}']);
  assert.equal(parsed.remainder, '{"event":"final"');
});

test("ignores blank NDJSON lines", () => {
  const parsed = takeNdjsonLines('\n{"event":"progress"}\n\n');

  assert.deepEqual(parsed.lines, ['{"event":"progress"}']);
  assert.equal(parsed.remainder, "");
});
