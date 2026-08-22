import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import { WORKSPACE_UI_CONTRIBUTIONS } from "../../features/extension-sdk/registry.ts";


function normalized(rows) {
  return rows.map((row) => ({
    plugin_id: row.plugin_id ?? row.pluginId,
    version: row.version ?? row.pluginVersion,
    contribution_id: row.contribution_id ?? row.id,
    slot: row.slot,
    capability: row.capability,
  })).sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b)));
}

function assertParity(frontendRows, backendRows) {
  assert.deepEqual(normalized(frontendRows), normalized(backendRows));
}


test("build registry matches the backend UI contract fixture", async () => {
  const fixture = JSON.parse(await readFile(
    new URL("../../../backend/tests/fixtures/ui_extension_contract.json", import.meta.url),
    "utf8",
  ));
  assert.equal(fixture.api_version, "1");
  assertParity(WORKSPACE_UI_CONTRIBUTIONS, fixture.contributions);
});


test("parity comparator is non-vacuous for both directions and exact nesting", () => {
  const good = [{
    plugin_id: "sample-plugin",
    version: "1.0.0",
    contribution_id: "sample-panel",
    slot: "workspace.side_panel",
    capability: "sample.ui.available",
  }];
  assertParity(good, good);
  for (const backendRows of [
    [],
    [{ ...good[0], plugin_id: "other-plugin" }],
    [{ ...good[0], contribution_id: "other-panel" }],
    [{ ...good[0], slot: "source.detail_section" }],
    [{ ...good[0], capability: "other.capability" }],
  ]) assert.throws(() => assertParity(good, backendRows), assert.AssertionError);
  assert.throws(() => assertParity([], good), assert.AssertionError);
});
