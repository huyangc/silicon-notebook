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
          // api 端口按**本条 contribution 的** pluginId 绑定：插件自己 import 不到
          // `./api.ts`，所以这是它拿到端口的唯一途径，也保证了插件 A 造不出插件 B
          // 的端口。
          //
          // ⚠ 插件作者注意这里的两半引用语义不同：`actions` 与 `context` 是**每帧新
          // 对象**（owner 闸就挂在 actions 的两个窄 command 上，每次渲染按当时的 owner
          // 现冻结），**不能**进 `useEffect`/`useMemo` 的依赖数组；`actions.api` 则由
          // `createWorkspaceExtensionApi` 按 pluginId 记忆，跨渲染引用稳定，可以进依赖
          // 数组。端口不闭包 owner，所以那份记忆与 outlet 的 ownerKey 门互不相干——
          // ownerKey 门管的是组件树的生命周期（它在下面的 key 里），不是端口的身份。
          actions={withExtensionApi(actions, contribution.pluginId)}
        />
      ))}
    </aside>
  );
}
