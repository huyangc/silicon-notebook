/**
 * linked-report-picker.tsx
 *
 * 问答的「关联报告」：来源栏里的「关联报告」分组（选择器 + 当前标签）与在途轮次上的报告名。
 * 都是呈现组件——标签挂在哪份报告、判词、就地说明都归 use-ask-session.ts 所有，这里只读视图、
 * 回调具名命令。
 *
 * 分组住在来源栏、不住在输入框：挂哪份报告回答「这一问读什么」，与来源、参考库同属一类；
 * 输入框下排只放「怎么答」（回答方式、检索档位）。两类混在一行，分区就没了。
 *
 * 选择器只列这个笔记本里**自己的、已完成的**报告：报告清单接口本来就只返回本人创建的
 * （在 SQL 里按创建者收窄），这里再只留 `status === "done"`。清单整份返回、界面分页
 * （与深度报告清单同一套控件与每页条数，见 product-and-api「列表分页」）；清单每次展开
 * 都重新挂载，所以从第一页开始、读的也是此刻的清单。
 *
 * 清单就地展开在分组里（占文档流，把下面的来源列表往下推），不是浮层：来源栏窄、
 * `.workspace-panel { overflow: hidden }`，浮层朝哪个方向都可能被裁掉一截，而来源列表本来
 * 就是让位的那个。点外部 / Esc / 控件转入禁用都会收起。不是根弹窗——它不抢焦点、
 * 也不需要协调器的租约。
 */
"use client";

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Check, FileText, X } from "lucide-react";

import {
  linkedReportLabel,
  linkedReportTagText,
  type LinkedReportTag,
  type LinkedReportVerdict,
} from "./linked-report.ts";
import { Pagination } from "./Pagination";
import { formatRelativeTime } from "./relative-time.ts";
import { listReports } from "./report-api.ts";
import type { ReportSummaryT } from "./report-model.ts";
import { useClientPagination } from "./use-client-pagination.ts";


/** 选择器每页显示的报告数：与深度报告清单（REPORT_LIST_PAGE_SIZE）同值。 */
export const LINKED_REPORT_PICKER_PAGE_SIZE = 20;

const NO_REPORTS: readonly ReportSummaryT[] = [];


/**
 * 展开区里的报告清单。只在展开时挂载：每次展开都重新读一遍、从第一页开始。
 * 读取在途时换了笔记本（`key` 随笔记本变）或收起，迟到的结果按 `current` 丢弃。
 */
function LinkedReportOptions({
  notebookId,
  linkedId,
  onPick,
  onRetry,
}: {
  notebookId: string;
  linkedId: string | null;
  onPick: (report: LinkedReportTag) => void;
  /** 「重试」按下的那一刻:重试键随即被「正在读取」替换,调用方把焦点留在展开区里。 */
  onRetry: () => void;
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
        // 失败只在展开区里就地说一句，不上页面横幅：选择器是可选动作，读不到清单不影响提问。
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
        <button
          type="button"
          className="linked-report-picker-retry"
          onClick={() => {
            onRetry();
            setAttempt((value) => value + 1);
          }}
        >
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
 * 来源栏里的「关联报告」分组：标题行（「关联报告」+「选择 / 更换」）、`children`（当前挂着的
 * 标签与它旁边的就地说明，问答里是 LinkedReportChip），以及按下「选择」后就地展开的报告清单。
 * 选中即挂标签并收起；当前已挂的那份标为已选。`disabled`（提交中、问题理解中、审阅卡在场、
 * 会话加载中）时按钮禁用，已展开的清单随之收起。选中后清单卸载，`onSelect` 的调用方负责把
 * 焦点送到下一处（问答里是输入框）。
 */
export function LinkedReportPicker({
  notebookId,
  linked,
  disabled,
  onSelect,
  children = null,
}: {
  notebookId: string;
  linked: LinkedReportTag | null;
  disabled: boolean;
  onSelect: (report: LinkedReportTag) => void;
  children?: ReactNode;
}) {
  // 记下是为哪个笔记本展开的：换了笔记本，旧的清单当场作废。
  const [openFor, setOpenFor] = useState<string | null>(null);
  const rootRef = useRef<HTMLElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  if (openFor !== null && (disabled || openFor !== notebookId)) setOpenFor(null);
  const open = openFor !== null && openFor === notebookId && !disabled;
  const action = linked ? "更换" : "选择";

  // 点外部 / Esc 收起（与 effort-picker 同一写法）。焦点规则:持焦点的节点随清单卸载时,
  // 焦点不能掉到 body——在分组里按 Esc 收起,焦点回到触发按钮;点外部收起不动焦点(用户
  // 已经点在别处);选中报告后焦点去哪由调用方决定(问答里是送进输入框,见 page.tsx)。
  useEffect(() => {
    if (!open) return;
    function handlePointerDown(event: PointerEvent) {
      const target = event.target;
      if (target instanceof Node && rootRef.current?.contains(target)) return;
      setOpenFor(null);
    }
    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key !== "Escape") return;
      const focusInside = document.activeElement instanceof Node
        && Boolean(rootRef.current?.contains(document.activeElement));
      setOpenFor(null);
      if (focusInside) triggerRef.current?.focus();
    }
    window.addEventListener("pointerdown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("pointerdown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  return (
    <section className="scope-group linked-report-picker" aria-label="关联报告" ref={rootRef}>
      <div className="linked-report-picker-head">
        <h3 className="scope-group-title">关联报告</h3>
        <button
          ref={triggerRef}
          type="button"
          className={open ? "linked-report-picker-toggle open" : "linked-report-picker-toggle"}
          disabled={disabled}
          aria-label={`${action}关联报告`}
          aria-expanded={open}
          onClick={() => setOpenFor(open ? null : notebookId)}
        >
          {action}
        </button>
      </div>
      {children}
      {open && (
        <div
          className="linked-report-picker-panel"
          role="region"
          aria-label="可关联的报告"
          // 可编程聚焦(不进 Tab 序):「重试」键被替换时焦点停在这里,而不是掉到 body。
          tabIndex={-1}
          ref={listRef}
        >
          <small className="linked-report-picker-hint">只列出你在这个笔记本里生成完成的报告</small>
          <LinkedReportOptions
            key={notebookId}
            notebookId={notebookId}
            linkedId={linked?.id ?? null}
            onPick={(report) => {
              setOpenFor(null);
              onSelect(report);
            }}
            onRetry={() => listRef.current?.focus()}
          />
        </div>
      )}
    </section>
  );
}


/**
 * 「关联报告」分组里的当前标签（「<题目>」+ 移除键），连同标签下的就地说明。
 *
 * - 判词说这一轮不会读这份报告时，标签整块置灰，原因紧挨着它说（`role="status"`）。
 * - `note`（范围怎么切的、报告已不存在而被移除）紧挨着标签显示；它的清除计时归
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
  const label = report ? linkedReportTagText(report.title) : "";
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
