/**
 * linked-report-picker.tsx
 *
 * 问答输入框的「关联报告」：选择器（输入框下排，与「检索范围」计数同一行）、标签（输入框
 * 上方）与在途轮次上的报告名。三者都是呈现组件——标签挂在哪份报告、判词、就地说明都归
 * use-ask-session.ts 所有，这里只读视图、回调具名命令。
 *
 * 选择器只列这个笔记本里**自己的、已完成的**报告：报告清单接口本来就只返回本人创建的
 * （在 SQL 里按创建者收窄），这里再只留 `status === "done"`。清单整份返回、界面分页
 * （与深度报告清单同一套控件与每页条数，见 product-and-api「列表分页」）；弹出层每次打开
 * 都重新挂载，所以从第一页开始、读的也是此刻的清单。
 *
 * 弹出层照 effort-picker.tsx 的写法：控件内的 `position: absolute` popover，打开时量一次夹回
 * 可视容器内，点外部 / Esc / 控件转入禁用都会收起。不是根弹窗——它不抢焦点、不遮挡页面，也不需要协调器的租约。
 */
"use client";

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Check, FileText, X } from "lucide-react";

import { clippingBounds } from "./effort-picker";
import { clampPopoverLeft } from "./effort-picker-logic";
import {
  linkedReportLabel,
  type LinkedReportTag,
  type LinkedReportVerdict,
} from "./linked-report.ts";
import { Pagination } from "./Pagination";
import { formatRelativeTime } from "./relative-time.ts";
import { listReports } from "./report-api.ts";
import type { ReportSummaryT } from "./report-model.ts";
import { useClientPagination } from "./use-client-pagination.ts";


/** popover 与容器内缘之间保留的留白（与 effort-picker 同值）。 */
const POPOVER_MARGIN = 8;

/** 选择器每页显示的报告数：与深度报告清单（REPORT_LIST_PAGE_SIZE）同值。 */
export const LINKED_REPORT_PICKER_PAGE_SIZE = 20;

const NO_REPORTS: readonly ReportSummaryT[] = [];


/**
 * 弹出层里的报告清单。只在弹出层打开时挂载：每次打开都重新读一遍、从第一页开始。
 * 读取在途时换了笔记本（`key` 随笔记本变）或收起，迟到的结果按 `current` 丢弃。
 */
function LinkedReportOptions({
  notebookId,
  linkedId,
  onPick,
}: {
  notebookId: string;
  linkedId: string | null;
  onPick: (report: LinkedReportTag) => void;
}) {
  // 「重试」递增它：同一个笔记本再读一次。结果按这把钥匙认领，旧请求的结果不会盖住新的。
  const [attempt, setAttempt] = useState(0);
  const requestKey = JSON.stringify([notebookId, attempt]);
  const [settled, setSettled] = useState<{
    key: string;
    reports: readonly ReportSummaryT[] | null;
  } | null>(null);

  useEffect(() => {
    let current = true;
    listReports(notebookId).then(
      (reports) => {
        if (current) setSettled({ key: requestKey, reports });
      },
      () => {
        // 失败只在弹出层里就地说一句，不上页面横幅：选择器是可选动作，读不到清单不影响提问。
        if (current) setSettled({ key: requestKey, reports: null });
      },
    );
    return () => {
      current = false;
    };
  }, [notebookId, requestKey]);

  // undefined = 这把钥匙的结果还没回来；null = 读取失败。
  const reports = settled && settled.key === requestKey ? settled.reports : undefined;
  const done = useMemo(
    () => (reports ? reports.filter((report) => report.status === "done") : NO_REPORTS),
    [reports],
  );
  const page = useClientPagination(done, LINKED_REPORT_PICKER_PAGE_SIZE);

  if (reports === undefined) {
    return <p className="linked-report-picker-state" role="status">正在读取报告…</p>;
  }
  if (reports === null) {
    return (
      <div className="linked-report-picker-state failed" role="status">
        <span>暂时无法读取报告列表，请重试</span>
        <button type="button" className="linked-report-picker-retry" onClick={() => setAttempt((value) => value + 1)}>
          重试
        </button>
      </div>
    );
  }
  if (done.length === 0) {
    return <p className="linked-report-picker-state">这个笔记本里还没有你生成完成的报告</p>;
  }
  return (
    <>
      <ul className="linked-report-picker-list" aria-label="已完成的报告">
        {page.pageItems.map((report) => {
          const selected = report.id === linkedId;
          return (
            <li key={report.id}>
              <button
                type="button"
                className={selected ? "linked-report-option selected" : "linked-report-option"}
                aria-pressed={selected}
                title={report.question}
                onClick={() => onPick({ id: report.id, title: report.question })}
              >
                <span className="linked-report-option-title">{report.question}</span>
                <small className="linked-report-option-meta">
                  {selected && <><Check size={12} aria-hidden="true" /> 已选 · </>}
                  {formatRelativeTime(report.updated_at || report.created_at)}
                  {report.section_count > 0 && ` · ${report.section_count} 节`}
                </small>
              </button>
            </li>
          );
        })}
      </ul>
      <Pagination
        page={page.page}
        pageSize={LINKED_REPORT_PICKER_PAGE_SIZE}
        total={page.total}
        onPage={page.setPage}
        label="关联报告分页"
        compact
      />
    </>
  );
}


/**
 * 「关联报告」选择器：一颗按钮 + 向上弹出的报告清单。选中即挂标签并收起；当前已挂的那份
 * 标为已选。`disabled`（提交中、问题理解中、审阅卡在场、会话加载中）时按钮禁用，已打开的
 * 弹出层随之收起。
 */
export function LinkedReportPicker({
  notebookId,
  linked,
  disabled,
  onSelect,
}: {
  notebookId: string;
  linked: LinkedReportTag | null;
  disabled: boolean;
  onSelect: (report: LinkedReportTag) => void;
}) {
  // 记下是为哪个笔记本打开的：换了笔记本，旧的弹出层（连同它的清单）当场作废。
  const [openFor, setOpenFor] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const popoverRef = useRef<HTMLDivElement | null>(null);
  // null = 用 CSS 的默认位(右对齐控件);量出越界才写具体 left。
  const [popoverLeft, setPopoverLeft] = useState<number | null>(null);
  if (openFor !== null && (disabled || openFor !== notebookId)) setOpenFor(null);
  const open = openFor !== null && openFor === notebookId && !disabled;

  // 打开时把弹出层夹回可视容器内(effort-picker 同一算法):选择器在输入行里的落位随宽度
  // 与「检索范围」那句话的长短变化,窄屏折行后任何一个静态锚点都会被
  // `.workspace-panel { overflow: hidden }` 裁掉一边。
  useLayoutEffect(() => {
    if (!open) {
      setPopoverLeft(null);
      return;
    }
    const sync = () => {
      const root = rootRef.current;
      const popover = popoverRef.current;
      if (!root || !popover) return;
      const anchor = root.getBoundingClientRect();
      const box = popover.getBoundingClientRect();
      // 尚未布局(如 jsdom 默认全零)时不写入凭空算出的位置——保持 CSS 默认位。
      if (box.width <= 0) return;
      const bounds = clippingBounds(root);
      setPopoverLeft(clampPopoverLeft({
        anchorLeft: anchor.left,
        anchorRight: anchor.right,
        popoverWidth: box.width,
        containerLeft: bounds.left,
        containerRight: bounds.right,
        margin: POPOVER_MARGIN,
      }));
    };
    sync();
    window.addEventListener("resize", sync);
    return () => window.removeEventListener("resize", sync);
  }, [open]);

  // 点外部 / Esc 收起（与 effort-picker 同一写法）。
  useEffect(() => {
    if (!open) return;
    function handlePointerDown(event: PointerEvent) {
      const target = event.target;
      if (target instanceof Node && rootRef.current?.contains(target)) return;
      setOpenFor(null);
    }
    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") setOpenFor(null);
    }
    window.addEventListener("pointerdown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("pointerdown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  return (
    <div className="linked-report-picker" ref={rootRef}>
      <button
        type="button"
        className={open ? "linked-report-picker-chip open" : "linked-report-picker-chip"}
        disabled={disabled}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpenFor(open ? null : notebookId)}
      >
        <FileText size={13} aria-hidden="true" />
        <span className="linked-report-picker-chip-label">关联报告</span>
      </button>
      {open && (
        <div
          className="linked-report-picker-popover"
          role="dialog"
          aria-label="选择关联报告"
          ref={popoverRef}
          style={popoverLeft === null ? undefined : { left: popoverLeft, right: "auto" }}
        >
          <div className="linked-report-picker-head">
            <span className="linked-report-picker-title">关联报告</span>
            <small className="linked-report-picker-hint">只列出你在这个笔记本里生成完成的报告</small>
          </div>
          <LinkedReportOptions
            key={notebookId}
            notebookId={notebookId}
            linkedId={linked?.id ?? null}
            onPick={(report) => {
              setOpenFor(null);
              onSelect(report);
            }}
          />
        </div>
      )}
    </div>
  );
}


/**
 * 输入框上方的「关联报告：<题目>」标签，连同标签旁的就地说明。
 *
 * - 判词说这一轮不会读这份报告时，标签整块置灰，原因紧挨着它说（`role="status"`）。
 * - `note`（范围怎么切的、报告已不存在而被移除）显示在同一行；它的清除计时归
 *   use-ask-session（`LINKED_REPORT_NOTE_MS`），这里只照视图显示。标签已被移除、只剩说明
 *   时也要显示——那句话说的正是它为什么不见了。
 * - 题目整串放进 `title`，标签本身只做单行省略（不截断数据）。
 */
export function LinkedReportChip({
  report,
  verdict,
  note,
  onRemove,
  removeDisabled = false,
}: {
  report: LinkedReportTag | null;
  verdict: LinkedReportVerdict | null;
  note: string;
  onRemove: () => void;
  removeDisabled?: boolean;
}) {
  if (!report && !note) return null;
  const unreadable = Boolean(report && verdict && !verdict.readable);
  const label = report ? linkedReportLabel(report.title) : "";
  return (
    <div className="linked-report-bar">
      {report && (
        <span className={unreadable ? "linked-report-chip unreadable" : "linked-report-chip"}>
          <FileText size={13} aria-hidden="true" />
          <span className="linked-report-chip-text" title={label}>{label}</span>
          <button
            type="button"
            className="linked-report-chip-remove"
            aria-label="移除关联报告"
            title="移除关联报告"
            disabled={removeDisabled}
            onClick={onRemove}
          >
            <X size={13} aria-hidden="true" />
          </button>
        </span>
      )}
      {unreadable && (
        <span className="linked-report-reason" role="status">
          {verdict?.reason || "本轮不读取这份报告"}
        </span>
      )}
      {note && <span className="linked-report-note" role="status">{note}</span>}
    </div>
  );
}


/** 在途轮次（理解、审阅、作答中）问题下方那枚小标签：这一问提交时挂的报告。 */
export function LinkedReportTurnTag({ report }: { report: LinkedReportTag }) {
  const label = linkedReportLabel(report.title);
  return (
    <div className="chat-turn-linked-report">
      <span className="chat-turn-linked-report-tag" title={label}>
        <FileText size={12} aria-hidden="true" />
        <span className="chat-turn-linked-report-text">{label}</span>
      </span>
    </div>
  );
}
