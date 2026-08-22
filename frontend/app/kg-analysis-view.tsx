/**
 * kg-analysis-view.tsx
 *
 * 「图谱分析」质量诊断视图(批 1:A1 对象构成 / A2 收敛率 / C1 主题板块 / C2 关联稀疏的来源 /
 * E1 板块俯瞰图)。入口在知识图谱视图头部,与「图谱 Schema」并列。
 *
 * page.tsx 已过大,面板逻辑集中在这里;page.tsx 只负责接线(一个按钮 + 一个开关)。
 *
 * 这个视图的全部价值在于**如实呈现**,四条不许打折(设计 §3.3 / §3.5):
 *
 *   1. **逐指标新鲜度**:每一块数据自己带「口径 · 建于哪次变更 · 落后多少」,而不是在
 *      页面顶部挂一条「可能过期」的横幅。真实教训:有人据一份陈旧数据推出了关于图
 *      结构的重大结论,随后才得知该库尚未整理,整个推断作废。
 *   2. **口径来源可分辨**:同屏并列的数字可能来自「整理当时的实时口径」与「上次主题
 *      板块划分」两种口径,不标注读者没有任何线索分辨为什么对不上。
 *   3. **单位**:计数的单位一律从响应的 `units` 里读(不在这里硬写),因为单位真的不
 *      一样——合并后的知识对象 / 合并前的成员 / 知识对象 / 板块对 / 关联 / 来源。
 *      ⚠ 来源画像的口径汇总(head_communities / head_members / total_members)只在
 *      **总览的数据清单**里带单位表,`/sources` 那一页的 units 不含这几个字段,所以
 *      那段文字读的是总览里的那份产物,而不是分页响应的 summary。
 *   4. **缺失 ≠ 为空 ≠ 截断**:没算过、合法缺席、算过但内容为 0、落库级截断、请求级
 *      截断,五种情形五种呈现,绝不都渲染成一个空白或一个 0。
 *
 * 规模(按生产库设计,不按本机小库):板块数是万级、来源近 5 万。所以俯瞰图只单独画
 * top-N 个板块 + 一个长尾汇总节点并声明覆盖率,来源表走后端分页。
 *
 * 页面先把技术统计翻译成可采取行动的诊断信号，再保留逐指标口径供复核；可编辑成员还能
 * 从这里触发生成/更新，复用知识图谱已有的后台重新合并与轮询链路。
 */
"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, RefreshCw } from "lucide-react";

import { AnomalyBadge } from "./anomaly-badge";
import {
  analysisArtifactAnomalies,
  analysisLedgerAnomalies,
  analysisSourceRowAnomalies,
  type Anomaly,
} from "./anomaly-severity";
import { toUserMessage } from "./errors";
import { FloatingModalCard } from "./floating-modal-card";
import {
  fetchKgAnalysis,
  fetchKgAnalysisSources,
  type KgAnalysisReport,
  type KgArtifactView,
  type KgSourceOrder,
  type KgSourceProfilePage,
  type KgSourceProfileRow,
} from "./kg-analysis-api";
import {
  artifactLabel,
  artifactPurpose,
  basisLabel,
  boardList,
  boardMapModel,
  BOARD_MAP_MAX_NODES,
  BOARD_MAP_VIEW,
  convergenceTable,
  countText,
  formatCount,
  formatPercent,
  freshnessNote,
  largestClusters,
  relationProvenance,
  reportHeadline,
  SOURCE_ORDER_LABEL,
  unitFor,
  type ConvergenceTable,
  type FreshnessNote,
  type LargestClusters,
  type RelationProvenance,
} from "./kg-analysis-model";
import { label } from "./vocabulary";

const SOURCE_PAGE_SIZE = 20;
const BOARD_LIST_LIMIT = 50;
const BOARD_EDGE_LIMIT = 200;
const BOARD_TOP_MEMBERS = 5;

const ARTIFACT_HISTOGRAM = "cluster_size_histogram";
const ARTIFACT_LARGEST_CLUSTERS = "largest_clusters";
const ARTIFACT_RELATION_PROVENANCE = "relation_provenance";
const ARTIFACT_COMMUNITY_EDGES = "community_edges";
const ARTIFACT_SOURCE_PROFILES = "source_profiles";

/** 口径 · 建于哪次变更 · 落后多少 —— 每一块数据都挂一条,这是本视图的硬要求。 */
function FreshnessLine({ note }: { note: FreshnessNote }) {
  return (
    <span className="kg-analysis-freshness">
      <span className="kg-analysis-basis">{note.basis}</span>
      <span aria-hidden="true">·</span>
      <span>{note.built}</span>
      {note.behind ? (
        <>
          <span aria-hidden="true">·</span>
          <span>{note.behind}</span>
        </>
      ) : null}
    </span>
  );
}

function AnomalyRow({ anomalies }: { anomalies: Anomaly[] }) {
  if (anomalies.length === 0) return null;
  return (
    <span className="kg-analysis-anomalies">
      {anomalies.map((anomaly, index) => (
        <AnomalyBadge key={`${anomaly.severity}-${index}`} anomaly={anomaly} />
      ))}
    </span>
  );
}

function BlockHead({
  title,
  note,
  anomalies,
  hint,
}: {
  title: string;
  note: FreshnessNote;
  anomalies: Anomaly[];
  hint?: string;
}) {
  return (
    <div className="kg-analysis-block-head">
      <div className="kg-analysis-block-title">
        <h3>{title}</h3>
        <AnomalyRow anomalies={anomalies} />
      </div>
      <FreshnessLine note={note} />
      {hint ? <p className="kg-analysis-hint">{hint}</p> : null}
    </div>
  );
}

/**
 * 一份数据「不在场」时的说明。
 *
 * 三种缺席的措辞刻意不同(never_computed / expected / unexpected,同时由 AnomalyBadge
 * 分档),这里再补上「所以这一格不是 0」那句话——把缺席渲染成 0 正是本视图要消灭的
 * 那类误读。
 */
function ArtifactAbsence({ absence }: { absence: string | null }) {
  const reason = absence === "expected"
    ? "这个知识库一个主题板块都没有，这份数据没有可说的内容，因此没有生成。"
    : absence === "unexpected"
      ? "同一轮里其它数据都在，唯独这一份没有写下来。"
      : "这个知识库还没算过这份数据。";
  return (
    <p className="kg-analysis-absent">
      {reason}
      <span className="kg-analysis-absent-note">这一格没有数字可看，与「算出来是 0」不是一回事。</span>
    </p>
  );
}

/** 数据在场、但内容确实是 0 —— 与「没算过」分开说。 */
function EmptyPayload({ text }: { text: string }) {
  return <p className="kg-analysis-empty">{text}</p>;
}

export function KgAnalysisView({
  notebookId,
  onClose,
  canAnalyze = false,
  analysisRunning = false,
  analysisBlocked = false,
  interactive = true,
  zIndex,
  onAnalyze,
}: {
  notebookId: string;
  onClose: () => void;
  canAnalyze?: boolean;
  analysisRunning?: boolean;
  analysisBlocked?: boolean;
  interactive?: boolean;
  zIndex?: number;
  onAnalyze?: () => void;
}) {
  const [report, setReport] = useState<KgAnalysisReport | null>(null);
  const [reportError, setReportError] = useState("");
  const [loading, setLoading] = useState(true);
  const [reloadToken, setReloadToken] = useState(0);

  const [sources, setSources] = useState<KgSourceProfilePage | null>(null);
  const [reviewCandidate, setReviewCandidate] = useState<KgSourceProfileRow | null>(null);
  const [sourcesError, setSourcesError] = useState("");
  const [sourcesLoading, setSourcesLoading] = useState(true);
  const [order, setOrder] = useState<KgSourceOrder>("sparse");
  const [offset, setOffset] = useState(0);

  // 换库时清掉**笔记本作用域**的那几份 state。
  //
  // ⚠ 必须在渲染期做,不能只交给下面的 effect:effect 在提交之后才跑,中间那一帧会拿
  // 上一个库的 `report` / `sources` 渲染出一份**完整的报告**(渲染只在 `report` 为
  // null 时才显示加载态),读者看到的是别的库的数字却没有任何标注 —— 正是本视图存在的
  // 理由的反面。`offset` 更实际:上一个库翻到第 40 页,新库可能只有 3 条,不归零就会
  // 替它请求一个越界的页。这是 React 文档里「prop 变了要调状态」的写法,setState 会让
  // 本次渲染的输出被丢弃、立刻用新 state 重渲,子组件与 DOM 都不会见到旧值。
  //
  // 刻意**不**复位的三项(它们不是笔记本作用域的):
  //   · `order` —— 排序偏好,两个取值对任何库都合法,跟着人走比跟着库走更合理;
  //   · `reloadToken` —— 单调计数器,只当 effect 依赖用,归零反而会漏掉一次重取;
  //   · 弹窗位置(`FloatingModalCard` 的 storageKey)—— 窗口几何,与库无关。
  //
  // 两份 error 与两个 loading 也在这里清,但它们**不是**同一档缺陷:下面的 effect 本来
  // 就会在自己开头清掉,所以它们最多陈旧一帧(而 `report` / `sources` / `offset` 是
  // effect 根本不碰的,会一直陈旧到新请求返回)。这里一并清是顺手把那一帧也去掉 ——
  // 一帧的差别在 React Testing Library 里观察不到(`act` 会把 effect 一起冲掉),所以
  // 这四个字段**没有**独立守卫,别照着它们再写一条恒绿的测试。
  const [shownNotebook, setShownNotebook] = useState(notebookId);
  if (shownNotebook !== notebookId) {
    setShownNotebook(notebookId);
    setReport(null);
    setReportError("");
    setLoading(true);
    setSources(null);
    setReviewCandidate(null);
    setSourcesError("");
    setSourcesLoading(true);
    setOffset(0);
  }

  // 结论区的复核候选固定来自“最稀疏”第一页，不能跟着下方表格的浏览排序消失。
  // 初始 sparse 首页直接复用分页请求；只有用户切到 connected 时才补一份独立的
  // 有界 sparse 首页，并在分析刷新后重取。
  useEffect(() => {
    if (order !== "connected") return;
    let cancelled = false;
    fetchKgAnalysisSources(notebookId, {
      limit: SOURCE_PAGE_SIZE,
      offset: 0,
      order: "sparse",
    })
      .then((data) => {
        if (cancelled) return;
        setReviewCandidate(data.present
          ? data.rows.find((row) => !row.source_missing) ?? null
          : null);
      })
      .catch(() => {
        if (!cancelled) setReviewCandidate(null);
      });
    return () => {
      cancelled = true;
    };
  }, [notebookId, order, reloadToken]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setReportError("");
    fetchKgAnalysis(notebookId, {
      boards: BOARD_LIST_LIMIT,
      topMembers: BOARD_TOP_MEMBERS,
      edges: BOARD_EDGE_LIMIT,
    })
      .then((data) => {
        if (cancelled) return;
        setReport(data);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setReport(null);
        setReportError(toUserMessage(error, "图谱分析暂时打不开，请稍后重试"));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [notebookId, reloadToken]);

  useEffect(() => {
    let cancelled = false;
    setSourcesLoading(true);
    setSourcesError("");
    fetchKgAnalysisSources(notebookId, { limit: SOURCE_PAGE_SIZE, offset, order })
      .then((data) => {
        if (cancelled) return;
        setSources(data);
        if (data.order === "sparse" && data.offset === 0) {
          setReviewCandidate(data.present
            ? data.rows.find((row) => !row.source_missing) ?? null
            : null);
        }
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setSources(null);
        setSourcesError(toUserMessage(error, "来源清单暂时打不开，请稍后重试"));
      })
      .finally(() => {
        if (!cancelled) setSourcesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [notebookId, offset, order, reloadToken]);

  const refresh = useCallback(() => {
    setOffset(0);
    setReloadToken((token) => token + 1);
  }, []);

  // 后台任务完成时自动取回新产物。这里只观察父层已经可靠配对 job_id 的忙碌态，
  // 不在报告弹窗里再造一套轮询与竞态处理。
  const previousAnalysisRunning = useRef(analysisRunning);
  useEffect(() => {
    if (previousAnalysisRunning.current && !analysisRunning) refresh();
    previousAnalysisRunning.current = analysisRunning;
  }, [analysisRunning, refresh]);

  const changeOrder = useCallback((next: KgSourceOrder) => {
    setOrder(next);
    setOffset(0);
  }, []);

  const boards = useMemo(() => boardList(report?.boards ?? null), [report]);
  const map = useMemo(
    () => boardMapModel(boards, report?.board_edges ?? null, BOARD_MAP_MAX_NODES),
    [boards, report],
  );
  const artifactOf = useCallback(
    (kind: string): KgArtifactView | null =>
      report?.artifacts.find((item) => item.kind === kind) ?? null,
    [report],
  );
  const histogram = artifactOf(ARTIFACT_HISTOGRAM);
  const convergence = useMemo(() => convergenceTable(histogram?.payload ?? null), [histogram]);
  const largestArtifact = artifactOf(ARTIFACT_LARGEST_CLUSTERS);
  const largest = useMemo(
    () => largestClusters(largestArtifact?.payload ?? null),
    [largestArtifact],
  );
  const provenanceArtifact = artifactOf(ARTIFACT_RELATION_PROVENANCE);
  const provenance = useMemo(
    () => relationProvenance(provenanceArtifact?.payload ?? null),
    [provenanceArtifact],
  );

  return (
    <section
      className="utility-modal"
      role="dialog"
      aria-modal={interactive}
      aria-hidden={!interactive}
      inert={interactive ? undefined : true}
      aria-label="图谱分析"
      style={{ zIndex }}
      onClick={(event) => {
        if (event.currentTarget === event.target) onClose();
      }}
    >
      <FloatingModalCard storageKey="kg-analysis.window" className="utility-modal-card kg-analysis-card">
        {(floating) => (
          <>
            <div className="source-modal-header" {...floating.dragHandleProps}>
              <div>
                <h2>图谱分析</h2>
                <p>
                  判断图谱数据是否可用、合并是否值得复核、主题之间如何连接，以及哪些来源可能偏离主体内容。
                </p>
              </div>
              <div className="kg-analysis-header-actions">
                <button type="button" className="sort-button" onClick={refresh} title="重新读取报告">
                  <RefreshCw size={15} /> 刷新
                </button>
                <button className="icon-button" onClick={onClose} title="Close">×</button>
              </div>
            </div>

            <div className="kg-analysis-body">
              {loading && !report ? (
                <p className="tool-hint">正在读取报告…</p>
              ) : reportError ? (
                <p className="kg-analysis-error" role="alert">{reportError}</p>
              ) : report ? (
                <>
                  <AnalysisReadout
                    report={report}
                    table={convergence}
                    boards={boards}
                    largest={largest}
                    provenance={provenance}
                    reviewCandidate={reviewCandidate}
                    canAnalyze={canAnalyze}
                    analysisRunning={analysisRunning}
                    analysisBlocked={analysisBlocked}
                    onAnalyze={onAnalyze}
                  />
                  <ReportState report={report} />
                  <ArtifactLedger report={report} />
                  <CompositionBlock artifact={histogram} table={convergence} />
                  <ConvergenceBlock artifact={histogram} table={convergence} />
                  <LargestClustersBlock artifact={largestArtifact} data={largest} />
                  <RelationProvenanceBlock artifact={provenanceArtifact} data={provenance} />
                  <BoardsBlock report={report} boards={boards} />
                  <BoardMapBlock
                    report={report}
                    map={map}
                    artifact={artifactOf(ARTIFACT_COMMUNITY_EDGES)}
                  />
                  <SourcesBlock
                    artifact={artifactOf(ARTIFACT_SOURCE_PROFILES)}
                    page={sources}
                    loading={sourcesLoading}
                    error={sourcesError}
                    order={order}
                    onOrder={changeOrder}
                    onOffset={setOffset}
                  />
                </>
              ) : null}
            </div>
          </>
        )}
      </FloatingModalCard>
    </section>
  );
}

// ------------------------------------------------------------ 先给结论与动作

function payloadNumber(payload: Record<string, unknown> | null | undefined, key: string): number {
  const value = payload?.[key];
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function AnalysisReadout({
  report,
  table,
  boards,
  largest,
  provenance,
  reviewCandidate,
  canAnalyze,
  analysisRunning,
  analysisBlocked,
  onAnalyze,
}: {
  report: KgAnalysisReport;
  table: ConvergenceTable;
  boards: ReturnType<typeof boardList>;
  largest: LargestClusters;
  provenance: RelationProvenance;
  reviewCandidate: KgSourceProfileRow | null;
  canAnalyze: boolean;
  analysisRunning: boolean;
  analysisBlocked: boolean;
  onAnalyze?: () => void;
}) {
  const hasIntegrityProblem = !report.ledger_consistent || report.artifacts.some((item) => (
    item.absence === "unexpected"
    || (item.freshness.seq_behind ?? 0) < 0
    || (item.freshness.cluster_seq_behind ?? 0) < 0
  ));
  const hasNoAnalysis = report.ledger_state === "empty";
  const needsUpdate = report.state.dirty || report.artifacts.some((item) => (
    item.present && item.freshness.stale === true
  ));
  const hasUnknownMergeGeneration = report.artifacts.some((item) => (
    item.present && item.freshness.stale === null
  ));
  const readiness = hasIntegrityProblem
    ? {
        tone: "danger",
        title: "暂不可据此下结论",
        text: "报告数据有缺失或版本互相对不上。先更新分析；更新后仍出现红色状态时再排查数据完整性。",
      }
    : hasNoAnalysis
      ? {
          tone: "neutral",
          title: "尚未生成分析",
          text: "当前只有图谱内容，没有质量统计。生成后才会出现合并、主题和来源诊断。",
        }
      : needsUpdate
        ? {
            tone: "warn",
            title: "先更新，再判断",
            text: "图谱内容或合并结果在这份报告之后发生过变化；黄色状态描述的是旧版本。",
          }
        : hasUnknownMergeGeneration
          ? {
              tone: "neutral",
              title: "可用，但有一项无法验证",
              text: "报告与当前图谱内容一致，但旧主题划分没有保存其合并代次，部分合并新鲜度无法核对。这不代表数据已经陈旧，重复更新也不一定消除该提示。",
            }
        : {
            tone: "ok",
            title: "可用于当前判断",
            text: "报告数据齐全，且与当前图谱版本一致。下面的指标可以作为排查线索。",
          };

  const concept = table.rows.find((row) => row.key === "concept");
  const largestRow = largest.rows[0];
  const convergenceText = concept && concept.rate !== null
    ? `概念条目从 ${formatCount(concept.memberRows)} 条收敛为 ${formatCount(concept.clusters)} 个，减少 ${formatPercent(concept.rate)}。${largestRow ? `最大合并组含 ${formatCount(largestRow.members)} 条成员。` : ""}`
    : "尚无可判断的概念合并数据。";

  const communityArtifact = report.artifacts.find((item) => item.kind === ARTIFACT_COMMUNITY_EDGES);
  const crossWeight = payloadNumber(communityArtifact?.payload, "cross_weight");
  const intraWeight = payloadNumber(communityArtifact?.payload, "intra_weight");
  const connectedWeight = crossWeight + intraWeight;
  const crossShare = connectedWeight > 0 ? crossWeight / connectedWeight : null;
  const topologyText = boards.total > 0
    ? `当前分为 ${formatCount(boards.total)} 个主题板块${crossShare === null ? "，尚无可统计的板块间关联。" : `；跨板块关联占全部板块关联的 ${formatPercent(crossShare)}。`}`
    : "尚未形成主题板块，无法判断主题边界与来源归属。";

  const sourceText = reviewCandidate
    ? `当前最先值得复核的是“${reviewCandidate.title || "没有标题的来源"}”：与主体板块的关联度为 ${formatPercent(reviewCandidate.mainstream_share)}。确认它是否偏题、解析不完整或缺少关系。`
    : "生成来源画像后，会按与主体板块的关联度列出优先复核候选；低关联不等于错误。";

  return (
    <div className="kg-analysis-block kg-analysis-readout">
      <div className="kg-analysis-readout-head">
        <div>
          <h3>先看结论</h3>
          <p className="kg-analysis-hint">这些是诊断信号，不是把图谱压成一个“质量总分”。</p>
        </div>
        {canAnalyze && onAnalyze ? (
          <button
            type="button"
            className="sort-button kg-analysis-run-button"
            disabled={analysisRunning || analysisBlocked}
            onClick={onAnalyze}
            title="重算跨文档合并、主题板块和质量统计；不会重新分析来源"
          >
            <RefreshCw size={15} className={analysisRunning ? "busy-spin" : undefined} />
            {analysisRunning ? "正在生成…" : hasNoAnalysis ? "生成分析" : "更新分析"}
          </button>
        ) : null}
      </div>
      <div className="kg-analysis-insight-grid">
        <article className={`kg-analysis-insight kg-analysis-insight--${readiness.tone}`}>
          <span>报告可信度</span>
          <strong>{readiness.title}</strong>
          <p>{readiness.text}</p>
        </article>
        <article className="kg-analysis-insight">
          <span>合并质量信号</span>
          <strong>{concept?.rate === null || !concept ? "暂无数据" : `概念收敛 ${formatPercent(concept.rate)}`}</strong>
          <p>{convergenceText} 收敛率不是越高越好：过高要查误合并，过低要查同义表达是否仍然碎片化。</p>
        </article>
        <article className="kg-analysis-insight">
          <span>主题结构信号</span>
          <strong>{boards.total > 0 ? `${formatCount(boards.total)} 个主题板块` : "尚未形成板块"}</strong>
          <p>{topologyText} 比例高可能是主题交织或边界过松，比例低可能是边界清晰或板块彼此孤立。</p>
        </article>
        <article className="kg-analysis-insight">
          <span>来源复核入口</span>
          <strong>{reviewCandidate ? "已有优先候选" : "等待来源画像"}</strong>
          <p>{sourceText}</p>
        </article>
      </div>
      <div className="kg-analysis-status-guide" aria-label="状态说明">
        <span><i className="kg-analysis-guide-dot is-danger" />红色：数字不可信，先更新或排障</span>
        <span><i className="kg-analysis-guide-dot is-warn" />黄色：旧版本，更新后再判断</span>
        <span><i className="kg-analysis-guide-dot is-neutral" />灰色：尚未生成、无需生成或代次无法验证</span>
        <span><i className="kg-analysis-guide-dot is-ok" />无异常徽标：已生成且版本一致</span>
      </div>
      <p className="kg-analysis-action-note">
        生成或更新分析会在后台重算跨文档合并、主题板块和五份质量数据，不会重新分析来源。
        {analysisBlocked ? " 当前有其它图谱任务进行中，完成后即可操作。" : " 完成后本页会自动刷新。"}
        {!canAnalyze ? " 只读成员可以查看，拥有编辑权限的成员可以生成或更新。" : ""}
      </p>
      {provenance.counted > 0 ? (
        <p className="kg-analysis-action-note">
          当前可用关联中有 {formatPercent(provenance.relinkShare)} 来自自动补连；这说明关联覆盖方式，
          不代表这些关联天然更好或更差。
        </p>
      ) : null}
    </div>
  );
}

// ------------------------------------------------------------ 报告口径与新鲜度

function ReportState({ report }: { report: KgAnalysisReport }) {
  const head = reportHeadline(report);
  const state = report.state;
  const rebuild = state.last_rebuild;
  return (
    <div className="kg-analysis-block kg-analysis-state">
      <div className="kg-analysis-block-head">
        <div className="kg-analysis-block-title">
          <h3>报告口径与新鲜度</h3>
          <AnomalyRow anomalies={analysisLedgerAnomalies(report)} />
        </div>
        <p className="kg-analysis-hint">
          同一份报告里的数字来自不同口径、也可能建于不同时刻。下面每一块都会重复标注自己的那一份，
          不要跨块相除。
        </p>
      </div>
      <div className="kg-analysis-state-grid">
        <div className="kg-analysis-stat">
          <span className="kg-analysis-stat-key">数据齐全度</span>
          <span className="kg-analysis-stat-value">{head.ledgerState}</span>
        </div>
        <div className="kg-analysis-stat">
          <span className="kg-analysis-stat-key">当前内容版本</span>
          <span className="kg-analysis-stat-value">
            #{formatCount(state.kg_mutation_seq)}
            <span className="kg-analysis-stat-sub">{state.dirty ? "有改动待整理" : "没有待整理的改动"}</span>
          </span>
        </div>
        <div className="kg-analysis-stat">
          <span className="kg-analysis-stat-key">主题板块划分</span>
          <span className="kg-analysis-stat-value">
            {head.boards.built}
            <span className="kg-analysis-stat-sub">{head.boards.behind || head.boards.basis}</span>
          </span>
        </div>
        <div className="kg-analysis-stat">
          <span className="kg-analysis-stat-key">合并结果版本</span>
          <span className="kg-analysis-stat-value">#{formatCount(state.cluster_mutation_seq)}</span>
        </div>
        <div className="kg-analysis-stat">
          <span className="kg-analysis-stat-key">合并后关联版本</span>
          <span className="kg-analysis-stat-value">#{formatCount(state.canonical_rel_seq)}</span>
        </div>
        <div className="kg-analysis-stat">
          <span className="kg-analysis-stat-key">上次整理</span>
          <span className="kg-analysis-stat-value">
            {rebuild.at || "从未整理"}
            <span className="kg-analysis-stat-sub">{basisLabel(rebuild.basis)}</span>
          </span>
        </div>
      </div>
      <p className="kg-analysis-note">
        {state.present
          ? `上次整理时的规模：${countText(rebuild.object_count, rebuild.units, "object_count")} · ${countText(rebuild.relation_count, rebuild.units, "relation_count")} · ${countText(rebuild.cluster_count, rebuild.units, "cluster_count")}。`
          : "这个知识库还没有任何整理记录，下面每一格都在说「没有」，而不是「是 0」。"}
      </p>
    </div>
  );
}

// ------------------------------------------------------------------ 数据清单

/**
 * 五份数据的在场情况。**恒五行**——缺席的那几份也在列表里,否则「少了一份数据」会
 * 表现成「少了一张卡片、没有任何提示」。
 */
function ArtifactLedger({ report }: { report: KgAnalysisReport }) {
  return (
    <div className="kg-analysis-block">
      <div className="kg-analysis-block-head">
        <div className="kg-analysis-block-title">
          <h3>本报告用到的数据</h3>
        </div>
        <p className="kg-analysis-hint">
          这五份数据由“生成/更新分析”统一产出。每一行说明它回答的问题以及当前是否可用。
        </p>
      </div>
      <ul className="kg-analysis-ledger">
        {report.artifacts.map((artifact) => (
          <li key={artifact.kind} className="kg-analysis-ledger-row">
            <span className="kg-analysis-ledger-name">
              <strong>{artifactLabel(artifact.kind)}</strong>
              <small>{artifactPurpose(artifact.kind)}</small>
            </span>
            <FreshnessLine note={freshnessNote(artifact.freshness)} />
            <AnomalyRow anomalies={analysisArtifactAnomalies(artifact)} />
          </li>
        ))}
      </ul>
    </div>
  );
}

// -------------------------------------------------------------- A1 对象构成

function CompositionBlock({
  artifact,
  table,
}: {
  artifact: KgArtifactView | null;
  table: ConvergenceTable;
}) {
  const note = freshnessNote(artifact?.freshness ?? null);
  const anomalies = artifact ? analysisArtifactAnomalies(artifact) : [];
  const unit = unitFor(artifact?.units ?? null, "member_rows");
  return (
    <div className="kg-analysis-block">
      <BlockHead
        title="对象构成"
        note={note}
        anomalies={anomalies}
        hint={`按对象类型分列，计数单位是「${unit}」——合并之前的条目数。`}
      />
      {!artifact || !artifact.present ? (
        <ArtifactAbsence absence={artifact?.absence ?? null} />
      ) : table.total.memberRows === 0 ? (
        <EmptyPayload text="这份数据已经生成，但当前没有任何可统计的条目。" />
      ) : (
        <>
          <div className="kg-analysis-bar" role="img" aria-label="对象类型构成">
            {table.rows.map((row) => (
              <span
                key={row.key}
                className={`kg-analysis-seg kg-analysis-seg-${row.key}`}
                aria-hidden="true"
                style={{ width: `${((row.share ?? 0) * 100).toFixed(3)}%` }}
              />
            ))}
          </div>
          <ul className="kg-analysis-legend">
            {table.rows.map((row) => (
              <li key={row.key}>
                <span className={`kg-analysis-dot kg-analysis-seg-${row.key}`} aria-hidden="true" />
                <span className="kg-analysis-legend-name">{row.label}</span>
                <span className="kg-analysis-legend-value">
                  {countText(row.memberRows, artifact.units, "member_rows")}
                </span>
                <span className="kg-analysis-legend-share">{formatPercent(row.share)}</span>
                {row.key === "other" && row.objectTypes > 0 ? (
                  <span className="kg-analysis-legend-extra">
                    {countText(row.objectTypes, artifact.units, "object_types")}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
          <p className="kg-analysis-note">
            合计 {countText(table.total.memberRows, artifact.units, "member_rows")}；
            另有 {countText(table.excludedMemberRows, artifact.units, "excluded_member_rows")}
            因所属对象当前不可用而未计入。
          </p>
        </>
      )}
    </div>
  );
}

// -------------------------------------------------------------- A2 收敛率

function ConvergenceBlock({
  artifact,
  table,
}: {
  artifact: KgArtifactView | null;
  table: ConvergenceTable;
}) {
  const note = freshnessNote(artifact?.freshness ?? null);
  const anomalies = artifact ? analysisArtifactAnomalies(artifact) : [];
  const memberUnit = unitFor(artifact?.units ?? null, "member_rows");
  const clusterUnit = unitFor(artifact?.units ?? null, "clusters");
  return (
    <div className="kg-analysis-block">
      <BlockHead
        title="合并收敛率"
        note={note}
        anomalies={anomalies}
        hint="按对象类型分列。混着算会被占比最大的那一类稀释，读出来的收敛率会显著偏低。"
      />
      {!artifact || !artifact.present ? (
        <ArtifactAbsence absence={artifact?.absence ?? null} />
      ) : table.total.memberRows === 0 ? (
        <EmptyPayload text="这份数据已经生成，但当前没有任何可统计的条目。" />
      ) : (
        <>
          <table className="kg-analysis-table">
            <thead>
              <tr>
                <th scope="col">类型</th>
                <th scope="col">{memberUnit}</th>
                <th scope="col">{clusterUnit}</th>
                <th scope="col">收敛率</th>
              </tr>
            </thead>
            <tbody>
              {table.rows.map((row) => (
                <tr key={row.key}>
                  <th scope="row">{row.label}</th>
                  <td>{formatCount(row.memberRows)}</td>
                  <td>{formatCount(row.clusters)}</td>
                  <td>{formatPercent(row.rate)}</td>
                </tr>
              ))}
              <tr className="kg-analysis-table-total">
                <th scope="row">{table.total.label}</th>
                <td>{formatCount(table.total.memberRows)}</td>
                <td>{formatCount(table.total.clusters)}</td>
                <td>{formatPercent(table.total.rate)}</td>
              </tr>
            </tbody>
          </table>
          <p className="kg-analysis-note">
            收敛率 =（{memberUnit} − {clusterUnit}）÷ {memberUnit}。
            其中 {countText(table.emptyClusters, artifact.units, "empty_clusters")}
            的成员已全部不可用，对应
            {countText(table.emptyClusterMemberRows, artifact.units, "empty_cluster_member_rows")}
            ，未计入上表。
          </p>
        </>
      )}
    </div>
  );
}

// ------------------------------------------------------- 最大合并组 / 关联出处

function LargestClustersBlock({
  artifact,
  data,
}: {
  artifact: KgArtifactView | null;
  data: LargestClusters;
}) {
  const note = freshnessNote(artifact?.freshness ?? null);
  const anomalies = artifact ? analysisArtifactAnomalies(artifact) : [];
  return (
    <div className="kg-analysis-block">
      <BlockHead
        title="需要复核的大型合并组"
        note={note}
        anomalies={anomalies}
        hint="成员很多不一定是错误，但最容易把不同概念误并在一起；优先检查榜首是否仍表达同一个概念。"
      />
      {!artifact || !artifact.present ? (
        <ArtifactAbsence absence={artifact?.absence ?? null} />
      ) : data.rows.length === 0 ? (
        <EmptyPayload text="这份数据已经生成，但当前没有概念合并组可供复核。" />
      ) : (
        <>
          <ol className="kg-analysis-large-clusters">
            {data.rows.map((row, index) => (
              <li key={row.id || `${row.name}-${index}`}>
                <span>#{index + 1}</span>
                <strong title={row.name}>{row.name || "（没有名称）"}</strong>
                <span>{countText(row.members, artifact.units, "members")}</span>
              </li>
            ))}
          </ol>
          <p className="kg-analysis-note">
            这里展示成员最多的概念合并组{data.truncated ? `，只保留前 ${formatCount(data.limit)} 个` : "，已全部列出"}。
            结论应来自对成员语义的复核，不能只根据组大小自动判定误合并。
          </p>
        </>
      )}
    </div>
  );
}

function RelationProvenanceBlock({
  artifact,
  data,
}: {
  artifact: KgArtifactView | null;
  data: RelationProvenance;
}) {
  const note = freshnessNote(artifact?.freshness ?? null);
  const anomalies = artifact ? analysisArtifactAnomalies(artifact) : [];
  return (
    <div className="kg-analysis-block">
      <BlockHead
        title="关联是怎样形成的"
        note={note}
        anomalies={anomalies}
        hint="它说明关联覆盖依赖原始内容还是后续自动补连，不是“自动越少越好”的质量评分。"
      />
      {!artifact || !artifact.present ? (
        <ArtifactAbsence absence={artifact?.absence ?? null} />
      ) : data.totalRows === 0 ? (
        <EmptyPayload text="这份数据已经生成，但当前没有任何关联可统计。" />
      ) : (
        <>
          <table className="kg-analysis-table">
            <thead>
              <tr>
                <th scope="col">形成方式</th>
                <th scope="col">{unitFor(artifact.units, "counted")}</th>
                <th scope="col">占可用关联</th>
              </tr>
            </thead>
            <tbody>
              {data.rows.map((row) => (
                <tr key={row.key}>
                  <th scope="row">{row.label}</th>
                  <td>{formatCount(row.count)}</td>
                  <td>{formatPercent(row.share)}</td>
                </tr>
              ))}
              <tr className="kg-analysis-table-total">
                <th scope="row">可用关联合计</th>
                <td>{formatCount(data.counted)}</td>
                <td>{data.counted > 0 ? "100%" : "—"}</td>
              </tr>
            </tbody>
          </table>
          <p className="kg-analysis-note">
            自动补连占 {formatPercent(data.relinkShare)}。另有
            {" "}{countText(data.rejected, artifact.units, "rejected")}已被拒绝、
            {countText(data.endpointUnusable, artifact.units, "endpoint_unusable")}因端点不可用而未进入图谱。
            如果自动补连占比很高，应检查来源分析是否遗漏了关系；占比低也不代表关系覆盖一定完整。
          </p>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------- C1 主题板块列表

function BoardsBlock({
  report,
  boards,
}: {
  report: KgAnalysisReport;
  boards: ReturnType<typeof boardList>;
}) {
  const note = freshnessNote(report.boards.freshness);
  const units = report.boards.units;
  return (
    <div className="kg-analysis-block">
      <BlockHead
        title="主题板块"
        note={note}
        // 这一块与「本报告用到的数据」「板块俯瞰图」读的是同一次社区划分,陈旧时必须挂
        // 同一档徽标。设计 §3.3 记的那次真实事故(据 88 580 个板块推出「图散成一地」,
        // 随后才得知库未整理)说的正是这一块数据 —— 别的块有黄标而它只有一行灰色小字,
        // 读者会把「没有标」读成「这块没问题」。
        // `communities` 表是社区划分本身,不是账本产物,所以 present 恒为 true;
        // 从没建过社区(community_seq < 0)时 freshness 三个字段都是 null,分档函数
        // 返回空数组,由下面的 EmptyPayload 说明。
        anomalies={analysisArtifactAnomalies({ present: true, freshness: report.boards.freshness })}
        hint="板块由跨来源的关联自动聚成，规模是它包含的合并后的知识对象数。"
      />
      {boards.total === 0 ? (
        <EmptyPayload text="这个知识库还没有分出任何主题板块。" />
      ) : (
        <>
          <p className="kg-analysis-note">
            共 {countText(boards.total, units, "total")}，本次列出
            {" "}{countText(boards.returned, units, "returned")}
            {boards.truncated
              ? `（列表上限 ${countText(boards.limit, units, "limit")}，还有 ${formatCount(boards.unreturned)} 个未列出）`
              : "（已全部列出）"}
            。每个板块最多列 {countText(boards.topMembersLimit, units, "top_members_limit")}做代表。
          </p>
          <ol className="kg-analysis-boards">
            {boards.rows.map((row) => (
              <li key={row.id} className="kg-analysis-board-row">
                <span className="kg-analysis-board-rank">#{row.rank}</span>
                <span className="kg-analysis-board-size">{countText(row.size, units, "size")}</span>
                <span className="kg-analysis-board-members" title={row.members.join("、")}>
                  {row.members.length > 0 ? row.members.join("、") : "没有可展示的代表对象"}
                  {row.membersTruncated ? "…" : ""}
                </span>
              </li>
            ))}
          </ol>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------- E1 板块俯瞰图

function BoardMapBlock({
  report,
  map,
  artifact,
}: {
  report: KgAnalysisReport;
  map: ReturnType<typeof boardMapModel>;
  artifact: KgArtifactView | null;
}) {
  const note = freshnessNote(report.board_edges.freshness);
  const anomalies = artifact ? analysisArtifactAnomalies(artifact) : [];
  const boardUnits = report.boards.units;
  const edgeUnits = report.board_edges.units;
  return (
    <div className="kg-analysis-block">
      <BlockHead
        title="板块俯瞰图"
        note={note}
        anomalies={anomalies}
        hint="一个板块一个节点，大小按它的规模；连线是板块之间的关联，粗细按关联强度。"
      />
      {map.total === 0 ? (
        <EmptyPayload text="这个知识库还没有分出任何主题板块，画不出这张图。" />
      ) : (
        <>
          <svg
            className="kg-analysis-map"
            viewBox={`0 0 ${BOARD_MAP_VIEW.width} ${BOARD_MAP_VIEW.height}`}
            role="img"
            aria-label={`板块俯瞰图，单独画出 ${map.drawn} 个板块`}
          >
            {map.edges.map((edge) => (
              <line
                key={edge.key}
                className="kg-analysis-map-edge"
                x1={edge.x1}
                y1={edge.y1}
                x2={edge.x2}
                y2={edge.y2}
                strokeWidth={edge.width}
              />
            ))}
            {map.nodes.map((node) => (
              <g key={node.key} className={`kg-analysis-map-node kg-analysis-map-${node.kind}`}>
                <circle cx={node.x} cy={node.y} r={node.r} />
                <title>{`${node.label} · ${countText(node.size, boardUnits, "size")}`}</title>
              </g>
            ))}
          </svg>
          <ul className="kg-analysis-coverage">
            <li>
              单独画出 {formatCount(map.drawn)} 个，占全部
              {" "}{countText(map.total, boardUnits, "total")}的 {formatPercent(map.boardCoverage)}
              （本图上限 {formatCount(BOARD_MAP_MAX_NODES)} 个）。
            </li>
            {map.tailBoards > 0 ? (
              <li>
                另有 {formatCount(map.tailBoards)} 个已取到规模的板块并成了中间那个汇总节点，
                合计 {countText(map.tailMembers, boardUnits, "size")}。
              </li>
            ) : null}
            {map.unreturned > 0 ? (
              <li>
                还有 {formatCount(map.unreturned)} 个板块本次没有取回，它们的规模未知，
                <strong>不在</strong>那个汇总节点里。
              </li>
            ) : null}
            {!report.board_edges.present ? (
              <li>
                <ArtifactAbsence absence={artifact?.absence ?? null} />
              </li>
            ) : (
              <>
                <li>
                  连线：本图画出 {formatCount(map.drawnEdges)} 条（两端都在图上的那些），
                  本次取回 {countText(map.returnedEdges, edgeUnits, "returned")}
                  （单次上限 {formatCount(map.requestLimit)}）。
                </li>
                <li>
                  库内存有 {countText(map.storedEdges, edgeUnits, "stored")}
                  {map.storedTruncated
                    ? `，是从 ${formatCount(map.storedTotalEdges)} 个板块对里按关联强度截到上限 ${formatCount(map.edgeLimit)} 的结果`
                    : `，未触及落库上限 ${formatCount(map.edgeLimit)}`}
                  。
                </li>
                <li>
                  本次取回的关联强度，占全部板块间关联强度的 {formatPercent(map.weightCoverage)}
                  {map.weightCoverage === null ? "（一条板块间关联都没有）" : ""}。
                </li>
              </>
            )}
          </ul>
        </>
      )}
    </div>
  );
}

// -------------------------------------------------------- C2 关联稀疏的来源

function SourcesBlock({
  artifact,
  page,
  loading,
  error,
  order,
  onOrder,
  onOffset,
}: {
  artifact: KgArtifactView | null;
  page: KgSourceProfilePage | null;
  loading: boolean;
  error: string;
  order: KgSourceOrder;
  onOrder: (next: KgSourceOrder) => void;
  onOffset: (next: number) => void;
}) {
  const note = freshnessNote(page?.freshness ?? artifact?.freshness ?? null);
  const units = page?.units ?? null;
  // ⚠ 按钮标的是**屏上这批行**的顺序,不是控件选的那个。切排序只重取 `/sources`,请求在
  // 飞的那段时间 `page` 还是上一次的 —— 拿控件值去标,旧的行当场就被写上新选的顺序,而
  // 那正是本视图存在理由的反面(「每个数字都要说得出自己的口径」)。分页那一半天然已经
  // 这样了(页码/范围/上下页全读 `page.offset` / `page.limit` / `page.has_more`),这里
  // 只是把排序补齐。一页都还没有时才落回控件值 —— 那时没有任何数据会被误标。
  //
  // 另一条路(请求一开始就清掉 `page`)会闪一下加载态,而且把「上一次的结果还看得见」
  // 这个好处也一起丢了;标注跟着数据走则两者都留得住。代价是点下去按钮不当场亮 ——
  // 所以旁边补一条在飞提示,让这次点击有反馈。
  const shownOrder = page?.order ?? order;
  return (
    <div className="kg-analysis-block">
      <BlockHead
        title="关联稀疏的来源"
        note={note}
        anomalies={page ? analysisArtifactAnomalies(page) : []}
        hint="排在前面的来源，其内容与这个知识库的主体板块几乎不连通——最可能是不属于这里的语料。"
      />
      <div className="kg-analysis-order">
        {(["sparse", "connected"] as KgSourceOrder[]).map((value) => (
          <button
            key={value}
            type="button"
            className={`sort-button${shownOrder === value ? " active" : ""}`}
            aria-pressed={shownOrder === value}
            onClick={() => onOrder(value)}
          >
            {label(SOURCE_ORDER_LABEL, value, "默认顺序")}
          </button>
        ))}
        {loading && page ? (
          <p className="kg-analysis-hint kg-analysis-order-status" aria-live="polite">
            正在读取…下面还是上一次的结果
          </p>
        ) : null}
      </div>
      <MainstreamBasis artifact={artifact} page={page} />
      {error ? (
        <p className="kg-analysis-error" role="alert">{error}</p>
      ) : loading && !page ? (
        <p className="tool-hint">正在读取来源清单…</p>
      ) : !page ? null : !page.present ? (
        <ArtifactAbsence absence={page.absence} />
      ) : page.total === 0 ? (
        <EmptyPayload text="这份数据已经生成，但当前没有任何来源画像可看。" />
      ) : (
        <>
          <div className="kg-analysis-table-scroll">
            <table className="kg-analysis-table kg-analysis-sources">
              <thead>
                <tr>
                  <th scope="col">来源</th>
                  <th scope="col">进入板块的{unitFor(units, "n_graph_objects")}</th>
                  <th scope="col">全部{unitFor(units, "n_objects")}</th>
                  <th scope="col">与主体板块的关联度</th>
                  <th scope="col">最集中板块占比</th>
                  <th scope="col">散布板块数</th>
                </tr>
              </thead>
              <tbody>
                {page.rows.map((row) => (
                  <tr key={row.source_id}>
                    <th scope="row">
                      <span className="kg-analysis-source-title" title={row.title || row.source_id}>
                        {row.title || "（没有标题）"}
                      </span>
                      <AnomalyRow anomalies={analysisSourceRowAnomalies(row)} />
                    </th>
                    <td>{formatCount(row.n_graph_objects)}</td>
                    <td>{formatCount(row.n_objects)}</td>
                    <td>{formatPercent(row.mainstream_share)}</td>
                    <td>{formatPercent(row.top_share)}</td>
                    <td>{formatCount(row.community_spread)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="kg-analysis-pager">
            <span className="kg-analysis-pager-text">
              第 {formatCount(page.offset + 1)}–{formatCount(page.offset + page.returned)} 个，
              共 {countText(page.total, units, "total")}（每页最多 {formatCount(page.limit)} 个）
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
        </>
      )}
    </div>
  );
}

/**
 * 「主体板块」的口径 —— 读**总览**里那份产物的账本载荷与它的单位表。
 *
 * ⚠ 不读 `/sources` 响应的 summary:那一页的 `units` 只覆盖行级字段
 * (n_objects / n_graph_objects / top_share …),不含 head_communities / head_members /
 * total_members。拿不到单位就自己编一个,正是这个视图要杜绝的事——head_members 是
 * **合并后的知识对象**计数,与来源表里的对象计数不可互比。
 *
 * ⚠⚠ 正因为它读的是**总览**那一轮,而这个块的其余部分(表格、缺席说明、分页器)读的是
 * `/sources` 那一轮 —— 翻页/换排序只重取 `/sources`,总览不重取 —— 它必须做两件事:
 *
 *   1. **门控在这一页的在场性上**。不门控就会出现上下相邻两行互相打脸的形状:
 *      总览建于变更 #100(head_communities=12)后台跑了一次整理,库变成零板块、
 *      来源画像合法缺席(#140),用户点「下一页」——上面一行写「共 12 主题板块,
 *      含 900 / 1,500 合并后的知识对象」(旧),紧挨着的下一行写「这个知识库一个主题
 *      板块都没有……因此没有生成。」(新)。而块头的新鲜度行用的是新的那一轮,
 *      没有任何东西标出上面那段来自旧的一轮。
 *   2. **自己带一条新鲜度行**。设计 §3.3 的硬要求是逐**指标**标注:这段数字与同块
 *      表格来自两轮不同的请求,靠块头那一条戳不住它。
 */
function MainstreamBasis({
  artifact,
  page,
}: {
  artifact: KgArtifactView | null;
  page: KgSourceProfilePage | null;
}) {
  if (!page || !page.present) return null;
  if (!artifact || !artifact.present || !artifact.payload) return null;
  const payload = artifact.payload;
  const coverage = typeof payload.mainstream_coverage === "number" ? payload.mainstream_coverage : null;
  const headBoards = typeof payload.head_communities === "number" ? payload.head_communities : null;
  const headMembers = typeof payload.head_members === "number" ? payload.head_members : null;
  const totalMembers = typeof payload.total_members === "number" ? payload.total_members : null;
  if (coverage === null || headBoards === null) return null;
  return (
    <p className="kg-analysis-note">
      主体板块 = 按规模排序、累计覆盖 {formatPercent(coverage)} 成员的头部板块，
      共 {countText(headBoards, artifact.units, "head_communities")}，
      含 {countText(headMembers ?? 0, artifact.units, "head_members")}
      {" / "}
      {countText(totalMembers ?? 0, artifact.units, "total_members")}。
      {" "}
      <FreshnessLine note={freshnessNote(artifact.freshness)} />
    </p>
  );
}
