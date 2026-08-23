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
 * **两条登记在案的限制**，插件作者与后来改这里的人都要知道：
 *
 * ① **只保证在 `workspace.side_panel` 下正确定位。** `FloatingModalCard` 始终给卡片
 *    施加 `translate3d(...)`（静止时也是 `translate3d(0,0,0)`），卡片因此成为其
 *    `position: fixed` 后代的**包含块**。`source.detail_section` 的宿主本身就是一个
 *    `FloatingModalCard`，所以在那个 slot 下渲染本组件，`.utility-modal` 的 fixed
 *    定位会相对**宿主卡片**而不是视口——弹窗会跟着来源详情窗跑。正解是 portal，
 *    但全仓没有一处 `createPortal`，本轮不引入（引入它要连带处理焦点、inert 与
 *    root-modal coordinator 的层级裁决，是独立一件事）。本轮 `source.detail_section`
 *    的 contribution 请不要开弹窗。
 *
 * **存储键分两段隔离**：`extension.` 前缀隔离的是「插件 vs 核心弹窗」，插件之间靠
 * `pluginId` 那一段隔离。两段都由本组件拼、插件改不了——`pluginId` 由 host 按
 * `contribution.pluginId` 注入进 `context`，插件只是把它原样转交。
 *
 * ② **不接入 `use-root-modal-coordinator`。** 因此它不参与 primary/topmost 的层级
 *    裁决，也不会把底层 dialog 置为 inert。生命周期由 outlet 的 `ownerKey` 门承担：
 *    contribution 的 React key 含 ownerKey，切库/换用户时整棵子树连同这个弹窗一起
 *    卸载（组件测试钉住了这一条）。代价是它可能与一个核心 dialog 同时可见；扩到
 *    `RootModalSlot` 是独立一件事（实现计划 R4 登记接受）。
 */
import type { ReactNode } from "react";

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
   * 本 contribution 的插件身份，插件从 `context.pluginId` 原样传进来（host 按
   * `contribution.pluginId` 注入，插件说了不算）。
   *
   * **它是存储键里承重的那一段**：没有它，两个插件都写 `storageKey="search"` 就共用
   * 同一格 sessionStorage 记忆、互相顶掉对方的窗口位置。`extension.` 前缀只隔离
   * 「插件 vs 核心弹窗」，隔离不了「插件 vs 插件」。
   */
  pluginId: string;
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
  onClose: () => void;
  children: ReactNode;
}>;


export function ExtensionModal({
  pluginId,
  storageKey,
  title,
  description,
  onClose,
  children,
}: ExtensionModalProps) {
  // 运行时判据，不是类型断言：`pluginId` 走的是 host 注入那条路（形状已由 registry
  // 校验过），而 `storageKey` 完全由插件给。两段都校验是因为「键的哪一段脏了」在
  // 事后完全看不出来——它只表现为两个插件偶尔抢同一格记忆。响亮失败比默默拼一个
  // 畸形键好。
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
  return (
    <section className="utility-modal" role="dialog" aria-modal="true" aria-label={title}>
      <FloatingModalCard storageKey={`extension.${pluginId}.${storageKey}.window`} className="utility-modal-card">
        {(floating) => (<>
          <div className="source-modal-header" {...floating.dragHandleProps}>
            <div>
              <h2>{title}</h2>
              {description ? <p>{description}</p> : null}
            </div>
            {/* 可见内容是 `×`，那也是浏览器算出来的可及名——所以显式给 aria-label，
                否则辅助技术读到的是一个叫「×」的按钮（`title` 有内容时不参与可及名）。 */}
            <button type="button" className="icon-button" onClick={onClose} title="关闭" aria-label="关闭">×</button>
          </div>
          <div className="source-detail-body">{children}</div>
        </>)}
      </FloatingModalCard>
    </section>
  );
}
