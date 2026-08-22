import type { UiMode } from "../../app/ui-mode.ts";
import type {
  SystemExtensionProjection,
  WorkspaceExtensionPermission,
  WorkspaceExtensionPermissionSnapshot,
  WorkspaceExtensionSlot,
  WorkspaceUiContribution,
} from "./contracts.ts";


export type WorkspaceVisibilityGate = Readonly<{
  slot: WorkspaceExtensionSlot;
  uiMode: UiMode;
  permissions: WorkspaceExtensionPermissionSnapshot;
}>;


function permissionAllowed(
  permission: WorkspaceExtensionPermission,
  snapshot: WorkspaceExtensionPermissionSnapshot,
): boolean {
  switch (permission) {
    case "notebook:read": return snapshot.notebookRead;
    case "notebook:write": return snapshot.notebookWrite;
    case "notebook:configure": return snapshot.notebookConfigure;
    case "source:read": return snapshot.sourceRead;
    case "source:write": return snapshot.sourceWrite;
    case "system:admin": return snapshot.systemAdmin;
  }
}


export function visibleWorkspaceUiContributions(
  registry: readonly WorkspaceUiContribution[],
  projection: SystemExtensionProjection | null,
  gate: WorkspaceVisibilityGate,
): readonly WorkspaceUiContribution[] {
  if (!projection || projection.apiVersion !== "1") return [];
  const exactRows = new Map<string, SystemExtensionProjection["extensions"][number] | null>();
  for (const row of projection.extensions) {
    const key = `${row.pluginId}\0${row.version}\0${row.contributionId}`;
    exactRows.set(key, exactRows.has(key) ? null : row);
  }
  return registry.filter((contribution) => {
    const server = exactRows.get(
      `${contribution.pluginId}\0${contribution.pluginVersion}\0${contribution.id}`,
    );
    return (
      contribution.slot === gate.slot
      && server !== undefined
      && server !== null
      && server.available === true
      && permissionAllowed(contribution.permission, gate.permissions)
      && (contribution.mode === "all" || gate.uiMode === "advanced")
    );
  });
}
