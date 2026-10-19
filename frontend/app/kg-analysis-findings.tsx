/**
 * kg-analysis-findings.tsx
 *
 * 图谱分析面板的**问题清单**(2026-10-19 设计 §三/§四/§五):头部(报告新鲜度、与上一次的
 * 对比、「更新分析」按钮)与四个区里的三个——待处理、需要开发处理、已通过。第四个区
 * 「诊断数据」是原有的统计区块,留在 kg-analysis-view.tsx 里,默认折叠。
 *
 * 边界(与 kg-analysis-view.tsx 同一条):这里只呈现。所有判断(排序、分区、按钮状态、
 * 证据措辞、出不出按钮)都是 kg-analysis-model.ts 里的纯函数;处置动作一律经 page.tsx 传进来
 * 的回调走既有入口(删除确认框、重新解析、补上关联、待确认合并)——面板自己不执行任何删除
 * 或重新解析,后端是权限的唯一判据。
 *
 * 交互反馈(AGENTS.md Interactive feedback):按下有可见变化(globals.css 的 :active 基线);
 * 后台在跑的动作在飞期间禁用并就地写状态;结果写在按钮旁,按自己的计时器消失。
 */
"use client";

import { useEffect, useRef, useState, type MouseEvent } from "react";
import { ChevronLeft, ChevronRight, RefreshCw } from "lucide-react";

import { httpErrorStatus, toUserMessage } from "./errors";
import {
  fetchKgFindings,
  type KgAnalysisReport,
  type KgFindingRow,
  type KgFindingsPage,
  type KgFindingZone,
} from "./kg-analysis-api";
import {
  analysisReadiness,
  analysisUpdateState,
  devItemViews,
  findingActionPlan,
  findingBasedOnChange,
  findingEvidenceView,
  findingHandledText,
  findingImpactText,
  findingRuleLabel,
  findingSeverityLabel,
  findingSeverityTone,
  findingsComparisonView,
  findingsReportLine,
  findingSubjectKindLabel,
  findingSubjectText,
  formatCount,
  partitionFindings,
  passedRowText,
  passedRuleLines,
  relinkResultText,
  relinkSpentText,
  truncatedRuleNotes,
  type FindingButtonSpec,
} from "./kg-analysis-model";
import type { PendingMerge } from "./workspace-model";

/** 结果文字在按钮旁停留多久(与「删除知识图谱」的结果同一量级)。 */
const FINDING_RESULT_HOLD_MS = 8000;

export type KgRelinkResult = { edgesAdded: number; isolatedAfter: number };

/** 删除来源与重新解析都先经确认框;确认之后的进展由 page 回报给这一行。 */
export type KgSourceActionTarget = { sourceId: string; title: string; sourceType: string };
export type KgDeleteOutcome = "deleted" | "failed";
export type KgReparsePhase = "running" | "started" | "skipped" | "failed";

/**
 * 处置入口。全部可选:没传的那一种就不出按钮(例如组件测试只关心一部分)。
 * page.tsx 把它们接到既有入口上,面板不复制任何一套确认文案或权限判断。
 */
export type KgFindingHandlers = {
  /** 在面板上层打开来源详情(面板不关)。 */
  onOpenSource?: (sourceId: string) => Promise<void> | void;
  /** 调起既有的删除确认框;确认并跑完后回报结果。取消时什么都不回报。 */
  onDeleteSource?: (target: KgSourceActionTarget, report: (outcome: KgDeleteOutcome) => void) => void;
  /** 先弹确认框(说明会重新调用模型、替换知识条目),确认后走批量重新解析。 */
  onReparseSource?: (target: KgSourceActionTarget, report: (phase: KgReparsePhase) => void) => void;
  /** 跳到知识图谱视图「待确认合并」里的那一对。 */
  onReviewMerge?: (mergeId: string) => void;
  /** 「补上关联」(后台任务,结果由 relinkResult 回来)。 */
  onRelink?: () => void;
};

export type KgFindingsActionState = {
  /** 可编辑成员;只读成员与镜像库为 false:一个按钮都没有,也没有「标为正常」。 */
  canWrite: boolean;
  deletingSourceIds: ReadonlySet<string>;
  relinking: boolean;
  /** 维护槽被占(重新合并 / 整理 / 删除知识图谱在跑):补上关联撞同一把锁,禁用。 */
  maintenanceBusy: boolean;
  relinkResult: KgRelinkResult | null;
  pendingMerges: readonly PendingMerge[];
};

type RowResult = { tone: "ok" | "failed" | "neutral"; text: string };

/** 一段会自己消失的结果文字。计时器只清自己那一条,换新结果时撤掉旧计时器。 */
function useTimedResult(): [RowResult | null, (result: RowResult | null) => void] {
  const [result, setResult] = useState<RowResult | null>(null);
  const timerRef = useRef<number | null>(null);
  useEffect(() => () => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
  }, []);
  const show = (next: RowResult | null) => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    timerRef.current = null;
    setResult(next);
    if (next) {
      timerRef.current = window.setTimeout(() => {
        timerRef.current = null;
        setResult((current) => (current === next ? null : current));
      }, FINDING_RESULT_HOLD_MS);
    }
  };
  return [result, show];
}

/** 组件卸载后不再回写 state(确认框可能在面板关掉之后才确认)。 */
function useMountedRef() {
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  return mounted;
}

// ------------------------------------------------------------------ 取数

export type FindingsPageState = {
  page: KgFindingsPage | null;
  loading: boolean;
  /** 取数失败时给用户看的那句话(已经过 toUserMessage);没有失败时为空串。 */
  failure: string;
  /**
   * `page` 是哪一代取回来的(库 + 失效令牌)。令牌变了、新一页还没回来时,屏上仍是上一代的
   * 那一页,这里也仍是上一代——跨页记住的东西按它对齐,不会拿旧页的内容冒充新一代。
   */
  generation: string;
};

/**
 * 问题清单的一页。`token` 是失效令牌:处置完成(来源删了、重新解析排上了、补上关联跑完、
 * 合并有了决定)或标为正常之后它会变,这一页就按当前偏移重取——「已处理」是读侧派生的,
 * 不靠前端记住「我刚点过」。
 *
 * 换库时在渲染期当场清空(与 KgAnalysisView 同一个理由:effect 要到提交之后才跑,中间那一帧
 * 会拿上一个库的问题渲染出一份完整清单)。
 */
export function useFindingsPage(
  notebookId: string,
  zone: KgFindingZone,
  limit: number,
  offset: number,
  token: string,
): FindingsPageState {
  const [page, setPage] = useState<KgFindingsPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [failure, setFailure] = useState("");
  const [generation, setGeneration] = useState("");
  const [shownNotebook, setShownNotebook] = useState(notebookId);
  if (shownNotebook !== notebookId) {
    setShownNotebook(notebookId);
    setPage(null);
    setGeneration("");
    setLoading(true);
    setFailure("");
  }
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setFailure("");
    fetchKgFindings(notebookId, { zone, limit, offset })
      .then((data) => {
        if (cancelled) return;
        setPage(data);
        setGeneration(`${notebookId}|${token}`);
      })
      .catch((caught: unknown) => {
        if (cancelled) return;
        setPage(null);
        setGeneration("");
        setFailure(toUserMessage(caught, "问题清单暂时打不开，请稍后重试"));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [notebookId, zone, limit, offset, token]);
  return { page, loading, failure, generation };
}

/** 「补上关联已经跑过」那一行在 `passed` 列表里的位置(它只有一条)。 */
export type RelinkSpentSighting = { row: KgFindingRow; offset: number };

/**
 * 从 `passed` 那一页里认出 relink_spent 那一行(§4.5),交给诊断数据那一块。它按严重度、影响面
 * 排序,通常在第一页;翻到不含它的页时,沿用**同一代**里看到过的那一行——令牌一变(新内容、
 * 更新了分析)就不再沿用,等新一页自己说它还在不在。
 */
export function useRelinkSpentRow(passed: FindingsPageState): RelinkSpentSighting | null {
  const [seen, setSeen] = useState<{ generation: string; sighting: RelinkSpentSighting } | null>(null);
  const page = passed.page?.present ? passed.page : null;
  const onPage = page ? partitionFindings(page.rows).spent[0] ?? null : null;
  if (page && onPage && (
    seen === null
    || seen.generation !== passed.generation
    || seen.sighting.row !== onPage
    || seen.sighting.offset !== page.offset
  )) {
    setSeen({ generation: passed.generation, sighting: { row: onPage, offset: page.offset } });
  }
  if (page && onPage) return { row: onPage, offset: page.offset };
  return page && seen && seen.generation === passed.generation ? seen.sighting : null;
}

function FindingsPager({
  page,
  loading,
  onOffset,
  label,
  elsewhere = null,
}: {
  page: KgFindingsPage;
  loading: boolean;
  onOffset: (next: number) => void;
  label: string;
  /** 这一列表里另放别处、不在这里列出也不计数的那一行(已通过区的 relink_spent)。 */
  elsewhere?: RelinkSpentSighting | null;
}) {
  if (page.offset <= 0 && !page.has_more) return null;
  const before = elsewhere && elsewhere.offset < page.offset ? 1 : 0;
  const here = elsewhere && elsewhere.offset === page.offset ? 1 : 0;
  const first = page.offset + 1 - before;
  const last = page.offset + page.returned - before - here;
  const total = page.total - (elsewhere ? 1 : 0);
  return (
    <div className="kg-analysis-pager" aria-label={label}>
      <span className="kg-analysis-pager-text">
        {last >= first ? `第 ${formatCount(first)}–${formatCount(last)} 条，` : ""}
        共 {formatCount(total)} 条
      </span>
      <button
        type="button"
        className="sort-button"
        disabled={page.offset <= 0 || loading}
        onClick={() => onOffset(Math.max(0, page.offset - page.limit))}
      >
        <ChevronLeft size={15} /> 上一页
      </button>
      <button
        type="button"
        className="sort-button"
        disabled={!page.has_more || loading}
        onClick={() => onOffset(page.offset + page.limit)}
      >
        下一页 <ChevronRight size={15} />
      </button>
    </div>
  );
}

/** 问题清单这一页本身不在场(还没生成 / 升级后还没生成)时的说明。 */
function FindingsAbsence({ absence, canWrite }: { absence: string | null; canWrite: boolean }) {
  // 只读成员没有「生成 / 更新分析」按钮:不叫他去点一颗他看不见的按钮。
  const next = canWrite
    ? "点上方「更新分析」生成。"
    : "拥有编辑权限的成员更新一次分析后就会生成。";
  return (
    <p className="kg-analysis-absent">
      {absence === "upgrade_pending"
        ? `这是升级后新增的问题清单，这个知识库还没生成过。${next}`
        : `这个知识库还没生成过问题清单。${next}`}
      <span className="kg-analysis-absent-note">这里没有列出问题，不代表没有问题。</span>
    </p>
  );
}

// ------------------------------------------------------------------ 头部

/**
 * 头部:报告新鲜度一行 + 与上一次的对比一行 + 「更新分析」按钮(§五 S4 的状态表)。按钮置灰
 * 时旁边写原因——按钮只在按下去不会有任何变化时置灰。
 */
export function FindingsHeader({
  report,
  canAnalyze,
  analysisRunning,
  analysisBlocked,
  onAnalyze,
}: {
  report: KgAnalysisReport;
  canAnalyze: boolean;
  analysisRunning: boolean;
  analysisBlocked: boolean;
  onAnalyze?: () => void;
}) {
  const summary = report.findings_summary;
  const readiness = analysisReadiness(report);
  const update = analysisUpdateState(report);
  const comparison = findingsComparisonView(summary.present ? summary.comparison : null);
  const reportLine = findingsReportLine(summary);
  const reason = analysisRunning
    ? ""
    : analysisBlocked
      ? "其它图谱任务进行中，完成后即可操作"
      : update.reason;
  return (
    <div className={`kg-analysis-block kg-findings-head kg-findings-head--${readiness.tone}`}>
      <div className="kg-findings-head-row">
        <div className="kg-findings-head-text">
          <h3>报告状态</h3>
          {reportLine ? <p className="kg-findings-report-line">{reportLine}</p> : null}
          {comparison ? (
            <p className={`kg-findings-compare${comparison.comparable ? "" : " is-muted"}`}>
              {comparison.text}
            </p>
          ) : null}
          {comparison && comparison.resolvedNames.length > 0 ? (
            <p className="kg-findings-resolved">
              已消失：{comparison.resolvedNames.join("、")}
            </p>
          ) : null}
          {readiness.tone !== "ok" ? (
            <p className={`kg-findings-readiness is-${readiness.tone}`}>
              <i className={`kg-analysis-guide-dot is-${readiness.tone}`} aria-hidden="true" />
              <strong>{readiness.title}</strong>
              <span>{readiness.text}</span>
            </p>
          ) : null}
        </div>
        {canAnalyze && onAnalyze ? (
          <div className="kg-findings-update">
            <button
              type="button"
              className="sort-button kg-analysis-run-button"
              disabled={analysisRunning || analysisBlocked || !update.enabled}
              onClick={onAnalyze}
              title="重算跨文档合并、主题板块、质量统计和问题清单；不会重新分析来源"
            >
              <RefreshCw size={15} className={analysisRunning ? "busy-spin" : undefined} />
              {analysisRunning ? "正在生成…" : update.label}
            </button>
            {reason ? <p className="kg-findings-update-reason" role="status">{reason}</p> : null}
          </div>
        ) : null}
      </div>
      <p className="kg-analysis-action-note">
        生成或更新分析会在后台重算跨文档合并、主题板块、质量统计和问题清单，不会重新分析来源。
        {analysisBlocked || analysisRunning ? "" : " 完成后本页会自动刷新。"}
        {!canAnalyze ? " 只读成员可以查看问题与证据，拥有编辑权限的成员可以处理。" : ""}
      </p>
    </div>
  );
}

// ------------------------------------------------------------------ 一条问题

function buttonLabel(spec: FindingButtonSpec, busy: string | null, deleting: boolean, relinking: boolean): string {
  switch (spec.kind) {
    case "open_source":
      return busy === `open:${spec.sourceId}` ? "打开中…" : "打开来源核对";
    case "delete_source":
      return deleting ? "删除中…" : "删除来源…";
    case "reparse_source":
      return busy === `reparse:${spec.sourceId}` ? "重新解析中…" : "重新解析…";
    case "review_merge":
      return "去待确认合并";
    case "relink":
      return relinking ? "补连中…" : "补上关联";
  }
}

/** 同一条问题里出现两份来源(F1 两份重复的错文件 / F2)时,按钮上带上是哪一份。 */
function buttonAria(spec: FindingButtonSpec, text: string, multiSource: boolean): string | undefined {
  if (!multiSource) return undefined;
  if (spec.kind === "open_source" || spec.kind === "delete_source" || spec.kind === "reparse_source") {
    return `${text.replace(/…$/, "")}：${spec.title}`;
  }
  return undefined;
}

function FindingRowView({
  row,
  units,
  basedOn,
  state,
  handlers,
  onDismiss,
  onChanged,
}: {
  row: KgFindingRow;
  units: Record<string, string> | null;
  /** 「基于变更 #N」:整份清单建于哪一代,报告落后时每条都标;一致时为空串。 */
  basedOn: string;
  state: KgFindingsActionState;
  handlers: KgFindingHandlers;
  onDismiss: (row: KgFindingRow) => Promise<void>;
  onChanged: () => void;
}) {
  const mounted = useMountedRef();
  const [busy, setBusy] = useState<string | null>(null);
  const [result, showResult] = useTimedResult();
  const evidence = findingEvidenceView(row);
  const plan = findingActionPlan(row, { canWrite: state.canWrite, pendingMerges: state.pendingMerges });
  const tone = findingSeverityTone(row.severity);
  const handledText = findingHandledText(row);
  const sourceIds = new Set(
    plan.buttons.flatMap((spec) => ("sourceId" in spec ? [spec.sourceId] : [])),
  );
  const multiSource = sourceIds.size > 1;
  const visible = plan.buttons.filter((spec) => {
    if (spec.kind === "open_source") return Boolean(handlers.onOpenSource);
    if (spec.kind === "delete_source") return Boolean(handlers.onDeleteSource);
    if (spec.kind === "reparse_source") return Boolean(handlers.onReparseSource);
    if (spec.kind === "review_merge") return Boolean(handlers.onReviewMerge);
    return Boolean(handlers.onRelink);
  });
  // 多份来源时按来源分组(组名是来源标题);只有一份或不对准来源的按钮放在一个无名组里。
  const groups: Array<{ key: string; title: string; specs: FindingButtonSpec[] }> = [];
  for (const spec of visible) {
    const sourceId = multiSource && "sourceId" in spec ? spec.sourceId : "";
    let group = groups.find((item) => item.key === sourceId);
    if (!group) {
      group = { key: sourceId, title: sourceId && "title" in spec ? spec.title : "", specs: [] };
      groups.push(group);
    }
    group.specs.push(spec);
  }

  const run = (spec: FindingButtonSpec, event: MouseEvent<HTMLButtonElement>) => {
    if (spec.kind === "open_source") {
      // 让这颗按钮成为焦点,来源详情关掉后焦点回到这里(Safari 点按钮默认不给焦点)。
      event.currentTarget.focus();
      const key = `open:${spec.sourceId}`;
      setBusy(key);
      Promise.resolve(handlers.onOpenSource?.(spec.sourceId))
        .catch((caught: unknown) => {
          if (mounted.current) {
            showResult({
              tone: "failed",
              text: httpErrorStatus(caught) === 404 ? "这篇来源已经不在了" : "没能打开这篇来源，请稍后重试",
            });
          }
        })
        .finally(() => {
          if (mounted.current) setBusy((current) => (current === key ? null : current));
        });
      return;
    }
    if (spec.kind === "delete_source") {
      handlers.onDeleteSource?.(
        { sourceId: spec.sourceId, title: spec.title, sourceType: spec.sourceType },
        (outcome) => {
          if (!mounted.current) return;
          if (outcome === "deleted") {
            showResult({ tone: "ok", text: `已删除「${spec.title}」；点「更新分析」后核验` });
            onChanged();
          } else {
            showResult({ tone: "failed", text: "没有删掉，请稍后重试" });
          }
        },
      );
      return;
    }
    if (spec.kind === "reparse_source") {
      const key = `reparse:${spec.sourceId}`;
      handlers.onReparseSource?.(
        { sourceId: spec.sourceId, title: spec.title, sourceType: spec.sourceType },
        (phase) => {
          if (!mounted.current) return;
          if (phase === "running") {
            setBusy(key);
            return;
          }
          setBusy((current) => (current === key ? null : current));
          if (phase === "started") {
            showResult({ tone: "ok", text: "已开始重新解析；解析完成后点「更新分析」核验" });
            onChanged();
          } else if (phase === "skipped") {
            showResult({ tone: "neutral", text: "这篇来源已不在或无需重新解析" });
            onChanged();
          } else {
            showResult({ tone: "failed", text: "没能开始重新解析，请稍后重试" });
          }
        },
      );
      return;
    }
    if (spec.kind === "review_merge") {
      handlers.onReviewMerge?.(spec.mergeId);
      return;
    }
    handlers.onRelink?.();
  };

  // 成功之后不急着放开按钮:这一行要等问题页重取回来才移走,其间再点只会再发一次请求。
  // 重取回来的新一页会换掉 `row`(哪怕这一行还在),那时再放开。
  const dismiss = async () => {
    setBusy("dismiss");
    try {
      await onDismiss(row);
    } catch (caught) {
      if (!mounted.current) return;
      showResult({ tone: "failed", text: toUserMessage(caught, "没能标为正常，请稍后重试") });
      setBusy((current) => (current === "dismiss" ? null : current));
    }
  };
  useEffect(() => {
    setBusy((current) => (current === "dismiss" ? null : current));
  }, [row]);

  // 在飞期间禁用(长任务按钮红线):删除在跑、重新解析已确认还没排上、来源正在解析时,
  // 对准这篇来源的写按钮都不能再点;补上关联与重新合并 / 整理 / 删除知识图谱共用一个维护槽。
  const disabledFor = (spec: FindingButtonSpec): boolean => {
    switch (spec.kind) {
      case "open_source":
        return busy === `open:${spec.sourceId}`;
      case "delete_source":
      case "reparse_source":
        return busy === `reparse:${spec.sourceId}`
          || state.deletingSourceIds.has(spec.sourceId)
          || plan.processingSourceIds.includes(spec.sourceId);
      case "review_merge":
        return false;
      case "relink":
        return state.relinking || state.maintenanceBusy;
    }
  };

  // 「标为正常」只给可编辑成员的待处理问题(已处理的等更新后核验,不再标)。
  const canDismiss = state.canWrite && row.status === "open";
  const relinkLine = row.rule_id === "F5" && state.relinkResult ? relinkResultText(state.relinkResult) : "";
  const relinkBlocked = row.rule_id === "F5" && state.maintenanceBusy && !state.relinking
    && visible.some((spec) => spec.kind === "relink");

  return (
    <li className={`kg-finding kg-finding--${tone}${handledText ? " is-handled" : ""}`}>
      <div className="kg-finding-head">
        <span className={`kg-finding-dot is-${tone}`} aria-hidden="true" />
        <span className="kg-finding-severity">{findingSeverityLabel(row.severity)}</span>
        <strong className="kg-finding-rule">{findingRuleLabel(row.rule_id)}</strong>
        <span className="kg-finding-kind">{findingSubjectKindLabel(row.subject.kind)}</span>
        {/* 全库一条的问题(没建立关联的内容)定位标签就是「全库」,不再重复一遍名字。 */}
        {row.subject.kind === "notebook" ? null : (
          <span className="kg-finding-subject" title={findingSubjectText(row)}>{findingSubjectText(row)}</span>
        )}
        <span className="kg-finding-impact">{findingImpactText(row, units)}</span>
      </div>
      {basedOn ? <p className="kg-finding-based">{basedOn}</p> : null}
      {evidence.lines.length > 0 ? (
        <ul className="kg-finding-evidence">
          {evidence.lines.map((line) => <li key={line}>{line}</li>)}
        </ul>
      ) : null}
      {evidence.samples.length > 0 ? (
        <p className="kg-finding-samples">
          <span className="kg-finding-samples-label">{evidence.sampleLabel}：</span>
          {evidence.samples.join("、")}
        </p>
      ) : null}
      {evidence.notes.map((note) => <p key={note} className="kg-finding-note">{note}</p>)}
      {handledText ? <p className="kg-finding-handled">{handledText}</p> : null}
      {plan.blocked.map((text) => <p key={text} className="kg-finding-blocked">{text}</p>)}
      {visible.length > 0 || canDismiss || result || relinkLine ? (
        <div className="kg-finding-actions">
          {groups.map((group) => {
            const buttons = group.specs.map((spec) => {
              const deleting = spec.kind === "delete_source" && state.deletingSourceIds.has(spec.sourceId);
              const text = buttonLabel(spec, busy, deleting, state.relinking);
              return (
                <button
                  key={`${spec.kind}:${"sourceId" in spec ? spec.sourceId : ""}`}
                  type="button"
                  className={`sort-button kg-finding-action${spec.kind === "delete_source" ? " is-danger" : ""}`}
                  disabled={disabledFor(spec)}
                  aria-label={buttonAria(spec, text, multiSource)}
                  onClick={(event) => run(spec, event)}
                >
                  {text}
                </button>
              );
            });
            // 一条问题涉及两份来源(两份重复的错文件 / 疑似重复导入)时,按钮按来源分组并写上
            // 是哪一份——否则两颗一模一样的「打开来源核对」分不清各开哪一篇。
            return group.title ? (
              <span key={group.key} className="kg-finding-source-group">
                <span className="kg-finding-source-name" title={group.title}>{group.title}</span>
                {buttons}
              </span>
            ) : buttons;
          })}
          {plan.notes.map((note) => <span key={note} className="kg-finding-action-note">{note}</span>)}
          {relinkBlocked ? <span className="kg-finding-action-note" role="status">其它图谱任务进行中</span> : null}
          {relinkLine ? <span className="kg-finding-result is-ok" role="status">{relinkLine}</span> : null}
          {result ? <span className={`kg-finding-result is-${result.tone}`} role="status">{result.text}</span> : null}
          {canDismiss ? (
            <button
              type="button"
              className="sort-button kg-finding-dismiss"
              disabled={busy === "dismiss"}
              onClick={() => { void dismiss(); }}
              title="确认这条不是问题；之后的报告不再把它列为待处理，可以在「已通过」里撤销"
            >
              {busy === "dismiss" ? "处理中…" : "标为正常"}
            </button>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}

// ------------------------------------------------------------------ 待处理

export function FindingsPendingZone({
  report,
  pageState,
  state,
  handlers,
  notice,
  onOffset,
  onDismiss,
  onChanged,
}: {
  report: KgAnalysisReport;
  pageState: FindingsPageState;
  state: KgFindingsActionState;
  handlers: KgFindingHandlers;
  notice: string;
  onOffset: (next: number) => void;
  onDismiss: (row: KgFindingRow) => Promise<void>;
  onChanged: () => void;
}) {
  const { page, loading, failure } = pageState;
  const summary = report.findings_summary;
  // 待处理区 = open + handled(读侧已经按区给了;这里只按行自己的状态再挡一次)。
  const rows = page?.present
    ? partitionFindings(page.rows).pending
    : [];
  const shownTotal = page?.present ? page.total : 0;
  const truncated = truncatedRuleNotes(summary);
  // 整份清单建于哪一代:报告落后时每条问题都标「基于变更 #N」。
  const basedOn = page?.present ? findingBasedOnChange(page.built_at, summary, report.state.dirty) : "";
  return (
    <section className="kg-analysis-block kg-findings-zone kg-findings-pending" aria-label="待处理">
      <div className="kg-analysis-block-head">
        <div className="kg-analysis-block-title">
          <h3>待处理{page?.present ? `（${formatCount(shownTotal)}）` : ""}</h3>
        </div>
        <p className="kg-analysis-hint">
          按严重度、影响面排列。处理之后点「更新分析」，按每条问题自己的来源或概念核验是否已经解决。
        </p>
      </div>
      {notice ? <p className="kg-findings-notice" role="status">{notice}</p> : null}
      {truncated.map((text) => <p key={text} className="kg-analysis-note">{text}</p>)}
      {failure ? (
        <p className="kg-analysis-error" role="alert">{failure}</p>
      ) : loading && !page ? (
        <p className="tool-hint">正在读取问题清单…</p>
      ) : !page ? null : !page.present ? (
        <FindingsAbsence absence={page.absence} canWrite={state.canWrite} />
      ) : rows.length === 0 ? (
        <p className="kg-analysis-empty">
          {shownTotal === 0 ? "没有待处理的问题。每条规则的检查结果见下方「已通过」。" : "这一页没有问题。"}
        </p>
      ) : (
        <ol className="kg-findings-list">
          {rows.map((row) => (
            <FindingRowView
              key={`${row.rule_id}:${row.subject.key}`}
              row={row}
              units={page.units}
              basedOn={basedOn}
              state={state}
              handlers={handlers}
              onDismiss={onDismiss}
              onChanged={onChanged}
            />
          ))}
        </ol>
      )}
      {page?.present ? (
        <FindingsPager page={page} loading={loading} onOffset={onOffset} label="待处理分页" />
      ) : null}
    </section>
  );
}

/**
 * 诊断数据里的「没建立关联的内容」:补上关联已经跑过、之后没有新内容,再跑也补不上
 * (§4.5)。只说现状,不放按钮。`row` 是 `passed` 那一页里 status 为 relink_spent 的那一行。
 */
export function FindingsRelinkSpentBlock({ row }: { row: KgFindingRow | null }) {
  if (!row) return null;
  const evidence = findingEvidenceView(row);
  return (
    <div className="kg-analysis-block kg-findings-relink-spent">
      <div className="kg-analysis-block-head">
        <div className="kg-analysis-block-title">
          <h3>{findingRuleLabel(row.rule_id)}</h3>
        </div>
      </div>
      <p className="kg-analysis-note">{relinkSpentText(row)}</p>
      {evidence.lines.length > 0 ? (
        <ul className="kg-finding-evidence">
          {evidence.lines.map((line) => <li key={line}>{line}</li>)}
        </ul>
      ) : null}
      {evidence.samples.length > 0 ? (
        <p className="kg-finding-samples">
          <span className="kg-finding-samples-label">{evidence.sampleLabel}：</span>
          {evidence.samples.join("、")}
        </p>
      ) : null}
    </div>
  );
}

// ------------------------------------------------------------------ 需要开发处理

/**
 * 只显示在本库测得到影响的开发侧缺陷(目前只有 S1)。只写现象、规模和「需要开发修复」,
 * **不放按钮**——一个按下去什么都不会变的按钮比没有按钮更糟。没有条目时整个区不出现。
 */
export function FindingsDevZone({ report }: { report: KgAnalysisReport }) {
  const items = devItemViews(report.findings_summary);
  if (items.length === 0) return null;
  return (
    <section className="kg-analysis-block kg-findings-zone kg-findings-dev" aria-label="需要开发处理">
      <div className="kg-analysis-block-head">
        <div className="kg-analysis-block-title">
          <h3>需要开发处理（{formatCount(items.length)}）</h3>
        </div>
      </div>
      <ul className="kg-findings-dev-list">
        {items.map((item) => <li key={item.key}>{item.text}</li>)}
      </ul>
    </section>
  );
}

// ------------------------------------------------------------------ 已通过

function PassedRowView({
  row,
  canWrite,
  onUndismiss,
}: {
  row: KgFindingRow;
  canWrite: boolean;
  onUndismiss: (row: KgFindingRow) => Promise<void>;
}) {
  const mounted = useMountedRef();
  const [busy, setBusy] = useState(false);
  const [result, showResult] = useTimedResult();
  const dismissed = row.status === "dismissed";
  // 与「标为正常」同理:成功后保持「撤销中…」,直到重取回来的新一页换掉这一行。
  const undo = async () => {
    setBusy(true);
    try {
      await onUndismiss(row);
    } catch (caught) {
      if (!mounted.current) return;
      showResult({ tone: "failed", text: toUserMessage(caught, "没能撤销，请稍后重试") });
      setBusy(false);
    }
  };
  useEffect(() => {
    setBusy(false);
  }, [row]);
  return (
    <li className="kg-findings-passed-row">
      <span className="kg-findings-passed-mark" aria-hidden="true">{dismissed ? "↺" : "≠"}</span>
      <span className="kg-findings-passed-text">{passedRowText(row)}</span>
      {dismissed && canWrite ? (
        <button type="button" className="sort-button kg-finding-undo" disabled={busy} onClick={() => { void undo(); }}>
          {busy ? "撤销中…" : "撤销"}
        </button>
      ) : null}
      {result ? <span className={`kg-finding-result is-${result.tone}`} role="status">{result.text}</span> : null}
    </li>
  );
}

/**
 * 已通过:每条规则一行,三种状态分开——✓ 检查过没发现(写检查了多少)、– 没法检查(写原因,
 * 不算通过)、已标为正常(可撤销);F4 的「已判定不是同一个」也在这里。`passed` 那一页里的
 * relink_spent(F5 补上关联已经跑过)不在这里列、也不计数——它在诊断数据里(§4.5)。
 */
export function FindingsPassedZone({
  report,
  passed,
  dismissed,
  spent,
  canWrite,
  notice,
  onPassedOffset,
  onDismissedOffset,
  onUndismiss,
}: {
  report: KgAnalysisReport;
  passed: FindingsPageState;
  dismissed: FindingsPageState;
  /** useRelinkSpentRow 认出的那一行:分页计数里减掉它。 */
  spent: RelinkSpentSighting | null;
  canWrite: boolean;
  notice: string;
  onPassedOffset: (next: number) => void;
  onDismissedOffset: (next: number) => void;
  onUndismiss: (row: KgFindingRow) => Promise<void>;
}) {
  const summary = report.findings_summary;
  const lines = passedRuleLines(summary);
  // passed 那一页 = F4 已判定不是同一个(+ F5 relink_spent,另放诊断数据);dismissed 那一页 =
  // 已标为正常。按行自己的状态分,不会把一行列两遍。
  const decided = passed.page?.present ? partitionFindings(passed.page.rows).passed : [];
  const marked = dismissed.page?.present ? partitionFindings(dismissed.page.rows).dismissed : [];
  const failure = passed.failure || dismissed.failure;
  const nothing = lines.length === 0 && decided.length === 0 && marked.length === 0;
  return (
    <section className="kg-analysis-block kg-findings-zone kg-findings-passed" aria-label="已通过">
      <div className="kg-analysis-block-head">
        <div className="kg-analysis-block-title">
          <h3>已通过</h3>
        </div>
        <p className="kg-analysis-hint">「✓」是检查过、没发现；「–」是没法检查或有一部分没判断，不算通过。</p>
      </div>
      {notice ? <p className="kg-findings-notice" role="status">{notice}</p> : null}
      {lines.length > 0 ? (
        <ul className="kg-findings-rule-lines">
          {lines.map((line) => (
            <li key={`${line.ruleId}:${line.state}`} className={`kg-findings-rule-line is-${line.state}`}>
              <span className="kg-findings-passed-mark" aria-hidden="true">
                {line.state === "clean" ? "✓" : line.state === "decided" ? "≠" : "–"}
              </span>
              <span>{line.text}</span>
            </li>
          ))}
        </ul>
      ) : null}
      {decided.length > 0 || marked.length > 0 ? (
        <ul className="kg-findings-passed-list">
          {decided.map((row) => (
            <PassedRowView key={`d:${row.rule_id}:${row.subject.key}`} row={row} canWrite={canWrite} onUndismiss={onUndismiss} />
          ))}
          {marked.map((row) => (
            <PassedRowView key={`m:${row.rule_id}:${row.subject.key}`} row={row} canWrite={canWrite} onUndismiss={onUndismiss} />
          ))}
        </ul>
      ) : null}
      {passed.page?.present ? (
        <FindingsPager
          page={passed.page}
          loading={passed.loading}
          onOffset={onPassedOffset}
          label="已判定不是同一个的分页"
          elsewhere={spent}
        />
      ) : null}
      {dismissed.page?.present ? (
        <FindingsPager page={dismissed.page} loading={dismissed.loading} onOffset={onDismissedOffset} label="已标为正常的分页" />
      ) : null}
      {failure ? <p className="kg-analysis-error" role="alert">{failure}</p> : null}
      {nothing && !failure ? (
        <p className="kg-analysis-empty">
          {summary.present
            ? "这一次没有可列出的检查结果。"
            : "生成问题清单后，这里会列出每条规则检查了多少、哪些没法检查。"}
        </p>
      ) : null}
    </section>
  );
}
