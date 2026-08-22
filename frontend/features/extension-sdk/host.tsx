"use client";

import type {
  SystemExtensionProjection,
  WorkspaceExtensionActions,
  WorkspaceExtensionContext,
  WorkspaceExtensionSlot,
  WorkspaceUiContribution,
} from "./contracts.ts";
import { visibleWorkspaceUiContributions } from "./visibility.ts";


type WorkspaceExtensionOutletProps = Readonly<{
  slot: WorkspaceExtensionSlot;
  registry: readonly WorkspaceUiContribution[];
  projection: SystemExtensionProjection | null;
  context: WorkspaceExtensionContext;
  actions: WorkspaceExtensionActions;
  ownerKey: string;
}>;


export function WorkspaceExtensionOutlet({
  slot,
  registry,
  projection,
  context,
  actions,
  ownerKey,
}: WorkspaceExtensionOutletProps) {
  const visible = visibleWorkspaceUiContributions(registry, projection, {
    slot,
    uiMode: context.uiMode,
    permissions: context.permissions,
  });
  if (visible.length === 0) return null;
  return (
    <aside className={`workspace-extension-outlet workspace-extension-outlet-${slot.replace(".", "-")}`}>
      {visible.map((contribution) => (
        <contribution.Component
          key={`${ownerKey}:${context.source?.id ?? ""}:${contribution.id}`}
          context={context}
          actions={actions}
        />
      ))}
    </aside>
  );
}
