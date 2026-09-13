/**
 * import-row-state.tsx
 *
 * 「把一个站外链接当一次普通链接来源导入本笔记本」这颗按钮的**逐行状态机**，
 * 两个面共用：
 *   - `answer-gap-suggestions.tsx` 的站外来源建议清单（``ask.gap_consult``）；
 *   - `answer-panel.tsx` 引用卡上的外部证据（``ask.reflect_action``，
 *     `object_type === "external"`，设计文档 §七）。
 *
 * 抽出来的理由不是省几行，而是长任务按钮红线（`docs/development.md`、AGENTS.md
 * 「Interactive feedback」）要求两处**逐字同款**：按下即禁用该行并换成进行中
 * 文案、成功冻结成不可再点的终态、失败把原因**持久**留在按钮紧邻处（绝不发
 * toast、绝不只在页面顶部横幅里说）、失败后按钮回到可点以便重试。两份各写一遍
 * 必然漂移，而漂移的那一份不会有人发现——按钮平时都是一次点击就成功。
 *
 * 状态按调用方给的 `rowKey` 分格。生产调用点（`AnswerView`）**一律用 URL 作键，
 * 且两个面共用同一个 controller 实例**——这是承重的，不是省事：同一个库外链接既
 * 可能出现在站外来源建议里、又可能作为外部证据被引用，两处各持一份状态就意味着
 * 同一个 URL 能被导入两次（后端 `POST /notebooks/{id}/sources/url` 既没有单飞守卫、
 * 也不按 URL 去重）。共享 controller + URL 键之后，任一处导入完成，另一处同一个
 * URL 立刻显示「已导入」，`onImport` 只被调用一次。
 *
 * ⚠ 这条去重的作用域**只到一次回答**。跨轮（同一会话的另一条回答、重开的历史会话）
 * 各有各的 `AnswerView`，也就各有各的 controller，同一个 URL 仍能被导入第二次——与
 * 站外来源建议接入以来的既有行为一致，本次不改。真正能关掉它的是**后端按 URL 去重**
 * （导入端点对已存在的同一 URL 返回既有来源而不是新建一条），后端当前**没有**，
 * 这里**登记为后续项**。前端不假装自己能兜住：跨 React 树的持久去重要么落在后端，
 * 要么落在一份跨 turn 的客户端账本上，两者都不属于本次改动的范围。
 *
 * ⚠ 状态住在**调用 `useImportRowController` 的那个组件**里，同样是承重的：引用卡
 * 是一张会被反复开合的浮层（`CitationPopover` 在点外部/滚动/Esc 时卸载），状态若住
 * 在卡片内部，「已导入」会在浮层关掉的一瞬间蒸发，用户重新点开同一条引用看到的又是
 * 可点的「导入为来源」——于是导入第二次。所以 `AnswerView` 持有 controller 并逐层传
 * 下去，而不是让卡片自己 `useState`。
 */
import { useCallback, useState } from "react";

export type ImportOutcome = { ok: boolean; message?: string };

export type ImportRowState =
  | { status: "idle" }
  | { status: "busy" }
  | { status: "done" }
  | { status: "failed"; message: string };

const IDLE: ImportRowState = { status: "idle" };

/** 调用方没给 message 时的兜底：失败必须说人话，绝不把空字符串送上屏。 */
const FALLBACK_MESSAGE = "未能添加这个链接";

export type ImportRowController = {
  stateOf: (rowKey: string | number) => ImportRowState;
  start: (rowKey: string | number, url: string) => void;
  /** 非空时这一批按钮全部渲染成禁用态，并把这句话挂到 `title` 上（「可写但已达
   *  笔记本文档数量上限」那类先验拒绝：不必先发一次远端探测再撞后端的必然拒绝）。
   *  已导入/导入中两个态的展示优先级高于它——那两态本身已经解释了按钮为什么点
   *  不动。 */
  disabledReason?: string;
};

/**
 * 缺省即不渲染导入按钮：`onImport` 为空时返回 `undefined`（`onSaveMemory` 的既有
 * 惯例——写回服务端的动作没有回调就不出按钮），只读工作区因此一颗按钮都不出。
 * hook 本身照常无条件调用，返回值才是可选的。
 */
export function useImportRowController(
  onImport?: (url: string) => Promise<ImportOutcome>,
  disabledReason?: string,
): ImportRowController | undefined {
  // ⚠ Map 而不是 `Record<string, …>` 对象:键现在是 URL——一个由后端/插件决定的
  // 字符串。裸对象上 `states["__proto__"]` 读到的是 Object.prototype(一个真值,
  // `?? IDLE` 兜不住,`.status` 是 undefined),`constructor` 同理。与
  // kg-type-mark.tsx / NON_KG_REFERENCE_LABELS 记下的是同一个坑,那两处用
  // `Object.hasOwn` 防,这里键是外来数据、又要频繁写,直接换 Map 更干净。
  const [states, setStates] = useState<ReadonlyMap<string, ImportRowState>>(() => new Map());

  const stateOf = useCallback(
    (rowKey: string | number): ImportRowState => states.get(String(rowKey)) ?? IDLE,
    [states],
  );

  const start = useCallback((rowKey: string | number, url: string) => {
    if (!onImport) return;
    const key = String(rowKey);
    const write = (state: ImportRowState) => {
      setStates((previous) => new Map(previous).set(key, state));
    };
    // busy 必须在**本次事件的同步段**里置位:这是「按下即有可见变化」那条红线的
    // 落点,也是双击防抖的落点(第二次点击时按钮已 disabled)。异步收尾另起,不让
    // onClick 返回一个没人 await 的 promise。
    write({ status: "busy" });
    void (async () => {
      let outcome: ImportOutcome;
      try {
        outcome = await onImport(url);
      } catch {
        // ⚠ 不读 error.message:错误人话层的唯一翻译入口在调用方(page.tsx 的
        // importGapSuggestion 已经用 toUserMessage 翻过一道),这里只兜住「调用方
        // 自己炸了」这种它翻不到的情形。
        outcome = { ok: false, message: FALLBACK_MESSAGE };
      }
      write(outcome.ok
        ? { status: "done" }
        : { status: "failed", message: outcome.message || FALLBACK_MESSAGE });
    })();
  }, [onImport]);

  if (!onImport) return undefined;
  return { stateOf, start, disabledReason };
}

/**
 * 一行的导入按钮 + 它紧邻的失败说明。两者是一个整体（片段，不额外套盒子）：失败
 * 文案必须与按钮同级相邻，调用方的 CSS 才能把它排在按钮正下方——AGENTS.md
 * 「Interactive feedback」要求动作结果落在按钮自身或紧邻处。
 *
 * 类名由调用方给：两个面的视觉语言不同（建议清单是列表行，引用卡是浮层头部），
 * 共享的是**行为**不是样式。
 */
export function ImportRowButton({
  controller,
  rowKey,
  url,
  className,
  errorClassName,
  idleLabel,
  busyLabel = "导入中…",
  doneLabel = "已导入",
}: {
  /** 缺省即整颗按钮不渲染（只读工作区）。失败文案同样不会出现——没有按钮就
   *  产生不了失败。 */
  controller?: ImportRowController;
  rowKey: string | number;
  url: string;
  className: string;
  errorClassName: string;
  idleLabel: string;
  busyLabel?: string;
  doneLabel?: string;
}) {
  const state = controller?.stateOf(rowKey) ?? IDLE;
  // 冻结态 = 进行中或已完成。两者都锁死按钮,且都压过 disabledReason 的 title
  // 提示——那句话解释的是「为什么现在不能导入」,而这两个态自己已经解释过了。
  const frozen = state.status === "busy" || state.status === "done";
  return (
    <>
      {controller && (
        <button
          type="button"
          className={`${className} ${state.status === "done" ? "is-done" : ""}`}
          disabled={frozen || Boolean(controller.disabledReason)}
          title={controller.disabledReason && !frozen ? controller.disabledReason : undefined}
          onClick={() => controller.start(rowKey, url)}
        >
          {state.status === "busy" ? busyLabel : state.status === "done" ? doneLabel : idleLabel}
        </button>
      )}
      {state.status === "failed" && <p className={errorClassName}>{state.message}</p>}
    </>
  );
}
