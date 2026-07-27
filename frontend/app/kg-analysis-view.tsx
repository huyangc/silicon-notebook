/**
 * kg-analysis-view.tsx
 *
 * 「图谱分析」只读视图(批 1:A1 对象构成 / A2 收敛率 / C1 主题板块 / C2 关联稀疏的来源 /
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
 * 边界:这是诊断工具,不是产品引导。职责到「如实标注」为止——不折叠、不置灰、不催促
 * 整理,也不替读者判断该不该信这些数字。
 */
"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
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
} from "./kg-analysis-api";
import {
  artifactLabel,
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
  reportHeadline,
  SOURCE_ORDER_LABEL,
  unitFor,
  type ConvergenceTable,
  type FreshnessNote,
} from "./kg-analysis-model";
import { label } from "./vocabulary";

const SOURCE_PAGE_SIZE = 20;
const BOARD_LIST_LIMIT = 50;
const BOARD_EDGE_LIMIT = 200;
const BOARD_TOP_MEMBERS = 5;

const ARTIFACT_HISTOGRAM = "cluster_size_histogram";
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
}: {
  notebookId: string;
  onClose: () => void;
}) {
  const [report, setReport] = useState<KgAnalysisReport | null>(null);
  const [reportError, setReportError] = useState("");
  const [loading, setLoading] = useState(true);
  const [reloadToken, setReloadToken] = useState(0);

  const [sources, setSources] = useState<KgSourceProfilePage | null>(null);
  const [sourcesError, setSourcesError] = useState("");
  const [sourcesLoading, setSourcesLoading] = useState(true);
  const [order, setOrder] = useState<KgSourceOrder>("sparse");
  const [offset, setOffset] = useState(0);

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

  return (
    <section
      className="utility-modal"
      role="dialog"
      aria-modal="true"
      aria-label="图谱分析"
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
                  只读诊断报告：知识对象的构成与合并收敛、主题板块的分布与关联，以及与主体板块关联稀疏的来源。
                  每一块数据都标注了它的口径、建于哪次变更、落后多少。
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
                  <ReportState report={report} />
                  <ArtifactLedger report={report} />
                  <CompositionBlock artifact={histogram} table={convergence} />
                  <ConvergenceBlock artifact={histogram} table={convergence} />
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
          每一份都单独记录了它建于哪次变更。缺席的那几份也列在这里，并写明为什么缺。
        </p>
      </div>
      <ul className="kg-analysis-ledger">
        {report.artifacts.map((artifact) => (
          <li key={artifact.kind} className="kg-analysis-ledger-row">
            <span className="kg-analysis-ledger-name">{artifactLabel(artifact.kind)}</span>
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
            className={`sort-button${order === value ? " active" : ""}`}
            aria-pressed={order === value}
            onClick={() => onOrder(value)}
          >
            {label(SOURCE_ORDER_LABEL, value, "默认顺序")}
          </button>
        ))}
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
