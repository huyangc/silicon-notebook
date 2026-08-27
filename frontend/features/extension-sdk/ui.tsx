"use client";

/**
 * SDK 共享 UI：插件唯一被允许使用的弹窗外壳。
 *
 * 它存在的理由不是省几行 JSX，而是把「视觉必须复用系统色板/边框/圆角/排版」这条
 * 红线做成**结构性**的：插件拿到的是一个只接内容的容器，骨架类名全在这里写死，
 * 插件那侧没有任何写 CSS 或内联颜色的入口（`extension-ui-layout-guard` 两侧都钉）。
 * 拖动同样是白拿的——`FloatingModalCard` 是全仓浮窗的唯一实现，插件不必也不得
 * 另造一套（见 `docs/development.md` 的跨界面前端交互护栏）。
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
 *  · **键盘焦点自己管，归还不管**（codex #578 review）。变为最上层时本组件把焦点
 *    从背景的触发按钮移进对话框（关闭按钮），并在最上层期间把 Tab/Shift+Tab 圈在
 *    对话框内——镜像 `app/model-service-panel.tsx` 现有的同款 `.utility-modal` +
 *    `topmost`/`interactive` 做法，而不是另造一套。**归还**焦点（关闭之后回到当初
 *    触发它的那个背景按钮）仍完全是协调器 `pendingFocusReturnRef` 的职责——那份
 *    记录在 `rootModals.open()` 那一刻捕获 `document.activeElement`，本组件够不着
 *    也不该重做。
 *  · **一次只有一个插件弹窗**。持有者由壳层记着、host 按 `contribution.id` 过滤
 *    （不是 plugin id——同一个插件可以注册多条 contribution），非持有者拿到的恒是
 *    一份 closed view。窗口位置记忆仍按 `pluginId` 分段：那是"记在哪一格"，粒度
 *    到插件就够了。
 *
 * **一条登记在案的限制，现已从"请不要"升级为结构性禁止**（codex #578 R4 P2），插件
 * 作者与后来改这里的人都要知道：
 *
 * ① **`source.detail_section` 下本组件直接拒绝渲染。** 两个理由都是硬约束，不是风格
 *    偏好：`FloatingModalCard` 始终给卡片施加 `translate3d(...)`（静止时也是
 *    `translate3d(0,0,0)`），卡片因此成为其 `position: fixed` 后代的**包含块**——
 *    `source.detail_section` 的宿主本身就是一个 `FloatingModalCard`，若允许在那个 slot
 *    下渲染本组件，`.utility-modal` 的 fixed 定位会相对**宿主卡片**而不是视口，弹窗会
 *    跟着来源详情窗跑（正解是 portal，但全仓没有一处 `createPortal`，本轮不引入）。
 *    第二个理由同样硬：那个 slot 的宿主握着 `source-detail` 这条 primary lease，而
 *    `extension` 也是 primary——开它会把来源详情窗冲突关掉，宿主一卸载，弹窗自己也
 *    没了，留下一格永远等不到人渲染的幽灵认领。所以本组件在 `context.slot ===
 *    "source.detail_section"` 时**无条件** `throw`（开发期响亮失败，见函数体）——不是
 *    "调用方最好别这样做"，而是这样做会立刻炸；配套地，`host.tsx` 已经把该槽位下
 *    `actions.openDialog()`/`closeDialog()` 换成不触达壳层的拒绝函数，`context.dialog`
 *    也恒为冻结的 CLOSED 视图，两道防线独立生效，任何一道单独失守都还有另一道兜底。
 *
 * **存储键分两段隔离**：`extension.` 前缀隔离的是「插件 vs 核心弹窗」，插件之间靠
 * `pluginId` 那一段隔离。两段都由本组件拼、插件改不了——`pluginId` 由 host 按
 * `contribution.pluginId` 注入进 `context`，插件只是把它原样转交。
 *
 * 生命周期还有第二道兜底：outlet 的 `ownerKey` 门——contribution 的 React key 含
 * ownerKey，切库/换用户时整棵子树连同这个弹窗一起卸载。它与协调器的 owner 失效是
 * 同向的两条路，组件测试两条都钉住了。
 */
import {
  useEffect,
  useId,
  useRef,
  type ComponentPropsWithoutRef,
  type FormEventHandler,
  type ReactNode,
} from "react";

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

/**
 * SDK 内容组件层：把「插件不能写 CSS / 不能写内联颜色」这条视觉红线继续往面板内容里
 * 推进。插件仍然只传结构与文案；输入框、清单、提示条这些基础版式由宿主统一提供，
 * 这样仓库外插件就不会因为守规而退化成浏览器默认样式。
 */

export type ExtensionFormRowProps = Readonly<{
  children: ReactNode;
  onSubmit?: FormEventHandler<HTMLFormElement>;
} & Omit<ComponentPropsWithoutRef<"form">, "children" | "className" | "onSubmit">>;

export function ExtensionFormRow({
  children,
  onSubmit,
  ...props
}: ExtensionFormRowProps) {
  return (
    <form
      {...props}
      className="extension-form-row"
      onSubmit={onSubmit}
    >
      {children}
    </form>
  );
}

export type ExtensionTextInputProps = Omit<
  ComponentPropsWithoutRef<"input">,
  "className" | "type"
>;

export function ExtensionTextInput(props: ExtensionTextInputProps) {
  return <input {...props} type="text" className="extension-input" />;
}

export type ExtensionResultListProps = Readonly<{
  children: ReactNode;
} & Omit<ComponentPropsWithoutRef<"ul">, "children" | "className">>;

export function ExtensionResultList({
  children,
  ...props
}: ExtensionResultListProps) {
  return (
    <ul {...props} className="extension-result-list">
      {children}
    </ul>
  );
}

export type ExtensionResultCheckbox = Readonly<{
  checked: boolean;
  onChange(checked: boolean): void;
  ariaLabel: string;
}>;

export type ExtensionResultItemProps = Readonly<{
  checkbox?: ExtensionResultCheckbox;
  title: ReactNode;
  meta?: ReactNode;
  summary?: ReactNode;
  children?: ReactNode;
} & Omit<ComponentPropsWithoutRef<"li">, "children" | "className">>;

export function ExtensionResultItem({
  checkbox,
  title,
  meta,
  summary,
  children,
  ...props
}: ExtensionResultItemProps) {
  const checkboxId = useId();
  return (
    <li {...props} className="extension-result-item">
      <div className="extension-result-item-row">
        {checkbox ? (
          <input
            type="checkbox"
            id={checkboxId}
            className="extension-result-item-checkbox"
            checked={checkbox.checked}
            aria-label={checkbox.ariaLabel}
            onChange={(event) => checkbox.onChange(event.target.checked)}
          />
        ) : null}
        <div className="extension-result-item-copy">
          {checkbox ? (
            <label htmlFor={checkboxId} className="extension-result-item-title">{title}</label>
          ) : (
            <div className="extension-result-item-title">{title}</div>
          )}
          {meta ? <div className="extension-result-item-meta">{meta}</div> : null}
          {summary ? <div className="extension-result-item-summary">{summary}</div> : null}
          {children}
        </div>
      </div>
    </li>
  );
}

export type ExtensionActionsProps = Readonly<{
  children: ReactNode;
} & Omit<ComponentPropsWithoutRef<"div">, "children" | "className">>;

export function ExtensionActions({
  children,
  ...props
}: ExtensionActionsProps) {
  return (
    <div {...props} className="extension-actions">
      {children}
    </div>
  );
}

export type ExtensionAlertTone = "error" | "warning" | "status";
export type ExtensionAlertProps = Readonly<{
  tone: ExtensionAlertTone;
  children: ReactNode;
} & Omit<ComponentPropsWithoutRef<"div">, "children" | "className" | "role">>;

export function ExtensionAlert({
  tone,
  children,
  ...props
}: ExtensionAlertProps) {
  if (tone === "error") {
    return (
      <div {...props} role="alert" className="extension-alert extension-alert--error">
        {children}
      </div>
    );
  }
  if (tone === "warning") {
    return (
      <div {...props} role="alert" className="extension-alert extension-alert--warning">
        {children}
      </div>
    );
  }
  return (
    <div {...props} role="status" className="extension-alert extension-alert--status">
      {children}
    </div>
  );
}

export type ExtensionEmptyStateProps = Readonly<{
  children: ReactNode;
} & Omit<ComponentPropsWithoutRef<"p">, "children" | "className">>;

export function ExtensionEmptyState({
  children,
  ...props
}: ExtensionEmptyStateProps) {
  return (
    <p {...props} className="extension-empty">
      {children}
    </p>
  );
}


export function ExtensionModal({
  context,
  actions,
  storageKey,
  title,
  description,
  children,
}: ExtensionModalProps) {
  const dialogRef = useRef<HTMLElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  // Hooks 必须排在下面几条 `throw`/`return null` 之前无条件调用（Rules of Hooks）：
  // 那几条判的是「这个组件压根不该出现在这里」，是构造期就该炸穿的合同违反，不是
  // 随渲染在 valid/invalid 之间来回翻的正常状态；把 hooks 摆在它们之后会让"同一个
  // 组件实例这次渲染调了 hooks、上次却在 hooks 之前就抛了"这种理论上的顺序错位
  // 变得可能。`topmost` 用可选链读——此刻 `context`/`context.dialog` 都还没被下面
  // 那几条校验过。
  const topmost = context?.dialog?.topmost === true;

  // 打开且在最上层时把焦点从背景的触发按钮移进对话框，镜像
  // `app/model-service-panel.tsx` 现有的同款 `.utility-modal` + `interactive`
  // 做法（那也是 core 最接近 `ExtensionModal` 结构的一个 primary 弹窗）。只依赖
  // `topmost`：它在打开时 false→true 翻一次（触发聚焦），关闭时 true→false 再翻
  // 一次（`closeButtonRef.current` 此刻多半已经是 null 或一个即将卸载的按钮，
  // `?.focus()` 是安全的 no-op）。**归还**焦点不在这里做——那是协调器
  // `pendingFocusReturnRef` 在 `rootModals.open()` 那一刻就捕获好的记录，回到关闭
  // 前的背景按钮。
  useEffect(() => {
    if (!topmost) return;
    closeButtonRef.current?.focus();
  }, [topmost]);

  // Tab/Shift+Tab 环绕，只在最上层生效——同样镜像 `model-service-panel.tsx`：绑在
  // `window` 而不是本组件的 `onKeyDown`，这样即便焦点已经跑出对话框（比如浏览器
  // 怪癖、或某个被覆盖层短暂抢走过焦点），也能在下一次 Tab 时把它按回来；本地
  // `onKeyDown` 处理不了「按下 Tab 那一刻焦点已经不在这棵子树里」的情形。被 `info`
  // 盖住时整棵子树本该已经 `inert`（浏览器语义上挡掉焦点进出），这里的 `topmost`
  // 判据是双保险，也是让 jsdom 下的用例钉得住「被盖住时不圈」这条的唯一依据——
  // jsdom 对 `inert` 的语义支持并不完整。**刻意不接 Escape**：`extension` slot 在
  // 协调器的 policy 表里是 `escape: false`（`use-root-modal-coordinator.ts` 的注释
  // 写明这不是遗漏），`WorkspaceExtensionDialogCloseReason` 的类型也只有
  // `"button" | "backdrop"` 两个值，给插件弹窗单独开 Escape 会比任何核心弹窗都更
  // 容易被误关。
  useEffect(() => {
    if (!topmost) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== "Tab") return;
      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(
        'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ));
      if (focusable.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (!(active instanceof HTMLElement) || !dialog.contains(active)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
        return;
      }
      if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [topmost]);

  // `source.detail_section` 结构性禁止——两条理由都写在头注释①里（定位相对宿主卡片
  // 而不是视口、宿主自己就是与 `extension` 互斥的 primary lease）。这条检查排在最
  // 前面且**无条件**：与下面几条不同，它不是在防某个字段畸形，而是"这个组件压根不
  // 该出现在这个槽位"，跟 `dialog`/`pluginId` 长什么样无关——即使 host 那道拒绝
  // （见 `host.tsx`）失守让 `dialog.open` 意外为真，这里仍要响亮失败而不是渲染一个
  // 定位错误、随时可能把来源详情窗一起干掉的弹窗。
  if (context?.slot === "source.detail_section") {
    throw new TypeError("ExtensionModal is only available to workspace.side_panel contributions");
  }
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
      ref={dialogRef}
      // 无实际内容时（理论上不会发生，`FloatingModalCard` 恒渲染带关闭按钮的头部）
      // 让 Tab 陷阱的零可聚焦兜底分支有地方落焦点，见上面的 `onKeyDown`。
      tabIndex={-1}
      className="utility-modal"
      role="dialog"
      // 被盖住时整棵子树退出交互树（`inert`）并对辅助技术隐藏，与每个核心 root dialog
      // 同一口径。**焦点归还**（关闭后回到触发它的背景按钮）仍由协调器的 layout
      // effect 统一做，这里不碰；**焦点移入与 Tab 陷阱**是本组件上面那两个 effect
      // 的职责，两者分工不重叠。
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
                否则辅助技术读到的是一个叫「×」的按钮（`title` 有内容时不参与可及名）。
                同时是打开/成为最上层时的默认焦点落点（见上面那个 `useEffect`）：它是
                本组件唯一自己渲染、保证每次都存在的可聚焦元素。 */}
            <button ref={closeButtonRef} type="button" className="icon-button" onClick={() => actions.closeDialog()} title="关闭" aria-label="关闭">×</button>
          </div>
          <div className="source-detail-body">{children}</div>
        </>)}
      </FloatingModalCard>
    </section>
  );
}
