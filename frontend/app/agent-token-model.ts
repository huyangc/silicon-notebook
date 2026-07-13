export const AGENT_SCOPE_OPTIONS = [
  { value: "knowledge:read", label: "读取 Knowledge" },
  { value: "memory:read", label: "读取已确认 Memory" },
  { value: "memory:read_candidates", label: "读取待确认候选" },
  { value: "memory:propose", label: "主动提交候选 Memory" },
  { value: "ask:execute", label: "执行 Notebook Ask" },
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
    expires_at: draft.expires_at || null,
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
