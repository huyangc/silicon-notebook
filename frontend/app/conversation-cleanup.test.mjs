import test from "node:test";
import assert from "node:assert/strict";

import { conversationsOlderThan, CLEANUP_PRESETS } from "./conversation-cleanup.ts";

// Fixed "now" so the test is deterministic regardless of wall clock / timezone.
// updated_at strings are naive-local ISO, parsed as local — same basis as NOW.
const NOW = Date.parse("2026-06-24T12:00:00");
const conv = (id, updated_at) => ({ id, updated_at });

test("picks only conversations whose last activity is older than N days", () => {
  const sessions = [
    conv("old", "2026-06-20T12:00:00"),    // 4 days ago  -> older than 3
    conv("fresh", "2026-06-23T12:00:00"),  // 1 day ago   -> within 3
  ];
  const ids = conversationsOlderThan(sessions, 3, NOW).map((s) => s.id);
  assert.deepEqual(ids, ["old"]);
});

test("exact boundary is kept (strict less-than)", () => {
  const exactly3 = conv("edge", "2026-06-21T12:00:00"); // exactly NOW - 3*24h
  assert.equal(conversationsOlderThan([exactly3], 3, NOW).length, 0);
});

test("presets are 3 / 7 / 30 days", () => {
  assert.deepEqual([...CLEANUP_PRESETS], [3, 7, 30]);
});
