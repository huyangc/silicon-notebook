"use client";

/**
 * SDK 共享 UI：插件唯一被允许使用的弹窗外壳。
 *
 * 它存在的理由不是省几行 JSX，而是把「视觉必须复用系统色板/边框/圆角/排版」这条
 * 红线做成**结构性**的：插件拿到的是一个只接内容的容器，骨架类名全在这里写死，
 * 插件那侧没有任何写 CSS 或内联颜色的入口（`extension-ui-layout-guard` 两侧都钉）。
 * 拖动同样是白拿的——`FloatingModalCard` 是全仓浮窗的唯一实现，插件不必也不得
 * 另造一套（CLAUDE.md「浮动弹窗」）。
 *
 * **它接入核心的 root-dialog 裁决**（`app/use-root-modal-coordinator.ts` 的
 * `"extension"` slot），走的是"一个通用格子 + 一个 SDK 端口"而不是"每个插件一个
 * slot"：核心的 slot 联合类型里不出现任何插件名（零补丁红线）。因此——
 *
 *  · **开关不归插件**。插件调 `actions.openDialog()` 提出请求，真开不开由
 *    `context.dialog.open` 说了算；被别的 primary 弹窗顶掉、切库、换用户都会把它
 *    关掉，插件不必也不该自己维护一个 `useState`。
 *  · **被盖住时退出交互树**。`context.dialog.topmost` 为假（例如上面盖了一个 `info`
 *    确认框）时本组件加 `inert` + `aria-hidden`，背景控件不再可聚焦；焦点归还由
 *    协调器的 layout effect 统一做。
 *  · **一次只有一个插件弹窗**。持有者由壳层记着、host 按 `contribution.id` 过滤
 *    （不是 plugin id——同一个插件可以注册多条 contribution），非持有者拿到的恒是
 *    一份 closed view。窗口位置记忆仍按 `pluginId` 分段：那是"记在哪一格"，粒度
 *    到插件就够了。
 *
 * **一条登记在案的限制**，插件作者与后来改这里的人都要知道：
 *
 * ① **只保证在 `workspace.side_panel` 下正确定位。** `FloatingModalCard` 始终给卡片
 *    施加 `translate3d(...)`（静止时也是 `translate3d(0,0,0)`），卡片因此成为其
 *    `position: fixed` 后代的**包含块**。`source.detail_section` 的宿主本身就是一个
 *    `FloatingModalCard`，所以在那个 slot 下渲染本组件，`.utility-modal` 的 fixed
 *    定位会相对**宿主卡片**而不是视口——弹窗会跟着来源详情窗跑。正解是 portal，
 *    但全仓没有一处 `createPortal`，本轮不引入。第二个理由同样硬：那个 slot 的宿主
 *    握着 `source-detail` 这条 primary lease，而 `extension` 也是 primary——开它会
 *    把来源详情窗冲突关掉，宿主一卸载，弹窗自己也没了。本轮 `source.detail_section`
 *    的 contribution 请不要开弹窗。
 *
 * **存储键分两段隔离**：`extension.` 前缀隔离的是「插件 vs 核心弹窗」，插件之间靠
 * `pluginId` 那一段隔离。两段都由本组件拼、插件改不了——`pluginId` 由 host 按
 * `contribution.pluginId` 注入进 `context`，插件只是把它原样转交。
 *
 * 生命周期还有第二道兜底：outlet 的 `ownerKey` 门——contribution 的 React key 含
 * ownerKey，切库/换用户时整棵子树连同这个弹窗一起卸载。它与协调器的 owner 失效是
 * 同向的两条路，组件测试两条都钉住了。
 */
import type { ReactNode } from "react";

import type {
  WorkspaceExtensionContext,
  WorkspaceExtensionPluginActions,
} from "./contracts.ts";
import { FloatingModalCard } from "../../app/floating-modal-card.tsx";


/** 与 `registry.ts` 的 `STABLE_ID`／`api.ts` 的 `PLUGIN_ID` 同一条形状。 */
const PLUGIN_ID = /^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$/;
/**
 * 插件自己那一段的形状：一到多个由 `.`/`_`/`-` 连接的字母数字短词，上限 64 字符。
 *
 * 它不必与 `PLUGIN_ID` 同形（插件可以写 `panel.left`），但必须挡住 `/`、空白与
 * 点段——存储键是 sessionStorage 的裸键，一个 `..` 段在这里不会穿越目录，可它会让
 * 键长成另一个插件的键。
 */
const STORAGE_KEY_SEGMENT = /^[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*$/;
const STORAGE_KEY_MAX = 64;


export type ExtensionModalProps = Readonly<{
  /**
   * 插件收到的那份 `context`，**原样转交**。本组件从中取两样东西：
   *
   *  · `pluginId` —— host 按 `contribution.pluginId` 注入，插件说了不算。**它是存储
   *    键里承重的那一段**：没有它，两个插件都写 `storageKey="search"` 就共用同一格
   *    sessionStorage 记忆、互相顶掉对方的窗口位置。`extension.` 前缀只隔离「插件 vs
   *    核心弹窗」，隔离不了「插件 vs 插件」。
   *  · `dialog` —— 核心 root-dialog 裁决算出的 open/topmost/zIndex。这是弹窗可见性的
   *    **唯一**真相来源：插件自己那个 `useState` 关不掉一次被协调器顶掉的弹窗。
   */
  context: WorkspaceExtensionContext;
  /**
   * 插件收到的那份 `actions`，用来关闭本弹窗。关闭**只**经 `actions.closeDialog()`：
   * 那格 lease 归协调器，插件自行 `setOpen(false)` 会留下一格永远等不到关闭通知的
   * 认领（弹窗还会照常渲染，因为 `context.dialog.open` 仍为真）。
   */
  actions: Pick<WorkspaceExtensionPluginActions, "closeDialog">;
  /**
   * 本会话内记住拖动位置的键，**只是插件自己那一段**。实际存储键是
   * `extension.<pluginId>.<storageKey>.window`——前缀与 pluginId 段由本组件加，插件
   * 改不了，所以插件之间、以及插件与核心弹窗之间都不会互相顶掉位置记忆。
   */
  storageKey: string;
  /** 标题栏文案，同时作为 dialog 的可及名（`aria-label`）。 */
  title: string;
  /** 可选的一句说明，排在标题下方。 */
  description?: string;
  children: ReactNode;
}>;


export function ExtensionModal({
  context,
  actions,
  storageKey,
  title,
  description,
  children,
}: ExtensionModalProps) {
  const pluginId = context?.pluginId;
  const dialog = context?.dialog;
  // 运行时判据，不是类型断言：`pluginId` 与 `dialog` 走的是 host 注入那条路（形状已由
  // registry / 协调器保证），而 `storageKey` 完全由插件给。三样都校验是因为它们坏掉
  // 之后都**看不出来**——畸形存储键只表现为两个插件偶尔抢同一格记忆，而缺席的
  // `dialog` 会让弹窗永远不出现（或永远不退出交互树）。响亮失败比默默错着跑好。
  if (typeof pluginId !== "string" || !PLUGIN_ID.test(pluginId)) {
    throw new TypeError("ExtensionModal: pluginId must be the contribution's registered plugin id");
  }
  if (
    typeof storageKey !== "string"
    || storageKey.length > STORAGE_KEY_MAX
    || !STORAGE_KEY_SEGMENT.test(storageKey)
  ) {
    throw new TypeError("ExtensionModal: storageKey must be a short dot/underscore/dash separated name");
  }
  if (
    !dialog
    || typeof dialog.open !== "boolean"
    || typeof dialog.topmost !== "boolean"
    || typeof dialog.zIndex !== "number"
  ) {
    throw new TypeError("ExtensionModal: context.dialog must be the host-injected root-dialog view");
  }
  // 可见性归核心：插件请求过、协调器也答应了，才有这一格。插件自己的 state 在这里
  // 说了不算——它关不掉一次被更晚的 primary 顶掉、或被切库撤销的弹窗。
  if (!dialog.open) return null;
  return (
    <section
      className="utility-modal"
      role="dialog"
      // 被盖住时整棵子树退出交互树（`inert`）并对辅助技术隐藏，与每个核心 root dialog
      // 同一口径；焦点归还由协调器的 layout effect 统一做，这里不碰焦点。
      aria-modal={dialog.topmost}
      aria-hidden={!dialog.topmost}
      inert={dialog.topmost ? undefined : true}
      aria-label={title}
      style={{ zIndex: dialog.zIndex }}
      onClick={(event) => {
        // 只认落在遮罩自身上的点击（卡片内的冒泡不算），与核心弹窗逐字同一个判据。
        if (event.currentTarget === event.target) actions.closeDialog("backdrop");
      }}
    >
      <FloatingModalCard storageKey={`extension.${pluginId}.${storageKey}.window`} className="utility-modal-card">
        {(floating) => (<>
          <div className="source-modal-header" {...floating.dragHandleProps}>
            <div>
              <h2>{title}</h2>
              {description ? <p>{description}</p> : null}
            </div>
            {/* 可见内容是 `×`，那也是浏览器算出来的可及名——所以显式给 aria-label，
                否则辅助技术读到的是一个叫「×」的按钮（`title` 有内容时不参与可及名）。 */}
            <button type="button" className="icon-button" onClick={() => actions.closeDialog()} title="关闭" aria-label="关闭">×</button>
          </div>
          <div className="source-detail-body">{children}</div>
        </>)}
      </FloatingModalCard>
    </section>
  );
}
