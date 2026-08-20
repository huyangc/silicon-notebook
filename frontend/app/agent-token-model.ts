import { humanizedError } from "./errors.ts";

export const AGENT_SCOPE_OPTIONS = [
  { value: "knowledge:read", label: "读取知识库" },
  { value: "memory:read", label: "读取已确认记忆" },
  { value: "memory:read_candidates", label: "读取待确认候选" },
  { value: "memory:propose", label: "主动提交候选记忆" },
  { value: "ask:execute", label: "执行笔记本问答" },
  { value: "knowhow:code", label: "Knowhow 代码附件写入" },
  { value: "sources:write", label: "添加/重新解析来源" },
  { value: "sources:delete", label: "删除 Agent 添加的来源" },
  { value: "maintenance:execute", label: "触发图谱分析与检索索引构建" },
  { value: "agent_profile:read", label: "读取对这个库的理解" },
  { value: "agent_observation:write", label: "写入使用线索" },
] as const;

export const AGENT_ACCESS_PAGE_SIZE = 25;

export type AgentScope = (typeof AGENT_SCOPE_OPTIONS)[number]["value"];

export type AgentTokenDraft = {
  default_notebook_id: string;
  notebook_ids: string[];
  scopes: string[];
  expires_at: string;
};

export function agentTokenDraft(defaultNotebookId = ""): AgentTokenDraft {
  return {
    default_notebook_id: defaultNotebookId,
    notebook_ids: defaultNotebookId ? [defaultNotebookId] : [],
    scopes: ["knowledge:read", "memory:read"],
    expires_at: "",
  };
}

export function localDateTimeToUtcIso(
  value: string,
  timezoneOffsetMinutes?: number,
): string {
  // 这两句是写给用户的场景文案(不是诊断串),所以走 humanizedError 盖章:
  // 裸 new Error 的话,memory-panel 的 catch 过 toUserMessage 时认不出它已经
  // 安全化,会压成通用兜底,用户就不知道是「过期时间」这一栏填错了。
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value);
  if (!match) throw humanizedError("过期时间格式无效");
  if (timezoneOffsetMinutes === undefined) {
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) throw humanizedError("过期时间格式无效");
    return parsed.toISOString();
  }
  const [, year, month, day, hour, minute, second = "0"] = match;
  const utcMillis = Date.UTC(
    Number(year),
    Number(month) - 1,
    Number(day),
    Number(hour),
    Number(minute),
    Number(second),
  ) + timezoneOffsetMinutes * 60_000;
  return new Date(utcMillis).toISOString();
}

export function agentTokenRequest(profileId: string, draft: AgentTokenDraft) {
  const notebookIds = Array.from(new Set([
    draft.default_notebook_id,
    ...draft.notebook_ids,
  ].filter(Boolean)));
  return {
    agent_profile_id: profileId,
    scopes: Array.from(new Set(draft.scopes)),
    default_notebook_id: draft.default_notebook_id,
    notebook_ids: notebookIds,
    expires_at: draft.expires_at ? localDateTimeToUtcIso(draft.expires_at) : null,
  };
}

export function canIssueAgentToken(profileId: string, draft: AgentTokenDraft): boolean {
  return Boolean(
    profileId
    && draft.default_notebook_id
    && draft.notebook_ids.includes(draft.default_notebook_id)
    && draft.scopes.length
    && draft.expires_at,
  );
}

export function agentPagePath(path: string, offset: number): string {
  const params = new URLSearchParams({
    offset: String(Math.max(0, offset)),
    limit: String(AGENT_ACCESS_PAGE_SIZE),
  });
  return `${path}?${params.toString()}`;
}

export function agentPageHasMore(page: readonly unknown[]): boolean {
  return page.length === AGENT_ACCESS_PAGE_SIZE;
}

export function mergeAgentPage<T extends { id: string }>(
  current: readonly T[],
  page: readonly T[],
): T[] {
  const incoming = new Map(page.map((item) => [item.id, item]));
  const merged = current.map((item) => incoming.get(item.id) ?? item);
  const known = new Set(current.map((item) => item.id));
  for (const item of page) {
    if (!known.has(item.id)) {
      merged.push(item);
      known.add(item.id);
    }
  }
  return merged;
}
