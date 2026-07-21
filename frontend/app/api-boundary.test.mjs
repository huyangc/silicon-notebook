import test from "node:test";
import assert from "node:assert/strict";

import { appSourceModules, callsIn } from "./test/semantic-source.mjs";

test("production HTTP calls are owned by api-client", async () => {
  const offenders = [];
  for (const { path, module } of await appSourceModules()) {
    if (path === "api-client.ts") continue;
    const direct = callsIn(module).filter((target) => target === "fetch" || target === "globalThis.fetch");
    if (direct.length > 0) offenders.push({ path, direct });
  }
  assert.deepEqual(offenders, []);
});
