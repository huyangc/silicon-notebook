/**
 * knowhow-cell-conflict.tsx
 *
 * 单元格乐观校验被拒（409，分享编辑 P1 决策 D7；规格 §5、§9「Knowhow 冲突」）时，在格子
 * 这一处就地给出的双版本对比：「对方的版本 / 我的版本」+「用我的覆盖」「放弃我的」。
 * 格子编辑器与「优化整行」浮层共用；合并共享格在界面上就是一格，也只在这一处出现。
 *
 * 两个版本按 Markdown 原文并排显示（不渲染）：服务端是逐字节比较，差在空白或标记上时，
 * 渲染之后两边看起来一样，用户无从判断该留哪个。
 *
 * group_changed（这一组被重新分组：成员、锁定值或分组列变了）不给覆盖按钮——旧的写目标
 * 已经不成立，覆盖会写错行；只给句中的「刷新」。
 *
 * 交互反馈（AGENTS.md「Interactive feedback」）：按下态由全站元素级 `button:active` 给；
 * 进行中按钮禁用并换成进行中的字；结果（已覆盖 / 已换成对方的版本 / 已刷新 / 失败原因）
 * 就写在这两颗按钮旁边，结果到点后由本组件自己的计时器交还给调用方收起（onSettled），
 * 不经页面顶部的状态条。
 */
"use client";

import { useEffect, useRef, useState } from "react";

import type { KnowhowCellConflictReason } from "./knowhow-model.ts";
import { extractErrorMessage } from "./knowhow-import-logic.ts";

export const CELL_CONFLICT_CONTENT_TEXT = "这一格在你编辑期间被其他人改过，请选择保留哪个版本。";
export const CELL_CONFLICT_THEIRS_LABEL = "对方的版本";
export const CELL_CONFLICT_MINE_LABEL = "我的版本";
export const CELL_CONFLICT_OVERWRITE_LABEL = "用我的覆盖";
export const CELL_CONFLICT_DISCARD_LABEL = "放弃我的";
export const CELL_CONFLICT_REFRESH_LABEL = "刷新";
/** 冲突提示挂着时编辑器两颗保存按钮的置灰原因。 */
export const SAVE_BLOCKED_CONFLICT_HINT = "先在上方选择保留哪个版本";
const CELL_CONFLICT_OVERWRITING_LABEL = "覆盖中…";
const CELL_CONFLICT_REFRESHING_LABEL = "刷新中…";
const CELL_CONFLICT_EMPTY_TEXT = "（空）";
const CELL_CONFLICT_OVERWRITE_FAILED = "覆盖没有成功，请重试";
const CELL_CONFLICT_REFRESH_FAILED = "刷新没有成功，请重试";
const CELL_CONFLICT_OUTCOME_TEXT = {
  overwritten: "已用我的版本覆盖",
  discarded: "已换成对方的版本",
  refreshed: "已刷新，可以继续编辑",
} as const;

/** 结果停留多久后交还给调用方收起。 */
export const CELL_CONFLICT_OUTCOME_MS = 1600;

export type KnowhowCellConflictOutcome = keyof typeof CELL_CONFLICT_OUTCOME_TEXT;
type Outcome = KnowhowCellConflictOutcome;

export type KnowhowCellConflictNoticeProps = {
  reason: KnowhowCellConflictReason;
  /** 冲突时服务端回传的这一格当前存储值（合并共享格取这一行；group_changed 时不用）。 */
  theirsMd: string;
  /** 用户这一版（编辑器里的草稿 /「优化整行」的建议）。 */
  mineMd: string;
  /** 以冲突时的当前值为基线重发。resolve(true)=写成；resolve(false)=又撞上一次冲突，调用方
   *  已换上新的冲突（会以新 key 重挂本组件）；reject=其他失败，原因写在按钮旁。 */
  onOverwrite: () => Promise<boolean>;
  /** 接受对方的版本（同步执行）。 */
  onDiscard: () => void;
  /** 重新拉取整张表；resolve(false) 表示没拉成。 */
  onRefresh: () => Promise<boolean>;
  /** 结果展示到点：调用方收起本提示。 */
  onSettled: (outcome: Outcome) => void;
  /** 调用方此刻不允许覆盖（例如图片还在上传），按钮置灰并给出原因。 */
  overwriteBlockedReason?: string;
};

export function KnowhowCellConflictNotice({
  reason,
  theirsMd,
  mineMd,
  onOverwrite,
  onDiscard,
  onRefresh,
  onSettled,
  overwriteBlockedReason,
}: KnowhowCellConflictNoticeProps) {
  const [pending, setPending] = useState<"overwrite" | "refresh" | null>(null);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const settledRef = useRef(onSettled);
  useEffect(() => {
    settledRef.current = onSettled;
  }, [onSettled]);
  useEffect(() => {
    if (!outcome) return;
    const timer = window.setTimeout(() => settledRef.current(outcome), CELL_CONFLICT_OUTCOME_MS);
    return () => window.clearTimeout(timer);
  }, [outcome]);

  const locked = pending !== null || outcome !== null;

  async function handleOverwrite() {
    if (locked || overwriteBlockedReason) return;
    setPending("overwrite");
    setFailure(null);
    try {
      const written = await onOverwrite();
      if (!mountedRef.current) return;
      setPending(null);
      if (written) setOutcome("overwritten");
    } catch (err) {
      if (!mountedRef.current) return;
      setPending(null);
      setFailure(extractErrorMessage(err, CELL_CONFLICT_OVERWRITE_FAILED));
    }
  }

  function handleDiscard() {
    if (locked) return;
    setFailure(null);
    onDiscard();
    setOutcome("discarded");
  }

  async function handleRefresh() {
    if (locked) return;
    setPending("refresh");
    setFailure(null);
    const refreshed = await onRefresh().catch(() => false);
    if (!mountedRef.current) return;
    setPending(null);
    if (refreshed) setOutcome("refreshed");
    else setFailure(CELL_CONFLICT_REFRESH_FAILED);
  }

  const status = outcome ? (
    <span className="kh-cell-conflict-outcome" role="status">{CELL_CONFLICT_OUTCOME_TEXT[outcome]}</span>
  ) : failure ? (
    <span className="kh-inline-error" role="alert">{failure}</span>
  ) : null;

  if (reason === "group_changed") {
    return (
      <div className="kh-cell-conflict" role="group" aria-label="保存冲突">
        <p className="kh-cell-conflict-text">
          这一组已被重新分组，请
          <button
            type="button"
            className="link-button inline-action"
            onClick={handleRefresh}
            disabled={locked}
          >
            {pending === "refresh" ? CELL_CONFLICT_REFRESHING_LABEL : CELL_CONFLICT_REFRESH_LABEL}
          </button>
          后再编辑
        </p>
        {status && <div className="kh-cell-conflict-actions">{status}</div>}
      </div>
    );
  }

  return (
    <div className="kh-cell-conflict" role="group" aria-label="保存冲突">
      <p className="kh-cell-conflict-text">{CELL_CONFLICT_CONTENT_TEXT}</p>
      <div className="kh-cell-conflict-versions">
        <section className="kh-optimize-pane">
          <h5>{CELL_CONFLICT_THEIRS_LABEL}</h5>
          <pre className="kh-cell-conflict-md">{theirsMd || CELL_CONFLICT_EMPTY_TEXT}</pre>
        </section>
        <section className="kh-optimize-pane kh-optimize-pane--suggestion">
          <h5>{CELL_CONFLICT_MINE_LABEL}</h5>
          <pre className="kh-cell-conflict-md">{mineMd || CELL_CONFLICT_EMPTY_TEXT}</pre>
        </section>
      </div>
      <div className="kh-cell-conflict-actions">
        {status}
        <div className="kh-footer-actions">
          <button type="button" onClick={handleDiscard} disabled={locked}>
            {CELL_CONFLICT_DISCARD_LABEL}
          </button>
          <button
            type="button"
            className="kh-primary-button"
            onClick={handleOverwrite}
            disabled={locked || Boolean(overwriteBlockedReason)}
            title={overwriteBlockedReason}
          >
            {pending === "overwrite" ? CELL_CONFLICT_OVERWRITING_LABEL : CELL_CONFLICT_OVERWRITE_LABEL}
          </button>
        </div>
      </div>
    </div>
  );
}
