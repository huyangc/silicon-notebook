import test from "node:test";
import assert from "node:assert/strict";

import {
  appSourceModules,
  callsIn,
  findFunction,
  importsIn,
  parseModule,
} from "./test/semantic-source.mjs";

test("production HTTP calls are owned by api-client", async () => {
  const offenders = [];
  for (const { path, module } of await appSourceModules()) {
    if (path === "api-client.ts") continue;
    const direct = callsIn(module).filter((target) => target === "fetch" || target === "globalThis.fetch");
    if (direct.length > 0) offenders.push({ path, direct });
  }
  assert.deepEqual(offenders, []);
});

test("notebook search is owned by Ask and imported by the workspace", async () => {
  const [ask, page] = await Promise.all([
    parseModule("ask-api.ts"),
    parseModule("page.tsx"),
  ]);
  assert.ok(findFunction(ask, "searchNotebook"));
  assert.ok(importsIn(page).some((item) => (
    item.module === "./ask-api" && item.imported === "searchNotebook"
  )));
});
