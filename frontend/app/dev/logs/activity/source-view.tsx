"use client";
// 「活动」视图里与来源有关的共享零件:左栏来源清单、中栏活动流、右栏详情三处
// 都要显示同一批异常小字,也都要把来源折成同一种形状。放在这里是为了让那两件事
// 各只有一份实现。

import { AnomalyBadge } from "../../../anomaly-badge.tsx";
import { sourceAnomalies } from "../../../anomaly-severity.ts";
import type { SourceSummary } from "../../../workspace-model.ts";
import type { ActivitySource } from "./types";

/**
 * 来源清单接口返回的 `SourceSummary` → 活动流条目的 `ActivitySource`。
 *
 * 左栏点一条来源和中栏点一条来源活动，右栏要显示的是同一件东西，所以两个入口在
 * 这里收敛成同一种形状，右栏详情只写一份。
 *
 * ⚠ 显示名取后端合成好的 `display_title`，**不是**原始的 `title` 列，也**不得**在
 * 这里补 `|| file_name`。
 *
 * `source_display_title()`（backend/app/services/source_display.py）是所有「为用户
 * 命名来源」的路径共用的唯一真源：论文标题优先，其次 `title`，两者都空白才回落
 * `file_name`（`title` 为纯空白时刻意让它遮蔽 file_name、返回空串——那代表「这份
 * 来源就是没有显示名」）。前端拿原始 `title` 自己拼，已接地的论文就会在左栏叫
 * `1706.03762.pdf`、在中栏叫《Attention Is All You Need》，正是 `docs/product-and-api.md` 契约要防的
 * 「同一篇论文在同一屏里有两个名字」。空标题的占位符由 format.ts::activityTitle 在
 * `display_title` **已经确定为空之后**统一给。
 *
 * `created_at` 取来源清单接口的**原始 ISO 时间戳**，右栏与中栏因此逐字显示同一个
 * 时刻。⚠ 不要改回 `created_label`：那是服务端按**服务端**日历日格式化好的字符串，
 * 服务端 UTC+8、浏览器 UTC 时，同一份来源左栏点是「8月5日」、中栏点是「8月4日
 * 17:00」——而设计稿写的是「时间一律显示浏览器本地时区」。右栏也因此不再接受
 * `createdLabel` 覆盖：留着那个口，就等于留着第二种时间格式。
 */
export function toActivitySource(
  source: SourceSummary,
  notebookId: string,
): ActivitySource {
  return {
    type: "source",
    id: source.id,
    notebook_id: source.notebook_id || notebookId,
    created_at: source.created_at,
    display_title: source.display_title,
    file_name: source.file_name,
    source_type: source.type,
    parse_status: source.parse_status,
    status: source.status,
    // 布尔化而不是搬运诊断原文（见 types.ts::ActivitySource.parse_failed）。
    // 来源清单响应里没有这一位时按解析状态推定——判据与紧挨着它渲染的
    // 「解析失败」异常小字（anomaly-severity.ts::sourceAnomalies）逐字相同，
    // 免得同一行的徽标与详情文案互相矛盾。
    parse_failed: source.parse_failed ?? (source.parse_status || source.status) === "failed",
    extraction_warning: source.extraction_warning ?? "",
    parse_quality_warning: Boolean(source.parse_quality_warning),
    paper_meta_status: source.paper_meta_status ?? "",
    notebook_name: "",
    notebook_deleted_at: "",
    retained_until: "",
  };
}

/**
 * 来源的异常小字。唯一渲染路径是 `AnomalyBadge` + `sourceAnomalies()`
 * （见 `docs/development.md`：不得手搓内联样式或裸警示符号，回归门 anomaly-guard.test.mjs）。
 */
export function SourceAnomalies({
  source,
  block,
}: {
  source: ActivitySource;
  block?: boolean;
}) {
  const anomalies = sourceAnomalies(source);
  if (anomalies.length === 0) return null;
  return (
    <div className={`activity-anomalies${block ? " block" : ""}`}>
      {anomalies.map((anomaly) => (
        <AnomalyBadge
          key={`${anomaly.severity}-${anomaly.label}`}
          anomaly={anomaly}
          block={block}
        />
      ))}
    </div>
  );
}
