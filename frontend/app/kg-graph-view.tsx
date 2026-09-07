"use client";

import { ReactNode } from "react";
import { BarChart3, Database } from "lucide-react";
import dynamic from "next/dynamic";

import { LatexText } from "./answer-panel";
import { KgEvidenceList } from "./kg-evidence-list";
import { fieldLabel, KgOccurrenceCard, KgProcedureStepCard, kgNodeName } from "./kg-object-cards";
import { KG_TYPE_STYLE, KgTypeMark, kgTypeLabel } from "./kg-type-mark";
import { KG_RANGE_STEPS } from "./kg-workspace-model.ts";
import { formatRelativeTime } from "./relative-time.ts";
import {
  describeScaleIndex,
  queuedScheduleHint,
  UNINDEXED_SCOPE_HINT,
  type ScaleIndexOp,
  type ScaleIndexStatus,
} from "./scale-index.ts";
import type { KgWorkspace } from "./use-kg-workspace";
import { label } from "./vocabulary";
import type {
  FgLink,
  FgNode,
  KgObject,
  PendingMerge,
  UnifiedConceptNode,
} from "./workspace-model.ts";

// react-force-graph-2d uses canvas/window; load client-side only.
const ForceGraph2D = dynamic(() => import("react-force-graph-2d"), { ssr: false });


// 图谱边类型 → 中文。取值真源:prompts.py 列出的 edge_type 词表(supports /
// depends_on / contrasts_with / about / defines / used_in / composed_of / mixed,
// 外加可传递的 derived_from / kind_of / prerequisite_of / precedes / part_of)。
// 此前有 8 个值只是把英文 id 抄了一遍(about: "about"),另有 5 个值压根没进表、
// 靠 `?? edge_type` 直接把英文渲染给用户——两条路都是英文外泄,一并补齐。
const RELATION_LABELS: Record<string, string> = {
  related_concepts: "关联概念",
  related_claims: "关联论断",
  related_formulas: "关联公式",
  related_procedures: "关联过程",
  about: "关于",
  defines: "定义",
  supports: "支持",
  depends_on: "依赖",
  composed_of: "包含",
  part_of: "属于",
  precedes: "先于",
  contrasts_with: "对比",
  used_in: "用于",
  derived_from: "推导自",
  kind_of: "是一种",
  prerequisite_of: "前置于",
  mixed: "多种关联"
};

/**
 * 边类型的界面名。未映射时退到中性的「关联」,**绝不回落成 edge_type 原值**——
 * 后端每加一个边类型,`RELATION_LABELS[t] ?? t` 那种写法都会把英文 id 直接画到
 * 图上(used_in / mixed 等 5 个值就是这么泄出去的)。label() 强制传兜底词,并在
 * 开发期把未映射的值 console.error 出来,让新值被发现而不是被静默渲染。
 */
function relationLabel(edgeType: string): string {
  return label(RELATION_LABELS, edgeType, "关联");
}

function truncateKgLabel(label: string, max = 34): string {
  return label.length > max ? `${label.slice(0, max - 1)}…` : label;
}

function kgPayloadValue(value: unknown): string {
  if (value == null || value === "") return "";
  if (Array.isArray(value)) return value.map(kgPayloadValue).filter(Boolean).join(", ");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function drawKgNode(node: FgNode, ctx: CanvasRenderingContext2D, globalScale: number, selectedId: string | null, denseView: boolean) {
  const x = node.x ?? 0;
  const y = node.y ?? 0;
  // Object.hasOwn 而非 KG_TYPE_STYLE[node.type]:后者走原型链,node.type 为自定义类型
  // "constructor"/"__proto__" 时命中继承属性(函数)→ style.color/glyph 变 undefined、
  // 图谱节点渲染异常。与 kg-type-mark.tsx 的 KgTypeMark 同款防护(PR A 原型链教训)。
  const style = Object.hasOwn(KG_TYPE_STYLE, node.type) ? KG_TYPE_STYLE[node.type] : { color: "#64748b", border: "#334155", text: node.type.slice(0, 2).toUpperCase(), glyph: "circle" };
  const selected = node.id === selectedId;
  const radius = 10 + Math.min(14, Math.sqrt(Math.max(1, node.val)) * 3.2) + (selected ? 2 : 0);

  ctx.save();
  ctx.beginPath();
  if (style.glyph === "diamond") {
    ctx.moveTo(x, y - radius);
    ctx.lineTo(x + radius, y);
    ctx.lineTo(x, y + radius);
    ctx.lineTo(x - radius, y);
    ctx.closePath();
  } else if (style.glyph === "square") {
    ctx.rect(x - radius, y - radius, radius * 2, radius * 2);
  } else if (style.glyph === "triangle") {
    ctx.moveTo(x, y - radius);
    ctx.lineTo(x + radius * 1.08, y + radius * 0.9);
    ctx.lineTo(x - radius * 1.08, y + radius * 0.9);
    ctx.closePath();
  } else {
    ctx.arc(x, y, radius, 0, Math.PI * 2);
  }
  ctx.fillStyle = style.color;
  ctx.fill();
  ctx.lineWidth = (selected ? 3 : 1.5) / globalScale;
  ctx.strokeStyle = selected ? "#111827" : style.border;
  ctx.stroke();

  const innerFont = Math.max(7, 9 / globalScale);
  ctx.fillStyle = "#ffffff";
  ctx.font = `700 ${innerFont}px Inter, ui-sans-serif, system-ui, sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(style.text, x, y + (style.glyph === "triangle" ? radius * 0.12 : 0));

  const shouldDrawLabel = selected || !denseView || node.degree >= 2;
  if (!shouldDrawLabel) {
    ctx.restore();
    return;
  }

  const label = truncateKgLabel(node.name, denseView ? 18 : (node.type === "claim" ? 30 : 24));
  const labelFont = Math.min(14, Math.max(9, 12 / globalScale));
  ctx.font = `650 ${labelFont}px Inter, ui-sans-serif, system-ui, sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  const labelX = x;
  const labelY = y + radius + labelFont * 0.95;
  const metrics = ctx.measureText(label);
  ctx.fillStyle = selected ? "rgba(255,255,255,0.96)" : "rgba(255,255,255,0.82)";
  ctx.fillRect(labelX - metrics.width / 2 - 3 / globalScale, labelY - labelFont * 0.75, metrics.width + 6 / globalScale, labelFont * 1.5);
  ctx.fillStyle = selected ? "#111827" : "#27303f";
  ctx.fillText(label, labelX, labelY);
  ctx.restore();
}

function paintKgPointerArea(node: FgNode, color: string, ctx: CanvasRenderingContext2D) {
  const x = node.x ?? 0;
  const y = node.y ?? 0;
  const radius = 18 + Math.min(16, Math.sqrt(Math.max(1, node.val)) * 3.2);
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(x, y, radius, 0, Math.PI * 2);
  ctx.fill();
}

function drawKgLinkLabel(link: FgLink, ctx: CanvasRenderingContext2D, globalScale: number, denseView: boolean) {
  if (denseView) return;
  const source = typeof link.source === "object" ? link.source : null;
  const target = typeof link.target === "object" ? link.target : null;
  if (!source || !target || source.x == null || source.y == null || target.x == null || target.y == null) return;
  const x = (source.x + target.x) / 2;
  const y = (source.y + target.y) / 2;
  let label = truncateKgLabel(relationLabel(link.label), 18);
  if ((link.sourceCount ?? 1) >= 2) label += ` ×${link.sourceCount}`;
  const fontSize = Math.min(12, Math.max(8, 10 / globalScale));

  ctx.save();
  ctx.font = `600 ${fontSize}px Inter, ui-sans-serif, system-ui, sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  const width = ctx.measureText(label).width + 8 / globalScale;
  ctx.fillStyle = "rgba(255,255,255,0.88)";
  ctx.fillRect(x - width / 2, y - fontSize * 0.72, width, fontSize * 1.45);
  ctx.fillStyle = "#475569";
  ctx.fillText(label, x, y);
  ctx.restore();
}

/**
 * 「知识图谱」全屏视图：左栏（搜索 / 图谱处理 / 当前视图 / 类型过滤 / 待确认合并）、
 * 中间 ForceGraph2D 画布（四态：graph / building / unavailable / empty）、右栏（节点总览
 * 与选中节点详情）。原本整块内联在 page.tsx，本组件是**逐字搬迁**，不改行为。
 *
 * 边界：
 * - state / ref / useMemo 派生 / effects / 命令编排全部留在 page.tsx 与
 *   use-kg-graph.ts；这里只呈现，写动作一律经显式回调交回去。
 * - props 刻意与 page.tsx 里原局部变量**同名**，这样搬过来的 JSX 逐字不变——
 *   `disabled={kgGraph.relinking || kgGraph.rebuilding || kgGraph.buildingKg}` 这类
 *   正是 kg-relink / kg-rebuild 两个守卫的判据文本，改名等于悄悄让守卫失去目标。
 * - `kgGraph` 用 `Pick<>` 收窄到 JSX 真正读的字段，hook 的命令面一个都不进来。
 * - `reportError` 不进组件：需要吞错的两条（加载更多成员 / 总览列表选点）由 page 侧
 *   包好 `.catch(reportError)` 再传进来。
 * - `children` 是「图谱分析」弹窗的插槽：它必须留在 `.kg-view` section 内（section 是
 *   position:fixed，自建层叠上下文，挪出去会改 z-index 归属），但它的 rootModals 开关
 *   编排属于 page，所以由 page 渲染、这里只留位置。
 */
export function KgGraphView({
  kgGraph,
  fgData,
  kgCanvas,
  kgSearching,
  kgDenseView,
  kgSize,
  kgTypeCounts,
  kgNodeGroups,
  selectedKgNode,
  selectedKgEdges,
  relatedNodeGroups,
  kgCanvasRef,
  kgGraphRef,
  kgDetailRef,
  readOnlyWorkspace,
  currentNotebookId,
  baseKgAvailable,
  scaleIndexStatus,
  openKgAnalysis,
  openKgSchemas,
  closeKgView,
  relinkFromKgView,
  confirmRefreshUnifiedKg,
  startKgRebuild,
  handleKgSearchChange,
  changeKgRange,
  toggleKgType,
  reviewPendingMerges,
  reviewAllMerges,
  decideMerge,
  fitKgGraphView,
  runScaleIndexOp,
  onClearTypes,
  onLoadMoreConceptMembers,
  onSelectCanvasNode,
  onSelectOverviewNode,
  children,
}: {
  kgGraph: Pick<
    KgWorkspace["graph"],
    | "buildingKg"
    | "conceptDetail"
    | "conceptDetailGeneration"
    | "conceptMembersLoadError"
    | "conceptMembersLoadingMore"
    | "decidingMerge"
    | "graph"
    | "merged"
    | "nodeContext"
    | "pendingMerges"
    | "rangeBusy"
    | "rangeLimit"
    | "rebuilding"
    | "relinking"
    | "reviewAllJob"
    | "reviewAllStarting"
    | "reviewBusy"
    | "search"
    | "searchBusy"
    | "selectedNodeId"
    | "selectedTypes"
    | "status"
  >;
  fgData: { nodes: FgNode[]; links: FgLink[]; searchHitCount: number };
  kgCanvas: "loading" | "building" | "unavailable" | "empty" | "graph";
  kgSearching: boolean;
  kgDenseView: boolean;
  kgSize: { width: number; height: number };
  kgTypeCounts: Array<{ type: string; label: string; count: number }>;
  kgNodeGroups: Array<{ type: string; label: string; nodes: FgNode[] }>;
  selectedKgNode: UnifiedConceptNode | null;
  selectedKgEdges: Array<{
    source_object_id: string;
    target_object_id: string;
    edge_type: string;
    source_count?: number;
    sourceName: string;
    sourceType: string;
    targetName: string;
    targetType: string;
  }>;
  relatedNodeGroups: Array<{ type: string; label: string; nodes: KgObject[] }>;
  kgCanvasRef: React.RefObject<HTMLDivElement | null>;
  kgGraphRef: React.MutableRefObject<any>;
  kgDetailRef: React.RefObject<HTMLElement | null>;
  readOnlyWorkspace: boolean;
  currentNotebookId: string | null;
  baseKgAvailable: boolean;
  scaleIndexStatus: ScaleIndexStatus | null;
  openKgAnalysis: () => void;
  openKgSchemas: () => void;
  closeKgView: () => void;
  relinkFromKgView: () => void;
  confirmRefreshUnifiedKg: () => void;
  startKgRebuild: (notebookId: string) => void;
  handleKgSearchChange: (value: string) => void;
  changeKgRange: (limit: number) => void;
  toggleKgType: (type: string) => void;
  reviewPendingMerges: () => void;
  reviewAllMerges: () => void;
  decideMerge: (candidate: PendingMerge, confirm: boolean) => void;
  fitKgGraphView: (duration?: number) => void;
  runScaleIndexOp: (op: ScaleIndexOp, onStarted?: () => void) => void;
  onClearTypes: () => void;
  onLoadMoreConceptMembers: () => void;
  /**
   * 画布上点节点。**刻意**与 `onSelectOverviewNode` 分成两个入口：搬迁前画布那条是
   * 不带 catch 的 `selectKgNode(n.id)`（失败落在 unhandledrejection），总览列表那条是
   * `.catch(reportError)`（失败弹 toast）。合成一个回调会把画布的失败呈现从「静默」
   * 变成「toast」——那是行为变化，不在这个零变化搬迁里做。缺口原样登记在此。
   */
  onSelectCanvasNode: (nodeId: string) => void;
  onSelectOverviewNode: (nodeId: string) => void;
  children?: ReactNode;
}) {
  return (
  <section className="kg-view" role="dialog" aria-modal="true">
    <div className="kg-view-header">
      <div><h2>知识图谱</h2><p>Object 级知识图谱：Concept / Claim / Formula / Procedure 同屏展示。节点名称、类型形状和边标签直接画在主视图中。</p></div>
      <div className="kg-view-header-actions">
        {/* 「图谱分析」= 只读诊断报告(对象构成 / 合并收敛 / 主题板块 / 板块俯瞰图 /
            关联稀疏的来源)。后端两个端点走 require_notebook_read,只读成员也能看,
            所以这里不做 admin 门控;面板本身不含任何写动作。 */}
        <button
          type="button"
          className="sort-button kg-schema-button"
          onClick={openKgAnalysis}
          title="查看这个知识库的构成、合并收敛与主题板块分布"
        >
          <BarChart3 size={16} /> 图谱分析
        </button>
        <button
          type="button"
          className="sort-button kg-schema-button"
          onClick={openKgSchemas}
          title="查看当前笔记本采用的知识对象类型与字段"
        >
          <Database size={16} /> 图谱 Schema
        </button>
        <button className="icon-button" onClick={() => closeKgView()} title="Close">×</button>
      </div>
    </div>
    <div className="kg-view-body">
      <aside className="kg-rail">
        <input className="kg-search" placeholder="搜索节点名称或类型…" value={kgGraph.search} onChange={(e) => handleKgSearchChange(e.target.value)} />
        {!readOnlyWorkspace && (
        <div className="kg-rail-section">
          <h3>图谱处理</h3>
          <div className="kg-action-stack">
            {/* codex R4 P2(B):「重新合并」与「补上关联」共用服务端同一把按笔记本
                单飞锁，disabled 必须认「任一忙碌位为真即忙」——否则占槽的那一件事
                在跑时，另一颗按钮仍可点，点了也只会撞 409。各自的进行态文案不变。 */}
            <button
              type="button"
              className="sort-button"
              disabled={kgGraph.relinking || kgGraph.rebuilding || kgGraph.buildingKg}
              title="为没建立关联的内容补上关联（快速、确定性，不覆盖现有图）"
              onClick={relinkFromKgView}
            >
              {kgGraph.relinking ? "补连中…" : "补上关联"}
            </button>
            <button
              type="button"
              className="sort-button"
              disabled={kgGraph.rebuilding || kgGraph.relinking || kgGraph.buildingKg}
              title="对现有概念重新聚类 / 跨文档合并并刷新（不重新分析来源，会先确认）"
              onClick={confirmRefreshUnifiedKg}
            >
              {kgGraph.rebuilding ? "合并中…" : "重新合并"}
            </button>
            <button
              type="button"
              className="sort-button kg-action-danger"
              disabled={kgGraph.buildingKg}
              title="清空现有知识图谱并重新分析全部来源（后台任务，可能数分钟）"
              onClick={() => { if (currentNotebookId) startKgRebuild(currentNotebookId); }}
            >
              {kgGraph.buildingKg ? "分析中…" : "全部重新分析"}
            </button>
          </div>
        </div>
        )}
        <div className="kg-rail-section">
          <h3>当前视图</h3>
          <div className="tag-row">
            <span className="tag">节点 {fgData.nodes.length}{!kgSearching && kgGraph.merged ? ` / ${kgGraph.merged.nodes.length}` : ""}</span>
            <span className="tag">边 {fgData.links.length}{!kgSearching && kgGraph.merged ? ` / ${kgGraph.merged.edges.length}` : ""}</span>
          </div>
          <label className="kg-range">
            <span>范围</span>
            <select value={kgGraph.rangeLimit} disabled={kgGraph.rangeBusy || kgSearching} onChange={(e) => changeKgRange(Number(e.target.value))}>
              {KG_RANGE_STEPS
                .filter((opt) => {
                  // index 索引库（base_kg_available）用搜索+展开代替全量拉取，隐藏「全部」。
                  if (opt.value === 0 && baseKgAvailable) return false;
                  return true;
                })
                .map((opt) => <option key={opt.value} value={opt.value}>{opt.label}</option>)}
            </select>
          </label>
          {kgSearching ? (
            <p className="tool-hint" style={{ margin: "4px 2px 0" }}>
              {kgGraph.searchBusy
                ? "搜索中…"
                : `命中 ${fgData.searchHitCount} 个节点`}
            </p>
          ) : kgGraph.graph && (
            <p className="tool-hint" style={{ margin: "4px 2px 0" }}>
              {kgGraph.rangeBusy
                ? "加载中…"
                : kgGraph.graph.truncated
                  ? `已载 ${kgGraph.graph.nodes.length} / 共 ${kgGraph.graph.total_nodes ?? kgGraph.graph.nodes.length} 节点 · 按连接度，可扩大范围`
                  : `共 ${kgGraph.graph.total_nodes ?? kgGraph.graph.nodes.length} 节点（已全部显示）`}
            </p>
          )}
          {kgGraph.status && (
            <div className="tag-row" style={{ marginTop: 4 }}>
              {/* 纯状态展示,非交互——唯一动作入口是上方「重新合并」按钮(去重复,见其 title)。 */}
              <span
                className="tag"
                title="概念合并状态；点击上方「重新合并」按钮可手动刷新"
                style={{ color: kgGraph.status.dirty ? "var(--color-warn, #b97a00)" : undefined }}
              >
                {kgGraph.rebuilding ? "重建中…" : kgGraph.status.dirty ? "待重建" : "最新"}
              </span>
              {kgGraph.status.last_rebuild_at && (
                <span className="tag">上次重建 · {formatRelativeTime(kgGraph.status.last_rebuild_at)}</span>
              )}
              {scaleIndexStatus && (() => {
                const s = scaleIndexStatus;
                const v = describeScaleIndex(s);
                const clickable = v.primaryOp !== null && !readOnlyWorkspace;
                const color = v.tone === "warn" ? "var(--color-warn, #b97a00)"
                  : v.tone === "ok" ? "var(--color-ok, #1a7f5a)" : undefined;
                const label = `检索索引：${v.stateLabel}${v.state === "indexed" ? ` · ${s.n_nodes} 节点` : ""}`;
                return (
                  <span
                    className="tag"
                    role={clickable ? "button" : undefined}
                    tabIndex={clickable ? 0 : undefined}
                    title={clickable
                      ? (v.primaryOp === "update" ? "点击更新检索索引（会先确认）" : v.primaryOp === "rebuild" ? "点击全量重建检索索引（会先确认）" : "点击构建检索索引（会先确认）")
                      : v.state === "queued" ? queuedScheduleHint(s, new Date())
                      : (s.eligible ? "" : "内容较少，暂不需要检索索引（直接搜索已够快）")}
                    onClick={clickable ? () => runScaleIndexOp(v.primaryOp!) : undefined}
                    onKeyDown={clickable ? ((e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); runScaleIndexOp(v.primaryOp!); } }) : undefined}
                    style={{ cursor: clickable ? "pointer" : "default", color }}
                  >
                    {label}
                    {s.exists && !s.delta_searchable && (s.unindexed_sources ?? 0) > 0 && (
                      <span title={UNINDEXED_SCOPE_HINT}>
                        {` · ${s.unindexed_sources} 源待索引`}
                      </span>
                    )}
                  </span>
                );
              })()}
            </div>
          )}
        </div>
        <div className="kg-rail-section">
          <h3>类型过滤</h3>
          <div className="kg-type-filter">
            <button
              aria-pressed={kgGraph.selectedTypes.length === 0}
              className={kgGraph.selectedTypes.length === 0 ? "active" : ""}
              onClick={onClearTypes}
            >
              <span className="kg-shape-stack">
                {kgTypeCounts.slice(0, 4).map((item) => <KgTypeMark key={item.type} type={item.type} />)}
              </span>
              <strong>全部</strong>
              <em>{kgGraph.graph?.nodes.length ?? 0}</em>
            </button>
            {kgTypeCounts.map((item) => (
              <button
                aria-pressed={kgGraph.selectedTypes.includes(item.type)}
                className={kgGraph.selectedTypes.includes(item.type) ? "active" : ""}
                key={item.type}
                onClick={() => toggleKgType(item.type)}
              >
                <KgTypeMark type={item.type} />
                <strong>{item.label}</strong>
                <em>{item.count}</em>
              </button>
            ))}
          </div>
        </div>
        <div className="kg-rail-section">
          <h3>待确认合并 ({kgGraph.pendingMerges.length})</h3>
          {!readOnlyWorkspace && (
            <>
              <button className="ghost-button" onClick={reviewPendingMerges} disabled={!kgGraph.pendingMerges.length || kgGraph.reviewBusy}>
                {kgGraph.reviewBusy ? "判重中…" : "自动判重"}
              </button>
              <button
                className="ghost-button"
                onClick={reviewAllMerges}
                disabled={!kgGraph.pendingMerges.length || kgGraph.reviewAllStarting || kgGraph.reviewAllJob?.status === "running"}
              >
                {kgGraph.reviewAllJob?.status === "running"
                  ? `全部判重中… ${kgGraph.reviewAllJob.done}/${kgGraph.reviewAllJob.total}`
                  : kgGraph.reviewAllStarting
                    ? "全部判重中…"
                    : "全部自动判重"}
              </button>
            </>
          )}
          {kgGraph.pendingMerges.length === 0 ? <p className="tool-hint">无</p> : kgGraph.pendingMerges.map((m) => (
            <div className="kg-merge-row" key={m.id}>
              <span>{m.canonical_a.replace(/^K-/, "")} ↔ {m.canonical_b.replace(/^K-/, "")} <em>({m.score.toFixed(2)})</em></span>
              {!readOnlyWorkspace && <span className="kg-merge-actions">
                {/* 确认会连带跑一次全量概念合并重建；重建完成前锁住整列，避免新决定
                    与正在发布的旧候选代次竞态。拒绝不重建，但提交期间同样防重复点。 */}
                <button disabled={kgGraph.decidingMerge !== null || kgGraph.rebuilding} onClick={() => decideMerge(m, true)}>
                  {kgGraph.decidingMerge?.id === m.id && kgGraph.decidingMerge.confirm ? "合并中…" : "合并"}
                </button>
                <button disabled={kgGraph.decidingMerge !== null || kgGraph.rebuilding} onClick={() => decideMerge(m, false)}>
                  {kgGraph.decidingMerge?.id === m.id && !kgGraph.decidingMerge.confirm ? "分开中…" : "拒绝"}
                </button>
              </span>}
            </div>
          ))}
        </div>
      </aside>
      <div className="kg-canvas" ref={kgCanvasRef}>
        {kgCanvas === "loading" ? (
          <p className="tool-hint kg-canvas-empty">加载中…</p>
        ) : kgCanvas === "building" ? (
          <div className="tool-hint kg-canvas-empty">
            <strong>图谱索引构建中，首次构建大库可能需要几分钟…</strong>
            <p style={{ marginTop: 6 }}>建成后会自动刷新为完整图谱</p>
          </div>
        ) : kgCanvas === "unavailable" ? (
          <div className="tool-hint kg-canvas-empty">
            <strong>库规模较大，图谱预览将在下一次索引构建后可用</strong>
            <p style={{ marginTop: 6 }}>这一次打开不会在后台生成预览；其余功能不受影响</p>
          </div>
        ) : kgCanvas === "empty" ? (
          <p className="tool-hint kg-canvas-empty">没有匹配的节点。清空搜索后可查看完整图谱。</p>
        ) : (
          <ForceGraph2D
            ref={kgGraphRef}
            graphData={fgData}
            nodeLabel={(n: any) => `${n.name} (${n.type})`}
            nodeVal={(n: any) => n.val}
            width={kgSize.width}
            height={kgSize.height}
            linkDirectionalArrowLength={7}
            linkDirectionalArrowRelPos={1}
            linkColor={() => "rgba(91, 105, 130, 0.42)"}
            linkWidth={(link: any) => 1.35 + Math.min(((link.sourceCount ?? 1) - 1), 4) * 0.5}
            linkLabel={(link: any) => {
              const base = relationLabel(link.label);
              return (link.sourceCount ?? 1) >= 2 ? `${base} · ${link.sourceCount} 源支持` : base;
            }}
            linkCanvasObjectMode={() => "after"}
            linkCanvasObject={(link: any, ctx: CanvasRenderingContext2D, globalScale: number) => drawKgLinkLabel(link, ctx, globalScale, kgDenseView)}
            nodeCanvasObject={(node: any, ctx: CanvasRenderingContext2D, globalScale: number) => drawKgNode(node, ctx, globalScale, kgGraph.selectedNodeId, kgDenseView)}
            nodePointerAreaPaint={(node: any, color: string, ctx: CanvasRenderingContext2D) => paintKgPointerArea(node, color, ctx)}
            d3VelocityDecay={0.32}
            onEngineStop={() => fitKgGraphView(350)}
            onNodeClick={(n: any) => onSelectCanvasNode(n.id)}
          />
        )}
        <div className="kg-legend">
          {Object.entries(KG_TYPE_STYLE).map(([type]) => (
            <span key={type}><KgTypeMark type={type} />{kgTypeLabel(type)}</span>
          ))}
        </div>
      </div>
      <aside className="kg-detail" ref={kgDetailRef}>
        <div className="kg-node-overview">
          <div className="kg-detail-heading">
            <h3>节点总览</h3>
            <span>{kgNodeGroups.reduce((sum, group) => sum + group.nodes.length, 0)} 个</span>
          </div>
          {kgNodeGroups.length === 0 ? <p className="tool-hint">暂无节点。</p> : kgNodeGroups.map((group) => (
            <section className="kg-type-group" key={group.type}>
              <div className="kg-type-header">
                <span><KgTypeMark type={group.type} />{group.label}</span>
                <strong>{group.nodes.length}</strong>
              </div>
              <div className="kg-node-list">
                {group.nodes.map((node) => (
                  <button
                    className={`kg-node-button ${kgGraph.selectedNodeId === node.id ? "active" : ""}`}
                    key={node.id}
                    onClick={() => onSelectOverviewNode(node.id)}
                  >
                    <span>{truncateKgLabel(node.name, 58)}</span>
                    <em>{node.degree}</em>
                  </button>
                ))}
              </div>
            </section>
          ))}
        </div>

        <div className="kg-selected-detail">
          {!selectedKgNode ? <p className="tool-hint">点击图中节点或总览列表查看详情。</p> : (
            <div className="stack">
              <h3><LatexText text={kgNodeName(selectedKgNode)} isFormula={selectedKgNode.object_type === "formula"} /></h3>
              <div className="tag-row">
                <span className="tag kg-selected-type"><KgTypeMark type={selectedKgNode.object_type} />{kgTypeLabel(selectedKgNode.object_type)}</span>
                <span className="tag">关系 {selectedKgEdges.length}</span>
              </div>
              {Object.entries(selectedKgNode.payload)
                .filter(([key, value]) => !["name", "section_path"].includes(key) && Boolean(kgPayloadValue(value)))
                .map(([key, value]) => (
                  <p key={key}><strong>{fieldLabel(key)}：</strong>{kgPayloadValue(value)}</p>
                ))}
              {selectedKgEdges.length > 0 && (
                <>
                  <h4>相邻关系</h4>
                  {selectedKgEdges.slice(0, 24).map((edge, index) => (
                    <div className="kg-relation-row" key={`${edge.source_object_id}-${edge.target_object_id}-${index}`}>
                      <span className="kg-relation-node"><KgTypeMark type={edge.sourceType} /><span>{truncateKgLabel(edge.sourceName, 28)}</span></span>
                      {edge.source_count && edge.source_count >= 2 ? (
                        <span className="kg-relation-mid">
                          <strong>{relationLabel(edge.edge_type)}</strong>
                          <span className="tag">×{edge.source_count}源</span>
                        </span>
                      ) : (
                        <strong>{relationLabel(edge.edge_type)}</strong>
                      )}
                      <span className="kg-relation-node"><KgTypeMark type={edge.targetType} /><span>{truncateKgLabel(edge.targetName, 28)}</span></span>
                    </div>
                  ))}
                </>
              )}
              {kgGraph.nodeContext?.definition && (<><h4>定义</h4><p className="kg-text-card">{kgGraph.nodeContext.definition}</p></>)}
              {kgGraph.nodeContext?.object_type === "procedure" && kgGraph.nodeContext.steps && kgGraph.nodeContext.steps.length > 0 && (
                <><h4>流程步骤</h4>{kgGraph.nodeContext.steps.map((s, i) => (
                  <KgProcedureStepCard step={s} index={i} key={`${s.name}-${i}`} />
                ))}</>
              )}
              {kgGraph.conceptDetail && (
                <>
                  <h4>出处</h4>
                  <KgEvidenceList evidence={kgGraph.conceptDetail.evidence} resetKey={`${kgGraph.conceptDetail.canonical_id}:${kgGraph.conceptDetailGeneration}`} />
                  <h4>相关节点</h4>
                  {relatedNodeGroups.length === 0 ? <p className="tool-hint">无</p> : relatedNodeGroups.map((group) => (
                    <section className="kg-related-group" key={group.type}>
                      <div className="kg-type-header">
                        <span><KgTypeMark type={group.type} />{group.label}</span>
                        <strong>{group.nodes.length}</strong>
                      </div>
                      <div className="kg-related-list">
                        {group.nodes.map((node) => (
                          <div className="kg-related-node" key={node.id}>
                            <span><KgTypeMark type={node.object_type} /><LatexText text={String(node.payload.name ?? "")} isFormula={node.object_type === "formula"} /></span>
                            {node.edge_type ? <em>{relationLabel(node.edge_type)}</em> : null}
                          </div>
                        ))}
                      </div>
                    </section>
                  ))}
                  {kgGraph.conceptDetail.next_cursor && (
                    <button
                      type="button"
                      className="kg-load-more-members"
                      disabled={kgGraph.conceptMembersLoadingMore}
                      onClick={onLoadMoreConceptMembers}
                    >
                      {kgGraph.conceptMembersLoadingMore
                        ? "加载中…"
                        : kgGraph.conceptMembersLoadError
                          ? "加载失败，点击重试"
                          : `加载更多成员（已加载 ${kgGraph.conceptDetail.members.length}/${kgGraph.conceptDetail.member_total}）`}
                    </button>
                  )}
                </>
              )}
              {!kgGraph.conceptDetail && kgGraph.nodeContext && (kgGraph.nodeContext.occurrences ?? []).length > 0 && (
                <><h4>出处</h4>{(kgGraph.nodeContext.occurrences ?? []).slice(0, 10).map((o, i) => (
                  <KgOccurrenceCard occurrence={o} index={i} key={`${o.source_title || o.source_id}-${i}`} />
                ))}</>
              )}
            </div>
          )}
        </div>
      </aside>
    </div>
    {children}
  </section>
  );
}
