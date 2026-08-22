import type { ComponentType } from "react";

import type { UiMode } from "../../app/ui-mode.ts";


export type WorkspaceExtensionSlot =
  | "workspace.side_panel"
  | "source.detail_section";

export type WorkspaceExtensionPermission =
  | "notebook:read"
  | "notebook:write"
  | "notebook:configure"
  | "source:read"
  | "source:write"
  | "system:admin";

export type WorkspaceExtensionModePolicy = "all" | "advanced";

export type WorkspaceExtensionPermissionSnapshot = Readonly<{
  notebookRead: boolean;
  notebookWrite: boolean;
  notebookConfigure: boolean;
  sourceRead: boolean;
  sourceWrite: boolean;
  systemAdmin: boolean;
}>;

export type WorkspaceExtensionContext = Readonly<{
  slot: WorkspaceExtensionSlot;
  actor: Readonly<{
    id: string;
    username: string;
    displayName: string;
  }>;
  notebook: Readonly<{
    id: string;
    name: string;
  }>;
  source: Readonly<{
    id: string;
    notebookId: string;
    title: string;
  }> | null;
  uiMode: UiMode;
  permissions: WorkspaceExtensionPermissionSnapshot;
}>;

export type WorkspaceExtensionProps = Readonly<{
  context: WorkspaceExtensionContext;
}>;

export type WorkspaceUiContribution = Readonly<{
  id: string;
  pluginId: string;
  pluginVersion: string;
  capability: string;
  slot: WorkspaceExtensionSlot;
  permission: WorkspaceExtensionPermission;
  mode: WorkspaceExtensionModePolicy;
  Component: ComponentType<WorkspaceExtensionProps>;
}>;

export type SystemExtensionProjection = Readonly<{
  apiVersion: "1";
  extensions: readonly Readonly<{
    pluginId: string;
    displayName: string;
    version: string;
    contributionId: string;
    available: boolean;
    unavailableReason: "disabled" | "unavailable" | null;
  }>[];
}>;
