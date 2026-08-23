/**
 * 内建 workspace UI contribution 目录 + 它的校验函数。
 *
 * **本模块与它 import 的一切必须是 `.ts`，绝不能是 `.tsx`。** `node --test` 泳道
 * （`tests/guards/extension-ui-parity.test.mjs`、`tests/unit/extension-ui-registry.test.mjs`）
 * 直接 import 它，而 Node 对 `.tsx` 报 `Unknown file extension`——内建插件写成
 * `workspace-plugin.ts` + `createElement` 而不是 JSX，理由就在这里。
 *
 * 同理，本模块**不得** import `./registry.local.ts`：那份生成文件会 import
 * 仓库外插件包的 `.tsx` 入口，一旦挂进这条闭包，整个 node 泳道当场 import 失败。
 * 合并（内建 + local）发生在 `./workspace-registry.ts`，只有浏览器/vitest/next
 * 这些能处理 `.tsx` 的消费方才 import 那一份。回归门是
 * `tests/guards/extension-module-graph-guard.test.mjs`。
 */
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


export const BUILTIN_WORKSPACE_UI_CONTRIBUTIONS = defineWorkspaceUiRegistry([{
  id: "builtin.ask_agent_profile.workspace_panel",
  pluginId: "builtin.ask_agent_profile",
  pluginVersion: "1.0.0",
  capability: "ui.agent_profile.available",
  slot: "workspace.side_panel",
  permission: "notebook:read",
  mode: "all",
  Component: AgentProfileWorkspacePanel,
}]);
