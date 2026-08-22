"use client";

import type {
  SystemExtensionProjection,
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
}>;


export function WorkspaceExtensionOutlet({
  slot,
  registry,
  projection,
  context,
}: WorkspaceExtensionOutletProps) {
  const visible = visibleWorkspaceUiContributions(registry, projection, {
    slot,
    uiMode: context.uiMode,
    permissions: context.permissions,
  });
  if (visible.length === 0) return null;
  return <>{visible.map((contribution) => (
    <contribution.Component
      key={`${context.actor.id}:${context.notebook.id}:${context.source?.id ?? ""}:${contribution.id}`}
      context={context}
    />
  ))}</>;
}
