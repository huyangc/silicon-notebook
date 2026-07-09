// 检索索引(scale retrieval index)客户端纯逻辑 —— 单测于 scale-index.test.mjs。
// 统一「看板卡片」与两处内联徽章的状态语义,避免六态 label/可点判断重复三份。

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
  delta_searchable?: boolean;
  last_built_at?: string;
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
  // 徽章点击 / 看板卡主按钮的主动作;indexed 与忙碌态为 null(indexed 只提供全量重建)。
  primaryOp: "build" | "update" | null;
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
  // 既不达标又没索引 = 小库,走暴力检索,「不需要」索引(纯信息,无动作)。
  const applicable = s.eligible || s.exists;
  const stateLabel = !applicable ? "不需要" : STATE_LABELS[state];
  const tone: ScaleIndexTone = !applicable ? "muted" : state === "indexed" ? "ok" : "warn";
  const primaryOp: "build" | "update" | null =
    !applicable || busy
      ? null
      : state === "unindexed" || state === "suggested"
        ? "build"
        : state === "stale"
          ? "update"
          : null;
  const canRebuild = s.exists && !busy;
  return { state, stateLabel, tone, primaryOp, canRebuild };
}

// 每个动作的确认文案 —— 描述具体精确,并诚实说明 update 会在何种条件下自动转全量。
export function scaleIndexOpConfirm(op: ScaleIndexOp, s: ScaleIndexStatus): string {
  if (op === "build") {
    return "构建检索索引？\n\n从零为本库构建向量检索索引（CSR 图 + KG/chunk ANN），加速语义检索与严格推理。后台进行，大库可能数分钟。";
  }
  if (op === "update") {
    const n = s.unindexed_sources ?? 0;
    const what = n > 0 ? `把 ${n} 个新增来源` : "把新增/变更内容";
    return `更新检索索引？\n\n${what}增量收进现有索引。后台进行，通常较快；新增来源过多或运行时切换向量维度时，会自动转为整体重建。`;
  }
  return "全量重建检索索引？\n\n删除现有索引并从头重建整套（CSR 图 + KG/chunk ANN）。后台进行，大库可能数分钟；期间检索走暴力回退。";
}
