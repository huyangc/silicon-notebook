"use client";

import type {
  SystemExtensionProjection,
  WorkspaceExtensionActions,
  WorkspaceExtensionContext,
  WorkspaceExtensionDialogView,
  WorkspaceExtensionSlot,
  WorkspaceUiContribution,
} from "./contracts.ts";
import { withExtensionApi } from "./api.ts";
import { visibleWorkspaceUiContributions } from "./visibility.ts";


/**
 * 非持有者看到的那一份。`zIndex` 取 0 只是让形状完整——`open` 为假时
 * `ExtensionModal` 直接不渲染，这个数到不了 DOM。
 */
const CLOSED_DIALOG: WorkspaceExtensionDialogView = Object.freeze({
  open: false, topmost: false, zIndex: 0,
});


/**
 * `source.detail_section` 的宿主本身就是一个 `FloatingModalCard`（`position: fixed`
 * 相对它解析），并握着与 `extension` 互斥的 `source-detail` primary lease——那格
 * contribution 若调 `actions.openDialog()`，会把宿主自己的 primary 冲突关掉，宿主
 * 一卸载，弹窗（连同这条 contribution）也跟着消失，留下一格永远等不到人渲染的幽灵
 * 认领（`ui.tsx` 头注释①，codex #578 R4 P2）。这条边界不该只靠插件作者读过那段
 * 注释——host 在这里把它做成结构性禁止：该槽位下 `withExtensionApi` 收到的
 * `openDialog`/`closeDialog` 一律替换成这两个函数，**都不触达壳层真正的实现**，
 * 所以那一格永远不会被这个槽位下的任何 contribution 认领，`context.dialog` 也随之
 * 恒为 `CLOSED_DIALOG`（见下方对 `dialogHolder` 判据的无条件旁路）。`ExtensionModal`
 * 在这个槽位下另外整体拒绝渲染（`ui.tsx`），两道防线各自独立生效。
 */
function refuseSourceDetailDialogOpen(contributionId: string): void {
  if (process.env.NODE_ENV !== "production") {
    console.warn(
      `workspace-extension-sdk: contribution "${contributionId}" called actions.openDialog() `
      + 'from the "source.detail_section" slot; the host refuses this request because that '
      + "slot's own host is a conflicting primary lease (see ExtensionModal's doc comment).",
    );
  }
}


/**
 * 该槽位从未真正认领过那格 lease（见上），关闭请求同样是 no-op——多半是插件卸载时
 * 按老习惯调用的清理代码，不必也不该为它打警告。
 */
function refuseSourceDetailDialogClose(): void {}


type WorkspaceExtensionOutletProps = Readonly<{
  slot: WorkspaceExtensionSlot;
  registry: readonly WorkspaceUiContribution[];
  projection: SystemExtensionProjection | null;
  /**
   * 壳层给的是**不含 `pluginId` 与 `dialog`** 的那一半：一个 outlet 会渲染多条来自
   * 不同插件的 contribution，插件身份与「这一格弹窗现在是不是你的」都只可能由 host
   * 逐条补上（见下面的 spread）。壳层若自己写一个 `pluginId`／一份 `dialog`，它对
   * 这一格里的每条 contribution 都是错的。
   */
  context: Omit<WorkspaceExtensionContext, "pluginId" | "dialog">;
  actions: WorkspaceExtensionActions;
  ownerKey: string;
  /** 核心 root-dialog 裁决给唯一那格 `extension` slot 算出的 view（壳层直接透传）。 */
  dialog: WorkspaceExtensionDialogView;
  /**
   * 当前认领了那一格的 **contribution id**（不是 plugin id）；`null` 表示没人认领。
   * 同一个插件可以注册多条 contribution，按 plugin 判会让它们一起挂出弹窗。
   */
  dialogHolder: string | null;
}>;


export function WorkspaceExtensionOutlet({
  slot,
  registry,
  projection,
  context,
  actions,
  ownerKey,
  dialog,
  dialogHolder,
}: WorkspaceExtensionOutletProps) {
  const visible = visibleWorkspaceUiContributions(registry, projection, {
    slot,
    uiMode: context.uiMode,
    permissions: context.permissions,
  });
  if (visible.length === 0) return null;
  // `source.detail_section` 结构性拒绝弹窗（见上面 refuseSourceDetailDialogOpen 的
  // 注释）：这一整个 outlet 下的 contribution 拿到的 `openDialog`/`closeDialog` 都不
  // 触达壳层真正的实现，跟哪条 contribution 自己是谁无关——判据是槽位，不是插件。
  const dialogActions: WorkspaceExtensionActions = slot === "source.detail_section"
    ? { ...actions, openDialog: refuseSourceDetailDialogOpen, closeDialog: refuseSourceDetailDialogClose }
    : actions;
  return (
    <aside className={`workspace-extension-outlet workspace-extension-outlet-${slot.replace(".", "-")}`}>
      {visible.map((contribution) => (
        <contribution.Component
          key={`${ownerKey}:${context.source?.id ?? ""}:${contribution.id}`}
          // 插件身份逐条补：`ExtensionModal` 用它给窗口位置的存储键分段，两个插件
          // 各写一个 `storageKey="search"` 也不会共用一格记忆。这里不新增任何引用
          // 稳定性承诺——`context` 本来就是每帧新对象（见下面那段），spread 只是让
          // 它多带一个取值稳定的字段。
          //
          // `dialog` 同理逐条补，**并且按持有者过滤**：核心那格 `extension` slot 一次
          // 只归一条 contribution，非持有者恒拿 closed view。少了这道过滤，多条
          // contribution 会在同一格 lease 上同时挂出弹窗——协调器只知道"那一格开着"，
          // 分不出是谁开的。键是 `contribution.id` 而不是 `pluginId`：同一个插件注册
          // 多条 contribution 时，按 plugin 判会让它们全都看到 open（codex #578 R1 P2）。
          //
          // `source.detail_section` 再叠一道无条件旁路：不看 `dialogHolder` 是否碰巧
          // 等于本 contribution 的 id——该槽位下 `openDialog` 已经不触达壳层，`dialog`
          // 与 `dialogHolder` 这两个 prop 因此永远不该让这一格显得"归它了"；就算调用
          // 方（壳层/测试）传错了，这里仍是 fail-closed 的最后一道防线。
          context={{
            ...context,
            pluginId: contribution.pluginId,
            dialog: slot === "source.detail_section"
              ? CLOSED_DIALOG
              : (dialogHolder === contribution.id ? dialog : CLOSED_DIALOG),
          }}
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
          actions={withExtensionApi(dialogActions, contribution.pluginId, contribution.id)}
        />
      ))}
    </aside>
  );
}
