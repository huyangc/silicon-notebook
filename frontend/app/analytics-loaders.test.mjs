import assert from "node:assert/strict";
import test from "node:test";

import { startAnalyticsLoads } from "./analytics-loaders.ts";


test("starts all reads immediately and isolates optional overview failure", async () => {
  const started = [];
  const loads = startAnalyticsLoads({
    analytics: () => {
      started.push("analytics");
      return Promise.resolve({ answers_total: 2 });
    },
    indexStatus: () => {
      started.push("index");
      return Promise.resolve({ kg: { building: false } });
    },
    contentOverview: () => {
      started.push("overview");
      return Promise.reject(new Error("overview unavailable"));
    },
  });

  assert.deepEqual(started, ["analytics", "index", "overview"]);
  assert.deepEqual(await loads.analytics, { answers_total: 2 });
  assert.deepEqual(await loads.indexStatus, {
    ok: true,
    value: { kg: { building: false } },
  });
  assert.deepEqual(await loads.contentOverview, { ok: false });
});
