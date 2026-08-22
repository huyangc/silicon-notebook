import type { WorkspaceUiContribution } from "./contracts.ts";
import { AgentProfileWorkspacePanel } from "../agent-profile/workspace-plugin.ts";


const STABLE_ID = /^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$/;
const SLOTS = new Set(["workspace.side_panel", "source.detail_section"]);
const PERMISSIONS = new Set([
  "notebook:read",
  "notebook:write",
  "notebook:configure",
  "source:read",
  "source:write",
  "system:admin",
]);
const MODES = new Set(["all", "advanced"]);


export function defineWorkspaceUiRegistry(
  contributions: readonly WorkspaceUiContribution[],
): readonly WorkspaceUiContribution[] {
  const ids = new Set<string>();
  const rows = [...contributions];
  for (const row of rows) {
    if (
      !STABLE_ID.test(row.id)
      || !STABLE_ID.test(row.pluginId)
      || !STABLE_ID.test(row.capability)
      || typeof row.pluginVersion !== "string"
      || row.pluginVersion.length === 0
      || !SLOTS.has(row.slot)
      || !PERMISSIONS.has(row.permission)
      || !MODES.has(row.mode)
      || typeof row.Component !== "function"
    ) {
      throw new TypeError("workspace UI contribution metadata is invalid");
    }
    if (ids.has(row.id)) throw new TypeError(`duplicate workspace UI contribution: ${row.id}`);
    ids.add(row.id);
  }
  rows.sort((left, right) => left.id.localeCompare(right.id));
  return Object.freeze(rows.map((row) => Object.freeze({ ...row })));
}


export const WORKSPACE_UI_CONTRIBUTIONS = defineWorkspaceUiRegistry([{
  id: "builtin.ask_agent_profile.workspace_panel",
  pluginId: "builtin.ask_agent_profile",
  pluginVersion: "1.0.0",
  capability: "ui.agent_profile.available",
  slot: "workspace.side_panel",
  permission: "notebook:read",
  mode: "all",
  Component: AgentProfileWorkspacePanel,
}]);
