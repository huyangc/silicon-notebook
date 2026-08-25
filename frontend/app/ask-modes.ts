// Built-in Ask modes remain a static cross-stack truth. Deployment engines are
// sanitized from GET /ask-modes and merged at runtime; no plugin id is compiled
// into the browser bundle.

export type AskModeId = "chunk" | "reasoning" | "graph";
export type AskModeGroup = "general" | "strict" | "extension";

export interface AskModeDef {
  id: string;
  group: AskModeGroup;
  label: string;
  desc: string;
  requiresKg: boolean;
  streamsTrace: boolean;
  groupDefault?: boolean;
}

export type AskModeProjection = Readonly<{
  id?: unknown;
  group?: unknown;
  label?: unknown;
  desc?: unknown;
  requires_kg?: unknown;
  streaming?: unknown;
  streams_trace?: unknown;
}>;

export const ASK_MODES: AskModeDef[] = [
  { id: "chunk", group: "general", label: "通用问答",
    desc: "默认。大范围检索原文，适合综述、对比、找事实。",
    requiresKg: false, streamsTrace: false, groupDefault: true },
  { id: "reasoning", group: "strict", label: "逐步推理",
    desc: "像人查资料一样逐层追问，展示推理过程；适合需要一步步查证的复杂问题。",
    requiresKg: true, streamsTrace: true, groupDefault: true },
  { id: "graph", group: "strict", label: "关联追溯",
    desc: "顺着资料之间的关联往外找，列出牵连到的内容；适合理清一件事的来龙去脉。",
    requiresKg: true, streamsTrace: false },
];

export const DEFAULT_ASK_MODE: AskModeId = "chunk";

export const ASK_MODE_GROUPS: { id: AskModeGroup; label: string }[] = [
  { id: "general", label: "通用问答" },
  { id: "strict", label: "深入分析" },
  { id: "extension", label: "扩展功能" },
];

const BUILTIN_IDS = new Set(ASK_MODES.map((mode) => mode.id));
const DEPLOYMENT_MODE_ID = /^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$/;

export function normalizeAskModeProjection(value: unknown): AskModeDef[] {
  if (!Array.isArray(value)) return ASK_MODES;
  const extensions: AskModeDef[] = [];
  const seen = new Set<string>(BUILTIN_IDS);
  for (const raw of value as AskModeProjection[]) {
    if (!raw || typeof raw !== "object") continue;
    const id = raw.id;
    const label = raw.label;
    const desc = raw.desc;
    if (
      typeof id !== "string"
      || !id.includes(".")
      || !DEPLOYMENT_MODE_ID.test(id)
      || seen.has(id)
      || raw.group !== "extension"
      || typeof label !== "string"
      || !label.trim()
      || typeof desc !== "string"
      || !desc.trim()
      || typeof raw.requires_kg !== "boolean"
      || raw.streams_trace !== false
      || (raw.streaming !== undefined && raw.streaming !== false)
    ) continue;
    seen.add(id);
    extensions.push({
      id,
      group: "extension",
      label,
      desc,
      requiresKg: raw.requires_kg,
      streamsTrace: false,
      groupDefault: extensions.length === 0,
    });
  }
  return extensions.length ? [...ASK_MODES, ...extensions] : ASK_MODES;
}

export function askModeIds(modes: readonly AskModeDef[] = ASK_MODES): string[] {
  return modes.map((mode) => mode.id);
}

function defOf(id: string, modes: readonly AskModeDef[] = ASK_MODES): AskModeDef | undefined {
  return modes.find((mode) => mode.id === id);
}

export function modeLabel(id: string, modes: readonly AskModeDef[] = ASK_MODES): string {
  const mode = defOf(id, modes);
  if (!mode) throw new Error(`unknown ask mode: ${id}`);
  return mode.label;
}

export function groupLabel(id: AskModeGroup): string {
  const group = ASK_MODE_GROUPS.find((candidate) => candidate.id === id);
  if (!group) throw new Error(`unknown ask mode group: ${id}`);
  return group.label;
}

export function askModeLabels(): string[] {
  return [...ASK_MODES.map((mode) => mode.label), ...ASK_MODE_GROUPS.map((group) => group.label)];
}

export function groupOf(
  id: string,
  modes: readonly AskModeDef[] = ASK_MODES,
): AskModeGroup {
  return defOf(id, modes)?.group ?? "general";
}

export function modesInGroup(
  group: AskModeGroup,
  modes: readonly AskModeDef[] = ASK_MODES,
): AskModeDef[] {
  return modes.filter((mode) => mode.group === group);
}

export function defaultModeForGroup(
  group: AskModeGroup,
  modes: readonly AskModeDef[] = ASK_MODES,
): string {
  const choices = modesInGroup(group, modes);
  return (choices.find((mode) => mode.groupDefault) ?? choices[0])?.id ?? DEFAULT_ASK_MODE;
}

export function requiresKg(
  id: string,
  modes: readonly AskModeDef[] = ASK_MODES,
): boolean {
  return defOf(id, modes)?.requiresKg ?? false;
}

export function streamsTrace(
  id: string,
  modes: readonly AskModeDef[] = ASK_MODES,
): boolean {
  return defOf(id, modes)?.streamsTrace ?? false;
}

export function canUseMode(
  id: string,
  kgReady: boolean,
  modes: readonly AskModeDef[] = ASK_MODES,
): boolean {
  return kgReady || !requiresKg(id, modes);
}

export function modeFromTurn(
  turn: { response?: { mode?: string } } | undefined,
  modes: readonly AskModeDef[] = ASK_MODES,
): string {
  const mode = turn?.response?.mode;
  return mode && askModeIds(modes).includes(mode) ? mode : DEFAULT_ASK_MODE;
}
