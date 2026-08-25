// 异常提示分级 — 唯一分类逻辑入口(anomaly-tiers spec)。
//
// 判据是「后果」不是「原因」:
//   integrity — 存下来的知识错/缺/不一致,可能给用户错误或悄悄不完整的答案,
//               多数不能自愈。红字 + 红底。
//   retrieval — 知识完好,只是检索没到满血,答案偏弱不算错,通常一个动作可修。
//               黄标 + 三角感叹号。
//   info      — 进行中/预期态/无需处理,不是异常。中性灰,禁用红黄。
//
// 渲染只走 AnomalyBadge(anomaly-badge.tsx)+ .anomaly-badge 系列 class
// (globals.css)。新增异常小字必须先在这里加一条映射,不要在调用处手搓样式
// 或散落的内联 hex/⚠字符。
//
// 文案出处红线:extraction_warning 用后端返回原文(勿改写);其余 label/
// tooltip 是前端静态文案,集中在下面这批常量,不内联散落到 page.tsx。

export type AnomalySeverity = "integrity" | "retrieval" | "info";

export interface Anomaly {
  severity: AnomalySeverity;
  label: string;
  tooltip?: string;
}

const PARSE_FAILED_LABEL = "解析失败";
const PARSE_FAILED_TOOLTIP = "这个来源没能解析成功，可删除后重新上传。";

const EXTRACTION_WARNING_LABEL = "部分内容未分析";
// tooltip 用后端返回的 extraction_warning 原文,不在此处硬编码。

const PARSE_QUALITY_WARNING_LABEL = "降级解析";
const PARSE_QUALITY_WARNING_TOOLTIP = "MinerU 暂时不可用，已改用本地解析器；版面、公式、表格或扫描内容可能不完整。可重新解析，不满意可删除来源。";

const INDEXING_CHUNK_FALLBACK_LABEL = "降级整理";
const INDEXING_CHUNK_FALLBACK_TOOLTIP =
  "这份来源没能按所选索引管线整理，已改用内建方式；它的检索效果可能与库内其它来源不一致。可重新解析重试。";

const PAPER_META_MISSING_LABEL = "待补全";
const PAPER_META_MISSING_TOOLTIP = "论文作者/机构等信息尚未补全";

const PAPER_META_NOT_PAPER_LABEL = "非论文";
const PAPER_META_NOT_PAPER_TOOLTIP = "该来源非学术论文，无需补全";

// —— 图谱分析报告的产物异常(kg-analysis-view spec §3.3)————————————————
// 分档判据同上,按「后果」不按「原因」:
//   · 账本非空却少了一份产物 / 在场产物不是同一轮算的 / 产物比当前库还新
//     —— 报告里的数字互相不可比,读者据此下的结论会是错的 → integrity。
//   · 产物落后当前若干次变更 —— 数字本身没算错,只是描述的是更早的库状态,
//     重新整理一次即可对齐 → retrieval。
//   · 从没算过 / 合法缺席(零板块的库不出来源画像)—— 预期态,不是异常 → info。
// 这一档的存在理由见设计 §3.3 的真实教训:有人拿一份陈旧产物推出了关于图结构的
// 重大结论,随后才得知该库尚未整理,整个推断作废。所以陈旧必须**逐份**标注出来,
// 而不是在报告顶部挂一条横幅。
const ARTIFACT_MISSING_LABEL = "本该有却缺失";
const ARTIFACT_MISSING_TOOLTIP = "同一轮里其它数据都在，唯独这一份没写下来。这一格的数字暂时无法与同屏其它数字互相印证。";

const ARTIFACT_NEVER_LABEL = "尚未生成";
const ARTIFACT_NEVER_TOOLTIP = "这个知识库还没算过这份数据。它不是「算出来是 0」，是根本没算过。";

const ARTIFACT_EXPECTED_LABEL = "本次无需生成";
const ARTIFACT_EXPECTED_TOOLTIP = "一个主题板块都没有时，这份数据没有可说的内容，因此刻意不生成——而不是写一张全 0 的表。";

const ARTIFACT_STALE_LABEL = "落后于当前内容";
const ARTIFACT_STALE_TOOLTIP = "这份数字描述的是更早的库内容，之后又有过改动。重新合并一次即可对齐。";

const ARTIFACT_AHEAD_LABEL = "比当前内容还新";
const ARTIFACT_AHEAD_TOOLTIP = "这份数字标记的版本比当前库还新，正常写入不会产生这种状态。";

// 在场,却**说不出**自己建在哪一次合并结果上。依赖主题板块的两份数据由「只补账本」
// 那条路径产出时就是这一档:板块划分沿用库里现成的(它建在哪一次合并上没有地方记),
// 而边与来源映射是按当前的合并结果现算的 —— 两截来自不同时候。数字本身没算错,
// 重新整理一次即可对齐,所以与「落后于当前内容」同档(retrieval)。
// ⚠ 它**不是**「无异常」:显示成「与当前一致」正是这个视图存在理由的反面。
const ARTIFACT_UNKNOWN_MERGE_LABEL = "对不上合并进度";
const ARTIFACT_UNKNOWN_MERGE_TOOLTIP = "这份数字里的主题板块沿用了上一次划分，无法判断它建在哪一次合并上。重新整理一次即可对齐。";

/**
 * 一份预计算产物(或它的缺席)要展示的异常小字。
 *
 * 入参刻意是**结构形状**而不是具体响应类型:总览里的五份产物、跨板块关联、
 * 来源画像页三处形状不同的响应都能直接喂进来,分档规则因此只有这一份。
 *
 * ``absence`` 的三个取值是后端内部代号(never_computed / expected / unexpected),
 * 界面词在上面的常量里;两个 ``*_behind`` 都不 clamp,负值单独成一档(见 ARTIFACT_AHEAD)。
 *
 * ⚠ **两条世代线必须一起判**:产物有 ``kg_mutation_seq`` 与 ``cluster_mutation_seq``
 * 两个戳,任何一个「比当前还新」都说明库被手工改过(例如恢复/重置了 state 行却留着
 * 产物),那是 integrity 档。只看其中一条会让另一条的损坏落进 ``stale`` 分支,把
 * 「数字不可信」显示成「重新合并一次即可对齐」—— 语义正好反转。
 * 这个洞真实发生过:第 5 轮加簇世代时只加了响应字段、没接进这里,而第 1 轮为
 * ``seq_behind`` 配的那 9 条 severity 断言全绿,因为它们只覆盖第一条世代线
 * (codex 第 6 轮评审)。新增第三条世代线时,这里同样要跟着改。
 */
export function analysisArtifactAnomalies(artifact: {
  present: boolean;
  absence?: string | null;
  freshness?: {
    built_at_seq?: number | null;
    seq_behind?: number | null;
    cluster_seq_behind?: number | null;
    stale?: boolean | null;
  } | null;
}): Anomaly[] {
  if (!artifact.present) {
    if (artifact.absence === "expected") {
      return [{ severity: "info", label: ARTIFACT_EXPECTED_LABEL, tooltip: ARTIFACT_EXPECTED_TOOLTIP }];
    }
    if (artifact.absence === "unexpected") {
      return [{ severity: "integrity", label: ARTIFACT_MISSING_LABEL, tooltip: ARTIFACT_MISSING_TOOLTIP }];
    }
    return [{ severity: "info", label: ARTIFACT_NEVER_LABEL, tooltip: ARTIFACT_NEVER_TOOLTIP }];
  }
  const behinds = [
    artifact.freshness?.seq_behind ?? null,
    artifact.freshness?.cluster_seq_behind ?? null,
  ];
  if (behinds.some((behind) => behind !== null && behind < 0)) {
    return [{ severity: "integrity", label: ARTIFACT_AHEAD_LABEL, tooltip: ARTIFACT_AHEAD_TOOLTIP }];
  }
  if (artifact.freshness?.stale) {
    return [{ severity: "retrieval", label: ARTIFACT_STALE_LABEL, tooltip: ARTIFACT_STALE_TOOLTIP }];
  }
  // ⚠ `stale` 是**三值**的:null = 报不出世代。少了这一条,一份混合世代的数据会一个
  // 小字都不带地显示成正常 —— 后端第 7 轮刚把「盖成当前」修掉,前端再把它显示成
  // 「无异常」等于原地复发。
  //
  // ⚠ **必须同时看 `built_at_seq`**:主题板块那一块传的是 `present: true` + 板块表
  // 自己的新鲜度,而「从没建过板块」时那份 freshness 三个字段全是 null(见
  // kg-analysis-view.tsx 里那段说明)—— 那一档由块内的空态文案负责解释,挂一条
  // 「对不上合并进度」是在给一个还没建过的东西报异常。建过(`built_at_seq` 是数字)
  // 而 `stale` 为 null,才是「板块划分沿用了上一次、合并世代无从判断」那一档。
  const builtAt = artifact.freshness?.built_at_seq ?? null;
  if (artifact.freshness?.stale === null && builtAt !== null) {
    return [{
      severity: "retrieval",
      label: ARTIFACT_UNKNOWN_MERGE_LABEL,
      tooltip: ARTIFACT_UNKNOWN_MERGE_TOOLTIP,
    }];
  }
  return [];
}

const LEDGER_MIXED_LABEL = "数字口径不一致";
const LEDGER_MIXED_TOOLTIP = "这份报告里的数据不是同一轮算出来的，彼此之间不可直接相除或比较。";

/** 在场的产物不是同一轮算的 —— 整份报告级别的异常(正常写入不可能产生)。 */
export function analysisLedgerAnomalies(report: { ledger_consistent: boolean }): Anomaly[] {
  if (report.ledger_consistent) return [];
  return [{ severity: "integrity", label: LEDGER_MIXED_LABEL, tooltip: LEDGER_MIXED_TOOLTIP }];
}

const SOURCE_GONE_LABEL = "来源已不存在";
// ⚠ 这条文案**不得**承诺「重新整理就会消失」(codex 第 9 轮 P2)。这一行留在报告里是
// **刻意**的:后端那次扫描专门保留了指向已删来源的孤儿引用(不写成 `JOIN sources`),
// 为的就是把这类知识对象报出来。重建板块不会删掉底层的知识对象,所以只要那些对象还在，
// 这一行每次重算都会照样出现。承诺它会自己消失，是让人白等一次整理。
const SOURCE_GONE_TOOLTIP =
  "原始来源已经从库里删掉了，但它带进来的知识对象还在。除非把这些对象也清理掉，这一行每次重新整理后都会照常出现。";

/** 来源画像里指向已删除来源的那一行(标题为空到底是「没标题」还是「已删除」)。 */
export function analysisSourceRowAnomalies(row: { source_missing: boolean }): Anomaly[] {
  if (!row.source_missing) return [];
  return [{ severity: "info", label: SOURCE_GONE_LABEL, tooltip: SOURCE_GONE_TOOLTIP }];
}

// severity 之间的展示优先级:integrity 排最前,info 排最后。同档内保持映射表
// 里各条目原本被 push 的顺序(稳定排序)。
const SEVERITY_RANK: Record<AnomalySeverity, number> = {
  integrity: 0,
  retrieval: 1,
  info: 2,
};

// 来源行/详情共用:返回该来源要展示的异常(0..N 条,稳定顺序 integrity 优先)。
//
// 同时接受 parse_status 与 status 两个字段:来源行的 status-dot 现有渲染逻辑
// 是 `source.parse_status || source.status`(parse_status 缺失时回退 status),
// 这里保持同一套回退规则,避免 status-dot 判定"失败"而异常徽标判定"没失败"
// 的不一致。
export function sourceAnomalies(source: {
  parse_status?: string | null;
  status?: string | null;
  extraction_warning?: string | null;
  parse_quality_warning?: boolean;
  indexing_chunk_fallback?: boolean;
  paper_meta_status?: string | null;
}): Anomaly[] {
  const anomalies: Anomaly[] = [];

  const effectiveParseStatus = source.parse_status || source.status;
  if (effectiveParseStatus === "failed") {
    anomalies.push({
      severity: "integrity",
      label: PARSE_FAILED_LABEL,
      tooltip: PARSE_FAILED_TOOLTIP,
    });
  }

  if (source.extraction_warning) {
    anomalies.push({
      severity: "retrieval",
      label: EXTRACTION_WARNING_LABEL,
      tooltip: source.extraction_warning,
    });
  }

  if (source.parse_quality_warning) {
    anomalies.push({
      severity: "retrieval",
      label: PARSE_QUALITY_WARNING_LABEL,
      tooltip: PARSE_QUALITY_WARNING_TOOLTIP,
    });
  }

  if (source.indexing_chunk_fallback) {
    anomalies.push({
      severity: "retrieval",
      label: INDEXING_CHUNK_FALLBACK_LABEL,
      tooltip: INDEXING_CHUNK_FALLBACK_TOOLTIP,
    });
  }

  if (source.paper_meta_status === "missing") {
    anomalies.push({
      severity: "info",
      label: PAPER_META_MISSING_LABEL,
      tooltip: PAPER_META_MISSING_TOOLTIP,
    });
  }

  if (source.paper_meta_status === "not_paper") {
    anomalies.push({
      severity: "info",
      label: PAPER_META_NOT_PAPER_LABEL,
      tooltip: PAPER_META_NOT_PAPER_TOOLTIP,
    });
  }

  return anomalies
    .map((anomaly, index) => ({ anomaly, index }))
    .sort((a, b) => SEVERITY_RANK[a.anomaly.severity] - SEVERITY_RANK[b.anomaly.severity] || a.index - b.index)
    .map(({ anomaly }) => anomaly);
}
