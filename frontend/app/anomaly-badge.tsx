import { AlertOctagon, AlertTriangle, Clock, type LucideIcon } from "lucide-react";
import type { Anomaly, AnomalySeverity } from "./anomaly-severity";

// severity → 图标,与 anomaly-severity.ts 的三档分类一一对应(anomaly-tiers spec)。
const SEVERITY_ICON: Record<AnomalySeverity, LucideIcon> = {
  integrity: AlertOctagon,
  retrieval: AlertTriangle,
  info: Clock,
};

// 唯一渲染路径:所有异常小字都经这个组件出,不在调用处再手搓 class/内联样式。
// block=true 用于来源详情等需要换行显示的场景(.anomaly-badge--block)。
export function AnomalyBadge({ anomaly, block }: { anomaly: Anomaly; block?: boolean }) {
  const Icon = SEVERITY_ICON[anomaly.severity];
  const className = `anomaly-badge anomaly-badge--${anomaly.severity}${block ? " anomaly-badge--block" : ""}`;
  // 详情/块模式把 tooltip 原文**可见地**渲染出来(收 codex 第2轮 P2):原生 title
  // 在触屏不可用、span 也不可聚焦,来源详情不能把失败比例/补救指引只藏进 title——
  // 那会让触屏/键盘用户丢失原先详情里可见的信息。紧凑行(非 block)信息密度高,
  // 保留 title 兜底即可,点开详情就能看到全文。
  const detail = block && anomaly.tooltip && anomaly.tooltip !== anomaly.label ? anomaly.tooltip : null;
  return (
    <span className={className} title={detail ? undefined : anomaly.tooltip}>
      <Icon size={13} aria-hidden="true" />
      <span className="anomaly-badge-text">{anomaly.label}</span>
      {detail && <span className="anomaly-badge-detail">{detail}</span>}
    </span>
  );
}
