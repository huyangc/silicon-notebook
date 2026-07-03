/**
 * report-view.tsx
 *
 * 「深度报告」tab:生成 / 列表 / 进度轮询 / 查看 / 取消 / 下载 .md。
 * page.tsx 已过大,面板逻辑集中在这里;page.tsx 只负责接线
 * (类型化 api 函数 + chat-body 里的 <ReportsPanel …/> 分支)。
 *
 * 轮询约定(镜像 page.tsx 的 kg 构建轮询写法):
 * - 列表视图:存在非终态(pending/running)报告时每 6s 刷一次列表,终态即停;
 * - 详情视图:打开的报告非终态时每 6s 刷一次详情,到终态后再同步一次列表;
 * - 组件卸载/依赖变化时清理 interval。
 */
"use client";

import { useEffect, useRef, useState } from "react";
import { ArrowLeft, CheckSquare, Download, Square, Trash2 } from "lucide-react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import { remarkCitations } from "./answer-citations";
import { referenceByAnchorKey, type AnswerReference } from "./answer-formatting";

// ---------------------------------------------------------------------------
// 类型(与 backend/app/models/schemas.py 的 ReportSummary/ReportDetail 对齐)
// ---------------------------------------------------------------------------

export type ReportSummaryT = {
  id: string;
  question: string;
  status: string; // pending | running | done | failed | cancelled
  progress: string;
  section_count: number;
  created_at: string;
  created_by: string;
  depth?: number;
};

export type ReportDetailT = ReportSummaryT & {
  outline: { title: string; scope: string; sub_queries: string[] }[];
  sections: { title: string; markdown: string; grounded: boolean; failed?: boolean }[];
  section_status?: { title: string; phase: string; step: number }[];
  gaps: string[];
  content_md: string;
  references: {
    key: string;
    label: string;
    name?: string;
    source_title?: string;
    location_label?: string;
    object_id?: string;
    object_type?: string;
    tier?: string;
  }[];
  error: string;
};

// 终态判定:pending/running 之外一律视为终态,未知状态不会无限轮询。
const isReportActive = (status: string) => status === "pending" || status === "running";

// 研究深度:五档命名,index 0→4 一一对应 DEPTHS(每节 reflect 步上限)。
// 各档都算深入,区别在充分程度;后端 create_report 会 clamp 到 [1,16]。
const DEPTHS = [1, 2, 4, 8, 16];
const DEPTH_LABELS = ["概览", "标准", "深入", "详尽", "穷尽"];
// 每档一句中性说明(不用快/聪明措辞),popover 里给选中档显示。
const DEPTH_HINTS = [
  "最快出稿,覆盖主干要点",
  "常用档,深度与篇幅平衡",
  "逐节多轮检索,论证更完整",
  "更广取证,细节与边角更全",
  "最充分深挖,覆盖尽可能全面",
];

// 节内进度:phase → 图标类型。完成=对勾,失败=叹号,其余进行中=点动画。
type SectionPhaseIcon = "done" | "failed" | "active";
const sectionPhaseIcon = (phase: string): SectionPhaseIcon =>
  phase === "完成" ? "done" : phase === "失败" ? "failed" : "active";

const STATUS_LABELS: Record<string, string> = {
  pending: "排队中",
  running: "生成中",
  done: "完成",
  failed: "失败",
  cancelled: "已取消",
};

// 与 page.tsx 的 formatRelativeTime 同款(page.tsx 未导出,报告面板本地复刻)。
function formatReportTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return "";
  const diffSec = Math.round((Date.now() - then) / 1000);
  if (diffSec < 60) return "刚刚";
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)} 分钟前`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} 小时前`;
  if (diffSec < 86400 * 30) return `${Math.floor(diffSec / 86400)} 天前`;
  return new Date(then).toLocaleDateString();
}

// 计划指定的导出方式:Blob → 临时 URL → 触发下载。
function downloadMd(r: ReportDetailT) {
  const blob = new Blob([r.content_md], { type: "text/markdown" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `report-${r.id}.md`;
  a.click();
  URL.revokeObjectURL(url);
}

// ---------------------------------------------------------------------------
// ReportMarkdown:复用 answer-markdown.tsx 的引用基建。
// 正文里的 [k\d+] 现为全局按来源去重编号,后端保证每个内联 marker 都有对应
// reference;由 references 构造 refsByKey → remarkCitations 把 [k] 转成可点击
// cite-chip,点击高亮并滚动到「参考文献」段(h2 覆盖挂 id=report-references)。
// ---------------------------------------------------------------------------

export function ReportMarkdown({
  markdown,
  references = [],
}: {
  markdown: string;
  references?: ReportDetailT["references"];
}) {
  const [selectedRefKey, setSelectedRefKey] = useState<string | null>(null);
  const refObjs: AnswerReference[] = references.map((r, i) => ({
    id: `report:${r.key}`,
    displayLabel: `[${i + 1}]`,
    anchor: {
      key: r.key,
      object_id: r.object_id || "",
      object_type: r.object_type || "",
      label: r.label,
      name: r.name,
      source_title: r.source_title,
      location_label: r.location_label,
      tier: r.tier,
    },
  }));
  const refsByKey = referenceByAnchorKey(refObjs);
  const components = {
    a({ href, children }: { href?: string; children?: React.ReactNode }) {
      if (href?.startsWith("cite:")) {
        const key = href.slice(5);
        if (refsByKey[key]) {
          return (
            <button
              type="button"
              className={`cite-chip${selectedRefKey === key ? " active" : ""}`}
              onClick={() => {
                setSelectedRefKey(key);
                document
                  .getElementById("report-references")
                  ?.scrollIntoView({ behavior: "smooth", block: "start" });
              }}
            >
              {children}
            </button>
          );
        }
        return <span>{children}</span>;
      }
      return (
        <a href={href} target="_blank" rel="noreferrer">
          {children}
        </a>
      );
    },
    h2({ children }: { children?: React.ReactNode }) {
      const text = Array.isArray(children) ? children.join("") : String(children ?? "");
      return <h2 id={text.includes("参考文献") ? "report-references" : undefined}>{children}</h2>;
    },
    // 代码块/表格沿用问答区现有样式 class,保持全站观感一致。
    pre({ children }: { children?: React.ReactNode }) {
      return <pre className="answer-code">{children}</pre>;
    },
    table({ children }: { children?: React.ReactNode }) {
      return (
        <div className="answer-table-wrap">
          <table className="answer-table">{children}</table>
        </div>
      );
    },
  } as Parameters<typeof ReactMarkdown>[0]["components"];
  return (
    <div className="report-markdown answer-markdown">
      <ReactMarkdown
        remarkPlugins={[
          remarkGfm,
          remarkMath,
          [remarkCitations, refsByKey] as [typeof remarkCitations, Record<string, AnswerReference>],
        ]}
        rehypePlugins={[rehypeKatex]}
        // 默认 urlTransform 会清掉 cite: 协议 → 徽章 href 丢失;放行 cite:,
        // 其余仍走默认清洗(防 javascript: 等不安全协议)。
        urlTransform={(url) => (url.startsWith("cite:") ? url : defaultUrlTransform(url))}
        components={components}
      >
        {markdown}
      </ReactMarkdown>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 状态徽章:pending/running 亮色(running 附 progress 文字),终态沉色。
// ---------------------------------------------------------------------------

function ReportStatusBadge({ status, progress }: { status: string; progress: string }) {
  const live = isReportActive(status);
  return (
    <span className={`report-status ${status}`} title={progress || undefined}>
      {live && <span className="report-status-dot" aria-hidden />}
      <span className="report-status-label">{STATUS_LABELS[status] ?? status}</span>
      {live && progress && <span className="report-status-progress">{progress}</span>}
    </span>
  );
}

// ---------------------------------------------------------------------------
// ReportsPanel
// ---------------------------------------------------------------------------

export interface ReportsPanelProps {
  notebookId: string;
  listReports: (nb: string) => Promise<ReportSummaryT[]>;
  getReport: (nb: string, rid: string) => Promise<ReportDetailT>;
  createReport: (nb: string, question: string, depth: number) => Promise<{ report_id: string }>;
  cancelReport: (nb: string, rid: string) => Promise<{ status: string }>;
  deleteReport: (nb: string, rid: string) => Promise<{ status: string }>;
  downloadReportsZip: (nb: string, reportIds: string[]) => Promise<void>;
  setToast: (message: string) => void;
}

export function ReportsPanel({
  notebookId,
  listReports,
  getReport,
  createReport,
  cancelReport,
  deleteReport,
  downloadReportsZip,
  setToast,
}: ReportsPanelProps) {
  const [reports, setReports] = useState<ReportSummaryT[] | null>(null);
  const [active, setActive] = useState<ReportDetailT | null>(null);
  const [question, setQuestion] = useState("");
  const [depthIdx, setDepthIdx] = useState(1); // 默认「标准」(depth=2)
  const [depthOpen, setDepthOpen] = useState(false);
  const depthRef = useRef<HTMLDivElement | null>(null);
  const [creating, setCreating] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  // 列表单篇下载:记录正在下载的 rid,禁用该行按钮防重复点击。
  const [downloadingId, setDownloadingId] = useState<string | null>(null);
  // 多选批量下载:是否处于多选模式 + 已选 rid 集合 + zip 下载中标志。
  const [selectMode, setSelectMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [zipBusy, setZipBusy] = useState(false);

  const surfaceError = (error: unknown) =>
    setToast(`报告操作失败：${error instanceof Error ? error.message : String(error)}`);

  // 进 tab / 切换 notebook:重置视图并拉一次列表。
  useEffect(() => {
    let cancelled = false;
    setReports(null);
    setActive(null);
    setQuestion("");
    setConfirmDelete(false);
    setSelectMode(false);
    setSelectedIds(new Set());
    listReports(notebookId)
      .then((rows) => { if (!cancelled) setReports(rows); })
      .catch((error) => { if (!cancelled) { setReports([]); surfaceError(error); } });
    return () => { cancelled = true; };
    // surfaceError 仅包装 setToast,不入依赖。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [notebookId, listReports]);

  // 列表轮询:列表视图下存在非终态报告时每 6s 刷新;终态即停,卸载清理。
  const hasLiveReports = (reports ?? []).some((r) => isReportActive(r.status));
  const detailOpen = active !== null;
  useEffect(() => {
    if (!hasLiveReports || detailOpen) return;
    let cancelled = false;
    const poll = window.setInterval(async () => {
      try {
        const rows = await listReports(notebookId);
        if (!cancelled) setReports(rows);
      } catch { /* 瞬时失败:下一轮继续 */ }
    }, 6000);
    return () => { cancelled = true; window.clearInterval(poll); };
  }, [hasLiveReports, detailOpen, notebookId, listReports]);

  // 详情轮询:打开的报告非终态时每 6s 刷新;到终态再同步一次列表徽章。
  const activeId = active?.id ?? null;
  const activeLive = active ? isReportActive(active.status) : false;
  useEffect(() => {
    if (!activeId || !activeLive) return;
    let cancelled = false;
    const poll = window.setInterval(async () => {
      try {
        const detail = await getReport(notebookId, activeId);
        if (cancelled) return;
        setActive((cur) => (cur && cur.id === activeId ? detail : cur));
        if (!isReportActive(detail.status)) {
          listReports(notebookId)
            .then((rows) => { if (!cancelled) setReports(rows); })
            .catch(() => {});
        }
      } catch { /* 瞬时失败:下一轮继续 */ }
    }, 6000);
    return () => { cancelled = true; window.clearInterval(poll); };
  }, [activeId, activeLive, notebookId, getReport, listReports]);

  // 删除二次确认 4s 后自动复位,避免按钮长期停在危险态。
  useEffect(() => {
    if (!confirmDelete) return;
    const timer = window.setTimeout(() => setConfirmDelete(false), 4000);
    return () => window.clearTimeout(timer);
  }, [confirmDelete]);

  // 研究深度 popover:点外部 / Esc 关闭(镜像 page.tsx 的菜单收起写法)。
  useEffect(() => {
    if (!depthOpen) return;
    function handlePointerDown(event: PointerEvent) {
      const target = event.target;
      if (target instanceof Node && depthRef.current?.contains(target)) return;
      setDepthOpen(false);
    }
    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") setDepthOpen(false);
    }
    window.addEventListener("pointerdown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("pointerdown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [depthOpen]);

  async function submitCreate() {
    const q = question.trim();
    if (!q || creating) return;
    setCreating(true);
    try {
      await createReport(notebookId, q, DEPTHS[depthIdx]);
      setQuestion("");
      setToast("已开始生成深度报告（后台进行，约 5–15 分钟）");
      setReports(await listReports(notebookId));
    } catch (error) {
      surfaceError(error);
    } finally {
      setCreating(false);
    }
  }

  async function openReport(rid: string) {
    try {
      setConfirmDelete(false);
      setActive(await getReport(notebookId, rid));
    } catch (error) {
      surfaceError(error);
    }
  }

  function backToList() {
    setActive(null);
    setConfirmDelete(false);
    listReports(notebookId).then(setReports).catch(() => {});
  }

  async function requestCancel() {
    if (!active || actionBusy) return;
    setActionBusy(true);
    try {
      await cancelReport(notebookId, active.id);
      setToast("已请求取消，报告将停在当前进度");
      const detail = await getReport(notebookId, active.id);
      setActive((cur) => (cur && cur.id === detail.id ? detail : cur));
    } catch (error) {
      surfaceError(error);
    } finally {
      setActionBusy(false);
    }
  }

  async function requestDelete() {
    if (!active || actionBusy) return;
    if (!confirmDelete) { setConfirmDelete(true); return; }
    setActionBusy(true);
    try {
      await deleteReport(notebookId, active.id);
      setToast("报告已删除");
      setActive(null);
      setConfirmDelete(false);
      setReports(await listReports(notebookId));
    } catch (error) {
      surfaceError(error);
    } finally {
      setActionBusy(false);
    }
  }

  // 列表内单篇下载:拉详情拿 content_md,复用 downloadMd;瞬时禁用防重复点击。
  async function downloadOne(rid: string) {
    if (downloadingId) return;
    setDownloadingId(rid);
    try {
      const detail = await getReport(notebookId, rid);
      if (!detail.content_md) {
        setToast("该报告没有正文内容，无法下载");
        return;
      }
      downloadMd(detail);
    } catch (error) {
      surfaceError(error);
    } finally {
      setDownloadingId(null);
    }
  }

  // 进入/退出多选模式;退出时清空已选。
  function toggleSelectMode() {
    setSelectMode((on) => {
      if (on) setSelectedIds(new Set());
      return !on;
    });
  }

  // 勾选/取消勾选单行(仅 done 行会调用)。
  function toggleSelected(rid: string) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(rid)) next.delete(rid);
      else next.add(rid);
      return next;
    });
  }

  // 批量下载:导出选中报告为 reports.zip;成功后退出多选并清空。
  async function downloadSelectedZip() {
    if (zipBusy || selectedIds.size === 0) return;
    setZipBusy(true);
    try {
      await downloadReportsZip(notebookId, Array.from(selectedIds));
      setSelectMode(false);
      setSelectedIds(new Set());
    } catch (error) {
      surfaceError(error);
    } finally {
      setZipBusy(false);
    }
  }

  // ---- 详情视图 ----
  if (active) {
    return (
      <div className="report-panel report-detail">
        <div className="report-detail-head">
          <button className="report-action" type="button" onClick={backToList}>
            <ArrowLeft size={14} /> 返回列表
          </button>
          <div className="report-detail-actions">
            {isReportActive(active.status) && (
              <button
                className="report-action"
                type="button"
                disabled={actionBusy}
                onClick={() => void requestCancel()}
              >
                <Square size={12} /> 取消生成
              </button>
            )}
            {active.content_md && (
              <button className="report-action" type="button" onClick={() => downloadMd(active)}>
                <Download size={14} /> 下载 .md
              </button>
            )}
            <button
              className={`report-action ${confirmDelete ? "danger" : ""}`}
              type="button"
              disabled={actionBusy}
              onClick={() => void requestDelete()}
            >
              <Trash2 size={14} /> {confirmDelete ? "确认删除" : "删除"}
            </button>
          </div>
        </div>
        <div className="report-detail-title">
          <h2 title={active.question}>{active.question}</h2>
          <div className="report-detail-meta">
            <ReportStatusBadge status={active.status} progress={active.progress} />
            <small>
              {formatReportTime(active.created_at)}
              {active.section_count > 0 && ` · ${active.section_count} 节`}
            </small>
          </div>
        </div>
        {active.status === "failed" && active.error && (
          <div className="report-error">生成失败：{active.error}</div>
        )}
        {isReportActive(active.status) && (
          <div className="report-running-hint">
            <p>正在后台生成，此页每 6 秒自动刷新；也可以先去其他 tab，随时回来查看。</p>
            {active.section_status && active.section_status.length > 0 ? (
              <ul className="report-section-status">
                {active.section_status.map((s, index) => {
                  const icon = sectionPhaseIcon(s.phase);
                  return (
                    <li className="report-section-row" key={`${s.title}-${index}`}>
                      <span className={`report-section-icon ${icon}`} aria-hidden>
                        {icon === "done" ? "✓" : icon === "failed" ? "!" : null}
                      </span>
                      <span className="report-section-title" title={s.title}>{s.title}</span>
                      <span className="report-section-phase">
                        {s.phase}
                        {s.phase === "深挖" && s.step > 0 && ` 第${s.step}步`}
                      </span>
                    </li>
                  );
                })}
              </ul>
            ) : active.outline.length > 0 ? (
              <>
                <span className="report-outline-caption">研究大纲（{active.outline.length} 节）</span>
                <ol className="report-outline">
                  {active.outline.map((o, index) => (
                    <li key={`${o.title}-${index}`} title={o.scope || undefined}>{o.title}</li>
                  ))}
                </ol>
              </>
            ) : (
              active.progress && <p className="report-running-progress">{active.progress}</p>
            )}
          </div>
        )}
        {active.content_md ? (
          <ReportMarkdown markdown={active.content_md} references={active.references} />
        ) : (
          !isReportActive(active.status) && active.status !== "failed" && (
            <p className="tool-hint">该报告没有正文内容（可能在完成前被取消）。</p>
          )
        )}
      </div>
    );
  }

  // ---- 列表视图 ----
  return (
    <div className="report-panel">
      <div className="report-compose">
        <textarea
          className="report-compose-input"
          rows={2}
          placeholder="想深入研究什么？例如：对比库内各时序收敛方法的适用场景、代价与已知坑"
          value={question}
          disabled={creating}
          onChange={(event) => setQuestion(event.target.value)}
        />
        <div className="report-compose-actions">
          <span className="report-compose-hint">后台多轮检索并逐节撰写，约 5–15 分钟，期间可离开此页</span>
          <div className="report-compose-controls">
            <div className="report-depth" ref={depthRef}>
              <button
                type="button"
                className={`report-depth-chip${depthOpen ? " open" : ""}`}
                disabled={creating}
                aria-haspopup="dialog"
                aria-expanded={depthOpen}
                onClick={() => setDepthOpen((open) => !open)}
              >
                <span className="report-depth-chip-label">深度</span>
                <span className="report-depth-chip-value">{DEPTH_LABELS[depthIdx]}</span>
              </button>
              {depthOpen && (
                <div className="report-depth-popover" role="dialog" aria-label="研究深度">
                  <div className="report-depth-popover-head">
                    <span className="report-depth-popover-title">研究深度</span>
                    <span className="report-depth-popover-current">{DEPTH_LABELS[depthIdx]}</span>
                  </div>
                  <div className="report-depth-segments" role="radiogroup" aria-label="研究深度">
                    {DEPTH_LABELS.map((label, index) => (
                      <button
                        key={label}
                        type="button"
                        role="radio"
                        aria-checked={index === depthIdx}
                        className={`report-depth-segment${index === depthIdx ? " active" : ""}`}
                        onClick={() => setDepthIdx(index)}
                      >
                        <span className="report-depth-segment-dot" aria-hidden />
                        <span className="report-depth-segment-label">{label}</span>
                      </button>
                    ))}
                  </div>
                  <p className="report-depth-popover-hint">{DEPTH_HINTS[depthIdx]}</p>
                </div>
              )}
            </div>
            <button
              className="button"
              type="button"
              disabled={creating || !question.trim()}
              onClick={() => void submitCreate()}
            >
              {creating ? "提交中…" : "生成深度报告"}
            </button>
          </div>
        </div>
      </div>
      {reports === null ? (
        <p className="tool-hint">加载中…</p>
      ) : reports.length === 0 ? (
        <div className="chat-session-empty">还没有深度报告。输入研究问题，生成第一份带出处的长文报告。</div>
      ) : (
        <>
          {(() => {
            const doneCount = reports.filter((r) => r.status === "done").length;
            return (
              <div className={`report-list-toolbar${selectMode ? " select" : ""}`}>
                {selectMode ? (
                  <>
                    <span className="report-select-count">已选 {selectedIds.size} 篇</span>
                    <div className="report-select-actions">
                      <button
                        className="report-action"
                        type="button"
                        disabled={zipBusy || selectedIds.size === 0}
                        onClick={() => void downloadSelectedZip()}
                      >
                        <Download size={14} /> {zipBusy ? "打包中…" : "下载 zip"}
                      </button>
                      <button className="report-action" type="button" onClick={toggleSelectMode}>
                        取消
                      </button>
                    </div>
                  </>
                ) : (
                  <button
                    className="report-list-select-toggle"
                    type="button"
                    disabled={doneCount === 0}
                    onClick={toggleSelectMode}
                  >
                    <CheckSquare size={14} /> 批量下载
                  </button>
                )}
              </div>
            );
          })()}
          <div className="report-list">
            {reports.map((r) => {
              const isDone = r.status === "done";
              const checked = selectedIds.has(r.id);
              return (
                <article
                  className={`chat-session-card report-card${selectMode && isDone ? " selectable" : ""}${checked ? " selected" : ""}`}
                  key={r.id}
                >
                  {selectMode && isDone && (
                    <label className="report-card-check" onClick={(e) => e.stopPropagation()}>
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => toggleSelected(r.id)}
                        aria-label={`选择报告：${r.question}`}
                      />
                    </label>
                  )}
                  <button
                    className="chat-session-card-main"
                    type="button"
                    title={r.question}
                    onClick={() => (selectMode && isDone ? toggleSelected(r.id) : void openReport(r.id))}
                  >
                    <span>{r.question}</span>
                    <small>
                      {formatReportTime(r.created_at)}
                      {r.section_count > 0 && ` · ${r.section_count} 节`}
                    </small>
                  </button>
                  <div className="report-card-tail">
                    <ReportStatusBadge status={r.status} progress={r.progress} />
                    {isDone && !selectMode && (
                      <button
                        className="report-card-download"
                        type="button"
                        title="下载 .md"
                        aria-label="下载 .md"
                        disabled={downloadingId === r.id}
                        onClick={(e) => {
                          e.stopPropagation();
                          void downloadOne(r.id);
                        }}
                      >
                        <Download size={16} />
                      </button>
                    )}
                  </div>
                </article>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}
