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

const PAPER_META_MISSING_LABEL = "待补全";
const PAPER_META_MISSING_TOOLTIP = "论文作者/机构等信息尚未补全";

const PAPER_META_NOT_PAPER_LABEL = "非论文";
const PAPER_META_NOT_PAPER_TOOLTIP = "该来源非学术论文，无需补全";

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
