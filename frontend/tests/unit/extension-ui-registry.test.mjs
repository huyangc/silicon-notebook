import test from "node:test";
import assert from "node:assert/strict";

import { defineWorkspaceUiRegistry } from "../../features/extension-sdk/registry.ts";
import { visibleWorkspaceUiContributions } from "../../features/extension-sdk/visibility.ts";


function Component() { return null; }

const base = {
  id: "sample-panel",
  pluginId: "sample-plugin",
  pluginVersion: "1.2.3",
  capability: "sample.ui.available",
  slot: "workspace.side_panel",
  permission: "notebook:read",
  mode: "all",
  Component,
};

const permissions = {
  notebookRead: true,
  notebookWrite: true,
  notebookConfigure: true,
  sourceRead: true,
  sourceWrite: true,
  systemAdmin: true,
};

const projection = {
  apiVersion: "1",
  extensions: [{
    pluginId: "sample-plugin",
    displayName: "Sample",
    version: "1.2.3",
    contributionId: "sample-panel",
    available: true,
    unavailableReason: null,
  }],
};


test("workspace registry freezes stable order and rejects aliases or duplicates", () => {
  const registry = defineWorkspaceUiRegistry([
    { ...base, id: "z-panel" },
    { ...base, id: "a-panel" },
  ]);
  assert.deepEqual(registry.map((row) => row.id), ["a-panel", "z-panel"]);
  assert.equal(Object.isFrozen(registry), true);
  assert.equal(Object.isFrozen(registry[0]), true);
  assert.throws(() => defineWorkspaceUiRegistry([base, base]), /duplicate/);
  assert.throws(
    () => defineWorkspaceUiRegistry([{ ...base, slot: "side_panel" }]),
    /invalid/,
  );
  assert.throws(
    () => defineWorkspaceUiRegistry([{ ...base, slot: "source_detail_section" }]),
    /invalid/,
  );
});


test("all four visibility gates are conjunctive across the full truth table", () => {
  for (let mask = 0; mask < 16; mask += 1) {
    const local = Boolean(mask & 1);
    const server = Boolean(mask & 2);
    const permission = Boolean(mask & 4);
    const mode = Boolean(mask & 8);
    const registry = local
      ? defineWorkspaceUiRegistry([{ ...base, mode: "advanced" }])
      : defineWorkspaceUiRegistry([]);
    const rows = visibleWorkspaceUiContributions(
      registry,
      server ? projection : { apiVersion: "1", extensions: [] },
      {
        slot: "workspace.side_panel",
        uiMode: mode ? "advanced" : "auto",
        permissions: { ...permissions, notebookRead: permission },
      },
    );
    assert.equal(rows.length, mask === 15 ? 1 : 0, `truth-table mask ${mask}`);
  }
});


test("server matching requires exact plugin, version, and contribution and fails closed", () => {
  const registry = defineWorkspaceUiRegistry([base]);
  assert.deepEqual(visibleWorkspaceUiContributions(registry, projection, {
    slot: "source.detail_section", uiMode: "advanced", permissions,
  }), []);
  for (const altered of [
    { pluginId: "other-plugin" },
    { version: "9.9.9" },
    { contributionId: "other-panel" },
    { available: false, unavailableReason: "unavailable" },
  ]) {
    const changed = {
      ...projection,
      extensions: [{ ...projection.extensions[0], ...altered }],
    };
    assert.deepEqual(visibleWorkspaceUiContributions(registry, changed, {
      slot: "workspace.side_panel", uiMode: "advanced", permissions,
    }), []);
  }
  assert.deepEqual(visibleWorkspaceUiContributions(registry, null, {
    slot: "workspace.side_panel", uiMode: "advanced", permissions,
  }), []);
  assert.deepEqual(visibleWorkspaceUiContributions(registry, {
    ...projection,
    extensions: [projection.extensions[0], projection.extensions[0]],
  }, {
    slot: "workspace.side_panel", uiMode: "advanced", permissions,
  }), []);
});


test("permission policies use the core snapshot and source write is separately gated", () => {
  for (const [permission, field] of [
    ["notebook:read", "notebookRead"],
    ["notebook:write", "notebookWrite"],
    ["notebook:configure", "notebookConfigure"],
    ["source:read", "sourceRead"],
    ["source:write", "sourceWrite"],
    ["system:admin", "systemAdmin"],
  ]) {
    const registry = defineWorkspaceUiRegistry([{ ...base, permission }]);
    const denied = { ...permissions, [field]: false };
    assert.equal(visibleWorkspaceUiContributions(registry, projection, {
      slot: "workspace.side_panel", uiMode: "advanced", permissions: denied,
    }).length, 0);
  }
});
