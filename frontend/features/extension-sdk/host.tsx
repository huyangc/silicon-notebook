"use client";

import type {
  SystemExtensionProjection,
  WorkspaceExtensionActions,
  WorkspaceExtensionContext,
  WorkspaceExtensionSlot,
  WorkspaceUiContribution,
} from "./contracts.ts";
import { withExtensionApi } from "./api.ts";
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
          // api 端口按**本条 contribution 的** pluginId 绑定，逐 contribution 现造：
          // 插件自己 import 不到 `./api.ts`，所以这是它拿到端口的唯一途径，也保证了
          // 插件 A 造不出插件 B 的端口。刻意不 memo——`createWorkspaceExtensionApi`
          // 只是拼几个闭包（零 I/O、零 hook），而模块级缓存会把一个跨 owner 存活的
          // 对象引进来，正好抵消 outlet 的 ownerKey 门。
          actions={withExtensionApi(actions, contribution.pluginId)}
        />
      ))}
    </aside>
  );
}
