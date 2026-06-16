// 前端用户可选 ask mode 的单一真源（镜像后端 app/services/ask_modes.py 的
// user_facing 子集；由 scripts/check_ask_modes_contract.py 锁同步）。
// 全前端唯一出现 mode 字面量的地方——其余代码只引用本文件。

export type AskModeId = "chunk" | "reasoning" | "graph";
export type AskModeGroup = "general" | "strict";

export interface AskModeDef {
  id: AskModeId;
  group: AskModeGroup;
  label: string;
  desc: string;
  requiresKg: boolean;
  groupDefault?: boolean; // 组内默认引擎
}

export const ASK_MODES: AskModeDef[] = [
  { id: "chunk", group: "general", label: "通用问答",
    desc: "默认。大范围检索原文，适合综述、对比、找事实。", requiresKg: false, groupDefault: true },
  { id: "reasoning", group: "strict", label: "深挖推理",
    desc: "agent 多轮深挖，展示思考轨迹。", requiresKg: true, groupDefault: true },
  { id: "graph", group: "strict", label: "图谱多跳",
    desc: "沿知识图谱多跳遍历，展示关联子图。", requiresKg: true },
];

export const DEFAULT_ASK_MODE: AskModeId = "chunk";

export const ASK_MODE_GROUPS: { id: AskModeGroup; label: string }[] = [
  { id: "general", label: "通用问答" },
  { id: "strict", label: "严格推理" },
];

export function askModeIds(): AskModeId[] {
  return ASK_MODES.map((m) => m.id);
}

function defOf(id: AskModeId): AskModeDef {
  const d = ASK_MODES.find((m) => m.id === id);
  if (!d) throw new Error(`unknown ask mode: ${id}`);
  return d;
}

export function groupOf(id: AskModeId): AskModeGroup {
  return defOf(id).group;
}

export function modesInGroup(group: AskModeGroup): AskModeDef[] {
  return ASK_MODES.filter((m) => m.group === group);
}

export function defaultModeForGroup(group: AskModeGroup): AskModeId {
  const d = modesInGroup(group).find((m) => m.groupDefault) ?? modesInGroup(group)[0];
  if (!d) throw new Error(`no mode in group: ${group}`);
  return d.id;
}

export function requiresKg(id: AskModeId): boolean {
  return defOf(id).requiresKg;
}

export function canUseMode(id: AskModeId, kgReady: boolean): boolean {
  return kgReady || !requiresKg(id);
}

// 按上一轮 turn.response.mode 精确恢复（含引擎）；非 user-facing/缺失 → 兜底默认。
export function modeFromTurn(
  turn: { response?: { mode?: string } } | undefined,
): AskModeId {
  const m = turn?.response?.mode;
  return m && (askModeIds() as string[]).includes(m) ? (m as AskModeId) : DEFAULT_ASK_MODE;
}
