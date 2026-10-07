"use client";

/**
 * info-modal-actions.tsx
 *
 * 通用提示弹窗(root modal slot `info`,例如「分析」面板)的动作按钮区。从 page.tsx
 * 抽出来,便于单独做组件测试(page.tsx 整体不可直接渲染)。
 *
 * 两种动作:
 * - `action`:原有形态——先关弹窗,再执行(结果由调用方自己呈现,例如打开另一个弹窗)。
 * - `run`:会被服务端拒绝的写动作(例如「设为公共知识库」)。按下后按钮置灰,弹窗
 *   保持打开;成功才关弹窗,失败把原因显示在**这个按钮旁边**(`role="status"`),
 *   而不是只在页面顶部闪一下——按钮的结果落在按钮旁(仓库的 Interactive feedback 规则)。
 */

import { Fragment, useEffect, useRef, useState } from "react";

import { toUserMessage } from "./errors";

export type InfoModalAction = {
  label: string;
  desc?: string;
  // 按钮旁的上下文注记(如「当前基准库:名字」),渲染为描述下方的小徽标
  note?: string;
  primary?: boolean;
  danger?: boolean;
  action?: () => void;
  run?: () => Promise<void>;
};

export function InfoModalActions({
  actions,
  requestClose,
  reportDetachedError,
}: {
  actions: readonly InfoModalAction[];
  /** 按「按钮」关闭弹窗;协调器拒绝时返回 false。 */
  requestClose: () => boolean;
  /**
   * 弹窗在 `run` 进行中被关掉(关闭入口不能被禁用)后才到达的失败:按钮已经不在了,
   * 原因交给页面级的错误呈现(顶栏),不能静默丢掉。
   */
  reportDetachedError: (error: unknown) => void;
}) {
  const [pending, setPending] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, string>>({});
  const mounted = useRef(true);
  const buttons = useRef<Record<string, HTMLButtonElement | null>>({});
  // 被拒后把焦点还给那个按钮:它在等待期间是 disabled,浏览器会把焦点挪到 body。
  const [refocus, setRefocus] = useState<string | null>(null);
  useEffect(() => {
    if (refocus === null || pending !== null) return;
    buttons.current[refocus]?.focus();
    setRefocus(null);
  }, [refocus, pending]);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  async function press(action: InfoModalAction) {
    if (!action.run) {
      if (requestClose()) action.action?.();
      return;
    }
    if (pending) return;
    setPending(action.label);
    setResults((previous) => {
      if (!(action.label in previous)) return previous;
      const next = { ...previous };
      delete next[action.label];
      return next;
    });
    try {
      await action.run();
      if (mounted.current) requestClose();
    } catch (error) {
      if (mounted.current) {
        setResults((previous) => ({ ...previous, [action.label]: toUserMessage(error) }));
        setRefocus(action.label);
      } else {
        reportDetachedError(error);
      }
    } finally {
      if (mounted.current) setPending(null);
    }
  }

  function button(action: InfoModalAction) {
    return (
      <button
        ref={(node) => { buttons.current[action.label] = node; }}
        className={action.danger ? "new-pill danger-pill" : action.primary ? "new-pill" : "sort-button"}
        disabled={pending !== null}
        aria-busy={pending === action.label || undefined}
        onClick={() => { void press(action); }}
      >
        {pending === action.label ? `${action.label}…` : action.label}
      </button>
    );
  }

  function result(action: InfoModalAction) {
    const message = results[action.label];
    return message ? <span className="info-action-note" role="status">{message}</span> : null;
  }

  if (actions.some((action) => action.desc || action.note)) {
    // 带描述的动作(分析弹窗):网格布局 —— 按钮列共享最宽标签宽度做到等宽对齐,描述/注记跟随右列
    return (
      <div className="info-action-grid">
        {actions.map((action) => (
          <Fragment key={action.label}>
            {button(action)}
            <div className="info-action-desc-cell">
              {action.desc && <span className="info-action-desc">{action.desc}</span>}
              {action.note && <span className="info-action-note">{action.note}</span>}
              {result(action)}
            </div>
          </Fragment>
        ))}
      </div>
    );
  }
  return (
    <>
      {actions.map((action) => (
        <Fragment key={action.label}>
          {button(action)}
          {result(action)}
        </Fragment>
      ))}
    </>
  );
}
