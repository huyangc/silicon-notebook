// 检索索引(scale retrieval index)客户端纯逻辑 —— 单测于 scale-index.test.mjs。
// 统一「看板卡片」与两处内联徽章的状态语义,避免六态 label/可点判断重复三份。

// 值导入须带 .ts 后缀:本模块被 node --test 直接加载,node 的 TS loader 不做无后缀解析。
import { groupLabel } from "./ask-modes.ts";

// 提问模式的显示名只有 ask-modes.ts 一个真源,散文里插值引用(不写死中文字面量)。
const STRICT_LABEL = groupLabel("strict");

export type ScaleIndexState =
  | "unindexed" | "suggested" | "queued" | "building" | "indexed" | "stale";

export type ScaleIndexStatus = {
  exists: boolean;
  stale: boolean;
  building: boolean;
  eligible: boolean;
  state?: ScaleIndexState;
  delta_chunks?: number;
  unindexed_sources?: number;
  has_unindexed_content?: boolean;
  delta_searchable?: boolean;
  last_built_at?: string;
  // 排队态透明化(空闲时段自动构建):均为可选,兼容缺这些字段的旧后端。
  queue_position?: number;
  queue_length?: number;
  queued_at?: string;
  offpeak_in_window?: boolean;
  offpeak_next_start_at?: string;
  last_build_ms?: number;
  n_nodes: number;
  n_chunks: number;
  n_ann: number;
  n_chunk_ann: number;
  has_chunk_ann: boolean;
};

// 三个「精确」动作 —— 与后端 rebuild 端点的 mode 一一对应:
// build  = 无索引时从零构建(full)
// update = 已过期时把新增来源增量收进现有索引(fold)
// rebuild= 删除现有索引、从头全量重建(full)
export type ScaleIndexOp = "build" | "update" | "rebuild";

export const SCALE_OP_MODE: Record<ScaleIndexOp, "fold" | "full"> = {
  build: "full",
  update: "fold",
  rebuild: "full",
};

export type ScaleIndexTone = "ok" | "warn" | "muted";

export type ScaleIndexView = {
  state: ScaleIndexState;
  stateLabel: string;
  tone: ScaleIndexTone;
  // 徽章点击 / 看板卡主按钮的主动作。stale 时按有无「可增量的新增来源」分流:
  //   有新增来源 → "update"(fold 增量收进);无(过期由图谱/概念变更等非-source 写入引起)
  //   → "rebuild"(fold 只认新增 source、会空转且仍过期,只有全量重建能清过期)。
  //   indexed 与忙碌态为 null(indexed 只提供全量重建 canRebuild)。
  primaryOp: "build" | "update" | "rebuild" | null;
  // 全量重建入口:任意「已建成」索引且非忙碌时可用(仅看板卡片暴露)。
  canRebuild: boolean;
};

const STATE_LABELS: Record<ScaleIndexState, string> = {
  building: "构建中…",
  queued: "已排队（空闲时建）",
  indexed: "最新",
  stale: "已过期",
  suggested: "建议构建",
  unindexed: "未构建",
};

export function describeScaleIndex(s: ScaleIndexStatus): ScaleIndexView {
  const state: ScaleIndexState =
    s.state ?? (s.building ? "building" : s.exists ? (s.stale ? "stale" : "indexed") : "unindexed");
  const busy = s.building || state === "building" || state === "queued";
  const hasUnindexedContent =
    s.has_unindexed_content ?? (s.unindexed_sources ?? 0) > 0;
  // 既不达标又没索引 = 小库,走暴力检索,「不需要」索引(纯信息,无动作)。
  const applicable = s.eligible || s.exists;
  const stateLabel = !applicable ? "不需要" : STATE_LABELS[state];
  const tone: ScaleIndexTone = !applicable ? "muted" : state === "indexed" ? "ok" : "warn";
  const primaryOp: "build" | "update" | "rebuild" | null =
    !applicable || busy
      ? null
      : state === "unindexed" || state === "suggested"
        ? "build"
        : state === "stale"
          // 过期但没有可增量的新增来源(过期由图谱/概念变更等非-source 写入引起)→ fold 会
          // 命中后端空操作守卫、白点且仍过期,故主按钮直接给全量重建;有新增来源才走 update(fold)。
          ? (hasUnindexedContent ? "update" : "rebuild")
          : null;
  const canRebuild = s.exists && !busy;
  return { state, stateLabel, tone, primaryOp, canRebuild };
}

// 「还有 N 个来源没进索引」徽章的悬浮说明 —— 来源栏与图谱侧栏两处共用。
//
// ⚠抽成常量不是为了省字:这串原本在 page.tsx 里各写一份,于是散文重写把统称
// 修成「知识对象」时只改到一处,另一处继续挂着「概念」——把 claim / formula /
// procedure 和 knowhow 自定义类型集体降格(见 docs/ui-vocabulary.md「解释与例外」)。
// 两份字面量正是它能漂移的原因,所以这里只留一份,并由
// kg-object-vocabulary.test.mjs 钉住:统称串里不许出现任何一个具体类型名。
export const UNINDEXED_SCOPE_HINT =
  "未索引部分不参与检索与推理（段落、知识对象、关联、图谱）；点「更新索引」或等待自动收进后可见";

// index_required is persisted with an answer and therefore describes the
// retrieval state at answer time.  A live notebook status is authoritative
// once an index has since been published; otherwise every historical answer
// would keep showing the degraded banner after a successful build.
export function shouldShowIndexRequiredBanner(
  answerIndexRequired: boolean | undefined,
  scaleStatus: Pick<ScaleIndexStatus, "exists"> | null | undefined,
): boolean {
  return Boolean(answerIndexRequired && !scaleStatus?.exists);
}

// The pending-action stream is the durable completion signal after the
// foreground 20-minute polling window has ended.  Refresh only the active
// notebook: completion events for other notebooks must not overwrite its
// live retrieval status.
export function shouldRefreshScaleIndexFromDone(
  event: { notebook_id: string; kind?: string } | null | undefined,
  activeNotebookId: string | null | undefined,
): boolean {
  return Boolean(
    event?.kind === "index_done"
      && activeNotebookId
      && event.notebook_id === activeNotebookId,
  );
}

// doneItems retains every SSE completion event, unlike the single toast value
// that React may overwrite when several messages are batched in one render.
// Items are newest-first, so the first active-notebook index event is the one
// whose publication should wake the live status refresh.
export function latestScaleIndexDoneForNotebook<T extends {
  notebook_id: string;
  kind?: string;
}>(
  events: readonly T[],
  activeNotebookId: string | null | undefined,
): T | null {
  return events.find((event) =>
    shouldRefreshScaleIndexFromDone(event, activeNotebookId)
  ) ?? null;
}

export function latestScaleIndexDoneKey(
  events: readonly { notebook_id: string; kind?: string; ts: number }[],
  activeNotebookId: string | null | undefined,
): string {
  const event = latestScaleIndexDoneForNotebook(events, activeNotebookId);
  return event ? `${event.notebook_id}:${event.ts}` : "";
}

// A queued operation is still replaceable by an explicit immediate request.
// Choose the same exact mode that the non-busy primary action would use.
export function queuedScaleIndexImmediateOp(s: ScaleIndexStatus): ScaleIndexOp {
  if (!s.exists) return "build";
  return (s.has_unindexed_content ?? (s.unindexed_sources ?? 0) > 0)
    ? "update"
    : "rebuild";
}

function sameLocalDate(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear()
    && a.getMonth() === b.getMonth()
    && a.getDate() === b.getDate();
}

// 上次构建耗时的档位文案:<1min→「1 分钟内」;<1h→按分钟;否则按「H 小时 M 分」。
function formatBuildDuration(ms: number): string {
  const seconds = ms / 1000;
  if (seconds < 60) return "1 分钟内";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  const remMinutes = minutes % 60;
  return `${hours} 小时 ${remMinutes} 分`;
}

// 排队态说明 —— 看板卡片、来源栏内联徽章共用。按可得字段逐段拼接空闲窗口开始
// 时刻/上次构建耗时;三段全缺(旧后端未下发新字段)时保留原有默认文案,优雅降级。
//
// ⚠「排在第 N 位」是假串行语义:低峰窗口一开,_process_idle_queue 对队列每一项
// 并发启动构建,位次不代表等待顺序 —— 因此这里只报队列长度(有意义时),不报位次。
// queue_position 字段仍保留在类型里(后端仍下发、也许有别的用途),文案不消费它。
export function queuedScheduleHint(s: ScaleIndexStatus, now: Date): string {
  const parts: string[] = [];
  if (s.offpeak_in_window) {
    parts.push("已进入空闲时段，即将开始");
  } else if (s.offpeak_next_start_at) {
    const start = new Date(s.offpeak_next_start_at);
    if (!Number.isNaN(start.getTime())) {
      if (start.getTime() <= now.getTime()) {
        // 时刻已过(排队等待超过状态轮询窗口、快照未刷新):此刻可能已在窗口内,
        // 也可能整个窗口都已结束(比如 07:00 看 02:00–06:00 的旧快照)——两种都
        // 无法从过期快照分辨,所以既不承诺「预计 … 后开始」也不谎称「即将开始」,
        // 直接省略窗口分段、落回中性文案(codex R1/R2 两条 P2)。
      } else {
        const dayLabel = sameLocalDate(start, now) ? "今天" : "明天";
        const hh = String(start.getHours()).padStart(2, "0");
        const mm = String(start.getMinutes()).padStart(2, "0");
        parts.push(`预计${dayLabel} ${hh}:${mm} 后开始`);
      }
    }
  }
  if ((s.last_build_ms ?? 0) > 0) {
    parts.push(`上次构建约 ${formatBuildDuration(s.last_build_ms!)}`);
  }
  const prefix = (s.queue_length ?? 0) >= 2 ? `已排队（共 ${s.queue_length} 项）` : "已排队";
  if (parts.length === 0) {
    return `${prefix}，将在服务器空闲时构建；完成后自动更新`;
  }
  return `${prefix}，${parts.join("；")}`;
}

// 每个动作的确认文案 —— 描述具体精确,并诚实说明 update 会在何种条件下自动转全量。
export function scaleIndexOpConfirm(op: ScaleIndexOp, s: ScaleIndexStatus): string {
  if (op === "build") {
    return `建立检索索引？\n\n为本笔记本的内容建立快速查找结构，加速语义检索与${STRICT_LABEL}。后台进行，内容较多时可能需要数分钟。`;
  }
  if (op === "update") {
    const n = s.unindexed_sources ?? 0;
    const what = n > 0 ? `把 ${n} 个新增来源` : "把新增/变更内容";
    return `更新检索索引？\n\n${what}增量收进现有索引。后台进行，通常较快；新增来源过多或运行时切换向量维度时，会自动转为整体重建。`;
  }
  return "全量重建检索索引？\n\n删除现有索引并从头重建。后台进行，内容较多时可能需要数分钟；期间检索会走较慢的直接搜索。";
}
